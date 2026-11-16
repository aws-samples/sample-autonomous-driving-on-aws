"""Extend the SOTIF ontology with an LLM, class by class, and keep only what passes the schema.

    python3 -m sotif.build --per-class 3                        # extend sotif/ontology.nt in place
    python3 -m sotif.build --docs ./notes --classes VRU,WeatherCondition --model opus

For each class the model sees the SOTIF schema, the class's existing instances and every known entity name,
plus optional source documents. It proposes new instances, descriptions for existing ones, and relations.
Rejected: bad names, duplicates (case-insensitive), predicates outside schema.json, unknown relation targets,
relations whose subject is not in the class. With --docs, also relations that cite no [source] (or say they are
inferred), and new instances that no kept relation mentions. Existing instances are never removed.
Writes the ontology file, augment/ontology_snapshot.json, sotif/provenance.json (with --docs) and a report.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional

import boto3
from botocore.config import Config

from sotif.rdf import NAME, SCHEMA, Ontology, constraint_violation

ROOT = Path(__file__).resolve().parents[1]
MODELS = {"opus": "global.anthropic.claude-opus-5-5", "sonnet": "global.anthropic.claude-sonnet-5-5", "haiku": "global.anthropic.claude-haiku-5-5"}
DOC_LIMIT = 14000
CHUNK_CHARS = 900

TOOL = {"toolSpec": {"name": "submit_ontology", "description": "Submit additions for one ontology class.", "inputSchema": {"json": {
    "type": "object",
    "properties": {
        "instances": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string", "description": "UpperCamelCase, ASCII letters/digits, unique"},
            "description": {"type": "string", "description": "한국어 1문장: 무엇이고 SOTIF상 왜 중요한지"}},
            "required": ["name", "description"]}},
        "describe": {"type": "object", "additionalProperties": {"type": "string"},
                     "description": "existing instance name -> 한국어 1문장 설명 (only for instances listed without a description)"},
        "relations": {"type": "array", "items": {"type": "object", "properties": {
            "subject": {"type": "string"}, "predicate": {"type": "string"}, "object": {"type": "string"},
            "evidence": {"type": "string", "description": "short reason; with source excerpts, start with the [source] id"}},
            "required": ["subject", "predicate", "object"]}},
    },
    "required": ["instances", "relations"]}}}}

PROMPT = """You are an ontology engineer for automated-driving safety (SOTIF, ISO 21448): triggering conditions,
functional insufficiencies of perception/planning, and the hazardous events they lead to.

## Class to extend: {cls}
Description: {description}  (parent chain: {chain}; SOTIF category: {category})

## Existing instances of {cls}
{existing}

## All known entities by class (relation targets must be one of these or one of your new instances)
{known}

## Predicates (use only these; subject must be an instance of {cls})
{predicates}
{documents}
## Task
1. Propose up to {per_class} NEW instances of {cls} that matter for testing urban automated driving and are not synonyms of existing ones.
2. For existing instances listed with "(no description)", give a one-sentence Korean description in `describe`.
3. Add relations from {cls} instances (new or existing) to known entities, e.g. a weather condition `degrades` a sensor,
   an occlusion `triggers` a hazardous event. Prefer few, defensible relations; give `evidence`.
