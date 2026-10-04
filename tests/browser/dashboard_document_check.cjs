/* N7 RTF attachment regression in the actual private dashboard.
 * Usage matches dashboard_check.cjs: a fresh dashboard_fixture.py output folder.
 * Real temporary HTTPS/Core admission; all task execution remains stopped.
 * Browser faults exercise file-read races and a lost acceptance response.
 */
const {chromium}=require('playwright');
const fs=require('fs'),path=require('path'),os=require('os'),assert=require('node:assert/strict');
const out=path.resolve(process.argv[2]);
// The fixture reads this from S.state_dir() after constructing its real Core.
// A loopback URL alone says nothing about the server's artifact directory.
const stateRoot=fs.realpathSync(fs.readFileSync(path.join(out,'fixture-state-root.txt'),'utf8').trim());
assert.ok(stateRoot.startsWith(fs.realpathSync(os.tmpdir())+path.sep));
assert.ok(path.basename(stateRoot).startsWith('solvio-dashboard-state-'));
const url=fs.readFileSync(path.join(out,'dashboard-url.txt'),'utf8').trim();
const origin=new URL(url).origin;
if(new URL(url).hostname!=='127.0.0.1')throw Error('Only the isolated loopback fixture is allowed');
let browser;
(async()=>{
  browser=await chromium.launch({headless:true,...(process.env.BROWSER_EXECUTABLE?{executablePath:process.env.BROWSER_EXECUTABLE}:{})});
  const context=await browser.newContext({viewport:{width:1100,height:950},ignoreHTTPSErrors:true});
  await context.route('**/*',route=>new URL(route.request().url()).origin===origin?route.continue():route.abort());
  const page=await context.newPage(),faults=[],posted=[],accepted=[];
  page.setDefaultTimeout(10000);page.on('pageerror',e=>faults.push(e.message));
  await page.goto(url);
  await page.getByLabel('Anmeldecode',{exact:true}).fill('n5-test-only-'.padEnd(43,'0'));
  await page.getByRole('button',{name:'Verbinden',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('#connection').textContent==='Mit deinem Core verbunden');
  const input=page.locator('#document-file'),start=page.locator('#task-submit'),clear=page.locator('#document-clear');
  const small=Buffer.concat([Buffer.from('{\\rtf1\\ansi '),Buffer.from([0xe4,0x80,0xff]),Buffer.from(' \\u223?}')]);
  async function composer(){
    await page.getByRole('button',{name:'SOLVIO',exact:true}).click();
    if(!await page.locator('#conversation-entry').isVisible())await page.locator('#new-task').click();
    await page.locator('#objective').waitFor({state:'visible'});
    for(const id of ['task-options','document-options']){
      const detail=page.locator('#'+id);if(!await detail.evaluate(n=>n.open))await detail.locator('summary').click();
    }
  }
  const upload=async(bytes=small,name='privater-lokaler-dateiname.rtf')=>{await composer();await input.setInputFiles({name,mimeType:'application/rtf',buffer:bytes});};
  const ready=()=>page.waitForFunction(()=>document.querySelector('#document-status').textContent.startsWith('RTF-Dokument angehängt'));
  async function until(check){const end=Date.now()+10000;while(!check()){if(Date.now()>end)throw Error('No browser observation');await new Promise(r=>setTimeout(r,20));}}
  let mode='reject',lost=false;
  await page.route('**/v1/agent/tasks',async route=>{
    if(route.request().method()!=='POST')return route.continue();
    posted.push(route.request().postDataJSON());
    if(mode==='reject')return route.fulfill({status:400,json:{error:'invalid_task'}});
    const response=await route.fetch(),body=await response.json();
    assert.ok(response.ok(),JSON.stringify(body));accepted.push(body);
    if(!lost){lost=true;return route.abort();}
    return route.fulfill({response});
  });
  async function rejectedSubmission(){
    await composer();
    const before=posted.length;await page.locator('#objective').fill('Isolierter Dokumenteingang');await start.click();
    await until(()=>posted.length===before+1);await page.waitForFunction(()=>!document.querySelector('#task-submit').disabled);
    return posted.at(-1).task;
  }

  // 1. A change to code work removes the document; returning does not revive it.
  await upload();await ready();await page.locator('#scope').selectOption('build');
  assert.equal(await page.locator('#document-field').isVisible(),false);
  assert.equal(await input.inputValue(),'');assert.equal(await input.isDisabled(),true);
  await page.locator('#scope').selectOption('research');
  assert.equal(await clear.isVisible(),false);
  assert.equal('document_request' in await rejectedSubmission(),false);

  // 2. Oversize replaces a valid selection with an explicit blocked draft.
  await upload();await ready();await upload(Buffer.alloc(65537,65),'zu-gross.rtf');
  await page.locator('#document-error').getByText('Das Dokument ist zu groß.',{exact:false}).waitFor();
  assert.equal(await start.isDisabled(),true);
  const beforeOversize=posted.length;
  await page.locator('#task-form').dispatchEvent('submit');await page.waitForTimeout(80);
  assert.equal(posted.length,beforeOversize);
  await clear.click();assert.equal(await start.isDisabled(),false);

  // 3. Clear, and scope changes during an actual asynchronous File read,
  // invalidate that read. Invalid headers never silently become empty tasks.
  await upload(Buffer.from('plain text'),'kein-rtf.rtf');
  await page.locator('#document-error').getByText('Bitte wähle ein RTF-Dokument.',{exact:false}).waitFor();
  assert.equal(await start.isDisabled(),true);await clear.click();
  await page.evaluate(()=>{
    const original=File.prototype.arrayBuffer;
    File.prototype.arrayBuffer=function(){
      const bytes=original.call(this);
      return this.name==='langsam.rtf'?new Promise(resolve=>{window.releaseDocumentRead=()=>resolve(bytes);}):bytes;
    };
  });
  for(const action of ['clear','scope']){
    await upload(small,'langsam.rtf');
    await page.waitForFunction(()=>document.querySelector('#document-status').textContent.includes('lokal geprüft'));
    assert.equal(await start.isDisabled(),true);
    if(action==='clear')await clear.click();
    else{await page.locator('#scope').selectOption('build');await page.locator('#scope').selectOption('research');}
    await page.evaluate(()=>window.releaseDocumentRead());await page.waitForTimeout(80);
    assert.equal(await input.inputValue(),'');assert.equal(await page.locator('#document-status').textContent(),'');
    assert.equal('document_request' in await rejectedSubmission(),false);
  }

  // 4. The full 64 KiB boundary reaches the real public Core. Raw high bytes
  // round-trip exactly; the local filename is absent. Losing the successful
  // response and retrying cannot change bytes, request id, or admitted task.
  const maximum=Buffer.alloc(65536,0xe4);small.copy(maximum);maximum[65535]=125;
  await upload(maximum);await ready();mode='accept';
  const before=posted.length;await page.locator('#objective').fill('Lies dieses RTF-Dokument offline.');await start.click();
  await page.locator('#task-retry').waitFor();
  assert.equal(await input.isDisabled(),true);assert.equal(await clear.isDisabled(),true);
  assert.equal(await page.locator('#scope').isDisabled(),true);
  const task=posted[before].task;
  assert.deepEqual(Object.keys(task).sort(),['client_request_id','document_request','objective','scope','target_repo']);
  assert.deepEqual(Object.keys(task.document_request).sort(),['content_b64','format','operation']);
  assert.equal(task.scope,'research');assert.equal(task.document_request.operation,'extract_text');
  assert.equal(task.document_request.format,'rtf');assert.deepEqual(Buffer.from(task.document_request.content_b64,'base64'),maximum);
  assert.equal(JSON.stringify(task).includes('privater-lokaler-dateiname'),false);
  await page.locator('#task-retry').click();await until(()=>accepted.length===2);
  await page.locator('#conversation').waitFor({state:'visible'});
  await page.locator('#conversation-result #result[data-run-id] > .state').waitFor();
  await page.waitForFunction(()=>!document.querySelector('#task-submit').disabled);
  assert.deepEqual(posted[before],posted[before+1]);
  assert.equal(accepted[0].task_id,accepted[1].task_id);assert.equal(accepted[0].run_id,accepted[1].run_id);
  assert.match(accepted[0].run_id,/^ar-[a-f0-9]{16}$/);
  assert.deepEqual(fs.readFileSync(path.join(stateRoot,'agent_runs',accepted[0].run_id,'input-document.rtf')),maximum);
  assert.equal(await input.inputValue(),'');assert.equal(await clear.isVisible(),false);
  assert.equal(await page.locator('#document-status').textContent(),'');
  mode='reject';const next=await rejectedSubmission();
  assert.equal('document_request' in next,false);assert.notEqual(next.client_request_id,task.client_request_id);

  // 5. The result link accepts only the Core's relative URL for that exact
  // run/artifact pair. This UI-only descriptor fixture does not claim an
  // execution; public Python tests verify actual result authorization/content.
  const runId=accepted[0].run_id,artifactId='aa-3333333333333333';
  const downloadUrl=`/v1/agent/runs/${runId}/artifacts/${artifactId}/download`;
  await page.route(`**/v1/agent/runs/${runId}`,async route=>{
    const response=await route.fetch(),body=await response.json();
    delete body.dateien; // Explicit legacy response: current file lists are authoritative.
    const artifact={art:'document_result',id:artifactId,pfad:'UI-only synthetic result',download_url:downloadUrl};
    await route.fulfill({response,json:{...body,artefakte:[...(body.artefakte||[]),artifact,
      {...artifact,download_url:'https://example.invalid/document.txt'},
      {...artifact,download_url:downloadUrl.replace(runId,'ar-0000000000000000')},
      {...artifact,download_url:'javascript:alert(1)'},
      {...artifact,art:'task_input'}]}});
  });
  await page.getByRole('button',{name:'Aufträge',exact:true}).click();
  await page.locator('#task-list').getByText('Lies dieses RTF-Dokument offline.',{exact:true}).click();
  const link=page.getByRole('link',{name:'Dokumenttext herunterladen',exact:true});await link.waitFor();
  assert.equal(await page.locator('.artifact-entry a').count(),1);assert.equal(await link.getAttribute('href'),downloadUrl);
  assert.equal(await link.getAttribute('download'),'solvio-dokument.txt');
  await upload();await ready();await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  await page.screenshot({path:path.join(out,'dashboard-document-mobile.png'),fullPage:true});
  await page.getByRole('button',{name:'Abmelden',exact:true}).click();
  await page.locator('#login-panel').waitFor({state:'visible'});
  assert.equal(await input.inputValue(),'');assert.equal(await page.locator('#document-status').textContent(),'');
  assert.deepEqual(faults,[]);
  const report={passed:['scope_change_clears_attachment','oversize_blocks_submission','clear_and_late_file_reads','raw_bytes_and_same_request_real_https_replay','bound_result_link_ui_only_descriptor'],javascript_errors:faults};
  fs.writeFileSync(path.join(out,'document-browser-result.json'),JSON.stringify(report,null,2)+'\n');
  console.log(JSON.stringify(report));await browser.close();
})().catch(async e=>{console.error(e.stack);if(browser)await browser.close();process.exitCode=1;});
