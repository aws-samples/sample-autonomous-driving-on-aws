"""Durable state: S3 artifact bucket (batch state, pipeline outputs, gallery), access-log bucket, ECR repositories.

All are retained on stack deletion so results and images survive infra changes; scripts/teardown.sh removes them.
"""

from aws_cdk import Duration, RemovalPolicy, Stack
from typing import Sequence

from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from constructs import Construct

from drive_atlas_infra.checks import checkov, nag


def grant_prefixes(bucket: s3.IBucket, principal: iam.IPrincipal, prefixes: Sequence[str], delete: bool = False) -> None:
    """Object read/write under the given key prefixes only. ListBucket stays bucket-wide: without it S3 answers 403
    instead of NoSuchKey for a missing key, which the pipeline relies on."""
    actions = ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload"] + (["s3:DeleteObject"] if delete else [])
    principal.add_to_principal_policy(iam.PolicyStatement(actions=actions, resources=[bucket.arn_for_objects(f"{p}*") for p in prefixes]))
    principal.add_to_principal_policy(iam.PolicyStatement(actions=["s3:ListBucket"], resources=[bucket.bucket_arn]))


class DataStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, name_prefix: str = "drive-atlas", **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # S3 server access logs, ALB access logs (logs/alb/) and CloudFront standard logs (logs/cloudfront/).
        # CloudFront standard logging writes with ACLs, so this bucket (only) keeps object ACLs on.
        self.log_bucket = s3.Bucket(
            self,
            "AccessLogBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            object_ownership=s3.ObjectOwnership.OBJECT_WRITER,
            versioned=True,
            lifecycle_rules=[s3.LifecycleRule(id="expire-logs", expiration=Duration.days(90), noncurrent_version_expiration=Duration.days(1))],
            removal_policy=RemovalPolicy.RETAIN,
        )
        # S3 requires a log target bucket to exist before logging to it, so two buckets cannot log into each other in
        # one deployment, and logging into itself would loop.
        nag(self.log_bucket, "AwsSolutions-S1", "This is the access-log bucket itself; see the comment above.")
        checkov(self.log_bucket, "CKV_AWS_18", "This is the access-log bucket itself; see the comment above.")

        self.bucket = s3.Bucket(
            self,
            "ArtifactBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            versioned=True,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            server_access_logs_bucket=self.log_bucket,
            server_access_logs_prefix="logs/s3/",
            lifecycle_rules=[
                s3.LifecycleRule(id="expire-noncurrent", noncurrent_version_expiration=Duration.days(30)),
                s3.LifecycleRule(id="abort-multipart", abort_incomplete_multipart_upload_after=Duration.days(7)),
            ],
            removal_policy=RemovalPolicy.RETAIN,
        )

        self.repository = ecr.Repository(
            self,
            "WebRepository",
            repository_name=f"{name_prefix}/web",
            image_scan_on_push=True,
            image_tag_mutability=ecr.TagMutability.IMMUTABLE,
            encryption=ecr.RepositoryEncryption.KMS,  # AWS managed key aws/ecr
            lifecycle_rules=[ecr.LifecycleRule(description="keep last 20 images", max_image_count=20)],
            removal_policy=RemovalPolicy.RETAIN,
        )

        # Pipeline images: Job stages (SUMO + RDS-HQ, Cosmos HDMap) and the pipeline API.
        self.pipeline_repositories = {
            name: ecr.Repository(
                self,
                construct_id,
                repository_name=f"{name_prefix}/{name}",
                image_scan_on_push=True,
                image_tag_mutability=ecr.TagMutability.IMMUTABLE,
                encryption=ecr.RepositoryEncryption.KMS,
                lifecycle_rules=[ecr.LifecycleRule(description="keep last 10 images", max_image_count=10)],
                removal_policy=RemovalPolicy.RETAIN,
            )
            for name, construct_id in (("pipeline", "PipelineRepository"), ("cosmos-hdmap", "CosmosHdmapRepository"), ("pipeline-api", "PipelineApiRepository"))
        }

