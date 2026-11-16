#!/usr/bin/env python3
"""Convert one augmentation variant (SUMO run) into an RDS-HQ clip for the Cosmos-Drive-Dreams renderer.

    python sumo_to_rdshq.py <variantDir> <rdshqRoot> [--clip-id ID] [--window-s 12.1]

<variantDir> is an output of scripts/augment_run.py (telemetry.json + sumo/net.net.xml).
Writes webdataset tars <rdshqRoot>/{pose,vehicle_pose,all_object_info,3d_lanelines,
3d_road_boundaries,3d_crosswalks}/<clipId>.tar, matching the "minimal required format"
of cosmos-drive-dreams-toolkits/convert_waymo_to_rds_hq.py with dataset config `waymo`
(single camera `front`, f-theta intrinsic falls back to the toolkit default).

World frame = SUMO map frame (x east, y north, z up, metres). Vehicle frame is FLU with
its origin on the ground at the ego rear axle. Map geometry is read from the SUMO network,
not re-declared, so lane lines and the crossing match what SUMO simulated.
Needs numpy and webdataset (the toolkit venv), not SUMO.
"""

from __future__ import annotations

import argparse
import json
import math
import defusedxml.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
from webdataset import TarWriter

POSE_FPS = 30
BOX_STEP = 3  # boxes at 10 Hz on the 30 Hz index grid; the renderer interpolates
REAR_AXLE_FROM_CENTER = 1.4
# front_wide mount of the toolkit example clip: FLU metres from the rear axle on the ground, 1.3 deg down
CAMERA_IN_VEHICLE = (1.739, -0.031, 1.329)
CAMERA_PITCH_DOWN_DEG = 1.3
HEIGHTS = {"car": 1.5, "parked": 1.5, "truck": 3.2, "bus": 3.2, "adult": 1.75, "elderly": 1.65, "runner": 1.75,
           "child": 1.2, "wheelchair": 1.3, "stroller": 1.75, "pedestrian": 1.75}
DEDUP_TOLERANCE_M = 0.05  # metres; de-duplication tolerance for shared lane borders


