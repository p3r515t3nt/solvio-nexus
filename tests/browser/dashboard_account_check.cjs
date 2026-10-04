/* Real isolated HTTPS/Core account amendment; no provider writes or model calls.
 * Start dashboard_account_fixture.py first. The fixture stops ticking before UI.
 */
const {chromium}=require('playwright');
const fs=require('fs'),path=require('path'),os=require('os'),assert=require('node:assert/strict');
const out=path.resolve(process.argv[2]);
const stateRoot=fs.realpathSync(fs.readFileSync(path.join(out,'fixture-state-root.txt'),'utf8').trim());
assert.ok(stateRoot.startsWith(fs.realpathSync(os.tmpdir())+path.sep));
const baseline=JSON.parse(fs.readFileSync(path.join(out,'account-fixture.json'),'utf8'));
const url=fs.readFileSync(path.join(out,'dashboard-url.txt'),'utf8').trim(),origin=new URL(url).origin;
assert.equal(new URL(url).hostname,'127.0.0.1');
const readObservation=()=>JSON.parse(fs.readFileSync(path.join(out,'account-observed.json'),'utf8'));
async function until(check){const end=Date.now()+10000;while(!check()){if(Date.now()>end)throw Error('Missing fixture observation');await new Promise(r=>setTimeout(r,30));}}
let browser;
(async()=>{
  assert.equal(baseline.state,'WAITING_USER');
  browser=await chromium.launch({headless:true,...(process.env.BROWSER_EXECUTABLE?{executablePath:process.env.BROWSER_EXECUTABLE}:{})});
  const context=await browser.newContext({viewport:{width:1440,height:1000},ignoreHTTPSErrors:true});
  await context.route('**/*',route=>new URL(route.request().url()).origin===origin?route.continue():route.abort());
  await context.addInitScript(()=>{
    window.__micRequests=0;
    if(navigator.mediaDevices)navigator.mediaDevices.getUserMedia=async()=>{window.__micRequests++;throw Error('Mic forbidden in this test');};
  });
  const page=await context.newPage(),faults=[],posted=[],responses=[];
  page.on('pageerror',e=>faults.push(e.message));page.setDefaultTimeout(10000);
  await page.goto(url);
  await page.getByLabel('Anmeldecode',{exact:true}).fill('n5-test-only-'.padEnd(43,'0'));
  await page.getByRole('button',{name:'Verbinden',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('#connection').textContent==='Mit deinem Core verbunden');
  await page.getByRole('button',{name:'Aufträge',exact:true}).click();
  await page.locator('#task-list').getByText(baseline.objective,{exact:true}).click();
  await page.getByRole('button',{name:'Neu verbundenes Konto auswählen',exact:true}).click();
  const dialog=page.locator('#decision-dialog'),select=dialog.getByLabel('Konto für diesen Auftrag',{exact:true});
  const submit=dialog.getByRole('button',{name:'Mit diesem Konto fortsetzen',exact:true});
  let loseFirst=true;
  await page.route(`**/v1/agent/runs/${baseline.run_id}/action-account`,async route=>{
    assert.equal(route.request().method(),'POST');posted.push(route.request().postDataJSON());
    const response=await route.fetch(),body=await response.json();
    responses.push({status:response.status(),body});assert.equal(response.status(),200,JSON.stringify(body));
    if(loseFirst){loseFirst=false;return route.abort();}
    return route.fulfill({response});
  });
  assert.equal(await select.inputValue(),'');
  await submit.click();
  await dialog.getByText('Bitte wähle das passende verbundene Konto.',{exact:true}).waitFor();
  assert.equal(posted.length,0);
  const account=baseline.binding.choices[0].account;
  assert.equal(await select.locator('option').count(),2);
  await select.selectOption(account);
  for(const width of [1440,390]){
    await page.setViewportSize({width,height:width===390?844:1000});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    await page.screenshot({path:path.join(out,`dashboard-account-select-${width}.png`),fullPage:true});
  }
  await submit.click();
  await dialog.getByText('Erneut versuchen übermittelt dieselbe Auswahl.',{exact:false}).waitFor();
  assert.equal(await select.isDisabled(),true);
  await until(()=>readObservation().account_amendments===1);
  assert.equal(readObservation().state,'RUNNING');
  const expected={action_id:baseline.binding.action_id,new_account:account,
    expected_account:baseline.binding.current_account,expected_receipt_digest:baseline.binding.receipt_digest};
  assert.deepEqual({...posted[0],client_request_id:undefined},{...expected,client_request_id:undefined});
  assert.match(posted[0].client_request_id,/^[a-f0-9-]{36}$/);
  // Even synthetic script edits cannot replace the already uncertain request.
  await select.evaluate(n=>{n.value='';n.dispatchEvent(new Event('change',{bubbles:true}));});
  await submit.click();
  await dialog.waitFor({state:'hidden'});
  assert.equal(posted.length,2);assert.deepEqual(posted[0],posted[1]);
  await until(()=>readObservation().account_amendments===1&&readObservation().state==='RUNNING');
  const observed=readObservation();
  assert.equal(observed.original_rows_unchanged,true);assert.equal(observed.original_receipt_unchanged,true);
  assert.equal(observed.native_transport_calls,1);assert.equal(observed.native_writes,0);
  assert.equal(observed.model_calls,0);assert.equal(observed.ticks,3);assert.equal(observed.run_count,1);
  for(const width of [1440,390]){
    await page.setViewportSize({width,height:width===390?844:1000});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    await page.screenshot({path:path.join(out,`dashboard-account-confirmed-${width}.png`),fullPage:true});
  }
  const mic=await page.evaluate(()=>window.__micRequests);
  assert.equal(mic,0);assert.deepEqual(faults,[]);
  const report={passed:['explicit_account_selection','actual_https_amendment_after_native_auth_failure',
    'lost_200_retry_exact_request','one_ledger_amendment_original_authority_unchanged','no_post_selection_execution',
    'responsive_390_1440'],post_requests:posted.length,http_statuses:responses.map(r=>r.status),
    observed,microphone_calls:mic,javascript_errors:faults};
  fs.writeFileSync(path.join(out,'account-browser-result.json'),JSON.stringify(report,null,2)+'\n');
  console.log(JSON.stringify(report));await browser.close();
})().catch(async e=>{console.error(e.stack);if(browser)await browser.close();process.exitCode=1;});
