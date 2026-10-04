/* Explicit, authenticated task details. This component only reads the service
 * catalogue; admission and effects belong to the existing Core task entrance. */
import {createPortalActionFields} from './portal-action.js';
const IDENTIFIER=/^[A-Za-z0-9][A-Za-z0-9._:@+\-]{0,127}$/;
const EMAIL=/^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~\-]+@[A-Za-z0-9](?:[A-Za-z0-9.\-]*[A-Za-z0-9])?$/;
const clone=value=>JSON.parse(JSON.stringify(value));

function text(value,label,limit,optional=false){
  if(typeof value!=='string'||Array.from(value).length>limit||(!optional&&!value.trim())||
    /[\u0000-\u0008\u000b-\u001f\ud800-\udfff]/u.test(value))
    throw Error(`${label}: Bitte ${optional?'höchstens':'einen Inhalt mit höchstens'} ${limit} Zeichen eingeben.`);
  return value;
}

function instant(value,label){
  const match=/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(value);
  if(!match)throw Error(`${label}: Bitte Datum und Uhrzeit vollständig eingeben.`);
  const [year,month,day,hour,minute]=match.slice(1).map(Number);
  const date=new Date(year,month-1,day,hour,minute,0,0);
  if(year<1000||date.getFullYear()!==year||date.getMonth()!==month-1||
    date.getDate()!==day||date.getHours()!==hour||date.getMinutes()!==minute)
    throw Error(`${label}: Diese Ortszeit gibt es nicht. Bitte Datum und Zeitumstellung prüfen.`);
  return date.toISOString();
}

function catalogue(value){
  if(!value||!Array.isArray(value.services)||value.services.length>100)
    throw Error('invalid_catalogue');
  const seen=new Set(),rows=[];
  for(const item of value.services){
    if(!item||typeof item!=='object'||typeof item.service!=='string')throw Error('invalid_catalogue');
    if(!['calendar','gmail','ha'].includes(item.service))continue;
    if(typeof item.account!=='string'||!IDENTIFIER.test(item.account)||
      typeof item.resource!=='string'||!IDENTIFIER.test(item.resource)||
      (item.service==='gmail'&&item.resource!=='me')||
      (item.service==='ha'&&item.resource!=='configured_home'))throw Error('invalid_catalogue');
    const row={service:item.service,account:item.account,resource:item.resource};
    const key=JSON.stringify(row);
    row.label=typeof item.label==='string'&&item.label.trim()?item.label.trim().slice(0,160):
      item.service==='calendar'?'Google-Kalender':item.service==='gmail'?'Gmail':'Home Assistant';
    if(!seen.has(key)){seen.add(key);rows.push(row);}
  }
  return rows;
}

function deviceCatalogue(value,account){
  if(!value||value.service!=='ha'||value.account!==account||typeof value.truncated!=='boolean'||
    !Array.isArray(value.items)||value.items.length>100)throw Error('invalid_devices');
  const seen=new Set();
  const items=value.items.map(item=>{
    const entity=item?.target?.entity_id;
    if(typeof entity!=='string'||!/^[a-z][a-z0-9_]*\.[a-z0-9_]+$/.test(entity)||
      Object.keys(item.target).length!==1||seen.has(entity)||item.domain!==entity.split('.')[0]||
      !['name','area','state'].every(key=>typeof item[key]==='string')||
      !Array.isArray(item.operations)||!item.operations.includes('set_state')||
      new Set(item.operations).size!==item.operations.length||item.operations.some(op=>
        op!=='set_state'&&(op!=='set_brightness'||item.domain!=='light')))throw Error('invalid_devices');
    seen.add(entity);
    return {target:{entity_id:entity},name:item.name.slice(0,200),area:item.area.slice(0,100),
      state:item.state.slice(0,100),domain:item.domain,operations:[...item.operations]};
  });
  return {items,truncated:value.truncated};
}

