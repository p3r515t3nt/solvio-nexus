import {AssistantRuntimeProvider, MessagePrimitive, useAuiState, useExternalStoreRuntime, type ThreadMessage} from '@assistant-ui/react';
import {StreamdownTextPrimitive} from '@assistant-ui/react-streamdown';
import {useEffect, useMemo, useState} from 'react';
import {createRoot} from 'react-dom/client';
import {createPortal} from 'react-dom';
import {ThreadMessageList} from '@/components/assistant-ui/thread/list';
import {SystemMessage} from '@/components/assistant-ui/thread/system-message';
import {ScaffoldRow} from '@/components/chat/scaffold-row';
import {ToolIcon} from '@/components/ui/tool-icon';
import {messageContentText} from '@/components/assistant-ui/thread/content';
import {I18nProvider} from '@/i18n';
import '../src/styles.css';
import './viewer.css';

type Observation = {event:string;status?:string;item_id?:string;native_thread_id:string;native_turn_id:string;invocation_id:string};
type Projection = {run:string;detail:{auftrag:string;ergebnis?:string};events:{id:number;at:number;summary:string;step_id:string;observation:Observation}[]};
const currentRun = () => new URL(location.href).searchParams.get('run') || '';
// No network, Gateway, onNew, onEdit, reload, cancel, speech or IPC adapter.
// The Core shell is the sole reader. Projection is ephemeral and cleared when
// the shell loses its authenticated binding; it never grants execution rights.
const components = {UserMessage: OwnerMessage, AssistantMessage: ResultMessage, SystemMessage: ObservationMessage};
const safeLink = ({href,children}:any) => /^https?:\/\//i.test(href || '')
  ? <a href={href} target="_blank" rel="noopener noreferrer">{children}</a> : <span>{children}</span>;
const markdownComponents = {a:safeLink,img:({alt}:any)=><span>{alt || 'Bild in Ergebnisdateien'}</span>};
const resultParts={Text:()=> <StreamdownTextPrimitive components={markdownComponents}/>};

function OwnerMessage(){
  const text=useAuiState(s=>messageContentText(s.message.content));
  return <MessagePrimitive.Root className="solvio-owner-message" data-role="user" data-slot="aui_user-message-root">
    <p className="solvio-message-label">Dein Auftrag</p><p>{text}</p>
  </MessagePrimitive.Root>;
}
function ResultMessage(){
  return <MessagePrimitive.Root className="solvio-result-message" data-role="assistant" data-slot="aui_assistant-message-root">
    <p className="solvio-message-label">SOLVIO-Ergebnis</p>
    <MessagePrimitive.Parts components={resultParts}/>
  </MessagePrimitive.Root>;
}
function ObservationMessage(){
  const observation=useAuiState(s=>s.message.metadata.custom.observation) as Observation | undefined;
  const step=useAuiState(s=>s.message.metadata.custom.step) as string;
  const text=useAuiState(s=>messageContentText(s.message.content));
  const [open,setOpen]=useState(false);
  if(!observation)return <SystemMessage/>;
  return <MessagePrimitive.Root className="solvio-tool-message" data-role="tool" data-slot="solvio_native-tool">
    <ScaffoldRow open={open} onToggle={()=>setOpen(!open)}>
      <ToolIcon name={observation.event==='web_search'?'web_search':'delegate_task'} />
      <span className="solvio-tool-label">{text}</span>
    </ScaffoldRow>
    {open&&<dl className="solvio-evidence">
      <dt>Core-Schritt</dt><dd>{step}</dd><dt>Aufruf</dt><dd>{observation.invocation_id}</dd>
      <dt>Nativer Thread</dt><dd>{observation.native_thread_id}</dd><dt>Nativer Turn</dt><dd>{observation.native_turn_id}</dd>
      {observation.item_id&&<><dt>Werkzeugereignis</dt><dd>{observation.item_id}</dd></>}
      <dt>Beobachtung</dt><dd>{observation.status||observation.event}</dd>
    </dl>}
  </MessagePrimitive.Root>;
}
function messagesFor(projection:Projection,evidence=false):ThreadMessage[]{
  const first=projection.events[0]?.at;
  const common={createdAt:new Date(Number.isFinite(first)?first*1000:0),metadata:{custom:{}}};
  const messages:any[]=evidence?[]:[{...common,id:'core-owner-'+projection.run,role:'user',content:[{type:'text',text:projection.detail.auftrag}],attachments:[]}];
  for(const event of evidence?projection.events:[]){
    messages.push({id:'core-event-'+projection.run+'-'+event.id,role:'system',createdAt:new Date(event.at*1000),
      content:[{type:'text',text:event.summary}],metadata:{custom:{observation:event.observation,step:event.step_id}}});
  }
  if(!evidence&&projection.detail.ergebnis)messages.push({...common,id:'core-result-'+projection.run,role:'assistant',
    content:[{type:'text',text:projection.detail.ergebnis}],status:{type:'complete',reason:'stop'},
    metadata:{unstable_state:null,unstable_annotations:[],unstable_data:[],steps:[],custom:{}}});
  return messages;
}
function ReadThread({projection,evidence=false}:{projection:Projection|null;evidence?:boolean}){
  const messages=useMemo(()=>projection?messagesFor(projection,evidence):[],[projection,evidence]);
  const runtime=useExternalStoreRuntime({messages,isRunning:false,isDisabled:true,isSendDisabled:true});
  return <AssistantRuntimeProvider runtime={runtime}><ThreadMessageList sessionKey={projection?.run || ''} components={components}/></AssistantRuntimeProvider>;
}
function Viewer(){
  const [projection,setProjection]=useState<Projection|null>(null);
  useEffect(()=>{
    const update=(event:Event)=>{const next=(event as CustomEvent).detail;
      const selected=currentRun();
      setProjection(/^ar-[a-f0-9]{16}$/.test(selected)&&next?.run===selected&&next.detail&&Array.isArray(next.events)?next:null);};
    window.addEventListener('solvio-hermes-projection',update);
    window.dispatchEvent(new Event('solvio-hermes-view-ready'));
    return()=>window.removeEventListener('solvio-hermes-projection',update);
  },[]);
  const evidenceRoot=document.getElementById('solvio-hermes-evidence');
  return <><main className="solvio-desktop-view" data-renderer="hermes-desktop-thread" aria-label="Auftrag und SOLVIO-Ergebnis">
    <div className="solvio-desktop-thread" data-chat-surface="">
      <ReadThread key={projection?.run || 'empty'} projection={projection}/>
    </div>
  </main>{projection&&evidenceRoot&&createPortal(
    <div className="solvio-evidence-thread" aria-label="Belegte Hermes-Werkzeugschritte">
      <ReadThread key={'evidence-'+projection.run} projection={projection} evidence/>
    </div>,evidenceRoot)}</>;
}
createRoot(document.getElementById('root')!).render(<I18nProvider configClient={null} initialLocale="en"><Viewer/></I18nProvider>);
