#!/usr/bin/env bash
# Delete everything e2e.sh / deploy_new_region.sh created in a region, including the
# RETAIN resources (S3 bucket, ECR repos) that would block a fresh redeploy.
# Usage: scripts/teardown.sh <region> [--yes]
set -euo pipefail

REGION="${1:?usage: $0 <region> [--yes]}"
# One kubeconfig per region: scripts for two regions can run at once without
# switching each other's current context.
export KUBECONFIG="${HOME}/.kube/cosmos-curate-${REGION}"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
BUCKET="cosmos-curate-${ACCOUNT_ID}-${REGION}"
CLUSTER="cosmos-curate-eks"

if [ "${2:-}" != "--yes" ]; then
  read -r -p "Delete all Cosmos Curate resources in ${REGION}, including the data in s3://${BUCKET}? [y/N] " a
  [ "${a}" = "y" ] || exit 1
fi

stack_status() {
  aws cloudformation describe-stacks --stack-name "$1" --region "${REGION}" \
    --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo NOT_FOUND
}

delete_cluster_directly() {
  aws eks describe-cluster --name "${CLUSTER}" --region "${REGION}" >/dev/null 2>&1 || return 0
  echo "    Deleting the leftover EKS cluster directly"
  for ng in $(aws eks list-nodegroups --cluster-name "${CLUSTER}" --region "${REGION}" --query nodegroups --output text); do
    aws eks delete-nodegroup --cluster-name "${CLUSTER}" --nodegroup-name "${ng}" --region "${REGION}" >/dev/null || true
    aws eks wait nodegroup-deleted --cluster-name "${CLUSTER}" --nodegroup-name "${ng}" --region "${REGION}" || true
  done
  until aws eks delete-cluster --name "${CLUSTER}" --region "${REGION}" >/dev/null 2>&1; do
    aws eks describe-cluster --name "${CLUSTER}" --region "${REGION}" >/dev/null 2>&1 || return 0
    echo "    Cluster update in progress, retrying in 30 s"; sleep 30
  done
  aws eks wait cluster-deleted --name "${CLUSTER}" --region "${REGION}"
}

# Retries by failure type instead of blindly retaining:
# - the cluster custom resource fails with "update in progress" while an addon or pod
#   identity association is still being removed -> wait and retry, never retain it
#   (a retained cluster keeps its security group and blocks the VPC deletion)
# - other custom resources (Kubernetes manifests once nodes are gone, the VPC default
#   security group restriction that can time out) -> retain them, they vanish with
#   their cluster or VPC
# - anything blocked by a leftover cluster -> delete the cluster directly, then retry
delete_stack() {
  local name="$1" attempt failed retain id type
  [ "$(stack_status "${name}")" = "NOT_FOUND" ] && { echo "  - ${name}: not found"; return; }
  echo "  - ${name}: deleting..."
  for attempt in 1 2 3 4 5; do
    retain=()
    if [ "$(stack_status "${name}")" = "DELETE_FAILED" ]; then
      failed=$(aws cloudformation describe-stack-resources --stack-name "${name}" --region "${REGION}" \
        --query "StackResources[?ResourceStatus=='DELETE_FAILED'].[LogicalResourceId,ResourceType]" --output text)
      while read -r id type; do
        [ -n "${id}" ] || continue
        # Every CDK custom resource except the cluster itself only manages objects that
        # disappear with their parent (k8s manifests, VPC default security group rules)
        if [[ "${type}" == Custom::* ]] && [ "${type}" != "Custom::AWSCDK-EKS-Cluster" ]; then
          retain+=("${id}")
        fi
      done <<< "${failed}"
      echo "    retry ${attempt}: failed resources $(echo "${failed}" | awk '{print $1}' | tr '\n' ' ')"
      if [ "${#retain[@]}" -eq 0 ]; then
        delete_cluster_directly
        sleep 30
      fi
    fi
    aws cloudformation delete-stack --stack-name "${name}" --region "${REGION}" \
      ${retain[@]:+--retain-resources "${retain[@]}"}
    aws cloudformation wait stack-delete-complete --stack-name "${name}" --region "${REGION}" 2>/dev/null || true
    if [ "$(stack_status "${name}")" = "NOT_FOUND" ]; then
      echo "    ✓ ${name} deleted"
      return
    fi
  done
  echo "❌ ${name} could not be deleted after 5 attempts. Check the stack events in the CloudFormation console."
  exit 1
}

echo "[1/4] Helm release"
if aws eks describe-cluster --name "${CLUSTER}" --region "${REGION}" >/dev/null 2>&1; then
  aws eks update-kubeconfig --name "${CLUSTER}" --region "${REGION}" >/dev/null
  helm uninstall cosmos-curator -n cosmos-curator --wait 2>/dev/null || true
fi

echo "[2/4] CloudFormation stacks"
delete_stack CosmosCurateOpenSearch
delete_stack CosmosCurateEks
delete_stack CosmosCurateBuild

delete_cluster_directly
VPC_ID=$(aws cloudformation describe-stacks --stack-name CosmosCurateCore --region "${REGION}" \
  --query "Stacks[0].Outputs[?OutputKey=='VpcId'].OutputValue" --output text 2>/dev/null || true)
if [ -n "${VPC_ID}" ] && [ "${VPC_ID}" != "None" ]; then
  for sg in $(aws ec2 describe-security-groups --region "${REGION}" \
      --filters "Name=vpc-id,Values=${VPC_ID}" "Name=group-name,Values=eks-cluster-sg-*" \
      --query 'SecurityGroups[].GroupId' --output text); do
    aws ec2 delete-security-group --group-id "${sg}" --region "${REGION}" && echo "  - deleted leftover security group ${sg}"
  done
fi
delete_stack CosmosCurateCore

echo "[3/4] Retained resources (S3 bucket, ECR repositories)"
if aws s3api head-bucket --bucket "${BUCKET}" --region "${REGION}" >/dev/null 2>&1; then
  aws s3 rb "s3://${BUCKET}" --force --region "${REGION}" >/dev/null && echo "  ✓ s3://${BUCKET}"
fi
for repo in cosmos-curate/curator-cpu cosmos-curate/caption-proxy; do
  aws ecr delete-repository --repository-name "${repo}" --force --region "${REGION}" >/dev/null 2>&1 \
    && echo "  ✓ ECR ${repo}" || true
done

echo "[4/4] kubeconfig"
rm -f "${KUBECONFIG}"

echo "✅ ${REGION} cleaned up (the CDK bootstrap stack is kept)"
