#!/usr/bin/env bash
# Deploy the AWS infrastructure for Cosmos Curate CPU into a region (idempotent).
# Usage: [OPTION=a|b|c] [PUBLIC_ACCESS_CIDRS=<cidr,...>] ./deploy_new_region.sh <region> [<vpc-id>]
# e.g.  ./deploy_new_region.sh us-west-2

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [ $# -lt 1 ]; then
  echo "Usage: OPTION=a|b|c $0 <region> [<vpc-id>]"
  echo "e.g.  $0 us-west-2"
  exit 1
fi

REGION="${1}"
# One kubeconfig per region: scripts for two regions can run at once without
# switching each other's current context.
export KUBECONFIG="${HOME}/.kube/cosmos-curate-${REGION}"
VPC_ID="${2:-}"
VERSION="v2.8.0"

echo "========================================"
echo "Cosmos Curate on EKS: deploy region"
echo "========================================"
echo "Region: ${REGION}"
echo "VPC ID: ${VPC_ID:-<new VPC>}"
echo "Version: ${VERSION}"
echo ""

error_exit() {
  echo "❌ $1"
  exit 1
}

check_stack_status() {
  local stack_name="$1"
  local status
  status=$(aws cloudformation describe-stacks \
    --stack-name "${stack_name}" \
    --region "${REGION}" \
    --query 'Stacks[0].StackStatus' \
    --output text 2>/dev/null || echo "NOT_FOUND")
  echo "${status}"
}

# A stack left in a *_FAILED state (e.g. an interrupted teardown) never completes;
# waiting on it would hang, so stop and point at teardown.sh.
fail_if_broken() {
  case "$2" in
    *_FAILED|ROLLBACK_COMPLETE|UPDATE_ROLLBACK_COMPLETE)
      [ "$2" = "ROLLBACK_COMPLETE" ] && return 0
      error_exit "Stack $1 is in state $2. Run scripts/teardown.sh ${REGION} first, then retry." ;;
  esac
}

wait_for_stack() {
  local stack_name="$1"
  echo "  Waiting for stack ${stack_name}..."
  aws cloudformation wait stack-create-complete \
    --stack-name "${stack_name}" \
    --region "${REGION}" || \
  aws cloudformation wait stack-update-complete \
    --stack-name "${stack_name}" \
    --region "${REGION}" || \
    error_exit "Stack deployment failed: ${stack_name}"
  echo "  ✓ Stack ready: ${stack_name}"
}

# ---- Node capacity -------------------------------------------------------
# 4 x 16-vCPU nodes, one curator pod each (Ray head = pod-0) plus caption-proxy.
#   OPTION=a  4 on-demand            (no interruptions)
#   OPTION=b  3 Spot + 1 on-demand   (default; the head runs on the on-demand node)
#   OPTION=c  4 Spot                 (lowest cost; a reclaimed head stops the job)
NODE_COUNT=4
VCPU_PER_NODE=16
CLUSTER_NAME="cosmos-curate-eks"
OPTION="${OPTION:-b}"
case "${OPTION}" in a|b|c) ;; *) error_exit "OPTION must be a, b or c (got '${OPTION}')" ;; esac

nodegroup_desired() {
  aws eks describe-nodegroup --cluster-name "${CLUSTER_NAME}" --nodegroup-name "$1" --region "${REGION}" \
    --query 'nodegroup.scalingConfig.desiredSize' --output text 2>/dev/null || echo 0
}

# An existing cluster keeps the option it runs now (e.g. after an earlier Spot fallback),
# otherwise the next cdk deploy would silently resize it.
if aws eks describe-cluster --name "${CLUSTER_NAME}" --region "${REGION}" >/dev/null 2>&1; then
  spot_now=$(nodegroup_desired cpu-spot); od_now=$(nodegroup_desired cpu-od)
  if [ "${spot_now}" = "0" ] && [ "${od_now}" -ge "${NODE_COUNT}" ]; then OPTION=a
  elif [ "${od_now}" = "0" ] && [ "${spot_now}" -ge "${NODE_COUNT}" ]; then OPTION=c
  elif [ "${spot_now}" -gt 0 ] && [ "${od_now}" -gt 0 ]; then OPTION=b
  fi
