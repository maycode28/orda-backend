from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from pyproj import Geod


# =========================================================
# 1. 경로 설정
# =========================================================

PROJECT_ROOT = Path(__file__).resolve().parents[2]  # /python 기준
INPUT_PATH = PROJECT_ROOT / "data" / "interim" / "a_output" / "major_peak_trails_merged.geojson"
OUTPUT_DIR = PROJECT_ROOT / "data" / "interim" / "b_output"

EDGES_OUTPUT_PATH = OUTPUT_DIR / "trail_network_edges.geojson"
NODES_OUTPUT_PATH = OUTPUT_DIR / "trail_network_nodes.geojson"

GEOD = Geod(ellps="WGS84")
COORD_PRECISION = 7


# =========================================================
# 2. 공통 유틸
# =========================================================

def read_geojson(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"입력 파일이 없습니다: {path}")

    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if data.get("type") != "FeatureCollection":
        raise ValueError("입력 파일의 최상위 type은 FeatureCollection이어야 합니다.")

    return data


def write_geojson(path: Path, features: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    output = {
        "type": "FeatureCollection",
        "features": features,
    }

    with path.open("w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)


def round_coord(coord: list[float], precision: int = COORD_PRECISION) -> list[float]:
    lng = round(float(coord[0]), precision)
    lat = round(float(coord[1]), precision)
    return [lng, lat]


def make_point_key(coord: list[float]) -> tuple[float, float]:
    c = round_coord(coord)
    return c[0], c[1]


def split_to_lines(geometry: dict[str, Any] | None) -> list[list[list[float]]]:
    if geometry is None:
        return []

    geom_type = geometry.get("type")
    coords = geometry.get("coordinates", [])

    if geom_type == "LineString":
        return [coords]

    if geom_type == "MultiLineString":
        return coords

    return []


def dedupe_consecutive_coords(coords: list[list[float]]) -> list[list[float]]:
    if not coords:
        return []

    result = [coords[0]]
    for coord in coords[1:]:
        if make_point_key(coord) != make_point_key(result[-1]):
            result.append(coord)

    return result


def calculate_length_m(coords: list[list[float]]) -> float:
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    length = GEOD.line_length(lons, lats)
    return round(abs(length), 1)


# =========================================================
# 3. 노드 레지스트리
# =========================================================

class NodeRegistry:
    def __init__(self) -> None:
        self._key_to_id: dict[tuple[float, float], str] = {}
        self._id_to_coord: dict[str, list[float]] = {}
        self._counter = 1

    def get_or_create(self, coord: list[float]) -> str:
        key = make_point_key(coord)
        if key not in self._key_to_id:
            node_id = f"N{self._counter:06d}"
            self._counter += 1
            self._key_to_id[key] = node_id
            self._id_to_coord[node_id] = [key[0], key[1]]
        return self._key_to_id[key]

    def coord(self, node_id: str) -> list[float]:
        return self._id_to_coord[node_id]


# =========================================================
# 4. 입력 정규화
# =========================================================

def normalize_input_features(data: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    features = data.get("features", [])

    normalized_segments: list[dict[str, Any]] = []

    stats = {
        "input_feature_count": len(features),
        "invalid_geometry": 0,
        "invalid_short_coords": 0,
        "invalid_missing_trail_id": 0,
    }

    for feature_index, feature in enumerate(features, start=1):
        properties = feature.get("properties", {}) or {}
        geometry = feature.get("geometry")

        trail_id = properties.get("trail_id")
        if not trail_id:
            stats["invalid_missing_trail_id"] += 1
            continue

        lines = split_to_lines(geometry)
        if not lines:
            stats["invalid_geometry"] += 1
            continue

        for line_index, coords in enumerate(lines, start=1):
            cleaned_coords: list[list[float]] = []

            for coord in coords:
                if not isinstance(coord, (list, tuple)) or len(coord) < 2:
                    continue

                try:
                    cleaned_coords.append(round_coord([float(coord[0]), float(coord[1])]))
                except (TypeError, ValueError):
                    continue

            cleaned_coords = dedupe_consecutive_coords(cleaned_coords)

            if len(cleaned_coords) < 2:
                stats["invalid_short_coords"] += 1
                continue

            normalized_segments.append({
                "trail_id": trail_id,
                "mountain_id": properties.get("mountain_id"),
                "mountain_name": properties.get("mountain_name"),
                "course_id": properties.get("course_id"),
                "course_name": properties.get("course_name"),

                # 코스 단위 추천을 위한 GPX 파일 출처
                "source_gpx": properties.get("source_gpx"),

                # 기존 추적용 메타
                "track_name": properties.get("track_name"),
                "source": properties.get("source"),
                "source_ref": properties.get("source_ref"),
                "trail_type": properties.get("trail_type"),
                "surface": properties.get("surface"),
                "is_official": properties.get("is_official"),
                "is_bidirectional": bool(properties.get("is_bidirectional", True)),
                "segment_order": properties.get("segment_order", line_index),
                "gpx_segment_order": properties.get("gpx_segment_order"),
                "geometry_source": properties.get("geometry_source"),
                "point_count": len(cleaned_coords),
                "coords": cleaned_coords,
            })

    return normalized_segments, stats


# =========================================================
# 5. 안정적인 정렬
# =========================================================

def sort_segments_for_stable_output(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        segments,
        key=lambda s: (
            str(s.get("mountain_id") or ""),
            str(s.get("mountain_name") or ""),
            str(s.get("course_id") or ""),
            str(s.get("source_gpx") or ""),
            str(s.get("trail_id") or ""),
            int(s.get("segment_order") or 0),
            str(s.get("source_ref") or ""),
        ),
    )


# =========================================================
# 6. raw edge / node 생성
# =========================================================

def build_raw_network_features(
        segments: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    registry = NodeRegistry()

    edge_features: list[dict[str, Any]] = []

    node_to_edge_ids: defaultdict[str, set[str]] = defaultdict(set)
    node_to_trail_refs: defaultdict[str, set[str]] = defaultdict(set)
    node_to_mountain_refs: defaultdict[str, set[str]] = defaultdict(set)
    node_to_course_refs: defaultdict[str, set[str]] = defaultdict(set)

    edge_counter = 1
    skipped_zero_length = 0

    for segment in sort_segments_for_stable_output(segments):
        coords = segment["coords"]
        distance_m = calculate_length_m(coords)

        if distance_m <= 0:
            skipped_zero_length += 1
            continue

        start_node_id = registry.get_or_create(coords[0])
        end_node_id = registry.get_or_create(coords[-1])

        edge_id = f"E{edge_counter:06d}"
        edge_counter += 1

        edge_features.append({
            "type": "Feature",
            "properties": {
                "edge_id": edge_id,
                "trail_id": segment["trail_id"],
                "surface": segment.get("surface"),
                "start_node_id": start_node_id,
                "end_node_id": end_node_id,
                "distance_m": distance_m,
                "segment_order": segment.get("segment_order"),
                "is_bidirectional": segment.get("is_bidirectional", True),
                "merge_status": "raw",

                # 코스 단위 추천을 위한 GPX 파일 출처
                "source_gpx": segment.get("source_gpx"),

                # 추적용 메타
                "mountain_id": segment.get("mountain_id"),
                "mountain_name": segment.get("mountain_name"),
                "course_id": segment.get("course_id"),
                "course_name": segment.get("course_name"),
                "track_name": segment.get("track_name"),
                "source": segment.get("source"),
                "source_ref": segment.get("source_ref"),
                "trail_type": segment.get("trail_type"),
                "is_official": segment.get("is_official"),
                "gpx_segment_order": segment.get("gpx_segment_order"),
                "geometry_source": segment.get("geometry_source"),
                "point_count": segment.get("point_count"),
            },
            "geometry": {
                "type": "LineString",
                "coordinates": coords,
            },
        })

        node_to_edge_ids[start_node_id].add(edge_id)
        node_to_edge_ids[end_node_id].add(edge_id)

        node_to_trail_refs[start_node_id].add(segment["trail_id"])
        node_to_trail_refs[end_node_id].add(segment["trail_id"])

        if segment.get("mountain_name"):
            node_to_mountain_refs[start_node_id].add(segment["mountain_name"])
            node_to_mountain_refs[end_node_id].add(segment["mountain_name"])

        if segment.get("course_id"):
            node_to_course_refs[start_node_id].add(segment["course_id"])
            node_to_course_refs[end_node_id].add(segment["course_id"])

    node_features: list[dict[str, Any]] = []

    for node_id in sorted(node_to_edge_ids.keys()):
        degree = len(node_to_edge_ids[node_id])

        # 보수적으로: 시작/끝만 찍은 구조라 degree 2 이상이면 junction,
        # degree 1이면 방향 판정 없이 start 로 둔다.
        if degree >= 2:
            node_type = "junction"
        else:
            node_type = "start"

        node_features.append({
            "type": "Feature",
            "properties": {
                "node_id": node_id,
                "node_type": node_type,
                "degree": degree,
                "trail_refs": sorted(node_to_trail_refs[node_id]),
                "mountain_refs": sorted(node_to_mountain_refs[node_id]),
                "course_refs": sorted(node_to_course_refs[node_id]),
            },
            "geometry": {
                "type": "Point",
                "coordinates": registry.coord(node_id),
            },
        })

    stats = {
        "edge_count": len(edge_features),
        "node_count": len(node_features),
        "skipped_zero_length": skipped_zero_length,
    }

    return edge_features, node_features, stats


# =========================================================
# 7. 실행
# =========================================================

def main() -> None:
    data = read_geojson(INPUT_PATH)

    normalized_segments, normalize_stats = normalize_input_features(data)

    print(f"입력 feature 수: {normalize_stats['input_feature_count']}")
    print(f"trail_id 누락 제거: {normalize_stats['invalid_missing_trail_id']}")
    print(f"geometry 오류 제거: {normalize_stats['invalid_geometry']}")
    print(f"좌표 부족 제거: {normalize_stats['invalid_short_coords']}")
    print(f"정규화된 raw line 수: {len(normalized_segments)}")

    edge_features, node_features, network_stats = build_raw_network_features(normalized_segments)

    write_geojson(EDGES_OUTPUT_PATH, edge_features)
    write_geojson(NODES_OUTPUT_PATH, node_features)

    print("----- 최종 완료 -----")
    print(f"최종 edge 수: {network_stats['edge_count']}")
    print(f"최종 node 수: {network_stats['node_count']}")
    print(f"거리 0으로 제거된 edge 수: {network_stats['skipped_zero_length']}")
    print(f"edges 저장 경로: {EDGES_OUTPUT_PATH}")
    print(f"nodes 저장 경로: {NODES_OUTPUT_PATH}")


if __name__ == "__main__":
    main()