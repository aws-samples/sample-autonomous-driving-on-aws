"""Crosswalk-yield variant space.

A variant is a flat JSON document. Every field has hard bounds so that any
variant accepted by `normalize` compiles into a SUMO scenario that runs; whether
the intended situation actually happens is measured afterwards by `Metrics`.

Map: urban-straight (scripts/compile_sumo.py). Ego drives eastbound on eb lane 1
(y = -5.25). Mid-block crossing at x = 200, width 3.5 m, between eb1 (x 100..200)
and wb1 (x 200..100). Parked cars are visual props only: SUMO pedestrians and the
ego have perfect knowledge, so occlusion changes the condition video, not behavior.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

MAP_ID = "urban-straight"
CROSSING_X = 200.0
CROSSING_HALF_WIDTH = 1.75
EGO_LANE_Y = -5.25
CARRIAGEWAY_HALF = 7.0  # two 3.5 m lanes each way
EGO_DEPART_X = 20.0
EGO_LENGTH = 4.8
EGO_WIDTH = 1.8
SIM_END = 70.0
STEP = 0.1
EDGE_SPEED = 13.9
CRITICAL_TTC = 1.5
CRITICAL_DISTANCE = 1.5

PED_KINDS = {
    "adult": {"width": 0.6, "length": 0.4},
    "child": {"width": 0.4, "length": 0.3},
    "elderly": {"width": 0.6, "length": 0.4},
    "runner": {"width": 0.6, "length": 0.4},
    "wheelchair": {"width": 0.8, "length": 1.1},
    "stroller": {"width": 0.7, "length": 1.4},
}

# field -> (min, max); the documented contract for GenAI output
BOUNDS = {
    "egoSpeedMps": (6.0, 18.0),
    "oncomingPeriodS": (4.0, 30.0),
    "parkedCount": (0, 6),
    "parkedEndX": (170.0, 196.0),
    "pedCount": (1, 4),
    "ped.departS": (0.0, 40.0),
    "ped.approachM": (1.0, 15.0),
    "ped.speedMps": (0.5, 3.0),
}


def _clamp(value: float, key: str, notes: List[str], label: str) -> float:
    lo, hi = BOUNDS[key]
    clamped = min(hi, max(lo, value))
    if clamped != value:
        notes.append(f"{label}: {value} -> {clamped}")
    return clamped


def normalize(raw: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """Coerce a raw (possibly LLM-written) variant into bounds. Returns (variant, adjustment notes)."""
    notes: List[str] = []
    ego = _clamp(float(raw.get("egoSpeedMps", 11.0)), "egoSpeedMps", notes, "egoSpeedMps")
    period = raw.get("oncomingPeriodS", 7.0)
    period = None if period is None else _clamp(float(period), "oncomingPeriodS", notes, "oncomingPeriodS")
    parked = int(_clamp(int(raw.get("parkedCount", 0)), "parkedCount", notes, "parkedCount"))
    parked_end = _clamp(float(raw.get("parkedEndX", 186.0)), "parkedEndX", notes, "parkedEndX")
    peds_raw = list(raw.get("pedestrians") or [])
    if not peds_raw:
        notes.append("pedestrians: empty -> 1 default adult")
        peds_raw = [{}]
    if len(peds_raw) > BOUNDS["pedCount"][1]:
        notes.append(f"pedestrians: {len(peds_raw)} -> {BOUNDS['pedCount'][1]}")
        peds_raw = peds_raw[: BOUNDS["pedCount"][1]]
    peds = []
    for index, p in enumerate(peds_raw):
        kind = p.get("kind", "adult")
        if kind not in PED_KINDS:
            notes.append(f"ped{index}.kind: {kind} -> adult")
            kind = "adult"
        direction = p.get("direction", "S2N")
        if direction not in ("S2N", "N2S"):
            notes.append(f"ped{index}.direction: {direction} -> S2N")
            direction = "S2N"
        peds.append({
            "kind": kind,
            "direction": direction,
            "departS": round(_clamp(float(p.get("departS", 13.0)), "ped.departS", notes, f"ped{index}.departS"), 2),
            "approachM": round(_clamp(float(p.get("approachM", 2.0)), "ped.approachM", notes, f"ped{index}.approachM"), 2),
            "speedMps": round(_clamp(float(p.get("speedMps", 1.3)), "ped.speedMps", notes, f"ped{index}.speedMps"), 2),
        })
    variant = {
        "variantId": str(raw.get("variantId") or "variant"),
        "baseScenario": "crosswalk",
        "rationale": str(raw.get("rationale", "")),
        "ontology": raw.get("ontology", {}),
        "egoSpeedMps": round(ego, 2),
        "oncomingPeriodS": None if period is None else round(period, 2),
        "parkedCount": parked,
        "parkedEndX": round(parked_end, 2),
        "pedestrians": peds,
        "seed": int(raw.get("seed", 42)),
    }
    return variant, notes


def routes_xml(variant: Dict[str, Any]) -> str:
    speed_factor = max(1.0, variant["egoSpeedMps"] / EDGE_SPEED)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<routes>",
        '  <vType id="traffic" vClass="passenger" length="4.8" width="1.8" minGap="2.5" accel="2.6" decel="4.5"'
        ' emergencyDecel="8.0" sigma="0.15" maxSpeed="13.9" speedFactor="1.0"/>',
        f'  <vType id="ego-type" vClass="passenger" length="{EGO_LENGTH}" width="{EGO_WIDTH}" minGap="2.5" accel="2.8"'
        f' decel="4.8" emergencyDecel="8.5" sigma="0.0" maxSpeed="{variant["egoSpeedMps"]:.2f}"'
        f' speedFactor="{speed_factor:.3f}" color="0,120,255"/>',
    ]
    for index, p in enumerate(variant["pedestrians"]):
        dims = PED_KINDS[p["kind"]]
        lines.append(f'  <vType id="ped-{index}" vClass="pedestrian" width="{dims["width"]}" length="{dims["length"]}"'
                     f' speed="{p["speedMps"]:.2f}"/>')
    lines += [
        '  <route id="ego-route" edges="eb0 eb1 eb2 eb3"/>',
        '  <route id="wb" edges="wb3 wb2 wb1 wb0"/>',
        f'  <vehicle id="ego" type="ego-type" route="ego-route" depart="0.0" departLane="1" departPos="{EGO_DEPART_X:.0f}"'
        f' departSpeed="{min(variant["egoSpeedMps"], EDGE_SPEED * speed_factor):.2f}" arrivalPos="max"/>',
    ]
    if variant["oncomingPeriodS"] is not None:
        lines.append(f'  <flow id="wb" type="traffic" route="wb" begin="0.0" end="{SIM_END:.0f}" period="{variant["oncomingPeriodS"]:.2f}"'
                     ' departLane="best" departSpeed="max"/>')
    # persons must be sorted by depart time inside a route file
    order = sorted(range(len(variant["pedestrians"])), key=lambda i: variant["pedestrians"][i]["departS"])
    for index in order:
        p = variant["pedestrians"][index]
        if p["direction"] == "S2N":  # eb1 south sidewalk -> crossing -> wb1 north sidewalk
            frm, to, depart_pos, arrival_pos = "eb1", "wb1", 100.0 - p["approachM"], 10.0
        else:
            frm, to, depart_pos, arrival_pos = "wb1", "eb1", p["approachM"], 90.0
        lines += [
            f'  <person id="ped-{index}" type="ped-{index}" depart="{p["departS"]:.2f}" departPos="{depart_pos:.2f}">',
            f'    <walk from="{frm}" to="{to}" arrivalPos="{arrival_pos:.2f}"/>',
            "  </person>",
        ]
    lines.append("</routes>")
    return "\n".join(lines) + "\n"


def parked_props(variant: Dict[str, Any]) -> List[Dict[str, Any]]:
    props = []
    for index in range(variant["parkedCount"]):
        x = variant["parkedEndX"] - index * 7.0
        props.append({"actorId": f"parked-{index}", "type": "vehicle", "subtype": "parked", "static": True,
                      "pose": {"x": round(x, 2), "y": -8.75, "z": 0.0, "headingDeg": 0.0},
                      "speedMps": 0.0, "lengthM": 4.8, "widthM": 1.8})
    return props


@dataclass
class Metrics:
    """Pedestrian-aware criticality; SUMO's leader TTC ignores persons."""

    min_ped_distance: Optional[float] = None
    min_ped_ttc: Optional[float] = None
    conflict_steps: int = 0
    yielded: bool = False
    max_decel: float = 0.0
    collisions: List[str] = field(default_factory=list)
    ego_arrival_s: Optional[float] = None

    def update(self, t: float, ego: Optional[Dict[str, float]], peds: List[Dict[str, Any]]) -> None:
        if ego is None:
            return
        self.max_decel = max(self.max_decel, -ego["accelerationMps2"])
        h = math.radians(ego["headingDeg"])
        front_x = ego["x"] + math.cos(h) * EGO_LENGTH / 2
        front_y = ego["y"] + math.sin(h) * EGO_LENGTH / 2
        for p in peds:
            px, py = p["pose"]["x"], p["pose"]["y"]
            d = math.hypot(px - front_x, py - front_y)
            self.min_ped_distance = d if self.min_ped_distance is None else min(self.min_ped_distance, d)
            on_ego_half = -CARRIAGEWAY_HALF <= py <= 0.0 and abs(px - CROSSING_X) <= CROSSING_HALF_WIDTH + 0.5
            approaching = 0.0 <= CROSSING_X - CROSSING_HALF_WIDTH - front_x <= 40.0
            if on_ego_half and approaching:
                self.conflict_steps += 1
                if ego["speedMps"] < 0.3:
                    self.yielded = True
            in_path = abs(py - ego["y"]) <= EGO_WIDTH / 2 + 0.6 and px > front_x
            if in_path and ego["speedMps"] > 0.1:
                ttc = (px - front_x) / ego["speedMps"]
                self.min_ped_ttc = ttc if self.min_ped_ttc is None else min(self.min_ped_ttc, ttc)

    def verdict(self, sim_ok: bool) -> Dict[str, Any]:
        """simulatable: ran to the goal. situation: the ego actually met a pedestrian on the crossing."""
        situation = self.conflict_steps > 0
        if not sim_ok:
            label = "invalid"
        elif self.collisions:
            label = "collision"
        elif not situation:
            label = "no-interaction"
        # max_decel is reported but not used: SUMO brakes at full decel before every priority
        # crossing even when it is empty (yield-link approach), so it does not measure the pedestrian
        elif (self.min_ped_ttc is not None and self.min_ped_ttc < CRITICAL_TTC) or (
                self.min_ped_distance is not None and self.min_ped_distance < CRITICAL_DISTANCE):
            label = "critical"
        elif self.yielded:
            label = "nominal-yield"
        else:
            label = "interaction"
        return {
            "label": label,
            "keep": label in ("critical", "nominal-yield", "interaction", "collision"),
            "minPedDistanceM": None if self.min_ped_distance is None else round(self.min_ped_distance, 2),
            "minPedTtcS": None if self.min_ped_ttc is None else round(self.min_ped_ttc, 2),
            "conflictSeconds": round(self.conflict_steps * STEP, 1),
            "yielded": self.yielded,
            "maxDecelMps2": round(self.max_decel, 2),
            "collisions": self.collisions,
            "egoArrivalS": self.ego_arrival_s,
        }
