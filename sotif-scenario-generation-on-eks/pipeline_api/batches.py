"""Batch state lives in S3 (pipeline/<batch>/batch.json + status/*.json), so the API can restart at any time."""

from __future__ import annotations

import json
import logging
import os
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

import boto3
from botocore.config import Config

from pipeline_api import jobs

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import augment_generate  # noqa: E402

log = logging.getLogger("pipeline")
BUCKET = os.environ["BUCKET"]
REGION = os.environ.get("AWS_REGION", "ap-northeast-2")
MODEL = os.environ.get("VARIANT_MODEL", "sonnet")
MAX_VARIANTS = int(os.environ.get("MAX_VARIANTS", "10"))
STAGE_ORDER = ["generate", "sumo", "rdshq", "hdmap"]
TERMINAL = {"succeeded", "failed"}

# Regional endpoint: presigned URLs on the global endpoint are redirected (307) for new buckets, which breaks the signature.
s3 = boto3.client("s3", region_name=REGION, endpoint_url=f"https://s3.{REGION}.amazonaws.com",
                  config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))
_lock = threading.Lock()
_active: set = set()


def _key(batch: str, rel: str) -> str:
    return f"pipeline/{batch}/{rel}"


def _get_json(key: str):
    try:
        return json.loads(s3.get_object(Bucket=BUCKET, Key=key)["Body"].read())
    except s3.exceptions.NoSuchKey:
        return None


def _put_json(key: str, value) -> None:
    s3.put_object(Bucket=BUCKET, Key=key, Body=json.dumps(value, ensure_ascii=False).encode(), ContentType="application/json")


def _set_status(batch: str, name: str, **fields) -> None:
    _put_json(_key(batch, f"status/{name}.json"), fields)


def statuses(batch: str) -> Dict[str, dict]:
    out = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=_key(batch, "status/")):
        for obj in page.get("Contents", []):
            name = obj["Key"].rsplit("/", 1)[1].removesuffix(".json")
            out[name] = _get_json(obj["Key"]) or {}
    return out


def create(n: int, focus: Optional[str], auto_run: bool = True, lang: str = "ko") -> dict:
    n = max(1, min(n, MAX_VARIANTS))
    batch = time.strftime("b%Y%m%d%H%M%S", time.gmtime()) + "-" + secrets.token_hex(2)
    record = {"batchId": batch, "createdAt": time.time(), "n": n, "focus": focus, "lang": lang, "model": MODEL, "autoRun": auto_run, "done": False}
    _put_json(_key(batch, "batch.json"), record)
    _set_status(batch, "generate", state="submitted", submittedAt=time.time())
    _start_generate(batch, n, focus, lang)
    with _lock:
        _active.add(batch)
    return record


def _generate(batch: str, n: int, focus: Optional[str], lang: str = "ko") -> None:
    started = time.time()
    _set_status(batch, "generate", state="running", startedAt=started)
    try:
        variants, meta = augment_generate.generate(n, MODEL, REGION, focus, lang)
        _put_json(_key(batch, "variants.json"), variants)
        _put_json(_key(batch, "variants.meta.json"), meta)
        _set_status(batch, "generate", state="succeeded", startedAt=started, finishedAt=time.time(),
                    result={"variants": [v["variantId"] for v in variants], "latencyS": meta["latencyS"]})
    except Exception as exc:  # recorded for the UI and the agent
        log.exception("generate failed for %s", batch)
        _set_status(batch, "generate", state="failed", startedAt=started, finishedAt=time.time(), error=str(exc)[-2000:])


def submit_stage(batch: str, stage: str, variant: Optional[str] = None) -> str:
    name = f"{stage}-{variant}" if variant else stage
    job = jobs.submit(stage, batch, BUCKET, variant)
    current = _get_json(_key(batch, f"status/{name}.json")) or {}
    if current.get("state") not in TERMINAL | {"running"}:
        _set_status(batch, name, state="submitted", submittedAt=time.time(), job=job)
    return job


