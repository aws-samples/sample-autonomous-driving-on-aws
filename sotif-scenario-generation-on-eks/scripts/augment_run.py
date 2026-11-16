#!/usr/bin/env python3
"""Run a batch of augmentation variants headless in SUMO and keep the ones that are simulatable and meaningful.

    python3 scripts/augment_run.py <variants.json> <outDir>

variants.json: list of raw variants (see augment/crosswalk.py). Each is normalized into bounds,
compiled to out/<variantId>/sumo, run through TraCI without pacing, and scored. Writes
out/<variantId>/{variant.json,telemetry.json,result.json}, out/results.json and out/summary.md.
Telemetry poses are box centers (SUMO reports the front bumper for vehicles).

Run inside the pinned SUMO container:
  docker run --rm -u $(id -u):$(id -g) -v "$PWD":/work -w /work drive-atlas-sumo:<tag> python3 scripts/augment_run.py ...
"""

from __future__ import annotations

import json
import math
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

import traci  # noqa: E402

import compile_sumo  # noqa: E402
from augment import crosswalk as space  # noqa: E402


def _center(x: float, y: float, angle: float, length: float) -> Dict[str, float]:
    heading = 90.0 - angle
    h = math.radians(heading)
    return {"x": round(x - math.cos(h) * length / 2, 3), "y": round(y - math.sin(h) * length / 2, 3), "z": 0.0,
            "headingDeg": round((heading + 180.0) % 360.0 - 180.0, 3)}


def run_variant(variant: Dict[str, Any], net_src: Path, out: Path) -> Dict[str, Any]:
    sumo_dir = out / "sumo"
    sumo_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(net_src, sumo_dir / "net.net.xml")
    (sumo_dir / "routes.rou.xml").write_text(space.routes_xml(variant))
    (sumo_dir / "scenario.sumocfg").write_text(compile_sumo.sumocfg("net.net.xml", "routes.rou.xml", space.SIM_END, variant["seed"]))
    label = variant["variantId"]
    traci.start(["sumo", "-c", str(sumo_dir / "scenario.sumocfg"), "--log", str(out / "sumo.log")], label=label)
    conn = traci.getConnection(label)
    props = space.parked_props(variant)
    metrics = space.Metrics()
    frames: List[Dict[str, Any]] = []
    error = None
    try:
        while conn.simulation.getTime() < space.SIM_END - 1e-6:
            conn.simulationStep()
            t = round(conn.simulation.getTime(), 2)
            for c in conn.simulation.getCollisions():
                if "ego" in (c.collider, c.victim):
                    metrics.collisions.append(f"{t}:{c.collider}->{c.victim}:{c.type}")
            if "ego" in conn.simulation.getArrivedIDList():
                metrics.ego_arrival_s = t
            ego = None
            actors: List[Dict[str, Any]] = []
            for vid in conn.vehicle.getIDList():
                x, y = conn.vehicle.getPosition(vid)
                length = float(conn.vehicle.getLength(vid))
                pose = _center(x, y, float(conn.vehicle.getAngle(vid)), length)
                speed = max(0.0, float(conn.vehicle.getSpeed(vid)))
                if vid == "ego":
                    ego = {**pose, "speedMps": round(speed, 3), "accelerationMps2": round(float(conn.vehicle.getAcceleration(vid)), 3),
                           "lengthM": length, "widthM": float(conn.vehicle.getWidth(vid))}
                else:
                    actors.append({"actorId": vid, "type": "vehicle", "subtype": "car", "pose": pose, "speedMps": round(speed, 3),
                                   "lengthM": length, "widthM": float(conn.vehicle.getWidth(vid))})
            peds = []
            for pid in conn.person.getIDList():
                x, y = conn.person.getPosition(pid)
                peds.append({"actorId": pid, "type": "pedestrian", "subtype": variant["pedestrians"][int(pid.split("-")[1])]["kind"],
                             "pose": {"x": round(x, 3), "y": round(y, 3), "z": 0.0, "headingDeg": round(90.0 - float(conn.person.getAngle(pid)), 3)},
                             "speedMps": round(max(0.0, float(conn.person.getSpeed(pid))), 3),
                             "lengthM": float(conn.person.getLength(pid)), "widthM": float(conn.person.getWidth(pid))})
            metrics.update(t, ego, peds)
            frames.append({"t": t, "ego": ego, "actors": actors + peds + props})
            if metrics.ego_arrival_s is not None:
                break
        teleports = int(conn.simulation.getStartingTeleportNumber())
    except traci.TraCIException as exc:  # a malformed variant must not stop the batch
        error, teleports = str(exc), 0
    finally:
        conn.close()
    sim_ok = error is None and metrics.ego_arrival_s is not None and teleports == 0
    result = {"variantId": label, "simulatable": sim_ok, "error": error, "teleports": teleports, "frames": len(frames),
              **metrics.verdict(sim_ok)}
    (out / "telemetry.json").write_text(json.dumps({"variantId": label, "map": space.MAP_ID, "stepS": space.STEP,
                                                    "poseRef": "box-center", "frames": frames}))
    (out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


def summary_md(rows: List[Dict[str, Any]], variants: Dict[str, Dict[str, Any]]) -> str:
    head = "| variant | 보행자 | ego m/s | 결과 | 유지 | 최소거리 m | 보행자 TTC s | 최대감속 | 양보 |\n|---|---|---|---|---|---|---|---|---|\n"
    body = ""
    for r in rows:
        v = variants[r["variantId"]]
        peds = ", ".join(f"{p['kind']}/{p['direction']}/{p['departS']}s/{p['speedMps']}" for p in v["pedestrians"])
        body += (f"| {r['variantId']} | {peds} | {v['egoSpeedMps']} | {r['label']} | {'O' if r['keep'] else 'X'} | "
                 f"{r['minPedDistanceM']} | {r['minPedTtcS']} | {r['maxDecelMps2']} | {'O' if r['yielded'] else 'X'} |\n")
    return head + body


def main(argv: List[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    raw_variants = json.loads(Path(argv[1]).read_text())
    out_root = Path(argv[2])
    out_root.mkdir(parents=True, exist_ok=True)
    net_src = compile_sumo.build_net(space.MAP_ID, out_root / "_net")
    rows, variants = [], {}
    for index, raw in enumerate(raw_variants):
        raw.setdefault("variantId", f"v{index:03d}")
        variant, notes = space.normalize(raw)
        variant["adjustments"] = notes
        variants[variant["variantId"]] = variant
        out = out_root / variant["variantId"]
        out.mkdir(parents=True, exist_ok=True)
        (out / "variant.json").write_text(json.dumps(variant, ensure_ascii=False, indent=2) + "\n")
        result = run_variant(variant, net_src, out)
        rows.append(result)
        print(f"{variant['variantId']}: {result['label']} keep={result['keep']} minDist={result['minPedDistanceM']} ttc={result['minPedTtcS']}")
    (out_root / "results.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n")
    (out_root / "summary.md").write_text(summary_md(rows, variants))
    kept = sum(r["keep"] for r in rows)
    print(f"{kept}/{len(rows)} kept -> {out_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
