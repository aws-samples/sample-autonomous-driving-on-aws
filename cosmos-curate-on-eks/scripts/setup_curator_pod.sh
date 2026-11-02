#!/usr/bin/env bash
# Deploy caption-proxy and the Cosmos Curator Ray cluster (upstream Helm chart) onto the
# EKS cluster created by deploy_new_region.sh.
# Usage: ./setup_curator_pod.sh <region> [<replicas>]      e.g. ./setup_curator_pod.sh us-west-2 4
# The node option (a/b/c) is read from the cluster's node groups, so it always matches
# what deploy_new_region.sh actually created (including a Spot -> on-demand fallback).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [ $# -lt 1 ]; then
  echo "Usage: $0 <region> [<replicas>]"
  exit 1
fi

REGION="${1}"
# One kubeconfig per region: scripts for two regions can run at once without
# switching each other's current context.
export KUBECONFIG="${HOME}/.kube/cosmos-curate-${REGION}"
REPLICAS="${2:-4}"
# One 16-vCPU pod exposes 15 Ray CPUs; split+embed+caption needs more (seen: 17 required)
if [ "${REPLICAS}" -lt 2 ]; then
  echo "❌ replicas must be >= 2 (one pod = 15 Ray CPUs, not enough for the pipeline)"
  exit 1
fi
NAMESPACE="cosmos-curator"
CLUSTER_NAME="cosmos-curate-eks"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
BUCKET_NAME="cosmos-curate-${ACCOUNT_ID}-${REGION}"
ECR_BASE="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/cosmos-curate"

error_exit() {
  echo "❌ $1"
  exit 1
}

echo "[1/5] kubectl context"
# Always switch context: a working context for another region would deploy there
aws eks update-kubeconfig --name "${CLUSTER_NAME}" --region "${REGION}" >/dev/null || \
  error_exit "update-kubeconfig failed"
kubectl get nodes >/dev/null 2>&1 || error_exit "cannot reach the cluster in ${REGION}"

nodegroup_desired() {
  aws eks describe-nodegroup --cluster-name "${CLUSTER_NAME}" --nodegroup-name "$1" --region "${REGION}" \
    --query 'nodegroup.scalingConfig.desiredSize' --output text 2>/dev/null || echo 0
}
SPOT_N=$(nodegroup_desired cpu-spot)
OD_N=$(nodegroup_desired cpu-od)
if [ "${SPOT_N}" = "0" ]; then OPTION=a
elif [ "${OD_N}" = "0" ]; then OPTION=c
else OPTION=b
fi
echo "  option ${OPTION}: Spot ${SPOT_N}, on-demand ${OD_N}, curator replicas ${REPLICAS}"
kubectl get namespace "${NAMESPACE}" >/dev/null 2>&1 || kubectl create namespace "${NAMESPACE}"

# Curator settings file. Schema is openai.caption.{base_url,api_key}; the key is a dummy
# (Curate rejects an empty one), the proxy authenticates to Bedrock with Pod Identity.
# Mounted with subPath, so running pods only see changes after a restart.
echo "[2/5] Curator config (ConfigMap curator-openai-config)"
CAPTION_PROXY_URL="http://caption-proxy.${NAMESPACE}.svc.cluster.local:8080/v1"
cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: ConfigMap
metadata:
  name: curator-openai-config
  namespace: ${NAMESPACE}
data:
  cosmos_curator.yaml: |
    openai:
      caption:
        base_url: "${CAPTION_PROXY_URL}"
        api_key: "unused-pod-identity"
EOF

echo "[3/5] Caption proxy"
# With one curator pod per node there is room for only one proxy replica beside it, so the
# two replicas must land on different nodes.
cat <<EOF | kubectl apply -f -
apiVersion: apps/v1
kind: Deployment
metadata:
  name: caption-proxy
  namespace: ${NAMESPACE}
  labels:
    app: caption-proxy
spec:
  replicas: 2
  selector:
    matchLabels:
      app: caption-proxy
  template:
    metadata:
      labels:
        app: caption-proxy
    spec:
      serviceAccountName: default
      automountServiceAccountToken: true
      affinity:
        podAntiAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            - labelSelector:
                matchLabels:
                  app: caption-proxy
              topologyKey: kubernetes.io/hostname
      tolerations:
        - key: spot-instance
          operator: Equal
          value: "true"
          effect: PreferNoSchedule
      containers:
      - name: caption-proxy
        image: ${ECR_BASE}/caption-proxy:latest
        imagePullPolicy: Always
        ports:
        - containerPort: 8080
        env:
        - name: AWS_REGION
          value: "${REGION}"
        - name: BEDROCK_MODEL_ID
          value: "global.anthropic.claude-opus-5-5"
        resources:
          requests:
            cpu: "1"
            memory: "2Gi"
          limits:
            cpu: "2"
            memory: "4Gi"
---
apiVersion: v1
kind: Service
metadata:
  name: caption-proxy
  namespace: ${NAMESPACE}
