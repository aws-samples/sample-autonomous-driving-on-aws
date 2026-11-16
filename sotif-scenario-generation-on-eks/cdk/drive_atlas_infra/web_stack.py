"""Web tier on EKS: Deployment on the web node group behind an internal ALB, published through CloudFront (VPC origin) + WAF.

The ALB and target group are CDK resources; a TargetGroupBinding (AWS Load Balancer Controller) registers the pod IPs.
The web pod only serves static files, so it gets no AWS credentials (no IRSA, no service account token).
"""


from aws_cdk import CfnOutput, Duration, Stack
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_eks_v2 as eks
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_s3 as s3
from constructs import Construct

from drive_atlas_infra.checks import checkov, nag
from drive_atlas_infra.eks_stack import NAMESPACE

CONTAINER_PORT = 4173
APP = "web"


class WebStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        vpc: ec2.IVpc,
        cluster: eks.Cluster,
        namespace: eks.KubernetesManifest,
        repository: ecr.IRepository,
        log_bucket: s3.IBucket,
        image_tag: str,
        web_acl_arn: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        alb_sg = ec2.SecurityGroup(self, "AlbSg", vpc=vpc, description="Drive Atlas internal ALB (CloudFront VPC origin only)", allow_all_outbound=True)
        cloudfront_prefix_list = ec2.PrefixList.from_lookup(self, "CloudFrontOriginFacing", prefix_list_name="com.amazonaws.global.cloudfront.origin-facing")
        alb_sg.add_ingress_rule(ec2.Peer.prefix_list(cloudfront_prefix_list.prefix_list_id), ec2.Port.tcp(80), "CloudFront VPC origin")

        alb = elbv2.ApplicationLoadBalancer(
            self,
            "Alb",
            vpc=vpc,
            internet_facing=False,
            security_group=alb_sg,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            drop_invalid_header_fields=True,
        )
        alb.log_access_logs(log_bucket, prefix="logs/alb")
        target_group = elbv2.ApplicationTargetGroup(
            self,
            "WebTargets",
            vpc=vpc,
            port=CONTAINER_PORT,
            protocol=elbv2.ApplicationProtocol.HTTP,
            target_type=elbv2.TargetType.IP,
            health_check=elbv2.HealthCheck(path="/", healthy_http_codes="200", interval=Duration.seconds(15)),
            deregistration_delay=Duration.seconds(15),
        )
        listener = alb.add_listener("Http", port=80, open=False, default_target_groups=[target_group])
        # TLS ends at CloudFront; the ALB is internal and reachable only from CloudFront's VPC origin.
        internal_http = "Internal ALB behind a CloudFront VPC origin (security group admits only CloudFront); viewers use HTTPS to CloudFront."
        checkov(listener, "CKV_AWS_2", internal_http)
        checkov(listener, "CKV_AWS_103", internal_http)
        self.alb, self.alb_sg, self.listener = alb, alb_sg, listener

        labels = {"app": APP}
        manifest = eks.KubernetesManifest(
            self,
            "WebApp",
            cluster=cluster,
            manifest=[
                {
                    "apiVersion": "apps/v1",
                    "kind": "Deployment",
                    "metadata": {"name": APP, "namespace": NAMESPACE, "labels": labels},
                    "spec": {
                        "replicas": 1,
                        "selector": {"matchLabels": labels},
                        # Start the new pod before stopping the old one: no outage on image updates.
                        "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0}},
                        "template": {
                            "metadata": {"labels": labels},
                            "spec": {
                                # Explicit: the API server keeps the deprecated serviceAccount field, so omitting this kept the deleted "web" account.
                                "serviceAccountName": "default",
                                "automountServiceAccountToken": False,
                                "nodeSelector": {"workload": "web"},
                                "containers": [{
                                    "name": APP,
                                    "image": repository.repository_uri_for_tag(image_tag),
                                    "ports": [{"containerPort": CONTAINER_PORT}],
                                    "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"memory": "256Mi"}},
                                    "readinessProbe": {"httpGet": {"path": "/", "port": CONTAINER_PORT}, "periodSeconds": 10},
                                }],
                            },
                        },
                    },
                },
                {
                    "apiVersion": "v1",
                    "kind": "Service",
                    "metadata": {"name": APP, "namespace": NAMESPACE},
                    "spec": {"selector": labels, "ports": [{"port": CONTAINER_PORT, "targetPort": CONTAINER_PORT}]},
                },
                {
                    "apiVersion": "elbv2.k8s.aws/v1beta1",
                    "kind": "TargetGroupBinding",
                    "metadata": {"name": APP, "namespace": NAMESPACE},
                    "spec": {
                        "serviceRef": {"name": APP, "port": CONTAINER_PORT},
                        "targetGroupARN": target_group.target_group_arn,
                        "targetType": "ip",
                        # The controller opens the pod port to the ALB security group only.
                        "networking": {"ingress": [{
                            "from": [{"securityGroup": {"groupID": alb_sg.security_group_id}}],
                            "ports": [{"protocol": "TCP", "port": CONTAINER_PORT}],
                        }]},
                    },
                },
            ],
        )
        manifest.node.add_dependency(namespace)
        manifest.node.add_dependency(cluster.alb_controller)

        origin = origins.VpcOrigin.with_application_load_balancer(
            alb, protocol_policy=cloudfront.OriginProtocolPolicy.HTTP_ONLY, http_port=80
        )
        api_behavior = cloudfront.BehaviorOptions(
            origin=origin,
            viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
            cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
            origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
        )
        distribution = cloudfront.Distribution(
            self,
            "Distribution",
            comment=f"Drive Atlas web ({self.stack_name})",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origin,
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.USE_ORIGIN_CACHE_CONTROL_HEADERS_QUERY_STRINGS,
                ),
            additional_behaviors={"/api/*": api_behavior},
            web_acl_id=web_acl_arn,
            price_class=cloudfront.PriceClass.PRICE_CLASS_200,
            enable_logging=True,
            log_bucket=log_bucket,
            log_file_prefix="logs/cloudfront/",
        )
        default_certificate = ("The sample uses the default *.cloudfront.net certificate, which CloudFront serves with its own TLS policy; "
                               "set a custom domain and ACM certificate with minimum TLSv1.2_2021 for production.")
        nag(distribution, "AwsSolutions-CFR4", default_certificate)
        nag(distribution, "AwsSolutions-CFR1", "Access is already limited by the WAF IP allowlist; which countries to block is the deployer's decision.")
        checkov(distribution, "CKV_AWS_174", default_certificate)

        CfnOutput(self, "Url", value=f"https://{distribution.distribution_domain_name}")
