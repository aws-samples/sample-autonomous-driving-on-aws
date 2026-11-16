import json
import shutil
import sys
from pathlib import Path

import pytest

from augment import crosswalk

ROOT = Path(__file__).resolve().parents[1]


def test_normalize_clamps_out_of_bounds_and_reports_it():
    variant, notes = crosswalk.normalize({
        "variantId": "x", "egoSpeedMps": 40, "parkedCount": 9,
        "pedestrians": [{"kind": "alien", "direction": "X", "departS": 99, "approachM": 0, "speedMps": 9}] * 6,
    })
    assert variant["egoSpeedMps"] == 18.0 and variant["parkedCount"] == 6
    assert len(variant["pedestrians"]) == 4
    p = variant["pedestrians"][0]
    assert (p["kind"], p["direction"], p["departS"], p["approachM"], p["speedMps"]) == ("adult", "S2N", 40.0, 1.0, 3.0)
    assert any(n.startswith("egoSpeedMps") for n in notes) and any(n.startswith("pedestrians: 6") for n in notes)


def test_routes_sorted_by_depart_and_speed_factor_above_limit():
    variant, _ = crosswalk.normalize({"egoSpeedMps": 16.0, "pedestrians": [
        {"kind": "child", "direction": "N2S", "departS": 20, "approachM": 3, "speedMps": 2.0},
        {"kind": "adult", "direction": "S2N", "departS": 5, "approachM": 3, "speedMps": 1.3},
    ]})
    xml = crosswalk.routes_xml(variant)
    assert xml.index('<person id="ped-1"') < xml.index('<person id="ped-0"')  # depart 5 s before 20 s
    assert 'speedFactor="1.151"' in xml  # 16.0 / 13.9 lets the ego exceed the edge limit


def _ego(x, speed, accel=0.0):
    return {"x": x, "y": crosswalk.EGO_LANE_Y, "headingDeg": 0.0, "speedMps": speed, "accelerationMps2": accel}


def _ped(x, y):
    return {"pose": {"x": x, "y": y}}


def test_metrics_label_ignores_junction_braking_without_pedestrian():
    m = crosswalk.Metrics()
    m.update(1.0, _ego(170.0, 8.0, accel=-4.8), [])
    m.ego_arrival_s = 30.0
    assert m.verdict(sim_ok=True)["label"] == "no-interaction"


def test_metrics_critical_on_short_pedestrian_ttc_and_yield():
    m = crosswalk.Metrics()
    m.update(1.0, _ego(190.0, 6.0), [_ped(200.0, -5.0)])  # front at 192.4, 7.6 m / 6 m/s = 1.27 s
    m.update(2.0, _ego(193.0, 0.0), [_ped(200.0, -4.0)])
    v = m.verdict(sim_ok=True)
    assert v["label"] == "critical" and v["yielded"] is True and v["minPedTtcS"] == 1.27


def test_sumo_to_rdshq_map_from_net(tmp_path):
    pytest.importorskip("webdataset")
    sys.path.insert(0, str(ROOT / "scripts"))
    import sumo_to_rdshq

    net = tmp_path / "net.net.xml"
    net.write_text("""<net>
  <edge id=":n2_c0" function="crossing"><lane id=":n2_c0_0" width="3.50" shape="200.00,7.00 200.00,-7.00"/></edge>
  <edge id="eb1"><lane id="eb1_0" index="0" allow="pedestrian" width="2.00" shape="100,-8 198.25,-8"/>
    <lane id="eb1_1" index="1" width="3.50" shape="100,-5.25 198.25,-5.25"/><lane id="eb1_2" index="2" width="3.50" shape="100,-1.75 198.25,-1.75"/></edge>
  <edge id="wb1"><lane id="wb1_1" index="1" width="3.50" shape="198.25,5.25 100,5.25"/><lane id="wb1_2" index="2" width="3.50" shape="198.25,1.75 100,1.75"/></edge>
</net>""")
    geometry = sumo_to_rdshq.map_from_net(net)
    ys = sorted(round(line[0][1], 2) for line in geometry["lanelines"])
    assert ys == [-3.5, 0.0, 3.5]  # shared centre line kept once
    assert sorted(round(line[0][1], 2) for line in geometry["road_boundaries"]) == [-7.0, 7.0]
    xs = sorted({round(p[0], 2) for p in geometry["crosswalks"][0]})
    assert xs == [198.25, 201.75]


@pytest.mark.skipif(shutil.which("sumo") is None, reason="pinned SUMO runtime is not available on the host")
def test_augment_run_batch(tmp_path):
    variants = tmp_path / "v.json"
    variants.write_text(json.dumps([
        {"variantId": "meet", "egoSpeedMps": 11, "pedestrians": [{"kind": "adult", "direction": "S2N", "departS": 13, "approachM": 2, "speedMps": 1.3}]},
        {"variantId": "miss", "egoSpeedMps": 18, "pedestrians": [{"kind": "adult", "direction": "S2N", "departS": 40, "approachM": 1, "speedMps": 1.3}]},
    ]))
    sys.path.insert(0, str(ROOT / "scripts"))
    import augment_run

    assert augment_run.main(["augment_run.py", str(variants), str(tmp_path / "out")]) == 0
    rows = {r["variantId"]: r for r in json.loads((tmp_path / "out" / "results.json").read_text())}
    assert rows["meet"]["simulatable"] and rows["meet"]["keep"]
    assert rows["miss"]["simulatable"] and rows["miss"]["label"] == "no-interaction" and not rows["miss"]["keep"]
