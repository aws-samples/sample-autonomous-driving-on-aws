#!/usr/bin/env python3
"""Compile scenario specs into runnable SUMO scenarios.

For each scenario whose spec marks SUMO as available/planned, write
scenarios/<id>/sumo/{nodes.nod.xml, edges.edg.xml, connections.con.xml,
routes.rou.xml, scenario.sumocfg, sumo.json} and run netconvert with
coordinate normalization disabled so SUMO positions equal the map template
meters used by the renderer.

sumo.json is the sidecar the generic SUMO worker reads: ego id, route edges,
route length, goal condition, TraCI hooks (lane closures, fault injection),
and the simulation horizon.

Run inside the pinned SUMO container (netconvert + sumolib required):
  docker run --rm -v "$PWD":/work -w /work drive-atlas-sumo:<tag> python3 scripts/compile_sumo.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parents[1]
LANE = 3.5
SIDEWALK = 2.0

XML_HEAD = '<?xml version="1.0" encoding="UTF-8"?>\n'

VTYPES = """  <vType id="traffic" vClass="passenger" length="4.8" width="1.8" minGap="2.5"
         accel="2.6" decel="4.5" emergencyDecel="8.0" sigma="0.15"
         maxSpeed="{traffic_max}" speedFactor="1.0" lcStrategic="1.0" lcCooperative="0.8"/>
  <vType id="truck" vClass="truck" length="12.0" width="2.5" minGap="3.0"
         accel="1.2" decel="4.0" emergencyDecel="7.0" sigma="0.1"
         maxSpeed="{truck_max}" speedFactor="1.0" color="0.9,0.85,0.7"/>
  <vType id="ego-type" vClass="passenger" length="4.8" width="1.8" minGap="2.5"
         accel="2.8" decel="4.8" emergencyDecel="8.5" sigma="0.0"
         maxSpeed="{ego_max}" speedFactor="1.0" color="0,120,255" lcStrategic="1.0" lcCooperative="1.0" lcSpeedGain="1.5"/>
  <vType id="ped" vClass="pedestrian" width="0.6" length="0.4" speed="1.3" color="0.33,0.38,0.42"/>
"""


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def sumocfg(net: str, routes: str, end: float, seed: int = 42) -> str:
    return f"""{XML_HEAD}<configuration xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
               xsi:noNamespaceSchemaLocation="https://sumo.dlr.de/xsd/sumoConfiguration.xsd">
  <input>
    <net-file value="{net}"/>
    <route-files value="{routes}"/>
  </input>
  <time>
    <begin value="0.0"/>
    <end value="{end:.1f}"/>
    <step-length value="0.1"/>
  </time>
  <processing>
    <time-to-teleport value="-1"/>
    <collision.action value="warn"/>
    <collision.check-junctions value="true"/>
    <pedestrian.model value="striping"/>
  </processing>
  <random_number>
    <seed value="{seed}"/>
  </random_number>
  <report>
    <verbose value="false"/>
    <no-step-log value="true"/>
    <no-warnings value="true"/>
    <duration-log.statistics value="true"/>
  </report>
</configuration>
"""


# --------------------------------------------------------------------------- networks

def net_urban_intersection() -> Dict[str, str]:
    ext = 110.0
    nodes = f"""{XML_HEAD}<nodes>
  <node id="C" x="0" y="0" type="traffic_light" tl="C"/>
  <node id="W" x="{-ext}" y="0" type="dead_end"/>
  <node id="E" x="{ext}" y="0" type="dead_end"/>
  <node id="S" x="0" y="{-ext}" type="dead_end"/>
  <node id="N" x="0" y="{ext}" type="dead_end"/>
</nodes>
"""
    edge_attrs = f'numLanes="2" speed="13.9" width="{LANE}" sidewalkWidth="{SIDEWALK}" spreadType="right" priority="3"'
    edges = f"""{XML_HEAD}<edges>
  <edge id="w_in"  from="W" to="C" {edge_attrs}/>
  <edge id="e_out" from="C" to="E" {edge_attrs}/>
  <edge id="e_in"  from="E" to="C" {edge_attrs}/>
  <edge id="w_out" from="C" to="W" {edge_attrs}/>
  <edge id="s_in"  from="S" to="C" {edge_attrs}/>
  <edge id="n_out" from="C" to="N" {edge_attrs}/>
  <edge id="n_in"  from="N" to="C" {edge_attrs}/>
  <edge id="s_out" from="C" to="S" {edge_attrs}/>
