"""Export a judged crosswalk variant as ASAM OpenSCENARIO XML 1.1 + OpenDRIVE 1.6.

Inputs are what scripts/augment_run.py writes for a variant: variant.json and telemetry.json (SUMO states every
0.1 s, box-center poses in the map-template frame, x east / y north, heading 0 = east). Outputs:

    road.xodr           the urban-straight road: 2 + 2 lanes, 2 m sidewalks, mid-block zebra crossing at x = 200
    scenario.xosc       replay: every entity, the ego included, follows its SUMO trajectory (what was judged and rendered)
    scenario_sut.xosc   system-under-test: the ego only gets its start pose and speed; everything else is replayed

Entities that SUMO adds later (oncoming cars) wait hidden at separate off-road positions and start their
trajectory when SUMO inserts them; entities SUMO removes (arrivals) are hidden again (VisibilityAction).
Parked cars are static props, as in the variant.
"""

from __future__ import annotations

import io
import json
import math
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Tuple
from html import escape

from augment import crosswalk

LANE = 3.5
SIDEWALK = 2.0
ROAD_LENGTH = 320.0
SAMPLE_S = 0.1  # trajectory vertex spacing = the SUMO step; coarser vertices cut pedestrian corners
HEIGHT = {"vehicle": 1.5, "pedestrian": 1.7}
PED_CATEGORY = {"wheelchair": "wheelchair"}  # everything else is "pedestrian"
README = """OpenSCENARIO export of a SOTIF scenario-generation variant (SUMO-judged crosswalk scenario).

road.xodr          OpenDRIVE 1.6 road (the SUMO urban-straight template): x east, y north, metres.
scenario.xosc      OpenSCENARIO 1.1 replay: all entities, the ego included, follow their SUMO trajectories.
scenario_sut.xosc  OpenSCENARIO 1.1 for a system under test: the ego starts at its SUMO pose and speed and is
                   left to your controller; pedestrians and other vehicles are replayed.
variant.json       the variant parameters and SUMO judgement.

Checked with esmini v3.8.1:  esmini --headless --osc scenario.xosc --fixed_timestep 0.05
"""


def _num(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".") if value % 1 else str(int(value))


# --------------------------------------------------------------------------- OpenDRIVE

def _lane(lane_id: int, lane_type: str, width: float, mark: str) -> str:
    return (f'          <lane id="{lane_id}" type="{lane_type}" level="false"><link/>'
            f'<width sOffset="0" a="{width}" b="0" c="0" d="0"/>'
            f'<roadMark sOffset="0" type="{mark}" weight="standard" color="standard" width="0.15"/></lane>\n')


def road_xodr() -> str:
    """Reference line on the centre line y = 0; right lanes are eastbound, left lanes westbound (right-hand traffic)."""
    left = _lane(3, "sidewalk", SIDEWALK, "none") + _lane(2, "driving", LANE, "solid") + _lane(1, "driving", LANE, "broken")
    right = _lane(-1, "driving", LANE, "broken") + _lane(-2, "driving", LANE, "solid") + _lane(-3, "sidewalk", SIDEWALK, "none")
    crossing_width = 2 * crosswalk.CARRIAGEWAY_HALF
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<OpenDRIVE>
  <header revMajor="1" revMinor="6" name="urban-straight" version="1.0" north="0" south="0" east="0" west="0"/>
  <road name="urban-straight" length="{_num(ROAD_LENGTH)}" id="1" junction="-1" rule="RHT">
    <link/>
    <type s="0" type="town"><speed max="50" unit="km/h"/></type>
    <planView>
      <geometry s="0" x="0" y="0" hdg="0" length="{_num(ROAD_LENGTH)}"><line/></geometry>
    </planView>
    <elevationProfile><elevation s="0" a="0" b="0" c="0" d="0"/></elevationProfile>
    <lateralProfile/>
    <lanes>
      <laneSection s="0">
        <left>
{left}        </left>
        <center>
          <lane id="0" type="none" level="false"><link/><roadMark sOffset="0" type="solid solid" weight="standard" color="yellow" width="0.15"/></lane>
        </center>
        <right>
{right}        </right>
      </laneSection>
    </lanes>
    <objects>
      <object id="crosswalk-1" name="zebra" type="crosswalk" s="{_num(crosswalk.CROSSING_X)}" t="0" zOffset="0" orientation="none"
              length="{_num(2 * crosswalk.CROSSING_HALF_WIDTH)}" width="{_num(crossing_width)}" height="0" hdg="0" pitch="0" roll="0"/>
    </objects>
  </road>
