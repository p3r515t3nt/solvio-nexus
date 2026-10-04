/* Actual Chromium -> authenticated temporary Core -> original native HA read
 * client. HA transport is synthetic; no task tick or device effect is run. */
const {chromium}=require('playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const out=path.resolve(process.argv[2]),url=fs.readFileSync(path.join(out,'dashboard-url.txt'),'utf8').trim();
const origin=new URL(url).origin,audit=()=>JSON.parse(fs.readFileSync(path.join(out,'audit.json'),'utf8'));
assert.equal(new URL(url).hostname,'127.0.0.1');
const results=[],reads=[],posts=[],faults=[],outside=[];let browser;
(async()=>{
  browser=await chromium.launch({headless:true,executablePath:process.env.BROWSER_EXECUTABLE});
  const context=await browser.newContext({ignoreHTTPSErrors:true,viewport:{width:1440,height:1000}});
  await context.route('**/*',route=>{
    if(new URL(route.request().url()).origin===origin)return route.continue();
    outside.push(route.request().url());return route.abort();
  });
  const page=await context.newPage();page.setDefaultTimeout(10000);
  page.on('pageerror',e=>faults.push(e.message));
  page.on('request',request=>{if(new URL(request.url()).pathname==='/v1/agent/action-resources')
    reads.push({url:request.url(),method:request.method(),body:request.postData()});});
  await page.addInitScript(()=>{
    window.micCalls=0;navigator.mediaDevices.getUserMedia=async()=>{window.micCalls++;throw Error('Microphone forbidden');};
  });
  const field=name=>page.locator(`[data-action-field="${name}"]`);
  async function waitAudit(predicate){
    const until=Date.now()+5000;while(!predicate(audit())&&Date.now()<until)await new Promise(r=>setTimeout(r,25));
    assert(predicate(audit()));return audit();
  }
  async function choose(){
    if(!await page.locator('#task-options').evaluate(n=>n.open))await page.locator('#task-options summary').click();
    await page.locator('#scope').selectOption('action');await page.locator('#action-mode').selectOption('exact');
    await field('kind').selectOption('ha');
    await page.waitForFunction(()=>!document.querySelector('[data-action-field="device"]').disabled);
  }
  async function home(){
    await page.getByRole('button',{name:'SOLVIO',exact:true}).click();await page.locator('#overview-view').waitFor({state:'visible'});
    if(!await page.locator('#conversation-entry').isVisible())await page.locator('#new-task').click();
    await page.locator('#objective').waitFor({state:'visible'});
  }
  async function capture(name){
    for(const width of [320,390,1440]){
      await page.setViewportSize({width,height:1000});await page.evaluate(()=>scrollTo(0,0));
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),`overflow ${width}`);
      await page.screenshot({path:path.join(out,`${name}-${width}.png`),fullPage:true});
    }
  }
  let loseNext=false;
  await page.route('**/v1/agent/tasks',async route=>{
    if(route.request().method()!=='POST')return route.continue();
    const response=await route.fetch(),accepted=await response.json();
    posts.push({request:route.request().postDataJSON(),accepted,status:response.status()});
    if(loseNext){loseNext=false;return route.abort('failed');}
    return route.fulfill({response});
  });
  await page.goto(url);await page.locator('#enrollment').fill('n5-test-only-'.padEnd(43,'0'));
  await page.locator('#login-form button').click();await page.locator('#workspace').waitFor({state:'visible'});
  await page.waitForFunction(()=>!document.querySelector('#task-submit').disabled);
  assert.equal(reads.length,0);assert.equal(audit().native_calls.length,0);
  await choose();
  assert.equal(reads.length,1);assert.equal(reads[0].method,'GET');assert.equal(reads[0].body,null);
  assert.equal(await field('device').inputValue(),'');
  assert.deepEqual(await field('device').locator('option').evaluateAll(rows=>rows.map(row=>row.value)),
    ['','light.fixture','switch.fixture']);
  const account=audit().services.find(row=>row.service==='ha').account;
  assert.equal(new URL(reads[0].url).searchParams.get('account'),account);
  assert.equal(audit().runs.length,0);results.push('opt-in native read returns only exposed executable normal devices; no task or device is chosen');

  await field('device').selectOption('light.fixture');await field('desired').selectOption('on');
  fs.writeFileSync(path.join(out,'fixture-control.json'),JSON.stringify({hide_light:true}));
  await waitAudit(a=>a.light_hidden);
  await page.getByRole('button',{name:'Geräteliste neu laden',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('[data-action-field="device"]').options.length===2);
  assert.equal(await field('device').inputValue(),'');assert.equal(await field('desired').inputValue(),'');
  assert(await page.getByText('Das gewählte Gerät ist nicht mehr verfügbar.',{exact:false}).isVisible());
  fs.writeFileSync(path.join(out,'fixture-control.json'),JSON.stringify({hide_light:false}));await waitAudit(a=>!a.light_hidden);
  await page.getByRole('button',{name:'Geräteliste neu laden',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('[data-action-field="device"]').options.length===3);
  assert.equal(await field('device').inputValue(),'light.fixture');assert.equal(await field('desired').inputValue(),'');
  assert.equal(audit().runs.length,0);results.push('fresh exposure removal invalidates selected target without silently substituting a device or desired state');

  await field('desired').selectOption('on');await page.locator('#objective').fill('Schalte die ausdrücklich ausgewählte Fixturelampe ein.');
  await capture('device-on');await page.locator('#task-submit').click();await page.locator('#conversation').waitFor({state:'visible'});
  assert.equal(posts[0].status,201);
  assert.deepEqual(posts[0].request.task.action_request,{actions:[{action_id:'a1',service:'ha',operation:'set_state',account,
    target:{entity_id:'light.fixture'},payload:{state:'on'}}]});
  await waitAudit(a=>a.runs.length===1);results.push('exact on command admitted through real dashboard and canonical Core task entrance');

  await home();await choose();await field('device').selectOption('switch.fixture');
  assert.deepEqual(await field('desired').locator('option').evaluateAll(rows=>rows.map(row=>row.value)),['','on','off']);
  await field('desired').selectOption('off');await page.locator('#objective').fill('Schalte den ausdrücklich ausgewählten Fixtureschalter aus.');
  await page.locator('#task-submit').click();await page.locator('#conversation').waitFor({state:'visible'});
  assert.equal(posts[1].status,201);assert.deepEqual(posts[1].request.task.action_request.actions[0],
    {action_id:'a1',service:'ha',operation:'set_state',account,target:{entity_id:'switch.fixture'},payload:{state:'off'}});
  await waitAudit(a=>a.runs.length===2);results.push('switch exposes only on/off and binds the exact off command');

  await home();await choose();await field('device').selectOption('light.fixture');await field('desired').selectOption('brightness');
  await field('brightness').fill('43');await page.locator('#objective').fill('Stelle die ausdrücklich ausgewählte Fixturelampe auf 43 Prozent.');
  await capture('device-brightness');loseNext=true;
  await page.locator('#task-submit').click();await page.locator('#task-retry').waitFor({state:'visible'});
  assert.equal(posts[2].status,201);assert(await field('device').isDisabled());assert(await field('desired').isDisabled());assert(await field('brightness').isDisabled());
  const before=await waitAudit(a=>a.runs.length===3);
  await page.locator('#task-retry').click();await page.locator('#conversation').waitFor({state:'visible'});
  assert.equal(posts[3].status,201);assert.deepEqual(posts[3].request,posts[2].request);assert.deepEqual(posts[3].accepted,posts[2].accepted);
  assert.deepEqual(posts[2].request.task.action_request.actions[0],
    {action_id:'a1',service:'ha',operation:'set_brightness',account,target:{entity_id:'light.fixture'},payload:{brightness_pct:43}});
  assert.deepEqual((await waitAudit(a=>a.runs.length===3)).runs,before.runs);
  results.push('light brightness exact integer survives lost response; frozen retry creates no second task');

  await home();await choose();await field('device').selectOption('light.fixture');await field('desired').selectOption('on');
  await page.locator('#logout').click();await page.locator('#login-panel').waitFor({state:'visible'});
  for(const name of ['device','desired','brightness','account','kind'])assert.equal(await field(name).inputValue(),'');
  assert.equal(await field('device').locator('option').count(),1);assert.deepEqual(faults,[]);assert.deepEqual(outside,[]);
  assert.equal(await page.evaluate(()=>window.micCalls),0);
  const final=audit();assert.equal(final.runs.length,3);assert.equal(final.native_mutations,0);
  assert.equal(final.counts.agent_action_claims,0);assert.equal(final.pending_approvals,0);
  assert(final.native_calls.length>0&&final.native_calls.every(([method,url])=>method==='GET'&&url.endsWith('/api/states')));
  results.push('logout clears device data; 320/390/1440 fit with no microphone, outside request, native write or JavaScript error');
  const evidence={results,reads,posts,audit:final,microphone_calls:0,external_requests:outside,js_errors:faults};
  fs.writeFileSync(path.join(out,'ha-browser-check.json'),JSON.stringify(evidence,null,2));console.log(JSON.stringify(evidence,null,2));
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{await browser?.close();});
