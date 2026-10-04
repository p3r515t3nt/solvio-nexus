// One bounded, ephemeral audio pipeline. No recording or persistent buffers.
// Wire format matches the Core/iPhone: 16 kHz mono signed little-endian PCM.
class SolvioVoice extends AudioWorkletProcessor {
  constructor(){
    super();this.enabled=false;this.generation=0;this.ratio=sampleRate/16000;
    this.weight=0;this.sum=0;this.input=new Int16Array(320);this.at=0;this.pending=0;
    this.ring=new Float32Array(16000*8);this.read=0;this.write=0;this.size=0;
    this.position=0;this.played=0;this.wasPlaying=false;this.tick=0;
    this.port.onmessage=({data:m})=>{
      if(m.type==='capture_ack')this.pending=Math.max(0,this.pending-1);
      if(m.type==='capture'){
        this.enabled=m.enabled===true;this.generation=m.generation;
        // No pre-mute partial frame may be sent after a later unmute.
        this.at=0;this.weight=0;this.sum=0;
      }
      if(m.type==='flush'){
        this.port.postMessage({type:'flushed',played_ms:Math.floor(this.played*1000/16000),had_audio:this.played>0||this.size>0,interrupt:m.interrupt===true});
        this.size=0;this.read=this.write=0;this.position=0;this.played=0;this.wasPlaying=false;
      }
      if(m.type==='pcm'){
        if(!(m.bytes instanceof ArrayBuffer)||m.bytes.byteLength%2||m.bytes.byteLength>65536){this.port.postMessage({type:'invalid_audio'});return;}
        this.port.postMessage({type:'playback_ack',bytes:m.bytes.byteLength});
        const view=new DataView(m.bytes), count=view.byteLength/2;
        if(count>this.ring.length-this.size){this.port.postMessage({type:'overflow'});return;}
        for(let i=0;i<count;i++){this.ring[this.write]=view.getInt16(i*2,true)/32768;this.write=(this.write+1)%this.ring.length;}
        this.size+=count;
      }
    };
  }
  process(inputs,outputs){
    const output=outputs[0][0], input=inputs[0]?.[0];let energy=0,outputEnergy=0;
    for(let i=0;i<output.length;i++){
      // Area resampling preserves phase across render quanta and rates.
      if(this.enabled&&input){
        const value=input[i]||0;energy+=value*value;let remaining=1;
        while(remaining>1e-8){
          const take=Math.min(remaining,this.ratio-this.weight);this.sum+=value*take;this.weight+=take;remaining-=take;
          if(this.weight>=this.ratio-1e-8){
            const v=Math.max(-1,Math.min(1,this.sum/this.ratio));this.input[this.at++]=Math.round(v*(v<0?32768:32767));this.weight=0;this.sum=0;
            if(this.at===this.input.length){const bytes=new ArrayBuffer(640),view=new DataView(bytes);for(let j=0;j<320;j++)view.setInt16(j*2,this.input[j],true);if(this.pending<4){this.pending++;this.port.postMessage({type:'capture',bytes,generation:this.generation},[bytes]);}this.at=0;}
          }
        }
      }
      if(this.size){
        output[i]=this.ring[this.read];this.position+=16000/sampleRate;this.wasPlaying=true;
        while(this.position>=1&&this.size){this.position-=1;this.read=(this.read+1)%this.ring.length;this.size--;this.played++;}
      }else{output[i]=0;this.position=0;}
      outputEnergy+=output[i]*output[i];
    }
    if(this.wasPlaying&&!this.size){this.wasPlaying=false;this.port.postMessage({type:'underrun',played_ms:Math.floor(this.played*1000/16000)});}
    this.tick+=output.length;
    if(this.tick>=sampleRate*.02){this.tick=0;this.port.postMessage({type:'level',rms:Math.sqrt(energy/output.length),output_rms:Math.sqrt(outputEnergy/output.length),playing:this.wasPlaying,played_ms:Math.floor(this.played*1000/16000)});}
    return true;
  }
}
registerProcessor('solvio-voice',SolvioVoice);
