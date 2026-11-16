"""SOTIF ontology as N-Triples: parse, validate against schema.json, serialize, and derive the variant-generator snapshot."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Set, Tuple

HERE = Path(__file__).resolve().parent
SCHEMA = json.loads((HERE / "schema.json").read_text())
NS = SCHEMA["namespace"]
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
RDFS_SUBCLASS = "http://www.w3.org/2000/01/rdf-schema#subClassOf"
DESCRIPTION = NS + "description"
NAME = re.compile(r"^[A-Z][A-Za-z0-9_]{1,63}$")
_LINE = re.compile(r'^<([^>]+)>\s+<([^>]+)>\s+(<[^>]+>|"(?:[^"\\]|\\.)*"(?:@[A-Za-z-]+|\^\^<[^>]+>)?)\s*\.\s*$')


_PARENT = {c["name"]: c.get("parent") for c in SCHEMA["classes"]}


def is_a(cls: str, ancestor: str) -> bool:
    while cls:
        if cls == ancestor:
            return True
        cls = _PARENT.get(cls)
    return False


def constraint_violation(predicate: str, subject_class: str, object_class: str):
    """Why (subject_class predicate object_class) breaks schema.json constraints, or None."""
    rule = SCHEMA.get("constraints", {}).get(predicate)
    if not rule:
        return None
    if "subject" in rule and not any(is_a(subject_class, c) for c in rule["subject"]):
        return f"{predicate} subject must be {rule['subject']}"
    if any(is_a(subject_class, c) for c in rule.get("subjectNot", [])):
        return f"{predicate} subject cannot be {subject_class}"
    if "object" in rule and not any(is_a(object_class, c) for c in rule["object"]):
        return f"{predicate} object must be {rule['object']}"
    return None


def _short(uri: str) -> str:
    return uri[len(NS):] if uri.startswith(NS) else uri.rsplit("/", 1)[-1].rsplit("#", 1)[-1]


def _literal(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def _unliteral(token: str) -> str:
    body = token[1:token.rindex('"')]
    return body.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")


@dataclass
class Ontology:
    classes: Dict[str, dict] = field(default_factory=dict)      # name -> {parent, description}
    instances: Dict[str, dict] = field(default_factory=dict)    # name -> {class, description}
    relations: Set[Tuple[str, str, str]] = field(default_factory=set)

    @classmethod
    def from_ntriples(cls, text: str) -> "Ontology":
        onto = cls()
        rows = []
        for number, line in enumerate(text.splitlines(), 1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            match = _LINE.match(line.strip())
            if not match:
                raise ValueError(f"line {number}: not an N-Triple: {line[:120]}")
            rows.append(match.groups())
        for s, p, o in rows:
            if p == RDF_TYPE and o == f"<{NS}Class>":
                onto.classes.setdefault(_short(s), {})
        for s, p, o in rows:
            name = _short(s)
            if p == RDF_TYPE:
                if o != f"<{NS}Class>":
                    onto.instances.setdefault(name, {})["class"] = _short(o.strip("<>"))
            elif p == RDFS_SUBCLASS:
                onto.classes.setdefault(name, {})["parent"] = _short(o.strip("<>"))
            elif p == DESCRIPTION:
                (onto.classes if name in onto.classes else onto.instances).setdefault(name, {})["description"] = _unliteral(o)
            elif p != RDFS_LABEL and o.startswith("<"):
                onto.relations.add((name, _short(p), _short(o.strip("<>"))))
        return onto

    def to_ntriples(self) -> str:
        lines: List[str] = []
        for name in sorted(self.classes):
            c = self.classes[name]
            lines += [f"<{NS}{name}> <{RDF_TYPE}> <{NS}Class> .", f"<{NS}{name}> <{RDFS_LABEL}> {_literal(name)} ."]
            if c.get("description"):
                lines.append(f"<{NS}{name}> <{DESCRIPTION}> {_literal(c['description'])} .")
            if c.get("parent"):
                lines.append(f"<{NS}{name}> <{RDFS_SUBCLASS}> <{NS}{c['parent']}> .")
        for name in sorted(self.instances):
            i = self.instances[name]
            lines += [f"<{NS}{name}> <{RDF_TYPE}> <{NS}{i['class']}> .", f"<{NS}{name}> <{RDFS_LABEL}> {_literal(name)} ."]
            if i.get("description"):
                lines.append(f"<{NS}{name}> <{DESCRIPTION}> {_literal(i['description'])} .")
        lines += [f"<{NS}{s}> <{NS}{p}> <{NS}{o}> ." for s, p, o in sorted(self.relations)]
        return "\n".join(lines) + "\n"

    def validate(self) -> List[str]:
        """Problems that make the file unusable; build.py drops bad LLM output before this point."""
        problems = []
        schema_classes = {c["name"] for c in SCHEMA["classes"]}
        for name, inst in self.instances.items():
            if inst.get("class") not in schema_classes:
                problems.append(f"{name}: class {inst.get('class')} not in schema")
            if not NAME.match(name):
                problems.append(f"{name}: bad name")
        known = set(self.classes) | set(self.instances)
        for s, p, o in self.relations:
            if p not in SCHEMA["predicates"]:
                problems.append(f"{s} {p} {o}: predicate not in schema")
            if s not in known or o not in known:
                problems.append(f"{s} {p} {o}: unknown endpoint")
            elif why := constraint_violation(p, self.class_of(s), self.class_of(o)):
                problems.append(f"{s} {p} {o}: {why}")
        problems += [f"required instance {n} missing" for n in SCHEMA["requiredInstances"] if n not in self.instances]
        return problems

    def class_of(self, name: str) -> str:
        return self.instances[name]["class"] if name in self.instances else name

    def snapshot(self, classes: List[str]) -> Dict[str, List[dict]]:
        """Same shape as augment.ontology._query_neptune(): {class: [{id, relations}]} for the given classes."""
        out: Dict[str, List[dict]] = {}
        for cls in classes:
            items = []
            for name in sorted(n for n, i in self.instances.items() if i["class"] == cls):
                rel: Dict[str, List[str]] = {}
                for s, p, o in sorted(self.relations):
                    if s == name:
                        rel.setdefault(p, []).append(o)
                items.append({"id": name, "relations": rel})
            if items:
                out[cls] = items
        return out
