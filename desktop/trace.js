// Renders GET /trace (server.trace_log): one block per input with the steps that served it.
const STAGES = {
  retrieve_client: '인출 (화면 표시용)', retrieve_respond: '인출 (답변 근거)', graph_search: '그래프 검색',
  respond: '답변 생성', memory_extract: '기억 추출', memory_extract_skipped: '기억 추출 생략 (규칙)', memory_evaluate: '독립 검증',
  graph_ingest: '그래프 반영', graph_remove: '그래프에서 제거', graph_judge: '관계 판정 (Claude)',
  rolling_summary: '대화 요약', relations_backfill: '관계 소급 분석', relations_check: '관계 소급 검증',
};
// Outcomes are stored as stable English identifiers (memory/trace.py); shown in Korean here.
const KEYS = {memories: '인출된 기억', candidates: '후보', degraded: '그래프 미사용', edges: '검색된 사실', cited: '인용',
  emotion: '감정', reply_chars: '답변 글자 수', claims: '주장', dropped_without_user_source: '사용자 근거 없어 제외',
  accepted: '채택', rejected: '거절', memory_ids: '기억', relation: '관계',
  judgement: '판정', reason: '사유', folded_turns: '요약한 턴', proposed: '제안', added: '반영한 기억 수', rebuild: '전체 재구축'};
const NEXT = {respond: '답변 생성', commit: '대화 저장 → 기억 추출 대기', memory_evaluate: '독립 검증',
  graph_ingest: '그래프 반영', searchable: '검색 가능', 'rank and budget': '순위·예산으로 선별',
  'done (no memory)': '종료 (저장할 기억 없음)', 'invalidate older edge': '이전 사실 무효화',
  'merge into existing edge': '기존 사실에 합침', 'add edge': '새 사실로 추가', relations_check: '관계 검증',
  done: '종료', 'summary stored': '요약 저장', cancelled: '취소됨', 'cancelled (memory changed)': '취소됨 (기억 변경)'};
const value = v => v === true ? '예' : v === false ? '아니오'
  : typeof v === 'string' ? v.replace(/^duplicate$/, '중복').replace(/^contradicts (\d+)$/, '모순 $1건').replace(/^coexists$/, '공존').replace(/^question only$/, '회상·가정·양자택일 질문만 있음') : v;
const STATUS = {pending: '기억 추출 대기', running: '기억 추출 중', done: '처리 완료', dead: '기억 추출 실패'};
const $ = id => document.getElementById(id);
const el = (tag, attrs = {}, ...children) => {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  node.append(...children.filter(c => c !== null && c !== undefined));
  return node;
};
const num = v => (v === null || v === undefined ? '' : Number(v).toLocaleString('ko-KR'));
const usd = v => (v ? '$' + Number(v).toFixed(4) : '');

function describe(outcome) {
  if (!outcome) return '';
  return Object.entries(outcome).filter(([k, v]) => k !== 'next' && v !== null && v !== undefined)
    .map(([k, v]) => `${KEYS[k] || k}: ${Array.isArray(v) ? (k.includes('memor') || k === 'cited' ? v.map(id => 'm:' + id).join(' ') : v.join(', ')) || '없음' : value(v)}`)
    .join(' · ');
}

function table(calls) {
  const head = el('tr', {}, ...['#', '단계', '모델', '입력 토큰', '캐시 읽기/쓰기', '출력 토큰', '임베딩 토큰', '비용', '시간', '결과', '다음 이동']
    .map(h => el('th', {scope: 'col'}, h)));
  const rows = calls.map((c, i) => {
    const shared = c.turns.length > 1 ? el('span', {class: 'shared', title: c.turns.length + '개 입력이 함께 쓴 호출'}, ` 공유 ${c.turns.length}`) : null;
    const flag = c.error ? el('span', {class: 'error'}, ` 오류: ${c.error}`) : c.cancelled ? el('span', {class: 'error'}, ' 취소됨') : null;
    return el('tr', {},
      el('td', {}, String(i + 1)),
      el('td', {}, STAGES[c.purpose] || c.purpose, shared, flag),
      el('td', {class: 'model'}, c.model || ''),
      el('td', {class: 'n'}, num(c.input_tokens)),
      el('td', {class: 'n'}, c.cache_read_tokens || c.cache_write_tokens ? `${num(c.cache_read_tokens || 0)} / ${num(c.cache_write_tokens || 0)}` : ''),
      el('td', {class: 'n'}, num(c.output_tokens || null)),
      el('td', {class: 'n'}, num(c.embed_tokens || null)),
      el('td', {class: 'n'}, usd(c.cost_usd)),
      el('td', {class: 'n'}, c.ms === null ? '' : `${num(c.ms)}ms`),
      el('td', {class: 'outcome'}, describe(c.outcome)),
      el('td', {}, NEXT[c.outcome?.next] || c.outcome?.next || ''));
  });
  return el('div', {class: 'scroll'}, el('table', {}, el('thead', {}, head), el('tbody', {}, ...rows)));
}