spec:
  selector:
    app: caption-proxy
  ports:
  - port: 8080
    targetPort: 8080
  type: ClusterIP
EOF
kubectl wait --for=condition=available --timeout=600s deployment/caption-proxy -n "${NAMESPACE}" || \
  error_exit "caption-proxy did not become available"

echo "[4/5] Cosmos Curator (Helm)"
CURATE_REPO_PATH="${PROJECT_ROOT}/.cache/cosmos-curate"
HELM_CHART_PATH="${CURATE_REPO_PATH}/charts/cosmos-curator"
if [ ! -d "${HELM_CHART_PATH}" ]; then
  git clone --depth 1 --branch v2.8.0 \
    https://github.com/nvidia-cosmos/cosmos-curate.git "${CURATE_REPO_PATH}"
fi
# A fresh clone has no charts/ subcharts; helm install fails without them even when
# the dependency is disabled. Register each dependency repo, then build.
if [ ! -d "${HELM_CHART_PATH}/charts" ]; then
  i=0
  for repo in $(awk '/^dependencies:/{d=1} d && /repository:/{print $2}' "${HELM_CHART_PATH}/Chart.yaml" | sort -u); do
    i=$((i + 1))
    helm repo add "curate-dep-${i}" "${repo}" --force-update >/dev/null
  done
  helm dependency build "${HELM_CHART_PATH}" >/dev/null || error_exit "helm dependency build failed"
fi

# Placement per option. The chart uses one pod template for head and workers, so the head
# is steered by order, not by name: OrderedReady (post-renderer) creates pod-0 first, and in
# option b the on-demand node is tainted dedicated=curator so nothing else can take it.
SPOT_TOLERATION='
  - key: spot-instance
    operator: Equal
    value: "true"
    effect: PreferNoSchedule'
case "${OPTION}" in
  a) TOLERATIONS="[]"; NODE_AFFINITY="" ;;
  b) TOLERATIONS="${SPOT_TOLERATION}
  - key: dedicated
    operator: Equal
    value: curator
    effect: NoSchedule"
     NODE_AFFINITY='
  nodeAffinity:
    preferredDuringSchedulingIgnoredDuringExecution:
      - weight: 100
        preference:
          matchExpressions:
            - key: node-type
              operator: In
              values: [on-demand]' ;;
  c) TOLERATIONS="${SPOT_TOLERATION}"; NODE_AFFINITY="" ;;
esac

VALUES_FILE=$(mktemp)
trap 'rm -f "${VALUES_FILE}"' EXIT
cat > "${VALUES_FILE}" <<EOF
replicas: ${REPLICAS}

image:
  repository: ${ECR_BASE}/curator-cpu
  tag: latest
  pullPolicy: IfNotPresent

NGC_ENABLE: false

# onto_nvcf.py starts the Ray head/workers; jobs need NVCF_MULTI_NODE=false (see k8s/run_split.sh)
args:
  - pixi
  - run
  - --as-is
  - python
  - cosmos_curator/scripts/onto_nvcf.py
  - --helm
  - "True"

resources:
  requests:
    cpu: "14"
    memory: "24Gi"
    nvidia.com/gpu: "0"
  limits:
    memory: "28Gi"
    nvidia.com/gpu: "0"

volumeMounts:
  - name: shm
    mountPath: /dev/shm

volumes:
  - name: shm
    emptyDir:
      medium: Memory
      sizeLimit: 8Gi

extraVolumeMounts:
  - name: curator-openai-config
    mountPath: /cosmos_curator/config/cosmos_curator.yaml
    subPath: cosmos_curator.yaml

extraVolumes:
  - name: curator-openai-config
    configMap:
      name: curator-openai-config

# Explicit list: the chart default tolerates every taint, including draining nodes
tolerations: ${TOLERATIONS}

nodeSelector:
  workload: curator-cpu

affinity:${NODE_AFFINITY}
  podAntiAffinity:
    preferredDuringSchedulingIgnoredDuringExecution:
      - weight: 100
        podAffinityTerm:
          labelSelector:
            matchLabels:
              app.kubernetes.io/name: cosmos-curator
          topologyKey: kubernetes.io/hostname

extraEnv:
  AWS_REGION: "${REGION}"
  COSMOS_CURATOR_CLOUD_STORAGE_MODEL_WEIGHTS_PREFIX: "s3://${BUCKET_NAME}/models/curate-v2.8.0/"
  NVCF_MULTI_NODE: "false"
  RAY_MAX_LIMIT_FROM_API_SERVER: "40000"
  RAY_memory_monitor_refresh_ms: "0"

metrics:
  enabled: false
  serviceMonitor:
    enabled: false
EOF

# Pull the 14.5 GB image on every curator node in parallel first. With OrderedReady each
# pod is only scheduled after the previous one is Ready, so otherwise the ~5.5 min pulls
# run one after another (4 nodes took longer than 30 min).
cat <<EOF | kubectl apply -f -
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: curator-image-prepull
  namespace: ${NAMESPACE}
