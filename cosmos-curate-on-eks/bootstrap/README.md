# Bootstrap: Model Weights

This directory contains scripts to seed NVIDIA Cosmos Curate model weights into S3.

## Why S3?

Curate downloads models from Hugging Face on first use. For production:
- **Consistency**: Pin specific model versions via sha256 checksums
- **Reliability**: Avoid HF Hub throttling during scale-out
- **No Hugging Face access from the cluster**: nodes read weights from S3 only

## Models

`models.txt` lists the models needed for the CPU pipeline:
- `transnetv2`: Shot boundary detection (video splitting)
- `aesthetic_scorer`: Aesthetic quality filter head
- `clip_vit`: CLIP ViT-L/14 backbone used by the aesthetic scorer (the motion filter uses no model)
- `cosmos_embed1_336p`: Video embeddings

Versions are pinned in Curate's `cosmos_curator/configs/all_models.json`.

## Usage

### 1. Build the Curate CPU image

`scripts/deploy_new_region.sh` does steps 1 and 2 for you. To do them by hand, see `../docker/README.md`.

### 2. Seed models to S3

Run `seed_models.sh` inside the image (it needs the pixi environment and model configs):

```bash
docker run --rm --network host \
  -e AWS_REGION=<REGION> \
  -v "$PWD/bootstrap:/bootstrap:ro" \
  <YOUR_ECR_URI>:<YOUR_TAG> \
  bash /bootstrap/seed_models.sh s3://<YOUR_BUCKET>/models/curate-v2.8.0/
```

**Requirements**:
- The container must have AWS credentials (e.g., via instance role or environment variables)
- S3 bucket must exist and be writable

**Idempotent**: Models are skipped if their checksummed files already exist in S3.

### 3. Use in Curate

Pass the S3 prefix to `run_pipeline`:

```bash
python -m cosmos_curator.pipelines.video.run_pipeline split \
  --model-weights-path s3://<YOUR_BUCKET>/models/curate-v2.8.0/ \
  ...
```

## Adding Models

To add models for captioning or other stages:
1. Append model names to `models.txt`
2. Re-run `seed_models.sh`

Model names come from the keys in `cosmos_curator/configs/all_models.json`.
