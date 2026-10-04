/* Generated, pinned Hermes React bundle in jsdom; no browser, HTTP or provider. */
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const {createRequire}=require('node:module');
const tree=path.resolve(process.argv[2]),fromBuild=createRequire(path.join(tree,'package.json'));
const {JSDOM,VirtualConsole}=fromBuild('jsdom'),{transformSync}=fromBuild('esbuild');
const root=path.resolve(__dirname,'..'),assets=path.join(root,'src/solvio/dashboard/assets');
const manifest=JSON.parse(fs.readFileSync(path.join(assets,'hermes/BUILD.json'),'utf8'));
assert.equal(manifest.commit,'fcbd1076a93841fa88855acce810e342a5b78101');
assert.equal(manifest.execution_transport_modules,0);
for(const [name,digest] of Object.entries(manifest.files)){
  assert.equal(crypto.createHash('sha256').update(fs.readFileSync(path.join(assets,'hermes',name))).digest('hex'),digest,name);
}
const entry=Object.keys(manifest.files).find(name=>/^assets\/solvio-view-.*\.js$/.test(name));
assert(entry);
// The browser entry is bundled already. Only the ESM wrapper is adapted for
// jsdom's JS context; original React/Hermes components remain in the bundle.
const bundle=transformSync(fs.readFileSync(path.join(assets,'hermes',entry),'utf8'),{format:'iife',target:'es2022'}).code;
const run='ar-0123456789abcdef';
const observation={id:1,at:1,summary:'Native Suche beobachtet',kind:'native_progress',step_id:'as-step',
  observation:{runtime:'hermes-codex-app-server',event:'started',seq:1,invocation_id:'inv-1',native_thread_id:'thread-1',native_turn_id:'turn-1'}};
const delay=()=>new Promise(resolve=>setTimeout(resolve,10));
async function until(check){for(let i=0;i<100;i++){if(check())return;await delay();}throw Error('Expected DOM state was not reached');}

