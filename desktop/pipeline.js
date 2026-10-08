// Phro pipeline caller — pure ESM, no DOM, no Electron.
//
// `post(path, body)` is injected instead of calling `fetch` directly: that's the whole reason this
// module is testable. The Node test (test-pipeline.mjs) passes a fake `post` that records
// call order and returns canned responses; the pages (app.js, overlay.js, manage.js) pass the real
// fetch-based one. Do not add `window` or `require('electron')` here — that belongs elsewhere.

/** The real `post` the browser injects into everything below — it lives here, not in overlay.js,
 *  only so it can be tested at all (it uses the global `fetch`; no DOM, no Electron).
 *
 *  The `res.ok` check is the whole point. server.py answers every stage exception with
 *  `500 + {"error": "..."}`, which is valid JSON: without this, `fetch` resolves, `res.json()`
 *  succeeds and the failure flows on as a *successful* response — `reply`/`emotion`/`cited` all
 *  undefined, /commit posts a body with no reply, and the user gets an empty bubble, no sound and
 *  an `--exit-after` code of 0. Every caller below only ever inspects `.cancelled`, so the guard
 *  belongs here rather than at each of the dozen `fire()` sites. */
export async function httpPost(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
    signal: AbortSignal.timeout(310000),
  });
  if (!res.ok) {
    const detail = await res.json().then((b) => b?.error).catch(() => null);
    throw new Error(detail || `HTTP ${res.status}`);
  }
  return res.json();
}

function fire(opts, stage, ms, res = {}) {
  if (typeof opts.onEvent === "function") opts.onEvent({ stage, ms, ...res });
}

/** One turn: /retrieve → /respond → /commit. Any stage answering `{cancelled: true}` stops the
 *  chain immediately without committing.
 *
 *  `opts.commit` (default `true`) lets a caller split retrieval+response from commit — speculative
 *  execution (e.g. a voice front end) fires /retrieve+/respond while the user may still be talking, then
 *  decides later (once the final transcript is known) whether to actually commit that reply. Pass
 *  `false` to get `{turnId, reply, emotion, cited, memories}` back without ever calling /commit;
 *  the caller then calls `commitTurn` itself once it's sure the turn should be kept.
 *
 *  `opts.turnId`, if given, is used instead of generating a fresh one. The pages need the id
 *  synchronously (before this async call resolves) so a cancel arriving mid-flight can be posted
 *  against the right turn — pipeline.js's own generated id isn't visible to the caller until the
 *  whole call returns, which is too late for that. */
export async function runTurn(post, text, opts = {}) {
  const commit = opts.commit !== false;
  const turnId = opts.turnId || crypto.randomUUID();
  const t0 = Date.now();

  const retrieved = await post("/retrieve", { turn_id: turnId, text });
  fire(opts, "retrieve", Date.now() - t0, retrieved);
  if (retrieved.cancelled) return { cancelled: true, turnId };

  const t1 = Date.now();
  const responded = await post("/respond", { turn_id: turnId, text, prompt_block: retrieved.prompt_block,
    memory_epoch: retrieved.memory_epoch });
  fire(opts, "respond", Date.now() - t1, responded);
  if (responded.cancelled) return { cancelled: true, turnId };

  if (!commit) {
    return {
      turnId,
      memoryEpoch: responded.memory_epoch,
      contextIds: responded.context_ids,
      reply: responded.reply,
      emotion: responded.emotion,
      cited: responded.cited,
      memories: retrieved.memories,
    };
  }

  const committed = await commitTurn(post, { turnId, userText: text, reply: responded.reply, cited: responded.cited,
    memoryEpoch: responded.memory_epoch, contextIds: responded.context_ids }, opts);
  if (committed.cancelled) return { cancelled: true, turnId };

  return {
    turnId,
    memories: retrieved.memories,
    reply: responded.reply,
    emotion: responded.emotion,
    committed: committed.committed,
    ms: Date.now() - t0,
  };
}

/** Commits a turn that was run with `{commit: false}` (or reused from a speculative run) —
 *  the split half of runTurn's old always-commit behavior. Threads the same `/commit` body shape
 *  runTurn itself always used (`turn_id`, `user_text`, `reply`, `cited`), so a caller that decides
 *  to keep a speculative reply commits it exactly as if runTurn had done it inline. */
export async function commitTurn(post, { turnId, userText, reply, cited, memoryEpoch, contextIds }, opts = {}) {
  const t0 = Date.now();
  const committed = await post("/commit", { turn_id: turnId, user_text: userText, reply, cited, memory_epoch: memoryEpoch, context_ids: contextIds });
  fire(opts, "commit", Date.now() - t0, committed);
  return { turnId, committed: committed.committed, cancelled: !!committed.cancelled };
}

/** Conversation folding only. Confirmed memories and KG projection belong to the server worker. */
export async function runCheckpoint(post, opts = {}) {
  const t0 = Date.now();
  const due = await post("/checkpoint_due", {});
  fire(opts, "checkpoint_due", Date.now() - t0, due);
  if (!due.summarize_due) return { due: false, ran: false, summarized: false };
  const result = await post("/summarize", {});
  fire(opts, "summarize", Date.now() - t0, result);
  return { due: false, ran: true, summarized: !!result.folded };
}

/** Cancels a turn by id. The id is threaded through unchanged — cancellation never generates a
 *  new turn_id, and a cancelled turn_id is never reused for a later turn. */
export async function cancelTurn(post, turnId, opts = {}) {
  const t0 = Date.now();
  const result = await post("/cancel", { turn_id: turnId });
  fire(opts, "cancel", Date.now() - t0, result);
  return { cancelled: result.cancelled, killed: result.killed };
}
