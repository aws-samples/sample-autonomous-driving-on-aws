# Natural-language driving scenarios on Amazon EKS: SOTIF ontology, SUMO, and Cosmos HDMap on CPU

Describe a driving test situation in plain language ("a child runs out from between parked cars at
night") and get a set of physically checked scenario variants, each with an HDMap condition video for
NVIDIA Cosmos. An agent on Amazon Bedrock AgentCore reads the request, a Claude model on Amazon Bedrock
writes variants constrained by a SOTIF (ISO 21448) ontology in Amazon Neptune, SUMO checks each variant,
and the Cosmos-Drive-Dreams toolkit renders the HDMap video. Every stage runs as a Kubernetes Job on
Spot CPU nodes; **no GPU is needed**.

The output is a **condition video** (lanes, crosswalk, and actor boxes), the input that Cosmos Transfer
turns into photorealistic footage. Photorealistic generation needs a GPU stage that this sample does not
include.

[한국어](README.ko.md)

## Architecture

```
 Browser ── CloudFront + WAF ── internal ALB (VPC origin)
                                  ├─ /              web (static pages)
                                  └─ /api/pipeline  pipeline API ──> Kubernetes Jobs on cpu-spot nodes
                                                      │   ▲            sumo -> rdshq -> hdmap
                                     chat requests    │   │ tools
                                                      ▼   │
                                        AgentCore Runtime (planner agent, in the VPC)

 Pipeline API ── Amazon Bedrock (Claude): variant generation
              ── Amazon Neptune: SOTIF ontology (IAM auth)
              ── Amazon S3: batch state, SUMO runs, RDS-HQ shards, videos, gallery
```

| Stage | What it does | Runs on | Measured (3 variants) |
|---|---|---|---|
| 1. Ontology | 23 SOTIF classes, 134 instances, 99 relations (`sotif/ontology.nt`) loaded into Neptune | Neptune `db.t4g.medium` | loaded once per file change |
| 2. Plan and generate | The agent calls `create_batch`; the API reads 9 classes from Neptune and asks Claude for 3-10 variants (pedestrian type, timing, speed, occlusion, weather, lighting) | AgentCore Runtime, pipeline API | agent 6-9 s, generation 17-22 s |
| 3. SUMO | Runs each variant, measures minimum distance and TTC to the pedestrian, keeps physically plausible ones | EKS Job (Spot) | about 5 s |
| 4. RDS-HQ | Converts SUMO trajectories and the road network into Cosmos RDS-HQ WebDataset shards | EKS Job (Spot) | about 3 s |
| 5. HDMap | `render_from_rds_hq.py` in HDMap mode with Mesa software OpenGL, one Job per variant in parallel | EKS Job (Spot) | 19-24 s per clip, 1280×704 |

A batch of three variants finished in 87-131 s end to end in our runs; the longer runs include waiting for
a Spot node to start.

- **One EKS cluster** (Kubernetes 1.37) runs everything: an On-Demand `web` node group (web, API, cluster add-ons) and a
  `cpu-spot` node group (c7i/m7i/c6i/m6i.4xlarge, 0-5 nodes) that Cluster Autoscaler scales from zero.
- **State is in S3** under `pipeline/<batchId>/`. Job names are deterministic and each Job skips steps
  that already succeeded, so a reclaimed Spot node or an API restart does not lose or duplicate work.
- **The ontology is versioned in git.** `scripts/build_ontology.sh` extends it with Bedrock from CC BY 4.0
  papers (`sotif/sources.json`) and keeps only relations that cite a source (`sotif/provenance.json`); the committed
  relations were checked against the source text (`sotif/SOURCES.md`); you review the
  diff, commit it, and the next deploy loads it into Neptune.
- **Gallery**: finished variants can be published from the generation page to a shared gallery page.

## Prerequisites

- An AWS account and credentials allowed to create VPC, EKS, IAM, S3, ECR, Neptune, CloudFront, WAF,
  Bedrock AgentCore, and CloudFormation resources (the edge stacks are created in us-east-1)
- Access to Claude Sonnet on Amazon Bedrock through the inference profile
  `global.anthropic.claude-sonnet-5-5`, in a region where Bedrock AgentCore Runtime is available
- EC2 quota for up to 5 × 16-vCPU Spot instances (80 vCPU) and 2 × m7i.large On-Demand
- A Linux build host with Docker (about 20 GB free disk; the four images total about 7.6 GB), AWS CLI v2, Python 3.11
  or later, Node.js 22 or later, and git; [uv](https://docs.astral.sh/uv/) for `scripts/security_scan.sh`
- The public CIDR of your network, to allow through the WAF

## Quick start

```bash
git clone https://github.com/aws-samples/sample-autonomous-driving-on-aws.git
cd sample-autonomous-driving-on-aws/sotif-scenario-generation-on-eks

# Empty region -> all stacks and images -> agent request -> 3 variants -> videos -> gallery check
ALLOWED_CIDRS=203.0.113.0/24 scripts/e2e.sh ap-northeast-2

# Remove everything, including the retained S3 buckets (and their data) and ECR repositories
scripts/teardown.sh ap-northeast-2
```

`e2e.sh` adds the build host's public IP to `ALLOWED_CIDRS` so the check can reach the site, runs
`scripts/deploy.sh`, and then `scripts/verify_pipeline.py` against the CloudFront URL. It can be re-run;
finished steps are skipped. Open the printed URL to use the generation page (`/?lang=en`) and the gallery
(`/gallery-en.html`).

Set `STAGE=<name>` on every script to deploy a second, independent copy in the same account
(`DriveAtlas<Name>*` stacks, `drive-atlas-<name>-*` resources).

### What the steps do

| Script | Does |
|---|---|
| `scripts/deploy.sh <region>` | CDK bootstrap (region and us-east-1); Network (VPC, 1 NAT gateway) and Data (S3 bucket, ECR repositories); builds and pushes the four images; EKS and Neptune; Web (ALB, CloudFront with a VPC origin, WAF in us-east-1); Pipeline (API, AgentCore Runtime, ontology load Job) |
| `scripts/build_and_push_images.sh <region> [tag]` | Builds `web`, `pipeline` (SUMO), `cosmos-hdmap`, and `pipeline-api` |
| `scripts/verify_pipeline.py <url>` | Fails unless the agent starts a batch, the ontology came from Neptune and matches the committed one, every kept variant has a video, telemetry and an OpenSCENARIO zip, and gallery publish and unpublish work; prints per-stage timings |
| `scripts/build_ontology.sh` | Extends the ontology with Bedrock; `--load <region>` loads the committed file into Neptune |
| `scripts/teardown.sh <region>` | Deletes every stack of the stage, the resources CloudFormation retains (both S3 buckets with all objects, ECR repositories), and the log groups that EKS, Neptune and AgentCore create |
| `scripts/check_openscenario.sh <zip>` | Plays an exported OpenSCENARIO zip in esmini and compares the replay with the SUMO judgement |
| `scripts/security_scan.sh` | ASH on the source and on the synthesized CloudFormation templates; fails on any finding without a recorded reason |

Options for `deploy.sh` (environment variables): `ALLOWED_CIDRS`, `STAGE`, `EKS_PUBLIC_CIDRS` (open
the EKS API to these CIDRs for kubectl; private by default), `TAG` (image tag; default git short SHA).

## Using the pipeline

From the generation page, type a request in Korean or English. The page shows the agent's reply and
then follows the batch: the generated variants with their rationale and ontology tags, SUMO's judgement
(minimum distance, TTC, whether the ego vehicle yielded), and the HDMap video for each kept variant.
The same flow is available through the API:

```bash
URL=https://<distribution>.cloudfront.net
curl -s -X POST $URL/api/pipeline/chat -H 'content-type: application/json' \
  -d '{"message": "A wheelchair user crossing in heavy rain, 4 variants", "lang": "en"}'
curl -s $URL/api/pipeline/batches/<batchId>
```

The scene is fixed: an urban mid-block zebra crossing where the ego vehicle must yield to pedestrians.
For requests outside it, the agent says so and offers the closest crosswalk variant.

### OpenSCENARIO export

Every variant that SUMO could simulate is also written as `pipeline/<batchId>/openscenario/<variantId>.zip`
(the **Download OpenSCENARIO** link on the generation page and in the gallery, or `openscenarioUrl` in the API):

| File | Contents |
|---|---|
| `road.xodr` | OpenDRIVE 1.6: the SUMO road (2 + 2 lanes, 2 m sidewalks, zebra crossing at x = 200 m) |
| `scenario.xosc` | OpenSCENARIO XML 1.1 replay: every entity, the ego included, follows its SUMO trajectory (0.1 s vertices) |
| `scenario_sut.xosc` | The same scene for a system under test: the ego only gets its start pose and speed, so your controller decides whether it yields |
| `variant.json` | Variant parameters and the SUMO judgement |

`scripts/check_openscenario.sh <zip>` plays both files in esmini v3.8.1 (headless, in Docker) and fails unless the
replay reproduces the SUMO judgement: in our tests the minimum ego-to-pedestrian distance matched to 0.01 m and
positions to within 0.08 m. Without a controller the ego in `scenario_sut.xosc` keeps its speed and can hit the
pedestrian; that is the situation the system under test has to handle.

## Cost

Main cost drivers, to be priced with your region's current rates:

| Item | Formula |
|---|---|
| Always on while the stacks exist | EKS control plane, 1 × m7i.large (web node group), Neptune `db.t4g.medium`, NAT gateway (hourly + data), internal ALB, CloudFront requests, WAF web ACL with two AWS managed rule groups, S3 storage, CloudWatch Logs ingestion (VPC flow logs, EKS control plane, Neptune audit) |
| Per batch | Spot node hours while Jobs run (idle nodes are removed after about 5 minutes) + Bedrock tokens (about 4,400 input and 2,300-2,600 output tokens to generate 3 variants, plus a few thousand for the agent) + AgentCore Runtime usage |

Scaling to zero does not stop the always-on items; run `scripts/teardown.sh` when you are done. See
[Amazon Bedrock pricing](https://aws.amazon.com/bedrock/pricing/),
[Amazon EKS pricing](https://aws.amazon.com/eks/pricing/), and
[Amazon Neptune pricing](https://aws.amazon.com/neptune/pricing/).

## Security

This is a sample, not a production deployment. Review these before using it with real data:

- **Who can use the site**: the WAF allows only `ALLOWED_CIDRS`. The API has no per-user
  authorization: anyone who gets through can start batches (which consume Bedrock tokens and Spot
  capacity) and publish or remove gallery items.
- **Network**: CloudFront reaches an internal ALB through a VPC origin; the ALB's security group admits
  only the CloudFront origin-facing prefix list. The EKS API endpoint is private unless you set
  `EKS_PUBLIC_CIDRS`. Neptune is in private subnets and requires IAM authentication.
- **WAF**: AWS managed rule groups for known bad inputs (including Log4j JNDI lookups) and IP
  reputation run before the allowlist, so they apply to allowed CIDRs too.
- **Workload permissions**: the web pod has no AWS credentials and no service account token. The
  pipeline API (EKS Pod Identity) can read and write only the `pipeline/` and `gallery/`
  prefixes of the bucket, query Neptune, call the configured Bedrock model, invoke its agent, and create
  Jobs through a namespaced Kubernetes Role; the stage Jobs can use only `pipeline/`. The agent role can
  invoke only the configured model and write its logs and traces.
- **S3**: the buckets block public access, are encrypted with S3-managed keys, and deny non-TLS
  requests. Videos are served through presigned URLs that expire after one hour.
- **Logging**: VPC flow logs of rejected traffic (30 days; set `FlowLogTrafficType.ALL` in `network_stack.py` for full traffic), all five EKS control plane log types, Neptune audit logs, and
  S3, ALB and CloudFront access logs (a separate log bucket, 90 days) are on.
- **Checks**: [cdk-nag](https://github.com/cdklabs/cdk-nag) `AwsSolutionsChecks` runs on every synth and
  fails it on any unacknowledged finding; each acknowledgement and checkov exception is in
  `cdk/drive_atlas_infra/checks.py` or next to the resource, with its reason (mostly roles and handlers
  that the CDK EKS module creates, and the default CloudFront certificate). `scripts/security_scan.sh`
  runs [ASH](https://github.com/awslabs/automated-security-helper) (bandit, semgrep, checkov,
  detect-secrets, cdk-nag, npm audit) on the source and on the synthesized templates.
- **Bedrock data location**: the global inference profile can process requests in other regions. To keep
  requests in one region, use a regional profile and update the IAM policy in `cdk/drive_atlas_infra/pipeline_stack.py`.
- `teardown.sh` deletes both S3 buckets **including all data and logs**.

See [CONTRIBUTING](../CONTRIBUTING.md#security-issue-notifications) for reporting security issues.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `403` from CloudFront | Your IP is not in `ALLOWED_CIDRS` | Redeploy with your CIDR added (WAF rules live in the us-east-1 edge stack) |
| Batch stays at `sumo` or `hdmap` for minutes | Cluster Autoscaler is starting a Spot node, or Spot capacity is short | Wait; the Job starts when the node is ready |
| Old diagram or page after a redeploy | Browser cache from a version before `Cache-Control: no-cache` was added | Hard reload |
| `teardown.sh` leaves the agent security group and the Network stack | AgentCore releases the VPC network interfaces of a deleted runtime late (hours in our test) | Everything else is already deleted; re-run `scripts/teardown.sh` later. Until then the VPC counts against the per-region VPC quota |

## Deploy with a coding agent

[`CLAUDE.md`](CLAUDE.md) describes how a coding agent should deploy, validate, and debug this sample. Open the
sample in a coding agent such as [Claude Code](https://claude.com/claude-code) and ask, for example, "Deploy to
ap-northeast-2 and run the end-to-end test." The agent runs `scripts/e2e.sh` and treats the run as passed only when
`scripts/verify_pipeline.py` reports that every check passed.

## Validation

Tested on 2026-11-16 in ap-northeast-1 from the exported sample (no git checkout): `scripts/e2e.sh` from an empty
region (Kubernetes 1.37 with the `aws_eks_v2` module and EKS Pod Identity, Neptune 1.5.0.0, KMS-encrypted Kubernetes secrets) deployed in 51 minutes and
`scripts/verify_pipeline.py` passed every check. The agent started a batch in 7.6 s; generation took 19.9 s, SUMO 5.1 s,
RDS-HQ 2.4 s, and three HDMap videos 19.9-20.9 s in parallel, 92 s in total, with all three variants kept. Cluster
Autoscaler scaled `cpu-spot` from zero for the Jobs. After an ontology change, re-running `scripts/e2e.sh` updated the
stage in under 5 minutes and the check that Neptune holds the committed ontology passed. `scripts/teardown.sh` removed an
earlier stage in the same region completely, after AgentCore released the agent's network interfaces (see
Troubleshooting). `scripts/security_scan.sh` reports no unacknowledged findings.

## Limitations

- One scene (urban mid-block crosswalk). Other scenes need a new road template in
  `scripts/compile_sumo.py` and a variant space like `augment/crosswalk.py`.
- HDMap videos are condition inputs, not photorealistic footage.
- LLM-proposed ontology relations are validated against the schema and required to cite a source, but
  their content is not reviewed automatically; review the diff before committing. The relations in this repository
  were checked by hand against the source text.

## Project structure

```
.
├── agent/          planner agent for AgentCore Runtime (standard library, boto3, urllib3)
├── augment/        crosswalk variant space and the ontology slice it reads
├── cdk/            AWS CDK app (Python): Network, Data, EKS, Neptune, Web, Pipeline, and edge WAF stacks
├── docker/         images: web, pipeline (SUMO), cosmos-hdmap (toolkit on CPU), pipeline-api
├── pipeline_api/   FastAPI service: batches, Kubernetes Jobs, gallery
├── scripts/        deploy, teardown, e2e, image build, stage Jobs, ontology build, verification
├── sotif/          SOTIF ontology, schema, sources, and the LLM build
├── tests/          unit tests (pytest)
└── web/            generation page, gallery, architecture diagram
```

## License

This sample is licensed under the MIT-0 License. See [LICENSE](../LICENSE). Third-party components that
are downloaded at build or run time, and the papers the ontology cites, are listed in [NOTICE](NOTICE).
