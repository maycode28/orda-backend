-- ──────────────────────────────────────────────
-- ORDA C단계 PostGIS 적재 스키마
-- 좌표계: WGS84 / EPSG:4326
-- geometry: 2D (고도는 속성 컬럼으로 분리)
-- ──────────────────────────────────────────────

-- 확장 활성화
CREATE EXTENSION IF NOT EXISTS postgis;

-- ──────────────────────────────────────────────
-- trail_nodes
-- 원본: node_with_elevation.geojson
-- ──────────────────────────────────────────────
CREATE TABLE trail_nodes (
                             node_id          TEXT PRIMARY KEY,
                             node_type        TEXT,
                             degree           INTEGER,
                             elevation_m      DOUBLE PRECISION,
                             elevation_status TEXT,
                             qa_status        TEXT,
                             geom             GEOMETRY(Point, 4326)
);

CREATE INDEX idx_trail_nodes_geom ON trail_nodes USING GIST (geom);

-- ──────────────────────────────────────────────
-- trail_edges
-- 원본: final_trail_dataset.geojson
--
-- source_gpx:
--   GPX 파일 단위 코스 식별자.
--   추천 기능에서 같은 GPX에서 나온 edge들을 하나의 코스로 묶는 기준으로 사용.
--
-- elevation_diff_m:
--   현재는 end-start 단순 차이값.
--   진짜 누적 상승고도(gain/loss)는 edge 내부 샘플링 구현 후 컬럼 확장 예정.
-- ──────────────────────────────────────────────
CREATE TABLE trail_edges (
                             edge_id           TEXT PRIMARY KEY,
                             source_gpx        TEXT,
                             start_node_id     TEXT NOT NULL REFERENCES trail_nodes(node_id),
                             end_node_id       TEXT NOT NULL REFERENCES trail_nodes(node_id),
                             distance_m        DOUBLE PRECISION,
                             elevation_start_m DOUBLE PRECISION,
                             elevation_end_m   DOUBLE PRECISION,
                             elevation_diff_m  DOUBLE PRECISION,
                             slope_percent     DOUBLE PRECISION,
                             difficulty_score  DOUBLE PRECISION,
                             difficulty        TEXT,
                             surface           TEXT,
                             nearest_summit_id TEXT,
                             qa_status         TEXT,
                             geom              GEOMETRY(LineString, 4326)
);

CREATE INDEX idx_trail_edges_geom ON trail_edges USING GIST (geom);
CREATE INDEX idx_trail_edges_source_gpx ON trail_edges (source_gpx);
CREATE INDEX idx_trail_edges_nearest_summit_id ON trail_edges (nearest_summit_id);
CREATE INDEX idx_trail_edges_difficulty ON trail_edges (difficulty);

-- ──────────────────────────────────────────────
-- summit_points
-- 원본: summit_points.geojson
-- ──────────────────────────────────────────────
CREATE TABLE summit_points (
                               summit_id   TEXT PRIMARY KEY,
                               name        TEXT,
                               elevation_m DOUBLE PRECISION,
                               source      TEXT,
                               radius_m    DOUBLE PRECISION,
                               geom        GEOMETRY(Point, 4326)
);

CREATE INDEX idx_summit_points_geom ON summit_points USING GIST (geom);