#!/usr/bin/env bash
# SOTIF ontology pipeline: LLM extension -> schema validation -> sotif/ontology.nt (+ augment/ontology_snapshot.json) -> Neptune.
#   scripts/build_ontology.sh [options]          # extend with Bedrock; options go to `python3 -m sotif.build`
#       --per-class N   new instances per class (default 3)    --classes VRU,WeatherCondition
#       --docs DIR      .txt/.md source documents to extract from  --model sonnet|opus|haiku|<model id>
#   [STAGE=..] [ALLOWED_CIDRS=..] scripts/build_ontology.sh --load <region> [tag]   (same values as scripts/deploy.sh)
#       load the committed sotif/ontology.nt into Neptune: deploys DriveAtlasPipeline, whose loader Job runs
#       inside the VPC and is named after the file hash (no change, no reload)
set -euo pipefail
cd "$(dirname "$0")/.."  # the sample folder (works as a repository root or as a subfolder)

if [ "${1:-}" = "--load" ]; then
  REGION=${2:?region}; TAG=${3:-$(git rev-parse --short HEAD 2>/dev/null || date -u +%Y%m%d%H%M%S)}; STAGE=${STAGE:-}
  git diff --quiet -- sotif/ontology.nt || echo "note: sotif/ontology.nt has uncommitted changes; they are loaded as they are"
  cd cdk
  npx cdk deploy --require-approval never -c "drive-atlas:region=${REGION}" -c "drive-atlas:stage=${STAGE}" \
    -c "drive-atlas:allowedCidrs=${ALLOWED_CIDRS:-}" -c "imageTag=${TAG}" -c "pipelineImageTag=${TAG}" "DriveAtlas${STAGE^}Pipeline"
  exit 0
fi

python3 -m sotif.build "$@"
python3 - <<'PY'
import json
r = json.load(open("build/ontology-report.json"))
dropped = sum(len(c["dropped"]) for c in r["classes"].values())
print(f"instances {r['totals']['instances'][0]} -> {r['totals']['instances'][1]}, relations {r['totals']['relations'][0]} -> {r['totals']['relations'][1]}, dropped {dropped} (see build/ontology-report.json)")
PY
git --no-pager diff --stat -- sotif/ontology.nt augment/ontology_snapshot.json
echo "review the diff, commit it, then: scripts/build_ontology.sh --load <region>"
