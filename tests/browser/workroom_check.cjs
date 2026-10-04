/* Real native Hermes renderer and local RPC worker via temporary HTTPS Core. */
const {chromium}=require('playwright');
const fs=require('fs'),path=require('path'),assert=require('node:assert/strict');
const out=path.resolve(process.argv[2]);
const url=fs.readFileSync(path.join(out,'dashboard-url.txt'),'utf8').trim();
const run=fs.readFileSync(path.join(out,'native-run.txt'),'utf8').trim();
const origin=new URL(url).origin;
if(new URL(url).hostname!=='127.0.0.1')throw Error('Isolated loopback only');
let browser;
(async()=>{
  browser=await chromium.launch({headless:true,executablePath:process.env.BROWSER_EXECUTABLE});
  const context=await browser.newContext({viewport:{width:1536,height:1080},ignoreHTTPSErrors:true});
  const rejected=[],calls=[],errors=[];
  await context.route('**/*',route=>{
    const u=new URL(route.request().url());
    if(u.origin!==origin||u.pathname.startsWith('/api/')){rejected.push(u.pathname);return route.abort();}
    calls.push({method:route.request().method(),path:u.pathname});return route.continue();
  });
  const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));
  await page.goto(url);await page.getByLabel('Anmeldecode',{exact:true}).fill('n5-test-only-'.padEnd(43,'0'));
  await page.getByRole('button',{name:'Verbinden',exact:true}).click();
  await page.locator('#workspace').waitFor({state:'visible'});
  await page.locator('nav [data-room="hermes"]').click();
  await page.locator('#workroom-task').selectOption(run);
  const frame=page.frameLocator('#hermes-frame');
  await frame.getByText('Native Websuche beendet; Ergebnisprüfung steht aus.',{exact:true}).waitFor();
  const current=await (await context.request.get(origin+'/v1/agent/runs/'+run)).json();
  assert.equal(current.zustand_code,'WAITING_SPECIALIST');
  assert.equal(await frame.locator('.native-state').textContent(),current.zustand);
  await page.locator('#workroom-knowledge > summary').click();
  await page.locator('#workroom-knowledge .memory-card').first().waitFor();
  await page.locator('#memory-search').fill('Bahnanbindung');
  await page.waitForFunction(()=>document.querySelectorAll('#memory-list .memory-card').length===1);
  assert.equal(await page.locator('#memory-list .memory-card').count(),1);
  await page.screenshot({path:path.join(out,'workroom-desktop.png'),fullPage:true});
  // Switching workspace removes the embedded document and its reader.
  await page.getByRole('button',{name:'Wissen',exact:true}).click();
  assert.equal(await page.locator('#hermes-frame').getAttribute('src'),null);
  assert.equal(await page.locator('#knowledge-view #memory-browser').count(),1);
  await page.locator('nav [data-room="hermes"]').click();
  await frame.getByText('Native Websuche beendet; Ergebnisprüfung steht aus.',{exact:true}).waitFor();
  await page.setViewportSize({width:390,height:844});
  await page.screenshot({path:path.join(out,'workroom-mobile.png'),fullPage:true});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  assert.deepEqual(rejected,[]);assert.deepEqual(errors,[]);
  assert.deepEqual(calls.filter(c=>c.method!=='GET').map(c=>c.path),['/v1/browser/session/login']);
  await page.getByRole('button',{name:'Abmelden',exact:true}).click();
  await page.locator('#login-panel').waitFor({state:'visible'});
  assert.equal(await page.locator('#hermes-frame').getAttribute('src'),null);
  assert.equal(await page.locator('#memory-list .memory-card').count(),0);
  fs.writeFileSync(path.join(out,'workroom-result.json'),JSON.stringify({passed:true,
    nativeRenderer:true,boundEventsRendered:true,externalRequests:rejected,jsErrors:errors,
    noTaskStartedByViewing:true,responsive:true,logoutClears:true},null,2));
  await browser.close();console.log('Workroom browser proof passed');
})().catch(async error=>{console.error(error);if(browser)await browser.close();process.exitCode=1;});
