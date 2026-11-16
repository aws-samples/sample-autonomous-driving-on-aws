#!/usr/bin/env python3
"""Generate crosswalk variants from the SOTIF ontology with a Bedrock model.

    python3 scripts/augment_generate.py --n 20 --model sonnet --out /tmp/variants.json

The model only proposes; augment.crosswalk.normalize enforces bounds and
scripts/augment_run.py decides, by running SUMO, which variants are kept.
Writes <out> (variant list) and <out>.meta.json (model, latency, token usage).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import boto3  # noqa: E402
from botocore.config import Config  # noqa: E402

from augment import crosswalk, ontology  # noqa: E402

MODELS = {
    "opus": "global.anthropic.claude-opus-5-5",
    "sonnet": "global.anthropic.claude-sonnet-5-5",
    "haiku": "global.anthropic.claude-haiku-5-5",
}

PED_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": sorted(crosswalk.PED_KINDS)},
        "direction": {"type": "string", "enum": ["S2N", "N2S"]},
        "departS": {"type": "number"},
        "approachM": {"type": "number"},
        "speedMps": {"type": "number"},
    },
    "required": ["kind", "direction", "departS", "approachM", "speedMps"],
}
VARIANT_SCHEMA = {
    "type": "object",
    "properties": {
        "variantId": {"type": "string", "description": "short kebab-case id, unique"},
        "rationale": {"type": "string", "description": "one sentence: which risk the variant targets (language given in the prompt)"},
        "ontology": {"type": "object", "description": "class -> instance id used, e.g. {\"VRU\": \"ChildPedestrian\"}",
                     "additionalProperties": {"type": "string"}},
        "egoSpeedMps": {"type": "number"},
        "oncomingPeriodS": {"type": ["number", "null"]},
        "parkedCount": {"type": "integer"},
        "parkedEndX": {"type": "number"},
        "pedestrians": {"type": "array", "items": PED_SCHEMA},
        "seed": {"type": "integer"},
    },
    "required": ["variantId", "rationale", "ontology", "egoSpeedMps", "parkedCount", "pedestrians"],
}
TOOL = {"toolSpec": {"name": "submit_variants", "description": "Submit the crosswalk scenario variants.",
                     "inputSchema": {"json": {"type": "object", "properties": {"variants": {"type": "array", "items": VARIANT_SCHEMA}},
                                              "required": ["variants"]}}}}

PROMPT = """You design test-scenario variants for an automated-driving crosswalk-yield scenario that will be executed in SUMO.

## Fixed scene (cannot change)
Straight urban road along +x, 2 lanes each way, 3.5 m lanes, 2 m sidewalks. Mid-block zebra crossing at x=200 (width 3.5 m), pedestrians have priority.
Ego departs x=20 in the eastbound right lane (y=-5.25) at egoSpeedMps and keeps that speed (SUMO always brakes hard approaching the crossing to ~6 m/s, then yields if a pedestrian is on it).
So the ego reaches the crossing at roughly t = 178 / egoSpeedMps + 1.5 s.
A pedestrian with direction S2N starts on the south sidewalk (ego side) approachM metres before the crossing, at time departS, walks at speedMps, and crosses 14 m of carriageway; the ego lane is the first 3.5-7 m for S2N. N2S starts on the north sidewalk, so it reaches the ego lane last.
Oncoming westbound traffic arrives every oncomingPeriodS seconds (null = none). Parked cars (parkedCount, the last one ending at parkedEndX) stand on the south kerb before the crossing.

## Hard bounds (values outside are clamped)
{bounds}
Pedestrian kinds: {kinds}. 1-4 pedestrians per variant.

## Ontology slice (SOTIF graph; support tag says what SUMO can honour)
{slice}

Support tags: simulated = changes SUMO behaviour; visual-only = changes the scene only (SUMO has perfect perception, occlusion does NOT change behaviour);
condition-only = record it in `ontology` for the later video-generation step, it has no simulation effect; target = an outcome you may aim for, it will be measured, not assumed;
unsupported = do not use.

