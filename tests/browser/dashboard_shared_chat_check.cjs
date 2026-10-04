/* Browser UX on real shipped assets; every URL is fulfilled locally. No Core,
 * provider, microphone permission or production session exists in this fixture. */
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require('playwright');
const assets=path.resolve(__dirname,'../../src/solvio/dashboard/assets');
const out=path.resolve(process.argv[2]);fs.mkdirSync(out,{recursive:true});
const CH='c-1111111111111111';
const chats=new Map([[CH,{conversation:{conversation_id:CH,title:'Unser Gespräch',message_count:2,last_activity_at:1},
 messages:[{message_id:'m-1111111111111111',role:'user',text:'Welche Schritte stehen heute an?'},{message_id:'m-2222222222222222',role:'assistant',text:'Wir können zuerst deine offenen Aufgaben ansehen. Sag mir einfach, womit du beginnen möchtest.'}],auftraege:[],deliveries_open:0}]]);
// A long pre-existing transcript exposes end anchoring while the text view is hidden.
for(let i=6;i<26;i++)chats.get(CH).messages.push({message_id:'m-'+String(i).padStart(16,'0'),role:i%2?'assistant':'user',text:`Vorheriger Gesprächsbeitrag ${i}. Dieser längere Verlauf bleibt vollständig erhalten.`});
chats.get(CH).conversation.message_count=chats.get(CH).messages.length;
const requests=[],faults=[],checks=[];let browser,historyGate=null;
(async()=>{
 const bundled=chromium.executablePath(),installed='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
 const executablePath=process.env.BROWSER_EXECUTABLE||(fs.existsSync(bundled)?bundled:installed);
 assert(fs.existsSync(executablePath),'Existing Chromium required; do not install a runtime');
 browser=await chromium.launch({headless:true,executablePath});
 const context=await browser.newContext({viewport:{width:1440,height:960}});
 await context.addInitScript(()=>{window.micCalls=0;navigator.mediaDevices.getUserMedia=async()=>{window.micCalls++;throw Error('Microphone forbidden in UX test');};localStorage.setItem('solvio.chat.selected.v1','c-1111111111111111');});
 await context.route('**/*',async route=>{
  const req=route.request(),url=new URL(req.url());assert.equal(url.origin,'https://dashboard.test');
  const pathname=url.pathname;requests.push({path:pathname,method:req.method()});
  if(pathname.startsWith('/dashboard/')){
   const name=pathname==='/dashboard/'?'index.html':pathname.replace('/dashboard/assets/','');
   assert(!name.includes('..'));const p=path.join(assets,name);
   if(!fs.existsSync(p))return route.fulfill({status:404,body:''});
   const body=name==='app.js'?fs.readFileSync(p,'utf8')+'\nwindow.presenceFixture={voice,presence,state,syncPresence,chat};':fs.readFileSync(p);
   return route.fulfill({body,contentType:name.endsWith('.css')?'text/css':name.endsWith('.js')?'text/javascript':name.endsWith('.svg')?'image/svg+xml':'text/html'});
  }
  let json;
  if(pathname==='/v1/browser/session')json={session_id:'fixture-only',csrf_token:'fixture-csrf-no-authority'};
  else if(pathname==='/v1/dashboard/state')json={environment:'isolated_test',runtime:'available',repositories:[],components:[],stand:1};
  else if(pathname==='/v1/agent/runs')json={laeufe:[]};
  else if(pathname==='/v1/agent/approvals')json={approvals:[]};
  else if(pathname==='/v1/control/audio')json={devices:[]};
  else if(pathname==='/v1/conversations'&&req.method()==='POST'){
   const id='c-3333333333333333';chats.set(id,{conversation:{conversation_id:id,title:'',message_count:0,last_activity_at:2},messages:[],auftraege:[],deliveries_open:0});json={conversation_id:id};
  }else if(pathname==='/v1/conversations')json={conversations:[...chats.values()].map(c=>c.conversation)};
  else if(chats.has(pathname.replace('/v1/conversations/',''))){if(historyGate)await historyGate;json=chats.get(pathname.replace('/v1/conversations/',''));}
  else if(pathname.startsWith('/v1/memory/'))json={memories:[],candidates:[],commands:[],total:0};
  else if(pathname.startsWith('/v1/control/inbox'))json={items:[]};
  else return route.fulfill({status:404,json:{error:'fixture_unconfigured'}});
  return route.fulfill({json});
 });
 const page=await context.newPage();page.on('pageerror',e=>faults.push(e.message));await page.goto('https://dashboard.test/dashboard/');
 await page.locator('#chat-messages .conversation-answer').first().waitFor();
 assert(await page.locator('#presence').isVisible(),'SOLVIO presence must remain visible in the signed-in chat');
 await page.waitForFunction(()=>Number(getComputedStyle(document.getElementById('mascot')).opacity)>.9);
 const picture=()=>page.locator('#orb').evaluate(n=>n.toDataURL());
 const firstFrame=await picture();await page.waitForTimeout(120);assert.notEqual(await picture(),firstFrame,'existing presence animates');
 await page.emulateMedia({reducedMotion:'reduce'});await page.waitForTimeout(100);
 const still=await picture();await page.waitForTimeout(120);assert.equal(await picture(),still,'Reduced Motion remains still');
 await page.emulateMedia({reducedMotion:'no-preference'});
 checks.push('visible_full_face_moves_and_reduced_motion_stays_still');
 for(const width of [1440,768,390,320]){
  await page.setViewportSize({width,height:width===1440?960:844});await page.waitForTimeout(100);
  const layout=await page.evaluate(()=>{const box=id=>{const b=document.getElementById(id).getBoundingClientRect();return {top:b.top,bottom:b.bottom,left:b.left,right:b.right};};return {width:innerWidth,height:innerHeight,scroll:document.documentElement.scrollWidth,input:box('message-form'),messages:box('chat-messages'),toolbar:box('chat-title')};});
  assert(layout.scroll<=width,JSON.stringify(layout));assert(layout.input.bottom<=layout.height+1,JSON.stringify(layout));assert(layout.messages.bottom<=layout.input.top+1,JSON.stringify(layout));
  assert(await page.locator('#voice-start').isVisible());assert(await page.locator('#new-chat').isVisible());
  assert(await page.locator('#voice-local').isVisible());assert.match(await page.locator('#voice-local').textContent(),/aus/);
  const face=await page.locator('#mascot').boundingBox();assert(face.width>=19&&face.height>=19,'recognizable full face, not hidden canvas only');
  const orb=await page.locator('#orb').boundingBox();assert(orb.height>=64&&orb.width>=84);
  assert.equal(await page.getByRole('button',{name:'Wissen',exact:true}).isVisible(),false);
  await page.locator('#chat-messages').evaluate(n=>n.scrollTop=0);
  assert(await page.locator('#chat-messages .conversation-answer').first().isVisible());
  await page.screenshot({path:path.join(out,`shared-chat-${width}.png`)});
  checks.push(`viewport_${width}_composer_and_navigation_visible_without_overflow`);
 }
 await page.locator('#document-options > summary').click();assert(await page.locator('#document-file').isVisible());
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);await page.locator('#document-options > summary').click();
 await page.locator('#workspace-menu > summary').click();await page.getByRole('button',{name:'Wissen',exact:true}).click();
 assert(await page.locator('#knowledge-view').isVisible());await page.locator('#workspace-menu > summary').click();await page.getByRole('button',{name:'Chat',exact:true}).click();
 await page.locator('#new-chat').click();await page.waitForFunction(()=>document.getElementById('chat').dataset.conversationId==='c-3333333333333333');
 assert.equal(await page.locator('#chat-messages article').count(),0);
 await page.locator('#chat-list-panel > summary').click();await page.locator(`[data-conversation-id="${CH}"]`).click();
 await page.locator('#chat-messages .conversation-answer').first().waitFor();assert.match(await page.locator('#chat-messages').textContent(),/Welche Schritte/);
 assert.equal(await page.locator('#chat-list-panel').evaluate(n=>n.open),false);
 // Drive only local protocol ports of the actual BrowserVoice; no microphone/audio/socket.
 await page.setViewportSize({width:390,height:844});
 await page.locator('#message-text').fill('Dieser Entwurf bleibt beim Sprachende erhalten.');
 await page.locator('#message-text').focus();
 await page.evaluate(()=>{
  const {voice:v,state}=window.presenceFixture;
  v.active=true;v.ready=false;v.startContext={conversation_id:'c-1111111111111111'};v.ownerSession=state.session;
  v.auth={session_id:'local-fixture',connection_id:'browser:fixture'};v.handoffRequired=true;v.flushed=null;v.closedProof=false;v.closeObserved=false;
  v.ws={readyState:1,send(){},close(){this.readyState=3;}};
  v.local.textContent='Mikrofonfreigabe wird angefragt …';v.buttons();
 });
 assert(await page.locator('#voice-focus').isVisible());assert.equal(await page.locator('#message-form').isVisible(),false);
 assert.equal(await page.locator('#chat-messages').isVisible(),false);
 assert.equal(await page.evaluate(()=>document.activeElement.id),'voice-end');
 await page.screenshot({path:path.join(out,'voice-focus-starting.png')});
 await page.evaluate(()=>{
  const v=presenceFixture.voice,track={enabled:true,readyState:'live',stop(){this.readyState='ended';}};
  v.stream={getAudioTracks:()=>[track],getTracks:()=>[track]};
  v.message(JSON.stringify({type:'session_ready',session_id:'local-fixture',connection_id:'browser:fixture',generation:1,conversation_id:v.startContext.conversation_id,handoff_protocol:1}));
 });
 assert.equal(await page.locator('#presence').getAttribute('data-mode'),'listening');
 await page.evaluate(()=>presenceFixture.voice.audio({type:'level',rms:.025,output_rms:0,playing:false,played_ms:0}));
 for(const [width,height] of [[1440,960],[390,844],[320,568],[844,390]]){
  await page.setViewportSize({width,height});await page.waitForTimeout(150);
  const layout=await page.evaluate(()=>{const rect=id=>{const b=document.getElementById(id).getBoundingClientRect();return {x:b.x,y:b.y,width:b.width,height:b.height};};return {scene:rect('orb'),face:rect('mascot'),end:rect('voice-end'),width:innerWidth,height:innerHeight,scroll:document.documentElement.scrollWidth};});
  assert(layout.scroll<=width,JSON.stringify(layout));assert(layout.end.y>=0&&layout.end.y+layout.end.height<=height+1,JSON.stringify(layout));
  assert(layout.scene.height>=(height<620?145:300),JSON.stringify(layout));assert(layout.face.width>=35,JSON.stringify(layout));
  assert.equal(await page.locator('#chat-messages').isVisible(),false);assert(await page.locator('#voice-local').isVisible());
  const labels=await page.locator('#voice-focus .voice-actions button:visible').evaluateAll(nodes=>nodes.map(n=>{const range=document.createRange();range.selectNodeContents(n);const b=n.getBoundingClientRect();return {text:n.textContent,contained:[...range.getClientRects()].every(r=>r.left>=b.left&&r.right<=b.right&&r.top>=b.top&&r.bottom<=b.bottom)};}));
  assert(labels.every(n=>n.contained),JSON.stringify(labels));
  await page.screenshot({path:path.join(out,`voice-focus-listening-${width}x${height}.png`)});
 }
 checks.push('voice_focus_starting_and_listening_hide_text_keep_end_and_original_scene');
 await page.setViewportSize({width:390,height:844});
 await page.evaluate(()=>presenceFixture.voice.audio({type:'level',rms:.025,output_rms:.08,playing:true,played_ms:20}));
 assert.equal(await page.locator('#presence').getAttribute('data-mode'),'speaking');
 assert.deepEqual(await page.evaluate(()=>presenceFixture.presence.level),{input:.2,output:.64});
 await page.locator('#voice-mute').click();
 assert.equal(await page.locator('#presence').getAttribute('data-mode'),'idle');
 await page.evaluate(()=>presenceFixture.voice.audio({type:'level',rms:.8,output_rms:.04,playing:true,played_ms:40}));
 assert.deepEqual(await page.evaluate(()=>presenceFixture.presence.level),{input:0,output:.32});
 assert.equal(await page.locator('#presence').getAttribute('data-mode'),'speaking');
 await page.screenshot({path:path.join(out,'voice-focus-speaking-muted.png')});
 await page.evaluate(()=>document.documentElement.style.fontSize='200%');
 await page.screenshot({path:path.join(out,'voice-focus-large-text.png')});
 const enlargedEnd=await page.locator('#voice-end').boundingBox();assert(enlargedEnd.y+enlargedEnd.height<=845);
 await page.evaluate(()=>document.documentElement.style.fontSize='');
 await page.emulateMedia({reducedMotion:'reduce'});await page.waitForTimeout(160);
 const stillVoice=await picture();await page.evaluate(()=>presenceFixture.voice.audio({type:'level',rms:.8,output_rms:.6,playing:true,played_ms:50}));await page.waitForTimeout(120);
 assert.equal(await picture(),stillVoice);await page.screenshot({path:path.join(out,'voice-focus-reduced-motion.png')});
 await page.emulateMedia({reducedMotion:'no-preference'});
 await page.locator('#workspace-menu > summary').click();await page.locator('#workspace-menu [data-view=tasks]').click();
 assert(await page.locator('#voice-dock #voice-end').isVisible());
 await page.locator('#workspace-menu > summary').click();await page.getByRole('button',{name:'Chat',exact:true}).click();
 assert(await page.locator('#voice-focus-controls #voice-end').isVisible());
 await page.locator('#voice-end').click();
 assert.equal(await page.evaluate(()=>presenceFixture.presence.level),null);
 assert(await page.locator('#voice-focus').isVisible());assert.equal(await page.locator('#chat-messages').isVisible(),false);
 await page.evaluate(()=>presenceFixture.voice.message(JSON.stringify({type:'session_closed',session_id:'local-fixture',connection_id:'browser:fixture',generation:1,previous_unconfirmed:0,provider:'closed_confirmed'})));
 assert(await page.locator('#voice-focus').isVisible());assert.match(await page.locator('#presence-title').textContent(),/endet/);
 await page.screenshot({path:path.join(out,'voice-focus-draining.png')});
 let releaseHistory;historyGate=new Promise(resolve=>releaseHistory=resolve);
 chats.get(CH).messages.push({message_id:'m-4444444444444444',role:'user',text:'Diese Worte wurden gesprochen.'},{message_id:'m-5555555555555555',role:'assistant',text:'Die Antwort steht im selben Chat.'});
 await page.evaluate(()=>presenceFixture.voice.message(JSON.stringify({type:'conversation_flushed',session_id:'local-fixture',conversation_id:'c-1111111111111111',status:'complete',provider_closed:true})));
 assert(await page.locator('#voice-focus').isVisible());assert.match(await page.locator('#presence-title').textContent(),/verlauf wird gelesen/);
 releaseHistory();historyGate=null;await page.locator('#message-form').waitFor({state:'visible'});
 assert.match(await page.locator('#chat-messages').textContent(),/Diese Worte wurden gesprochen.*Die Antwort steht im selben Chat/s);
 assert.equal(await page.locator('#chat').getAttribute('data-conversation-id'),CH);
 const latest=await page.locator('[data-message-id="m-5555555555555555"]').evaluate(n=>{const box=n.getBoundingClientRect(),parent=document.getElementById('chat-messages'),viewport=parent.getBoundingClientRect();return {top:box.top,bottom:box.bottom,viewTop:viewport.top,viewBottom:viewport.bottom,scrollTop:parent.scrollTop};});
 assert(latest.scrollTop>0&&latest.top>=latest.viewTop&&latest.bottom<=latest.viewBottom,JSON.stringify(latest));
 checks.push('long_history_returns_to_visible_latest_spoken_reply');
 assert.equal(await page.evaluate(()=>document.activeElement.id),'chat');
 assert.equal(await page.locator('#voice-start').isEnabled(),true,'ending permits a later explicit start, never an automatic restart');
 assert.equal(await page.locator('#message-text').inputValue(),'Dieser Entwurf bleibt beim Sprachende erhalten.');
 await page.screenshot({path:path.join(out,'voice-focus-returned-chat.png')});
 await page.locator('#chat-messages').evaluate(n=>n.scrollTop=0);
 await page.evaluate(()=>presenceFixture.chat.reload());
 assert.equal(await page.locator('#chat-messages').evaluate(n=>n.scrollTop),0,'a later read must respect intentional scrolling to older messages');
 checks.push('return_anchor_applied_once_respects_later_manual_scroll');
 // The same confirmed end can happen while tasks, not the chat, are on screen.
 await page.evaluate(()=>{
  const v=presenceFixture.voice,track={enabled:true,readyState:'live',stop(){this.readyState='ended';}};
  v.active=true;v.closing=false;v.ready=false;v.muted=false;v.flushed=null;v.closedProof=false;v.closeObserved=false;v.handoffRequired=true;
  v.ws={readyState:1,send(){},close(){this.readyState=3;}};
  v.stream={getAudioTracks:()=>[track],getTracks:()=>[track]};v.buttons();
  v.message(JSON.stringify({type:'session_ready',session_id:'local-fixture',connection_id:'browser:fixture',generation:1,conversation_id:v.startContext.conversation_id,handoff_protocol:1}));
 });
 await page.locator('#workspace-menu > summary').click();await page.locator('#workspace-menu [data-view=tasks]').click();
 await page.locator('#voice-dock #voice-end').click();
 chats.get(CH).messages.push({message_id:'m-6666666666666666',role:'assistant',text:'Auch nach dem Gesprächsende bei den Aufträgen siehst du diese letzte Antwort.'});
 await page.evaluate(()=>{
  const v=presenceFixture.voice;
  v.message(JSON.stringify({type:'session_closed',session_id:'local-fixture',connection_id:'browser:fixture',generation:1,previous_unconfirmed:0,provider:'closed_confirmed'}));
  v.message(JSON.stringify({type:'conversation_flushed',session_id:'local-fixture',conversation_id:'c-1111111111111111',status:'complete',provider_closed:true}));
 });
 await page.waitForFunction(()=>presenceFixture.state.voiceReturn?.reveal===true);
 assert(await page.locator('#tasks-view').isVisible());assert.equal(await page.locator('#chat-messages').isVisible(),false);
 await page.locator('#workspace-menu > summary').click();await page.getByRole('button',{name:'Chat',exact:true}).click();
 const dockLatest=await page.locator('[data-message-id="m-6666666666666666"]').evaluate(n=>{const b=n.getBoundingClientRect(),c=document.getElementById('chat-messages').getBoundingClientRect();return b.top>=c.top&&b.bottom<=c.bottom;});
 assert(dockLatest,'return from task dock must reveal the newest confirmed response once');
 assert.equal(await page.evaluate(()=>presenceFixture.state.voiceReturn),null);
 await page.screenshot({path:path.join(out,'voice-focus-returned-from-dock.png')});
 checks.push('confirmed_dock_end_defers_anchor_until_actual_chat_return');
 checks.push('closed_before_flush_stays_focused_until_same_chat_fresh_get','draft_retained_and_focus_returns_without_keyboard_or_autostart','large_text_reduced_motion_and_existing_navigation');
 await page.setViewportSize({width:390,height:480});await page.locator('#message-text').focus();
 const keyboardLayout=await page.locator('#message-form').boundingBox();assert(keyboardLayout.y+keyboardLayout.height<=481,JSON.stringify(keyboardLayout));
 await page.screenshot({path:path.join(out,'shared-chat-keyboard-height.png')});
 await page.setViewportSize({width:390,height:844});
 await page.evaluate(()=>{
  const {state,syncPresence}=presenceFixture;
  state.runs=[{zustand_code:'RUNNING',offen:true}];syncPresence();
 });
 assert.match(await page.locator('#presence-detail').textContent(),/1 Auftrag im Hintergrund/);
 assert.equal(await page.locator('#presence').getAttribute('data-mode'),'idle');
 await page.screenshot({path:path.join(out,'shared-chat-background-work.png')});
 await page.locator('#presence-detail').click();assert(await page.locator('#tasks-view').isVisible());
 checks.push('voice_state_meter_mute_and_end_keep_microphone_truth','background_work_separate_and_opens_existing_tasks','keyboard_height_keeps_draft_reachable_after_voice');
 assert.equal(requests.filter(r=>r.method==='POST').length,1);assert.equal(await page.evaluate(()=>micCalls),0);assert.deepEqual(faults,[]);
 checks.push('attachment_and_secondary_navigation_accessible','one_new_empty_chat_and_original_history_preserved','no_microphone_provider_or_voice_session');
 const report={state:'passed',checks,real_dashboard_assets:true,all_network_fulfilled_locally:true,core_sessions:0,provider_calls:0,microphone_calls:0,voice_events:'local protocol fixture only',keyboard:'focused composer with shortened viewport; no physical keyboard',javascript_errors:faults,executablePath};
 fs.writeFileSync(path.join(out,'report.json'),JSON.stringify(report,null,2)+'\n');console.log(JSON.stringify(report));
})().catch(e=>{console.error(e.stack);process.exitCode=1;}).finally(async()=>{await browser?.close();});
