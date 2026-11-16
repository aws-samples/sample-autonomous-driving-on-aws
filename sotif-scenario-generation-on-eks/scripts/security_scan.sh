#!/usr/bin/env bash
# AWS Automated Security Helper (ASH) on the source tree and on the synthesized CloudFormation templates.
#   scripts/security_scan.sh [output-dir]        # default build/security; needs uv (uvx) and the cdk/ toolchain
# Fails if ASH reports a finding at MEDIUM or above that has no recorded reason (.ash/.ash.yaml,
# cdk/drive_atlas_infra/checks.py), or if Semgrep finds anything with inline "# nosemgrep" suppressions ignored, using
# audit rules beyond ASH's defaults. cdk-nag also runs on every synth.
set -euo pipefail
cd "$(dirname "$0")/.."  # the sample folder (works as a repository root or as a subfolder)
OUT=$(realpath -m "${1:-build/security}")
ASH_VERSION=${ASH_VERSION:-v3.1.2}
CONFIG="$PWD/.ash/.ash.yaml"  # absolute: ASH resolves a relative --config against --source-dir
ASH=(uvx --python 3.12 --from "git+https://github.com/awslabs/automated-security-helper.git@${ASH_VERSION}" ash)
rm -rf "$OUT" && mkdir -p "$OUT/templates"

# In the development repository, scan what would be published (scripts/export_sample.sh); in the sample, scan it in place.
SRC=.
if [ -x scripts/export_sample.sh ]; then
  scripts/export_sample.sh "$OUT/sample" > "$OUT/export.log" 2>&1 || { tail -20 "$OUT/export.log"; exit 1; }
  SRC="$OUT/sample"
fi

# Templates are only scanned, never deployed: the allowlist is a documentation range (RFC 5737).
(cd cdk && npx cdk synth -q -o "$OUT/cdk.out" -c drive-atlas:allowedCidrs=203.0.113.0/24 \
  -c "drive-atlas:region=${AWS_REGION:-us-west-2}" > "$OUT/synth.log" 2>&1) || { tail -30 "$OUT/synth.log"; exit 1; }
cp "$OUT"/cdk.out/*.template.json "$OUT/templates/"

status=0
"${ASH[@]}" --mode local --config "$CONFIG" --source-dir "$SRC" --output-dir "$OUT/source" --no-progress > "$OUT/source.log" 2>&1 || status=1
"${ASH[@]}" --mode local --config "$CONFIG" --source-dir "$OUT/templates" --output-dir "$OUT/infra" --no-progress > "$OUT/infra.log" 2>&1 || status=1
# Semgrep registry rule packs plus audit rules for subprocess calls, DOM XSS sinks and XML parsing; "# nosemgrep" ignored.
SEMGREP_RULES=(p/default p/python p/javascript p/security-audit p/secrets p/dockerfile
  r/python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
  r/javascript.browser.security.insecure-document-method.insecure-document-method
  r/javascript.browser.security.insecure-innerhtml.insecure-innerhtml
  r/python.lang.security.use-defused-xml.use-defused-xml)
(cd "$SRC" && uvx semgrep scan "${SEMGREP_RULES[@]/#/--config=}" --disable-nosem --metrics=off --quiet --json \
  --x-ignore-semgrepignore-files . > "$OUT/semgrep.json" 2> "$OUT/semgrep.log") || true
python3 - "$OUT/semgrep.json" <<'PY' || status=1
import json, sys
results = json.load(open(sys.argv[1]))["results"]
for r in results:
    print(f"  {r['check_id'].rsplit('.', 1)[-1]}  {r['path']}:{r['start']['line']}")
print(f"semgrep (nosemgrep ignored): {len(results)} finding(s)")
sys.exit(1 if results else 0)
PY

for part in source infra; do
  echo "== ${part}"; grep -E "^│ [a-z-]+ +│" "$OUT/$part.log" || tail -5 "$OUT/$part.log"
done
echo "reports: $OUT/source/reports/ash.html, $OUT/infra/reports/ash.html"
exit $status
