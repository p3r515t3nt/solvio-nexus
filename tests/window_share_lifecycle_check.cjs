/* Actual Core JS, small in-memory DOM/media/signaling ports. No browser/network. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const assets=path.resolve(process.env.SOLVIO_WINDOW_TEST_ASSETS||path.join(__dirname,'../src/solvio/dashboard/assets'));
const sourceHashes=Object.fromEntries(['window-share.js','hermes-workspace.js'].map(name=>[name,require('node:crypto').createHash('sha256').update(fs.readFileSync(path.join(assets,name))).digest('hex')]));
const flush=async()=>{for(let i=0;i<40;i++)await Promise.resolve();};
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};

class Events {
  constructor(){this.events=new Map();}
  addEventListener(name,fn){if(!this.events.has(name))this.events.set(name,new Set());this.events.get(name).add(fn);}
  removeEventListener(name,fn){this.events.get(name)?.delete(fn);}
  dispatchEvent(event){for(const fn of [...(this.events.get(event.type)||[])])fn(event);}
}
class Element extends Events {
  constructor(tag,doc){super();this.tag=tag;this.ownerDocument=doc;this.children=[];this.attributes=new Map();this.textContent='';this.hidden=false;this.disabled=false;this.open=false;this.value='';this.srcObject=null;this.videoWidth=0;this.readyState=0;this.totalVideoFrames=0;this.droppedVideoFrames=0;this.frameCallbacks=new Map();this.nextFrame=0;}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this.children=[...nodes];}
  setAttribute(k,v){this.attributes.set(k,v);}
  hasAttribute(k){return this.attributes.has(k);}
  querySelectorAll(selector){const match=n=>selector.startsWith('[')?n.hasAttribute(selector.slice(1,-1)):n.tag===selector;
    return this.children.flatMap(n=>[...(match(n)?[n]:[]),...n.querySelectorAll(selector)]);}
  querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
  get options(){return this.children.filter(n=>n.tag==='option');}
  pause(){this.paused=true;}
  async play(){this.paused=false;}
  requestVideoFrameCallback(fn){const id=++this.nextFrame;this.frameCallbacks.set(id,fn);return id;}
  cancelVideoFrameCallback(id){this.frameCallbacks.delete(id);}
  getVideoPlaybackQuality(){return {totalVideoFrames:this.totalVideoFrames,droppedVideoFrames:this.droppedVideoFrames};}
  presentFrame(){this.videoWidth=640;this.readyState=2;this.totalVideoFrames++;const callbacks=[...this.frameCallbacks.values()];this.frameCallbacks.clear();for(const callback of callbacks)callback(0,{presentedFrames:this.totalVideoFrames});}
}
function world(){
  const document=new Events();document.hidden=false;document.createElement=tag=>new Element(tag,document);document.getElementById=()=>null;
  const window=new Events(),timers=new Map(),timerRequests=[],sockets=[],pcs=[],captures=[],projections=[];
  window.addEventListener('solvio-hermes-projection',event=>projections.push(event.detail));
  let nextTimer=0,now=0,offerGate=null,remoteGate=null,answerGate=null,localGate=null,gathering='complete';
  const setTimer=(fn,ms,kind)=>{assert(Number.isFinite(ms)&&ms>0);const id=++nextTimer;timers.set(id,{fn,ms,kind,due:now+ms});timerRequests.push({id,ms,kind,createdAt:now,due:now+ms});return id;};
  async function advance(value){assert(value>=now,'clock never goes backward');let ticks=0;while(true){const next=[...timers.entries()].filter(([,t])=>t.due<=value).sort((a,b)=>a[1].due-b[1].due||a[0]-b[0])[0];if(!next)break;assert(++ticks<10000,'bounded deterministic clock');const [id,timer]=next;now=timer.due;if(timer.kind==='timeout')timers.delete(id);else timer.due+=timer.ms;timer.fn();await flush();}now=value;await flush();}

  class Stream {constructor(tracks){this.tracks=tracks;}getTracks(){return this.tracks;}getVideoTracks(){return this.tracks.filter(t=>t.kind==='video');}getAudioTracks(){return this.tracks.filter(t=>t.kind==='audio');}}
  function track(){return {kind:'video',readyState:'live',stop(){this.readyState='ended';},getSettings:()=>({displaySurface:'window'})};}
  class Socket {
    static OPEN=1;
    constructor(url){this.url=String(url);this.readyState=0;this.sent=[];this.closes=0;sockets.push(this);}
    open(){this.readyState=1;this.onopen?.();}
    send(data){assert.equal(this.readyState,1);this.sent.push(JSON.parse(data));}
    close(){this.closes++;this.readyState=3;this.onclose?.({code:1000,reason:''});}
    receive(data){this.onmessage?.({data:JSON.stringify(data)});}
    remoteClose(code,reason){this.readyState=3;this.onclose?.({code,reason});}
  }
  class Peer extends Events {
    constructor(options){super();assert.deepEqual(JSON.parse(JSON.stringify(options)),{iceServers:[],bundlePolicy:'max-bundle'});this.connectionState='new';this.iceConnectionState='new';this.signalingState='stable';this.iceGatheringState=gathering;this.closed=0;this.calls=[];pcs.push(this);}
    addTransceiver(track,options){assert.equal(track.kind,'video');assert.equal(options.direction,'sendonly');}
    async createOffer(){this.calls.push('createOffer');if(offerGate)await offerGate.promise;return {type:'offer',sdp:'synthetic-video-offer'};}
    async createAnswer(){this.calls.push('createAnswer');if(answerGate)await answerGate.promise;return {type:'answer',sdp:'synthetic-video-answer'};}
    async setLocalDescription(value){this.calls.push('setLocalDescription');if(localGate)await localGate.promise;this.localDescription=value;}
    async setRemoteDescription(value){this.calls.push('setRemoteDescription');if(remoteGate)await remoteGate.promise;this.remoteDescription=value;}
    close(){this.closed++;this.connectionState='closed';}
    connect(){this.connectionState='connected';this.iceConnectionState='connected';this.onconnectionstatechange?.();}
    fail(){this.connectionState='failed';this.onconnectionstatechange?.();}
    deliverTrack(){const value=track();this.ontrack?.({track:value});return value;}
  }
  const context=vm.createContext({window,document,URL,AbortController,console,Map,Set,Promise,
    CustomEvent:class {constructor(type,options){this.type=type;this.detail=options?.detail;}},
    location:{href:'https://127.0.0.1/dashboard/hermes/solvio-view.html?run=ar-0123456789abcdef'},
    history:{replaceState(){}},performance:{now:()=>now},WebSocket:Socket,RTCPeerConnection:Peer,MediaStream:Stream,
    navigator:{mediaDevices:{async getDisplayMedia(options){assert.equal(options.audio,false);const stream=new Stream([track()]);captures.push(stream);return stream;}}},
    setTimeout:(fn,ms)=>setTimer(fn,ms,'timeout'),setInterval:(fn,ms)=>setTimer(fn,ms,'interval'),
    clearTimeout:id=>timers.delete(id),clearInterval:id=>timers.delete(id),
    fetch:()=>{throw Error('network is forbidden in this test');}});
  window.RTCPeerConnection=Peer;
  const source=fs.readFileSync(path.join(assets,'window-share.js'),'utf8');
  assert.equal((source.match(/export class WindowShare/g)||[]).length,1);
  vm.runInContext(source.replace('export class WindowShare','class WindowShare')+'\nglobalThis.WindowShare=WindowShare;',context,{filename:'window-share.js'});
  const workspace=fs.readFileSync(path.join(assets,'hermes-workspace.js'),'utf8');
  const importLine="import {WindowShare} from '/dashboard/assets/window-share.js';";
  assert.equal(workspace.split(importLine).length,2);
  vm.runInContext(workspace.replace(importLine,'').replaceAll('export function ','function ')+
    '\nglobalThis.mountWorkspace=mountWorkspace;',context,{filename:'hermes-workspace.js'});
  const element=tag=>document.createElement(tag);
  function shareRoot(){const root=element('section');
    for(const attribute of ['watch','publish','stop','status']){const n=element(attribute==='status'?'p':'button');n.setAttribute('data-share-'+attribute,'');root.append(n);}
    const channel=element('select');channel.value='hermes';root.append(channel,element('video'));return root;}
  function share(){return new context.WindowShare(shareRoot(),()=>({csrf_token:'a'.repeat(64)}));}
  const controls={failure:null,session:'owner-session',hold:null,purpose:'owner',events:[],reads:[],
    detail:{auftrag:'Synthetic task',zustand:'Fertig',schritte:[],ergebnis:'Bound Core result'}};
  async function mount(){const root=element('section'),native=element('div');
    const read=async(url,signal)=>{
      controls.reads.push(url);
      if(controls.hold&&url==='/v1/browser/session'){
        const gate=controls.hold;signal.addEventListener('abort',()=>gate.reject(Error('aborted')),{once:true});await gate.promise;
      }
      if(controls.failure)throw Object.assign(Error('synthetic read failure'),{status:controls.failure});
      if(url==='/v1/browser/session')return {session_id:controls.session,csrf_token:'a'.repeat(64),purpose:controls.purpose};
      if(url==='/v1/agent/runs')return {laeufe:[{id:'ar-0123456789abcdef',auftrag:'Synthetic task'}]};
      if(url.includes('/events?'))return {events:controls.events,next_cursor:controls.events.length,has_more:false};
      assert.match(url,/^\/v1\/agent\/runs\/ar-[a-f0-9]{16}$/);
      return {id:url.split('/').at(-1),...controls.detail};
    };
    const mounted=context.mountWorkspace(root,native,{read});await flush();return {root,native,...mounted};
  }
  return {window,document,context,timers,timerRequests,sockets,pcs,captures,projections,controls,share,mount,advance,
    get now(){return now;},setOfferGate:g=>{offerGate=g;},setRemoteGate:g=>{remoteGate=g;},setAnswerGate:g=>{answerGate=g;},setLocalGate:g=>{localGate=g;},setGathering:value=>{gathering=value;},
    hidden(){document.hidden=true;document.dispatchEvent({type:'visibilitychange'});},
    visible(){document.hidden=false;document.dispatchEvent({type:'visibilitychange'});},
    pagehide(){window.dispatchEvent({type:'pagehide'});}};
}
async function ready(w,share,role='viewer'){
  await share.start(role);const socket=w.sockets.at(-1);socket.open();socket.receive({type:'ready'});await flush();return socket;
}
function inactive(w,share){assert.equal(share.role,null);assert.equal(share.socket,null);assert.equal(share.peers.size,0);assert.equal(share.video.srcObject,null);assert.equal(share.video.hidden,true);assert.equal(share.stream,null);assert(w.sockets.every(s=>s.readyState===3));assert(w.captures.every(s=>s.getTracks().every(t=>t.readyState==='ended')));}


// Advance scheduled timers chronologically, while keeping only the Core lease alive.
// No test invokes a timeout callback directly or assumes a private timer field.
async function withLeases(w,socket,until){
  while(w.now+4000<until){await w.advance(w.now+4000);socket.receive({type:'lease'});await flush();}
  await w.advance(until);
}
function noShareResources(w,share){inactive(w,share);assert.equal(w.timers.size,0);assert.equal(share.video.frameCallbacks.size,0);
  assert(w.pcs.every(pc=>pc.closed>0));assert(w.pcs.every(pc=>[...pc.events.values()].every(set=>set.size===0)));}
function timeoutRequests(w){return w.timerRequests.filter(t=>t.kind==='timeout'&&t.ms===20000);}

const cases={
  async first_failure_survives_repeated_cleanup(){
    const w=world(),share=w.share(),socket=await ready(w,share);
    socket.onerror();const reason=share.status.textContent;assert.match(reason,/nicht erreichbar/);
    share.stop();share.stop('Ein späterer Aufräumgrund.');w.pagehide();
    assert.equal(share.status.textContent,reason);inactive(w,share);
  },
  async manual_end_and_explicit_restart_are_distinct(){
    const w=world(),share=w.share();await ready(w,share);
    share.root.querySelector('[data-share-stop]').onclick();assert.match(share.status.textContent,/manuell/);
    const reason=share.status.textContent;share.stop();assert.equal(share.status.textContent,reason);
    await ready(w,share);assert.match(share.status.textContent,/Warte auf/);assert.notEqual(share.status.textContent,reason);share.stop();
  },
  async hidden_viewer_ends_and_keeps_reason_through_aborted_read(){
    const w=world(),mounted=await w.mount(),share=mounted.share;await ready(w,share);
    const gate=deferred();w.controls.hold=gate;const reading=mounted.refresh();await flush();
    w.hidden();await reading;const reason=share.status.textContent;
    assert.match(reason,/nicht mehr sichtbar/);inactive(w,share);
    w.controls.hold=null;w.visible();await flush();assert.equal(w.sockets.length,1);
    w.pagehide();assert.equal(share.status.textContent,reason);
  },
  async hidden_publisher_closes_capture_and_late_offer_cannot_revive(){
    const w=world(),mounted=await w.mount(),share=mounted.share,socket=await ready(w,share,'publisher');
    const gate=deferred();w.setOfferGate(gate);socket.receive({type:'viewer_joined',peer:'v1'});await flush();
    w.hidden();const reason=share.status.textContent;assert.match(reason,/nicht mehr sichtbar/);inactive(w,share);
    gate.resolve();await flush();assert.equal(socket.sent.filter(x=>x.type==='offer').length,0);
    assert.equal(share.status.textContent,reason);mounted.stop();
  },
  async local_capture_end_and_remote_sender_end_are_explicit(){
    const w=world(),share=w.share();await ready(w,share,'publisher');w.captures[0].getVideoTracks()[0].onended();
    assert.match(share.status.textContent,/ausgewählte Fenster/);assert.match(share.status.textContent,/nicht mehr freigegeben/);inactive(w,share);
    const other=world(),view=other.share(),socket=await ready(other,view);socket.remoteClose(4000,'sender_ended');
    assert.match(view.status.textContent,/Sender.*beendet/);inactive(other,view);
  },
  async publisher_reports_new_handshake_before_offer_finishes(){
    const w=world(),share=w.share(),socket=await ready(w,share,'publisher');
    socket.receive({type:'viewer_left',peer:'old'});await flush();assert.match(share.status.textContent,/kein Empfänger/);
    const gate=deferred();w.setOfferGate(gate);socket.receive({type:'viewer_joined',peer:'new'});await flush();
    assert.match(share.status.textContent,/Empfänger.*Verbindung wird aufgebaut/);assert.doesNotMatch(share.status.textContent,/kein Empfänger|empfängt/);
    gate.resolve();await flush();assert.equal(socket.sent.filter(x=>x.type==='offer').length,1);
    w.pcs[0].connect();assert.match(share.status.textContent,/ist verbunden/);share.stop();
  },
  async viewer_reports_received_offer_before_peer_setup_finishes(){
    const w=world(),share=w.share(),socket=await ready(w,share),gate=deferred();w.setRemoteGate(gate);
    socket.receive({type:'offer',peer:'publisher',sdp:'synthetic-video'});await flush();
    assert.match(share.status.textContent,/Sender gefunden.*Bildverbindung wird aufgebaut/);assert.doesNotMatch(share.status.textContent,/Warte auf/);
    gate.resolve();await flush();assert.equal(socket.sent.filter(x=>x.type==='answer').length,1);share.stop();
  },
  async stopped_old_generation_never_overwrites_explicit_restart(){
    const w=world(),share=w.share(),old=await ready(w,share),gate=deferred();w.setRemoteGate(gate);
    old.receive({type:'offer',peer:'publisher',sdp:'synthetic-video'});await flush();
    share.stop('Alte Verbindung fehlgeschlagen.');await ready(w,share);const fresh=share.status.textContent;
    gate.reject(Error('late failure'));await flush();assert.equal(share.status.textContent,fresh);assert.equal(share.role,'viewer');
    assert.equal(old.sent.filter(x=>x.type==='answer').length,0);share.stop();
  },
  async status_read_failure_has_own_reason_and_no_automatic_restart(){
    const w=world(),mounted=await w.mount(),share=mounted.share;await ready(w,share);
    w.controls.failure=503;await mounted.refresh();const reason=share.status.textContent;
    assert.match(reason,/Arbeitsstand.*nicht mehr gelesen/);assert.doesNotMatch(reason,/Anmeldung.*abgelaufen/);inactive(w,share);
    await mounted.refresh();w.hidden();w.controls.failure=null;w.visible();await flush();
    assert.equal(share.status.textContent,reason);assert.equal(w.sockets.length,1);mounted.stop();
  },
  async revoked_session_and_changed_session_keep_distinct_reasons(){
    for(const failure of [401,403,404]){const w=world(),mounted=await w.mount();await ready(w,mounted.share);
      w.controls.failure=failure;await mounted.refresh();assert.match(mounted.share.status.textContent,/Auftrag oder Anmeldung nicht mehr verfügbar/);inactive(w,mounted.share);mounted.stop();}
    const w=world(),mounted=await w.mount();await ready(w,mounted.share);w.controls.session='new-owner-session';await mounted.refresh();
    assert.match(mounted.share.status.textContent,/Anmeldung geändert/);const reason=mounted.share.status.textContent;w.hidden();assert.equal(mounted.share.status.textContent,reason);inactive(w,mounted.share);mounted.stop();
  },
  async second_viewer_handshake_and_departure_preserve_confirmed_first(){
    const w=world(),share=w.share(),socket=await ready(w,share,'publisher');
    socket.receive({type:'viewer_joined',peer:'first'});await flush();w.pcs[0].connect();
    socket.receive({type:'viewer_joined',peer:'second'});await flush();
    assert.match(share.status.textContent,/ist verbunden.*weiter/);
    socket.receive({type:'viewer_left',peer:'second'});await flush();assert.match(share.status.textContent,/ist verbunden/);
    assert.doesNotMatch(share.status.textContent,/aufgebaut|kein Empfänger/);assert.equal(w.pcs[0].closed,0);assert.equal(share.role,'publisher');assert.equal(share.peers.size,1);share.stop();
  },
  async lease_and_unknown_close_never_invent_a_source_end(){
    const w=world(),share=w.share();await ready(w,share);await w.advance(7000);assert.match(share.status.textContent,/nicht mehr bestätigt/);
    const reason=share.status.textContent;share.stop();assert.equal(share.status.textContent,reason);inactive(w,share);
    const other=world(),view=other.share(),socket=await ready(other,view);socket.remoteClose(1006,'');
    assert.match(view.status.textContent,/Fensterverbindung beendet/);assert.doesNotMatch(view.status.textContent,/Sender hat|Fenster.*geschlossen/);inactive(other,view);
  },
  async collapse_pagehide_and_channel_change_still_close_resources(){
    const w=world(),mounted=await w.mount(),share=mounted.share;await ready(w,share,'publisher');
    const desktop=mounted.root.children.at(-1);desktop.open=false;desktop.dispatchEvent({type:'toggle'});
    assert.match(share.status.textContent,/Einklappen/);const reason=share.status.textContent;inactive(w,share);
    w.pagehide();assert.equal(share.status.textContent,reason);
    const page=world(),view=page.share();await ready(page,view);page.pagehide();assert.match(view.status.textContent,/Seite verlassen/);inactive(page,view);
    const channel=world(),sender=channel.share();await ready(channel,sender,'publisher');sender.channel.onchange();
    assert.match(sender.status.textContent,/Fensterbereich gewechselt/);inactive(channel,sender);
  },
  async session_close_and_playback_failure_survive_followup_cleanup(){
    const w=world(),share=w.share(),socket=await ready(w,share);socket.remoteClose(4401,'session_ended');
    const auth=share.status.textContent;assert.match(auth,/Anmeldung nicht mehr bestätigt/);share.stop();assert.equal(share.status.textContent,auth);inactive(w,share);
    const other=world(),mounted=await other.mount(),view=mounted.share,remote=await ready(other,view);
    remote.receive({type:'offer',peer:'publisher',sdp:'synthetic-video'});await flush();
    view.video.play=()=>Promise.reject(Error('synthetic play rejection'));
    other.pcs[0].ontrack({track:{kind:'video'}});await flush();const playback=view.status.textContent;
    assert.match(playback,/nicht abgespielt/);other.controls.failure=503;await mounted.refresh();other.hidden();
    assert.equal(view.status.textContent,playback);inactive(other,view);mounted.stop();
  },
  async conversation_precedes_one_collapsed_details_region_and_optional_window(){
    const w=world(),mounted=await w.mount(),children=mounted.root.children;
    assert.equal(children[1].tag,'header');assert.equal(children[2],mounted.native);
    assert.equal(children[3].tag,'details');assert.equal(children[3].hasAttribute('data-workspace-details'),true);
    assert.equal(children[3].open,false);assert.equal(children[4].tag,'details');assert.equal(children[4].open,false);
    assert.equal(mounted.root.querySelectorAll('h1').length,1);
    assert.equal(mounted.native.hidden,false);assert.equal(w.sockets.length,0);assert.equal(w.captures.length,0);
    assert.equal(w.projections.at(-1).detail.ergebnis,'Bound Core result');mounted.stop();
  },
  async generic_core_result_is_projected_without_invented_hermes_calls(){
    const w=world(),mounted=await w.mount(),projection=w.projections.at(-1);
    assert.equal(projection.run,'ar-0123456789abcdef');assert.equal(projection.events.length,0);
    assert.equal(projection.detail.auftrag,'Synthetic task');assert.equal(projection.detail.ergebnis,'Bound Core result');
    const evidence=mounted.root.children[3].children.find(n=>n.id==='solvio-hermes-evidence');
    assert.equal(evidence.hidden,true);assert.equal(mounted.native.hidden,false);
    assert(w.controls.reads.every(url=>url==='/v1/browser/session'||url.startsWith('/v1/agent/runs/')));mounted.stop();
  },
  async native_evidence_remains_bound_inside_details_and_clears_before_notification(){
    const w=world();w.controls.events=[{id:1,at:1,kind:'native_progress',step_id:'as-step',summary:'Observed search',
      observation:{runtime:'hermes-codex-app-server',event:'started',seq:1,invocation_id:'inv-1',native_thread_id:'thread-1',native_turn_id:'turn-1'}}];
    const mounted=await w.mount(),details=mounted.root.children[3],evidence=details.children.find(n=>n.id==='solvio-hermes-evidence');
    assert.equal(details.open,false);assert.equal(evidence.hidden,false);assert.equal(w.projections.at(-1).events.length,1);
    let clearSeen=false;w.window.addEventListener('solvio-hermes-projection',event=>{
      if(event.detail===null){clearSeen=true;assert.equal(evidence.hidden,true);assert.equal(mounted.native.hidden,true);}
    });
    w.controls.failure=401;await mounted.refresh();assert.equal(clearSeen,true);assert.equal(w.projections.at(-1),null);
    assert.equal(w.sockets.length,0);mounted.stop();
  },
  async observer_keeps_task_selection_but_has_no_window_authority(){
    const w=world();w.context.location.href='https://127.0.0.1/dashboard/hermes/solvio-view.html';w.controls.purpose='hermes_observer_v1';
    const mounted=await w.mount(),choose=mounted.root.children[0],picker=choose.querySelector('select');
    assert.equal(choose.hidden,false);assert.equal(picker.value,'');assert.equal(picker.options.length,2);
    assert.equal(mounted.native.hidden,true);assert.equal(mounted.root.children.at(-1).hidden,true);
    picker.value='ar-0123456789abcdef';picker.dispatchEvent({type:'change'});await flush();
    assert.equal(mounted.native.hidden,false);assert.equal(w.projections.at(-1).detail.ergebnis,'Bound Core result');
    assert.equal(mounted.root.children.at(-1).hidden,true);
    assert(mounted.share.root.querySelectorAll('button').every(button=>button.disabled));
    assert.equal(w.sockets.length,0);assert.equal(w.captures.length,0);mounted.stop();
  },
  async failed_read_hides_both_render_targets_without_discarding_end_reason(){
    const w=world(),mounted=await w.mount();await ready(w,mounted.share);
    const details=mounted.root.children[3],evidence=details.children.find(n=>n.id==='solvio-hermes-evidence');
    w.controls.failure=503;await mounted.refresh();assert.equal(mounted.native.hidden,true);assert.equal(evidence.hidden,true);
    assert.equal(w.projections.at(-1),null);assert.equal(mounted.root.children[1].querySelector('[role]').hidden,false);
    const reason=mounted.share.status.textContent;w.hidden();assert.equal(mounted.share.status.textContent,reason);mounted.stop();
  },

  async first_frame_deadline_uses_actual_20s_from_offer_and_ignores_leases(){
    const w=world(),share=w.share(),socket=await ready(w,share),gate=deferred();
    await w.advance(1370);w.setRemoteGate(gate);socket.receive({type:'offer',peer:'publisher',sdp:'synthetic-video'});await flush();
    assert.deepEqual(timeoutRequests(w).map(({ms,createdAt,due})=>({ms,createdAt,due})),[{ms:20000,createdAt:1370,due:21370}]);
    await withLeases(w,socket,21369);assert.equal(share.role,'viewer');assert.equal(w.pcs[0].closed,0);
    await w.advance(21370);assert.match(share.status.textContent,/20 Sekunden/);noShareResources(w,share);
    const reason=share.status.textContent;gate.resolve();await flush();assert.equal(socket.sent.filter(x=>x.type==='answer').length,0);
    assert.deepEqual(w.pcs[0].calls,['setRemoteDescription']);assert.equal(share.status.textContent,reason);
  },
  async connected_track_and_successful_play_are_not_a_presented_frame(){
    const w=world(),share=w.share(),socket=await ready(w,share);
    socket.receive({type:'offer',peer:'publisher',sdp:'synthetic-video'});await flush();
    w.pcs[0].connect();w.pcs[0].deliverTrack();await flush();
    assert.equal(share.video.paused,false);assert.equal(share.video.hidden,false);
    assert.doesNotMatch(share.status.textContent,/Live-Fenster/);assert.match(share.status.textContent,/erste Fensterbild/);
    assert.equal(share.video.frameCallbacks.size,1);await withLeases(w,socket,19999);assert.equal(share.role,'viewer');
    await w.advance(20000);assert.match(share.status.textContent,/20 Sekunden/);noShareResources(w,share);
  },
  async presented_frame_confirms_live_and_cancels_only_peer_deadline(){
    const w=world(),share=w.share(),socket=await ready(w,share);
    socket.receive({type:'offer',peer:'publisher',sdp:'synthetic-video'});await flush();w.pcs[0].connect();w.pcs[0].deliverTrack();await flush();
    assert.doesNotMatch(share.status.textContent,/Live-Fenster/);share.video.presentFrame();assert.match(share.status.textContent,/Live-Fenster/);
    assert.equal(share.video.frameCallbacks.size,0);assert.equal(timeoutRequests(w).length,1);
    await withLeases(w,socket,25000);assert.equal(share.role,'viewer');assert.equal(w.pcs[0].closed,0);
    assert.match(share.status.textContent,/Live-Fenster/);share.stop();noShareResources(w,share);
  },
  async fallback_requires_new_nondropped_frame_dimensions_and_ready_video(){
    const w=world(),share=w.share();share.video.requestVideoFrameCallback=undefined;
    share.video.totalVideoFrames=7;const socket=await ready(w,share);
    socket.receive({type:'offer',peer:'publisher',sdp:'synthetic-video'});await flush();w.pcs[0].connect();w.pcs[0].deliverTrack();await flush();
    await w.advance(100);assert.doesNotMatch(share.status.textContent,/Live-Fenster/);
    share.video.totalVideoFrames=8;await w.advance(200);assert.doesNotMatch(share.status.textContent,/Live-Fenster/);
    share.video.videoWidth=640;share.video.readyState=1;await w.advance(300);assert.doesNotMatch(share.status.textContent,/Live-Fenster/);
    share.video.readyState=2;share.video.droppedVideoFrames=1;await w.advance(400);assert.doesNotMatch(share.status.textContent,/Live-Fenster/);
    share.video.totalVideoFrames=9;await w.advance(500);assert.match(share.status.textContent,/Live-Fenster/);
    assert.equal([...w.timers.values()].filter(t=>t.kind==='interval'&&t.ms===100).length,0);
    await withLeases(w,socket,25000);assert.equal(share.role,'viewer');share.stop();noShareResources(w,share);
  },
  async fallback_without_frame_times_out_and_cleans_its_poll(){
    const w=world(),share=w.share();share.video.requestVideoFrameCallback=undefined;
    const socket=await ready(w,share);socket.receive({type:'offer',peer:'publisher',sdp:'synthetic-video'});await flush();
    w.pcs[0].connect();w.pcs[0].deliverTrack();await flush();share.video.videoWidth=640;share.video.readyState=2;
    assert(w.timerRequests.some(t=>t.kind==='interval'&&t.ms===100));await withLeases(w,socket,20000);
    assert.match(share.status.textContent,/20 Sekunden/);noShareResources(w,share);
  },
  async stop_during_gathering_removes_deadlines_and_listener_without_late_send(){
    const w=world(),share=w.share(),socket=await ready(w,share,'publisher');w.setGathering('gathering');
    socket.receive({type:'viewer_joined',peer:'first'});await flush();
    assert(w.timerRequests.some(t=>t.kind==='timeout'&&t.ms===5000));assert.equal(w.pcs[0].events.get('icegatheringstatechange').size,1);
    share.stop('Manuell beendet.');noShareResources(w,share);const old=w.pcs[0];w.setGathering('complete');
    await ready(w,share,'publisher');const state=share.status.textContent;
    old.iceGatheringState='complete';old.dispatchEvent({type:'icegatheringstatechange'});await flush();
    assert.equal(socket.sent.filter(x=>x.type==='offer').length,0);assert.equal(share.status.textContent,state);
    share.stop();noShareResources(w,share);
  },
  async stuck_second_offer_expires_without_closing_first_and_releases_queue(){
    const w=world(),share=w.share(),socket=await ready(w,share,'publisher');
    socket.receive({type:'viewer_joined',peer:'first'});await flush();w.pcs[0].connect();
    assert.equal(w.pcs[0].connectionState,'connected');
    await withLeases(w,socket,5000);const gate=deferred();w.setOfferGate(gate);
    socket.receive({type:'viewer_joined',peer:'second'});await flush();assert.equal(w.pcs.length,2);
    socket.receive({type:'viewer_joined',peer:'third'});await flush();w.setOfferGate(null);
    assert.deepEqual(timeoutRequests(w).map(t=>t.due),[20000,25000]);
    await withLeases(w,socket,24999);assert.equal(w.pcs[1].closed,0);assert.equal(w.pcs.length,2);
    await w.advance(25000);assert.equal(w.pcs[1].closed,1);assert.equal(w.pcs[0].closed,0);
    assert.equal(w.pcs.length,3);assert.equal(share.peers.size,2);assert.equal(share.role,'publisher');
    assert.equal(w.captures[0].getVideoTracks()[0].readyState,'live');
    assert.equal(socket.sent.filter(x=>x.type==='offer'&&x.peer==='third').length,1);
    assert.equal(socket.sent.filter(x=>x.type==='offer'&&x.peer==='second').length,0);
    gate.resolve();await flush();assert.deepEqual(w.pcs[1].calls,['createOffer']);
    assert.equal(socket.sent.filter(x=>x.type==='offer'&&x.peer==='second').length,0);
    assert.match(share.status.textContent,/ist verbunden/);assert.doesNotMatch(share.status.textContent,/empfängt|empfangen/);share.stop();noShareResources(w,share);
  },
  async cancelled_answer_stages_never_continue_or_change_new_generation(){
    for(const [hold,expected] of [['setRemoteGate',['setRemoteDescription']],['setAnswerGate',['setRemoteDescription','createAnswer']],['setLocalGate',['setRemoteDescription','createAnswer','setLocalDescription']]]){
      const w=world(),share=w.share(),socket=await ready(w,share),gate=deferred();w[hold](gate);
      socket.receive({type:'offer',peer:'publisher',sdp:'synthetic-video'});await flush();const old=w.pcs[0];assert.deepEqual(old.calls,expected);
      share.stop('Erster Ausgang.');w[hold](null);await ready(w,share);const nextStatus=share.status.textContent;
      gate.resolve();await flush();assert.deepEqual(old.calls,expected);assert.equal(socket.sent.filter(x=>x.type==='answer').length,0);
      assert.equal(share.status.textContent,nextStatus);assert.equal(share.role,'viewer');assert.equal(share.peers.size,0);
      share.stop();noShareResources(w,share);
    }
  },
  async cancelled_frame_callback_and_old_peer_cannot_confirm_new_view(){
    const w=world(),share=w.share(),socket=await ready(w,share);
    socket.receive({type:'offer',peer:'publisher',sdp:'synthetic-video'});await flush();w.pcs[0].deliverTrack();await flush();
    const old=w.pcs[0],lateFrame=[...share.video.frameCallbacks.values()][0];assert.equal(typeof lateFrame,'function');
    share.stop('Erster Ausgang.');assert.equal(share.video.frameCallbacks.size,0);
    const fresh=await ready(w,share);fresh.receive({type:'offer',peer:'publisher',sdp:'synthetic-video'});await flush();
    w.pcs[1].connect();w.pcs[1].deliverTrack();await flush();const waiting=share.status.textContent;
    lateFrame(0,{presentedFrames:1});old.connect();await flush();assert.equal(share.status.textContent,waiting);
    assert.doesNotMatch(share.status.textContent,/Live-Fenster/);assert.equal(share.video.frameCallbacks.size,1);
    share.video.presentFrame();assert.match(share.status.textContent,/Live-Fenster/);share.stop();noShareResources(w,share);
  },
  async dropped_peer_callbacks_cannot_close_another_connected_publisher_peer(){
    const w=world(),share=w.share(),socket=await ready(w,share,'publisher');
    socket.receive({type:'viewer_joined',peer:'first'});await flush();w.pcs[0].connect();
    socket.receive({type:'viewer_joined',peer:'second'});await flush();const old=w.pcs[1];
    socket.receive({type:'viewer_left',peer:'second'});await flush();
    socket.receive({type:'viewer_joined',peer:'third'});await flush();w.pcs[2].connect();const status=share.status.textContent;
    old.fail();old.ondatachannel?.();old.ontrack?.({track:{kind:'audio'}});await flush();
    assert.equal(share.status.textContent,status);assert.equal(share.peers.size,2);assert.equal(w.pcs[0].closed,0);assert.equal(w.pcs[2].closed,0);
    assert.equal(share.role,'publisher');assert.doesNotMatch(status,/empfängt|empfangen/);share.stop();noShareResources(w,share);
  },
  async failure_diagnostics_are_collapsed_enums_only_and_reset_on_explicit_start(){
    const w=world(),share=w.share(),socket=await ready(w,share);
    const details=share.root.querySelector('[data-share-diagnostics]');assert(details);assert.equal(details.tag,'details');
    assert.equal(details.hidden,true);assert.equal(details.open,false);
    socket.receive({type:'offer',peer:'secret-peer-token',sdp:'a=candidate:192.168.178.123 secret-sdp-token'});await flush();
    const pc=w.pcs[0];pc.signalingState='https://192.168.178.123/private-secret';pc.iceConnectionState='checking';
    pc.connectionState='secret-connection-token';assert.equal(details.hidden,true);
    await withLeases(w,socket,20000);assert.equal(details.hidden,false);assert.equal(details.open,false);
    const text=details.querySelector('pre').textContent;assert.match(text,/Schritt:/);assert.match(text,/ICE: checking/);assert.match(text,/Erstes Bild dargestellt: nein/);
    assert.doesNotMatch(text,/192\.168|secret|candidate|https:|synthetic-video|[a-f0-9]{64}/);
    const reason=share.status.textContent;share.stop();w.pagehide();assert.equal(share.status.textContent,reason);assert.equal(details.querySelector('pre').textContent,text);
    await ready(w,share);assert.equal(details.hidden,true);assert.equal(details.open,false);assert.equal(details.querySelector('pre').textContent,'');share.stop();noShareResources(w,share);
  },
  async delayed_answer_for_timed_out_peer_cannot_revive_it(){
    const w=world(),share=w.share(),socket=await ready(w,share,'publisher');
    socket.receive({type:'viewer_joined',peer:'first'});await flush();w.pcs[0].connect();
    socket.receive({type:'viewer_joined',peer:'second'});await flush();const old=w.pcs[1];
    await withLeases(w,socket,20000);assert.equal(old.closed,1);assert.equal(share.role,'publisher');assert.equal(share.peers.size,1);
    const calls=[...old.calls];socket.receive({type:'answer',peer:'second',sdp:'late-answer'});await flush();
    assert.deepEqual(old.calls,calls);assert.equal(share.peers.size,1);assert.equal(w.pcs[0].closed,0);share.stop();noShareResources(w,share);
  },


  async server_signal_rejection_preserves_preclose_phase_and_ice_without_payload(){
    const w=world(),share=w.share(),socket=await ready(w,share);
    socket.receive({type:'offer',peer:'private-peer-token',sdp:'a=candidate:192.168.178.123 private-sdp-token'});await flush();
    assert.equal(socket.sent.filter(row=>row.type==='answer').length,1);
    const pc=w.pcs[0];pc.iceConnectionState='checking';pc.connectionState='connecting';
    const details=share.root.querySelector('[data-share-diagnostics]');assert.equal(details.hidden,true);
    socket.remoteClose(4400,'window_connection_ended');noShareResources(w,share);
    assert.equal(pc.connectionState,'closed');assert.equal(details.hidden,false);assert.equal(details.open,false);
    const diagnostic=details.querySelector('pre').textContent;
    assert.match(diagnostic,/Schritt: Antwort gesendet; auf Fensterbild warten/);
    assert.match(diagnostic,/ICE: checking/);assert.match(diagnostic,/Bildverbindung: connecting/);
    assert.doesNotMatch(diagnostic,/192\.168|private|candidate|synthetic-video|[a-f0-9]{64}/);
    const reason=share.status.textContent;share.stop();w.pagehide();assert.equal(share.status.textContent,reason);
    assert.equal(details.querySelector('pre').textContent,diagnostic);
    const normal=world(),view=normal.share(),remote=await ready(normal,view);
    remote.receive({type:'offer',peer:'publisher',sdp:'synthetic-video'});await flush();
    remote.remoteClose(4000,'sender_ended');assert.match(view.status.textContent,/Sender.*beendet/);
    assert.equal(view.root.querySelector('[data-share-diagnostics]').hidden,true);noShareResources(normal,view);
  },

};
(async()=>{const results=[];for(const [name,run] of Object.entries(cases)){
  try{await run();results.push({name,passed:true});}catch(error){results.push({name,passed:false,error:String(error)});}}
  console.log(JSON.stringify({actualSources:['window-share.js','hermes-workspace.js'],browser:false,network:false,sourceRoot:assets,sourceHashes,results}));
  if(results.some(r=>!r.passed))process.exitCode=1;
})().catch(error=>{console.error(error);process.exitCode=1;});
