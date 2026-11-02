#!/usr/bin/env bash
# Build the curator-cpu and caption-proxy images and push them to ECR.
# Usage: ./build_and_push_images.sh <region> [<version>]
# e.g.  ./build_and_push_images.sh us-west-2 v2.8.0
# Needs a Linux build host (GNU sha256sum and df).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [ $# -lt 1 ]; then
  echo "Usage: $0 <region> [<version>]"
  echo "e.g.  $0 us-west-2 v2.8.0"
  exit 1
fi

REGION="${1}"
VERSION="${2:-v2.8.0}"

echo "=================================="
echo "Build and push images"
echo "=================================="
echo "Region: ${REGION}"
echo "Version: ${VERSION}"
echo ""

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text --region "${REGION}")
ECR_BASE="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
REPO_NAME="cosmos-curate"

echo "ECR registry: ${ECR_BASE}"
echo "Repository prefix: ${REPO_NAME}"
echo ""

echo "[1/5] ECR login"
aws ecr get-login-password --region "${REGION}" | \
  docker login --username AWS --password-stdin "${ECR_BASE}"
echo "✓ ECR login done"
echo ""

echo "[2/5] Build curator-cpu"
cd "${PROJECT_ROOT}/docker"
# Region-independent local tag keyed on the build context, so a second region
# re-tags the existing ~30 GB image instead of rebuilding it.
BUILD_HASH=$(find . -type f ! -path './caption-proxy/*' -print0 | sort -z | xargs -0 sha256sum | sha256sum | cut -c1-12)
LOCAL_TAG="cosmos-curate/curator-cpu:build-${BUILD_HASH}"
if docker image inspect "${LOCAL_TAG}" >/dev/null 2>&1; then
  echo "  Reusing local image with the same build context: ${LOCAL_TAG}"
else
  # Observed: a full build succeeds with ~37 GB free
  FREE_GB=$(df -BG --output=avail "$(docker info -f '{{.DockerRootDir}}')" | tail -1 | tr -dc '0-9')
  if [ "${FREE_GB}" -lt 40 ]; then
    echo "❌ Docker disk has ${FREE_GB} GB free; the build needs 40 GB. Free space (docker system prune -a) or grow the volume, then retry."
    exit 1
  fi
  docker build -f Dockerfile.cpu -t "${LOCAL_TAG}" .
fi
docker tag "${LOCAL_TAG}" "${ECR_BASE}/${REPO_NAME}/curator-cpu:${VERSION}"
docker tag "${LOCAL_TAG}" "${ECR_BASE}/${REPO_NAME}/curator-cpu:latest"
echo "✓ curator-cpu built"
echo ""

echo "[3/5] Build caption-proxy"
docker build \
  -t "${ECR_BASE}/${REPO_NAME}/caption-proxy:${VERSION}" \
  -t "${ECR_BASE}/${REPO_NAME}/caption-proxy:latest" \
  caption-proxy/
echo "✓ caption-proxy built"
echo ""

# A push of the 14.5 GB image can drop mid-layer ("unexpected EOF"); retry, layers
# that already made it are skipped.
push() {
  local attempt
  for attempt in 1 2 3; do
    docker push "$1" && return 0
    echo "  push failed (${attempt}/3), retrying in 30 s"
    sleep 30
  done
  return 1
}

echo "[4/5] Push curator-cpu (version tag + latest)"
push "${ECR_BASE}/${REPO_NAME}/curator-cpu:${VERSION}"
push "${ECR_BASE}/${REPO_NAME}/curator-cpu:latest"
echo "✓ curator-cpu pushed"
echo ""

echo "[5/5] Push caption-proxy (version tag + latest)"
push "${ECR_BASE}/${REPO_NAME}/caption-proxy:${VERSION}"
push "${ECR_BASE}/${REPO_NAME}/caption-proxy:latest"
echo "✓ caption-proxy pushed"
echo ""

echo "=================================="
echo "Images pushed"
echo "=================================="
echo "curator-cpu:"
echo "  - ${ECR_BASE}/${REPO_NAME}/curator-cpu:${VERSION}"
echo "  - ${ECR_BASE}/${REPO_NAME}/curator-cpu:latest"
echo ""
echo "caption-proxy:"
echo "  - ${ECR_BASE}/${REPO_NAME}/caption-proxy:${VERSION}"
echo "  - ${ECR_BASE}/${REPO_NAME}/caption-proxy:latest"
echo ""
