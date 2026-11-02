# CLAUDE.md — deploying and validating Cosmos Curate on EKS

Instructions for [Claude Code](https://claude.com/claude-code) (or any coding agent)
working in this repository. A user can open the repo in Claude Code and ask, for
example, "deploy to us-west-2 and run the end-to-end test", and the agent should
follow this file.

## The supported way to run

```bash
[OPTION=a|b|c] scripts/e2e.sh <region> [local .mp4 dir]   # empty region -> search results
scripts/teardown.sh <region> --yes          # remove everything, incl. RETAIN S3/ECR
```

`e2e.sh` chains these steps; each is safe to re-run and skips finished work:

1. `scripts/deploy_new_region.sh` — CDK Core (VPC/S3/ECR), image build + push,
   Hugging Face weights -> S3 (`bootstrap/seed_models.sh`), EKS, OpenSearch Serverless
2. `scripts/setup_curator_pod.sh <region> 4` — caption-proxy, Helm chart, 4-pod Ray cluster;
   checks that the Ray head (pod-0) is on the on-demand node in option `b`
3. Input video: the 30 s sample bundled in the image, or the given local directory
4. `k8s/run_split.sh` — split + Cosmos-Embed1 embedding + Bedrock caption as a Ray job
5. `scripts/upload_embeddings_to_opensearch.py`, run inside pod-0 (its role has AOSS access)
6. `scripts/verify_opensearch.py` — exits non-zero unless documents exist and both
   text and kNN search return hits

**Done** means step 6 passed in a region that was empty before the run.
When preparing a release from a working copy that has extra files, validate the exported
tree (the files users will get), not the working copy.

Before starting, confirm with the user: target region, that the deploying role may call
Bedrock (Claude), and that the build host has at least 40 GB free Docker disk. Ask which
node option they want (`a` 4 on-demand, `b` 3 Spot + 1 on-demand head (default), `c` 4 Spot).
The region needs 64 vCPU of quota.
Ask before running `teardown.sh`; it deletes the S3 bucket and its data.

## Rules

- **Fix the script, not the cluster.** If a step fails, change the script or CDK code,
  then re-run (from `teardown.sh` if the failure left partial state). A manual fix in
  one region (kubectl patch, console change, `docker prune`) silently breaks the next.
- Don't report success until `verify_opensearch.py` exits 0. Infrastructure being up
  is not an end-to-end pass.
- Don't pipe long-running commands through `| tail` / `| grep` when the exit code
  matters; it can hide a failed build.
- After changing scripts or CDK code, confirm the change with `scripts/e2e.sh` in an empty region.

## Known traps (handled in the scripts — keep them)

| Trap | Where handled |
|---|---|
| Pod args must be `onto_nvcf.py --helm True`; overriding them (e.g. `ray start; sleep infinity`) breaks head/worker startup | setup_curator_pod.sh |
| `--helm` sets `NVCF_MULTI_NODE=true`, which makes Curate fetch weights from NVCF (needs an NGC key). Jobs must pass `NVCF_MULTI_NODE=false` to use the S3 weights | run_split.sh |
| One 16-vCPU pod exposes 15 Ray CPUs; the pipeline needs 17, so replicas >= 2 (default 4, one per node) | setup_curator_pod.sh |
| The upstream chart hardcodes `podManagementPolicy: Parallel`, so a node-affinity preference alone does not put pod-0 (Ray head) on on-demand. Option `b` uses an on-demand taint `dedicated=curator:NoSchedule` (keeps caption-proxy and CoreDNS off) plus `OrderedReady` via `k8s/post-renderer.sh`. Never taint the nodes in option `a` (CoreDNS would have no node). `podManagementPolicy` is immutable: an existing StatefulSet is replaced with `--cascade=orphan` | eks-stack.ts, setup_curator_pod.sh, post-renderer.sh |
| With one curator pod per node there is room for one caption-proxy replica per node; the two replicas use required anti-affinity | setup_curator_pod.sh |
| A new Ray auth token on each `helm upgrade` splits head and workers (`InvalidAuthToken`); reuse the existing secret | setup_curator_pod.sh |
| StatefulSet restarts the head last, orphaning workers; the script re-creates them until all join | setup_curator_pod.sh |
| Caption config schema is `openai.caption.{base_url,api_key}` at `/cosmos_curator/config/cosmos_curator.yaml` (subPath mount: pods must restart to see changes) | setup_curator_pod.sh |
| Kubernetes objects belong to the scripts, AWS resources to CDK. Don't add manifests to the CDK stacks: they duplicated the script's objects (with a wrong image) and their custom resources fail on delete | cdk/, setup_curator_pod.sh |
| OpenSearch Serverless needs IAM `aoss:APIAccessAll` in addition to the collection data access policy | eks-stack.ts |
| The kNN index needs `index.knn: true`; new indexes and documents take ~1-2 min to become searchable | upload / verify scripts |
| kubectl uses the current context, which may be another region; always `aws eks update-kubeconfig` for the target region | all scripts |
| The deploying IAM role must be mapped into aws-auth (`-c kubectlRole=<role>`) or kubectl gets `Unauthorized`. Every `cdk deploy` must pass the same context: deploying OpenSearch also redeploys EKS and drops the mapping otherwise | deploy_new_region.sh |
| The CPU image is ~30 GB and the build needs ~37 GB free; an identical build context is re-tagged for another region instead of rebuilt | build_and_push_images.sh |
| Pipeline flags are `--input-video-path` / `--output-clip-path` (not `--input-path` or `--video_source_s3_url`) | run_split.sh |
| Run Curate with `pixi run --as-is`; without `--as-is` pixi tries to lock the root-owned envs and fails | run_split.sh |
| Each node pulls the 14.5 GB image (~5.5 min); a Spot reclaim during rollout forces a second pull, so Helm/rollout waits are 30 min. With `OrderedReady` the pulls would run one after another, so a pre-pull DaemonSet pulls on all nodes in parallel first | setup_curator_pod.sh |
| Cosmos-Embed1 hardcodes bf16. On CPUs without AMX (c5/m5/c6i/m6i, AMD c6a/m6a) bf16 is 5-300x slower than fp32; a run on m6a sat in the embedding stage for over an hour. bf16 only with `amx_bf16`, else fp32. Which types you get is up to Spot, so a pass on c7i says nothing about the others | docker/patch_cpu.py |
| A 14.5 GB push can drop with `unexpected EOF`; pushes are retried 3 times | build_and_push_images.sh |
| Losing any Ray node (Spot reclaim/rebalance) fails the whole job. Jobs are submitted `--no-wait` and polled (a `kubectl exec` log stream can drop while the job runs); a job that failed with a dead node is resubmitted after `setup_curator_pod.sh` repairs the cluster, into a new output prefix, up to 3 attempts | run_split.sh |
| `ray status` keeps dead nodes under Active, so counting its lines overstates joined workers; count `ray.nodes()` with `Alive` | setup_curator_pod.sh |
| `cmd \| grep -q` under `pipefail` fails on a match when `cmd` gets SIGPIPE; capture the output first, then grep | run_split.sh |
| A fresh chart clone has no `charts/` subcharts; `helm dependency build` (after `helm repo add` for each dependency) is required | setup_curator_pod.sh |
| No Spot capacity / quota: nodes never come up. deploy_new_region.sh reads node-group launch errors and switches to option `a` (4 on-demand); an existing cluster keeps its current option, read from the node group sizes. Service Quotas is checked first when the caller may read it | deploy_new_region.sh, eks-stack.ts (8 instance types) |
| S3 bucket and ECR repos are `RETAIN`; `cdk destroy` leaves them and a redeploy then fails with "already exists" | teardown.sh |

## Debugging pointers

- Ray job errors are often wrapped (`Can't pickle RayTaskError`); the real cause is in
  `/tmp/ray/session_latest/logs/worker-*.err` on the pod.
- Cluster resources: `kubectl -n cosmos-curator exec cosmos-curator-0 -- pixi run --as-is ray status`
- Caption proxy (Bedrock) logs: `kubectl -n cosmos-curator logs -l app=caption-proxy`
