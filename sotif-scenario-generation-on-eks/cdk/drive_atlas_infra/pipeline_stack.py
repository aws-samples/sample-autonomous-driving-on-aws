"""Scenario pipeline: pipeline API on EKS (creates the stage Jobs) and the planner agent on AgentCore Runtime.

    browser --CloudFront--> ALB /api/pipeline/* --> pipeline API (EKS) --> Jobs on cpu-spot nodes
                                                        |  chat
                                                        v
                                  AgentCore Runtime (VPC) --tools--> ALB /api/pipeline/*
"""

import hashlib
import shutil
import subprocess
from pathlib import Path
from typing import Dict

import jsii
from aws_cdk import AssetHashType, BundlingOptions, CfnOutput, DockerImage, Duration, ILocalBundling, Stack
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_eks_v2 as eks
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_assets as s3_assets
from constructs import Construct

from drive_atlas_infra.checks import nag
from drive_atlas_infra.data_stack import grant_prefixes

from drive_atlas_infra.eks_stack import BATCH_TAINT_KEY, NAMESPACE
from drive_atlas_infra.neptune_stack import PORT as NEPTUNE_PORT

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_SOURCE = REPO_ROOT / "agent"
ONTOLOGY_FILE = REPO_ROOT / "sotif" / "ontology.nt"
API_PORT = 8080
APP = "pipeline-api"


@jsii.implements(ILocalBundling)
class _AgentBundle:
    """main.py + boto3 (pure Python), so the zip runs on the arm64 AgentCore runtime without an arm64 build host."""

    def try_bundle(self, output_dir: str, *, image=None, **_kwargs) -> bool:
        # cdk.json runs the app with .venv/bin/python, so this is the interpreter running this code.
        subprocess.run([".venv/bin/python", "-m", "pip", "install", "--quiet", "--no-compile", "--target", output_dir,
                        "--only-binary=:all:", "--platform", "manylinux2014_aarch64", "--python-version", "3.13",
                        "--implementation", "cp", "boto3==1.44.0"], check=True)
        # AgentCore rejects artifacts with bytecode compiled for another Python; console scripts are not needed either.
        out = Path(output_dir)
        for cache in list(out.rglob("__pycache__")):
            shutil.rmtree(cache, ignore_errors=True)
        shutil.rmtree(out / "bin", ignore_errors=True)
        shutil.copy(AGENT_SOURCE / "main.py", out / "main.py")
        return True


class PipelineStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        vpc: ec2.IVpc,
        cluster: eks.Cluster,
        namespace: eks.KubernetesManifest,
        bucket: s3.IBucket,
        alb: elbv2.IApplicationLoadBalancer,
        alb_sg: ec2.ISecurityGroup,
        listener: elbv2.IApplicationListener,
        repositories: Dict[str, ecr.IRepository],
        image_tag: str,
        neptune_sparql_url: str,
        neptune_data_arn: str,
        neptune_sg: ec2.ISecurityGroup,
        runtime_name: str = "drive_atlas_planner",
        model_id: str = "global.anthropic.claude-sonnet-5-5",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        bedrock_models = iam.PolicyStatement(
            actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
            resources=[
                f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/{model_id}",
                "arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-5-5*",
                "arn:aws:bedrock:::foundation-model/anthropic.claude-sonnet-5-5*",
            ],
        )

        runtime = self._agent_runtime(vpc, alb, alb_sg, model_id, bedrock_models, runtime_name)

        # Pods use the cluster security group on the node ENIs.
        ec2.CfnSecurityGroupIngress(
            self, "NeptuneFromCluster", group_id=neptune_sg.security_group_id, ip_protocol="tcp",
            from_port=NEPTUNE_PORT, to_port=NEPTUNE_PORT, source_security_group_id=cluster.cluster_security_group_id,
            description="EKS pods (pipeline API, ontology loader)",
        )
        neptune_env = {"DRIVE_ATLAS_NEPTUNE_SPARQL": neptune_sparql_url, "DRIVE_ATLAS_NEPTUNE_IAM": "1"}
        self._ontology_loader(cluster, namespace, repositories["pipeline"], image_tag, neptune_env, neptune_data_arn)

        # Pipeline API identity: S3 batch state, Bedrock variant generation, agent invocation, Job creation (RBAC below).
        service_account = eks.ServiceAccount(self, "ApiSa", cluster=cluster, name=APP, namespace=NAMESPACE,
                                             identity_type=eks.IdentityType.POD_IDENTITY)
        service_account.node.add_dependency(namespace)
        grant_prefixes(bucket, service_account, ["pipeline/", "gallery/"], delete=True)
        service_account.add_to_principal_policy(bedrock_models)
        service_account.add_to_principal_policy(iam.PolicyStatement(actions=["neptune-db:ReadDataViaQuery"], resources=[neptune_data_arn]))
        service_account.add_to_principal_policy(iam.PolicyStatement(
            actions=["bedrock-agentcore:InvokeAgentRuntime"],
            resources=[runtime.attr_agent_runtime_arn, f"{runtime.attr_agent_runtime_arn}/*"],
        ))
        nag(service_account, "AwsSolutions-IAM5", "Wildcards only where the ARN cannot be known: object keys under pipeline/ and gallery/ "
            "of this bucket, Neptune IAM auth (cluster resource id/*), the global inference profile's foundation model in any "
            "region, and this agent runtime's endpoints (runtime ARN/*).",
            [r"^Resource::.*/(pipeline|gallery)/\*$", r"^Resource::arn:aws:neptune-db:.*/\*$",
             r"^Resource::arn:aws:bedrock:\*?::foundation-model/.*\*$", r"^Resource::<PlannerAgent\.AgentRuntimeArn>/\*$"])

        target_group = elbv2.ApplicationTargetGroup(
            self,
            "ApiTargets",
            vpc=vpc,
            port=API_PORT,
            protocol=elbv2.ApplicationProtocol.HTTP,
            target_type=elbv2.TargetType.IP,
            health_check=elbv2.HealthCheck(path="/api/pipeline/health", healthy_http_codes="200", interval=Duration.seconds(15)),
            deregistration_delay=Duration.seconds(15),
        )
        elbv2.ApplicationListenerRule(
            self,
            "ApiRoute",
            listener=listener,
            priority=10,
            conditions=[elbv2.ListenerCondition.path_patterns(["/api/pipeline/*"])],
            target_groups=[target_group],
        )

        labels = {"app": APP}
        env = {
            "BUCKET": bucket.bucket_name,
            "AWS_REGION": self.region,
            "PIPELINE_IMAGE": repositories["pipeline"].repository_uri_for_tag(image_tag),
            "COSMOS_IMAGE": repositories["cosmos-hdmap"].repository_uri_for_tag(image_tag),
            "AGENT_RUNTIME_ARN": runtime.attr_agent_runtime_arn,
            "VARIANT_MODEL": model_id,
            "MAX_VARIANTS": "10",
            **neptune_env,
        }
        manifest = eks.KubernetesManifest(
            self,
            "ApiApp",
            cluster=cluster,
            manifest=[
                {
                    "apiVersion": "rbac.authorization.k8s.io/v1",
                    "kind": "Role",
                    "metadata": {"name": APP, "namespace": NAMESPACE},
                    "rules": [{"apiGroups": ["batch"], "resources": ["jobs", "jobs/status"],
                               "verbs": ["create", "get", "list", "watch", "delete"]}],
                },
                {
                    "apiVersion": "rbac.authorization.k8s.io/v1",
                    "kind": "RoleBinding",
                    "metadata": {"name": APP, "namespace": NAMESPACE},
                    "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": APP},
                    "subjects": [{"kind": "ServiceAccount", "name": APP, "namespace": NAMESPACE}],
                },
                {
                    "apiVersion": "apps/v1",
                    "kind": "Deployment",
                    "metadata": {"name": APP, "namespace": NAMESPACE, "labels": labels},
                    "spec": {
                        "replicas": 1,
                        # One orchestrator at a time; Job names are deterministic, so an overlap would only cause 409s.
                        "strategy": {"type": "Recreate"},
                        "selector": {"matchLabels": labels},
                        "template": {
                            "metadata": {"labels": labels},
                            "spec": {
                                "serviceAccountName": APP,
                                "nodeSelector": {"workload": "web"},
                                "containers": [{
                                    "name": APP,
                                    "image": repositories["pipeline-api"].repository_uri_for_tag(image_tag),
                                    "ports": [{"containerPort": API_PORT}],
                                    "env": [{"name": k, "value": v} for k, v in env.items()],
                                    "resources": {"requests": {"cpu": "250m", "memory": "512Mi"}, "limits": {"memory": "1Gi"}},
                                    "readinessProbe": {"httpGet": {"path": "/api/pipeline/health", "port": API_PORT}, "periodSeconds": 10},
                                }],
                            },
                        },
                    },
                },
                {
                    "apiVersion": "v1",
                    "kind": "Service",
                    "metadata": {"name": APP, "namespace": NAMESPACE},
                    "spec": {"selector": labels, "ports": [{"port": API_PORT, "targetPort": API_PORT}]},
                },
                {
                    "apiVersion": "elbv2.k8s.aws/v1beta1",
                    "kind": "TargetGroupBinding",
                    "metadata": {"name": APP, "namespace": NAMESPACE},
                    "spec": {
                        "serviceRef": {"name": APP, "port": API_PORT},
                        "targetGroupARN": target_group.target_group_arn,
                        "targetType": "ip",
                        "networking": {"ingress": [{
                            "from": [{"securityGroup": {"groupID": alb_sg.security_group_id}}],
                            "ports": [{"protocol": "TCP", "port": API_PORT}],
                        }]},
                    },
                },
            ],
        )
        manifest.node.add_dependency(service_account)
        manifest.node.add_dependency(cluster.alb_controller)

        CfnOutput(self, "AgentRuntimeArn", value=runtime.attr_agent_runtime_arn)

    def _ontology_loader(self, cluster, namespace, repository, image_tag, neptune_env, neptune_data_arn) -> None:
        """Job that replaces the Neptune graph with sotif/ontology.nt.

        Its name hashes the file and the image tag: a Job's pod template is immutable, so any change must be a new Job
        (CDK prunes the old one), and an unchanged deploy does not reload.
        """
        ontology = s3_assets.Asset(self, "OntologyFile", path=str(ONTOLOGY_FILE))
        loader_sa = eks.ServiceAccount(self, "OntologyLoaderSa", cluster=cluster, name="ontology-loader", namespace=NAMESPACE,
                                       identity_type=eks.IdentityType.POD_IDENTITY)
        loader_sa.node.add_dependency(namespace)
        # The loader only reads this one object (sotif.neptune load s3://...), so no bucket-wide grant_read().
        loader_sa.add_to_principal_policy(iam.PolicyStatement(
            actions=["s3:GetObject"], resources=[ontology.bucket.arn_for_objects(ontology.s3_object_key)]))
        nag(loader_sa, "AwsSolutions-IAM5", "Neptune IAM auth names the cluster resource id, so data access is cluster resource id/*.",
            [r"^Resource::arn:aws:neptune-db:.*/\*$"])
        loader_sa.add_to_principal_policy(iam.PolicyStatement(
            actions=["neptune-db:ReadDataViaQuery", "neptune-db:WriteDataViaQuery", "neptune-db:DeleteDataViaQuery"],
            resources=[neptune_data_arn],
        ))
        digest = hashlib.sha256(ONTOLOGY_FILE.read_bytes() + image_tag.encode()).hexdigest()[:12]
        job = eks.KubernetesManifest(self, "OntologyLoad", cluster=cluster, manifest=[{
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": f"ontology-load-{digest}", "namespace": NAMESPACE, "labels": {"app": "ontology-load"}},
            "spec": {
                "backoffLimit": 6,  # Neptune may still be starting on the first deploy
                "template": {"spec": {
                    "serviceAccountName": "ontology-loader",
                    "restartPolicy": "Never",
                    "nodeSelector": {"workload": "cpu"},
                    "tolerations": [{"key": BATCH_TAINT_KEY, "operator": "Equal", "value": "true", "effect": "NoSchedule"}],
                    "containers": [{
                        "name": "load",
                        "image": repository.repository_uri_for_tag(image_tag),
                        "command": ["python3", "-m", "sotif.neptune", "load", ontology.s3_object_url],
                        "env": [{"name": k, "value": v} for k, v in {**neptune_env, "AWS_REGION": self.region}.items()],
                        "resources": {"requests": {"cpu": "250m", "memory": "256Mi"}},
                    }],
                }},
            },
        }])
        job.node.add_dependency(loader_sa)

    def _agent_runtime(self, vpc, alb, alb_sg, model_id, bedrock_models, runtime_name) -> agentcore.CfnRuntime:
        agent_sg = ec2.SecurityGroup(self, "AgentSg", vpc=vpc, description="Drive Atlas planner agent (AgentCore Runtime ENIs)", allow_all_outbound=True)
        # The agent reaches the pipeline API through the internal ALB.
        ec2.CfnSecurityGroupIngress(
            self, "AlbFromAgent", group_id=alb_sg.security_group_id, ip_protocol="tcp", from_port=80, to_port=80,
            source_security_group_id=agent_sg.security_group_id, description="AgentCore planner agent",
        )

        code = s3_assets.Asset(
            self,
            "AgentCode",
            path=str(AGENT_SOURCE),
            # Key the asset by the bundle, not agent/: a bundling change must upload a new zip.
            asset_hash_type=AssetHashType.OUTPUT,
            bundling=BundlingOptions(image=DockerImage.from_registry("unused-local-bundling"), local=_AgentBundle()),
        )

        role = iam.Role(
            self,
            "AgentRole",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com", conditions={
                "StringEquals": {"aws:SourceAccount": self.account},
            }),
        )
        role.add_to_policy(bedrock_models)
        role.add_to_policy(iam.PolicyStatement(
            actions=["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams", "logs:DescribeLogGroups"],
            resources=[f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/bedrock-agentcore/runtimes/*"],
        ))
        role.add_to_policy(iam.PolicyStatement(actions=["xray:PutTraceSegments", "xray:PutTelemetryRecords"], resources=["*"]))
        role.add_to_policy(iam.PolicyStatement(
            actions=["cloudwatch:PutMetricData"], resources=["*"],
            conditions={"StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"}},
        ))
        # AgentCore reads only the agent code zip.
        role.add_to_policy(iam.PolicyStatement(actions=["s3:GetObject"], resources=[code.bucket.arn_for_objects(code.s3_object_key)]))
        nag(role, "AwsSolutions-IAM5", "X-Ray and CloudWatch PutMetricData do not support resource ARNs (metrics limited to the "
            "bedrock-agentcore namespace); AgentCore names the runtime's log groups, so they are matched by prefix; the global "
            "inference profile's foundation model in any region.",
            [r"^Resource::\*$", r"^Resource::arn:aws:logs:.*:log-group:/aws/bedrock-agentcore/runtimes/\*$",
             r"^Resource::arn:aws:bedrock:\*?::foundation-model/.*\*$"])

        return agentcore.CfnRuntime(
            self,
            "PlannerAgent",
            agent_runtime_name=runtime_name,
            description="Drive Atlas scenario planner (tools call the pipeline API on EKS)",
            role_arn=role.role_arn,
            agent_runtime_artifact=agentcore.CfnRuntime.AgentRuntimeArtifactProperty(
                code_configuration=agentcore.CfnRuntime.CodeConfigurationProperty(
                    code=agentcore.CfnRuntime.CodeProperty(
                        s3=agentcore.CfnRuntime.S3LocationProperty(bucket=code.s3_bucket_name, prefix=code.s3_object_key),
                    ),
                    entry_point=["main.py"],
                    runtime="PYTHON_3_13",
                ),
            ),
            network_configuration=agentcore.CfnRuntime.NetworkConfigurationProperty(
                network_mode="VPC",
                network_mode_config=agentcore.CfnRuntime.VpcConfigProperty(
                    subnets=[s.subnet_id for s in vpc.private_subnets],
                    security_groups=[agent_sg.security_group_id],
                ),
            ),
            protocol_configuration="HTTP",
            environment_variables={
                "PIPELINE_API_URL": f"http://{alb.load_balancer_dns_name}/api/pipeline",
                "MODEL_ID": model_id,
            },
        )
