# Pipeline Job image: SUMO judge + RDS-HQ conversion. Build context: repository root.
#   docker build -f docker/pipeline/Dockerfile -t drive-atlas/pipeline:<tag> .
# SUMO from its official PyPI wheels (eclipse-sumo, EPL-2.0) on the public ECR Python image: no Docker Hub or GHCR pulls.
FROM public.ecr.aws/docker/library/python:3.13-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Shared libraries the SUMO binaries link against (X11/GL are linked even though no GUI is used).
RUN apt-get update && apt-get install -y --no-install-recommends \
        libx11-6 libxext6 libxrender1 libgl1 libatomic1 libexpat1 libfontconfig1 libxft2 libxcursor1 libxfixes3 libxinerama1 libgomp1 \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir eclipse-sumo==1.28.0 traci==1.28.0 sumolib==1.28.0 numpy==2.4.4 webdataset==1.0.2 boto3==1.44.0 defusedxml==0.7.1

WORKDIR /app
COPY augment /app/augment
COPY sotif /app/sotif
COPY scripts/augment_run.py scripts/compile_sumo.py scripts/sumo_to_rdshq.py scripts/pipeline_job.py /app/scripts/

RUN useradd --uid 10001 --create-home job
USER 10001
# Runs as a Kubernetes Job to completion; there is no long-running process to probe.
HEALTHCHECK NONE
ENTRYPOINT ["python3", "/app/scripts/pipeline_job.py"]
