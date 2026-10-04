/* Actual A5 dashboard integration. Native RPC and window pixels are synthetic. */
const {chromium}=require('playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const out=path.resolve(process.argv[2]),url=fs.readFileSync(path.join(out,'dashboard-url.txt'),'utf8').trim();
const fixture=JSON.parse(fs.readFileSync(path.join(out,'fixture.json'),'utf8')),origin=new URL(url).origin;
assert.equal(new URL(url).hostname,'127.0.0.1');
const audit=()=>JSON.parse(fs.readFileSync(path.join(out,'audit.json'),'utf8'));
let browser;
(async()=>{
  browser=await chromium.launch({headless:true,executablePath:process.env.BROWSER_EXECUTABLE});
  const context=await browser.newContext({ignoreHTTPSErrors:true,viewport:{width:1440,height:1100}});
  const rejected=[],requests=[],faults=[],websockets=[];
  await context.route('**/*',route=>{
    const parsed=new URL(route.request().url());
    if(parsed.origin!==origin||parsed.pathname.startsWith('/api/')){rejected.push(parsed.href);return route.abort();}
    requests.push({method:route.request().method(),path:parsed.pathname});return route.continue();
  });
  const page=await context.newPage();page.setDefaultTimeout(15000);
  page.on('pageerror',error=>{faults.push(error.message);console.error('Browser error:',error.stack);});
  page.on('websocket',socket=>websockets.push(socket.url()));
  await page.addInitScript(()=>{
    if(window===window.top)window.captureProof={started:0,stopped:0,microphone:0};
    navigator.mediaDevices.getUserMedia=async()=>{window.top.captureProof.microphone++;throw Error('Microphone forbidden');};
    navigator.mediaDevices.getDisplayMedia=async options=>{
      if(options.audio!==false)throw Error('Audio not allowed');
      window.top.captureProof.started++;
      const canvas=document.createElement('canvas');canvas.width=320;canvas.height=200;
      const ctx=canvas.getContext('2d');ctx.fillStyle='#116699';ctx.fillRect(0,0,320,200);
      const stream=canvas.captureStream(5),track=stream.getVideoTracks()[0],stop=track.stop.bind(track);
      track.getSettings=()=>({displaySurface:'window'});
      let stopped=false;track.stop=()=>{if(!stopped){stopped=true;window.top.captureProof.stopped++;}stop();};
      return stream;
    };
  });
  await page.goto(url);await page.locator('#enrollment').fill('n5-test-only-'.padEnd(43,'0'));
  await page.locator('#login-form button').click();await page.locator('#workspace').waitFor({state:'visible'});
  await page.locator('nav [data-room="hermes"]').click();
  await page.locator('#workroom-task').selectOption(fixture.native_run);
  let frame=page.frameLocator('#hermes-frame');
  await frame.locator('[data-renderer="hermes-desktop-thread"]').waitFor();
  const openDetails=async current=>{
    const details=current.locator('[data-workspace-details]');
    if(!await details.evaluate(node=>node.open))await details.locator(':scope > summary').click();
  };
  assert.equal(await frame.locator('[data-workspace-details]').evaluate(node=>node.open),false);
  assert.equal(await frame.locator('#root [data-role="tool"]').count(),0);
  await openDetails(frame);
  await frame.getByText('2 belegte Hermes-Aufrufe',{exact:true}).waitFor();
  await frame.getByText('Native Websuche beendet; Ergebnisprüfung steht aus.',{exact:true}).first().waitFor();
  assert.equal(await frame.locator('#root').isVisible(),true);
  for(const button of await frame.locator('#solvio-hermes-evidence [data-slot="solvio_native-tool"] button').all())await button.click();
  await frame.locator('#solvio-hermes-evidence .solvio-evidence').first().waitFor();
  const content=await frame.locator('#solvio-workspace').textContent();
  for(const id of ['local-thread-1','local-thread-2','local-turn-1','local-turn-2'])assert(content.includes(id),id);
  assert(content.includes('Ausgang ungewiss'));assert(content.includes('noch kein Auftragsabschluss'));
  assert.equal(websockets.length,0);assert.deepEqual(await page.evaluate(()=>window.captureProof),{started:0,stopped:0,microphone:0});
  const before=audit();assert.equal(before.local_rpc_turns,2);assert.equal(before.real_provider_turns,0);
  assert.equal(before.window_peers,0);
  const actualFrame=page.frames().find(candidate=>candidate.url().includes('/dashboard/hermes/solvio-view.html'));
  const grouping=await actualFrame.evaluate(async()=>{
    const {observedCalls}=await import('/dashboard/hermes/solvio-workspace.js');
    const event={step_id:'as-step',kind:'native_progress',at:1,observation:{runtime:'hermes-codex-app-server',
      invocation_id:'inv-a',native_thread_id:'same-thread',native_turn_id:'same-turn',seq:1,event:'started'}};
    return {duplicates:observedCalls([event,event]).length,
      separate:observedCalls([event,{...event,observation:{...event.observation,invocation_id:'inv-b'}}]).length,
      foreign:observedCalls([{...event,observation:{...event.observation,runtime:'not-hermes'}}]).length};
  });
  assert.deepEqual(grouping,{duplicates:1,separate:2,foreign:0});
  await frame.locator('[data-workspace-details] > summary').click();
  for(const width of [320,390,1440]){
    await page.setViewportSize({width,height:1100});await page.evaluate(()=>scrollTo(0,0));
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    assert.equal(await actualFrame.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    await page.screenshot({path:path.join(out,`workspace-${width}.png`),fullPage:true});
  }
  // The same existing WindowShare class now lives inside the Hermes frame.
  // Its capture is synthetic; the real HTTPS hub and teardown are not stubbed.
  await frame.getByText('Hermes-Fenster ansehen (optional)',{exact:true}).click();
  await frame.getByText('Am Mac bereitstellen',{exact:true}).click();
  await frame.getByRole('button',{name:'Fenster auf diesem Rechner auswählen',exact:true}).click();
  await frame.locator('[data-share-status]').filter({hasText:'Fensterfreigabe aktiv'}).waitFor();
  assert.equal(await page.evaluate(()=>window.captureProof.started),1);
  await frame.getByText('Hermes-Fenster ansehen (optional)',{exact:true}).click();
  await page.waitForFunction(()=>window.captureProof.stopped===1);
  await frame.getByText('Hermes-Fenster ansehen (optional)',{exact:true}).click();
  await frame.getByRole('button',{name:'Fenster auf diesem Rechner auswählen',exact:true}).click();
  await frame.locator('[data-share-status]').filter({hasText:'Fensterfreigabe aktiv'}).waitFor();
  await page.locator('#workroom-task').selectOption(fixture.other_run);
  frame=page.frameLocator('#hermes-frame');
  await frame.locator('#root [data-role="user"]').waitFor();
  await openDetails(frame);
  await frame.getByText('Für diesen Auftrag liegen noch keine belegten Hermes-Aufrufe vor. Sein tatsächlicher Arbeitsstand steht oben.',{exact:true}).waitFor();
  assert.equal(await frame.locator('#root').isVisible(),true);
  assert.equal(await frame.locator('#solvio-hermes-evidence').isVisible(),false);
  await page.waitForFunction(()=>window.captureProof.stopped===2);
  // A second capture must also end when the actual authenticated session ends.
  await page.locator('#workroom-task').selectOption(fixture.native_run);
  frame=page.frameLocator('#hermes-frame');await openDetails(frame);await frame.getByText('2 belegte Hermes-Aufrufe',{exact:true}).waitFor();
  await frame.getByText('Hermes-Fenster ansehen (optional)',{exact:true}).click();
  await frame.getByText('Am Mac bereitstellen',{exact:true}).click();
  await frame.getByRole('button',{name:'Fenster auf diesem Rechner auswählen',exact:true}).click();
  await frame.locator('[data-share-status]').filter({hasText:'Fensterfreigabe aktiv'}).waitFor();
  const session=await (await context.request.get(origin+'/v1/browser/session')).json();
  const logout=await context.request.post(origin+'/v1/browser/session/logout',{
    headers:{Origin:origin,'X-CSRF-Token':session.csrf_token},data:{}});
  assert.equal(logout.status(),200);
  await page.waitForFunction(()=>window.captureProof.stopped===3);
  await page.locator('#login-panel').waitFor({state:'visible'});
  assert.equal(await page.locator('#hermes-frame').getAttribute('src'),null);
  const until=Date.now()+5000;while(audit().window_peers!==0&&Date.now()<until)await new Promise(resolve=>setTimeout(resolve,50));
  assert.equal(audit().window_peers,0);assert.equal(audit().local_rpc_turns,2);assert.equal(audit().runs,2);
  assert.deepEqual(rejected,[]);assert.deepEqual(faults,[]);
  const proof=await page.evaluate(()=>window.captureProof);assert.deepEqual(proof,{started:3,stopped:3,microphone:0});
  // The external APIRequestContext revocation above does not pass through the
  // page route observer. The rendered workspace itself issued no mutation.
  assert.deepEqual(requests.filter(request=>request.method!=='GET').map(request=>request.path),['/v1/browser/session/login']);
  const result={passed:true,allNativeInvocationsBound:true,grouping,nonHermesAccurate:true,
    unknownStepVisible:true,optionalCaptureOnly:true,collapseStops:true,taskSwitchStops:true,revocationStops:true,
    responsive:[320,390,1440],proof,audit:audit(),externalRequests:rejected,jsErrors:faults};
  fs.writeFileSync(path.join(out,'hermes-workspace-check.json'),JSON.stringify(result,null,2));console.log(JSON.stringify(result,null,2));
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{await browser?.close();});