spec:
  selector:
    matchLabels:
      app: curator-image-prepull
  template:
    metadata:
      labels:
        app: curator-image-prepull
    spec:
      nodeSelector:
        workload: curator-cpu
      tolerations:
        - operator: Exists
      terminationGracePeriodSeconds: 0
      containers:
      - name: pull
        image: ${ECR_BASE}/curator-cpu:latest
        command: ["sleep", "infinity"]
        resources:
          requests:
            cpu: 10m
            memory: 16Mi
EOF
kubectl -n "${NAMESPACE}" rollout status ds/curator-image-prepull --timeout=1800s || \
  error_exit "image pre-pull did not finish"
kubectl -n "${NAMESPACE}" delete ds curator-image-prepull --wait=true

# podManagementPolicy is immutable: an existing StatefulSet created with Parallel must be
# replaced. --cascade=orphan keeps the running pods; the new StatefulSet adopts them.
CURRENT_POLICY=$(kubectl -n "${NAMESPACE}" get sts cosmos-curator -o jsonpath='{.spec.podManagementPolicy}' 2>/dev/null || true)
if [ -n "${CURRENT_POLICY}" ] && [ "${CURRENT_POLICY}" != "OrderedReady" ]; then
  echo "  replacing StatefulSet (podManagementPolicy ${CURRENT_POLICY} -> OrderedReady)"
  kubectl -n "${NAMESPACE}" delete sts cosmos-curator --cascade=orphan
fi

# Reuse the existing token: a new one on upgrade splits head and workers (InvalidAuthToken)
RAY_TOKEN=$(kubectl -n "${NAMESPACE}" get secret ray-auth-token -o jsonpath='{.data.token}' 2>/dev/null | base64 -d || true)
RAY_TOKEN="${RAY_TOKEN:-$(openssl rand -hex 32)}"

# 30m: pods start one by one (image already pulled above)
helm upgrade --install cosmos-curator "${HELM_CHART_PATH}" \
  -n "${NAMESPACE}" \
  -f "${VALUES_FILE}" \
  --set rayAuth.token="${RAY_TOKEN}" \
  --post-renderer "${PROJECT_ROOT}/k8s/post-renderer.sh" \
  --wait --timeout 30m
kubectl -n "${NAMESPACE}" rollout status sts/cosmos-curator --timeout=1800s >/dev/null

echo "[5/5] Ray cluster checks"
head_node_type() {
  local node
  node=$(kubectl -n "${NAMESPACE}" get pod cosmos-curator-0 -o jsonpath='{.spec.nodeName}')
  kubectl get node "${node}" -o jsonpath='{.metadata.labels.node-type}'
}
# Option b promises the head on on-demand. If an older deployment left it on Spot, move it
# once: delete pod-0 so it is recreated onto the reserved on-demand node.
if [ "${OPTION}" = "b" ] && [ "$(head_node_type)" != "on-demand" ]; then
  echo "  Ray head is on a $(head_node_type) node, recreating pod-0"
  kubectl -n "${NAMESPACE}" delete pod cosmos-curator-0
  kubectl -n "${NAMESPACE}" rollout status sts/cosmos-curator --timeout=1800s >/dev/null
  [ "$(head_node_type)" = "on-demand" ] || error_exit "Ray head (pod-0) is not on the on-demand node"
fi
echo "  ✓ Ray head (pod-0) on a $(head_node_type) node"

# StatefulSet restarts highest ordinal first, so the head (pod-0) restarts last and
# workers that joined the old head stay orphaned. Recreate workers until all join.
NODES=0
for attempt in 1 2 3; do
  kubectl -n "${NAMESPACE}" rollout status sts/cosmos-curator --timeout=1800s >/dev/null
  # Count alive nodes: `ray status` keeps listing dead nodes under Active for a while
  NODES=$(kubectl -n "${NAMESPACE}" exec cosmos-curator-0 -- pixi run --as-is python -c "
import ray
ray.init(address='auto', logging_level='ERROR', log_to_driver=False)
print(sum(1 for n in ray.nodes() if n['Alive']))" 2>/dev/null | tail -1 || echo 0)
  [[ "${NODES}" =~ ^[0-9]+$ ]] || NODES=0
  if [ "${NODES}" -ge "${REPLICAS}" ]; then
    break
  fi
  echo "  Ray nodes ${NODES}/${REPLICAS}, recreating workers (${attempt}/3)"
  for i in $(seq 1 $((REPLICAS - 1))); do
    kubectl -n "${NAMESPACE}" delete pod "cosmos-curator-${i}" --wait=false
  done
  sleep 30
done
[ "${NODES}" -ge "${REPLICAS}" ] || error_exit "Ray workers did not join (${NODES}/${REPLICAS})"
echo "  ✓ Ray nodes ${NODES}/${REPLICAS} joined"

kubectl -n "${NAMESPACE}" get pods -o wide
echo
echo "✓ Curator is ready. Run the pipeline with: k8s/run_split.sh ${REGION} <dataset under s3://${BUCKET_NAME}/inputs/>"
