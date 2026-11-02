#!/usr/bin/env bash
# Helm post-renderer for the upstream cosmos-curator chart.
# The chart hardcodes podManagementPolicy: Parallel, so all pods start at once and any of
# them can take the on-demand node. OrderedReady creates pod-0 (the Ray head) first, so it
# lands on the on-demand node it prefers (option b) before the workers are scheduled.
# Usage: helm upgrade --install ... --post-renderer k8s/post-renderer.sh
set -euo pipefail

TMP=$(mktemp -d)
trap 'rm -rf "${TMP}"' EXIT

cat > "${TMP}/manifest.yaml"
cat > "${TMP}/kustomization.yaml" <<'EOF'
resources:
  - manifest.yaml
patches:
  - target:
      kind: StatefulSet
      name: cosmos-curator
    patch: |-
      - op: add
        path: /spec/podManagementPolicy
        value: OrderedReady
EOF
kubectl kustomize "${TMP}"
