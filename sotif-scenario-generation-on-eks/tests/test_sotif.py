import json
from pathlib import Path

from augment import ontology as augment_ontology
from sotif.build import merge
from sotif.rdf import Ontology

ROOT = Path(__file__).resolve().parents[1]


def load():
    return Ontology.from_ntriples((ROOT / "sotif" / "ontology.nt").read_text())


def test_file_roundtrips_and_validates():
    text = (ROOT / "sotif" / "ontology.nt").read_text()
    onto = Ontology.from_ntriples(text)
    lines = lambda t: {line.strip() for line in t.splitlines() if line.strip()}  # noqa: E731
    assert lines(onto.to_ntriples()) == lines(text)
    assert onto.validate() == []


def test_snapshot_matches_file():
    snap = load().snapshot(augment_ontology.CLASSES)
    committed = json.loads((ROOT / "augment" / "ontology_snapshot.json").read_text())
    canon = lambda s: {c: sorted((i["id"], json.dumps({k: sorted(v) for k, v in i["relations"].items()})) for i in items) for c, items in s.items()}  # noqa: E731
    assert canon(snap) == canon(committed)


def test_merge_keeps_only_schema_valid_additions():
    onto = load()
    onto.instances["ChildPedestrian"].pop("description", None)
    result = merge(onto, "VRU", {
        "instances": [{"name": "Skateboarder", "description": "d"}, {"name": "Childpedestrian", "description": "dup"}, {"name": "bad name", "description": "x"}],
        "describe": {"ChildPedestrian": "어린이", "HeavyRain": "not in class"},
        "relations": [
            {"subject": "Skateboarder", "predicate": "triggers", "object": "MisclassificationError"},
            {"subject": "Skateboarder", "predicate": "eats", "object": "Camera"},
            {"subject": "HeavyRain", "predicate": "degrades", "object": "Camera"},
            {"subject": "Skateboarder", "predicate": "triggers", "object": "Nowhere"},
        ],
    }, per_class=3)
    assert result["added"]["instances"] == ["Skateboarder"]
    assert result["added"]["descriptions"] == ["ChildPedestrian"]
    assert result["added"]["relations"] == [["Skateboarder", "triggers", "MisclassificationError", None]]
    assert sorted(d["reason"] for d in result["dropped"]) == ["bad name", "duplicate", "predicate not in schema", "subject not in class", "unknown object"]
    assert onto.validate() == []


def test_cited_only_keeps_relations_with_a_known_source():
    from sotif.build import cited_source, prune_unsupported

    sources = {"arxiv-2303.04029"}
    assert cited_source("[arxiv-2303.04029] heavy rain may cause flooding", sources) == "arxiv-2303.04029"
    assert cited_source("[arxiv-9999.00000] something", sources) is None
    assert cited_source("[arxiv-2303.04029] 비슷한 조건 (추론)", sources) is None
    # markers that slipped through before: "(inference)" and "가정" (assumption)
    assert cited_source("[arxiv-2303.04029] children are easily occluded (inference).", sources) is None
    assert cited_source("[arxiv-2303.04029] 임시 차선 표시 가정", sources) is None
    assert cited_source("no citation", sources) is None

    onto = load()
    result = merge(onto, "WeatherCondition", {
        "instances": [{"name": "SideWind", "description": "d"}, {"name": "Sandstorm", "description": "d"}],
        "relations": [
            {"subject": "SideWind", "predicate": "triggers", "object": "LaneDeparture", "evidence": "[arxiv-2303.04029] side wind"},
            {"subject": "Sandstorm", "predicate": "degrades", "object": "Camera", "evidence": "general knowledge"},
        ],
    }, per_class=3, sources=sources)
    removed = prune_unsupported(onto, set(result["added"]["instances"]), result["dropped"])
    assert removed == {"Sandstorm"} and "SideWind" in onto.instances and "Sandstorm" not in onto.instances
    assert {d["reason"] for d in result["dropped"]} == {"no source citation", "no cited relation"}
    assert onto.validate() == []


def test_constraints_reject_inverted_degrades():
    onto = load()
    result = merge(onto, "PerceptionFunction", {"instances": [], "relations": [
        {"subject": "LaneDetection", "predicate": "degrades", "object": "Fog"},
        {"subject": "LaneDetection", "predicate": "requires", "object": "Camera"},
    ]}, per_class=3)
    assert [d["reason"] for d in result["dropped"]] == ["degrades subject cannot be PerceptionFunction"]
    assert [r[:3] for r in result["added"]["relations"]] == [["LaneDetection", "requires", "Camera"]]
