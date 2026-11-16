"""Kubernetes Jobs for pipeline stages (in-cluster service account)."""

from __future__ import annotations

import hashlib
import os
from typing import Optional

from kubernetes import client, config
from kubernetes.client.rest import ApiException

NAMESPACE = os.environ.get("PIPELINE_NAMESPACE", "drive-atlas")
JOB_SERVICE_ACCOUNT = os.environ.get("PIPELINE_JOB_SERVICE_ACCOUNT", "pipeline-job")
BATCH_TAINT_KEY = "drive-atlas/batch"

# Per stage: image env var, requests. hdmap fits four per 16-vCPU node.
STAGES = {
    "sumo": {"image_env": "PIPELINE_IMAGE", "cpu": "1", "memory": "2Gi", "memory_limit": "4Gi"},
    "rdshq": {"image_env": "PIPELINE_IMAGE", "cpu": "1", "memory": "2Gi", "memory_limit": "4Gi"},
    "hdmap": {"image_env": "COSMOS_IMAGE", "cpu": "3500m", "memory": "6Gi", "memory_limit": "10Gi"},
}

_batch_api: Optional[client.BatchV1Api] = None


def _api() -> client.BatchV1Api:
    global _batch_api
    if _batch_api is None:
        config.load_incluster_config()
        _batch_api = client.BatchV1Api()
    return _batch_api


def job_name(stage: str, batch: str, variant: Optional[str]) -> str:
    # Names must be DNS-1123 and <= 63 chars; variant ids can be 48 chars, so hash them.
    suffix = "-" + hashlib.sha256(variant.encode()).hexdigest()[:10] if variant else ""
    return f"{stage}-{batch}{suffix}"[:63].rstrip("-")


def submit(stage: str, batch: str, bucket: str, variant: Optional[str] = None) -> str:
    """Create the Job; an existing Job with the same name counts as submitted (idempotent)."""
    spec = STAGES[stage]
    name = job_name(stage, batch, variant)
    args = [stage, "--bucket", bucket, "--batch", batch] + (["--variant", variant] if variant else [])
    job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "namespace": NAMESPACE,
            "labels": {"app": "drive-atlas-pipeline", "stage": stage, "batch": batch},
            "annotations": {"drive-atlas/variant": variant or ""},
        },
        "spec": {
            "backoffLimit": 3,  # Spot interruption: the pod is recreated and the stage skips finished work
            "activeDeadlineSeconds": 3600,
            "ttlSecondsAfterFinished": 86400,
            "template": {
                "metadata": {"labels": {"app": "drive-atlas-pipeline", "stage": stage, "batch": batch}},
                "spec": {
                    "serviceAccountName": JOB_SERVICE_ACCOUNT,
                    "restartPolicy": "Never",
                    "nodeSelector": {"workload": "cpu"},
                    "tolerations": [{"key": BATCH_TAINT_KEY, "operator": "Equal", "value": "true", "effect": "NoSchedule"}],
                    "containers": [{
                        "name": stage,
                        "image": os.environ[spec["image_env"]],
                        "args": args,
                        "env": [{"name": "AWS_DEFAULT_REGION", "value": os.environ.get("AWS_REGION", "ap-northeast-2")}],
                        "resources": {
                            "requests": {"cpu": spec["cpu"], "memory": spec["memory"]},
                            "limits": {"memory": spec["memory_limit"]},
                        },
                    }],
                },
            },
        },
    }
    try:
        _api().create_namespaced_job(NAMESPACE, job)
    except ApiException as exc:
        if exc.status != 409:
            raise
    return name


def job_state(name: str) -> Optional[str]:
    """pending / running / succeeded / failed, or None if the Job does not exist."""
    try:
        status = _api().read_namespaced_job_status(name, NAMESPACE).status
    except ApiException as exc:
        if exc.status == 404:
            return None
        raise
    if status.succeeded:
        return "succeeded"
    if any(c.type == "Failed" and c.status == "True" for c in status.conditions or []):
        return "failed"
    return "running" if status.active else "pending"
