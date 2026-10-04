// This client owns only this tab's live audio. Tasks remain in the Core.
// A socket close never proves that a provider connection was closed.
export class BrowserVoice {
  constructor(root,{api,getSession,prepareStart=async()=>({}),onPresence=()=>{},onLevel=()=>{},onState=()=>{}}){
    this.root=root;this.api=api;this.prepareStart=prepareStart;this.handoffWaiters=[];this.blockedChats=new Set();this.handoffBlocked=false;this.handoffRequired=false;this.getSession=getSession;this.onPresence=onPresence;this.onLevel=onLevel;this.onState=onState;this.lastRenderedState=null;
    this.startButton=root.querySelector('#voice-start');this.endButton=root.querySelector('#voice-end');this.interruptButton=root.querySelector('#voice-interrupt');this.muteButton=root.querySelector('#voice-mute');
    this.local=root.querySelector('#voice-local');this.provider=root.querySelector('#voice-provider');this.error=root.querySelector('#voice-error');
    this.available=false;this.serial=0;this.active=false;this.closing=false;this.uncertain=false;
    this.startButton.addEventListener('click',()=>void this.start());
    this.endButton.addEventListener('click',()=>this.end());
    this.interruptButton.addEventListener('click',()=>this.interrupt());
    this.muteButton.addEventListener('click',()=>this.toggleMute());
  }
  setAvailable(value){this.available=value;if(!value&&this.active)this.end();this.buttons();}
  buttons(){
    this.startButton.disabled=!this.available||this.active||this.closing;this.endButton.disabled=!this.active;this.interruptButton.hidden=!this.ready;
    this.muteButton.disabled=!this.available||!this.active||!this.ready||this.closing;
    this.muteButton.textContent=this.muted?'Mikrofon einschalten':'Stummschalten';this.muteButton.setAttribute('aria-pressed',String(!!this.muted));
    this.root.dataset.listening=String(!!this.ready&&!this.muted);this.root.dataset.muted=String(!!this.ready&&this.muted);
    this.render();
  }
  render(){
    // Presentation only: the dashboard may move this same control node between
    // views without reopening a stream, socket, or authenticated conversation.
    const state=Object.freeze({available:!!this.available,active:!!this.active,
      starting:!!this.active&&!this.ready,ready:!!this.ready,
      capturing:!!this.active&&!!this.ready&&!this.muted&&!this.closing&&!!this.stream?.getAudioTracks().some(t=>t.readyState==='live'&&t.enabled),
      muted:!!this.muted,closing:!!this.closing,uncertain:!!this.uncertain,
      conversationId:this.startContext?.conversation_id||null,handoffBlocked:!!this.handoffBlocked,
      localText:this.local.textContent,providerText:this.provider.textContent,errorText:this.error.textContent});
    const key=JSON.stringify(state);if(key===this.lastRenderedState)return;
    this.lastRenderedState=key;this.onState(state);
  }
  send(value){if(this.ws?.readyState===WebSocket.OPEN)this.ws.send(JSON.stringify(value));}
  async start(){
    if(!this.available||this.active||this.closing||!this.getSession())return;
    const serial=++this.serial,session=this.getSession(),current=()=>serial===this.serial&&this.active&&this.getSession()===session;
    this.active=true;this.ready=false;this.fullDuplex=false;this.muted=false;this.captureGeneration=0;this.ownerSession=session;this.error.textContent='';this.auth=null;this.generation=null;this.closedProof=false;this.closeObserved=false;this.interrupted=false;this.playbackPending=0;this.played=0;this.quiet=false;this.loud=0;
    this.startContext=null;this.flushed=null;this.handoffProtocol=null;
    this.local.textContent='Chat wird vorbereitet …';this.provider.textContent=this.uncertain?'Das Ende eines früheren Gesprächs ist weiterhin unbestätigt.':'Noch keine Sprachverbindung gestartet.';this.buttons();
    try{
      this.startContext=Object.freeze(await this.prepareStart());if(!current())return;
      this.handoffBlocked=this.blockedChats.has(this.startContext.conversation_id);
      this.local.textContent='Mikrofonfreigabe wird angefragt …';this.render();
      if(!navigator.mediaDevices?.getUserMedia||!window.AudioWorkletNode)throw {voice:'Dieser Browser unterstützt den Sprachzugang nicht. Bitte nutze einen aktuellen Chrome- oder Edge-Browser.'};
      const stream=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:true,noiseSuppression:true,autoGainControl:true},video:false});
      if(!current()){stream.getTracks().forEach(t=>t.stop());return;}
      this.stream=stream;
      if(!stream.getAudioTracks().some(t=>t.readyState==='live'))throw {voice:'Das Mikrofon ist nicht verfügbar.'};
      for(const track of stream.getTracks())track.addEventListener('ended',()=>{if(current())this.fail('Der Mikrofonzugriff wurde beendet.');},{once:true});
      this.local.textContent='Mikrofon an · Verbindung wird aufgebaut. Noch keine Übertragung.';this.render();
      const ctx=new AudioContext({latencyHint:'interactive'});this.ctx=ctx;
      await ctx.resume();if(!current())return;
      await ctx.audioWorklet.addModule('/dashboard/assets/voice-worklet.js');if(!current())return;
      this.node=new AudioWorkletNode(ctx,'solvio-voice',{numberOfInputs:1,numberOfOutputs:1,outputChannelCount:[1]});
      this.input=ctx.createMediaStreamSource(stream);this.input.connect(this.node);this.node.connect(ctx.destination);
      this.node.port.onmessage=e=>{if(current())this.audio(e.data);};
      this.abort=new AbortController();
      this.timer=setTimeout(()=>{if(current())this.fail('Der Gesprächsstart wurde nicht bestätigt.');},18000);
      const ticket=await this.api('/v1/browser/voice/session',{method:'POST',body:this.startContext,signal:this.abort.signal});if(!current())return;
      if(ticket.protocol_version!==1||ticket.websocket_path!=='/v1/browser/voice'||typeof ticket.nonce!=='string'||!ticket.nonce||ticket.audio?.encoding!=='pcm_s16le'||ticket.audio?.sample_rate!==16000||ticket.audio?.channels!==1)throw {voice:'Der Core konnte keinen gültigen Sprachzugang bestätigen.'};
      const url=new URL(ticket.websocket_path,location.href);url.protocol=location.protocol==='https:'?'wss:':'ws:';
      const ws=new WebSocket(url);this.ws=ws;ws.binaryType='arraybuffer';
      ws.onopen=()=>{if(current())this.send({type:'session_auth',nonce:ticket.nonce});else ws.close();};
      ws.onmessage=({data})=>{if(serial!==this.serial)return;try{this.message(data);}catch{this.fail('Die Sprachverbindung hat eine ungültige Antwort geliefert.');}};
      ws.onerror=()=>{if(serial===this.serial)this.fail('Die Sprachverbindung wurde unterbrochen.');};
      ws.onclose=()=>{if(serial!==this.serial)return;this.finishTransport();};
    }catch(e){
      if(!current())return;
      const text=e.name==='NotAllowedError'?'Kein Mikrofonzugriff. Du kannst ihn in den Browser-Einstellungen erlauben und danach erneut auf Sprechen klicken.':e.name==='NotFoundError'?'Kein Mikrofon gefunden.':e.status===409?'SOLVIO führt bereits ein Gespräch. Beende das andere Gespräch zuerst.':e.status===401?'Deine Anmeldung ist nicht mehr gültig. Bitte melde dich erneut an.':e.status===503?'Der Sprachzugang ist gerade nicht verfügbar.':e.voice||'Der Sprachzugang konnte nicht gestartet werden.';
      this.fail(text);
    }
  }
  message(data){
    if(data instanceof ArrayBuffer){if(this.ready&&!this.closing&&!this.interrupted){if(data.byteLength>65536||data.byteLength%2)throw Error('audio');this.playbackPending+=data.byteLength;if(this.playbackPending>256000)throw Error('audio_backlog');this.node?.port.postMessage({type:'pcm',bytes:data},[data]);}return;}
    if(typeof data!=='string'||data.length>8192)throw Error('control');const m=JSON.parse(data);
    if(m.type==='session_authenticated'){
      if(this.auth||!this.active||m.protocol_version!==1||typeof m.session_id!=='string'||!m.session_id||typeof m.connection_id!=='string'||!m.connection_id.startsWith('browser:'))throw Error('auth');
      this.handoffRequired=true;
      this.auth={session_id:m.session_id,connection_id:m.connection_id};this.provider.textContent='Sprachverbindung wird geöffnet …';this.render();this.send({type:'session_start'});
    }else if(m.type==='session_ready'){
      if(!this.auth||!this.active||this.closing||this.ready||m.session_id!==this.auth.session_id||m.connection_id!==this.auth.connection_id)throw Error('ready');
      if(m.conversation_id!==undefined&&m.conversation_id!==this.startContext?.conversation_id)throw Error('conversation_binding');
      this.handoffProtocol=m.handoff_protocol===1&&m.conversation_id===this.startContext?.conversation_id?1:null;
      // A generation supplied by Core is pinned here, never borrowed from a poll.
      if(Number.isInteger(m.generation)&&m.generation>0)this.generation=m.generation;
      // Only this authenticated connection's ready envelope selects duplex.
      // Missing/unknown capabilities preserve the existing interruption mode.
      this.fullDuplex=m.voice_mode==='full_duplex';
      clearTimeout(this.timer);this.ready=true;this.capture(true);this.local.textContent='Mikrofon an · SOLVIO hört zu.';this.provider.textContent='Gespräch offen. Zum Abschließen bitte Beenden drücken.';this.onPresence('listening');this.buttons();
    }else if(m.type==='conversation_flushed'){
      if(!this.auth||this.handoffProtocol!==1||m.session_id!==this.auth.session_id||m.conversation_id!==this.startContext?.conversation_id)return;
      this.flushed=m.status==='complete'&&m.provider_closed===true;
      if(!this.flushed)this.blockHandoff();
      this.settleHandoff();
      if(this.closeObserved){this.closing=false;this.ws?.close();this.finishTransport();}
    }else if(m.type==='session_closed'){
      if(!this.auth||m.session_id!==this.auth.session_id||m.connection_id!==this.auth.connection_id)return;
      const generation=Number.isInteger(m.generation)&&m.generation>=0&&(this.generation===null||m.generation>=this.generation);
      const valid=generation&&Number.isInteger(m.previous_unconfirmed)&&m.previous_unconfirmed>=0&&((m.provider==='closed_confirmed'&&m.generation>0)||(m.provider==='not_opened'&&m.generation===0));
      this.closeObserved=true;this.closedProof=valid&&m.previous_unconfirmed===0;this.uncertain=this.uncertain||!this.closedProof;
      this.provider.textContent=this.closedProof?(this.uncertain?'Dieses Gespräch ist beendet. Ein früheres Audioende bleibt unbestätigt.':m.provider==='not_opened'?'Keine Sprachverbindung geöffnet.':'Gesprächsende vom Core bestätigt.'):'Mikrofon aus. Das Ende der Sprachverbindung ist nicht vollständig bestätigt.';
      this.active=false;this.stopLocal();this.settleHandoff();
      if(this.handoffProtocol===1&&this.flushed===null){
        // The transcript drain may arrive after the independent audio-end proof.
        this.closing=true;clearTimeout(this.timer);this.timer=setTimeout(()=>{this.ws?.close();this.finishTransport();},20000);this.buttons();
      }else{this.closing=false;clearTimeout(this.timer);this.ws?.close();this.finishTransport();}
    }else if(m.type==='session_end'){this.end();}
    else if(m.type==='flush'){this.interrupted=false;this.node?.port.postMessage({type:'flush'});}
    else if(m.type==='ping'){this.send({type:'pong'});}
    else if(m.type==='error'){this.fail('Der Core hat das Gespräch beendet.');}
  }
  audio(m){
    if(m.type==='playback_ack'){this.playbackPending=Math.max(0,this.playbackPending-m.bytes);return;}
    if(m.type==='capture'){
      this.node?.port.postMessage({type:'capture_ack'});
      if(!this.active||!this.ready||this.muted||this.closing||m.generation!==this.captureGeneration)return;
      if(this.ws?.readyState!==WebSocket.OPEN||this.ws.bufferedAmount>65536){this.fail('Die Sprachverbindung ist zu langsam. Das Mikrofon wurde ausgeschaltet.');return;}
      this.ws.send(m.bytes);
    }else if(m.type==='flushed'){
      if(m.interrupt)this.send({type:'barge_in',played_ms:m.played_ms});
      else if(m.had_audio)this.send({type:'heard',played_ms:m.played_ms});
    }
    else if(m.type==='underrun'){this.send({type:'underrun'});this.onPresence(this.muted?'idle':'listening');}
    else if(m.type==='overflow'||m.type==='invalid_audio'){this.fail('Die Audiowiedergabe konnte nicht sicher fortgesetzt werden.');}
    else if(m.type==='level'&&this.ready){
      this.onLevel({input:this.muted?0:m.rms,output:m.playing?m.output_rms:0});
      this.played=m.played_ms;this.onPresence(m.playing?'speaking':this.muted?'idle':'listening');
      if(this.muted||this.fullDuplex)return;
      // Deliberately conservative local interruption, after a quiet interval.
      if(m.rms<.015){this.quiet=true;this.loud=0;}
      else if(m.playing&&this.quiet&&m.rms>.06){this.loud=(this.loud||0)+1;if(this.loud>=5){this.interrupt();this.quiet=false;this.loud=0;}}
    }
  }
  interrupt(){if(!this.ready||this.interrupted)return;this.interrupted=true;this.node?.port.postMessage({type:'flush',interrupt:true});}
  capture(enabled){this.node?.port.postMessage({type:'capture',enabled,generation:++this.captureGeneration});}
  toggleMute(){
    if(!this.available||!this.active||!this.ready||this.closing||this.getSession()!==this.ownerSession)return;
    const tracks=this.stream?.getAudioTracks()||[];
    if(!tracks.some(t=>t.readyState==='live')){this.fail('Das Mikrofon ist nicht verfügbar.');return;}
    this.muted=!this.muted;for(const track of tracks)track.enabled=!this.muted;
    this.capture(!this.muted);this.quiet=false;this.loud=0;
    this.local.textContent=this.muted?'Mikrofon stummgeschaltet · keine Mikrofonübertragung. Wiedergabe bleibt an.':'Mikrofon an · SOLVIO hört zu.';
    this.provider.textContent=this.muted?'Gespräch bleibt offen. Die Realtime-Verbindung läuft kostenpflichtig weiter.':'Gespräch offen. Zum Abschließen bitte Beenden drücken.';
    this.onPresence(this.muted?'idle':'listening');this.buttons();
  }
  blockHandoff(){
    this.handoffBlocked=true;
    if(this.startContext?.conversation_id)this.blockedChats.add(this.startContext.conversation_id);
  }
  isHandoffBlocked(id){return this.blockedChats.has(id);}
  continueWithStoredHistory(id){
    if(this.active||this.closing||!this.blockedChats.has(id))return false;
    this.blockedChats.delete(id);
    if(id===this.startContext?.conversation_id){this.handoffBlocked=false;this.handoffRequired=false;}
    // Explicit local recovery never upgrades the audio/transcript evidence.
    this.render();return true;
  }
  settleHandoff(){
    if(this.handoffBlocked){for(const resolve of this.handoffWaiters.splice(0))resolve(false);return;}
    if(this.closedProof&&this.flushed===true){
      this.handoffRequired=false;for(const resolve of this.handoffWaiters.splice(0))resolve(true);
    }
  }
  async endAndWait(conversationId){
    if(this.blockedChats.has(conversationId))return false;
    if(!this.active&&!this.closing&&conversationId!==this.startContext?.conversation_id)return true;
    if(!this.active&&!this.closing&&!this.handoffRequired)return true;
    if(!conversationId||conversationId!==this.startContext?.conversation_id)return false;
    // Stop before switching mode, even if the Core does not speak the new protocol.
    const wait=new Promise(resolve=>this.handoffWaiters.push(resolve));
    if(this.active)this.end();
    if(!this.ws&&!this.handoffRequired){for(const resolve of this.handoffWaiters.splice(0))resolve(true);}
    else if(!this.ws){this.blockHandoff();this.settleHandoff();}
    return wait;
  }
  stopLocal(){
    this.onLevel(null);
    this.ready=false;this.muted=false;this.abort?.abort();this.abort=null;
    this.stream?.getTracks().forEach(t=>t.stop());this.stream=null;
    if(this.node){this.node.port.onmessage=null;this.node.port.close();this.node.disconnect();this.node=null;}
    this.input?.disconnect();this.input=null;
    const ctx=this.ctx;this.ctx=null;if(ctx)void ctx.close().catch(()=>{});
    this.local.textContent='Mikrofon in diesem Tab aus. Wiedergabe beendet.';this.root.dataset.listening='false';this.root.dataset.muted='false';this.onPresence(null);
  }
  end({detach=false}={}){
    if(!this.active&&!this.closing)return;
    const hadSocket=!!this.ws;this.active=false;this.stopLocal();clearTimeout(this.timer);
    if(hadSocket){
      this.closing=true;this.send({type:'session_end'});if(!this.closedProof)this.provider.textContent='Mikrofon aus. Bestätigung des Gesprächsendes steht aus.';
      if(detach){this.uncertain=!this.closedProof||this.uncertain;this.ws.close();this.finishTransport();}
      else{this.timer=setTimeout(()=>{this.ws?.close();this.finishTransport();},20000);}
    }else{++this.serial;this.closing=false;this.provider.textContent=this.uncertain?'Ein früheres Audioende bleibt unbestätigt.':'Keine Sprachverbindung gestartet.';}
    this.buttons();
  }
  fail(text){this.error.textContent=text;this.end({detach:true});}
  finishTransport(){
    clearTimeout(this.timer);this.active=false;this.stopLocal();this.closing=false;
    if(this.ws&&!this.closedProof&&!this.closeObserved){this.uncertain=true;this.provider.textContent='Mikrofon aus. Das Ende der Sprachverbindung ist nicht bestätigt.';}
    this.ws=null;
    if(this.handoffRequired&&!(this.closedProof&&this.flushed===true))this.blockHandoff();
    this.settleHandoff();this.buttons();
  }
}
