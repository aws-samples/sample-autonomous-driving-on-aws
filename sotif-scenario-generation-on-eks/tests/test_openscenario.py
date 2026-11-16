import io
import json
import defusedxml.ElementTree as ET
import zipfile

from augment import openscenario


def _telemetry():
    frames = []
    for i in range(60):
        t = round(i * 0.1, 1)
        actors = [{"actorId": "ped-0", "type": "pedestrian", "subtype": "wheelchair",
                   "pose": {"x": 200.0, "y": -9.0 + 0.1 * i, "z": 0.0, "headingDeg": 90.0}, "speedMps": 1.0, "lengthM": 1.1, "widthM": 0.8}]
        if t >= 2.0:  # inserted by SUMO later
            actors.append({"actorId": "wb.0", "type": "vehicle", "subtype": "car",
                           "pose": {"x": 318.0 - 10 * (t - 2.0), "y": 5.25, "z": 0.0, "headingDeg": -180.0}, "speedMps": 10.0, "lengthM": 4.8, "widthM": 1.8})
        frames.append({"t": t, "ego": {"x": 20.0 + 11 * t, "y": -5.25, "z": 0.0, "headingDeg": 0.0, "speedMps": 11.0,
                                       "accelerationMps2": 0.0, "lengthM": 4.8, "widthM": 1.8}, "actors": actors})
    return {"variantId": "v", "map": "urban-straight", "stepS": 0.1, "poseRef": "box-center", "frames": frames}


def _variant_dir(tmp_path):
    variant = {"variantId": "v", "egoSpeedMps": 11.0, "parkedCount": 2, "parkedEndX": 186.0, "pedestrians": [], "rationale": "a -- b"}
    (tmp_path / "variant.json").write_text(json.dumps(variant))
    (tmp_path / "telemetry.json").write_text(json.dumps(_telemetry()))
    (tmp_path / "result.json").write_text(json.dumps({"label": "critical", "minPedDistanceM": 3.5}))
    return tmp_path


def test_export_replay_and_sut(tmp_path):
    files = openscenario.export(_variant_dir(tmp_path))
    road = ET.fromstring(files["road.xodr"])
    assert [o.get("type") for o in road.iter("object")] == ["crosswalk"]
    assert sorted(int(lane.get("id")) for lane in road.iter("lane")) == [-3, -2, -1, 0, 1, 2, 3]

    replay = ET.fromstring(files["scenario.xosc"])
    names = [o.get("name") for o in replay.iter("ScenarioObject")]
    assert names == ["Ego", "ped-0", "wb.0", "parked-0", "parked-1"]
    assert replay.find(".//Pedestrian").get("pedestrianCategory") == "wheelchair"
    followed = {g.find("Actors/EntityRef").get("entityRef") for g in replay.iter("ManeuverGroup")}
    assert followed == {"Ego", "ped-0", "wb.0"}
    # wb.0 waits hidden off the road, then starts with its first SUMO sample
    hidden = [p.get("entityRef") for p in replay.iter("Private") if p.find(".//VisibilityAction") is not None]
    assert hidden == ["wb.0"]
    vertices = [v for t in replay.iter("Trajectory") if t.get("name") == "wb.0-sumo" for v in t.iter("Vertex")]
    assert float(vertices[0].get("time")) == 2.0
    # a "rising" edge on a time condition near 0 can be missed (esmini warns), so start triggers use "none"
    assert all(c.get("conditionEdge") == "none" for t in replay.iter("StartTrigger") for c in t.iter("Condition"))

    sut = ET.fromstring(files["scenario_sut.xosc"])
    assert {g.find("Actors/EntityRef").get("entityRef") for g in sut.iter("ManeuverGroup")} == {"ped-0", "wb.0"}
    assert json.loads(files["variant.json"])["sumo"]["label"] == "critical"


def test_export_zip(tmp_path):
    with zipfile.ZipFile(io.BytesIO(openscenario.export_zip(_variant_dir(tmp_path)))) as archive:
        assert sorted(archive.namelist()) == ["README.txt", "road.xodr", "scenario.xosc", "scenario_sut.xosc", "variant.json"]


def test_parked_props_from_telemetry_are_not_duplicated(tmp_path):
    variant_dir = _variant_dir(tmp_path)
    telemetry = json.loads((variant_dir / "telemetry.json").read_text())
    for frame in telemetry["frames"]:  # augment_run.py writes the parked props into every frame
        frame["actors"].append({"actorId": "parked-0", "type": "vehicle", "subtype": "parked",
                                "pose": {"x": 186.0, "y": -8.75, "z": 0.0, "headingDeg": 0.0}, "speedMps": 0.0, "lengthM": 4.8, "widthM": 1.8})
    (variant_dir / "telemetry.json").write_text(json.dumps(telemetry))
    names = [o.get("name") for o in ET.fromstring(openscenario.export(variant_dir)["scenario.xosc"]).iter("ScenarioObject")]
    assert len(names) == len(set(names))
    assert "parked-1" in names
