/* Actual Chromium -> actual temporary authenticated Core -> existing native
 * owned-session catalogue. Portal/CDP only synthetic; never a login/action tick. */
const {chromium}=require('playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const out=path.resolve(process.argv[2]),url=fs.readFileSync(path.join(out,'dashboard-url.txt'),'utf8').trim();
const origin=new URL(url).origin,audit=()=>JSON.parse(fs.readFileSync(path.join(out,'audit.json'),'utf8'));
assert.equal(new URL(url).hostname,'127.0.0.1');
let browser;const results=[],faults=[],outside=[],posts=[],reads=[];
(async()=>{
 browser=await chromium.launch({headless:true,executablePath:process.env.BROWSER_EXECUTABLE});
 const context=await browser.newContext({ignoreHTTPSErrors:true,viewport:{width:1440,height:1000}});
 await context.route('**/*',route=>{
  if(new URL(route.request().url()).origin===origin)return route.continue();
  outside.push(route.request().url());return route.abort();
 });
 const page=await context.newPage();page.setDefaultTimeout(10000);
 page.on('pageerror',e=>faults.push(e.message));
 page.on('request',r=>{if(new URL(r.url()).pathname==='/v1/agent/action-portal-sessions')reads.push(r.method());});
 await page.addInitScript(()=>{window.micCalls=0;navigator.mediaDevices.getUserMedia=async()=>{window.micCalls++;throw Error('Microphone forbidden');};});
 const field=name=>page.locator(`[data-action-field="${name}"]`);
 const waitAudit=async predicate=>{const end=Date.now()+5000;while(!predicate(audit())&&Date.now()<end)await new Promise(r=>setTimeout(r,25));assert(predicate(audit()));};
 let loseNext=true;
 await page.route('**/v1/agent/tasks',async route=>{
  if(route.request().method()!=='POST')return route.continue();
  const response=await route.fetch();const accepted=await response.json();
  posts.push({request:route.request().postDataJSON(),accepted,status:response.status()});
  if(loseNext){loseNext=false;return route.abort('failed');}return route.fulfill({response});
 });
 await page.goto(url);await page.locator('#enrollment').fill('n8-portal-status-'.padEnd(43,'0'));
 await page.locator('#login-form button').click();await page.locator('#workspace').waitFor({state:'visible'});
 await page.waitForFunction(()=>!document.querySelector('#task-submit').disabled);
 assert.equal(reads.length,0);assert.equal(audit().runs.length,0);
 await page.locator('#task-options summary').click();await page.locator('#scope').selectOption('action');
 await page.locator('#action-mode').selectOption('exact');await field('kind').selectOption('portal');
 await page.waitForFunction(()=>!document.querySelector('[data-action-field="portal_session"]').disabled);
 assert.deepEqual(reads,['GET']);assert.equal(await field('portal_session').inputValue(),'');
 const choice=await field('portal_session').locator('option').nth(1).getAttribute('value');
 assert(!await field('portal_session').innerText().then(t=>t.includes('ps-4242-1')));
 assert.equal(audit().runs.length,0);assert(!await page.getByRole('button',{name:/Portal.*verbinden/i}).count());
 results.push('opening the opt-in portal selector only reads owned sessions; no automatic selection or task/login');
 await field('portal_session').selectOption(choice);
 for(const width of [320,390,1440]){
  await page.setViewportSize({width,height:1000});await page.evaluate(()=>scrollTo(0,0));
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),`overflow ${width}`);
  await page.screenshot({path:path.join(out,`portal-status-${width}.png`),fullPage:true});
 }
 fs.writeFileSync(path.join(out,'fixture-control.json'),JSON.stringify({hide_session:true}));await waitAudit(a=>!a.session_present);
 await page.getByRole('button',{name:'Verbindungen neu lesen',exact:true}).click();
 await page.waitForFunction(()=>document.querySelector('[data-action-field="portal_session"]').options.length===1);
 assert.equal(await field('portal_session').inputValue(),'');assert(await field('portal_session').isDisabled());
 assert.equal(audit().runs.length,0);results.push('disappeared owned connection leaves an honest empty selector without replacement');
 fs.writeFileSync(path.join(out,'fixture-control.json'),JSON.stringify({hide_session:false}));await waitAudit(a=>a.session_present);
 await page.getByRole('button',{name:'Verbindungen neu lesen',exact:true}).click();
 await page.waitForFunction(()=>!document.querySelector('[data-action-field="portal_session"]').disabled);
 await field('portal_session').selectOption(choice);await page.locator('#objective').fill('Zeige den Status meiner bestehenden Portalverbindung.');
 await page.locator('#task-submit').click();await page.locator('#task-retry').waitFor({state:'visible'});
 assert.equal(posts[0].status,201);assert.equal(posts[0].request.task.action_request.actions[0].operation,'status');
 await waitAudit(a=>a.runs.length===1);assert.equal(audit().runs[0].receipt_method,'dashboard_session');
 assert(await field('portal_session').isDisabled());await page.locator('#task-retry').click();
 await page.waitForFunction(()=>{const notice=document.querySelector('#notice');return notice&&!notice.hidden&&notice.textContent.includes('Auftrag angenommen');});
 assert.equal(posts.length,2);assert.deepEqual(posts[0].request,posts[1].request);assert.equal(posts[0].accepted.run_id,posts[1].accepted.run_id);
 results.push('actual public lost-201 and retry keep exactly one status task and original bound session; no approval or login');
 const fresh=await context.newPage();await fresh.goto(url);await fresh.locator('#workspace').waitFor({state:'visible'});
 assert.equal(audit().runs.length,1);assert.equal(audit().claims,0);assert.equal(audit().pending_approvals,0);
 assert.equal(await page.evaluate(()=>window.micCalls),0);assert.deepEqual(outside,[]);assert.deepEqual(faults,[]);
 results.push('fresh dashboard reads existing work without another task; zero microphone/provider/native effect calls');
 fs.writeFileSync(path.join(out,'result.json'),JSON.stringify({passed:results.length,failed:0,results,posts:posts.map(p=>({status:p.status,run_id:p.accepted.run_id})),faults,outside,audit:audit(),widths:[320,390,1440]},null,2));
 await fresh.close();console.log(JSON.stringify({passed:results.length,failed:0}));
})().catch(e=>{console.error(e);process.exitCode=1;}).finally(async()=>{await browser?.close();});
