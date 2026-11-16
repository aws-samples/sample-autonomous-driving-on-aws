# Cosmos-Drive-Dreams hdmap condition video on CPU (Mesa software EGL). Build context: repository root.
#   docker build -f docker/cosmos-hdmap/Dockerfile -t drive-atlas/cosmos-hdmap:<tag> .
# The toolkit (Apache-2.0) is fetched at build time at a pinned commit and not redistributed here.
FROM public.ecr.aws/docker/library/python:3.12-slim-bookworm

ARG TOOLKIT_REPO=https://github.com/nv-tlabs/Cosmos-Drive-Dreams
ARG TOOLKIT_COMMIT=915f79f

RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates ffmpeg libegl1 libgl1 libegl-mesa0 libgl1-mesa-dri libgles2 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 USE_RAY=False \
    COSMOS_TOOLKIT_DIR=/opt/cosmos-drive-dreams/cosmos-drive-dreams-toolkits

COPY docker/cosmos-hdmap/requirements.lock.txt /tmp/requirements.lock.txt
# torch's dependencies are pinned in the lock file; resolving them from the CPU wheel index fails.
# decord ships only a manylinux2010 wheel, which current pip check reports as unsupported although it imports fine.
RUN pip install --no-cache-dir --no-deps torch==2.4.0+cpu --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r /tmp/requirements.lock.txt \
    && { pip check | grep -vx "decord 0.6.0 is not supported on this platform" || true; } > /tmp/pip-check.txt \
    && { [ ! -s /tmp/pip-check.txt ] || { cat /tmp/pip-check.txt; exit 1; }; } \
    && python3 -c "import decord, torch, cv2, webdataset"

RUN git clone --filter=blob:none "$TOOLKIT_REPO" /opt/cosmos-drive-dreams \
    && git -C /opt/cosmos-drive-dreams checkout "$TOOLKIT_COMMIT" \
    && rm -rf /opt/cosmos-drive-dreams/.git

WORKDIR /app
COPY scripts/pipeline_job.py /app/scripts/

RUN useradd --uid 10001 --create-home job
USER 10001
# Runs as a Kubernetes Job to completion; there is no long-running process to probe.
HEALTHCHECK NONE
ENTRYPOINT ["python3", "/app/scripts/pipeline_job.py"]
