from __future__ import annotations

import json
import os
import re
from typing import Any

import psycopg2
from psycopg2.extras import execute_values

from config import (
    DB_APPLICATION_NAME,
    DB_CONNECT_TIMEOUT,
    DB_HOST,
    DB_NAME,
    DB_PASSWORD,
    DB_PORT,
    DB_SSLMODE,
    DB_USER,
    FINAL_TRAIL_DATASET_PATH,
    NODE_ELEVATION_PATH,
    POSTGIS_SCHEMA,
    SUMMIT_POINTS_PATH,
)
from pipeline import read_geojson_features


_VALID_PG_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def validate_pg_identifier(value: str, label: str) -> str:
    """스키마명 같은 SQL 식별자를 최소 검증한다."""
    if not value or not _VALID_PG_IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} 값이 올바르지 않습니다: {value!r}")
    return value


def env_flag(name: str, default: bool = False) -> bool:
    """환경변수를 bool로 해석한다."""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


POSTGIS_SCHEMA_SAFE = validate_pg_identifier(POSTGIS_SCHEMA, "POSTGIS_SCHEMA")
ORDA_ENV = os.getenv("ORDA_ENV", "local").strip().lower()
ORDA_ALLOW_DESTRUCTIVE_LOAD = env_flag("ORDA_ALLOW_DESTRUCTIVE_LOAD", False)


def postgis_fn(name: str) -> str:
    """PostGIS 함수명을 스키마 포함 형태로 만든다."""
    return f"{POSTGIS_SCHEMA_SAFE}.{name}"


def build_search_path() -> str:
    """public + PostGIS 스키마를 search_path로 구성한다."""
    schemas = ["public"]
    if POSTGIS_SCHEMA_SAFE not in schemas:
        schemas.append(POSTGIS_SCHEMA_SAFE)
    return ",".join(schemas)


def get_connection():
    """PostgreSQL / Supabase 접속 연결을 반환한다."""
    kwargs = {
        "host": DB_HOST,
        "port": DB_PORT,
        "dbname": DB_NAME,
        "user": DB_USER,
        "password": DB_PASSWORD,
        "connect_timeout": DB_CONNECT_TIMEOUT,
        "application_name": DB_APPLICATION_NAME,
        "options": f"-c search_path={build_search_path()}",
    }

    if DB_SSLMODE:
        kwargs["sslmode"] = DB_SSLMODE

    return psycopg2.connect(**kwargs)


# ──────────────────────────────────────────────
# 유틸
# ──────────────────────────────────────────────

