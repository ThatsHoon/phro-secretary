// Memory management in the chat window: browse, archive (after showing what else goes with it),
// restore, and permanently delete only after the user types the confirmation word.
import {httpPost} from '/pipeline.js';
const $ = id => document.getElementById(id);
const STATUS = {linked: '검색 가능', no_relation: '관계 없음', missing: '관계 누락', pending: '반영 대기'};
const el = (tag, props = {}, ...children) => {
  const node = Object.assign(document.createElement(tag), props);
  node.append(...children);
  return node;
};

function note(text) {$('manage-note').textContent = text;}

// Fetched when the panel opens or changes; searching filters locally and shows at most SHOWN rows.
const SHOWN = 200;
let all = [];
async function memories() {
  const r = await fetch('/graph'); if (!r.ok) throw Error('기억 목록을 읽지 못했습니다.');
  all = (await r.json()).memories;
  render();
}

function render() {
  // Older (migrated) memories were never split into graph relations; analysing them uses the model.
  const unanalysed = all.filter(m => m.relations == null).length;
  $('manage-backfill').replaceChildren(...(unanalysed ? [el('button', {type: 'button', className: 'secondary',
    textContent: `관계 분석 안 된 기억 ${unanalysed}개 분석 (Claude 사용, 최대 30개씩)`, onclick: backfill})] : []));
  const query = $('manage-query').value.trim().toLowerCase();
  const list = all.filter(m => !query || m.statement.toLowerCase().includes(query));
  $('manage-list').replaceChildren(...list.slice(0, SHOWN).map(m => {
    const row = el('li', {className: 'memory'},
      el('span', {className: 'statement', textContent: m.statement}),
      el('span', {className: 'meta ' + m.graph_status, textContent: `${m.kind} · ${STATUS[m.graph_status] || m.graph_status}`}),
      el('button', {type: 'button', textContent: '보관', onclick: () => preview(m, row)}));
    return row;
  }));
  if (!list.length) $('manage-list').append(el('li', {className: 'empty', textContent: query ? '검색 결과 없음' : '확정된 기억 없음'}));
  if (list.length > SHOWN) $('manage-list').append(el('li', {className: 'empty', textContent: `${list.length}개 중 ${SHOWN}개 표시 — 검색으로 좁히세요`}));
}

async function backfill(event) {
  event.target.disabled = true;
  note('관계 분석 중…');
  try {
    const r = await httpPost('/relations_backfill', {});
    note(`${r.analysed}개 분석, ${r.accepted}개 반영(검증 통과), 남은 ${r.remaining}개. 그래프를 다시 만듭니다.`);
    await memories();
  } catch (e) {note(e.message); event.target.disabled = false;}
}

// Archiving follows provenance: other memories and whole conversations that used this fact go too.
async function preview(memory, row) {
  row.querySelector('.confirm')?.remove();
  try {
    const scope = await httpPost('/forget_preview', {memory_ids: [memory.id]});
    const others = scope.memory_ids.length - 1;
    // Later turns that may have repeated this fact lose only the reply; the user's words and memories stay.
    const text = `함께 보관: 다른 기억 ${others}개, 대화 ${scope.turns}턴` +
      (scope.reply_turns ? `, 이후 대화 ${scope.reply_turns}턴의 답변` : '') +
      (scope.untracked_turns ? ` (출처 기록 없는 이관 대화 ${scope.untracked_turns}턴 포함)` : '') +
      '. 보관은 복구할 수 있습니다.';
    const box = el('div', {className: 'confirm', role: 'alert'}, el('p', {textContent: text}),
      el('button', {type: 'button', textContent: '보관하기', onclick: () => archive(memory)}),
      el('button', {type: 'button', className: 'secondary', textContent: '취소', onclick: () => box.remove()}));
    row.append(box);
  } catch (e) {note(e.message);}
}

async function archive(memory) {
  try {
    const result = await httpPost('/forget', {memory_ids: [memory.id], reason: '사용자 보관'});
    note(`배치 ${result.batch}: 기억 ${result.memory_ids.length}개, 대화 ${result.turns}턴 보관됨`);
    await Promise.all([memories(), batches()]);
  } catch (e) {note(e.message);}
}

const BATCH = {archived: '보관됨', restored: '복구됨', purged: '완전삭제됨'};
async function batches() {
  const list = (await httpPost('/forget_batches', {})).batches;
  $('manage-batches').replaceChildren(...list.map(b => {
    const row = el('li', {className: 'batch'}, el('span', {textContent: `#${b.id} · ${BATCH[b.status] || b.status} · ${b.created_at.slice(0, 16).replace('T', ' ')}`}));
    if (b.status === 'archived') row.append(
      el('button', {type: 'button', textContent: '복구', onclick: () => run('/restore', {batch: b.id}, `배치 ${b.id} 복구됨`)}),
      el('button', {type: 'button', className: 'danger', textContent: '완전삭제…', onclick: () => purgeConfirm(b, row)}));
    return row;
  }));
}

// Irreversible: the button stays disabled until the exact word is typed.
function purgeConfirm(batch, row) {
  row.querySelector('.confirm')?.remove();
  const input = el('input', {placeholder: '삭제', 'aria-label': '확인을 위해 삭제 입력', autocomplete: 'off'});
  const go = el('button', {type: 'button', className: 'danger', textContent: '영구 삭제', disabled: true,
    onclick: () => run('/purge', {batch: batch.id, confirm: true}, `배치 ${batch.id} 영구 삭제됨`)});
  input.addEventListener('input', () => {go.disabled = input.value.trim() !== '삭제';});
  const box = el('div', {className: 'confirm', role: 'alert'},
    el('p', {textContent: `되돌릴 수 없습니다. 배치 #${batch.id}의 기억과 대화 원문이 영구 삭제됩니다. 계속하려면 "삭제"를 입력하세요.`}),
    input, go, el('button', {type: 'button', className: 'secondary', textContent: '취소', onclick: () => box.remove()}));
  row.append(box); input.focus();
}

async function run(path, body, done) {
  try {await httpPost(path, body); note(done); await Promise.all([memories(), batches()]);}
  catch (e) {note(e.message);}
}

$('manage').addEventListener('toggle', () => {if ($('manage').open) Promise.all([memories(), batches()]).catch(e => note(e.message));});
$('manage-query').addEventListener('input', render);
