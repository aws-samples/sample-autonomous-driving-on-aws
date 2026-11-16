#!/usr/bin/env python3
"""SOTIF Scenario Generation on AWS (CDK app). Account comes from the caller's credentials; nothing account-specific is committed.

    scripts/deploy.sh <region>                       # everything, in order (bootstrap, images, stacks)

Context (-c key=value):
    drive-atlas:region                region for every stack except the CloudFront edge stacks (us-east-1)
    drive-atlas:stage                 optional name prefix, e.g. "eks" -> stacks DriveAtlasEks*, resources drive-atlas-eks-*;
                                      lets several copies live in one account
    drive-atlas:allowedCidrs          comma-separated CIDRs allowed through the CloudFront WAF (required)
    drive-atlas:eksPublicAccessCidrs  kubectl from outside the VPC (default: private API endpoint only)
    drive-atlas:gpuMaxNodes           create the GPU node group with this many nodes at most (default 0: no GPU group)
    imageTag / pipelineImageTag       ECR image tags (scripts/build_and_push_images.sh)

"""

import json
import os
from pathlib import Path

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks

from drive_atlas_infra.checks import LambdaHandlers
from drive_atlas_infra.data_stack import DataStack
from drive_atlas_infra.edge_waf_stack import EdgeWafStack
from drive_atlas_infra.eks_stack import EksStack
from drive_atlas_infra.naming import Naming
from drive_atlas_infra.neptune_stack import NeptuneStack
from drive_atlas_infra.network_stack import NetworkStack
from drive_atlas_infra.pipeline_stack import PipelineStack
from drive_atlas_infra.web_stack import WebStack

HERE = Path(__file__).parent

app = cdk.App()
ctx = app.node.try_get_context
names = Naming(ctx("drive-atlas:stage") or "")
account = os.environ.get("CDK_DEFAULT_ACCOUNT")
env = cdk.Environment(account=account, region=ctx("drive-atlas:region"))
edge_env = cdk.Environment(account=account, region="us-east-1")


def _list(key: str):
    return [item.strip() for item in (ctx(key) or "").split(",") if item.strip()]


allowed_cidrs = _list("drive-atlas:allowedCidrs")
if not allowed_cidrs and (HERE / "allowlist.local.json").exists():
    allowed_cidrs = json.loads((HERE / "allowlist.local.json").read_text())["cidrs"]

network = NetworkStack(app, names.stack("Network"), env=env, vpc_cidr=ctx("drive-atlas:vpcCidr"))
data = DataStack(app, names.stack("Data"), env=env, name_prefix=names.prefix)

eks = EksStack(
    app, names.stack("Eks"), env=env,
    vpc=network.vpc, bucket=data.bucket, cluster_name=names.prefix,
    public_access_cidrs=_list("drive-atlas:eksPublicAccessCidrs"),
    gpu_max_nodes=int(ctx("drive-atlas:gpuMaxNodes") or 0),
)

# CloudFront-scope WAF (us-east-1): AWS managed rules, then only the allowlisted CIDRs.
if not allowed_cidrs:
    raise SystemExit("set -c drive-atlas:allowedCidrs=<cidr,...> (or cdk/allowlist.local.json): the site is open only to these CIDRs")
waf = EdgeWafStack(app, names.stack("EdgeWaf"), env=edge_env, allowed_cidrs=allowed_cidrs, cross_region_references=True)

web = WebStack(
    app, names.stack("Web"), env=env, cross_region_references=True,
    vpc=network.vpc, cluster=eks.cluster, namespace=eks.namespace, repository=data.repository, log_bucket=data.log_bucket,
    image_tag=ctx("imageTag") or "unset", web_acl_arn=waf.web_acl.attr_arn,
)

neptune = NeptuneStack(app, names.stack("Neptune"), env=env, vpc=network.vpc, identifier=f"{names.prefix}-ontology")

pipeline = PipelineStack(
    app, names.stack("Pipeline"), env=env,
    vpc=network.vpc, cluster=eks.cluster, namespace=eks.namespace, bucket=data.bucket,
    alb=web.alb, alb_sg=web.alb_sg, listener=web.listener,
    repositories=data.pipeline_repositories, image_tag=ctx("pipelineImageTag") or "unset",
    neptune_sparql_url=neptune.sparql_url, neptune_data_arn=neptune.data_arn, neptune_sg=neptune.security_group,
    runtime_name=names.underscored("planner"),
)
pipeline.add_dependency(neptune)

for stack in app.node.children:
    if isinstance(stack, cdk.Stack):
        cdk.Tags.of(stack).add("project", names.prefix)

# Every synth runs the AWS Solutions rules; exceptions carry their reason (drive_atlas_infra/checks.py).
cdk.Aspects.of(app).add(LambdaHandlers())
cdk.Aspects.of(app).add(AwsSolutionsChecks())

app.synth()
