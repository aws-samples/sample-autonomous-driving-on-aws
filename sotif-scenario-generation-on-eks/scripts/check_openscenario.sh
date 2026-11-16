#!/usr/bin/env bash
# Play an exported OpenSCENARIO zip in esmini (headless, in Docker) and compare the replay with the SUMO judgement.
#   scripts/check_openscenario.sh <variant>-openscenario.zip
# Fails if esmini reports an error, if the replay has a collision SUMO did not have, or if the minimum
# ego-front-to-pedestrian distance differs from SUMO's by more than 0.15 m. scenario_sut.xosc is only played.
set -euo pipefail
ZIP=$(realpath "${1:?usage: scripts/check_openscenario.sh <openscenario.zip>}")
ESMINI_VERSION=v3.8.1
ESMINI_SHA256=05e6c9bb0f3156cef6f193c126b5ae340d42256da9c4a9eb7436d3b99dee9654
IMAGE="openscenario-check:${ESMINI_VERSION}"

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  docker build -q -t "$IMAGE" - >/dev/null <<EOF
FROM public.ecr.aws/docker/library/python:3.13-slim-trixie
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libx11-6 libxext6 libxrender1 libxinerama1 \
      libxrandr2 libxcursor1 libxi6 libgomp1 libfontconfig1 libxft2 unzip && rm -rf /var/lib/apt/lists/*
RUN python3 -c "import urllib.request; urllib.request.urlretrieve('https://github.com/esmini/esmini/releases/download/${ESMINI_VERSION}/esmini-bin_Linux.zip', '/tmp/esmini.zip')" \
 && echo "${ESMINI_SHA256}  /tmp/esmini.zip" | sha256sum -c - \
 && unzip -q /tmp/esmini.zip 'esmini/bin/esmini' -d /opt && rm /tmp/esmini.zip
RUN useradd --uid 10001 --create-home check
USER 10001
EOF
fi

docker run --rm -i --network none -v "$ZIP:/in/scenario.zip:ro" "$IMAGE" sh -s <<'SH'
set -eu
mkdir /tmp/w && cd /tmp/w && unzip -q /in/scenario.zip
for f in scenario scenario_sut; do
  status=0
  /opt/esmini/bin/esmini --headless --osc "$f.xosc" --fixed_timestep 0.05 --csv_logger "$f.csv" --collision > "$f.log" 2>&1 || status=$?
  if [ "$status" -ne 0 ] || grep -q "\] \[error\]" "$f.log"; then
    echo "FAIL $f.xosc: esmini exit $status" >&2
    grep "\] \[error\]" "$f.log" >&2 || tail -20 "$f.log" >&2
    exit 1
  fi
  echo "$f.xosc: played to the end"
done
python3 - <<'PY'
import json, math, sys
lines = open("scenario.csv").read().splitlines()
header = next(l for l in lines if l.startswith("Index")).split(", ")
per = [h for h in header if h.startswith("#1 ")]
col = {name: per.index("#1 " + name) for name in ("Entity_Name [-]", "World_Position_X [m]", "World_Position_Y [m]", "World_Heading_Angle [rad]", "bb_length [m]")}
states = {}
for line in lines:
    if not line[:1].isdigit():
        continue
    values = [v.strip() for v in line.split(",")]
    t, body = float(values[1]), values[2:]
    for k in range(len(body) // len(per)):
        e = body[k * len(per):(k + 1) * len(per)]
        states.setdefault(t, {})[e[col["Entity_Name [-]"]]] = [float(e[col[c]]) for c in list(col)[1:]]
best = None
for t, entities in states.items():
    x, y, h, length = entities["Ego"]
    fx, fy = x + math.cos(h) * length / 2, y + math.sin(h) * length / 2
    for name, (px, py, _, _) in entities.items():
        if name.startswith("ped-") and px > -40:  # skip pedestrians still waiting off the road
            d = math.hypot(px - fx, py - fy)
            best = d if best is None else min(best, d)
sumo = json.load(open("variant.json")).get("sumo") or {}
expected = sumo.get("minPedDistanceM")
print(f"min ego-front to pedestrian: esmini {best:.2f} m, SUMO {expected} m")
collided = "Collision between Ego" in open("scenario.log").read()
if collided and not sumo.get("collisions"):
    sys.exit("replay has an ego collision that SUMO did not have")
if expected is not None and abs(best - expected) > 0.15:
    sys.exit("replay differs from the SUMO judgement")
print("replay matches the SUMO judgement")
PY
SH
