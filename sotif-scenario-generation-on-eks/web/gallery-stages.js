// Stage descriptions for the pipeline diagram on gallery.html / gallery-en.html (language from <html lang>).
// Numbers are from the v2 deployment check (2026-11-11, ap-northeast-2, 3 variants per batch).
(function () {
  const box = (title, text) => `<div class="data-section"><div class="data-section-title">${title}</div><div class="data-box">${text}</div></div>`;
  const KO = {
    1: {
      title: 'Stage 1: SOTIF 온톨로지 (Amazon Neptune)',
      subtitle: 'ISO 21448 SOTIF 개념(트리거 조건, 기능 불충분, 위험 사건)을 그래프로 정리한 지식',
      body: box('왜 필요한가?', `LLM이 시나리오를 마음대로 만들면 테스트가 어떤 안전 문제를 노리는지 추적하기 어렵습니다.
온톨로지가 허용된 개념(보행자 종류, 속도대, 가림, 조명·날씨)과 그 관계를 정해 두고,
변형 생성은 그 안에서만 고릅니다.`) +
        box('어떻게 만들었나?', `- 스키마: 23개 클래스, 9개 관계(degrades, triggers, occludes, requires …)와 주어·목적어 규칙
- 인스턴스: scripts/build_ontology.sh가 CC BY 4.0 논문의 관련 문단을 근거로 Bedrock이 제안하고,
  출처를 인용한 관계만 남긴 뒤 원문과 대조해 정리했습니다 (sotif/provenance.json, sotif/SOURCES.md)
- 적재: 배포 때 EKS Job이 sotif/ontology.nt를 Neptune에 넣습니다 (IAM 인증, 파일·이미지가 바뀔 때만)`) +
        box('입력 → 출력', `입력: sotif/ontology.nt (인스턴스 134, 관계 99)
조회: SPARQL, 횡단보도 장면에 쓰는 9개 클래스
출력: 지원 태그가 붙은 인스턴스 목록
  simulated      SUMO 동작을 바꿈 (보행자 종류, 자차 속도대)
  visual-only    장면만 바꿈 (주차 차량 = 가림)
  condition-only 영상 생성용으로 기록 (조명, 날씨)`),
    },
    2: {
      title: 'Stage 2: 요청 해석과 변형 생성 (AgentCore + Bedrock)',
      subtitle: '자연어 요청 → 에이전트 → 온톨로지 제약 안에서 시나리오 변형 N개',
      body: box('어떻게 동작하나?', `1. AgentCore Runtime의 에이전트가 요청을 해석해 파이프라인 API의 create_batch를 호출
2. API가 온톨로지를 조회해 프롬프트에 넣고 Claude Sonnet 5.5에 변형을 요청 (tool use)
3. 모델이 제안한 값은 경계 안으로 보정되고, 다음 단계(SUMO)가 실제로 쓸지 판정`) +
        box('변형 하나의 내용', `variantId, rationale(어떤 위험을 노리는지, 요청 언어로)
egoSpeedMps, parkedCount, oncomingPeriodS
pedestrians[] (kind, direction, departS, approachM, speedMps)
ontology {클래스: 인스턴스}`) +
        box('실측', `변형 3개 생성: 약 20초, 입력 약 4,400 / 출력 약 2,600 토큰`),
    },
    3: {
      title: 'Stage 3: SUMO 물리 판정 (EKS Job)',
      subtitle: '변형마다 실제로 주행시켜 자차가 보행자를 만나는지 측정하고, 상호작용이 없으면 버림',
      body: box('어떻게 동작하나?', `- 장면: 편도 2차로 직선 도로, x=200 m 횡단보도, 보도 2 m
- SUMO 1.27.1 + TraCI, 0.1초 간격으로 자차·보행자·주차/대향 차량 기록
- 판정: 최소 거리, 최소 TTC, 양보 여부, 최대 감속 → critical / interaction / nominal-yield / no-interaction`) +
        box('실행 위치와 실측', `EKS cpu-spot 노드그룹의 Job (Spot, 0대에서 Cluster Autoscaler가 확장)
변형 3개: 약 5초`) +
        box('출력', `run/<variant>/telemetry.json (생성 화면의 "위에서 본 장면")
run/results.json (판정)`),
    },
    4: {
      title: 'Stage 4: RDS-HQ 변환 (EKS Job)',
      subtitle: 'SUMO 궤적을 Cosmos-Drive-Dreams 툴킷 입력 형식으로',
      body: box('무엇을 바꾸나?', `- 좌표: SUMO 지도 좌표 → 차량 FLU, 원점은 자차 뒤축
- 포즈 30 Hz, 객체 박스 10 Hz, 차선·도로 경계·횡단보도는 SUMO 네트워크에서
- 카메라: 툴킷 기본 f-theta 앞카메라(120°)
- 구간: 자차가 횡단보도에 다가가는 12.1초(121프레임 청크 3개)`) +
        box('실행 위치와 실측', `EKS cpu-spot Job, 채택된 변형 3개: 약 3초
출력: rdshq/<variant>/ (WebDataset .tar)`),
    },
    5: {
      title: 'Stage 5: HDMap 조건 영상 (EKS Job, CPU)',
      subtitle: '차선·경계·횡단보도와 차량·보행자 박스를 그린 앞카메라 영상',
      body: box('어떻게 동작하나?', `- Cosmos-Drive-Dreams 툴킷(render_from_rds_hq.py)의 hdmap 모드를 CPU(Mesa 소프트웨어 EGL)로 실행
- 변형마다 Job 1개, 병렬 실행. 청크 3개를 이어 붙여 mp4와 webm 저장`) +
        box('실행 위치와 실측', `EKS cpu-spot Job (요청 3.5 vCPU, 6 GiB)
클립당 약 23초, 12.1초 · 363프레임 · 1280×704`) +
        box('다음 단계 (GPU, 미검증)', `HDMap은 실사 영상이 아니라 조건 영상입니다.
Cosmos Transfer로 실사 같은 영상을 만드는 단계는 NVIDIA GPU(g7e Spot 노드그룹)가 필요하며 아직 켜져 있지 않습니다.`),
    },
  };
  const EN = {
    1: {
      title: 'Stage 1: SOTIF ontology (Amazon Neptune)',
      subtitle: 'ISO 21448 SOTIF concepts (triggering conditions, functional insufficiencies, hazardous events) as a graph',
      body: box('Why?', `If an LLM invents scenarios freely, it is hard to trace which safety problem a test targets.
The ontology fixes the allowed concepts (pedestrian kind, speed band, occlusion, lighting, weather) and how they relate;
variant generation chooses only among them.`) +
        box('How it was built', `- Schema: 23 classes, 9 predicates (degrades, triggers, occludes, requires …) with subject/object rules
- Instances: scripts/build_ontology.sh lets Bedrock propose additions grounded in CC BY 4.0 papers, keeps
  only relations that cite a source, and they were then checked against the source text (sotif/provenance.json, sotif/SOURCES.md)
- Load: at deploy time an EKS Job writes sotif/ontology.nt to Neptune (IAM auth, only when the file or image changes)`) +
        box('Input → output', `Input: sotif/ontology.nt (134 instances, 99 relations)
Query: SPARQL over the 9 classes used by the crosswalk scene
Output: instances tagged by how they can be honoured
  simulated      changes SUMO behaviour (pedestrian kind, ego speed band)
  visual-only    changes the scene only (parked cars = occlusion)
  condition-only recorded for video generation (lighting, weather)`),
    },
    2: {
      title: 'Stage 2: Request and variant generation (AgentCore + Bedrock)',
      subtitle: 'Natural-language request → agent → N scenario variants within the ontology',
      body: box('How it works', `1. The agent on AgentCore Runtime interprets the request and calls the pipeline API's create_batch
2. The API reads the ontology into the prompt and asks Claude Sonnet 5.5 for variants (tool use)
3. Proposed values are clamped to bounds; the next stage (SUMO) decides which ones are kept`) +
        box('One variant', `variantId, rationale (the targeted risk, in the request language)
egoSpeedMps, parkedCount, oncomingPeriodS
pedestrians[] (kind, direction, departS, approachM, speedMps)
ontology {class: instance}`) +
        box('Measured', `3 variants: about 20 s, about 4,400 input / 2,600 output tokens`),
    },
    3: {
      title: 'Stage 3: SUMO physics check (EKS Job)',
      subtitle: 'Each variant is driven to measure whether the ego really meets the pedestrian; variants without an interaction are dropped',
      body: box('How it works', `- Scene: straight road, two lanes each way, zebra crossing at x = 200 m, 2 m sidewalks
- SUMO 1.27.1 + TraCI, ego, pedestrians, parked and oncoming vehicles recorded every 0.1 s
- Judgement: minimum distance, minimum TTC, yield, max deceleration → critical / interaction / nominal-yield / no-interaction`) +
        box('Where and how long', `Job on the EKS cpu-spot node group (Spot; the Cluster Autoscaler scales it from zero)
3 variants: about 5 s`) +
        box('Output', `run/<variant>/telemetry.json (the top-down view on the generation page)
run/results.json (judgements)`),
    },
    4: {
      title: 'Stage 4: RDS-HQ conversion (EKS Job)',
      subtitle: 'SUMO trajectories into the Cosmos-Drive-Dreams toolkit input format',
      body: box('What changes', `- Coordinates: SUMO map → vehicle FLU, origin at the ego rear axle
- Poses at 30 Hz, object boxes at 10 Hz; lanes, road boundaries and crossings from the SUMO network
- Camera: the toolkit's default f-theta front camera (120°)
- Window: 12.1 s as the ego approaches the crossing (three 121-frame chunks)`) +
        box('Where and how long', `EKS cpu-spot Job, 3 kept variants: about 3 s
Output: rdshq/<variant>/ (WebDataset .tar)`),
    },
    5: {
      title: 'Stage 5: HDMap condition video (EKS Job, CPU)',
      subtitle: 'Front-camera video of lanes, boundaries, crossings and vehicle/pedestrian boxes',
      body: box('How it works', `- The Cosmos-Drive-Dreams toolkit (render_from_rds_hq.py) in hdmap mode on CPU (Mesa software EGL)
- One Job per variant, in parallel; the three chunks are joined into mp4 and webm`) +
        box('Where and how long', `EKS cpu-spot Job (requests 3.5 vCPU, 6 GiB)
About 23 s per clip; 12.1 s · 363 frames · 1280×704`) +
        box('Next (GPU, not verified)', `HDMap is a condition video, not photorealistic footage.
Photorealistic generation with Cosmos Transfer needs an NVIDIA GPU (the g7e Spot node group) and is not enabled yet.`),
    },
  };
  window.GALLERY_STAGES = document.documentElement.lang === 'en' ? EN : KO;
})();
