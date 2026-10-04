/* Isolated browser regression. The Python fixture uses real temporary HTTPS
 * Core stores. Fault injection below affects this browser only. No providers.
 * NODE_PATH may supply Playwright; BROWSER_EXECUTABLE selects installed Chrome.
 * Usage: node tests/browser/dashboard_check.cjs <fixture-output-directory>
 */
const {chromium}=require('playwright');
const fs=require('fs'), path=require('path'), assert=require('node:assert/strict');
const out=path.resolve(process.argv[2]);
const url=fs.readFileSync(path.join(out,'dashboard-url.txt'),'utf8').trim();
const origin=new URL(url).origin;
if(new URL(url).hostname!=='127.0.0.1')throw Error('Only the isolated loopback fixture is allowed');
let browser;
(async()=>{
  browser=await chromium.launch({headless:true,...(process.env.BROWSER_EXECUTABLE?{executablePath:process.env.BROWSER_EXECUTABLE}:{})});
  const context=await browser.newContext({viewport:{width:1536,height:1100},ignoreHTTPSErrors:true});
  await context.route('**/*',route=>new URL(route.request().url()).origin===origin?route.continue():route.abort());
  const page=await context.newPage(), faults=[];page.setDefaultTimeout(10000);page.on('pageerror',e=>faults.push(e.message));
  await page.goto(url);
  await page.getByLabel('Anmeldecode',{exact:true}).fill('n5-test-only-'.padEnd(43,'0'));
  await page.getByLabel('Anmeldecode',{exact:true}).press('Tab');
  assert.equal(await page.getByRole('button',{name:'Verbinden',exact:true}).evaluate(n=>n===document.activeElement),true);
  await page.keyboard.press('Enter');
  await page.locator('#workspace').waitFor({state:'visible'});
  await page.getByRole('button',{name:'Aufträge',exact:true}).click();
  await page.locator('#task-list').getByText('Testauftrag: Drei Reiseoptionen für Hamburg vergleichen.',{exact:true}).click();
  await page.locator('#conversation-result #result[data-run-id] > .state').waitFor();
  // A selected canonical task appears inline on the conversation start page.
  await page.locator('#conversation').waitFor({state:'visible'});
  await page.screenshot({path:path.join(out,'dashboard-desktop.png'),fullPage:true});
  // Real native sources carry a description before the URL. Exercise the
  // existing GET -> selectRun -> renderDetail path, not a copied URL helper.
  const sourceRows=[
    'https://example.test/plain',
    'Offizielle Quelle – Beschreibung: https://example.test/information',
    'Native Websuche: https://example.test/tourismus/hamburg-erkunden/spaziergaenge/spaziergang-hafencity-speicherstadt-offizielle-route-mit-oeffnungszeiten-und-anfahrt',
    'javascript:alert(1)', 'data:text/html,<svg onload=alert(1)>',
    'https://user:pass@example.test/private',
    'Quelle: https://user:pass@example.test/private',
    'Quelle: http://example.test/insecure',
    '<img src=x onerror=alert(1)>', '<a href="https://example.test/html">Quelle</a>',
    'https://one.example.test https://two.example.test',
    'http://one.example.test und https://two.example.test',
    'Quelle: https://example.test/one oder https://example.test/two',
    'Quelle: https://example.test/one mit weiterem Text',
  ];
  await page.route('**/v1/agent/runs/*',async route=>{
    assert.equal(route.request().method(),'GET');
    const response=await route.fetch(),data=await response.json();
    await route.fulfill({response,json:{...data,quellen:sourceRows}});
  });
  await page.getByRole('button',{name:'Aufträge',exact:true}).click();
  await page.locator('#task-list .task-card').first().click();
  const supporting=page.locator('#result > details[data-section="details"]');
  await supporting.locator(':scope > summary').click();
  const sourceSection=supporting.locator('details[data-section="sources"]');
  await sourceSection.locator(':scope > summary').click();
  await sourceSection.locator('li').last().waitFor();
  assert.deepEqual(await sourceSection.locator('li').allTextContents(),sourceRows);
  const renderedLinks=await sourceSection.locator('a').evaluateAll(nodes=>nodes.map(a=>({href:a.href,text:a.textContent,target:a.target,rel:a.rel})));
  assert.deepEqual(renderedLinks,sourceRows.slice(0,3).map(text=>({href:text.split(' ').at(-1),text:text.split(' ').at(-1),target:'_blank',rel:'noopener noreferrer'})));
  assert.equal(await sourceSection.locator('img,svg,script,[onerror],[onload]').count(),0);
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  assert.deepEqual(await sourceSection.locator('li').allTextContents(),sourceRows);
  await page.setViewportSize({width:1536,height:1100});
  await page.unroute('**/v1/agent/runs/*');
  await page.getByRole('button',{name:'Aufträge',exact:true}).click();
  await page.locator('#task-list .task-card').first().click();
  await page.getByRole('button',{name:'Wissen',exact:true}).click();
  await page.locator('.memory-card').first().waitFor();
  await page.getByText('Warum weiß SOLVIO das?',{exact:true}).first().click();
  await page.waitForFunction(()=>document.querySelector('.memory-card details').textContent.includes('app:fixture'));
  await page.getByText('Persönliches Lernen',{exact:true}).click();
  await page.screenshot({path:path.join(out,'dashboard-knowledge.png'),fullPage:true});
  await page.getByRole('button',{name:'Lernen fortsetzen',exact:true}).click();
  await page.getByRole('button',{name:'Bestätigen',exact:true}).click();
  await page.locator('#decision-dialog').waitFor({state:'hidden'});
  await page.evaluate(()=>window.dispatchEvent(new Event('online')));
  await page.getByText('Keine offenen Lernvorgänge im gelesenen Stand.',{exact:true}).waitFor();
  // Execute a real temporary correction, then lose the HTTP response. The
  // retry must read the same journal success, not create a second correction.
  const memoryPosts=[];
  await page.route('**/v1/dashboard/memory-commands',async route=>{
    if(route.request().method()!=='POST')return route.continue();
    memoryPosts.push(route.request().postDataJSON());
    if(memoryPosts.length===1){await route.fetch();return route.abort();}
    return route.continue();
  });
  await page.locator('#memory-list .memory-card').first().getByRole('button',{name:'Korrigieren',exact:true}).click();
  await page.getByLabel('Die richtige Aussage').fill('Testprofil: Ich bevorzuge ein ruhiges Zimmer im Innenhof.');
  await page.getByRole('button',{name:'Änderung ausführen',exact:true}).click();
  await page.getByRole('button',{name:'Dieselbe Änderung erneut übermitteln',exact:true}).click();
  await page.locator('#decision-dialog').waitFor({state:'hidden'});
  await page.getByText('Testprofil: Ich bevorzuge ein ruhiges Zimmer im Innenhof.',{exact:true}).first().waitFor();
  assert.deepEqual(memoryPosts[0],memoryPosts[1]);
  assert.equal(await page.locator('#memory-list .memory-card').count(),3);
  await page.locator('#candidate-list').getByRole('button',{name:'Stimmt',exact:true}).click();
  await page.getByRole('button',{name:'Änderung ausführen',exact:true}).click();
  await page.locator('#decision-dialog').waitFor({state:'hidden'});
  await page.waitForFunction(()=>document.querySelectorAll('#memory-list .memory-card').length===4);
  await page.locator('#memory-list .memory-card').filter({hasText:'Testprofil: Ich bevorzuge ein ruhiges Zimmer im Innenhof.'}).getByRole('button',{name:'Vergessen',exact:true}).click();
  await page.getByRole('button',{name:'Änderung ausführen',exact:true}).click();
  await page.locator('#decision-dialog').waitFor({state:'hidden'});
  await page.waitForFunction(()=>document.querySelectorAll('#memory-list .memory-card').length===3);
  await page.unroute('**/v1/dashboard/memory-commands');
  await page.getByRole('button',{name:'SOLVIO',exact:true}).click();
  await page.locator('#new-task').click();await page.locator('#objective').waitFor({state:'visible'});
  await page.setViewportSize({width:390,height:844});
  await page.screenshot({path:path.join(out,'dashboard-mobile.png'),fullPage:true});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  await page.setViewportSize({width:1536,height:1100});

  // Polling preserves the chosen repository; removed repositories become invalid.
  let repositories=[{path:'/test/a',name:'Projekt A'},{path:'/test/b',name:'Projekt B'}], reads=0;
  await page.route('**/v1/dashboard/state',async route=>{
    const response=await route.fetch(), data=await response.json();
    await route.fulfill({response,json:{...data,repositories}}); reads++;
  });
  async function until(check){const deadline=Date.now()+10000;while(!check()){if(Date.now()>deadline)throw Error('Browser observation timed out');await new Promise(r=>setTimeout(r,20));}}
  async function refresh(){const before=reads;await page.evaluate(()=>window.dispatchEvent(new Event('online')));await page.waitForFunction(()=>document.querySelector('#connection').textContent==='Mit deinem Core verbunden');await until(()=>reads!==before);await page.waitForTimeout(80);}
  await page.locator('#task-options > summary').click();
  await refresh();await page.locator('#scope').selectOption('build');await page.locator('#repository').selectOption('/test/b');
  await page.locator('#objective').fill('Ein neuer Entwurf');await refresh();
  assert.equal(await page.locator('#repository').inputValue(),'/test/b');
  repositories=[repositories[0]];await refresh();assert.equal(await page.locator('#repository').inputValue(),'');
  await refresh();assert.equal(await page.locator('#repository').inputValue(),'');
  await page.locator('#scope').selectOption('research');

  // A temporary network failure displays uncertainty, then a fresh read recovers.
  await page.route('**/v1/dashboard/state',route=>route.abort());
  await page.evaluate(()=>window.dispatchEvent(new Event('online')));
  await page.locator('#offline').waitFor({state:'visible'});
  assert.equal(await page.getByRole('button',{name:'Auftrag senden',exact:false}).isDisabled(),true);
  await page.unroute('**/v1/dashboard/state');
  await page.getByRole('button',{name:'Neu verbinden'}).click();
  await page.locator('#offline').waitFor({state:'hidden'});

  // Missing response retries the same bound body. This UI fault route does not
  // execute a fake second task; public Python tests prove backend idempotency.
  const posted=[];let held;
  await page.route('**/v1/agent/tasks',async route=>{
    if(route.request().method()!=='POST')return route.continue();
    posted.push(route.request().postDataJSON());
    if(posted.length===1)return route.abort();
    held=route;
  });
  await page.locator('#objective').fill('Test: gleiche Kennung bei ungewisser Antwort');
  await page.getByRole('button',{name:'Auftrag senden',exact:false}).click();
  await page.getByRole('button',{name:'Denselben Auftrag erneut übermitteln'}).click();
  await until(()=>!!held);
  assert.deepEqual(posted[0],posted[1]);

  // A session loss closes and clears private dialogs. Then an old accepted
  // response must not erase the new draft or select an old run.
  await page.getByRole('button',{name:'Aufträge',exact:true}).click();
  await page.locator('#task-list').getByText('Testauftrag: Eine übersichtliche Wochenplanung vorbereiten.',{exact:true}).click();
  await page.getByRole('button',{name:'Auftrag abbrechen',exact:true}).click();
  await page.locator('#decision-dialog').waitFor({state:'visible'});
  const realSession=await page.evaluate(async()=>await(await fetch('/v1/browser/session')).json());
  await page.route('**/v1/dashboard/state',route=>route.fulfill({status:401,json:{error:'unauthorized'}}));
  await page.evaluate(()=>window.dispatchEvent(new Event('online')));
  await page.locator('#login-panel').waitFor({state:'visible'});
  assert.equal(await page.locator('#decision-dialog').evaluate(n=>n.open),false);
  assert.equal(await page.locator('#decision-content').textContent(),'');
  await page.unroute('**/v1/dashboard/state');
  // The actual fixture session remains valid; only the prior read was faulted.
  await page.route('**/v1/browser/session/login',route=>route.fulfill({json:realSession}));
  await page.getByLabel('Anmeldecode',{exact:true}).fill('synthetic-login-response');
  await page.getByRole('button',{name:'Verbinden',exact:true}).click();
  await page.locator('#workspace').waitFor({state:'visible'});
  await page.getByRole('button',{name:'SOLVIO',exact:true}).click();
  await page.locator('#objective').fill('Dieser neue Entwurf muss erhalten bleiben');
  await held.fulfill({status:201,json:{run_id:'obsolete-run',annahme:'accepted'}});
  await page.waitForTimeout(120);
  assert.equal(await page.locator('#objective').inputValue(),'Dieser neue Entwurf muss erhalten bleiben');
  assert.equal(await page.locator('#result').textContent(),'');
  await page.getByRole('button',{name:'Einstellungen',exact:true}).click();
  await page.locator('#threshold').fill('12');
  await page.route('**/v1/agent/cost-policy',route=>route.request().method()==='PUT'?route.fulfill({status:401,json:{error:'unauthorized'}}):route.continue());
  await page.getByRole('button',{name:'Grenze speichern',exact:true}).click();
  await page.locator('#login-panel').waitFor({state:'visible'});
  await page.getByLabel('Anmeldecode',{exact:true}).fill('synthetic-login-response');
  await page.getByRole('button',{name:'Verbinden',exact:true}).click();
  await page.locator('#workspace').waitFor({state:'visible'});
  assert.equal(await page.locator('#threshold').isDisabled(),false);
  assert.equal(await page.getByRole('button',{name:'Grenze speichern',exact:true}).isDisabled(),false);
  assert.deepEqual(faults,[]);
  const report={passed:['same_task_learning_resume','real_https_login_keyboard','canonical_result_and_memory','labeled_https_sources_safe_unambiguous_links','real_correction_lost_response_same_journal_replay','candidate_confirmation_and_forget','desktop_and_mobile_no_overflow','repository_poll_preserves_or_invalidates_twice','offline_and_reconnect','same_request_retry','session_loss_clears_dialog','old_response_preserves_new_draft','cost_form_usable_after_reauthentication'],javascript_errors:faults,webmcp:await page.evaluate(()=>!!document.modelContext?.registerTool)};
  fs.writeFileSync(path.join(out,'browser-result.json'),JSON.stringify(report,null,2)+'\n');
  console.log(JSON.stringify(report));
  await browser.close();
})().catch(async e=>{console.error(e.stack);if(browser)await browser.close();process.exitCode=1;});