export function createActionComposer({container,api,isCurrent=()=>true,onChange=()=>{}}){
  const doc=container.ownerDocument;
  const node=(tag,content='',className='')=>{
    const result=doc.createElement(tag);result.textContent=content;
    if(className)result.className=className;
    return result;
  };
  const details=node('details','','composer-disclosure');
  details.append(node('summary','Termin, Mail, Gerät oder Portalstatus'));
  const content=node('div','','document-field');details.append(content);
  const fields={},controls=[];
  function field(name,label,{type='text',limit,rows,parent=content}={}){
    const wrapper=node('label',label,'search-label');
    const input=node(rows?'textarea':type==='select'?'select':'input');
    input.setAttribute('data-action-field',name);
    input.setAttribute('aria-label',label);
    if(!rows&&type!=='select')input.type=type;
    if(limit)input.maxLength=limit;
    if(rows)input.rows=rows;
    input.autocomplete='off';
    wrapper.append(input);parent.append(wrapper);
    fields[name]=input;controls.push(input);return input;
  }
  function option(select,value,label){const item=node('option',label);item.value=value;select.append(item);}
  const kind=field('kind','Aktion',{type:'select'});
  option(kind,'','Bitte eine Aktion wählen');
  option(kind,'calendar','Kalendertermin erstellen');
  option(kind,'gmail','Mailentwurf erstellen');
  option(kind,'ha','Gerät schalten');
  option(kind,'portal','Portalstatus ansehen');
  const account=field('account','Verknüpftes Konto',{type:'select'});
  const calendar=node('div'),mail=node('div'),home=node('div'),portalContainer=node('div');
  content.append(calendar,mail,home,portalContainer);
  field('summary','Titel',{limit:500,parent:calendar});
  field('start','Beginn',{type:'datetime-local',parent:calendar});
  field('end','Ende',{type:'datetime-local',parent:calendar});
  let zone='Ortszeit dieses Browsers';
  try{zone=Intl.DateTimeFormat().resolvedOptions().timeZone||zone;}catch{}
  calendar.append(node('p',`Zeitzone: ${zone}. Bitte konkrete Daten und Uhrzeiten wählen.`,'small muted'));
  field('location','Ort (optional)',{limit:500,parent:calendar});
  field('description','Beschreibung (optional)',{limit:4000,rows:3,parent:calendar});
  field('to','Empfänger',{type:'email',limit:254,parent:mail});
  const mailMode=field('mail_mode','Entwurf',{type:'select',parent:mail});
  option(mailMode,'compose','SOLVIO formulieren lassen');
  option(mailMode,'exact','Fertigen Text verwenden');
  const composed=node('div'),exact=node('div');mail.append(composed,exact);
  field('instruction','Dein Anliegen',{limit:4000,rows:5,parent:composed});
  composed.append(node('p','Beschreibe, worum es geht und welchen Ton du möchtest. SOLVIO formuliert Betreff und Nachricht.','small muted'));
  field('subject','Betreff',{limit:500,parent:exact});
  field('body','Nachricht',{limit:8000,rows:5,parent:exact});
  mail.append(node('p','Der Auftrag erstellt einen Entwurf in deinem Postfach. Er versendet ihn nicht.','small muted'));
  const device=field('device','Gerät',{type:'select',parent:home});
  const desired=field('desired','Gewünschter Zustand',{type:'select',parent:home});
  const brightness=field('brightness','Helligkeit in Prozent',{type:'number',parent:home});
  brightness.min='0';brightness.max='100';brightness.step='1';
  const deviceStatus=node('p','','small muted');deviceStatus.setAttribute('role','status');
  deviceStatus.setAttribute('aria-live','polite');
  const reloadDevices=node('button','Geräteliste neu laden');reloadDevices.type='button';controls.push(reloadDevices);
  home.append(deviceStatus,reloadDevices);
  const status=node('p','','small muted');status.setAttribute('role','status');
  status.setAttribute('aria-live','polite');
  const error=node('p','','error');error.setAttribute('role','alert');
  const reload=node('button','Konten neu laden');reload.type='button';controls.push(reload);
  content.append(status,error,reload);container.replaceChildren(details);
  let enabled=false,busy=false,ready=false,generation=0,controller=null,rows=[],frozen=null;
  let deviceReady=false,deviceGeneration=0,deviceController=null,deviceAccount='',devices=[],truncated=false;
  const selections=new Map(),deviceSelections=new Map();
  const portal=createPortalActionFields({container:portalContainer,api,isCurrent,onChange});
  const key=row=>JSON.stringify([row.service,row.account,row.resource]);
  const currentRows=()=>rows.filter(row=>row.service===kind.value);
  function refresh(){
    container.hidden=!enabled;
    portal.setBusy(busy);portal.setEnabled(enabled&&kind.value==='portal');
    calendar.hidden=kind.value!=='calendar';mail.hidden=kind.value!=='gmail';home.hidden=kind.value!=='ha';
    composed.hidden=mailMode.value!=='compose';exact.hidden=mailMode.value!=='exact';
    account.parentNode.hidden=!kind.value||kind.value==='portal';
    status.hidden=kind.value==='portal';reload.hidden=kind.value==='portal';
    for(const control of controls)control.disabled=!enabled||busy;
    account.disabled=account.disabled||!ready||!currentRows().length;
    for(const name of ['summary','start','end','location','description'])
      fields[name].disabled=fields[name].disabled||calendar.hidden||!ready||!currentRows().length;
    for(const name of ['to','mail_mode','instruction','subject','body'])
      fields[name].disabled=fields[name].disabled||mail.hidden||!ready||!currentRows().length;
    fields.instruction.disabled=fields.instruction.disabled||composed.hidden;
    fields.subject.disabled=fields.subject.disabled||exact.hidden;
    fields.body.disabled=fields.body.disabled||exact.hidden;
    const availableDevice=deviceReady&&deviceAccount===account.value&&devices.some(row=>row.target.entity_id===device.value);
    device.disabled=device.disabled||home.hidden||!deviceReady||!devices.length;
    desired.disabled=desired.disabled||home.hidden||!availableDevice;
    brightness.parentNode.hidden=desired.value!=='brightness';
    brightness.disabled=brightness.disabled||desired.disabled||brightness.parentNode.hidden;
    reloadDevices.disabled=reloadDevices.disabled||home.hidden||!ready||!account.value;
  }
  function accounts(){
    account.replaceChildren();option(account,'','Bitte ein Konto wählen');
    const available=currentRows();
    for(const row of available){
      const duplicate=available.filter(other=>other.label===row.label&&other.resource===row.resource).length>1;
      option(account,key(row),`${row.label}${row.service==='calendar'?' · '+row.resource:''}${duplicate?' · '+row.account.slice(-6):''}`);
    }
    const previous=selections.get(kind.value);
    if(previous===undefined&&available.length===1)selections.set(kind.value,key(available[0]));
    account.value=available.some(row=>key(row)===selections.get(kind.value))?selections.get(kind.value):'';
    if(ready){
      if(!kind.value)status.textContent='Wähle eine Aktion und ergänze die konkreten Angaben.';
      else if(!available.length)status.textContent=kind.value==='ha'?
        'Kein Zuhause verbunden. Home Assistant muss zuerst im Core eingerichtet werden.':kind.value==='calendar'?
        'Kein Kalender verbunden. Ein Kalenderkonto muss zuerst im Core eingerichtet werden.':
        'Kein Mailkonto verbunden. Ein Mailkonto muss zuerst im Core eingerichtet werden.';
      else if(previous&& !account.value)status.textContent='Das gewählte Konto ist nicht mehr verfügbar. Bitte wähle das passende Konto neu.';
      else status.textContent=account.value?'Verknüpftes Konto ausgewählt.':
        'Bitte wähle das Konto für diesen Auftrag ausdrücklich aus.';
    }
    refresh();
  }
  function clear(){
    generation++;controller?.abort();controller=null;ready=false;rows=[];selections.clear();frozen=null;
    deviceSelections.clear();invalidateDevices();
    portal.clear();
    for(const field of Object.values(fields))field.value='';
    mailMode.value='compose';
    error.textContent='';status.textContent='Konten wurden noch nicht geladen.';
    details.open=false;accounts();
  }
  function deviceOptions(){
    const previousDesired=desired.value;
    device.replaceChildren();option(device,'','Bitte ein Gerät wählen');
    for(const item of devices)option(device,item.target.entity_id,
      `${item.name||item.target.entity_id}${item.area?' · '+item.area:''} · ${item.target.entity_id}`);
    const previous=deviceSelections.get(deviceAccount);
    device.value=devices.some(item=>item.target.entity_id===previous)?previous:'';
    desired.replaceChildren();option(desired,'','Bitte einen Zustand wählen');
    const item=devices.find(item=>item.target.entity_id===device.value);
    if(item){
      option(desired,'on','Einschalten');option(desired,'off','Ausschalten');
      if(item.operations.includes('set_brightness'))option(desired,'brightness','Helligkeit einstellen');
    }
    desired.value=previousDesired;
    if(deviceReady){
      deviceStatus.textContent=previous&&!item?'Das gewählte Gerät ist nicht mehr verfügbar. Bitte wähle neu.':
        !devices.length?'Es sind derzeit keine schaltbaren Geräte für diesen Auftrag verfügbar.':
        item?`Zuletzt gelesen: ${item.state}. Wähle den gewünschten Zustand.`:'Wähle das Gerät für diesen Auftrag ausdrücklich aus.';
      if(truncated)deviceStatus.textContent+=' Die Liste ist begrenzt; nicht angezeigte Geräte können hier noch nicht gewählt werden.';
    }
    refresh();
  }
  function invalidateDevices(){
    deviceGeneration++;deviceController?.abort();deviceController=null;
    deviceReady=false;deviceAccount='';devices=[];truncated=false;
    deviceStatus.textContent='Die Geräteliste wird erst nach deiner Auswahl gelesen.';deviceOptions();
  }
  async function loadDevices(){
    if(!current()||!enabled||busy||kind.value!=='ha'||!ready||!account.value)return false;
    const selected=currentRows().find(row=>key(row)===account.value);
    if(!selected)return false;
    const selectedKey=key(selected),rememberedDesired=desired.value;
    invalidateDevices();deviceAccount=selectedKey;
    const stamp=deviceGeneration;deviceController=new AbortController();
    deviceStatus.textContent='Verfügbare Geräte werden frisch gelesen …';refresh();
    const active=()=>stamp===deviceGeneration&&enabled&&current()&&kind.value==='ha'&&
      ready&&account.value===selectedKey;
    try{
      const value=await api('/v1/agent/action-resources?service=ha&account='+encodeURIComponent(selected.account)+'&limit=100',
        {signal:deviceController.signal});
      if(!active())return false;
      const result=deviceCatalogue(value,selected.account);devices=result.items;truncated=result.truncated;
      deviceReady=true;deviceOptions();desired.value=rememberedDesired;refresh();onChange();return true;
    }catch{
      if(!active())return false;
      deviceReady=false;devices=[];deviceOptions();
      deviceStatus.textContent='Die Geräteliste konnte nicht bestätigt werden. Bitte Verbindung prüfen und neu laden.';
      onChange();return false;
    }
  }
  function current(){
    if(isCurrent())return true;
    enabled=false;clear();return false;
  }
  function buildRequest(){
    if(kind.value==='portal')return portal.request();
    if(!ready)throw Error('Die verbundenen Konten sind noch nicht bestätigt. Bitte Konten neu laden.');
    if(!['calendar','gmail','ha'].includes(kind.value))throw Error('Bitte wähle eine Aktion.');
    const row=currentRows().find(row=>key(row)===account.value);
    if(!row)throw Error(currentRows().length?'Bitte wähle ein verfügbares Konto.':
      'Für diese Aktion ist kein Konto verbunden.');
    let target,payload,operation;
    if(kind.value==='calendar'){
      const start=instant(fields.start.value,'Beginn'),end=instant(fields.end.value,'Ende');
      if(Date.parse(end)<=Date.parse(start))throw Error('Das Ende muss nach dem Beginn liegen.');
      operation='create';target={calendar_id:row.resource};
      payload={summary:text(fields.summary.value,'Titel',500),start,end,all_day:false,
        description:text(fields.description.value,'Beschreibung',4000,true),
        location:text(fields.location.value,'Ort',500,true)};
    }else if(kind.value==='ha'){
      if(!deviceReady||deviceAccount!==key(row))throw Error('Die Geräteliste ist noch nicht bestätigt. Bitte neu laden.');
      const item=devices.find(item=>item.target.entity_id===device.value);
      if(!item)throw Error('Bitte wähle ein verfügbares Gerät.');
      target={entity_id:item.target.entity_id};
      if(['on','off'].includes(desired.value)&&item.operations.includes('set_state')){
        operation='set_state';payload={state:desired.value};
      }else if(desired.value==='brightness'&&item.operations.includes('set_brightness')){
        if(!/^(?:0|[1-9][0-9]?|100)$/.test(brightness.value))throw Error('Bitte eine Helligkeit von 0 bis 100 Prozent eingeben.');
        operation='set_brightness';payload={brightness_pct:Number(brightness.value)};
      }else throw Error('Bitte wähle einen verfügbaren gewünschten Zustand.');
    }else{
      const to=fields.to.value;
      if(to.length>254||!EMAIL.test(to))throw Error('Bitte genau eine vollständige E-Mail-Adresse als Empfänger eingeben.');
      if(mailMode.value==='compose'){
        operation='compose_draft';target={mailbox:'me',to};
        payload={instruction:text(fields.instruction.value,'Dein Anliegen',4000)};
      }else if(mailMode.value==='exact'){
        const subject=text(fields.subject.value,'Betreff',500);
        if(/[\r\n]/.test(subject))throw Error('Der Betreff darf keinen Zeilenumbruch enthalten.');
        operation='create_draft';target={mailbox:'me',to,reply_to_message:''};
        payload={subject,body:text(fields.body.value,'Nachricht',8000),thread_id:'',in_reply_to:''};
      }else throw Error('Bitte wähle, wie der Entwurf entstehen soll.');
    }
    return {actions:[{action_id:'a1',service:row.service,operation,account:row.account,target,payload}]};
  }
  function request(){
    if(!current()||!enabled)return null;
    try{
      if(busy){if(!frozen)throw Error('Der laufende Auftrag kann hier nicht verändert werden.');return clone(frozen);}
      const result=buildRequest();error.textContent='';return result;
    }catch(failure){error.textContent=failure.message;details.open=true;throw failure;}
  }
  function setBusy(value){
    const next=!!value;
    if(next&&!busy){try{frozen=enabled&&current()?buildRequest():null;}catch{frozen=null;}}
    if(!next)frozen=null;
    busy=next;refresh();
  }
  function setEnabled(value){
    const next=!!value&&current();
    if(enabled===next)return;
    enabled=next;if(!enabled)clear();refresh();
  }
  async function load(){
    if(!current()||!enabled||busy)return false;
    const stamp=++generation;controller?.abort();controller=new AbortController();
    invalidateDevices();
    ready=false;rows=[];error.textContent='';status.textContent='Verbundene Konten werden gelesen …';accounts();
    const active=()=>stamp===generation&&enabled&&current();
    try{
      const value=await api('/v1/agent/action-services',{signal:controller.signal});
      if(!active())return false;
      rows=catalogue(value);ready=true;accounts();onChange();
      if(kind.value==='ha')await loadDevices();return true;
    }catch{
      if(!active())return false;
      rows=[];ready=false;accounts();
      status.textContent='Die verbundenen Konten konnten nicht gelesen werden. Bitte Anmeldung und Verbindung prüfen und erneut laden.';
      onChange();return false;
    }
  }
  for(const [name,input] of Object.entries(fields))input.addEventListener(['kind','account','mail_mode','device','desired'].includes(name)?'change':'input',()=>{
    if(!current()||!enabled||busy)return;
    error.textContent='';
    if(name==='kind'){invalidateDevices();accounts();if(kind.value==='ha')void loadDevices();}
    if(name==='account'){
      invalidateDevices();desired.value='';brightness.value='';
      selections.set(kind.value,account.value);accounts();if(kind.value==='ha')void loadDevices();
    }
    if(name==='device'){
      deviceSelections.set(deviceAccount,device.value);desired.value='';brightness.value='';deviceOptions();
    }
    if(name==='desired')refresh();
    if(name==='mail_mode')refresh();
    onChange();
  });
  reload.addEventListener('click',()=>{void load();});
  reloadDevices.addEventListener('click',()=>{void loadDevices();});
  clear();refresh();
  return {setEnabled,load,loadDevices,request,clear,setBusy};
}
