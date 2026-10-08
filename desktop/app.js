import {runTurn, runCheckpoint, cancelTurn, httpPost} from '/pipeline.js';
import {STATES, frame, validSheet, hasPixels, reaction} from './sprite.js';
const $ = id => document.getElementById(id);
let current, image, state='idle', since=performance.now(), until=Infinity, pending=null, loadToken=0, retrievalError=null;
const ctx=$('pet').getContext('2d');
let drawn='';
// Shared with the overlay: {state, ms}; ms makes it a short reaction that falls back to idle.
const overlay=new BroadcastChannel('phro-motion');
function setMotion(next,ms) {if(state!==next)since=performance.now();state=next;until=ms?performance.now()+ms:Infinity;$('motion').textContent=next;}
function motion(next,ms) {setMotion(next,ms);overlay.postMessage({state:next,ms});}
overlay.onmessage=({data})=>setMotion(data.state,data.ms);
function draw(t) {
  if(t>until)setMotion('idle');
  if(image && current){
    const action=$('action').value;const active=action==='auto'?state:action;
    const f=frame(active,t-since,current.version,Number($('direction').value));
    const key=`${current.id}:${f.x}:${f.y}`;
    if(key!==drawn){ctx.clearRect(0,0,192,208);ctx.drawImage(image,f.x,f.y,192,208,0,0,192,208);drawn=key;}
    if($('motion').textContent!==active)$('motion').textContent=active;
  }
  requestAnimationFrame(draw);
}
requestAnimationFrame(draw);
function message(text, who='assistant') {const p=document.createElement('p');p.className='message '+who;p.textContent=text;$('chat').append(p);p.scrollIntoView({block:'nearest'});}
// Turns typed on the overlay appear here too, and this window's progress shows on the overlay card.
const activity=new BroadcastChannel('phro-activity');
activity.onmessage=({data})=>{if(data.who==='user'||data.who==='assistant')message(data.text,data.who);else if(data.who==='error')message(data.text);};
// Earlier conversation from the DB, placed above anything that arrived while loading.
fetch('/history').then(r=>r.ok?r.json():Promise.reject(Error('대화 기록을 불러오지 못했습니다.')))
  .then(({messages})=>{$('chat').prepend(...messages.map(m=>Object.assign(document.createElement('p'),{className:'message '+m.role,textContent:m.text})));$('chat').scrollTop=$('chat').scrollHeight;})
  .catch(e=>$('status').textContent=e.message);
