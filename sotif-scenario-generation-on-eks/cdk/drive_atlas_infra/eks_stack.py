"""EKS cluster for every workload of the sample (web, pipeline jobs, GPU rendering).

Node groups:
- web:  On-Demand, always 1 node. Web Deployment and cluster add-ons (ALB controller, autoscaler).
- cpu:  Spot, 0-5 nodes, tainted. SUMO, RDS-HQ conversion and Cosmos HDMap (CPU) Jobs.
- gpu:  Spot g7e first, tainted, created only when gpu_max_nodes > 0. Photorealistic rendering Jobs.

The Cluster Autoscaler scales cpu/gpu from zero when Jobs are pending.
"""

from typing import Sequence

from aws_cdk import CfnOutput, Stack
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_eks_v2 as eks
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_s3 as s3
from aws_cdk.lambda_layer_kubectl_v37 import KubectlV37Layer
from constructs import Construct

from drive_atlas_infra.checks import checkov, nag
from drive_atlas_infra.data_stack import grant_prefixes

NAMESPACE = "drive-atlas"
BATCH_TAINT_KEY = "drive-atlas/batch"


class EksStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        vpc: ec2.IVpc,
        bucket: s3.IBucket,
        cluster_name: str = "drive-atlas",
        public_access_cidrs: Sequence[str] = (),
        gpu_max_nodes: int = 0,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Private API endpoint unless operator CIDRs are given (kubectl from outside the VPC).
        endpoint_access = (
            eks.EndpointAccess.PUBLIC_AND_PRIVATE.only_from(*public_access_cidrs)
            if public_access_cidrs
            else eks.EndpointAccess.PRIVATE
        )
        cluster = eks.Cluster(
            self,
            "Cluster",
            cluster_name=cluster_name,
            version=eks.KubernetesVersion.V1_37,
            vpc=vpc,
            vpc_subnets=[ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS)],
            endpoint_access=endpoint_access,
            # Managed node groups + Cluster Autoscaler instead of the module's default (EKS Auto Mode).
            default_capacity_type=eks.DefaultCapacityType.NODEGROUP,
            default_capacity=0,
            # Lambda that runs kubectl/helm for the manifests and charts below; in the VPC because the endpoint is private.
            kubectl_provider_options=eks.KubectlProviderOptions(
                kubectl_layer=KubectlV37Layer(self, "KubectlLayer"),
                private_subnets=vpc.select_subnets(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS).subnets,
            ),
            alb_controller=eks.AlbControllerOptions(version=eks.AlbControllerVersion.V2_17_1),
            cluster_logging=list(eks.ClusterLoggingTypes),
            # Envelope encryption of Kubernetes Secrets in etcd.
            secrets_encryption_key=kms.Key(self, "SecretsKey", enable_key_rotation=True,
                                           description=f"{cluster_name} EKS secrets encryption"),
        )

        node_role = iam.Role(
            self,
            "NodeRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(name)
                for name in (
                    "AmazonEKSWorkerNodePolicy",
                    "AmazonEKS_CNI_Policy",
                    "AmazonEC2ContainerRegistryReadOnly",
                    "AmazonSSMManagedInstanceCore",
                )
            ],
        )

        web_nodegroup = cluster.add_nodegroup_capacity(
            "WebNodes",
            nodegroup_name="web",
            node_role=node_role,
            instance_types=[ec2.InstanceType("m7i.large"), ec2.InstanceType("m6i.large")],
            ami_type=eks.NodegroupAmiType.AL2023_X86_64_STANDARD,
            capacity_type=eks.CapacityType.ON_DEMAND,
            min_size=1,
            desired_size=1,
            max_size=2,
            disk_size=50,
            labels={"workload": "web"},
        )

        batch_taint = eks.TaintSpec(effect=eks.TaintEffect.NO_SCHEDULE, key=BATCH_TAINT_KEY, value="true")
        cpu_nodegroup = cluster.add_nodegroup_capacity(
            "CpuNodes",
            nodegroup_name="cpu-spot",
            node_role=node_role,
            instance_types=[ec2.InstanceType(t) for t in ("c7i.4xlarge", "m7i.4xlarge", "c6i.4xlarge", "m6i.4xlarge")],
            ami_type=eks.NodegroupAmiType.AL2023_X86_64_STANDARD,
            capacity_type=eks.CapacityType.SPOT,
            min_size=0,
            desired_size=0,
            max_size=5,
            disk_size=120,
            labels={"workload": "cpu"},
            taints=[batch_taint],
        )

        # A node group needs MaxSize >= 1, so the GPU group exists only when enabled (-c drive-atlas:gpuMaxNodes=N).
        if gpu_max_nodes > 0:
            gpu_nodegroup = cluster.add_nodegroup_capacity(
                "GpuNodes",
                nodegroup_name="gpu-spot",
                node_role=node_role,
                instance_types=[
                    ec2.InstanceType("g7e.2xlarge"),  # RTX PRO 6000 96GB, 8 vCPU, 64GB RAM
                    ec2.InstanceType("g7e.4xlarge"),  # RTX PRO 6000 96GB, 16 vCPU, 128GB RAM
                    ec2.InstanceType("g6e.2xlarge"),  # L40S 48GB fallback
                ],
                # CDK cannot infer a GPU AMI for g7e, so set it explicitly.
                ami_type=eks.NodegroupAmiType.AL2023_X86_64_NVIDIA,
                capacity_type=eks.CapacityType.SPOT,
                min_size=0,
                desired_size=0,
                max_size=gpu_max_nodes,
                disk_size=200,
                labels={"workload": "gpu"},
                taints=[
                    batch_taint,
                    eks.TaintSpec(effect=eks.TaintEffect.NO_SCHEDULE, key="nvidia.com/gpu", value="true"),
                ],
            )
            CfnOutput(self, "GpuNodeGroup", value=gpu_nodegroup.nodegroup_name)

        # Add-ons wait for their pods (helm --wait), so they need the web nodes to exist first.
        cluster.alb_controller.node.add_dependency(web_nodegroup)
        self._add_cluster_autoscaler(cluster, cluster_name)

        cluster.add_manifest(
            "NvidiaDevicePlugin",
            {
                "apiVersion": "apps/v1",
                "kind": "DaemonSet",
                "metadata": {"name": "nvidia-device-plugin", "namespace": "kube-system"},
                "spec": {
                    "selector": {"matchLabels": {"name": "nvidia-device-plugin"}},
                    "template": {
                        "metadata": {"labels": {"name": "nvidia-device-plugin"}},
                        "spec": {
                            "nodeSelector": {"workload": "gpu"},
                            "tolerations": [{"operator": "Exists", "effect": "NoSchedule"}],
                            "priorityClassName": "system-node-critical",
                            "containers": [{
                                "name": "nvidia-device-plugin",
                                "image": "nvcr.io/nvidia/k8s-device-plugin:v0.17.0",
                                "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
                                "volumeMounts": [{"name": "device-plugin", "mountPath": "/var/lib/kubelet/device-plugins"}],
                            }],
                            "volumes": [{"name": "device-plugin", "hostPath": {"path": "/var/lib/kubelet/device-plugins"}}],
                        },
                    },
                },
            },
        )

        namespace = cluster.add_manifest(
            "Namespace", {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NAMESPACE}}
        )

        # Identity for pipeline Job pods: read inputs from and write outputs to S3.
        job_sa = cluster.add_service_account("PipelineJobSa", name="pipeline-job", namespace=NAMESPACE,
                                              identity_type=eks.IdentityType.POD_IDENTITY)
        job_sa.node.add_dependency(namespace)
        grant_prefixes(bucket, job_sa, ["pipeline/"])
        nag(job_sa, "AwsSolutions-IAM5", "Object access is limited to this bucket's pipeline/ prefix; object keys are created per batch, so the key itself is a wildcard.", [r"^Resource::.*/pipeline/\*$"])
        nag(node_role, "AwsSolutions-IAM4", "Managed policies required for EKS worker nodes (kubelet, VPC CNI, ECR pulls) and SSM Session Manager access.")
        self._acknowledge_cdk_eks_internals(cluster)

        self.cluster = cluster
        self.namespace = namespace

        CfnOutput(self, "ClusterName", value=cluster.cluster_name)
        CfnOutput(self, "KubeconfigCommand", value=f"aws eks update-kubeconfig --name {cluster.cluster_name} --region {self.region}")
        CfnOutput(self, "CpuNodeGroup", value=cpu_nodegroup.nodegroup_name)

    def _acknowledge_cdk_eks_internals(self, cluster: eks.Cluster) -> None:
        """Roles and handlers that the CDK EKS module creates for itself (the cluster is a native resource)."""
        reason = ("Created and scoped by the aws-cdk-lib aws_eks_v2 module (cluster service role with the EKS-required "
                  "managed policy, kubectl provider Lambda and its provider framework, upstream ALB controller IAM "
                  "policy); not configurable from this app.")
        internals = [cluster.node.find_child("Role"), cluster.node.find_child("KubectlProvider"), cluster.alb_controller]
        for rule in ("AwsSolutions-IAM4", "AwsSolutions-IAM5", "AwsSolutions-L1"):
            nag(internals, rule, reason)
        for check in ("CKV_AWS_107", "CKV_AWS_108", "CKV_AWS_109", "CKV_AWS_110", "CKV_AWS_111"):
            for construct in internals:
                for cfn in construct.node.find_all():
                    if isinstance(cfn, iam.CfnPolicy) or isinstance(cfn, iam.CfnRole):
                        checkov(cfn, check, reason)

    def _add_cluster_autoscaler(self, cluster: eks.Cluster, cluster_name: str) -> None:
        sa = cluster.add_service_account("ClusterAutoscalerSa", name="cluster-autoscaler", namespace="kube-system",
                                        identity_type=eks.IdentityType.POD_IDENTITY)
        sa.add_to_principal_policy(iam.PolicyStatement(
            actions=[
                "autoscaling:DescribeAutoScalingGroups",
                "autoscaling:DescribeAutoScalingInstances",
                "autoscaling:DescribeLaunchConfigurations",
                "autoscaling:DescribeScalingActivities",
                "autoscaling:DescribeTags",
                "ec2:DescribeImages",
                "ec2:DescribeInstanceTypes",
                "ec2:DescribeLaunchTemplateVersions",
                "ec2:GetInstanceTypesFromInstanceRequirements",
                "eks:DescribeNodegroup",
            ],
            resources=["*"],
        ))
        # Managed node groups tag their ASGs with this key, so only this cluster's groups can be scaled.
        sa.add_to_principal_policy(iam.PolicyStatement(
            actions=["autoscaling:SetDesiredCapacity", "autoscaling:TerminateInstanceInAutoScalingGroup"],
            resources=["*"],
            conditions={"StringEquals": {f"aws:ResourceTag/k8s.io/cluster-autoscaler/{cluster_name}": "owned"}},
        ))
        chart = cluster.add_helm_chart(
            "ClusterAutoscaler",
            chart="cluster-autoscaler",
            repository="https://kubernetes.github.io/autoscaler",
            version="9.60.0",
            namespace="kube-system",
            release="cluster-autoscaler",
            values={
                "autoDiscovery": {"clusterName": cluster_name},
                "awsRegion": self.region,
                "image": {"tag": "v1.37.0"},  # must match the cluster minor version
                "rbac": {"serviceAccount": {"create": False, "name": "cluster-autoscaler"}},
                "nodeSelector": {"workload": "web"},
                "extraArgs": {
                    "expander": "least-waste",
                    "balance-similar-node-groups": True,
                    "skip-nodes-with-system-pods": False,
                    "scale-down-unneeded-time": "5m",
                },
            },
        )
        chart.node.add_dependency(sa)
        nag(sa, "AwsSolutions-IAM5", "Autoscaling Describe* calls do not support resource ARNs; scaling actions are limited by the "
            "k8s.io/cluster-autoscaler/<cluster>=owned tag condition to this cluster's node groups.", [r"^Resource::\*$"])
        # The ALB controller's webhook intercepts every Service; installing before its pods are ready fails.
        chart.node.add_dependency(cluster.alb_controller)
