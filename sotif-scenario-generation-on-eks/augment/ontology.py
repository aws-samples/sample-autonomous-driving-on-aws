"""Ontology slice for crosswalk augmentation, read from the SOTIF Neptune graph.

Without DRIVE_ATLAS_NEPTUNE_SPARQL (or if Neptune is empty) the slice comes from ontology_snapshot.json,
which scripts/build_ontology.sh derives from sotif/ontology.nt.

Each ontology instance is tagged with how the variant space can honour it:
  simulated       changes SUMO behavior (actor kind, speed, timing)
  visual-only     changes the scene (props) but not SUMO behavior
  condition-only  recorded for the condition video / generation prompt; no simulation effect
  target          an outcome the variant may aim for; measured by the SUMO run, never asserted
  unsupported     not expressible in this variant space yet
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Dict, List

from sotif import neptune

NEPTUNE_SPARQL = os.environ.get("DRIVE_ATLAS_NEPTUNE_SPARQL")
SNAPSHOT = Path(__file__).with_name("ontology_snapshot.json")
NS = "http://sotif.ontology/"
CLASSES = ["VRU", "EgoState", "OcclusionState", "DrivingBehavior", "LightingCondition", "WeatherCondition",
           "StaticObject", "HazardousEvent", "FunctionalInsufficiency"]

SUPPORT: Dict[str, Dict] = {
    "AdultPedestrian": {"support": "simulated", "maps": {"kind": "adult", "speedMps": [1.0, 1.6]}},
    "ChildPedestrian": {"support": "simulated", "maps": {"kind": "child", "speedMps": [0.8, 2.5]}},
    "WheelchairUser": {"support": "simulated", "maps": {"kind": "wheelchair", "speedMps": [0.5, 1.0]}},
    "PedestrianWithStroller": {"support": "simulated", "maps": {"kind": "stroller", "speedMps": [0.6, 1.1]}},
    "Cyclist": {"support": "unsupported"},
    "EScooterRider": {"support": "unsupported"},
    "LowSpeed": {"support": "simulated", "maps": {"egoSpeedMps": [6.0, 8.3]}},
    "MediumSpeed": {"support": "simulated", "maps": {"egoSpeedMps": [8.3, 13.9]}},
    "HighSpeed": {"support": "simulated", "maps": {"egoSpeedMps": [13.9, 18.0]}},
    "Stationary": {"support": "unsupported"},
    "NoOcclusion": {"support": "visual-only", "maps": {"parkedCount": [0, 0]}},
    "PartialOcclusion": {"support": "visual-only", "maps": {"parkedCount": [1, 3]}},
    "FullOcclusion": {"support": "visual-only", "maps": {"parkedCount": [4, 6]}},
    "ParkedVehicle": {"support": "visual-only", "maps": {"parkedCount": [1, 6]}},
    "NormalDriving": {"support": "simulated"},
    "CollisionWithVRU": {"support": "target"},
}
CONDITION_ONLY_CLASSES = {"LightingCondition", "WeatherCondition"}
SOURCE = "snapshot"


def sparql(query: str) -> List[Dict[str, str]]:
    return neptune.query(query)


def _short(uri: str) -> str:
    return uri.rsplit("/", 1)[-1].rsplit("#", 1)[-1]


def _query_neptune() -> Dict[str, Dict[str, Dict]]:
    values = " ".join(f"<{NS}{c}>" for c in CLASSES)
    rows = sparql(f"""SELECT ?cls ?i ?p ?o WHERE {{ VALUES ?cls {{ {values} }} ?i a ?cls .
        OPTIONAL {{ ?i ?p ?o FILTER(?p != <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> && !isLiteral(?o)) }} }}""")
    out: Dict[str, Dict[str, Dict]] = {}
    for r in rows:
        cls, inst = _short(r["cls"]), _short(r["i"])
        entry = out.setdefault(cls, {}).setdefault(inst, {"id": inst, "relations": {}})
        if "p" in r:
            entry["relations"].setdefault(_short(r["p"]), []).append(_short(r["o"]))
    return out


def fetch_slice() -> Dict[str, List[Dict]]:
    """{class: [{id, relations: {predicate: [object]}, support, maps}]} for the crosswalk-relevant classes.

    SOURCE is set to where the slice actually came from ("neptune" or "snapshot").
    """
    global SOURCE
    out = _query_neptune() if NEPTUNE_SPARQL else {}
    SOURCE = "neptune" if out else "snapshot"
    if not out:
        if NEPTUNE_SPARQL:
            logging.getLogger(__name__).warning("Neptune returned no ontology instances; using %s", SNAPSHOT.name)
        out = {cls: {i["id"]: i for i in items} for cls, items in json.loads(SNAPSHOT.read_text()).items()}
    result: Dict[str, List[Dict]] = {}
    for cls, instances in out.items():
        items = []
        for inst, entry in sorted(instances.items()):
            tag = SUPPORT.get(inst, {"support": "condition-only" if cls in CONDITION_ONLY_CLASSES else "unsupported"})
            items.append({**entry, **tag})
        result[cls] = items
    return result