def _write(sample: Dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sink = TarWriter(str(path))
    sink.write(sample)
    sink.close()


def _offset(shape: List[Tuple[float, float]], d: float) -> List[List[float]]:
    """Polyline offset to the left by d (straight segments only; enough for SUMO edge shapes)."""
    out = []
    for i, (x, y) in enumerate(shape):
        a, b = shape[max(0, i - 1)], shape[min(len(shape) - 1, i + 1)]
        dx, dy = b[0] - a[0], b[1] - a[1]
        n = math.hypot(dx, dy) or 1.0
        out.append([round(x - dy / n * d, 3), round(y + dx / n * d, 3), 0.0])
    return out


def _parse_shape(text: str) -> List[Tuple[float, float]]:
    return [tuple(map(float, p.split(",")[:2])) for p in text.split()]


def map_from_net(net_path: Path) -> Dict[str, List[List[List[float]]]]:
    root = ET.parse(net_path).getroot()
    lanelines, boundaries, crosswalks = [], [], []
    seen: List[List[List[float]]] = []

    def add_unique(target: List, line: List[List[float]]) -> None:
        for other in seen:
            if len(other) == len(line) and all(math.dist(p[:2], q[:2]) < DEDUP_TOLERANCE_M for p, q in zip(other, line[::-1])):
                return
            if len(other) == len(line) and all(math.dist(p[:2], q[:2]) < DEDUP_TOLERANCE_M for p, q in zip(other, line)):
                return
        seen.append(line)
        target.append(line)

    for edge in root.iter("edge"):
        function = edge.get("function")
        if function == "crossing":
            lane = edge.find("lane")
            (x0, y0), (x1, y1) = _parse_shape(lane.get("shape"))[:2]
            w = float(lane.get("width")) / 2
            dx, dy = x1 - x0, y1 - y0
            n = math.hypot(dx, dy)
            nx, ny = -dy / n * w, dx / n * w
            crosswalks.append([[round(x0 + nx, 3), round(y0 + ny, 3), 0.0], [round(x1 + nx, 3), round(y1 + ny, 3), 0.0],
                               [round(x1 - nx, 3), round(y1 - ny, 3), 0.0], [round(x0 - nx, 3), round(y0 - ny, 3), 0.0]])
            continue
        if function:  # internal junction lanes
            continue
        lanes = sorted((l for l in edge.findall("lane") if "pedestrian" not in (l.get("allow") or "")),
                       key=lambda l: int(l.get("index")))
        if not lanes:
            continue
        for i, lane in enumerate(lanes):
            shape = _parse_shape(lane.get("shape"))
            half = float(lane.get("width")) / 2
            if i == 0:  # rightmost vehicle lane: kerb
                add_unique(boundaries, _offset(shape, -half))
            add_unique(lanelines, _offset(shape, half))  # left border: divider or centre line
    return {"lanelines": lanelines, "road_boundaries": boundaries, "crosswalks": crosswalks}


def _yaw_matrix(x: float, y: float, z: float, yaw: float) -> np.ndarray:
    c, s = math.cos(yaw), math.sin(yaw)
    m = np.eye(4)
    m[:3, :3] = [[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]]
    m[:3, 3] = [x, y, z]
    return m


def _vehicle_to_world(ego: Dict[str, float]) -> np.ndarray:
    yaw = math.radians(ego["headingDeg"])
    return _yaw_matrix(ego["x"] - math.cos(yaw) * REAR_AXLE_FROM_CENTER, ego["y"] - math.sin(yaw) * REAR_AXLE_FROM_CENTER, 0.0, yaw)


def _interp_ego(a: Dict[str, float], b: Dict[str, float], w: float) -> Dict[str, float]:
    dh = (b["headingDeg"] - a["headingDeg"] + 180.0) % 360.0 - 180.0
    return {"x": a["x"] + (b["x"] - a["x"]) * w, "y": a["y"] + (b["y"] - a["y"]) * w, "headingDeg": a["headingDeg"] + dh * w}


def choose_window(frames: List[Dict], window_s: float) -> Tuple[int, int]:
    """Frames ending 0.5 s after the ego front clears the crossing (x=205), window_s long."""
    end = len(frames) - 1
    for i, f in enumerate(frames):
        e = f["ego"]
        if e and e["x"] + e["lengthM"] / 2 > 205.0:
            end = min(len(frames) - 1, i + 5)
            break
    start = max(0, end - int(round(window_s * 10)))
    while start < end and not frames[start]["ego"]:
        start += 1
    return start, end


def convert(variant_dir: Path, root: Path, clip_id: str, window_s: float) -> Dict:
    telemetry = json.loads((variant_dir / "telemetry.json").read_text())
    frames = telemetry["frames"]
    start, end = choose_window(frames, window_s)
    window = frames[start:end + 1]
    camera_to_vehicle = _yaw_matrix(*CAMERA_IN_VEHICLE, 0.0)
    pitch = math.radians(CAMERA_PITCH_DOWN_DEG)
    camera_to_vehicle[:3, :3] = [[math.cos(pitch), 0.0, math.sin(pitch)], [0.0, 1.0, 0.0], [-math.sin(pitch), 0.0, math.cos(pitch)]]

    pose = {"__key__": clip_id}
    vehicle_pose = {"__key__": clip_id}
    last = (len(window) - 1) * BOX_STEP
    for idx in range(last + 3):  # +2 extrapolated frames, as the Waymo converter does
        k, r = divmod(min(idx, last), BOX_STEP)
        a = window[k]["ego"]
        b = window[min(k + 1, len(window) - 1)]["ego"]
        ego = _interp_ego(a, b, r / BOX_STEP)
        if idx > last:  # extrapolate with the last 10 Hz motion
            prev = window[-2]["ego"]
            steps = (idx - last) / BOX_STEP
            ego = {"x": a["x"] + (a["x"] - prev["x"]) * steps, "y": a["y"] + (a["y"] - prev["y"]) * steps, "headingDeg": a["headingDeg"]}
        v2w = _vehicle_to_world(ego)
        c2w = v2w @ camera_to_vehicle
        c2w_opencv = np.concatenate([-c2w[:, 1:2], -c2w[:, 2:3], c2w[:, 0:1], c2w[:, 3:4]], axis=1)
        vehicle_pose[f"{idx:06d}.vehicle_pose.npy"] = v2w
        pose[f"{idx:06d}.pose.front.npy"] = c2w_opencv

    objects = {"__key__": clip_id}
    counts = {"Car": 0, "Pedestrian": 0}
    for k, f in enumerate(window):
        info = {}
        for actor in f["actors"]:
            subtype = actor.get("subtype") or actor["type"]
            height = HEIGHTS.get(subtype, 1.5)
            yaw = math.radians(actor["pose"]["headingDeg"])
            o2w = _yaw_matrix(actor["pose"]["x"], actor["pose"]["y"], height / 2, yaw)
            kind = "Pedestrian" if actor["type"] == "pedestrian" else "Car"
            info[actor["actorId"]] = {
                "object_to_world": o2w.tolist(),
                "object_lwh": [actor["lengthM"], actor["widthM"], height],
                "object_is_moving": bool(actor.get("speedMps", 0.0) > 0.2) and not actor.get("static"),
                "object_type": kind,
            }
            counts[kind] += 1
        objects[f"{k * BOX_STEP:06d}.all_object_info.json"] = info

    geometry = map_from_net(variant_dir / "sumo" / "net.net.xml")
    _write(pose, root / "pose" / f"{clip_id}.tar")
    _write(vehicle_pose, root / "vehicle_pose" / f"{clip_id}.tar")
    _write(objects, root / "all_object_info" / f"{clip_id}.tar")
    for name, items in geometry.items():
        kind = "surface" if name == "crosswalks" else "polyline3d"
        sample = {"__key__": clip_id, f"{name}.json": {"labels": [{"labelData": {"shape3d": {kind: {"vertices": v}}}} for v in items]}}
        _write(sample, root / f"3d_{name}" / f"{clip_id}.tar")
    summary = {"clipId": clip_id, "variantId": telemetry["variantId"], "simWindowS": [window[0]["t"], window[-1]["t"]],
               "poseFrames30Hz": last + 3, "boxFrames10Hz": len(window), "boxCounts": counts,
               "map": {k: len(v) for k, v in geometry.items()}}
    (root / f"{clip_id}.source.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("variant_dir")
    parser.add_argument("rdshq_root")
    parser.add_argument("--clip-id", default=None, help="no dots; defaults to the variant id")
    parser.add_argument("--window-s", type=float, default=12.1, help="12.1 s = 3 renderer chunks of 121 frames at 30 fps")
    args = parser.parse_args()
    variant_dir = Path(args.variant_dir)
    clip_id = (args.clip_id or variant_dir.name).replace(".", "-")
    print(json.dumps(convert(variant_dir, Path(args.rdshq_root), clip_id, args.window_s)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
