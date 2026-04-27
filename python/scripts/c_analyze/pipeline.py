from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Optional

import rasterio
import numpy as np
from scipy.spatial import cKDTree

from config import (
    INPUT_EDGES_PATH,
    INPUT_NODES_PATH,
    INPUT_DEM_PATH,
    INPUT_SUMMIT_PATH,
    INPUT_PUBLIC_TRAILS_PATH,
    NODE_ELEVATION_PATH,
    SUMMIT_LINK_MAX_DISTANCE_M,
)


# ──────────────────────────────────────────────
# 파일 읽기/쓰기 유틸
# ──────────────────────────────────────────────

def read_json_file(path: Path) -> Any:
    if not path.exists():
        raise FileNotFoundError(f"파일이 없습니다: {path}")

    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def write_json_file(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)


def read_geojson_features(path: Path) -> list[dict[str, Any]]:
    data = read_json_file(path)

    if not isinstance(data, dict):
        raise ValueError(f"GeoJSON 형식이 아닙니다: {path}")

    if data.get("type") != "FeatureCollection":
        raise ValueError(f"FeatureCollection이 아닙니다: {path}")

    features = data.get("features")
    if not isinstance(features, list):
        raise ValueError(f"features가 list가 아닙니다: {path}")

    return features


def save_geojson(path: Path, data: dict[str, Any]) -> None:
    if not isinstance(data, dict):
        raise ValueError("저장할 데이터가 dict가 아닙니다.")

    if data.get("type") != "FeatureCollection":
        raise ValueError("저장할 데이터가 FeatureCollection이 아닙니다.")

    features = data.get("features")
    if not isinstance(features, list):
        raise ValueError("저장할 데이터의 features가 list가 아닙니다.")

    write_json_file(path, data)


# ──────────────────────────────────────────────
# DEM 관련 함수
# ──────────────────────────────────────────────

def open_dem(dem_path: Path):
    """DEM 파일을 열어서 rasterio dataset 객체를 반환한다."""
    if not dem_path.exists():
        raise FileNotFoundError(f"DEM 파일이 없습니다: {dem_path}")
    return rasterio.open(dem_path)


def sample_elevation(
        dem_dataset,
        lng: float,
        lat: float,
) -> tuple[Optional[float], str]:
    """
    DEM에서 [lng, lat] 좌표의 고도값을 추출한다.

    반환: (elevation_m, status)
    - status: "ok" | "nodata" | "out_of_bounds" | "error:{에러타입}"
    """
    try:
        bounds = dem_dataset.bounds
        if not (bounds.left <= lng <= bounds.right and bounds.bottom <= lat <= bounds.top):
            return None, "out_of_bounds"

        values = list(dem_dataset.sample([(lng, lat)]))
        if len(values) == 0:
            return None, "no_sample"

        elevation = float(values[0][0])

        if dem_dataset.nodata is not None and elevation == dem_dataset.nodata:
            return None, "nodata"

        return round(elevation, 1), "ok"

    except Exception as e:
        return None, f"error:{type(e).__name__}"


# ──────────────────────────────────────────────
# node 고도 산출물 생성
# ──────────────────────────────────────────────

def build_node_elevation(
        node_features: list[dict[str, Any]],
        dem_dataset,
) -> dict[str, Any]:
    """
    모든 node에 대해 DEM 고도를 샘플링하여
    node_with_elevation FeatureCollection + lookup index를 생성한다.

    반환: {
        "geojson": FeatureCollection (저장용),
        "index": { node_id: {"elevation_m": float|None, "status": str} }
    }
    """
    features: list[dict[str, Any]] = []
    elev_index: dict[str, dict[str, Any]] = {}

    for feature in node_features:
        props = feature.get("properties", {})
        geom = feature.get("geometry", {})
        coords = geom.get("coordinates", [])
        node_id = props.get("node_id")

        if not node_id or len(coords) < 2:
            continue

        lng, lat = coords[0], coords[1]
        elevation_m, status = sample_elevation(dem_dataset, lng, lat)

        qa_status = "pass" if status == "ok" else f"fail:{status}"

        elev_index[node_id] = {
            "elevation_m": elevation_m,
            "status": status,
        }

        new_props = {
            **props,
            "elevation_m": elevation_m,
            "elevation_status": status,
            "qa_status": qa_status,
        }

        features.append({
            "type": "Feature",
            "properties": new_props,
            "geometry": geom,
        })

    geojson = {
        "type": "FeatureCollection",
        "features": features,
    }

    total = len(features)
    ok = sum(1 for v in elev_index.values() if v["status"] == "ok")
    fail = total - ok
    print(f"  [node elevation] 총 {total}개 중 {ok}개 정상, {fail}개 실패")

    if fail > 0:
        from collections import Counter
        fail_reasons = Counter(
            v["status"] for v in elev_index.values() if v["status"] != "ok"
        )
        for reason, count in fail_reasons.items():
            print(f"    - {reason}: {count}개")

    return {"geojson": geojson, "index": elev_index}


