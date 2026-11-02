import * as cdk from 'aws-cdk-lib';
import * as eks from 'aws-cdk-lib/aws-eks';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { Construct } from 'constructs';
import { KubectlV31Layer } from '@aws-cdk/lambda-layer-kubectl-v31';

// All 16 vCPU / >=32 GiB x86, so a curator pod (14 CPU, 24Gi) fits on any of them.
// More types = fewer Spot capacity failures; on-demand uses the same list as a fallback.
const CPU_NODE_TYPES = [
  'c7i.4xlarge', 'c6i.4xlarge', 'm7i.4xlarge', 'm6i.4xlarge',
  'c6a.4xlarge', 'm6a.4xlarge', 'c5.4xlarge', 'm5.4xlarge',
];

export interface EksStackProps extends cdk.StackProps {
  vpc: ec2.IVpc;
  bucket: s3.IBucket;
}

export class EksStack extends cdk.Stack {
  public readonly cluster: eks.Cluster;
  public readonly curatorRole: iam.Role;

  constructor(scope: Construct, id: string, props: EksStackProps) {
    super(scope, id, props);


    // Optional: -c publicAccessCidrs=203.0.113.0/24,198.51.100.7/32 limits the public API endpoint
    const publicAccessCidrs = String(this.node.tryGetContext('publicAccessCidrs') ?? '')
      .split(',').map((c) => c.trim()).filter((c) => c);

    // EKS Cluster
    const clusterName = this.node.tryGetContext('clusterName') || 'cosmos-curate-eks';
    this.cluster = new eks.Cluster(this, 'CosmosCurateCluster', {
      clusterName: clusterName,
      version: eks.KubernetesVersion.V1_31,
      vpc: props.vpc,
      vpcSubnets: [{ subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS }],
      defaultCapacity: 0, // We'll add custom node groups
      endpointAccess: publicAccessCidrs.length
        ? eks.EndpointAccess.PUBLIC_AND_PRIVATE.onlyFrom(...publicAccessCidrs)
        : eks.EndpointAccess.PUBLIC_AND_PRIVATE,
      kubectlLayer: new KubectlV31Layer(this, 'KubectlLayer'),
    });

    // Pod Identity agent add-on
    const podIdentityAgent = new eks.Addon(this, 'PodIdentityAgent', {
      cluster: this.cluster,
      addonName: 'eks-pod-identity-agent',
    });

    // Optional: Grant additional IAM principals kubectl access
    // Usage: cdk deploy -c kubectlRole=role-name
    const kubectlRoleName = this.node.tryGetContext('kubectlRole');
    if (kubectlRoleName) {
      const kubectlRole = iam.Role.fromRoleArn(
        this,
        'KubectlRole',
        `arn:aws:iam::${this.account}:role/${kubectlRoleName}`
      );
      this.cluster.awsAuth.addMastersRole(kubectlRole);
    }

    // CPU Spot node group (workers)
    const spotDesiredSize = Number(this.node.tryGetContext('spotDesiredSize') ?? 3);
    this.cluster.addNodegroupCapacity('CpuSpotNodes', {
      nodegroupName: 'cpu-spot',
      instanceTypes: CPU_NODE_TYPES.map((t) => new ec2.InstanceType(t)),
      minSize: 0,
      desiredSize: spotDesiredSize,
      maxSize: 10,
      capacityType: eks.CapacityType.SPOT,
      diskSize: 120, // the curator image is 14.5 GB compressed; smaller disks hit disk-pressure
      labels: {
        workload: 'curator-cpu',
        'node-type': 'spot',
      },
      taints: [
        {
          key: 'spot-instance',
          value: 'true',
          effect: eks.TaintEffect.PREFER_NO_SCHEDULE,
        },
      ],
      tags: {
        Name: 'cosmos-curate-eks-cpu-spot',
      },
    });

    // On-demand node group (head + fallback)
    const onDemandDesiredSize = Number(this.node.tryGetContext('onDemandDesiredSize') ?? 1);
    // Context values arrive as strings from `-c key=value`
    const dedicatedOnDemand = String(this.node.tryGetContext('dedicatedOnDemand') ?? 'false') === 'true';
    this.cluster.addNodegroupCapacity('CpuOnDemandNodes', {
      nodegroupName: 'cpu-od',
      instanceTypes: CPU_NODE_TYPES.map((t) => new ec2.InstanceType(t)),
      minSize: 0,
      desiredSize: onDemandDesiredSize,
      maxSize: 10,
      capacityType: eks.CapacityType.ON_DEMAND,
      diskSize: 120, // the curator image is 14.5 GB compressed; smaller disks hit disk-pressure
      labels: {
        workload: 'curator-cpu',
        'node-type': 'on-demand',
      },
      // Option b only: keeps other pods (caption-proxy, CoreDNS) off the on-demand node so
      // the Ray head (pod-0) always finds room there; only curator pods tolerate it.
      // Not used when every node is on-demand (option a), or CoreDNS would have nowhere to run.
      taints: dedicatedOnDemand
        ? [{ key: 'dedicated', value: 'curator', effect: eks.TaintEffect.NO_SCHEDULE }]
        : [],
      tags: {
        Name: 'cosmos-curate-eks-cpu-od',
      },
    });

    // Create namespace for Curator
    const curatorNamespace = this.cluster.addManifest('CuratorNamespace', {
      apiVersion: 'v1',
      kind: 'Namespace',
      metadata: { name: 'cosmos-curator' },
    });

    // IAM role for the Curator pods and the caption proxy (Pod Identity trust: AssumeRole + TagSession)
    this.curatorRole = new iam.Role(this, 'CuratorPodRole', {
      assumedBy: new iam.SessionTagsPrincipal(
        new iam.ServicePrincipal('pods.eks.amazonaws.com'),
      ),
    });

    props.bucket.grantReadWrite(this.curatorRole);
    this.curatorRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
        // The global profile routes each request to the model in any region (including the
        // region-less foundation-model ARN), so only the region is a wildcard.
        resources: [
          `arn:aws:bedrock:${this.region}:${this.account}:inference-profile/global.anthropic.claude-opus-5-5`,
          'arn:aws:bedrock:*::foundation-model/anthropic.claude-opus-5-5',
        ],
      }),
    );

    // AOSS needs this IAM permission in addition to the collection data access policy
    this.curatorRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['aoss:APIAccessAll'],
        resources: [`arn:aws:aoss:${this.region}:${this.account}:collection/*`],
      }),
    );

    // Bound to cosmos-curator/default: the upstream chart cannot set the Curator service account
    const podIdentity = new eks.CfnPodIdentityAssociation(this, 'CuratorPodIdentity', {
      clusterName: this.cluster.clusterName,
      namespace: 'cosmos-curator',
      serviceAccount: 'default',
      roleArn: this.curatorRole.roleArn,
    });
    podIdentity.node.addDependency(curatorNamespace);
    podIdentity.node.addDependency(podIdentityAgent);

    // Outputs
    new cdk.CfnOutput(this, 'ClusterName', {
      value: this.cluster.clusterName,
      description: 'EKS cluster name',
    });

    new cdk.CfnOutput(this, 'ClusterArn', {
      value: this.cluster.clusterArn,
      description: 'EKS cluster ARN',
    });

    new cdk.CfnOutput(this, 'ConfigCommand', {
      value: `aws eks update-kubeconfig --region ${this.region} --name ${this.cluster.clusterName}`,
      description: 'Command to configure kubectl',
    });
  }
}
