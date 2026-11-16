"""Neptune SPARQL over HTTPS, SigV4-signed when DRIVE_ATLAS_NEPTUNE_IAM=1 (IAM database authentication).

    python3 -m sotif.neptune load sotif/ontology.nt          # replace the graph with the file (or s3://bucket/key)
    python3 -m sotif.neptune count
Endpoint: DRIVE_ATLAS_NEPTUNE_SPARQL=https://<cluster endpoint>:8182/sparql
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse

import urllib3
from typing import Dict, List

ENDPOINT = os.environ.get("DRIVE_ATLAS_NEPTUNE_SPARQL")
IAM = os.environ.get("DRIVE_ATLAS_NEPTUNE_IAM", "").lower() in ("1", "true", "yes")
CHUNK = 400


def _post(form: Dict[str, str], timeout: int = 60) -> dict:
    if not ENDPOINT or not ENDPOINT.startswith("https://"):
        raise RuntimeError("DRIVE_ATLAS_NEPTUNE_SPARQL must be set to an https:// URL")
    body = urllib.parse.urlencode(form)
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/sparql-results+json, application/json"}
    if IAM:
        import boto3
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest

        session = boto3.Session()
        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or session.region_name
        request = AWSRequest(method="POST", url=ENDPOINT, data=body, headers=headers)
        SigV4Auth(session.get_credentials().get_frozen_credentials(), "neptune-db", region).add_auth(request)
        headers = dict(request.headers.items())
    response = urllib3.request("POST", ENDPOINT, body=body.encode(), headers=headers, timeout=float(timeout), retries=False)
    if response.status >= 400:
        raise RuntimeError(f"Neptune HTTP {response.status}: {response.data.decode(errors='replace')[:500]}")
    return json.loads(response.data) if response.data.strip() else {}


def query(sparql: str) -> List[Dict[str, str]]:
    bindings = _post({"query": sparql}).get("results", {}).get("bindings", [])
    return [{k: v["value"] for k, v in b.items()} for b in bindings]


def update(sparql: str) -> None:
    _post({"update": sparql}, timeout=300)


def count() -> int:
    return int(query("SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }")[0]["n"])


def load(ntriples: str) -> int:
    """Replace the whole graph with the given N-Triples; returns the triple count afterwards."""
    lines = [line for line in ntriples.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    update("DROP ALL")
    for start in range(0, len(lines), CHUNK):
        update("INSERT DATA {\n" + "\n".join(lines[start:start + CHUNK]) + "\n}")
    loaded = count()
    if loaded != len(set(lines)):
        raise RuntimeError(f"loaded {loaded} triples, file has {len(set(lines))}")
    return loaded


def _read(source: str) -> str:
    if source.startswith("s3://"):
        import boto3

        bucket, key = source[5:].split("/", 1)
        return boto3.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read().decode()
    with open(source, encoding="utf-8") as handle:
        return handle.read()


def main(argv: List[str]) -> int:
    if argv[:1] == ["count"]:
        print(count())
        return 0
    if len(argv) == 2 and argv[0] == "load":
        print(json.dumps({"loaded": load(_read(argv[1])), "endpoint": ENDPOINT}))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