// Status polls the small /health snapshot; the full memory list is only fetched while its panel is open.
function describe(m) {
  const busy=(m.jobs.pending||0)+(m.jobs.running||0);
  if(m.jobs.dead)return `기억 처리 실패 ${m.jobs.dead}건 · 원인 해결 후 /memory_retry`;
  if(m.error)return 'KG 오류 · '+m.error;
  if(busy)return `기억 정리 중 ${busy}건`;
  if(!m.ready)return 'KG 반영 대기 중';
  if(m.coverage?.missing)return `KG 반영 완료 · 관계 누락 ${m.coverage.missing}건(문장으로만 검색)`;
  return '기억 · KG 반영 완료';
}
async function refresh() {
  let m;
  try{const r=await fetch('/health',{signal:AbortSignal.timeout(4000)});if(!r.ok)throw Error();m=(await r.json()).memory;}
  catch{$('status').textContent='서버에 연결할 수 없음';return;}
  // A failed retrieval stays visible until a later retrieval succeeds, not until the next poll.
  $('status').textContent=(retrievalError?'최근 KG 검색 실패 · '+retrievalError+' · ':'')+describe(m);
  if($('memory').closest('details').open){
    const r=await fetch('/graph');if(!r.ok)throw Error('기억 목록을 읽지 못했습니다.');
    $('memory').textContent=JSON.stringify(await r.json(),null,2);
  }
}
let catalog=[];
// Re-read on demand: the overlay menu can import characters while the app runs.
async function loadCatalog() {
  const r=await fetch('/pets/catalog.json');if(!r.ok)throw Error('캐릭터 목록을 읽지 못했습니다.');catalog=await r.json();
  const keep=$('character').value;$('character').replaceChildren(...catalog.map(p=>new Option(p.name,p.id)));
  if(catalog.some(p=>p.id===keep))$('character').value=keep;
}
async function select() {
  const token=++loadToken;const item=catalog.find(p=>p.id===$('character').value);const img=new Image();
  try{
    img.src=item.sheet;await img.decode();
    if(!validSheet(img.naturalWidth,img.naturalHeight,item.version))throw Error('지원하지 않는 시트 규격');
    if(!hasPixels(img))throw Error('시트가 비어 있거나 손상됨');
  }catch(error){
    // A broken sheet leaves the current character selected.
    if(current)$('character').value=current.id;
    throw Error(`${item.name}: 시트를 불러오지 못했습니다 (${error.message||'decode'})`);
  }
  if(token!==loadToken)return;
  current=item;image=img;try{localStorage.setItem('phro-pet',item.id);}catch{}$('credit').textContent=`${item.name} · ${item.author} · V${item.version}`;
  $('look-label').hidden=item.version!==2;$('action').replaceChildren(new Option('대화에 맞춰 자동','auto'),...STATES.map(s=>new Option(s,s)));
  if(item.version===2)$('action').add(new Option('방향 보기','look'));
}
$('form').addEventListener('submit',async e=>{
  e.preventDefault();if(pending)return;const text=$('text').value.trim();if(!text)return;
  const op={id:crypto.randomUUID(),cancelled:false};pending=op;$('send').disabled=true;$('cancel').disabled=false;
  message(text,'user');$('text').value='';motion('running');
  activity.postMessage({who:'user',text});activity.postMessage({who:'status',text:'기억 찾는 중',body:text,busy:true});
  try {
    const result=await runTurn(httpPost,text,{turnId:op.id,onEvent:event=>{
      if(event.stage==='retrieve'){
        retrievalError=event.degraded?(event.error||'검색 불가'):null;
        $('retrieval').textContent='이번 KG 인출\n'+JSON.stringify(event,null,2);
      }
      if(op.cancelled)return;
      if(event.stage==='retrieve'){activity.postMessage({who:'status',text:'답변 만드는 중',body:text,busy:true});motion('review');}
      // The answer is shown as soon as it exists; the commit that follows is bookkeeping.
      if(event.stage==='respond'){message(event.reply);activity.postMessage({who:'assistant',text:event.reply});motion(reaction(event.emotion),1800);}
    }});
    if(op.cancelled||result.cancelled){motion('idle');return;}
    // Memory confirmation/projection continues independently on the backend.
    try{await runCheckpoint(httpPost);}catch(error){$('status').textContent=error.message;}
    await refresh();
  }catch(error){if(!op.cancelled){message(error.message);activity.postMessage({who:'error',text:error.message});motion('failed',3000);}}
  finally{if(pending===op){pending=null;$('send').disabled=false;$('cancel').disabled=true;}}
});
$('cancel').addEventListener('click',async()=>{if(!pending)return;pending.cancelled=true;motion('idle');activity.postMessage({who:'status',text:'취소됨'});try{await cancelTurn(httpPost,pending.id);}catch(e){$('status').textContent=e.message;}});
$('refresh').addEventListener('click',()=>refresh().catch(e=>$('status').textContent=e.message));
$('memory').closest('details').addEventListener('toggle',e=>{if(e.target.open)refresh().catch(err=>$('status').textContent=err.message);});
$('direction').addEventListener('input',()=>{$('action').value='look';});
try {
  await loadCatalog();
  try{const saved=localStorage.getItem('phro-pet');if(catalog.some(p=>p.id===saved))$('character').value=saved;}catch{}
  $('character').addEventListener('change',()=>select().catch(e=>$('status').textContent=e.message));
  // The overlay menu can switch characters too.
  window.addEventListener('storage',async e=>{
    if(e.key!=='phro-pet'||$('character').value===e.newValue)return;
    try{
      if(!catalog.some(p=>p.id===e.newValue))await loadCatalog();
      if(catalog.some(p=>p.id===e.newValue)){$('character').value=e.newValue;await select();}
    }catch(err){$('status').textContent=err.message;}
  });
  try{await select();}catch(err){$('status').textContent=err.message;$('character').value=catalog[0].id;await select();}
  await refresh();
}catch(e){$('status').textContent=e.message;}
setInterval(()=>{if(!document.hidden)refresh().catch(e=>$('status').textContent=e.message);},5000);
