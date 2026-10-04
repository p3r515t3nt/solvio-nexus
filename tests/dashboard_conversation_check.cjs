/* Real dashboard controller, chat module and action-answer module, with local
 * DOM/HTTP/storage ports. No browser, network, model, microphone, or
 * production state; the C3 conversation routes are local stand-ins. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const assets=path.resolve(process.argv[2]||path.join(__dirname,'../src/solvio/dashboard/assets'));
const plain=value=>JSON.parse(JSON.stringify(value));
// Digests run on the real WebCrypto threadpool, so flushing needs macrotasks too.
const flush=async()=>{for(let n=0;n<5;n++){await new Promise(resolve=>setTimeout(resolve,1));for(let m=0;m<40;m++)await Promise.resolve();}};
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};

class Events {
  constructor(){this.listeners=new Map();}
  addEventListener(type,fn){if(!this.listeners.has(type))this.listeners.set(type,new Set());this.listeners.get(type).add(fn);}
  removeEventListener(type,fn){this.listeners.get(type)?.delete(fn);}
  dispatchEvent(event){event.target??=this;event.preventDefault??=()=>{};for(const fn of this.listeners.get(event.type)||[])fn(event);}
}
class Element extends Events {
  constructor(tag,doc){super();this.tag=tag;this.ownerDocument=doc;this.children=[];this.parentElement=null;
    this.attrs=new Map();this.dataset={};this._text='';this.className='';this.hidden=false;this.disabled=false;
    this.open=false;this.value='';this.files=[];this.style={};
    this.classList={toggle:(name,on)=>{const names=new Set(this.className.split(/\s+/).filter(Boolean));
      const chosen=on??!names.has(name);if(chosen)names.add(name);else names.delete(name);this.className=[...names].join(' ');return chosen;}};
  }
  get id(){return this.attrs.get('id')||'';}set id(value){this.attrs.set('id',value);}
  get src(){return this.attrs.get('src')||'';}set src(value){this.attrs.set('src',String(value));}
  get textContent(){return this._text+this.children.map(n=>n.textContent).join('');}
  set textContent(value){this.replaceChildren();this._text=String(value);}
  append(...nodes){for(const node of nodes){node.remove();node.parentElement=this;this.children.push(node);}}
  replaceChildren(...nodes){for(const n of this.children)n.parentElement=null;this.children=[];this._text='';this.append(...nodes);}
  remove(){if(this.parentElement){const list=this.parentElement.children;list.splice(list.indexOf(this),1);this.parentElement=null;}}
  contains(node){return this===node||this.children.some(n=>n.contains(node));}
  get isConnected(){return this.ownerDocument.root.contains(this);}
  get firstChild(){return this.children[0]||null;}get lastChild(){return this.children.at(-1)||null;}
  get options(){return this.children.filter(n=>n.tag==='option');}
  get elements(){return this.querySelectorAll('input,textarea,select,button');}
  setAttribute(name,value){this.attrs.set(name,String(value));if(name==='class')this.className=String(value);
    if(['hidden','disabled','open'].includes(name))this[name]=true;
    if(name==='value')this.value=String(value);
    if(name.startsWith('data-'))this.dataset[name.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]=String(value);}
  getAttribute(name){if(name.startsWith('data-'))return this.dataset[name.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]??null;
    return name==='class'?this.className:(this.attrs.get(name)??null);}
  hasAttribute(name){return ['hidden','disabled','open'].includes(name)?this[name]:this.getAttribute(name)!==null;}
  removeAttribute(name){this.attrs.delete(name);if(['hidden','disabled','open'].includes(name))this[name]=false;}
  matches(selector){
    const excluded=selector.match(/:not\(([^)]+)\)/);if(excluded&&this.matches(excluded[1]))return false;
    selector=selector.replace(/:not\([^)]+\)/g,'');
    const attrs=[...selector.matchAll(/\[([^\]=]+)(?:=["']?([^\]"']+)["']?)?\]/g)];
    if(attrs.some(([,key,value])=>!this.hasAttribute(key)||(value!==undefined&&this.getAttribute(key)!==value)))return false;
    const basic=selector.replace(/\[[^\]]+\]/g,''),id=basic.match(/#([\w-]+)/),cls=[...basic.matchAll(/\.([\w-]+)/g)];
    if(id&&this.id!==id[1]||cls.some(m=>!this.className.split(/\s+/).includes(m[1])))return false;
    const tag=basic.match(/^[a-z][\w-]*/i);return !tag||this.tag===tag[0];
  }
  querySelectorAll(selector){const selectors=selector.split(',').map(s=>s.trim());
    return this.children.flatMap(n=>[...(selectors.some(s=>n.matches(s))?[n]:[]),...n.querySelectorAll(selector)]);}
  querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
  focus(){this.ownerDocument.activeElement=this;}
  blur(){if(this.ownerDocument.activeElement===this)this.ownerDocument.activeElement=null;}
  scrollIntoView(options){this.lastScroll=options;}
  reportValidity(){return !this.required||!!this.value;}
  click(){if(!this.disabled)this.dispatchEvent({type:'click'});}
  showModal(){this.open=true;}close(){this.open=false;this.dispatchEvent({type:'close'});}
  pause(){this.paused=true;}load(){}
}
function documentFromHtml(html){
  const doc=new Events();doc.hidden=false;doc.activeElement=null;
  doc.createElement=tag=>new Element(tag,doc);doc.root=doc.createElement('document');
  doc.getElementById=id=>doc.root.querySelector('#'+id);doc.querySelectorAll=s=>doc.root.querySelectorAll(s);
  const stack=[doc.root],voids=new Set(['area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr']);
  for(const token of html.match(/<!--[\s\S]*?-->|<![^>]*>|<[^>]+>|[^<]+/g)||[]){
    if(token.startsWith('<!'))continue;
    if(token.startsWith('</')){const tag=token.slice(2).match(/^[\w-]+/)?.[0];let index=stack.length-1;
      while(index>0&&stack[index].tag!==tag)index--;if(index>0)stack.length=index;continue;}
    if(!token.startsWith('<')){stack.at(-1)._text+=token.replace(/&amp;/g,'&').replace(/&gt;/g,'>');continue;}
    const tag=token.match(/^<([\w-]+)/)?.[1];if(!tag)continue;const node=doc.createElement(tag);
    const attrs=token.slice(tag.length+1).replace(/\/?\s*>$/,'');
    for(const match of attrs.matchAll(/([^\s=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s]+)))?/g))node.setAttribute(match[1],match[2]??match[3]??match[4]??'');
    stack.at(-1).append(node);if(!voids.has(tag)&&!token.endsWith('/>'))stack.push(node);
  }
  for(const select of doc.querySelectorAll('select'))select.value=(select.options.find(o=>o.hasAttribute('selected'))||select.options[0])?.value||'';
  return doc;
}
const A='ar-1111111111111111',B='ar-2222222222222222';
const CH1='c-1111111111111111',CH2='c-2222222222222222';
function run(id=A,extra={}){return {id,aufgabe:'at-'+id.slice(3),auftrag:id===A?'Termine vergleichen':'Zweiter eigener Auftrag',
  zustand:'Fertig',zustand_code:'SUCCEEDED',offen:false,ergebnis:'Ein bestätigtes Ergebnis für '+id,
  kosten:{configured:true,counts:{unknown:0},ai_tool:{spent_cents:0,reserved_cents:0},ask_threshold_cents:1000},
  verlauf:[{zeit:1,text:'SUCCEEDED'}],quellen:['https://example.invalid/source'],...extra};}
function conversation(id=CH1,extra={}){return {conversation_id:id,title:id===CH1?'Hotel in Hamburg':'Zweiter Chat',kind:'text',
  last_activity_at:1,message_count:0,open_task_count:0,open_delivery_count:0,...extra};}
function chatDetail(id=CH1,{messages=[],auftraege=[],deliveries_open=0,...extra}={}){
  return {conversation:conversation(id,extra),messages,auftraege,deliveries_open};}
let deliverySerial=0;
function delivery(extra={}){return {delivery_id:'cd-'+String(++deliverySerial).padStart(16,'0'),status:'completed',error_code:'',task_id:'',run_id:'',revision:0,...extra};}
function userMessage(sequence,text,extra={}){return {message_id:'m-'+String(sequence).padStart(16,'0'),sequence,role:'user',text,created_at:sequence,...extra};}
function answer(sequence,text){return {message_id:'m-'+String(sequence).padStart(16,'0'),sequence,role:'assistant',text,created_at:sequence};}
function response(value,status=200){return {status,ok:status>=200&&status<300,json:async()=>plain(value)};}
function storageStore(seed={}){
  const map=new Map(Object.entries(seed).map(([k,v])=>[k,typeof v==='string'?v:JSON.stringify(v)]));
  return {getItem:k=>map.has(k)?map.get(k):null,setItem(k,v){map.set(k,String(v));},removeItem(k){map.delete(k);},
    key:i=>[...map.keys()][i]??null,get length(){return map.size;},snapshot:()=>Object.fromEntries(map)};
}
const webcrypto=require('node:crypto').webcrypto;
function sha256(text){return require('node:crypto').createHash('sha256').update(text,'utf8').digest('hex');}
function canonicalText(value){
  if(value===null||typeof value!=='object')return JSON.stringify(value);
  if(Array.isArray(value))return '['+value.map(canonicalText).join(',')+']';
  return '{'+Object.keys(value).sort().map(k=>JSON.stringify(k)+':'+canonicalText(value[k])).join(',')+'}';
}

async function world({chats=[chatDetail(CH1),chatDetail(CH2)],storage={}}={}){
  const document=documentFromHtml(fs.readFileSync(path.join(assets,'index.html'),'utf8')),window=new Events();
  const requests=[],voices=[],shares=[],timers=new Map(),runs=new Map([[A,run()],[B,run(B)]]);
  const conversations=new Map(chats.map(c=>[c.conversation.conversation_id,c])),creations=new Map();
  let responder=null,id=0,serial=0,created=0;
  const session={session_id:'temporary-owner-session',csrf_token:'fixture-only-csrf'};
  const localStorage=storageStore(storage);
  async function fetch(url,options={}){
    const req={url:String(url),method:options.method||'GET',body:options.body?JSON.parse(options.body):undefined,storage:localStorage.snapshot()};requests.push(req);
    if(responder){const result=await responder(req);if(result!==undefined)return result;}
    if(req.url==='/v1/browser/session')return response(session);
    if(req.url==='/v1/dashboard/state')return response({environment:'isolated_test',runtime:'available',repositories:[{name:'Temporary repo',path:'/tmp/fixture-repo'}],components:[],stand:1});
    if(req.url==='/v1/agent/runs'||req.url==='/v1/agent/runs?view=tasks')return response({laeufe:[...runs.values()]});
    if(req.url==='/v1/agent/approvals')return response({approvals:[]});
    if(req.url==='/v1/control/audio')return response({scope:'No audio in fixture',devices:[]});
    if(req.url==='/v1/control/inbox')return response({meldungen:[],ungelesen:0,stand:1});
    if(req.url==='/v1/control/activity')return response({ereignisse:[],stand:1});
    if(req.url==='/v1/control/tasks')return response({aufgaben:[],stand:1});
    if(req.url==='/v1/memory/memories?limit=100')return response({memories:[],total:0});
    if(req.url==='/v1/memory/candidates')return response({candidates:[]});
    if(req.url==='/v1/memory/commands')return response({commands:[]});
    const match=req.url.match(/^\/v1\/agent\/runs\/(ar-[a-f0-9]{16})$/);
    if(match)return response(runs.get(match[1])||{error:'not_found'},runs.has(match[1])?200:404);
    // Local stand-ins for the C3 conversation routes (§2.2): same paths, bodies and codes.
    if(req.url==='/v1/conversations?limit=30'&&req.method==='GET')return response({conversations:[...conversations.values()].map(c=>c.conversation)});
    if(req.url==='/v1/conversations'&&req.method==='POST'){
      const key=req.body.client_request_id;
      if(creations.has(key))return response({conversation_id:creations.get(key),title:'',kind:'text',created_at:1},200);
      const cid='c-'+String(++created+2).padStart(16,'0');creations.set(key,cid);
      conversations.set(cid,chatDetail(cid,{title:'',message_count:0}));
      return response({conversation_id:cid,title:'',kind:'text',created_at:1},201);
    }
    const one=req.url.match(/^\/v1\/conversations\/(c-[a-f0-9]{16})$/);
    if(one&&req.method==='GET')return conversations.has(one[1])?response(conversations.get(one[1])):response({error:'unknown_conversation'},404);
    if(one&&req.method==='PATCH'){const c=conversations.get(one[1]);if(!c)return response({error:'unknown_conversation'},404);c.conversation.title=req.body.title;return response({conversation:c.conversation});}
    const post=req.url.match(/^\/v1\/conversations\/(c-[a-f0-9]{16})\/messages$/);
    if(post&&req.method==='POST'){
      const c=conversations.get(post[1]);if(!c)return response({error:'unknown_conversation'},404);
      const m=req.body.message;if(m.conversation_id!==post[1]||!m.text)return response({error:'invalid_message'},400);
      const existing=c.messages.find(x=>x.delivery?.client_message_id===m.client_message_id);
      if(existing)return response({delivery_id:existing.delivery.delivery_id,status:existing.delivery.status,message_id:existing.message_id},202);
      const sequence=c.messages.length+1,d=delivery({status:'accepted',client_message_id:m.client_message_id});
      c.messages.push(userMessage(sequence,m.text,{delivery:d}));c.deliveries_open=1;c.conversation.message_count=sequence;
      return response({delivery_id:d.delivery_id,status:'accepted',message_id:'m-'+String(sequence).padStart(16,'0')},202);
    }
    const state=req.url.match(/^\/v1\/conversations\/(c-[a-f0-9]{16})\/deliveries\/(cd-[a-f0-9]{16})$/);
    if(state&&req.method==='GET'){
      const c=conversations.get(state[1]),m=c?.messages.find(x=>x.delivery?.delivery_id===state[2]);
      return m?response(m.delivery):response({error:'unknown_delivery'},404);
    }
    throw Error('Unconfigured local HTTP port: '+req.method+' '+req.url);
  }
  class Presence{setState(){}setLevel(){}stop(){}}
  class WindowShare{constructor(){this.stops=0;shares.push(this);}stop(){this.stops++;}}
  class BrowserVoice{
    constructor(root,options){this.root=root;this.options=options;this.active=false;this.ends=0;voices.push(this);}
    setAvailable(value){this.available=value;}end(){this.ends++;this.active=false;}
    emit(value){this.active=!!value.active;this.closing=!!value.closing;this.options.onState(value);}
    async ticket(){const body=await this.options.prepareStart?.()||{};return this.options.api('/v1/browser/voice/session',{method:'POST',body});}
    async endAndWait(){return true;}
    isHandoffBlocked(){return false;}
  }
  const composer={setEnabled(){},clear(){},setBusy(){},request(){return this.contract;},load:async()=>{},contract:{actions:[{action_id:'draft',service:'gmail',operation:'compose_draft',account:'bound-account',target:{mailbox:'me',to:'owner@example.invalid'},payload:{instruction:'Schreibe einen Entwurf.'}}]}};
  const context=vm.createContext({document,window,console,URL,AbortController,AbortSignal,TextDecoder,TextEncoder,Uint8Array,localStorage,
    crypto:{randomUUID:()=>`fixture-request-${++serial}`,subtle:webcrypto.subtle},location:{origin:'https://127.0.0.1',reload(){}},
    navigator:{},fetch,Presence,WindowShare,BrowserVoice,createActionComposer:()=>composer,
    Option:function(text,value){const n=document.createElement('option');n.textContent=text;n.value=value;return n;},
    btoa:value=>Buffer.from(value,'binary').toString('base64'),
    setTimeout:(fn,ms)=>{timers.set(++id,{fn,ms});return id;},clearTimeout:i=>timers.delete(i),
    setInterval:(fn,ms)=>{timers.set(++id,{fn,ms});return id;},clearInterval:i=>timers.delete(i)});
  const load=(name,transform)=>vm.runInContext(transform(fs.readFileSync(path.join(assets,name),'utf8')),context,{filename:name});
  load('action-intent.js',src=>src.replace('export function createActionIntent','function createActionIntent'));
  load('canonical.js',src=>src.replace(/^export /gm,''));
  load('chat.js',src=>src.replace(/^import .*;\n/gm,'').replace('export function createChat','function createChat'));
  const source=fs.readFileSync(path.join(assets,'app.js'),'utf8');
  assert.equal((source.match(/^import .*;$/gm)||[]).length,6,'only dependency ports are replaced');
  await vm.runInContext('(async()=>{'+source.replace(/^import .*;\n/gm,'')+
    '\nglobalThis.controller={state,showView,selectRun,renderDetail,renderRuns,showSession,setConnected,refresh,transmitTask,actionIntent,syncVoiceMount,openDecision,chat};})()',context,{filename:'app.js'});
  await flush();
  return {document,window,requests,runs,conversations,session,voices,shares,composer,timers,localStorage,app:context.controller,
    $:id=>document.getElementById(id),respond:fn=>{responder=fn;},flush,
    async submit(text,scope='research'){this.$('objective').value=text;this.$('scope').value=scope;
      this.$('task-form').dispatchEvent({type:'submit'});await flush();},
    async send(text){this.$('message-text').value=text;this.$('message-form').dispatchEvent({type:'submit'});await flush();},
    chatTimers(){return [...this.timers.values()].filter(t=>t.ms!==12000&&t.ms!==10000);},
    async fireChatTimer(){const [entry]=[...this.timers.entries()].filter(([,t])=>t.ms!==12000&&t.ms!==10000);assert(entry,'a chat poll timer exists');
      this.timers.delete(entry[0]);await entry[1].fn();await flush();}};
}
// Element-valued assert.equal would render the cyclic DOM port on failure; compare identity with assert().
function mutations(w){return w.requests.filter(r=>r.method!=='GET');}
function outsideDetails(node,boundary){for(let n=node.parentElement;n&&n!==boundary;n=n.parentElement)if(n.tag==='details')return false;return true;}
const C='ar-3333333333333333',revisionDigest='a'.repeat(64);
function finalFollowup(extra={}){return run(A,{task_revision:{revision:1,digest:revisionDigest,text:run().auftrag,parent_run_id:''},
  followup:{eligible:true,reason:''},...extra});}
