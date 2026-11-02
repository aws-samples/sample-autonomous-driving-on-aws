#!/bin/bash
# Run split + Cosmos-Embed1 embedding + Bedrock caption as a Ray job on the Curator head pod.
# Usage: k8s/run_split.sh <region> <dataset under s3://$BUCKET/inputs/>
# NVCF_MULTI_NODE=false is required: the pod runs onto_nvcf.py --helm, which sets it to true
# and would make Curate fetch weights from NVCF (NGC key) instead of the S3 prefix.
# A job that fails because a Ray node died (Spot reclaim) is resubmitted after the Ray cluster
# is repaired, up to MAX_ATTEMPTS times, each into a fresh output prefix.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REGION=${1:?region}
# One kubeconfig per region: scripts for two regions can run at once without
# switching each other's current context.
export KUBECONFIG="${HOME}/.kube/cosmos-curate-${REGION}"
DATASET=${2:?dataset}
MAX_ATTEMPTS=${MAX_ATTEMPTS:-3}
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
B="s3://cosmos-curate-${ACCOUNT_ID}-${REGION}"
NS=cosmos-curator
aws eks update-kubeconfig --name cosmos-curate-eks --region "${REGION}" >/dev/null

head_exec() { kubectl -n "${NS}" exec cosmos-curator-0 -- "$@"; }

# PENDING/RUNNING/SUCCEEDED/FAILED/STOPPED; UNKNOWN when the head no longer knows the job
# (head pod restarted); UNREACHABLE when the head cannot be asked right now
job_status() {
  local s
  s=$(head_exec pixi run --as-is python -c "
import sys
from ray.job_submission import JobSubmissionClient
try:
    print(JobSubmissionClient('http://127.0.0.1:8265').get_job_status(sys.argv[1]))
except Exception as e:
    print('UNKNOWN' if 'does not exist' in str(e) else 'UNREACHABLE')
" "$1" 2>/dev/null | tail -1) || true
  echo "${s:-UNREACHABLE}"
}

for attempt in $(seq 1 "${MAX_ATTEMPTS}"); do
  RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)
  JOB="curate-${RUN_ID}"
  OUT="${B}/outputs/${DATASET}/${RUN_ID}/"
  echo "attempt ${attempt}/${MAX_ATTEMPTS}: job ${JOB}"
  head_exec pixi run --as-is ray job submit --no-wait --submission-id "${JOB}" \
    --runtime-env-json '{"env_vars":{"NVCF_MULTI_NODE":"false"}}' \
    -- pixi run --as-is -e dev python -m cosmos_curator.pipelines.video.run_pipeline split \
    --input-video-path "${B}/inputs/${DATASET}/" \
    --output-clip-path "${OUT}" \
    --model-weights-path "${B}/models/curate-v2.8.0/" \
    --splitting-algorithm transnetv2 \
    --transnetv2-frame-decoder-mode ffmpeg_cpu \
    --transnetv2-gpus-per-worker 0 \
    --embedding-algorithm cosmos-embed1-336p \
    --embedding-gpus-per-worker 0 \
    --captioning-algorithm openai \
    --openai-model-name claude-opus-5-5 >/dev/null

  # Stream logs; the stream can drop (kubectl exec websocket) while the job keeps running,
  # so the job status decides, not the stream's exit code
  head_exec pixi run --as-is ray job logs -f "${JOB}" || true
  STATUS=$(job_status "${JOB}")
  unreachable=0
  while [ "${STATUS}" = "PENDING" ] || [ "${STATUS}" = "RUNNING" ] || [ "${STATUS}" = "UNREACHABLE" ]; do
    if [ "${STATUS}" = "UNREACHABLE" ]; then
      unreachable=$((unreachable + 1))
      # 10 min without an answer: the head pod is gone, the job with it
      [ "${unreachable}" -lt 20 ] || { STATUS=UNKNOWN; break; }
    else
      unreachable=0
    fi
    sleep 30
    STATUS=$(job_status "${JOB}")
  done
  echo "job ${JOB}: ${STATUS}"

  if [ "${STATUS}" = "SUCCEEDED" ]; then
    echo "output: ${OUT}"
    exit 0
  fi
  RETRY=false
  if [ "${STATUS}" = "UNKNOWN" ]; then
    RETRY=true
  elif [ "${STATUS}" = "FAILED" ]; then
    # Read the logs first: `| grep -q` would exit early, kill kubectl with SIGPIPE, and
    # pipefail would turn a match into a failure
    LOGS=$(head_exec pixi run --as-is ray job logs "${JOB}" 2>/dev/null || true)
    if grep -qE "node has died|missing too many heartbeats|NodeDiedError" <<< "${LOGS}"; then
      RETRY=true
    fi
  fi
  if [ "${RETRY}" != true ]; then
    echo "❌ job ${JOB} ${STATUS} (not a lost node, not retried)"
    exit 1
  fi
  [ "${attempt}" -lt "${MAX_ATTEMPTS}" ] || break
  echo "  a Ray node was lost (Spot reclaim?): repairing the Ray cluster, then resubmitting"
  REPLICAS=$(kubectl -n "${NS}" get sts cosmos-curator -o jsonpath='{.spec.replicas}')
  "${SCRIPT_DIR}/../scripts/setup_curator_pod.sh" "${REGION}" "${REPLICAS}"
done
echo "❌ job failed after ${MAX_ATTEMPTS} attempts"
exit 1
