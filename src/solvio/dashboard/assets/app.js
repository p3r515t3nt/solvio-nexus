import {Presence} from './presence.js';
import {WindowShare} from './window-share.js';
import {BrowserVoice} from './browser-voice.js';
import {createActionComposer} from './action-composer.js';
import {createActionIntent} from './action-intent.js';
import {createChat} from './chat.js';

const $ = id => document.getElementById(id);
const presence = new Presence($('orb'), $('mascot'), $('glasses'));
const viewNames = {overview:'SOLVIO', feed:'Überblick', ideas:'Ideen', library:'Bibliothek', tasks:'Deine Aufträge.', detail:'Dein Auftrag.', workroom:'Dein Arbeitsraum.', knowledge:'Dein Wissen.', inbox:'Dein Posteingang.', activity:'Aktivitäten', settings:'Dein Rahmen.', connections:'Verbindungen'};
const state = {session:null, view:'overview', runs:null, selected:null, detail:null, memories:null,
  candidates:null, connected:false, refreshing:false, epoch:0, selectedEpoch:0, submitted:null, data:null,memoryPending:null,document:null,
  approvals:null, room:'hermes', voiceUI:null, voicePresence:null, voiceOutcome:false, voiceReturn:null, voiceFocusVisible:false, followup:null};
const library = {runs:null,next:null,loading:false,error:'',generation:0,query:'',rendered:null};
const readLists = Object.fromEntries(['activity','recurring'].map(kind=>[kind,{rows:null,stand:null,error:'',loading:false,generation:0,rendered:undefined}]));
let librarySearchTimer=null;
const documentLimits={rtf:65536,txt:65536,docx:1048576,odt:1048576};
let documentGeneration=0;
const actionComposer=createActionComposer({container:$('action-fields'),api,
  isCurrent:()=>!!state.session,onChange:()=>setTaskControls()});
const actionIntent=createActionIntent({api,isCurrent:()=>!!state.session,
  onRefresh:async id=>{if(state.selected===id)await selectRun(id,{open:false});}});
// Pin the canonical chat before microphone permission or any voice ticket.
const voice=new BrowserVoice($('browser-voice'),{api,getSession:()=>state.session,
  prepareStart:async()=>({conversation_id:await chat.ensureSelected()}),onState:snapshot=>{
  const previous=state.voiceUI;
  const ended=(previous?.active||previous?.closing)&&!snapshot.active&&!snapshot.closing;
  if(ended&&!snapshot.uncertain)state.voiceOutcome=true;
  if(snapshot.active){state.voiceOutcome=false;state.voiceReturn=null;}
  state.voiceUI=snapshot;
  // Audio closure alone is not a saved transcript. Keep the focus scene until
  // the existing flush proof AND a fresh read of this same canonical chat.
  if(ended&&voice.closedProof===true&&voice.flushed===true&&snapshot.conversationId===chat.selectedId()){
    void readVoiceHistory(snapshot.conversationId);
  }else{
    syncVoiceMount();chat.syncControls();
    if(snapshot.ready&&!previous?.ready)void chat.reload();
  }
},onLevel:level=>presence.setLevel(level),onPresence:mode=>{
  state.voicePresence=mode;syncPresence();
}});
const windowShare=new WindowShare($('window-share'),()=>state.session);
const errors = {room_conversation_read_only:'Dieses Raumgespräch ist hier zum Nachlesen. Starte einen neuen privaten Chat.',voice_handoff_unconfirmed:'Der Abschluss des Sprachchats ist noch nicht bestätigt. Dein Text bleibt erhalten und wurde nicht gesendet.',unauthorized:'Deine Anmeldung ist abgelaufen. Bitte melde dich erneut an.',
  invalid_enrollment:'Dieser Anmeldecode ist ungültig, abgelaufen oder schon verwendet.',
  untrusted_browser_origin:'Diese Browseradresse ist am Core noch nicht eingerichtet.',
  agent_runtime_disabled:'Die Aufgabenbearbeitung ist gerade nicht verfügbar.',
  cognitive_router_unavailable:'Die Aufgabenbearbeitung ist gerade nicht verfügbar.',
  core_stopping:'SOLVIO wird gerade beendet. Bitte prüfe den aktuellen Auftragsstand nach dem Neustart.',
  memory_unavailable:'Das Gedächtnis ist gerade nicht erreichbar.',
  cost_unbounded:'Der Core hat noch keinen verlässlichen Beleg für die Zusatzkosten. Die Ausführung wartet.',
  cost_recovery_required:'Der Ausgang einer Kostenbuchung ist ungewiss. Es wird kein zweiter Versuch gestartet.',
  quota_exhausted:'Das Kontingent ist erschöpft. Du entscheidest über den nächsten Schritt.',
  not_waiting:'Dieser Auftrag wartet nicht auf eine Fortsetzung.', not_cancellable:'Dieser Auftrag lässt sich gerade nicht abbrechen.',
  network:'Keine bestätigte Antwort. Bitte prüfe den aktuellen Stand vor einem neuen Auftrag.',
  invalid_task:'Die Aufgabe konnte mit diesen Angaben nicht angenommen werden.',
  account_selection_changed:'Der Auftrag oder die Kontobindung hat sich geändert. Bitte lies den aktuellen Stand erneut.',
  owner_configuration_required:'Diese Einstellung steht nur dem eingerichteten Owner zur Verfügung.'};
Object.assign(errors,{observation_resume_unavailable:'Für diese Beobachtung ist keine sichere Fortsetzung verfügbar.',
  activity_expired:'Die Frist dieser Beobachtung ist abgelaufen.',activity_source_revoked:'Die ursprüngliche Berechtigung wurde zurückgezogen.',
  observation_queue_unavailable:'Die Verarbeitung ist gerade ausgelastet. Die Beobachtung bleibt angehalten.'});
Object.assign(errors,{message_conflict:'Unter dieser Kennung wurde schon eine andere Nachricht angenommen. Bitte lies den Verlauf und schreibe sie neu.',
  unknown_conversation:'Dieser Chat ist am Core nicht (mehr) vorhanden.',unknown_delivery:'Diese Zustellung ist am Core nicht bekannt.',
  deliveries_open:'In diesem Chat wird noch eine Nachricht verarbeitet.',invalid_message:'Die Nachricht konnte mit diesen Angaben nicht angenommen werden.',
  invalid_title:'Bitte wähle einen Titel mit 1 bis 80 Zeichen.',conversation_deleted:'Dieser Chat wurde gelöscht.',
  conversation_store_unavailable:'Der Chatspeicher ist gerade nicht erreichbar.',
  processing_timeout:'Die Verarbeitung dieser Nachricht hat zu lange gedauert. Sie wurde nicht abgeschlossen.',
  source_revoked:'Deine Anmeldung ist inzwischen abgelaufen — bitte neu anmelden und die Nachricht erneut senden.',
  provider_output_invalid:'Die Antwort des Modells war nicht verwertbar. Es wurde kein Ergebnis übernommen.',
  provider_unavailable:'Der Anbieter ist gerade nicht erreichbar. Diese Nachricht wurde nicht verarbeitet.',
  quota:'Das Kontingent ist erschöpft. Diese Nachricht wurde nicht verarbeitet.',
  assessment_unavailable:'SOLVIO konnte diese Nachricht nicht einordnen. Es wurde kein Auftrag angelegt.',
  core_restarted_unresolved:'Der Core wurde neu gestartet, bevor diese Nachricht abgeschlossen war. Ihr Ausgang ist nicht bestätigt.',
  objective_too_long:'Der Auftragstext ist zu lang für einen Auftrag. Bitte fasse ihn kürzer.',
  followup_not_available:'Der genannte Auftrag lässt sich so nicht weiterbearbeiten.'});
const runStateNames={CREATED:'Angenommen',PLANNING:'Wird geplant',RUNNING:'In Arbeit',VERIFYING:'Wird geprüft',HARVESTING:'Ergebnis wird übernommen',SUCCEEDED:'Fertig',FAILED:'Fehlgeschlagen',CANCELLED:'Abgebrochen',KILLED:'Beendet',WAITING_USER:'Wartet auf dich',WAITING_APPROVAL:'Wartet auf Freigabe',WAITING_SPECIALIST:'Fachagent arbeitet',WAITING_CAPABILITY:'Werkzeug arbeitet'};
const timelineText=text=>String(text||'').replace(/\b[A-Z_]+\b/g,token=>runStateNames[token]||token);
const money = cents => Number.isInteger(cents) ? new Intl.NumberFormat('de-DE',{style:'currency',currency:'EUR'}).format(cents/100) : 'Unbekannt';
const when = value => { if (!value) return 'Zeit unbekannt'; const d = new Date(typeof value==='number'?value*1000:value); return isNaN(d)?'Zeit unbekannt':d.toLocaleString('de-DE',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}); };
const el = (tag,text='',cls='') => { const n=document.createElement(tag); if(text!==undefined&&text!==null)n.textContent=String(text); if(cls)n.className=cls; return n; };
const empty = text => el('p',text,'empty');
const button = (label, action, cls='') => { const b=el('button',label,cls); b.type='button'; b.addEventListener('click',()=>Promise.resolve(action(b)).catch(e=>notify(message(e)))); return b; };
const message = e => errors[e?.code] || (e?.code ? `Der Core meldet „${e.code}“. Es wurde kein Erfolg bestätigt.` : errors.network);
function notify(text){$('notice').textContent=text;$('notice').hidden=false;}
function busyForm(form, busy){for(const c of form.elements)c.disabled=busy;}
const chat=createChat({api,el,button,empty,message,errors,notify,renderDetail,stopFilePreviews,confirmAction,openDecision,busyForm,
  getSession:()=>state.session,getEpoch:()=>state.epoch,isConnected:()=>state.connected,
  runtimeAvailable:()=>state.data?state.data.runtime==='available':null,isVoiceActive:()=>voice.active||voice.closing,
  getDocument:()=>state.document,clearDocument,onSelection:id=>selectRun(id),onChange:()=>{syncPresence();syncVoiceMount();},
  beforeSubmit:async id=>{
    if(!voice.active&&!voice.closing&&!voice.handoffRequired&&!voice.isHandoffBlocked(id))return;
    if(!await voice.endAndWait(id))throw {code:'voice_handoff_unconfirmed'};
    if(id&&!await chat.reload())throw chat.state.detailError||{code:'network'};
  },
  canChangeChat:()=>!voice.active&&!voice.closing&&!state.voiceReturn?.loading,
  canRecoverVoice:id=>voice.isHandoffBlocked(id),
  recoverVoice:async id=>{
    const epoch=state.epoch,read=await chat.reload();
    if(!read||epoch!==state.epoch||chat.selectedId()!==id||chat.state.detailError)return false;
    return voice.continueWithStoredHistory(id);
  }});
