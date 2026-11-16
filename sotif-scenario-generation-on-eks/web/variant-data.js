// 실제 생성된 시나리오의 파라미터 데이터
const VARIANT_DATA = {
  "child-fullocc-night-hs": {
    "egoSpeedMps": 14.0,
    "egoSpeedKmh": 50.4,
    "parkedCount": 4,
    "pedestrians": [{"kind": "child", "direction": "S2N", "departS": 8.4, "approachM": 3.0, "speedMps": 1.5, "speedKmh": 5.4}]
  },
  "adult-partial-fog-hs": {
    "egoSpeedMps": 16.0,
    "egoSpeedKmh": 57.6,
    "parkedCount": 2,
    "pedestrians": [{"kind": "adult", "direction": "S2N", "departS": 6.0, "approachM": 4.0, "speedMps": 1.4, "speedKmh": 5.0}]
  },
  "stroller-n2s-clear-med": {
    "egoSpeedMps": 10.0,
    "egoSpeedKmh": 36.0,
    "parkedCount": 0,
    "pedestrians": [{"kind": "stroller", "direction": "N2S", "departS": 1.8, "approachM": 5.0, "speedMps": 1.0, "speedKmh": 3.6}]
  },
  "wheelchair-rain-low": {
    "egoSpeedMps": 7.0,
    "egoSpeedKmh": 25.2,
    "parkedCount": 3,
    "pedestrians": [{"kind": "wheelchair", "direction": "S2N", "departS": 17.0, "approachM": 2.0, "speedMps": 0.8, "speedKmh": 2.9}]
  },
  "runner-glare-fullocc-hs": {
    "egoSpeedMps": 18.0,
    "egoSpeedKmh": 64.8,
    "parkedCount": 5,
    "pedestrians": [{"kind": "runner", "direction": "S2N", "departS": 7.2, "approachM": 6.0, "speedMps": 3.0, "speedKmh": 10.8}]
  },
  "elderly-n2s-lowsun-med": {
    "egoSpeedMps": 12.0,
    "egoSpeedKmh": 43.2,
    "parkedCount": 1,
    "pedestrians": [{"kind": "elderly", "direction": "S2N", "departS": 1.5, "approachM": 1.0, "speedMps": 0.9, "speedKmh": 3.2}]
  },
  "child-snow-fullocc-low": {
    "egoSpeedMps": 8.0,
    "egoSpeedKmh": 28.8,
    "parkedCount": 6,
    "pedestrians": [{"kind": "child", "direction": "S2N", "departS": 18.5, "approachM": 5.0, "speedMps": 2.0, "speedKmh": 7.2}]
  },
  "adult-n2s-headlight-oncoming": {
    "egoSpeedMps": 16.0,
    "egoSpeedKmh": 57.6,
    "parkedCount": 2,
    "pedestrians": [{"kind": "adult", "direction": "N2S", "departS": 4.6, "approachM": 5.0, "speedMps": 1.3, "speedKmh": 4.7}]
  },
  "elderly-comfortable-lightrain": {
    "egoSpeedMps": 11.0,
    "egoSpeedKmh": 39.6,
    "parkedCount": 0,
    "pedestrians": [{"kind": "elderly", "direction": "S2N", "departS": 1.0, "approachM": 3.0, "speedMps": 0.9, "speedKmh": 3.2}]
  },
  "adult-lowspeed-oncoming-tunnelexit": {
    "egoSpeedMps": 6.5,
    "egoSpeedKmh": 23.4,
    "parkedCount": 2,
    "pedestrians": [{"kind": "adult", "direction": "N2S", "departS": 15.0, "approachM": 4.0, "speedMps": 1.4, "speedKmh": 5.0}]
  },
  "child-n2s-fog-hs": {
    "egoSpeedMps": 15.0,
    "egoSpeedKmh": 54.0,
    "parkedCount": 5,
    "pedestrians": [{"kind": "child", "direction": "N2S", "departS": 9.0, "approachM": 6.0, "speedMps": 2.2, "speedKmh": 7.9}]
  },
  "stroller-fullocc-lightrain-med": {
    "egoSpeedMps": 11.0,
    "egoSpeedKmh": 39.6,
    "parkedCount": 4,
    "pedestrians": [{"kind": "stroller", "direction": "S2N", "departS": 10.5, "approachM": 3.0, "speedMps": 1.0, "speedKmh": 3.6}]
  },
  "wheelchair-tunnelentry-med": {
    "egoSpeedMps": 10.0,
    "egoSpeedKmh": 36.0,
    "parkedCount": 2,
    "pedestrians": [{"kind": "wheelchair", "direction": "S2N", "departS": 11.0, "approachM": 3.0, "speedMps": 0.8, "speedKmh": 2.9}]
  },
  "adult-glare-partial-med": {
    "egoSpeedMps": 13.0,
    "egoSpeedKmh": 46.8,
    "parkedCount": 2,
    "pedestrians": [{"kind": "adult", "direction": "S2N", "departS": 8.5, "approachM": 2.0, "speedMps": 1.4, "speedKmh": 5.0}]
  },
  "elderly-n2s-snow-low": {
    "egoSpeedMps": 7.5,
    "egoSpeedKmh": 27.0,
    "parkedCount": 3,
    "pedestrians": [{"kind": "elderly", "direction": "N2S", "departS": 14.0, "approachM": 3.0, "speedMps": 0.9, "speedKmh": 3.2}]
  },
  "child-adult-opposing-rain": {
    "egoSpeedMps": 12.0,
    "egoSpeedKmh": 43.2,
    "parkedCount": 4,
    "pedestrians": [
      {"kind": "child", "direction": "S2N", "departS": 5.5, "approachM": 3.0, "speedMps": 1.8, "speedKmh": 6.5},
      {"kind": "adult", "direction": "N2S", "departS": 5.0, "approachM": 4.0, "speedMps": 1.3, "speedKmh": 4.7}
    ]
  },
  "stroller-runner-night-hs": {
    "egoSpeedMps": 14.5,
    "egoSpeedKmh": 52.2,
    "parkedCount": 2,
    "pedestrians": [
      {"kind": "stroller", "direction": "S2N", "departS": 6.0, "approachM": 3.0, "speedMps": 1.0, "speedKmh": 3.6},
      {"kind": "runner", "direction": "S2N", "departS": 8.5, "approachM": 3.5, "speedMps": 3.0, "speedKmh": 10.8}
    ]
  },
  "three-peds-fullocc-rain-med": {
    "egoSpeedMps": 11.5,
    "egoSpeedKmh": 41.4,
    "parkedCount": 5,
    "pedestrians": [
      {"kind": "adult", "direction": "S2N", "departS": 4.0, "approachM": 3.0, "speedMps": 1.4, "speedKmh": 5.0},
      {"kind": "child", "direction": "S2N", "departS": 6.0, "approachM": 4.0, "speedMps": 1.5, "speedKmh": 5.4},
      {"kind": "elderly", "direction": "S2N", "departS": 8.0, "approachM": 2.0, "speedMps": 0.9, "speedKmh": 3.2}
    ]
  },
  "adult-hail-boundary-speed": {
    "egoSpeedMps": 13.9,
    "egoSpeedKmh": 50.0,
    "parkedCount": 0,
    "pedestrians": [{"kind": "adult", "direction": "S2N", "departS": 9.0, "approachM": 5.0, "speedMps": 1.4, "speedKmh": 5.0}]
  }
};

// 속도 범위 정보
const SPEED_RANGES = {
  "LowSpeed": {"min": 6.0, "max": 8.3, "minKmh": 21.6, "maxKmh": 29.9},
  "MediumSpeed": {"min": 8.3, "max": 13.9, "minKmh": 29.9, "maxKmh": 50.0},
  "HighSpeed": {"min": 13.9, "max": 18.0, "minKmh": 50.0, "maxKmh": 64.8}
};
