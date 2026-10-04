/* Actual packaged dashboard in Chromium; loopback HTTP and synthetic API only.
 * This proves UI bindings/races, not interpretation or a native service effect.
 * NODE_PATH=<bundled node_modules> BROWSER_EXECUTABLE=<Chrome> node file outdir */
const {chromium}=require('playwright');
const fs=require('node:fs'),path=require('node:path'),http=require('node:http'),assert=require('node:assert/strict');
const assets=path.resolve(__dirname,'../../src/solvio/dashboard/assets');
const out=path.resolve(process.argv[2]);fs.mkdirSync(out,{recursive:true});
const tasks=new Map(),runs=new Map(),answers=new Map(),calls=[],passed=[],faults=[],outside=[];
let loggedIn=false,server,browser,origin,catalogues=0,detailReads=0,failDetail=false,rejectAnswer=null,holdAnswer=null;
const question=(id,revision,field,input_type,prompt,extra={})=>({id,revision,digest:String(revision).padStart(64,'a'),field,input_type,prompt,placeholder:'Bitte ergänzen',...extra});
function setQuestion(run,q){run.zustand='Wartet auf dich';run.zustand_code='WAITING_USER';run.offen=true;run.action_intent={status:'waiting_user',question:q};}
function newRun(task){
  const id='ar-'+String(runs.size+1).padStart(16,'0');
  const run={id,aufgabe:'at-'+String(runs.size+1).padStart(16,'0'),auftrag:task.objective,zustand:'Angenommen',zustand_code:'CREATED',offen:true,angelegt:1790000000,
    kosten:{configured:false},verlauf:[],befunde:[],quellen:[],artefakte:[]};
  if(task.action_intent)setQuestion(run,question('q-date',1,'date','text','An welchem Tag soll der Termin stattfinden?',{placeholder:'morgen oder 2026-09-15'}));
  runs.set(id,run);return run;
}
const json=(res,status,value)=>{res.writeHead(status,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify(value));};
async function serve(req,res){
  const url=new URL(req.url,origin),pathname=url.pathname;let body;
  if(req.method!=='GET'){let bytes='';for await(const part of req)bytes+=part;body=bytes?JSON.parse(bytes):null;}
  calls.push({method:req.method,path:pathname,body});
  if(pathname==='/v1/browser/session/login'){loggedIn=true;return json(res,200,{csrf_token:'synthetic-session-csrf',principal:'local-owner'});}
  if(pathname==='/v1/browser/session/logout'){loggedIn=false;return json(res,200,{ok:true});}
  if(pathname.startsWith('/v1/')){
    if(!loggedIn)return json(res,401,{error:'unauthorized'});
    if(req.method!=='GET')assert.equal(req.headers['x-csrf-token'],'synthetic-session-csrf');
    if(pathname==='/v1/browser/session')return json(res,200,{csrf_token:'synthetic-session-csrf',principal:'local-owner'});
    if(pathname==='/v1/dashboard/state')return json(res,200,{environment:'isolated_test',runtime:'available',stand:1790000000,repositories:[],components:[],learning:{state:'disabled'},cost_controls:'unavailable'});
    if(pathname==='/v1/agent/approvals')return json(res,200,{approvals:[]});
    if(pathname==='/v1/control/audio')return json(res,200,{scope:'Keine Sprachsitzung in diesem UI-Test.',devices:[]});
    if(pathname==='/v1/agent/action-services'){catalogues++;return json(res,200,{services:[{service:'calendar',account:'cal-test',resource:'test-calendar'},{service:'gmail',account:'gmail-test',resource:'me'}]});}
    if(pathname==='/v1/agent/runs')return json(res,200,{laeufe:[...runs.values()]});
    if(pathname==='/v1/agent/tasks'&&req.method==='POST'){
      let accepted=tasks.get(body.task.client_request_id);
      if(!accepted){const run=newRun(body.task);accepted={run_id:run.id,task_id:run.aufgabe};tasks.set(body.task.client_request_id,accepted);}
      return json(res,201,accepted);
    }
    const route=pathname.match(/^\/v1\/agent\/runs\/(ar-[0-9a-f]{16})(\/(?:action-answer|resume))?$/),run=route&&runs.get(route[1]);
    if(run&&req.method==='GET'){detailReads++;return failDetail?json(res,503,{error:'unavailable'}):json(res,200,run);}
    if(run&&route[2]==='/resume'&&req.method==='POST'){
      assert.equal(run.action_intent.question.field,'account');
      setQuestion(run,question('q-account-connected',7,'account','select','Welches verbundene Konto soll verwendet werden?',{options:[{value:'opaque-connected',label:'Neu verbundenes Konto'}]}));
      return json(res,200,{ok:true});
    }
    if(run&&route[2]&&req.method==='POST'){
      if(holdAnswer)await holdAnswer;
      if(rejectAnswer){const error=rejectAnswer;rejectAnswer=null;return json(res,409,error);}
      let recorded=answers.get(body.client_request_id);
      if(recorded)assert.deepEqual(recorded.body,body);
      else{
        assert.equal(body.question_id,run.action_intent.question.id);
        assert.equal(body.expected_revision,run.action_intent.question.revision);
        assert.equal(body.expected_digest,run.action_intent.question.digest);
        recorded={body:structuredClone(body),run_id:run.id};answers.set(body.client_request_id,recorded);
        run.action_intent={status:'interpreting',question:null};run.zustand_code='PLANNING';run.zustand='Wird geplant';
      }
      return json(res,200,{action_intent:run.action_intent});
    }
    return json(res,404,{error:'unexpected_test_route'});
  }
  let file=pathname==='/dashboard/'?path.join(assets,'index.html'):pathname.startsWith('/dashboard/assets/')?path.resolve(assets,pathname.slice('/dashboard/assets/'.length)):'';
  if(!file.startsWith(assets+path.sep)||!fs.existsSync(file)||!fs.statSync(file).isFile()){res.writeHead(404);res.end();return;}
  const type={'.js':'text/javascript','.css':'text/css','.html':'text/html','.svg':'image/svg+xml','.png':'image/png','.woff2':'font/woff2'}[path.extname(file)]||'application/octet-stream';
  res.writeHead(200,{'Content-Type':type,'Cache-Control':'no-store'});res.end(fs.readFileSync(file));
}
const settle=async predicate=>{const until=Date.now()+5000;while(!predicate()&&Date.now()<until)await new Promise(r=>setTimeout(r,10));assert(predicate());};
(async()=>{
  server=http.createServer((req,res)=>serve(req,res).catch(error=>{faults.push(String(error.stack));res.destroy();}));
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));origin='http://127.0.0.1:'+server.address().port;
  browser=await chromium.launch({headless:true,executablePath:process.env.BROWSER_EXECUTABLE});
  const context=await browser.newContext({timezoneId:'Europe/Berlin',viewport:{width:1440,height:1000}});
  await context.route('**/*',route=>{if(new URL(route.request().url()).origin===origin)return route.continue();outside.push(route.request().url());return route.abort();});
  const page=await context.newPage();page.setDefaultTimeout(7000);page.on('pageerror',e=>faults.push(e.message));
  page.on('dialog',dialog=>dialog.accept());
  await page.addInitScript(()=>{window.micCalls=0;navigator.mediaDevices.getUserMedia=async()=>{window.micCalls++;throw Error('No microphone in this test');};});
  const postCalls=()=>calls.filter(c=>c.method==='POST'&&c.path==='/v1/agent/tasks');
  const answerCalls=()=>calls.filter(c=>c.path.endsWith('/action-answer'));
  const input=page.locator('#action-answer');
  async function login(){await page.locator('#enrollment').fill('test-only-code');await page.locator('#login-form button').click();await page.waitForFunction(()=>!document.querySelector('#task-submit').disabled);}
  async function home(){
    await page.getByRole('button',{name:'SOLVIO',exact:true}).click();
    if(!await page.locator('#conversation-entry').isVisible())await page.locator('#new-task').click();
    await page.locator('#objective').waitFor({state:'visible'});
  }
  async function action(){
    if(!await page.locator('#task-options').evaluate(n=>n.open))await page.locator('#task-options > summary').click();
    await page.locator('#scope').selectOption('action');
  }
  async function reread(){const before=detailReads;await page.evaluate(()=>window.dispatchEvent(new Event('online')));await settle(()=>detailReads>before);await page.waitForTimeout(40);}
  async function capture(prefix){for(const width of [320,390,1440]){await page.setViewportSize({width,height:1000});await page.evaluate(()=>scrollTo(0,0));assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,`${prefix} overflow at ${width}`);await page.screenshot({path:path.join(out,`${prefix}-${width}.png`),fullPage:true});}}
  await page.goto(origin+'/dashboard/');await login();
  assert.equal(await page.locator('#scope').inputValue(),'research');assert.equal(await page.locator('#action-entry').isVisible(),false);
  await action();assert.equal(await page.locator('#action-mode').inputValue(),'natural');assert.equal(await page.locator('#action-fields').isVisible(),false);
  assert.equal(catalogues,0);assert.equal(tasks.size,0);
  await page.locator('#objective').fill('Termin');await page.locator('#task-submit').click();
  assert.match(await page.locator('#task-error').textContent(),/12 bis 2.000/);assert.equal(tasks.size,0);
  await page.locator('#objective').fill('Trage morgen meinen Werkstatttermin um 15 Uhr ein.');await capture('natural-home');
  passed.push('explicit action selection exposes own-words input, default research unchanged, no account/catalogue or microphone request');

  let loseStart=true;
  await page.route('**/v1/agent/tasks',async route=>{if(!loseStart)return route.continue();loseStart=false;await route.fetch();await route.abort();});
  await page.locator('#task-submit').click();await page.locator('#task-retry').waitFor();
  assert.equal(await page.locator('#objective').isDisabled(),true);assert.equal(await page.locator('#action-mode').isDisabled(),true);
  await page.locator('#task-retry').click();await input.waitFor();
  assert.equal(tasks.size,1);assert.equal(postCalls().length,2);assert.deepEqual(postCalls()[0].body,postCalls()[1].body);
  assert.deepEqual(postCalls()[0].body.task.action_intent,{version:1});assert(!Object.hasOwn(postCalls()[0].body.task,'action_request'));
  const run=[...runs.values()][0];assert.equal(await page.getByRole('button',{name:'Fortsetzen',exact:true}).count(),0);
  assert.equal(await page.locator('#decision-dialog').isVisible(),false);
  passed.push('lost task acceptance retries identical intent, objective and ID; one synthetic admission and no extra confirmation');

  await input.fill('2026-09-15');await input.focus();const handle=await input.elementHandle();await reread();
  assert.equal(await input.inputValue(),'2026-09-15');assert.equal(await handle.evaluate(n=>n===document.activeElement),true);
  failDetail=true;await reread();assert.equal(await input.inputValue(),'2026-09-15');assert.equal(await input.isDisabled(),true);
  failDetail=false;await reread();assert.equal(await input.inputValue(),'2026-09-15');assert.equal(await input.isDisabled(),false);
  setQuestion(run,question('q-duration',2,'duration','number','Wie viele Minuten soll der Termin dauern?'));
  await input.focus();await reread();assert.equal(await input.inputValue(),'');assert.equal(await input.getAttribute('type'),'number');
  assert.equal(await page.locator('.action-question').textContent().then(s=>s.includes('Wie viele Minuten')),true);
  await input.fill('60');await capture('question');
  passed.push('focused polling preserves typed input; failed reads disable while retaining it; a changed canonical question invalidates the old form');

  let loseAnswer=true;
  await page.route('**/action-answer',async route=>{if(!loseAnswer)return route.continue();loseAnswer=false;await route.fetch();await route.abort();});
  await page.getByRole('button',{name:'Antworten',exact:true}).click();await page.getByRole('button',{name:'Dieselbe Antwort erneut übermitteln',exact:true}).waitFor();
  assert.equal(await input.isDisabled(),true);assert.equal(answers.size,1);assert.equal(answerCalls().length,1);
  await page.getByRole('button',{name:'Dieselbe Antwort erneut übermitteln',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('.action-question')?.textContent.includes('SOLVIO klärt'));
  assert.equal(answers.size,1);assert.equal(answerCalls().length,2);assert.deepEqual(answerCalls()[0].body,answerCalls()[1].body);
  assert.equal(tasks.size,1);assert.equal(await input.count(),0);
  passed.push('lost answer response freezes exact run/question/revision/digest/body and request ID; replay never starts another task or claims completion');

  setQuestion(run,question('q-recipient',3,'recipient','email','An welche Adresse soll der Entwurf gehen?'));
  await reread();await input.fill('person@example.invalid');rejectAnswer={error:'invalid_answer',reason:'Adresse prüfen <img src=x onerror="window.injected=true">'};
  await page.getByRole('button',{name:'Antworten',exact:true}).click();await page.waitForFunction(()=>document.querySelector('#action-answer-feedback').textContent.startsWith('Adresse prüfen'));
  assert.equal(await input.isDisabled(),false);assert.equal(await input.inputValue(),'person@example.invalid');
  assert.equal(await page.locator('.action-question img').count(),0);assert.equal(await page.evaluate(()=>!!window.injected),false);
  const badId=answerCalls().at(-1).body.client_request_id;
  await input.fill('corrected@example.invalid');await page.getByRole('button',{name:'Antworten',exact:true}).click();
  await page.waitForFunction(()=>!document.querySelector('#action-answer'));assert.notEqual(answerCalls().at(-1).body.client_request_id,badId);
  passed.push('confirmed invalid answer is editable; corrected answer gets a new ID; server prompt and reason are plain text');

  setQuestion(run,question('q-old',4,'time','text','Zu welcher Uhrzeit?'));await reread();await input.fill('15:30');
  setQuestion(run,question('q-new',5,'duration','number','Welche Dauer ist richtig?'));
  rejectAnswer={error:'stale_question'};const count=answers.size;
  await page.getByRole('button',{name:'Antworten',exact:true}).click();await page.waitForFunction(()=>document.querySelector('.action-question label')?.textContent==='Welche Dauer ist richtig?');
  assert.equal(await input.inputValue(),'');assert.equal(answers.size,count);assert.equal(await input.isDisabled(),false);
  passed.push('stale answer cannot resume old work; refreshed question replaces its binding without a second task');

  run.zustand_code='CANCELLED';run.zustand='Abgebrochen';run.offen=false;await reread();
  assert.equal(await input.count(),0);assert.equal(await page.locator('.action-question').count(),0);
  assert.equal(await page.getByRole('button',{name:'Antworten',exact:true}).count(),0);
  passed.push('terminal cancelled task suppresses a stale waiting question rather than offering another action');

  setQuestion(run,question('q-account',6,'account','select','Welches verbundene Konto soll verwendet werden?',{options:[{value:'opaque-a',label:'Privat <b>kein HTML</b>'},{value:'opaque-b',label:'Arbeit'}]}));
  await reread();assert.equal(await input.inputValue(),'');assert.equal(await page.locator('.action-question b').count(),0);
  const postsBefore=answerCalls().length;await page.getByRole('button',{name:'Antworten',exact:true}).click();assert.equal(answerCalls().length,postsBefore);
  await input.selectOption('opaque-b');await page.getByRole('button',{name:'Antworten',exact:true}).click();
  await page.waitForFunction(()=>!document.querySelector('#action-answer'));assert.equal(answerCalls().at(-1).body.answer,'opaque-b');
  passed.push('optional native account choice is explicit, has no preselection and sends only the opaque offered value');

  setQuestion(run,question('q-account-missing',7,'account','select','Verbinde zuerst ein Google-Konto.',{options:[]}));await reread();
  assert.equal(await input.isDisabled(),true);assert.equal(await page.getByRole('button',{name:'Antworten',exact:true}).isDisabled(),true);
  const answersBeforeCatalog=answerCalls().length;
  await page.getByRole('button',{name:'Verbundene Konten neu prüfen',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('#action-answer')?.options?.length===2);
  assert.equal(await input.inputValue(),'');assert.equal(answerCalls().length,answersBeforeCatalog);
  assert.equal(calls.filter(c=>c.path.endsWith('/resume')).length,1);
  passed.push('missing account offers only an explicit catalogue refresh on the same task; no phantom selection or answer is submitted');

  setQuestion(run,question('q-logout',7,'title','text','Wie lautet der Titel?'));await reread();await input.fill('Private Antwort vor Abmeldung');
  let release;holdAnswer=new Promise(resolve=>release=resolve);
  const priorAnswerCalls=answerCalls().length;
  await page.getByRole('button',{name:'Antworten',exact:true}).click();await settle(()=>answerCalls().at(-1).body.answer==='Private Antwort vor Abmeldung');
  await page.locator('.action-question form').evaluate(n=>n.dispatchEvent(new Event('submit',{cancelable:true})));
  assert.equal(answerCalls().length,priorAnswerCalls+1);
  await page.locator('#logout').click();await page.locator('#login-panel').waitFor({state:'visible'});
  release();holdAnswer=null;await page.waitForTimeout(80);
  assert.equal(await input.count(),0);assert.equal(await page.locator('#objective').inputValue(),'');
  await login();assert.equal(await page.locator('#scope').inputValue(),'research');assert.equal(await page.locator('#action-entry').isVisible(),false);
  assert.equal(await page.locator('#workspace').textContent().then(s=>s.includes('Private Antwort vor Abmeldung')),false);
  passed.push('logout clears cached input and ignores an in-flight answer from the previous session');

  await home();await page.locator('#objective').fill('Recherchiere, wie man einen Kalendertermin formuliert.');
  await page.locator('#task-submit').click();await page.locator('#conversation').waitFor({state:'visible'});
  const research=postCalls().at(-1).body.task;assert.equal(research.scope,'research');assert(!research.action_intent);assert(!research.action_request);
  setQuestion(run,question('q-background',8,'title','text','Wie lautet der korrigierte Titel?'));
  await page.getByRole('button',{name:'Aufträge',exact:true}).click();await page.locator('#task-list').getByText(run.auftrag,{exact:true}).click();
  await input.fill('Antwort für den anderen Auftrag');let releaseBackground;holdAnswer=new Promise(resolve=>releaseBackground=resolve);
  const beforeBackground=answers.size;
  await page.getByRole('button',{name:'Antworten',exact:true}).click();await settle(()=>answerCalls().at(-1).body.answer==='Antwort für den anderen Auftrag');
  await page.getByRole('button',{name:'Aufträge',exact:true}).click();await page.locator('#task-list').getByText(research.objective,{exact:true}).click();
  releaseBackground();holdAnswer=null;await settle(()=>answers.size===beforeBackground+1);await page.waitForTimeout(80);
  assert.equal(await page.locator('#conversation-objective').textContent(),research.objective);assert.equal(await input.count(),0);
  assert.equal(await page.evaluate(()=>window.dispatchEvent(new Event('beforeunload',{cancelable:true}))),true,'confirmed inactive answer must not leave a false uncertain warning');
  passed.push('answer confirmed after switching tasks settles only its own session cache; active task stays selected and no false unload warning remains');
  await home();await action();await page.locator('#action-mode').selectOption('exact');
  assert.equal(await page.locator('#action-fields details').evaluate(n=>n.open),true);await page.locator('[data-action-field="kind"]').selectOption('gmail');
  await page.waitForFunction(()=>!!document.querySelector('[data-action-field="account"]').value);
  await page.locator('[data-action-field="to"]').fill('exact@example.invalid');await page.locator('[data-action-field="instruction"]').fill('Freundlich nach einem Werkstatttermin fragen.');
  await page.locator('#objective').fill('Erstelle diesen Entwurf.');await page.locator('#task-submit').click();await page.locator('#conversation').waitFor({state:'visible'});
  const exact=postCalls().at(-1).body.task;assert(!exact.action_intent);assert.equal(exact.action_request.actions[0].operation,'compose_draft');assert.equal(exact.action_request.actions[0].target.to,'exact@example.invalid');
  passed.push('research text keeps research authority; explicit exact form still sends the existing action_request only');

  assert.equal(await page.evaluate(()=>window.micCalls),0);assert.deepEqual(outside,[]);assert.deepEqual(faults,[]);
  assert.equal(calls.filter(c=>c.path.endsWith('/resume')).length,1);
  passed.push('320/390/1440 widths fit; zero microphone, external requests, general question resume, real service/model calls or browser errors');
  const result={passed:passed.length,checks:passed,boundary:'Real Chromium and packaged assets; local synthetic HTTP API. Does not prove Core admission, native interpretation, account selection or service effects.',microphone_calls:0,external_requests:outside,js_errors:faults,synthetic_tasks:tasks.size,synthetic_answers:answers.size};
  fs.writeFileSync(path.join(out,'action-intent-browser-result.json'),JSON.stringify(result,null,2)+'\n');console.log(JSON.stringify(result,null,2));
})().catch(error=>{console.error(error.stack);console.error('Browser errors:',faults);process.exitCode=1;}).finally(async()=>{await browser?.close();await new Promise(resolve=>server?server.close(resolve):resolve());});
