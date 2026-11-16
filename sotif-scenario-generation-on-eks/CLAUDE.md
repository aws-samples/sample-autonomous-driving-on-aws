# CLAUDE.md — deploying and validating SOTIF scenario generation on EKS

Instructions for [Claude Code](https://claude.com/claude-code) (or any coding agent) working in this sample.
A user can open it in a coding agent and ask, for example, "deploy to ap-northeast-2 and run the end-to-end
test", and the agent should follow this file.

## The supported way to run

```bash
ALLOWED_CIDRS=<user's CIDR> [STAGE=<name>] scripts/e2e.sh <region>   # empty region -> verified pipeline
[STAGE=<name>] scripts/teardown.sh <region>                           # remove everything, incl. RETAIN S3/ECR
```

`e2e.sh` chains these steps; each is safe to re-run and skips finished work:

1. `scripts/deploy.sh` — CDK bootstrap (region and us-east-1), Network and Data, the four images
   (`scripts/build_and_push_images.sh`), EKS and Neptune, Web (ALB, CloudFront VPC origin, WAF in us-east-1),
   Pipeline (pipeline API, AgentCore Runtime, ontology load Job)
2. `scripts/verify_pipeline.py <CloudFront URL>` — through CloudFront: agent request -> batch -> ontology from
   Neptune -> per kept variant an HDMap video, telemetry and an OpenSCENARIO zip -> gallery publish/unpublish

**Done** means `verify_pipeline.py` printed `all checks passed` against a stage that was empty before the run.
Infrastructure being up is not a pass. When preparing a release, validate the exported tree (the files users get),
not a working copy with extra files.

Before starting, confirm with the user: the region (Bedrock AgentCore Runtime and the
`global.anthropic.claude-sonnet-5-5` inference profile must be available), the CIDR to allow through the WAF
(`e2e.sh` adds the build host's own public IP), that the build host is Linux x86_64 with Docker and about 20 GB free,
and that the region has a free VPC (the default quota is 5). Ask before running `teardown.sh`: it deletes both S3
buckets with all batches, gallery items and logs.

Optional checks after a pass: `scripts/check_openscenario.sh <zip>` (esmini replay of a downloaded OpenSCENARIO zip)
and `scripts/security_scan.sh` (ASH on the source and the synthesized templates; needs `uv`).

## Rules

- **Fix the code, not the account.** If a step fails, change the script, CDK app or image, then re-run (from
  `teardown.sh` if the failure left partial state). A manual fix (kubectl patch, console edit) breaks the next deploy.
- `cdk synth` runs cdk-nag. Fix a finding when possible; acknowledge it only with a reason
  (`cdk/drive_atlas_infra/checks.py`, IAM wildcards by regex) when it cannot be fixed here.
- Don't pipe long-running commands through `| tail` or `| grep` when the exit code matters.
- The EKS API endpoint is private. kubectl works only from inside the VPC, or after deploying with
  `EKS_PUBLIC_CIDRS=<cidr>`; otherwise inspect through CloudFormation, the ALB target group and CloudWatch Logs.
- After changing scripts, CDK code or images, confirm with `scripts/e2e.sh` on an empty stage.

## Known traps (handled in the code — keep them)

| Trap | Where handled |
|---|---|
| AgentCore releases a deleted VPC runtime's network interfaces hours later; until then the agent security group and the VPC cannot be deleted (and the VPC counts against the quota) | teardown.sh deletes everything else first, waits `ENI_WAIT_MIN`, otherwise asks to re-run later |
| CloudFormation only deletes the agent stack when the security group is left out | teardown.sh `--retain-resources` for the failed resource |
| The AgentCore code zip must contain no `.pyc` from the build host's Python, and must change whenever the bundle changes | pipeline_stack.py: `--no-compile`, `AssetHashType.OUTPUT`, `--python-version` matching the runtime |
| A Kubernetes Job is immutable; the ontology load Job name includes the image tag so a new image creates a new Job | pipeline_stack.py |
| Presigned S3 URLs signed for the global endpoint get a 307 redirect for new buckets and fail the signature | pipeline_api/batches.py uses the regional endpoint |
| The ALB controller's webhook rejects Services until its pods are ready, so later Helm charts fail | eks_stack.py: chart dependencies on the controller |
| Removing `serviceAccountName` from a Deployment keeps the deprecated `serviceAccount` field; new pods then reference a deleted account and never start (ALB 503) | web_stack.py sets `serviceAccountName: default` explicitly |
| CloudFormation refuses changes that replace a named resource (for example ECR encryption on `drive-atlas-*` repositories, or EKS secrets encryption on the cluster); deleting the resource first does not help and leaves the stack without it | change such properties only on a new stage (or tear the stage down first); both are set when the stage is created |
| EKS upgrades one minor version at a time; jumping several versions in the CDK app fails | redeploy a new stage instead, or step through versions |
| Cluster Autoscaler's image tag must match the Kubernetes minor version | eks_stack.py |
| `pip check` reports decord's manylinux2010 wheel as unsupported although it imports fine | docker/cosmos-hdmap/Dockerfile filters that one line and imports the modules |
| The Kubernetes Python client 37.0.2 needs python-dateutil 3, botocore needs < 3 | docker/pipeline-api/Dockerfile pins 37.0.1 |
| A rising-edge time condition at t = 0 can be missed in esmini, and time conditions fire one step late | augment/openscenario.py uses edge `none` and starts entities one SUMO step early |
| SUMO already writes parked cars into the telemetry; adding them again from the variant duplicates entity names | augment/openscenario.py |
| Browsers keep an old diagram or page after a redeploy | docker/web/serve.py sends `Cache-Control: no-cache` |
| Spot capacity can be reclaimed during a batch; Jobs have deterministic names and skip finished steps | pipeline_api/jobs.py, scripts/pipeline_job.py |

## Debugging pointers

- Batch state: `GET /api/pipeline/batches/<batchId>` (timeline with each stage's error), S3 `pipeline/<batchId>/status/`.
- Web 503: `aws elbv2 describe-target-health` for the stack's target group; no targets means the web pod is not running.
- Agent: CloudWatch Logs `/aws/bedrock-agentcore/runtimes/<runtime>-DEFAULT`; pipeline API and Jobs log to stdout
  (Container Insights is not enabled; use kubectl from inside the VPC).
