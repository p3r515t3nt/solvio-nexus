/* Actual pinned Electron window; temporary real Core auth, synthetic stored evidence. */
const {_electron}=require('playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const out=path.resolve(process.argv[2]),entry=path.resolve(process.argv[3]),binary=path.resolve(process.argv[4]);
const fixture=JSON.parse(fs.readFileSync(path.join(out,'fixture.json'),'utf8'));
assert.equal(fixture.test_only,true);assert(fixture.control_socket.startsWith('/private/tmp/solvio-observer-'));
assert.equal(new URL(fixture.origin).hostname,'127.0.0.1');
const audit=()=>JSON.parse(fs.readFileSync(path.join(out,'audit.json'),'utf8'));
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function until(predicate,limit=15000){const start=Date.now();while(!await predicate()){if(Date.now()-start>limit)throw Error('condition timed out');await pause(100);}}
let app,page,sequence=audit().command_seq;
const proof={test_only:true,cases:[],screenshots:[],originalRenderer:true,realProviderTurns:0,nativeRPCTurns:0};
async function command(action){const seq=++sequence;fs.writeFileSync(path.join(out,'command.json'),JSON.stringify({action,seq}));await until(()=>audit().command_seq===seq);}
async function open(){
  app=await _electron.launch({executablePath:binary,args:[entry,path.join(out,'fixture.json')],
    env:{PATH:process.env.PATH,HOME:process.env.HOME,USER:process.env.USER,TMPDIR:process.env.TMPDIR},timeout:20000});
  page=await app.firstWindow();page.setDefaultTimeout(15000);
  await page.getByRole('heading',{name:'Belegter Zwischenstand',exact:true}).waitFor();
  await until(()=>app.evaluate(()=>!!globalThis.__solvioObserverTest));
  assert.equal(await page.locator('[data-renderer="hermes-desktop-thread"]').count(),1);
}
async function close(){
  const snapshot=await app.evaluate(()=>globalThis.__solvioObserverTest.snapshot());
  await app.close();app=null;await until(()=>!fs.existsSync(snapshot.folder));
  return snapshot;
}
(async()=>{
  const before=audit();await open();
  const started=await app.evaluate(({BrowserWindow})=>({
    ...globalThis.__solvioObserverTest.snapshot(),windows:BrowserWindow.getAllWindows().length,
    preferences:((p)=>({sandbox:p.sandbox,contextIsolation:p.contextIsolation,nodeIntegration:p.nodeIntegration,
      webviewTag:p.webviewTag,preload:!!p.preload}))(BrowserWindow.getAllWindows()[0].webContents.getLastWebPreferences())
  }));
  assert.equal(started.stage,'active');assert.equal(started.enrollments,1);assert.equal(started.persistentSession,false);
  assert.deepEqual(started.preferences,{sandbox:true,contextIsolation:true,nodeIntegration:false,webviewTag:false,preload:false});
  assert.equal(started.windows,1);proof.start=started;
  assert.equal(await page.locator('[data-role="user"]').count(),1);
  assert.equal(await page.locator('[data-role="assistant"]').count(),1);
  assert.equal(await page.getByRole('link',{name:'Quelle der lokalen Testdaten'}).getAttribute('href'),'https://example.invalid/observer-fixture');
  assert.equal(await page.locator('textarea,[contenteditable="true"]').count(),0);
  assert.equal(await page.locator('[data-share-publish]').isVisible(),false);
  assert.equal(await page.locator('[data-share-publish]').isDisabled(),true);
  await page.locator('[data-slot="solvio_native-tool"] button').first().click();
  assert((await page.locator('[data-slot="solvio_native-tool"] .solvio-evidence').first().textContent()).includes('fixture-thread-1'));
  await page.locator('[data-slot="solvio_native-tool"] button').first().click();
  proof.cases.push('real_native_window_original_thread_separate_observer_auth');
  for(const width of [320,390,1440]){
    await app.evaluate(({BrowserWindow},width)=>BrowserWindow.getAllWindows()[0].setContentSize(width,1000),width);
    await page.waitForFunction(width=>innerWidth===width,width);
    const sizes=await page.evaluate(()=>({width:innerWidth,scroll:document.documentElement.scrollWidth}));
    assert(sizes.scroll<=sizes.width,'horizontal overflow at '+width);
    const screenshot=path.join(out,'electron-'+width+'.png');await page.screenshot({path:screenshot,fullPage:true});
    proof.screenshots.push({width,path:screenshot,...sizes});
    const result=page.getByRole('heading',{name:'Belegter Zwischenstand',exact:true});
    await result.scrollIntoViewIfNeeded();const box=await result.boundingBox();
    assert(box&&box.y>=0&&box.y+box.height<=1000,'result cannot be reached at '+width);
    const resultShot=path.join(out,'electron-result-'+width+'.png');await page.screenshot({path:resultShot,fullPage:true});
    proof.screenshots.push({width,path:resultShot,result_in_view:true});
    await page.getByLabel('Auftrag ansehen').scrollIntoViewIfNeeded();
  }
  proof.cases.push('actual_native_window_widths_320_390_1440');
  const blocked=await page.evaluate(async()=>{
    const attempt=async fn=>{try{await fn();return 'allowed'}catch(error){return error.name||'blocked'}};
    return {
      task:await attempt(()=>fetch('/v1/agent/tasks',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})),
      memory:await attempt(()=>fetch('/v1/memory/memories')),
      microphone:await attempt(()=>navigator.mediaDevices.getUserMedia({audio:true})),
      display:await attempt(()=>navigator.mediaDevices.getDisplayMedia({video:true,audio:false})),
      node:typeof window.require,bridge:typeof window.hermesDesktop,
      popup:window.open('https://example.invalid/forbidden')===null
    };
  });
  assert.notEqual(blocked.task,'allowed');assert.notEqual(blocked.memory,'allowed');
  assert.notEqual(blocked.microphone,'allowed');assert.notEqual(blocked.display,'allowed');
  assert.equal(blocked.node,'undefined');assert.equal(blocked.bridge,'undefined');assert.equal(blocked.popup,true);
  const oldURL=page.url();await page.evaluate(()=>{location.href='https://example.invalid/forbidden';});await pause(200);
  assert.equal(page.url(),oldURL);proof.blocks=blocked;
  assert.equal(await app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows().length),1);
  proof.cases.push('native_media_navigation_and_mutation_sperren');
  // A cancelled native navigation leaves Playwright's navigation waiter pending;
  // a genuine same-Core reload confirms the still-usable original surface.
  await page.reload();await page.getByRole('heading',{name:'Belegter Zwischenstand',exact:true}).waitFor();
  await page.getByLabel('Auftrag ansehen').selectOption(fixture.second_native_run);
  await page.getByRole('heading',{name:'Eigener Hermes-Arbeitsstand für den Museumsbesuch.',exact:true}).waitFor();
  await page.getByRole('heading',{name:'Museumszwischenstand',exact:true}).waitFor();
  assert((await page.locator('[data-role="user"]').textContent()).includes('Museumsbesuch'));
  assert.equal(await page.getByRole('link',{name:'Museumsquelle'}).getAttribute('href'),'https://example.invalid/museum-fixture');
  assert.equal(await page.getByRole('heading',{name:'Belegter Zwischenstand',exact:true}).count(),0);
  await page.locator('[data-slot="solvio_native-tool"] button').first().click();
  assert((await page.locator('[data-slot="solvio_native-tool"] .solvio-evidence').first().textContent()).includes('museum-thread'));
  assert(!(await page.locator('body').textContent()).includes('fixture-thread-1'));
  await page.screenshot({path:path.join(out,'electron-second-hermes.png'),fullPage:true});
  await page.reload();await page.getByRole('heading',{name:'Museumszwischenstand',exact:true}).waitFor();
  proof.cases.push('two_hermes_runs_preserve_own_roles_results_sources_and_evidence');
  await page.getByLabel('Auftrag ansehen').selectOption(fixture.other_run);
  await page.getByRole('heading',{name:'Zweiter lokaler Auftrag ohne Hermes-Beleg.',exact:true}).waitFor();
  await pause(2800);assert.equal(await page.locator('[data-role="assistant"]').count(),0);
  await page.getByLabel('Auftrag ansehen').selectOption(fixture.native_run);
  await page.getByRole('heading',{name:'Belegter Zwischenstand',exact:true}).waitFor();
  await page.reload();await page.getByRole('heading',{name:'Belegter Zwischenstand',exact:true}).waitFor();
  proof.cases.push('same_core_catalog_selection_and_fresh_load');
  await page.goto(fixture.origin+'/dashboard/hermes/solvio-view.html?run=ar-0000000000000000');
  await page.getByText('Auftrag oder Anmeldung nicht mehr verfügbar. Bitte im Dashboard neu auswählen.').waitFor();
  assert.equal(await page.locator('[data-role="assistant"]').count(),0);
  await page.screenshot({path:path.join(out,'electron-missing.png'),fullPage:true});
  proof.cases.push('missing_run_clears_previous_thread');
  await page.goto(oldURL);await page.getByRole('heading',{name:'Belegter Zwischenstand',exact:true}).waitFor();
  const count=audit().observer_enrollments;
  await command('revoke');await page.getByText('Diese Ansicht ist nicht mehr angemeldet. Schließe das Fenster, um den Zugang zu beenden.').waitFor();
  await pause(3200);assert.equal(audit().observer_enrollments,count);
  assert.equal(await page.locator('[data-role="assistant"],select').count(),0);
  await page.screenshot({path:path.join(out,'electron-revoked.png'),fullPage:true});
  proof.revoked=await close();assert.equal(audit().active_observers,0);
  proof.cases.push('revocation_clears_view_no_enrollment_loop_and_profile_deleted');
  await open();assert.equal(audit().observer_enrollments,count+1);
  proof.onlineClose=await close();await until(()=>audit().active_observers===0);
  proof.cases.push('deliberate_fresh_process_then_confirmed_self_logout');
  await open();await command('offline');
  await page.getByText('Die Verbindung zum Core ist unterbrochen. Schließe das Fenster und öffne es später erneut.').waitFor();
  proof.offline=await close();proof.cases.push('offline_clears_view_without_retrying_enrollment');
  const after=audit();assert.equal(after.runs,3);assert.equal(after.real_provider_turns,0);
  assert.equal(after.native_rpc_turns,0);assert.equal(after.window_peers,0);
  assert.equal(after.native_events,6);assert.equal(after.second_native_events,1);assert.equal(after.native_run_state,'WAITING_USER');
  for(const operation of after.control_operations)assert.deepEqual(operation,{op:'browser_session_enroll',purpose:'hermes_observer_v1',fields:['op','purpose']});
  for(const request of after.requests_after_preparation){
    assert(request.method==='GET'||request.method==='POST'&&['/v1/browser/session/login','/v1/browser/session/logout'].includes(request.path));
  }
  proof.before=before;proof.after=after;proof.passed=proof.cases.length;proof.failed=0;
  fs.writeFileSync(path.join(out,'hermes-electron-check.json'),JSON.stringify(proof,null,2));
  console.log(JSON.stringify({passed:proof.passed,failed:0,evidence:path.join(out,'hermes-electron-check.json')}));
})().catch(error=>{proof.passed=proof.cases.length;proof.failed=1;proof.error=error.stack;
  fs.writeFileSync(path.join(out,'hermes-electron-check.json'),JSON.stringify(proof,null,2));
  console.error(error.stack);process.exitCode=1;}).finally(async()=>{if(app)await app.close().catch(()=>{});});
