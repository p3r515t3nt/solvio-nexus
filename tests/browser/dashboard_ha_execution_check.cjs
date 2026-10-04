/* Browser admission -> detached real Core runtime -> synthetic HA REST effect
 * and native readback -> newly created browser page sees canonical success. */
const {chromium}=require('playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const out=path.resolve(process.argv[2]),url=fs.readFileSync(path.join(out,'dashboard-url.txt'),'utf8').trim();
const origin=new URL(url).origin,audit=()=>JSON.parse(fs.readFileSync(path.join(out,'audit.json'),'utf8'));
assert.equal(new URL(url).hostname,'127.0.0.1');
const faults=[],outside=[],results=[];let browser,micCalls=0;
(async()=>{
  browser=await chromium.launch({headless:true,executablePath:process.env.BROWSER_EXECUTABLE});
  async function session(token){
    const context=await browser.newContext({ignoreHTTPSErrors:true,viewport:{width:390,height:1000}});
    await context.route('**/*',route=>{
      if(new URL(route.request().url()).origin===origin)return route.continue();
      outside.push(route.request().url());return route.abort();
    });
    const page=await context.newPage();page.setDefaultTimeout(10000);
    page.on('pageerror',e=>faults.push(e.message));
    await page.exposeFunction('unexpectedMic',()=>micCalls++);
    await page.addInitScript(()=>{navigator.mediaDevices.getUserMedia=async()=>{await window.unexpectedMic();throw Error('Microphone forbidden');};});
    await page.goto(url);await page.locator('#enrollment').fill(token.padEnd(43,'0'));
    await page.locator('#login-form button').click();await page.locator('#workspace').waitFor({state:'visible'});
    await page.waitForFunction(()=>!document.querySelector('#task-submit').disabled);
    return {context,page};
  }
  const first=await session('n5-test-only-'),page=first.page;
  const field=name=>page.locator(`[data-action-field="${name}"]`);
  assert.equal(audit().native_calls.length,0);
  await page.locator('#task-options summary').click();await page.locator('#scope').selectOption('action');
  await page.locator('#action-mode').selectOption('exact');
  assert.equal(await page.locator('#action-fields details').evaluate(n=>n.open),true);await field('kind').selectOption('ha');
  await page.waitForFunction(()=>!document.querySelector('[data-action-field="device"]').disabled);
  await field('device').selectOption('light.fixture');await field('desired').selectOption('brightness');await field('brightness').fill('43');
  const objective='Stelle die ausgewählte Fixturelampe auf genau 43 Prozent.';
  await page.locator('#objective').fill(objective);
  const responsePromise=page.waitForResponse(response=>new URL(response.url()).pathname==='/v1/agent/tasks'&&response.request().method()==='POST');
  await page.locator('#task-submit').click();const response=await responsePromise;
  const accepted=await response.json();assert.equal(response.status(),201);
  await page.locator('#conversation').waitFor({state:'visible'});await first.context.close();
  assert.equal(audit().native_mutations,0);
  results.push('actual browser chose current native device and exact brightness; canonical task accepted before browser closed');
  fs.writeFileSync(path.join(out,'fixture-control.json'),JSON.stringify({execute:true}));
  const until=Date.now()+20000;while(!audit().execution_checked&&Date.now()<until)await new Promise(r=>setTimeout(r,50));
  const completed=audit();assert(completed.execution_checked,JSON.stringify(completed));
  assert.equal(completed.runs[0].run_id,accepted.run_id);assert.equal(completed.runs[0].state,'SUCCEEDED');
  assert.equal(completed.native_mutations,1);
  const receipt=completed.runs[0].receipts[0];assert.equal(receipt.status,'completed');assert(receipt.native.observed.confirmed);
  assert.equal(completed.fixture_state.state,'on');assert.equal(completed.fixture_state.attributes.brightness,110);
  assert.deepEqual(completed.native_writes,[['POST','http://127.0.0.1:8123/api/services/light/turn_on',{entity_id:'light.fixture',brightness_pct:43}]]);
  assert.equal(completed.provider_calls.length,1);assert.equal(completed.provider_calls[0].kind,'assessment');assert.equal(completed.provider_calls[0].claimed,1);
  results.push('detached real Core performed one synthetic native REST service effect plus readback and local model-protocol assessment; restart and another tick did not repeat it');
  const fresh=await session('n8-action-relogin-');
  await fresh.page.getByRole('button',{name:'Aufträge',exact:true}).click();
  await fresh.page.getByRole('button').filter({hasText:objective}).first().click();
  await fresh.page.locator('#conversation').waitFor({state:'visible'});
  await fresh.page.locator('#result > .state.success').waitFor({state:'visible'});
  assert.equal((await fresh.page.locator('#result > .state.success').innerText()).toLowerCase(),'fertig');
  const visible=await fresh.page.locator('#conversation').innerText();
  const result=await fresh.page.locator('#result > .result-copy').innerText();
  assert(result.includes('light.fixture'));assert(result.includes('43')||result.includes('brightness'));
  assert(await fresh.page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  await fresh.page.screenshot({path:path.join(out,'ha-completed-fresh-390.png'),fullPage:true});
  assert.equal(audit().native_mutations,1);assert.equal(micCalls,0);assert.deepEqual(outside,[]);assert.deepEqual(faults,[]);
  results.push('fresh authenticated browser reads the actual completed result; no second effect, microphone, external request or JavaScript error');
  fs.writeFileSync(path.join(out,'ha-execution-browser-check.json'),JSON.stringify({results,accepted,visible,audit:completed,
    microphone_calls:micCalls,external_requests:outside,js_errors:faults},null,2));console.log(results);
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{await browser?.close();});
