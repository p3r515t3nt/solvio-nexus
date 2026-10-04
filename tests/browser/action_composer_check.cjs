/* Deterministic checks of the packaged module. Minimal DOM, no browser,
 * server, account, provider or package dependency. Run: node this-file.cjs. */
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const assert=require('node:assert/strict');
const source=fs.readFileSync(path.resolve(__dirname,'../../src/solvio/dashboard/assets/action-composer.js'),'utf8');
const portalSource=fs.readFileSync(path.resolve(__dirname,'../../src/solvio/dashboard/assets/portal-action.js'),'utf8');
const plain=value=>JSON.parse(JSON.stringify(value));
const tests=[];
const test=(name,run)=>tests.push({name,run});

class Element {
  constructor(tag,document){
    this.tagName=tag.toUpperCase();this.ownerDocument=document;this.children=[];
    this.attributes={};this.listeners={};this._text='';this._value='';this._selected=undefined;
    this.hidden=false;this.disabled=false;this.open=false;this.parentNode=null;
  }
  set textContent(value){this.children=[];this._text=String(value);}
  get textContent(){return this._text+this.children.map(child=>child.textContent).join('');}
  set innerHTML(_value){throw Error('Product must not insert HTML');}
  set value(value){
    if(this.tagName==='SELECT')this._selected=this.children.some(child=>child.value===String(value))?String(value):'';
    else this._value=String(value);
  }
  get value(){return this.tagName==='SELECT'?(this._selected===undefined?(this.children[0]?.value||''):this._selected):this._value;}
  append(...nodes){for(const node of nodes){node.parentNode=this;this.children.push(node);}}
  replaceChildren(...nodes){this.children=[];this._text='';this._selected=undefined;this.append(...nodes);}
  setAttribute(key,value){this.attributes[key]=String(value);}
  addEventListener(kind,callback){(this.listeners[kind]||=[]).push(callback);}
  dispatch(kind){for(const callback of this.listeners[kind]||[])callback({target:this,type:kind});}
}
function all(root){return [root,...root.children.flatMap(all)];}
const calendar={service:'calendar',account:'calendar-personal',resource:'personal@example.test'};
const gmail={service:'gmail',account:'gmail-personal',resource:'me'};
const ha={service:'ha',account:'ha-home',resource:'configured_home'};
const deviceRows=[{target:{entity_id:'light.desk'},name:'Schreibtisch',area:'Büro',domain:'light',state:'off',operations:['set_state','set_brightness']},
  {target:{entity_id:'switch.desk'},name:'Schreibtisch',area:'Büro',domain:'switch',state:'on',operations:['set_state']}];
const resources=(items=deviceRows,account=ha.account)=>({service:'ha',account,items,truncated:false});
const portalRow={account:'portal-owned',target:{portal_id:'solvio-studio',session_id:'ps-123-1'},
  label:'SOLVIO Studio',origin:'https://solvio-studio.de',authenticated:true,expires_in_s:300};
const portalRows=(items=[portalRow])=>({service:'portal',items,truncated:false,observed_at:Date.now()/1000});
const settle=()=>new Promise(resolve=>setImmediate(resolve));
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
function make(rows=[calendar,gmail]){
  const document={createElement:tag=>new Element(tag,document)};
  const container=document.createElement('div');let current=true,changes=0;
  const calls=[],answers=[],resourceAnswers=[],portalAnswers=[];
  const context={document,Date,Intl,AbortController,URL};
  vm.runInNewContext('const createPortalActionFields=(()=>{'+portalSource.replace('export function createPortalActionFields','function createPortalActionFields')+';return createPortalActionFields;})();\n'+
    source.replace("import {createPortalActionFields} from './portal-action.js';",'').replace('export function createActionComposer','function createActionComposer')+
    '\nglobalThis.factory=createActionComposer;',context,{filename:'action-composer.js',codeGeneration:{strings:false,wasm:false}});
  const composer=context.factory({container,isCurrent:()=>current,onChange:()=>{changes++;},
    api:async(url,options)=>{
      calls.push({url,options});
      if(url.startsWith('/v1/agent/action-portal-sessions?'))return portalAnswers.length?await portalAnswers.shift():portalRows();
      if(url.startsWith('/v1/agent/action-resources?'))return resourceAnswers.length?await resourceAnswers.shift():resources();
      if(answers.length)return await answers.shift();return {services:rows};
    }});
  const fields=Object.fromEntries(all(container).filter(n=>n.attributes['data-action-field']).map(n=>[n.attributes['data-action-field'],n]));
  const change=(name,value)=>{fields[name].value=value;fields[name].dispatch(fields[name].tagName==='SELECT'?'change':'input');};
  return {composer,container,fields,calls,answers,resourceAnswers,portalAnswers,change,all:()=>all(container),
    current:value=>{current=value;},changes:()=>changes,
    ready:async(kind='calendar')=>{composer.setEnabled(true);await composer.load();change('kind',kind);},
    calendar:()=>{change('summary','Werkstatttermin');change('start','2026-09-15T10:00');change('end','2026-09-15T11:00');},
    gmail:()=>{change('mail_mode','exact');change('to','person@example.test');change('subject','Rückfrage');change('body','Bitte um einen Termin.');},
    result:()=>plain(composer.request())};
}