fi

spot_nodes() { case "$1" in a) echo 0 ;; b) echo $((NODE_COUNT - 1)) ;; c) echo "${NODE_COUNT}" ;; esac; }
od_nodes()   { echo $((NODE_COUNT - $(spot_nodes "$1"))); }

# Quota preflight: best effort, needs servicequotas:GetServiceQuota. Without it the
# launch errors from the node groups decide (see ensure_nodes).
quota() {
  aws service-quotas get-service-quota --service-code ec2 --quota-code "$1" --region "${REGION}" \
    --query 'Quota.Value' --output text 2>/dev/null | cut -d. -f1
}
SPOT_QUOTA=$(quota L-34B43A08 || true)  # All Standard Spot Instance Requests (vCPU)
OD_QUOTA=$(quota L-1216C47A || true)    # Running On-Demand Standard instances (vCPU)
if [ -n "${SPOT_QUOTA}" ] && [ -n "${OD_QUOTA}" ]; then
  echo "vCPU quota: Spot ${SPOT_QUOTA}, on-demand ${OD_QUOTA} (current usage not subtracted)"
  if [ "${OPTION}" != "a" ] && { [ "${SPOT_QUOTA}" -lt $(( $(spot_nodes "${OPTION}") * VCPU_PER_NODE )) ] || [ "${OD_QUOTA}" -lt $(( $(od_nodes "${OPTION}") * VCPU_PER_NODE )) ]; }; then
    echo "  Not enough quota for option ${OPTION} -> option a (all on-demand)"
    OPTION=a
  fi
  if [ "${OPTION}" = "a" ] && [ "${OD_QUOTA}" -lt $(( NODE_COUNT * VCPU_PER_NODE )) ]; then
    error_exit "On-demand vCPU quota ${OD_QUOTA} < $(( NODE_COUNT * VCPU_PER_NODE )). Request an increase of 'Running On-Demand Standard instances' in Service Quotas."
  fi
else
  echo "vCPU quota not readable (no servicequotas permission); node launch results decide"
fi

# Every cdk deploy below must get the same context: deploying one stack also updates
# its dependencies, and a missing -c kubectlRole there drops the aws-auth mapping.
build_cdk_ctx() {
  CDK_CTX=()
  if [ -n "${VPC_ID}" ]; then
    CDK_CTX+=(-c "vpcId=${VPC_ID}")
  fi
  CDK_CTX+=(-c "spotDesiredSize=$(spot_nodes "${OPTION}")" -c "onDemandDesiredSize=$(od_nodes "${OPTION}")")
  # Option b reserves the on-demand node for the Ray head (taint dedicated=curator)
  if [ "${OPTION}" = "b" ]; then CDK_CTX+=(-c dedicatedOnDemand=true); else CDK_CTX+=(-c dedicatedOnDemand=false); fi
  # Optional: limit the public EKS API endpoint; the host running kubectl must be inside one of the CIDRs
  if [ -n "${PUBLIC_ACCESS_CIDRS:-}" ]; then CDK_CTX+=(-c "publicAccessCidrs=${PUBLIC_ACCESS_CIDRS}"); fi
  local caller
  caller=$(aws sts get-caller-identity --query Arn --output text)
  if [[ "${caller}" == *":assumed-role/"* ]]; then
    # Map the deploying IAM role into aws-auth, otherwise kubectl gets Unauthorized
    CDK_CTX+=(-c "kubectlRole=$(echo "${caller}" | cut -d/ -f2)")
  fi
}
build_cdk_ctx
echo "Node option: ${OPTION} (Spot $(spot_nodes "${OPTION}"), on-demand $(od_nodes "${OPTION}"))"