</edges>
"""
    # crossings on all four approaches; vehicle lanes are 1 and 2 (0 = sidewalk)
    connections = f"""{XML_HEAD}<connections>
  <crossing node="C" edges="w_in w_out" width="3.0"/>
  <crossing node="C" edges="e_in e_out" width="3.0"/>
  <crossing node="C" edges="n_in n_out" width="3.0"/>
  <crossing node="C" edges="s_in s_out" width="3.0"/>
</connections>
"""
    return {"nodes": nodes, "edges": edges, "connections": connections}


def net_urban_straight() -> Dict[str, str]:
    xs = [0.0, 100.0, 200.0, 250.0, 320.0]
    node_lines = []
    for i, x in enumerate(xs):
        kind = "dead_end" if i in (0, len(xs) - 1) else "priority"
        node_lines.append(f'  <node id="n{i}" x="{x}" y="0" type="{kind}"/>')
    nodes = f"{XML_HEAD}<nodes>\n" + "\n".join(node_lines) + "\n</nodes>\n"
    edge_attrs = f'numLanes="2" speed="13.9" width="{LANE}" sidewalkWidth="{SIDEWALK}" spreadType="right" priority="3"'
    edge_lines = []
    for i in range(len(xs) - 1):
        edge_lines.append(f'  <edge id="eb{i}" from="n{i}" to="n{i + 1}" {edge_attrs}/>')
        edge_lines.append(f'  <edge id="wb{i}" from="n{i + 1}" to="n{i}" {edge_attrs}/>')
    edges = f"{XML_HEAD}<edges>\n" + "\n".join(edge_lines) + "\n</edges>\n"
    # mid-block crossing at x=200 (node n2)
    connections = f"""{XML_HEAD}<connections>
  <crossing node="n2" edges="eb1 wb1" width="3.5" priority="true"/>
</connections>
"""
    return {"nodes": nodes, "edges": edges, "connections": connections}


def net_highway_3lane() -> Dict[str, str]:
    # Edge shape is the left border (spreadType=right): lanes fill southwards.
    # lane_3 y=8.75, lane_2 y=5.25, lane_1 y=1.75 (drive), lane_0 y=-1.5 shoulder (3.0 m)
    top = 3 * LANE
    nodes = f"""{XML_HEAD}<nodes>
  <node id="h0" x="0" y="{top}" type="dead_end"/>
  <node id="h1" x="1000" y="{top}" type="dead_end"/>
</nodes>
"""
    edges = f"""{XML_HEAD}<edges>
  <edge id="hw" from="h0" to="h1" numLanes="4" speed="30.0" spreadType="right" priority="3">
    <lane index="0" width="3.0" allow="emergency" speed="8.0"/>
    <lane index="1" width="{LANE}"/>
    <lane index="2" width="{LANE}"/>
    <lane index="3" width="{LANE}"/>
  </edge>
</edges>
"""
    return {"nodes": nodes, "edges": edges, "connections": f"{XML_HEAD}<connections/>\n"}


NETWORKS = {
    "urban-intersection": net_urban_intersection,
    "urban-straight": net_urban_straight,
    "highway-3lane": net_highway_3lane,
}


# --------------------------------------------------------------------------- scenarios

def routes_urban_turn() -> str:
    return f"""{XML_HEAD}<routes>
{VTYPES.format(traffic_max=13.9, truck_max=12.0, ego_max=12.0)}
  <route id="ego-route" edges="w_in n_out"/>
  <route id="oncoming" edges="e_in w_out"/>
  <route id="north" edges="s_in n_out"/>
  <route id="south" edges="n_in s_out"/>
  <route id="east" edges="w_in e_out"/>

  <vehicle id="ego" type="ego-type" route="ego-route" depart="0.0" departLane="2" departPos="10" departSpeed="11.0" arrivalPos="max"/>
  <flow id="oncoming" type="traffic" route="oncoming" begin="2.5" end="80.0" period="6.0" departLane="best" departSpeed="max"/>
  <flow id="north" type="traffic" route="north" begin="1.0" end="80.0" period="9.0" departLane="best" departSpeed="max"/>
  <flow id="south" type="traffic" route="south" begin="3.0" end="80.0" period="9.0" departLane="best" departSpeed="max"/>
  <flow id="east" type="traffic" route="east" begin="14.0" end="80.0" period="12.0" departLane="1" departSpeed="max"/>

  <person id="ped-north" type="ped" depart="4.0" departPos="6">
    <walk from="n_out" to="n_in" arrivalPos="6"/>
  </person>
  <person id="ped-north-2" type="ped" depart="16.0" departPos="6">
    <walk from="n_in" to="n_out" arrivalPos="6"/>
  </person>
