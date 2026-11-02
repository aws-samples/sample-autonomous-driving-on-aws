# Docker Images

This directory contains Dockerfiles for running NVIDIA Cosmos Curate on CPU-only infrastructure.

## Images

### 1. Curator CPU (`Dockerfile.cpu`)

The main image for running Curate pipelines on CPU nodes.

**Base**: Ubuntu 24.04  
**Key components**:
- Pixi (conda environment manager)
- NVIDIA Cosmos Curate v2.8.0 (cloned at build time)
- CPU patches for TransNetV2 and Cosmos-Embed1 models
- Ray for distributed execution

**Build**:
```bash
docker build -f docker/Dockerfile.cpu -t curator-cpu:latest docker/
```

**Size**: about 30 GB locally, 14.5 GB compressed in ECR (pixi environments and dependencies)

### 2. Caption Proxy (`caption-proxy/`)

OpenAI-compatible API server for video captioning, backed by Amazon Bedrock (Claude Opus 5.5).

**Why**: Curate's `--captioning-algorithm openai` expects an OpenAI endpoint. This proxy:
- Extracts frames from base64-encoded video
- Sends frames to Bedrock Claude via Strands Agents
- Returns captions in OpenAI format

**Build**:
```bash
docker build -t caption-proxy:latest docker/caption-proxy/
```

**Size**: about 0.7 GB

## CPU Patches

`patch_cpu.py` modifies three Curate files to run without GPUs:

1. **TransNetV2** (`models/transnetv2.py`): Remove hardcoded `.cuda()` calls
2. **Cosmos-Embed1** (`models/cosmos_embed1.py`): Use CPU when no GPU is available
3. **Cosmos-Embed1 stage** (`pipelines/video/embedding/cosmos_embed1_stages.py`): Request CPUs instead of GPUs when `--embedding-gpus-per-worker 0`

These patches are applied during image build and verified by replacement count checks.

## Environment Variables

Key variables set in `Dockerfile.cpu`:

- `COSMOS_CURATOR_CODE_DIR=/opt/cosmos-curator` (hardcoded by upstream chart)
- `PYTHONPATH`, `COSMOS_S3_PROFILE_PATH` (for boto3 credential chain)
- `CONDA_OVERRIDE_CUDA=13.0.3` (required for pixi `default` env on CPU hosts)
- Ray limits: `RAY_MAX_LIMIT_*=40000` (increased for large jobs)

## Usage Example

### Run a split pipeline locally

```bash
docker run --rm \
  -e AWS_REGION=us-east-1 \
  -v /path/to/videos:/input \
  -v /path/to/output:/output \
  curator-cpu:latest \
  pixi run --as-is -e dev python -m cosmos_curator.pipelines.video.run_pipeline split \
    --input-video-path /input \
    --output-clip-path /output \
    --model-weights-path s3://YOUR_BUCKET/models/curate-v2.8.0/ \
    --splitting-algorithm transnetv2 \
    --transnetv2-frame-decoder-mode ffmpeg_cpu \
    --transnetv2-gpus-per-worker 0 \
    --motion-filter enable \
    --motion-score-gpus-per-worker 0 \
    --aesthetic-gpus-per-worker 0
```

### Test caption proxy

```bash
docker run -d -p 8080:8080 \
  -e AWS_REGION=us-east-1 \
  -e BEDROCK_MODEL_ID=global.anthropic.claude-opus-5-5 \
  caption-proxy:latest

curl http://localhost:8080/v1/models
```

## Pushing to ECR

`scripts/build_and_push_images.sh <region>` builds both images and pushes them as
`<account>.dkr.ecr.<region>.amazonaws.com/cosmos-curate/curator-cpu:{v2.8.0,latest}` and
`.../cosmos-curate/caption-proxy:{v2.8.0,latest}`, the repositories created by the CDK `CosmosCurateCore` stack.
