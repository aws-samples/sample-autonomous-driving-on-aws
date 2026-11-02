#!/usr/bin/env python3
"""Exit 0 only if the index has documents and both text and kNN search return hits."""
import argparse
import sys
import time
from urllib.parse import urlparse

import boto3
from opensearchpy import AWSV4SignerAuth, OpenSearch, RequestsHttpConnection


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--collection-endpoint", required=True)
    p.add_argument("--region", required=True)
    p.add_argument("--index-name", default="video-clips")
    p.add_argument("--query", default="video clip scene camera")
    p.add_argument("--location-prefix", help="only count documents whose clip_location starts with this (one run's output)")
    p.add_argument("--min-docs", type=int, default=1, help="wait until at least this many documents are searchable")
    args = p.parse_args()

    auth = AWSV4SignerAuth(boto3.Session().get_credentials(), args.region, "aoss")
    client = OpenSearch(
        hosts=[{"host": urlparse(args.collection_endpoint).hostname, "port": 443}],
        http_auth=auth, use_ssl=True, verify_certs=True, connection_class=RequestsHttpConnection,
    )

    # Newly written AOSS documents took up to ~90 s to become searchable (us-west-2); allow 6 min
    for attempt in range(24):
        try:
            count_body = {"query": {"prefix": {"clip_location": args.location_prefix}}} if args.location_prefix else None
            count = client.count(index=args.index_name, body=count_body)["count"]
            if count < args.min_docs:
                raise LookupError(f"{count}/{args.min_docs} documents visible")
            text = client.search(index=args.index_name, body={
                "size": 3, "query": {"match": {"caption": args.query}}, "_source": ["clip_id", "caption"]})
            doc = client.search(index=args.index_name, body={"size": 1})["hits"]["hits"][0]["_source"]
            knn = client.search(index=args.index_name, body={
                "size": 3, "query": {"knn": {"embedding": {"vector": doc["embedding"], "k": 3}}},
                "_source": ["clip_id", "clip_location"]})
        except Exception as e:  # index/doc not visible yet
            print(f"  not searchable yet ({type(e).__name__}: {e}), retry {attempt + 1}/24")
            time.sleep(15)
            continue
        text_hits = text["hits"]["hits"]
        knn_hits = knn["hits"]["hits"]
        print(f"documents: {count}")
        print(f"text search '{args.query}': {len(text_hits)} hits")
        for h in text_hits:
            print(f"  {h['_score']:.2f} {h['_source']['clip_id'][:12]} {h['_source']['caption'][:80]!r}")
        print(f"kNN search (dim {len(doc['embedding'])}): {len(knn_hits)} hits")
        for h in knn_hits:
            print(f"  {h['_score']:.4f} {h['_source']['clip_location']}")
        return 0 if count >= args.min_docs and text_hits and knn_hits else 1
    print("❌ index never became searchable")
    return 1


if __name__ == "__main__":
    sys.exit(main())