</routes>
"""


def routes_crosswalk() -> str:
    return f"""{XML_HEAD}<routes>
{VTYPES.format(traffic_max=13.9, truck_max=12.0, ego_max=12.0)}
  <route id="ego-route" edges="eb0 eb1 eb2 eb3"/>
  <route id="wb" edges="wb3 wb2 wb1 wb0"/>

  <vehicle id="ego" type="ego-type" route="ego-route" depart="0.0" departLane="1" departPos="20" departSpeed="11.0" arrivalPos="max"/>
  <flow id="wb" type="traffic" route="wb" begin="0.0" end="60.0" period="7.0" departLane="best" departSpeed="max"/>

  <person id="ped-occluded" type="ped" depart="13.0" departPos="98">
    <walk from="eb1" to="wb1" arrivalPos="98"/>
  </person>
</routes>
"""


def routes_construction() -> str:
    return f"""{XML_HEAD}<routes>
{VTYPES.format(traffic_max=13.9, truck_max=12.0, ego_max=12.5)}
  <route id="ego-route" edges="eb0 eb1 eb2 eb3"/>
  <route id="eb" edges="eb0 eb1 eb2 eb3"/>
  <route id="wb" edges="wb3 wb2 wb1 wb0"/>

  <vehicle id="ego" type="ego-type" route="ego-route" depart="0.0" departLane="1" departPos="10" departSpeed="12.0" arrivalPos="max"/>
  <flow id="eb" type="traffic" route="eb" begin="4.0" end="60.0" period="10.0" departLane="best" departSpeed="max"/>
  <flow id="wb" type="traffic" route="wb" begin="0.0" end="60.0" period="8.0" departLane="best" departSpeed="max"/>
</routes>
"""


def routes_overtake() -> str:
    return f"""{XML_HEAD}<routes>
{VTYPES.format(traffic_max=30.0, truck_max=17.0, ego_max=30.0)}
  <route id="hw" edges="hw"/>

  <vehicle id="slow-truck" type="truck" route="hw" depart="0.0" departLane="1" departPos="90" departSpeed="17.0" arrivalPos="max"/>
  <vehicle id="ego" type="ego-type" route="hw" depart="0.0" departLane="1" departPos="0" departSpeed="25.0" arrivalPos="max"/>
  <vehicle id="rear-fast" type="traffic" route="hw" depart="1.0" departLane="2" departPos="0" departSpeed="30.0" arrivalPos="max"/>
  <flow id="l2" type="traffic" route="hw" begin="6.0" end="40.0" period="6.0" departLane="2" departSpeed="max"/>
  <flow id="l3" type="traffic" route="hw" begin="0.0" end="40.0" period="5.0" departLane="3" departSpeed="max"/>
</routes>
"""


def routes_minimal_risk() -> str:
    return f"""{XML_HEAD}<routes>
{VTYPES.format(traffic_max=30.0, truck_max=17.0, ego_max=28.0)}
  <route id="hw" edges="hw"/>

  <vehicle id="ego" type="ego-type" route="hw" depart="0.0" departLane="2" departPos="0" departSpeed="27.0" arrivalPos="max"/>
  <flow id="l1" type="traffic" route="hw" begin="0.0" end="60.0" period="7.0" departLane="1" departSpeed="max"/>
  <flow id="l2" type="traffic" route="hw" begin="3.0" end="60.0" period="6.0" departLane="2" departSpeed="max"/>
  <flow id="l3" type="traffic" route="hw" begin="1.0" end="60.0" period="5.0" departLane="3" departSpeed="max"/>
