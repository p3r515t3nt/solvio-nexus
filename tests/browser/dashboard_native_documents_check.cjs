/* Additive TXT/DOCX/ODT upload admission through real browser and HTTPS Core.
 * dashboard_account_fixture.py creates genuine native fixtures but never ticks
 * these document tasks. Conversion is deliberately not claimed by this test.
 */
const {chromium}=require('playwright');
const fs=require('fs'),path=require('path'),os=require('os'),crypto=require('crypto'),assert=require('node:assert/strict');
const out=path.resolve(process.argv[2]),read=name=>fs.readFileSync(path.join(out,name),'utf8');
const stateRoot=fs.realpathSync(read('fixture-state-root.txt').trim());
assert.ok(stateRoot.startsWith(fs.realpathSync(os.tmpdir())+path.sep));
const documents=JSON.parse(read('native-documents.json'));
const url=read('dashboard-url.txt').trim(),origin=new URL(url).origin;
assert.equal(new URL(url).hostname,'127.0.0.1');
let browser;
async function until(check){const end=Date.now()+10000;while(!check()){if(Date.now()>end)throw Error('Missing fixture observation');await new Promise(r=>setTimeout(r,30));}}
(async()=>{
  browser=await chromium.launch({headless:true,...(process.env.BROWSER_EXECUTABLE?{executablePath:process.env.BROWSER_EXECUTABLE}:{})});
  const context=await browser.newContext({viewport:{width:1440,height:1000},ignoreHTTPSErrors:true});
  await context.route('**/*',route=>new URL(route.request().url()).origin===origin?route.continue():route.abort());
  await context.addInitScript(()=>{
    window.__micRequests=0;
    if(navigator.mediaDevices)navigator.mediaDevices.getUserMedia=async()=>{window.__micRequests++;throw Error('Mic forbidden');};
  });
  const page=await context.newPage(),faults=[],posted=[],responses=[],accepted=[];
  page.setDefaultTimeout(10000);page.on('pageerror',e=>faults.push(e.message));
  await page.goto(url);
  await page.getByLabel('Anmeldecode',{exact:true}).fill('n5-test-only-'.padEnd(43,'0'));
  await page.getByRole('button',{name:'Verbinden',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('#connection').textContent==='Mit deinem Core verbunden');
  const input=page.locator('#document-file'),start=page.locator('#task-submit'),clear=page.locator('#document-clear');
  async function composer(){
    await page.getByRole('button',{name:'SOLVIO',exact:true}).click();
    if(!await page.locator('#conversation-entry').isVisible())await page.locator('#new-task').click();
    await page.locator('#objective').waitFor({state:'visible'});
    for(const id of ['task-options','document-options']){
      const detail=page.locator('#'+id);if(!await detail.evaluate(n=>n.open))await detail.locator('summary').click();
    }
    if(await page.locator('#scope').inputValue()!=='research')await page.locator('#scope').selectOption('research');
  }
  async function upload(name,buffer){
    await composer();await input.setInputFiles({name,mimeType:'application/octet-stream',buffer});
  }
  let loseDocx=true;
  await page.route('**/v1/agent/tasks',async route=>{
    if(route.request().method()!=='POST')return route.continue();
    const body=route.request().postDataJSON();posted.push(body);
    const response=await route.fetch(),result=await response.json();
    responses.push({status:response.status(),result});
    if(response.ok())accepted.push(result);
    if(response.ok()&&body.task.document_request?.format==='docx'&&loseDocx){loseDocx=false;return route.abort();}
    return route.fulfill({response});
  });
  // Text's strict UTF-8 and per-format size fail locally without a new task.
  for(const [name,bytes] of [['ungültig.txt',Buffer.from([0xc3,0x28])],
    ['leer.txt',Buffer.from(' \n\t')],['zu-gross.txt',Buffer.alloc(65537,65)],
    ['zu-gross.docx',Buffer.alloc(1048577,65)],['zu-gross.odt',Buffer.alloc(1048577,65)]]){
    await upload(name,bytes);
    await page.waitForFunction(()=>!!document.querySelector('#document-error').textContent);
    assert.equal(await start.isDisabled(),true);
    await page.locator('#task-form').dispatchEvent('submit');await page.waitForTimeout(30);
    assert.equal(posted.length,0);await clear.click();
  }
  // The browser never treats an arbitrary ZIP extension as proven validity.
  // This malformed container reaches the real Core, which rejects it before admission.
  await upload('kaputt.docx',Buffer.from('PK\x03\x04not-an-office-container'));
  await page.waitForFunction(()=>document.querySelector('#document-status').textContent.startsWith('DOCX-Dokument angehängt'));
  await page.locator('#objective').fill('Isolierte kaputte Dokumentprobe');await start.click();
  await until(()=>responses.length===1);assert.equal(responses[0].status,400);
  await page.waitForFunction(()=>!document.querySelector('#task-submit').disabled);
  assert.equal(accepted.length,0);assert.equal(JSON.parse(read('account-observed.json')).run_count,1);
  await clear.click();
  const admissions=[];
  for(const doc of documents){
    const bytes=fs.readFileSync(path.join(out,doc.name));
    assert.equal(bytes.length,doc.bytes);
    assert.equal(crypto.createHash('sha256').update(bytes).digest('hex'),doc.sha256);
    await upload(doc.name,bytes);
    await page.waitForFunction(fmt=>document.querySelector('#document-status').textContent.startsWith(fmt.toUpperCase()+'-Dokument angehängt'),doc.format);
    if(doc.format==='docx'){
      for(const width of [1440,390]){
        await page.setViewportSize({width,height:width===390?844:1000});
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
        await page.screenshot({path:path.join(out,`dashboard-native-document-${width}.png`),fullPage:true});
      }
    }
    const before=posted.length,acceptedBefore=accepted.length;
    await page.locator('#objective').fill('Lies die synthetische '+doc.format.toUpperCase()+'-Datei offline.');
    await start.click();
    if(doc.format==='docx'){
      await page.locator('#task-retry').waitFor();
      assert.equal(await input.isDisabled(),true);assert.equal(await clear.isDisabled(),true);
      await page.locator('#task-retry').click();
      await until(()=>accepted.length===acceptedBefore+2);
      assert.deepEqual(posted[before],posted[before+1]);
      assert.equal(accepted[acceptedBefore].run_id,accepted[acceptedBefore+1].run_id);
      assert.equal(accepted[acceptedBefore].task_id,accepted[acceptedBefore+1].task_id);
    }else await until(()=>accepted.length===acceptedBefore+1);
    await page.locator('#conversation').waitFor({state:'visible'});
    await page.waitForFunction(()=>!document.querySelector('#task-submit').disabled);
    const request=posted[before].task;
    assert.deepEqual(Object.keys(request.document_request).sort(),['content_b64','format','operation']);
    assert.equal(request.document_request.format,doc.format);assert.equal(request.document_request.operation,'extract_text');
    assert.equal(JSON.stringify(request).includes(doc.name),false);
    assert.deepEqual(Buffer.from(request.document_request.content_b64,'base64'),bytes);
    const admitted=accepted[acceptedBefore],file=path.join(stateRoot,'agent_runs',admitted.run_id,'input-document.'+doc.format);
    assert.deepEqual(fs.readFileSync(file),bytes);
    const response=await page.request.get(origin+'/v1/agent/runs/'+admitted.run_id);assert.equal(response.status(),200);
    const view=await response.json();
    assert.equal(view.zustand_code,'CREATED');
    assert.deepEqual(view.schritte,[]);assert.equal(view.ergebnis,'');
    assert.deepEqual(view.dateien,[]);
    admissions.push({format:doc.format,bytes:bytes.length,sha256:doc.sha256,run_id:admitted.run_id});
    assert.equal(await input.inputValue(),'');assert.equal(await clear.isVisible(),false);
  }
  await until(()=>JSON.parse(read('account-observed.json')).run_count===4);
  const observed=JSON.parse(read('account-observed.json'));
  assert.equal(observed.native_transport_calls,1);assert.equal(observed.native_writes,0);
  assert.equal(observed.model_calls,0);assert.equal(observed.ticks,3);
  const mic=await page.evaluate(()=>window.__micRequests);
  assert.equal(mic,0);assert.deepEqual(faults,[]);
  const report={passed:['strict_local_text_and_format_limits','malformed_office_rejected_by_real_core',
    'native_txt_docx_odt_original_bytes_public_admission','docx_lost_acceptance_same_request_one_task',
    'no_document_execution_or_result_claim','responsive_390_1440'],admissions,post_requests:posted.length,
    http_statuses:responses.map(r=>r.status),observed,microphone_calls:mic,javascript_errors:faults};
  fs.writeFileSync(path.join(out,'native-document-browser-result.json'),JSON.stringify(report,null,2)+'\n');
  console.log(JSON.stringify(report));await browser.close();
})().catch(async e=>{console.error(e.stack);if(browser)await browser.close();process.exitCode=1;});