## Task
Produce exactly {n} variants. Each variant must make the ego actually meet a pedestrian on the crossing (time the pedestrian against the ego arrival estimate),
and the set must be diverse: spread pedestrian kinds, directions, ego speed bands, occlusion levels, lighting/weather tags, and aim for a mix of comfortable yields and near-misses
(pedestrian stepping in shortly before the ego arrives). Record every ontology instance you used in `ontology`. Call submit_variants once.
"""


LANGUAGES = {"ko": "Korean", "en": "English"}


def build_prompt(n: int, slice_: dict, focus: str | None = None, lang: str = "ko") -> str:
    compact = {cls: [{"id": i["id"], "support": i["support"], **({"maps": i["maps"]} if "maps" in i else {}),
                      **({"relations": i["relations"]} if i["relations"] else {})} for i in items if i["support"] != "unsupported"]
               for cls, items in slice_.items()}
    compact = {k: v for k, v in compact.items() if v}
    prompt = PROMPT.format(bounds=json.dumps(crosswalk.BOUNDS), kinds=", ".join(sorted(crosswalk.PED_KINDS)),
                           slice=json.dumps(compact, ensure_ascii=False, indent=1), n=n)
    prompt += f"\nWrite every `rationale` in {LANGUAGES.get(lang, 'Korean')}.\n"
    if focus:
        prompt += f"\n## User request (follow it within the bounds above; it narrows, not replaces, the diversity rule)\n{focus}\n"
    return prompt


def generate(n: int, model: str = "sonnet", region: str = "ap-northeast-2", focus: str | None = None, lang: str = "ko") -> tuple[list, dict]:
    """Ask the model for n variants; returns (variants with unique ids, metadata)."""
    model_id = MODELS.get(model, model)
    slice_ = ontology.fetch_slice()
    prompt = build_prompt(n, slice_, focus, lang)
    client = boto3.client("bedrock-runtime", region_name=region, config=Config(read_timeout=600, retries={"max_attempts": 1}))
    started = time.time()
    response = client.converse(
        modelId=model_id,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        # Opus/Sonnet 5.5 reject a forced toolChoice; the prompt asks for the call and we check for it
        toolConfig={"tools": [TOOL], "toolChoice": {"auto": {}}},
        inferenceConfig={"maxTokens": 16000},
    )
    latency = round(time.time() - started, 1)
    blocks = [b["toolUse"]["input"] for b in response["output"]["message"]["content"] if "toolUse" in b]
    if not blocks:
        raise RuntimeError("model returned no tool call")
    variants = blocks[0]["variants"]
    seen = set()
    for index, v in enumerate(variants):  # ids become directory names
        vid = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in str(v.get("variantId") or f"v{index:03d}"))[:48]
        v["variantId"] = vid if vid not in seen else f"{vid}-{index}"
        seen.add(v["variantId"])
    meta = {"model": model_id, "requested": n, "returned": len(variants), "latencyS": latency,
            "usage": response.get("usage", {}), "stopReason": response.get("stopReason"),
            "ontology": {"source": ontology.SOURCE,
                         "classes": {cls: {"instances": len(items), "usable": sum(i["support"] != "unsupported" for i in items),
                                           "relations": sum(len(objs) for i in items for objs in i["relations"].values())}
                                     for cls, items in sorted(slice_.items())}}}
    return variants, meta


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=20)
    parser.add_argument("--model", default="sonnet", help=f"{'/'.join(MODELS)} or a full Bedrock model id")
    parser.add_argument("--out", required=True)
    parser.add_argument("--region", default="ap-northeast-2")
    parser.add_argument("--focus", help="optional user request that narrows the variant set")
    parser.add_argument("--lang", default="ko", choices=sorted(LANGUAGES), help="language of the variant rationales")
    args = parser.parse_args()
    try:
        variants, meta = generate(args.n, args.model, args.region, args.focus, args.lang)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(variants, ensure_ascii=False, indent=2) + "\n")
    Path(str(out) + ".meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(json.dumps(meta))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
