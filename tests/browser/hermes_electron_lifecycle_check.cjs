/* Actual Electron closes across held Core handoff/accepted HTTP boundaries. */
const {_electron}=require('playwright'),fs=require('node:fs'),path=require('node:path');
const out=path.resolve(process.argv[2]),entry=path.resolve(process.argv[3]),binary=path.resolve(process.argv[4]);
const read=()=>JSON.parse(fs.readFileSync(path.join(out,'audit.json'),'utf8'));
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function until(fn,limit=15000){let start=Date.now();while(!await fn()){if(Date.now()-start>limit)throw Error('lifecycle timeout');await pause(50)}}
let seq=read().command_seq,app=null,child=null;
const proof={cases:[],test_only:true};
const fixturePath=path.join(out,'fixture.json'), originalFixture=JSON.parse(fs.readFileSync(fixturePath,'utf8'));
function holdCompleteLogin(enabled){fs.writeFileSync(fixturePath,JSON.stringify({...originalFixture,hold_complete_login:enabled}));}
async function command(action){const n=++seq;fs.writeFileSync(path.join(out,'command.json'),JSON.stringify({action,seq:n}));await until(()=>read().command_seq===n)}
async function open(){app=await _electron.launch({executablePath:binary,args:[entry,path.join(out,'fixture.json')],env:{PATH:process.env.PATH,HOME:process.env.HOME,USER:process.env.USER,TMPDIR:process.env.TMPDIR},timeout:20000});child=app.process();return app.firstWindow()}
async function exited(){await until(()=>child.exitCode!==null||child.signalCode!==null);app=null;}
(async()=>{
  await command('delay_handoff');await open();await until(()=>read().delay_waits.handoff===1);
  await app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows()[0].close());await exited();await command('release_delays');
  proof.cases.push({name:'close_before_handoff',passed:read().active_observers===0,active_observers:read().active_observers});
  await command('delay_login');await open();await until(()=>read().delay_waits.login===1);
  const partialCookieArrived=await app.evaluate(async({BrowserWindow})=>(await BrowserWindow.getAllWindows()[0].webContents.session.cookies.get({name:'__Host-solvio-hermes-observer'})).length===1);
  const before=read().observer_enrollments;
  await app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows()[0].close());await exited();await command('release_delays');await pause(200);
  proof.cases.push({name:'close_during_partial_login_revokes_if_cookie_arrived_otherwise_unconfirmed',passed:read().active_observers===(partialCookieArrived?0:1)&&read().observer_enrollments===before,
    cookie_arrived:partialCookieArrived,
    active_observers:read().active_observers,enrollment_unchanged:read().observer_enrollments===before});
  // This Core acceptance could not reach the cookie store; clean only test state.
  await command('revoke');
  holdCompleteLogin(true);await open();await until(()=>fs.existsSync(path.join(out,'complete-login-held.json')));
  const cookieArrived=await app.evaluate(async({BrowserWindow})=>(await BrowserWindow.getAllWindows()[0].webContents.session.cookies.get({name:'__Host-solvio-hermes-observer'})).length===1);
  const enrolled=read().observer_enrollments;
  const hiddenAfterAcceptedLogin=await app.evaluate(({BrowserWindow})=>{const win=BrowserWindow.getAllWindows()[0];win.close();return win.isDestroyed()||!win.isVisible()});
  await exited();await until(()=>read().active_observers===0);holdCompleteLogin(false);
  proof.cases.push({name:'close_after_actual_complete_login_before_startup_continuation',passed:cookieArrived&&hiddenAfterAcceptedLogin&&read().active_observers===0&&read().observer_enrollments===enrolled,
    cookie_arrived:cookieArrived,view_stopped_immediately:hiddenAfterAcceptedLogin,active_observers:read().active_observers,enrollment_unchanged:read().observer_enrollments===enrolled});
  const page=await open();await page.getByRole('heading',{name:'Belegter Zwischenstand',exact:true}).waitFor();
  await command('delay_probe_and_logout');await until(()=>read().delay_waits.probe>=1);
  const closedImmediately=await app.evaluate(({BrowserWindow})=>{const win=BrowserWindow.getAllWindows()[0];win.close();return win.isDestroyed()||!win.isVisible()});
  await until(()=>read().delay_waits.logout>=1);
  await command('release_delays');await exited();
  proof.cases.push({name:'active_probe_close_hides_before_logout_response',passed:closedImmediately&&read().active_observers===0,
    view_stopped_immediately:closedImmediately,active_observers:read().active_observers});
  proof.after=read();proof.passed=proof.cases.filter(item=>item.passed).length;proof.failed=proof.cases.length-proof.passed;
  fs.writeFileSync(path.join(out,'hermes-electron-lifecycle-check.json'),JSON.stringify(proof,null,2));console.log(JSON.stringify(proof.cases));
  if(proof.failed)process.exitCode=1;
})().catch(error=>{proof.error=error.stack;proof.failed=1;fs.writeFileSync(path.join(out,'hermes-electron-lifecycle-check.json'),JSON.stringify(proof,null,2));console.error(error.stack);process.exitCode=1}).finally(async()=>{if(app){await command('release_delays');await app.close().catch(()=>{})}});
