/* Core-owned read projection into pinned native Hermes Desktop components.
 * WindowShare is the existing N6 transport, opened only by its own buttons. */
import {WindowShare} from '/dashboard/assets/window-share.js';

const RUNTIME='hermes-codex-app-server';
const identifier=value=>typeof value==='string'&&/^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$/.test(value);
const states={pending:'Ausstehend',running:'In Arbeit',succeeded:'Abgeschlossen',failed:'Fehlgeschlagen',
  unknown:'Ausgang ungewiss',skipped:'Übersprungen',cancelled:'Abgebrochen'};

export function observedCalls(events){
  const groups=new Map();
  for(const event of events){
    const observation=event.observation;
    if(event.kind!=='native_progress'||!observation||observation.runtime!==RUNTIME||
      ![event.step_id,observation.invocation_id,observation.native_thread_id,observation.native_turn_id].every(identifier)||
      !Number.isInteger(observation.seq)||observation.seq<1||
      !['started','web_search'].includes(observation.event)||
      observation.event==='web_search'&&!['started','completed'].includes(observation.status))continue;
    const key=JSON.stringify([event.step_id,observation.invocation_id,observation.native_thread_id,observation.native_turn_id]);
    let group=groups.get(key);
    if(!group){group={step:event.step_id,invocation:observation.invocation_id,thread:observation.native_thread_id,
      turn:observation.native_turn_id,started:false,searches:new Map(),seen:new Set(),lastAt:event.at};groups.set(key,group);}
    if(group.seen.has(observation.seq))continue;
    group.seen.add(observation.seq);group.lastAt=event.at;
    if(observation.event==='started')group.started=true;
    else if(identifier(observation.item_id))group.searches.set(observation.item_id,observation.status);
  }
  return [...groups.values()].map(group=>({...group,seen:undefined,
    searches:[...group.searches].map(([id,status])=>({id,status}))}));
}

