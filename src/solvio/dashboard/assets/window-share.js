/* Reuse the browser's window picker and WebRTC. No recording or remote input. */
export class WindowShare {
  constructor(root, session) {
    this.root=root;this.session=session;this.generation=0;this.peers=new Map();this.peerState=new Map();
    this.video=root.querySelector('video');this.status=root.querySelector('[data-share-status]');
    this.channel=root.querySelector('select');
    this.diagnostics=document.createElement('details');this.diagnostics.setAttribute('data-share-diagnostics','');
    const summary=document.createElement('summary');summary.textContent='Verbindungsdetails';
    this.diagnosticText=document.createElement('pre');this.diagnostics.append(summary,this.diagnosticText);
    this.diagnostics.hidden=true;root.append(this.diagnostics);
    root.querySelector('[data-share-watch]').onclick=()=>void this.start('viewer');
    root.querySelector('[data-share-publish]').onclick=()=>void this.start('publisher');
    root.querySelector('[data-share-stop]').onclick=()=>this.stop(this.role==='publisher'?
      'Fensterfreigabe auf diesem Rechner manuell beendet.':
      'Fensteransicht auf diesem Rechner manuell beendet. Ein Sender auf einem anderen Rechner kann weiter übertragen.');
    this.channel.onchange=()=>this.stop('Fensterbereich gewechselt. Aufnahme und Ansicht auf diesem Rechner wurden beendet.');
    window.addEventListener('pagehide',()=>this.stop('Seite verlassen. Aufnahme und Ansicht auf diesem Rechner wurden beendet.'));
    this.stop();
  }
  say(text){this.status.textContent=text;}
  closePeer(id){
    const info=this.peerState.get(id),pc=this.peers.get(id);
    this.peerState.delete(id);this.peers.delete(id);
    if(info){clearTimeout(info.deadline);clearInterval(info.framePoll);info.cancel();
      if(info.frameCallback!==undefined)this.video.cancelVideoFrameCallback?.(info.frameCallback);
      for(const clean of info.cleanup)clean();}
    pc?.close();
  }
  diagnose(info){
    // Only fixed phases and WebRTC enum states. Never SDP, candidate addresses,
    // window titles, task contents, credentials or a browser-wide diagnostic.
    const state=(value,allowed)=>allowed.includes(value)?value:'unbekannt';
    const pc=info.pc;
    this.diagnosticText.textContent=[
      'Seite: '+(this.role==='publisher'?'Sender':'Empfänger'),
      'Schritt: '+info.phase,
      'Signalisierung: '+state(pc.signalingState,['stable','have-local-offer','have-remote-offer','closed']),
      'ICE: '+state(pc.iceConnectionState,['new','checking','connected','completed','disconnected','failed','closed']),
      'Bildverbindung: '+state(pc.connectionState,['new','connecting','connected','disconnected','failed','closed']),
      'Erstes Bild dargestellt: '+(this.role==='publisher'?'am Empfänger nicht prüfbar':info.frameSeen?'ja':'nein')
    ].join('\n');
    this.diagnostics.hidden=false;
  }
  stop(message) {
    const ending=!!this.role,publishing=this.role==='publisher';
    this.generation++;this.role=null;clearInterval(this.timer);this.timer=null;
    if(this.socket){this.socket.onclose=null;this.socket.close();this.socket=null;}
    for(const id of this.peers.keys())this.closePeer(id);
    if(this.stream)for(const track of this.stream.getTracks())track.stop();
    this.stream=null;this.video.pause();this.video.srcObject=null;this.video.hidden=true;
    this.root.querySelector('[data-share-stop]').disabled=true;
    this.root.querySelector('[data-share-watch]').disabled=false;
    this.root.querySelector('[data-share-publish]').disabled=false;
    this.channel.disabled=false;
    // Cleanup may run again after an error, visibility change or session read.
    // Preserve the first end reason until the owner's next explicit start.
    if(ending||!this.endMessage)this.endMessage=message||(publishing?'Fensterfreigabe auf diesem Rechner beendet.':
      'Keine Fensteransicht geöffnet. Ein Sender auf einem anderen Rechner kann weiter übertragen.');
    this.say(this.endMessage);
  }
  async start(role) {
    if(!this.session())return;
    this.stop();this.endMessage=null;this.diagnostics.hidden=true;this.diagnostics.open=false;
    this.diagnosticText.textContent='';const generation=this.generation;this.role=role;
    const active=()=>generation===this.generation;
    this.channel.disabled=true;
    this.root.querySelector('[data-share-stop]').disabled=false;
    this.root.querySelector('[data-share-watch]').disabled=true;
    this.root.querySelector('[data-share-publish]').disabled=true;
    try{
      if(!window.RTCPeerConnection)throw Error('Dieser Browser unterstützt die Fensterverbindung nicht.');
      if(role==='publisher'){
        if(!navigator.mediaDevices?.getDisplayMedia)throw Error('Bitte die Fensterfreigabe im Chrome-Browser auf dem Mac öffnen.');
        this.say('Wähle das gewünschte App-Fenster auf diesem Rechner. Kein Ton wird übertragen.');
        // Must remain directly inside the explicit click, before network awaits.
        const stream=await navigator.mediaDevices.getDisplayMedia({
          video:{displaySurface:'window',frameRate:{ideal:12,max:15}},audio:false,
          monitorTypeSurfaces:'exclude',selfBrowserSurface:'exclude',surfaceSwitching:'exclude',
          systemAudio:'exclude'});
        const tracks=stream.getTracks(),video=stream.getVideoTracks();
        if(!active()){tracks.forEach(t=>t.stop());return;}
        if(video.length!==1||stream.getAudioTracks().length||video[0].getSettings().displaySurface!=='window'){
          tracks.forEach(t=>t.stop());throw Error('Bitte ausschließlich ein einzelnes App-Fenster auswählen. Die Auswahl wurde beendet.');
        }
        this.stream=stream;this.video.srcObject=stream;this.video.hidden=false;
        video[0].onended=()=>{if(active())this.stop('Das ausgewählte Fenster wird nicht mehr freigegeben. Die lokale Aufnahme wurde beendet.');};
        await this.video.play();if(!active())return;
      }
      this.say(role==='publisher'?'Fenster ausgewählt. Private Verbindung wird aufgebaut.':'Private Fensteransicht wird verbunden.');
      const url=new URL('/v1/dashboard/window',location.href);url.protocol='wss:';
      const ws=new WebSocket(url);this.socket=ws;let lastLease=performance.now(),queue=Promise.resolve();
      const send=data=>{if(!active()||ws.readyState!==WebSocket.OPEN)throw Error('Die Fensterverbindung ist beendet.');ws.send(JSON.stringify(data));};
      this.timer=setInterval(()=>{if(active()&&performance.now()-lastLease>6000)
        this.stop('Verbindung nicht mehr bestätigt. Die lokale Aufnahme beziehungsweise Ansicht wurde beendet.');},1000);
      ws.onopen=()=>{if(active())send({type:'hello',role,channel:this.channel.value,csrf:this.session()?.csrf_token});};
      ws.onclose=event=>{if(!active())return;
        if(event.code===4400){const info=this.peerState.values().next().value;if(info)this.diagnose(info);}
        this.stop(event.code===4000&&event.reason==='sender_ended'?
        'Der Sender hat die Fensterfreigabe beendet. Die lokale Ansicht wurde geschlossen.':
        event.code===4401&&event.reason==='session_ended'?
        'Anmeldung nicht mehr bestätigt. Aufnahme und Ansicht auf diesem Rechner wurden beendet.':
        'Fensterverbindung beendet. Keine lokale Aufnahme oder Wiedergabe aktiv.');};
      ws.onerror=()=>{if(active())this.stop('Fensterverbindung nicht erreichbar. Bitte Anmeldung und Verbindung prüfen.');};
      const drop=id=>this.closePeer(id);
      const alive=info=>active()&&this.peerState.get(info.id)===info;
      const publisherStatus=()=>{
        const connected=[...this.peers.values()].filter(pc=>pc.connectionState==='connected').length;
        const linked=connected===1?'Ein angemeldetes Dashboard ist verbunden':
          connected+' angemeldete Dashboards sind verbunden';
        this.say(connected?'Fensterfreigabe aktiv · '+linked+
          (this.peers.size>connected?' · Ein weiterer Empfänger wird verbunden':'')+' · Ohne Ton':
          this.peers.size?'Empfänger angemeldet · Verbindung wird aufgebaut · Ohne Ton.':
          'Fensterfreigabe aktiv · Zurzeit kein Empfänger · Ohne Ton.');
      };
      const fail=(info,message)=>{
        if(!alive(info))return;
        this.diagnose(info);
        if(role==='viewer')this.stop(message);
        else{drop(info.id);publisherStatus();this.say(this.status.textContent+' '+message);}
      };
      const cancelled=Symbol('closed window peer');
      const step=async(info,phase,operation)=>{
        if(!alive(info))throw cancelled;
        info.phase=phase;
        const result=await Promise.race([
          Promise.resolve().then(()=>alive(info)?operation():cancelled),info.closed
        ]);
        if(result===cancelled||!alive(info))throw cancelled;
        return result;
      };
      const negotiate=async(info,operation)=>{
        try{await operation();}catch(error){if(error!==cancelled)
          fail(info,'Bildverbindung fehlgeschlagen. Bitte bei Bedarf erneut öffnen.');}
      };
      const make=id=>{
        if(this.peers.has(id)||this.peers.size>=(role==='publisher'?2:1))throw Error('Unerwartete Fensterverbindung.');
        const pc=new RTCPeerConnection({iceServers:[],bundlePolicy:'max-bundle'});this.peers.set(id,pc);
        const info={id,pc,phase:'Verbindungsaufbau',cleanup:new Set(),frameSeen:false};
        info.closed=new Promise(resolve=>{info.cancel=()=>resolve(cancelled);});this.peerState.set(id,info);
        // The signaling lease proves only Core reachability. It must not extend
        // the bounded wait for a peer connection / first presented video frame.
        info.deadline=setTimeout(()=>fail(info,role==='viewer'?
          'Nach 20 Sekunden kein Fensterbild. Ansicht beendet; bitte Verbindungsdetails prüfen.':
          'Ein Empfänger konnte in 20 Sekunden nicht verbunden werden.'),20000);
        pc.onconnectionstatechange=()=>{
          if(!alive(info))return;
          if(['failed','disconnected'].includes(pc.connectionState)){
            fail(info,role==='viewer'?'Bildverbindung unterbrochen. Ansicht geschlossen; bitte bei Bedarf erneut öffnen.':
              'Die Verbindung zu einem Empfänger wurde beendet.');
          }else if(pc.connectionState==='connected'){
            if(role==='publisher'){clearTimeout(info.deadline);publisherStatus();}
            else if(!info.frameSeen)this.say('Bildverbindung steht · Warte auf das erste Fensterbild · Ohne Ton.');
          }
        };
        pc.ontrack=event=>{
          if(!alive(info))return;
          if(role!=='viewer'||event.track.kind!=='video'){this.stop('Unerwartete Medienverbindung beendet.');return;}
          if(this.video.srcObject){this.stop('Unerwartete zweite Bildspur beendet.');return;}
          this.video.srcObject=new MediaStream([event.track]);this.video.hidden=false;
          event.track.onended=()=>{if(alive(info))this.stop('Das übertragene Fensterbild ist beendet. Die lokale Ansicht wurde geschlossen.');};
          const presented=()=>{
            if(!alive(info)||info.frameSeen)return;
            info.frameSeen=true;clearTimeout(info.deadline);clearInterval(info.framePoll);
            this.say('Live-Fenster vom Mac · Nur ansehen · Ohne Ton.');
          };
          if(this.video.requestVideoFrameCallback)info.frameCallback=this.video.requestVideoFrameCallback(presented);
          else{
            const displayed=()=>{const q=this.video.getVideoPlaybackQuality?.();return q?q.totalVideoFrames-q.droppedVideoFrames:0;};
            const before=displayed();
            info.framePoll=setInterval(()=>{
              if(this.video.videoWidth>0&&this.video.readyState>=2&&displayed()>before)presented();
            },100);
          }
          void this.video.play().catch(()=>fail(info,'Das Fensterbild konnte nicht abgespielt werden.'));
        };
        pc.ondatachannel=()=>{if(alive(info))this.stop('Unerwarteter Steuerkanal beendet.');};
        return info;
      };
      const gather=info=>new Promise((resolve,reject)=>{
        const pc=info.pc;
        const timer=setTimeout(()=>{clean();reject(Error('Keine direkte Fensterverbindung gefunden.'));},5000);
        const changed=()=>{if(pc.iceGatheringState==='complete'){clean();resolve();}};
        const clean=()=>{clearTimeout(timer);pc.removeEventListener('icegatheringstatechange',changed);info.cleanup.delete(clean);};
        info.cleanup.add(clean);
        pc.addEventListener('icegatheringstatechange',changed);changed();
      });
      const receive=async data=>{
        if(!active())return;
        if(data.type==='ready'){this.say(role==='publisher'?'Fensterfreigabe aktiv · Warte auf ein angemeldetes Dashboard · Ohne Ton.':
          'Warte auf das ausgewählte Mac-Fenster. Die Senderseite muss geöffnet bleiben.');return;}
        if(data.type==='viewer_left'&&role==='publisher'){
          drop(data.peer);publisherStatus();return;
        }
        if(data.type==='viewer_joined'&&role==='publisher'){
          const info=make(data.peer),pc=info.pc;
          publisherStatus();
          await negotiate(info,async()=>{
            pc.addTransceiver(this.stream.getVideoTracks()[0],{direction:'sendonly',streams:[this.stream]});
            const offer=await step(info,'Angebot erstellen',()=>pc.createOffer());
            await step(info,'Angebot vorbereiten',()=>pc.setLocalDescription(offer));
            await step(info,'Lokale Verbindungswege sammeln',()=>gather(info));
            if(!alive(info))return;
            send({type:'offer',peer:data.peer,sdp:pc.localDescription.sdp});info.phase='Auf Antwort des Empfängers warten';
          });return;
        }
        if(data.type==='offer'&&role==='viewer'){
          const info=make(data.peer),pc=info.pc;this.say('Sender gefunden · Bildverbindung wird aufgebaut · Ohne Ton.');
          await negotiate(info,async()=>{
            await step(info,'Angebot des Senders übernehmen',()=>pc.setRemoteDescription({type:'offer',sdp:data.sdp}));
            const answer=await step(info,'Antwort erstellen',()=>pc.createAnswer());
            await step(info,'Antwort vorbereiten',()=>pc.setLocalDescription(answer));
            await step(info,'Lokale Verbindungswege sammeln',()=>gather(info));
            if(!alive(info))return;
            send({type:'answer',peer:data.peer,sdp:pc.localDescription.sdp});info.phase='Antwort gesendet; auf Fensterbild warten';
          });return;
        }
        if(data.type==='answer'&&role==='publisher'){
          const info=this.peerState.get(data.peer);if(!info)return; // A timed-out peer may answer late.
          await negotiate(info,async()=>{
            await step(info,'Antwort des Empfängers übernehmen',()=>info.pc.setRemoteDescription({type:'answer',sdp:data.sdp}));
            info.phase='Antwort übernommen; auf Bildverbindung warten';
          });return;
        }
        throw Error('Unbekannte Fensterantwort.');
      };
      ws.onmessage=event=>{
        if(!active())return;
        let data;try{data=JSON.parse(event.data);}catch{this.stop('Ungültige Fensterantwort.');return;}
        if(data.type==='lease'){lastLease=performance.now();return;}
        queue=queue.then(()=>receive(data)).catch(()=>{if(active())this.stop('Fensterverbindung fehlgeschlagen. Bitte erneut öffnen.');});
      };
    }catch(error){if(active())this.stop(error?.name==='NotAllowedError'?'Keine Fensterfreigabe erteilt.':
      error?.message||'Die Fensterfreigabe konnte nicht gestartet werden.');}
  }
}
