"""VPC for every workload of the sample: 2 AZs, private subnets with one NAT gateway, flow logs of rejected traffic to CloudWatch Logs."""

from aws_cdk import RemovalPolicy, Stack
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_logs as logs
from constructs import Construct


class NetworkStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, vpc_cidr: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.vpc = ec2.Vpc(
            self,
            "Vpc",
            ip_addresses=ec2.IpAddresses.cidr(vpc_cidr),
            max_azs=2,
            nat_gateways=1,
            subnet_configuration=[
                ec2.SubnetConfiguration(name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24),
                ec2.SubnetConfiguration(name="private", subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS, cidr_mask=22),
            ],
        )
        # S3 traffic (pipeline state, videos, image layers) stays off the NAT gateway.
        self.vpc.add_gateway_endpoint("S3", service=ec2.GatewayVpcEndpointAwsService.S3)

        key = kms.Key(self, "FlowLogsKey", description="VPC flow logs", enable_key_rotation=True, removal_policy=RemovalPolicy.DESTROY)
        key.grant_encrypt_decrypt(iam.ServicePrincipal(f"logs.{self.region}.amazonaws.com"))
        flow_logs = logs.LogGroup(self, "FlowLogs", retention=logs.RetentionDays.ONE_MONTH, encryption_key=key, removal_policy=RemovalPolicy.DESTROY)
        self.vpc.add_flow_log("FlowLog", destination=ec2.FlowLogDestination.to_cloud_watch_logs(flow_logs), traffic_type=ec2.FlowLogTrafficType.REJECT)
