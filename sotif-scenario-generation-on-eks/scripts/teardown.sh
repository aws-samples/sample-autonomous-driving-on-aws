#!/usr/bin/env bash
# Delete one deployed copy (stage) of the sample: every stack of the stage (including the us-east-1 edge stacks) and the resources
# CloudFormation retains on purpose (S3 buckets with all objects, ECR repositories) or that the services create
# themselves (EKS, Neptune and AgentCore log groups).
#   [STAGE=v2] scripts/teardown.sh <region>          # asks you to type the stack prefix; YES=1 skips the prompt
# The CDK bootstrap stacks are left in place. Re-runnable: finished steps are no-ops.
# AgentCore releases the planner's VPC network interfaces up to hours after its runtime is deleted; until then the
# agent security group and the VPC cannot be deleted. Everything else is removed first, then the script waits
# ENI_WAIT_MIN minutes (default 30) and otherwise asks you to re-run it later.
set -euo pipefail
REGION=${1:?usage: scripts/teardown.sh <region>}
cd "$(dirname "$0")/.."  # the sample folder (works as a repository root or as a subfolder)
STAGE=${STAGE:-}
PREFIX="drive-atlas${STAGE:+-$STAGE}"
STACK="DriveAtlas${STAGE^}"

if [ "${YES:-}" != "1" ]; then
  read -r -p "Delete ${STACK}* in ${REGION} and us-east-1, including all data in its S3 buckets? Type ${STACK}: " answer
  [ "$answer" = "$STACK" ] || { echo "aborted"; exit 1; }
fi

resource() {  # <stack> <resource type> -> physical id, empty if the stack or resource does not exist
  aws cloudformation describe-stack-resources --region "$REGION" --stack-name "$1" \
    --query "StackResources[?ResourceType=='$2'].PhysicalResourceId | [0]" --output text 2>/dev/null | grep -v '^None$' || true
}
# Artifact bucket first: it writes access logs into the log bucket, which is emptied and deleted last.
BUCKETS=$(aws cloudformation describe-stack-resources --region "$REGION" --stack-name "${STACK}Data" \
  --query "StackResources[?ResourceType=='AWS::S3::Bucket'].[LogicalResourceId,PhysicalResourceId]" --output text 2>/dev/null \
  | sort -r -k1,1 | awk '{print $2}' || true)
# Data stack already deleted (e.g. by cdk destroy): its retained buckets keep the stack name as their prefix.
[ -n "$BUCKETS" ] || BUCKETS=$(aws s3api list-buckets --query "Buckets[?starts_with(Name, '${STACK,,}data-')].Name" --output text | tr '\t' '\n' | sort -r)

stack_status() {
  aws cloudformation describe-stacks --region "$REGION" --stack-name "$1" --query "Stacks[0].StackStatus" --output text 2>/dev/null || echo GONE
}
agent_sg() {
  aws ec2 describe-security-groups --region "$REGION" --filters "Name=group-name,Values=${STACK}Pipeline-AgentSg*" \
    --query "SecurityGroups[0].GroupId" --output text 2>/dev/null | grep -v '^None$' || true
}

cd cdk
# Synthesis needs an access setting; the value does not matter for deletion.
CDK=(npx cdk -c "drive-atlas:region=${REGION}" -c "drive-atlas:stage=${STAGE}" -c "drive-atlas:allowedCidrs=127.0.0.1/32")
if ! "${CDK[@]}" destroy --force --all; then
  if [ "$(stack_status "${STACK}Pipeline")" = "DELETE_FAILED" ]; then
    # Keep only the agent security group; it is deleted below once AgentCore lets go of its interfaces.
    aws cloudformation delete-stack --region "$REGION" --stack-name "${STACK}Pipeline" \
      --retain-resources "$(aws cloudformation describe-stack-resources --region "$REGION" --stack-name "${STACK}Pipeline" \
        --query "StackResources[?ResourceStatus=='DELETE_FAILED'].LogicalResourceId" --output text)"
    aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name "${STACK}Pipeline"
  fi
  # Everything but Network: destroy --all stops at the first failure, and Network usually fails while AgentCore still
  # holds interfaces in its subnets (it is deleted at the end of this script).
  "${CDK[@]}" destroy --force --exclusively "${STACK}"{Pipeline,Web,Neptune,Eks,EdgeWaf,Data} \
    || echo "some stacks are left; continuing with the retained resources"