export function mountWorkspace(root, nativeRoot, {read, interval=2500, windowShare=WindowShare}={}){
  const doc=root.ownerDocument;
  const node=(tag,text='',className='')=>{
    const element=doc.createElement(tag);element.textContent=text;if(className)element.className=className;return element;
  };
  const title=node('h1','Arbeitsraum'),status=node('p','Noch keine bestätigte Verbindung.','workspace-muted');
  const choose=node('label','Auftrag','workspace-picker'),picker=node('select');
  picker.setAttribute('aria-label','Auftrag ansehen');choose.append(picker);choose.hidden=true;
  status.setAttribute('role','status');status.setAttribute('aria-live','polite');
  const state=node('p','','workspace-state'),content=node('div'),calls=node('div');
  const header=node('header','','workspace-header');header.append(title,state,status);
  const details=node('details','','workspace-details');details.setAttribute('data-workspace-details','');
  const stamp=node('p','','workspace-muted'),evidenceRoot=node('div');evidenceRoot.id='solvio-hermes-evidence';evidenceRoot.hidden=true;
  const help=node('div','','workspace-help');help.append(node('h2','Über diesen Arbeitsraum'));
  help.append(node('p','Die originale Nachrichtenliste aus Hermes Desktop zeigt deinen Auftrag, belegte Rechercheereignisse aus Hermes’ nativem Codex-Transport und das Core-Ergebnis. Der Auftrag bleibt im SOLVIO-Core und kann mehrere Fachagenten und Werkzeuge nutzen.'));
  help.append(node('p','Ergebnis, Quellen, Dateien, Abbruch und Fortsetzung findest du im SOLVIO-Dashboard und in der iPhone-App. Das Beenden einer Fensteransicht beendet keinen Auftrag.'));
  help.append(node('p','Die native Hermes-Chat-, Terminal-, Skills- und MCP-Verwaltung ist hier nicht als Steuerung angeschlossen. Ein Fensterbild erhält dadurch keine Eingaberechte.'));
  const sourceHint=node('p','Quellenlinks öffnest du im SOLVIO-Dashboard. Dieses lesende Fenster öffnet keine weiteren Seiten.');
  sourceHint.hidden=true;help.append(sourceHint);
  const attribution=node('p','Nachrichtenansicht: Hermes Desktop 0.17.0. ');
  for(const [label,href] of [['Originalquelle','https://github.com/NousResearch/hermes-agent/tree/fcbd1076a93841fa88855acce810e342a5b78101/apps/desktop'],
    ['Hermes-Lizenz','/dashboard/hermes/LICENSE-Hermes.txt'],['Bibliothekslizenzen','/dashboard/hermes/THIRD-PARTY-LICENSES.txt']]){
    const link=node('a',label);link.href=href;link.target='_blank';link.rel='noopener noreferrer';attribution.append(link);
  }
  help.append(attribution);
  details.append(node('summary','Details'),stamp,content,calls,evidenceRoot,help);
  const desktop=node('details');desktop.append(node('summary','Hermes-Fenster ansehen (optional)'));
  const sharing=node('section'),channel=node('select');channel.setAttribute('aria-label','Fensterbereich');
  const option=node('option','Hermes Desktop');option.value='hermes';channel.append(option);channel.hidden=true;
  sharing.append(node('p','Zusätzliches Fensterbild · nur ansehen, ohne Ton. Es ist nicht automatisch dem ausgewählten Auftrag zugeordnet. Die Fensterwahl am Mac gehört dir; der Browser bestätigt den App-Namen nicht.'));
  sharing.append(channel);
  const buttons=node('div','','workspace-controls');
  for(const [attribute,label] of [['watch','Fenster ansehen'],['stop','Aufnahme / Ansicht beenden']]){
    const button=node('button',label);button.type='button';button.setAttribute('data-share-'+attribute,'');buttons.append(button);
  }
  const shareStatus=node('p','','workspace-muted');shareStatus.setAttribute('data-share-status','');shareStatus.setAttribute('role','status');
  const video=node('video');video.autoplay=true;video.muted=true;video.playsInline=true;video.hidden=true;
  video.setAttribute('aria-label','Optionales Hermes-Fensterbild');
  const publish=node('details');publish.append(node('summary','Am Mac bereitstellen'));
  publish.append(node('p','Öffne dieses Dashboard am Mac, wähle den Auftrag und dann ausdrücklich das gewünschte Hermes-Fenster. Die Senderseite bleibt während der Übertragung geöffnet.'));
  const select=node('button','Fenster auf diesem Rechner auswählen');select.type='button';select.setAttribute('data-share-publish','');publish.append(select);
  sharing.append(buttons,shareStatus,video,publish);desktop.append(sharing);
  // Keep the original React container; moving it does not create another reader.
  // Messages/results lead. Technical evidence and window controls stay opt-in.
  root.replaceChildren(choose,header,nativeRoot,details,desktop);
  let session=null,stopped=false,busy=false,controller=null,known=[],cursor=0,truncated=false,projection=null,generation=0;
  const emitProjection=()=>window.dispatchEvent(new CustomEvent('solvio-hermes-projection',{detail:projection}));
  window.addEventListener('solvio-hermes-view-ready',emitProjection);
  const share=new windowShare(sharing,()=>session);
  const guardControls=()=>{desktop.hidden=!session||session.purpose==='hermes_observer_v1';
    sourceHint.hidden=session?.purpose!=='hermes_observer_v1';
    for(const element of sharing.querySelectorAll('button'))element.disabled=!session||desktop.hidden||
    (element.hasAttribute('data-share-stop')?!share.role:!!share.role);};
  desktop.addEventListener('toggle',()=>{
    if(!desktop.open&&share.role){share.stop('Fensteransicht beim Einklappen beendet.');guardControls();}
  });
  guardControls();
  let run=new URL(location.href).searchParams.get('run')||'';
  let valid=/^ar-[a-f0-9]{16}$/.test(run),path='/v1/agent/runs/'+encodeURIComponent(run);
  const get=read||async function(url,signal){
    const response=await fetch(url,{credentials:'same-origin',cache:'no-store',signal});
    if(!response.ok)throw Object.assign(Error('unavailable'),{status:response.status});
    return response.json();
  };
  function clear(message,shareMessage){
    session=null;known=[];cursor=0;truncated=false;share.stop(shareMessage);guardControls();
    picker.replaceChildren();choose.hidden=true;
    nativeRoot.hidden=true;evidenceRoot.hidden=true;projection=null;emitProjection();
    title.textContent='Arbeitsraum';state.textContent='';
    content.replaceChildren();calls.replaceChildren();stamp.textContent='';status.hidden=false;status.textContent=message;
  }
  picker.addEventListener('change',()=>{
    const selected=picker.value;
    if(!/^ar-[a-f0-9]{16}$/.test(selected)||selected===run)return;
    generation++;controller?.abort();clear('Arbeitsstand wird gelesen.',
      'Anderer Auftrag ausgewählt. Aufnahme und Fensteransicht auf diesem Rechner wurden beendet.');
    run=selected;valid=true;path='/v1/agent/runs/'+run;
    const next=new URL(location.href);next.searchParams.set('run',run);history.replaceState(null,'',next);
    void refresh();
  });
  function showChoices(catalog){
    if(!Array.isArray(catalog?.laeufe))throw Error('invalid_catalog');
    const rows=catalog.laeufe.filter(row=>/^ar-[a-f0-9]{16}$/.test(row.id));
    const options=[['','Auftrag auswählen'],...rows.map(row=>[row.id,row.auftrag||'Auftrag'])];
    if(valid&&!rows.some(row=>row.id===run))options.push([run,'Ausgewählter Auftrag']);
    if(JSON.stringify([...picker.options].map(o=>[o.value,o.textContent]))!==JSON.stringify(options)){
      picker.replaceChildren(...options.map(([value,label])=>{const option=node('option',label);option.value=value;return option;}));
    }
    picker.value=run;choose.hidden=false;
  }
  function render(detail){
    const groups=observedCalls(known);
    title.textContent='Auftrag';state.textContent=detail.zustand||'Stand unbekannt';status.hidden=true;
    stamp.textContent='Im Core gelesen · '+new Date().toLocaleTimeString('de-DE')+
      (truncated?' · Älterer Verlauf möglicherweise gekürzt.':'');
    const entries=Array.isArray(detail.schritte)?detail.schritte:[];
    const list=node('ol');
    for(const step of entries){
      const name=step.spezialist||step.faehigkeit||'Core-Schritt';
      const item=node('li',name+' · '+(states[step.zustand]||'Stand unbekannt'));
      if(step.zusammenfassung)item.append(node('p',step.zusammenfassung,'workspace-muted'));list.append(item);
    }
    content.replaceChildren(node('h2','Beteiligte Schritte ('+entries.length+')'),list);
    nativeRoot.hidden=false;evidenceRoot.hidden=groups.length===0;
    const bound=new Set(groups.map(group=>JSON.stringify([group.step,group.invocation,group.thread,group.turn])));
    projection={run,detail,events:known.filter(event=>{
      const o=event.observation;
      return event.kind==='native_progress'&&o?.runtime===RUNTIME&&
        ['started','web_search'].includes(o.event)&&bound.has(JSON.stringify([event.step_id,o.invocation_id,o.native_thread_id,o.native_turn_id]));
    })};
    emitProjection();
    if(!groups.length){calls.replaceChildren(node('p',truncated?
      'Im verfügbaren Verlauf liegen keine belegten Hermes-Aufrufe. Frühere Arbeit kann fehlen.':
      'Für diesen Auftrag liegen noch keine belegten Hermes-Aufrufe vor. Sein tatsächlicher Arbeitsstand steht oben.','workspace-muted'));return;}
    const searches=groups.flatMap(group=>group.searches),completed=searches.filter(search=>search.status==='completed').length;
    calls.replaceChildren(node('h2',groups.length+' belegte Hermes-Aufruf'+(groups.length===1?'':'e')),
      node('p',completed+' von '+searches.length+' beobachteten Websuchen beendet. Das ist noch kein Auftragsabschluss.','workspace-muted'));
  }
  async function refresh(){
    if(stopped||busy||document.hidden)return;
    const revision=generation,selectedPath=path;
    const stale=()=>stopped||revision!==generation;
    busy=true;controller=new AbortController();const timeout=setTimeout(()=>controller.abort(),10000);
    try{
      const identity=await get('/v1/browser/session',controller.signal);
      if(stale())return;
      if(!identity?.csrf_token||!identity?.session_id)throw Error('invalid_binding');
      if(identity.purpose==='hermes_observer_v1'){
        const catalog=await get('/v1/agent/runs',controller.signal);
        if(stale())return;
        if(!valid){clear('Wähle einen Auftrag aus.');session=identity;guardControls();showChoices(catalog);return;}
        showChoices(catalog);session=identity;guardControls();
      }
      if(!valid){clear('Bitte wähle im Dashboard einen gültigen Auftrag.');return;}
      const detail=await get(selectedPath,controller.signal);
      if(stale())return;
      if(!identity?.csrf_token||!identity?.session_id||detail.id!==run)throw Error('invalid_binding');
      let more=true;
      for(let page=0;more&&page<21;page++){
        const data=await get(selectedPath+'/events?after='+cursor+'&limit=100',controller.signal);
        if(stale())return;
        if(!Array.isArray(data.events)||!Number.isInteger(data.next_cursor)||data.next_cursor<cursor)throw Error('invalid_events');
        known.push(...data.events);truncated ||= !!data.history_may_be_incomplete||known.length>2000;
        known=known.slice(-2000);cursor=data.next_cursor;more=!!data.has_more;
      }
      if(more)truncated=true;
      if(stale())return;
      if(session&&session.session_id!==identity.session_id)share.stop('Anmeldung geändert. Fensteransicht beendet.');
      session=identity;guardControls();render(detail);
    }catch(error){
      if(stale())return;
      if([401,403,404].includes(error.status)){
        clear('Auftrag oder Anmeldung nicht mehr verfügbar. Bitte im Dashboard neu auswählen.',
          'Auftrag oder Anmeldung nicht mehr verfügbar. Aufnahme und Fensteransicht auf diesem Rechner wurden beendet.');
      }else{
        session=null;share.stop('Arbeitsstand konnte nicht mehr gelesen werden. Aufnahme und Fensteransicht auf diesem Rechner wurden beendet.');guardControls();nativeRoot.hidden=true;evidenceRoot.hidden=true;
        projection=null;emitProjection();
        status.hidden=false;status.textContent='Verbindung unterbrochen. Die letzte Ansicht ist veraltet; eine Fensteransicht wurde beendet.';
      }
    }finally{clearTimeout(timeout);busy=false;if(!stopped&&revision!==generation)void refresh();}
  }
  function stop(){stopped=true;controller?.abort();clearInterval(timer);clear('Ansicht beendet. Der Auftrag kann im Core weiterlaufen.',
    'Seite verlassen. Aufnahme und Fensteransicht auf diesem Rechner wurden beendet.');window.removeEventListener('solvio-hermes-view-ready',emitProjection);}
  function visibility(){if(document.hidden){controller?.abort();share.stop(
    'Seite nicht mehr sichtbar. Aufnahme und Fensteransicht auf diesem Rechner wurden beendet.');guardControls();}else void refresh();}
  window.addEventListener('pagehide',stop);
  document.addEventListener('visibilitychange',visibility);
  const timer=setInterval(()=>void refresh(),interval);
  void refresh();
  return {refresh,stop,share};
}

const root=document.getElementById('solvio-workspace');
if(root)mountWorkspace(root,document.getElementById('root'));