def table_exists(cursor, table_name: str) -> bool:
    """public 스키마에 해당 테이블이 존재하는지 확인한다."""
    cursor.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name = %s
        )
        """,
        (table_name,),
    )
    return cursor.fetchone()[0]


def ensure_required_tables(cursor) -> None:
    """필수 테이블 존재 여부를 확인한다."""
    required = ["trail_nodes", "trail_edges", "summit_points"]
    missing = [table for table in required if not table_exists(cursor, table)]

    if missing:
        raise RuntimeError(
            "필수 테이블이 없습니다. 먼저 스키마를 생성하세요. "
            f"누락 테이블: {', '.join(missing)}"
        )


def ensure_destructive_load_allowed() -> None:
    """파괴적 적재가 허용된 환경인지 확인한다."""
    if ORDA_ENV == "prod":
        raise RuntimeError(
            "ORDA_ENV=prod 환경에서는 파괴적 적재를 실행할 수 없습니다."
        )

    if not ORDA_ALLOW_DESTRUCTIVE_LOAD:
        raise RuntimeError(
            "파괴적 적재가 차단되었습니다. "
            "실행하려면 ORDA_ALLOW_DESTRUCTIVE_LOAD=true 를 명시하세요."
        )


def reset_static_tables(cursor) -> None:
    """
    정적 테이블만 순차 초기화한다.

    주의:
    - summit_verifications 같은 사용자/서비스성 데이터는 건드리지 않는다.
    - CASCADE를 사용하지 않아 연쇄 삭제를 방지한다.
    - FK를 고려해 child → parent 순서로 초기화한다.
    """
    cursor.execute("TRUNCATE trail_edges RESTART IDENTITY")
    cursor.execute("TRUNCATE trail_nodes RESTART IDENTITY")
    cursor.execute("TRUNCATE summit_points RESTART IDENTITY")


def build_geom_expr(param_name: str = "geom") -> str:
    """GeoJSON 문자열을 geometry로 바꾸는 SQL 조각을 반환한다."""
    return (
        f"{postgis_fn('ST_SetSRID')}("
        f"{postgis_fn('ST_GeomFromGeoJSON')}(%({param_name})s), 4326)"
    )


# ──────────────────────────────────────────────
# 적재 함수
# ──────────────────────────────────────────────

def load_nodes(cursor, features: list[dict[str, Any]]) -> int:
    """node_with_elevation.geojson → trail_nodes 테이블에 배치 적재 시도한다."""
    sql = """
          INSERT INTO trail_nodes (
              node_id, node_type, degree,
              elevation_m, elevation_status, qa_status,
              geom
          ) VALUES %s
              ON CONFLICT (node_id) DO NOTHING \
          """

    template = (
        f"(%(node_id)s, %(node_type)s, %(degree)s, "
        f"%(elevation_m)s, %(elevation_status)s, %(qa_status)s, "
        f"{build_geom_expr('geom')})"
    )

    rows = []
    for feat in features:
        props = feat.get("properties", {})
        geom = feat.get("geometry")

        if not geom:
            continue
        if not props.get("node_id"):
            continue

        rows.append(
            {
                "node_id": props.get("node_id"),
                "node_type": props.get("node_type"),
                "degree": props.get("degree"),
                "elevation_m": props.get("elevation_m"),
                "elevation_status": props.get("elevation_status"),
                "qa_status": props.get("qa_status"),
                "geom": json.dumps(geom, ensure_ascii=False),
            }
        )

    if not rows:
        return 0

    execute_values(cursor, sql, rows, template=template, page_size=1000)
    return len(rows)


def load_summits(cursor, features: list[dict[str, Any]]) -> int:
    """summit_points.geojson → summit_points 테이블에 배치 적재 시도한다."""
    sql = """
          INSERT INTO summit_points (
              summit_id, name, elevation_m,
              source, radius_m,
              geom
          ) VALUES %s
              ON CONFLICT (summit_id) DO NOTHING \
          """

    template = (
        f"(%(summit_id)s, %(name)s, %(elevation_m)s, "
        f"%(source)s, %(radius_m)s, {build_geom_expr('geom')})"
    )

    rows = []
    for feat in features:
        props = feat.get("properties", {})
        geom = feat.get("geometry")

        if not geom:
            continue
        if not props.get("summit_id"):
            continue

        rows.append(
            {
                "summit_id": props.get("summit_id"),
                "name": props.get("name"),
                "elevation_m": props.get("elevation_m"),
                "source": props.get("source"),
                "radius_m": props.get("radius_m"),
                "geom": json.dumps(geom, ensure_ascii=False),
            }
        )

    if not rows:
        return 0

    execute_values(cursor, sql, rows, template=template, page_size=1000)
    return len(rows)


def load_edges(cursor, features: list[dict[str, Any]]) -> int:
    """final_trail_dataset.geojson → trail_edges 테이블에 배치 적재 시도한다."""
    sql = """
          INSERT INTO trail_edges (
              edge_id, source_gpx,
              start_node_id, end_node_id,
              distance_m, elevation_start_m, elevation_end_m,
              elevation_diff_m, slope_percent, difficulty_score, difficulty,
              surface, nearest_summit_id, qa_status,
              geom
          ) VALUES %s
              ON CONFLICT (edge_id) DO NOTHING \
          """

    template = (
        f"(%(edge_id)s, %(source_gpx)s, %(start_node_id)s, %(end_node_id)s, "
        f"%(distance_m)s, %(elevation_start_m)s, %(elevation_end_m)s, "
        f"%(elevation_diff_m)s, %(slope_percent)s, %(difficulty_score)s, %(difficulty)s, "
        f"%(surface)s, %(nearest_summit_id)s, %(qa_status)s, "
        f"{build_geom_expr('geom')})"
    )

    rows = []
    for feat in features:
        props = feat.get("properties", {})
        geom = feat.get("geometry")

        if not geom:
            continue
        if not props.get("edge_id"):
            continue

        rows.append(
            {
                "edge_id": props.get("edge_id"),
                "source_gpx": props.get("source_gpx"),
                "start_node_id": props.get("start_node_id"),
                "end_node_id": props.get("end_node_id"),
                "distance_m": props.get("distance_m"),
                "elevation_start_m": props.get("elevation_start_m"),
                "elevation_end_m": props.get("elevation_end_m"),
                "elevation_diff_m": props.get("elevation_diff_m"),
                "slope_percent": props.get("slope_percent"),
                "difficulty_score": props.get("difficulty_score"),
                "difficulty": props.get("difficulty"),
                "surface": props.get("surface"),
                "nearest_summit_id": props.get("nearest_summit_id"),
                "qa_status": props.get("qa_status"),
                "geom": json.dumps(geom, ensure_ascii=False),
            }
        )

    if not rows:
        return 0

    execute_values(cursor, sql, rows, template=template, page_size=1000)
    return len(rows)


# ──────────────────────────────────────────────
# 적재 후 검증
# ──────────────────────────────────────────────

def verify_counts(cursor) -> None:
    """적재 후 각 테이블 row 수를 출력한다."""
    tables = ["trail_nodes", "trail_edges", "summit_points"]

    print("\n[적재 검증]")
    for table in tables:
        cursor.execute(f"SELECT COUNT(*) FROM {table}")
        count = cursor.fetchone()[0]
        print(f"  {table}: {count}행")


def verify_spatial(cursor) -> None:
    """공간 데이터가 정상인지 간단히 확인한다."""
    print("\n[공간 데이터 검증]")

    cursor.execute("SELECT COUNT(*) FROM trail_nodes WHERE geom IS NULL")
    null_nodes = cursor.fetchone()[0]
    print(f"  trail_nodes geom NULL: {null_nodes}개")

    cursor.execute("SELECT COUNT(*) FROM trail_edges WHERE geom IS NULL")
    null_edges = cursor.fetchone()[0]
    print(f"  trail_edges geom NULL: {null_edges}개")

    cursor.execute("SELECT COUNT(*) FROM summit_points WHERE geom IS NULL")
    null_summits = cursor.fetchone()[0]
    print(f"  summit_points geom NULL: {null_summits}개")

    cursor.execute("SELECT ST_SRID(geom) FROM trail_nodes LIMIT 1")
    row = cursor.fetchone()
    if row:
        print(f"  trail_nodes SRID: {row[0]}")

    cursor.execute("SELECT ST_SRID(geom) FROM trail_edges LIMIT 1")
    row = cursor.fetchone()
    if row:
        print(f"  trail_edges SRID: {row[0]}")

    cursor.execute("SELECT ST_SRID(geom) FROM summit_points LIMIT 1")
    row = cursor.fetchone()
    if row:
        print(f"  summit_points SRID: {row[0]}")

    cursor.execute(
        """
        SELECT COUNT(*)
        FROM trail_edges e
        WHERE NOT EXISTS (
            SELECT 1 FROM trail_nodes n WHERE n.node_id = e.start_node_id
        )
           OR NOT EXISTS (
            SELECT 1 FROM trail_nodes n WHERE n.node_id = e.end_node_id
        )
        """
    )
    orphan = cursor.fetchone()[0]
    print(f"  edge → node 참조 실패: {orphan}개")


def verify_source_gpx(cursor) -> None:
    """추천 기능에 필요한 source_gpx 적재 상태를 확인한다."""
    print("\n[GPX 출처 검증]")

    cursor.execute("SELECT COUNT(*) FROM trail_edges")
    total = cursor.fetchone()[0]

    cursor.execute("""
                   SELECT COUNT(*)
                   FROM trail_edges
                   WHERE source_gpx IS NOT NULL
                     AND source_gpx <> ''
                   """)
    with_source_gpx = cursor.fetchone()[0]

    cursor.execute("""
                   SELECT COUNT(DISTINCT source_gpx)
                   FROM trail_edges
                   WHERE source_gpx IS NOT NULL
                     AND source_gpx <> ''
                   """)
    distinct_source_gpx = cursor.fetchone()[0]

    print(f"  source_gpx 있음: {with_source_gpx}개 / 없음: {total - with_source_gpx}개")
    print(f"  distinct source_gpx: {distinct_source_gpx}개")

    cursor.execute("""
                   SELECT source_gpx, COUNT(*) AS edge_count
                   FROM trail_edges
                   WHERE source_gpx IS NOT NULL
                     AND source_gpx <> ''
                   GROUP BY source_gpx
                   ORDER BY edge_count DESC, source_gpx
                       LIMIT 5
                   """)
    rows = cursor.fetchall()

    print("  source_gpx 샘플:")
    for source_gpx, edge_count in rows:
        print(f"    - {source_gpx}: {edge_count}개 edge")


# ──────────────────────────────────────────────
# 메인
# ──────────────────────────────────────────────

def main() -> None:
    print("[PostGIS 적재] 시작...")
    print(f"  ORDA_ENV: {ORDA_ENV}")
    print(f"  ORDA_ALLOW_DESTRUCTIVE_LOAD: {ORDA_ALLOW_DESTRUCTIVE_LOAD}")

    ensure_destructive_load_allowed()

    print("\n[1] GeoJSON 로딩")
    node_features = read_geojson_features(NODE_ELEVATION_PATH)
    edge_features = read_geojson_features(FINAL_TRAIL_DATASET_PATH)
    summit_features = read_geojson_features(SUMMIT_POINTS_PATH)

    print(f"  nodes: {len(node_features)}개")
    print(f"  edges: {len(edge_features)}개")
    print(f"  summits: {len(summit_features)}개")

    print("\n[2] DB 접속 및 적재")
    conn = get_connection()
    cursor = conn.cursor()

    try:
        ensure_required_tables(cursor)

        print("  정적 테이블 초기화...")
        reset_static_tables(cursor)
        print("    trail 정적 테이블 초기화 완료")

        node_attempt_count = load_nodes(cursor, node_features)
        print(f"  trail_nodes 적재 시도: {node_attempt_count}건")

        summit_attempt_count = load_summits(cursor, summit_features)
        print(f"  summit_points 적재 시도: {summit_attempt_count}건")

        edge_attempt_count = load_edges(cursor, edge_features)
        print(f"  trail_edges 적재 시도: {edge_attempt_count}건")

        verify_counts(cursor)
        verify_spatial(cursor)
        verify_source_gpx(cursor)

        conn.commit()
        print("\n[완료] 적재 성공, 커밋됨")

    except Exception as e:
        conn.rollback()
        print(f"\n[오류] 적재 실패, 롤백됨: {e}")
        raise

    finally:
        cursor.close()
        conn.close()


if __name__ == "__main__":
    main()