function totals(calls) {
  const sum = {input: 0, output: 0, embed: 0, cost: 0, ms: 0};
  for (const c of calls) {
    const share = 1 / Math.max(1, c.turns.length);
    sum.input += (c.input_tokens || 0) * share; sum.output += (c.output_tokens || 0) * share;
    sum.embed += (c.embed_tokens || 0) * share; sum.cost += (c.cost_usd || 0) * share; sum.ms += (c.ms || 0) * share;
  }
  return `입력 ${num(Math.round(sum.input))} · 출력 ${num(Math.round(sum.output))} · 임베딩 ${num(Math.round(sum.embed))} 토큰 · `
    + `${usd(sum.cost) || '$0'} · ${(sum.ms / 1000).toFixed(1)}초`;
}

function turnBlock(turn) {
  const memories = turn.memories.length
    ? el('ul', {class: 'memories'}, ...turn.memories.map(m => el('li', {},
        el('b', {}, `m:${m.id}`), ' ', m.statement, el('span', {class: 'meta'}, ` 그래프: ${m.graph_status}${m.archived ? ' · 보관됨' : ''}`))))
    : el('p', {class: 'meta'}, '이 입력에서 확정된 기억 없음');
  return el('article', {},
    el('header', {},
      el('h2', {}, turn.user_text),
      el('p', {class: 'meta'}, `${turn.created_at.replace('T', ' ').slice(0, 19)} · ${STATUS[turn.status] || turn.status}`
        + (turn.error ? ` (${turn.error})` : '') + (turn.reply_chars ? ` · 답변 ${turn.reply_chars}자` : '') + ` · ${turn.turn_key}`),
      el('p', {class: 'total'}, '합계: ' + totals(turn.calls))),
    turn.calls.length ? table(turn.calls) : el('p', {class: 'meta'}, '기록된 호출 없음'),
    el('h3', {}, '만들어진 기억'), memories);
}

async function load() {
  $('status').textContent = '불러오는 중…';
  try {
    const response = await fetch('/trace?limit=' + $('limit').value);
    if (!response.ok) throw Error('HTTP ' + response.status);
    const data = await response.json();
    $('turns').replaceChildren(...(data.turns.length ? data.turns.map(turnBlock) : [el('p', {}, '아직 기록된 입력이 없습니다.')]));
    $('other').replaceChildren(data.unattributed.length ? table(data.unattributed) : el('p', {class: 'meta'}, '없음'));
    $('status').textContent = `입력 ${data.turns.length}개 · ${new Date().toLocaleTimeString('ko-KR')} 기준`;
  } catch (error) {
    $('status').textContent = '기록을 불러오지 못했습니다: ' + error.message;
  }
}
$('refresh').addEventListener('click', load);
$('limit').addEventListener('change', load);
load();

// Expansion candidates (GET /vocabulary): relations outside the vocabulary and how Claude settled them.
const JUDGED = {contradicts: '대체', coexists: '누적', first: '첫 등장', negation: '부정 짝', unjudged: '판정 안 됨',
  'pair-duplicate': '다른 관계와 중복', 'pair-contradicts': '다른 관계와 모순', 'pair-coexists': '다른 관계와 공존',
  'engine-extracted': '이전 자동 추출이 이름 붙임'};
const REASON = {subject: '주어·소유 오류', negation: '부정 누락', hypothetical: '가정·질문', ambiguous: '모호한 대상',
  unsupported: '말하지 않은 내용', relation: '관계가 문장과 다름', date: '날짜', other: '기타', omitted: '판정 누락'};
const KIND = {single: '대체 (새 값이 이전 값을 바꿈)', multi: '누적 (값이 쌓임)'};

async function post(path, body) {
  const response = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok || data.error) throw Error(data.error || 'HTTP ' + response.status);
  return data;
}

