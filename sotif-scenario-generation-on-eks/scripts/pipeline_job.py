#!/usr/bin/env python3
"""Run one pipeline stage against S3 (the entrypoint of the EKS Job images).

    pipeline_job.py sumo  --bucket B --batch ID                 # variants.json -> run/ (SUMO judge) + openscenario/<variantId>.zip
    pipeline_job.py rdshq --bucket B --batch ID                 # run/<kept variants> -> rdshq/<variantId>/
    pipeline_job.py hdmap --bucket B --batch ID --variant V     # rdshq/V/ -> hdmap/V.mp4 + V.webm (Cosmos toolkit, CPU)

S3 layout under s3://B/pipeline/<batch>/: variants.json, run/, openscenario/, rdshq/, hdmap/, status/<stage>[-<variant>].json.
A stage whose status is already "succeeded" is skipped, so a Job retried after a Spot interruption is safe.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]
TOOLKIT = Path(os.environ.get("COSMOS_TOOLKIT_DIR", "/opt/cosmos-drive-dreams/cosmos-drive-dreams-toolkits"))
s3 = boto3.client("s3")


def prefix(batch: str) -> str:
    return f"pipeline/{batch}/"


def download_prefix(bucket: str, key_prefix: str, dest: Path) -> int:
    count = 0
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=key_prefix):
        for obj in page.get("Contents", []):
            target = dest / obj["Key"][len(key_prefix):]
            target.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(bucket, obj["Key"], str(target))
            count += 1
    return count


def upload_dir(bucket: str, src: Path, key_prefix: str) -> int:
    count = 0
    for path in sorted(p for p in src.rglob("*") if p.is_file()):
        s3.upload_file(str(path), bucket, key_prefix + path.relative_to(src).as_posix())
        count += 1
    return count


def status_key(batch: str, stage: str, variant: str | None) -> str:
    return prefix(batch) + f"status/{stage}{'-' + variant if variant else ''}.json"


def write_status(bucket: str, key: str, **fields) -> None:
    s3.put_object(Bucket=bucket, Key=key, Body=json.dumps(fields, ensure_ascii=False).encode(), ContentType="application/json")


def already_succeeded(bucket: str, key: str) -> bool:
    try:
        return json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read()).get("state") == "succeeded"
    except s3.exceptions.NoSuchKey:
        return False


# SUMO and RDS-HQ steps are imported in-process (pipeline image only); the HDMap stage runs the toolkit's CLI and ffmpeg.


def stage_sumo(bucket: str, batch: str, work: Path, _variant: str | None) -> dict:
    variants = work / "variants.json"
    s3.download_file(bucket, prefix(batch) + "variants.json", str(variants))
    import augment_run  # noqa: E402 (pipeline image only)

    if augment_run.main(["augment_run.py", str(variants), str(work / "run")]) != 0:
        raise SystemExit("augment_run failed")
    shutil.rmtree(work / "run" / "_net", ignore_errors=True)
    upload_dir(bucket, work / "run", prefix(batch) + "run/")
    rows = json.loads((work / "run" / "results.json").read_text())
    from augment import openscenario  # noqa: E402 (needs ROOT on sys.path)

    exported = []
    for row in (r for r in rows if r.get("simulatable")):
        vid = row["variantId"]
        s3.put_object(Bucket=bucket, Key=prefix(batch) + f"openscenario/{vid}.zip", Body=openscenario.export_zip(work / "run" / vid),
                      ContentType="application/zip")
        exported.append(vid)
    return {"variants": len(rows), "kept": [r["variantId"] for r in rows if r.get("keep")], "openscenario": exported}


def stage_rdshq(bucket: str, batch: str, work: Path, _variant: str | None) -> dict:
    results = json.loads(s3.get_object(Bucket=bucket, Key=prefix(batch) + "run/results.json")["Body"].read())
    import sumo_to_rdshq  # noqa: E402 (pipeline image only)

    converted = []
    for row in (r for r in results if r.get("keep")):
        vid = row["variantId"]
        download_prefix(bucket, prefix(batch) + f"run/{vid}/", work / "run" / vid)
        print(json.dumps(sumo_to_rdshq.convert(work / "run" / vid, work / "rdshq" / vid, vid.replace(".", "-"), 12.1)), flush=True)
        upload_dir(bucket, work / "rdshq" / vid, prefix(batch) + f"rdshq/{vid}/")
        converted.append(vid)
    return {"converted": converted}


def stage_hdmap(bucket: str, batch: str, work: Path, variant: str | None) -> dict:
    if not variant:
        raise SystemExit("hdmap needs --variant")
    if not download_prefix(bucket, prefix(batch) + f"rdshq/{variant}/", work / "rdshq"):
        raise SystemExit(f"no RDS-HQ input for {variant}")
    out = work / "render"
    # The toolkit finds config/ and assets/ relative to its own directory; skip lidar and the GPU-only world_scenario.
    subprocess.run(["python3", "render_from_rds_hq.py", "-i", str(work / "rdshq"), "-o", str(out),
                    "-d", "waymo", "-c", "ftheta", "-s", "lidar", "-s", "world_scenario"],
                   cwd=TOOLKIT, env={**os.environ, "USE_RAY": "False"}, check=True)
    chunks = sorted((out / "hdmap" / "ftheta_front").glob(f"{variant}_*.mp4"), key=lambda p: int(p.stem.rsplit("_", 1)[1]))
    if not chunks:
        raise SystemExit("renderer wrote no video (window shorter than 121 frames?)")
    listing = work / "concat.txt"
    listing.write_text("".join(f"file '{c}'\n" for c in chunks))
    merged = work / f"{variant}.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy",
                    "-movflags", "+faststart", str(merged)], check=True)
    # VP9 copy for browsers without H.264 (e.g. open-source Chromium builds).
    webm = work / f"{variant}.webm"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(merged), "-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "36",
                    "-deadline", "realtime", "-cpu-used", "8", "-row-mt", "1", str(webm)], check=True)
    s3.upload_file(str(merged), bucket, prefix(batch) + f"hdmap/{variant}.mp4", ExtraArgs={"ContentType": "video/mp4"})
    s3.upload_file(str(webm), bucket, prefix(batch) + f"hdmap/{variant}.webm", ExtraArgs={"ContentType": "video/webm"})
    upload_dir(bucket, out, prefix(batch) + f"hdmap/{variant}/")
    return {"video": prefix(batch) + f"hdmap/{variant}.mp4", "webm": prefix(batch) + f"hdmap/{variant}.webm", "chunks": len(chunks)}


STAGES = {"sumo": stage_sumo, "rdshq": stage_rdshq, "hdmap": stage_hdmap}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=sorted(STAGES))
    parser.add_argument("--bucket", default=os.environ.get("BUCKET"))
    parser.add_argument("--batch", required=True)
    parser.add_argument("--variant")
    args = parser.parse_args()
    if not args.bucket:
        parser.error("--bucket or BUCKET is required")

    key = status_key(args.batch, args.stage, args.variant)
    if already_succeeded(args.bucket, key):
        print(f"{key} already succeeded; nothing to do")
        return 0
    started = time.time()
    write_status(args.bucket, key, state="running", startedAt=started, pod=os.environ.get("HOSTNAME"))
    try:
        with tempfile.TemporaryDirectory(prefix=f"{args.stage}-") as tmp:
            result = STAGES[args.stage](args.bucket, args.batch, Path(tmp), args.variant)
    except BaseException as exc:
        write_status(args.bucket, key, state="failed", startedAt=started, finishedAt=time.time(), error=str(exc)[-2000:])
        raise
    write_status(args.bucket, key, state="succeeded", startedAt=started, finishedAt=time.time(), result=result)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