test('hidden and inert until explicitly enabled; catalogue is the only network route',async()=>{
  const f=make();assert(f.container.hidden);assert.equal(f.composer.request(),null);
  assert.equal(await f.composer.load(),false);assert.equal(f.calls.length,0);
  f.composer.setEnabled(true);assert(!f.container.hidden);assert.equal(f.container.children[0].open,false);
  assert.throws(()=>f.composer.request(),/Konten/);await f.composer.load();
  assert.equal(f.calls.length,1);assert.equal(f.calls[0].url,'/v1/agent/action-services');
  assert.deepEqual(Object.keys(f.calls[0].options),['signal']);assert(!f.calls[0].options.signal.aborted);
  const actionOptions=f.fields.kind.children.map(n=>n.value);
  assert.deepEqual(actionOptions,['','calendar','gmail','ha','portal']);
  assert(!f.all().some(n=>n.tagName==='BUTTON'&&/versenden|senden/i.test(n.textContent)));
});
test('single calendar account produces exact contract with explicit absolute time',async()=>{
  const f=make();await f.ready();f.calendar();f.change('location','Radladen');
  assert.equal(f.fields.account.children.length,2);assert(f.fields.account.value);
  assert.deepEqual(f.result(),{actions:[{action_id:'a1',service:'calendar',operation:'create',account:calendar.account,
    target:{calendar_id:calendar.resource},payload:{summary:'Werkstatttermin',start:'2026-09-15T08:00:00.000Z',
      end:'2026-09-15T09:00:00.000Z',all_day:false,description:'',location:'Radladen'}}]});
  assert(f.container.textContent.includes('Europe/Berlin'));
});
test('mail is a draft, one recipient, and preserves text without interpreting markup',async()=>{
  const f=make();await f.ready('gmail');f.gmail();
  const body='<img src=x onerror=alert(1)>\nHallo 😀\tWelt';f.change('body',body);
  assert.deepEqual(f.result(),{actions:[{action_id:'a1',service:'gmail',operation:'create_draft',account:gmail.account,
    target:{mailbox:'me',to:'person@example.test',reply_to_message:''},
    payload:{subject:'Rückfrage',body,thread_id:'',in_reply_to:''}}]});
  assert.equal(f.all().filter(n=>n.tagName==='IMG').length,0);
  assert(f.container.textContent.includes('Er versendet ihn nicht.'));
});
test('mail composition is default and binds only the fixed recipient and complete instruction',async()=>{
  const f=make();await f.ready('gmail');
  assert.equal(f.fields.mail_mode.value,'compose');
  assert(f.fields.subject.disabled&&f.fields.body.disabled);assert(!f.fields.instruction.disabled);
  f.change('to','person@example.test');
  const instruction='Bitte freundlich nach dem Termin fragen.\nKeine Zusage erfinden. 😀';
  f.change('instruction',instruction);f.change('subject','hidden old subject');f.change('body','hidden old body');
  assert.deepEqual(f.result(),{actions:[{action_id:'a1',service:'gmail',operation:'compose_draft',account:gmail.account,
    target:{mailbox:'me',to:'person@example.test'},payload:{instruction}}]});
  for(const value of ['', '  ', 'x'.repeat(4001), '\u0000']){
    f.change('instruction',value);assert.throws(()=>f.composer.request(),/Anliegen/);
  }
  f.change('instruction','x'.repeat(4000));assert(f.result());
  f.change('to','a@example.test,b@example.test');assert.throws(()=>f.composer.request(),/Empfänger/);
  f.gmail();assert(f.fields.instruction.disabled);assert(!f.fields.subject.disabled);
  assert.equal(f.result().actions[0].operation,'create_draft');
  f.change('mail_mode','compose');assert.equal(f.result().actions[0].payload.instruction.length,4000);
  const before=f.result();f.composer.setBusy(true);f.change('mail_mode','exact');f.change('instruction','changed');
  assert.deepEqual(f.result(),before);
  f.composer.clear();assert.equal(f.fields.instruction.value,'');assert.equal(f.fields.mail_mode.value,'compose');
});
test('multiple configured accounts need explicit choice; no first-account guess',async()=>{
  const second={...calendar,account:'calendar-work',resource:'work@example.test'};
  const f=make([calendar,second]);await f.ready();f.calendar();
  assert.equal(f.fields.account.value,'');assert.throws(()=>f.composer.request(),/Konto/);
  f.change('account',JSON.stringify(['calendar',second.account,second.resource]));
  assert(f.container.textContent.includes('Verknüpftes Konto ausgewählt.'));
  assert.equal(f.result().actions[0].account,second.account);
  assert.equal(f.result().actions[0].target.calendar_id,second.resource);
  f.change('account','invented');assert.throws(()=>f.composer.request(),/Konto/);
});
test('same account with different calendar resources still needs explicit choice',async()=>{
  const f=make([calendar,{...calendar,resource:'other@example.test'}]);await f.ready();f.calendar();
  assert.equal(f.fields.account.value,'');assert.throws(()=>f.composer.request(),/Konto/);
  const duplicate=make([calendar,{...calendar}]);await duplicate.ready();duplicate.calendar();
  assert.equal(duplicate.fields.account.children.length,2);assert(duplicate.result());
});
test('missing or malformed catalogue refuses requests and exposes no phantom account',async()=>{
  for(const response of [{services:[]},{services:[{service:'calendar',account:'bad<script>',resource:'me'}]},
    {services:[{...gmail,resource:'other'}]},{services:[null]},{services:'calendar'},{},
    {services:Array(101).fill(calendar)}]){
    const f=make();f.answers.push(response);await f.ready();f.calendar();
    assert.throws(()=>f.composer.request(),/Konto|Konten/);
    assert.equal(f.fields.account.value,'');assert.equal(f.fields.account.children.length,1);
    assert(!f.container.textContent.includes('bad<script>'));
  }
  const f=make([{service:'ha',account:'ha-home',resource:'configured_home'}]);await f.ready('gmail');f.gmail();
  assert(f.container.textContent.includes('Kein Mailkonto verbunden'));assert.throws(()=>f.composer.request(),/Konto/);
});
test('disappeared account is not silently replaced with a different singleton',async()=>{
  const f=make([calendar]);await f.ready();f.calendar();const before=f.result();
  f.answers.push({services:[{...calendar,account:'calendar-other'}]});await f.composer.load();
  assert.equal(f.fields.account.value,'');assert.throws(()=>f.composer.request(),/Konto/);
  assert(f.container.textContent.includes('nicht mehr verfügbar'));
  await f.composer.load();assert.deepEqual(f.result(),before,'same bound account can return');
  f.answers.push({services:[{...calendar,account:'calendar-other'}]});await f.composer.load();
  f.change('account',JSON.stringify(['calendar','calendar-other',calendar.resource]));
  assert.equal(f.result().actions[0].account,'calendar-other');
});
test('calendar rejects vague, malformed, impossible and DST-gap local timestamps',async()=>{
  const f=make();await f.ready();f.calendar();
  for(const value of ['morgen um zehn','2026-09-15','2026-09-15T10:00Z','2026-02-30T10:00',
    '2026-13-01T10:00','2026-09-15T24:00','0099-09-15T10:00','2026-03-29T02:30']){
    f.change('start',value);assert.throws(()=>f.composer.request(),/Beginn/);
  }
  f.calendar();f.change('end','2026-09-15T10:00');assert.throws(()=>f.composer.request(),/nach/);
  f.change('end','2026-09-14T12:00');assert.throws(()=>f.composer.request(),/nach/);
  f.change('start','2026-10-25T02:30');f.change('end','2026-10-25T03:30');
  assert.match(f.result().actions[0].payload.start,/Z$/,'overlap is an explicit native Date instant');
});
test('timezone conversion follows actual browser zone; New York DST gap is rejected',async()=>{
  const previous=process.env.TZ;process.env.TZ='America/New_York';
  try{
    const f=make();await f.ready();f.calendar();
    assert.equal(f.result().actions[0].payload.start,'2026-09-15T14:00:00.000Z');
    assert(f.container.textContent.includes('America/New_York'));
    f.change('start','2026-03-08T02:30');assert.throws(()=>f.composer.request(),/Ortszeit/);
  }finally{process.env.TZ=previous;}
});
test('calendar limits and control characters are enforced beyond browser maxlength',async()=>{
  const f=make();await f.ready();f.calendar();
  for(const [name,limit] of [['summary',500],['location',500],['description',4000]]){
    f.change(name,'x'.repeat(limit));assert(f.result());
    f.change(name,'x'.repeat(limit+1));assert.throws(()=>f.composer.request(),/Zeichen/);
    f.change(name,'\u0000');assert.throws(()=>f.composer.request(),/Zeichen/);f.change(name,name==='summary'?'Termin':'');
  }
  f.change('summary','   ');assert.throws(()=>f.composer.request(),/Zeichen/);
  f.change('summary','😀'.repeat(500));assert(f.result());
  f.change('summary','\ud800');assert.throws(()=>f.composer.request(),/Zeichen/);
});
test('mail rejects recipient lists, injected headers, empty and oversized content',async()=>{
  const f=make();await f.ready('gmail');f.gmail();
  for(const recipient of ['a@example.test,b@example.test','Name <a@example.test>','a@example.test\nBcc:b@example.test',
    '','a'.repeat(250)+'@example.test']){
    f.change('to',recipient);assert.throws(()=>f.composer.request(),/Empfänger/);
  }
  f.gmail();
  for(const subject of ['', ' '.repeat(2),'x'.repeat(501),'Hallo\nBcc: evil@example.test','Hallo\rBetreff','\u0000']){
    f.change('subject',subject);assert.throws(()=>f.composer.request(),/Betreff/);
  }
  f.gmail();f.change('subject','s'.repeat(500));f.change('body','b'.repeat(8000));assert(f.result());
  f.change('body','b'.repeat(8001));assert.throws(()=>f.composer.request(),/Nachricht/);
  f.change('body','\t\n');assert.throws(()=>f.composer.request(),/Nachricht/);
});
test('clear invalidates in-flight read and removes local content',async()=>{
  const f=make();f.composer.setEnabled(true);f.change('subject','private text');
  const wait=deferred();f.answers.push(wait.promise);const pending=f.composer.load();
  f.composer.clear();assert(f.calls[0].options.signal.aborted);
  wait.resolve({services:[calendar]});assert.equal(await pending,false);
  assert.equal(f.fields.subject.value,'');assert.equal(f.fields.account.children.length,1);
  assert.throws(()=>f.composer.request(),/Konten/);assert.equal(f.changes(),1);
});
test('disable and re-enable cannot revive data from an older session load',async()=>{
  const f=make();f.composer.setEnabled(true);const wait=deferred();f.answers.push(wait.promise);
  const pending=f.composer.load();f.composer.setEnabled(false);assert(f.container.hidden);
  f.composer.setEnabled(true);await f.composer.load();f.change('kind','gmail');f.gmail();
  const before=f.result(),changes=f.changes();wait.resolve({services:[calendar]});assert.equal(await pending,false);
  assert.deepEqual(f.result(),before);assert.equal(f.changes(),changes);
});
test('only latest catalogue response can paint',async()=>{
  const f=make();f.composer.setEnabled(true);const old=deferred(),fresh=deferred();
  f.answers.push(old.promise,fresh.promise);const first=f.composer.load(),second=f.composer.load();
  fresh.resolve({services:[gmail]});assert.equal(await second,true);f.change('kind','gmail');f.gmail();
  old.resolve({services:[calendar]});assert.equal(await first,false);assert.equal(f.result().actions[0].service,'gmail');
  assert(f.calls[0].options.signal.aborted);
});
test('identity guard purges data and blocks callbacks and further loads',async()=>{
  const f=make();f.composer.setEnabled(true);f.change('subject','private');
  const wait=deferred();f.answers.push(wait.promise);const pending=f.composer.load();
  const changes=f.changes();f.current(false);wait.resolve({services:[calendar]});
  assert.equal(await pending,false);assert(f.container.hidden);assert.equal(f.fields.subject.value,'');
  assert.equal(f.changes(),changes);assert.equal(f.composer.request(),null);
  assert.equal(await f.composer.load(),false);assert.equal(f.calls.length,1);
});
test('busy freezes exact uncertain submission; no edits, callbacks or reload effects',async()=>{
  const f=make();await f.ready();f.calendar();const original=f.result();f.composer.setBusy(true);
  assert(f.all().filter(n=>['INPUT','SELECT','TEXTAREA','BUTTON'].includes(n.tagName)).every(n=>n.disabled));
  const changes=f.changes(),calls=f.calls.length;
  f.change('summary','Changed while uncertain');f.change('kind','gmail');f.change('to','other@example.test');
  assert.deepEqual(f.result(),original);assert.equal(f.changes(),changes);
  assert.equal(await f.composer.load(),false);assert.equal(f.calls.length,calls);
  const output=f.composer.request();output.actions[0].payload.summary='mutated returned object';
  assert.deepEqual(f.result(),original);
  f.composer.clear();assert.throws(()=>f.composer.request(),/nicht verändert/);
  f.composer.setBusy(false);f.composer.setEnabled(false);assert.equal(f.composer.request(),null);
});
test('read failure invalidates an earlier successful account and stays explicit',async()=>{
  const f=make();await f.ready();f.calendar();assert(f.result());
  const wait=deferred();f.answers.push(wait.promise);const pending=f.composer.load();
  assert.throws(()=>f.composer.request(),/Konten/);wait.reject(Error('private server internals'));
  assert.equal(await pending,false);assert.equal(f.fields.account.value,'');
  assert.throws(()=>f.composer.request(),/Konten/);assert(!f.container.textContent.includes('private server internals'));
});
test('devices load only after explicit action choice and never choose a device by its display name',async()=>{
  const f=make([calendar,gmail,ha]);await f.ready('calendar');
  assert.equal(f.calls.length,1);f.change('kind','ha');await settle();
  assert.equal(f.calls.length,2);
  assert.equal(f.calls[1].url,'/v1/agent/action-resources?service=ha&account=ha-home&limit=100');
  assert.equal(f.fields.device.value,'');assert.throws(()=>f.result(),/Gerät/);
  assert.equal(f.fields.device.children.length,3);
  assert(f.fields.device.children[1].textContent.includes('Büro'));
  assert(f.fields.device.children[1].textContent.includes('light.desk'));
  f.change('device','light.desk');assert.throws(()=>f.result(),/Zustand/);
  f.change('desired','on');
  assert.deepEqual(f.result(),{actions:[{action_id:'a1',service:'ha',operation:'set_state',account:ha.account,
    target:{entity_id:'light.desk'},payload:{state:'on'}}]});
  f.change('device','switch.desk');assert.equal(f.fields.desired.value,'');assert.throws(()=>f.result(),/Zustand/);
  f.change('desired','off');assert.equal(f.result().actions[0].target.entity_id,'switch.desk');
  assert.deepEqual(f.result().actions[0].payload,{state:'off'});
});
test('brightness is offered only by an actual light and binds an exact integer',async()=>{
  const f=make([ha]);await f.ready('ha');await settle();f.change('device','light.desk');f.change('desired','brightness');
  for(const value of ['', '-1','101','01','2.5','2e1',' 20']){
    f.change('brightness',value);assert.throws(()=>f.result(),/Helligkeit/);
  }
  for(const value of ['0','43','100']){
    f.change('brightness',value);assert.equal(f.result().actions[0].operation,'set_brightness');
    assert.deepEqual(f.result().actions[0].payload,{brightness_pct:Number(value)});
  }
  f.change('device','switch.desk');assert(!f.fields.desired.children.some(n=>n.value==='brightness'));
  f.change('desired','brightness');assert.throws(()=>f.result(),/Zustand/);
});
test('device refresh holds its exact identity but refuses disappearance or lost operation',async()=>{
  const f=make([ha]);await f.ready('ha');await settle();f.change('device','light.desk');f.change('desired','brightness');f.change('brightness','40');
  const original=f.result();await f.composer.loadDevices();assert.deepEqual(f.result(),original);
  f.resourceAnswers.push(resources([deviceRows[1]]));await f.composer.loadDevices();
  assert.equal(f.fields.device.value,'');assert.throws(()=>f.result(),/Gerät/);
  assert(f.container.textContent.includes('nicht mehr verfügbar'));assert.equal(f.fields.desired.value,'');
  f.resourceAnswers.push(resources());await f.composer.loadDevices();assert.equal(f.fields.device.value,'light.desk');
  assert.throws(()=>f.result(),/Zustand/);f.change('desired','brightness');
  f.resourceAnswers.push(resources([{...deviceRows[0],operations:['set_state']}]));await f.composer.loadDevices();
  assert.equal(f.fields.desired.value,'');assert.throws(()=>f.result(),/Zustand/);
});
test('wrong account malformed device list duplicate identity and read failure never retain a usable list',async()=>{
  for(const response of [resources(deviceRows,'ha-other'),{},resources([deviceRows[0],deviceRows[0]]),
    resources([{...deviceRows[0],target:{entity_id:'light.desk',url:'https://foreign.example'}}]),
    resources([{...deviceRows[1],operations:['set_state','set_brightness']}]),
    resources([{...deviceRows[0],domain:'switch'}]),{...resources(),truncated:'yes'}]){
    const f=make([ha]);await f.ready('ha');await settle();f.change('device','light.desk');f.change('desired','on');assert(f.result());
    f.resourceAnswers.push(response);assert.equal(await f.composer.loadDevices(),false);
    assert.throws(()=>f.result(),/Geräteliste/);assert.equal(f.fields.device.value,'');
  }
  const f=make([ha]);await f.ready('ha');await settle();const wait=deferred();f.resourceAnswers.push(wait.promise);
  const pending=f.composer.loadDevices();wait.reject(Error('secret failure'));
  assert.equal(await pending,false);assert(!f.container.textContent.includes('secret failure'));assert.throws(()=>f.result(),/Geräteliste/);
});
test('late device response cannot cross action account or session changes',async()=>{
  const second={...ha,account:'ha-other'},f=make([ha,second]);await f.ready('ha');
  assert.equal(f.calls.length,1);const wait=deferred();f.resourceAnswers.push(wait.promise);
  f.change('account',JSON.stringify(['ha',ha.account,ha.resource]));const first=f.calls.at(-1);
  f.resourceAnswers.push(resources([{...deviceRows[0],target:{entity_id:'light.other'}}],second.account));
  f.change('account',JSON.stringify(['ha',second.account,second.resource]));await settle();
  assert(first.options.signal.aborted);wait.resolve(resources());await settle();
  assert.equal(f.fields.device.children.length,2);assert.equal(f.fields.device.children[1].value,'light.other');
  const pendingResponse=deferred();f.resourceAnswers.push(pendingResponse.promise);const pending=f.composer.loadDevices();
  f.composer.clear();pendingResponse.resolve(resources(deviceRows,second.account));assert.equal(await pending,false);
  assert.equal(f.fields.device.children.length,1);assert.equal(f.fields.account.value,'');
});
test('truncated empty lists remain honest and device retry freezes the full original effect',async()=>{
  const f=make([ha]);f.resourceAnswers.push({...resources([]),truncated:true});await f.ready('ha');await settle();
  assert(f.container.textContent.includes('Liste ist begrenzt'));assert.throws(()=>f.result(),/Gerät/);
  await f.composer.loadDevices();f.change('device','light.desk');f.change('desired','on');const original=f.result();
  f.composer.setBusy(true);const calls=f.calls.length;
  f.change('device','switch.desk');f.change('desired','off');f.change('kind','gmail');
  assert.deepEqual(f.result(),original);assert.equal(await f.composer.loadDevices(),false);assert.equal(f.calls.length,calls);
  assert(f.fields.device.disabled&&f.fields.desired.disabled&&f.fields.brightness.disabled);
});

