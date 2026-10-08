const {app, BrowserWindow, Menu, Tray, dialog, globalShortcut, ipcMain, nativeImage, screen, session} = require('electron');
const {spawn} = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');
const {installPet} = require('./pets.cjs');
// Installed: backend sources and the embeddable Python ship as resources (see package.json "build").
const root = app.isPackaged ? path.join(process.resourcesPath, 'phro') : path.resolve(__dirname, '..');
const port = Number(process.env.PHRO_DESKTOP_PORT || 8771);
const origin = `http://127.0.0.1:${port}`;
let backend, quitting=false;
const shutdownToken=require('node:crypto').randomBytes(24).toString('hex');
app.whenReady().then(async () => {
  if (!Number.isInteger(port) || port < 1024 || port > 65535) throw Error('Invalid desktop port');
  // Own the backend lifecycle; never attach silently to an unrelated process/database.
  // The backend starts what it needs and stops what it started (server.main): the WSL session that keeps
  // FalkorDB up, FalkorDB itself, Ollama; Claude CLI calls are its children. POST /shutdown with this run's token
  // is the quit signal (not a stdin pipe: on Windows a thread blocked reading stdin deadlocks subprocess spawning).
  backend = spawn(python(), [path.join(root,'desktop','serve.py')], {cwd:root, windowsHide:true,
    env:{...process.env, PYTHONIOENCODING:'utf-8', PHRO_PETS_DIR:petsDir(), PHRO_SHUTDOWN_TOKEN:shutdownToken},
    stdio:['ignore','pipe','pipe']});
  let listening=false;
  backend.stdout.on('data', d => {process.stdout.write(d);if(d.toString().includes(`phro-secretary ${origin}`))listening=true;});
  backend.stderr.on('data', d => process.stderr.write(d));
  let failed;
  backend.on('error', e => {failed=e;});
  backend.on('exit', code => {failed=Error(`Backend exited (${code})`);});
  let ready=false;
  for (let i=0;i<100;i++) {
    if (failed) throw failed;
    await new Promise(r=>setTimeout(r,200));
    if(listening)try { const r=await fetch(origin+'/health',{signal:AbortSignal.timeout(1500)}); if(r.ok) {ready=true;break;} } catch {}
  }
  if(!ready || failed) throw failed || Error('Backend startup timed out');
  const webPreferences={nodeIntegration:false,contextIsolation:true,sandbox:true};
  const lock=(win,page)=>{
    win.webContents.setWindowOpenHandler(()=>({action:'deny'}));
    win.webContents.on('will-navigate',(event,url)=>{if(url!==origin+page)event.preventDefault();});
  };
  session.defaultSession.setPermissionRequestHandler((_w,_p,callback)=>callback(false));
  const settings=loadSettings();
  // The chat window hides on close; quitting is in the overlay/tray menu (or Alt+F4 on the overlay).
  const chat = new BrowserWindow({width:850,height:760,title:'phro-secretary · Character Sheets',show:false,webPreferences});
  lock(chat,'/');
  chat.on('close',e=>{if(!quitting){e.preventDefault();chat.hide();}});
  const overlay = new BrowserWindow({width:200,height:262,show:false,frame:false,transparent:true,
    resizable:false,maximizable:false,alwaysOnTop:true,skipTaskbar:true,hasShadow:false,title:'phro-secretary',
    webPreferences:{...webPreferences,preload:path.join(__dirname,'preload.cjs'),backgroundThrottling:false}});
  lock(overlay,'/overlay.html');
  overlay.on('closed',()=>app.quit());
  const send=(channel,...args)=>{if(!overlay.isDestroyed())overlay.webContents.send(channel,...args);};
  const fromOverlay=e=>e.sender===overlay.webContents;
  // The window is placed from `home` (top centre of the character), so it returns there when the card closes.
  function place(width,height){
    const a=screen.getDisplayNearestPoint({x:Math.round(settings.home.cx),y:Math.round(settings.home.y)}).workArea;
    const x=Math.max(a.x,Math.min(Math.round(settings.home.cx-width/2),a.x+a.width-width));
    const y=Math.max(a.y,Math.min(Math.round(settings.home.y),a.y+a.height-height));
    overlay.setBounds({x,y,width,height});
  }
  place(200,262);
  function apply(){
    overlay.setAlwaysOnTop(true,settings.aboveFullscreen?'screen-saver':'floating');
    send('overlay-settings',{scale:settings.scale,lookAtCursor:settings.lookAtCursor,onboarded:settings.onboarded});
  }
  function update(patch){Object.assign(settings,patch);saveSettings(settings);apply();}
  const toggleChat=()=>{if(chat.isVisible()&&chat.isFocused())chat.hide();else{chat.show();chat.focus();}};
  const toggleOverlay=()=>{if(overlay.isVisible())overlay.hide();else overlay.showInactive();};
  const openCompose=()=>{overlay.show();overlay.focus();send('open-compose');};
  const shortcut='Control+Shift+Space';
  let currentPet=null, tray=null;
  function menu(){
    let pets=builtInPets();
    try{pets=pets.concat(JSON.parse(fs.readFileSync(path.join(petsDir(),'catalog.json'),'utf8')).filter(p=>!pets.some(b=>b.id===p.id)));}
    catch(err){if(err.code!=='ENOENT')console.error('imported pets:',err.message);}
    return Menu.buildFromTemplate([
      {label:overlay.isVisible()?'숨기기':'보이기',click:toggleOverlay},
      {label:'글쓰기',accelerator:shortcut,click:openCompose},
      {label:'대화 창 열기/닫기',click:toggleChat},
      {label:'처리 기록 보기',click:openTrace},
      {type:'separator'},
      {label:'캐릭터',submenu:[...pets.map(p=>({label:p.name,type:'radio',checked:p.id===currentPet,click:()=>send('select-pet',p.id)})),
        {type:'separator'},{label:'캐릭터 가져오기…',click:importPet}]},
      {label:'크기',submenu:[1,1.5,2].map(s=>({label:`${s}x`,type:'radio',checked:settings.scale===s,click:()=>update({scale:s})}))},
      {label:'커서 바라보기',type:'checkbox',checked:settings.lookAtCursor,click:i=>update({lookAtCursor:i.checked})},
      {label:'전체화면 앱 위에도 표시',type:'checkbox',checked:settings.aboveFullscreen,click:i=>update({aboveFullscreen:i.checked})},
      {type:'separator'},
      {label:'사용법',click:()=>{overlay.showInactive();send('show-help');}},
      {label:'종료',click:()=>app.quit()},
    ]);
  }
  // Per-input processing record (stages, models, tokens): desktop/trace.html over GET /trace.
  let traceWindow=null;
  function openTrace(){
    if(traceWindow&&!traceWindow.isDestroyed()){traceWindow.show();traceWindow.focus();return;}
    traceWindow=new BrowserWindow({width:1200,height:820,title:'phro-secretary · 처리 기록',webPreferences});
    lock(traceWindow,'/trace.html');
    traceWindow.loadURL(origin+'/trace.html');
  }
  async function importPet(){
    const pick=await dialog.showOpenDialog(overlay,{title:'캐릭터 시트 폴더 선택 (pet.json + spritesheet.webp)',properties:['openDirectory']});
    if(pick.canceled||!pick.filePaths.length)return;
    try{
      const entry=installPet(pick.filePaths[0],petsDir(),builtInPets().map(p=>p.id));
      send('select-pet',entry.id);
    }catch(err){
      // The previous character stays selected; nothing was swapped in.
      dialog.showMessageBox(overlay,{type:'warning',title:'phro-secretary',message:'캐릭터를 가져오지 못했습니다',detail:err.message});
    }
  }
  let grab=null;
  ipcMain.on('overlay-drag',(e,phase)=>{
    if(!fromOverlay(e))return;
    const c=screen.getCursorScreenPoint(), [x,y]=overlay.getPosition();
    if(phase==='start')grab={dx:c.x-x,dy:c.y-y};
    else if(phase==='move'&&grab){
      // Report the cursor-derived target, not 'move' events, which can arrive with intermediate positions.
      overlay.setPosition(c.x-grab.dx,c.y-grab.dy);
      send('overlay-move',c.x,c.y);
    }
    else if(phase==='end'&&grab){
      grab=null;
      const b=overlay.getBounds();
      update({home:{cx:b.x+b.width/2,y:b.y}});
    }
  });
  ipcMain.on('overlay-nudge',(e,dx,dy)=>{
    if(!fromOverlay(e)||grab||!Number.isFinite(dx)||!Number.isFinite(dy))return;
    const b=overlay.getBounds();
    settings.home={cx:b.x+b.width/2+dx,y:b.y+dy};
    place(b.width,b.height);
    const n=overlay.getBounds();
    update({home:{cx:n.x+n.width/2,y:n.y}});
  });
  ipcMain.on('overlay-resize',(e,width,height)=>{
    if(!fromOverlay(e)||grab)return;
    place(Math.min(600,Math.max(100,Math.round(width))),Math.min(800,Math.max(100,Math.round(height))));
  });
  // Transparent pixels pass clicks through; `forward` keeps mousemove coming so the page can switch back.
  ipcMain.on('overlay-ignore',(e,ignore)=>{if(fromOverlay(e))overlay.setIgnoreMouseEvents(!!ignore,{forward:true});});
  ipcMain.on('overlay-menu',e=>{if(fromOverlay(e))menu().popup({window:overlay});});
  ipcMain.on('toggle-chat',e=>{if(fromOverlay(e))toggleChat();});
  ipcMain.on('overlay-onboarded',e=>{if(fromOverlay(e))update({onboarded:true});});
  ipcMain.on('pet-icon',(e,id,dataUrl)=>{
    if(!fromOverlay(e)||typeof dataUrl!=='string'||!dataUrl.startsWith('data:image/png;base64,'))return;
    currentPet=String(id);
    const icon=nativeImage.createFromDataURL(dataUrl);
    if(icon.isEmpty())return;
    if(!tray){
      tray=new Tray(icon);
      tray.setToolTip('phro-secretary');
      tray.on('click',toggleOverlay);
      tray.on('right-click',()=>tray.popUpContextMenu(menu()));
    } else tray.setImage(icon);
  });
  // Cursor offset from the character for "look at cursor"; only polled while that option is on.
  let lastCursor='';
  setInterval(()=>{
    if(!settings.lookAtCursor||overlay.isDestroyed()||!overlay.isVisible())return;
    const c=screen.getCursorScreenPoint(), b=overlay.getBounds();
    const key=`${c.x},${c.y},${b.x},${b.y}`;
    if(key!==lastCursor){lastCursor=key;send('overlay-cursor',c.x-(b.x+b.width/2),c.y-(b.y+104*settings.scale));}
  },150);
  if(!globalShortcut.register(shortcut,openCompose))console.error(`Shortcut ${shortcut} is taken by another app`);
  overlay.webContents.on('did-finish-load',apply);
  // Shown before loading: on Windows, showing a hidden window later can undo bounds set while hidden.
  overlay.showInactive();
  await Promise.all([chat.loadURL(origin+'/'),overlay.loadURL(origin+'/overlay.html')]);
  if (app.isPackaged) firstRun(()=>send('select-pet',builtInPets()[0]?.id)).catch(err=>console.error('setup check:',err.message));
  if (process.env.PHRO_DESKTOP_SHOT) {
    setTimeout(async()=>{
      fs.writeFileSync(process.env.PHRO_DESKTOP_SHOT,(await overlay.webContents.capturePage()).toPNG());
      console.log('DESKTOP_CAPTURED');
    },2000);
  }
}).catch(e=>{dialog.showErrorBox('phro-secretary',e.message);app.quit();});
function python(){
  return process.env.PHRO_PYTHON||(app.isPackaged?path.join(process.resourcesPath,'python','python.exe'):path.join(root,'.venv','Scripts','python.exe'));
}
// Installed copies ship without the community sheets (redistribution not cleared) and without the external
// services; say what is missing instead of leaving a blank overlay or silent errors.
async function firstRun(reload){
  const missing=builtInPets().filter(p=>![path.join(root,'desktop','pets',p.id),path.join(petsDir(),p.id)]
    .some(dir=>fs.existsSync(path.join(dir,'spritesheet.webp'))));
  if(missing.length){
    const {response}=await dialog.showMessageBox({type:'question',title:'phro-secretary',buttons:['내려받기','나중에'],defaultId:0,
      message:'기본 캐릭터 시트를 내려받을까요?',
      detail:`codex-pets.net 커뮤니티 시트 ${missing.length}개(${missing.map(p=>p.name).join(', ')})를 출처에서 직접 받아 해시를 확인합니다. 나중에 메뉴의 "캐릭터 가져오기"로 다른 시트를 쓸 수도 있습니다.`});
    if(response===0){
      const code=await new Promise(done=>spawn(python(),[path.join(root,'desktop','download_pets.py'),petsDir()],{windowsHide:true,stdio:'inherit'})
        .on('exit',done).on('error',()=>done(-1)));
      if(code===0)reload();
      else dialog.showMessageBox({type:'warning',title:'phro-secretary',message:'캐릭터 시트를 받지 못했습니다',detail:'네트워크를 확인한 뒤 앱을 다시 시작하세요.'});
    }
  }
  await new Promise(r=>setTimeout(r,15000));
  const health=await (await fetch(origin+'/health')).json();
  const problems=[];
  if(!health.claude)problems.push('Claude Code CLI(claude)를 PATH에서 찾지 못했습니다. 설치·로그인 후 다시 시작하세요. 답변과 기억 저장이 동작하지 않습니다.');
  if(health.memory?.error)problems.push(`지식그래프 연결 오류(${health.memory.error}): phro-secretary가 자동으로 켜는 WSL Ubuntu-24.04의 FalkorDB(phro-falkor 서비스, 6379)와 Ollama(11434, nomic-embed-text 모델)가 설치돼 있어야 합니다.`);
  if(problems.length)dialog.showMessageBox({type:'warning',title:'phro-secretary',message:'준비되지 않은 구성 요소가 있습니다',detail:problems.join('\n\n')});
}
function petsDir(){return path.join(app.getPath('userData'),'pets');}
function builtInPets(){
  try{return JSON.parse(fs.readFileSync(path.join(root,'desktop','pets','catalog.json'),'utf8'));}
  catch(err){console.error(err.message);return [];}
}
function settingsFile(){return path.join(app.getPath('userData'),'overlay.json');}
function loadSettings(){
  let s={};
  try{s=JSON.parse(fs.readFileSync(settingsFile(),'utf8'));}catch{}
  // Ignore a saved spot that is no longer on any display (monitor unplugged, layout changed).
  const h=s.home, onScreen=h&&Number.isFinite(h.cx)&&Number.isFinite(h.y)&&
    screen.getAllDisplays().some(({workArea:a})=>h.cx>=a.x&&h.y>=a.y&&h.cx<a.x+a.width&&h.y+100<=a.y+a.height);
  const a=screen.getPrimaryDisplay().workArea;
  return {home:onScreen?h:{cx:a.x+a.width-120,y:a.y+a.height-280},scale:[1,1.5,2].includes(s.scale)?s.scale:1,
    lookAtCursor:!!s.lookAtCursor,aboveFullscreen:!!s.aboveFullscreen,onboarded:!!s.onboarded};
}
function saveSettings(s){try{fs.writeFileSync(settingsFile(),JSON.stringify(s));}catch(err){console.error(err.message);}}
app.on('will-quit',()=>globalShortcut.unregisterAll());
app.on('window-all-closed',()=>app.quit());
// Quit waits for the backend's own shutdown (worker, Ollama, FalkorDB) with the windows already gone; a backend
// that does not finish in time is killed, and its kill-on-close job still takes its children with it.
let stopping=false;
app.on('before-quit',e=>{
  quitting=true;
  if(!backend||backend.exitCode!==null||backend.signalCode)return;
  e.preventDefault();
  if(stopping)return;
  stopping=true;
  for(const win of BrowserWindow.getAllWindows())win.hide();
  const force=setTimeout(()=>backend.kill(),30000);
  backend.once('exit',()=>{clearTimeout(force);app.quit();});
  fetch(origin+'/shutdown',{method:'POST',headers:{'X-Phro-Shutdown':shutdownToken},signal:AbortSignal.timeout(5000)})
    .catch(()=>backend.kill());
});
