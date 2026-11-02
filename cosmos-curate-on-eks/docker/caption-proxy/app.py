"""OpenAI-compatible caption endpoint for Curate's `--captioning-algorithm openai`, backed by a
Strands Agent on Amazon Bedrock (Claude Opus 5.5).

Curate posts one request per caption window: a base64 mp4 (`video_url`) plus the prompt text.
Claude takes images, not video, so we sample N frames uniformly with ffmpeg and send them in
temporal order. Text-only requests (e.g. Curate's --enhance-captions with the openai variant:
system prompt + caption) are passed through as a plain chat turn. Auth is the pod's AWS
credentials (EKS Pod Identity); no API keys.

Endpoints: GET /v1/models, POST /v1/chat/completions, GET /healthz.
One JSON line per request is logged to stdout (tokens, latency) for cost accounting.
"""

import base64
import json
import os
import subprocess  # nosec B404 - fixed ffmpeg/ffprobe argument lists, no shell
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from strands import Agent
from strands.models import BedrockModel

MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "global.anthropic.claude-opus-5-5")
SERVED_NAME = os.environ.get("SERVED_MODEL_NAME", "claude-opus-5-5")
NUM_FRAMES = int(os.environ.get("NUM_FRAMES", "8"))
FRAME_WIDTH = int(os.environ.get("FRAME_WIDTH", "1568"))  # Claude's long-edge image cap
MAX_CONCURRENCY = int(os.environ.get("MAX_CONCURRENCY", "8"))
PORT = int(os.environ.get("PORT", "8080"))

_model = BedrockModel(model_id=MODEL_ID, region_name=os.environ.get("AWS_REGION"))
_slots = threading.BoundedSemaphore(MAX_CONCURRENCY)


def sample_frames(mp4: bytes) -> tuple[list[bytes], float]:
    """Return NUM_FRAMES jpegs spread evenly over the clip, and the clip duration in seconds."""
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "in.mp4"
        src.write_bytes(mp4)
        dur = float(
            subprocess.run(  # nosec B603 B607 - fixed binary from the image, list args
                ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(src)],
                check=True, capture_output=True, text=True,
            ).stdout.strip() or 0
        )
        rate = NUM_FRAMES / dur if dur > 0 else 1
        subprocess.run(  # nosec B603 B607 - fixed binary from the image, list args
            ["ffmpeg", "-v", "error", "-i", str(src), "-vf", f"fps={rate:.6f},scale='min({FRAME_WIDTH},iw)':-2",
             "-frames:v", str(NUM_FRAMES), "-q:v", "3", str(Path(tmp) / "f%03d.jpg")],
            check=True,
        )
        return [p.read_bytes() for p in sorted(Path(tmp).glob("f*.jpg"))], dur


def _text(content: object) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(p["text"] for p in content if p.get("type") == "text")


def chat_text(messages: list[dict]) -> tuple[str, dict]:
    system = "\n".join(_text(m["content"]) for m in messages if m["role"] == "system") or None
    user = "\n".join(_text(m["content"]) for m in messages if m["role"] == "user")
    result = Agent(model=_model, system_prompt=system, callback_handler=None)(user)
    usage = result.metrics.accumulated_usage
    return str(result).strip(), {"prompt_tokens": usage["inputTokens"], "completion_tokens": usage["outputTokens"],
                                  "total_tokens": usage["totalTokens"], "frames": 0}


def caption(messages: list[dict]) -> tuple[str, dict]:
    parts = messages[-1]["content"]
    if isinstance(parts, str) or not any(p.get("type") == "video_url" for p in parts):
        return chat_text(messages)
    video_url = next(p["video_url"]["url"] for p in parts if p.get("type") == "video_url")
    prompt = "\n".join(p["text"] for p in parts if p.get("type") == "text")
    frames, dur = sample_frames(base64.b64decode(video_url.split(",", 1)[1]))
    content = [{"image": {"format": "jpeg", "source": {"bytes": f}}} for f in frames]
    content.append({"text": f"The images above are {len(frames)} frames sampled uniformly, in temporal order, "
                            f"from a {dur:.1f}-second video clip. Treat them as that video.\n\n{prompt}"})
    agent = Agent(model=_model, callback_handler=None)  # fresh agent: no history between windows
    result = agent([{"role": "user", "content": content}])
    usage = result.metrics.accumulated_usage
    return str(result).strip(), {"prompt_tokens": usage["inputTokens"], "completion_tokens": usage["outputTokens"],
                                  "total_tokens": usage["totalTokens"], "frames": len(frames)}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path.startswith("/healthz"):
            self._send(200, {"ok": True})
        elif self.path.startswith("/v1/models"):
            self._send(200, {"object": "list", "data": [{"id": SERVED_NAME, "object": "model"}]})
        else:
            self._send(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:
        if not self.path.startswith("/v1/chat/completions"):
            self._send(404, {"error": {"message": "not found"}})
            return
        req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        t0 = time.time()
        try:
            with _slots:
                text, usage = caption(req["messages"])
        except Exception as exc:  # surfaced to Curate as a 500; it retries per --openai-caption-retries
            print(json.dumps({"event": "error", "error": repr(exc)[:500]}), flush=True)
            self._send(500, {"error": {"message": repr(exc)[:500], "type": "server_error"}})
            return
        print(json.dumps({"event": "caption", "latency_s": round(time.time() - t0, 2), **usage}), flush=True)
        self._send(200, {
            "id": f"chatcmpl-{uuid.uuid4().hex}", "object": "chat.completion", "created": int(time.time()),
            "model": SERVED_NAME,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": {k: usage[k] for k in ("prompt_tokens", "completion_tokens", "total_tokens")},
        })

    def log_message(self, *args: object) -> None:  # keep stdout for the JSON usage lines
        pass


if __name__ == "__main__":
    # Listens inside the pod; reachable only through the ClusterIP Service
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()  # nosec B104