# Launch errors of node groups that should have nodes, e.g. "cpu-spot:InsufficientInstanceCapacity"
capacity_errors() {
  local ng asg
  for ng in cpu-spot cpu-od; do
    [ "$(nodegroup_desired "${ng}")" -gt 0 ] || continue
    asg=$(aws eks describe-nodegroup --cluster-name "${CLUSTER_NAME}" --nodegroup-name "${ng}" --region "${REGION}" \
      --query 'nodegroup.resources.autoScalingGroups[0].name' --output text 2>/dev/null) || continue
    aws autoscaling describe-scaling-activities --auto-scaling-group-name "${asg}" --region "${REGION}" --max-items 10 \
      --query "Activities[?StatusCode=='Failed'].StatusMessage" --output text 2>/dev/null \
      | grep -oE 'InsufficientInstanceCapacity|MaxSpotInstanceCountExceeded|VcpuLimitExceeded|UnfulfillableCapacity|SpotMaxPriceTooLow' \
      | sort -u | sed "s/^/${ng}:/" || true
  done
  return 0
}

# Wait until NODE_COUNT curator nodes are Ready; stop early on a hard launch error.
ensure_nodes() {
  local deadline=$((SECONDS + 900)) ready errs
  while :; do
    ready=$(kubectl get nodes -l workload=curator-cpu --no-headers 2>/dev/null | grep -c " Ready " || true)
    if [ "${ready}" -ge "${NODE_COUNT}" ]; then
      echo "  ✓ Nodes ${ready}/${NODE_COUNT} Ready (option ${OPTION})"
      return 0
    fi
    errs=$(capacity_errors | tr '\n' ' ' || true)
    if [ -n "${errs}" ] || [ "${SECONDS}" -ge "${deadline}" ]; then
      NODE_ERRORS="${errs:-nodes not Ready after 15 min}"
      echo "  Nodes ${ready}/${NODE_COUNT}: ${NODE_ERRORS}"
      return 1
    fi
    echo "  Nodes ${ready}/${NODE_COUNT} Ready, waiting..."
    sleep 30
  done
}
# ---------------------------------------------------------------------------

# 1. CDK Bootstrap
echo "[1/7] CDK bootstrap"
CDK_TOOLKIT_STACK="CDKToolkit"
toolkit_status=$(check_stack_status "${CDK_TOOLKIT_STACK}")
if [ "${toolkit_status}" = "NOT_FOUND" ]; then
  echo "  Running cdk bootstrap..."
  cd "${PROJECT_ROOT}/cdk"
  npm install
  npx cdk bootstrap "aws://$(aws sts get-caller-identity --query Account --output text)/${REGION}"
  echo "  ✓ CDK bootstrap done"
else
  echo "  ✓ CDK bootstrap already done (${toolkit_status})"
fi
echo ""

# 2. CoreStack (VPC, S3, ECR)
echo "[2/7] CoreStack (VPC, S3, ECR)"
cd "${PROJECT_ROOT}/cdk"
npm install
CORE_STACK="CosmosCurateCore"
core_status=$(check_stack_status "${CORE_STACK}")
fail_if_broken "${CORE_STACK}" "${core_status}"
if [ "${core_status}" = "NOT_FOUND" ] || [ "${core_status}" = "ROLLBACK_COMPLETE" ]; then
  echo "  Deploying CoreStack..."
  npx cdk deploy CosmosCurateCore --region "${REGION}" "${CDK_CTX[@]}" --require-approval never
  wait_for_stack "${CORE_STACK}"
elif [ "${core_status}" = "CREATE_COMPLETE" ] || [ "${core_status}" = "UPDATE_COMPLETE" ]; then
  echo "  ✓ CoreStack already deployed (${core_status})"
else
  # In progress
  echo "  CoreStack is ${core_status}, waiting..."
  wait_for_stack "${CORE_STACK}"
fi
echo ""

# 3. Images
echo "[3/7] Build and push images"
"${SCRIPT_DIR}/build_and_push_images.sh" "${REGION}" "${VERSION}"
echo ""

# 4. Model weights: Hugging Face -> S3 (once per region)
echo "[4/7] Model weights -> S3"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
BUCKET_NAME="cosmos-curate-${ACCOUNT_ID}-${REGION}"
MODEL_PREFIX="s3://${BUCKET_NAME}/models/curate-v2.8.0/"
ECR_URI="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/cosmos-curate/curator-cpu:latest"

if aws s3 ls "${MODEL_PREFIX}" --region "${REGION}" >/dev/null 2>&1; then
  echo "  ✓ Model weights already in S3"
