/* Result-file UI on the actual temporary HTTPS dashboard. The text download and
 * inbox use Core receipts/stores; additional media descriptors and previews are
 * synthetic. No provider, production effect or microphone is used. */
const {chromium}=require('playwright');
const fs=require('fs'),path=require('path'),crypto=require('crypto'),assert=require('node:assert/strict');
const out=path.resolve(process.argv[2]);
const url=fs.readFileSync(path.join(out,'dashboard-url.txt'),'utf8').trim(),origin=new URL(url).origin;
assert.equal(new URL(url).hostname,'127.0.0.1');
assert.match(fs.readFileSync(path.join(out,'fixture-state-root.txt'),'utf8'),/solvio-dashboard-state-/);
const hash=b=>crypto.createHash('sha256').update(b).digest('hex');
const png=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=','base64');
const text=Buffer.from('Lokale Dateiprobe.\n<img src=x onerror="window.fileInjection=true">\nVollständiger Text im Download.\n');
const wav=Buffer.alloc(64044);wav.write('RIFF');wav.writeUInt32LE(wav.length-8,4);wav.write('WAVEfmt ',8);wav.writeUInt32LE(16,16);wav.writeUInt16LE(1,20);wav.writeUInt16LE(1,22);wav.writeUInt32LE(16000,24);wav.writeUInt32LE(32000,28);wav.writeUInt16LE(2,32);wav.writeUInt16LE(16,34);wav.write('data',36);wav.writeUInt32LE(wav.length-44,40);
let browser;const passed=[];
(async()=>{
 // Chromium's download manager does not inherit context.ignoreHTTPSErrors.
 // This flag is confined to the temporary loopback-only browser test.
 browser=await chromium.launch({headless:true,executablePath:process.env.BROWSER_EXECUTABLE,args:['--ignore-certificate-errors']});
 const context=await browser.newContext({ignoreHTTPSErrors:true,viewport:{width:1440,height:1000}});
 await context.route('**/*',r=>new URL(r.request().url()).origin===origin?r.continue():r.abort());
 const page=await context.newPage(),faults=[],requests=[];page.setDefaultTimeout(12000);
 page.on('pageerror',e=>faults.push(e.message));
 await page.addInitScript(()=>{
  window.micCalls=0;navigator.mediaDevices.getUserMedia=async()=>{window.micCalls++;throw Error('No microphone in file test');};
  Object.defineProperty(navigator,'share',{configurable:true,value:undefined});
  Object.defineProperty(navigator,'canShare',{configurable:true,value:undefined});
 });
 await page.goto(url);await page.locator('#enrollment').fill('n5-test-only-'.padEnd(43,'0'));
 await page.locator('#login-form button').click();await page.locator('#workspace').waitFor({state:'visible'});
 const originalList=await(await context.request.get(origin+'/v1/agent/runs')).json();
 const actual=JSON.parse(fs.readFileSync(path.join(out,'actual-result-file.json'),'utf8'));
 const id=actual.run_id,base=await(await context.request.get(origin+'/v1/agent/runs/'+id)).json();
 const bytes=new Map(),file=(suffix,name,mime_type,preview_kind,data)=>{
  const fid=name===actual.file.name?actual.file.id:'aa-'+suffix.padEnd(16,'0'),root=`/v1/agent/runs/${id}/artifacts/${fid}`;
  const f={id:fid,name,mime_type,size:data.length,sha256:hash(data),download_url:root+'/download',preview_url:root+'/preview',preview_kind};
  bytes.set(f.preview_url,{body:data,contentType:mime_type});bytes.set(f.download_url,{body:data,contentType:mime_type});return f;
 };
 const files=[file('1','Ergebnisbild.png','image/png','image',png),
  file('2','Dokumenttext.txt','text/plain','text',text),
  file('3','Ausgabe.pdf','application/pdf','pdf',Buffer.from('%PDF-1.4\n% synthetic download fixture\n%%EOF')),
  file('4','Audioprobe.wav','audio/wav','audio',wav),
  file('5','Videoprobe.mp4','video/mp4','video',Buffer.from('synthetic video descriptor; no playback claim')),
  {...file('6','<img src=x onerror=window.fileInjection=true>.bin','application/octet-stream','none',Buffer.from('fixture')),preview_url:null}];
 let detail={...base,id,zustand:'Fertig',zustand_code:'SUCCEEDED',offen:false,auftrag:'Dateien aus deinem Auftrag',ergebnis:'Die bestätigten Dateien stehen oben bereit.',
  dateien:files,artefakte:[{art:'document_result',id:files[1].id,download_url:files[1].download_url,pfad:'internal fixture path'}]};
 await page.route('**/v1/agent/runs',r=>r.fulfill({json:{...originalList,laeufe:[detail]}}));
 await page.route('**/v1/agent/runs/'+id,r=>r.fulfill({json:detail}));
 let delayed=null;
 await page.route('**/v1/agent/runs/*/artifacts/*/*',async r=>{
  const pathname=new URL(r.request().url()).pathname;requests.push(pathname);
  if(pathname===actual.file.download_url)return r.continue(); // Actual Core receipt/readback, not a fulfilled download interception.
  if(delayed&&pathname===files[1].preview_url)await delayed;
  const value=bytes.get(pathname);await r.fulfill(value?{...value,headers:{'Cache-Control':'no-store','X-Content-Type-Options':'nosniff',
   ...(pathname.endsWith('/download')?{'Content-Disposition':'attachment; filename="Dokumenttext.txt"'}:{})}}:{status:404,body:'not found'}).catch(()=>{});
 });
 const open=async()=>{
  await page.getByRole('button',{name:'Aufträge',exact:true}).click();
  await page.evaluate(()=>window.dispatchEvent(new Event('online')));
  await page.locator('#task-list').getByText(detail.auftrag,{exact:true}).click();
  await page.locator('.result-file-card').first().waitFor();
 };
 const card=i=>page.locator('.result-file-card').filter({has:page.getByRole('heading',{name:files[i].name,exact:true})});
 await open();
 assert.equal(await page.locator('.result-file-card').count(),6);
 assert.equal(await page.getByRole('link',{name:'Dokumenttext herunterladen',exact:true}).count(),1);
 assert.equal(await page.locator('#result iframe, #result object, #result embed').count(),0);
 assert.equal(await page.locator('#result audio, #result video').count(),0);
 assert.equal(await page.getByRole('button',{name:'Link teilen',exact:true}).count(),0);
 await card(0).locator('img').evaluate(img=>img.complete?true:new Promise(resolve=>img.addEventListener('load',resolve,{once:true})));
 assert.equal(await card(0).locator('img').evaluate(img=>img.naturalWidth),1);
 assert(requests.every(p=>p===files[0].preview_url));
 assert.equal(await page.evaluate(()=>Boolean(window.fileInjection)),false);
 assert.equal(await card(5).locator('img').count(),0);
 assert(await page.locator('.result-files').evaluate(n=>!!(n.compareDocumentPosition(document.querySelector('.result-copy'))&Node.DOCUMENT_POSITION_FOLLOWING)));
 passed.push('primary_bound_files_deduplicate_document_and_do_not_autoplay_or_embed_pdf');

 for(const [width,height,label] of [[1440,1000,'desktop'],[390,844,'mobile'],[320,780,'narrow']]){
  await page.setViewportSize({width,height});await page.screenshot({path:path.join(out,'files-'+label+'.png'),fullPage:true});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
 }
 await page.setViewportSize({width:1440,height:1000});
 passed.push('desktop_mobile_and_320px_file_cards_preserve_layout');

 await card(1).getByRole('button',{name:'Vorschau anzeigen',exact:true}).click();
 await card(1).locator('pre').waitFor();assert.equal(await card(1).locator('pre').textContent(),text.toString());
 assert.equal(await card(1).locator('pre img').count(),0);assert.equal(await page.evaluate(()=>Boolean(window.fileInjection)),false);
 const oldCard=await card(1).elementHandle();
 await page.locator('#conversation-objective').click();const refreshed=page.waitForResponse(r=>new URL(r.url()).pathname==='/v1/agent/runs/'+id);
 await page.evaluate(()=>window.dispatchEvent(new Event('online')));await refreshed;
 assert.equal(await oldCard.evaluate(n=>n.isConnected&&!!n.querySelector('pre')),true);
 const downloadEvent=page.waitForEvent('download');await card(1).getByRole('link',{name:'Dokumenttext herunterladen'}).click();
 const download=await downloadEvent;assert.equal(download.suggestedFilename(),files[1].name);
 assert.equal(await download.failure(),null,'Actual temporary Core HTTPS download must complete');
 assert.deepEqual(fs.readFileSync(await download.path()),text);
 assert.equal(await card(2).getByRole('link',{name:'PDF öffnen',exact:true}).getAttribute('href'),files[2].preview_url);
 assert.equal(await card(2).getByRole('link',{name:'PDF öffnen',exact:true}).getAttribute('rel'),'noopener noreferrer');
 passed.push('inert_text_preview_download_bytes_and_polling_preserve_current_card');

 await card(3).getByRole('button',{name:'Vorschau anzeigen',exact:true}).click();
 const audio=await card(3).locator('audio').elementHandle();
 assert.equal(await audio.evaluate(n=>n.autoplay||n.preload!=='none'),false);
 await audio.evaluate(async n=>{n.muted=true;await n.play();});assert.equal(await audio.evaluate(n=>n.paused),false);
 await card(4).getByRole('button',{name:'Vorschau anzeigen',exact:true}).click();
 assert.equal(await card(4).locator('video').evaluate(n=>n.autoplay||n.preload!=='none'),false);
 await page.getByRole('button',{name:'Aufträge',exact:true}).click();
 assert.equal(await audio.evaluate(n=>n.paused&&!n.hasAttribute('src')),true);
 assert.equal(await page.locator('#result audio, #result video').count(),0);
 assert.equal(await page.locator('#browser-voice').count(),1);
 await open();assert.equal(await page.locator('.result-file-card').count(),6);
 assert.equal(await page.locator('#result audio, #result video').count(),0);
 passed.push('explicit_media_preview_stops_on_navigation_and_reopen_never_resumes');

 const original=detail;
 detail={...detail,zustand:'Fehlgeschlagen',zustand_code:'FAILED',grund:'Die vollständige Prüfung konnte nicht bestätigt werden.',datei_hinweis:'Eine geänderte Datei steht nicht mehr bereit.',dateien:[files[0]]};
 await open();
 assert.match(await page.locator('.result-failure').textContent(),/Auftrag fehlgeschlagen/);
 assert(await page.locator('.result-failure').evaluate(n=>!!(n.compareDocumentPosition(document.querySelector('.result-files'))&Node.DOCUMENT_POSITION_FOLLOWING)));
 assert.match(await page.locator('.file-warning').textContent(),/geänderte Datei/);
 assert.equal(await page.locator('.result-file-card').count(),1); // New list is authoritative: no old download resurrection.
 passed.push('failed_status_and_missing_file_warning_precede_available_results');

 detail={...original};delete detail.dateien;
 await open();assert.equal(await page.locator('.result-file-card').count(),1);
 assert.equal(await page.getByRole('link',{name:'Dokumenttext herunterladen',exact:true}).getAttribute('href'),files[1].download_url);
 passed.push('legacy_response_without_file_list_keeps_existing_document_download');

 detail={...original,artefakte:[],dateien:[
  {...files[0],download_url:'https://example.invalid/other'},
  {...files[1],download_url:files[1].download_url.replace(id,'ar-0000000000000000')},
  {...files[2],download_url:'javascript:window.fileInjection=true'},
  {...files[3],sha256:'not-a-digest'},
  {...files[4],size:-1},
  {...files[5],mime_type:'text/html',preview_kind:'text',preview_url:files[5].download_url.replace('/download','/preview')} ]};
 await open();assert.equal(await page.locator('.result-file-card').count(),1);
 assert.equal(await page.locator('#result .file-open, #result .file-preview img, #result pre').count(),0);
 assert.equal(await page.locator('#result a').evaluateAll(a=>a.every(n=>n.getAttribute('href').startsWith('/v1/agent/runs/'))),true);
 passed.push('foreign_external_script_malformed_descriptors_and_active_formats_cannot_preview');

 detail={...original,artefakte:[],dateien:[{...files[1],size:70000}]};
 bytes.set(files[1].preview_url,{body:Buffer.alloc(70000,65),contentType:'text/plain'});
 await open();await card(1).getByRole('button',{name:'Vorschau anzeigen',exact:true}).click();
 await card(1).locator('pre').waitFor();assert.equal((await card(1).locator('pre').textContent()).length,65536);
 assert.match(await card(1).locator('.file-status').textContent(),/begrenzt/);
 bytes.set(files[1].preview_url,{body:Buffer.from('<script>fileInjection=true</script>'),contentType:'text/html'});
 await card(1).getByRole('button',{name:'Vorschau schließen',exact:true}).click();
 await card(1).getByRole('button',{name:'Vorschau anzeigen',exact:true}).click();
 await page.waitForFunction(()=>document.querySelector('.file-status')?.textContent.includes('nicht verfügbar'));
 assert.equal(await page.locator('#result pre').count(),0);
 passed.push('text_response_is_bounded_and_rejects_active_response_mime');

 await page.evaluate(()=>{
  window.shared=[];Object.defineProperty(navigator,'share',{configurable:true,value:async data=>window.shared.push(data)});
  Object.defineProperty(navigator,'canShare',{configurable:true,value:data=>typeof data.url==='string'});
 });
 detail={...original,artefakte:[],dateien:[files[1]]};bytes.set(files[1].preview_url,{body:text,contentType:'text/plain'});
 await open();await card(1).getByRole('button',{name:'Link teilen',exact:true}).click();
 const shares=await page.evaluate(()=>window.shared);assert.equal(shares.length,1);assert.equal(shares[0].url,origin+files[1].download_url);
 assert.match(await page.locator('.file-share-note').textContent(),/Anmeldung/);
 passed.push('web_share_offers_only_supported_explicit_private_link_sharing');

 const writes=[];
 page.on('request',r=>{if(r.method()==='POST')writes.push(new URL(r.url()).pathname);});
 await page.locator('[data-view="inbox"]').click();
 await page.locator('#inbox-list').getByRole('button',{name:'Ergebnis ansehen',exact:true}).click();
 await page.locator('#conversation').waitFor({state:'visible'});
 await card(1).waitFor();
 assert.equal(await page.locator('.result-file-card').count(),1);
 assert.deepEqual(writes,[]);
 const afterInbox=await(await context.request.get(origin+'/v1/agent/runs')).json();
 assert.deepEqual(afterInbox.laeufe.map(r=>r.id).sort(),originalList.laeufe.map(r=>r.id).sort());
 passed.push('actual_inbox_result_link_opens_existing_task_files_without_post_or_new_task');

 let release;delayed=new Promise(resolve=>{release=resolve;});
 const lateRequest=page.waitForRequest(r=>new URL(r.url()).pathname===files[1].preview_url);
 await card(1).getByRole('button',{name:'Vorschau anzeigen',exact:true}).click();await lateRequest;
 await page.locator('#logout').click();await page.locator('#login-panel').waitFor({state:'visible'});
 release();delayed=null;
 await page.waitForTimeout(100);assert.equal(await page.locator('#result').textContent(),'');
 assert.equal(await page.locator('#result pre, #result audio, #result video').count(),0);
 assert.equal(await page.evaluate(()=>micCalls),0);assert.deepEqual(faults,[]);
 passed.push('logout_cancels_late_file_preview_without_restoring_private_content_or_voice');
 fs.writeFileSync(path.join(out,'files-browser-result.json'),JSON.stringify({passed,temporary_https:true,actual_core_text_download:true,actual_core_inbox_link:true,mocked_additional_file_descriptors:true,synthetic_media:true,microphone_calls:0,provider_calls:0,javascript_errors:faults},null,2)+'\n');
 console.log(JSON.stringify({passed:passed.length,tests:passed}));
})().catch(e=>{console.error(e.stack);process.exitCode=1;}).finally(async()=>{await browser?.close();});
