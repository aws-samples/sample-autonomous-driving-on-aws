#!/usr/bin/env bash
# End-to-end from an empty region: infra -> Ray -> split/embed/caption -> OpenSearch -> search.
# Usage: [OPTION=a|b|c] scripts/e2e.sh <region> [local dir with .mp4 files]
#   OPTION a = 4 on-demand nodes, b = 3 Spot + 1 on-demand for the Ray head (default), c = 4 Spot
# Without a dir, the 30 s sample video bundled in the Curate image is used.
# Safe to re-run: every step skips work that is already done.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
REGION="${1:?usage: OPTION=a|b|c $0 <region> [local video dir]}"
# One kubeconfig per region: scripts for two regions can run at once without
# switching each other's current context.
export KUBECONFIG="${HOME}/.kube/cosmos-curate-${REGION}"
INPUT_DIR="${2:-}"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
BUCKET="cosmos-curate-${ACCOUNT_ID}-${REGION}"
# Own input prefix per source, so runs with different videos never mix
if [ -n "${INPUT_DIR}" ]; then DATASET="$(basename "$(realpath "${INPUT_DIR}")")"; else DATASET="e2e-sample"; fi
NS="cosmos-curator"
SAMPLE="/opt/cosmos-curator/tests/cosmos_curator/pipelines/video/data/test_video_30s.mp4"

step() { echo; echo "======== [e2e $1] $2 ========"; }

step 1/6 "Infrastructure (CDK, images, model weights)"
"${SCRIPT_DIR}/deploy_new_region.sh" "${REGION}"

step 2/6 "Caption proxy + Curator Ray cluster"
"${SCRIPT_DIR}/setup_curator_pod.sh" "${REGION}" 4

step 3/6 "Upload input videos"
if [ -n "${INPUT_DIR}" ]; then
  aws s3 sync "${INPUT_DIR}" "s3://${BUCKET}/inputs/${DATASET}/" --exclude "*" --include "*.mp4" --region "${REGION}"
else
  kubectl -n "${NS}" exec cosmos-curator-0 -- aws s3 cp "${SAMPLE}" "s3://${BUCKET}/inputs/${DATASET}/" --region "${REGION}"
fi

step 4/6 "Split + Embedding + Caption (Ray job)"
RUN_LOG=$(mktemp)
"${PROJECT_ROOT}/k8s/run_split.sh" "${REGION}" "${DATASET}" 2>&1 | tee "${RUN_LOG}"
OUT=$(sed -n 's/^output: //p' "${RUN_LOG}")
PREFIX="${OUT#s3://${BUCKET}/}"
CLIPS=$(aws s3 ls --recursive "${OUT}clips/" --region "${REGION}" | grep -c '\.mp4$' || true)
[ "${CLIPS}" -gt 0 ] || { echo "❌ No clips were produced: ${OUT}"; exit 1; }
echo "✓ ${CLIPS} clips: ${OUT}"

step 5/6 "Index embeddings in OpenSearch"
ENDPOINT=$(aws cloudformation describe-stacks --stack-name CosmosCurateOpenSearch --region "${REGION}" \
  --query "Stacks[0].Outputs[?OutputKey=='CollectionEndpoint'].OutputValue" --output text)
kubectl cp "${SCRIPT_DIR}/upload_embeddings_to_opensearch.py" "${NS}/cosmos-curator-0:/tmp/upload_embeddings_to_opensearch.py"
kubectl -n "${NS}" exec cosmos-curator-0 -- bash -c "pip3 install --user --quiet opensearch-py && \
  python3 /tmp/upload_embeddings_to_opensearch.py --collection-endpoint ${ENDPOINT} \
    --bucket ${BUCKET} --prefix ${PREFIX} --region ${REGION} --index-name video-clips"

step 6/6 "Search check (text + kNN)"
# Re-copy and re-install: pod-0 may have been replaced (Spot) since step 5, and its disk is ephemeral
kubectl cp "${SCRIPT_DIR}/verify_opensearch.py" "${NS}/cosmos-curator-0:/tmp/verify_opensearch.py"
kubectl -n "${NS}" exec cosmos-curator-0 -- bash -c "pip3 install --user --quiet opensearch-py && \
  python3 -u /tmp/verify_opensearch.py --collection-endpoint ${ENDPOINT} --region ${REGION} \
    --index-name video-clips --min-docs ${CLIPS} --location-prefix ${OUT}"

echo
echo "✅ e2e passed (${REGION}): ${OUT}"
echo "kubectl: export KUBECONFIG=${KUBECONFIG}"
