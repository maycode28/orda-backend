from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Any

import pandas as pd
import psycopg2
from sklearn.metrics.pairwise import cosine_similarity
from sklearn.preprocessing import StandardScaler


FEATURE_COLUMNS = ["difficulty_score", "distance_km", "elevation_gain_m"]
RECENT_SESSION_COUNT = 3
RECENT_SESSION_WEIGHT = 2.0


@dataclass(frozen=True)
class Recommendation:
    trail_id: str
    name: str
    distance_km: float
    elevation_gain_m: float
    difficulty_score: float
    similarity_score: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "trail_id": self.trail_id,
            "name": self.name,
            "distance_km": round(self.distance_km, 2),
            "elevation_gain_m": round(self.elevation_gain_m, 1),
            "difficulty_score": round(self.difficulty_score, 2),
            "similarity_score": round(self.similarity_score, 4),
        }


def _database_url() -> str | None:
    value = os.getenv("DATABASE_URL") or os.getenv("DB_URL")
    if value and value.startswith("jdbc:"):
        return value.removeprefix("jdbc:")
    return value


def _connect():
    database_url = _database_url()
    if database_url:
        kwargs = {}
        username = os.getenv("DB_USERNAME") or os.getenv("ORDA_DB_USER")
        password = os.getenv("DB_PASSWORD") or os.getenv("ORDA_DB_PASSWORD")
        if username:
            kwargs["user"] = username
        if password:
            kwargs["password"] = password
        return psycopg2.connect(database_url, **kwargs)

    return psycopg2.connect(
        host=os.getenv("ORDA_DB_HOST", "localhost"),
        port=int(os.getenv("ORDA_DB_PORT", "5432")),
        dbname=os.getenv("ORDA_DB_NAME", "orda"),
        user=os.getenv("ORDA_DB_USER", "postgres"),
        password=os.getenv("ORDA_DB_PASSWORD", "0000"),
    )


def _course_name_expr() -> str:
    return """
        COALESCE(
            NULLIF(regexp_replace(e.source_gpx, '\\.gpx$', '', 'i'), ''),
            e.source_gpx
        )
    """


def _load_course_features(conn) -> pd.DataFrame:
    sql = f"""
        SELECT
            e.source_gpx AS trail_id,
            {_course_name_expr()} AS name,
            COALESCE(SUM(e.distance_m), 0) / 1000.0 AS distance_km,
            COALESCE(SUM(GREATEST(COALESCE(e.elevation_diff_m, 0), 0)), 0) AS elevation_gain_m,
            COALESCE(
                SUM(COALESCE(e.difficulty_score, 0) * COALESCE(NULLIF(e.distance_m, 0), 1))
                / NULLIF(SUM(COALESCE(NULLIF(e.distance_m, 0), 1)), 0),
                AVG(e.difficulty_score),
                0
            ) AS difficulty_score,
            COUNT(*) AS edge_count
        FROM trail_edges e
        WHERE e.source_gpx IS NOT NULL
          AND e.source_gpx <> ''
          AND e.distance_m IS NOT NULL
        GROUP BY e.source_gpx
        HAVING SUM(e.distance_m) > 0
        ORDER BY e.source_gpx
    """
    return pd.read_sql_query(sql, conn)


def _load_user_course_history(conn, user_id: int) -> pd.DataFrame:
    sql = """
        WITH user_sessions AS (
            SELECT session_id, started_at
            FROM hiking_sessions
            WHERE user_id = %(user_id)s
              AND status = 'COMPLETED'
            ORDER BY started_at DESC
        ),
        matched_points AS (
            SELECT
                s.session_id,
                s.started_at,
                nearest.source_gpx
            FROM user_sessions s
            JOIN gps_tracks g ON g.session_id = s.session_id
            JOIN LATERAL (
                SELECT e.source_gpx
                FROM trail_edges e
                WHERE e.source_gpx IS NOT NULL
                  AND e.source_gpx <> ''
                  AND e.geom IS NOT NULL
                  AND ST_DWithin(
                      e.geom::geography,
                      ST_SetSRID(
                          ST_MakePoint(
                              COALESCE(g.snapped_longitude, g.raw_longitude),
                              COALESCE(g.snapped_latitude, g.raw_latitude)
                          ),
                          4326
                      )::geography,
                      50
                  )
                ORDER BY e.geom <->
                    ST_SetSRID(
                        ST_MakePoint(
                            COALESCE(g.snapped_longitude, g.raw_longitude),
                            COALESCE(g.snapped_latitude, g.raw_latitude)
                        ),
                        4326
                    )
                LIMIT 1
            ) nearest ON TRUE
        ),
        course_votes AS (
            SELECT
                session_id,
                started_at,
                source_gpx AS trail_id,
                COUNT(*) AS matched_count,
                ROW_NUMBER() OVER (
                    PARTITION BY session_id
                    ORDER BY COUNT(*) DESC, source_gpx
                ) AS rn
            FROM matched_points
            GROUP BY session_id, started_at, source_gpx
        )
        SELECT session_id, started_at, trail_id, matched_count
        FROM course_votes
        WHERE rn = 1
        ORDER BY started_at DESC
    """
    return pd.read_sql_query(sql, conn, params={"user_id": user_id})