else
  echo "  Downloading from Hugging Face and uploading to S3..."
  docker run --rm \
    -e AWS_REGION="${REGION}" \
    -v "${PROJECT_ROOT}/bootstrap:/bootstrap:ro" \
    "${ECR_URI}" \
    bash /bootstrap/seed_models.sh "${MODEL_PREFIX}"
  echo "  ✓ Model weights uploaded"
fi
echo ""

# 5. EKS stack
echo "[5/7] EKS stack"
EKS_STACK="CosmosCurateEks"
eks_status=$(check_stack_status "${EKS_STACK}")
fail_if_broken "${EKS_STACK}" "${eks_status}"
if [ "${eks_status}" = "NOT_FOUND" ] || [ "${eks_status}" = "ROLLBACK_COMPLETE" ]; then
  echo "  Deploying EKS stack..."
  npx cdk deploy CosmosCurateEks --region "${REGION}" "${CDK_CTX[@]}" --require-approval never
  wait_for_stack "${EKS_STACK}"
elif [ "${eks_status}" = "CREATE_COMPLETE" ] || [ "${eks_status}" = "UPDATE_COMPLETE" ]; then
  echo "  ✓ EKS stack already deployed (${eks_status})"
else
  # In progress
  echo "  EKS stack is ${eks_status}, waiting..."
  wait_for_stack "${EKS_STACK}"
fi
echo ""

# 6. kubectl and nodes
echo "[6/7] kubectl and nodes"
echo "  cluster: ${CLUSTER_NAME}"

aws eks update-kubeconfig --name "${CLUSTER_NAME}" --region "${REGION}" || \
  error_exit "update-kubeconfig failed; check EKS access for your IAM role."

if kubectl get nodes >/dev/null 2>&1; then
  echo "  ✓ kubectl connected"
  kubectl get nodes
else
  error_exit "kubectl cannot reach the cluster; check IAM access (aws-auth) and network."
fi

echo "  Waiting for nodes..."
if ! ensure_nodes; then
  if [ "${OPTION}" != "a" ]; then
    echo "  Spot nodes failed (${NODE_ERRORS}) -> option a (${NODE_COUNT} on-demand)"
    OPTION=a
    build_cdk_ctx
    (cd "${PROJECT_ROOT}/cdk" && npx cdk deploy CosmosCurateEks --region "${REGION}" "${CDK_CTX[@]}" --require-approval never)
    ensure_nodes || error_exit "On-demand nodes failed too (${NODE_ERRORS}). For VcpuLimitExceeded, request 'Running On-Demand Standard instances' >= $(( NODE_COUNT * VCPU_PER_NODE )) vCPU in Service Quotas."
  else
    error_exit "On-demand nodes failed (${NODE_ERRORS}). For VcpuLimitExceeded, request 'Running On-Demand Standard instances' >= $(( NODE_COUNT * VCPU_PER_NODE )) vCPU in Service Quotas."
  fi
fi
echo ""

# 7. OpenSearch Serverless stack
echo "[7/7] OpenSearch Serverless stack"
OPENSEARCH_STACK="CosmosCurateOpenSearch"
opensearch_status=$(check_stack_status "${OPENSEARCH_STACK}")
fail_if_broken "${OPENSEARCH_STACK}" "${opensearch_status}"
if [ "${opensearch_status}" = "NOT_FOUND" ] || [ "${opensearch_status}" = "ROLLBACK_COMPLETE" ]; then
  echo "  Deploying OpenSearch stack..."
  npx cdk deploy CosmosCurateOpenSearch --region "${REGION}" "${CDK_CTX[@]}" --require-approval never
  wait_for_stack "${OPENSEARCH_STACK}"
elif [ "${opensearch_status}" = "CREATE_COMPLETE" ] || [ "${opensearch_status}" = "UPDATE_COMPLETE" ]; then
  echo "  ✓ OpenSearch stack already deployed (${opensearch_status})"
else
  # In progress
  echo "  OpenSearch stack is ${opensearch_status}, waiting..."
  wait_for_stack "${OPENSEARCH_STACK}"
fi
echo ""

echo "✓ Infrastructure ready in ${REGION} (option ${OPTION})"
echo "Next: scripts/setup_curator_pod.sh ${REGION}   (or run everything with scripts/e2e.sh ${REGION})"
