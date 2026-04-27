from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


# =========================================================
# 1. 경로 설정
# =========================================================

PROJECT_ROOT = Path(__file__).resolve().parents[2]  # /python 기준
INPUT_ROOT = PROJECT_ROOT / "data" / "raw" / "major_peak_gpx_raw"
OUTPUT_PATH = PROJECT_ROOT / "data" / "interim" / "a_output" / "major_peak_trails_merged.geojson"

SOURCE_NAME = "MAJOR_PEAK_GPX"
COORD_PRECISION = 7
DEFAULT_IS_BIDIRECTIONAL = True


# =========================================================
# 2. 공통 유틸
# =========================================================

def write_geojson(path: Path, features: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    output = {
        "type": "FeatureCollection",
        "features": features,
    }

    with path.open("w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)


def round_coord(lng: float, lat: float) -> list[float]:
    return [round(float(lng), COORD_PRECISION), round(float(lat), COORD_PRECISION)]


def dedupe_consecutive_coords(coords: list[list[float]]) -> list[list[float]]:
    if not coords:
        return []

    deduped = [coords[0]]
    for coord in coords[1:]:
        if coord != deduped[-1]:
            deduped.append(coord)

    return deduped


def split_folder_name(folder_name: str) -> tuple[str, str]:
    if "_" not in folder_name:
        return folder_name, folder_name

    mountain_id, mountain_name = folder_name.split("_", 1)
    mountain_id = mountain_id.strip()
    mountain_name = mountain_name.strip()

    return mountain_id or folder_name, mountain_name or folder_name


def parse_course_name_and_seq(file_stem: str) -> tuple[str, str]:
    match = re.match(r"^(?P<course_name>.+?)_(?P<course_seq>\d+)$", file_stem)
    if match:
        return match.group("course_name").strip(), match.group("course_seq").strip()

    return file_stem.strip(), "0000000001"


def local_name(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def iter_children_by_local_name(parent: ET.Element, name: str):
    for child in list(parent):
        if local_name(child.tag) == name:
            yield child


def first_child_text(parent: ET.Element, name: str) -> str | None:
    for child in iter_children_by_local_name(parent, name):
        if child.text and child.text.strip():
            return child.text.strip()
    return None


def get_source_gpx(gpx_path: Path) -> str:
    """
    추천 기능에서 코스 단위로 edge를 묶기 위한 GPX 출처 키.

    폴더명과 파일명이 중복 성격을 가지므로,
    source_gpx에는 파일명만 저장한다.

    예:
    만장봉_0000000001.gpx
    """
    return gpx_path.name


# =========================================================
# 3. GPX 파싱
# =========================================================

def extract_sequences_from_gpx(gpx_path: Path) -> list[dict[str, Any]]:
    """
    GPX 파일에서 LineString 후보 좌표열을 추출한다.

    우선순위:
    1) trk/trkseg/trkpt
    2) rte/rtept
    """
    tree = ET.parse(gpx_path)
    root = tree.getroot()

    sequences: list[dict[str, Any]] = []

    # 1) track 우선
    for trk in root.iter():
        if local_name(trk.tag) != "trk":
            continue

        track_name = first_child_text(trk, "name") or gpx_path.stem
        track_segment_index = 0

        for trkseg in iter_children_by_local_name(trk, "trkseg"):
            coords: list[list[float]] = []

            for trkpt in iter_children_by_local_name(trkseg, "trkpt"):
                lat_text = trkpt.get("lat")
                lng_text = trkpt.get("lon")

                if lat_text is None or lng_text is None:
                    continue

                try:
                    lat = float(lat_text)
                    lng = float(lng_text)
                except ValueError:
                    continue

                coords.append(round_coord(lng, lat))

            coords = dedupe_consecutive_coords(coords)
            if len(coords) < 2:
                continue

            track_segment_index += 1
            sequences.append({
                "track_name": track_name,
                "gpx_segment_order": track_segment_index,
                "coords": coords,
                "point_count": len(coords),
                "geometry_source": "trkseg",
            })

    if sequences:
        return sequences

    # 2) route fallback
    for rte in root.iter():
        if local_name(rte.tag) != "rte":
            continue

        route_name = first_child_text(rte, "name") or gpx_path.stem
        coords: list[list[float]] = []

        for rtept in iter_children_by_local_name(rte, "rtept"):
            lat_text = rtept.get("lat")
            lng_text = rtept.get("lon")

            if lat_text is None or lng_text is None:
                continue

            try:
                lat = float(lat_text)
                lng = float(lng_text)
            except ValueError:
                continue

            coords.append(round_coord(lng, lat))

        coords = dedupe_consecutive_coords(coords)
        if len(coords) < 2:
            continue

        sequences.append({
            "track_name": route_name,
            "gpx_segment_order": 1,
            "coords": coords,
            "point_count": len(coords),
            "geometry_source": "rte",
        })

    return sequences


# =========================================================
# 4. Feature 생성
# =========================================================

def build_features_from_gpx(gpx_path: Path) -> list[dict[str, Any]]:
    relative_path = gpx_path.relative_to(INPUT_ROOT).as_posix()

    mountain_id, mountain_name = split_folder_name(gpx_path.parent.name)
    course_name, course_seq = parse_course_name_and_seq(gpx_path.stem)
    course_id = f"{mountain_id}_{course_seq}"

    source_gpx = get_source_gpx(gpx_path)

    sequences = extract_sequences_from_gpx(gpx_path)
    features: list[dict[str, Any]] = []

    for local_segment_index, sequence in enumerate(sequences, start=1):
        trail_id = f"MP_{mountain_id}_{course_seq}_{local_segment_index:02d}"

        features.append({
            "type": "Feature",
            "properties": {
                "trail_id": trail_id,
                "mountain_id": mountain_id,
                "mountain_name": mountain_name,
                "course_id": course_id,
                "course_name": course_name,

                # 코스 단위 추천을 위한 GPX 파일 출처
                "source_gpx": source_gpx,

                # 기존 추적용 메타
                "track_name": sequence["track_name"],
                "source": SOURCE_NAME,
                "source_ref": relative_path,
                "trail_type": "major_peak_course",
                "surface": None,
                "is_official": True,
                "is_bidirectional": DEFAULT_IS_BIDIRECTIONAL,
                "segment_order": local_segment_index,
                "gpx_segment_order": sequence["gpx_segment_order"],
                "geometry_source": sequence["geometry_source"],
                "point_count": sequence["point_count"],
            },
            "geometry": {
                "type": "LineString",
                "coordinates": sequence["coords"],
            },
        })

    return features


# =========================================================
# 5. 실행
# =========================================================

def main() -> None:
    if not INPUT_ROOT.exists():
        raise FileNotFoundError(f"입력 폴더가 없습니다: {INPUT_ROOT}")

    gpx_paths = sorted(INPUT_ROOT.rglob("*.gpx"))
    if not gpx_paths:
        raise FileNotFoundError(f"GPX 파일이 없습니다: {INPUT_ROOT}")

    all_features: list[dict[str, Any]] = []

    skipped_files = 0
    error_files = 0

    print(f"입력 GPX 파일 수: {len(gpx_paths)}")

    for index, gpx_path in enumerate(gpx_paths, start=1):
        try:
            features = build_features_from_gpx(gpx_path)
            if not features:
                skipped_files += 1
                print(f"[건너뜀] 좌표열 없음: {gpx_path.relative_to(INPUT_ROOT).as_posix()}")
                continue

            all_features.extend(features)

            if index % 200 == 0:
                print(f"  진행 중: {index}/{len(gpx_paths)} 파일 처리 완료")

        except Exception as e:
            error_files += 1
            print(f"[오류] {gpx_path.relative_to(INPUT_ROOT).as_posix()} -> {type(e).__name__}: {e}")

    write_geojson(OUTPUT_PATH, all_features)

    print("----- 완료 -----")
    print(f"출력 feature 수: {len(all_features)}")
    print(f"좌표열 없음으로 건너뜀: {skipped_files}")
    print(f"오류 파일 수: {error_files}")
    print(f"저장 경로: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()