/* A2: actual Chromium -> packaged dashboard -> authenticated temporary Core.
 * No tick, native action, provider, browser account or microphone is used. */
const {chromium}=require('playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const out=path.resolve(process.argv[2]);
const url=fs.readFileSync(path.join(out,'dashboard-url.txt'),'utf8').trim();
const origin=new URL(url).origin;
assert.equal(new URL(url).hostname,'127.0.0.1');
const audit=()=>JSON.parse(fs.readFileSync(path.join(out,'audit.json'),'utf8'));
const results=[];
let browser;
(async()=>{
  browser=await chromium.launch({headless:true,executablePath:process.env.BROWSER_EXECUTABLE});
  const context=await browser.newContext({ignoreHTTPSErrors:true,timezoneId:'Europe/Berlin',
    viewport:{width:1440,height:1000}});
  const outside=[],catalogues=[],posts=[],faults=[];
  await context.route('**/*',route=>{
    if(new URL(route.request().url()).origin===origin)return route.continue();
    outside.push(route.request().url());return route.abort();
  });
  const page=await context.newPage();page.setDefaultTimeout(10000);
  page.on('pageerror',e=>faults.push(e.message));
  page.on('request',request=>{
    if(new URL(request.url()).pathname==='/v1/agent/action-services')
      catalogues.push({method:request.method(),body:request.postData()});
  });
  await page.addInitScript(()=>{
    window.micCalls=0;
    navigator.mediaDevices.getUserMedia=async()=>{window.micCalls++;throw Error('Microphone forbidden in action proof');};
  });
  const field=name=>page.locator(`[data-action-field="${name}"]`);
  async function login(prefix='n5-test-only-'){
    await page.locator('#enrollment').fill(prefix.padEnd(43,'0'));
    await page.locator('#login-form button').click();
    await page.locator('#workspace').waitFor({state:'visible'});
    await page.waitForFunction(()=>!document.querySelector('#task-submit').disabled);
  }
  async function home(){
    await page.getByRole('button',{name:'SOLVIO',exact:true}).click();
    await page.locator('#overview-view').waitFor({state:'visible'});
    if(!await page.locator('#conversation-entry').isVisible())await page.locator('#new-task').click();
    await page.locator('#objective').waitFor({state:'visible'});
  }
  async function choose(kind){
    if(!await page.locator('#task-options').evaluate(n=>n.open))
      await page.locator('#task-options summary').click();
    await page.locator('#scope').selectOption('action');
    await page.locator('#action-mode').selectOption('exact');
    assert.equal(await page.locator('#action-fields details').evaluate(n=>n.open),true);
    await field('kind').selectOption(kind);
    await page.waitForFunction(()=>!document.querySelector('[data-action-field="account"]').disabled&&
      !!document.querySelector('[data-action-field="account"]').value);
  }
  async function capture(kind){
    for(const width of [320,390,1440]){
      await page.setViewportSize({width,height:1000});
      // Full-page captures start at the top: fixed accessibility links should
      // retain their normal viewport position, not the last focused field's.
      await page.evaluate(()=>scrollTo(0,0));
      const dimensions=await page.evaluate(()=>({inner:innerWidth,width:document.documentElement.scrollWidth}));
      assert(dimensions.width<=dimensions.inner,`${kind} overflows at ${width}: ${JSON.stringify(dimensions)}`);
      await page.screenshot({path:path.join(out,`${kind}-${width}.png`),fullPage:true});
    }
  }
  async function canonicalCount(count){
    const deadline=Date.now()+5000;
    while(audit().runs.length!==count&&Date.now()<deadline)
      await new Promise(resolve=>setTimeout(resolve,25));
    assert.equal(audit().runs.length,count);
    return audit();
  }
  await page.goto(url);await login();
  assert.equal(await page.locator('#objective').inputValue(),'');
  assert.equal(await page.locator('#scope').inputValue(),'research');
  assert.equal(await page.locator('#action-fields').isVisible(),false);
  assert.equal(catalogues.length,0);assert.equal(audit().runs.length,0);
  await capture('home');results.push('default home remains empty and action form opt-in');

  await choose('calendar');
  const listed=await (await context.request.get(origin+'/v1/agent/action-services')).json();
  assert.deepEqual(listed.services,audit().services);
  assert.equal(audit().runs.length,0);
  assert(catalogues.length>=1&&catalogues.every(row=>row.method==='GET'&&row.body===null));
  results.push('real native account catalogue read without task, approval, dispatch or provider');

  await page.route('**/v1/agent/tasks',async route=>{
    if(route.request().method()!=='POST')return route.continue();
    const response=await route.fetch(),accepted=await response.json();
    posts.push({request:route.request().postDataJSON(),accepted,status:response.status()});
    await route.fulfill({response});
  });
  await page.locator('#objective').fill('Lege den Werkstatttermin mit diesen konkreten Angaben an.');
  await field('summary').fill('Werkstatttermin');
  await field('start').fill('2026-09-15T10:00');await field('end').fill('2026-09-15T11:00');
  await field('location').fill('Radladen');await field('description').fill('Bremsen prüfen.');
  await capture('calendar');
  await page.locator('#task-submit').click();
  await page.locator('#conversation').waitFor({state:'visible'});
  assert.equal(posts.length,1);assert.equal(posts[0].status,201,JSON.stringify(posts[0]));
  const calendar=audit().services.find(row=>row.service==='calendar');
  const expectedCalendar={actions:[{action_id:'a1',service:'calendar',operation:'create',account:calendar.account,
    target:{calendar_id:calendar.resource},payload:{summary:'Werkstatttermin',start:'2026-09-15T08:00:00.000Z',
      end:'2026-09-15T09:00:00.000Z',all_day:false,description:'Bremsen prüfen.',location:'Radladen'}}]};
  assert.deepEqual(posts[0].request.task.action_request,expectedCalendar);
  const first=await canonicalCount(1);
  assert.deepEqual(first.runs[0].actions,expectedCalendar.actions);
  assert.equal(first.runs[0].grant_method,'dashboard_session');
  assert.equal(first.runs[0].origin,'trusted_dashboard');
  results.push('calendar exact absolute-time contract admitted through real dashboard and Core');
  console.log('SMOKE PASSED: actual calendar dashboard admission');
  if(process.argv.includes('--smoke')){
    fs.writeFileSync(path.join(out,'action-smoke.json'),JSON.stringify({results,posts,audit:first},null,2));
    return;
  }

  await home();await choose('gmail');
  await field('mail_mode').selectOption('exact');
  await page.locator('#objective').fill('Erstelle einen Mailentwurf mit diesen konkreten Angaben.');
  await field('to').fill('recipient@example.invalid');await field('subject').fill('Rückfrage zum Termin');
  const mailBody='Guten Tag,\nbitte bestätigen Sie den Termin. 😀\n<img src=x onerror="window.actionInjection=true">';
  await field('body').fill(mailBody);await capture('gmail');
  assert.equal(await page.locator('#action-fields').getByText('Der Auftrag erstellt einen Entwurf in deinem Postfach. Er versendet ihn nicht.').isVisible(),true);
  await page.unroute('**/v1/agent/tasks');
  let loseOnce=true;
  await page.route('**/v1/agent/tasks',async route=>{
    if(route.request().method()!=='POST')return route.continue();
    const response=await route.fetch(),accepted=await response.json();
    posts.push({request:route.request().postDataJSON(),accepted,status:response.status()});
    if(loseOnce){loseOnce=false;return route.abort('failed');}
    return route.fulfill({response});
  });
  await page.locator('#task-submit').click();await page.locator('#task-retry').waitFor({state:'visible'});
  assert.equal(posts[1].status,201,JSON.stringify(posts[1]));
  const beforeRetry=await canonicalCount(2);
  assert.equal(await field('body').isDisabled(),true);
  assert.equal(await page.locator('#scope').isDisabled(),true);
  await page.locator('#task-retry').click();await page.locator('#conversation').waitFor({state:'visible'});
  assert.equal(posts.length,3);assert.equal(posts[2].status,201,JSON.stringify(posts[2]));
  assert.deepEqual(posts[2].request,posts[1].request);
  assert.equal(posts[2].accepted.task_id,posts[1].accepted.task_id);
  assert.equal(posts[2].accepted.run_id,posts[1].accepted.run_id);
  const gmail=audit().services.find(row=>row.service==='gmail');
  const expectedMail={actions:[{action_id:'a1',service:'gmail',operation:'create_draft',account:gmail.account,
    target:{mailbox:'me',to:'recipient@example.invalid',reply_to_message:''},
    payload:{subject:'Rückfrage zum Termin',body:mailBody,thread_id:'',in_reply_to:''}}]};
  assert.deepEqual(posts[1].request.task.action_request,expectedMail);
  const afterRetry=await canonicalCount(2);
  assert.deepEqual(afterRetry.runs,beforeRetry.runs);
  assert.deepEqual(afterRetry.runs.find(run=>run.run_id===posts[1].accepted.run_id).actions,expectedMail.actions);
  assert.equal(afterRetry.counts.agent_action_contracts,2);
  assert.equal(await page.evaluate(()=>!!window.actionInjection),false);
  results.push('mail is draft-only; complete payload survives actual lost response, exact retry creates no second task');

  await home();await choose('gmail');
  assert.equal(await field('mail_mode').inputValue(),'compose');
  assert.equal(await field('subject').isVisible(),false);
  assert.equal(await field('body').isVisible(),false);
  await page.locator('#objective').fill('Formuliere einen freundlichen Mailentwurf für dieses Anliegen.');
  await field('to').fill('fixed-recipient@example.invalid');
  const instruction='Bitte freundlich um einen Termin für die Fahrradreparatur bitten.\nSchlage keine erfundenen Uhrzeiten vor. 😀';
  await field('instruction').fill(instruction);await capture('compose');
  loseOnce=true;
  await page.locator('#task-submit').click();await page.locator('#task-retry').waitFor({state:'visible'});
  assert.equal(posts[3].status,201,JSON.stringify(posts[3]));
  assert.equal(await field('instruction').isDisabled(),true);
  assert.equal(await field('mail_mode').isDisabled(),true);
  const composeBefore=await canonicalCount(3);
  await page.locator('#task-retry').click();await page.locator('#conversation').waitFor({state:'visible'});
  assert.equal(posts.length,5);assert.equal(posts[4].status,201,JSON.stringify(posts[4]));
  assert.deepEqual(posts[4].request,posts[3].request);
  assert.equal(posts[4].accepted.run_id,posts[3].accepted.run_id);
  const expectedComposed={actions:[{action_id:'a1',service:'gmail',operation:'compose_draft',account:gmail.account,
    target:{mailbox:'me',to:'fixed-recipient@example.invalid'},payload:{instruction}}]};
  assert.deepEqual(posts[3].request.task.action_request,expectedComposed);
  const composeAfter=await canonicalCount(3);
  assert.deepEqual(composeAfter.runs,composeBefore.runs);
  assert.deepEqual(composeAfter.runs.find(run=>run.run_id===posts[3].accepted.run_id).actions,expectedComposed.actions);
  assert.equal(composeAfter.counts.agent_action_contracts,3);
  results.push('default mail composition binds recipient and whole instruction; lost response retries the same canonical task without formulating or sending');

  await home();await choose('gmail');
  await field('to').fill('private-local@example.invalid');await field('instruction').fill('Logout must remove this unsent content.');
  await page.locator('#objective').fill('Unsent private task');
  await page.locator('#logout').click();await page.locator('#login-panel').waitFor({state:'visible'});
  assert.equal(await page.locator('#objective').inputValue(),'');
  assert.equal(await page.locator('#action-fields').isVisible(),false);
  for(const name of ['kind','account','to','instruction','subject','body','summary','start','end','location','description'])
    assert.equal(await field(name).inputValue(),'');
  assert.equal(await field('mail_mode').inputValue(),'compose');
  await login('n8-action-relogin-');
  assert.equal(await page.locator('#scope').inputValue(),'research');
  assert.equal(await page.locator('#action-fields').isVisible(),false);
  assert.equal(posts.length,5);await canonicalCount(3);
  results.push('logout clears all unsent action fields, new session starts empty');
  assert.deepEqual(faults,[]);assert.deepEqual(outside,[]);
  assert.equal(await page.evaluate(()=>window.micCalls),0);
  const final=audit();
  assert.equal(final.native_calls,0);assert.equal(final.native_mutations,0);assert.equal(final.pending_approvals,0);
  assert.equal(final.counts.agent_action_claims,0);
  results.push('three viewport sizes have no overflow; no microphone, external network, service dispatch or JavaScript errors');
  const evidence={results,posts,catalogues,audit:final,microphone_calls:0,external_requests:outside,js_errors:faults};
  fs.writeFileSync(path.join(out,'action-browser-check.json'),JSON.stringify(evidence,null,2));
  console.log(JSON.stringify(evidence,null,2));
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{await browser?.close();});