async function readVoiceHistory(id){
  if(!state.session||!id||id!==chat.selectedId()||state.voiceReturn?.loading)return;
  const pending={id,epoch:state.epoch,loading:true,error:false};state.voiceReturn=pending;
  syncVoiceMount();chat.syncControls();
  const read=await chat.reload();
  if(state.voiceReturn!==pending||pending.epoch!==state.epoch)return;
  pending.loading=false;
  pending.error=read!==true||chat.selectedId()!==id||!!chat.state.detailError||chat.state.detail?.conversation?.conversation_id!==id;
  pending.reveal=!pending.error;
  syncVoiceMount();chat.syncControls();
}
$('voice-return-retry').addEventListener('click',()=>{if(state.voiceReturn?.error)void readVoiceHistory(state.voiceReturn.id);});
function syncVoiceMount(){
  const v=state.voiceUI,panel=$('browser-voice'),dialog=$('decision-dialog');
  if(state.voiceReturn&&!state.voiceReturn.loading&&chat.selectedId()&&state.voiceReturn.id!==chat.selectedId())state.voiceReturn=null;
  const focus=!!state.session&&!!(v?.active||v?.closing||state.voiceReturn?.loading);
  const visibleFocus=focus&&state.view==='overview'&&!dialog.open;
  const hadFocus=$('voice-focus').contains(document.activeElement);
  const wasVisible=state.voiceFocusVisible;state.voiceFocusVisible=visibleFocus;
  $('voice-focus').hidden=!focus;$('chat-text-view').hidden=focus;
  const identityHost=focus?$('voice-scene'):$('chat-identity-home');
  if($('conversation-welcome').parentElement!==identityHost)identityHost.append($('conversation-welcome'));
  $('chat-identity-home').hidden=focus;
  const live=!!(v?.active||v?.closing||v?.uncertain||state.voiceReturn?.loading);
  panel.dataset.engaged=String(live);
  const visible=live||(state.voiceOutcome&&state.view!=='overview');
  const host=visible&&state.session?(dialog.open?$('voice-dialog'):state.view!=='overview'?$('voice-dock'):focus?$('voice-focus-controls'):$('voice-home')):$('voice-home');
  if(panel.parentElement!==host){
    const focused=panel.contains(document.activeElement)?document.activeElement:null;
    host.append(panel);
    if(focused)focused.focus({preventScroll:true});
  }
  $('voice-start').disabled=!voice.available||voice.active||voice.closing||!!state.voiceReturn?.loading||chat.readOnly();
  const readError=state.voiceReturn?.error;
  const incomplete=!!state.session&&!focus&&((v?.conversationId===chat.selectedId()&&v?.uncertain)||voice.isHandoffBlocked(chat.selectedId()));
  $('voice-return-status').hidden=!readError&&!incomplete;
  $('voice-return-retry').hidden=!readError||state.voiceReturn.id!==chat.selectedId();
  const text=readError?'Das Gespräch ist beendet, aber der aktuelle Verlauf konnte nicht gelesen werden. Du siehst den zuletzt gelesenen Stand. Dein Entwurf bleibt erhalten.':incomplete?'Mikrofon aus. Letzter Sprachverlauf möglicherweise unvollständig. Der Abschluss ist nicht bestätigt.':'';
  if($('voice-return-message').textContent!==text)$('voice-return-message').textContent=text;
  $('voice-dock').hidden=host!==$('voice-dock');
  $('voice-dialog').hidden=host!==$('voice-dialog');
  $('workspace').classList.toggle('voice-docked',host===$('voice-dock'));
  // Disabling Beenden can move browser focus to body while closure is pending.
  const lostEndFocus=$('voice-end').disabled&&(document.activeElement===$('voice-end')||document.activeElement===document.body);
  if(visibleFocus&&(!wasVisible||lostEndFocus))($('voice-end').disabled?$('voice-focus'):$('voice-end')).focus({preventScroll:true});
  else if(wasVisible&&!focus&&hadFocus&&state.view==='overview'&&!dialog.open)$('chat').focus({preventScroll:true});
  // Rendering while hidden has no reliable scroll geometry. Consume this one
  // return anchor only when the confirmed chat is actually shown (also from a dock).
  if(!focus&&state.view==='overview'&&state.voiceReturn?.reveal&&state.voiceReturn.id===chat.selectedId()){
    $('chat-messages').scrollTop=$('chat-messages').scrollHeight;state.voiceReturn=null;
  }
}
function openDecision(){ $('decision-dialog').showModal();syncVoiceMount(); }
$('decision-dialog').addEventListener('close',syncVoiceMount);
function setTaskControls(){
  const busy=!state.connected||state.data?.runtime!=='available'||!!state.submitted;
  busyForm($('task-form'),busy);
  actionComposer.setBusy(busy);
  $('task-submit').disabled=busy;
  $('composer-mode').textContent={research:'Recherchieren / Lesen',build:'Programmieren',action:'Im Alltag erledigen'}[$('scope').value];
}
function clearDocument(clearInput=true){
  documentGeneration++;state.document=null;
  if(clearInput)$('document-file').value='';
  $('document-status').textContent='';$('document-error').textContent='';
  chat.syncControls();
}
async function chooseDocument(){
  if(chat.isSending())return;
  const chosen=Array.from($('document-file').files),file=chosen[0];
  clearDocument(false);if(!file)return;
  const generation=documentGeneration,epoch=state.epoch;
  const current=()=>generation===documentGeneration&&epoch===state.epoch;
  state.document={status:'reading'};$('document-status').textContent='Datei wird lokal geprüft …';chat.syncControls();
  try{
    const formats=chosen.map(item=>String(item.name).split('.').pop().toLowerCase());
    if(formats.every(format=>['csv','xlsx'].includes(format))){
      const limit=8*1024*1024;
      if(chosen.length>4||chosen.reduce((n,item)=>n+item.size,0)>limit)
        throw {documentError:'Bitte wähle höchstens vier Tabellen mit zusammen maximal 8 MiB.'};
      const files=[];let total=0;
      for(const item of chosen){
        const bytes=new Uint8Array(await item.arrayBuffer());if(!current())return;
        total+=bytes.length;
        if(!bytes.length||total>limit)throw {documentError:'Eine Tabelle ist leer oder die Dateien überschreiten zusammen 8 MiB.'};
        let binary='';for(let i=0;i<bytes.length;i+=8192)binary+=String.fromCharCode(...bytes.subarray(i,i+8192));
        files.push(Object.freeze({name:item.name,content_b64:btoa(binary)}));
      }
      state.document={status:'ready',kind:'files',request:Object.freeze({operation:'process_files',files:Object.freeze(files)})};
      $('document-status').textContent=chosen.map(item=>item.name).join(' · ');
      return;
    }
    if(chosen.length!==1)throw {documentError:'Tabellen kannst du gemeinsam anhängen. Bitte wähle Textdokumente einzeln.'};
    const format=String(file.name).split('.').pop().toLowerCase(),limit=documentLimits[format];
    if(!limit)throw {documentError:'Bitte wähle CSV/XLSX-Tabellen oder ein RTF-, TXT-, DOCX- oder ODT-Dokument.'};
    if(file.size>limit)throw {documentError:`Das Dokument ist zu groß. ${format.toUpperCase()} darf höchstens ${limit===65536?'64 KiB (65.536 Bytes)':'1 MiB (1.048.576 Bytes)'} enthalten.`};
    const bytes=new Uint8Array(await file.arrayBuffer());
    if(!current())return;
    if(!bytes.length||bytes.length>limit)throw {documentError:'Die Datei ist leer oder überschreitet die erlaubte Größe.'};
    if(format==='rtf'&&!/^\{\\rtf1(?:\\|[ \t\r\n])/.test(String.fromCharCode(...bytes.subarray(0,8))))throw {documentError:'Bitte wähle ein RTF-Dokument. Die Datei hat keinen gültigen RTF-Anfang.'};
    if(format==='txt'){
      let text;try{text=new TextDecoder('utf-8',{fatal:true}).decode(bytes);}catch{throw {documentError:'Bitte wähle eine Textdatei mit UTF-8-Kodierung.'};}
      if(!text.trim()||text.includes('\u0000'))throw {documentError:'Die Textdatei enthält keinen lesbaren Text.'};
    }
    let binary='';for(let i=0;i<bytes.length;i+=8192)binary+=String.fromCharCode(...bytes.subarray(i,i+8192));
    state.document={status:'ready',request:Object.freeze({operation:'extract_text',format,content_b64:btoa(binary)})};
    $('document-status').textContent=`${format.toUpperCase()}-Dokument angehängt · ${new Intl.NumberFormat('de-DE').format(bytes.length)} Bytes. Der Core prüft Format und Inhalt bei der Annahme.`;
  }catch(e){
    if(!current())return;
    state.document={status:'invalid'};$('document-status').textContent='';
    $('document-error').textContent=e?.documentError||'Die Datei konnte nicht gelesen werden. Bitte wähle sie erneut oder entferne sie.';
  }finally{if(current())chat.syncControls();}
}
function setConnected(connected){
  state.connected=connected;
  if(state.view==='ideas')renderIdeas();
  actionIntent.setAvailable(connected&&!!state.session);
  voice.setAvailable(connected&&!!state.session);
  $('connection').textContent=connected?'Mit deinem Core verbunden':state.session?'Verbindung unterbrochen':'Privater Zugang';
  $('connection-dot').className='dot'+(connected?' online':state.session?' offline':'');
  $('offline').hidden=connected||!state.session;
  setTaskControls();chat.syncControls();
  if(state.session)for(const kind of Object.keys(readLists))renderReadList(kind);
  const retry=$('task-retry');if(retry)retry.disabled=!connected;
  syncConversation();
  syncPresence();
}
async function api(path, {method='GET', body, signal}={}){
  const epoch=state.epoch;
  const headers={Accept:'application/json'};
  if(body!==undefined)headers['Content-Type']='application/json';
  if(method!=='GET'&&state.session)headers['X-CSRF-Token']=state.session.csrf_token;
  let res;
  try{res=await fetch(path,{method,headers,credentials:'same-origin',cache:'no-store',body:body===undefined?undefined:JSON.stringify(body),signal:signal||AbortSignal.timeout(12000)});}
  catch{throw {code:'network'};}
  let data;
  try{data=await res.json();}catch{throw {code:'invalid_response'};}
  if(epoch!==state.epoch)throw {code:'obsolete'};
  if(res.status===401&&state.session){showSession(null);$('login-error').textContent=errors.unauthorized;}
  if(!res.ok)throw {code:data.error||'request_failed',status:res.status,reason:typeof data.reason==='string'?data.reason:undefined};
  return data;
}
function showSession(session){
  stopFilePreviews(true);
  voice.end({detach:true});
  windowShare.stop();
  state.session=session;state.epoch++;state.selectedEpoch++;state.voiceReturn=null;
  chat.reset();
  state.followup=null;$('followup-text').value='';$('followup-files').replaceChildren();$('followup-status').textContent='';
  actionComposer.setEnabled(false);
  actionComposer.clear();
  actionIntent.clear();
  $('action-mode').value='natural';$('action-entry').hidden=true;
  $('scope').value='research';$('repository-label').hidden=true;
  state.voiceOutcome=false;
  syncVoiceMount();
  clearDocument();
  $('hermes-frame').removeAttribute('src');$('hermes-frame').hidden=true;
  state.dialogKey=null;
  busyForm($('cost-form'),false);
  $('decision-dialog').close();$('decision-content').replaceChildren();
  $('notice').hidden=true;$('notice').textContent='';$('task-error').replaceChildren();
  $('workroom-menu').open=false;$('task-entry').open=false;
  $('login-panel').hidden=!!session;$('workspace').hidden=!session;$('logout').hidden=!session;
  (session?$('presence-work'):$('presence-home')).append($('presence'));
  if(!session){
    state.runs=state.memories=state.detail=state.candidates=state.data=state.approvals=null;state.selected=null;
    stopFilePreviews(true,$('library-list'));
    $('library-search').value='';
    clearTimeout(librarySearchTimer);library.generation++;library.runs=null;library.next=null;library.loading=false;library.error='';library.query='';library.rendered=null;
    for(const id of ['task-list','approval-list','home-attention-list','home-activity-list','result','memory-list','candidate-list','memory-commands','learning-list','inbox-list','feed-list','library-list','ideas-list','personal-ideas-list','component-list','connection-list','audio-details'])$(id).replaceChildren();
    for(const [kind,list] of Object.entries(readLists)){
      list.generation++;list.rows=null;list.stand=null;list.error='';list.loading=false;list.rendered=undefined;
      $(kind+'-list').replaceChildren();$(kind+'-status').textContent='';
    }
    $('home-attention').hidden=true;
    $('objective').value='';state.submitted=null;state.memoryPending=null;setConnected(false);
    $('memory-search').value='';$('memory-summary').textContent='';$('cost-message').textContent='';$('threshold').value='';
  }
  void showView('overview');
}
async function showView(name){
  if(!Object.hasOwn(viewNames,name))throw {code:'invalid_view'};
  if(state.view!==name){stopFilePreviews();stopFilePreviews(true,$('library-list'));}
  if(state.view==='workroom'&&name!=='workroom')windowShare.stop();
  if(state.view!==name)state.voiceOutcome=false;
  $('workspace-menu').open=false;
  state.view=name;$('page-title').textContent=name==='workroom'?(state.room==='hermes'?'Hermes':'Obsidian'):viewNames[name];
  if(name==='workroom')$('workroom-menu').open=true;
  for(const n of Object.keys(viewNames))$(n+'-view').hidden=n!==name;
  const navigation=name==='detail'?'tasks':name;
  for(const b of document.querySelectorAll('[data-view]')){if(b.dataset.view===navigation)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current');}
  for(const b of document.querySelectorAll('[data-room]')){
    const selected=name==='workroom'&&b.dataset.room===state.room;
    if(selected)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current');
    if(b.hasAttribute('aria-pressed'))b.setAttribute('aria-pressed',String(selected));
  }
  syncWorkroom();
  if(name==='ideas')renderIdeas();
  if(name==='library'){renderLibrary();if(state.session&&library.runs===null)await loadLibrary();}
  syncVoiceMount();
  if(state.session&&state.connected)await loadSecondary(name==='workroom'?'knowledge':name);
}
function syncWorkroom(){
  const active=state.view==='workroom'&&!!state.session;
  $('hermes-panel').hidden=state.room!=='hermes';
  $('obsidian-panel').hidden=state.room!=='obsidian';
  const memoryHome=active?$('workroom-knowledge'):$('memory-home');
  const resultHome=active?$('workroom-controls'):$('conversation-result');
  if($('memory-browser').parentElement!==memoryHome)memoryHome.append($('memory-browser'));
  if($('result').parentElement!==resultHome)resultHome.append($('result'));
  const select=$('workroom-task');
  if(document.activeElement!==select){
    select.replaceChildren(new Option('Bitte einen Auftrag wählen',''),...taskRuns().map(r=>new Option(r.auftrag||'Auftrag',r.id)));
    select.value=state.selected||'';
  }
  const frame=$('hermes-frame');
  const url=active&&state.room==='hermes'&&state.selected?'/dashboard/hermes/solvio-view.html?run='+encodeURIComponent(state.selected):'';
  if(url&&frame.getAttribute('src')!==url)frame.src=url;
  else if(!url)frame.removeAttribute('src');
  frame.hidden=!url;$('hermes-empty').hidden=!!url;
  syncConversation();
}
function syncConversation(){
  const selected=!!state.session&&!!state.selected;
  $('conversation').hidden=!selected;
  syncPresence();
  const run=state.detail?.id===state.selected?state.detail:state.runs?.find(r=>r.id===state.selected);
  $('conversation-objective').textContent=run?.auftrag||'Auftrag wird gelesen …';
  const revision=run?.task_revision;
  $('conversation-followup-message').hidden=!(revision?.revision>1);
  $('conversation-followup-text').textContent=revision?.revision>1?revision.text:'';
  $('open-task-workroom').disabled=!state.connected||!selected;
  syncFollowup();
}
function syncFollowup(){
  const run=state.detail?.id===state.selected?state.detail:null,revision=run?.task_revision;
  const valid=!!run&&run.followup?.eligible===true&&
    Number.isInteger(revision?.revision)&&revision.revision>=1&&revision.revision<100&&/^[a-f0-9]{64}$/.test(revision?.digest);
  let draft=state.followup;
  if(draft&&!draft.submission&&(draft.runId!==state.selected||(run&&draft.digest!==revision?.digest))){
    state.followup=null;draft=null;
  }
  if(valid&&!draft){
    draft=state.followup={runId:run.id,taskId:run.aufgabe,revision:revision.revision,digest:revision.digest,submission:null,sending:false};
    $('followup-text').value='';$('followup-status').textContent='';
    $('followup-files').replaceChildren();
    for(const file of resultFiles(run).items.filter(f=>/\.(csv|xlsx)$/i.test(f.name)&&f.size>0&&f.size<=8*1024*1024)){
      const label=el('label'),input=el('input');input.type='checkbox';input.value=file.id;
      input.dataset.size=String(file.size);label.append(input,el('span',file.name));$('followup-files').append(label);
    }
    $('followup-files-options').hidden=!$('followup-files').children.length;
    $('followup-files-options').open=false;
  }
  const pending=!!draft?.submission&&draft.runId===state.selected;
  $('followup-form').hidden=!state.session||(!valid&&!pending&&!(draft?.rejected&&draft.runId===state.selected));
  const busy=!state.connected||state.data?.runtime!=='available'||!run||!!draft?.sending;
  busyForm($('followup-form'),busy||pending||!valid);
  // Only the identical frozen request can be retried after an uncertain reply,
  // including after a poll reports that its original parent is no longer final.
  $('followup-submit').disabled=busy||(!valid&&!pending);
  $('followup-submit').textContent=pending?'Dieselbe Folgeanweisung erneut senden':'Weiterbearbeiten ↑';
}
async function transmitFollowup(draft){
  if(draft.sending||state.followup!==draft||!state.session||!state.connected)return;
  const epoch=state.epoch,request=draft.submission;
  if(!request)return;
  const current=()=>epoch===state.epoch&&state.followup===draft&&state.selected===draft.runId;
  draft.sending=true;$('followup-status').textContent='Folgeanweisung wird übermittelt …';syncFollowup();
  try{
    const result=await api(`/v1/agent/runs/${encodeURIComponent(draft.runId)}/followup`,{method:'POST',body:request});
    if(!current())return;
    if(!/^ar-[a-f0-9]{16}$/.test(result?.run_id)||result.run_id===draft.runId||result.task_id!==draft.taskId||
      result.parent_run_id!==draft.runId||result.revision!==draft.revision+1||!/^[a-f0-9]{64}$/.test(result.digest)||
      !['ready','preparing'].includes(result.annahme))throw {code:'invalid_response'};
    state.followup=null;
    notify('Folgeanweisung angenommen. SOLVIO arbeitet im selben Auftrag weiter.');
    await selectRun(result.run_id);if(epoch===state.epoch)await refresh();
  }catch(e){
    if(!current())return;
    if(e.status===400||e.status===409){
      draft.submission=null;draft.rejected=true;
      $('followup-status').textContent=e.reason||'Diese Folgeanweisung wurde nicht angenommen. Bitte lies den aktuellen Auftrag erneut.';
      // The old eligibility view is no longer sufficient for a new submission.
      state.detail=null;
    }else $('followup-status').textContent='Übermittlung unbestätigt. Erneut senden übermittelt dieselbe Folgeanweisung mit denselben Dateien.';
  }finally{
    draft.sending=false;
    if(epoch===state.epoch){syncFollowup();setTaskControls();}
  }
}
$('followup-form').addEventListener('submit',async event=>{
  event.preventDefault();const draft=state.followup;
  if(!draft||draft.sending||!state.connected||state.detail?.id!==draft.runId)return;
  if(!draft.submission){
    if(state.detail.followup?.eligible!==true)return;
    const text=$('followup-text').value.trim();
    if(!text||text.length>2000)return;
    const selected=[...$('followup-files').querySelectorAll('input')].filter(input=>input.checked);
    if(selected.length>4||selected.reduce((sum,input)=>sum+Number(input.dataset.size),0)>8*1024*1024){
      $('followup-status').textContent='Bitte wähle höchstens vier Ergebnisdateien mit zusammen maximal 8 MiB.';return;
    }
    draft.submission=Object.freeze({run_id:draft.runId,text,expected_revision:draft.revision,expected_digest:draft.digest,
      input_artifact_ids:Object.freeze(selected.map(input=>input.value)),client_request_id:crypto.randomUUID()});
  }
  await transmitFollowup(draft);
});
$('workroom-task').addEventListener('change',()=>{if($('workroom-task').value)void selectRun($('workroom-task').value,{open:false});});
function statusClass(code){return code==='SUCCEEDED'?'success':['FAILED','CANCELLED','KILLED'].includes(code)?'error':code?.startsWith('WAITING')?'waiting':['PLANNING','RUNNING','VERIFYING','HARVESTING'].includes(code)?'working':'';}
function taskRuns(){
  const tasks=new Map();
  for(const run of state.runs||[]){
    const key=run.aufgabe||run.id,previous=tasks.get(key);
    if(!previous||(run.task_revision?.revision||1)>(previous.task_revision?.revision||1)||
       (run.task_revision?.revision||1)===(previous.task_revision?.revision||1)&&(run.angelegt||0)>(previous.angelegt||0))tasks.set(key,run);
  }
  return [...tasks.values()];
}
function taskCard(r,cls='task-card'){
  const card=button('',()=>selectRun(r.id),cls+(r.id===state.selected?' selected':''));
  card.setAttribute('aria-pressed',String(r.id===state.selected));
  card.append(el('span',r.auftrag||'Auftrag ohne lesbaren Titel','task-title'));
  const meta=el('span','','task-meta');
  meta.append(el('span',r.zustand,'state '+statusClass(r.zustand_code)),el('time',when(r.angelegt)));
  card.append(meta);return card;
}
const assistantIdeas = [
  ['Meinen Tag vorbereiten','Gib mir einen Überblick über meinen heutigen Tag: wichtige Mails, Termine und offene Aufgaben.'],
  ['Etwas vergleichen lassen','Ich möchte etwas vergleichen. Kläre mit mir kurz, was ich suche und welche Anforderungen mir wichtig sind. Recherchiere danach passende Angebote mit Quellen. Kaufe nichts.'],
  ['Eine Mail beantworten','Hilf mir, eine Mail zu beantworten. Kläre zuerst, welche Mail gemeint ist, und bereite die Antwort vor. Vor dem Versand möchte ich Empfänger und Inhalt prüfen.'],
  ['An eine Antwort erinnern','Erinnere mich, wenn auf eine bestimmte Mail keine Antwort kommt. Kläre mit mir, welche Mail und bis wann ich eine Antwort erwarte.'],
  ['Ein Dokument ausarbeiten','Hilf mir, ein Dokument auszuarbeiten. Kläre zuerst mit mir Thema, Ziel und gewünschtes Format.']
];
// A read-only suggestion from the existing task list; no new inference or action.
function personalIdeas(now=Date.now()/1000){
  return taskRuns().flatMap(run=>{
    if(!/^ar-[0-9a-f]{16}$/.test(run.id))return [];
    const kind=run.offen&&run.zustand_code==='WAITING_USER'?'question':
      run.offen&&run.zustand_code==='WAITING_APPROVAL'?'approval':
      !run.offen&&run.angelegt>now-30*86400&&run.angelegt<=now&&run.zustand_code==='FAILED'?'blocked':
      !run.offen&&run.angelegt>now-30*86400&&run.angelegt<=now&&run.zustand_code==='SUCCEEDED'&&
        (String(run.ergebnis||'').trim()||(run.dateien||[]).length)?'result':null;
    const labels={question:['Eine offene Frage klären','Hier wartet SOLVIO auf deine Antwort.','Frage ansehen'],
      approval:['Eine ausstehende Entscheidung prüfen','Sieh dir an, wofür dein OK benötigt wird.','Auftrag ansehen'],
      blocked:['Ein Hindernis klären','Prüfe, was den Abschluss verhindert hat.','Hindernis ansehen'],
      result:['Ein Ergebnis weiterverwenden','Ergebnis öffnen, nutzen oder ergänzen lassen.','Ergebnis ansehen']};
    return kind?[{run,kind,labels:labels[kind],priority:['question','approval','blocked','result'].indexOf(kind)}]:[];
  }).sort((a,b)=>a.priority-b.priority||(b.run.angelegt||0)-(a.run.angelegt||0)||a.run.id.localeCompare(b.run.id)).slice(0,4);
}
function renderIdeas(){
  const list=$('personal-ideas-list');
  if(!state.session){list.replaceChildren();$('ideas-list').replaceChildren();return;}
  // Preserve focus during polling. Clicking always reads the bound task afresh.
  if(!state.connected||state.runs===null||!list.contains(document.activeElement)){
    const ideas=state.connected&&state.runs!==null?personalIdeas():[];
    list.replaceChildren(...ideas.map(({run,labels})=>{
      const card=el('article','','surface');
      card.append(el('h3',labels[0]),el('p',run.auftrag||'Dein Auftrag','idea-origin'),el('p',labels[1]),
        button(labels[2],()=>selectRun(run.id)));return card;
    }));
    if(!ideas.length)list.append(empty(!state.connected||state.runs===null?
      'Deine Anregungen erscheinen, sobald der aktuelle Auftragsstand wieder erreichbar ist.':
      'Gerade gibt es keinen passenden nächsten Schritt aus deinen zuletzt geladenen Aufträgen. Starte unten mit einer neuen Idee.'));
  }
  if($('ideas-list').contains(document.activeElement))return;
  $('ideas-list').replaceChildren(...assistantIdeas.map(([title,prompt])=>{
    const card=el('article','','surface');
    card.append(el('h3',title),button('Im Chat vorbereiten',async()=>{
      if(!chat.prepareIdea(prompt)){notify('Im Chat liegt noch eine Nachricht, ein Anhang oder ein laufendes Gespräch. Dein Entwurf bleibt erhalten.');return;}
      await showView('overview');$('message-text').focus({preventScroll:true});
    }));return card;
  }));
}
function renderLibrary(){
  $('library-more').hidden=!library.next;$('library-more').disabled=library.loading;
  $('library-refresh').disabled=library.loading;
  $('library-status').textContent=library.loading?'Ergebnisse werden gelesen …':library.error;
  if(!library.runs){stopFilePreviews(true,$('library-list'));$('library-list').replaceChildren();library.rendered=null;return;}
  const query=$('library-search').value.trim().toLocaleLowerCase('de');
  const entries=library.runs.filter(r=>{
    const files=resultFiles(r).items;
    if(!files.length&&!String(r.ergebnis||'').trim())return false;
    return !query||query===library.query.toLocaleLowerCase('de')||[r.auftrag,r.ergebnis,...files.map(f=>f.name)].some(v=>String(v||'').toLocaleLowerCase('de').includes(query));
  });
  const signature=JSON.stringify(entries);
  if(library.rendered===signature)return;
  stopFilePreviews(true,$('library-list'));library.rendered=signature;
  $('library-list').replaceChildren(...entries.map(r=>{
    const card=el('article','','surface');card.append(el('h3',r.auftrag),el('p',r.zustand||'Stand ungeklärt','small muted'));
    if(r.ergebnis)card.append(el('p',r.ergebnis,'library-excerpt'));
    card.append(button('Ergebnis ansehen',()=>selectRun(r.id)));
    for(const f of resultFiles(r).items)card.append(fileCard(f));
    return card;
  }));
  if(!entries.length)$('library-list').append(empty(query?'Kein passendes Ergebnis.':'Noch keine Ergebnisse oder Dateien.'));
}
async function loadLibrary(older=false){
  if(!state.session||library.loading||(older&&!library.next))return;
  const epoch=state.epoch,generation=++library.generation,cursor=older?library.next:null,query=$('library-search').value.trim();
  library.loading=true;library.error='';renderLibrary();
  try{
    let next=cursor,found=[];
    const visited=new Set(cursor?[cursor]:[]);
    do {
      const params=[...(next?['before='+encodeURIComponent(next)]:[]),...(query?['q='+encodeURIComponent(query)]:[])];
      const page=await api('/v1/agent/runs'+(params.length?'?'+params.join('&'):''));
      if(epoch!==state.epoch||generation!==library.generation||!state.session)return;
      next=page.next_before??null;
      if(!Array.isArray(page.laeufe)||page.laeufe.some(r=>!/^ar-[a-f0-9]{16}$/.test(r.id))||
         (next!==null&&(!/^ar-[a-f0-9]{16}$/.test(next)||visited.has(next)||(!query&&next!==page.laeufe.at(-1)?.id))))throw {code:'invalid_run_page'};
      if(next)visited.add(next);
      found.push(...page.laeufe);
    } while(query&&next&&found.length<25);
    library.runs=[...new Map([...(older?library.runs||[]:[]),...found].map(r=>[r.id,r])).values()];
    library.next=next;library.query=query;
  }catch(e){
    if(epoch!==state.epoch||generation!==library.generation)return;
    if(e.status===401){showSession(null);return;}
    library.error='Ergebnisse konnten nicht nachgeladen werden. Bereits geladene Ergebnisse bleiben sichtbar. Bitte erneut versuchen.';
  }finally{
    if(epoch===state.epoch&&generation===library.generation){library.loading=false;renderLibrary();}
  }
}
$('library-more').addEventListener('click',()=>void loadLibrary(true));
$('library-refresh').addEventListener('click',()=>void loadLibrary());
$('library-search').addEventListener('input',()=>{
  clearTimeout(librarySearchTimer);library.generation++;library.loading=false;library.next=null;library.error='';
  renderLibrary();librarySearchTimer=setTimeout(()=>void loadLibrary(),350);
});

function renderHome(){
  const attention=$('home-attention-list'),activity=$('home-activity-list');
  if(!attention.contains(document.activeElement)){
    const rows=[];
    if(state.approvals===null)rows.push(empty('Freigabestatus noch nicht bestätigt.'));
    else for(const a of state.approvals){
      const row=button('',()=>showView('tasks'),'home-row');
      row.append(el('span',a.task||'Gesprochener Auftrag','task-title'),el('span','Dein OK wird gebraucht · ansehen','state waiting'));
      rows.push(row);
    }
    for(const r of taskRuns())if(['WAITING_USER','WAITING_APPROVAL'].includes(r.zustand_code))rows.push(taskCard(r,'home-row'));
    const learning=state.data?.learning;
    if(learning?.activities?.some(a=>a.status==='held'))rows.push(button('Ein Lernvorgang braucht deine Entscheidung · Wissen öffnen',()=>showView('knowledge'),'home-row'));
    attention.replaceChildren(...rows);$('home-attention').hidden=!rows.length;
  }
  if(!activity.contains(document.activeElement)){
    const selectedTask=state.detail?.aufgabe||(state.runs||[]).find(r=>r.id===state.selected)?.aufgabe;
    const recent=taskRuns().filter(r=>r.id!==state.selected&&r.aufgabe!==selectedTask&&!['WAITING_USER','WAITING_APPROVAL'].includes(r.zustand_code));
    activity.replaceChildren(...recent.slice(0,3).map(r=>taskCard(r,'home-row')));
    if(state.runs===null)activity.append(empty('Aufgabenstatus noch nicht bestätigt. Ein Auftrag kann im Core weiterlaufen.'));
    else if(!recent.length)activity.append(empty(state.selected?'Weitere Aufträge findest du hier.':'Deine Aufträge und Ergebnisse erscheinen hier.'));
  }
}
// Presentation of existing chat/voice/task state only; never starts audio or work.
function syncPresence(){
  const v=state.voiceUI, c=chat.state;
  const working=r=>r.offen===true&&['PLANNING','RUNNING','VERIFYING','WAITING_SPECIALIST'].includes(r.zustand_code);
  const active=(state.runs||[]).filter(working).length;
  const waiting=(state.runs||[]).filter(r=>['WAITING_USER','WAITING_APPROVAL'].includes(r.zustand_code)).length;
  const parked=(state.runs||[]).filter(r=>r.offen===true&&r.zustand_code==='WAITING_CAPABILITY').length;
  let mode='idle', title='Ich bin bereit.';
  if(state.voiceReturn?.loading){mode='ended';title='Mikrofon aus · Gesprächsverlauf wird gelesen …';}
  else if(v?.closing){mode='ended';title='Mikrofon aus · Gespräch endet …';}
  else if(v?.active){
    if(v.ready&&state.voicePresence==='speaking'){mode='speaking';title=v.muted?'SOLVIO spricht · Mikrofon stumm':'SOLVIO spricht.';}
    else if(v.capturing&&state.voicePresence==='listening'){mode='listening';title='SOLVIO hört zu.';}
    else if(v.ready&&v.muted)title='Mikrofon stumm · Gespräch offen';
    else title=v.localText||'Sprachverbindung wird aufgebaut …';
  }else if(v?.uncertain){mode='ended';title='Mikrofon aus · Ende unbestätigt';}
  else if(!state.connected){mode='offline';title=state.session?'Verbindung unterbrochen':'Privater Zugang';}
  else if(c.detailError)title='Chatstand nicht erreichbar';
  else if(c.sending?.sending)title='Nachricht wird gesendet …';
  else if(c.detail?.conversation?.conversation_id===c.selected){
    if(Number(c.detail.deliveries_open)>0){mode='thinking';title='SOLVIO bearbeitet deine Nachricht …';}
    else if((c.detail.auftraege||[]).some(working)){mode='deepWork';title='SOLVIO arbeitet an deinem Auftrag.';}
  }
  const node=$('presence');
  if(node.dataset.mode!==mode){node.dataset.mode=mode;presence.setState(mode);}
  const engaged=String(!!(v?.active||v?.closing));
  if($('conversation-welcome').dataset.voice!==engaged)$('conversation-welcome').dataset.voice=engaged;
  if($('presence-title').textContent!==title)$('presence-title').textContent=title;
  // Global work remains separate from this chat and from microphone truth.
  const detail=$('presence-detail');
  detail.hidden=!state.session||(!active&&!waiting&&!parked&&state.runs!==null);
  const workText=state.runs===null?'Auftragsstand nicht erreichbar':[
    active?`${active} ${active===1?'Auftrag':'Aufträge'} im Hintergrund`:'',
    waiting?`${waiting} ${waiting===1?'Auftrag wartet':'Aufträge warten'} auf dich`:'',
    parked?`${parked} ${parked===1?'Auftrag wartet':'Aufträge warten'} auf ein Werkzeug`:''
  ].filter(Boolean).join(' · ');
  if(detail.textContent!==workText)detail.textContent=workText;
}
function renderRuns(){
  syncWorkroom();
  $('task-count').textContent=state.runs?taskRuns().length:'—';
  if(!state.runs)return;
  // Do not steal keyboard focus from a selected task while a poll completes.
  if(!$('task-list').contains(document.activeElement)){
    $('task-list').replaceChildren(...taskRuns().map(r=>taskCard(r)));
    if(!state.runs.length)$('task-list').append(empty('Noch keine Aufträge. Im Chat kannst du SOLVIO einen Auftrag geben.'));
  }
  syncPresence();
  renderHome();
}
async function selectRun(id,{open=true}={}){
  if(state.followup?.submission&&id!==state.followup.runId){notify('Die Folgeanweisung ist noch nicht bestätigt. Bitte kläre zuerst die Übermittlung in diesem Auftrag.');return;}
  stopFilePreviews(true);
  actionIntent.suspend();
  state.selected=id;state.detail=null;const selectedEpoch=++state.selectedEpoch;renderRuns();
  $('result').replaceChildren(empty('Auftrag wird gelesen.'));
  if(open)await showView('detail');
  if(state.selectedEpoch!==selectedEpoch||!state.session)return;
  if(open&&state.view==='detail'){
    $('conversation').focus({preventScroll:true});
    $('conversation').scrollIntoView({block:'start'});renderHome();
  }
  try{const data=await api('/v1/agent/runs/'+encodeURIComponent(id));if(state.selectedEpoch!==selectedEpoch||!state.session)return;state.detail=data;renderDetail(data);}
  catch(e){if(state.selectedEpoch===selectedEpoch){actionIntent.unavailable();$('result').replaceChildren(empty(message(e)));}}
}
function safeLink(value){
  const text=String(value??''),plain=()=>el('span',text);
  // Core sources are either a URL or a description followed by one URL.
  // Keep the description as text; never pick a URL from competing sources.
  const candidate=text.trim().split(/\s+/).at(-1),start=text.lastIndexOf(candidate),prefix=text.slice(0,start);
  if(!/^https:\/\//i.test(candidate)||/:\/\/|\b(?:javascript|data|mailto):/i.test(prefix))return plain();
  try{
    const u=new URL(candidate);if(u.protocol!=='https:'||u.username||u.password)return plain();
    const a=el('a',candidate);a.href=u.href;a.target='_blank';a.rel='noopener noreferrer';a.title=u.href;
    const line=el('span');line.append(prefix,a,text.slice(start+candidate.length));return line;
  }catch{return plain();}
}
// File descriptors are data, never arbitrary URLs or HTML. The Core verifies
// their bytes; the UI accepts only the route for this exact run/artifact pair.
const filePreviewTypes={image:['image/png','image/jpeg','image/webp','image/gif'],
  pdf:['application/pdf'],text:['text/plain'],audio:['audio/mpeg','audio/mp4','audio/ogg','audio/wav','audio/x-wav','audio/webm'],
  video:['video/mp4','video/webm','video/ogg']};
function resultFiles(r){
  const items=new Map(),idPattern=/^[A-Za-z0-9_-]{1,160}$/;
  const path=id=>typeof r.id==='string'&&idPattern.test(r.id)&&typeof id==='string'&&idPattern.test(id)
    ?`/v1/agent/runs/${encodeURIComponent(r.id)}/artifacts/${encodeURIComponent(id)}`:null;
  const documents=new Set((Array.isArray(r.artefakte)?r.artefakte:[]).filter(a=>a?.art==='document_result').map(a=>a.id));
  let rejected=false;
  for(const f of Array.isArray(r.dateien)?r.dateien:[]){
    const base=path(f?.id);
    if(!base||f.download_url!==base+'/download'||typeof f.name!=='string'||!f.name.trim()||f.name.length>512||
      typeof f.mime_type!=='string'||f.mime_type.length>128||!Number.isSafeInteger(f.size)||f.size<0||
      typeof f.sha256!=='string'||!/^[a-f0-9]{64}$/.test(f.sha256)){rejected=true;continue;}
    const kind=filePreviewTypes[f.preview_kind]?.includes(f.mime_type)&&f.preview_url===base+'/preview'?f.preview_kind:'none';
    if(!items.has(f.id))items.set(f.id,{id:f.id,name:f.name,mime:f.mime_type,size:f.size,sha256:f.sha256,
      download:base+'/download',preview:kind==='none'?null:base+'/preview',kind,document:documents.has(f.id)});
  }
  // Older Core responses only carry document_result. Keep that proven legacy
  // download, without inventing a preview or counting it a second time.
  for(const a of !Array.isArray(r.dateien)&&Array.isArray(r.artefakte)?r.artefakte:[]){
    const base=path(a?.id);
    if(a?.art==='document_result'&&base&&a.download_url===base+'/download'&&!items.has(a.id))
      items.set(a.id,{id:a.id,name:'solvio-dokument.txt',mime:'text/plain',size:null,sha256:null,
        download:base+'/download',preview:null,kind:'none',document:true});
  }
  return {items:[...items.values()],rejected};
}
function stopFilePreviews(dispose=false,root=$('result')){
  for(const card of root.querySelectorAll('.result-file-card'))card.stopPreview?.(dispose);
}
function fileCard(f){
  const card=el('article','','result-file-card');card.dataset.fileId=f.id;card.fileSignature=JSON.stringify(f);
  const heading=el('div','','file-heading'),kindNames={image:'Bild',pdf:'PDF',audio:'Audio',video:'Video',text:'Text',none:'Datei'};
  heading.append(el('span',kindNames[f.kind],'file-kind'),el('h4',f.name,'file-name'));
  const format=f.name.match(/\.([A-Za-z0-9]{1,8})$/)?.[1].toUpperCase()||kindNames[f.kind];
  const size=f.size===null?'':f.size>=1048576?`${new Intl.NumberFormat('de-DE',{maximumFractionDigits:1}).format(f.size/1048576)} MB`:
    f.size>=1024?`${new Intl.NumberFormat('de-DE',{maximumFractionDigits:1}).format(f.size/1024)} KB`:`${f.size} Bytes`;
  card.append(heading,el('p',[format,size].filter(Boolean).join(' · '),'small muted file-meta'));
  const actions=el('div','','actions file-actions artifact-entry'),download=el('a',f.document?'Dokumenttext herunterladen':'Herunterladen','file-download');
  download.href=f.download;download.download=f.name;actions.append(download);card.append(actions);
  const status=el('p','','small muted file-status');status.setAttribute('role','status');card.append(status);
  const preview=el('div','','file-preview');card.append(preview);
  let pending=null,previewButton=null;
  card.stopPreview=(dispose=false)=>{
    pending?.abort();pending=null;
    for(const media of preview.querySelectorAll('audio,video')){media.pause();media.removeAttribute('src');media.load();}
    if(f.kind!=='image'||dispose){preview.replaceChildren();if(previewButton){previewButton.textContent='Vorschau anzeigen';previewButton.setAttribute('aria-expanded','false');}status.textContent='';}
  };
  if(f.preview){
    const open=el('a',f.kind==='pdf'?'PDF öffnen':'Öffnen','file-open');open.href=f.preview;
    open.target='_blank';open.rel='noopener noreferrer';actions.append(open);
    if(f.kind==='image'){
      const image=el('img');image.alt=f.name;image.loading='lazy';image.decoding='async';image.src=f.preview;
      image.addEventListener('error',()=>{image.hidden=true;status.textContent='Die Bildvorschau ist nicht verfügbar. Bitte lies den Auftragsstand erneut.';});preview.append(image);
    }else if(f.kind!=='pdf'){
      previewButton=button('Vorschau anzeigen',async()=>{
        if(previewButton.getAttribute('aria-expanded')==='true'){card.stopPreview();return;}
        previewButton.setAttribute('aria-expanded','true');previewButton.textContent='Vorschau schließen';status.textContent='';
        if(f.kind==='audio'||f.kind==='video'){
          const media=el(f.kind);media.controls=true;media.preload='none';media.src=f.preview;
          if(f.kind==='video')media.playsInline=true;
          media.addEventListener('error',()=>{status.textContent='Diese Vorschau konnte nicht wiedergegeben werden. Du kannst die Datei herunterladen.';});preview.append(media);return;
        }
        // Text stays inert and bounded even when a file response is malformed.
        const controller=new AbortController(),epoch=state.epoch,selection=state.selectedEpoch;
        pending=controller;const current=()=>pending===controller&&state.epoch===epoch&&state.selectedEpoch===selection&&card.isConnected;
        const timeout=setTimeout(()=>controller.abort(),10000),limit=65536;
        status.textContent='Textvorschau wird gelesen …';
        try{
          const response=await fetch(f.preview,{credentials:'same-origin',cache:'no-store',redirect:'error',signal:controller.signal});
          if(!current())return;
          if(response.status===401){showSession(null);$('login-error').textContent=errors.unauthorized;return;}
          if(!response.ok||response.headers.get('Content-Type')?.split(';')[0].trim()!=='text/plain'||!response.body)throw Error('preview_unavailable');
          const reader=response.body.getReader(),chunks=[];let length=0,truncated=false;
          try{while(true){const {done,value}=await reader.read();if(done)break;
            const part=value.subarray(0,limit-length);chunks.push(part);length+=part.length;
            if(length===limit){truncated=f.size>limit||value.length>part.length;break;}
          }}finally{await reader.cancel();}
          if(!current())return;
          const bytes=new Uint8Array(length);let offset=0;for(const part of chunks){bytes.set(part,offset);offset+=part.length;}
          preview.append(el('pre',new TextDecoder().decode(bytes),'file-text'));
          status.textContent=truncated?'Vorschau auf 64 KiB begrenzt. Die vollständige Datei steht zum Herunterladen bereit.':'';
        }catch{if(current())status.textContent='Die Textvorschau ist nicht verfügbar. Du kannst die Datei herunterladen.';}
        finally{clearTimeout(timeout);if(pending===controller)pending=null;}
      });
      previewButton.setAttribute('aria-expanded','false');actions.append(previewButton);
    }
  }else card.append(el('p','Für dieses Format steht keine Vorschau bereit.','small muted file-format-note'));
  const shareData={title:f.name,url:new URL(f.download,location.origin).href};
  if(typeof navigator.share==='function'&&typeof navigator.canShare==='function'&&navigator.canShare(shareData)){
    const share=button('Link teilen',async()=>{try{await navigator.share(shareData);}catch(e){if(e?.name!=='AbortError')status.textContent='Der Link konnte hier nicht geteilt werden. Du kannst die Datei herunterladen.';}});
    share.title='Privater Dateilink – die Anmeldung an deinem Core bleibt erforderlich.';actions.append(share);
    card.append(el('p','Geteilte Links erfordern die Anmeldung an deinem Core.','small muted file-share-note'));
  }
  return card;
}
// Renders one run into `container`. Embedded (chat card): no bound answer
// form, no run selection; the caller decides how a re-read happens.
function renderDetail(r,container=$('result'),{embedded=false,reread=()=>selectRun(r.id),question:askEmbedded=null}={}){
  const question=embedded?(askEmbedded?askEmbedded(r):Object.assign(el('div'),{hidden:true})):actionIntent.update(r);
  if(!embedded)syncConversation();
  if(container.contains(document.activeElement))return;
  const openSections=new Set(container.dataset.runId===r.id?[...container.querySelectorAll('details[data-section][open]')].map(n=>n.dataset.section):[]);
  const disclosure=(title,key)=>{
    const n=el('details','','result-section');n.dataset.section=key;n.open=openSections.has(key);
    const summary=el('summary');summary.append(el('h3',title));n.append(summary);return n;
  };
  const nodes=[el('span',r.zustand,'state '+statusClass(r.zustand_code)),el('h3',r.auftrag,'result-title')];
  const supporting=disclosure('Details zum Auftrag','details');
  if(Array.isArray(r.task_history)&&r.task_history.length>1){
    const history=disclosure('Auftrag und Folgeanweisungen','revisions');
    for(const entry of r.task_history){
      if(!/^ar-[a-f0-9]{16}$/.test(entry.run_id)||!Number.isInteger(entry.revision))continue;
      const item=el('article');item.append(el('h4',entry.revision===1?'Ursprünglicher Auftrag':`Folgeanweisung ${entry.revision-1}`),
        el('p',entry.text,'result-copy'),el('p',runStateNames[entry.state]||'Stand unbekannt','small muted'));
      if(entry.result_summary)item.append(el('p',entry.result_summary,'result-copy'));
      if(entry.run_id!==r.id)item.append(button('Ergebnis und Dateien ansehen',()=>selectRun(entry.run_id),'quiet'));
      history.append(item);
    }
    supporting.append(history);
  }
  if(r.zustand_code==='FAILED'){
    const title=r.grund_code==='goal_unverified'?(r.zustand||'Abschluss nicht bestätigt'):'Auftrag fehlgeschlagen';
    const failed=el('div','','result-failure');failed.append(el('strong',title),el('p',r.grund||'Die vollständige Erfüllung dieses Auftrags ist nicht bestätigt.'));
    nodes.push(failed);
  }
  const files=resultFiles(r),previousCards=new Map([...container.querySelectorAll('.result-file-card')].map(n=>[n.dataset.fileId,n]));
  if(r.datei_hinweis||files.rejected)nodes.push(el('p',r.datei_hinweis||'Ein Dateieintrag ist nicht gültig gebunden und wird nicht angeboten.','boundary file-warning'));
  if(files.items.length){
    const section=el('section','','result-files');section.append(el('h3',files.items.length===1?'Deine Datei':'Deine Dateien'));
    const grid=el('div','','result-file-grid');
    for(const file of files.items){
      const old=previousCards.get(file.id),reuse=old?.fileSignature===JSON.stringify(file);
      if(old&&!reuse)old.stopPreview?.(true);
      grid.append(reuse?old:fileCard(file));previousCards.delete(file.id);
    }
    section.append(grid);nodes.push(section);
  }
  for(const old of previousCards.values())old.stopPreview?.(true);
  if(r.ergebnis){if(question.hidden||r.ergebnis!==r.action_intent?.question?.prompt)nodes.push(el('p',r.ergebnis,'result-copy'));}
  else if(question.hidden||r.grund!==r.action_intent?.question?.prompt)nodes.push(el('p',r.grund||'Noch kein bestätigtes Ergebnis.','muted small'));
  if(!question.hidden)nodes.push(question);
  if(r.wartet_auf||r.anbietergrenze){const boundary=el('div','','boundary');const b=r.wartet_auf;boundary.append(el('strong',b?.handlung||'Eine Anbietergrenze braucht deine Entscheidung.'));if(b?.grund)boundary.append(el('p',b.grund));if(b?.danach)boundary.append(el('p',b.danach));if(r.anbietergrenze){boundary.append(el('p',errors[r.anbietergrenze.grund]||r.anbietergrenze.grund));boundary.append(el('p','Warten oder Plan ändern: danach ausdrücklich fortsetzen. Ein Anbieterwechsel erfolgt nur nach deiner Entscheidung.'));}nodes.push(boundary);}
  const actions=el('div','','actions');
  if(r.kontobindung?.selection_required)actions.append(button('Neu verbundenes Konto auswählen',()=>chooseActionAccount(r,reread),'primary'));
  else if(r.zustand_code==='WAITING_USER' && r.action_intent?.status!=='waiting_user' && (!r.anbietergrenze || r.anbietergrenze.fortsetzbar))actions.append(button('Fortsetzen',()=>confirmAction('Auftrag fortsetzen?',r.wartet_auf?.danach||'Der Core prüft Befugnisse, Anmeldung und Kosten erneut.',async current=>{await api(`/v1/agent/runs/${encodeURIComponent(r.id)}/resume`,{method:'POST'});if(!current())return;notify('Fortsetzung angefragt. Der aktuelle Stand wird neu gelesen.');await reread();await refresh();}),'primary'));
  if(r.zustand_code==='WAITING_USER' && /^[a-f0-9]{64}$/.test(r.anbietergrenze?.boundary_ref||'')){
    for(const choice of r.anbietergrenze?.wechseloptionen||[]){
      if(!['codex','claude-code'].includes(choice.provider))continue;
      actions.append(button(`Mit ${choice.label} fortsetzen`,()=>confirmAction(`Zu ${choice.label} wechseln?`,`${choice.hinweis}\nVerfügbare Werkzeuge: ${(choice.werkzeuge||[]).join(', ')}`,async current=>{
        await api(`/v1/agent/runs/${encodeURIComponent(r.id)}/resume`,{method:'POST',body:{provider:choice.provider,boundary_ref:r.anbietergrenze.boundary_ref}});
        if(!current())return;notify('Anbieterwahl bestätigt. Derselbe Auftrag wird fortgesetzt.');await reread();await refresh();
      })));
    }
  }
  if(r.offen)actions.append(button('Auftrag abbrechen',()=>confirmAction('Diesen Auftrag abbrechen?',r.auftrag,async current=>{await api(`/v1/agent/runs/${encodeURIComponent(r.id)}/cancel`,{method:'POST'});if(!current())return;notify('Abbruch bestätigt. Bereits erfolgte Wirkungen werden dadurch nicht rückgängig.');await reread();await refresh();}),'danger'));
  if(actions.children.length)nodes.push(actions);
  const technical=disclosure('Technische Details','technical');
  if(r.pruefung){technical.append(el('h4','Prüfung'));for(const [key,value] of Object.entries(r.pruefung))technical.append(el('p',`${key}: ${typeof value==='object'?JSON.stringify(value):value}`,'small muted'));}
  if(r.artefakte?.length){
    for(const a of r.artefakte){
      const row=el('p',undefined,'small muted artifact-entry');
      if(a.art!=='document_result'||!files.items.some(f=>f.id===a.id)){row.textContent=`${a.art}: ${a.pfad}`;technical.append(row);}
    }
  }
  if(r.vorbereitet)nodes.push(el('p','Änderung vorbereitet. Eine produktive Übernahme ist damit nicht bestätigt.','small'));
  if(r.kosten?.counts?.unknown)nodes.push(el('p','Eine Kostenbuchung ist ungewiss. Ihre Reserve bleibt bestehen.','boundary'));
  else if(!r.kosten?.configured)nodes.push(el('p','Noch kein bestätigter Kostenstand.','small muted'));
  if(r.befunde?.length){const s=disclosure('Befunde','findings');for(const b of r.befunde)s.append(el('p',b,'result-copy'));supporting.append(s);}
  if(r.quellen?.length){const s=disclosure('Quellen','sources'),ul=el('ul');for(const url of r.quellen){const li=el('li');li.append(safeLink(url));ul.append(li);}s.append(ul);supporting.append(s);}
  const cost=disclosure('Zusatzkosten dieses Auftrags','costs');
  if(r.kosten?.ai_tool?.credit_usage_unmeasured)cost.append(el('p','ChatGPT-Credits waren freigegeben. Ihr Verbrauch und Eurogegenwert wurden nicht gemessen.','small'));
  if(r.kosten?.configured){const c=r.kosten.ai_tool,dl=el('dl','','cost-grid');for(const [label,value] of [['Gebucht',c?.spent_cents],['Reserviert',c?.reserved_cents],['Fragegrenze',r.kosten.ask_threshold_cents],['Kaufbudget',r.kosten.purchase_cap_cents]]){const group=el('div');group.append(el('dt',label),el('dd',money(value)));dl.append(group);}cost.append(dl);if(r.kosten.counts?.unknown)cost.append(el('p','Eine Buchung ist ungewiss. Ihre Reserve bleibt bestehen.','small'));if(r.offen)cost.append(button('Kostenrahmen freigeben',()=>costApproval(r,reread)));}
  else cost.append(el('p','Noch kein bestätigter Kostenstand.','small muted'));
  cost.append(el('p',`Anbieter: ${r.anbieter||'nicht belegt'} · Abrechnung: ${r.abrechnung==='subscription'?'Monatsabo':r.abrechnung==='unknown'||!r.abrechnung?'unbekannt':r.abrechnung}`,'small muted'));supporting.append(cost);
  if(r.verlauf?.length){const s=disclosure('Verlauf','history'),ul=el('ol','','timeline');for(const event of r.verlauf){const li=el('li');li.append(el('time',when(event.zeit)),el('span',timelineText(event.text||event.art)));ul.append(li);}s.append(ul);supporting.append(s);}
  if(technical.children.length>1)supporting.append(technical);
  nodes.push(supporting);
  container.dataset.runId=r.id;
  container.replaceChildren(...nodes);
}
function confirmAction(title, text, action){
  const epoch=state.epoch,key=Symbol();state.dialogKey=key;
  const current=()=>state.epoch===epoch&&state.dialogKey===key&&$('decision-dialog').open;
  const content=$('decision-content');content.replaceChildren(el('h2',title),el('p',text));
  const controls=el('div','','actions');controls.append(button('Abbrechen',()=>$('decision-dialog').close()),button('Bestätigen',async b=>{if(!current())return;b.disabled=true;try{await action(current);if(current())$('decision-dialog').close();}catch(e){if(current())content.append(el('p',message(e),'error'));}finally{if(current())b.disabled=false;}},'primary'));content.append(controls);openDecision();
}
function chooseActionAccount(run,reread=()=>selectRun(run.id)){
  const binding=run.kontobindung,epoch=state.epoch,key=Symbol();
  state.dialogKey=key;
  const current=()=>state.epoch===epoch&&state.dialogKey===key&&$('decision-dialog').open;
  const content=$('decision-content'),label=el('label','Konto für diesen Auftrag'),select=el('select');
  select.setAttribute('aria-label','Konto für diesen Auftrag');
  const placeholder=el('option','Bitte wählen');placeholder.value='';select.append(placeholder);
  for(const row of binding.choices){const option=el('option',`${row.label||'Verbundenes Konto'}${row.resource==='me'?'':' · '+row.resource}`);option.value=row.account;select.append(option);}
  label.append(select);
  content.replaceChildren(el('h2','Mit neu verbundenem Konto fortsetzen'),
    el('p','Der Zugang wurde neu verbunden. Wähle das Konto für den unveränderten offenen Schritt dieses Auftrags.'),
    el('p',run.auftrag),label);
  let submission=null;
  const feedback=el('p','','error'),controls=el('div','','actions');
  controls.append(button('Zurück',()=>$('decision-dialog').close()),button('Mit diesem Konto fortsetzen',async b=>{
    if(!current())return;
    if(!submission){
      if(!binding.choices.some(row=>row.account===select.value)){feedback.textContent='Bitte wähle das passende verbundene Konto.';return;}
      submission={action_id:binding.action_id,new_account:select.value,expected_account:binding.current_account,
        expected_receipt_digest:binding.receipt_digest,client_request_id:crypto.randomUUID()};
    }
    b.disabled=true;select.disabled=true;feedback.textContent='';
    try{
      await api(`/v1/agent/runs/${encodeURIComponent(run.id)}/action-account`,{method:'POST',body:submission});
      if(!current())return;
      $('decision-dialog').close();notify('Kontozuordnung bestätigt. Der aktuelle Auftragsstand wird gelesen.');
      await reread();await refresh();
    }catch(e){if(current())feedback.textContent=message(e)+' Erneut versuchen übermittelt dieselbe Auswahl.';}
    finally{if(current())b.disabled=false;}
  },'primary'));
  content.append(feedback,controls);openDecision();
}
function costApproval(r,reread=()=>selectRun(r.id)){
  const epoch=state.epoch,key=Symbol();state.dialogKey=key;
  const current=()=>state.epoch===epoch&&state.dialogKey===key&&$('decision-dialog').open;
  const content=$('decision-content');content.replaceChildren(el('h2','Zusatzkosten freigeben'),el('p',r.auftrag),el('p','Dieser Betrag ist der neue gesamte KI- und Werkzeugkostenrahmen dieses Auftrags. Bereits gebuchte und reservierte Beträge zählen mit.'));
  const label=el('label','Gesamtrahmen in Euro');label.htmlFor='task-cost-limit';const input=el('input');input.id='task-cost-limit';input.type='number';input.min='0';input.step='.01';input.required=true;
  const form=el('form');form.append(label,input,el('button','Rahmen freigeben','primary'));form.lastChild.type='submit';const requestId=crypto.randomUUID();
  form.addEventListener('submit',async e=>{e.preventDefault();if(!current())return;busyForm(form,true);try{const cents=Math.round(Number(input.value)*100);await api(`/v1/agent/tasks/${encodeURIComponent(r.aufgabe)}/cost-approval`,{method:'POST',body:{max_total_cents:cents,client_request_id:requestId}});if(!current())return;$('decision-dialog').close();notify('Kostenrahmen bestätigt. Ein wartender Auftrag wird über „Fortsetzen“ weitergeführt.');await reread();}catch(e){if(current())content.append(el('p',message(e),'error'));}finally{if(current())busyForm(form,false);}});content.append(form);openDecision();
}
function renderApprovals(data){
  if($('approval-list').contains(document.activeElement))return;
  $('approval-list').replaceChildren(...data.approvals.map(a=>{
    const n=el('div','','approval-card');n.append(el('span','GESPROCHENER AUFTRAG','eyebrow'),el('p',a.task||'Auftrag ohne lesbaren Wortlaut'),el('p',`Gültig bis ${when(a.expires_at)}`,'small muted'));
    const actions=el('div','','actions');for(const [decision,label] of [['APPROVE','OK, ausführen'],['DENY','Ablehnen']])actions.append(button(label,()=>confirmAction(decision==='APPROVE'?'Diesen Pi-Auftrag freigeben?':'Diesen Pi-Auftrag ablehnen?',a.task,async current=>{await api(`/v1/agent/approvals/${encodeURIComponent(a.approval_id)}/decision`,{method:'POST',body:{action_digest:a.action_digest,decision}});if(!current())return;notify(decision==='APPROVE'?'Freigabe bestätigt. Der Core übernimmt den Auftrag.':'Ablehnung bestätigt.');await refresh();}),decision==='APPROVE'?'primary':''));n.append(actions);return n;
  }));if(!data.approvals.length)$('approval-list').append(empty('Gerade wartet kein Pi-Auftrag auf dein OK.'));
}
function renderState(data){
  state.data=data;$('environment').textContent=data.environment==='private'?'DEIN ARBEITSRAUM':'ISOLIERTE TESTUMGEBUNG';
  $('footer-state').textContent=data.environment==='private'?'SOLVIO Nexus · Privater Zugang':'SOLVIO Nexus · Temporäre Testdaten';
  $('cost-control-status').textContent=data.cost_controls==='configured'?'Ein Kostenprüfweg ist eingerichtet. Jeder Aufruf wird einzeln geprüft.':'Noch kein belastbarer Kostenprüfweg eingerichtet. Ausführung mit unbekannten Zusatzkosten bleibt angehalten.';
  const learning=data.learning;
  $('learning-status').textContent=learning?.state==='enabled'?'Persönliches Lernen ist eingerichtet. Verarbeitet bedeutet nicht automatisch eine neue Erinnerung.':learning?.state==='disabled'?'Persönliches Lernen ist ausgeschaltet.':'Persönliches Lernen ist derzeit nicht angeschlossen.';
  if(!$('learning-list').contains(document.activeElement)){
    const labels={held:'Angehalten',pending:'Noch kein Abschluss',expired:'Frist abgelaufen',revoked:'Berechtigung zurückgezogen'};
    const reasons={observation_interrupted:'Die Verarbeitung wurde durch einen Neustart unterbrochen. Es gab keinen automatischen Wiederholungsversuch.',quota:'Das Abo-Kontingent ist erschöpft. Du entscheidest über den nächsten Schritt.',logged_out:'Bitte die Anbieter-Anmeldung erneuern und danach ausdrücklich fortsetzen.',cost_approval_required:'Die Zusatzkosten brauchen deine ausdrückliche Entscheidung.',extractor_interrupted:'Verarbeitung unterbrochen. Ein möglicher Schreibvorgang wird vor der Fortsetzung geprüft.',memory_disabled:'Lernen ist ausgeschaltet.',cost_unbounded:'Ein verlässlicher Kostenbeleg fehlt.',quota_exhausted:'Das Abo-Kontingent ist erschöpft.',login_required:'Die Anbieter-Anmeldung muss erneuert werden.'};
    $('learning-list').replaceChildren(...(learning?.activities||[]).map(a=>{
      const n=el('article','','surface');n.append(el('strong',`${labels[a.status]||'Stand ungeklärt'} · ${a.source_kind==='task'?'aus einem Auftrag':'aus einem App-Gespräch'}`),el('p',reasons[a.held_reason]||errors[a.held_reason]||'Die Verarbeitung hat noch keinen bestätigten Abschluss.'),el('p',`Angenommen ${when(a.accepted_at)} · Frist ${when(a.expires_at)}`,'small muted'));
      if(a.status==='held'&&a.source_kind==='task'&&learning.state==='enabled')n.append(button('Lernen fortsetzen',()=>confirmAction('Diese Beobachtung fortsetzen?','Der Core prüft dieselbe Auftragsbindung und die Kosten erneut. Gespeicherte Vorschläge werden weiterverarbeitet; ein ungewisser Schreibausgang bleibt angehalten.',async current=>{await api(`/v1/dashboard/learning/${encodeURIComponent(a.activity_id)}/resume`,{method:'POST'});if(!current())return;notify('Fortsetzung eingereiht. Der aktuelle Stand wird neu gelesen.');await refresh();})));
      else if(a.status==='held'&&a.source_kind!=='task')n.append(el('p','Für diese Sprachbeobachtung fehlt hier ein erneuerbarer Gerätenachweis. Sie wird nicht automatisch neu gestartet.','small muted'));
      return n;
    }));
    if(learning?.state==='enabled'&&!learning.activities?.length)$('learning-list').append(empty('Keine offenen Lernvorgänge im gelesenen Stand.'));
  }
  if(!$('repository').contains(document.activeElement)){
    const selected=$('repository').value, known=data.repositories.some(r=>r.path===selected);
    const placeholder=el('option',selected&&!known?'Gewähltes Projekt nicht mehr verfügbar':'Bitte Projekt wählen');placeholder.value='';placeholder.disabled=true;
    const options=[placeholder,...data.repositories.map(r=>{const o=el('option',r.name);o.value=r.path;return o;})];
    $('repository').replaceChildren(...options);
    $('repository').value=known?selected:'';
  }
  $('component-list').replaceChildren(...(data.components||[]).map(c=>{const n=el('article','','surface');n.append(el('h3',c.name),el('p',c.grund||'Kein Messbeleg'),el('p',`Zustand: ${c.zustand} · geprüft ${when(c.geprueft_um)}`,'small muted'));return n;}));
  if(!data.components?.length)$('component-list').append(empty('Noch keine Betriebsprüfung verfügbar.'));
  $('connection-list').replaceChildren(...[['gmail','Gmail'],['calendar','Google Kalender']].map(([id,title])=>{
    const c=(data.components||[]).find(item=>item.komponente===id), n=el('article','','surface');
    const status=!c||!c.geprueft_um||c.zustand==='unknown'?'Noch nicht geprüft':c.zustand==='healthy'?'Zuletzt verfügbar':c.zustand==='auth_required'?'Anmeldung erforderlich':'Verbindung prüfen';
    n.append(el('h3',title),el('p',status),el('p',c?.geprueft_um?`Letzte Prüfung: ${when(c.geprueft_um)}`:'Kein Messbeleg','small muted'));
    if(c?.grund)n.append(el('p',c.grund));
    return n;
  }));
}
async function refresh(){
  if(state.refreshing||!state.session)return;state.refreshing=true;const epoch=state.epoch;
  try{
    const data=await api('/v1/dashboard/state');if(epoch!==state.epoch)return;renderState(data);setConnected(true);
    const responses=await Promise.allSettled([api('/v1/agent/runs?view=tasks'),api('/v1/agent/approvals'),api('/v1/control/audio')]);
    if(epoch!==state.epoch)return;
    if(responses.some(r=>r.status==='rejected'&&r.reason.status===401))throw {code:'unauthorized',status:401};
    const [runs,approvals,audio]=responses;
    if(runs.status==='fulfilled'){state.runs=runs.value.laeufe;renderRuns();}else{$('task-list').replaceChildren(empty(message(runs.reason)));$('task-count').textContent='—';state.runs=null;syncPresence();}
    if(approvals.status==='fulfilled'){state.approvals=approvals.value.approvals;renderApprovals(approvals.value);}else{state.approvals=null;$('approval-list').replaceChildren(empty('Freigabestatus nicht erreichbar.'));}
    renderHome();
    if(state.view==='library')renderLibrary();
    if(state.view==='ideas')renderIdeas();
    if(state.view==='feed')await loadSecondary('feed');
    renderAudio(audio.status==='fulfilled'?audio.value:null);
    $('freshness').textContent='Core gelesen · '+when(data.stand);
    await chat.refresh();if(epoch!==state.epoch)return;
    if(state.selected){
      const selection=state.selectedEpoch;
      try{const r=await api('/v1/agent/runs/'+encodeURIComponent(state.selected));if(epoch===state.epoch&&selection===state.selectedEpoch){state.detail=r;renderDetail(r);}}
      catch(e){if(epoch===state.epoch&&selection===state.selectedEpoch){stopFilePreviews(true);state.detail=null;actionIntent.unavailable();if(!$('result').querySelector('.action-question:not([hidden])'))$('result').replaceChildren(empty('Der aktuelle Stand dieses Auftrags konnte nicht gelesen werden. '+message(e)));}}
    }
  }catch(e){if(epoch!==state.epoch)return;if(e.status===401){showSession(null);$('login-error').textContent=message(e);}else setConnected(false);}
  finally{state.refreshing=false;}
}
function renderAudio(data){
  $('audio-summary').textContent=data?'Gerätestatus separat ansehen':'Kein bestätigter Audiostatus';
  $('audio-details').replaceChildren();
  if(!data){$('audio-details').append(empty('Der Audiostatus ist nicht verfügbar. Das bedeutet nicht, dass ein Mikrofon aus ist.'));return;}
  $('audio-details').append(el('p',data.scope,'small muted'));
  if(data.history_uncertain)$('audio-details').append(el('p','Der ältere Verbindungsverlauf ist unvollständig. Ein vollständiges Audioende ist damit nicht belegt.','error'));
  const names={not_started:'noch nicht begonnen',not_opened:'nicht geöffnet',opening:'wird geöffnet',connecting:'Verbindungsaufbau',active:'aktiv',ready:'bereit',closing:'wird beendet',reconnecting:'Wiederverbindung läuft',ended:'beendet',closed_confirmed:'Abschluss bestätigt',unknown:'unbekannt'};
  for(const device of data.devices){const row=el('div','','audio-row');row.append(el('strong',device.device_id),el('p',device.description),el('p',`Gespräch: ${names[device.conversation]||'unbekannt'} · Anbieter: ${names[device.provider]||'unbekannt'}`),el('p',`Quelle: ${device.source} · ${when(device.observed_at)}`));
    if(device.measurement_age_s!==null)row.append(el('p',`Mikrofonmessung ${Math.round(device.measurement_age_s)} Sekunden alt.`,'small muted'));
    row.append(el('p',`Letztes Audiopaket im Core: ${when(device.last_received_at)}. Letzte bestätigte Weitergabe: ${when(device.last_forwarded_at)}.`,'small muted'));
    $('audio-details').append(row);}
  if(!data.devices.length)$('audio-details').append(empty('Noch kein Gerätebeleg vorhanden. Mikrofonstatus unbekannt.'));
}
const readId = value => typeof value==='string'&&value.length>0&&value.length<=128;
function renderReadList(kind){
  const data=readLists[kind],list=$(kind+'-list'),status=$(kind+'-status');
  $(kind+'-refresh').disabled=!state.connected||data.loading;
  status.textContent=!state.connected?'Verbindung unterbrochen. Die letzte Ansicht ist veraltet.':
    data.loading?'Aktueller Stand wird gelesen …':data.error?'Der aktuelle Stand konnte nicht gelesen werden. '+data.error:
    data.stand?'Core gelesen · '+when(data.stand):'Noch kein bestätigter Stand.';
  if(data.rendered===data.rows)return;
  data.rendered=data.rows;
  const rows=data.rows;
  if(rows===null){list.replaceChildren(empty('Noch kein bestätigter Stand.'));return;}
  list.replaceChildren(...rows.map(row=>{
    const card=el('article','','surface');
    if(kind==='activity'){
      card.append(el('time',when(row.zeit),'small muted'),el('h3',row.titel||'Ereignis'));
      if(row.detail)card.append(el('p',row.detail));
      if(readId(row.meldung))card.append(button('Hinweis ansehen',()=>showReadDetail('inbox',row.meldung)));
      if(readId(row.aufgabe))card.append(button('Plan ansehen',()=>showReadDetail('recurring',row.aufgabe)));
    }else{
      card.append(el('h3',row.titel||'Geplante Aufgabe'),el('p',row.was||'Aufgabe'),el('p',row.wann||'Zeitpunkt nicht belegt'));
      card.append(el('p',(row.aktiv===true?'Aktiv':row.aktiv===false?'Pausiert':'Aktivität ungeklärt')+' · '+(row.zustand||'Stand ungeklärt')));
      card.append(el('p','Nächster Lauf: '+when(row.naechster_lauf)+' · Letzter Lauf: '+when(row.letzter_lauf),'small muted'));
      if(row.letzter_fehler)card.append(el('p',row.letzter_fehler,'boundary'));
      if(readId(row.id))card.append(button('Einzelheiten ansehen',()=>showReadDetail('recurring',row.id)));
    }
    return card;
  }));
  if(!rows.length)list.append(empty(kind==='activity'?'Keine Ereignisse im gelesenen Stand.':'Keine geplanten Aufgaben im gelesenen Stand.'));
}
async function loadReadList(kind){
  if(!state.session||!state.connected)return;
  const list=readLists[kind],epoch=state.epoch,generation=++list.generation;
  const current=()=>state.epoch===epoch&&list.generation===generation&&!!state.session;
  list.loading=true;list.error='';renderReadList(kind);
  try{
    const data=await api(kind==='activity'?'/v1/control/activity':'/v1/control/tasks');
    if(!current())return;
    const rows=data?.[kind==='activity'?'ereignisse':'aufgaben'];
    if(!Array.isArray(rows)||rows.some(row=>!row||typeof row!=='object'||Array.isArray(row)))throw {code:'invalid_response'};
    list.rows=rows;list.stand=Number.isFinite(data.stand)?data.stand:null;
  }catch(e){if(current())list.error=message(e);}
  finally{if(current()){list.loading=false;renderReadList(kind);}}
}
async function showReadDetail(kind,id){
  if(!readId(id)||!state.session||!state.connected)return;
  const epoch=state.epoch,key=Symbol();state.dialogKey=key;
  const current=()=>state.epoch===epoch&&state.dialogKey===key&&$('decision-dialog').open;
  const content=$('decision-content');
  content.replaceChildren(el('h2',kind==='inbox'?'Hinweis':'Geplante Aufgabe'),el('p','Einzelheiten werden gelesen …','small muted'));
  openDecision();
  try{
    const data=await api('/v1/control/'+(kind==='inbox'?'inbox/':'tasks/')+encodeURIComponent(id));
    if(!current())return;
    if(!data||data.id!==id)throw {code:'invalid_response'};
    const nodes=[];
    if(kind==='inbox'){
      if(!Array.isArray(data.befunde)||data.befunde.some(row=>typeof row!=='string'))throw {code:'invalid_response'};
      nodes.push(el('h2',data.zusammenfassung||'Hinweis'),el('p',when(data.zeit)+' · '+(data.quelle||'Quelle nicht belegt'),'small muted'));
      if(data.herkunft)nodes.push(el('p',data.herkunft,'boundary'));
      const findings=el('ul','','read-detail-list');
      for(const finding of data.befunde)if(finding.trim())findings.append(el('li',finding));
      nodes.push(findings.children.length?findings:empty('Keine zusätzlichen Einzelheiten im gespeicherten Hinweis.'));
    }else{
      nodes.push(el('h2',data.titel||'Geplante Aufgabe'),el('p',(data.was||'Aufgabe')+' · '+(data.wann||'Zeitpunkt nicht belegt')));
      nodes.push(el('p',(data.aktiv===true?'Aktiv':data.aktiv===false?'Pausiert':'Aktivität ungeklärt')+' · '+(data.zustand||'Stand ungeklärt')));
      nodes.push(el('p','Nächster Lauf: '+when(data.naechster_lauf)+' · Letzter Lauf: '+when(data.letzter_lauf),'small muted'));
      if(data.letzter_fehler)nodes.push(el('p',data.letzter_fehler,'boundary'));
      if(data.auftrag)nodes.push(el('h3','Dein ursprünglicher Auftrag'),el('p',data.auftrag,'read-detail-text'));
      if(!Array.isArray(data.laeufe)||data.laeufe.some(row=>!row||typeof row!=='object'||Array.isArray(row)))throw {code:'invalid_response'};
      const history=el('ul','','read-detail-list');
      for(const row of data.laeufe)history.append(el('li',when(row.zeit)+' · '+(row.zustand||'Stand ungeklärt')+(row.detail?' · '+row.detail:'')));
      nodes.push(el('h3','Letzte Läufe'),history.children.length?history:empty('Noch kein gespeicherter Lauf.'));
    }
    content.replaceChildren(...nodes);
  }catch(e){
    if(!current())return;
    content.replaceChildren(el('h2','Einzelheiten nicht erreichbar'),el('p',message(e),'error'),
      button('Erneut lesen',()=>{if(current())return showReadDetail(kind,id);}));
  }
}
for(const kind of Object.keys(readLists))$(kind+'-refresh').addEventListener('click',()=>void loadReadList(kind));
async function loadSecondary(view){
  const epoch=state.epoch;
  try{
    if(view==='knowledge'){
      const [memory,candidates]=await Promise.all([api('/v1/memory/memories?limit=100'),api('/v1/memory/candidates')]);if(epoch!==state.epoch)return;
      state.memories=memory.memories;state.candidates=candidates.candidates;
      $('memory-summary').textContent=`${memory.memories.length} von ${memory.total} aktuellen Erinnerungen geladen.`;renderMemories();
      $('candidate-list').replaceChildren(...candidates.candidates.map(c=>{const n=el('article','','memory-card');n.append(el('span','NOCH NICHT BESTÄTIGT','eyebrow'),el('h3',c.statement),el('p',c.ask_reason||'Diese Angabe braucht eine bewusste Entscheidung.'));if(c.contradicts)n.append(el('p','Bisher: '+c.contradicts.content));return n;}));if(!candidates.candidates.length)$('candidate-list').append(empty('Keine offenen Vermutungen.'));
      for(const [index,card] of [...$('candidate-list').querySelectorAll('.memory-card')].entries()){
        const candidate=candidates.candidates[index],actions=el('div','','actions');
        if(candidate.state==='adopting'){card.append(el('p','Die Übernahme hat noch keinen bestätigten Abschluss. Dieser Vorschlag wird nicht erneut übernommen.'));continue;}
        actions.append(button('Stimmt',()=>memoryDialog('memory_confirm_candidate',candidate),'primary'),button('Stimmt nicht',()=>memoryDialog('memory_decline_candidate',candidate)));
        card.append(actions);
      }
      await loadMemoryCommands();
    }else if(view==='activity'||view==='tasks'){
      await loadReadList(view==='activity'?'activity':'recurring');
    }else if(view==='inbox'||view==='feed'){
      const data=await api('/v1/control/inbox');if(epoch!==state.epoch)return;
      const target=$(view==='feed'?'feed-list':'inbox-list');
      const notices=[...data.meldungen].sort((a,b)=>(b.zeit||0)-(a.zeit||0));
      target.replaceChildren(...(view==='feed'?notices.slice(0,8):notices).map(m=>{
        const n=el('article','','inbox-card');n.append(el('span',when(m.zeit),'eyebrow'));
        if(typeof m.lauf==='string'&&/^ar-[0-9a-f]{16}$/.test(m.lauf))n.append(button('Ergebnis ansehen',()=>selectRun(m.lauf)));
        n.append(el('h3',m.zusammenfassung),el('p',m.quelle));
        if(readId(m.id))n.append(button('Hinweis ansehen',()=>showReadDetail('inbox',m.id)));
        n.append(button('Als gelesen markieren',async()=>{await api(`/v1/control/inbox/${encodeURIComponent(m.id)}/read`,{method:'POST'});await loadSecondary(view);}));return n;
      }));if(!data.meldungen.length)target.append(empty('Noch keine Hinweise im gelesenen Stand.'));
      if(view==='feed')target.append(button('Alle Hinweise',()=>showView('inbox')));
    }else if(view==='settings'){
      const policy=await api('/v1/agent/cost-policy');if(epoch!==state.epoch)return;if(document.activeElement!==$('threshold'))$('threshold').value=(policy.ask_threshold_cents/100).toFixed(2);
      $('native-credit-settings').replaceChildren(button('ChatGPT-Credit-Nutzung verwalten',async()=>{
        try{
          const credit=await api('/v1/agent/native-credits');if(epoch!==state.epoch)return;
          const target=$('native-credit-settings');target.replaceChildren(el('p',credit.enabled?'Credit-Nutzung freigegeben.':'Credit-Nutzung noch nicht freigegeben.'),el('p',credit.terms,'small'));
          target.append(button(credit.enabled?'Credits für SOLVIO sperren':'Credits für SOLVIO freigeben',()=>confirmAction('Credit-Nutzung ändern?',credit.terms,async current=>{
            await api('/v1/agent/native-credits',{method:'POST',body:{account:credit.account,enabled:!credit.enabled}});
            if(current())target.replaceChildren(el('p','Bitte in SOLVIO auf dem iPhone unter Freigaben mit Face ID bestätigen. Eine Ablehnung bleibt endgültig.'));
          })));
        }catch(e){if(epoch===state.epoch)$('native-credit-settings').textContent=message(e);}
      }));
    }
  }catch(e){if(epoch!==state.epoch)return;const target={knowledge:'memory-list',inbox:'inbox-list',feed:'feed-list',settings:'cost-message'}[view];if(target)$(target).replaceChildren(empty(message(e)));if(e.status===401){showSession(null);$('login-error').textContent=message(e);}}
}
function renderMemories(){
  const query=$('memory-search').value.trim().toLocaleLowerCase('de-DE');
  const items=(state.memories||[]).filter(m=>(m.content+' '+m.subject).toLocaleLowerCase('de-DE').includes(query));
  $('memory-list').replaceChildren(...items.map(m=>{const n=el('article','','memory-card');n.append(el('span',({learned:'VON SOLVIO ABGELEITET',explicit:'VON DIR GESAGT',confirmed:'VON DIR BESTÄTIGT',temporary:'ZEITLICH BEGRENZT'})[m.lifecycle]||'AKTUELLE ERINNERUNG','eyebrow'),el('h3',m.content),el('p',m.explanation),el('p',`Aktualisiert ${when(m.updated_at)}${m.valid_until?' · Gültig bis '+when(m.valid_until):''}`));
    const details=el('details');details.append(el('summary','Warum weiß SOLVIO das?'));details.addEventListener('toggle',async()=>{if(!details.open||details.dataset.loaded)return;details.dataset.loaded='yes';try{const data=await api(`/v1/memory/memories/${encodeURIComponent(m.id)}/provenance`);for(const row of data.chain||[])details.append(el('p',`${row.source_type} · ${row.source}\n${row.note||''}`));if(!data.chain?.length)details.append(el('p','Keine zusätzliche Herkunftskette verfügbar.'));}catch(e){details.append(el('p',message(e)));}});n.append(details);
    const actions=el('div','','actions');
    if(m.sensitivity!=='secret_reference')actions.append(button('Korrigieren',()=>memoryDialog('memory_correct',m)));
    actions.append(button('Vergessen',()=>memoryDialog('memory_forget',m)),button('Endgültig löschen',()=>memoryDialog('memory_purge',m),'danger'));
    n.append(actions);return n;}));
  if(!items.length)$('memory-list').append(empty(query?'Keine Treffer im geladenen Bestand.':'Noch keine sichtbaren Erinnerungen.'));
}
const memoryNames={memory_correct:'Erinnerung korrigieren',memory_forget:'Erinnerung vergessen',memory_purge:'Erinnerung endgültig löschen',memory_confirm_candidate:'Vermutung bestätigen',memory_decline_candidate:'Vermutung ablehnen'};
const memoryStates={succeeded:'Änderung bestätigt',not_executed:'Nicht ausgeführt',expired:'Befehl abgelaufen',pending:'Noch kein Abschluss',outcome_unconfirmed:'Ausgang ungewiss – wird nicht automatisch wiederholt'};
async function loadMemoryCommands(){
  const epoch=state.epoch;
  try{const data=await api('/v1/dashboard/memory-commands');if(epoch!==state.epoch)return;
    $('memory-commands').replaceChildren(...data.commands.map(c=>{const card=el('article','','surface');card.append(el('h3',memoryNames[c.capability]||'Wissensänderung'),el('p',memoryStates[c.state]||'Ausgang unbekannt'),el('time',when(c.requested_at)));
      const details=el('details');details.append(el('summary','Beauftragte Änderung ansehen'),el('p',c.description,'result-copy'));card.append(details);
      if(c.reason==='memory_target_changed')card.append(el('p','Die Erinnerung wurde zwischenzeitlich geändert. Es ist keine zweite Fassung entstanden.'));
      if(['pending','outcome_unconfirmed'].includes(c.state))card.append(button('Stand erneut lesen',()=>loadMemoryCommands()));return card;}));
    if(!data.commands.length)$('memory-commands').append(empty('Noch keine Wissensänderung aus dem Dashboard.'));
  }catch(e){if(epoch===state.epoch)$('memory-commands').replaceChildren(empty(message(e)));}
}
function memoryDialog(capability,target){
  if(state.memoryPending){state.memoryPending.reopen();return;}
  const epoch=state.epoch,key=Symbol();state.dialogKey=key;
  const current=()=>epoch===state.epoch&&state.dialogKey===key&&$('decision-dialog').open;
  const content=$('decision-content');content.replaceChildren(el('h2',memoryNames[capability]),el('p',target.content||target.statement));
  const explanations={memory_purge:'Diese Erinnerung wird endgültig gelöscht. Das lässt sich nicht rückgängig machen.',memory_forget:'SOLVIO verwendet diese Erinnerung künftig nicht mehr. Der bisherige Eintrag bleibt als Historie erhalten.',memory_correct:'Die korrigierte Aussage ersetzt genau diese Erinnerung. Ihre Herkunft und die Änderung bleiben nachvollziehbar.',memory_confirm_candidate:'Diese Aussage wird als von dir bestätigtes Wissen übernommen.',memory_decline_candidate:'Diese Vermutung wird verworfen und nicht erneut vorgeschlagen.'};
  content.append(el('p',explanations[capability]));
  const form=el('form'),input=el('textarea');
  if(capability==='memory_correct'){const label=el('label','Die richtige Aussage');label.htmlFor='memory-statement';input.id='memory-statement';input.value=target.content;input.maxLength=4000;input.required=true;input.rows=4;form.append(label,input);}
  const submit=el('button',capability==='memory_purge'?'Endgültig löschen':'Änderung ausführen',capability==='memory_purge'?'danger':'primary');submit.type='submit';
  const feedback=el('div');feedback.setAttribute('role','status');form.append(submit,feedback);content.append(form);
  const contents=[...content.childNodes];
  let draft=null;
  async function send(readOnly=false){
    if(!current()||!draft||draft.sending)return;
    draft.sending=true;busyForm(form,true);feedback.replaceChildren(empty(readOnly?'Abschluss wird gelesen.':'Änderung wird übermittelt.'));
    try{
      const result=await api(readOnly?'/v1/dashboard/memory-commands/'+encodeURIComponent(draft.commandId):'/v1/dashboard/memory-commands',readOnly?{}:{method:'POST',body:draft.body});
      if(epoch!==state.epoch||state.memoryPending!==draft)return;
      if(typeof result.command_id!=='string'||!Object.hasOwn(memoryStates,result.state))throw {code:'invalid_response'};
      draft.commandId=result.command_id;
      if(['succeeded','not_executed','expired'].includes(result.state)){
        state.memoryPending=null;
        if(current())$('decision-dialog').close();
        notify(result.reason==='memory_target_changed'?'Die Erinnerung wurde inzwischen geändert. Bitte verwende den frisch gelesenen Eintrag.':memoryStates[result.state]);
        await loadSecondary('knowledge');
      }else{
        feedback.replaceChildren(el('p',memoryStates[result.state]),button('Nur den Stand lesen',()=>send(true)));
        if(current())await loadMemoryCommands();
      }
    }catch(e){
      if(epoch!==state.epoch||state.memoryPending!==draft)return;
      feedback.replaceChildren(el('p',message(e)),button(draft.commandId?'Nur den Stand lesen':'Dieselbe Änderung erneut übermitteln',()=>send(!!draft.commandId)));
      if(e.status&&e.status<500){state.memoryPending=null;draft=null;if(current()){feedback.replaceChildren(el('p',message(e)));busyForm(form,false);}}
    }finally{if(draft)draft.sending=false;}
  }
  form.addEventListener('submit',async event=>{event.preventDefault();if(!current()||draft)return;
    const arguments_=capability.includes('candidate')?{candidate_id:target.candidate_id}:{memory_id:target.id};
    if(capability==='memory_correct')arguments_.statement=input.value.trim();
    draft={body:{capability,arguments:arguments_,client_request_id:crypto.randomUUID()},commandId:null,sending:false,
      reopen(){if(epoch!==state.epoch)return;state.dialogKey=key;content.replaceChildren(...contents);openDecision();}};
    state.memoryPending=draft;await send();
  });
  openDecision();
}
$('login-form').addEventListener('submit',async event=>{
  event.preventDefault();busyForm(event.target,true);$('login-error').textContent='';
  try{const session=await api('/v1/browser/session/login',{method:'POST',body:{token:$('enrollment').value.trim()}});$('enrollment').value='';showSession(session);await refresh();}
  catch(e){$('login-error').textContent=message(e);}finally{busyForm(event.target,false);}
});
$('logout').addEventListener('click',async()=>{stopFilePreviews();voice.end({detach:true});try{await api('/v1/browser/session/logout',{method:'POST'});showSession(null);$('notice').hidden=true;}catch(e){notify(message(e));}});
async function transmitTask(submission){
  const epoch=state.epoch;
  const current=()=>epoch===state.epoch&&state.submitted===submission;
  if(!current()||submission.sending)return;
  submission.sending=true;setTaskControls();$('task-error').replaceChildren();
  try{
    const accepted=await api('/v1/agent/tasks',{method:'POST',body:{task:submission.task}});
    if(!current())return;
    if(typeof accepted.run_id!=='string'||!accepted.run_id)throw {code:'invalid_response'};
    state.submitted=null;$('objective').value='';actionComposer.setEnabled(false);
    $('action-mode').value='natural';$('action-entry').hidden=true;
    $('scope').value='research';$('repository-label').hidden=true;
    notify(accepted.annahme==='preparing'?(submission.task.action_intent?'Auftrag angenommen. SOLVIO klärt jetzt die Angaben.':'Auftrag angenommen. Der Core vervollständigt noch die Startbindung.'):'Auftrag angenommen. Er bleibt auch nach dem Schließen dieses Fensters im Core.');
    await refresh();if(epoch===state.epoch)await selectRun(accepted.run_id);
  }catch(e){
    if(!current())return;
    if(e.code==='network'||e.code==='invalid_response'||e.status>=500){
      submission.uncertain=true;
      $('task-error').replaceChildren(el('span','Annahme ungewiss. Bei erneuter Übermittlung bleibt es derselbe Auftrag mit derselben Kennung. '),Object.assign(button('Denselben Auftrag erneut übermitteln',()=>current()?transmitTask(submission):undefined),{id:'task-retry'}));
    }else{state.submitted=null;$('task-error').textContent=message(e);}
  }finally{submission.sending=false;if(epoch===state.epoch)setConnected(state.connected);}
}
$('task-form').addEventListener('submit',async event=>{
  event.preventDefault();if(!state.connected||state.submitted)return;
  const task={scope:$('scope').value,objective:$('objective').value.trim(),target_repo:$('scope').value==='build'?$('repository').value:'',client_request_id:crypto.randomUUID()};
  if(!task.objective)return;if(task.scope==='build'&&!task.target_repo){$('task-error').textContent='Bitte wähle ein verfügbares Projekt für die Codearbeit.';return;}
  if(task.scope==='action'){
    try{if($('action-mode').value==='natural'){
      if(task.objective.length<12||task.objective.length>2000){$('task-error').textContent='Beschreibe deinen Alltagsauftrag bitte mit 12 bis 2.000 Zeichen.';return;}
      task.action_intent={version:1};
    }else {task.action_request=actionComposer.request();if(!task.action_request)return;}}
    catch(e){$('task-error').textContent=e.message;return;}
  }
  state.submitted={task,uncertain:false,sending:false};await transmitTask(state.submitted);
});
function setActionMode(){
  const enabled=$('scope').value==='action',exact=enabled&&$('action-mode').value==='exact';
  $('action-entry').hidden=!enabled;
  $('action-entry-help').textContent=exact?'Gib die konkreten Angaben für einen Termin, einen Mailentwurf oder ein Gerät ein.':'Beschreibe oben einen Kalendertermin oder einen Mailentwurf. Falls etwas fehlt, fragt SOLVIO im selben Auftrag nach. Ein Mailentwurf wird nicht versendet.';
  actionComposer.setEnabled(exact);setTaskControls();
  if(exact){$('action-fields').querySelector('details').open=true;void actionComposer.load();}
}
$('scope').addEventListener('change',()=>{
  if(state.submitted)return;
  $('repository-label').hidden=$('scope').value!=='build';
  $('action-mode').value='natural';setActionMode();
});
$('action-mode').addEventListener('change',()=>{if(!state.submitted)setActionMode();});
$('document-file').addEventListener('change',()=>void chooseDocument());
$('document-clear').addEventListener('click',()=>{if(!chat.isSending())clearDocument();});
$('memory-search').addEventListener('input',renderMemories);
$('cost-form').addEventListener('submit',async event=>{
  event.preventDefault();const epoch=state.epoch,cents=Math.round(Number($('threshold').value)*100);busyForm(event.target,true);
  try{await api('/v1/agent/cost-policy',{method:'PUT',body:{ask_threshold_cents:cents,client_request_id:crypto.randomUUID()}});if(epoch!==state.epoch)return;$('cost-message').textContent='Gespeichert. Gilt für neue Aufträge.';}catch(e){if(epoch===state.epoch)$('cost-message').textContent=message(e);}finally{if(epoch===state.epoch)busyForm(event.target,false);}
});
for(const b of document.querySelectorAll('[data-view]'))b.addEventListener('click',async()=>{await showView(b.dataset.view);if(b.dataset.focus)$(b.dataset.focus).scrollIntoView({block:'start'});});
for(const b of document.querySelectorAll('[data-room]'))b.addEventListener('click',()=>{
  if(state.room!==b.dataset.room)windowShare.stop();
  state.room=b.dataset.room;void showView('workroom');
});
$('tasks-back').addEventListener('click',()=>void showView('tasks'));
$('open-task-workroom').addEventListener('click',()=>{state.room='hermes';void showView('workroom');});
$('reconnect').addEventListener('click',()=>refresh());
window.addEventListener('online',()=>refresh());
window.addEventListener('offline',()=>setConnected(false));
const poll=setInterval(()=>{if(!document.hidden)void refresh();},12000);
window.addEventListener('beforeunload',event=>{if(state.submitted?.uncertain||state.memoryPending||actionIntent.uncertain||state.followup?.submission||chat.uncertain){event.preventDefault();event.returnValue='';}});
const toolsLife=new AbortController();
if(document.modelContext?.registerTool){
  const tools=[{name:'read_solvio_dashboard',title:'SOLVIO-Stand lesen',description:'Liest den zuletzt sichtbar geladenen Stand. Startet keine Aufgabe und keine Anbieterabfrage.',inputSchema:{type:'object',properties:{},additionalProperties:false},annotations:{readOnlyHint:true,untrustedContentHint:true},execute(input){if(input===null||typeof input!=='object'||Object.keys(input).length)throw new Error('empty object required');return {authenticated:!!state.session,connected:state.connected,view:state.view,runs:state.runs?.map(r=>({id:r.id,objective:r.auftrag,state:r.zustand_code}))??null};}},
    {name:'open_solvio_workspace',title:'SOLVIO-Bereich öffnen',description:'Navigiert zur vorhandenen Ansicht; kein Auftrag, keine Freigabe und keine Gedächtnisänderung.',inputSchema:{type:'object',properties:{view:{type:'string',enum:Object.keys(viewNames)}},required:['view'],additionalProperties:false},annotations:{readOnlyHint:false,untrustedContentHint:true},async execute(input){if(!input||Object.keys(input).length!==1||!Object.hasOwn(viewNames,input.view))throw new Error('invalid view');await showView(input.view);return {view:state.view,authenticated:!!state.session};}}];
  for(const tool of tools){try{Promise.resolve(document.modelContext.registerTool(tool,{signal:toolsLife.signal})).catch(()=>{});}catch{}}
}
window.addEventListener('pagehide',()=>{stopFilePreviews(true);voice.end({detach:true});actionIntent.clear();chat.stop();clearInterval(poll);toolsLife.abort();presence.stop();});
// A restored bfcache page owns neither its old polling loop nor its old login.
window.addEventListener('pageshow',event=>{if(event.persisted){showSession(null);location.reload();}});
try{showSession(await api('/v1/browser/session'));await refresh();}catch(e){showSession(null);if(e.status!==401)$('login-error').textContent=message(e);}