Call submit_ontology once."""


def _chain(name: str) -> str:
    parents = {c["name"]: c.get("parent") for c in SCHEMA["classes"]}
    out = []
    while name:
        out.append(name)
        name = parents.get(name)
    return " < ".join(out)


def _chunks(docs: Optional[Path]) -> List[tuple]:
    """(source name, paragraph) pairs of roughly CHUNK_CHARS from every .txt/.md file."""
    if not docs:
        return []
    out = []
    for path in sorted(p for p in docs.rglob("*") if p.suffix.lower() in (".txt", ".md")):
        text = re.sub(r"[ \t]+", " ", path.read_text(errors="ignore"))
        buf = ""
        for para in re.split(r"\n\s*\n|(?<=[.!?])\n", text):
            buf += para.strip() + "\n"
            if len(buf) >= CHUNK_CHARS:
                out.append((path.stem, buf.strip()))
                buf = ""
        if buf.strip():
            out.append((path.stem, buf.strip()))
    return out


def _terms(onto: Ontology, cls: str) -> set:
    words = re.findall(r"[A-Z][a-z]+|[A-Z]+(?![a-z])|[a-z]+", " ".join([cls] + [n for n, i in onto.instances.items() if i["class"] == cls]))
    return {w.lower() for w in words if len(w) > 2} | {cls.lower()}


def _excerpts(onto: Ontology, cls: str, chunks: List[tuple]) -> str:
    """The paragraphs that mention the class and its instances most, up to DOC_LIMIT characters."""
    if not chunks:
        return ""
    terms = _terms(onto, cls)
    scored = []
    for index, (source, chunk) in enumerate(chunks):
        words = re.findall(r"[a-z]+", chunk.lower())
        hits = sum(1 for w in words if w in terms or w.rstrip("s") in terms)
        if hits:
            scored.append((hits / (len(words) ** 0.5 + 1), index, source, chunk))
    picked, used = [], 0
    for _, index, source, chunk in sorted(scored, reverse=True):
        if used + len(chunk) > DOC_LIMIT:
            continue
        picked.append((index, source, chunk))
        used += len(chunk)
    body = "\n\n".join(f"[{source}] {chunk}" for _, source, chunk in sorted(picked))
    return ("\n## Source excerpts (prefer facts stated here; cite the [source] id in `evidence`; "
            "do not invent facts that are not supported)\n" + body + "\n") if body else ""


def _prompt(onto: Ontology, cls: str, per_class: int, chunks: List[tuple]) -> str:
    meta = next(c for c in SCHEMA["classes"] if c["name"] == cls)
    existing = [f"- {n}: {i.get('description') or '(no description)'}" for n, i in sorted(onto.instances.items()) if i["class"] == cls]
    by_class: Dict[str, List[str]] = {}
    for n, i in onto.instances.items():
        by_class.setdefault(i["class"], []).append(n)
    known = "\n".join(f"- {c}: {', '.join(sorted(v))}" for c, v in sorted(by_class.items()))
    predicates = "\n".join(f"- {p}: {d}" for p, d in SCHEMA["predicates"].items())
    return PROMPT.format(cls=cls, description=meta.get("description") or "-", chain=_chain(cls), category=meta.get("sotif_category") or "-",
                         existing="\n".join(existing) or "(none)", known=known, predicates=predicates, documents=_excerpts(onto, cls, chunks), per_class=per_class)


def _ask(client, model_id: str, prompt: str) -> tuple:
    started = time.time()
    response = client.converse(modelId=model_id, messages=[{"role": "user", "content": [{"text": prompt}]}],
                               toolConfig={"tools": [TOOL], "toolChoice": {"auto": {}}}, inferenceConfig={"maxTokens": 8000})
    blocks = [b["toolUse"]["input"] for b in response["output"]["message"]["content"] if "toolUse" in b]
    return (blocks[0] if blocks else None), round(time.time() - started, 1), response.get("usage", {})


INFERRED = re.compile(r"추론|가정|infer|assum|speculat", re.I)  # "(inference)" and "가정" slipped through "inferred"


def cited_source(evidence: Optional[str], sources: set) -> Optional[str]:
    """The [source] id the evidence starts with, if it is a known source and not marked as the model's own inference."""
    match = re.match(r"\s*\[([A-Za-z0-9.\-]+)\]", evidence or "")
    return match.group(1) if match and match.group(1) in sources and not INFERRED.search(evidence) else None


def merge(onto: Ontology, cls: str, proposal: dict, per_class: int, sources: Optional[set] = None) -> dict:
    """Apply one class's proposal; returns what was added and what was dropped (with reasons).

    With `sources`, a relation is kept only if its evidence cites one of them.
    """
    added, dropped = {"instances": [], "descriptions": [], "relations": []}, []
    lower = {n.lower() for n in onto.instances} | {c.lower() for c in onto.classes}
    for item in (proposal.get("instances") or [])[:per_class]:
        name, desc = str(item.get("name", "")).strip(), str(item.get("description", "")).strip()
        if not NAME.match(name):
            dropped.append({"instance": name, "reason": "bad name"})
        elif name.lower() in lower:
            dropped.append({"instance": name, "reason": "duplicate"})
        else:
            onto.instances[name] = {"class": cls, "description": desc}
            lower.add(name.lower())
            added["instances"].append(name)
    for name, desc in (proposal.get("describe") or {}).items():
        inst = onto.instances.get(name)
        if inst and inst["class"] == cls and not inst.get("description") and str(desc).strip():
            inst["description"] = str(desc).strip()
            added["descriptions"].append(name)
    known = set(onto.instances) | set(onto.classes)
    for rel in proposal.get("relations") or []:
        s, p, o = (str(rel.get(k, "")).strip() for k in ("subject", "predicate", "object"))
        reason = ("predicate not in schema" if p not in SCHEMA["predicates"]
                  else "subject not in class" if onto.instances.get(s, {}).get("class") != cls
                  else "unknown object" if o not in known
                  else "self relation" if s == o
                  else constraint_violation(p, cls, onto.class_of(o))
                  or ("duplicate" if (s, p, o) in onto.relations
                      else "no source citation" if sources is not None and not cited_source(rel.get("evidence"), sources) else None))
        if reason:
            dropped.append({"relation": [s, p, o], "reason": reason})
        else:
            onto.relations.add((s, p, o))
            added["relations"].append([s, p, o, rel.get("evidence")])
    return {"added": added, "dropped": dropped}


def prune_unsupported(onto: Ontology, added_instances: set, dropped: list) -> set:
    """Remove instances added in this run that no kept relation mentions (nothing in the sources supports them)."""
    used = {x for s, _, o in onto.relations for x in (s, o)}
    removed = {n for n in added_instances if n not in used}
    for name in removed:
        del onto.instances[name]
        dropped.append({"instance": name, "reason": "no cited relation"})
    return removed

