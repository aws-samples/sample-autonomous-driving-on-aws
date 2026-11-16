"""SOTIF scenario planner agent for AgentCore Runtime (HTTP protocol: POST /invocations, GET /ping on :8080).

The model plans; the tools call the pipeline API inside the EKS VPC, which runs the work as Kubernetes Jobs.
Request: {"prompt": "...", "lang": "ko" | "en"}
Response: {"text": "...", "batchId": "<last batch created or inspected>", "trace": [{"tool", "input", "ok", "ms"}]}
"""

from __future__ import annotations

import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import boto3
import urllib3
from botocore.config import Config

API = os.environ["PIPELINE_API_URL"].rstrip("/")
if not API.startswith(("http://", "https://")):
    raise RuntimeError("PIPELINE_API_URL must be an http(s) URL")
MODEL_ID = os.environ.get("MODEL_ID", "global.anthropic.claude-sonnet-5-5")
REGION = os.environ.get("AWS_REGION", "ap-northeast-2")
MAX_TURNS = 8
HTTP = urllib3.PoolManager(retries=False)
bedrock = boto3.client("bedrock-runtime", region_name=REGION, config=Config(read_timeout=300))

SYSTEM = """You are a SOTIF driving-scenario planner. Users describe driving test situations in Korean or English.
The pipeline currently supports one scene: an urban mid-block zebra crossing where the ego vehicle must yield to pedestrians
(adult, child, elderly, wheelchair user, pedestrian with stroller; day/night, weather, occlusion by parked cars, ego speed bands).
For a scenario request, call create_batch once with n (default 5, at most 10) and a short `focus` that restates the user's intent.
The pipeline then runs by itself: variant generation (SOTIF ontology + Bedrock) -> SUMO physics check -> RDS-HQ conversion -> Cosmos HDMap condition video on CPU.
Answer in the language named at the end of the user's message. Say what was started and what each stage does; do not claim results you have not read with get_batch.
If the request is outside the crosswalk scene, say so and offer the closest crosswalk variant instead.
HDMap videos are lane/box condition renders, not photorealistic footage; photorealistic generation needs a GPU stage that is not enabled."""

TOOLS = [
    {"toolSpec": {"name": "create_batch", "description": "Generate crosswalk scenario variants and run the whole pipeline automatically.",
                  "inputSchema": {"json": {"type": "object", "properties": {
                      "n": {"type": "integer", "minimum": 1, "maximum": 10},
                      "focus": {"type": "string", "description": "the user's intent, e.g. 'child pedestrian at night, high ego speed'"}},
                      "required": ["n", "focus"]}}}},
    {"toolSpec": {"name": "get_batch", "description": "Read a batch: stage timeline, variants, SUMO judgement, HDMap status.",
                  "inputSchema": {"json": {"type": "object", "properties": {"batchId": {"type": "string"}}, "required": ["batchId"]}}}},
    {"toolSpec": {"name": "list_batches", "description": "List recent batches.",
                  "inputSchema": {"json": {"type": "object", "properties": {}}}}},
    {"toolSpec": {"name": "rerun_stage", "description": "Resubmit one stage (sumo, rdshq, or hdmap with a variant) after a failure.",
                  "inputSchema": {"json": {"type": "object", "properties": {
                      "batchId": {"type": "string"}, "stage": {"type": "string", "enum": ["sumo", "rdshq", "hdmap"]},
                      "variant": {"type": "string"}}, "required": ["batchId", "stage"]}}}},
]


def _call(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    response = HTTP.request(method, API + path, body=data, headers={"content-type": "application/json"}, timeout=30.0)
    if response.status >= 400:
        return {"error": response.status, "detail": response.data.decode()[:500]}
    return json.loads(response.data)


def _summarise(batch: dict) -> dict:
    # Keep tool results small: presigned URLs and full ontology maps are for the UI, not the model.
    return {"batchId": batch.get("batchId"), "done": batch.get("done"), "error": batch.get("error"),
            "timeline": [{k: t.get(k) for k in ("stage", "state", "error")} for t in batch.get("timeline", [])],
            "variants": [{"variantId": v["variantId"], "rationale": v.get("rationale"),
                          "sumo": {k: (v.get("sumo") or {}).get(k) for k in ("label", "keep", "minPedDistanceM", "minPedTtcS", "yielded")},
                          "hdmap": v.get("hdmap")} for v in batch.get("variants", [])]}


LANGUAGES = {"ko": "Korean", "en": "English"}


def run_tool(name: str, args: dict, lang: str = "ko") -> dict:
    if name == "create_batch":
        return _call("POST", "/batches", {"n": int(args.get("n", 5)), "focus": args.get("focus"), "autoRun": True, "lang": lang})
    if name == "get_batch":
        found = _call("GET", f"/batches/{args['batchId']}")
        return found if "error" in found else _summarise(found)
    if name == "list_batches":
        return {"batches": [{k: b.get(k) for k in ("batchId", "focus", "n", "done", "error")} for b in _call("GET", "/batches").get("batches", [])]}
    if name == "rerun_stage":
        return _call("POST", f"/batches/{args['batchId']}/stages/{args['stage']}", {"variant": args.get("variant")})
    return {"error": f"unknown tool {name}"}


def handle(prompt: str, lang: str = "ko") -> dict:
    lang = lang if lang in LANGUAGES else "ko"
    messages = [{"role": "user", "content": [{"text": f"{prompt}\n\n(Answer in {LANGUAGES[lang]}.)"}]}]
    batch_id = None
    trace = []
    for _ in range(MAX_TURNS):
        response = bedrock.converse(modelId=MODEL_ID, system=[{"text": SYSTEM}], messages=messages,
                                    toolConfig={"tools": TOOLS}, inferenceConfig={"maxTokens": 2000})
        message = response["output"]["message"]
        messages.append(message)
        uses = [block["toolUse"] for block in message["content"] if "toolUse" in block]
        if not uses:
            text = "".join(block.get("text", "") for block in message["content"])
            return {"text": text, "batchId": batch_id, "trace": trace}
        results = []
        for use in uses:
            started = time.time()
            result = run_tool(use["name"], use.get("input") or {}, lang)
            trace.append({"tool": use["name"], "input": use.get("input") or {}, "ok": "error" not in result,
                          "ms": round((time.time() - started) * 1000)})
            batch_id = result.get("batchId") or (use.get("input") or {}).get("batchId") or batch_id
            results.append({"toolResult": {"toolUseId": use["toolUseId"], "content": [{"json": result}]}})
        messages.append({"role": "user", "content": results})
    limit = {"ko": "도구 호출 횟수 한도에 도달했습니다.", "en": "Reached the tool-call limit."}[lang]
    return {"text": limit, "batchId": batch_id, "trace": trace}


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: dict) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/ping":
            self._send(200, {"status": "Healthy"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/invocations":
            return self._send(404, {"error": "not found"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
            self._send(200, handle(str(body.get("prompt", "")), str(body.get("lang", "ko"))))
        except Exception as exc:  # surfaced to the caller instead of a dropped connection
            self._send(500, {"error": str(exc)[:500]})


if __name__ == "__main__":
    # "" = every interface: AgentCore Runtime reaches the agent on the runtime's network interface.
    ThreadingHTTPServer(("", 8080), Handler).serve_forever()