async function world({observer=false,empty=false,events=[observation]}={}){
  const errors=[],reads=[];let socketCalls=0,captureCalls=0,failure=0;
  const log=new VirtualConsole();log.on('jsdomError',error=>errors.push(String(error)));
  const dom=new JSDOM('<!doctype html><html lang="de"><body></body></html>',{
    url:'https://127.0.0.1/dashboard/hermes/solvio-view.html'+(empty?'':'?run='+run),
    pretendToBeVisual:true,runScripts:'outside-only',virtualConsole:log});
  const w=dom.window;
  for(const name of ['TransformStream','ReadableStream','WritableStream','TextEncoder','TextDecoder'])w[name]=globalThis[name];
  w.addEventListener('error',event=>errors.push(String(event.error||event.message)));
  w.matchMedia=()=>({matches:false,addEventListener(){},removeEventListener(){},addListener(){},removeListener(){}});
  w.ResizeObserver=class {observe(){}unobserve(){}disconnect(){}};
  w.IntersectionObserver=class {observe(){}unobserve(){}disconnect(){}};
  w.HTMLElement.prototype.scrollTo=function(value){this.scrollTop=value?.top||0;};
  w.HTMLMediaElement.prototype.pause=function(){};
  w.HTMLMediaElement.prototype.play=async function(){};
  w.WebSocket=class {constructor(){socketCalls++;throw Error('Socket not allowed by this fixture');}};
  Object.defineProperty(w.navigator,'mediaDevices',{value:{getDisplayMedia(){captureCalls++;throw Error('Capture not allowed by this fixture');},getUserMedia(){throw Error('Audio not allowed by this fixture');}}});
  w.fetch=async raw=>{
    const url=new URL(raw,w.location.href);assert.equal(url.origin,w.location.origin);reads.push(url.pathname);
    if(failure)return {ok:false,status:failure};
    let value;
    if(url.pathname==='/v1/browser/session')value={session_id:'session-local',csrf_token:'a'.repeat(64),purpose:observer?'hermes_observer_v1':'owner'};
    else if(url.pathname==='/v1/agent/runs')value={laeufe:[{id:run,auftrag:'Mein tatsächlicher Core-Auftrag'}]};
    else if(url.pathname==='/v1/agent/runs/'+run+'/events')value={events,next_cursor:events.length,has_more:false};
    else if(url.pathname==='/v1/agent/runs/'+run)value={id:run,auftrag:'Mein tatsächlicher Core-Auftrag',zustand:'Fertig',
      ergebnis:'Das ist das bestätigte SOLVIO-Ergebnis.\n\n[Quelle](https://example.test/source)',schritte:[{spezialist:'Kalender',zustand:'unknown',zusammenfassung:'Ausgang ungewiss'}]};
    else throw Error('Unexpected read '+url.pathname);
    return {ok:true,status:200,json:async()=>value};
  };
  w.eval(fs.readFileSync(path.join(assets,'window-share.js'),'utf8').replace('export class WindowShare','class WindowShare')+'\nwindow.WindowShare=WindowShare;');
  const shell=fs.readFileSync(path.join(assets,'hermes-workspace.js'),'utf8');
  const importLine="import {WindowShare} from '/dashboard/assets/window-share.js';";
  assert.equal(shell.split(importLine).length,2);
  w.eval(shell.replace(importLine,'').replaceAll('export function ','function ')+'\nwindow.mountWorkspace=mountWorkspace;');
  const section=w.document.createElement('section');section.id='solvio-workspace';
  const native=w.document.createElement('div');native.id='root';native.hidden=true;
  w.document.body.append(section,native);
  const workspace=w.mountWorkspace(section,native);
  try{
    w.eval(bundle);
    await until(()=>empty?section.querySelector('select').options.length===2:native.querySelector('[data-role="assistant"]'));
  }catch(error){workspace.stop();dom.window.close();throw error;}
  return {w,section,native,workspace,reads,errors,setFailure:value=>{failure=value;},
    assertNoEffects(){assert.equal(socketCalls,0);assert.equal(captureCalls,0);assert.deepEqual(errors,[]);},
    async close(){workspace.stop();await delay();dom.window.close();}};
}
(async()=>{
  const checks=[];
  const current=await world();
  try{
    const {w,section,native}=current,details=section.querySelector('[data-workspace-details]'),evidence=w.document.getElementById('solvio-hermes-evidence');
    assert.equal(section.querySelectorAll('h1').length,1);assert.equal(native.parentElement,section);
    assert.equal(section.children[2],native);assert.equal(section.children[3],details);assert.equal(details.open,false);
    assert.equal(native.querySelectorAll('[data-role="user"]').length,1);assert.equal(native.querySelectorAll('[data-role="assistant"]').length,1);
    assert.match(native.textContent,/SOLVIO-Ergebnis/);assert.equal(native.querySelectorAll('[data-role="tool"]').length,0);
    checks.push('generated_original_messages_and_core_result_lead');
    await until(()=>evidence.querySelector('[data-role="tool"]'));
    assert.equal(details.contains(evidence),true);assert.match(evidence.textContent,/Native Suche beobachtet/);
    details.open=true;evidence.querySelector('button').click();await until(()=>evidence.textContent.includes('thread-1'));
    assert.match(evidence.textContent,/as-step/);assert.match(evidence.textContent,/inv-1/);
    checks.push('original_tool_row_and_bound_identifiers_live_only_in_details');
    assert.equal(section.querySelectorAll('details').length,3); // Details, optional window, publisher help.
    assert.equal(section.children[4].open,false);assert.match(details.textContent,/Bibliothekslizenzen/);
    checks.push('single_technical_details_and_separate_closed_window');
    current.setFailure(401);await current.workspace.refresh();
    assert.equal(native.hidden,true);assert.equal(evidence.hidden,true);
    await until(()=>native.querySelectorAll('[data-role]').length===0&&evidence.childElementCount===0);
    assert.equal(section.querySelector('header [role="status"]').hidden,false);
    checks.push('revocation_hides_then_clears_both_actual_react_targets');
    current.assertNoEffects();
  }finally{await current.close();}
  const generic=await world({events:[]});
  try{
    assert.match(generic.native.textContent,/SOLVIO-Ergebnis/);assert.equal(generic.native.hidden,false);
    assert.equal(generic.w.document.getElementById('solvio-hermes-evidence').hidden,true);
    assert.match(generic.section.querySelector('[data-workspace-details]').textContent,/keine belegten Hermes-Aufrufe/);
    checks.push('non_hermes_core_result_is_visible_without_hermes_claim');generic.assertNoEffects();
  }finally{await generic.close();}
  const observer=await world({observer:true,empty:true,events:[]});
  try{
    const picker=observer.section.querySelector('select');assert.equal(picker.value,'');
    assert.equal(observer.native.hidden,true);picker.value=run;picker.dispatchEvent(new observer.w.Event('change'));
    await until(()=>observer.native.querySelector('[data-role="assistant"]'));
    assert.equal(observer.section.children[4].hidden,true);assert.match(observer.native.textContent,/SOLVIO-Ergebnis/);
    assert.equal(observer.w.location.search,'?run='+run);
    checks.push('native_observer_task_selection_survives_without_window_authority');observer.assertNoEffects();
  }finally{await observer.close();}
  console.log(JSON.stringify({passed:true,checks,dom:'jsdom_with_generated_original_hermes_react_bundle',
    build_manifest_sha256:crypto.createHash('sha256').update(fs.readFileSync(path.join(assets,'hermes/BUILD.json'))).digest('hex'),
    browser:false,pixels_or_layout_verified:false,network:false,real_provider_calls:0},null,2));
})().catch(error=>{console.error(error);process.exitCode=1;});