</OpenDRIVE>
"""


# --------------------------------------------------------------------------- tracks from telemetry

def tracks(telemetry: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """actorId -> {type, subtype, lengthM, widthM, samples: [(t, x, y, h_rad, speed)]}; the ego is "Ego"."""
    out: Dict[str, Dict[str, Any]] = {}

    def add(key: str, kind: str, subtype: str, length: float, width: float, t: float, pose: Dict[str, float], speed: float) -> None:
        track = out.setdefault(key, {"type": kind, "subtype": subtype, "lengthM": length, "widthM": width, "samples": []})
        track["samples"].append((round(t, 2), pose["x"], pose["y"], math.radians(pose["headingDeg"]), speed))

    for frame in telemetry["frames"]:
        ego = frame.get("ego")
        if ego:
            add("Ego", "vehicle", "ego", ego["lengthM"], ego["widthM"], frame["t"], ego, ego["speedMps"])
        for actor in frame.get("actors", []):
            add(actor["actorId"], actor["type"], actor.get("subtype", ""), actor["lengthM"], actor["widthM"], frame["t"], actor["pose"], actor["speedMps"])
    return out


def _thin(samples: List[Tuple[float, ...]]) -> List[Tuple[float, ...]]:
    """Keep one vertex per SAMPLE_S plus the last one; headings are unwrapped so interpolation never spins."""
    kept, next_t = [], None
    for sample in samples:
        if next_t is None or sample[0] >= next_t - 1e-6:
            kept.append(sample)
            next_t = sample[0] + SAMPLE_S
    if kept[-1] is not samples[-1]:
        kept.append(samples[-1])
    unwrapped, previous = [], None
    for t, x, y, h, v in kept:
        if previous is not None:
            h = previous + (h - previous + math.pi) % (2 * math.pi) - math.pi
        unwrapped.append((t, x, y, h, v))
        previous = h
    return unwrapped


# --------------------------------------------------------------------------- OpenSCENARIO

def _bbox(length: float, width: float, height: float) -> str:
    return (f'<BoundingBox><Center x="0" y="0" z="{_num(height / 2)}"/>'
            f'<Dimensions width="{_num(width)}" length="{_num(length)}" height="{_num(height)}"/></BoundingBox>')


def _vehicle(name: str, length: float, width: float) -> str:
    axle = length * 0.3
    return f"""    <ScenarioObject name="{name}">
      <Vehicle name="{name}" vehicleCategory="car">
        <Performance maxSpeed="40" maxAcceleration="10" maxDeceleration="10"/>
        {_bbox(length, width, HEIGHT["vehicle"])}
        <Axles>
          <FrontAxle maxSteering="0.5" wheelDiameter="0.6" trackWidth="1.6" positionX="{_num(axle)}" positionZ="0.3"/>
          <RearAxle maxSteering="0" wheelDiameter="0.6" trackWidth="1.6" positionX="{_num(-axle)}" positionZ="0.3"/>
        </Axles>
        <Properties/>
      </Vehicle>
    </ScenarioObject>
"""


def _pedestrian(name: str, kind: str, length: float, width: float) -> str:
    category = PED_CATEGORY.get(kind, "pedestrian")
    return f"""    <ScenarioObject name="{name}">
      <Pedestrian name="{name}" model="{kind}" mass="{80 if kind == 'wheelchair' else 60}" pedestrianCategory="{category}">
        {_bbox(length, width, HEIGHT["pedestrian"])}
        <Properties><Property name="kind" value="{kind}"/></Properties>
      </Pedestrian>
    </ScenarioObject>