test('portal selection only reads existing own sessions and requires an explicit choice',async()=>{
  const f=make();await f.ready();assert.equal(f.calls.length,1);
  f.change('kind','portal');await settle();
  assert.equal(f.calls.length,2);assert.equal(f.calls[1].url,'/v1/agent/action-portal-sessions?limit=50');
  assert(f.calls.every(c=>!c.options.method));assert.throws(()=>f.result(),/Portalverbindung/);
  const option=f.fields.portal_session.children[1];assert(!option.textContent.includes('ps-123-1'));
  f.change('portal_session',option.value);
  assert.deepEqual(f.result(),{actions:[{action_id:'a1',service:'portal',operation:'status',
    account:portalRow.account,target:portalRow.target,payload:{}}]});
});
test('portal refresh loses stale selection and never substitutes another session',async()=>{
  const f=make();await f.ready('portal');await settle();
  f.change('portal_session',f.fields.portal_session.children[1].value);
  f.portalAnswers.push(portalRows([{...portalRow,target:{...portalRow.target,session_id:'ps-123-2'}}]));
  f.all().find(n=>n.tagName==='BUTTON'&&n.textContent==='Verbindungen neu lesen').dispatch('click');await settle();
  assert.equal(f.fields.portal_session.value,'');assert.throws(()=>f.result(),/Portalverbindung/);
  assert(f.container.textContent.includes('nicht mehr verfügbar'));
});
test('portal unconfirmed task keeps the original bound session and account',async()=>{
  const f=make();await f.ready('portal');await settle();
  f.change('portal_session',f.fields.portal_session.children[1].value);const original=f.result();
  f.composer.setBusy(true);const count=f.calls.length;
  f.change('portal_session','');f.all().find(n=>n.tagName==='BUTTON'&&n.textContent==='Verbindungen neu lesen').dispatch('click');await settle();
  assert.deepEqual(f.result(),original);assert.equal(f.calls.length,count);assert(f.fields.portal_session.disabled);
});
test('portal empty invalid or failed catalogues never permit a status task',async()=>{
  for(const reply of [portalRows([]),portalRows([portalRow,portalRow]),portalRows([{...portalRow,authenticated:'yes'}]),
    portalRows([{...portalRow,origin:'https://solvio-studio.de/foreign'}])]){
    const f=make();f.portalAnswers.push(reply);await f.ready('portal');await settle();
    assert.throws(()=>f.result(),/Portalverbindungen|Portalverbindung/);
    assert(f.fields.portal_session.disabled);assert(f.calls.every(c=>!c.options.method));
  }
  const f=make();await f.ready('portal');await settle();f.portalAnswers.push(Promise.resolve(null));
  f.all().find(n=>n.tagName==='BUTTON'&&n.textContent==='Verbindungen neu lesen').dispatch('click');await settle();
  assert.throws(()=>f.result(),/Portalverbindungen/);assert(!f.container.textContent.includes('SOLVIO Studio'));
});
test('late portal catalogue cannot repopulate a disabled or revoked view',async()=>{
  for(const revoke of [false,true]){
    const f=make(),late=deferred();f.portalAnswers.push(late.promise);await f.ready('portal');
    if(revoke)f.current(false);else f.composer.setEnabled(false);
    late.resolve(portalRows());await settle();
    assert.equal(f.result(),null);assert.equal(f.fields.portal_session.children.length,1);
    assert(!f.container.textContent.includes('SOLVIO Studio'));
  }
});

(async()=>{
  const previous=process.env.TZ;process.env.TZ='Europe/Berlin';let passed=0;
  try{for(const {name,run} of tests){await run();passed++;console.log(`PASS ${name}`);}}
  finally{if(previous===undefined)delete process.env.TZ;else process.env.TZ=previous;}
  console.log(`${passed}/${tests.length} action composer boundaries passed (DOM fixture only, no browser or service calls)`);
})().catch(error=>{console.error(error);process.exitCode=1;});
