import { test } from 'node:test';
import assert from 'node:assert/strict';
import { runTurn, runCheckpoint, cancelTurn, httpPost } from './pipeline.js';

// 가짜 백엔드. 호출 순서를 기록하고, 경로별로 정해진 응답을 돌려준다.
function fakePost(overrides = {}) {
  const calls = [];
  const post = async (path, body) => {
    calls.push({ path, body });
    if (overrides[path]) return overrides[path](body);
    const canned = {
      '/retrieve': { memories: [{ id: 1 }], prompt_block: '[m:1] (2026-09-17, 사용자) 테스트' },
      '/respond': { reply: '네 알겠습니다.', emotion: 'happy', cited: [1], ms: 10 },
      '/commit': { committed: true, messages: 2 },
      '/checkpoint_due': { due: true, reason: 'turns', pending_turns: 5 },
      '/summarize': { folded: 0 },
      '/cancel': { cancelled: 'x', killed: true },
    };
    return canned[path] ?? {};
  };
  return { post, calls };
}

test('한 턴은 인출 → 응답 → 커밋 순으로 부른다', async () => {
  const { post, calls } = fakePost();
  const r = await runTurn(post, '커피 좋아해');
  assert.deepEqual(calls.map((c) => c.path), ['/retrieve', '/respond', '/commit']);
  assert.equal(r.reply, '네 알겠습니다.');
  assert.equal(r.emotion, 'happy');
  assert.equal(r.committed, true);
});

test('턴마다 새 turn_id를 만들고 모든 호출에 싣는다', async () => {
  const { post, calls } = fakePost();
  const a = await runTurn(post, '첫째');
  const b = await runTurn(post, '둘째');
  assert.notEqual(a.turnId, b.turnId, '턴마다 달라야 한다');
  for (const c of calls) assert.ok(c.body.turn_id, `${c.path}에 turn_id가 없다`);
});

test('응답이 취소되면 커밋하지 않는다', async () => {
  const { post, calls } = fakePost({ '/respond': () => ({ cancelled: true }) });
  const r = await runTurn(post, '취소될 발화');
  assert.equal(r.cancelled, true);
  assert.ok(!calls.some((c) => c.path === '/commit'), '취소된 턴을 커밋했다');
});

test('체크포인트는 due가 거짓이면 아무것도 부르지 않는다', async () => {
  const { post, calls } = fakePost({ '/checkpoint_due': () => ({ due: false, reason: null, pending_turns: 0 }) });
  const r = await runCheckpoint(post);
  assert.equal(r.ran, false);
  assert.deepEqual(calls.map((c) => c.path), ['/checkpoint_due']);
});

test('KG writes are server-owned; checkpoint only folds context', async () => {
  const { post, calls } = fakePost({ '/checkpoint_due': () => ({ summarize_due: true }), '/summarize': () => ({ folded: 5 }) });
  const result = await runCheckpoint(post);
  assert.deepEqual(calls.map(c => c.path), ['/checkpoint_due', '/summarize']);
  assert.equal(result.summarized, true);
});

test('진행 이벤트가 단계마다 흐른다', async () => {
  const { post } = fakePost();
  const seen = [];
  await runTurn(post, '안녕', { onEvent: (e) => seen.push(e.stage) });
  assert.deepEqual(seen, ['retrieve', 'respond', 'commit']);
});

test('취소는 turn_id를 그대로 넘긴다', async () => {
  const { post, calls } = fakePost();
  await cancelTurn(post, 'turn-abc');
  assert.equal(calls[0].path, '/cancel');
  assert.equal(calls[0].body.turn_id, 'turn-abc');
});

// ---- httpPost: 스테이지 500은 성공 응답이 아니다 -------------------------------------------
// server.py는 모든 스테이지 예외를 `500 + {"error": ...}`로 답한다. 유효한 JSON이라
// fetch는 reject하지 않고 res.json()도 성공한다 — res.ok를 안 보면 실패가 *성공 응답*으로 흘러
// reply/emotion/cited가 전부 undefined인 채 체인이 계속되고, 사용자는 빈 말풍선과 침묵만 받는다.
function fakeFetch(status, payload, { json = true } = {}) {
  return async () => ({
    ok: status >= 200 && status < 300,
    status,
    json: async () => { if (!json) throw new SyntaxError('not json'); return payload; },
  });
}

async function withFetch(impl, fn) {
  const real = globalThis.fetch;
  globalThis.fetch = impl;
  try { return await fn(); } finally { globalThis.fetch = real; }
}

test('스테이지가 500이면 httpPost는 서버 error 메시지로 던진다', async () => {
  await withFetch(fakeFetch(500, { error: 'RuntimeError: claude failed (1): rate limit' }), async () => {
    await assert.rejects(
      () => httpPost('/respond', { text: '안녕' }),
      /claude failed/,
      '500을 성공 응답으로 통과시켰다',
    );
  });
});

test('요약만 밀렸어도 실행하고 KG 해석은 건너뛴다', async () => {
  const calls = [];
  await runCheckpoint(async (path) => {
    calls.push(path);
    return path === '/checkpoint_due' ? { due: false, summarize_due: true } : {};
  });
  assert.deepEqual(calls, ['/checkpoint_due', '/summarize']);
});

test('본문이 JSON이 아닌 500도 조용히 통과하지 않는다', async () => {
  await withFetch(fakeFetch(502, null, { json: false }), async () => {
    await assert.rejects(() => httpPost('/commit', {}), /HTTP 502/);
  });
});

test('200이면 본문을 그대로 돌려준다 (정상 경로는 그대로)', async () => {
  await withFetch(fakeFetch(200, { reply: '네', emotion: 'happy' }), async () => {
    assert.deepEqual(await httpPost('/respond', { text: '안녕' }), { reply: '네', emotion: 'happy' });
  });
});

// 500이 예외가 되면 화면의 기존 오류 경로로 들어간다 — 그게 이 가드의 값이다.
// (말풍선·HUD·0이 아닌 종료 코드가 이미 거기 달려 있다.) 체인이 그냥 이어지지 않는지 확인한다.
test('/respond가 500이면 체인이 커밋까지 가지 않는다', async () => {
  const calls = [];
  await withFetch(async (path) => {
    calls.push(path);
    if (path === '/respond') return { ok: false, status: 500, json: async () => ({ error: '터졌다' }) };
    return { ok: true, status: 200, json: async () => ({ prompt_block: '(없음)' }) };
  }, async () => {
    await assert.rejects(() => runTurn(httpPost, '안녕'), /터졌다/);
  });
  assert.ok(!calls.includes('/commit'), '응답이 실패했는데 커밋까지 갔다');
});


test('response context provenance reaches commit, including speculative response', async () => {
  const { post, calls } = fakePost({ '/respond': () => ({ reply:'ok', emotion:'neutral', cited:[], memory_epoch:'epoch', context_ids:[3,4] }) });
  await runTurn(post,'question');
  assert.deepEqual(calls.find(c=>c.path==='/commit').body.context_ids,[3,4]);
  const speculative = await runTurn(post,'question',{commit:false});
  assert.deepEqual(speculative.contextIds,[3,4]);
});
