"""Neptune (RDF/SPARQL) for the SOTIF ontology, in the private subnets, IAM database authentication on, audit log to CloudWatch.

The graph is rebuilt from sotif/ontology.nt by a loader Job (PipelineStack), so the cluster holds no data of record.
"""

from aws_cdk import CfnDeletionPolicy, CfnOutput, Stack
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_neptune as neptune
from constructs import Construct

PORT = 8182
ENGINE_VERSION = "1.5.0.0"
PARAMETER_FAMILY = "neptune1.5"


class NeptuneStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, vpc: ec2.IVpc, identifier: str = "drive-atlas-ontology", instance_class: str = "db.t4g.medium", **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.security_group = ec2.SecurityGroup(
            self, "NeptuneSg", vpc=vpc, description="Drive Atlas Neptune (ingress added by the pipeline stack)", allow_all_outbound=False
        )
        subnet_group = neptune.CfnDBSubnetGroup(
            self,
            "SubnetGroup",
            db_subnet_group_description="Drive Atlas Neptune subnets",
            subnet_ids=[subnet.subnet_id for subnet in vpc.private_subnets],
        )
        parameters = neptune.CfnDBClusterParameterGroup(
            self,
            "ClusterParameters",
            family=PARAMETER_FAMILY,
            description="Drive Atlas Neptune: audit log on",
            parameters={"neptune_enable_audit_log": "1"},
        )
        cluster = neptune.CfnDBCluster(
            self,
            "Cluster",
            db_cluster_identifier=identifier,
            engine_version=ENGINE_VERSION,
            db_cluster_parameter_group_name=parameters.ref,
            enable_cloudwatch_logs_exports=["audit"],
            db_subnet_group_name=subnet_group.ref,
            vpc_security_group_ids=[self.security_group.security_group_id],
            iam_auth_enabled=True,
            storage_encrypted=True,
            backup_retention_period=7,
            deletion_protection=False,
        )
        # Rebuilt from version control on every load, so no final snapshot is kept.
        cluster.cfn_options.deletion_policy = CfnDeletionPolicy.DELETE
        instance = neptune.CfnDBInstance(
            self,
            "Instance",
            db_instance_class=instance_class,
            db_cluster_identifier=cluster.ref,
            db_instance_identifier=f"{identifier}-1",
            auto_minor_version_upgrade=True,
            allow_major_version_upgrade=True,
        )
        instance.add_dependency(cluster)

        self.sparql_url = f"https://{cluster.attr_endpoint}:{PORT}/sparql"
        self.data_arn = f"arn:aws:neptune-db:{self.region}:{self.account}:{cluster.attr_cluster_resource_id}/*"
        self.instance = instance

        CfnOutput(self, "SparqlEndpoint", value=self.sparql_url)
