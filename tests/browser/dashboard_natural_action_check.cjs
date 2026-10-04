/* Actual dashboard -> authenticated Core -> persisted question/answer -> native
 * contract/effect/readback. CLI interpretation and native transport are local
 * synthetic counterparts; no real model, account or microphone is used. */
const {chromium}=require('playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const out=path.resolve(process.argv[2]),url=fs.readFileSync(path.join(out,'dashboard-url.txt'),'utf8').trim();
const origin=new URL(url).origin,audit=()=>JSON.parse(fs.readFileSync(path.join(out,'audit.json'),'utf8'));
assert.equal(new URL(url).hostname,'127.0.0.1');
const objective='Trage morgen den Termin Fahrradwerkstatt um 09:00 ein.';
const results=[],outside=[],errors=[],starts=[],answers=[];let browser,micCalls=0;
async function until(predicate){const end=Date.now()+20000;while(!predicate()&&Date.now()<end)await new Promise(r=>setTimeout(r,30));assert(predicate(),JSON.stringify(audit()));}
function stage(value){const next=path.join(out,'fixture-control-next.json');fs.writeFileSync(next,JSON.stringify({stage:value}));fs.renameSync(next,path.join(out,'fixture-control.json'));}
(async()=>{
  browser=await chromium.launch({headless:true,executablePath:process.env.BROWSER_EXECUTABLE});
  async function session(prefix){
    const context=await browser.newContext({ignoreHTTPSErrors:true,timezoneId:'Europe/Berlin',viewport:{width:390,height:1000}});
    await context.route('**/*',route=>{if(new URL(route.request().url()).origin===origin)return route.continue();outside.push(route.request().url());return route.abort();});
    const page=await context.newPage();page.setDefaultTimeout(10000);page.on('pageerror',e=>errors.push(e.message));
    await page.exposeFunction('unexpectedMic',()=>micCalls++);
    await page.addInitScript(()=>{navigator.mediaDevices.getUserMedia=async()=>{await window.unexpectedMic();throw Error('No microphone');};});
    await page.goto(url);await page.locator('#enrollment').fill(prefix.padEnd(43,'0'));await page.locator('#login-form button').click();
    await page.waitForFunction(()=>!document.querySelector('#task-submit').disabled);return {page,context};
  }
  const first=await session('n8-natural-browser-'),page=first.page;
  let loseStart=true,loseAnswer=true;
  await page.route('**/v1/agent/tasks',async route=>{
    const response=await route.fetch(),accepted=await response.json();starts.push({body:route.request().postDataJSON(),accepted,status:response.status()});
    if(loseStart){loseStart=false;return route.abort();}return route.fulfill({response});
  });
  await page.route('**/action-answer',async route=>{
    const response=await route.fetch(),accepted=await response.json();answers.push({body:route.request().postDataJSON(),accepted,status:response.status()});
    if(loseAnswer){loseAnswer=false;return route.abort();}return route.fulfill({response});
  });
  await page.locator('#task-options summary').click();await page.locator('#scope').selectOption('action');
  assert.equal(await page.locator('#action-mode').inputValue(),'natural');assert.equal(await page.locator('#action-fields').isVisible(),false);
  await page.locator('#objective').fill(objective);await page.locator('#task-submit').click();await page.locator('#task-retry').waitFor();
  await page.locator('#task-retry').click();await page.locator('#conversation').waitFor({state:'visible'});
  assert.equal(starts.length,2);assert(starts.every(s=>s.status===202));assert.deepEqual(starts[0].body,starts[1].body);assert.deepEqual(starts[0].accepted,starts[1].accepted);
  await until(()=>audit().runs.length===1);const admitted=audit(),runId=admitted.runs[0].run_id;
  assert.equal(runId,starts[0].accepted.run_id);assert.equal(admitted.counts.agent_tasks,1);assert.equal(admitted.counts.agent_task_grants,0);assert.equal(admitted.native_mutations,0);assert.equal(admitted.local_protocol_calls.length,0);
  assert.equal(admitted.runs[0].origin,'trusted_dashboard');assert.equal(admitted.runs[0].binding.source.receipt_method,'dashboard_session');
  results.push('real HTTPS own-words admission202 and lost-response replay persist one original task and owner receipt, without action grant or interpretation at admission');

  stage('interpret');await until(()=>audit().phase==='question_ready');
  await page.evaluate(()=>window.dispatchEvent(new Event('online')));await page.locator('#action-answer').waitFor();
  const pending=audit(),q=pending.runs[0].intent.question;assert.equal(q.field,'duration');assert.equal(pending.runs[0].state,'WAITING_USER');
  assert.equal(await page.locator('#result').getByText(q.prompt,{exact:true}).count(),1);
  assert.equal(pending.local_protocol_calls.length,1);assert.equal(pending.local_protocol_calls[0].kind,'interpret');assert.equal(pending.counts.agent_task_grants,0);
  assert.equal(await page.getByRole('button',{name:'Fortsetzen',exact:true}).count(),0);assert.equal(await page.locator('#decision-dialog').isVisible(),false);
  await page.locator('#action-answer').fill('60');
  for(const width of [320,390,1440]){await page.setViewportSize({width,height:1000});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);await page.screenshot({path:path.join(out,`natural-question-${width}.png`),fullPage:true});}
  results.push('actual persisted missing-duration question appears in the same task after one local subscription-protocol call; no native grant/effect or second start confirmation');

  await page.getByRole('button',{name:'Antworten',exact:true}).click();await page.getByRole('button',{name:'Dieselbe Antwort erneut übermitteln',exact:true}).waitFor();
  assert.equal(await page.locator('#action-answer').isDisabled(),true);
  await page.getByRole('button',{name:'Dieselbe Antwort erneut übermitteln',exact:true}).click();
  await page.waitForFunction(()=>!document.querySelector('#action-answer'));
  assert.equal(answers.length,2);assert(answers.every(a=>a.status===200),JSON.stringify(answers));assert.deepEqual(answers[0].body,answers[1].body);
  assert.deepEqual(answers[0].body,{question_id:q.id,expected_revision:q.revision,expected_digest:q.digest,answer:'60',client_request_id:answers[0].body.client_request_id});
  await until(()=>audit().counts.agent_action_intent_answers===1);assert.equal(audit().counts.agent_tasks,1);assert.equal(audit().native_mutations,0);
  stage('resolve');await until(()=>audit().phase==='bound');const bound=audit(),action=bound.runs[0].actions[0];
  assert.equal(bound.runs[0].run_id,runId);assert.equal(bound.counts.agent_task_grants,1);assert.equal(bound.counts.agent_action_contracts,1);assert.equal(bound.local_protocol_calls.length,1);
  assert.deepEqual(bound.runs[0].binding,admitted.runs[0].binding);assert.equal(bound.runs[0].receipt_method,'dashboard_session');
  assert.equal(action.service,'calendar');assert.equal(action.operation,'create');assert.equal(action.payload.summary,'Fahrradwerkstatt');
  assert.equal(Date.parse(action.payload.end)-Date.parse(action.payload.start),3600000);
  const nextDay=new Date(Date.parse(admitted.runs[0].binding.anchor.slice(0,10)+'T00:00:00Z')+86400000).toISOString().slice(0,10);
  assert.equal(action.payload.start.slice(0,16),nextDay+'T09:00');assert.equal(action.payload.end.slice(0,16),nextDay+'T10:00');assert.equal(bound.native_mutations,0);
  results.push('bound answer lost-response replay persists one answer; same original owner receipt/time anchor produces exactly one calendar grant with explicit sixty-minute duration');

  await first.context.close();stage('execute');await until(()=>audit().phase==='completed');const completed=audit();
  assert.equal(completed.runs[0].state,'SUCCEEDED');assert.equal(completed.native_mutations,1);
  assert.deepEqual(completed.local_protocol_calls.map(c=>c.kind),['interpret','assessment']);
  assert.equal(completed.runs[0].receipts.length,1);assert.equal(completed.runs[0].receipts[0].status,'completed');assert.equal(completed.runs[0].receipts[0].native.observed.confirmed,true);
  assert.equal(completed.counts.agent_tasks,1);assert.equal(completed.counts.agent_runs,1);assert.equal(completed.counts.agent_action_intent_answers,1);assert.equal(completed.counts.agent_task_grants,1);
  results.push('after browser closes real Core executes one synthetic native calendar write/readback; reconstructed runtime and another tick preserve the receipt without repetition');

  const fresh=await session('n8-natural-fresh-');await fresh.page.getByRole('button',{name:'Aufträge',exact:true}).click();
  await fresh.page.locator('#task-list').getByText(objective,{exact:true}).click();await fresh.page.locator('#result > .state.success').waitFor();
  const visible=await fresh.page.locator('#result').innerText();assert(visible.includes('Fahrradwerkstatt'));assert.equal(await fresh.page.locator('#action-answer').count(),0);
  assert.equal(await fresh.page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);await fresh.page.screenshot({path:path.join(out,'natural-completed-fresh-390.png'),fullPage:true});
  assert.equal(audit().native_mutations,1);assert.equal(micCalls,0);assert.deepEqual(outside,[]);assert.deepEqual(errors,[]);
  results.push('fresh authenticated browser reads canonical completed result; no second task, microphone, outside network, real provider/account call or JavaScript error');
  fs.writeFileSync(path.join(out,'natural-action-browser-result.json'),JSON.stringify({results,starts,answers,admitted,pending,bound,completed,visible,
    microphone_calls:micCalls,external_requests:outside,js_errors:errors,limitation:'CLI interpretation/assessment and calendar transport are synthetic; actual model interpretation and real account execution are not claimed.'},null,2));
  console.log(JSON.stringify({passed:results.length,results},null,2));
})().catch(error=>{console.error(error.stack);console.error('Browser errors',errors);process.exitCode=1;}).finally(async()=>{await browser?.close();});
