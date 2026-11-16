"""Pipeline API (EKS): batches of generated scenario variants -> SUMO -> RDS-HQ -> HDMap videos.

Used by the AgentCore agent (tools) and by the web demo (progress, videos, chat).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from contextlib import asynccontextmanager
from typing import Literal, Optional

import boto3
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from pipeline_api import batches

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    threading.Thread(target=batches.run_forever, name="orchestrator", daemon=True).start()
    yield


app = FastAPI(title="SOTIF Scenario Generation pipeline API", lifespan=_lifespan)
AGENT_RUNTIME_ARN = os.environ.get("AGENT_RUNTIME_ARN")
_agentcore = boto3.client("bedrock-agentcore", region_name=batches.REGION) if AGENT_RUNTIME_ARN else None


class BatchRequest(BaseModel):
    n: int = Field(5, ge=1, le=batches.MAX_VARIANTS, description="number of variants to generate")
    focus: Optional[str] = Field(None, max_length=1000, description="user request that narrows the variants")
    lang: Literal["ko", "en"] = Field("ko", description="language of generated rationales")
    autoRun: bool = Field(True, description="run SUMO, RDS-HQ and HDMap automatically after generation")


class StageRequest(BaseModel):
    variant: Optional[str] = None


class PublishRequest(BaseModel):
    batchId: str = Field(..., max_length=64)
    variantId: str = Field(..., max_length=64)


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    sessionId: Optional[str] = Field(None, min_length=33, max_length=100)
    lang: Literal["ko", "en"] = Field("ko", description="UI language: the agent answers and writes rationales in it")


@app.get("/api/pipeline/health")
def health():
    return {"ok": True, "agent": bool(AGENT_RUNTIME_ARN)}


@app.post("/api/pipeline/batches", status_code=202)
def create_batch(body: BatchRequest):
    return batches.create(body.n, body.focus, body.autoRun, body.lang)


@app.get("/api/pipeline/batches")
def list_batches():
    return {"batches": batches.recent()}


@app.get("/api/pipeline/batches/{batch_id}")
def get_batch(batch_id: str):
    found = batches.describe(batch_id)
    if not found:
        raise HTTPException(404, "batch not found")
    return found


@app.get("/api/pipeline/batches/{batch_id}/variants/{variant_id}/telemetry")
def get_telemetry(batch_id: str, variant_id: str):
    found = batches.telemetry(batch_id, variant_id)
    if not found:
        raise HTTPException(404, "telemetry not found")
    return found


@app.get("/api/pipeline/gallery")
def list_gallery():
    return {"items": batches.gallery()}


@app.post("/api/pipeline/gallery", status_code=201)
def publish_to_gallery(body: PublishRequest):
    try:
        item = batches.publish(body.batchId, body.variantId)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    if not item:
        raise HTTPException(404, "variant not found")
    return item


@app.delete("/api/pipeline/gallery/{batch_id}/{variant_id}", status_code=204)
def unpublish_from_gallery(batch_id: str, variant_id: str):
    if not batches.unpublish(batch_id, variant_id):
        raise HTTPException(404, "not in the gallery")


@app.post("/api/pipeline/batches/{batch_id}/stages/{stage}", status_code=202)
def run_stage(batch_id: str, stage: str, body: StageRequest):
    if stage not in ("sumo", "rdshq", "hdmap"):
        raise HTTPException(400, "stage must be sumo, rdshq or hdmap")
    if stage == "hdmap" and not body.variant:
        raise HTTPException(400, "hdmap needs a variant")
    if not batches.describe(batch_id):
        raise HTTPException(404, "batch not found")
    return {"job": batches.submit_stage(batch_id, stage, body.variant)}


@app.post("/api/pipeline/chat")
def chat(body: ChatRequest):
    if not _agentcore:
        raise HTTPException(503, "agent runtime is not configured")
    session = body.sessionId or f"web-{uuid.uuid4().hex}"
    response = _agentcore.invoke_agent_runtime(
        agentRuntimeArn=AGENT_RUNTIME_ARN,
        runtimeSessionId=session,
        contentType="application/json",
        payload=json.dumps({"prompt": body.message, "lang": body.lang}).encode(),
    )
    raw = response["response"].read().decode()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = {"text": raw}
    return {"sessionId": session, **(data if isinstance(data, dict) else {"text": str(data)})}
