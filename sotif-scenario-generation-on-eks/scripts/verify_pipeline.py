#!/usr/bin/env python3
"""End-to-end check of a deployed copy through its CloudFront URL; exits non-zero unless every check passes.

    scripts/verify_pipeline.py https://dxxxx.cloudfront.net [--n 3] [--lang en] [--timeout 1200]

Checks: API and agent health -> natural-language request through the agent -> batch finishes ->
ontology came from Neptune -> every kept variant has an HDMap video (HTTP 200, video/mp4), telemetry and an
OpenSCENARIO zip (scenario.xosc, scenario_sut.xosc, road.xodr) ->
gallery publish and unpublish. Prints per-stage timings as JSON at the end.
The caller's public IP must be allowed by the WAF (scripts/e2e.sh adds it).
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
import zipfile
from pathlib import Path

import urllib3

SNAPSHOT = Path(__file__).resolve().parents[1] / "augment" / "ontology_snapshot.json"
PROMPTS = {"ko": "야간에 아이가 주차된 차 사이에서 갑자기 튀어나오는 상황", "en": "A child suddenly runs out from between parked cars at night"}


class HttpError(Exception):
    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.status = status


def fetch(url: str, method: str = "GET", data: bytes | None = None, timeout: int = 120):
    """HTTPS request -> (status, headers, body); raises HttpError for 4xx/5xx."""
    response = urllib3.request(method, url, body=data, headers={"content-type": "application/json"},
                               timeout=float(timeout), retries=False, redirect=False)
    if response.status >= 400:
        raise HttpError(response.status)
    return response.status, response.headers, response.data


def call(base: str, method: str, path: str, body: dict | None = None, timeout: int = 120):
    status, _, raw = fetch(base + path, method, json.dumps(body).encode() if body is not None else None, timeout)
    return status, (json.loads(raw) if raw.strip() else None)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("url", help="CloudFront URL of the deployed copy")
    parser.add_argument("--n", type=int, default=3)
    parser.add_argument("--lang", default="en", choices=sorted(PROMPTS))
    parser.add_argument("--timeout", type=int, default=1200, help="seconds to wait for the batch")
    args = parser.parse_args()
    base = args.url.rstrip("/")
    if not base.startswith("https://"):
        parser.error("url must start with https://")
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> bool:
        print(f"{'PASS' if ok else 'FAIL'}  {name}{('  ' + detail) if detail else ''}", flush=True)
        if not ok:
            failures.append(name)
        return ok

    # Right after a deploy the API Pod may not be a healthy ALB target yet (502/503/504): wait up to 5 minutes.
    deadline = time.time() + 300
    while True:
        try:
            status, _, _ = fetch(base + "/", timeout=30)
            _, health = call(base, "GET", "/api/pipeline/health")
            break
        except HttpError as exc:
            if exc.status in (502, 503, 504) and time.time() < deadline:
                time.sleep(10)
                continue
            hint = " (is this host's public IP in ALLOWED_CIDRS?)" if exc.status == 403 else ""
            check("reach the site", False, f"HTTP {exc.status}{hint}")
            return 1
    check("web page", status == 200)
    check("pipeline API health", bool(health and health.get("ok")))
    if not check("agent configured", bool(health and health.get("agent"))):
        return 1

    started = time.time()
    _, reply = call(base, "POST", "/api/pipeline/chat", {"message": f"{PROMPTS[args.lang]} ({args.n} variants)", "lang": args.lang}, timeout=300)
    agent_s = time.time() - started
    batch = reply.get("batchId")
    tools = [t["tool"] for t in reply.get("trace", [])]
    if not check("agent started a batch", bool(batch) and "create_batch" in tools, f"{batch} via {tools} in {agent_s:.1f}s"):
        return 1

    deadline = time.time() + args.timeout
    while True:
        _, found = call(base, "GET", f"/api/pipeline/batches/{batch}")
        if found["done"] or time.time() > deadline:
            break
        time.sleep(10)
    if not check("batch finished", found["done"] and not found.get("error"), found.get("error") or ""):
        return 1

    generation = found.get("generation") or {}
    check("ontology read from Neptune", (generation.get("ontology") or {}).get("source") == "neptune")
    # Neptune must hold the committed ontology, not an earlier load: compare relation counts per class.
    committed = {cls: sum(len(objs) for i in items for objs in i["relations"].values())
                 for cls, items in json.loads(SNAPSHOT.read_text()).items()}
    loaded = {cls: c.get("relations") for cls, c in ((generation.get("ontology") or {}).get("classes") or {}).items()}
    check("Neptune holds the committed ontology", loaded == committed,
          "" if loaded == committed else f"Neptune {loaded} vs augment/ontology_snapshot.json {committed}")
    kept = [v for v in found["variants"] if (v.get("sumo") or {}).get("keep")]
    check("at least one variant kept by SUMO", bool(kept), f"{len(kept)}/{len(found['variants'])}")
    for v in kept:
        ok = v["hdmap"] == "succeeded" and str(v.get("videoUrl", "")).startswith("https://")
        if ok:
            status, headers, _ = fetch(v["videoUrl"], timeout=60)
            ok = status == 200 and headers.get("Content-Type") == "video/mp4"
        check(f"HDMap video {v['variantId']}", ok)
        _, tel = call(base, "GET", f"/api/pipeline/batches/{batch}/variants/{v['variantId']}/telemetry")
        check(f"telemetry {v['variantId']}", bool(tel and tel.get("frames")))
        ok = str(v.get("openscenarioUrl", "")).startswith("https://")
        if ok:
            _, _, archive = fetch(v["openscenarioUrl"], timeout=60)
            with zipfile.ZipFile(io.BytesIO(archive)) as files:
                ok = {"scenario.xosc", "scenario_sut.xosc", "road.xodr"} <= set(files.namelist())
        check(f"OpenSCENARIO {v['variantId']}", ok)

    if kept:
        vid = kept[0]["variantId"]
        status, _ = call(base, "POST", "/api/pipeline/gallery", {"batchId": batch, "variantId": vid})
        _, listed = call(base, "GET", "/api/pipeline/gallery")
        check("gallery publish", status == 201 and any(i["batchId"] == batch and i["variantId"] == vid for i in listed["items"]))
        status, _ = call(base, "DELETE", f"/api/pipeline/gallery/{batch}/{vid}")
        check("gallery unpublish", status == 204)

    stages = {t["stage"]: round((t["finishedAt"] or 0) - (t["startedAt"] or 0), 1) for t in found["timeline"] if t.get("startedAt")}
    print(json.dumps({"batchId": batch, "agentS": round(agent_s, 1), "totalS": round(found["finishedAt"] - found["createdAt"], 1),
                      "variants": len(found["variants"]), "kept": len(kept), "stagesS": stages,
                      "model": generation.get("model"), "tokens": generation.get("usage")}, indent=1))
    if failures:
        print(f"{len(failures)} check(s) failed: {failures}", file=sys.stderr)
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
