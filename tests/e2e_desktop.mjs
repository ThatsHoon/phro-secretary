// Desktop E2E on a synthetic DB, isolated Electron profile and a stand-in `claude` (no account, no cost).
// Run from the repo root on Windows: node tests/e2e_desktop.mjs
// Claude results are always the stand-in (tests/fake_claude); the graph is real only if the embedding model is in models/,
// and the report says which. Exit code 1 if any check fails.
import {spawn, execFileSync} from 'node:child_process';
import {mkdtempSync, mkdirSync, rmSync, readFileSync, writeFileSync, existsSync} from 'node:fs';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {DatabaseSync} from 'node:sqlite';

const root = path.resolve(import.meta.dirname, '..');
const python = path.join(root, '.venv', 'Scripts', 'python.exe');
const electron = path.join(root, 'desktop', 'node_modules', 'electron', 'dist', 'electron.exe');
const tmp = mkdtempSync(path.join(tmpdir(), 'phro-e2e-'));
const db = path.join(tmp, 'memory.db'), profile = path.join(tmp, 'profile'), bin = path.join(tmp, 'bin');
const port = 8781, debug = 9334, origin = `http://127.0.0.1:${port}`;
const sleep = ms => new Promise(r => setTimeout(r, ms));
const results = [];
const check = (name, ok, detail = '') => {results.push({name, ok: !!ok}); console.log(ok ? 'PASS' : 'FAIL', name, detail);};

const env = {...process.env, PATH: bin + path.delimiter + process.env.PATH, PHRO_MEMORY_DB: db,
  PHRO_DESKTOP_PORT: String(port), FAKE_CLAUDE_PY: python, FAKE_CLAUDE_SCRIPT: path.join(root, 'tests', 'fake_claude', 'claude.py')};

let app;
async function launch() {
  app = spawn(electron, ['.', `--remote-debugging-port=${debug}`, `--user-data-dir=${profile}`],
              {cwd: path.join(root, 'desktop'), env, stdio: 'ignore'});
  for (let i = 0; i < 60; i++) {
    await sleep(1000);
    try {
      const targets = await (await fetch(`http://127.0.0.1:${debug}/json`)).json();
      const overlay = targets.find(t => t.url === origin + '/overlay.html'), chat = targets.find(t => t.url === origin + '/');
      if (overlay && chat) {
        // The DevTools target exists before its document has loaded.
        const pages = {overlay: await connect(overlay), chat: await connect(chat)};
        // A provisional about:blank document can already report 'complete'; wait for the real elements.
        const ready = id => `document.readyState==='complete'&&!!document.getElementById('${id}')`;
        if (await until(async () => await pages.overlay(ready('pet')) && await pages.chat(ready('chat')))) return pages;
      }
    } catch {}
  }
  throw Error('app did not start');
}
async function quit() {app.kill(); await new Promise(r => app.once('exit', r)); await sleep(1500);}
async function connect(target) {
  const ws = new WebSocket(target.webSocketDebuggerUrl); await new Promise(r => ws.onopen = r);
  let id = 0; const pending = new Map();
  ws.onmessage = e => {const m = JSON.parse(e.data); pending.get(m.id)?.(m);};
  return async expression => {
    const m = await new Promise(r => {pending.set(++id, r); ws.send(JSON.stringify({id, method: 'Runtime.evaluate',
      params: {expression, awaitPromise: true, returnByValue: true}}));});
    if (m.result.exceptionDetails) throw Error(m.result.exceptionDetails.exception?.description || 'page error');
    return m.result.result.value;
  };
}
async function until(fn, ms = 15000) {
  for (const end = Date.now() + ms; Date.now() < end; await sleep(250)) {const v = await fn(); if (v) return v;}
  return null;
}
const turns = () => {const conn = new DatabaseSync(db, {readOnly: true}); try {return conn.prepare('SELECT turn_key FROM turns').all().map(r => r.turn_key);} finally {conn.close();}};

