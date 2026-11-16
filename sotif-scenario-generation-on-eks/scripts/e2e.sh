#!/usr/bin/env bash
# Empty account/region -> deployed -> verified end to end (scripts/deploy.sh, then scripts/verify_pipeline.py).
#   [STAGE=..] [ALLOWED_CIDRS=203.0.113.0/24] scripts/e2e.sh <region>
# This host's public IP is added to ALLOWED_CIDRS so the check can reach CloudFront. Re-runnable: finished steps are no-ops.
set -euo pipefail
REGION=${1:?usage: scripts/e2e.sh <region>}
cd "$(dirname "$0")/.."  # the sample folder (works as a repository root or as a subfolder)
STAGE=${STAGE:-}
MY_IP="$(curl -fsS https://checkip.amazonaws.com | tr -d '[:space:]')/32"
export STAGE ALLOWED_CIDRS="${ALLOWED_CIDRS:+${ALLOWED_CIDRS},}${MY_IP}"

started=$(date +%s)
scripts/deploy.sh "$REGION"
deployed=$(date +%s)
URL=$(aws cloudformation describe-stacks --region "$REGION" --stack-name "DriveAtlas${STAGE^}Web" \
  --query "Stacks[0].Outputs[?OutputKey=='Url'].OutputValue" --output text)
cdk/.venv/bin/python scripts/verify_pipeline.py "$URL" --lang "${E2E_LANG:-en}"  # deploy.sh created this venv (cdk/requirements.txt has urllib3)
echo "deploy $(( deployed - started )) s, verify $(( $(date +%s) - deployed )) s, site ${URL}"
