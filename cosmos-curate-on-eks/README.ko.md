# Amazon EKS에서 GPU 없이 NVIDIA Cosmos Curate 실행하기

[NVIDIA Cosmos Curate](https://github.com/nvidia-cosmos/cosmos-curate) 영상 큐레이션 파이프라인을
GPU 인스턴스 없이 Amazon EKS에서 실행하는 샘플입니다. 장면 분할, 필터, 임베딩은 CPU 노드에서 처리하고,
캡션은 OpenAI 호환 프록시를 거쳐 Amazon Bedrock의 Claude가 생성합니다. 임베딩은 Amazon OpenSearch
Serverless에 색인해 텍스트 검색과 벡터 검색에 사용합니다.

[English](README.md)

## 아키텍처

```
                         Amazon EKS (프라이빗 서브넷, 16 vCPU 노드 4대)
 ┌───────────────────────────────────────────────────────────────────────────────┐
 │  Cosmos Curator StatefulSet (upstream Helm 차트, Pod 하나가 Ray 노드 하나)      │
 │    pod-0 = Ray head          pod-1..3 = Ray worker                             │
 │    분할 (TransNetV2) -> 필터 -> Cosmos-Embed1 임베딩 -> 캡션 ─────────────┐     │
 │                                                                           │     │
 │  caption-proxy (Deployment, 2개) <── OpenAI 형식 요청 ────────────────────┘     │
 └────────────┬────────────────────────────┬────────────────────────────┬────────┘
              │ Converse API (프레임)      │ 모델 가중치, 영상,         │ 임베딩
              ▼                            ▼ 클립, 메타데이터           ▼
       Amazon Bedrock (Claude)        Amazon S3                  OpenSearch Serverless
```

- **Curator**는 `docker/Dockerfile.cpu`로 빌드한 CPU 이미지로 upstream 차트를 실행합니다.
  `docker/patch_cpu.py`가 CUDA를 전제로 한 세 곳(TransNetV2, Cosmos-Embed1, 임베딩 단계의 CPU 요청량)을
  고치고, upstream 코드가 바뀌어 치환이 맞지 않으면 빌드를 실패시킵니다.
- **Caption proxy**(`docker/caption-proxy/`)는 Curator의 OpenAI 형식 캡션 요청을 받아 캡션 윈도우마다
  ffmpeg로 균일한 간격의 프레임 8장을 뽑고 Bedrock Converse API를 호출합니다. Claude 모델은 영상
  입력을 받지 않기 때문입니다.
- **모델 가중치**는 Hugging Face에서 S3로 한 번 내려받고(`bootstrap/seed_models.sh`, sha256 검증),
  작업을 시작할 때 모든 노드로 동기화합니다. 이 저장소에는 가중치가 들어 있지 않습니다.
- **권한**은 EKS Pod Identity로 S3, Bedrock, OpenSearch 접근을 부여합니다. 키는 저장하지 않습니다.

### 노드 옵션

모든 옵션은 16 vCPU x86 노드 4대(c7i/m7i/c6a.4xlarge 등 서로 바꿔 쓸 수 있는 8종)와 노드마다
Curator Pod 하나, 모두 4개를 씁니다.

| `OPTION` | 노드 | Ray head | 용도 |
|---|---|---|---|
| `a` | 온디맨드 4 | 온디맨드 | 중단되면 안 되는 작업 |
| `b` (기본) | Spot 3 + 온디맨드 1 | 온디맨드 | 비용과 안정성의 균형 |
| `c` | Spot 4 | Spot | 비용 우선. head 노드가 회수되면 작업이 중단됨 |

Ray 노드가 하나라도 사라지면 실행 중인 Curate job이 실패합니다. `k8s/run_split.sh`는 노드 유실로 실패한 job을
감지해 Ray 클러스터가 복구될 때까지 기다린 뒤 새 출력 경로로 다시 제출합니다(최대 3회). job은 처음부터 다시
실행됩니다. head가 사라지면 job 상태도 함께 사라집니다.
옵션 `b`는 head를 Spot에서 떼어 둡니다. 온디맨드 노드에 `dedicated=curator:NoSchedule` taint를 걸어
Curator Pod만 들어갈 수 있게 하고, StatefulSet을 `OrderedReady`로 바꿔(`k8s/post-renderer.sh`)
pod-0이 먼저 생성되어 그 노드를 차지하게 합니다. `setup_curator_pod.sh`가 pod-0이 온디맨드 노드에
있는지 확인합니다.

Spot 용량이나 할당량이 부족하면 `deploy_new_region.sh`가 노드 실행 오류를 보고 옵션 `a`로 다시
배포합니다.

## 사전 준비

- VPC, EKS, IAM, S3, ECR, OpenSearch Serverless, CloudFormation 리소스를 만들고 Amazon Bedrock에서
  Claude(추론 프로파일 `global.anthropic.claude-opus-5-5`)를 호출할 수 있는 AWS 계정과 자격 증명
- 대상 리전의 EC2 할당량 64 vCPU (옵션 `b`는 Spot 48 + 온디맨드 16, 옵션 `a`는 온디맨드 64)
- Docker 디스크 여유 공간이 **40GB 이상**인 **Linux** 빌드 호스트 (CPU 이미지가 약 30GB이므로 루트
  볼륨 100GB 권장), AWS CLI v2, kubectl, Helm 3, Node.js 20 이상, git, openssl
- 이미지 빌드는 중국에 있는 서버에서 PaddlePaddle 패키지를 내려받습니다. 테스트에서는 미국 리전의
  빌드 호스트에서 타임아웃이 났고 아시아 태평양(서울)에서는 정상이었습니다

## 빠른 시작

```bash
git clone https://github.com/aws-samples/sample-autonomous-driving-on-aws.git
cd sample-autonomous-driving-on-aws/cosmos-curate-on-eks

# 빈 리전 -> 인프라, 이미지, 모델 가중치 -> Ray 클러스터 ->
# 분할 + Cosmos-Embed1 + Bedrock 캡션 -> OpenSearch -> 텍스트·kNN 검색 확인
scripts/e2e.sh us-west-2                    # 이미지에 들어 있는 30초 샘플 영상 사용
scripts/e2e.sh us-west-2 ./my-videos        # 직접 준비한 .mp4 파일 사용
OPTION=a scripts/e2e.sh us-west-2           # 노드 옵션 선택 (기본 b)

# 보존되는 S3 버킷(데이터 포함)과 ECR 리포지토리까지 모두 삭제
scripts/teardown.sh us-west-2
```

`e2e.sh`는 다시 실행해도 됩니다. 끝난 단계는 건너뜁니다. 빈 리전에서 처음부터 실행하면 65~100분쯤
걸리며, 대부분 이미지 빌드와 push, EKS 생성, 노드마다 14.5GB 이미지를 받는 시간입니다.

### 각 단계가 하는 일

| 스크립트 | 하는 일 |
|---|---|
| `scripts/deploy_new_region.sh <region>` | CDK bootstrap, `CosmosCurateCore`(NAT 게이트웨이 1개인 VPC, S3 버킷 `cosmos-curate-<account>-<region>`, ECR 리포지토리 `cosmos-curate/curator-cpu`와 `cosmos-curate/caption-proxy`), 이미지 빌드·push, 모델 가중치 S3 업로드, `CosmosCurateEks`(클러스터, 노드 그룹, Pod Identity), `CosmosCurateOpenSearch` |
| `scripts/build_and_push_images.sh <region>` | 이미지 두 개를 빌드합니다. 다른 리전에 배포할 때는 빌드 컨텍스트가 같은 로컬 이미지를 재사용합니다 |
| `scripts/setup_curator_pod.sh <region> [replicas]` | Curator 설정(ConfigMap), caption proxy, Curator Helm 배포, head 위치 확인, Ray 노드가 모두 합류할 때까지 대기 |
| `k8s/run_split.sh <region> <dataset>` | `s3://<bucket>/inputs/<dataset>/`에 대해 분할 + 임베딩 + 캡션을 Ray job으로 제출하고 출력 경로를 출력합니다 |
| `scripts/upload_embeddings_to_opensearch.py` | 실행 결과의 임베딩과 캡션을 색인합니다 (OpenSearch 권한이 있는 pod-0 안에서 실행) |
| `scripts/verify_opensearch.py` | 이번 실행의 문서가 텍스트 검색과 kNN 검색 모두에서 조회되지 않으면 실패합니다 |
| `scripts/teardown.sh <region>` | Helm 릴리스, 스택, S3 버킷, ECR 리포지토리를 삭제합니다 |

## 내 영상으로 파이프라인 실행하기

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
aws s3 sync ./my-videos s3://cosmos-curate-${ACCOUNT_ID}-us-west-2/inputs/my-videos/
k8s/run_split.sh us-west-2 my-videos
```

`run_split.sh`는 Curate의 `split` 파이프라인을 CPU의 TransNetV2 샷 검출, Cosmos-Embed1-336p 임베딩,
프록시를 거친 캡션으로 실행합니다. 결과(클립, 캡션이 든 메타데이터, 임베딩, `summary.json`)는
`s3://<bucket>/outputs/<dataset>/<run-id>/`에 저장됩니다.

이 구성에 꼭 필요한 설정 두 가지는 스크립트에 이미 들어 있습니다.

- `--runtime-env-json '{"env_vars":{"NVCF_MULTI_NODE":"false"}}'`: Helm 배포는 `NVCF_MULTI_NODE=true`를
  설정합니다. 이 값이 켜져 있으면 Curate가 `--model-weights-path`를 무시하고 NVIDIA NGC에서 가중치를 받습니다.
- `pixi run --as-is`: `--as-is`가 없으면 pixi가 이미지 안의 환경을 갱신하려다 실패합니다.

캡션은 클립 단위가 아니라 캡션 윈도우 단위로 만들어지므로 클립이 길수록 Bedrock 호출이 늘어납니다.
캡션 내용을 바꾸려면 `--captioning-prompt-text`로 프롬프트를 지정합니다
(`k8s/prompts/caption_prompt.txt`가 일반적인 예시입니다).

실행 결과를 색인하고 검색하는 방법입니다(`<prefix>`는 `run_split.sh`가 출력한 경로에서 버킷 뒤 부분).

```bash
export KUBECONFIG=~/.kube/cosmos-curate-us-west-2
ENDPOINT=$(aws cloudformation describe-stacks --stack-name CosmosCurateOpenSearch --region us-west-2 \
  --query "Stacks[0].Outputs[?OutputKey=='CollectionEndpoint'].OutputValue" --output text)
kubectl cp scripts/upload_embeddings_to_opensearch.py cosmos-curator/cosmos-curator-0:/tmp/upload.py
kubectl -n cosmos-curator exec cosmos-curator-0 -- bash -c "pip3 install --user --quiet opensearch-py && \
  python3 /tmp/upload.py --collection-endpoint ${ENDPOINT} --bucket cosmos-curate-${ACCOUNT_ID}-us-west-2 \
    --prefix <prefix> --region us-west-2 --index-name video-clips"
```

## 비용

주요 비용 항목입니다. 리전의 현재 단가로 계산하세요.

| 항목 | 계산식 |
|---|---|
| 노드 | Σ(노드 그룹별 노드 수 × 시간당 단가) × 작업 시간 |
| 캡션 | 캡션 윈도우 수 × (입력 토큰 × 입력 단가 + 출력 토큰 × 출력 단가) |
| 스택이 있는 동안 계속 발생 | EKS 컨트롤 플레인, NAT 게이트웨이(시간 요금과 데이터 처리 요금), OpenSearch Serverless OCU, S3 저장 |

캡션을 켜면 대부분의 비용이 캡션에서 나옵니다. 소량의 영상으로 먼저 실행해 윈도우당 토큰 수를
확인하세요. 노드 그룹을 0으로 줄여도 컨트롤 플레인, NAT 게이트웨이, OpenSearch 요금은 계속 나오므로
다 쓰면 `scripts/teardown.sh`를 실행하세요. [Amazon Bedrock 요금](https://aws.amazon.com/bedrock/pricing/)과
[Amazon EKS 요금](https://aws.amazon.com/eks/pricing/)을 참고하세요.

## 보안

이 저장소는 샘플이며 운영 환경 구성이 아닙니다. 실제 데이터에 쓰기 전에 다음을 검토하세요.

- **EKS API 엔드포인트**는 퍼블릭과 프라이빗이 모두 켜져 있습니다. `PUBLIC_ACCESS_CIDRS=203.0.113.0/24 scripts/e2e.sh <region>`처럼
  퍼블릭 엔드포인트를 내 네트워크로 제한하거나(쉼표로 여러 개, 스크립트를 실행하는 호스트가 포함되어야 함)
  프라이빗으로 바꾸세요.
- **S3** 버킷은 퍼블릭 접근 차단, S3 관리형 키 암호화, TLS가 아닌 요청 거부가 설정되어 있습니다. 서버 접근
  로그, VPC flow log, EKS 컨트롤 플레인 로그는 샘플 비용을 줄이려고 꺼 두었으니 운영 환경에서는 켜세요.
- **OpenSearch Serverless** 네트워크 정책이 컬렉션 엔드포인트의 퍼블릭 접근을 허용합니다. 접근은 IAM과
  컬렉션 데이터 접근 정책으로만 통제됩니다. 운영 환경에서는 VPC 엔드포인트를 쓰세요.
- **Bedrock 데이터 처리 위치**: 글로벌 추론 프로파일은 다른 리전에서 요청을 처리할 수 있습니다.
  프레임이 한 리전 밖으로 나가면 안 된다면 리전 모델·프로파일을 쓰고 IAM 정책을 바꾸세요.
- Curator Pod와 caption proxy는 `cosmos-curator`의 `default` 서비스 계정을 함께 씁니다(upstream 차트가
  Curator 서비스 계정을 지정할 수 없음). 그래서 같은 IAM 역할을 받습니다.
- `teardown.sh`는 S3 버킷을 **데이터까지** 삭제합니다. 그 외에는 CloudFormation이 버킷과 ECR
  리포지토리를 보존합니다.

Curator 이미지는 upstream Cosmos Curate v2.8.0의 Python 잠금 환경(`pixi.lock`)을 그대로 설치합니다. 이미지
스캔에서 고정된 패키지 일부(pillow, PyJWT, aiohttp, transformers 등)의 알려진 취약점이 보고되므로, 운영에
쓰기 전에 이미지를 스캔하고 upstream 새 버전으로 다시 빌드하세요.

CDK 앱은 synth할 때마다 [cdk-nag](https://github.com/cdklabs/cdk-nag) `AwsSolutionsChecks`를 실행합니다.
남은 규칙은 `cdk/bin/cdk.ts`에 이유와 함께 예외로 적어 두었습니다(대부분 CDK EKS 모듈이 만드는 리소스).

보안 문제 신고 방법은 [CONTRIBUTING](../CONTRIBUTING.md#security-issue-notifications)을 참고하세요.

## 문제 해결

| 증상 | 원인 | 해결 (스크립트에 이미 반영) |
|---|---|---|
| NGC에서 가중치를 받으려 함 / `NGC_NVCF_API_KEY not found` | Helm 배포의 `NVCF_MULTI_NODE=true` | job 실행 환경에 `NVCF_MULTI_NODE=false` 지정 |
| `Not enough CPU resources ... requires 17 CPUs but only 15` | Pod 하나는 Ray CPU 15개 | Curator Pod 2개 이상 (기본 4) |
| 업그레이드 후 worker가 `InvalidAuthToken`으로 실패 | Ray 토큰이 새로 생성됨 | `ray-auth-token` secret 재사용 |
| `OpenAI caption configuration not found` | 설정 스키마가 틀림 | ConfigMap에 `openai.caption.{base_url,api_key}`. 바꾼 뒤에는 Pod 재시작 |
| Pod에서 OpenSearch `403` | IAM `aoss:APIAccessAll` 누락 | `eks-stack.ts`에서 부여 |
| `Field 'embedding' is not built for ANN search` | 인덱스에 `index.knn`이 없음 | `index.knn: true`로 생성 |
| 배포 후 kubectl `Unauthorized` | 배포 역할이 aws-auth에 없음 | `deploy_new_region.sh`가 모든 배포에 `-c kubectlRole=<role>` 전달 |
| 일부 노드에서 임베딩 단계가 한 시간 넘게 걸림 | AMX가 없는 CPU(c5/m5/c6i/m6i, AMD c6a/m6a)에서 bf16은 fp32보다 5~300배 느림 | `docker/patch_cpu.py`가 AMX가 있는 CPU(c7i/m7i)에서만 bf16, 나머지는 fp32 사용 |
| job이 `The actor died because its node has died`로 실패 | job 도중 Spot 노드가 회수됨 | `run_split.sh`가 Ray 클러스터를 복구하고 다시 제출 (최대 3회) |
| 노드 `disk-pressure`, `ImagePullBackOff` | 14.5GB 이미지 | 노드 디스크 120GB |
| 스택이 `DELETE_FAILED`에 멈춤 | 삭제 중 클러스터 변경이 진행 중 | `scripts/teardown.sh` 다시 실행 |

자주 쓰는 명령입니다(`KUBECONFIG=~/.kube/cosmos-curate-<region>`).

```bash
kubectl -n cosmos-curator get pods -o wide
kubectl -n cosmos-curator exec cosmos-curator-0 -- pixi run --as-is ray status
kubectl -n cosmos-curator logs -l app=caption-proxy
# Ray는 단계 오류를 감싸서 보여 줍니다. 실제 원인은 worker 로그에 있습니다.
kubectl -n cosmos-curator exec cosmos-curator-0 -- bash -c 'tail -n 50 /tmp/ray/session_latest/logs/worker-*.err'
```

## 코딩 에이전트로 배포하기

[`CLAUDE.md`](CLAUDE.md)에 코딩 에이전트가 이 샘플을 배포, 검증, 디버깅하는 방법이 적혀 있습니다.
저장소를 [Claude Code](https://claude.com/claude-code) 같은 코딩 에이전트로 열고 예를 들어 "us-west-2에 배포하고 e2e
테스트 돌려줘"라고 하면, 에이전트가 `scripts/e2e.sh`를 실행하고 검색 확인까지 통과해야 성공으로 봅니다.

## 검증 결과

`scripts/e2e.sh`는 빈 리전(스택, ECR 리포지토리, S3 버킷이 없는 상태)에서 이미지 내장 샘플 영상으로
세 노드 옵션 모두 통과했습니다. `a`는 us-west-2(c7i 4대), `b`는 ap-northeast-1(온디맨드 c7i head,
Spot c5/m5 worker), `c`는 ap-southeast-1(Spot c5 4대)에서 실행했습니다. 모두 클립 4개, 캡션 4/4
성공, 검색 가능한 문서 4건이었고 텍스트·kNN 검색 모두 해당 클립을 찾았습니다. 추가로 ap-southeast-1에서 옵션 `b`에 `PUBLIC_ACCESS_CIDRS`를
빌드 호스트로 제한해 실행하면서 첫 job의 임베딩 단계에 worker Pod를 삭제했고, `run_split.sh`가 클러스터를
복구한 뒤 두 번째 시도가 통과했습니다. Cosmos Curate v2.8.0 기준입니다.

## 제한 사항

- CPU에서의 샷 검출과 임베딩은 GPU보다 훨씬 느립니다. 소량으로 먼저 측정하세요.
- 캡션 처리량은 노드 수보다 Bedrock 할당량의 영향을 받습니다.
- worker를 잃으면 job이 처음부터 다시 실행됩니다(작업 도중 worker Pod 삭제로 테스트했고, us-west-2에서 실제
  Spot 재조정으로도 발생). 긴 작업은 진행분을 잃으므로 큰 입력은 여러 데이터셋으로 나누세요. head를 잃는
  경우(옵션 `c`)도 같은 재시도 경로를 타지만 테스트하지 않았습니다.

## 디렉터리 구성

```
.
├── bootstrap/        Hugging Face -> S3 모델 가중치 미러 (models.txt, seed_models.sh)
├── cdk/              AWS CDK 앱: Core (VPC, S3, ECR), EKS, OpenSearch Serverless 스택
├── docker/           CPU Curator 이미지 (Dockerfile.cpu, patch_cpu.py)와 caption proxy
├── k8s/              run_split.sh, Helm post-renderer, 캡션 프롬프트 예시
└── scripts/          e2e.sh, 배포·설정·정리 스크립트, OpenSearch 업로드·확인
```

## 라이선스

이 샘플은 MIT-0 라이선스를 따릅니다. [LICENSE](../LICENSE)를 참고하세요. 빌드나 실행 시에 내려받는
서드파티 구성 요소는 [NOTICE](NOTICE)에 정리했습니다.
