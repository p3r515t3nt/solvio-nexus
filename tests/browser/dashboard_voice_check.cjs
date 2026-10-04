/* Actual browser AudioContext/Worklet/getUserMedia using Chromium's synthetic
 * device only. HTTP/WS speech peers are local protocol fixtures, never Core
 * providers. Authentication/page/assets use the normal temporary HTTPS fixture.
 * No real microphone permissions, provider turns, production, or persistent PCM.
 */
const {chromium}=require('playwright');
const fs=require('fs'),path=require('path'),assert=require('node:assert/strict');
const out=path.resolve(process.argv[2]),url=fs.readFileSync(path.join(out,'dashboard-url.txt'),'utf8').trim(),origin=new URL(url).origin;
assert.equal(new URL(url).hostname,'127.0.0.1');
let browser;const results=[];
(async()=>{
 browser=await chromium.launch({headless:true,executablePath:process.env.BROWSER_EXECUTABLE, args:['--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream','--autoplay-policy=no-user-gesture-required']});
 const context=await browser.newContext({ignoreHTTPSErrors:true,viewport:{width:1280,height:1100}});
 await context.route('**/*',r=>new URL(r.request().url()).origin===origin?r.continue():r.abort());
 const login=await context.newPage();await login.goto(url);await login.locator('#enrollment').fill('n5-test-only-'.padEnd(43,'0'));await login.locator('#login-form button').click();await login.locator('#workspace').waitFor({state:'visible'});await login.close();
 async function fixture({mic='allow',postStatus=201,autoReady=true,voiceMode}={}){
  const p=await context.newPage();p.setDefaultTimeout(10000);const faults=[],requests=[],frames=[];let socket;let readySent=false;
  p.on('pageerror',e=>faults.push(e.message));
  await p.addInitScript(({mic})=>{
   const native=navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);window.audioProbe={calls:0,streams:[],contexts:[],nodes:[],constraints:[],stopCount:0};
   navigator.mediaDevices.getUserMedia=async constraints=>{
    const q=window.audioProbe;q.calls++;q.constraints.push(constraints);
    if(mic==='deny')throw new DOMException('synthetic denied','NotAllowedError');
    const stream=await native(constraints);q.streams.push(stream);
    for(const track of stream.getTracks()){const stop=track.stop.bind(track);track.stop=()=>{q.stopCount++;stop();};}
    if(mic==='late')await new Promise(resolve=>q.resolve=resolve);
    return stream;
   };
   const AC=window.AudioContext;window.AudioContext=class extends AC{constructor(...args){super(...args);window.audioProbe.contexts.push(this);}};
   const AWN=window.AudioWorkletNode;window.AudioWorkletNode=class extends AWN{constructor(...args){super(...args);window.audioProbe.nodes.push(this);}};
  },{mic});
  await p.route('**/v1/browser/voice/session',async r=>{
   requests.push({body:r.request().postDataJSON(),headers:r.request().headers()});
   assert.equal(requests.at(-1).headers['x-csrf-token']?.length>10,true);assert.deepEqual(requests.at(-1).body,{});
   await r.fulfill({status:postStatus,json:postStatus===201?{nonce:'fixture-once-memory-only',expires_at:Date.now()/1000+30,websocket_path:'/v1/browser/voice',protocol_version:1,audio:{encoding:'pcm_s16le',sample_rate:16000,channels:1}}:{error:'voice_busy'}});
  });
  await p.routeWebSocket('**/v1/browser/voice',ws=>{
   socket=ws;ws.onMessage(m=>{
    if(Buffer.isBuffer(m)){frames.push({bytes:m.length,beforeReady:!readySent,nonzero:m.some(b=>b!==0)});return;}
    const frame=JSON.parse(m);frames.push(frame);
    if(frame.type==='session_auth'){assert.deepEqual(frame,{type:'session_auth',nonce:'fixture-once-memory-only'});ws.send(JSON.stringify({type:'session_authenticated',session_id:'fixture-session',connection_id:'browser:fixture',protocol_version:1}));}
    if(frame.type==='session_start'&&autoReady)ready();
   });
  });
  function send(m){socket.send(JSON.stringify(m));}
  function ready(overrides={}){readySent=true;send({type:'session_ready',session_id:'fixture-session',connection_id:'browser:fixture',generation:2,...(voiceMode===undefined?{}:{voice_mode:voiceMode}),...overrides});}
  function closed(overrides={}){send({type:'session_closed',session_id:'fixture-session',connection_id:'browser:fixture',generation:2,provider:'closed_confirmed',previous_unconfirmed:0,...overrides});}
  await p.goto(url);await p.locator('#workspace').waitFor({state:'visible'});await p.waitForFunction(()=>!document.querySelector('#voice-start').disabled);
  const allStopped=()=>p.evaluate(()=>window.audioProbe.streams.every(s=>s.getTracks().every(t=>t.readyState==='ended'))&&window.audioProbe.contexts.every(c=>c.state==='closed'));
  async function waitStopped(){await p.waitForFunction(()=>window.audioProbe.streams.every(s=>s.getTracks().every(t=>t.readyState==='ended'))&&window.audioProbe.contexts.every(c=>c.state==='closed'));}
  return {p,requests,frames,faults,ready,send,closed,get socket(){return socket},allStopped,waitStopped,async start(){await p.locator('#voice-start').click();},async listening(){await p.waitForFunction(()=>document.querySelector('#browser-voice').dataset.listening==='true');},async done(){await p.close();assert.deepEqual(faults,[]);}};
 }
 async function test(name,fn){await fn();results.push(name);console.log('PASS '+name);}
 await test('Opening dashboard never requests microphone or voice ticket',async()=>{const f=await fixture();assert.equal(await f.p.evaluate(()=>audioProbe.calls),0);assert.equal(f.requests.length,0);await f.done();});
 await test('Local off and busy notices stay scoped to one tab while another tab captures',async()=>{
  // Same authenticated browser context. Only the second tab's busy response is
  // synthetic; the first tab keeps a real browser stream and Worklet running.
  // The backend suite separately proves the shared Core conversation owner.
  const first=await fixture();await first.start();await first.listening();
  const second=await fixture({postStatus:409});
  assert.equal(await second.p.locator('#voice-local').textContent(),'Mikrofon in diesem Tab aus.');
  assert.equal(await second.p.evaluate(()=>audioProbe.calls),0);
  const count=first.frames.filter(v=>v.bytes).length;
  await second.p.waitForTimeout(120);
  assert(first.frames.filter(v=>v.bytes).length>count,'opening another tab must not stop the first capture');
  // The real second-tab microphone briefly opens, then the existing busy path
  // stops only that stream. Its status must still not describe the whole device.
  await second.start();await second.p.getByText('SOLVIO führt bereits ein Gespräch. Beende das andere Gespräch zuerst.',{exact:true}).waitFor();
  await second.waitStopped();
  assert.equal(await second.p.locator('#voice-local').textContent(),'Mikrofon in diesem Tab aus. Wiedergabe beendet.');
  assert.equal(second.requests.length,1);assert.equal(second.frames.length,0);
  assert.equal(await first.p.evaluate(()=>audioProbe.streams.some(s=>s.getAudioTracks().some(t=>t.readyState==='live'&&t.enabled))&&audioProbe.contexts.some(c=>c.state==='running')),true);
  const continuing=first.frames.filter(v=>v.bytes).length;await second.p.waitForTimeout(120);
  assert(first.frames.filter(v=>v.bytes).length>continuing,'stopping the rejected tab must not stop its peer');
  await first.p.locator('#voice-end').click();first.closed();await first.waitStopped();
  await first.done();await second.done();
 });
 await test('Denied microphone creates no voice session',async()=>{const f=await fixture({mic:'deny'});await f.start();await f.p.getByText('Kein Mikrofonzugriff.',{exact:false}).waitFor();assert.equal(f.requests.length,0);assert.equal(await f.allStopped(),true);await f.done();});
 await test('Late microphone permission after End stops the stream without ticket',async()=>{const f=await fixture({mic:'late'});await f.start();await f.p.waitForFunction(()=>!!audioProbe.resolve);await f.p.locator('#voice-end').click();await f.p.evaluate(()=>audioProbe.resolve());await f.waitStopped();assert.equal(f.requests.length,0);await f.done();});
 await test('Core busy closes the actual microphone and AudioContext',async()=>{const f=await fixture({postStatus:409});await f.start();await f.p.getByText('SOLVIO führt bereits ein Gespräch.',{exact:false}).waitFor();await f.waitStopped();assert.equal(f.requests.length,1);assert.equal(f.frames.length,0);await f.done();});
 await test('Authentication and Ready precede bounded actual PCM; heard measures playback',async()=>{
  const f=await fixture({autoReady:false});await f.start();await f.p.waitForTimeout(500);assert.equal(f.frames.filter(v=>v.type==='session_start').length,1);assert.equal(f.frames.some(v=>v.bytes),false);
  f.ready();await f.listening();await f.p.waitForTimeout(250);const pcm=f.frames.filter(v=>v.bytes);assert(pcm.length>0);assert(pcm.every(v=>v.bytes===640&&!v.beforeReady));
  const voice=Buffer.alloc(16000*2*2);for(let i=0;i<voice.length;i+=2)voice.writeInt16LE(1000,i);
  f.socket.send(voice);await f.p.waitForTimeout(180);await f.p.locator('#voice-interrupt').click();await f.p.waitForTimeout(80);
  const barge=f.frames.find(v=>v.type==='barge_in');assert(barge&&barge.played_ms>0&&barge.played_ms<1000);
  f.send({type:'flush'});await f.p.waitForTimeout(50);assert.equal(f.frames.some(v=>v.type==='heard'),false,'server echo must not overwrite heard amount with zero');
  f.socket.send(voice);await f.p.waitForTimeout(150);f.send({type:'flush'});await f.p.waitForTimeout(80);
  const heard=f.frames.find(v=>v.type==='heard');assert(heard&&heard.played_ms>0&&heard.played_ms<1000);
  await f.p.locator('#voice-end').click();await f.waitStopped();assert.match(await f.p.locator('#voice-provider').textContent(),/steht aus/);assert(f.frames.some(v=>v.type==='session_end'));
  f.closed();await f.p.getByText('Gesprächsende vom Core bestätigt.',{exact:true}).waitFor();assert.equal(f.requests.length,1);await f.done();
 });
 async function localSpeechWhilePlaying(f){
  // Deterministic inputs at the actual Worklet/controller seam. The native
  // browser capture, playback, flush and outgoing socket remain real.
  await f.p.evaluate(()=>{
   const deliver=data=>audioProbe.nodes.at(-1).port.onmessage({data});
   deliver({type:'level',rms:0,playing:true,played_ms:100});
   for(let i=0;i<5;i++)deliver({type:'level',rms:.1,playing:true,played_ms:120+i*20});
  });
 }
 await test('Core-negotiated full duplex preserves overlap while manual interrupt mute and End still work',async()=>{
  const f=await fixture({voiceMode:'full_duplex'});await f.start();await f.listening();
  const voice=Buffer.alloc(64000);for(let i=0;i<voice.length;i+=2)voice.writeInt16LE(1000,i);
  f.socket.send(voice);await f.p.waitForTimeout(120);
  const before=f.frames.filter(v=>v.bytes).length;await localSpeechWhilePlaying(f);await f.p.waitForTimeout(120);
  assert.equal(f.frames.some(v=>v.type==='barge_in'),false,'overlapping speech must not flush a negotiated continuous stream');
  assert(f.frames.filter(v=>v.bytes).length>before,'capture continues while output plays');
  await f.p.locator('#voice-interrupt').click();await f.p.waitForTimeout(80);
  const manual=f.frames.filter(v=>v.type==='barge_in');assert.equal(manual.length,1);assert(manual[0].played_ms>0);
  f.send({type:'flush'});await f.p.locator('#voice-mute').click();await f.p.waitForTimeout(80);
  const muted=f.frames.filter(v=>v.bytes).length;f.socket.send(voice);await localSpeechWhilePlaying(f);await f.p.waitForTimeout(100);
  assert.equal(f.frames.filter(v=>v.bytes).length,muted);assert.match(await f.p.locator('#voice-provider').textContent(),/kostenpflichtig/);
  await f.p.locator('#voice-mute').click();await f.listening();await f.p.waitForTimeout(80);
  assert(f.frames.filter(v=>v.bytes).length>muted);assert.equal(await f.p.evaluate(()=>audioProbe.calls),1);
  await f.p.locator('#voice-end').click();await f.waitStopped();f.closed();
  await f.p.getByText('Gesprächsende vom Core bestätigt.',{exact:true}).waitFor();await f.done();
 });
 await test('Missing unknown and next-session mode keep the established automatic interruption',async()=>{
  for(const voiceMode of [undefined,'unknown']){
   const f=await fixture({voiceMode});await f.start();await f.listening();f.socket.send(Buffer.alloc(64000));await f.p.waitForTimeout(80);
   await localSpeechWhilePlaying(f);await f.p.waitForTimeout(80);assert.equal(f.frames.filter(v=>v.type==='barge_in').length,1);
   await f.p.locator('#voice-end').click();f.closed();await f.waitStopped();await f.done();
  }
  const f=await fixture({autoReady:false});await f.start();await f.p.waitForTimeout(150);f.ready({voice_mode:'full_duplex'});await f.listening();
  await f.p.locator('#voice-end').click();f.closed();await f.p.getByText('Gesprächsende vom Core bestätigt.',{exact:true}).waitFor();
  await f.start();await f.p.waitForTimeout(150);f.ready();await f.listening();f.socket.send(Buffer.alloc(64000));await f.p.waitForTimeout(80);
  await localSpeechWhilePlaying(f);await f.p.waitForTimeout(80);assert.equal(f.frames.filter(v=>v.type==='barge_in').length,1,'a new session cannot inherit duplex from the old socket');
  await f.p.locator('#voice-end').click();f.closed();await f.waitStopped();assert.equal(f.requests.length,2);await f.done();
 });
 await test('Socket loss stops local audio but does not claim provider end or restart',async()=>{const f=await fixture();await f.start();await f.listening();f.socket.close();await f.waitStopped();await f.p.getByText('Das Ende der Sprachverbindung ist nicht bestätigt.',{exact:false}).waitFor();await f.p.waitForTimeout(100);assert.equal(f.requests.length,1);assert.equal(await f.p.evaluate(()=>audioProbe.calls),1);await f.done();});
 await test('Foreign completion does not stop or confirm this session',async()=>{const f=await fixture();await f.start();await f.listening();f.closed({session_id:'old-session'});await f.p.waitForTimeout(50);assert.equal(await f.allStopped(),false);assert.doesNotMatch(await f.p.locator('#voice-provider').textContent(),/bestätigt/);f.socket.close();await f.waitStopped();await f.done();});
 await test('Older provider generation cannot confirm closure',async()=>{const f=await fixture();await f.start();await f.listening();await f.p.locator('#voice-end').click();f.closed({generation:1});await f.waitStopped();await f.p.getByText('nicht vollständig bestätigt.',{exact:false}).waitFor();await f.done();});
 await test('Later same-session generation is valid only with explicit zero previous unknown',async()=>{const f=await fixture();await f.start();await f.listening();await f.p.locator('#voice-end').click();f.closed({generation:3});await f.p.getByText('Gesprächsende vom Core bestätigt.',{exact:true}).waitFor();await f.done();});
 await test('Prior unknown and missing-count closure stay unconfirmed',async()=>{for(const value of [1,null,false]){const f=await fixture();await f.start();await f.listening();await f.p.locator('#voice-end').click();f.closed({previous_unconfirmed:value});await f.p.getByText('nicht vollständig bestätigt.',{exact:false}).waitFor();await f.waitStopped();await f.done();}});
 await test('View changes retain the same live controls and PCM; global End closes without another microphone start',async()=>{
  const f=await fixture();await f.p.evaluate(()=>window.originalVoiceNode=document.querySelector('#browser-voice'));
  await f.start();await f.listening();const before=f.frames.filter(v=>v.bytes).length;
  await f.p.getByRole('button',{name:'Wissen',exact:true}).click();
  await f.p.locator('#voice-dock #voice-end').waitFor({state:'visible'});
  await f.p.waitForTimeout(120);assert(f.frames.filter(v=>v.bytes).length>before);
  assert.equal(f.frames.some(v=>v.type==='session_end'),false);assert.equal(await f.allStopped(),false);
  assert.equal(await f.p.evaluate(()=>originalVoiceNode===document.querySelector('#voice-dock #browser-voice')),true);
  await f.p.locator('#voice-mute').click();await f.p.waitForTimeout(80);const muted=f.frames.filter(v=>v.bytes).length;
  await f.p.getByRole('button',{name:'Aufträge',exact:true}).click();await f.p.waitForTimeout(120);
  assert.equal(f.frames.filter(v=>v.bytes).length,muted);assert.equal(await f.p.locator('#task-list .task-card').count(),2);
  await f.p.locator('#voice-mute').click();await f.listening();await f.p.waitForTimeout(120);
  assert(f.frames.filter(v=>v.bytes).length>muted);assert.equal(await f.p.evaluate(()=>audioProbe.calls),1);assert.equal(f.requests.length,1);
  await f.p.locator('#voice-dock #voice-end').click();await f.waitStopped();
  assert.match(await f.p.locator('#voice-provider').textContent(),/steht aus/);assert.equal(f.frames.filter(v=>v.type==='session_end').length,1);
  f.closed();await f.p.waitForFunction(()=>document.querySelector('#voice-provider').textContent==='Gesprächsende vom Core bestätigt.');
  await f.p.getByRole('button',{name:'SOLVIO',exact:true}).click();
  assert.equal(await f.p.evaluate(()=>originalVoiceNode===document.querySelector('#voice-home #browser-voice')),true);
  assert.equal(await f.p.evaluate(()=>audioProbe.calls),1);assert.equal(f.requests.length,1);await f.done();
 });
 await test('An open task decision keeps the same microphone globally stoppable inside the modal',async()=>{
  const f=await fixture();await f.start();await f.listening();
  await f.p.evaluate(()=>window.originalVoiceNode=document.querySelector('#browser-voice'));
  await f.p.getByRole('button',{name:'Aufträge',exact:true}).click();
  await f.p.locator('#task-list').getByText('Testauftrag: Eine übersichtliche Wochenplanung vorbereiten.',{exact:true}).click();
  await f.p.getByRole('button',{name:'Auftrag abbrechen',exact:true}).click();
  await f.p.locator('#decision-dialog').waitFor({state:'visible'});
  await f.p.locator('#voice-dialog #voice-end').waitFor({state:'visible'});
  assert.equal(await f.p.evaluate(()=>originalVoiceNode===document.querySelector('#voice-dialog #browser-voice')),true);
  const before=f.frames.filter(v=>v.bytes).length;await f.p.waitForTimeout(120);
  assert(f.frames.filter(v=>v.bytes).length>before);assert.equal(f.frames.some(v=>v.type==='session_end'),false);
  await f.p.locator('#voice-dialog #voice-end').click();await f.waitStopped();
  assert.equal(await f.p.locator('#decision-dialog').evaluate(n=>n.open),true);
  assert.match(await f.p.locator('#voice-provider').textContent(),/steht aus/);
  f.closed();await f.p.waitForFunction(()=>document.querySelector('#voice-provider').textContent==='Gesprächsende vom Core bestätigt.');
  await f.p.getByRole('button',{name:'Schließen',exact:true}).click();
  assert.equal(await f.p.evaluate(()=>audioProbe.calls),1);assert.equal(f.requests.length,1);
  assert.equal(f.frames.filter(v=>v.type==='session_end').length,1);await f.done();
 });
 await test('Broken websocket protocol fails closed before capture',async()=>{const f=await fixture({autoReady:false});await f.start();await f.p.waitForTimeout(300);f.send({type:'session_ready',session_id:'wrong',connection_id:'browser:fixture',generation:2});await f.waitStopped();assert.equal(f.frames.some(v=>v.bytes),false);await f.done();});
 await test('Bounded playback rejects an oversized audio frame',async()=>{const f=await fixture();await f.start();await f.listening();f.socket.send(Buffer.alloc(65538));await f.waitStopped();await f.p.locator('#voice-error').filter({hasText:'ungültige Antwort'}).waitFor();await f.done();});
 await test('Logout stops capture before the HTTP logout reply arrives',async()=>{const f=await fixture();await f.start();await f.listening();let held;await f.p.route('**/v1/browser/session/logout',r=>held=r);await f.p.locator('#logout').click();await f.waitStopped();assert(held);assert(f.frames.some(v=>v.type==='session_end'));await held.fulfill({status:503,json:{error:'temporary_fixture_failure'}});await f.done();});
 await test('Server-initiated end stops local audio before provider confirmation',async()=>{
  const f=await fixture();await f.start();await f.listening();f.send({type:'session_end',reason:'timeout'});
  await f.waitStopped();assert.match(await f.p.locator('#voice-provider').textContent(),/steht aus/);
  assert(f.frames.some(v=>v.type==='session_end'));f.closed();
  await f.p.getByText('Gesprächsende vom Core bestätigt.',{exact:true}).waitFor();
  assert.equal(await f.p.evaluate(()=>audioProbe.calls),1);assert.equal(f.requests.length,1);await f.done();
 });
 await test('Navigation and pagehide during pending permission cannot retain or restart capture',async()=>{
  const f=await fixture();await f.start();await f.listening();
  await f.p.evaluate(()=>window.addEventListener('pagehide',()=>sessionStorage.setItem('voice-pagehide-proof',JSON.stringify({
   stopped:audioProbe.streams.every(s=>s.getTracks().every(t=>t.readyState==='ended')),
   contextsClosed:audioProbe.contexts.every(c=>c.state==='closed'),calls:audioProbe.calls})),{once:true}));
  await f.p.goto(url);const state=await f.p.evaluate(()=>JSON.parse(sessionStorage.getItem('voice-pagehide-proof')));
  assert.deepEqual(state,{stopped:true,contextsClosed:true,calls:1});assert.equal(f.requests.length,1);
  assert.equal(await f.p.evaluate(()=>audioProbe.calls),0);await f.done();
  const late=await fixture({mic:'late'});await late.start();await late.p.waitForFunction(()=>!!audioProbe.resolve);
  await late.p.evaluate(()=>{window.dispatchEvent(new PageTransitionEvent('pagehide',{persisted:false}));audioProbe.resolve();});
  await late.waitStopped();assert.equal(late.requests.length,0);assert.equal(await late.p.evaluate(()=>audioProbe.calls),1);await late.done();
 });
 await test('Mute stops microphone frames and explicit unmute reuses the same stream',async()=>{
  const f=await fixture();await f.start();await f.listening();await f.p.waitForTimeout(120);
  assert(f.frames.some(v=>v.bytes));await f.p.locator('#voice-mute').click();
  await f.p.waitForTimeout(80);const count=f.frames.filter(v=>v.bytes).length;
  await f.p.waitForTimeout(250);assert.equal(f.frames.filter(v=>v.bytes).length,count);
  assert.equal(await f.p.evaluate(()=>audioProbe.streams.every(s=>s.getAudioTracks().every(t=>!t.enabled&&t.readyState==='live'))),true);
  assert.equal(await f.p.locator('#browser-voice').getAttribute('data-listening'),'false');
  assert.match(await f.p.locator('#voice-local').textContent(),/stummgeschaltet/);
  assert.match(await f.p.locator('#voice-provider').textContent(),/offen.*kostenpflichtig/);
  assert.equal(f.frames.some(v=>v.type==='session_end'),false);
  await f.p.getByRole('button',{name:'Mikrofon einschalten',exact:true}).click();await f.listening();
  await f.p.waitForTimeout(160);assert(f.frames.filter(v=>v.bytes).length>count);
  assert.equal(await f.p.evaluate(()=>audioProbe.streams.every(s=>s.getAudioTracks().every(t=>t.enabled&&t.readyState==='live'))),true);
  assert.equal(await f.p.evaluate(()=>audioProbe.calls),1);assert.equal(f.requests.length,1);
  await f.p.locator('#voice-end').click();f.closed();await f.waitStopped();await f.done();
 });
 await test('Muted playback and server controls never automatically unmute capture',async()=>{
  const f=await fixture();await f.start();await f.listening();
  await f.p.evaluate(()=>window.mutedTaskCard=document.querySelector('#task-list .task-card'));
  await f.p.locator('#voice-mute').click();await f.p.waitForTimeout(80);
  const count=f.frames.filter(v=>v.bytes).length,voice=Buffer.alloc(32000);for(let i=0;i<voice.length;i+=2)voice.writeInt16LE(1000,i);
  f.socket.send(voice);f.send({type:'ping'});await f.p.waitForTimeout(180);f.send({type:'flush'});await f.p.waitForTimeout(100);
  assert(f.frames.some(v=>v.type==='heard'&&v.played_ms>0));assert(f.frames.some(v=>v.type==='pong'));
  assert.equal(f.frames.filter(v=>v.bytes).length,count);assert.equal(await f.p.locator('#voice-mute').getAttribute('aria-pressed'),'true');
  assert.equal(await f.p.evaluate(()=>audioProbe.streams.every(s=>s.getAudioTracks().every(t=>!t.enabled&&t.readyState==='live'))),true);
  assert.equal(await f.p.evaluate(()=>audioProbe.contexts.every(c=>c.state==='running')),true);
  assert.equal(await f.p.evaluate(()=>window.mutedTaskCard===document.querySelector('#task-list .task-card')),true,'muted level callbacks must not repeatedly rebuild the task list');
  await f.p.locator('#voice-end').click();f.closed();await f.waitStopped();await f.done();
 });
 await test('Ending a muted conversation closes tracks and cannot revive through unmute',async()=>{
  const f=await fixture();await f.start();await f.listening();await f.p.locator('#voice-mute').click();
  await f.p.locator('#voice-end').click();await f.waitStopped();assert.equal(await f.p.locator('#voice-mute').isDisabled(),true);
  await f.p.evaluate(()=>document.querySelector('#voice-mute').click());f.send({type:'flush'});f.send({type:'ping'});f.closed();
  await f.p.getByText('Gesprächsende vom Core bestätigt.',{exact:true}).waitFor();await f.p.waitForTimeout(80);
  assert.equal(await f.allStopped(),true);assert.equal(await f.p.evaluate(()=>audioProbe.calls),1);assert.equal(f.requests.length,1);
  assert.equal(await f.p.locator('#voice-mute').isDisabled(),true);await f.done();
 });
 fs.writeFileSync(path.join(out,'voice-browser-results.json'),JSON.stringify({scope:'isolated protocol fixture with real browser audio APIs and synthetic microphone only',passed:results.length,tests:results},null,2));
 console.log(`${results.length}/${results.length} browser voice cases passed`);
})().catch(e=>{console.error(e);process.exitCode=1;}).finally(async()=>{await browser?.close();});
