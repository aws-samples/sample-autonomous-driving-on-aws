#!/usr/bin/env bash
# Build the four container images and push them to ECR (repositories come from the <prefix>Data stack).
#   scripts/build_and_push_images.sh <region> [tag] [repository prefix]    # tag: git short SHA; prefix: drive-atlas
set -euo pipefail
REGION=${1:?region}
TAG=${2:-$(git rev-parse --short HEAD 2>/dev/null || date -u +%Y%m%d%H%M%S)}
PREFIX=${3:-drive-atlas}
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGISTRY="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"
cd "$(dirname "$0")/.."  # the sample folder (works as a repository root or as a subfolder)

aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REGISTRY" >/dev/null
for spec in "web:docker/web/Dockerfile" "pipeline:docker/pipeline/Dockerfile" "cosmos-hdmap:docker/cosmos-hdmap/Dockerfile" "pipeline-api:docker/pipeline-api/Dockerfile"; do
  name=${spec%%:*}; file=${spec#*:}; repo="${PREFIX}/${name}"; image="${REGISTRY}/${repo}:${TAG}"
  if aws ecr describe-images --region "$REGION" --repository-name "$repo" --image-ids imageTag="$TAG" >/dev/null 2>&1; then
    echo "skip ${repo}:${TAG} (already in ECR; tags are immutable)"; continue
  fi
  echo "build ${repo}:${TAG}"
  docker build --platform linux/amd64 -f "$file" -t "$image" .
  docker push "$image" >/dev/null
done
echo "$TAG"
