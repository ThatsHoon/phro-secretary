import {frame, validSheet, hasPixels, reaction, lookDirection} from './sprite.js';
import {runTurn, runCheckpoint, cancelTurn, httpPost} from '/pipeline.js';
const $ = id => document.getElementById(id);
const phro = window.phro, canvas = $('pet');
const ctx = canvas.getContext('2d', {willReadFrequently: true});
let pet, image, loadToken = 0, drawn = '', settings = {scale: 1, lookAtCursor: false, onboarded: true};

// ---- Motion -------------------------------------------------------------------------------------
// Priority: drag/landing > conversation state > idle extras (cursor look, occasional waiting).
// Either window may run a turn; `phro-motion` carries {state, ms}, where ms makes it a short reaction.
let base = {state: 'idle', since: performance.now(), until: Infinity};
let drag = null;                       // {state, since, until}
let cursor = {dx: 0, dy: 0, at: -Infinity}, nextExtra = performance.now() + 20000, extra = null;
const motionChannel = new BroadcastChannel('phro-motion');
function setMotion(state, ms) {const t = performance.now(); base = {state, since: t, until: ms ? t + ms : Infinity};}
function motion(state, ms) {setMotion(state, ms); motionChannel.postMessage({state, ms});}
motionChannel.onmessage = ({data}) => setMotion(data.state, data.ms);

function current(t) {
  if (drag && t < drag.until) return [drag.state, t - drag.since];
  drag = null;
  if (t >= base.until) setMotion('idle');
  if (base.state !== 'idle') return [base.state, t - base.since];
  if (settings.lookAtCursor && pet?.version === 2 && t - cursor.at < 2500 && Math.hypot(cursor.dx, cursor.dy) < 320)
    return ['look', 0, lookDirection(cursor.dx)];
  if (extra && t < extra.until) return [extra.state, t - extra.since];
  if (t > nextExtra) {extra = {state: 'waiting', since: t, until: t + 1500}; nextExtra = t + 20000 + Math.random() * 20000;}
  return ['idle', t - base.since];
}
function draw(t) {
  if (image) {
    const [state, elapsed, direction] = current(t);
    const f = frame(state, elapsed, pet.version, direction);
    const key = `${pet.id}:${f.x}:${f.y}`;
    if (key !== drawn) {ctx.clearRect(0, 0, 192, 208); ctx.drawImage(image, f.x, f.y, 192, 208, 0, 0, 192, 208); drawn = key;}
  }
  requestAnimationFrame(draw);
}
phro.onCursor((dx, dy) => {cursor = {dx, dy, at: performance.now()};});

// ---- Drag, click, keyboard ----------------------------------------------------------------------
// Main moves the window from the OS cursor position, so coordinates stay correct across DPI scales.
let dragStart = null, lastX = null, dragged = false;
canvas.addEventListener('pointerdown', e => {
  if (e.button !== 0) return;
  canvas.setPointerCapture(e.pointerId); dragStart = null; lastX = null; dragged = false; phro.drag('start');
});
canvas.addEventListener('pointermove', e => {if (canvas.hasPointerCapture(e.pointerId)) phro.drag('move');});
canvas.addEventListener('lostpointercapture', () => {
  phro.drag('end');
  // A short hop on landing.
  if (dragged) {const t = performance.now(); drag = {state: 'jumping', since: t, until: t + 500};}
});
// A few pixels of travel are needed before the facing changes, so hand jitter does not flip it.
phro.onMove((x, y) => {
  if (!dragStart) {dragStart = {x, y}; lastX = x; return;}
  if (Math.hypot(x - dragStart.x, y - dragStart.y) >= 3) dragged = true;
  const dx = x - lastX;
  if (Math.abs(dx) < 3) return;
  lastX = x;
  run(dx > 0 ? 'running-right' : 'running-left', 200);
});
function run(state, ms) {
  const t = performance.now();
  drag = {state, since: drag?.state === state ? drag.since : t, until: t + ms};
}
canvas.addEventListener('click', () => {if (!dragged && base.state === 'idle') setMotion('waving', 1200);});
canvas.addEventListener('dblclick', () => {if (!dragged) compose(true);});
canvas.addEventListener('keydown', e => {
  const step = e.shiftKey ? 50 : 10;
  const moves = {ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step]};
  if (moves[e.key]) {
    e.preventDefault(); phro.nudge(...moves[e.key]);
    if (moves[e.key][0]) run(moves[e.key][0] > 0 ? 'running-right' : 'running-left', 300);
  } else if (e.key === 'Enter' || e.key === ' ') {e.preventDefault(); compose(true);}
  else if (e.key === 'Escape') hideCard();
  else if (e.key === 'ContextMenu' || (e.shiftKey && e.key === 'F10')) {e.preventDefault(); phro.menu();}
});
window.addEventListener('contextmenu', e => {e.preventDefault(); phro.menu();});