def advance(batch: str) -> None:
    """Submit the next stage when the previous one succeeded; mark the batch done when nothing is left."""
    record = _get_json(_key(batch, "batch.json"))
    if not record or record.get("done") or not record.get("autoRun"):
        with _lock:
            _active.discard(batch)
        return
    st = statuses(batch)
    state = lambda name: (st.get(name) or {}).get("state")  # noqa: E731
    failed = next((name for name in ("generate", "sumo", "rdshq") if state(name) == "failed"), None)
    if failed:
        return _finish(batch, record, error=f"{failed} failed")
    if state("generate") != "succeeded":
        if state("generate") in ("submitted", "running") and not _generating(batch):
            # The API restarted while generating: run it again.
            _start_generate(batch, record["n"], record.get("focus"), record.get("lang", "ko"))
        return
    if state("sumo") is None:
        submit_stage(batch, "sumo")
        return
    if state("sumo") != "succeeded":
        return
    if not st["sumo"].get("result", {}).get("kept"):
        return _finish(batch, record, error="no variant passed the SUMO check")
    if state("rdshq") is None:
        submit_stage(batch, "rdshq")
        return
    if state("rdshq") != "succeeded":
        return
    converted = st["rdshq"].get("result", {}).get("converted", [])
    pending = False
    for vid in converted:
        s = state(f"hdmap-{vid}")
        if s is None:
            submit_stage(batch, "hdmap", vid)
        if s not in TERMINAL:
            pending = True
    if not pending:
        _finish(batch, record)


def _start_generate(batch: str, n: int, focus: Optional[str], lang: str = "ko") -> None:
    threading.Thread(target=_generate, args=(batch, n, focus, lang), name=f"generate-{batch}", daemon=True).start()


def _generating(batch: str) -> bool:
    return any(t.name == f"generate-{batch}" for t in threading.enumerate())


def _finish(batch: str, record: dict, error: Optional[str] = None) -> None:
    _put_json(_key(batch, "batch.json"), {**record, "done": True, "finishedAt": time.time(), "error": error})
    with _lock:
        _active.discard(batch)


def describe(batch: str) -> Optional[dict]:
    record = _get_json(_key(batch, "batch.json"))
    if not record:
        return None
    st = statuses(batch)
    variants = _get_json(_key(batch, "variants.json")) or []
    results = {r["variantId"]: r for r in (_get_json(_key(batch, "run/results.json")) or [])}
    rows = []
    for v in variants:
        vid = v["variantId"]
        hd = st.get(f"hdmap-{vid}") or {}
        video = webm = None
        if hd.get("state") == "succeeded":
            sign = lambda ext: s3.generate_presigned_url("get_object", Params={"Bucket": BUCKET, "Key": _key(batch, f"hdmap/{vid}.{ext}")}, ExpiresIn=3600)  # noqa: E731
            video = sign("mp4")
            webm = sign("webm") if (hd.get("result") or {}).get("webm") else None
        exported = ((st.get("sumo") or {}).get("result") or {}).get("openscenario") or []
        osc = _sign(_osc_key(batch, vid), f"{vid}-openscenario.zip") if vid in exported else None
        rdshq = _get_json(_key(batch, f"rdshq/{vid}/{vid}.source.json")) if (st.get("rdshq") or {}).get("state") == "succeeded" else None
        params = {k: v.get(k) for k in ("egoSpeedMps", "parkedCount", "oncomingPeriodS", "pedestrians")}
        rows.append({"variantId": vid, "rationale": v.get("rationale"), "ontology": v.get("ontology"), "params": params,
                     "sumo": results.get(vid), "rdshq": rdshq, "hdmap": hd.get("state"), "hdmapSeconds": _elapsed(hd),
                     "videoUrl": video, "webmUrl": webm, "openscenarioUrl": osc, "published": _exists(_gallery_key(batch, vid))})
    timeline = [{"stage": name, **{k: s.get(k) for k in ("state", "submittedAt", "startedAt", "finishedAt", "error")}}
                for name, s in sorted(st.items(), key=lambda kv: (kv[1].get("startedAt") or kv[1].get("submittedAt") or 0))]
    meta = _get_json(_key(batch, "variants.meta.json"))
    return {**record, "variants": rows, "timeline": timeline, "generation": meta}


def _elapsed(status: dict) -> Optional[float]:
    if status.get("startedAt") and status.get("finishedAt"):
        return round(status["finishedAt"] - status["startedAt"], 1)
    return None


def _osc_key(batch: str, variant: str) -> str:
    return _key(batch, f"openscenario/{variant}.zip")


def _gallery_key(batch: str, variant: str) -> str:
    return f"gallery/items/{batch}__{variant}.json"


def _exists(key: str) -> bool:
    try:
        s3.head_object(Bucket=BUCKET, Key=key)
        return True
    except s3.exceptions.ClientError:
        return False