fi
cd ..

# Retained resources: keep going on errors (e.g. missing permissions) and list what is left at the end.
LEFT=()
try() { local what=$1; shift; if "$@" >/dev/null; then echo "deleted ${what}"; else LEFT+=("$what"); fi; }
empty_bucket() {
  python3 - "$1" "$REGION" <<'PY'
import sys, time, boto3
bucket = boto3.resource("s3", region_name=sys.argv[2]).Bucket(sys.argv[1])
for attempt in range(5):  # late access-log deliveries can refill a log bucket between emptying and deleting
    bucket.object_versions.delete()
    try:
        bucket.delete()
        break
    except bucket.meta.client.exceptions.ClientError as exc:
        if "BucketNotEmpty" not in str(exc) or attempt == 4:
            raise
        time.sleep(10)
PY
}

for bucket in $BUCKETS; do
  aws s3api head-bucket --bucket "$bucket" >/dev/null 2>&1 || continue  # already deleted by an earlier run
  try "S3 bucket ${bucket}" empty_bucket "$bucket"
done
for name in web pipeline cosmos-hdmap pipeline-api; do
  if aws ecr describe-repositories --region "$REGION" --repository-names "${PREFIX}/${name}" >/dev/null 2>&1; then
    try "ECR ${PREFIX}/${name}" aws ecr delete-repository --region "$REGION" --repository-name "${PREFIX}/${name}" --force
  fi
done
for prefix in "/aws/eks/${PREFIX}/" "/aws/neptune/${PREFIX}-ontology/" "/aws/bedrock-agentcore/runtimes/${PREFIX//-/_}_planner"; do
  for group in $(aws logs describe-log-groups --region "$REGION" --log-group-name-prefix "$prefix" --query "logGroups[].logGroupName" --output text); do
    try "log group ${group}" aws logs delete-log-group --region "$REGION" --log-group-name "$group"
  done
done

# Lambda functions that CDK creates (custom resources, EKS handlers) leave their log groups behind.
for name in Network Data Eks Neptune Web Pipeline EdgeWaf; do
  for r in "$REGION" us-east-1; do
    for group in $(aws logs describe-log-groups --region "$r" --log-group-name-prefix "/aws/lambda/${STACK}${name}-" --query "logGroups[].logGroupName" --output text); do
      try "log group ${group}" aws logs delete-log-group --region "$r" --log-group-name "$group"
    done
  done
done

SG=$(agent_sg)
if [ -n "$SG" ] || [ "$(stack_status "${STACK}Network")" != "GONE" ]; then
  if [ -n "$SG" ]; then
    deadline=$(( $(date +%s) + ${ENI_WAIT_MIN:-30} * 60 ))
    while [ "$(aws ec2 describe-network-interfaces --region "$REGION" --filters "Name=group-id,Values=${SG}" --query 'length(NetworkInterfaces)' --output text)" != "0" ]; do
      [ "$(date +%s)" -lt "$deadline" ] || break
      echo "waiting for AgentCore to release the network interfaces of ${SG}"; sleep 60
    done
    aws ec2 delete-security-group --region "$REGION" --group-id "$SG" >/dev/null 2>&1 && echo "deleted agent security group ${SG}"
  fi
  if [ -z "$(agent_sg)" ]; then
    (cd cdk && "${CDK[@]}" destroy --force --exclusively "${STACK}Network") || LEFT+=("stack ${STACK}Network")
  else
    LEFT+=("agent security group $(agent_sg) and stack ${STACK}Network: AgentCore still holds its network interfaces; re-run this script later")
  fi
fi

if [ ${#LEFT[@]} -gt 0 ]; then
  printf 'not deleted (delete manually):\n'; printf '  %s\n' "${LEFT[@]}"; exit 1
fi
echo "done"
