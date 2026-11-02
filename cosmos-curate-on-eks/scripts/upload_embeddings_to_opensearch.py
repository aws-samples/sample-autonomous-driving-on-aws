#!/usr/bin/env python3
"""
Upload video embeddings and metadata to OpenSearch Serverless.

This script:
1. Loads embeddings (pickle) and metadata (JSON) from S3
2. Creates a kNN vector index in OpenSearch if it doesn't exist
3. Uploads documents with embeddings for vector similarity search
4. Provides examples of text and vector search queries

Usage:
    python3 upload_embeddings_to_opensearch.py \
        --collection-endpoint <OPENSEARCH_ENDPOINT> \
        --bucket cosmos-curate-<ACCOUNT>-<REGION> \
        --prefix outputs/sample/
"""

import argparse
import io
import json
import pickle  # nosec B403 - only numpy arrays are unpickled (_NumpyOnlyUnpickler)
import sys
import time
from typing import Dict, List, Any

import boto3
from opensearchpy import OpenSearch, RequestsHttpConnection, AWSV4SignerAuth
from opensearchpy.helpers import bulk


class _NumpyOnlyUnpickler(pickle.Unpickler):
    """Curate writes each clip embedding as a pickled numpy array; refuse anything else."""

    _ALLOWED = {
        ("numpy", "ndarray"), ("numpy", "dtype"),
        ("numpy.core.multiarray", "_reconstruct"), ("numpy._core.multiarray", "_reconstruct"),
        ("numpy.core.multiarray", "scalar"), ("numpy._core.multiarray", "scalar"),
    }

    def find_class(self, module, name):
        if (module, name) in self._ALLOWED:
            return super().find_class(module, name)
        raise pickle.UnpicklingError(f"refusing to unpickle {module}.{name}")  # nosemgrep: avoid-pickle


def load_embedding(data: bytes):
    return _NumpyOnlyUnpickler(io.BytesIO(data)).load()  # nosec B301 - restricted to numpy arrays


def create_opensearch_client(endpoint: str, region: str = 'ap-northeast-2') -> OpenSearch:
    """Create authenticated OpenSearch client using AWS credentials."""
    credentials = boto3.Session().get_credentials()
    auth = AWSV4SignerAuth(credentials, region, 'aoss')

    client = OpenSearch(
        hosts=[{'host': endpoint.replace('https://', '').replace('/', ''), 'port': 443}],
        http_auth=auth,
        use_ssl=True,
        verify_certs=True,
        connection_class=RequestsHttpConnection,
        timeout=30,
    )
    return client


def create_knn_index(client: OpenSearch, index_name: str = 'video-clips') -> bool:
    """Create kNN vector index with 768-dimensional Cosmos-Embed1 vectors."""
    if client.indices.exists(index=index_name):
        print(f"✓ Index '{index_name}' already exists")
        return False

    # index.knn is required, otherwise kNN queries fail with "not built for ANN search"
    index_body = {
        "settings": {"index": {"knn": True}},
        "mappings": {
            "properties": {
                "clip_id": {"type": "keyword"},
                "video_id": {"type": "keyword"},
                "video_source": {"type": "keyword"},
                "clip_location": {"type": "keyword"},
                "caption": {
                    "type": "text",
                    "fields": {
                        "keyword": {"type": "keyword"}
                    }
                },
                "caption_windows": {"type": "integer"},
                "embedding": {
                    "type": "knn_vector",
                    "dimension": 768,
                    "method": {"name": "hnsw", "engine": "faiss", "space_type": "l2"}
                },
                "duration_s": {"type": "float"},
                "width": {"type": "integer"},
                "height": {"type": "integer"},
                "framerate": {"type": "float"},
                "num_frames": {"type": "integer"},
            }
        }
    }

    response = client.indices.create(index=index_name, body=index_body)
    print(f"✓ Created index '{index_name}': {response}")
    return True


