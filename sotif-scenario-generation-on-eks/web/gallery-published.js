// Scenarios published from the generation page (index.html) into the gallery, read from the pipeline API.
// Shared by gallery.html and gallery-en.html; the page language comes from <html lang>.
// Built with DOM nodes and textContent only: API data never becomes HTML.
(function () {
  const EN = document.documentElement.lang === 'en';
  const T = EN ? {
    title: 'Generated and published scenarios', sub: 'Published from the scenario generation page · click to play the HDMap video',
    empty: 'Nothing published yet. On the generation page, use “Add to gallery” on a finished variant.',
    unavailable: 'The pipeline API is not reachable, so published scenarios cannot be shown.',
    request: 'Request', published: 'Published', process: 'Show how it was generated →', osc: 'Download OpenSCENARIO (.xosc + .xodr)', ego: 'Ego', peds: 'Pedestrians', parked: 'Parked cars',
    oncoming: 'Oncoming traffic', none: 'none', every: (s) => `every ${s} s`, label: 'SUMO label', minDist: 'Min distance', ttc: 'Min TTC',
    yielded: 'Yielded', yes: 'yes', no: 'no', ontology: 'Ontology conditions', params: 'Generated parameters', judge: 'SUMO judgement',
    window: (a, b) => `HDMap window: ${a}–${b} s of the simulation`,
  } : {
    title: '생성해서 게시한 시나리오', sub: '시나리오 생성 화면에서 게시한 결과 · 클릭하여 HDMap 영상 보기',
    empty: '아직 게시된 시나리오가 없습니다. 생성 화면에서 끝난 변형의 “갤러리에 추가”를 누르세요.',
    unavailable: '파이프라인 API에 연결할 수 없어 게시된 시나리오를 보여줄 수 없습니다.',
    request: '요청', published: '게시', process: '생성 과정 보기 →', osc: 'OpenSCENARIO 내려받기 (.xosc + .xodr)', ego: '자차', peds: '보행자', parked: '주차 차량',
    oncoming: '대향 차량', none: '없음', every: (s) => `${s}초 간격`, label: 'SUMO 판정', minDist: '최소 거리', ttc: '최소 TTC',
    yielded: '양보', yes: '예', no: '아니오', ontology: '온톨로지 조건', params: '생성된 파라미터', judge: 'SUMO 판정 결과',
    window: (a, b) => `HDMap 구간: 시뮬레이션 ${a}–${b}초`,
  };
  const kmh = (mps) => (mps == null ? '–' : `${Math.round(mps * 3.6)} km/h`);
  const date = (t) => new Date(t * 1000).toLocaleString(EN ? 'en-US' : 'ko-KR', { dateStyle: 'medium', timeStyle: 'short' });
  const https = (url) => (typeof url === 'string' && url.startsWith('https://') ? url : null);

  // el('p', {className: 'x', style: 'color:red'}, 'text', childNode, ...)
  function el(tag, props, ...children) {
    const node = document.createElement(tag);
    Object.entries(props || {}).forEach(([key, value]) => {
      if (key === 'style') node.style.cssText = value;
      else node[key] = value;
    });
    children.flat().forEach((child) => {
      if (child != null && child !== false) node.append(child instanceof Node ? child : String(child));
    });
    return node;
  }
  const link = (href, text) => el('a', { href, style: 'color:#667eea' }, text);

  const anchor = document.querySelector('.scenario-section');
  const grid = el('div', { className: 'scenario-grid', id: 'publishedGrid' });
  const section = el('div', { className: 'scenario-section', id: 'published' },
    el('div', { className: 'section-header' }, el('h2', {}, T.title), el('p', {}, T.sub)),
    grid);
  anchor.after(section);
  const note = (text) => grid.replaceChildren(el('p', { style: 'color:#9aa0a6' }, text));

  function show(item) {
    const p = item.params || {}, s = item.sumo || {};
    document.getElementById('scenarioModalTitle').textContent = item.variantId;
    document.getElementById('scenarioModalSubtitle').textContent = item.rationale || '';

    const video = el('video', { className: 'video-player', controls: true, autoplay: true, loop: true, muted: true, playsInline: true });
    [[https(item.webmUrl), 'video/webm'], [https(item.videoUrl), 'video/mp4']].forEach(([src, type]) => {
      if (src) video.append(el('source', { src, type }));
    });
    const process = `index.html?lang=${EN ? 'en' : 'ko'}&batch=${encodeURIComponent(item.batchId)}`;
    const osc = https(item.openscenarioUrl);
    const peds = (p.pedestrians || []).map((x) => `  ${x.kind} ${x.direction} · ${x.departS}s · ${x.speedMps} m/s · ${x.approachM} m`);
    const params = [
      `${T.ego}: ${kmh(p.egoSpeedMps)}`, `${T.peds}:`, ...peds, `${T.parked}: ${p.parkedCount ?? '–'}`,
      `${T.oncoming}: ${p.oncomingPeriodS ? T.every(p.oncomingPeriodS) : T.none}`,
      ...(item.rdshq ? [T.window(item.rdshq.simWindowS[0], item.rdshq.simWindowS[1])] : []),
    ];
    const judge = [`${T.label}: ${s.label ?? ''}`, `${T.minDist}: ${s.minPedDistanceM ?? '–'} m`,
      `${T.ttc}: ${s.minPedTtcS ?? '–'} s`, `${T.yielded}: ${s.yielded ? T.yes : T.no}`];
    const ontology = Object.entries(item.ontology || {}).map(([k, v]) => `${k}: ${v}`);

    document.getElementById('scenarioModalBody').replaceChildren(
      video,
      el('p', { style: 'margin:8px 0 16px' }, link(process, T.process), osc && ' · ', osc && link(osc, T.osc)),
      el('h3', {}, T.params), el('div', { className: 'data-box' }, params.join('\n')),
      el('h3', {}, T.judge), el('div', { className: 'data-box' }, judge.join('\n')),
      el('h3', {}, T.ontology), el('div', { className: 'data-box' }, ontology.join('\n')),
      el('p', { style: 'color:#9aa0a6;font-size:13px;margin-top:12px' },
        `${T.request}: ${item.focus || '-'} · ${T.published}: ${date(item.publishedAt)} · ${item.model || ''}`),
    );
    document.getElementById('scenarioModal').classList.add('active');
  }

  fetch('/api/pipeline/gallery')
    .then((r) => { if (!r.ok) throw new Error(r.statusText); return r.json(); })
    .then(({ items }) => {
      if (!items.length) { note(T.empty); return; }
      items.forEach((item) => {
        const tags = [item.sumo && item.sumo.label, ...Object.values(item.ontology || {}).slice(0, 4)].filter(Boolean);
        const card = el('div', { className: 'scenario-card', onclick: () => show(item) },
          el('div', { className: 'scenario-name' }, item.variantId),
          el('div', { className: 'scenario-desc' }, item.rationale || ''),
          el('div', { className: 'scenario-tags' }, tags.map((t) => el('span', { className: 'scenario-tag' }, t))));
        grid.append(card);
      });
      if (location.hash === '#published') section.scrollIntoView();
    })
    .catch(() => note(T.unavailable));
})();
