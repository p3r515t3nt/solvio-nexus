/* A question is part of one accepted task. Polling never manufactures another
 * task or a resume request. An unconfirmed answer keeps its original bytes/ID. */
export function createActionIntent({api, isCurrent, onRefresh}) {
  const el=(tag,text='',cls='')=>{const n=document.createElement(tag);n.textContent=text;if(cls)n.className=cls;return n;};
  const root=el('section','','action-question boundary');root.hidden=true;
  root.setAttribute('aria-label','Rückfrage zu diesem Auftrag');
  const entries=new Map();let active=null,available=false,fresh=false;
  const owned=entry=>isCurrent()&&entries.get(entry.key)===entry;
  const current=entry=>owned(entry)&&active===entry;
  const validQuestion=q=>q&&typeof q.id==='string'&&q.id.length>0&&Number.isInteger(q.revision)&&q.revision>=1&&
    typeof q.digest==='string'&&q.digest.length>0&&typeof q.field==='string'&&typeof q.prompt==='string'&&q.prompt.trim();
  function sync(entry){
    const disabled=!current(entry)||!available||!fresh||entry.sending||entry.stale||entry.acknowledged;
    entry.input.disabled=disabled||!!entry.submission||entry.emptyChoices;
    entry.submit.disabled=disabled||entry.emptyChoices;
    entry.submit.textContent=entry.submission?'Dieselbe Antwort erneut übermitteln':'Antworten';
    entry.read.disabled=!current(entry)||!available||entry.sending;
    entry.read.hidden=!(entry.submission||entry.stale||entry.acknowledged||!fresh);
    entry.catalog.disabled=disabled||!!entry.submission;
  }
  async function read(entry){
    if(!current(entry)||!available||entry.sending)return;
    try{await onRefresh(entry.runId);}catch{if(current(entry))unavailable();}
  }
  function unavailable(){
    fresh=false;
    if(active){active.feedback.textContent='Der aktuelle Auftragsstand ist nicht erreichbar. Deine Eingabe bleibt hier erhalten. Bitte den Stand erneut lesen.';sync(active);}
  }
  function make(runId,q,key){
    const form=el('form'),heading=el('h4','Noch eine Angabe'),label=el('label',q.prompt);
    const select=q.input_type==='select';
    const input=el(select?'select':'input');input.id='action-answer';input.required=true;
    label.htmlFor=input.id;input.setAttribute('aria-describedby','action-answer-feedback');
    if(select){
      const placeholder=el('option','Bitte wählen');placeholder.value='';input.append(placeholder);
      const seen=new Set();
      for(const item of Array.isArray(q.options)?q.options:[]){
        if(typeof item?.value!=='string'||!item.value||seen.has(item.value)||typeof item.label!=='string')continue;
        seen.add(item.value);const option=el('option',item.label);option.value=item.value;input.append(option);
      }
      input.value='';
    }else{
      input.type=['email','number'].includes(q.input_type)?q.input_type:'text';
      input.maxLength=2000;input.placeholder=typeof q.placeholder==='string'?q.placeholder:'';
      if(input.type==='number'){input.min='1';input.step='1';}
    }
    const feedback=el('p','','small');feedback.id='action-answer-feedback';feedback.setAttribute('role','status');
    const controls=el('div','','actions'),submit=el('button','Antworten','primary'),readButton=el('button','Stand erneut lesen','quiet');
    const catalog=el('button','Verbundene Konten neu prüfen','quiet');catalog.type='button';catalog.hidden=q.field!=='account';
    submit.type='submit';readButton.type='button';controls.append(submit,catalog,readButton);
    form.append(heading,label,input,controls,feedback);
    const entry={runId,key,question:q,form,input,feedback,submit,read:readButton,catalog,emptyChoices:select&&input.options.length<2,
      submission:null,sending:false,stale:false,acknowledged:false};
    readButton.addEventListener('click',()=>void read(entry));
    catalog.addEventListener('click',async()=>{
      if(q.field!=='account'||!current(entry)||!available||!fresh||entry.sending||entry.submission||entry.stale||entry.acknowledged)return;
      entry.sending=true;feedback.textContent='Verbundene Konten werden neu gelesen …';sync(entry);
      try{
        // The Core accepts this only for an existing account question and a
        // changed native catalogue. It neither answers another field nor
        // grants a new task. Other questions never offer this operation.
        await api(`/v1/agent/runs/${encodeURIComponent(runId)}/resume`,{method:'POST'});
        if(owned(entry))feedback.textContent='Kontostand gelesen. Der Auftrag wird neu angezeigt.';
      }catch(error){
        if(owned(entry))feedback.textContent=error.code==='not_waiting'?'Es wurde noch keine neue Kontoverbindung bestätigt.':'Der Kontostand ist nicht bestätigt. Bitte den aktuellen Auftragsstand erneut lesen.';
      }finally{
        entry.sending=false;if(current(entry)){sync(entry);await read(entry);}
      }
    });
    form.addEventListener('submit',async event=>{
      event.preventDefault();
      if(!current(entry)||!available||!fresh||entry.sending||entry.stale||entry.acknowledged)return;
      if(!entry.submission){
        const answer=input.value.trim();
        if(!answer||!input.reportValidity())return;
        if(answer.length>2000){feedback.textContent='Bitte begrenze diese Antwort auf 2.000 Zeichen.';return;}
        entry.submission=Object.freeze({question_id:q.id,expected_revision:q.revision,expected_digest:q.digest,
          answer,client_request_id:crypto.randomUUID()});
      }
      entry.sending=true;feedback.textContent='Antwort wird übermittelt …';sync(entry);
      try{
        const response=await api(`/v1/agent/runs/${encodeURIComponent(runId)}/action-answer`,{method:'POST',body:entry.submission});
        if(!owned(entry))return;
        if(!response?.action_intent||!['waiting_user','interpreting','resolved','failed'].includes(response.action_intent.status))throw {code:'invalid_response'};
        entry.acknowledged=true;feedback.textContent='Antwort angenommen. Der aktuelle Stand wird gelesen.';
      }catch(error){
        if(!owned(entry))return;
        if(error.code==='stale_question'){
          entry.stale=true;feedback.textContent='Diese Rückfrage hat sich inzwischen geändert. Der aktuelle Stand wird gelesen.';
        }else if(error.code==='invalid_answer'){
          entry.submission=null;
          feedback.textContent=typeof error.reason==='string'&&error.reason.trim()?error.reason:'Bitte prüfe deine Antwort. Diese Angabe konnte noch nicht übernommen werden.';
        }else if(error.status&&error.status<500){
          entry.stale=true;feedback.textContent='Die Antwort ist nicht bestätigt. Bitte den aktuellen Stand erneut lesen.';
        }else feedback.textContent='Übermittlung unbestätigt. Bei erneutem Versuch bleibt es dieselbe Antwort für diese Rückfrage. Du kannst auch zuerst den Stand lesen.';
      }finally{
        entry.sending=false;
        if(current(entry)){sync(entry);if(entry.acknowledged||entry.stale)await read(entry);}
      }
    });
    return entry;
  }
  function update(run){
    const intent=run.action_intent,q=intent?.question;
    fresh=true;
    if(intent?.status==='waiting_user'&&run.zustand_code==='WAITING_USER'&&validQuestion(q)){
      const key=JSON.stringify([run.id,q.id,q.revision,q.digest]);
      // Only the canonical question identity carries an in-flight answer. A new
      // question invalidates the old form even while its input owns focus.
      for(const [oldKey,entry] of entries)if(entry.runId===run.id&&oldKey!==key)entries.delete(oldKey);
      const entry=entries.get(key)||make(run.id,q,key);entries.set(key,entry);active=entry;
      if(root.firstChild!==entry.form)root.replaceChildren(entry.form);
      root.hidden=false;sync(entry);return root;
    }
    for(const [key,entry] of entries)if(entry.runId===run.id)entries.delete(key);
    active=null;root.replaceChildren();root.hidden=!intent;
    if(['SUCCEEDED','FAILED','CANCELLED','KILLED'].includes(run.zustand_code))root.hidden=true;
    else if(intent?.status==='interpreting')root.append(el('p','SOLVIO klärt die Angaben zu deinem Auftrag. Das ist noch kein bestätigter Abschluss.'));
    else if(intent?.status==='waiting_user')root.append(el('p','Die Rückfrage konnte noch nicht vollständig gelesen werden. Bitte den aktuellen Auftragsstand erneut laden.'));
    else root.hidden=true;
    return root;
  }
  return {update,unavailable,
    setAvailable(value){available=!!value;if(active)sync(active);},
    suspend(){fresh=false;if(active)sync(active);active=null;root.remove();},
    clear(){entries.clear();active=null;fresh=false;root.replaceChildren();root.hidden=true;},
    get uncertain(){return [...entries.values()].some(e=>e.submission&&!e.acknowledged&&!e.stale);}
  };
}