def load_embeddings_from_s3(bucket: str, prefix: str) -> List[Dict[str, Any]]:
    """Load embeddings and metadata from S3."""
    s3 = boto3.client('s3')

    # List all embedding pickle files
    embedding_prefix = f"{prefix}ce1_embd_336p/"
    response = s3.list_objects_v2(Bucket=bucket, Prefix=embedding_prefix)

    if 'Contents' not in response:
        print(f"❌ No embeddings found at s3://{bucket}/{embedding_prefix}")
        return []

    documents = []

    for obj in response['Contents']:
        if not obj['Key'].endswith('.pickle'):
            continue

        clip_id = obj['Key'].split('/')[-1].replace('.pickle', '')

        # Load embedding
        emb_obj = s3.get_object(Bucket=bucket, Key=obj['Key'])
        embedding = load_embedding(emb_obj['Body'].read())

        # Load metadata
        meta_key = f"{prefix}metas/v0/{clip_id}.json"
        try:
            meta_obj = s3.get_object(Bucket=bucket, Key=meta_key)
            metadata = json.loads(meta_obj['Body'].read())
        except s3.exceptions.NoSuchKey:
            print(f"⚠ No metadata for {clip_id}, skipping")
            continue

        # Aggregate captions from all windows
        captions = []
        for window in metadata.get('windows', []):
            if window.get('caption_status') == 'success':
                captions.append(window.get('openai_caption', ''))

        full_caption = '\n\n'.join(captions)

        # Convert embedding to list of float
        if hasattr(embedding, 'tolist'):
            emb_list = embedding.tolist()
        elif isinstance(embedding, list):
            emb_list = embedding
        else:
            emb_list = list(embedding)

        # Flatten if nested and ensure all values are plain float
        if emb_list and isinstance(emb_list[0], (list, tuple)):
            emb_list = emb_list[0]  # Take first row if 2D

        emb_list = [float(x) for x in emb_list]

        # Create document
        doc = {
            'clip_id': clip_id,
            'video_id': metadata.get('source_video', '').split('/')[-1],
            'video_source': metadata.get('source_video', ''),
            'clip_location': metadata.get('clip_location', ''),
            'caption': full_caption,
            'caption_windows': len(metadata.get('windows', [])),
            'embedding': emb_list,
            'duration_s': float((metadata.get('end_ns', 0) - metadata.get('start_ns', 0)) / 1e9),
            'width': int(metadata.get('width', 0)),
            'height': int(metadata.get('height', 0)),
            'framerate': float(metadata.get('framerate', 0)),
            'num_frames': int(metadata.get('num_frames', 0)),
        }

        documents.append(doc)
        print(f"  Loaded {clip_id[:8]}... ({len(captions)} caption windows)")

    print(f"✓ Loaded {len(documents)} documents from S3")
    return documents


def upload_documents(client: OpenSearch, documents: List[Dict[str, Any]], index_name: str = 'video-clips'):
    """Upload documents to OpenSearch one by one (OpenSearch Serverless compatible)."""
    success_count = 0
    failed_count = 0
    errors = []

    for i, doc in enumerate(documents, 1):
        try:
            # Upload document without explicit ID (Serverless auto-generates)
            response = client.index(
                index=index_name,
                body=doc,
                refresh=False  # Don't refresh after each document
            )
            success_count += 1
            print(f"  Uploaded {i}/{len(documents)}: {doc['clip_id'][:16]}...")
        except Exception as e:
            failed_count += 1
            errors.append(f"{doc['clip_id'][:16]}: {str(e)[:100]}")
            if failed_count <= 3:
                print(f"  ⚠ Failed {doc['clip_id'][:16]}: {e}")

    print(f"\n✓ Uploaded {success_count}/{len(documents)} documents")
    if failed_count > 0:
        print(f"⚠ Failed {failed_count} documents")
        if failed_count > 3:
            print(f"  (showing first 3 errors)")

    # Refresh index once at the end
    if success_count > 0:
        try:
            client.indices.refresh(index=index_name)
            print(f"✓ Index refreshed")
        except Exception as e:
            print(f"⚠ Refresh failed (may still be searchable): {e}")
    else:
        print(f"⚠ No documents uploaded, skipping refresh")


