#!/usr/bin/env bash
# Deploy the sample from an empty account/region: CDK bootstrap -> network, data -> images -> EKS, Neptune -> web
# (CloudFront, WAF) -> pipeline (API, AgentCore agent, ontology load Job).
#   ALLOWED_CIDRS=203.0.113.0/24 scripts/deploy.sh <region>
# Env:
#   ALLOWED_CIDRS     CIDRs allowed through the CloudFront WAF (required)
#   STAGE             optional name prefix so several copies can share an account (e.g. STAGE=v2)
#   EKS_PUBLIC_CIDRS  open the EKS API to these CIDRs for kubectl (default: private endpoint only)
#   GPU_MAX_NODES     create the g7e/g6e Spot node group with this many nodes at most (default 0)
#   TAG               image tag (default: git short SHA)
set -euo pipefail
REGION=${1:?usage: scripts/deploy.sh <region>}
cd "$(dirname "$0")/.."  # the sample folder (works as a repository root or as a subfolder)
STAGE=${STAGE:-}
TAG=${TAG:-$(git rev-parse --short HEAD 2>/dev/null || date -u +%Y%m%d%H%M%S)}
PREFIX="drive-atlas${STAGE:+-$STAGE}"
STACK="DriveAtlas${STAGE^}"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)

cd cdk
[ -x .venv/bin/python ] || "$(command -v python3.11 || command -v python3)" -m venv .venv
.venv/bin/python -m pip install -q -r requirements.txt
[ -d node_modules ] || npm install --silent
CDK=(npx cdk -c "drive-atlas:region=${REGION}" -c "drive-atlas:stage=${STAGE}" -c "drive-atlas:allowedCidrs=${ALLOWED_CIDRS:-}"
     -c "drive-atlas:eksPublicAccessCidrs=${EKS_PUBLIC_CIDRS:-}" -c "drive-atlas:gpuMaxNodes=${GPU_MAX_NODES:-0}"
     -c "imageTag=${TAG}" -c "pipelineImageTag=${TAG}")

# CloudFront's WAF lives in us-east-1, so both regions need the bootstrap stack.
"${CDK[@]}" bootstrap "aws://${ACCOUNT}/${REGION}" "aws://${ACCOUNT}/us-east-1"
"${CDK[@]}" deploy --require-approval never "${STACK}Network" "${STACK}Data"
(cd .. && scripts/build_and_push_images.sh "$REGION" "$TAG" "$PREFIX")
"${CDK[@]}" deploy --require-approval never "${STACK}Eks" "${STACK}Neptune"
"${CDK[@]}" deploy --require-approval never "${STACK}Web" "${STACK}Pipeline"

aws cloudformation describe-stacks --region "$REGION" --stack-name "${STACK}Web" --query "Stacks[0].Outputs[?OutputKey=='Url'].OutputValue" --output text