"""


def _world(x: float, y: float, h: float) -> str:
    return f'<WorldPosition x="{_num(x)}" y="{_num(y)}" z="0" h="{h:.4f}"/>'


def _private_init(name: str, x: float, y: float, h: float, speed: float, visible: bool) -> str:
    visibility = "" if visible else (
        '        <PrivateAction><VisibilityAction graphics="false" traffic="false" sensors="false"/></PrivateAction>\n')
    return f"""      <Private entityRef="{name}">
        <PrivateAction><TeleportAction><Position>{_world(x, y, h)}</Position></TeleportAction></PrivateAction>
        <PrivateAction><LongitudinalAction><SpeedAction>
          <SpeedActionDynamics dynamicsShape="step" value="0" dynamicsDimension="time"/>
          <SpeedActionTarget><AbsoluteTargetSpeed value="{_num(speed)}"/></SpeedActionTarget>
        </SpeedAction></LongitudinalAction></PrivateAction>
{visibility}      </Private>
"""


def _at_time(name: str, t: float) -> str:
    return (f'<StartTrigger><ConditionGroup><Condition name="{name}" delay="0" conditionEdge="none">'
            f'<ByValueCondition><SimulationTimeCondition value="{_num(t)}" rule="greaterThan"/></ByValueCondition>'
            f'</Condition></ConditionGroup></StartTrigger>')


def _follow_events(name: str, samples: List[Tuple[float, ...]], show_at: float | None, hide_at: float | None) -> str:
    # Time conditions are "greater than" and act on the next step: fire one SUMO step early so the entity is in place.
    lead = 0.1
    start = max(0.0, show_at - lead) if show_at else 0
    vertices = "\n".join(f'                <Vertex time="{_num(t)}"><Position>{_world(x, y, h)}</Position></Vertex>'
                         for t, x, y, h, _ in samples)
    events = f"""          <Event name="{name}-follow" priority="overwrite" maximumExecutionCount="1">
            <Action name="{name}-trajectory"><PrivateAction><RoutingAction><FollowTrajectoryAction>
              <TrajectoryRef><Trajectory name="{name}-sumo" closed="false"><Shape><Polyline>
{vertices}
              </Polyline></Shape></Trajectory></TrajectoryRef>
              <TimeReference><Timing domainAbsoluteRelative="absolute" scale="1" offset="0"/></TimeReference>
              <TrajectoryFollowingMode followingMode="position"/>
            </FollowTrajectoryAction></RoutingAction></PrivateAction></Action>
            {_at_time(f"{name}-follow-start", start)}
          </Event>
"""
    for label, t, visible in (("show", show_at, "true"), ("hide", hide_at, "false")):
        if t is not None:
            events += f"""          <Event name="{name}-{label}" priority="parallel" maximumExecutionCount="1">
            <Action name="{name}-{label}"><PrivateAction><VisibilityAction graphics="{visible}" traffic="{visible}" sensors="{visible}"/></PrivateAction></Action>
            {_at_time(f"{name}-{label}-at", max(0.0, t - lead))}
          </Event>
"""
    return events


def _maneuver_group(name: str, events: str) -> str:
    return f"""        <ManeuverGroup maximumExecutionCount="1" name="{name}-group">
          <Actors selectTriggeringEntities="false"><EntityRef entityRef="{name}"/></Actors>
          <Maneuver name="{name}-maneuver">
{events}          </Maneuver>
        </ManeuverGroup>
