# Autonomous Driving on AWS

AWS에서 자율주행과 Physical AI 데이터 파이프라인을 구축하는 샘플 모음입니다.
샘플마다 폴더가 따로 있고, 폴더마다 README, 배포 방법, NOTICE가 있습니다.

[English](README.md)

## 샘플

| 샘플 | 설명 |
|---|---|
| [cosmos-curate-on-eks](cosmos-curate-on-eks/README.ko.md) | NVIDIA Cosmos Curate 영상 큐레이션 파이프라인(분할, 필터, 임베딩, 캡션)을 GPU 인스턴스 없이 Amazon EKS의 CPU 노드에서 실행합니다. 캡션은 Amazon Bedrock의 Claude가 생성하고, 임베딩은 Amazon OpenSearch Serverless에서 검색할 수 있습니다. |

## 보안

자세한 내용은 [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications)을 참고하세요.

## 라이선스

이 라이브러리는 MIT-0 라이선스를 따릅니다. [LICENSE](LICENSE) 파일을 참고하세요.
