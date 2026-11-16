# Amazon EKS에서 자연어로 주행 시나리오 만들기: SOTIF 온톨로지, SUMO, CPU로 만드는 Cosmos HDMap

주행 시험 상황을 평범한 문장으로 적으면("야간에 아이가 주차된 차 사이에서 갑자기 튀어나오는 상황") 물리적으로
검사한 시나리오 변형 여러 개와, 변형마다 NVIDIA Cosmos용 HDMap 조건 영상을 받습니다. Amazon Bedrock AgentCore의
에이전트가 요청을 읽고, Amazon Bedrock의 Claude가 Amazon Neptune에 있는 SOTIF(ISO 21448) 온톨로지 안에서 변형을
만들고, SUMO가 변형마다 검사하고, Cosmos-Drive-Dreams 툴킷이 HDMap 영상을 렌더링합니다. 모든 단계가 Spot CPU 노드의
Kubernetes Job으로 돌며 **GPU가 필요 없습니다**.

결과물은 **조건 영상**(차선, 횡단보도, 객체 박스)입니다. Cosmos Transfer가 이 영상을 받아 실사 영상으로 바꿉니다.
실사 생성에는 GPU 단계가 필요하며, 이 샘플에는 들어 있지 않습니다.

[English](README.md)

## 구조

```
 브라우저 ── CloudFront + WAF ── 내부 ALB (VPC origin)
                                  ├─ /              web (정적 페이지)
                                  └─ /api/pipeline  pipeline API ──> cpu-spot 노드의 Kubernetes Job
                                                      │   ▲            sumo -> rdshq -> hdmap
                                        대화 요청     │   │ 도구
                                                      ▼   │
                                        AgentCore Runtime (계획 에이전트, VPC 안)

 Pipeline API ── Amazon Bedrock (Claude): 변형 생성
              ── Amazon Neptune: SOTIF 온톨로지 (IAM 인증)
              ── Amazon S3: 배치 상태, SUMO 실행 결과, RDS-HQ 샤드, 영상, 갤러리
```

| 단계 | 하는 일 | 실행 위치 | 실측 (변형 3개) |
|---|---|---|---|
| 1. 온톨로지 | SOTIF 클래스 23개, 인스턴스 134개, 관계 99개(`sotif/ontology.nt`)를 Neptune에 적재 | Neptune `db.t4g.medium` | 파일이 바뀔 때 한 번 |
| 2. 계획과 생성 | 에이전트가 `create_batch`를 호출하고, API가 Neptune에서 클래스 9개를 읽어 Claude에게 변형 1~10개(기본 5개)를 요청. 변형마다 보행자 종류, 시점, 속도, 가림, 날씨, 조명을 정함 | AgentCore Runtime, pipeline API | 에이전트 6~9초, 생성 17~22초 |
| 3. SUMO | 변형마다 실행해 보행자와의 최소 거리와 TTC를 재고, 물리적으로 그럴듯한 것만 남김 | EKS Job (Spot) | 약 5초 |
| 4. RDS-HQ | SUMO 궤적과 도로망을 Cosmos RDS-HQ WebDataset 샤드로 변환 | EKS Job (Spot) | 약 3초 |
| 5. HDMap | `render_from_rds_hq.py`를 HDMap 모드, Mesa 소프트웨어 OpenGL로 실행. 변형마다 Job 하나씩 병렬 | EKS Job (Spot) | 클립당 19~24초, 1280×704 |

변형 3개짜리 배치는 처음부터 끝까지 87~131초 걸렸습니다. 오래 걸린 경우는 Spot 노드가 뜨기를 기다린 시간이 포함됩니다.

- **EKS 클러스터 하나**(Kubernetes 1.37)가 모든 것을 실행합니다. 온디맨드 `web` 노드 그룹(웹, API, 클러스터 애드온)과
  Cluster Autoscaler가 0대에서 늘리는 `cpu-spot` 노드 그룹(c7i/m7i/c6i/m6i.4xlarge, 0~5대)입니다.
- **상태는 S3**의 `pipeline/<batchId>/` 아래에 있습니다. Job 이름이 결정적이고 각 Job은 이미 성공한 단계를 건너뛰므로,
  Spot 노드가 회수되거나 API가 재시작돼도 작업이 사라지거나 중복되지 않습니다.