def _write_provenance(path: Path, report: dict, onto: Ontology) -> None:
    """relation -> {source, evidence}; instance -> sources of the relations that mention it. Merged with earlier runs."""
    prov = json.loads(path.read_text()) if path.exists() else {"relations": {}, "instances": {}}
    for entry in report["classes"].values():
        for s, p, o, evidence in entry.get("added", {}).get("relations", []):
            source = re.match(r"\s*\[([A-Za-z0-9.\-]+)\]", evidence).group(1)
            prov["relations"][f"{s} {p} {o}"] = {"source": source, "evidence": re.sub(r"^\s*\[[^\]]+\]\s*", "", evidence)}
    for key, value in prov["relations"].items():
        s, _, o = key.split(" ")
        for name in (s, o):
            if name in onto.instances:
                entry = prov["instances"].setdefault(name, {"sources": []})
                if value["source"] not in entry["sources"]:
                    entry["sources"].append(value["source"])
    prov["relations"] = dict(sorted(prov["relations"].items()))
    prov["instances"] = dict(sorted(prov["instances"].items()))
    path.write_text(json.dumps(prov, ensure_ascii=False, indent=1) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ontology", default=str(ROOT / "sotif" / "ontology.nt"))
    parser.add_argument("--classes", help="comma-separated; default: every schema class that has instances")
    parser.add_argument("--per-class", type=int, default=3)
    parser.add_argument("--docs", type=Path, help="directory of .txt/.md source documents")
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--region", default="ap-northeast-2")
    parser.add_argument("--report", default=str(ROOT / "build" / "ontology-report.json"))
    parser.add_argument("--snapshot", default=str(ROOT / "augment" / "ontology_snapshot.json"))
    parser.add_argument("--provenance", default=str(ROOT / "sotif" / "provenance.json"))
    parser.add_argument("--allow-uncited", action="store_true", help="with --docs, also keep relations that cite no source")
    parser.add_argument("--dry-run", action="store_true", help="print the first prompt and exit")
    args = parser.parse_args()

    path = Path(args.ontology)
    onto = Ontology.from_ntriples(path.read_text())
    if problems := onto.validate():
        print("input ontology is invalid:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 1
    schema_classes = [c["name"] for c in SCHEMA["classes"]]
    classes = args.classes.split(",") if args.classes else [c for c in schema_classes if any(i["class"] == c for i in onto.instances.values())]
    if unknown := [c for c in classes if c not in schema_classes]:
        parser.error(f"unknown classes: {unknown}")
    chunks = _chunks(args.docs)
    if args.dry_run:
        print(_prompt(onto, classes[0], args.per_class, chunks))
        return 0

    model_id = MODELS.get(args.model, args.model)
    client = boto3.client("bedrock-runtime", region_name=args.region, config=Config(read_timeout=600, retries={"max_attempts": 2}))
    before = (len(onto.instances), len(onto.relations))
    # Prompts are built from the same starting ontology so the calls can run in parallel; merging is sequential.
    prompts = {cls: _prompt(onto, cls, args.per_class, chunks) for cls in classes}
    with ThreadPoolExecutor(max_workers=4) as pool:
        answers = dict(zip(classes, pool.map(lambda c: _ask(client, model_id, prompts[c]), classes)))
    sources = None if (not args.docs or args.allow_uncited) else {source for source, _ in chunks}
    report = {"model": model_id, "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "docs": str(args.docs) if args.docs else None, "classes": {}}
    for cls in classes:
        proposal, latency, usage = answers[cls]
        result = merge(onto, cls, proposal, args.per_class, sources) if proposal else {"added": {}, "dropped": [{"reason": "no tool call"}]}
        report["classes"][cls] = {"latencyS": latency, "usage": usage, **result}
    if sources is not None:
        for cls, entry in report["classes"].items():
            added = entry.get("added", {})
            removed = prune_unsupported(onto, set(added.get("instances", [])), entry["dropped"])
            added["instances"] = [n for n in added.get("instances", []) if n not in removed]
    for cls, entry in report["classes"].items():
        added = entry.get("added", {})
        print(f"{cls:24s} +{len(added.get('instances', []))} instances +{len(added.get('descriptions', []))} descriptions "
              f"+{len(added.get('relations', []))} relations, {len(entry['dropped'])} dropped ({entry['latencyS']}s)")

    if problems := onto.validate():
        print("result failed validation (not written):\n  " + "\n  ".join(problems), file=sys.stderr)
        return 1
    path.write_text(onto.to_ntriples())
    if sources is not None:
        _write_provenance(Path(args.provenance), report, onto)
    from augment import ontology as augment_ontology

    Path(args.snapshot).write_text(json.dumps(onto.snapshot(augment_ontology.CLASSES), ensure_ascii=False, indent=1, sort_keys=True) + "\n")
    report["totals"] = {"instances": [before[0], len(onto.instances)], "relations": [before[1], len(onto.relations)]}
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n")
    print(json.dumps(report["totals"]), f"-> {path}, {args.snapshot}, {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