// ---- Click-through ------------------------------------------------------------------------------
// Only opaque character pixels and the panels take the mouse; everything else reaches the window behind.
let ignoring = false;
function interactive(e) {
  if (canvas.matches(':active')) return true;
  const el = document.elementFromPoint(e.clientX, e.clientY);
  if (!el || el === document.body || el === document.documentElement || el.id === 'root' || el.id === 'stage') return false;
  if (el !== canvas) return true;
  const r = canvas.getBoundingClientRect();
  const x = Math.floor((e.clientX - r.left) / r.width * 192), y = Math.floor((e.clientY - r.top) / r.height * 208);
  return ctx.getImageData(x, y, 1, 1).data[3] > 16;
}
window.addEventListener('mousemove', e => {
  if (e.buttons) return;
  const ignore = !interactive(e);
  if (ignore !== ignoring) {ignoring = ignore; phro.ignoreMouse(ignore);}
});

// ---- Open/close motion --------------------------------------------------------------------------
// CSS runs the entering animation when an element is unhidden; leaving plays .leaving first, then hides,
// so the window only shrinks after the panel has animated out.
const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
function reveal(el) {el.classList.remove('leaving'); el.hidden = false;}
function conceal(el, then) {
  if (el.hidden || el.classList.contains('leaving')) return;
  if (reducedMotion.matches) {el.hidden = true; then?.(); return;}
  el.classList.add('leaving');
  el.addEventListener('animationend', function done(e) {
    if (e.target !== el) return;
    el.removeEventListener('animationend', done);
    // A reveal() during the exit cancels it.
    if (el.classList.contains('leaving')) {el.classList.remove('leaving'); el.hidden = true; then?.();}
  });
}

// ---- Card ---------------------------------------------------------------------------------------
let cardTimer, cardDeadline = 0, cardKind = null, onCardHidden = null;
function card(title, body = '', {linger = false, busy = false, kind = null, onHidden = null} = {}) {
  clearTimeout(cardTimer);
  $('card-title').textContent = title; $('card-body').textContent = body;
  $('card-note').hidden = true; $('card').classList.toggle('busy', busy); $('card').classList.remove('expanded');
  reveal($('card')); cardKind = kind; onCardHidden = onHidden;
  // Long answers stay longer; hovering pauses the countdown.
  cardDeadline = linger ? Math.min(60000, 6000 + body.length * 60) : 0;
  if (cardDeadline && !$('card').matches(':hover')) cardTimer = setTimeout(hideCard, cardDeadline);
}
function hideCard() {
  clearTimeout(cardTimer); conceal($('card')); cardKind = null;
  const done = onCardHidden; onCardHidden = null; done?.();
}
$('card').addEventListener('mouseenter', () => clearTimeout(cardTimer));
$('card').addEventListener('mouseleave', () => {if (cardDeadline) cardTimer = setTimeout(hideCard, 4000);});
$('card-close').addEventListener('click', hideCard);
$('card-body').addEventListener('click', () => {if (!getSelection().toString()) $('card').classList.toggle('expanded');});

const activity = new BroadcastChannel('phro-activity');
activity.onmessage = ({data}) => {
  if (data.who === 'status') card(data.text, data.body || '', {busy: !!data.busy, linger: !data.busy});
  else if (data.who === 'assistant') card('답변', data.text, {linger: true});
  else if (data.who === 'error') card('오류', data.text, {linger: true});
};