- **온톨로지는 git으로 버전 관리합니다.** `scripts/build_ontology.sh`가 CC BY 4.0 논문(`sotif/sources.json`)을 근거로
  Bedrock으로 확장하고, 출처를 인용한 관계만 남깁니다(`sotif/provenance.json`). 커밋된 관계는 원문과 대조해 정리했습니다
  (`sotif/SOURCES.md`). diff를 검토해 커밋하면 다음 배포 때 Neptune에 적재됩니다.
- **갤러리**: 다 만든 변형을 생성 화면에서 공용 갤러리 페이지로 게시할 수 있습니다.

## 사전 준비

- VPC, EKS, IAM, S3, ECR, Neptune, CloudFront, WAF, Bedrock AgentCore, CloudFormation 리소스를 만들 수 있는 AWS 계정과
  자격 증명 (엣지 스택은 us-east-1에 만들어짐)
- 추론 프로필 `global.anthropic.claude-sonnet-5-5`로 Amazon Bedrock의 Claude Sonnet을 쓸 수 있고, Bedrock AgentCore
  Runtime을 지원하는 리전
- 16 vCPU Spot 인스턴스 최대 5대(80 vCPU)와 m7i.large 온디맨드 2대분의 EC2 할당량
- Docker가 있는 Linux 빌드 호스트(여유 디스크 약 20 GB, 이미지 4개 합계 약 7.6 GB), AWS CLI v2, Python 3.11 이상,
  Node.js 22 이상, git. `scripts/security_scan.sh`에는 [uv](https://docs.astral.sh/uv/)
- WAF에서 허용할 네트워크의 공인 CIDR

## 빠른 시작

```bash
git clone https://github.com/aws-samples/sample-autonomous-driving-on-aws.git
cd sample-autonomous-driving-on-aws/sotif-scenario-generation-on-eks

# 빈 리전 -> 모든 스택과 이미지 -> 에이전트 요청 -> 변형 3개 -> 영상 -> 갤러리 확인
ALLOWED_CIDRS=203.0.113.0/24 scripts/e2e.sh ap-northeast-2

# 보존 설정된 S3 버킷(과 데이터), ECR 저장소까지 모두 삭제
scripts/teardown.sh ap-northeast-2
```

`e2e.sh`는 검증이 사이트에 닿도록 빌드 호스트의 공인 IP를 `ALLOWED_CIDRS`에 더하고, `scripts/deploy.sh`를 실행한 뒤
CloudFront 주소로 `scripts/verify_pipeline.py`를 돌립니다. 다시 실행해도 되고, 끝난 단계는 건너뜁니다. 출력된 주소로
생성 화면(`/`)과 갤러리(`/gallery.html`)를 엽니다.

모든 스크립트에 `STAGE=<이름>`을 주면 같은 계정에 독립된 사본을 하나 더 배포합니다
(`DriveAtlas<Name>*` 스택, `drive-atlas-<name>-*` 리소스).

### 단계별 스크립트

| 스크립트 | 하는 일 |
|---|---|
| `scripts/deploy.sh <region>` | CDK bootstrap(해당 리전과 us-east-1), Network(VPC, NAT 게이트웨이 1개)와 Data(S3 버킷, ECR 저장소), 이미지 4개 빌드·푸시, EKS와 Neptune, Web(ALB, VPC origin을 쓰는 CloudFront, us-east-1의 WAF), Pipeline(API, AgentCore Runtime, 온톨로지 적재 Job) |
| `scripts/build_and_push_images.sh <region> [tag]` | `web`, `pipeline`(SUMO), `cosmos-hdmap`, `pipeline-api` 빌드 |
| `scripts/verify_pipeline.py <url>` | 에이전트가 배치를 시작하고, 온톨로지를 Neptune에서 읽고 그 내용이 커밋된 것과 같고, 채택된 변형마다 영상·텔레메트리·OpenSCENARIO zip이 있고, 갤러리 게시·취소가 되어야 통과. 단계별 시간 출력 |
| `scripts/build_ontology.sh` | Bedrock으로 온톨로지 확장. `--load <region>`은 커밋된 파일을 Neptune에 적재 |
| `scripts/teardown.sh <region>` | 해당 stage의 모든 스택, CloudFormation이 보존하는 리소스(S3 버킷 두 개와 모든 객체, ECR 저장소), EKS·Neptune·AgentCore가 만든 로그 그룹 삭제 |
| `scripts/check_openscenario.sh <zip>` | 내보낸 OpenSCENARIO zip을 esmini로 재생하고 SUMO 판정과 비교 |
| `scripts/security_scan.sh` | 소스와 합성한 CloudFormation 템플릿에 ASH 실행. 사유가 기록되지 않은 지적이 있으면 실패 |

`deploy.sh` 옵션(환경 변수): `ALLOWED_CIDRS`, `STAGE`, `EKS_PUBLIC_CIDRS`(이 CIDR에서 kubectl용 EKS API를 엶. 기본은
비공개), `TAG`(이미지 태그. 기본은 git 짧은 SHA).

## 사용하기

생성 화면에 한국어나 영어로 요청을 적습니다. 화면이 에이전트의 답을 보여 주고 배치를 따라갑니다. 생성된 변형과
그 근거·온톨로지 태그, SUMO 판정(최소 거리, TTC, 자차의 양보 여부), 채택된 변형마다 HDMap 영상이 나옵니다.
같은 흐름을 API로도 쓸 수 있습니다.

```bash
URL=https://<distribution>.cloudfront.net
curl -s -X POST $URL/api/pipeline/chat -H 'content-type: application/json' \
  -d '{"message": "폭우 속에 휠체어 사용자가 건너는 상황, 4개", "lang": "ko"}'
curl -s $URL/api/pipeline/batches/<batchId>
```

장면은 하나로 고정입니다. 도심 단일로의 횡단보도에서 자차가 보행자에게 양보해야 하는 장면입니다. 이 장면을 벗어나는
요청에는 에이전트가 그렇다고 알리고 가장 가까운 횡단보도 변형을 제안합니다.

### OpenSCENARIO 내보내기

SUMO가 실행할 수 있었던 변형은 모두 `pipeline/<batchId>/openscenario/<variantId>.zip`으로도 저장됩니다(생성 화면과
갤러리의 **OpenSCENARIO 내려받기** 링크, 또는 API의 `openscenarioUrl`).

| 파일 | 내용 |
|---|---|
| `road.xodr` | OpenDRIVE 1.6. SUMO 도로(왕복 2+2차로, 2 m 보도, x = 200 m의 횡단보도) |
| `scenario.xosc` | OpenSCENARIO XML 1.1 재현용. 자차를 포함한 모든 객체가 SUMO 궤적(0.1초 간격 꼭짓점)을 따라감 |
| `scenario_sut.xosc` | 시험 대상 시스템용. 자차는 시작 위치와 속도만 받고, 양보 여부는 시험할 제어기가 결정 |
| `variant.json` | 변형 파라미터와 SUMO 판정 |

`scripts/check_openscenario.sh <zip>`은 두 파일을 esmini v3.8.1(화면 없이, Docker 안)로 재생하고, 재현 결과가 SUMO
판정과 다르면 실패합니다. 저희 시험에서는 자차-보행자 최소 거리가 0.01 m 안으로, 위치가 0.08 m 안으로 같았습니다.
`scenario_sut.xosc`에서 제어기가 없으면 자차가 속도를 유지해 보행자와 부딪칠 수 있습니다. 시험 대상 시스템이 다뤄야
할 바로 그 상황입니다.

## 비용

주요 비용 항목입니다. 리전의 현재 단가로 계산하세요.

| 항목 | 계산식 |
|---|---|
| 스택이 있는 동안 항상 | EKS 컨트롤 플레인, m7i.large 1대(web 노드 그룹), Neptune `db.t4g.medium`, NAT 게이트웨이(시간 + 데이터), 내부 ALB, CloudFront 요청, AWS 관리형 규칙 그룹 2개가 있는 WAF web ACL, S3 저장, CloudWatch Logs 수집(VPC flow logs, EKS 컨트롤 플레인, Neptune 감사) |
| 배치마다 | Job이 도는 동안의 Spot 노드 시간(유휴 노드는 약 5분 뒤 제거) + Bedrock 토큰(변형 3개 생성에 입력 약 4,400, 출력 2,300~2,600, 에이전트에 수천) + AgentCore Runtime 사용량 |

노드를 0대로 줄여도 상시 항목은 멈추지 않습니다. 다 쓰면 `scripts/teardown.sh`를 실행하세요.
[Amazon Bedrock 요금](https://aws.amazon.com/bedrock/pricing/),
[Amazon EKS 요금](https://aws.amazon.com/eks/pricing/),
[Amazon Neptune 요금](https://aws.amazon.com/neptune/pricing/)을 참고하세요.

## 보안

이 샘플은 운영 환경용 배포가 아닙니다. 실제 데이터로 쓰기 전에 다음을 검토하세요.

- **사이트에 접속할 수 있는 사람**: WAF가 `ALLOWED_CIDRS`만 허용합니다. API에는 사용자별 권한이 없어서, 통과한
  사람은 누구나 배치를 시작하고(Bedrock 토큰과 Spot 용량을 씀) 갤러리 항목을 게시하거나 지울 수 있습니다.
- **네트워크**: CloudFront는 VPC origin으로 내부 ALB에 닿고, ALB 보안 그룹은 CloudFront origin-facing 접두사 목록만
  받습니다. EKS API 엔드포인트는 `EKS_PUBLIC_CIDRS`를 주지 않으면 비공개입니다. Neptune은 비공개 서브넷에 있고 IAM 인증이
  필요합니다.
- **WAF**: 알려진 악성 입력(Log4j JNDI 조회 포함)과 IP 평판에 대한 AWS 관리형 규칙 그룹이 허용 목록보다 먼저 돌아서,
  허용된 CIDR에도 적용됩니다.
- **워크로드 권한**: 웹 Pod에는 AWS 자격 증명과 서비스 계정 토큰이 없습니다. pipeline API(EKS Pod Identity)는
  버킷의 `pipeline/`, `gallery/` 경로만 읽고 쓰고, Neptune 조회, 지정한 Bedrock 모델 호출, 에이전트 호출, 네임스페이스
  범위 Kubernetes Role로 Job 생성만 할 수 있습니다. 단계 Job은 `pipeline/`만 씁니다. 에이전트 역할은 지정한 모델 호출과
  자신의 로그·트레이스 기록만 할 수 있습니다.
- **S3**: 버킷은 퍼블릭 접근을 막고, S3 관리형 키로 암호화하고, TLS가 아닌 요청을 거부합니다. 영상은 1시간 뒤 만료되는
  서명 URL로 제공합니다.
- **로그**: 거부된 트래픽의 VPC flow logs(30일, 전체 트래픽은 `network_stack.py`의 `FlowLogTrafficType.ALL`), EKS
  컨트롤 플레인 로그 5종, Neptune 감사 로그, S3·ALB·CloudFront 접근 로그(별도 로그 버킷, 90일)가 켜져 있습니다.
- **검사**: [cdk-nag](https://github.com/cdklabs/cdk-nag) `AwsSolutionsChecks`가 합성할 때마다 돌고, 사유가 없는 지적이
  있으면 합성을 실패시킵니다. 예외와 checkov 예외는 모두 `cdk/drive_atlas_infra/checks.py`나 해당 리소스 옆에 사유와 함께
  있습니다(대부분 CDK EKS 모듈이 만드는 역할·핸들러와 CloudFront 기본 인증서). `scripts/security_scan.sh`는
  [ASH](https://github.com/awslabs/automated-security-helper)(bandit, semgrep, checkov, detect-secrets, cdk-nag, npm audit)를
  소스와 합성한 템플릿에 돌립니다.
- **Bedrock 데이터 위치**: 전역 추론 프로필은 다른 리전에서 요청을 처리할 수 있습니다. 한 리전 안에서만 처리하려면
  리전 프로필을 쓰고 `cdk/drive_atlas_infra/pipeline_stack.py`의 IAM 정책을 고치세요.
- `teardown.sh`는 S3 버킷 두 개를 **모든 데이터와 로그까지** 지웁니다.

보안 문제 신고는 [CONTRIBUTING](../CONTRIBUTING.md#security-issue-notifications)을 참고하세요.

## 문제 해결

| 증상 | 원인 | 해결 |
|---|---|---|
| CloudFront에서 `403` | 내 IP가 `ALLOWED_CIDRS`에 없음 | CIDR을 더해 다시 배포(WAF 규칙은 us-east-1 엣지 스택에 있음) |
| 배치가 몇 분째 `sumo`나 `hdmap`에 머묾 | Cluster Autoscaler가 Spot 노드를 띄우는 중이거나 Spot 용량이 부족함 | 기다리면 노드가 준비될 때 Job이 시작됨 |
| 다시 배포한 뒤에도 예전 그림이나 페이지가 보임 | `Cache-Control: no-cache`를 넣기 전 버전의 브라우저 캐시 | 강력 새로고침 |
| `teardown.sh`가 에이전트 보안 그룹과 Network 스택을 남김 | AgentCore가 지운 런타임의 VPC 네트워크 인터페이스를 늦게 회수함(저희 시험에서 수 시간) | 나머지는 이미 지워짐. 나중에 `scripts/teardown.sh`를 다시 실행. 그때까지 그 VPC가 리전당 VPC 할당량을 차지함 |

## 코딩 에이전트로 배포하기

[`CLAUDE.md`](CLAUDE.md)에 코딩 에이전트가 이 샘플을 배포·검증·디버깅하는 방법이 있습니다.
[Claude Code](https://claude.com/claude-code) 같은 코딩 에이전트에서 이 샘플을 열고, 예를 들어 "ap-northeast-2에
배포하고 end-to-end 테스트를 돌려 줘"라고 요청하세요. 에이전트는 `scripts/e2e.sh`를 실행하고,
`scripts/verify_pipeline.py`가 모든 항목을 통과했다고 보고할 때만 성공으로 봅니다.

## 검증

2026-11-16에 ap-northeast-1에서 내보낸 샘플(git 저장소 아님)로 시험했습니다. 빈 리전에서 `scripts/e2e.sh`(Kubernetes 1.37,
`aws_eks_v2` 모듈과 EKS Pod Identity, Neptune 1.5.0.0, KMS로 암호화한 Kubernetes Secret)가 51분 만에 배포했고, `scripts/verify_pipeline.py`가 모든 항목을
통과했습니다. 에이전트가 7.6초 만에 배치를 시작했고, 생성 19.9초, SUMO 5.1초, RDS-HQ 2.4초, HDMap 영상 3개가 병렬로
19.9~20.9초, 합계 92초였으며 변형 3개가 모두 채택됐습니다. Cluster Autoscaler가 Job을 위해 `cpu-spot`을 0대에서
늘렸습니다. 온톨로지를 바꾼 뒤 `scripts/e2e.sh`를 다시 실행하면 5분 안에 갱신됐고, Neptune이 커밋된 온톨로지를 담고 있는지
보는 검사도 통과했습니다. `scripts/teardown.sh`는 AgentCore가 에이전트의 네트워크 인터페이스를 회수한 뒤(문제 해결 참고)
같은 리전의 이전 stage를 완전히 지웠습니다. `scripts/security_scan.sh`는 사유 없는 지적을 하나도 보고하지 않습니다.

## 한계

- 장면은 하나(도심 단일로 횡단보도)입니다. 다른 장면은 `scripts/compile_sumo.py`에 새 도로 템플릿을, `augment/crosswalk.py`
  같은 변형 공간을 만들어야 합니다.
- HDMap 영상은 조건 입력이지 실사 영상이 아닙니다.
- LLM이 제안한 온톨로지 관계는 스키마로 검증하고 출처 인용을 요구하지만, 내용 자체를 자동으로 검토하지는 않습니다. 이 저장소의 관계는 사람이 원문과 대조했습니다.
  커밋하기 전에 diff를 검토하세요.

## 폴더 구조

```
.
├── agent/          AgentCore Runtime용 계획 에이전트 (표준 라이브러리, boto3, urllib3)
├── augment/        횡단보도 변형 공간과 그것이 읽는 온톨로지 조각
├── cdk/            AWS CDK 앱(Python): Network, Data, EKS, Neptune, Web, Pipeline, 엣지 WAF 스택
├── docker/         이미지: web, pipeline(SUMO), cosmos-hdmap(CPU 툴킷), pipeline-api
├── pipeline_api/   FastAPI 서비스: 배치, Kubernetes Job, 갤러리
├── scripts/        배포, 삭제, e2e, 이미지 빌드, 단계 Job, 온톨로지 빌드, 검증
├── sotif/          SOTIF 온톨로지, 스키마, 출처, LLM 빌드
├── tests/          단위 테스트 (pytest)
└── web/            생성 화면, 갤러리, 아키텍처 그림
```

## 라이선스

이 샘플은 MIT-0 라이선스를 따릅니다. [LICENSE](../LICENSE)를 참고하세요. 빌드나 실행 때 내려받는 제3자 구성요소와
온톨로지가 인용한 논문은 [NOTICE](NOTICE)에 있습니다.