def _sign(key: str, download_as: Optional[str] = None) -> str:
    params = {"Bucket": BUCKET, "Key": key}
    if download_as:
        params["ResponseContentDisposition"] = f'attachment; filename="{download_as}"'
    return s3.generate_presigned_url("get_object", Params=params, ExpiresIn=3600)


def publish(batch: str, variant: str) -> Optional[dict]:
    """Copy a finished variant's summary into the gallery list (videos stay where the pipeline wrote them)."""
    found = describe(batch)
    row = next((v for v in (found or {}).get("variants", []) if v["variantId"] == variant), None)
    if not row:
        return None
    if row["hdmap"] != "succeeded":
        raise ValueError("only variants with a finished HDMap video can be published")
    hd = (statuses(batch).get(f"hdmap-{variant}") or {}).get("result") or {}
    item = {
        "batchId": batch, "variantId": variant, "publishedAt": time.time(), "createdAt": found["createdAt"],
        "focus": found.get("focus"), "lang": found.get("lang", "ko"), "rationale": row["rationale"],
        "ontology": row["ontology"], "params": row["params"], "sumo": row["sumo"], "rdshq": row["rdshq"],
        "model": (found.get("generation") or {}).get("model"),
        "videoKey": _key(batch, f"hdmap/{variant}.mp4"), "webmKey": hd.get("webm"),
        "openscenarioKey": _osc_key(batch, variant) if row.get("openscenarioUrl") else None,
    }
    _put_json(_gallery_key(batch, variant), item)
    return item


def unpublish(batch: str, variant: str) -> bool:
    key = _gallery_key(batch, variant)
    if not _exists(key):
        return False
    s3.delete_object(Bucket=BUCKET, Key=key)
    return True


def gallery(limit: int = 100) -> List[dict]:
    keys = [obj["Key"] for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix="gallery/items/")
            for obj in page.get("Contents", []) if obj["Key"].endswith(".json")]
    items = [i for i in (_get_json(k) for k in keys) if i]
    items.sort(key=lambda i: i.get("publishedAt", 0), reverse=True)
    for item in items[:limit]:
        item["videoUrl"] = _sign(item["videoKey"])
        item["webmUrl"] = _sign(item["webmKey"]) if item.get("webmKey") else None
        key = item.get("openscenarioKey")
        item["openscenarioUrl"] = _sign(key, f"{item['variantId']}-openscenario.zip") if key else None
    return items[:limit]


def telemetry(batch: str, variant: str, every: int = 2) -> Optional[dict]:
    """SUMO telemetry thinned to every n-th 0.1 s frame, in a compact shape for the top-down view."""
    raw = _get_json(_key(batch, f"run/{variant}/telemetry.json"))
    if not raw:
        return None
    frames = []
    for frame in raw["frames"][::every]:
        ego = frame.get("ego")
        frames.append({
            "t": frame["t"],
            "ego": [round(ego["x"], 2), round(ego["y"], 2), round(ego.get("headingDeg", 0), 1), round(ego.get("speedMps", 0), 1)] if ego else None,
            "actors": [[a["actorId"], a["type"], a.get("subtype"), round(a["pose"]["x"], 2), round(a["pose"]["y"], 2),
                        round(a["pose"].get("headingDeg", 0), 1), a.get("lengthM"), a.get("widthM")] for a in frame.get("actors", [])],
        })
    return {"variantId": variant, "map": raw.get("map"), "stepS": raw.get("stepS", 0.1) * every, "frames": frames}


def recent(limit: int = 20) -> List[dict]:
    resp = s3.list_objects_v2(Bucket=BUCKET, Prefix="pipeline/", Delimiter="/")
    batches = sorted((p["Prefix"].split("/")[1] for p in resp.get("CommonPrefixes", [])), reverse=True)[:limit]
    return [r for r in (_get_json(_key(b, "batch.json")) for b in batches) if r]


def resume_active() -> None:
    for record in recent(50):
        if record.get("autoRun") and not record.get("done"):
            with _lock:
                _active.add(record["batchId"])


def run_forever(interval_s: float = 5.0) -> None:
    # Retry: one failed S3 call at pod start (credentials not ready yet) must not stop the orchestrator for good.
    while True:
        try:
            resume_active()
            break
        except Exception:
            log.exception("resuming unfinished batches failed; retrying")
            time.sleep(interval_s)
    while True:
        with _lock:
            batches = list(_active)
        for batch in batches:
            try:
                advance(batch)
            except Exception:
                log.exception("advance failed for %s", batch)
        time.sleep(interval_s)