"""


def scenario_xosc(variant: Dict[str, Any], telemetry: Dict[str, Any], replay_ego: bool) -> str:
    all_tracks = tracks(telemetry)
    first_t = telemetry["frames"][0]["t"]
    end_t = telemetry["frames"][-1]["t"]
    entities, inits, groups = [], [], []
    staged = 0
    for name, track in all_tracks.items():
        samples = _thin(track["samples"])
        if track["type"] == "pedestrian":
            entities.append(_pedestrian(name, track["subtype"], track["lengthM"], track["widthM"]))
        else:
            entities.append(_vehicle(name, track["lengthM"], track["widthM"]))
        t0, x0, y0, h0, v0 = samples[0]
        show_at = t0 if t0 > first_t + 1e-6 else None
        hide_at = samples[-1][0] + 0.1 if samples[-1][0] < end_t - 1e-6 else None
        if show_at is None:
            inits.append(_private_init(name, x0, y0, h0, v0, visible=True))
        else:  # not inserted yet: park it hidden off the road, apart from the others
            inits.append(_private_init(name, -50.0 - 10.0 * staged, -40.0, 0.0, 0.0, visible=False))
            staged += 1
        if name == "Ego" and not replay_ego:
            continue
        groups.append(_maneuver_group(name, _follow_events(name, samples, show_at, hide_at)))
    for prop in crosswalk.parked_props(variant):
        if prop["actorId"] in all_tracks:  # augment_run.py already writes the parked props into the telemetry
            continue
        name, pose = prop["actorId"], prop["pose"]
        entities.append(_vehicle(name, prop["lengthM"], prop["widthM"]))
        inits.append(_private_init(name, pose["x"], pose["y"], math.radians(pose["headingDeg"]), 0.0, visible=True))

    mode = "replay of every SUMO trajectory" if replay_ego else "ego left to the system under test; other entities replayed"
    description = f"SOTIF crosswalk variant {variant['variantId']}: {mode}"
    judgement = variant.get("sumo") or {}
    parameters = "".join(
        f'    <ParameterDeclaration name="{key}" parameterType="{kind}" value="{value}"/>\n'
        for key, kind, value in (
            ("EgoTargetSpeed", "double", _num(variant["egoSpeedMps"])),
            ("SumoLabel", "string", judgement.get("label", "")),
            ("SumoMinPedDistance", "double", _num(judgement.get("minPedDistanceM") or 0)),
        ))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<OpenSCENARIO>
  <FileHeader revMajor="1" revMinor="1" date="2026-01-01T00:00:00" description="{escape(description, quote=True)}" author="sotif-scenario-generation-on-eks augment/openscenario.py"/>
  <ParameterDeclarations>
{parameters}  </ParameterDeclarations>
  <CatalogLocations/>
  <RoadNetwork>
    <LogicFile filepath="road.xodr"/>
  </RoadNetwork>
  <Entities>
{"".join(entities)}  </Entities>
  <Storyboard>
    <Init>
      <Actions>
{"".join(inits)}      </Actions>
    </Init>
    <Story name="sumo-replay">
      <Act name="replay">
{"".join(groups)}        {_at_time("replay-start", 0)}
      </Act>
    </Story>
    <StopTrigger><ConditionGroup><Condition name="end" delay="0" conditionEdge="rising">
      <ByValueCondition><SimulationTimeCondition value="{_num(end_t)}" rule="greaterThan"/></ByValueCondition>
    </Condition></ConditionGroup></StopTrigger>
  </Storyboard>
</OpenSCENARIO>
"""


def export(variant_dir: Path) -> Dict[str, str]:
    """Files for one augment_run.py variant directory (variant.json, telemetry.json, result.json)."""
    variant = json.loads((variant_dir / "variant.json").read_text())
    telemetry = json.loads((variant_dir / "telemetry.json").read_text())
    result_path = variant_dir / "result.json"
    if result_path.exists():
        variant = {**variant, "sumo": json.loads(result_path.read_text())}
    return {
        "road.xodr": road_xodr(),
        "scenario.xosc": scenario_xosc(variant, telemetry, replay_ego=True),
        "scenario_sut.xosc": scenario_xosc(variant, telemetry, replay_ego=False),
        "variant.json": json.dumps(variant, ensure_ascii=False, indent=2) + "\n",
        "README.txt": README,
    }


def export_zip(variant_dir: Path) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in export(variant_dir).items():
            archive.writestr(name, text)
    return buffer.getvalue()