try {
  mkdirSync(bin);
  execFileSync('powershell', ['-NoProfile', '-Command',
    `Add-Type -TypeDefinition (Get-Content -Raw '${path.join(root, 'tests', 'fake_claude', 'stub.cs')}') ` +
    `-OutputAssembly '${path.join(bin, 'claude.exe')}' -OutputType ConsoleApplication`]);
  let {overlay, chat} = await launch();
  const card = () => overlay("[document.getElementById('card').hidden?'':document.getElementById('card-title').textContent, document.getElementById('card-body').textContent]");
  const send = text => overlay(`document.getElementById('card-close').click();document.getElementById('write').click();` +
    `document.getElementById('text').value=${JSON.stringify(text)};document.getElementById('compose').requestSubmit();`);

  // Sheet loading and character switch.
  const opaque = () => overlay(`(()=>{const d=document.getElementById('pet').getContext('2d').getImageData(0,0,192,208).data;let n=0;for(let i=3;i<d.length;i+=4)if(d[i]>16)n++;return n})()`);
  check('sheet renders on the overlay', await until(async () => (await opaque()) > 500));
  const pets = await chat("[...document.getElementById('character').options].map(o=>o.value)");
  await chat(`const s=document.getElementById('character');s.value=${JSON.stringify(pets.at(-1))};s.dispatchEvent(new Event('change'))`);
  check('character switch reaches the overlay', await until(() => overlay(`localStorage.getItem('phro-pet')===${JSON.stringify(pets.at(-1))}`)), pets.at(-1));

  // Imported sheets (what the menu's folder picker installs): a good one is selectable at once,
  // a broken one leaves the current character in place.
  const {installPet} = (await import('node:module')).createRequire(import.meta.url)('../desktop/pets.cjs');
  const builtIn = JSON.parse(readFileSync(path.join(root, 'desktop', 'pets', 'catalog.json'), 'utf8'));
  const donor = builtIn.find(p => existsSync(path.join(root, 'desktop', p.sheet)));
  if (donor) {
    const kitDir = id => path.join(tmp, 'kit-' + id);
    const makeKit = (id, sheet) => {mkdirSync(kitDir(id)); writeFileSync(path.join(kitDir(id), 'pet.json'), JSON.stringify({id, displayName: id}));
      writeFileSync(path.join(kitDir(id), 'spritesheet.webp'), sheet); return kitDir(id);};
    const donorSheet = readFileSync(path.join(root, 'desktop', donor.sheet));
    // What the menu does after an import: main sends select-pet, the overlay stores the id and reloads.
    const selectPet = id => overlay(`localStorage.setItem('phro-pet',${JSON.stringify(id)});window.dispatchEvent(new StorageEvent('storage',{key:'phro-pet'}))`);
    installPet(makeKit('e2e-good', donorSheet), path.join(profile, 'pets'), builtIn.map(p => p.id));
    await selectPet('e2e-good');
    check('imported character appears in both windows', await until(async () =>
      await chat("[...document.getElementById('character').options].some(o=>o.value==='e2e-good')") &&
      await overlay("localStorage.getItem('phro-pet')==='e2e-good'")));
    // Valid header (passes the size check) but an undecodable body.
    installPet(makeKit('e2e-broken', donorSheet.subarray(0, 64)), path.join(profile, 'pets'), builtIn.map(p => p.id));
    await selectPet('e2e-broken');
    check('broken imported sheet keeps the current character', await until(() => overlay("localStorage.getItem('phro-pet')==='e2e-good'")) &&
          (await opaque()) > 500);
    await selectPet(pets.at(-1));
    await until(() => chat(`document.getElementById('character').value===${JSON.stringify(pets.at(-1))}`));
  } else check('imported character checks [skipped: no downloaded sheet]', true);

  // Conversation from the overlay (stand-in Claude), mirrored in the chat window and committed.
  await overlay("document.getElementById('card-close').click()");
  await send('나는 서울에 살아.');
  const answered = await until(async () => {const [t, b] = await card(); return t === '답변' && b.startsWith('대역 답변') && b;});
  check('overlay turn shows the answer card [fake claude]', answered, answered || JSON.stringify(await card()));
  check('chat window mirrors the overlay turn', await until(() => chat("document.querySelectorAll('#chat .message').length>=2")));
  check('turn committed to the DB', await until(() => turns().length === 1));
  const savedNote = await until(() => overlay("document.getElementById('card').hidden?'':document.getElementById('card').textContent.match(/기억 \\d+개 저장됨/)?.[0]"), 90000);
  check('overlay reports the saved memory [fake claude]', savedNote, savedNote || JSON.stringify(await card()));
  // Processing record: the turn's foreground and background steps, and the page that shows them.
  const steps = await until(() => chat(`fetch('/trace').then(r=>r.json()).then(d=>{const c=d.turns[0]?.calls||[];
    return ['respond','memory_extract','memory_evaluate'].every(p=>c.some(x=>x.purpose===p&&x.outcome?.next))&&c.map(x=>x.purpose).join(',')})`), 30000);
  const pageServed = await chat(`Promise.all(['/trace.html','/trace.js','/trace.css'].map(u=>fetch(u).then(r=>r.ok))).then(a=>a.every(Boolean))`);
  check('processing record traces the turn', steps && pageServed, steps || '');

  // Cancel race: cancel while the reply is still being generated; nothing may be committed.
  const before = turns().length;
  await send('SLOW 취소될 질문');
  await sleep(1500);
  // Like a user: reopen the input if focus changes closed it, then press the stop button.
  await overlay("document.getElementById('write').click();document.getElementById('send').click()");
  check('cancel shows on the card', await until(async () => (await card())[0] === '취소됨'), JSON.stringify(await card()));
  await sleep(3000);
  check('cancelled turn is not committed', turns().length === before, JSON.stringify(turns()));

  // Error path: the failure is shown and the next turn still works.
  await send('FAIL 오류 질문');
  check('model failure shows an error card', await until(async () => (await card())[0] === '오류'), JSON.stringify(await card()));
  await send('다시 질문');
  check('next turn works after an error', await until(async () => (await card())[0] === '답변'));

  // Graph: real only when local services are up.
  // Extraction runs in the background worker after the commit.
  const graphNow = async () => (await fetch(origin + '/graph')).json();
  await until(async () => (await graphNow()).memories.some(m => m.statement.includes('서울')), 60000);
  const memory = await graphNow();
  const graphReal = memory.memory.ready && memory.memories.some(m => m.graph_status === 'linked');
  check(`memory extracted from the overlay turn [fake claude, ${graphReal ? 'real' : 'unavailable'} graph]`,
        memory.memories.some(m => m.statement.includes('서울')), JSON.stringify(memory.memories.map(m => [m.statement, m.graph_status])));

  // Memory management in the chat window: preview the cascade, archive, restore, purge only after typing.
  const seoul = memory.memories.find(m => m.statement.includes('서울'));
  if (seoul) {
    const row = `[...document.querySelectorAll('#manage-list li')].find(l=>l.textContent.includes('서울'))`;
    const visible = async () => (await (await fetch(origin + '/graph')).json()).memories.some(m => m.id === seoul.id);
    const batchRow = `document.querySelector('#manage-batches li')`;
    // A step that never happens stops here with the panel's own message, instead of a null click later.
    const need = async (fn, step) => {if (!await until(fn)) throw Error(`${step}: ${await chat("document.getElementById('manage-note').textContent")}`);};
    await chat(`document.getElementById('manage').open=true`);
    await need(() => chat(`!!${row}`), 'memory row');
    await chat(`${row}.querySelector('button').click()`);
    const preview = await until(() => chat(`${row}.querySelector('.confirm p')?.textContent`));
    check('archive shows what goes with it before confirming', preview && /대화 [1-9]\d*턴/.test(preview) && preview.includes('복구'), preview);
    await chat(`${row}.querySelector('.confirm button').click()`);
    check('archive hides the memory', await until(async () => !(await visible())));
    await need(() => chat(`${batchRow}?.textContent.includes('보관됨')`), 'archived batch');
    await chat(`${batchRow}.querySelector('button').click()`);
    check('restore brings it back', await until(visible));
    // The list re-renders once more after a restore; a click on the row it replaces is lost, so click until the
    // confirmation is on the current row.
    await need(async () => {
      await chat(`${row}?.querySelector('.confirm button') || ${row}?.querySelector('button')?.click()`);
      return chat(`!!${row}?.querySelector('.confirm button')`);
    }, 'second preview');
    await chat(`${row}.querySelector('.confirm button').click()`);
    await need(async () => !(await visible()) && await chat(`${batchRow}?.textContent.includes('보관됨')`), 'second archive');
    await chat(`${batchRow}.querySelector('button.danger').click()`);
    const purgeButton = `${batchRow}.querySelector('.confirm button.danger')`;
    const lockedFirst = await until(() => chat(`${purgeButton}?.disabled===true`));
    await chat(`const i=${batchRow}.querySelector('.confirm input');i.value='삭제';i.dispatchEvent(new Event('input'))`);
    const unlocked = await chat(`${purgeButton}.disabled===false`);
    await chat(`${purgeButton}.click()`);
    const purged = await until(() => chat(`${batchRow}?.textContent.includes('완전삭제됨')`));
    const text = (() => {const conn = new DatabaseSync(db, {readOnly: true});
      try {return conn.prepare("SELECT COUNT(*) n FROM messages WHERE text LIKE '%서울%'").get().n;} finally {conn.close();}})();
    check('purge needs the typed word and erases the text', lockedFirst && unlocked && purged && text === 0, JSON.stringify({lockedFirst, unlocked, purged, text}));
  } else check('memory management checks [skipped: no memory extracted]', false);

  // Restart: position, character and conversation survive.
  await send('기록 확인 질문');
  await until(async () => (await card())[0] === '답변');
  const settings = path.join(profile, 'overlay.json');
  await overlay("window.phro.nudge(-40,-30)"); await sleep(800);
  const saved = existsSync(settings) && JSON.parse(readFileSync(settings, 'utf8')).home;
  const committed = turns().length;
  await quit();
  ({overlay, chat} = await launch());
  check('settings live in the isolated profile', !!saved, JSON.stringify(saved));
  check('character restored after restart', await until(() => overlay(`localStorage.getItem('phro-pet')===${JSON.stringify(pets.at(-1))}`)));
  check('conversation persisted across restart', turns().length === committed, String(committed));
  const shown = await until(() => chat("[...document.querySelectorAll('#chat .message')].map(p=>p.textContent).join('|')"));
  check('chat window restores the visible conversation', shown && shown.includes('기록 확인 질문') && !shown.includes('서울'), shown);
  const after = await overlay('[screenX+innerWidth/2, screenY]');
  check('overlay position restored', saved && Math.abs(after[0] - saved.cx) <= 2 && Math.abs(after[1] - saved.y) <= 2, JSON.stringify(after));

  // Displays: a spot on another monitor is kept; a spot on a monitor that is gone falls back to the primary one.
  const areas = JSON.parse(execFileSync('powershell', ['-NoProfile', '-Command', 'Add-Type -AssemblyName System.Windows.Forms; ' +
    'ConvertTo-Json -Compress @([System.Windows.Forms.Screen]::AllScreens | % {$w=$_.WorkingArea; @{p=$_.Primary;x=$w.X;y=$w.Y;w=$w.Width;h=$w.Height}})'], {encoding: 'utf8'}));
  const inside = (pos, a) => pos[0] >= a.x && pos[0] < a.x + a.w && pos[1] >= a.y && pos[1] < a.y + a.h;
  const relaunchAt = async home => {
    await quit();
    writeFileSync(settings, JSON.stringify({...JSON.parse(readFileSync(settings, 'utf8')), home}));
    ({overlay, chat} = await launch());
    return overlay('[screenX+innerWidth/2, screenY]');
  };
  const other = areas.find(a => !a.p);
  if (other) {
    const pos = await relaunchAt({cx: other.x + other.w / 2, y: other.y + other.h / 2});
    check('overlay restored on the secondary display', inside(pos, other), JSON.stringify({pos, other}));
  } else check('secondary display check [skipped: one display]', true);
  const gone = await relaunchAt({cx: 100000, y: 100000});
  check('spot on a missing display falls back to the primary', inside(gone, areas.find(a => a.p)), JSON.stringify(gone));
} catch (error) {
  check('e2e run', false, error.stack);
} finally {
  if (app && app.exitCode === null) await quit();
  // The synthetic DB and its graph file (memory.graph.db beside it) live in tmp.
  rmSync(tmp, {recursive: true, force: true, maxRetries: 5, retryDelay: 500});
  const failed = results.filter(r => !r.ok).length;
  console.log(`${results.length - failed}/${results.length} desktop E2E checks passed`);
  process.exitCode = failed ? 1 : 0;
}
