import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import { Construct } from 'constructs';

export interface CoreStackProps extends cdk.StackProps {
  vpcId?: string; // Optional: use existing VPC
}

export class CoreStack extends cdk.Stack {
  public readonly vpc: ec2.IVpc;
  public readonly bucket: s3.Bucket;
  public readonly ecrRepository: ecr.Repository;
  public readonly captionProxyRepository: ecr.Repository;

  constructor(scope: Construct, id: string, props?: CoreStackProps) {
    super(scope, id, props);

    // VPC: use existing or create new
    if (props?.vpcId) {
      this.vpc = ec2.Vpc.fromLookup(this, 'Vpc', {
        vpcId: props.vpcId,
      });
    } else {
      this.vpc = new ec2.Vpc(this, 'CosmosCurateVpc', {
        maxAzs: 3,
        natGateways: 1,
        subnetConfiguration: [
          {
            cidrMask: 24,
            name: 'Public',
            subnetType: ec2.SubnetType.PUBLIC,
          },
          {
            cidrMask: 24,
            name: 'Private',
            subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
          },
        ],
      });
    }

    // S3 Bucket for models and data
    this.bucket = new s3.Bucket(this, 'CosmosCurateBucket', {
      bucketName: `cosmos-curate-${this.account}-${this.region}`,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      versioned: false,
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
    });

    // ECR Repositories
    this.ecrRepository = new ecr.Repository(this, 'CuratorRepository', {
      repositoryName: 'cosmos-curate/curator-cpu',
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      imageScanOnPush: true,
      lifecycleRules: [
        {
          description: 'Keep last 10 images',
          maxImageCount: 10,
        },
      ],
    });

    this.captionProxyRepository = new ecr.Repository(this, 'CaptionProxyRepository', {
      repositoryName: 'cosmos-curate/caption-proxy',
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      imageScanOnPush: true,
      lifecycleRules: [
        {
          description: 'Keep last 10 images',
          maxImageCount: 10,
        },
      ],
    });

    // Outputs
    new cdk.CfnOutput(this, 'BucketName', {
      value: this.bucket.bucketName,
      description: 'S3 bucket for models and data',
    });

    new cdk.CfnOutput(this, 'EcrRepositoryUri', {
      value: this.ecrRepository.repositoryUri,
      description: 'ECR repository URI for curator-cpu',
    });

    new cdk.CfnOutput(this, 'CaptionProxyRepositoryUri', {
      value: this.captionProxyRepository.repositoryUri,
      description: 'ECR repository URI for caption-proxy',
    });

    new cdk.CfnOutput(this, 'VpcId', {
      value: this.vpc.vpcId,
      description: 'VPC ID',
    });
  }
}
