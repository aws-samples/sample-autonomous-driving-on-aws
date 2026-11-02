# CDK app

Three stacks, deployed by `scripts/deploy_new_region.sh` (or `scripts/e2e.sh`), which also passes
the context values below. Kubernetes resources (config, caption proxy, Curator Helm release) are
managed by `scripts/setup_curator_pod.sh`, not by CDK.

| Stack | Creates |
|---|---|
| `CosmosCurateCore` | VPC (3 AZs, 1 NAT gateway) or an existing VPC (`-c vpcId=`), S3 bucket `cosmos-curate-<account>-<region>`, ECR repositories `cosmos-curate/curator-cpu` and `cosmos-curate/caption-proxy` (bucket and repositories are retained on delete) |
| `CosmosCurateEks` | EKS 1.31 cluster, Spot and on-demand node groups (8 x 16-vCPU instance types, 120 GB disks), Pod Identity agent, IAM role for the `cosmos-curator/default` service account (S3, Bedrock, OpenSearch Serverless), `cosmos-curator` namespace |
| `CosmosCurateOpenSearch` | OpenSearch Serverless vector search collection `cosmos-curator` with data access for the cluster and pod roles |

Context values:

| Key | Default | Meaning |
|---|---|---|
| `spotDesiredSize` | 3 | Spot nodes |
| `onDemandDesiredSize` | 1 | On-demand nodes |
| `dedicatedOnDemand` | `false` | Taint on-demand nodes `dedicated=curator:NoSchedule` (option `b`) |
| `kubectlRole` | none | IAM role name to map into aws-auth as cluster admin |
| `vpcId` | none | Use an existing VPC |
| `publicAccessCidrs` | none | Comma-separated CIDRs allowed to reach the public EKS API endpoint (`PUBLIC_ACCESS_CIDRS` in the scripts) |

Build check without credentials for an account:

```bash
npm ci
npx tsc --noEmit
CDK_DEFAULT_REGION=us-west-2 npx cdk synth
```

`cdk synth` runs cdk-nag `AwsSolutionsChecks` and fails on any unsuppressed finding. Suppressions
and their reasons are in `bin/cdk.ts`.

Use `scripts/teardown.sh <region>` instead of `cdk destroy`: it also removes the retained bucket and
repositories and handles EKS custom resources that fail to delete.
