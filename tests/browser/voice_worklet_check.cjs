// Deterministic boundary checks on the actual packaged processor, no devices.
const fs=require('fs'),vm=require('vm'),path=require('path'),assert=require('node:assert/strict');
const source=fs.readFileSync(path.resolve(__dirname,'../../src/solvio/dashboard/assets/voice-worklet.js'),'utf8');
function make(rate,ack=true){
 let Processor;const messages=[];
 class Base {constructor(){this.port={postMessage:m=>{messages.push(m);if(ack&&m.type==='capture')this.port.onmessage({data:{type:'capture_ack'}});}};}}
 vm.runInNewContext(source,{AudioWorkletProcessor:Base,sampleRate:rate,registerProcessor:(name,p)=>{assert.equal(name,'solvio-voice');Processor=p;},Float32Array,Int16Array,ArrayBuffer,DataView,Math});
 const p=new Processor();return {p,messages,send:data=>p.port.onmessage({data}),render:(value,count=128)=>{const input=new Float32Array(count).fill(value),out=new Float32Array(count);p.process([[input]],[[out]]);return out;}};
}
for(const rate of [8000,16000,44100,48000]){
 const w=make(rate);w.render(.25);assert.equal(w.messages.some(m=>m.type==='capture'),false);
 w.send({type:'capture',enabled:true});let frames=0;while(frames<rate){const n=Math.min(128,rate-frames);w.render(.25,n);frames+=n;}
 const pcm=w.messages.filter(m=>m.type==='capture');assert.equal(pcm.length,50,`one second at ${rate}`);
 for(const m of pcm){assert.equal(m.bytes.byteLength,640);const v=new DataView(m.bytes);for(let i=0;i<320;i++)assert.equal(v.getInt16(i*2,true),8192);}
}
const bound=make(48000,false);bound.send({type:'capture',enabled:true});for(let i=0;i<1000;i++)bound.render(-.5);assert.equal(bound.messages.filter(m=>m.type==='capture').length,4,'no unbounded MessagePort recording');
const player=make(48000);const bytes=new ArrayBuffer(32000),v=new DataView(bytes);for(let i=0;i<16000;i++)v.setInt16(i*2,-16384,true);
player.send({type:'pcm',bytes});for(let i=0;i<75;i++)assert(player.render(0).every(x=>x===-.5));player.send({type:'flush'});
assert.equal(player.messages.find(m=>m.type==='flushed').played_ms,200,'only rendered 200ms counted, not queued full second');assert(player.render(0).every(x=>x===0),'flush actually silences output');
const flood=make(48000);for(let i=0;i<5;i++)flood.send({type:'pcm',bytes:new ArrayBuffer(64000)});assert.equal(flood.messages.filter(m=>m.type==='overflow').length,1);assert(flood.p.size<=128000);
const mute=make(16000);mute.send({type:'capture',enabled:true,generation:1});mute.render(.75,160);
mute.send({type:'capture',enabled:false,generation:2});mute.render(.75,640);assert.equal(mute.messages.some(m=>m.type==='capture'),false);
mute.send({type:'capture',enabled:true,generation:3});mute.render(-.25,320);
const resumed=mute.messages.find(m=>m.type==='capture');assert.equal(resumed.generation,3);const resumedView=new DataView(resumed.bytes);
for(let i=0;i<320;i++)assert.equal(resumedView.getInt16(i*2,true),-8192,'pre-mute partial samples must not return after unmute');
const meter=make(16000);meter.send({type:'capture',enabled:true});meter.render(.25,320);
assert.equal(meter.messages.at(-1).rms,.25);assert.equal(meter.messages.at(-1).output_rms,0);
meter.send({type:'capture',enabled:false});meter.send({type:'pcm',bytes});meter.render(.9,320);
assert.equal(meter.messages.at(-1).rms,0);assert.equal(meter.messages.at(-1).output_rms,.5);assert.equal(meter.messages.at(-1).playing,true);
meter.send({type:'flush'});meter.render(.9,320);assert.equal(meter.messages.at(-1).output_rms,0);
console.log('9/9 processor boundaries passed (four rates, capture backlog, heard/flush, playback capacity, mute discards partial frame)');
