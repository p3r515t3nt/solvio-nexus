/* Real Chrome/WebRTC/signaling; ONLY capture source is a synthetic canvas. */
const {chromium}=require('playwright');
const fs=require('fs'),path=require('path'),assert=require('node:assert/strict');
const out=path.resolve(process.argv[2]);
const url=fs.readFileSync(path.join(out,'dashboard-url.txt'),'utf8').trim();
const origin=new URL(url).origin;
if(new URL(url).hostname!=='127.0.0.1')throw Error('Isolated loopback only');
let browser;
(async()=>{
  browser=await chromium.launch({headless:true,executablePath:process.env.BROWSER_EXECUTABLE});
  const errors=[],external=[],mutations=[];
  async function context(){
    const c=await browser.newContext({ignoreHTTPSErrors:true,viewport:{width:1280,height:1000}});
    await c.route('**/*',r=>{const u=new URL(r.request().url());
      if(u.origin!==origin){external.push(u.origin);return r.abort();}
      if(r.request().method()!=='GET')mutations.push(u.pathname);return r.continue();});
    const p=await c.newPage();p.on('pageerror',e=>errors.push(e.message));return p;
  }
  const sender=await context(),viewer=await context();
  await sender.addInitScript(()=>{
    window.testCaptures=[];window.testMode='window';
    navigator.mediaDevices.getDisplayMedia=async options=>{
      const canvas=document.createElement('canvas');canvas.width=640;canvas.height=360;
      const ctx=canvas.getContext('2d');let frame=0;
      const draw=()=>{frame++;ctx.fillStyle=frame%2?'#123f59':'#273e52';ctx.fillRect(0,0,640,360);
        ctx.fillStyle='#67dbaf';ctx.font='25px sans-serif';ctx.fillText('Isolierter Übertragungstest',35,65);
        ctx.fillText('Frame '+frame,35,120);};draw();const timer=setInterval(draw,80);
      const stream=canvas.captureStream(12),track=stream.getVideoTracks()[0];
      const settings=track.getSettings.bind(track);track.getSettings=()=>({...settings(),displaySurface:window.testMode});
      const stop=track.stop.bind(track);track.stop=()=>{clearInterval(timer);stop();};
      window.testCaptures.push({stream,options,track});return stream;
    };
  });
  async function login(page,prefix){
    await page.goto(url);await page.getByLabel('Anmeldecode',{exact:true}).fill(prefix.padEnd(43,'0'));
    await page.getByRole('button',{name:'Verbinden',exact:true}).click();
    await page.locator('#workspace').waitFor({state:'visible'});
    await page.locator('nav [data-room="obsidian"]').click();
  }
  await login(sender,'n6-sender-test-');await login(viewer,'n6-viewer-test-');
  assert.equal(await sender.evaluate(()=>testCaptures.length),0);
  const s=sender.locator('#window-share'),v=viewer.locator('#window-share');
  await v.getByRole('button',{name:'Fenster ansehen',exact:true}).click();
  await v.getByText('Warte auf das ausgewählte Mac-Fenster. Die Senderseite muss geöffnet bleiben.',{exact:true}).waitFor();
  await s.locator('summary').click();await s.getByRole('button',{name:'Fenster auf diesem Rechner auswählen'}).click();
  await viewer.waitForFunction(()=>document.querySelector('#window-share video').videoWidth===640);
  const frames=await viewer.locator('video').evaluate(v=>v.getVideoPlaybackQuality().totalVideoFrames);
  await viewer.waitForFunction(n=>document.querySelector('#window-share video').getVideoPlaybackQuality().totalVideoFrames>n+3,frames);
  assert.equal(await sender.evaluate(()=>testCaptures[0].options.audio),false);
  assert.equal(await viewer.locator('video').evaluate(v=>v.srcObject.getAudioTracks().length),0);
  await viewer.screenshot({path:path.join(out,'window-stream-test.png'),fullPage:true});
  // Closing a viewer must not end the remote sender; it can join again.
  await v.getByRole('button',{name:'Aufnahme / Ansicht beenden'}).click();
  assert.equal(await viewer.locator('video').evaluate(v=>v.srcObject),null);
  assert.equal(await sender.evaluate(()=>testCaptures[0].track.readyState),'live');
  await v.getByRole('button',{name:'Fenster ansehen',exact:true}).click();
  await viewer.waitForFunction(()=>document.querySelector('#window-share video').videoWidth===640);
  await s.getByRole('button',{name:'Aufnahme / Ansicht beenden'}).click();
  await viewer.waitForFunction(()=>document.querySelector('#window-share video').srcObject===null);
  assert.equal(await sender.evaluate(()=>testCaptures[0].track.readyState),'ended');
  // Whole-screen selection is stopped before any signaling or transmission.
  await sender.evaluate(()=>window.testMode='monitor');
  await s.getByRole('button',{name:'Fenster auf diesem Rechner auswählen'}).click();
  await s.getByText('Bitte ausschließlich ein einzelnes App-Fenster auswählen. Die Auswahl wurde beendet.',{exact:true}).waitFor();
  assert.equal(await sender.evaluate(()=>testCaptures.at(-1).track.readyState),'ended');
  // Browser logout terminates the sender and clears receiver frames.
  await sender.evaluate(()=>window.testMode='window');
  await v.getByRole('button',{name:'Fenster ansehen',exact:true}).click();
  await s.getByRole('button',{name:'Fenster auf diesem Rechner auswählen'}).click();
  await viewer.waitForFunction(()=>document.querySelector('#window-share video').videoWidth===640);
  await sender.getByRole('button',{name:'Abmelden',exact:true}).click();
  await viewer.waitForFunction(()=>document.querySelector('#window-share video').srcObject===null);
  assert.equal(await sender.evaluate(()=>testCaptures.at(-1).track.readyState),'ended');
  assert.deepEqual(errors,[]);assert.deepEqual(external,[]);
  assert(mutations.every(p=>p==='/v1/browser/session/login'||p==='/v1/browser/session/logout'));
  fs.writeFileSync(path.join(out,'window-result.json'),JSON.stringify({passed:true,
    transport:'actual_chrome_webrtc_and_https_core',capture:'synthetic_canvas_only',
    realDesktopOrWindowsAcceptance:false,changingFrames:true,audioTracks:0,
    viewerRejoin:true,senderStop:true,monitorRejected:true,logoutClears:true,
    noTaskStartedByViewing:true,jsErrors:errors,externalRequests:external},null,2));
  await browser.close();console.log('Window transport browser proof passed; actual desktop acceptance pending');
})().catch(async e=>{console.error(e);if(browser)await browser.close();process.exitCode=1;});
