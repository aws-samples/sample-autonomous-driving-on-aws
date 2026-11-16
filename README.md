# Autonomous Driving on AWS

Samples for building autonomous driving and Physical AI data pipelines on AWS.
Each sample lives in its own folder with its own README, deployment steps, and NOTICE.

[한국어](README.ko.md)

## Samples

| Sample | Description |
|---|---|
| [cosmos-curate-on-eks](cosmos-curate-on-eks/) | Run the NVIDIA Cosmos Curate video curation pipeline (split, filter, embed, caption) on Amazon EKS CPU nodes without GPU instances. Captions come from Claude on Amazon Bedrock; embeddings are searchable in Amazon OpenSearch Serverless. |
| [sotif-scenario-generation-on-eks](sotif-scenario-generation-on-eks/) | Turn a natural-language driving test request into SOTIF (ISO 21448) crosswalk scenario variants on Amazon EKS without GPU instances. An agent on Amazon Bedrock AgentCore and Claude on Amazon Bedrock write variants constrained by a SOTIF ontology in Amazon Neptune; SUMO checks each variant, the Cosmos-Drive-Dreams toolkit renders HDMap condition videos on CPU, and variants export to OpenSCENARIO and OpenDRIVE. |

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.