# ──────────────────────────────────────────────
# 인덱스 빌더
# ──────────────────────────────────────────────

def build_node_index(
        node_features: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    node_index: dict[str, dict[str, Any]] = {}

    for feature in node_features:
        properties = feature.get("properties", {})
        node_id = properties.get("node_id")

        if not node_id:
            continue

        node_index[node_id] = feature

    return node_index


def build_summit_list(
        summit_features: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    summit feature 목록에서 summit_id와 좌표를 추출해 리스트로 반환.
    nearest_summit_id 계산에 사용.
    """
    summits = []

    for feature in summit_features:
        properties = feature.get("properties", {})
        geometry = feature.get("geometry", {})
        coords = geometry.get("coordinates", [])

        summit_id = properties.get("summit_id")
        if not summit_id or len(coords) < 2:
            continue

        summits.append({
            "summit_id": summit_id,
            "lng": coords[0],
            "lat": coords[1],
        })

    return summits


def build_summit_kdtree(
        summit_list: list[dict[str, Any]],
) -> Optional[tuple[cKDTree, list[str]]]:
    """
    summit 좌표로 KDTree를 구축한다.
    반환: (kdtree, summit_id_list) 또는 summit이 없으면 None

    좌표를 라디안 변환 후 3D 직교좌표(x, y, z)로 변환하여
    유클리드 거리 기반 KDTree에서 정확한 최근접 검색이 가능하게 한다.
    """
    if not summit_list:
        return None

    radius_m = 6371000.0
    coords = []
    ids = []

    for summit in summit_list:
        lat_rad = math.radians(summit["lat"])
        lng_rad = math.radians(summit["lng"])
        x = radius_m * math.cos(lat_rad) * math.cos(lng_rad)
        y = radius_m * math.cos(lat_rad) * math.sin(lng_rad)
        z = radius_m * math.sin(lat_rad)
        coords.append([x, y, z])
        ids.append(summit["summit_id"])

    tree = cKDTree(np.array(coords))
    return tree, ids


def find_nearest_summit_id_kdtree(
        midpoint_lng: float,
        midpoint_lat: float,
        summit_kdtree: tuple[cKDTree, list[str]],
        max_distance_m: float,
) -> Optional[str]:
    """
    KDTree 기반으로 edge 중점에서 가장 가까운 정상을 찾되,
    max_distance_m 이내일 때만 summit_id를 반환한다.
    """
    tree, ids = summit_kdtree

    radius_m = 6371000.0
    lat_rad = math.radians(midpoint_lat)
    lng_rad = math.radians(midpoint_lng)
    x = radius_m * math.cos(lat_rad) * math.cos(lng_rad)
    y = radius_m * math.cos(lat_rad) * math.sin(lng_rad)
    z = radius_m * math.sin(lat_rad)

    dist, idx = tree.query([x, y, z])

    if dist <= max_distance_m:
        return ids[idx]

    return None


def build_public_surface_map(
        public_trail_features: list[dict[str, Any]],
) -> dict[str, Optional[str]]:
    """
    standard_public_trail.geojson 에서
    trail_id -> surface 매핑 딕셔너리를 생성한다.

    surface가 없거나 null이면 그대로 None 유지.
    """
    surface_map: dict[str, Optional[str]] = {}

    for feature in public_trail_features:
        properties = feature.get("properties", {})
        trail_id = properties.get("trail_id")

        if not trail_id:
            continue

        surface_map[trail_id] = properties.get("surface")

    return surface_map


def collect_edge_ids(
        edge_features: list[dict[str, Any]],
) -> tuple[list[str], set[str]]:
    edge_ids: list[str] = []
    duplicate_edge_ids: set[str] = set()
    seen: set[str] = set()

    for feature in edge_features:
        properties = feature.get("properties", {})
        edge_id = properties.get("edge_id")

        if not edge_id:
            continue

        edge_ids.append(edge_id)

        if edge_id in seen:
            duplicate_edge_ids.add(edge_id)
        else:
            seen.add(edge_id)

    return edge_ids, duplicate_edge_ids


# ──────────────────────────────────────────────
# 거리 계산 (Haversine)
# ──────────────────────────────────────────────

def haversine_distance_m(
        lng1: float,
        lat1: float,
        lng2: float,
        lat2: float,
) -> float:
    """
    두 좌표 사이의 거리를 미터 단위로 계산한다.
    입력은 [lng, lat] 순서.
    """
    radius_m = 6371000.0

    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)

    a = (
            math.sin(dlat / 2) ** 2
            + math.cos(lat1_rad) * math.cos(lat2_rad)
            * math.sin(dlng / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    return radius_m * c


# ──────────────────────────────────────────────
# edge 중점 계산 (실제 선 길이 기준)
# ──────────────────────────────────────────────

def get_edge_midpoint(geometry: dict[str, Any]) -> Optional[tuple[float, float]]:
    """
    LineString geometry에서 실제 선 길이 기준 중점 좌표를 반환한다.
    좌표 배열의 중간 인덱스가 아니라, 누적 거리 기준으로 보간한다.
    반환: (lng, lat) 또는 None
    """
    coords = geometry.get("coordinates", [])
    if not coords or len(coords) < 2:
        return None

    if len(coords) == 2:
        return (
            (coords[0][0] + coords[1][0]) / 2,
            (coords[0][1] + coords[1][1]) / 2,
        )

    segment_lengths: list[float] = []
    for i in range(len(coords) - 1):
        distance_m = haversine_distance_m(
            coords[i][0], coords[i][1],
            coords[i + 1][0], coords[i + 1][1],
        )
        segment_lengths.append(distance_m)

    total_length = sum(segment_lengths)
    half_length = total_length / 2

    cumulative = 0.0
    for i, segment_length in enumerate(segment_lengths):
        if cumulative + segment_length >= half_length:
            remaining = half_length - cumulative
            ratio = remaining / segment_length if segment_length > 0 else 0
            lng = coords[i][0] + ratio * (coords[i + 1][0] - coords[i][0])
            lat = coords[i][1] + ratio * (coords[i + 1][1] - coords[i][1])
            return lng, lat
        cumulative += segment_length

    return coords[-1][0], coords[-1][1]


# ──────────────────────────────────────────────
# nearest summit 계산
# ──────────────────────────────────────────────

def find_nearest_summit_id(
        midpoint_lng: float,
        midpoint_lat: float,
        summit_list: list[dict[str, Any]],
        max_distance_m: float,
) -> Optional[str]:
    """
    edge 중점에서 가장 가까운 정상을 찾되,
    max_distance_m 이내일 때만 summit_id를 반환한다.
    """
    nearest_id: Optional[str] = None
    nearest_dist: float = float("inf")

    for summit in summit_list:
        dist = haversine_distance_m(
            midpoint_lng, midpoint_lat,
            summit["lng"], summit["lat"],
        )
        if dist < nearest_dist:
            nearest_dist = dist
            nearest_id = summit["summit_id"]

    if nearest_dist <= max_distance_m:
        return nearest_id

    return None


# ──────────────────────────────────────────────
# 계산 함수들
# ──────────────────────────────────────────────

DEFAULT_TERRAIN_SCORE = 60.0

SURFACE_TERRAIN_SCORE: dict[str, float] = {
    # 포장 (가장 쉬움)
    "asphalt": 20.0,
    "concrete": 20.0,
    "paved": 20.0,
    "paving_stones": 20.0,
    "paving_stone": 20.0,
    # 다짐/목재
    "compacted": 40.0,
    "fine_gravel": 40.0,
    "ground": 40.0,
    "wood": 40.0,
    "woodchips": 40.0,
    # 흙/모래/자갈
    "dirt": 60.0,
    "earth": 60.0,
    "sand": 60.0,
    "gravel": 60.0,
    "흙길": 60.0,
    "토사": 60.0,
    # 비포장/돌
    "unpaved": 80.0,
    "cobblestone": 80.0,
    "pebblestone": 80.0,
    "mud": 80.0,
    # 암반
    "rock": 100.0,
    "토사/암반": 100.0,
}


def get_terrain_score(surface: Optional[str]) -> float:
    """
    surface 값을 terrain 점수(0~100)로 변환한다.
    매핑 없거나 None이면 기본값 60.0 반환.
    """
    if surface is None:
        return DEFAULT_TERRAIN_SCORE
    return SURFACE_TERRAIN_SCORE.get(surface.strip(), DEFAULT_TERRAIN_SCORE)


def normalize(value: float, min_val: float, max_val: float) -> float:
    """
    value를 min_val~max_val 범위 기준으로 0~100 점수로 정규화한다.
    범위를 벗어나면 0 또는 100으로 클램핑.
    """
    if max_val <= min_val:
        return 0.0
    score = (value - min_val) / (max_val - min_val) * 100
    return max(0.0, min(100.0, score))


def calculate_difficulty_score(
        slope_percent: Optional[float],
        elevation_diff_m: Optional[float],
        distance_m: Optional[float],
        surface: Optional[str],
        max_slope: float = 50.0,
        max_elevation: float = 500.0,
        max_distance: float = 10000.0,
) -> Optional[float]:
    """
    4변수 난이도 점수를 계산한다.
    - slope     가중치 0.40
    - elevation 가중치 0.25
    - distance  가중치 0.15
    - terrain   가중치 0.20

    slope, elevation_diff_m, distance_m 중 하나라도 None이면 None 반환.
    max_slope, max_elevation, max_distance는 실데이터 기반 최댓값.
    """
    if slope_percent is None or elevation_diff_m is None or distance_m is None:
        return None

    slope_score = normalize(abs(slope_percent), 0.0, max_slope)
    elevation_score = normalize(abs(elevation_diff_m), 0.0, max_elevation)
    distance_score = normalize(distance_m, 0.0, max_distance)
    terrain_score = get_terrain_score(surface)

    score = (
            slope_score * 0.40
            + elevation_score * 0.25
            + distance_score * 0.15
            + terrain_score * 0.20
    )
    return round(score, 1)


def classify_difficulty(difficulty_score: Optional[float]) -> Optional[str]:
    """
    difficulty_score(0~100) 기준으로 등급을 분류한다.
    None이면 None 반환.
    """
    if difficulty_score is None:
        return None
    if difficulty_score < 20:
        return "easy"
    if difficulty_score < 40:
        return "moderate"
    if difficulty_score < 60:
        return "hard"
    if difficulty_score < 80:
        return "very_hard"
    return "extreme"


def calculate_slope_percent(
        elevation_diff_m: float,
        distance_m: float,
) -> float:
    if distance_m <= 0:
        raise ValueError("distance_m은 0보다 커야 합니다.")

    slope_percent = (elevation_diff_m / distance_m) * 100
    return round(slope_percent, 1)


# ──────────────────────────────────────────────
# 입력 로딩
# ──────────────────────────────────────────────

def load_c_stage_inputs() -> dict[str, Any]:
    """
    C단계에 필요한 모든 입력 데이터를 로딩한다.
    - B단계 산출물: edges, nodes
    - DEM: rasterio dataset → node 고도 샘플링 후 닫기
    - 정상 데이터: summit features
    - A단계 공공 등산로: surface 매핑용

    흐름:
    1. 파일 로딩
    2. DEM 열기 → node 고도 샘플링 → DEM 닫기
    3. node_with_elevation.geojson 저장
    4. 인덱스 생성 후 반환
    """
    edge_features = read_geojson_features(INPUT_EDGES_PATH)
    node_features = read_geojson_features(INPUT_NODES_PATH)
    summit_features = read_geojson_features(INPUT_SUMMIT_PATH)
    public_trail_features = read_geojson_features(INPUT_PUBLIC_TRAILS_PATH)

    dem_dataset = open_dem(INPUT_DEM_PATH)
    node_elev_result = build_node_elevation(node_features, dem_dataset)
    dem_dataset.close()

    save_geojson(NODE_ELEVATION_PATH, node_elev_result["geojson"])

    node_index = build_node_index(node_features)
    summit_list = build_summit_list(summit_features)
    public_surface_map = build_public_surface_map(public_trail_features)
    edge_ids, duplicate_edge_ids = collect_edge_ids(edge_features)
    node_ids = set(node_index.keys())

    return {
        "edge_features": edge_features,
        "node_features": node_features,
        "node_elev_index": node_elev_result["index"],
        "summit_list": summit_list,
        "public_surface_map": public_surface_map,
        "edge_ids": edge_ids,
        "duplicate_edge_ids": duplicate_edge_ids,
        "node_ids": node_ids,
    }


# ──────────────────────────────────────────────
# edge 메트릭 계산 (node 고도 참조 방식)
# ──────────────────────────────────────────────

def build_edge_metrics(
        edge_feature: dict[str, Any],
        node_elev_index: dict[str, dict[str, Any]],
        public_surface_map: dict[str, Optional[str]],
        max_slope: float = 50.0,
        max_elevation: float = 500.0,
        max_distance: float = 10000.0,
) -> dict[str, Any]:
    """
    node_elev_index에서 고도를 참조하여 edge 메트릭을 계산한다.
    DEM 직접 접근 없음.

    surface 우선순위:
    1. B단계 edge properties의 surface (직접 상속)
    2. A단계 공공 등산로의 trail_id 기준 surface (fallback)
    매핑 실패 또는 값 없음이면 surface는 None 유지.

    source_gpx:
    B단계 edge properties에서 받은 GPX 파일명.
    코스 단위 추천을 위해 final_trail_dataset.geojson까지 보존한다.
    """
    properties = edge_feature.get("properties", {})

    edge_id = properties.get("edge_id")
    trail_id = properties.get("trail_id")
    source_gpx = properties.get("source_gpx")
    start_node_id = properties.get("start_node_id")
    end_node_id = properties.get("end_node_id")
    distance_m = properties.get("distance_m")

    start_elev = node_elev_index.get(start_node_id, {})
    end_elev = node_elev_index.get(end_node_id, {})

    elevation_start_m = start_elev.get("elevation_m")
    elevation_end_m = end_elev.get("elevation_m")

    elevation_diff_m: Optional[float] = None
    slope_percent: Optional[float] = None

    if (
            elevation_start_m is not None
            and elevation_end_m is not None
            and isinstance(distance_m, (int, float))
            and distance_m > 0
    ):
        elevation_diff_m = round(elevation_end_m - elevation_start_m, 1)
        slope_percent = calculate_slope_percent(
            elevation_diff_m, float(distance_m)
        )

    # B단계 edge surface 우선, 없으면 A단계 공공 등산로 fallback
    surface = properties.get("surface") or public_surface_map.get(trail_id)

    difficulty_score = calculate_difficulty_score(
        slope_percent,
        elevation_diff_m,
        distance_m,
        surface,
        max_slope=max_slope,
        max_elevation=max_elevation,
        max_distance=max_distance,
    )
    difficulty = classify_difficulty(difficulty_score)

    return {
        "edge_id": edge_id,
        "trail_id": trail_id,
        "source_gpx": source_gpx,
        "start_node_id": start_node_id,
        "end_node_id": end_node_id,
        "distance_m": distance_m,
        "elevation_start_m": elevation_start_m,
        "elevation_end_m": elevation_end_m,
        "elevation_diff_m": elevation_diff_m,
        "slope_percent": slope_percent,
        "difficulty_score": difficulty_score,
        "difficulty": difficulty,
        "surface": surface,
    }


# ──────────────────────────────────────────────
# 최종 feature 빌드
# ──────────────────────────────────────────────

def build_final_trail_feature(
        edge_feature: dict[str, Any],
        edge_metrics: dict[str, Any],
        nearest_summit_id: Optional[str],
        qa_result: dict[str, Any],
) -> dict[str, Any]:
    """
    문서 기준 final_trail_dataset.geojson의 feature 하나를 생성한다.

    필드 순서:
    edge_id, source_gpx, start_node_id, end_node_id, distance_m,
    elevation_start_m, elevation_end_m, elevation_diff_m,
    slope_percent, difficulty_score, difficulty, surface, nearest_summit_id, qa_status,
    geometry
    """
    geometry = edge_feature.get("geometry")

    properties = {
        "edge_id": edge_metrics["edge_id"],
        "source_gpx": edge_metrics["source_gpx"],
        "start_node_id": edge_metrics["start_node_id"],
        "end_node_id": edge_metrics["end_node_id"],
        "distance_m": edge_metrics["distance_m"],
        "elevation_start_m": edge_metrics["elevation_start_m"],
        "elevation_end_m": edge_metrics["elevation_end_m"],
        "elevation_diff_m": edge_metrics["elevation_diff_m"],
        "slope_percent": edge_metrics["slope_percent"],
        "difficulty_score": edge_metrics["difficulty_score"],
        "difficulty": edge_metrics["difficulty"],
        "surface": edge_metrics["surface"],
        "nearest_summit_id": nearest_summit_id,
        "qa_status": qa_result["qa_status"],
    }

    return {
        "type": "Feature",
        "properties": properties,
        "geometry": geometry,
    }


# ──────────────────────────────────────────────
# 전체 dataset 빌드
# ──────────────────────────────────────────────

def build_final_trail_dataset(
        edge_features: list[dict[str, Any]],
        node_elev_index: dict[str, dict[str, Any]],
        summit_list: list[dict[str, Any]],
        public_surface_map: dict[str, Optional[str]],
        node_ids: set[str],
        duplicate_edge_ids: set[str],
) -> dict[str, Any]:
    """
    모든 edge를 순회하며 final_trail_dataset.geojson을 생성한다.

    각 edge마다:
    1. node_elev_index에서 고도 참조 → diff/slope/difficulty_score/difficulty 계산
    2. B단계 surface 우선, A단계 fallback
    3. B단계 source_gpx 보존
    4. KDTree 기반 nearest_summit_id 계산
    5. qa_rules로 qa_status 계산
    6. 최종 feature 생성
    """
    from qa_rules import evaluate_edge_qa

    # 실데이터 기반 최댓값 계산
    slope_values = []
    elevation_values = []
    distance_values = []

    for edge_feature in edge_features:
        props = edge_feature.get("properties", {})
        start_node = node_elev_index.get(props.get("start_node_id"), {})
        end_node = node_elev_index.get(props.get("end_node_id"), {})
        distance_m = props.get("distance_m")

        elev_start = start_node.get("elevation_m")
        elev_end = end_node.get("elevation_m")

        if elev_start is not None and elev_end is not None and distance_m and distance_m > 0:
            elev_diff = abs(elev_end - elev_start)
            slope = abs((elev_diff / distance_m) * 100)
            slope_values.append(slope)
            elevation_values.append(elev_diff)

        if distance_m:
            distance_values.append(distance_m)

    max_slope = float(np.percentile(slope_values, 99)) if slope_values else 50.0
    max_elevation = float(np.percentile(elevation_values, 99)) if elevation_values else 500.0
    max_distance = float(np.percentile(distance_values, 99)) if distance_values else 10000.0

    print(f"  [난이도 기준값] slope: {max_slope:.1f}% / elevation: {max_elevation:.1f}m / distance: {max_distance:.1f}m")

    # KDTree 한 번 구축하여 재사용
    summit_kdtree = build_summit_kdtree(summit_list)

    final_features: list[dict[str, Any]] = []

    for edge_feature in edge_features:
        edge_metrics = build_edge_metrics(
            edge_feature,
            node_elev_index,
            public_surface_map,
            max_slope=max_slope,
            max_elevation=max_elevation,
            max_distance=max_distance,
        )

        nearest_summit_id: Optional[str] = None
        geometry = edge_feature.get("geometry", {})
        midpoint = get_edge_midpoint(geometry)

        if midpoint is not None and summit_kdtree is not None:
            nearest_summit_id = find_nearest_summit_id_kdtree(
                midpoint[0],
                midpoint[1],
                summit_kdtree,
                SUMMIT_LINK_MAX_DISTANCE_M,
            )

        qa_result = evaluate_edge_qa(
            edge_feature=edge_feature,
            node_ids=node_ids,
            edge_metrics=edge_metrics,
            duplicate_edge_ids=duplicate_edge_ids,
        )

        final_feature = build_final_trail_feature(
            edge_feature,
            edge_metrics,
            nearest_summit_id,
            qa_result,
        )
        final_features.append(final_feature)

    return {
        "type": "FeatureCollection",
        "features": final_features,
    }