def _fallback_courses(courses: pd.DataFrame, top_n: int) -> list[Recommendation]:
    ranked = courses.sort_values(
        by=["edge_count", "distance_km", "difficulty_score", "trail_id"],
        ascending=[False, False, False, True],
    ).head(top_n)

    return [
        Recommendation(
            trail_id=str(row.trail_id),
            name=str(row.name),
            distance_km=float(row.distance_km),
            elevation_gain_m=float(row.elevation_gain_m),
            difficulty_score=float(row.difficulty_score),
            similarity_score=0.0,
        )
        for row in ranked.itertuples(index=False)
    ]


def _build_preference_vector(history: pd.DataFrame) -> pd.Series | None:
    if history.empty:
        return None

    history = history.copy().reset_index(drop=True)
    history["weight"] = 1.0
    history.loc[: RECENT_SESSION_COUNT - 1, "weight"] = RECENT_SESSION_WEIGHT

    weights = history["weight"]
    return pd.Series(
        {
            column: float((history[column] * weights).sum() / weights.sum())
            for column in FEATURE_COLUMNS
        }
    )


def recommend(user_id: int, top_n: int = 5) -> list[dict]:
    top_n = max(1, min(int(top_n), 20))

    with _connect() as conn:
        courses = _load_course_features(conn)
        if courses.empty:
            return []

        user_history = _load_user_course_history(conn, user_id)
        if user_history.empty:
            return [item.to_dict() for item in _fallback_courses(courses, top_n)]

        history = user_history.merge(courses, on="trail_id", how="inner")
        visited = set(user_history["trail_id"].dropna().astype(str))
        candidates = courses[~courses["trail_id"].astype(str).isin(visited)].copy()

        if history.empty or candidates.empty:
            return [item.to_dict() for item in _fallback_courses(courses, top_n)]

        preference = _build_preference_vector(history)
        if preference is None:
            return [item.to_dict() for item in _fallback_courses(courses, top_n)]

        scaler = StandardScaler()
        scaler.fit(courses[FEATURE_COLUMNS])

        candidate_vectors = scaler.transform(candidates[FEATURE_COLUMNS])
        preference_vector = scaler.transform(pd.DataFrame([preference], columns=FEATURE_COLUMNS))
        similarities = cosine_similarity(preference_vector, candidate_vectors)[0]

        candidates["similarity_score"] = similarities
        ranked = candidates.sort_values(
            by=["similarity_score", "edge_count", "trail_id"],
            ascending=[False, False, True],
        ).head(top_n)

        recommendations = [
            Recommendation(
                trail_id=str(row.trail_id),
                name=str(row.name),
                distance_km=float(row.distance_km),
                elevation_gain_m=float(row.elevation_gain_m),
                difficulty_score=float(row.difficulty_score),
                similarity_score=float(row.similarity_score),
            )
            for row in ranked.itertuples(index=False)
        ]
        return [item.to_dict() for item in recommendations]


def main() -> int:
    parser = argparse.ArgumentParser(description="Recommend ORDA hiking courses.")
    parser.add_argument("--user_id", type=int, required=True)
    parser.add_argument("--top_n", type=int, default=5)
    args = parser.parse_args()

    try:
        result = recommend(args.user_id, args.top_n)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