function followed(extra={}){return run(C,{aufgabe:run().aufgabe,auftrag:run().auftrag,zustand:'Angenommen',zustand_code:'CREATED',offen:true,
  task_revision:{revision:2,digest:'b'.repeat(64),text:'Bitte ergänze September.',parent_run_id:A},followup:{eligible:false,reason:'active'},
  task_history:[{run_id:A,revision:1,text:run().auftrag,state:'SUCCEEDED',result_summary:'Das erste Ergebnis.'},
    {run_id:C,revision:2,text:'Bitte ergänze September.',state:'CREATED',result_summary:''}],...extra});}
function followupReceipt(extra={}){return {task_id:run().aufgabe,run_id:C,parent_run_id:A,revision:2,digest:'b'.repeat(64),annahme:'ready',...extra};}
function fileDescriptor(id='aa-1111111111111111',name='Vergleich.xlsx',size=512,runId=A){const base=`/v1/agent/runs/${runId}/artifacts/${id}`;
  return {id,name,mime_type:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',size,sha256:'c'.repeat(64),download_url:base+'/download',preview_kind:'none'};}
function sendFollowup(w,text='Bitte ergänze September.'){
  w.$('followup-text').value=text;w.$('followup-form').dispatchEvent({type:'submit'});
}
function selectChat(w,id){const row=w.$('chat-list').querySelectorAll('button').find(b=>b.dataset.conversationId===id);assert(row,'chat row '+id);row.click();return flush();}
function pendingRows(w){return JSON.parse(w.localStorage.getItem('solvio.chat.pending.v1')||'[]');}
// Real BrowserVoice with inert audio/socket ports: protocol events are explicit,
// no microphone, provider, server or timer is started by these checks.
function voiceWorld({prepareStart=async()=>({conversation_id:CH1}),protocol=1}={}){
  const document=documentFromHtml(fs.readFileSync(path.join(assets,'index.html'),'utf8'));
  const timers=new Map(),requests=[],sockets=[];let micCalls=0,serial=0;
  const track={readyState:'live',enabled:true,addEventListener(){},stop(){this.readyState='ended';}};
  class Socket{static OPEN=1;constructor(){this.readyState=1;this.bufferedAmount=0;this.sent=[];sockets.push(this);}send(m){this.sent.push(JSON.parse(m));}close(){this.readyState=3;this.onclose?.();}}
  class AudioContext{constructor(){this.audioWorklet={addModule:async()=>{}};}async resume(){}async close(){}createMediaStreamSource(){return {connect(){},disconnect(){}};}}
  class AudioWorkletNode{constructor(){this.port={postMessage(){},close(){}};}connect(){}disconnect(){}}
  const context=vm.createContext({console,document,window:{AudioWorkletNode},AudioContext,AudioWorkletNode,WebSocket:Socket,
    navigator:{mediaDevices:{getUserMedia:async()=>{micCalls++;return {getTracks:()=>[track],getAudioTracks:()=>[track]};}}},
    location:{href:'https://fixture.invalid/dashboard/',protocol:'https:'},URL,AbortController,
    setTimeout:fn=>{timers.set(++serial,fn);return serial;},clearTimeout:id=>timers.delete(id)});
  vm.runInContext(fs.readFileSync(path.join(assets,'browser-voice.js'),'utf8').replace('export class BrowserVoice','globalThis.BrowserVoice=class BrowserVoice'),context);
  const voice=new context.BrowserVoice(document.getElementById('browser-voice'),{getSession:()=>({}),prepareStart,
    api:async(url,opts)=>{requests.push({url,body:plain(opts.body)});return {nonce:'once',websocket_path:'/v1/browser/voice',protocol_version:1,audio:{encoding:'pcm_s16le',sample_rate:16000,channels:1}};}});
  // Session object identity is stable, just like the actual authenticated dashboard.
  const session={};voice.getSession=()=>session;voice.setAvailable(true);
  const send=m=>voice.message(JSON.stringify(m));
  return {voice,requests,sockets,timers,track,get micCalls(){return micCalls;},
    ready(){sockets.at(-1).onopen();send({type:'session_authenticated',session_id:'voice-1',connection_id:'browser:one',protocol_version:1});send({type:'session_ready',session_id:'voice-1',connection_id:'browser:one',generation:1,conversation_id:CH1,handoff_protocol:protocol});},
    closed(extra={}){send({type:'session_closed',session_id:'voice-1',connection_id:'browser:one',generation:1,provider:'closed_confirmed',previous_unconfirmed:0,...extra});},
    flushed(extra={}){send({type:'conversation_flushed',session_id:'voice-1',conversation_id:CH1,status:'complete',provider_closed:true,...extra});}}
}
const cases={
  async inbox_detail_reads_saved_findings_as_text_without_acknowledging_or_starting_work(){
    const w=await world(),id='synthetic-inbox-a';
    w.respond(req=>req.url==='/v1/control/inbox'?response({meldungen:[{id,zusammenfassung:'Dein Tagesüberblick.',quelle:'Tagesüberblick',zeit:1}]}):
      req.url==='/v1/control/inbox/'+id?response({id,zusammenfassung:'Dein Tagesüberblick.',quelle:'Tagesüberblick',zeit:1,
        befunde:['Termine heute: 1.','<b>Nur synthetisches Material</b>','Offene SOLVIO-Aufträge: 2.'],herkunft:'aus einer fremden Quelle'}):undefined);
    await w.app.showView('feed');
    assert(!w.$('feed-list').textContent.includes('Termine heute'));
    w.$('feed-list').querySelectorAll('button').find(b=>b.textContent==='Hinweis ansehen').click();await flush();
    assert.equal(w.$('decision-dialog').open,true);assert.match(w.$('decision-content').textContent,/Termine heute: 1/);
    assert.match(w.$('decision-content').textContent,/<b>Nur synthetisches Material<\/b>/);
    assert.equal(w.$('decision-content').querySelector('b'),null);
    assert.match(w.$('decision-content').textContent,/aus einer fremden Quelle/);
    assert.match(w.$('feed-list').textContent,/Dein Tagesüberblick/);
    assert.equal(mutations(w).length,0);
  },
  async inbox_detail_rejects_a_different_item_and_retries_without_losing_the_list(){
    const w=await world(),id='synthetic-inbox-a';let correct=false;
    w.respond(req=>req.url==='/v1/control/inbox'?response({meldungen:[{id,zusammenfassung:'Gespeicherter Hinweis',quelle:'Lokal',zeit:1}]}):
      req.url==='/v1/control/inbox/'+id?response({id:correct?id:'different-item',zusammenfassung:'Einzelheiten',befunde:['Bestätigter synthetischer Befund']}):undefined);
    await w.app.showView('inbox');w.$('inbox-list').querySelector('button').click();await flush();
    assert.match(w.$('decision-content').textContent,/nicht erreichbar/);
    assert(!w.$('decision-content').textContent.includes('Bestätigter synthetischer Befund'));
    assert.match(w.$('inbox-list').textContent,/Gespeicherter Hinweis/);
    correct=true;w.$('decision-content').querySelector('button').click();await flush();
    assert.match(w.$('decision-content').textContent,/Bestätigter synthetischer Befund/);assert.equal(mutations(w).length,0);
  },
  async inbox_detail_discards_closed_replaced_and_logged_out_replies(){
    const w=await world(),first='synthetic-inbox-a',second='synthetic-inbox-b',old=deferred(),late=deferred();let pending=old;
    w.respond(req=>req.url==='/v1/control/inbox'?response({meldungen:[first,second].map(id=>({id,zusammenfassung:id,zeit:1}))}):
      req.url==='/v1/control/inbox/'+first?pending.promise:
      req.url==='/v1/control/inbox/'+second?response({id:second,zusammenfassung:'Zweiter Hinweis',befunde:['Aktueller zweiter Inhalt']}):undefined);
    await w.app.showView('inbox');
    const buttons=w.$('inbox-list').querySelectorAll('button').filter(b=>b.textContent==='Hinweis ansehen');
    buttons[0].click();await flush();w.$('decision-dialog').close();buttons[1].click();await flush();
    old.resolve(response({id:first,zusammenfassung:'Alter Hinweis',befunde:['Veralteter erster Inhalt']}));await flush();
    assert.match(w.$('decision-content').textContent,/Aktueller zweiter Inhalt/);
    assert(!w.$('decision-content').textContent.includes('Veralteter erster Inhalt'));
    w.$('decision-dialog').close();pending=late;buttons[0].click();await flush();w.app.showSession(null);
    late.resolve(response({id:first,zusammenfassung:'Nach Abmelden',befunde:['Verspäteter privater Inhalt']}));await flush();
    assert.equal(w.$('decision-dialog').open,false);assert.equal(w.$('decision-content').textContent,'');
    assert.equal(w.$('inbox-list').textContent,'');assert.equal(mutations(w).length,0);
  },
  async activity_menu_reads_the_existing_bounded_chronicle_and_states_its_scope(){
    const w=await world();
    w.respond(req=>req.url==='/v1/control/activity'?response({ereignisse:[{zeit:1,titel:'<b>Geplante Aufgabe gelaufen</b>',detail:'Synthetische Chronik',art:'run',ton:'gut'}],stand:2}):undefined);
    w.document.querySelectorAll('[data-view]').find(b=>b.dataset.view==='activity').click();await flush();
    assert.equal(w.app.state.view,'activity');assert.equal(w.$('activity-view').hidden,false);
    assert.match(w.$('activity-view').textContent,/Agentenaufträge findest du unter Aufgaben/);
    assert.match(w.$('activity-list').textContent,/<b>Geplante Aufgabe gelaufen<\/b>/);
    assert.equal(w.$('activity-list').querySelector('b'),null);
    assert(w.requests.some(r=>r.url==='/v1/control/activity'&&r.method==='GET'));
    assert(!w.requests.some(r=>r.url.includes('/system')||r.url.includes('/diagnose')));
    assert.equal(mutations(w).length,0);
  },
  async activity_refresh_failure_keeps_the_last_read_chronicle_and_can_recover(){
    const w=await world();let failed=false;
    w.respond(req=>req.url==='/v1/control/activity'?(failed?response({error:'unavailable'},503):
      response({ereignisse:[{zeit:1,titel:'Gespeichertes Ereignis'}],stand:2})):undefined);
    await w.app.showView('activity');const card=w.$('activity-list').children[0];
    failed=true;w.$('activity-refresh').click();await flush();
    assert(w.$('activity-list').children[0]===card);assert.match(w.$('activity-status').textContent,/nicht gelesen/);
    assert.equal(w.$('activity-refresh').disabled,false);
    failed=false;w.$('activity-refresh').click();await flush();assert.match(w.$('activity-status').textContent,/Core gelesen/);
    assert.equal(mutations(w).length,0);
  },
  async recurring_tasks_show_schedule_and_saved_run_details_without_scheduler_control(){
    const w=await world(),id='synthetic-plan-a';
    const task={id,titel:'Synthetischer Tagesüberblick',was:'Tagesüberblick',wann:'Täglich um 08:00',aktiv:true,
      zustand:'wartet',naechster_lauf:100,letzter_lauf:1,letzter_fehler:'Abfrage nicht bestätigt'};
    w.respond(req=>req.url==='/v1/control/tasks'?response({aufgaben:[task],stand:2}):
      req.url==='/v1/control/tasks/'+id?response({...task,auftrag:'<b>Mein ursprünglicher Entwurf</b>',
        laeufe:[{zeit:1,zustand:'failed',detail:'Synthetische Verbindung fehlt'}]}):undefined);
    w.$('message-text').value='Ungesendeter Chatentwurf';await w.app.showView('tasks');
    assert.match(w.$('recurring-list').textContent,/Täglich um 08:00/);
    assert.match(w.$('recurring-list').textContent,/Nächster Lauf/);
    w.$('recurring-list').querySelector('button').click();await flush();
    assert.match(w.$('decision-content').textContent,/Mein ursprünglicher Entwurf/);
    assert.equal(w.$('decision-content').querySelector('b'),null);
    assert.match(w.$('decision-content').textContent,/Synthetische Verbindung fehlt/);
    assert.equal(w.$('message-text').value,'Ungesendeter Chatentwurf');assert.equal(mutations(w).length,0);
    assert.equal(w.$('decision-content').querySelectorAll('button').length,0);
  },
  async recurring_list_rejects_superseded_reads_and_erases_late_logout_results(){
    const w=await world(),old=deferred(),late=deferred();let mode='old';
    w.respond(req=>req.url==='/v1/control/tasks'?(mode==='old'?old.promise:mode==='late'?late.promise:
      response({aufgaben:[{id:'fresh-plan',titel:'Frisch gelesener Plan'}],stand:2})):undefined);
    const previous=w.app.showView('tasks');await flush();await w.app.showView('overview');
    mode='fresh';await w.app.showView('tasks');
    old.resolve(response({aufgaben:[{id:'old-plan',titel:'Überholter Plan'}],stand:1}));await previous;
    assert.match(w.$('recurring-list').textContent,/Frisch gelesener Plan/);
    assert(!w.$('recurring-list').textContent.includes('Überholter Plan'));
    mode='late';w.$('recurring-refresh').click();await flush();w.app.showSession(null);
    late.resolve(response({aufgaben:[{id:'late-plan',titel:'Plan nach Abmelden'}],stand:3}));await flush();
    assert.equal(w.$('recurring-list').textContent,'');assert.equal(w.$('recurring-status').textContent,'');
    assert.equal(mutations(w).length,0);
  },
  async recurring_partial_failure_preserves_agent_runs_and_prior_plan_rows(){
    const w=await world();let failed=false;
    w.respond(req=>req.url==='/v1/control/tasks'?(failed?response({error:'unavailable'},503):
      response({aufgaben:[{id:'synthetic-plan',titel:'Bestehender Plan'}],stand:2})):undefined);
    await w.app.showView('tasks');const plans=w.$('recurring-list').textContent,runs=w.$('task-list').textContent;
    failed=true;w.$('recurring-refresh').click();await flush();
    assert.equal(w.$('recurring-list').textContent,plans);assert.equal(w.$('task-list').textContent,runs);
    assert.match(w.$('recurring-status').textContent,/nicht gelesen/);
    assert(!w.requests.some(r=>/\/(run_now|pause|resume|delete)$/.test(r.url)));assert.equal(mutations(w).length,0);
  },
  async room_history_is_readable_and_keeps_private_draft_out_of_room(){
    const w=await world();
    w.respond(req=>req.url==='/v1/conversations/'+CH1?response(chatDetail(CH1,{read_only:true,messages:[userMessage(1,'Gesprochen im Raum')]})):undefined);
    await selectChat(w,CH1);
    assert.match(w.$('chat-messages').textContent,/Gesprochen im Raum/);
    assert.match(w.$('chat-messages').textContent,/Im Raum/);
    assert.match(w.$('chat-hint').textContent,/Neuer privater Chat/);
    assert.equal(w.$('chat').dataset.readOnly,'true');
    assert.equal(w.$('message-submit').disabled,true);
    assert.equal(w.$('voice-start').disabled,true);
    await w.send('Privater Entwurf');
    assert.equal(mutations(w).length,0);
    assert.equal(w.$('message-text').value,'Privater Entwurf');
    await assert.rejects(w.app.chat.ensureSelected());
    await selectChat(w,CH2);
    assert.equal(w.$('chat').dataset.readOnly,'false');
    assert.equal(w.$('message-submit').disabled,false);
    assert.equal(w.$('voice-start').disabled,false);
    assert.equal(w.$('message-text').value,'Privater Entwurf');
  },

  async assistant_tabs_and_avatar_preserve_chat_without_starting_work(){
    const w=await world();await selectChat(w,CH1);w.$('message-text').value='Noch nicht gesendeter Entwurf';
    const nav=w.document.querySelectorAll('[aria-label="Hauptbereiche"]')[0];
    assert.deepEqual(nav.querySelectorAll('button').map(n=>n.textContent),['Chat','Überblick','Ideen','Aufgaben','Bibliothek']);
    w.document.querySelectorAll('[data-view="connections"]')[0].click();await flush();
    assert.equal(w.$('connections-view').hidden,false);assert.equal(w.app.chat.selectedId(),CH1);
    nav.querySelector('[data-view="tasks"]').click();await flush();assert.equal(w.$('tasks-view').hidden,false);
    nav.querySelector('[data-view="overview"]').click();await flush();
    assert.equal(w.app.chat.selectedId(),CH1);assert.equal(w.$('message-text').value,'Noch nicht gesendeter Entwurf');
    w.$('presence-work').click();await flush();assert.equal(w.$('tasks-view').hidden,false);
    assert.equal(mutations(w).length,0);assert.equal(w.voices[0].active,false);
  },
  async ideas_prepare_without_requests_and_preserve_existing_draft(){
    const w=await world();await selectChat(w,CH1);await w.app.showView('ideas');
    const before=mutations(w).length;
    const focused=w.$('ideas-list').querySelector('button');focused.focus();w.app.setConnected(true);
    assert(w.$('ideas-list').querySelector('button')===focused,'Polling preserves keyboard focus on general suggestions');
    focused.click();await flush();
    assert.equal(w.app.state.view,'overview');assert.match(w.$('message-text').value,/heutigen Tag/);
    assert.equal(w.app.chat.selectedId(),null);assert.equal(mutations(w).length,before);
    await w.app.showView('ideas');w.$('ideas-list').querySelectorAll('button')[1].click();await flush();
    assert.equal(w.app.state.view,'ideas');assert.match(w.$('message-text').value,/heutigen Tag/);
    assert.match(w.$('notice').textContent,/Entwurf bleibt erhalten/);assert.equal(mutations(w).length,before);
  },
  async personal_ideas_rank_latest_tasks_and_open_only_the_bound_source(){
    const w=await world(),now=Date.now()/1000;
    const old=run(A,{zustand_code:'WAITING_USER',offen:true,task_revision:{revision:1}});
    const newer=run(C,{aufgabe:old.aufgabe,zustand_code:'RUNNING',offen:true,task_revision:{revision:2}});
    w.runs.set(B,run(B,{zustand_code:'WAITING_USER',zustand:'Deine Antwort fehlt',offen:true,auftrag:'<b>Welche Farbe?</b>'}));
    w.app.state.runs=[old,newer,w.runs.get(B),run('ar-4444444444444444',{angelegt:now-10}),
      run('ar-5555555555555555',{zustand_code:'CANCELLED',angelegt:now-2}),
      run('ar-6666666666666666',{zustand_code:'FAILED',angelegt:now-31*86400})];
    await w.app.showView('ideas');
    const list=w.$('personal-ideas-list');assert.equal(list.children.length,2);
    assert.match(list.children[0].textContent,/offene Frage/);assert.match(list.children[0].textContent,/<b>Welche Farbe/);
    assert.equal(list.querySelector('b'),null);assert.match(list.children[1].textContent,/Ergebnis weiterverwenden/);
    w.$('message-text').value='Mein ungesendeter Entwurf';const before=w.requests.length;
    list.querySelector('button').click();await flush();
    assert.equal(w.app.state.selected,B);assert.equal(w.app.state.view,'detail');
    assert(w.requests.slice(before).some(r=>r.url==='/v1/agent/runs/'+B&&r.method==='GET'));
    assert.equal(mutations(w).length,0);assert.equal(w.$('message-text').value,'Mein ungesendeter Entwurf');
  },
  async personal_ideas_bound_count_and_remove_stale_content_on_disconnect_logout(){
    const w=await world();w.app.state.runs=Array.from({length:7},(_,i)=>run('ar-'+String(i+1).repeat(16),
      {zustand_code:'WAITING_APPROVAL',offen:true,auftrag:'Private Aufgabe '+i,angelegt:i+1}));
    await w.app.showView('ideas');assert.equal(w.$('personal-ideas-list').children.length,4);
    assert.match(w.$('personal-ideas-list').children[0].textContent,/Private Aufgabe 6/);
    w.$('personal-ideas-list').querySelector('button').focus();w.app.setConnected(false);
    assert(!w.$('personal-ideas-list').textContent.includes('Private Aufgabe'));
    assert.match(w.$('personal-ideas-list').textContent,/erreichbar/);
    w.app.setConnected(true);assert.match(w.$('personal-ideas-list').textContent,/Private Aufgabe/);
    w.app.state.runs=null;await w.app.showView('ideas');assert(!w.$('personal-ideas-list').textContent.includes('Private Aufgabe'));
    w.app.showSession(null);assert.equal(w.$('personal-ideas-list').textContent,'');assert.equal(mutations(w).length,0);
  },
  async library_reuses_results_search_and_clears_private_content_on_logout(){
    const w=await world();await w.app.showView('library');
    assert.match(w.$('library-list').textContent,/Ergebnis ansehen/);
    w.$('library-search').value='kein-treffer-9876';w.$('library-search').dispatchEvent({type:'input'});
    assert.match(w.$('library-list').textContent,/Kein passendes Ergebnis/);
    w.$('library-search').value='';w.$('library-search').dispatchEvent({type:'input'});
    assert.match(w.$('library-list').textContent,/Ergebnis ansehen/);
    w.app.showSession(null);assert.equal(w.$('library-list').textContent,'');assert.equal(mutations(w).length,0);
  },
  async library_poll_preserves_preview_nodes_and_focus(){
    const w=await world();await w.app.showView('library');
    const card=w.$('library-list').children[0],button=card.querySelector('button');
    const preview=w.document.createElement('pre');preview.textContent='Geöffnete Vorschau';card.append(preview);button.focus();
    await w.app.refresh();await w.app.refresh();
    assert(w.$('library-list').children[0]===card,'Polling must not replace library cards');
    assert(preview.isConnected,'Polling must not close the preview');
    assert(w.document.activeElement===button,'Polling must preserve keyboard focus');
    assert(w.requests.some(r=>r.url==='/v1/agent/runs?view=tasks'));
  },
  async library_search_requests_all_history_and_rejects_obsolete_query_reply(){
    const w=await world();await w.app.showView('library');
    let oldReply;
    w.respond(req=>req.url.includes('?q=alt')?new Promise(resolve=>{oldReply=resolve;}):
      req.url.includes('?q=neu')?response({laeufe:[run(B,{auftrag:'Neuer Suchtreffer'})],next_before:null}):undefined);
    const search=async query=>{w.$('library-search').value=query;w.$('library-search').dispatchEvent({type:'input'});
      const [key,timer]=[...w.timers].find(([,v])=>v.ms===350);w.timers.delete(key);timer.fn();await flush();};
    await search('alt');await search('neu');
    assert.match(w.$('library-list').textContent,/Neuer Suchtreffer/);
    oldReply(response({laeufe:[run(A)],next_before:null}));await flush();
    assert.match(w.$('library-list').textContent,/Neuer Suchtreffer/);
    assert(!w.$('library-list').textContent.includes(run(A).auftrag));
    assert.equal(mutations(w).length,0);
  },
  async library_pagination_keeps_revisions_retries_and_discards_late_logout_reply(){
    const w=await world();
    w.respond(req=>req.url==='/v1/agent/runs'?response({laeufe:[run(B)],next_before:B}):undefined);
    await w.app.showView('library');assert.equal(w.$('library-more').hidden,false);
    w.respond(req=>req.url.includes('?before=')?response({error:'unavailable'},503):undefined);
    w.$('library-more').click();await flush();assert.match(w.$('library-list').textContent,/Zweiter eigener Auftrag/);
    assert.match(w.$('library-status').textContent,/erneut versuchen/);
    w.respond(req=>req.url.includes('?before=')?response({laeufe:[run(A,{aufgabe:run(B).aufgabe})],next_before:null}):undefined);
    w.$('library-more').click();await flush();assert.match(w.$('library-list').textContent,/Termine vergleichen/);
    assert.match(w.$('library-list').textContent,/Zweiter eigener Auftrag/);assert.equal(w.$('library-more').hidden,true);
    let finish;w.respond(req=>req.url==='/v1/agent/runs'?new Promise(resolve=>{finish=resolve;}):undefined);
    w.$('library-refresh').click();await flush();w.app.showSession(null);finish(response({laeufe:[run(A)],next_before:A}));await flush();
    assert.equal(w.$('library-list').textContent,'');assert.equal(mutations(w).length,0);
  },
  async connections_distinguish_measured_expired_and_missing_then_clear_on_logout(){
    const w=await world();assert.match(w.$('connection-list').textContent,/Noch nicht geprüft/);
    w.respond(req=>req.url==='/v1/dashboard/state'?response({environment:'isolated_test',runtime:'available',repositories:[],stand:1,components:[
      {komponente:'gmail',zustand:'healthy',geprueft_um:1},{komponente:'calendar',zustand:'auth_required',geprueft_um:1}]}):undefined);
    await w.app.refresh();const text=w.$('connection-list').textContent;
    assert.match(text,/Zuletzt verfügbar/);assert.match(text,/Anmeldung erforderlich/);
    w.app.setConnected(false);assert.equal(w.$('offline').hidden,false);
    w.app.showSession(null);assert.equal(w.$('connection-list').textContent,'');assert.equal(mutations(w).length,0);
  },
  async voice_focus_keeps_draft_and_waits_for_confirmed_history_read(){
    const w=await world({storage:{'solvio.chat.selected.v1':CH1}}),v=w.voices[0];
    w.$('message-text').value='Mein unveränderter Entwurf';w.$('message-text').focus();
    v.emit({active:true,conversationId:CH1,ready:false});
    assert.equal(w.$('chat-text-view').hidden,true);assert.equal(w.$('voice-focus').hidden,false);
    assert.equal(w.$('conversation-welcome').parentElement?.id,'voice-scene');
    assert.equal(w.$('browser-voice').parentElement?.id,'voice-focus-controls');
    v.emit({active:false,closing:true,conversationId:CH1});
    assert.equal(w.$('chat-text-view').hidden,true);
    const gate=deferred();w.respond(req=>req.url==='/v1/conversations/'+CH1?gate.promise:undefined);
    v.closedProof=true;v.flushed=true;v.emit({active:false,closing:false,conversationId:CH1});await flush();
    assert.equal(w.$('chat-text-view').hidden,true);assert.match(w.$('presence-title').textContent,/verlauf/);
    await w.app.chat.select(CH2);assert.equal(w.app.chat.selectedId(),CH1);
    gate.resolve(response(chatDetail(CH1,{messages:[userMessage(1,'Gesprochen'),answer(2,'Gespeichert')]})));await flush();
    assert.equal(w.$('chat-text-view').hidden,false);assert.equal(w.$('voice-focus').hidden,true);
    assert.doesNotMatch(w.$('presence-title').textContent,/wird gelesen/);
    assert.match(w.$('chat-messages').textContent,/Gesprochen.*Gespeichert/);
    assert.equal(w.$('message-text').value,'Mein unveränderter Entwurf');
    assert.equal(w.$('conversation-welcome').parentElement?.id,'chat-identity-home');
    assert.equal(mutations(w).length,0);
  },
  async voice_focus_read_failure_has_read_only_retry_and_logout_discards_late_read(){
    const w=await world({storage:{'solvio.chat.selected.v1':CH1}}),v=w.voices[0];
    w.$('message-text').value='Entwurf';v.emit({active:true,conversationId:CH1});
    w.respond(req=>req.url==='/v1/conversations/'+CH1?response({error:'network'},503):undefined);
    v.closedProof=true;v.flushed=true;v.emit({active:false,conversationId:CH1});await flush();
    assert.equal(w.$('chat-text-view').hidden,false);assert.equal(w.$('voice-return-retry').hidden,false);
    assert.match(w.$('voice-return-status').textContent,/nicht.*gelesen/);
    const gate=deferred();w.respond(req=>req.url==='/v1/conversations/'+CH1?gate.promise:undefined);
    w.$('voice-return-retry').click();await flush();assert.equal(w.$('chat-text-view').hidden,true);
    gate.resolve(response(chatDetail(CH1,{messages:[answer(1,'Nachgelesen')]})));await flush();
    assert.match(w.$('chat-messages').textContent,/Nachgelesen/);assert.equal(w.$('voice-return-status').hidden,true);
    assert.equal(w.$('message-text').value,'Entwurf');assert.equal(mutations(w).length,0);
    assert.doesNotMatch(w.$('presence-title').textContent,/wird gelesen/);
    v.emit({active:true,conversationId:CH1});const late=deferred();w.respond(req=>req.url==='/v1/conversations/'+CH1?late.promise:undefined);
    v.emit({active:false,conversationId:CH1});await flush();w.app.showSession(null);
    late.resolve(response(chatDetail(CH1,{messages:[answer(2,'Nicht mehr anzeigen')]})));await flush();
    assert.equal(w.$('workspace').hidden,true);assert.equal(w.app.state.voiceReturn,null);
    assert.doesNotMatch(w.$('chat-messages').textContent,/Nicht mehr anzeigen/);
  },
  async voice_focus_final_read_rejects_older_poll_and_missing_chat(){
    const w=await world({storage:{'solvio.chat.selected.v1':CH1}}),v=w.voices[0];
    v.emit({active:true,conversationId:CH1});
    const old=deferred();let count=0;
    w.respond(req=>req.url==='/v1/conversations/'+CH1?(++count===1?old.promise:response(chatDetail(CH1,{messages:[answer(3,'Frischer Sprachverlauf')]}))):undefined);
    const earlier=w.app.chat.reload();await flush();
    v.closedProof=true;v.flushed=true;v.emit({active:false,conversationId:CH1});await flush();
    assert.equal(w.$('chat-text-view').hidden,false);assert.match(w.$('chat-messages').textContent,/Frischer Sprachverlauf/);
    old.resolve(response(chatDetail(CH1,{messages:[answer(1,'Alter Zwischenstand')]})));await earlier;await flush();
    assert.match(w.$('chat-messages').textContent,/Frischer Sprachverlauf/);assert.doesNotMatch(w.$('chat-messages').textContent,/Alter Zwischenstand/);
    w.$('message-text').value='Entwurf';v.emit({active:true,conversationId:CH1});
    w.respond(req=>req.url==='/v1/conversations/'+CH1?response({error:'unknown_conversation'},404):undefined);
    v.emit({active:false,conversationId:CH1});await flush();
    assert.equal(w.$('chat-text-view').hidden,false);assert.equal(w.app.chat.selectedId(),null);
    assert.match(w.$('voice-return-status').textContent,/nicht.*gelesen/);assert.equal(w.$('message-text').value,'Entwurf');
    await w.app.chat.select(CH2);assert.equal(w.$('voice-return-status').hidden,true);
    assert.equal(mutations(w).length,0);
  },
  async voice_focus_unknown_or_start_failure_returns_without_success_or_autostart(){
    const w=await world({storage:{'solvio.chat.selected.v1':CH1}}),v=w.voices[0];
    w.$('message-text').value='Bleibt erhalten';
    v.emit({active:true,conversationId:CH1});v.emit({active:false,conversationId:CH1,uncertain:true,handoffBlocked:true});
    assert.equal(w.$('chat-text-view').hidden,false);assert.match(w.$('voice-return-status').textContent,/möglicherweise unvollständig/);
    assert.equal(w.$('message-text').value,'Bleibt erhalten');assert.equal(mutations(w).length,0);
    await w.app.chat.select(CH2);assert.equal(w.$('voice-return-status').hidden,true);
    v.emit({active:true});v.emit({active:false,errorText:'Kein Mikrofonzugriff.'});
    assert.equal(w.$('chat-text-view').hidden,false);assert.equal(w.$('voice-return-status').hidden,true);
    assert.equal(w.$('message-text').value,'Bleibt erhalten');assert.equal(mutations(w).length,0);
  },
  async presence_separates_current_chat_work_from_background_and_logout(){
    const w=await world({storage:{'solvio.chat.selected.v1':CH1}});
    assert.equal(w.$('conversation-welcome').hidden,false);
    w.runs.set(B,run(B,{zustand_code:'RUNNING',offen:true}));w.app.state.runs=[...w.runs.values()];w.app.renderRuns();
    assert.equal(w.$('presence').dataset.mode,'idle');
    assert.match(w.$('presence-detail').textContent,/1 Auftrag im Hintergrund/);
    w.conversations.get(CH1).deliveries_open=1;await w.app.chat.reload();
    assert.equal(w.$('presence').dataset.mode,'thinking');
    w.conversations.get(CH1).deliveries_open=0;await w.app.chat.reload();
    assert.equal(w.$('presence').dataset.mode,'idle');
    w.runs.set(B,run(B,{zustand_code:'WAITING_CAPABILITY',offen:true}));w.app.state.runs=[...w.runs.values()];w.app.renderRuns();
    assert.equal(w.$('presence').dataset.mode,'idle');assert.match(w.$('presence-detail').textContent,/wartet auf ein Werkzeug/);
    w.$('presence-detail').click();await flush();assert.equal(w.app.state.view,'tasks');
    w.app.showSession(null);assert.equal(w.$('presence-detail').hidden,true);
    assert.equal(w.$('presence').parentElement?.id,'presence-home');
  },
  async presence_updates_on_voice_snapshots_without_claiming_muted_or_ended_capture(){
    const w=await world(),v=w.voices[0];
    v.emit({active:true,ready:false,localText:'Mikrofonfreigabe wird angefragt …'});
    assert.equal(w.$('presence').dataset.mode,'idle');assert.match(w.$('presence-title').textContent,/angefragt/);
    v.options.onPresence('listening');v.emit({active:true,ready:true,capturing:true});
    assert.equal(w.$('presence').dataset.mode,'listening');
    v.emit({active:true,ready:true,capturing:false,muted:true});
    assert.equal(w.$('presence').dataset.mode,'idle');assert.match(w.$('presence-title').textContent,/stumm/);
    v.options.onPresence('speaking');assert.equal(w.$('presence').dataset.mode,'speaking');
    assert.match(w.$('presence-title').textContent,/spricht.*stumm/);
    v.emit({active:false,closing:true});assert.equal(w.$('presence').dataset.mode,'ended');
    assert.match(w.$('presence-title').textContent,/Mikrofon aus/);
    v.emit({active:false,closing:false,uncertain:true});assert.match(w.$('presence-title').textContent,/unbestätigt/);
    v.emit({active:false,closing:false,uncertain:false});assert.equal(w.$('presence').dataset.mode,'idle');
    assert.equal(mutations(w).length,0);
  },
  async voice_meter_uses_local_playback_and_mute_and_clears_on_end(){
    const w=voiceWorld(),levels=[];w.voice.onLevel=value=>levels.push(value);
    await w.voice.start();w.ready();
    w.voice.audio({type:'level',rms:.08,output_rms:.3,playing:false,played_ms:0});
    assert.deepEqual(plain(levels.at(-1)),{input:.08,output:0});
    w.voice.toggleMute();w.voice.audio({type:'level',rms:.8,output_rms:.3,playing:true,played_ms:20});
    assert.deepEqual(plain(levels.at(-1)),{input:0,output:.3});
    w.voice.end();assert.equal(levels.at(-1),null);
  },
  async source_chat_arrives_without_switching_and_opens_only_on_explicit_click(){
    const w=await world({chats:[chatDetail(CH1,{messages:[userMessage(1,'Früherer Verlauf')]}),
      chatDetail(CH2,{deliveries_open:1,messages:[userMessage(1,'Führe den Hotelvergleich fort.',{delivery:delivery({status:'running'})})]})],
      storage:{'solvio.chat.selected.v1':CH2}});
    const sourceReads=()=>w.requests.filter(r=>r.url==='/v1/conversations/'+CH1).length;
    assert.equal(sourceReads(),0);assert.equal(w.$('chat-messages').querySelector('.chat-source'),null);
    const current=w.conversations.get(CH2);current.deliveries_open=0;
    current.messages[0].delivery=delivery({source_chat:{conversation_id:CH1,title:'Hotel in Hamburg'}});
    await w.fireChatTimer();
    assert.equal(w.app.chat.selectedId(),CH2);assert.equal(w.localStorage.getItem('solvio.chat.selected.v1'),CH2);
    assert.equal(w.$('chat-title').textContent,'Zweiter Chat');assert.equal(sourceReads(),0);
    const link=w.$('chat-messages').querySelector('.chat-source');assert(link);
    assert.equal(link.textContent,'Aus Chat Hotel in Hamburg');assert.equal(link.tag,'button');
    // Real buttons take focus before the click; navigation must escape the focused-history render guard.
    link.focus();link.click();await flush();
    assert.equal(w.app.chat.selectedId(),CH1);assert.equal(w.localStorage.getItem('solvio.chat.selected.v1'),CH1);
    assert.equal(sourceReads(),1);assert.equal(w.$('chat-messages').querySelector('article').querySelector('p').textContent,'Früherer Verlauf');
    assert.equal(mutations(w).length,0);
  },
  async source_chat_title_is_literal_and_navigation_does_not_need_a_list_entry(){
    const title='<img src=x onerror=alert(1)> [Hotel](https://example.invalid)';
    const w=await world({chats:[chatDetail(CH2,{messages:[userMessage(1,'Mach damit weiter.',{
      delivery:delivery({source_chat:{conversation_id:CH1,title,url:'https://example.invalid'}})})]})],
      storage:{'solvio.chat.selected.v1':CH2}});
    assert.equal(w.$('chat-list').querySelectorAll('button').length,1);
    w.conversations.set(CH1,chatDetail(CH1,{messages:[answer(1,'Der frühere Stand.')]}));
    const link=w.$('chat-messages').querySelector('.chat-source');assert(link);
    assert.equal(link.textContent,'Aus Chat '+title);assert.equal(link.querySelectorAll('img,a').length,0);
    assert.equal(link.getAttribute('href'),null);assert.equal(w.requests.some(r=>r.url.includes('example.invalid')),false);
    link.focus();link.click();await flush();
    assert.equal(w.app.chat.selectedId(),CH1);assert.match(w.$('chat-messages').textContent,/Der frühere Stand/);
    assert.equal(mutations(w).length,0);assert(w.requests.every(r=>r.url.startsWith('/v1/')));
  },
  async absent_invalid_and_self_source_chat_metadata_preserves_the_history_without_a_link(){
    const sources=[undefined,null,'https://example.invalid',[],{},
      {conversation_id:'https://example.invalid',title:'URL'},
      {conversation_id:CH1+'\n',title:'Ungültig'},
      {conversation_id:[CH1],title:'Ungültig'},
      {conversation_id:CH1,title:17},
      {conversation_id:CH2,title:'Derselbe Chat'}];
    const w=await world({chats:[chatDetail(CH2,{messages:sources.map((source_chat,i)=>
      userMessage(i+1,'Nachricht '+i,{delivery:delivery({source_chat})}))})],storage:{'solvio.chat.selected.v1':CH2}});
    assert.equal(w.$('chat-messages').querySelectorAll('article').length,sources.length);
    assert.equal(w.$('chat-messages').querySelectorAll('.chat-source').length,0);
    assert.equal(w.app.chat.selectedId(),CH2);assert.equal(mutations(w).length,0);
  },
  async chat_list_shows_only_server_chats_and_remembers_the_selection_as_an_id(){
    const w=await world({chats:[chatDetail(CH1,{open_task_count:1}),chatDetail(CH2,{messages:[userMessage(1,'Wie spät ist es?',{delivery:delivery()}),answer(2,'Es ist 10 Uhr.')]})]});
    assert.equal(w.app.state.view,'overview');
    const rows=w.$('chat-list').querySelectorAll('button');
    assert.deepEqual(rows.map(r=>r.querySelector('.chat-row-title').textContent),['Hotel in Hamburg','Zweiter Chat']);
    assert.equal(rows[0].querySelectorAll('.chat-dot').length,1);assert.equal(rows[1].querySelectorAll('.chat-dot').length,0);
    assert.equal(w.$('chat-messages').querySelectorAll('article').length,0);assert.equal(w.localStorage.getItem('solvio.chat.selected.v1'),null);
    await selectChat(w,CH2);
    assert.equal(w.localStorage.getItem('solvio.chat.selected.v1'),CH2);
    const articles=w.$('chat-messages').querySelectorAll('article');
    assert.deepEqual(articles.map(a=>a.className),['conversation-message conversation-user','conversation-message conversation-answer']);
    assert.equal(articles[0].querySelector('p').textContent,'Wie spät ist es?');assert.equal(articles[1].querySelector('p').textContent,'Es ist 10 Uhr.');
    assert.equal(w.$('chat-title').textContent,'Zweiter Chat');assert.equal(w.$('chat').dataset.conversationId,CH2);
    assert.equal(w.requests.filter(r=>r.url==='/v1/conversations/'+CH2).length,1);
    assert.equal(mutations(w).length,0);assert.equal(w.voices[0].active,false);assert.equal(w.app.state.view,'overview');
    assert.deepEqual(Object.keys(w.localStorage.snapshot()),['solvio.chat.selected.v1']);
  },
  async new_chat_is_idempotent_across_a_double_click_and_a_lost_reply(){
    const w=await world();let posts=0;
    w.respond(req=>{if(req.url==='/v1/conversations'&&req.method==='POST'){posts++;if(posts===1)throw Error('lost reply');}});
    w.$('new-chat').click();w.$('new-chat').click();await flush();
    const first=mutations(w);assert.equal(first.length,1);assert.equal(w.app.chat.selectedId(),null);
    assert.equal(w.$('new-chat-retry')?.tag,'button');
    w.$('new-chat-retry').click();await flush();
    const all=mutations(w);assert.equal(all.length,2);assert.deepEqual(all[0].body,all[1].body);
    assert.equal(all[1].body.client_request_id,'fixture-request-1');
    const created=all[1].body.client_request_id&&w.app.chat.selectedId();
    assert.match(created,/^c-[a-f0-9]{16}$/);assert.equal(w.localStorage.getItem('solvio.chat.selected.v1'),created);
    assert.equal(w.$('chat-list').querySelectorAll('button').length,3);assert(w.$('new-chat-retry')===null,'no retry offer after the confirmed creation');
    assert(w.document.activeElement===w.$('message-text'),'the composer takes focus after a new chat');
  },
  async sending_records_pending_before_fetch_then_binds_the_delivery_and_retries_the_same_id(){
    const w=await world();await selectChat(w,CH1);const text='Besorg mir für morgen ein gutes Hotel in Hamburg.';
    let posts=0;w.respond(req=>{if(req.url.endsWith('/messages')&&req.method==='POST'){if(++posts===1)throw Error('lost');}});
    await w.send(text);
    const attempt=mutations(w)[0],stored=JSON.parse(attempt.storage['solvio.chat.pending.v1']);
    assert.equal(stored.length,1);assert.equal(stored[0].client_message_id,attempt.body.message.client_message_id);
    assert.equal(stored[0].conversation_id,CH1);assert.equal(stored[0].delivery_id,undefined);
    assert.equal(stored[0].digest,sha256(canonicalText(attempt.body.message)));
    assert.deepEqual(Object.keys(stored[0]).sort(),['client_message_id','conversation_id','created_at','digest']);
    assert.equal(w.$('message-text').value,text);assert.equal(w.$('message-text').disabled,true);
    assert.match(w.$('message-error').textContent,/ungewiss/);assert.equal(w.app.chat.uncertain,true);
    let blocked=false;w.window.dispatchEvent({type:'beforeunload',preventDefault(){blocked=true;}});assert.equal(blocked,true);
    w.$('message-retry').click();await flush();
    const calls=mutations(w);assert.equal(calls.length,2);assert.deepEqual(calls[0].body,calls[1].body);
    assert.deepEqual(calls[1].body,{message:{conversation_id:CH1,client_message_id:calls[0].body.message.client_message_id,text}});
    assert.equal(w.$('message-text').value,'');assert.equal(w.$('message-text').disabled,false);
    const rows=pendingRows(w);assert.equal(rows.length,1);assert.match(rows[0].delivery_id,/^cd-[a-f0-9]{16}$/);
    const article=w.$('chat-messages').querySelector('article');assert.equal(article.querySelector('p').textContent,text);
    assert.equal(article.querySelector('.chat-progress').textContent,'SOLVIO liest …');
    assert.equal(w.chatTimers().length,1);assert.equal(w.chatTimers()[0].ms,3000);
  },
  async a_definitive_503_names_the_reason_and_is_not_uncertain(){
    // Review Runde 2, F-3: ein 503 mit bekanntem Grund ist eine Absage des Core (nichts angenommen),
    // keine ungewisse Zustellung — der Grund steht da, Erneut-Senden bleibt möglich, kein beforeunload-Riegel.
    const w=await world();await selectChat(w,CH1);
    let posts=0;w.respond(req=>(req.url.endsWith('/messages')&&req.method==='POST'&&++posts===1)?response({error:'cognitive_router_unavailable'},503):undefined);
    await w.send('Besorg mir für morgen ein gutes Hotel in Hamburg.');
    assert.match(w.$('message-error').textContent,/Aufgabenbearbeitung ist gerade nicht verfügbar/);
    assert.match(w.$('message-error').textContent,/nicht angenommen/);
    assert.doesNotMatch(w.$('message-error').textContent,/ungewiss/);assert.notEqual(w.app.chat.uncertain,true);
    let blocked=false;w.window.dispatchEvent({type:'beforeunload',preventDefault(){blocked=true;}});assert.equal(blocked,false);
    w.$('message-retry').click();await flush();
    const calls=mutations(w);assert.equal(calls.length,2);assert.deepEqual(calls[0].body,calls[1].body);
    assert.equal(w.$('message-text').value,'');
  },
  async reload_reconciles_stored_deliveries_against_the_core_without_resending(){
    const known=delivery({status:'completed',task_id:'',run_id:''}),blocked=delivery({status:'blocked',error_code:'quota'});
    const w=await world({chats:[chatDetail(CH1,{messages:[userMessage(1,'Erste Frage',{delivery:known}),answer(2,'Antwort')]}),
        chatDetail(CH2,{messages:[userMessage(1,'Andere Frage',{delivery:blocked})]})],
      storage:{'solvio.chat.selected.v1':CH1,'solvio.chat.pending.v1':[
        {conversation_id:CH1,client_message_id:'fixture-old-1',digest:'d'.repeat(64),delivery_id:known.delivery_id,created_at:1},
        {conversation_id:CH1,client_message_id:'fixture-old-2',digest:'e'.repeat(64),created_at:2},
        {conversation_id:CH2,client_message_id:'fixture-old-3',digest:'f'.repeat(64),delivery_id:blocked.delivery_id,created_at:3},
        {conversation_id:'not-a-chat',client_message_id:'x',digest:'g',created_at:'never'}]}});
    assert.equal(mutations(w).length,0);assert.equal(w.app.chat.selectedId(),CH1);
    assert(w.requests.some(r=>r.url===`/v1/conversations/${CH2}/deliveries/${blocked.delivery_id}`));
    assert.deepEqual(pendingRows(w).map(r=>r.client_message_id),['fixture-old-2']);
    assert.equal(w.$('chat-hint').hidden,false);assert.match(w.$('chat-hint').textContent,/möglicherweise nicht angekommen/);
    assert.match(w.$('chat-list').querySelectorAll('button')[0].textContent,/ungewiss/);
    w.$('chat-hint').querySelector('button').click();await flush();
    assert.deepEqual(pendingRows(w),[]);assert.equal(w.$('chat-hint').hidden,true);assert.equal(mutations(w).length,0);
  },
  async blocked_delivery_shows_the_error_line_and_never_claims_success(){
    const d=delivery({status:'blocked',error_code:'source_revoked'});
    const w=await world({chats:[chatDetail(CH1,{messages:[userMessage(1,'Buche das Hotel',{delivery:d})]})],
      storage:{'solvio.chat.selected.v1':CH1,'solvio.chat.pending.v1':[{conversation_id:CH1,client_message_id:'fixture-old-9',digest:'a'.repeat(64),delivery_id:d.delivery_id,created_at:1}]}});
    const article=w.$('chat-messages').querySelector('article');
    assert.match(article.querySelector('.chat-blocked').textContent,/Anmeldung.*abgelaufen/);
    assert.equal(article.querySelectorAll('.chat-progress').length,0);
    assert.doesNotMatch(w.$('chat-messages').textContent,/angenommen|Erfolg bestätigt/);
    assert.equal(w.$('chat-messages').querySelectorAll('.conversation-answer').length,0);
    assert.deepEqual(pendingRows(w),[]);assert.equal(w.chatTimers().length,0);
  },
  async task_card_follows_its_triggering_message_with_bound_file_and_cancel(){
    const running=run(A,{zustand:'Wartet auf dich',zustand_code:'WAITING_USER',offen:true,ergebnis:'',dateien:[fileDescriptor()],
      action_intent:{status:'waiting_user',question:{id:'q1',revision:1,digest:'x',field:'duration',prompt:'Wie viele Nächte?',input_type:'number'}},wartet_auf:null});
    const w=await world({chats:[chatDetail(CH1,{deliveries_open:0,auftraege:[running],messages:[
      userMessage(1,'Hallo',{delivery:delivery()}),answer(2,'Hallo zurück.'),
      userMessage(3,'Vergleiche Hotels',{delivery:delivery({task_id:running.aufgabe,run_id:A,revision:1})}),answer(4,'Ich habe das als Auftrag aufgenommen.')]})],
      storage:{'solvio.chat.selected.v1':CH1}});
    const children=w.$('chat-messages').children;
    assert.deepEqual(children.map(n=>n.className),['conversation-message conversation-user','conversation-message conversation-answer',
      'conversation-message conversation-user','chat-task-card','conversation-message conversation-answer']);
    const card=children[3];assert.equal(card.dataset.runId,A);
    assert.equal(card.querySelector('.result-file-card').querySelector('a').href,`/v1/agent/runs/${A}/artifacts/aa-1111111111111111/download`);
    assert.equal(outsideDetails(card.querySelector('.result-file-card'),card),true);
    assert(w.$('action-answer')===null,'the bound answer form is never rendered inside a chat card');
    assert.match(card.querySelector('.action-question').textContent,/Wie viele Nächte\?/);
    const cancel=card.querySelectorAll('button').find(b=>b.textContent==='Auftrag abbrechen');assert(cancel);
    assert.equal(w.chatTimers().length,1,'an open task keeps the fast poll');
    w.respond(req=>req.url.endsWith('/cancel')?response({id:A,zustand:'abgebrochen'}):undefined);
    cancel.click();await flush();assert.equal(w.$('decision-dialog').open,true);assert.equal(mutations(w).length,0);
    w.$('decision-content').querySelectorAll('button').find(n=>n.textContent==='Bestätigen').click();await flush();
    assert.equal(mutations(w).length,1);assert.equal(mutations(w)[0].url,'/v1/agent/runs/'+A+'/cancel');
    assert.equal(w.app.state.view,'overview');assert.equal(w.app.state.selected,null);assert.equal(w.app.chat.selectedId(),CH1);
    card.querySelectorAll('button').find(b=>b.textContent==='Im Auftrag beantworten').click();await flush();
    assert.equal(w.app.state.view,'detail');assert.equal(w.app.state.selected,A);assert(w.$('result').parentElement===w.$('conversation-result'),'the run opens in the detail view');
  },
  async late_detail_of_a_previous_chat_never_overwrites_the_current_selection(){
    const w=await world({chats:[chatDetail(CH1,{messages:[userMessage(1,'Alter Chat',{delivery:delivery()})]}),chatDetail(CH2,{messages:[userMessage(1,'Neuer Chat',{delivery:delivery()})]})]});
    const gate=deferred();w.respond(req=>req.url==='/v1/conversations/'+CH1?gate.promise:undefined);
    const old=w.app.chat.select(CH1);await flush();await w.app.chat.select(CH2);
    gate.resolve(response(w.conversations.get(CH1)));await old;await flush();
    assert.equal(w.app.chat.selectedId(),CH2);assert.equal(w.$('chat').dataset.conversationId,CH2);
    assert.equal(w.$('chat-messages').querySelector('article').querySelector('p').textContent,'Neuer Chat');
    assert.equal(w.$('chat-title').textContent,'Zweiter Chat');assert.equal(w.localStorage.getItem('solvio.chat.selected.v1'),CH2);
    assert.equal(w.$('chat-list').querySelectorAll('button').find(b=>b.dataset.conversationId===CH2).getAttribute('aria-pressed'),'true');
  },
  async voice_starts_only_by_its_button_and_carries_the_selected_chat(){
    const w=await world();await selectChat(w,CH1);w.$('new-chat').click();await flush();
    assert.equal(w.voices.length,1);assert.equal(w.voices[0].active,false);
    assert.equal(w.requests.filter(r=>r.url.includes('/voice/')).length,0);
    w.respond(req=>req.url==='/v1/browser/voice/session'?response({nonce:'fixture-nonce'}):undefined);
    await w.voices[0].ticket();
    const ticket=mutations(w).find(r=>r.url==='/v1/browser/voice/session');
    assert.deepEqual(ticket.body,{conversation_id:w.app.chat.selectedId()});assert.match(ticket.body.conversation_id,/^c-[a-f0-9]{16}$/);
    const fresh=await world();fresh.respond(req=>req.url==='/v1/browser/voice/session'?response({nonce:'fixture-nonce'}):undefined);
    await fresh.voices[0].ticket();
    const freshCalls=mutations(fresh);assert.equal(freshCalls.length,2,'create a canonical chat before the voice ticket');
    assert.equal(freshCalls[0].url,'/v1/conversations');assert.deepEqual(freshCalls[1].body,{conversation_id:fresh.app.chat.selectedId()});
  },
  async voice_preparation_precedes_microphone_and_keeps_the_start_chat_pinned(){
    const gate=deferred(),w=voiceWorld({prepareStart:()=>gate.promise});
    const start=w.voice.start();assert.equal(w.micCalls,0);assert.equal(w.requests.length,0);
    gate.resolve({conversation_id:CH1});await start;assert.equal(w.micCalls,1);assert.deepEqual(w.requests[0].body,{conversation_id:CH1});
    const late=deferred(),cancelled=voiceWorld({prepareStart:()=>late.promise});
    const pending=cancelled.voice.start();cancelled.voice.end();late.resolve({conversation_id:CH1});await pending;
    assert.equal(cancelled.micCalls,0);assert.equal(cancelled.requests.length,0);
    const unavailable=await world();unavailable.respond(req=>req.url==='/v1/conversations'&&req.method==='POST'?response({error:'conversation_store_unavailable'},503):undefined);
    await assert.rejects(()=>unavailable.voices[0].ticket());assert.equal(unavailable.requests.filter(r=>r.url.includes('/voice/')).length,0);
  },
  async real_voice_handoff_requires_matching_flush_and_audio_close_in_either_order(){
    for(const order of ['close_first','flush_first']){
      const w=voiceWorld();await w.voice.start();w.ready();let result=null;
      const waiting=w.voice.endAndWait(CH1).then(v=>{result=v;});
      assert.equal(w.track.readyState,'ended');assert.equal(w.sockets[0].sent.filter(x=>x.type==='session_end').length,1);
      w.flushed({session_id:'foreign'});w.flushed({conversation_id:CH2});await flush();assert.equal(result,null);
      if(order==='close_first')w.closed();else w.flushed();await flush();assert.equal(result,null);
      if(order==='close_first')w.flushed();else w.closed();await waiting;
      assert.equal(result,true);assert.equal(await w.voice.endAndWait(CH1),true);assert.equal(w.requests.length,1);
    }
    for(const reason of ['unknown','unconfirmed_audio','socket_loss','old_protocol','timeout']){
      const w=voiceWorld({protocol:reason==='old_protocol'?0:1});await w.voice.start();w.ready();
      const waiting=w.voice.endAndWait(CH1);
      if(reason==='unknown'){w.flushed({status:'unknown'});w.closed();}
      else if(reason==='unconfirmed_audio'){w.flushed();w.closed({previous_unconfirmed:1});}
      else if(reason==='old_protocol')w.closed();
      else if(reason==='timeout'){for(const fn of [...w.timers.values()])fn();}
      else w.sockets[0].close();
      assert.equal(await waiting,false,reason);assert.equal(await w.voice.endAndWait(CH1),false,'no second click bypasses '+reason);
      assert.equal(await w.voice.endAndWait(CH2),true,'another chat is independently writable');
      assert.equal(w.voice.continueWithStoredHistory(CH1),true);assert.equal(await w.voice.endAndWait(CH1),true);
      assert.equal(w.voice.flushed===true&&w.voice.closedProof, false,'recovery is not proof');
    }
  },
  async text_handoff_preserves_edited_drafts_and_never_sends_before_confirmed_drain(){
    for(const variation of ['complete','unknown','edited','selected','logout']){
      const w=await world();await selectChat(w,CH1);const gate=deferred(),voice=w.voices[0];voice.active=true;
      let blocked=variation==='unknown';voice.isHandoffBlocked=()=>blocked;
      voice.continueWithStoredHistory=()=>{blocked=false;voice.active=false;return true;};
      voice.endAndWait=async id=>{assert.equal(id,CH1);return gate.promise;};
      await w.send('Mein Entwurf');assert.equal(mutations(w).length,0);assert.equal(w.$('message-text').disabled,false);assert.equal(w.$('message-submit').disabled,true);
      if(variation==='edited')w.$('message-text').value='Geänderter Entwurf';
      if(variation==='selected'){voice.emit({active:false,closing:false});await selectChat(w,CH2);}
      if(variation==='logout')w.app.showSession(null);
      gate.resolve(variation!=='unknown');await flush();
      assert.equal(mutations(w).filter(r=>r.url.endsWith('/messages')).length,variation==='complete'?1:0,variation);
      if(variation==='unknown'){
        assert.equal(w.$('message-text').value,'Mein Entwurf');assert.match(w.$('message-error').textContent,/Abschluss.*nicht bestätigt/);
        await w.send('Mein Entwurf');assert.equal(mutations(w).length,0,'ordinary send cannot clear uncertainty');
        const recover=w.$('message-error').querySelector('button');assert.match(recover.textContent,/Mit vorhandenem Verlauf/);
        recover.click();await flush();assert.equal(mutations(w).length,0,'explicit recovery only reads');assert.equal(w.$('message-text').value,'Mein Entwurf');
        assert.match(w.$('message-error').textContent,/möglicherweise unvollständig/);
        await w.send('Mein Entwurf');assert.equal(mutations(w).filter(r=>r.url.endsWith('/messages')).length,1);
      }
      if(variation==='edited')assert.equal(w.$('message-text').value,'Geänderter Entwurf');
      if(variation==='selected')assert.equal(w.app.chat.selectedId(),CH2);
    }
  },
  async live_voice_cannot_be_hidden_by_chat_navigation_or_bypassed_by_message_retry(){
    const w=await world();await selectChat(w,CH1);const voice=w.voices[0];
    voice.emit({active:true,closing:false});await w.app.chat.select(CH2);w.$('new-chat').click();await flush();
    assert.equal(w.app.chat.selectedId(),CH1);assert.equal(w.$('new-chat').disabled,true);assert.equal(mutations(w).length,0);
    voice.emit({active:false,closing:true});await w.app.chat.select(CH2);assert.equal(w.app.chat.selectedId(),CH1);
    voice.emit({active:false,closing:false});await w.app.chat.select(CH2);assert.equal(w.app.chat.selectedId(),CH2);
    let posts=0;w.respond(req=>{if(req.url.endsWith('/messages')&&++posts===1)throw Error('lost response');});
    await w.send('Gebundene Wiederholung');const original=mutations(w)[0].body;assert(w.$('message-retry'));
    voice.active=true;voice.endAndWait=async()=>false;
    w.$('message-retry').click();await flush();assert.equal(posts,1);assert.equal(w.app.chat.isSending(),true,'retain the uncertain original binding');
    w.$('message-retry').click();await flush();assert.equal(posts,1,'a second retry does not bypass the audio handoff');
    voice.active=false;voice.endAndWait=async()=>true;
    w.$('message-retry').click();await flush();assert.equal(posts,2);assert.deepEqual(mutations(w).at(-1).body,original);
  },
  async local_storage_holds_identifiers_and_digests_only(){
    const w=await world();const texts=['Geheimzahl 4711 für die Kasse','Ruf Dr. Beispiel unter 0800 an'];
    await selectChat(w,CH1);await w.send(texts[0]);await w.send(texts[1]);
    assert.equal(mutations(w).length,2);
    const snapshot=w.localStorage.snapshot();
    assert.deepEqual(Object.keys(snapshot).sort(),['solvio.chat.pending.v1','solvio.chat.selected.v1']);
    for(const text of texts)for(const word of text.split(' '))assert.doesNotMatch(JSON.stringify(snapshot),new RegExp(word));
    const rows=pendingRows(w);assert.equal(rows.length,2);
    for(const row of rows){assert.deepEqual(Object.keys(row).sort(),['client_message_id','conversation_id','created_at','delivery_id','digest']);
      assert.match(row.digest,/^[a-f0-9]{64}$/);assert.match(row.delivery_id,/^cd-[a-f0-9]{16}$/);}
  },
  async chat_polls_every_three_seconds_only_while_work_is_open_and_backs_off_on_errors(){
    const w=await world({chats:[chatDetail(CH1,{deliveries_open:1,messages:[userMessage(1,'Frage',{delivery:delivery({status:'running'})})]})],storage:{'solvio.chat.selected.v1':CH1}});
    assert.deepEqual(w.chatTimers().map(t=>t.ms),[3000]);
    const reads=()=>w.requests.filter(r=>r.url==='/v1/conversations/'+CH1).length;const before=reads();
    w.conversations.get(CH1).deliveries_open=0;w.conversations.get(CH1).messages[0].delivery.status='completed';
    await w.fireChatTimer();assert.equal(reads(),before+1);assert.deepEqual(w.chatTimers(),[]);
    assert.equal(w.$('chat-messages').querySelectorAll('.chat-progress').length,0);
    await w.app.refresh();assert.equal(reads(),before+2);assert.deepEqual(w.chatTimers(),[]);
    w.respond(req=>req.url==='/v1/conversations/'+CH1?response({error:'conversation_store_unavailable'},503):undefined);
    await w.app.chat.reload();assert.deepEqual(w.chatTimers().map(t=>t.ms),[3000]);
    await w.fireChatTimer();assert.deepEqual(w.chatTimers().map(t=>t.ms),[6000]);
    assert.match(w.$('chat-hint').textContent,/Chatspeicher/);assert.match(w.$('chat-messages').textContent,/Frage/,'the last read state stays visible');assert.equal(w.app.chat.selectedId(),CH1);
  },
  async chat_attachment_is_sent_as_message_attachments_and_retried_with_exact_bytes(){
    const w=await world();await selectChat(w,CH1);let posts=0;
    w.respond(req=>{if(req.url.endsWith('/messages')&&req.method==='POST'&&++posts===1)throw Error('lost response');});
    const bytes=[Buffer.from('Monat,Umsatz\nAugust,37\n'),Buffer.from('synthetic-xlsx-wire')];
    w.$('document-file').files=bytes.map((content,i)=>({name:i?'September.xlsx':'August.csv',size:content.length,arrayBuffer:async()=>content}));
    w.$('document-file').dispatchEvent({type:'change'});await flush();
    assert.equal(w.app.state.document.status,'ready');assert(w.$('document-file').hasAttribute('multiple'));
    assert.equal(w.$('document-options').open,true);
    await w.send('Analysiere die angehängten Tabellen mit Exceldatei, Diagramm und Bericht.');
    assert.equal(w.app.chat.uncertain,true);assert.equal(w.$('document-file').disabled,true);
    w.$('document-file').files=[];w.$('document-file').dispatchEvent({type:'change'});await flush();
    w.$('message-retry').click();await flush();
    const calls=mutations(w);assert.equal(calls.length,2);assert.deepEqual(calls[0].body,calls[1].body);
    assert.equal(calls[0].body.message.attachments.operation,'process_files');
    assert.deepEqual(calls[0].body.message.attachments.files.map(f=>Buffer.from(f.content_b64,'base64').toString()),bytes.map(b=>b.toString()));
    assert.equal(w.app.state.document,null);assert.equal(w.voices[0].active,false);
  },
  async chat_attachment_limits_and_mixed_documents_never_send_partial_input(){
    for(const files of [
      [{name:'large.csv',size:8*1024*1024+1}],
      Array.from({length:5},(_,i)=>({name:i+'.csv',size:10})),
      [{name:'table.csv',size:10},{name:'text.txt',size:10}]
    ]){
      const w=await world();await selectChat(w,CH1);
      w.$('document-file').files=files.map(f=>({...f,arrayBuffer:async()=>Buffer.from('a,b\n1,2\n')}));
      w.$('document-file').dispatchEvent({type:'change'});await flush();
      assert.equal(w.app.state.document.status,'invalid');assert(w.$('document-error').textContent);
      await w.send('Analysiere die vollständigen Anhänge.');assert.equal(mutations(w).length,0);
      w.$('document-clear').click();assert.equal(w.app.state.document,null);
    }
  },
  async late_attachment_read_after_logout_cannot_restore_private_bytes(){
    const w=await world(),gate=deferred();w.$('document-file').files=[{name:'private.csv',size:8,arrayBuffer:()=>gate.promise}];
    w.$('document-file').dispatchEvent({type:'change'});await flush();
    w.app.showSession(null);gate.resolve(Buffer.from('a,b\n1,2\n'));await flush();
    assert.equal(w.app.state.document,null);assert.equal(w.$('document-status').textContent,'');
    assert.equal(mutations(w).length,0);
  },
  async logout_removes_chat_content_and_a_late_reply_cannot_restore_it(){
    const w=await world({chats:[chatDetail(CH1,{messages:[userMessage(1,'Privater Wortlaut',{delivery:delivery()})]})]});
    await selectChat(w,CH1);assert.match(w.$('chat-messages').textContent,/Privater Wortlaut/);
    const gate=deferred();w.respond(req=>req.url==='/v1/conversations/'+CH1?gate.promise:undefined);
    w.$('message-text').value='Noch nicht gesendet';const old=w.app.chat.reload();await flush();
    w.app.showSession(null);gate.resolve(response(w.conversations.get(CH1)));await old;await flush();
    assert.equal(w.$('workspace').hidden,true);assert.equal(w.$('chat-messages').textContent,'');assert.equal(w.$('chat-list').textContent,'');
    assert.equal(w.$('message-text').value,'');assert.equal(w.$('chat-title').textContent,'Neuer Chat');
    assert.equal(w.app.chat.selectedId(),null);assert.equal(mutations(w).length,0);assert.equal(w.chatTimers().length,0);
    assert.equal(w.localStorage.getItem('solvio.chat.selected.v1'),CH1,'the stored id survives, the text does not');
  },
  async provider_switch_requires_explicit_confirmation_and_sends_exact_boundary_object(){
    const w=await world(),ref='a'.repeat(64);
    const waiting=run(A,{zustand_code:'WAITING_USER',offen:true,anbietergrenze:{fortsetzbar:true,boundary_ref:ref,
      wechseloptionen:[{provider:'claude-code',label:'Claude Code',hinweis:'Derselbe Auftrag und Kostenrahmen.',werkzeuge:['Websuche (WebSearch)','Webseiten lesen (WebFetch)']}]}});
    w.runs.set(A,waiting);await w.app.selectRun(A);assert.equal(w.app.state.view,'detail');
    const choose=w.$('result').querySelectorAll('button').find(n=>n.textContent==='Mit Claude Code fortsetzen');
    assert(choose);assert.equal(mutations(w).length,0);choose.click();await flush();
    assert.equal(w.$('decision-dialog').open,true);assert.match(w.$('decision-content').textContent,/Websuche \(WebSearch\), Webseiten lesen \(WebFetch\)/);
    assert(!w.$('decision-content').textContent.includes('Hermes-Browser'));
    assert.equal(mutations(w).length,0);
    w.respond(req=>req.url.endsWith('/resume')?response({id:A,zustand:'arbeitet'}):undefined);
    w.$('decision-content').querySelectorAll('button').find(n=>n.textContent==='Bestätigen').click();await flush();
    assert.equal(mutations(w).length,1);assert.deepEqual(mutations(w)[0].body,{provider:'claude-code',boundary_ref:ref});
    assert.equal(mutations(w)[0].url,'/v1/agent/runs/'+A+'/resume');assert.equal(w.app.state.selected,A);
  },
  async provider_switch_without_core_offer_or_after_session_loss_cannot_dispatch(){
    const w=await world();w.runs.set(A,run(A,{zustand_code:'WAITING_USER',offen:true,
      anbietergrenze:{fortsetzbar:false,boundary_ref:'a'.repeat(64),wechseloptionen:[]}}));
    await w.app.selectRun(A);assert(!w.$('result').querySelectorAll('button').some(n=>n.textContent.includes('Code fortsetzen')));
    w.runs.set(A,run(A,{zustand_code:'WAITING_USER',offen:true,anbietergrenze:{fortsetzbar:true,boundary_ref:'a'.repeat(64),
      wechseloptionen:[{provider:'claude-code',label:'Claude Code',hinweis:'Auftrag behalten.',werkzeuge:['WebSearch']}]}}));
    await w.app.selectRun(A);w.$('result').querySelectorAll('button').find(n=>n.textContent==='Mit Claude Code fortsetzen').click();
    const confirm=w.$('decision-content').querySelectorAll('button').find(n=>n.textContent==='Bestätigen');
    w.app.showSession(null);confirm.click();await flush();assert.equal(mutations(w).length,0);
  },
  async explicit_followup_of_unfulfilled_research_keeps_failure_and_requires_core_eligibility(){
    const w=await world(),text='Ein Zentimeter Abweichung ist erlaubt. Die Preisgrenze bleibt.';
    const failed=finalFollowup({zustand:'Recherche unvollständig',zustand_code:'FAILED',grund_code:'goal_unverified',
      ergebnis:'Kein Angebot erfüllt sämtliche Bedingungen.',offen:false});
    for(const [status,reason,expected] of [
      ['Recherche unvollständig','goal_unverified','Recherche unvollständig'],
      ['Abschluss nicht bestätigt','goal_unverified','Abschluss nicht bestätigt'],
      ['','goal_unverified','Abschluss nicht bestätigt'],
      ['fehlgeschlagen','specialist_failed','Auftrag fehlgeschlagen'],
    ]){
      w.runs.set(A,{...failed,zustand:status,grund_code:reason});await w.app.selectRun(A);
      assert.equal(w.$('result').querySelector('.result-failure').querySelector('strong').textContent,expected);
      assert.equal(w.app.state.detail.zustand_code,'FAILED');
      assert.match(w.$('result').textContent,/Kein Angebot erfüllt sämtliche Bedingungen/);
      assert.equal(mutations(w).length,0);
    }
    w.runs.set(A,failed);await w.app.selectRun(A);
    assert.equal(w.$('followup-form').hidden,false);assert.equal(mutations(w).length,0);
    assert.equal(w.app.state.detail.zustand_code,'FAILED');
    w.respond(req=>{if(req.url.endsWith('/followup')){
      w.runs.set(C,followed({task_revision:{revision:2,digest:'b'.repeat(64),text,parent_run_id:A},
        task_history:[{run_id:A,revision:1,text:failed.auftrag,state:'FAILED',result_summary:failed.ergebnis},
          {run_id:C,revision:2,text,state:'CREATED',result_summary:''}]}));
      return response(followupReceipt(),202);
    }});
    sendFollowup(w,text);await flush();
    assert.equal(mutations(w).length,1);assert.equal(mutations(w)[0].body.text,text);
    assert.equal(w.app.state.selected,C);assert.equal(w.app.state.detail.aufgabe,failed.aufgabe);
    const history=w.$('result').querySelector('[data-section="revisions"]');
    assert.match(history.textContent,/Kein Angebot erfüllt sämtliche Bedingungen/);
    assert.equal(w.runs.get(A).zustand_code,'FAILED');
    for(const code of ['FAILED','CANCELLED','RUNNING']){
      w.runs.set(A,{...failed,zustand_code:code,followup:{eligible:false,reason:'not eligible'}});
      await w.app.selectRun(A);assert.equal(w.$('followup-form').hidden,true);
      sendFollowup(w,text);await flush();assert.equal(mutations(w).length,1);
    }
  },
  async followup_accepts_same_task_exact_selected_file_and_consolidates_real_history(){
    const w=await world(),gate=deferred();w.runs.set(A,finalFollowup({dateien:[fileDescriptor()]}));await w.app.selectRun(A);
    assert.equal(w.$('followup-form').hidden,false);assert.equal(w.$('followup-files-options').open,false);
    w.$('followup-files').querySelector('input').checked=true;
    w.respond(req=>req.url.endsWith('/followup')?gate.promise:undefined);
    sendFollowup(w);sendFollowup(w,'This second click must not change the text');await flush();
    assert.equal(mutations(w).length,1);assert.deepEqual(mutations(w)[0].body,{run_id:A,text:'Bitte ergänze September.',
      expected_revision:1,expected_digest:revisionDigest,input_artifact_ids:['aa-1111111111111111'],client_request_id:'fixture-request-1'});
    w.runs.set(C,followed());gate.resolve(response(followupReceipt(),202));await flush();
    assert.equal(w.app.state.selected,C);assert.equal(w.$('conversation-objective').textContent,run().auftrag);
    assert.equal(w.$('conversation-followup-text').textContent,'Bitte ergänze September.');
    assert.equal(w.$('conversation-followup-message').hidden,false);assert.equal(w.$('task-count').textContent,'2');
    assert.equal(w.$('task-list').children.length,2);assert.equal(w.$('workroom-task').options.length,3);
    const history=w.$('result').querySelector('[data-section="revisions"]');assert(history);assert.equal(history.open,false);
    assert.match(history.textContent,/Das erste Ergebnis/);history.querySelector('button').click();await flush();
    assert.equal(w.app.state.selected,A);assert.equal(w.$('conversation-followup-message').hidden,true);
    assert.equal(w.voices[0].active,false);
  },
  async uncertain_followup_keeps_exact_request_after_poll_and_blocks_discard(){
    const w=await world();w.runs.set(A,finalFollowup({dateien:[fileDescriptor()]}));await w.app.selectRun(A);
    w.$('followup-files').querySelector('input').checked=true;let count=0;
    w.respond(req=>{if(req.url.endsWith('/followup')){if(++count===1)throw Error('lost');w.runs.set(C,followed());return response(followupReceipt(),202);}});
    sendFollowup(w);await flush();const frozen=plain(w.app.state.followup.submission);assert(frozen);
    await w.app.selectRun(B);assert.equal(w.app.state.selected,A);assert.match(w.$('notice').textContent,/Folgeanweisung/);
    w.runs.set(A,finalFollowup({followup:{eligible:false,reason:'already advanced'}}));await w.app.refresh();
    assert.equal(w.$('followup-form').hidden,false);assert.equal(w.$('followup-text').disabled,true);
    assert.equal(w.$('followup-submit').disabled,false);assert.match(w.$('followup-status').textContent,/unbestätigt/);
    sendFollowup(w,'mutated DOM is not the frozen request');await flush();
    assert.equal(mutations(w).length,2);assert.deepEqual(mutations(w)[0].body,mutations(w)[1].body);
    assert.deepEqual(mutations(w)[1].body,frozen);assert.equal(w.app.state.selected,C);
  },
  async followup_late_reply_after_logout_cannot_restore_private_task_or_request(){
    const w=await world(),gate=deferred();w.runs.set(A,finalFollowup({dateien:[fileDescriptor()]}));await w.app.selectRun(A);
    w.respond(req=>req.url.endsWith('/followup')?gate.promise:undefined);sendFollowup(w);await flush();
    w.app.showSession(null);gate.resolve(response(followupReceipt(),202));await flush();
    assert.equal(w.app.state.followup,null);assert.equal(w.app.state.selected,null);
    assert.equal(w.$('followup-text').value,'');assert.equal(w.$('followup-files').textContent,'');
    assert.equal(w.$('followup-form').hidden,true);assert.equal(w.$('conversation-followup-text').textContent,'');
    assert.equal(w.$('result').textContent,'');assert.equal(mutations(w).length,1);
  },
  async followup_requires_fresh_eligibility_and_retains_unsent_text_while_offline(){
    const w=await world();await w.app.selectRun(A);assert.equal(w.$('followup-form').hidden,true);
    w.runs.set(A,finalFollowup());await w.app.selectRun(A);w.$('followup-text').value='Nicht verlieren';
    w.app.setConnected(false);sendFollowup(w,'Nicht verlieren');await flush();
    assert.equal(mutations(w).length,0);assert.equal(w.$('followup-text').value,'Nicht verlieren');
    assert.equal(w.$('followup-submit').disabled,true);w.app.setConnected(true);
    w.runs.set(A,finalFollowup({followup:{eligible:false,reason:'source revoked'}}));await w.app.refresh();
    sendFollowup(w);await flush();assert.equal(mutations(w).length,0);assert.equal(w.$('followup-form').hidden,true);
  },
  async followup_file_choices_reject_foreign_routes_and_enforce_total_limit(){
    const w=await world(),files=[fileDescriptor('aa-1111111111111111','A.xlsx',5*1024*1024),fileDescriptor('aa-2222222222222222','B.csv',5*1024*1024),
      {...fileDescriptor('aa-3333333333333333','Foreign.csv'),download_url:'https://foreign.example/file'},fileDescriptor('aa-4444444444444444','Report.pdf')];
    w.runs.set(A,finalFollowup({dateien:files}));await w.app.selectRun(A);
    const choices=w.$('followup-files').querySelectorAll('input');assert.equal(choices.length,2);
    for(const choice of choices)choice.checked=true;sendFollowup(w);await flush();
    assert.equal(mutations(w).length,0);assert.match(w.$('followup-status').textContent,/8 MiB/);
  },
  async followup_wrong_acceptance_binding_remains_uncertain_and_retryable(){
    const w=await world();w.runs.set(A,finalFollowup());await w.app.selectRun(A);
    w.respond(req=>req.url.endsWith('/followup')?response(followupReceipt({task_id:'at-foreign'}),202):undefined);
    sendFollowup(w);await flush();assert.equal(w.app.state.selected,A);assert(w.app.state.followup.submission);
    assert.match(w.$('followup-status').textContent,/unbestätigt/);
    let blocked=false;w.window.dispatchEvent({type:'beforeunload',preventDefault(){blocked=true;}});assert.equal(blocked,true);
  },
  async followup_definitive_rejection_requires_new_read_before_another_send(){
    const w=await world();w.runs.set(A,finalFollowup());await w.app.selectRun(A);
    w.respond(req=>req.url.endsWith('/followup')?response({error:'followup_not_available',reason:'Der Stand hat sich geändert.'},409):undefined);
    sendFollowup(w);await flush();assert.equal(w.app.state.followup.submission,null);
    assert.equal(w.$('followup-form').hidden,false);assert.match(w.$('followup-status').textContent,/Stand/);
    sendFollowup(w);await flush();assert.equal(mutations(w).length,1);assert.equal(w.$('followup-submit').disabled,true);
    await w.app.refresh();assert.equal(w.$('followup-submit').disabled,false);
  },
  async empty_home_shows_the_chat_without_implicit_task_voice_or_hermes_start(){
    const w=await world({chats:[]});assert.equal(w.app.state.view,'overview');assert.equal(w.$('conversation').hidden,true);
    assert.equal(w.$('overview-view').hidden,false);assert.equal(w.$('message-form').hidden,false);assert.equal(w.$('message-text').disabled,false);
    assert.match(w.$('chat-list').textContent,/Noch kein Chat/);assert.match(w.$('chat-messages').textContent,/erste Nachricht/);
    assert.equal(w.$('task-form').parentElement.parentElement.id,'task-entry');assert.equal(w.$('task-entry').open,false);
    assert.equal(w.$('hermes-frame').getAttribute('src'),null);assert(w.$('browser-voice').parentElement===w.$('voice-home'),'voice controls sit under the composer');
    assert.equal(mutations(w).length,0);assert.equal(w.voices.length,1);assert.equal(w.voices[0].active,false);
    assert.equal(w.$('document-options').parentElement.parentElement.id,'message-form');
  },
  async a_late_send_confirmation_never_overrides_a_newer_chat_selection(){
    // Codex-Review 19.09.2026, Befund c: waehrend die Bestaetigung einer Nachricht an Chat 1 unterwegs ist,
    // waehlt der Nutzer Chat 2. Die spaete Bestaetigung darf die Auswahl nicht auf Chat 1 zurueckdrehen.
    const w=await world({chats:[chatDetail(CH1,{messages:[userMessage(1,'Alter Chat',{delivery:delivery()})]}),chatDetail(CH2,{messages:[userMessage(1,'Neuer Chat',{delivery:delivery()})]})]});
    await selectChat(w,CH1);
    const gate=deferred();let held=null;
    w.respond(req=>{if(req.url.endsWith('/messages')&&req.method==='POST'){held=req;return gate.promise;}});
    const sending=w.send('Besorg mir für morgen ein gutes Hotel in Hamburg.');await flush();
    assert(held,'the POST is in flight');assert.equal(w.app.chat.isSending(),true);
    await selectChat(w,CH2);
    assert.equal(w.app.chat.selectedId(),CH2);
    gate.resolve(response({delivery_id:'cd-'+'a'.repeat(16),status:'accepted'},202));
    await sending;await flush();
    assert.equal(w.app.chat.selectedId(),CH2,'the late confirmation switched the selection back');
    assert.equal(w.$('chat').dataset.conversationId,CH2);assert.equal(w.localStorage.getItem('solvio.chat.selected.v1'),CH2);
    assert.equal(w.$('chat-title').textContent,'Zweiter Chat');
    assert.equal(w.$('chat-messages').querySelector('article').querySelector('p').textContent,'Neuer Chat');
    assert.equal(w.app.chat.isSending(),false);assert.equal(w.$('message-text').value,'');
    const rows=pendingRows(w);assert.equal(rows.length,1);assert.equal(rows[0].conversation_id,CH1);assert.match(rows[0].delivery_id,/^cd-/);
    assert.equal(w.$('chat-list').querySelectorAll('button').find(b=>b.dataset.conversationId===CH2).getAttribute('aria-pressed'),'true');
    // Gegenstueck: ohne gewaehlten Chat legt die erste Nachricht einen an, und der wird gewaehlt.
    const fresh=await world({chats:[]});await fresh.send('Wie wird das Wetter morgen in Hamburg?');
    assert(fresh.app.chat.selectedId(),'a chat created by the first message is selected');
  },
  async first_message_without_a_chat_creates_one_idempotently_before_sending(){
    const w=await world({chats:[]});let posts=0;
    w.respond(req=>{if(req.url==='/v1/conversations'&&req.method==='POST'&&++posts===1)throw Error('lost');});
    await w.send('Wie wird das Wetter morgen in Hamburg?');
    assert.equal(mutations(w).length,1);assert.equal(w.app.chat.uncertain,true);assert.deepEqual(pendingRows(w),[]);
    w.$('message-retry').click();await flush();
    const calls=mutations(w);assert.equal(calls.length,3);assert.deepEqual(calls[0].body,calls[1].body);
    assert.equal(calls[2].url,'/v1/conversations/'+w.app.chat.selectedId()+'/messages');
    assert.equal(calls[2].body.message.conversation_id,w.app.chat.selectedId());
    assert.equal(w.$('chat-list').querySelectorAll('button').length,1);assert.equal(w.$('message-text').value,'');
    assert.equal(w.$('chat-messages').querySelector('article').querySelector('p').textContent,'Wie wird das Wetter morgen in Hamburg?');
  },
  async canonical_result_uses_one_node_across_detail_and_workroom(){
    const w=await world(),result=w.$('result'),oldCard=w.$('home-activity-list').querySelector('button');
    oldCard.focus();await w.app.selectRun(A);
    assert.equal(w.app.state.view,'detail');assert.equal(result.parentElement,w.$('conversation-result'));
    assert(w.document.activeElement===w.$('conversation'),'focus moves from the recent card into its conversation');
    assert.deepEqual(plain(w.$('conversation').lastScroll),{block:'start'});
    assert.equal(oldCard.isConnected,false,'the formerly focused recent card must not survive as a duplicate');
    assert.equal(w.$('conversation-objective').textContent,w.runs.get(A).auftrag);
    assert.match(result.textContent,/bestätigtes Ergebnis/);assert.equal(w.$('conversation').hidden,false);
    const retained=result.querySelector('details');retained.open=true;
    await w.app.showView('workroom');assert.equal(w.$('result'),result);assert.equal(result.parentElement,w.$('workroom-controls'));
    assert.equal(result.querySelector('details'),retained);assert.equal(retained.open,true);
    assert.match(w.$('hermes-frame').getAttribute('src'),new RegExp(A+'$'));
    await w.app.showView('overview');assert.equal(result.parentElement,w.$('conversation-result'));
    assert.equal(w.$('hermes-frame').getAttribute('src'),null);assert.equal(mutations(w).length,0);
    assert(w.$('overview-view').querySelector('#result')===null,'the run result never renders inside the chat view');
  },
  async newer_selection_defeats_late_old_run_response(){
    const w=await world(),gate=deferred();w.respond(req=>req.url==='/v1/agent/runs/'+A?gate.promise:undefined);
    const old=w.app.selectRun(A);await flush();await w.app.selectRun(B);gate.resolve(response(run()));await old;
    assert.equal(w.app.state.selected,B);assert.equal(w.$('result').dataset.runId,B);
    assert.equal(w.$('conversation-objective').textContent,run(B).auftrag);assert.doesNotMatch(w.$('result').textContent,new RegExp(A));
  },
  async uncertain_task_start_preserves_exact_body_and_lands_in_the_detail_view(){
    const w=await world();let posts=0;w.respond(req=>{if(req.url==='/v1/agent/tasks'){posts++;if(posts===1)throw Error('lost response');return response({run_id:A,annahme:'accepted'},201);}});
    await w.app.showView('tasks');await w.submit('Vergleiche die angehängten Angaben.');const submitted=w.app.state.submitted;
    assert.equal(submitted.uncertain,true);assert.equal(w.$('objective').disabled,true);
    assert.equal(w.$('objective').value,'Vergleiche die angehängten Angaben.');
    w.$('task-retry').click();await flush();
    const attempts=mutations(w);assert.equal(attempts.length,2);assert.deepEqual(attempts[0].body,attempts[1].body);
    assert.equal(attempts[0].body.task.file_request,undefined);assert.equal(attempts[0].body.task.document_request,undefined);
    assert.equal(w.app.state.selected,A);assert.equal(w.app.state.view,'detail');assert.equal(w.app.state.submitted,null);
  },
  async scope_and_exact_action_bytes_are_not_inferred_from_chat_text(){
    for(const scope of ['research','build','action']){
      const w=await world();w.respond(req=>req.url==='/v1/agent/tasks'?response({run_id:A,annahme:'accepted'},201):undefined);
      w.$('repository').value='/tmp/fixture-repo';w.$('action-mode').value='exact';
      await w.submit('Sende keine Mail; prüfe diese konkrete Aufgabe.',scope);
      const task=mutations(w)[0].body.task;assert.equal(task.scope,scope);
      assert.equal(task.target_repo,scope==='build'?'/tmp/fixture-repo':'');
      if(scope==='action')assert.deepEqual(task.action_request,w.composer.contract);
      else assert.equal(task.action_request,undefined);
      assert.equal(task.action_intent,undefined);assert.equal(mutations(w).length,1);
    }
  },
  async unconfirmed_canonical_answer_survives_navigation_and_blocks_unload(){
    const w=await world();const waiting=run(A,{zustand:'Wartet auf dich',zustand_code:'WAITING_USER',offen:true,ergebnis:'',
      action_intent:{status:'waiting_user',question:{id:'question-1',revision:1,digest:'bound-question-digest',field:'duration',prompt:'Wie viele Minuten?',input_type:'number'}}});
    w.runs.set(A,waiting);w.respond(req=>{if(req.url.endsWith('/action-answer'))throw Error('lost answer response');});
    await w.app.selectRun(A);w.$('action-answer').value='60';
    const form=w.$('action-answer').parentElement;form.dispatchEvent({type:'submit'});await flush();
    assert.equal(w.app.actionIntent.uncertain,true);
    await w.app.showView('overview');await w.app.selectRun(B);await w.app.selectRun(A);
    assert.equal(w.$('action-answer').value,'60');assert.equal(w.$('action-answer').disabled,true);assert.equal(w.app.actionIntent.uncertain,true);
    let blocked=false;w.window.dispatchEvent({type:'beforeunload',preventDefault(){blocked=true;}});assert.equal(blocked,true);
    assert.equal(mutations(w).length,1);assert.equal(mutations(w)[0].url,'/v1/agent/runs/'+A+'/action-answer');
  },
  async decisions_and_bound_files_stay_inline_while_supporting_details_collapse(){
    const w=await world(),id='file_one',base='/v1/agent/runs/'+A+'/artifacts/'+id;
    const detail=run(A,{zustand:'Wartet auf dich',zustand_code:'WAITING_USER',offen:true,
      wartet_auf:{handlung:'Bitte entscheide über den nächsten Schritt.',grund:'Anmeldung fehlt.'},
      dateien:[{id,name:'Ergebnis.txt',mime_type:'text/plain',size:12,sha256:'a'.repeat(64),download_url:base+'/download',preview_kind:'none'}]});
    w.runs.set(A,detail);await w.app.selectRun(A);const result=w.$('result');
    const file=result.querySelector('.result-file-card'),boundary=result.querySelector('.boundary');
    assert(file&&boundary);assert.equal(outsideDetails(file,result),true);assert.equal(outsideDetails(boundary,result),true);
    assert.equal(file.querySelector('a').href,base+'/download');
    const support=result.querySelector('details');assert.equal(support.open,false);assert.match(support.textContent,/Quellen/);
    assert.match(support.textContent,/Zusatzkosten/);assert.equal(result.querySelectorAll('.result-file-card').length,1);
  },
  async offline_and_logout_keep_run_read_only_then_remove_private_content(){
    const w=await world();await w.app.selectRun(A);const result=w.$('result');
    w.app.setConnected(false);assert.equal(w.$('offline').hidden,false);assert.equal(w.$('open-task-workroom').disabled,true);
    assert.equal(w.$('task-submit').disabled,true);assert.equal(w.$('message-submit').disabled,true);assert.equal(w.$('new-chat').disabled,true);
    assert.equal(w.app.state.selected,A);assert.match(result.textContent,/bestätigtes Ergebnis/);
    const gate=deferred();w.respond(req=>req.url==='/v1/agent/runs/'+A?gate.promise:undefined);const old=w.app.selectRun(A);await flush();
    w.app.showSession(null);gate.resolve(response(run()));await old;
    assert.equal(w.$('workspace').hidden,true);assert.equal(result.textContent,'');assert.equal(w.$('conversation').hidden,true);
    assert.equal(w.$('conversation-objective').textContent.includes(run().auftrag),false);assert.equal(mutations(w).length,0);
  },
  async conversation_navigation_preserves_single_voice_controller_and_modal_mount(){
    const w=await world(),voice=w.voices[0],node=w.$('browser-voice'),ends=voice.ends;
    voice.emit({active:true,closing:false,uncertain:false});assert.equal(node.parentElement?.id,'voice-focus-controls');
    await w.app.selectRun(A);await w.app.showView('workroom');await w.app.showView('overview');
    assert.equal(w.voices.length,1);assert.equal(w.$('browser-voice'),node);assert.equal(voice.ends,ends);assert.equal(voice.active,true);
    w.app.openDecision();assert.equal(node.parentElement?.id,'voice-dialog');
    w.$('decision-dialog').close();assert.equal(node.parentElement?.id,'voice-focus-controls');
    voice.emit({active:false,closing:true,uncertain:false});await selectChat(w,CH1);assert.equal(node.parentElement?.id,'voice-focus-controls');
    voice.emit({active:false,closing:false,uncertain:true});await w.app.showView('tasks');assert.equal(node.parentElement?.id,'voice-dock');
    assert.equal(w.voices.length,1);assert.equal(voice.ends,ends);assert.equal(mutations(w).length,0);
  }
};
(async()=>{const results=[];for(const [name,test] of Object.entries(cases)){
  try{await test();results.push({name,passed:true});}catch(error){results.push({name,passed:false,error:error.stack});}}
  console.log(JSON.stringify({actualSources:['app.js','chat.js','canonical.js','action-intent.js','browser-voice.js','index.html'],browser:false,network:false,results}));
  if(results.some(r=>!r.passed))process.exitCode=1;
})().catch(error=>{console.error(error.stack);process.exitCode=1;});
