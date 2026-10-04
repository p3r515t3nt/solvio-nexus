/* Durable chats over /v1/conversations. The Core owns every chat, message,
 * delivery and task; this module only shows the last read state and keeps
 * delivery identifiers (never texts) so a lost reply can be checked, not
 * blindly resent. Task cards reuse the canonical run renderer. */
import {requestDigest} from './canonical.js';

const SELECTED_KEY='solvio.chat.selected.v1',PENDING_KEY='solvio.chat.pending.v1';
const CONVERSATION=/^c-[a-f0-9]{16}$/,DELIVERY=/^cd-[a-f0-9]{16}$/,CLIENT_ID=/^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$/,DIGEST=/^[a-f0-9]{64}$/;
// 503-Antworten, die der Core selbst gibt, BEVOR er etwas annimmt (§2.5): kein Zweifel, ein Grund.
const REFUSED_503=new Set(['cognitive_router_unavailable','agent_runtime_disabled','conversation_store_unavailable']);
const FAST_POLL=3000,MAX_BACKOFF=60000,MAX_PENDING=50;

export function createChat({api,el,button,empty,message,errors,notify,renderDetail,stopFilePreviews,confirmAction,openDecision,busyForm,
  getSession,getEpoch,isConnected,runtimeAvailable,getDocument,clearDocument,onSelection=()=>{},onChange=()=>{},beforeSubmit=async()=>{},isVoiceActive=()=>false,canRecoverVoice=()=>false,recoverVoice=async()=>false,canChangeChat=()=>true}){
  const $=id=>document.getElementById(id);
  const chat={list:null,listError:null,selected:null,detail:null,detailError:null,epoch:0,detailRead:0,pending:[],sending:null,creating:null,preparing:false,
    timer:null,backoff:0,reconcile:false,unconfirmed:new Set()};
  const cards=new Map();
  const store={
    read(key){try{return localStorage.getItem(key);}catch{return null;}},
    write(key,value){try{if(value===null)localStorage.removeItem(key);else localStorage.setItem(key,value);}catch{}}
  };
  // Only identifiers and digests are persisted. A malformed or oversized
  // entry is dropped rather than interpreted.
  function loadPending(){
    let rows=[];try{rows=JSON.parse(store.read(PENDING_KEY)||'[]');}catch{rows=[];}
    if(!Array.isArray(rows))rows=[];
    chat.pending=rows.filter(row=>row&&typeof row==='object'&&CONVERSATION.test(row.conversation_id)&&CLIENT_ID.test(row.client_message_id)&&
      DIGEST.test(row.digest)&&(row.delivery_id===undefined||DELIVERY.test(row.delivery_id))&&Number.isFinite(row.created_at))
      .slice(-MAX_PENDING).map(row=>({conversation_id:row.conversation_id,client_message_id:row.client_message_id,digest:row.digest,
        ...(row.delivery_id?{delivery_id:row.delivery_id}:{}),created_at:row.created_at}));
  }
  function savePending(){store.write(PENDING_KEY,chat.pending.length?JSON.stringify(chat.pending):null);}
  function pendingFor(conversationId){return chat.pending.filter(row=>row.conversation_id===conversationId);}
  function removePending(clientMessageId){
    chat.pending=chat.pending.filter(row=>row.client_message_id!==clientMessageId);chat.unconfirmed.delete(clientMessageId);savePending();
  }
  function current(epoch,appEpoch){return epoch===chat.epoch&&appEpoch===getEpoch()&&!!getSession();}
  function relative(value){
    if(!Number.isFinite(value))return 'Zeit unbekannt';
    const seconds=Math.max(0,Date.now()/1000-value);
    if(seconds<60)return 'gerade eben';
    if(seconds<3600)return `vor ${Math.round(seconds/60)} Min`;
    if(seconds<86400)return `vor ${Math.round(seconds/3600)} Std`;
    if(seconds<172800)return 'gestern';
    return new Date(value*1000).toLocaleDateString('de-DE',{day:'2-digit',month:'2-digit'});
  }
  function listEntry(id){return (chat.list||[]).find(c=>c.conversation_id===id);}
  function readOnly(){return (chat.detail?.conversation?.conversation_id===chat.selected?chat.detail.conversation:listEntry(chat.selected))?.read_only===true;}
  function validConversation(c){return c&&typeof c==='object'&&CONVERSATION.test(c.conversation_id);}
  function openWork(detail){
    return !!detail&&(Number(detail.deliveries_open)>0||(Array.isArray(detail.auftraege)&&detail.auftraege.some(r=>r?.offen===true)));
  }
  function clearTimer(){if(chat.timer!==null){clearTimeout(chat.timer);chat.timer=null;}}
  function schedule(delay){
    clearTimer();if(!chat.selected||!getSession())return;
    const epoch=chat.epoch,appEpoch=getEpoch();
    chat.timer=setTimeout(()=>{chat.timer=null;if(!current(epoch,appEpoch))return;
      if(document.hidden){schedule(delay);return;}void loadDetail(epoch);},delay);
  }
  function syncPanel(){ $('chat-list-panel').open=false; }
  function renderList(){
    const nav=$('chat-list'),list=chat.list;
    const open=(list||[]).reduce((n,c)=>n+(Number(c.open_task_count)>0||Number(c.open_delivery_count)>0?1:0),0);
    $('chat-list-summary').textContent=list===null?'':`${list.length} ${list.length===1?'Chat':'Chats'}${open?` · ${open} in Arbeit`:''}`;
    if(nav.contains(document.activeElement))return;
    const rows=[];
    if(list===null)rows.push(empty(chat.listError?message(chat.listError):'Chats werden gelesen.'));
    else{
      for(const c of list){
        const selected=c.conversation_id===chat.selected;
        const row=button('',()=>select(c.conversation_id),'chat-row'+(selected?' selected':''));
        row.setAttribute('aria-pressed',String(selected));row.dataset.conversationId=c.conversation_id;
        row.append(el('span',c.title||'Neuer Chat','chat-row-title'));
        const meta=el('span','','chat-row-meta');meta.append(el('time',relative(c.last_activity_at)));
        if(Number(c.open_task_count)>0){const dot=el('span','','chat-dot');dot.title='Ein Auftrag läuft';dot.setAttribute('aria-label','Ein Auftrag läuft');meta.append(dot);}
        if(Number(c.open_delivery_count)>0)meta.append(el('span',String(c.open_delivery_count),'chat-count'));
        if(pendingFor(c.conversation_id).some(row=>chat.unconfirmed.has(row.client_message_id)))meta.append(el('span','Nachricht ungewiss','chat-unconfirmed'));
        row.append(meta);rows.push(row);
      }
      if(!list.length)rows.push(empty('Noch kein Chat. Deine erste Nachricht beginnt einen.'));
    }
    nav.replaceChildren(...rows);
  }
  function embeddedQuestion(r){
    const q=r.action_intent?.question,section=el('section','','action-question boundary');section.hidden=true;
    if(r.action_intent?.status==='waiting_user'&&r.zustand_code==='WAITING_USER'&&typeof q?.prompt==='string'&&q.prompt.trim()){
      section.hidden=false;section.append(el('strong','Noch eine Angabe'),el('p',q.prompt),
        button('Im Auftrag beantworten',()=>onSelection(r.id),'primary'));
    }
    return section;
  }
  function taskCard(run){
    const container=cards.get(run.id)||el('div','','chat-task-card');cards.set(run.id,container);
    container.dataset.chatRunId=run.id;
    renderDetail(run,container,{embedded:true,reread:reload,question:embeddedQuestion});
    return container;
  }
  function discardCards(keep=new Set()){
    for(const [id,card] of cards)if(!keep.has(id)){stopFilePreviews(true,card);cards.delete(id);}
  }
  function runFor(detail,delivery){
    const runs=Array.isArray(detail.auftraege)?detail.auftraege.filter(r=>r&&typeof r.id==='string'):[];
    if(delivery?.run_id){const exact=runs.find(r=>r.id===delivery.run_id);if(exact)return exact;}
    if(delivery?.task_id){
      const same=runs.filter(r=>r.aufgabe===delivery.task_id);
      return same.sort((a,b)=>(b.task_revision?.revision||1)-(a.task_revision?.revision||1))[0]||null;
    }
    return null;
  }
  function deliveryLine(delivery){
    if(!delivery||typeof delivery!=='object')return null;
    if(delivery.status==='accepted')return el('p','SOLVIO liest …','small muted chat-progress');
    if(delivery.status==='running')return el('p','SOLVIO arbeitet …','small muted chat-progress');
    if(delivery.status==='blocked'){
      const code=typeof delivery.error_code==='string'?delivery.error_code:'';
      return el('p',errors[code]||(code?`Diese Nachricht wurde nicht verarbeitet („${code}“). Es wurde kein Erfolg bestätigt.`:'Diese Nachricht wurde nicht verarbeitet. Es wurde kein Erfolg bestätigt.'),'error chat-blocked');
    }
    return null;
  }
  function sourceChatLink(delivery){
    const source=delivery?.source_chat,id=source?.conversation_id;
    if(typeof id!=='string'||id.length!==18||!CONVERSATION.test(id)||id===chat.selected||typeof source.title!=='string')return null;
    const title=source.title.trim().slice(0,80)||'Neuer Chat';
    // Nur der Core liefert die Kennung. Der Titel bleibt Text; es gibt keine fremde URL.
    return button('Aus Chat '+title,control=>{
      // Der Schutz fokussierter Auftragsfelder darf den bewussten Chatwechsel nicht festhalten.
      control.blur();return select(id);
    },'quiet small chat-source');
  }
  function renderHint(){
    const hint=$('chat-hint');hint.replaceChildren();
    if(readOnly())hint.append(el('span','Gespräch am Raspberry Pi · zum Nachlesen. Raumgespräche werden nach 90 Tagen ohne Aktivität entfernt. '),button('Neuer privater Chat',()=>newChat(),'quiet'));

    const unconfirmed=chat.selected?pendingFor(chat.selected).filter(row=>chat.unconfirmed.has(row.client_message_id)):[];
    if(unconfirmed.length){
      hint.append(el('span',unconfirmed.length===1?'Eine Nachricht ist möglicherweise nicht angekommen. Prüfe den Verlauf, bevor du sie erneut schreibst. ':
        `${unconfirmed.length} Nachrichten sind möglicherweise nicht angekommen. Prüfe den Verlauf, bevor du sie erneut schreibst. `),
        button('Verlauf geprüft',()=>{for(const row of unconfirmed)removePending(row.client_message_id);renderHint();renderList();},'quiet'));
    }
    if(chat.creating?.uncertain){
      hint.append(el('span','Der neue Chat ist noch nicht bestätigt. '),Object.assign(button('Denselben Chat erneut anlegen',()=>newChat(),'quiet'),{id:'new-chat-retry'}));
    }
    if(chat.selected&&chat.detail&&chat.detailError)hint.append(el('span','Der aktuelle Stand dieses Chats konnte nicht gelesen werden; du siehst den zuletzt gelesenen. '+message(chat.detailError)));
    hint.hidden=!hint.children.length;
  }
  function renderChat(){
    onChange();
    const entry=chat.selected?listEntry(chat.selected):null;
    $('chat-title').textContent=chat.selected?(chat.detail?.conversation?.title||entry?.title||'Neuer Chat'):'Neuer Chat';
    $('chat-rename').hidden=!chat.selected;
    $('chat').dataset.conversationId=chat.selected||'';
    renderHint();
    const container=$('chat-messages');
    if(container.contains(document.activeElement))return;
    if(!chat.selected){discardCards();container.replaceChildren(empty('Schreib SOLVIO. Deine erste Nachricht beginnt einen Chat; Aufträge und Antworten erscheinen hier.'));return;}
    const detail=chat.detail;
    if(!detail||detail.conversation?.conversation_id!==chat.selected){
      discardCards();container.replaceChildren(empty(chat.detailError?message(chat.detailError):'Chat wird gelesen.'));return;
    }
    const nodes=[],used=new Set();
    for(const m of Array.isArray(detail.messages)?detail.messages:[]){
      if(!m||typeof m!=='object'||typeof m.text!=='string')continue;
      const user=m.role==='user';
      const article=el('article','','conversation-message '+(user?'conversation-user':'conversation-answer'));
      if(typeof m.message_id==='string')article.dataset.messageId=m.message_id;
      article.append(el('span',user?(readOnly()?'Im Raum':'Du'):'SOLVIO','message-author'),el('p',m.text,user?'':'result-copy'));
      if(user){
        const line=deliveryLine(m.delivery);if(line)article.append(line);
        const source=sourceChatLink(m.delivery);if(source)article.append(source);
      }
      nodes.push(article);
      if(user&&m.delivery){
        const run=runFor(detail,m.delivery);
        if(run&&!used.has(run.id)){used.add(run.id);nodes.push(taskCard(run));}
      }
    }
    for(const run of Array.isArray(detail.auftraege)?detail.auftraege:[]){
      if(!run||typeof run.id!=='string'||used.has(run.id))continue;
      used.add(run.id);const wrap=el('div','','chat-task-orphan');wrap.append(el('p','Auftrag in diesem Chat','small muted'),taskCard(run));nodes.push(wrap);
    }
    discardCards(used);
    if(!nodes.length)nodes.push(empty('Noch keine Nachricht in diesem Chat.'));
    const atEnd=container.scrollHeight-container.scrollTop-container.clientHeight<100;
    const lastId=nodes.at(-1)?.dataset.messageId||'';
    const changed=container.dataset.lastMessageId!==lastId;
    container.replaceChildren(...nodes);container.dataset.lastMessageId=lastId;
    if(atEnd&&changed)container.scrollTop=container.scrollHeight;
  }
  function syncControls(){
    $('chat').dataset.readOnly=String(readOnly());
    onChange();
    const session=!!getSession(),connected=isConnected()&&session;
    const busy=!connected||runtimeAvailable()===false||!!chat.sending||readOnly();
    const doc=getDocument();
    $('message-text').disabled=busy;
    $('message-submit').disabled=busy||chat.preparing||!!chat.creating?.sending||!!(doc&&doc.status!=='ready');
    $('document-file').disabled=busy;$('document-clear').disabled=busy;$('document-clear').hidden=!doc;
    if(doc)$('document-options').open=true;
    $('new-chat').disabled=!connected||chat.preparing||!!chat.creating?.sending||!canChangeChat();
    for(const row of $('chat-list').querySelectorAll('button'))row.disabled=!canChangeChat();
    $('chat-rename').disabled=!connected||!chat.selected;
    const retry=$('message-retry');if(retry)retry.disabled=!connected;
  }
  function setError(text){$('message-error').replaceChildren();if(text)$('message-error').textContent=text;}
  function reconcileDetail(detail){
    const id=detail.conversation.conversation_id,messages=Array.isArray(detail.messages)?detail.messages:[];
    let changed=false;
    for(const row of pendingFor(id)){
      if(chat.sending?.clientMessageId===row.client_message_id)continue;
      const found=messages.find(m=>m?.role==='user'&&m.delivery&&typeof m.delivery==='object'&&
        ((row.delivery_id&&m.delivery.delivery_id===row.delivery_id)||(typeof m.delivery.client_message_id==='string'&&m.delivery.client_message_id===row.client_message_id)));
      if(found){
        if(!row.delivery_id&&DELIVERY.test(found.delivery.delivery_id)){row.delivery_id=found.delivery.delivery_id;changed=true;}
        chat.unconfirmed.delete(row.client_message_id);
        if(['completed','blocked'].includes(found.delivery.status)){chat.pending=chat.pending.filter(r=>r!==row);changed=true;}
      }else if(row.delivery_id){chat.pending=chat.pending.filter(r=>r!==row);changed=true;}
      else chat.unconfirmed.add(row.client_message_id);
    }
    if(changed)savePending();
  }
  async function reconcileAll(){
    const appEpoch=getEpoch(),epoch=chat.epoch;
    for(const row of [...chat.pending]){
      if(!current(epoch,appEpoch))return;
      if(row.conversation_id===chat.selected&&!row.delivery_id)continue;
      try{
        if(row.delivery_id){
          const state=await api(`/v1/conversations/${encodeURIComponent(row.conversation_id)}/deliveries/${encodeURIComponent(row.delivery_id)}`);
          if(!current(epoch,appEpoch))return;
          if(['completed','blocked'].includes(state?.status))removePending(row.client_message_id);
        }else{
          const detail=await api('/v1/conversations/'+encodeURIComponent(row.conversation_id));
          if(!current(epoch,appEpoch))return;
          if(validConversation(detail?.conversation))reconcileDetail(detail);
        }
      }catch(e){
        if(!current(epoch,appEpoch))return;
        if(e.status===404||e.status===410)removePending(row.client_message_id);
      }
    }
    renderHint();renderList();
  }
  async function refreshList(){
    const appEpoch=getEpoch(),epoch=chat.epoch;
    if(!getSession())return;
    try{
      const data=await api('/v1/conversations?limit=30');
      if(!current(epoch,appEpoch))return;
      if(!Array.isArray(data?.conversations))throw {code:'invalid_response'};
      chat.list=data.conversations.filter(validConversation);chat.listError=null;
    }catch(e){if(!current(epoch,appEpoch))return;chat.listError=e;}
    renderList();renderChat();
  }
  async function loadDetail(epoch){
    const appEpoch=getEpoch(),id=chat.selected,read=++chat.detailRead;
    const currentRead=()=>current(epoch,appEpoch)&&read===chat.detailRead&&chat.selected===id;
    if(!id||!currentRead())return false;
    try{
      const data=await api('/v1/conversations/'+encodeURIComponent(id));
      if(!currentRead())return false;
      if(!validConversation(data?.conversation)||data.conversation.conversation_id!==id||!Array.isArray(data.messages))throw {code:'invalid_response'};
      chat.detail=data;chat.detailError=null;chat.backoff=0;
      reconcileDetail(data);renderChat();renderList();syncControls();
      if(openWork(data)||isVoiceActive())schedule(FAST_POLL);
      return true;
    }catch(e){
      if(!currentRead())return false;
      if(e.status===404||e.status===410){deselect();notify(message(e));void refreshList();return false;}
      chat.detailError=e;renderChat();
      schedule(Math.min(FAST_POLL*2**chat.backoff++,MAX_BACKOFF));
      return false;
    }
  }
  function deselect(){
    chat.selected=null;chat.detail=null;chat.detailError=null;chat.epoch++;clearTimer();store.write(SELECTED_KEY,null);
    discardCards();renderList();renderChat();syncControls();
  }
  async function select(id,{forVoice=false}={}){
    if(!CONVERSATION.test(id))return;
    if(id!==chat.selected&&!forVoice&&!canChangeChat()){notify('Beende das laufende Gespräch, bevor du den Chat wechselst.');return;}
    if(chat.selected!==id){
      chat.selected=id;chat.detail=null;chat.detailError=null;chat.backoff=0;discardCards();store.write(SELECTED_KEY,id);setError('');
    }
    const epoch=++chat.epoch;clearTimer();
    renderList();renderChat();syncControls();syncPanel();
    $('chat-messages').scrollTop=0;
    await loadDetail(epoch);
  }
  async function reload(){clearTimer();return chat.selected?await loadDetail(chat.epoch):false;}
  async function refresh(){
    if(!getSession())return;
    const appEpoch=getEpoch(),epoch=chat.epoch;
    await refreshList();if(!current(epoch,appEpoch))return;
    if(chat.selected&&chat.timer===null)await loadDetail(epoch);
    if(!current(epoch,appEpoch))return;
    // One reconcile pass per session: stored delivery identifiers are checked
    // against the Core, never resent.
    if(chat.reconcile){chat.reconcile=false;await reconcileAll();}
  }
  async function createConversation(clientRequestId){
    const created=await api('/v1/conversations',{method:'POST',body:{client_request_id:clientRequestId}});
    if(!CONVERSATION.test(created?.conversation_id))throw {code:'invalid_response'};
    return created.conversation_id;
  }
  function prepareIdea(text){
    if(!getSession()||!isConnected()||!canChangeChat()||chat.sending||chat.preparing||chat.creating||
       chat.pending.length||$('message-text').value.trim()||getDocument())return false;
    clearTimer();chat.epoch++;chat.selected=null;chat.detail=null;chat.detailError=null;
    chat.backoff=0;chat.reconcile=false;discardCards();store.write(SELECTED_KEY,null);
    $('message-text').value=text;setError('');renderList();renderChat();syncControls();onChange();return true;
  }
  async function newChat({forVoice=false}={}){
    if(!forVoice&&!canChangeChat()){notify('Beende das laufende Gespräch, bevor du einen neuen Chat beginnst.');return;}
    if(chat.creating?.sending||!getSession()||!isConnected())return;
    const appEpoch=getEpoch(),epoch=chat.epoch;
    const creating=chat.creating||{client_request_id:crypto.randomUUID(),sending:false,uncertain:false};
    chat.creating=creating;creating.sending=true;creating.uncertain=false;syncControls();renderHint();
    try{
      const id=await createConversation(creating.client_request_id);
      if(!current(epoch,appEpoch))return;
      chat.creating=null;
      await refreshList();if(!current(epoch,appEpoch))return;
      await select(id,{forVoice});$('message-text').focus({preventScroll:true});
      return chat.selected===id&&chat.detail?.conversation?.conversation_id===id&&!chat.detailError?id:null;
    }catch(e){
      if(!current(epoch,appEpoch))return;
      if(e.code==='network'||e.code==='invalid_response'||e.status>=500){creating.uncertain=true;}
      else{chat.creating=null;notify(message(e));}
    }finally{creating.sending=false;if(appEpoch===getEpoch()){syncControls();renderHint();}}
  }
  async function ensureSelected(){
    if(chat.sending||chat.preparing||chat.creating?.sending||!getSession()||!isConnected())throw {voice:'Der Chat ist noch beschäftigt. Bitte warte auf die Bestätigung.'};
    const id=chat.selected,epoch=chat.epoch;
    if(id){
      await loadDetail(epoch);
      if(chat.selected===id&&chat.detail?.conversation?.conversation_id===id&&!chat.detailError&&!readOnly())return id;
    }else{
      const created=await newChat({forVoice:true});if(created)return created;
    }
    throw {voice:'Der Chat konnte nicht bestätigt werden. Prüfe den Verlauf vor dem Sprachstart.'};
  }
  async function transmit(attempt){
    if(attempt.sending||chat.sending!==attempt||!getSession()||!isConnected())return;
    const appEpoch=getEpoch();
    const live=()=>appEpoch===getEpoch()&&chat.sending===attempt&&!!getSession();
    attempt.sending=true;syncControls();setError('');
    try{
      if(!attempt.conversationId){
        attempt.creationId||=crypto.randomUUID();
        const id=await createConversation(attempt.creationId);if(!live())return;
        attempt.conversationId=id;
      }
      await beforeSubmit(attempt.conversationId);if(!live())return;
      if(!attempt.body){
        const body={conversation_id:attempt.conversationId,client_message_id:attempt.clientMessageId,text:attempt.text};
        if(attempt.attachments)body.attachments=attempt.attachments;
        attempt.body=Object.freeze(body);attempt.digest=await requestDigest(body);if(!live())return;
      }
      if(!chat.pending.some(row=>row.client_message_id===attempt.clientMessageId)){
        chat.pending.push({conversation_id:attempt.conversationId,client_message_id:attempt.clientMessageId,digest:attempt.digest,created_at:Date.now()/1000});
        if(chat.pending.length>MAX_PENDING)chat.pending=chat.pending.slice(-MAX_PENDING);
        savePending();
      }
      const accepted=await api(`/v1/conversations/${encodeURIComponent(attempt.conversationId)}/messages`,{method:'POST',body:{message:attempt.body}});
      if(!live())return;
      if(!DELIVERY.test(accepted?.delivery_id))throw {code:'invalid_response'};
      const row=chat.pending.find(r=>r.client_message_id===attempt.clientMessageId);
      if(row){row.delivery_id=accepted.delivery_id;savePending();}
      chat.sending=null;$('message-text').value='';clearDocument();syncControls();
      // Eine spaete Bestaetigung wechselt nie die inzwischen gewaehlte Auswahl (Codex-Review 19.09.2026, Befund c):
      // nur ein von dieser Nachricht erst angelegter Chat wird gewaehlt — und nur, solange nichts anderes gewaehlt ist.
      if(chat.selected===attempt.conversationId){await reload();void refreshList();}
      else{await refreshList();if(appEpoch!==getEpoch())return;if(!chat.selected&&attempt.creationId)await select(attempt.conversationId);}
    }catch(e){
      if(!live())return;
      if(e.code==='voice_handoff_unconfirmed'){
        setError(message(e));appendVoiceRecovery(attempt.conversationId);
        $('message-error').append(Object.assign(button('Dieselbe Nachricht erneut senden',()=>live()?transmit(attempt):undefined),{id:'message-retry'}));
      }else if(e.status===503&&REFUSED_503.has(e.code)){
        // Der Core hat geantwortet und NICHT angenommen — nichts ist persistiert, nichts
        // ungewiss: der Grund wird genannt, dieselbe Nachricht darf erneut gesendet werden.
        $('message-error').replaceChildren(el('span',message(e)+' Die Nachricht wurde nicht angenommen. '),
          Object.assign(button('Erneut senden',()=>live()?transmit(attempt):undefined),{id:'message-retry'}));
      }else if(e.code==='network'||e.code==='invalid_response'||e.status>=500){
        attempt.uncertain=true;
        $('message-error').replaceChildren(el('span','Zustellung ungewiss. Erneut senden übermittelt dieselbe Nachricht mit derselben Kennung. '),
          Object.assign(button('Dieselbe Nachricht erneut senden',()=>live()?transmit(attempt):undefined),{id:'message-retry'}));
      }else{
        removePending(attempt.clientMessageId);chat.sending=null;setError(message(e));
        if(e.code==='unknown_conversation'){deselect();void refreshList();}
      }
    }finally{attempt.sending=false;if(appEpoch===getEpoch())syncControls();}
  }
  function appendVoiceRecovery(id){
    if(!canRecoverVoice(id))return;
    $('message-error').append(el('p','Letzter Sprachverlauf möglicherweise unvollständig.'),button('Mit vorhandenem Verlauf weiterschreiben',async control=>{
      control.disabled=true;
      try{
        if(chat.selected!==id||!await recoverVoice(id))return;
        setError('Letzter Sprachverlauf möglicherweise unvollständig. Dein Entwurf bleibt erhalten; du kannst ihn jetzt senden.');
        if(chat.sending)$('message-error').append(Object.assign(button('Dieselbe Nachricht erneut senden',()=>transmit(chat.sending)),{id:'message-retry'}));
      }finally{control.disabled=false;}
    },'quiet'));
  }
  async function submit(){
    if(readOnly()){setError('Das Raumgespräch ist zum Nachlesen. Starte einen neuen privaten Chat.');return;}
    if(chat.sending||chat.preparing||chat.creating?.sending||!getSession()||!isConnected()||runtimeAvailable()===false)return;
    const text=$('message-text').value.trim();
    if(!text)return;
    if(text.length>4000||text.includes('\u0000')){setError('Bitte begrenze deine Nachricht auf 4.000 Zeichen.');return;}
    const doc=getDocument();
    if(doc&&doc.status!=='ready')return;
    const selected=chat.selected,appEpoch=getEpoch();
    chat.preparing=true;syncControls();setError('');
    try{
      await beforeSubmit(selected);
      if(appEpoch!==getEpoch()||!getSession())return;
      if(chat.selected!==selected||$('message-text').value.trim()!==text||getDocument()!==doc){
        setError('Dein Entwurf oder Chat hat sich geändert. Bitte prüfe ihn vor dem Senden.');return;
      }
    }catch(e){
      if(appEpoch===getEpoch()){
        setError(message(e));
        if(e.code==='voice_handoff_unconfirmed')appendVoiceRecovery(selected);
      }
      return;
    }
    finally{if(appEpoch===getEpoch()){chat.preparing=false;syncControls();}}
    chat.sending={text,attachments:doc?.request||null,clientMessageId:crypto.randomUUID(),conversationId:chat.selected,creationId:null,
      body:null,digest:'',sending:false,uncertain:false};
    await transmit(chat.sending);
  }
  function rename(){
    if(!chat.selected||!isConnected())return;
    const id=chat.selected,appEpoch=getEpoch(),epoch=chat.epoch,content=$('decision-content');
    const form=el('form'),label=el('label','Titel dieses Chats'),input=el('input');
    label.htmlFor='chat-title-input';input.id='chat-title-input';input.type='text';input.maxLength=80;input.required=true;
    input.value=chat.detail?.conversation?.title||listEntry(id)?.title||'';
    const submit=el('button','Titel speichern','primary');submit.type='submit';const feedback=el('p','','error');
    form.append(label,input,submit,feedback);content.replaceChildren(el('h2','Chat umbenennen'),form);
    form.addEventListener('submit',async event=>{
      event.preventDefault();const title=input.value.trim();
      if(!title||title.length>80){feedback.textContent='Bitte wähle einen Titel mit 1 bis 80 Zeichen.';return;}
      busyForm(form,true);
      try{
        const data=await api('/v1/conversations/'+encodeURIComponent(id),{method:'PATCH',body:{title}});
        if(!current(epoch,appEpoch)||chat.selected!==id)return;
        if(validConversation(data?.conversation)&&chat.detail)chat.detail.conversation=data.conversation;
        $('decision-dialog').close();await refreshList();renderChat();
      }catch(e){if(appEpoch===getEpoch())feedback.textContent=message(e);}
      finally{if(appEpoch===getEpoch())busyForm(form,false);}
    });
    openDecision();
  }
  function reset(){
    clearTimer();chat.epoch++;chat.list=null;chat.listError=null;chat.detail=null;chat.detailError=null;chat.sending=null;chat.creating=null;chat.preparing=false;
    chat.unconfirmed.clear();discardCards();
    $('message-text').value='';setError('');$('chat-messages').replaceChildren();$('chat-list').replaceChildren();$('chat-hint').replaceChildren();$('chat-hint').hidden=true;
    $('chat-title').textContent='Neuer Chat';$('chat-list-summary').textContent='';
    if(getSession()){
      loadPending();
      const stored=store.read(SELECTED_KEY);chat.selected=CONVERSATION.test(stored||'')?stored:null;
      chat.reconcile=true;renderList();renderChat();
    }else{chat.selected=null;chat.reconcile=false;}
    syncControls();syncPanel();
  }
  $('message-form').addEventListener('submit',event=>{event.preventDefault();void submit();});
  $('new-chat').addEventListener('click',()=>void newChat());
  $('chat-rename').addEventListener('click',rename);
  if(typeof matchMedia==='function')matchMedia('(min-width: 900px)').addEventListener?.('change',syncPanel);
  return {reset,refresh,reload,select,prepareIdea,ensureSelected,syncControls,stop:clearTimer,
    readOnly,selectedId:()=>chat.selected,hasRun:id=>cards.has(id),isSending:()=>!!chat.sending,
    get uncertain(){return !!(chat.sending?.uncertain||chat.creating?.uncertain);},
    get state(){return chat;}};
}