</routes>
"""


SCENARIOS = {
    "urban-turn": {
        "map": "urban-intersection",
        "routes": routes_urban_turn,
        "end": 90.0,
        "egoRouteEdges": ["w_in", "n_out"],
        "goal": {"kind": "arrive", "requiredEdge": "n_out"},
        # start in the east-west green so the ego reaches the junction on a permissive left, not on red
        "hooks": [{"at": "start", "op": "setPhase", "tls": "C", "phase": 3}],
    },
    "crosswalk": {
        "map": "urban-straight",
        "routes": routes_crosswalk,
        "end": 70.0,
        "egoRouteEdges": ["eb0", "eb1", "eb2", "eb3"],
        "goal": {"kind": "arrive"},
        "hooks": [],
    },
    "construction": {
        "map": "urban-straight",
        "routes": routes_construction,
        "end": 70.0,
        "egoRouteEdges": ["eb0", "eb1", "eb2", "eb3"],
        "goal": {"kind": "arrive"},
        # close the right vehicle lane (index 1; index 0 is the sidewalk) between x=100 and x=250
        "hooks": [{"at": "start", "op": "closeLanes", "lanes": ["eb1_1", "eb2_1"], "speedLimit": 8.0, "edges": ["eb1", "eb2"]}],
    },
    "overtake": {
        "map": "highway-3lane",
        "routes": routes_overtake,
        "end": 60.0,
        "egoRouteEdges": ["hw"],
        "goal": {"kind": "arrive", "mustPass": "slow-truck"},
        "hooks": [],
    },
    "minimal-risk": {
        "map": "highway-3lane",
        "routes": routes_minimal_risk,
        "end": 70.0,
        "egoRouteEdges": ["hw"],
        "goal": {"kind": "stopped-on-lane", "laneId": "hw_0"},
        "hooks": [{"at": "egoX>=120", "op": "minimalRisk", "targetLaneIndex": 0, "stopSpeed": 0.0, "decelSeconds": 14.0}],
    },
}


def route_length(net_path: Path, edges: List[str]) -> float:
    import sumolib  # available in the SUMO container

    net = sumolib.net.readNet(str(net_path))
    return round(sum(net.getEdge(e).getLength() for e in edges), 3)


def build_net(map_id: str, out: Path) -> Path:
    """Write the node/edge/connection files for a map template and netconvert them into out/net.net.xml."""
    out.mkdir(parents=True, exist_ok=True)
    files = NETWORKS[map_id]()
    write(out / "nodes.nod.xml", files["nodes"])
    write(out / "edges.edg.xml", files["edges"])
    write(out / "connections.con.xml", files["connections"])
    net_path = out / "net.net.xml"
    subprocess.run([
        "netconvert",
        "--node-files", str(out / "nodes.nod.xml"),
        "--edge-files", str(out / "edges.edg.xml"),
        "--connection-files", str(out / "connections.con.xml"),
        "--output-file", str(net_path),
        "--offset.disable-normalization", "true",
        "--no-turnarounds", "true",
        "--junctions.corner-detail", "5",
        "--tls.default-type", "static",
        "--tls.green.time", "35",
        "--tls.yellow.time", "3",
        "--crossings.guess", "false",
        "--sidewalks.guess", "false",
        "--no-warnings", "true",
    ], check=True, capture_output=True, text=True)
    return net_path


def compile_scenario(scenario_id: str, cfg: Dict) -> None:
    out = ROOT / "scenarios" / scenario_id / "sumo"
    net_path = build_net(cfg["map"], out)
    write(out / "routes.rou.xml", cfg["routes"]())
    write(out / "scenario.sumocfg", sumocfg("net.net.xml", "routes.rou.xml", cfg["end"]))
    sidecar = {
        "version": "1.0",
        "scenarioId": scenario_id,
        "map": cfg["map"],
        "coordinateSystem": "MAP_TEMPLATE_METERS",
        "config": "scenario.sumocfg",
        "egoId": "ego",
        "egoRouteEdges": cfg["egoRouteEdges"],
        "routeLengthM": route_length(net_path, cfg["egoRouteEdges"]),
        "simulationEnd": cfg["end"],
        "goal": cfg["goal"],
        "hooks": cfg["hooks"],
        "seed": 42,
    }
    write(out / "sumo.json", json.dumps(sidecar, ensure_ascii=False, indent=2) + "\n")
    print(f"compiled {scenario_id}: route {sidecar['routeLengthM']} m over {cfg['egoRouteEdges']}")


def main(argv: List[str]) -> int:
    if shutil.which("netconvert") is None:
        print("netconvert not found; run inside the pinned SUMO container", file=sys.stderr)
        return 2
    wanted = argv[1:] or list(SCENARIOS)
    for scenario_id in wanted:
        compile_scenario(scenario_id, SCENARIOS[scenario_id])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