// ---- Compose and turns --------------------------------------------------------------------------
let pending = null, statusTimer;
// The pencil pill steps aside and the input grows out from it; closing reverses that.
function compose(open) {
  if (open) {
    $('tools').hidden = true; reveal($('compose')); $('text').focus();
  } else {
    conceal($('compose'), () => reveal($('tools')));
  }
}
$('write').addEventListener('click', () => compose(true));
$('chat').addEventListener('click', () => phro.toggleChat());
phro.onOpenCompose(() => compose(true));
// Clicking elsewhere closes the input; the draft stays for next time.
window.addEventListener('blur', () => {if (!$('compose').hidden) compose(false);});
$('text').addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  if (pending) cancel(); else compose(false);
});
function setPending(op) {
  pending = op;
  $('send').classList.toggle('stop', !!op);
  $('send').setAttribute('aria-label', op ? '중지' : '보내기'); $('send').title = op ? '중지 (Esc)' : '보내기';
}
function status(op, title, body) {
  if (op.cancelled) return;
  clearInterval(statusTimer);
  const started = performance.now();
  const show = () => {
    const s = Math.floor((performance.now() - started) / 1000);
    card(s >= 3 ? `${title} · ${s}초` : title, body, {busy: true, kind: op.id});
  };
  show(); statusTimer = setInterval(() => {if (cardKind === op.id) show(); else clearInterval(statusTimer);}, 1000);
  activity.postMessage({who: 'status', text: title, body, busy: true});
}
// /health carries the count; /graph would ship every memory (megabytes at a few thousand) on each poll.
async function memoryCount() {
  const r = await fetch('/health'); if (!r.ok) throw Error('memory status unavailable');
  const {memory} = await r.json();
  return {count: memory.memories, busy: (memory.jobs.pending || 0) + (memory.jobs.running || 0)};
}
// Extraction runs on the server after commit; report new memories once its queue drains.
async function watchMemories(before, replyId) {
  for (let i = 0; i < 40; i++) {
    await new Promise(r => setTimeout(r, 3000));
    const now = await memoryCount();
    if (now.busy) continue;
    const added = now.count - before;
    if (added <= 0) return;
    const text = `기억 ${added}개 저장됨`;
    if (cardKind === replyId && !$('card').hidden) {$('card-note').textContent = text; $('card-note').hidden = false;}
    else if ($('card').hidden) card(text, '', {linger: true});
    return;
  }
}
$('compose').addEventListener('submit', async e => {
  e.preventDefault();
  if (pending) return cancel();
  const text = $('text').value.trim();
  if (!text) return;
  const op = {id: crypto.randomUUID(), cancelled: false}; setPending(op);
  $('text').value = '';
  activity.postMessage({who: 'user', text});
  status(op, '기억 찾는 중', text); motion('running');
  const before = await memoryCount().then(m => m.count, () => null);
  try {
    const result = await runTurn(httpPost, text, {turnId: op.id, onEvent: event => {
      if (op.cancelled) return;
      if (event.stage === 'retrieve') {status(op, '답변 만드는 중', text); motion('review');}
      // Show the answer as soon as it exists; the commit that follows is bookkeeping.
      if (event.stage === 'respond') {
        clearInterval(statusTimer);
        card('답변', event.reply, {linger: true, kind: 'reply:' + op.id});
        activity.postMessage({who: 'assistant', text: event.reply});
        motion(reaction(event.emotion), 1800);
      }
    }});
    if (op.cancelled || result.cancelled) return;
    if (before !== null) watchMemories(before, 'reply:' + op.id).catch(console.error);
    await runCheckpoint(httpPost).catch(console.error);
  } catch (error) {
    if (!op.cancelled) {
      clearInterval(statusTimer);
      card('오류', error.message, {linger: true}); activity.postMessage({who: 'error', text: error.message});
      motion('failed', 3000);
    }
  } finally {
    if (pending === op) setPending(null);
  }
});
async function cancel() {
  const op = pending; if (!op) return;
  op.cancelled = true; setPending(null); clearInterval(statusTimer); motion('idle');
  card('취소됨', '', {linger: true}); activity.postMessage({who: 'status', text: '취소됨'});
  try {await cancelTurn(httpPost, op.id);} catch (e) {card('오류', e.message, {linger: true});}
}