async function loadVocabulary() {
  $('vocab-status').textContent = '불러오는 중…';
  try {
    const data = await (await fetch('/vocabulary')).json();
    const act = (label, cls, fn) => {const b = el('button', {type: 'button', class: cls}, label); b.addEventListener('click', fn); return b;};
    const change = (fn, done) => async () => {
      try {await fn(); await loadVocabulary(); $('vocab-status').textContent = done;}
      catch (error) {$('vocab-status').textContent = '실패: ' + error.message;}
    };
    const head = ['관계', '관측', '판정 분포', '추천', '예시 기억', '확정'].map(h => el('th', {scope: 'col'}, h));
    const rows = data.candidates.map(c => el('tr', {},
      el('td', {class: 'model'}, c.relation),
      el('td', {class: 'n'}, `${c.count}회 (근거 ${c.evidence})`),
      el('td', {}, Object.entries(c.judgements).map(([k, n]) => `${JUDGED[k] || k} ${n}`).join(' · ')),
      el('td', {class: c.suggestion ? 'suggest' : 'meta'}, c.suggestion ? KIND[c.suggestion] : `보류 (근거 ${data.rule.min_evidence}회 이상, ${Math.round(data.rule.share * 100)}% 일치 필요)`),
      el('td', {}, ...c.examples.map(m => el('div', {}, `m:${m.id} ${m.statement}`))),
      el('td', {},
        act('대체로 확정', c.suggestion === 'single' ? '' : 'secondary', change(() => post('/vocabulary_promote', {relation: c.relation, kind: 'single'}), `${c.relation}: 대체로 확정했습니다.`)),
        act('누적으로 확정', c.suggestion === 'multi' ? '' : 'secondary', change(() => post('/vocabulary_promote', {relation: c.relation, kind: 'multi'}), `${c.relation}: 누적으로 확정했습니다.`)))));
    $('candidates').replaceChildren(rows.length
      ? el('div', {class: 'scroll'}, el('table', {}, el('thead', {}, el('tr', {}, ...head)), el('tbody', {}, ...rows)))
      : el('p', {class: 'meta'}, '사전에 없는 관계가 아직 없습니다.'));
    $('learned').replaceChildren(data.learned.length
      ? el('div', {class: 'scroll'}, el('table', {}, el('tbody', {}, ...data.learned.map(l => el('tr', {},
          el('td', {class: 'model'}, l.relation), el('td', {}, KIND[l.kind]),
          el('td', {}, act('되돌리기', 'secondary', change(() => post('/vocabulary_demote', {relation: l.relation}), `${l.relation}: 다시 Claude 판정으로 돌렸습니다.`))))))))
      : el('p', {class: 'meta'}, '없음'));
    $('empty').replaceChildren(data.empty_extractions.length
      ? el('ul', {class: 'memories'}, ...data.empty_extractions.map(e => el('li', {}, e.user_text, el('span', {class: 'meta'}, ` ${e.created_at}`))))
      : el('p', {class: 'meta'}, '없음'));
    const rj = data.rejections;
    $('rejections').replaceChildren(el('p', {class: 'meta'}, `최근 검증 ${rj.checks}회, 제안 ${rj.claims}개 중 거절 ${rj.rejected}개`),
      ...(rj.reasons.length ? [el('div', {class: 'scroll'}, el('table', {},
        el('thead', {}, el('tr', {}, ...['사유', '횟수', '상태', '예시 입력'].map(h => el('th', {scope: 'col'}, h)))),
        el('tbody', {}, ...rj.reasons.map(r => el('tr', {},
          el('td', {}, REASON[r.reason] || r.reason),
          el('td', {class: 'n'}, `${r.count}회`),
          el('td', {class: r.flagged ? 'suggest' : 'meta'}, r.flagged ? '검토 필요' : `관찰 중 (${rj.rule.flag}회부터 검토)`),
          el('td', {}, ...r.examples.map(e => el('div', {}, e.user_text))))))))] : []));
    $('builtin').textContent = `대체: ${data.builtin.single.join(', ')} / 누적: ${data.builtin.multi.join(', ')}`;
    $('vocab-status').textContent = `후보 ${data.candidates.length}개`;
  } catch (error) {
    $('vocab-status').textContent = '불러오지 못했습니다: ' + error.message;
  }
}

function show(tab) {
  for (const [id, view] of [['tab-turns', 'view-turns'], ['tab-vocab', 'view-vocab']]) {
    $(id).setAttribute('aria-selected', String(id === tab)); $(view).hidden = id !== tab;
  }
  if (tab === 'tab-vocab') loadVocabulary();
}
$('tab-turns').addEventListener('click', () => show('tab-turns'));
$('tab-vocab').addEventListener('click', () => show('tab-vocab'));
