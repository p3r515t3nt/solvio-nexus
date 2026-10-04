/* Read the Core's existing owned sessions. Opening this selector never opens,
 * signs into or controls a portal. The parent keeps the existing task/retry. */
const PORTAL_ID=/^[A-Za-z0-9][A-Za-z0-9._:@+\-]{0,127}$/;
const SESSION_ID=/^ps-[0-9]+-[0-9]+$/;

export function createPortalActionFields({container,api,isCurrent,onChange}){
  const doc=container.ownerDocument;
  const node=(tag,text='')=>{const item=doc.createElement(tag);item.textContent=text;return item;};
  const label=node('label','Bestehende Portalverbindung');label.className='search-label';
  const select=node('select');select.setAttribute('aria-label','Bestehende Portalverbindung');
  select.setAttribute('data-action-field','portal_session');label.append(select);
  const status=node('p');status.className='small muted';status.setAttribute('role','status');
  status.setAttribute('aria-live','polite');
  const reload=node('button','Verbindungen neu lesen');reload.type='button';
  const help=node('p','Dieser Auftrag liest den Anmeldestatus einer bestehenden Verbindung. Er meldet dich nicht an und verändert keine Portalinhalte.');
  help.className='small muted';container.replaceChildren(label,help,status,reload);
  let enabled=false,busy=false,ready=false,generation=0,controller=null,rows=[],selected='';
  const rowKey=row=>JSON.stringify([row.account,row.target.portal_id,row.target.session_id]);
  function current(){return enabled&&isCurrent();}
  function options(){
    select.replaceChildren();const placeholder=node('option','Bitte eine Verbindung wählen');placeholder.value='';select.append(placeholder);
    for(const [index,row] of rows.entries()){
      const minutes=Math.max(1,Math.ceil(row.expires_in_s/60));
      const option=node('option',`${row.label} · Verbindung ${index+1} · ${row.authenticated?'angemeldet':'Anmeldung nicht bestätigt'} · bis zu ${minutes} Min.`);
      option.value=rowKey(row);select.append(option);
    }
    select.value=rows.some(row=>rowKey(row)===selected)?selected:'';
    select.disabled=!current()||busy||!ready||!rows.length;
    reload.disabled=!current()||busy;
  }
  function clear(){
    generation++;controller?.abort();controller=null;ready=false;rows=[];selected='';
    status.textContent='Portalverbindungen wurden noch nicht gelesen.';options();
  }
  function parse(value){
    if(!value||value.service!=='portal'||!Array.isArray(value.items)||value.items.length>50||
      typeof value.truncated!=='boolean'||!Number.isFinite(value.observed_at))throw Error('invalid_portal_sessions');
    const seen=new Set();
    return value.items.map(item=>{
      const target=item?.target;
      if(!item||typeof item.account!=='string'||!PORTAL_ID.test(item.account)||
        !target||Object.keys(target).sort().join(',')!=='portal_id,session_id'||
        typeof target.portal_id!=='string'||!PORTAL_ID.test(target.portal_id)||
        typeof target.session_id!=='string'||!SESSION_ID.test(target.session_id)||seen.has(target.session_id)||
        typeof item.label!=='string'||!item.label.trim()||typeof item.origin!=='string'||
        typeof item.authenticated!=='boolean'||!Number.isInteger(item.expires_in_s)||
        item.expires_in_s<0||item.expires_in_s>3600)throw Error('invalid_portal_session');
      const origin=new URL(item.origin);
      if(origin.protocol!=='https:'||origin.origin!==item.origin)throw Error('invalid_portal_origin');
      seen.add(target.session_id);
      return {account:item.account,target:{portal_id:target.portal_id,session_id:target.session_id},
        label:item.label.trim().slice(0,160),origin:item.origin,authenticated:item.authenticated,
        expires_in_s:item.expires_in_s};
    });
  }
  async function load(){
    if(!current()||busy)return false;
    const stamp=++generation;controller?.abort();controller=new AbortController();
    ready=false;rows=[];status.textContent='Deine bestehenden Portalverbindungen werden gelesen …';options();
    const active=()=>current()&&!busy&&generation===stamp;
    try{
      const response=await api('/v1/agent/action-portal-sessions?limit=50',{signal:controller.signal});
      if(!active())return false;
      rows=parse(response);ready=true;options();
      status.textContent=!rows.length?'Es ist gerade keine Portalverbindung verfügbar. Hier kann noch kein Statusauftrag gestartet werden.':
        selected&&!select.value?'Die gewählte Verbindung ist nicht mehr verfügbar. Bitte wähle neu.':
        'Wähle die bestehende Verbindung, deren Status SOLVIO lesen soll.';
      if(response.truncated)status.textContent+=' Weitere Verbindungen sind nicht in dieser begrenzten Liste enthalten.';
      onChange();return true;
    }catch{
      if(!active())return false;
      ready=false;rows=[];options();status.textContent='Die Portalverbindungen konnten nicht bestätigt werden. Bitte Verbindung prüfen und neu lesen.';
      onChange();return false;
    }
  }
  function request(){
    if(!current()||!ready)throw Error('Bitte die Portalverbindungen zuerst neu lesen.');
    const row=rows.find(item=>rowKey(item)===select.value);
    if(!row)throw Error('Bitte wähle eine verfügbare Portalverbindung.');
    return {actions:[{action_id:'a1',service:'portal',operation:'status',account:row.account,
      target:{...row.target},payload:{}}]};
  }
  function setEnabled(value){
    const next=!!value;
    if(next===enabled)return;
    enabled=next;if(!enabled)clear();container.hidden=!enabled;options();
    if(enabled)void load();
  }
  function setBusy(value){busy=!!value;options();}
  select.addEventListener('change',()=>{
    if(!current()||busy)return;
    selected=select.value;onChange();
  });
  reload.addEventListener('click',()=>void load());
  clear();container.hidden=true;
  return {setEnabled,setBusy,load,request,clear};
}