// ---- Server health ------------------------------------------------------------------------------
async function health() {
  let state = 'down', text = '서버에 연결할 수 없음';
  try {
    const r = await fetch('/health', {signal: AbortSignal.timeout(4000)});
    if (r.ok) {
      const m = (await r.json()).memory;
      if (m.jobs.dead) [state, text] = ['warn', `기억 처리 실패 ${m.jobs.dead}건`];
      else if (m.error) [state, text] = ['warn', `KG 오류: ${m.error}`];
      else if (!m.ready) [state, text] = ['warn', 'KG 반영 대기 중'];
      else if (m.coverage?.missing) [state, text] = ['warn', `KG 관계 누락 ${m.coverage.missing}건 (문장으로만 검색)`];
      else [state, text] = ['ok', '연결됨 · KG 반영 완료'];
    }
  } catch {}
  $('dot').dataset.state = state; $('dot').title = text; $('dot').setAttribute('aria-label', '연결 상태: ' + text);
}
$('dot').addEventListener('click', () => card('연결 상태', $('dot').title, {linger: true}));
health(); setInterval(health, 10000);

// ---- Settings, character, help ------------------------------------------------------------------
const HELP = '끌어서 옮기기 · 클릭하면 인사 · 더블클릭이나 연필로 대화\n' +
  '우클릭 또는 트레이 아이콘: 캐릭터·크기·종료\nCtrl+Shift+Space: 어디서나 입력창 열기\n캐릭터 선택 후 방향키로 이동';
function help(first) {card('사용법', HELP, {linger: true, onHidden: first ? () => phro.onboarded() : null});}
phro.onShowHelp(() => help(false));
let helpShown = false;
phro.onSettings(s => {
  settings = s;
  document.documentElement.style.setProperty('--s', s.scale);
  if (!s.onboarded && !helpShown) {helpShown = true; help(true);}
});
// The window follows #root's size, keeping the character where it is.
new ResizeObserver(() => phro.resize(Math.ceil($('root').offsetWidth), Math.ceil($('root').offsetHeight))).observe($('root'));

async function decode(item) {
  const img = new Image(); img.src = item.sheet; await img.decode();
  if (!validSheet(img.naturalWidth, img.naturalHeight, item.version) || !hasPixels(img)) throw Error('unusable sheet: ' + item.id);
  return img;
}
// The catalogue is re-read each time: characters can be imported while the app runs.
async function load() {
  const token = ++loadToken;
  const r = await fetch('/pets/catalog.json');
  if (!r.ok) throw Error('catalog unavailable');
  const catalog = await r.json();
  let id; try {id = localStorage.getItem('phro-pet');} catch {}
  let item = catalog.find(p => p.id === id) || catalog[0], img;
  try {img = await decode(item);}
  catch (error) {
    // A missing or damaged sheet falls back to the current character, or the first built-in one at startup.
    console.error(error);
    if (pet) {try {localStorage.setItem('phro-pet', pet.id);} catch {} return;}
    if (item === catalog[0]) throw error;
    item = catalog[0]; img = await decode(item);
  }
  if (token !== loadToken) return;
  pet = item; image = img; drawn = '';
  const icon = new OffscreenCanvas(32, 32).getContext('2d');
  icon.drawImage(img, 0, 0, 192, 208, 1, 0, 30, 32);
  const blob = await icon.canvas.convertToBlob({type: 'image/png'});
  const url = await new Promise(r => {const f = new FileReader(); f.onload = () => r(f.result); f.readAsDataURL(blob);});
  phro.petIcon(item.id, url);
}
window.addEventListener('storage', e => {if (e.key === 'phro-pet') load().catch(console.error);});
phro.onSelectPet(id => {try {localStorage.setItem('phro-pet', id);} catch {} load().catch(console.error);});
// The loop starts regardless of the first load: on an installed copy's first run no sheet exists yet, and the
// one the first-run download delivers (select-pet) must still be drawn.
requestAnimationFrame(draw);
load().catch(console.error);
