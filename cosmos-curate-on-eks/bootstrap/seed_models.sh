#!/bin/bash
# Bootstrap (before IaC, and after each Curate version bump): mirror pinned model
# weights from Hugging Face into private S3, in the layout Curate reads with
# --model-weights-path <prefix>  (<prefix><HF model id>/<files>).
# Runs inside the Curate CPU image; S3 access via the default AWS credential chain.
# Idempotent: a model is skipped when all its checksummed files already exist in S3;
# downloaded files are verified against the sha256 in all_models.json before upload.
#
# Usage (on a host with the image and an instance role):
#   docker run --rm -e AWS_REGION -v "$PWD/bootstrap:/bootstrap:ro" <image> \
#     bash /bootstrap/seed_models.sh s3://<bucket>/models/curate-v2.8.0/
set -euo pipefail
PREFIX=${1:?usage: seed_models.sh <s3-prefix> [models-file]}
LIST=${2:-$(dirname "$0")/models.txt}
[[ $PREFIX == */ ]] || PREFIX=$PREFIX/
cd "$COSMOS_CURATOR_CODE_DIR"
py() { pixi run --as-is -e model-download python "$@"; }

for m in $(grep -v '^\s*#' "$LIST"); do
  read -r model_id sha_files < <(py -c '
import json, sys
d = json.load(open("cosmos_curator/configs/all_models.json"))[sys.argv[1]]
print(d["model_id"], ",".join(sorted(d.get("sha256") or {})))' "$m")
  dest="${PREFIX}${model_id}/"
  missing=0
  for f in ${sha_files//,/ }; do aws s3 ls "${dest}${f}" >/dev/null 2>&1 || missing=1; done
  if [[ -n $sha_files && $missing == 0 ]]; then echo "skip $m ($dest)"; continue; fi

  py -m cosmos_curator.core.managers.model_cli download --models "$m"
  py -c '
import hashlib, json, pathlib, sys
d = json.load(open("cosmos_curator/configs/all_models.json"))[sys.argv[1]]
root = pathlib.Path("/config/models") / d["model_id"]
for name, want in (d.get("sha256") or {}).items():
    got = hashlib.sha256((root / name).read_bytes()).hexdigest()
    sys.exit(f"sha256 mismatch {root / name}: {got} != {want}") if got != want else print("sha256 ok", name)' "$m"
  aws s3 sync --only-show-errors --exclude ".cache/*" "/config/models/${model_id}/" "$dest"
  echo "uploaded $m -> $dest"
done
