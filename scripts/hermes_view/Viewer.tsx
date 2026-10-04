import {useEffect, useState} from 'react';
import {createRoot} from 'react-dom/client';
import {MessageList} from './pages/SessionsPage';
import {I18nProvider, useI18n} from './i18n';
import type {SessionMessage} from './lib/api';
import './solvio-viewer.css';

type Event = {id:number; at:number; kind:string; summary:string;
  observation?:{event:string; native_thread_id:string; native_turn_id:string}};
type Detail = {auftrag:string; zustand:string; ergebnis?:string; zustand_code:string};
const run = new URL(location.href).searchParams.get('run') || '';
const path = '/v1/agent/runs/'+encodeURIComponent(run);
async function read(url:string, signal:AbortSignal) {
  const response = await fetch(url,{credentials:'same-origin',cache:'no-store',signal});
  if(!response.ok)throw new Error(response.status===401?'signed_out':'unavailable');
  return response.json();
}

function Viewer(){
  const {setLocale}=useI18n();
  const [detail,setDetail]=useState<Detail|null>(null);
  const [events,setEvents]=useState<Event[]>([]);
  const [notice,setNotice]=useState('Verbindung wird geprüft.');
  const [stamp,setStamp]=useState('');
  useEffect(()=>setLocale('de'),[setLocale]);
  useEffect(()=>{
    let stopped=false, busy=false, cursor=0, known:Event[]=[];
    let controller:AbortController|null=null;
    const clear=()=>{known=[];cursor=0;setEvents([]);setDetail(null);setStamp('');};
    async function refresh(){
      if(stopped||busy||document.hidden)return;
      busy=true;controller=new AbortController();
      const timeout=setTimeout(()=>controller?.abort(),10000);
      try{
        const current=await read(path,controller.signal) as Detail;
        let more=true, incomplete=false;
        for(let page=0;more&&page<21;page++){
          const data=await read(path+'/events?after='+cursor+'&limit=100',controller.signal);
          if(stopped)return;
          const rows=data.events as Event[];
          incomplete ||= data.history_may_be_incomplete;
          known.push(...rows);known=known.slice(-2000);
          cursor=data.next_cursor;more=data.has_more;
        }
        if(stopped)return;
        setDetail(current);setEvents([...known]);setStamp(new Date().toLocaleTimeString('de-DE'));
        setNotice(incomplete?'Älterer Verlauf möglicherweise gekürzt.':
          'Ausführung im Core · Diese Ansicht startet keinen Agenten.');
      }catch(error){
        if(stopped)return;
        if(error instanceof Error&&error.message==='signed_out'){
          clear();setNotice('Anmeldung beendet. Bitte im SOLVIO-Dashboard erneut anmelden.');
        }else setNotice('Verbindung unterbrochen. Die letzte Ansicht ist veraltet.');
      }finally{clearTimeout(timeout);busy=false;}
    }
    const hide=()=>{if(document.hidden)controller?.abort();else void refresh();};
    const end=()=>{stopped=true;controller?.abort();clear();};
    document.addEventListener('visibilitychange',hide);
    window.addEventListener('pagehide',end);
    const timer=setInterval(()=>void refresh(),2500);void refresh();
    return()=>{stopped=true;controller?.abort();clearInterval(timer);
      document.removeEventListener('visibilitychange',hide);window.removeEventListener('pagehide',end);};
  },[]);
  const messages:SessionMessage[]=events.filter(e=>e.kind==='native_progress').map(e=>({
    role:e.observation?.event==='web_search'?'tool':'system',
    tool_name:e.observation?.event==='web_search'?'Native Websuche':undefined,
    content:e.summary,timestamp:e.at}));
  const binding=events.find(e=>e.observation)?.observation;
  return <main className="solvio-native-viewer">
    <header><span className="native-brand">HERMES</span><span>Nachrichtenansicht · Nur lesen</span></header>
    <p className="native-origin">Externe SOLVIO-Ausführung · Hermes’ nativer Codex-Transport</p>
    <p role="status">{notice}{stamp?' · Gelesen '+stamp:''}</p>
    {detail&&<><h1>{detail.auftrag}</h1><p className="native-state">{detail.zustand}</p></>}
    {messages.length?<MessageList messages={messages}/>:<p>Noch keine belegten nativen Werkzeugereignisse für diesen Auftrag.</p>}
    {detail?.ergebnis&&<section className="native-result"><h2>Stand aus dem Core</h2><p>{detail.ergebnis}</p></section>}
    {binding&&<details><summary>Zuordnung zum ausgeführten Lauf</summary><p>Nativer Thread: {binding.native_thread_id}</p><p>Nativer Turn: {binding.native_turn_id}</p></details>}
  </main>;
}

createRoot(document.getElementById('root')!).render(<I18nProvider><Viewer/></I18nProvider>);