def example_searches(client: OpenSearch, index_name: str = 'video-clips'):
    """Show example text and vector searches."""
    print("\n" + "="*60)
    print("Example Searches")
    print("="*60)

    # Text search
    print("\n1. Text Search: 'person walking street'")
    query = {
        "size": 3,
        "query": {
            "match": {
                "caption": "person walking street"
            }
        },
        "_source": ["clip_id", "video_id", "clip_location"]
    }
    results = client.search(index=index_name, body=query)
    for hit in results['hits']['hits']:
        print(f"   Score: {hit['_score']:.2f} | {hit['_source']['clip_id'][:16]}... | {hit['_source']['video_id']}")

    # Get first embedding for vector search
    print("\n2. Vector Search: Find similar clips")
    first_doc = client.search(index=index_name, body={"size": 1})
    if first_doc['hits']['hits']:
        query_vector = first_doc['hits']['hits'][0]['_source']['embedding']
        query_id = first_doc['hits']['hits'][0]['_source']['clip_id']

        knn_query = {
            "size": 3,
            "query": {
                "knn": {
                    "embedding": {
                        "vector": query_vector,
                        "k": 3
                    }
                }
            },
            "_source": ["clip_id", "video_id", "clip_location"]
        }
        results = client.search(index=index_name, body=knn_query)
        print(f"   Query clip: {query_id[:16]}...")
        for hit in results['hits']['hits']:
            print(f"   Score: {hit['_score']:.4f} | {hit['_source']['clip_id'][:16]}... | {hit['_source']['video_id']}")

    print("\n" + "="*60)


def main():
    parser = argparse.ArgumentParser(description='Upload embeddings to OpenSearch Serverless')
    parser.add_argument('--collection-endpoint', required=True, help='OpenSearch collection endpoint')
    parser.add_argument('--bucket', required=True, help='S3 bucket name')
    parser.add_argument('--prefix', required=True, help='S3 prefix (e.g., outputs/sample/)')
    parser.add_argument('--region', default='ap-northeast-2', help='AWS region')
    parser.add_argument('--index-name', default='video-clips', help='OpenSearch index name')

    args = parser.parse_args()

    print("="*60)
    print("Upload Embeddings to OpenSearch Serverless")
    print("="*60)
    print(f"Collection: {args.collection_endpoint}")
    print(f"S3: s3://{args.bucket}/{args.prefix}")
    print(f"Region: {args.region}")
    print(f"Index: {args.index_name}")
    print("="*60 + "\n")

    # Connect to OpenSearch
    print("Connecting to OpenSearch...")
    client = create_opensearch_client(args.collection_endpoint, args.region)
    print("✓ Connected\n")

    # Test permissions
    print("Testing permissions...")
    try:
        client.cat.indices()
        print("✓ Permission check passed\n")
    except Exception as e:
        print("❌ Permission denied!")
        print(f"\nError: {e}")
        print("\nTo fix this, add your IAM role to the OpenSearch data access policy:")
        print(f"  1. Get your current role: aws sts get-caller-identity")
        print(f"  2. Update policy: aws opensearchserverless update-access-policy \\")
        print(f"       --name cosmos-curator-access --type data --region {args.region} \\")
        print(f"       --policy-version <VERSION> --policy file://policy.json")
        print(f"\nSee README.md for detailed instructions.")
        sys.exit(1)

    # Create index
    print("Creating kNN vector index...")
    create_knn_index(client, args.index_name)
    print()

    # Load embeddings from S3
    print("Loading embeddings from S3...")
    documents = load_embeddings_from_s3(args.bucket, args.prefix)
    if not documents:
        print("❌ No documents to upload")
        sys.exit(1)
    print()

    # Upload to OpenSearch
    print("Uploading documents to OpenSearch...")
    upload_documents(client, documents, args.index_name)
    print()

    # Show examples
    # A newly created AOSS index takes up to ~1 min to become searchable
    for attempt in range(6):
        try:
            example_searches(client, args.index_name)
            break
        except Exception as e:
            print(f"  search not ready yet ({type(e).__name__}), retrying in 15s...")
            time.sleep(15)

    print("\n✅ Upload complete!")
    print(f"\nDashboard URL: https://cosmos-curator.{args.region}.aoss.amazonaws.com/_dashboards")


if __name__ == '__main__':
    main()
