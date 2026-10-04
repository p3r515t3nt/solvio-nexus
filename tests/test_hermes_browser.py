"""Real Hermes/MCP/Chrome seam with only local test HTML and temporary state."""
from __future__ import annotations
import asyncio
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from solvio.specialists import hermes_browser as B, hermes_native as N
from solvio.specialists import hermes_native_worker as W

# The isolated browser runtime lives outside ~/.codex (that tree belongs to the
# desktop app and vanished on 2026-09-17). The operator paths win; the fallback
# is the rebuilt runtime hermes-browser-20260917 (see its BUILD.json).
RUNTIME = Path.home() / ".solvio-nexus/runtimes/hermes-browser-20260917"
PYTHON = os.environ.get("AGENT_RUNTIME_HERMES_BROWSER_PYTHON") or str(RUNTIME / "bin/python")
HERMES = str(Path.home() / ".solvio-hermes/src")
HERMES_PYTHON = str(Path.home() / ".solvio-hermes/venv/bin/python")
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
BIN = os.environ.get("AGENT_RUNTIME_HERMES_BROWSER_BIN") or str(RUNTIME / "bin/agent-browser-0.26.0")


def refused(call):
    try:
        call()
    except (ValueError, KeyError):
        return
    require(False, "Invalid input was accepted")


def t_factory_pins_runtime_task_and_exact_mcp_surface():
    B.validate_runtime(PYTHON, BIN, CHROME, HERMES)
    config = N.NativeResearchConfig(HERMES_PYTHON,HERMES,"/usr/bin/false","/tmp/test-home","synthetic",30,3,
                                    PYTHON,BIN,CHROME)
    refused(lambda: N.worker_invocation(config,"/tmp/test"))
    invocation = N.worker_invocation(config,"/tmp/test",task_id="at-safe")
    require_equal(invocation.argv[-2:],("--task-id","at-safe"))
    for task in ("", "../../other", "foreign task", "x"*129):
        refused(lambda: N.worker_invocation(config,"/tmp/test",task_id=task))
    entry = B.mcp_entry(python=PYTHON,binary=BIN,chrome=CHROME,hermes_python=HERMES_PYTHON,
        source=HERMES,task_id="at-safe",workdir="/tmp/test",timeout=30)
    require_equal(set(entry["enabled_tools"]),set(B.TOOLS))
    require(entry["required"] is True and entry["env_vars"] == [])
    require(not any("sandbox" in arg for arg in entry["args"]))
    raw = {B.SERVER:entry}
    W._validate_layers({"layers":[{"config":{"mcp_servers":raw}}]},raw)
    for key, expected in (("startup_timeout_sec",15.0),("tool_timeout_sec",35.0)):
        require(type(entry[key]) is float and entry[key]==expected)
        measured=json.loads(json.dumps(raw)); measured[B.SERVER][key]=float(expected)
        W._validate_layers({"layers":[{"config":{"mcp_servers":measured}}]},raw)
        for changed in (False,True,int(expected),expected+0.5):
            foreign=json.loads(json.dumps(raw));foreign[B.SERVER][key]=changed
            refused(lambda:W._validate_layers({"layers":[{"config":{"mcp_servers":foreign}}]},raw))
    refused(lambda: W._validate_layers({"layers":[{"config":{"mcp_servers":raw}}]}))
    foreign = json.loads(json.dumps(raw)); foreign[B.SERVER]["args"] += ["--task-id","foreign"]
    refused(lambda: W._validate_layers({"layers":[{"config":{"mcp_servers":foreign}}]},raw))
    normalized=W._effective_browser_config(raw)
    require_equal(normalized[B.SERVER]["environment_id"],"local")
    require("env_vars" not in normalized[B.SERVER])


def t_complete_snapshot_chunks_keep_late_negation_and_reject_other_task_or_changes():
    with tempfile.TemporaryDirectory() as directory:
        root=Path(directory).resolve(); (root/"cache/web").mkdir(parents=True)
        obj=B.Browser.__new__(B.Browser)
        obj.task_id="at-first"; obj.home=root; obj.saved={}; obj.current=None
        def store(text):
            p=root/"cache/web/full.txt"; p.write_text(text); return str(p)
        obj.original_store=store
        text="a"*60_000+" Dieses Angebot passt NICHT."
        obj.store(text)
        sid=obj.current; offset=0; chunks=[]
        while True:
            result=obj.read_snapshot({"snapshot_id":sid,"offset":offset})
            chunks.append(result["text"])
            if result["next_offset"] is None:break
            offset=result["next_offset"]
        require_equal("".join(chunks),text)
        require(len(chunks)>1 and max(map(len,chunks))<=B.CHUNK)
        refused(lambda: obj.read_snapshot({"snapshot_id":sid.replace("at-first","at-second")}))
        refused(lambda: obj.read_snapshot({"snapshot_id":sid,"offset":len(text)+1}))
        refused(lambda: obj.read_snapshot({"snapshot_id":sid,"full":True}))
        (root/"cache/web/full.txt").write_text("changed")
        refused(lambda: obj.read_snapshot({"snapshot_id":sid}))
        refused(lambda: obj.store("x"*(B.MAX_SNAPSHOT+1)))


def t_process_cleanup_uses_existing_expected_start_and_rejects_changed_identity():
    obj=B.Browser.__new__(B.Browser); obj.bound={123:(987654,"/tmp/owned","h_owned")}
    calls=[]
    obj.psutil=SimpleNamespace(pid_exists=lambda pid:True)
    obj.PR=SimpleNamespace(_host_pid_is_ours=lambda pid,start: pid==123 and start==987654)
    obj.B=SimpleNamespace(_verify_reapable_browser_daemon=lambda pid,directory,name: True)
    obj.original_terminate=lambda pid,**kw:calls.append((pid,kw))
    obj.terminate_bound(123)
    require_equal(calls,[(123,{"expected_start":987654})])
    for pid,start in ((124,None),(123,987655),(123,False)):
        refused(lambda:obj.terminate_bound(pid,start))
    obj.PR._host_pid_is_ours=lambda *_:False
    refused(lambda:obj.terminate_bound(123))
    obj.PR._host_pid_is_ours=lambda *_:True
    obj.B._verify_reapable_browser_daemon=lambda *_:False
    refused(lambda:obj.terminate_bound(123))
    require_equal(len(calls),1)


def t_native_events_accept_only_bound_browser_tools_and_keep_other_tools_refused():
    def event(server, tool, *, thread="thread", turn="turn"):
        return {"method":"item/completed", "params":{"threadId":thread,"turnId":turn,
            "item":{"type":"mcpToolCall","server":server,"tool":tool}}}
    recorder=W.Recorder(); recorder.thread_id="thread"; recorder.turn_id="turn"
    recorder.browser_tools=B.TOOLS
    for tool in B.TOOLS:
        recorder.on_event(event(B.SERVER,tool))
        require_equal(recorder.failure,"")
    recorder.on_event(event("foreign","browser_navigate",thread="other"))
    require_equal(recorder.failure,"")
    recorder.on_event(event(B.SERVER,"browser_click"))
    require_equal(recorder.failure,"native_tool_not_allowed")
    for server,tools in (("foreign",B.TOOLS),(B.SERVER,())):
        recorder=W.Recorder();recorder.thread_id="thread";recorder.turn_id="turn"
        recorder.browser_tools=tools
        recorder.on_event(event(server,"browser_navigate"))
        require_equal(recorder.failure,"native_tool_not_allowed")


def t_each_mcp_instance_requires_its_own_strict_terminal_proof():
    with tempfile.TemporaryDirectory() as directory:
        root=B.control_dir(directory);root.mkdir(mode=0o700)
        first="a"*24;second="b"*24
        def write(marker, **changes):
            ready={"task_id":"at-own","instance":marker,"pid":123}
            proof={"task_id":"at-own","instance":marker,"closed":True,"llm_calls":0,
                   "sources":["https://example.org/"+marker]}
            proof.update(changes)
            (root/("ready-"+marker+".json")).write_text(json.dumps(ready))
            (root/("closed-"+marker+".json")).write_text(json.dumps(proof))
        write(first);write(second)
        require_equal(set(B.closed_sources(directory,"at-own",timeout=0)),
                      {"https://example.org/"+first,"https://example.org/"+second})
        for changes in ({"task_id":"other"},{"instance":first},{"closed":1},
                        {"closed":False},{"llm_calls":False},{"llm_calls":1}):
            write(second,**changes)
            refused(lambda:B.closed_sources(directory,"at-own",timeout=0))
        write(second)
        (root/("closed-"+second+".json")).unlink()
        refused(lambda:B.closed_sources(directory,"at-own",timeout=0))
        B.request_stop(directory);require_equal((root/"stop").read_text(),"finish")
        B.request_stop(directory,cancelled=True);B.request_stop(directory)
        require_equal((root/"stop").read_text(),"cancel")


SERVER_FIXTURE = r'''
import argparse,asyncio,json,sys,signal
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent))
from hermes_browser import Browser,serve
p=argparse.ArgumentParser()
for key in ('hermes-source','hermes-python','browser-bin','chrome','task-id','workdir','fixture-origin'):
 p.add_argument('--'+key,required=True)
p.add_argument('--timeout',type=float,required=True)
a=p.parse_args()
b=Browser(a)
signal.signal(signal.SIGTERM,b.request_instance_stop)
signal.signal(signal.SIGINT,b.request_instance_stop)
# Exception exists only in this test script, not in the product CLI or schemas.
origin=a.fixture_origin
b.public_url=lambda url:url in {origin+'/alpha',origin+'/beta',origin+'/long',origin+'/blocked'}
try:asyncio.run(serve(b))
finally:b.close(cancelled=not b.done.is_set())
'''

DRIVER = r'''
import asyncio,contextlib,hashlib,json,os,sys,threading,time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
sys.path.append(str(Path.home()/'.solvio-hermes/venv/lib/python3.13/site-packages'))
import psutil
root=Path(sys.argv[1]); setup=json.loads((root/'setup.json').read_text())
blocked=threading.Event(); release=threading.Event(); checks=[]
def check(ok,name):
 if not ok:raise AssertionError(name)
 checks.append(name)
class Fixture(BaseHTTPRequestHandler):
 def do_GET(self):
  if self.path=='/blocked':blocked.set();release.wait(40)
  body=('<html><body><h1>'+self.path+'</h1>'+(''.join('<p>Paragraph %d %s</p>'%(i,'x'*140) for i in range(350))+'<p>Late qualification: NICHT geeignet</p>' if self.path=='/long' else '<p>Own task page</p>')+'</body></html>').encode()
  try:self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(body)
  except (BrokenPipeError,ConnectionResetError):pass
 def log_message(self,*a):pass
server=ThreadingHTTPServer(('127.0.0.1',0),Fixture)
threading.Thread(target=server.serve_forever,daemon=True).start()
origin='http://127.0.0.1:'+str(server.server_port)
def params(task):
 work=root/task;work.mkdir(mode=0o700,exist_ok=True)
 return StdioServerParameters(command=sys.executable,args=['-I','-B',str(root/'server.py'),
  '--hermes-source',setup['hermes'],'--hermes-python',setup['hermes_python'],'--browser-bin',setup['bin'],
  '--chrome',setup['chrome'],'--task-id',task,'--workdir',str(work),'--fixture-origin',origin,'--timeout','90'],
  env={'HOME':str(Path.home()),'PATH':'/usr/bin:/bin','CI':'1','OPENAI_API_KEY':'synthetic-do-not-forward','AGENT_BROWSER_ARGS':'--no-sandbox'})
async def call(session,name,args):
 result=await session.call_tool(name,args)
 value=json.loads(result.content[0].text)
 return result,value
async def main():
 own=[]
 async with stdio_client(params('alpha')) as aa,stdio_client(params('beta')) as bb:
  async with ClientSession(*aa) as a,ClientSession(*bb) as b:
   await a.initialize();await b.initialize()
   catalogue=(await a.list_tools()).tools
   names={t.name for t in catalogue}
   check(names=={'browser_navigate','browser_snapshot','browser_scroll','browser_back','browser_get_images'},'exact-five-tools')
   check(len({t.description for t in catalogue})==5,'tool-specific-read-instructions')
   check(all(t.annotations.read_only_hint and not t.annotations.destructive_hint for t in catalogue),'read-only-tool-catalogue')
   result,value=await call(a,'browser_navigate',{'url':origin+'/alpha','task_id':'beta'})
   check(result.is_error,'task-override-refused')
   result,value=await call(a,'browser_snapshot',{})
   check(result.is_error,'snapshot-before-navigation-refused')
   result,value=await call(a,'browser_navigate',{'url':'file:///etc/passwd'})
   check(result.is_error,'local-file-url-refused')
   result,value=await call(a,'browser_navigate',{'url':origin+'/alpha'})
   check(not result.is_error and value.get('success'),'alpha-open')
   result,value=await call(b,'browser_navigate',{'url':origin+'/beta'})
   check(not result.is_error and value.get('success'),'beta-open')
   result,value=await call(a,'browser_navigate',{'url':origin+'/long'})
   check(not result.is_error,'long-open')
   result,value=await call(a,'browser_snapshot',{'full':True})
   full=value['full_snapshot'];sid=full['snapshot_id'];chunks=[full['text']];offset=full['next_offset']
   while offset is not None:
    result,part=await call(a,'browser_snapshot',{'snapshot_id':sid,'offset':offset})
    check(not result.is_error,'complete-chunk')
    chunks.append(part['text']);offset=part['next_offset']
   text=''.join(chunks)
   check(len(text)>15000 and 'NICHT geeignet' in text,'late-negation-after-preview')
   check(hashlib.sha256(text.encode()).hexdigest()==full['sha256'],'full-snapshot-hash')
   result,value=await call(b,'browser_snapshot',{'snapshot_id':sid})
   check(result.is_error,'other-task-snapshot-refused')
   result,value=await call(a,'browser_snapshot',{'snapshot_id':sid,'offset':True})
   check(result.is_error,'boolean-offset-refused')
   result,value=await call(a,'browser_snapshot',{'snapshot_id':sid,'limit':8001})
   check(result.is_error,'oversized-chunk-refused')
   for task in ('alpha','beta'):
    ready=json.loads(next((root/task/'.solvio-browser').glob('ready-*.json')).read_text())
    sockets=list(Path(ready['root']).glob('agent-browser-h_*'))
    check(len(sockets)==1,'one-socket-owner-'+task)
    name=sockets[0].name.removeprefix('agent-browser-')
    p=psutil.Process(int((sockets[0]/(name+'.pid')).read_text()))
    procs=[p,*p.children(recursive=True)]
    own.extend((x.pid,x.create_time()) for x in procs)
    chrome=[x for x in procs if x.cmdline() and x.cmdline()[0]==setup['chrome']]
    check(len(chrome)==1,'fresh-browser-'+task)
    check(all('--no-sandbox' not in x.cmdline() for x in procs),'sandbox-bypass-cleared-'+task)
    check(all('synthetic-do-not-forward' not in str(x.environ()) for x in procs),'credential-not-forwarded-'+task)
   blocked_call=asyncio.create_task(a.call_tool('browser_navigate',{'url':origin+'/blocked'}))
   check(await asyncio.to_thread(blocked.wait,8),'real-blocking-request-started')
   begun=time.monotonic();blocked_call.cancel()
   with contextlib.suppress(asyncio.CancelledError):await blocked_call
   for _ in range(100):
    closed=list((root/'alpha/.solvio-browser').glob('closed-*.json'))
    if closed:break
    await asyncio.sleep(.05)
   proof=json.loads(closed[0].read_text())
   check(proof['closed'] is True and proof['cancelled'] is True,'blocked-tool-cleanup-confirmed')
   cancel_elapsed=time.monotonic()-begun
   print(json.dumps({'cancel_elapsed_s':cancel_elapsed}),flush=True)
   check(cancel_elapsed<2.5,'cleanup-within-terminal-proof-budget-measured')
   check(proof['llm_calls']==0,'no-model-call-alpha')
   check({'browser_navigate','browser_snapshot'}<=set(proof['successful_tools']),'actual-successful-browser-tools')
   result,value=await call(a,'browser_navigate',{'url':origin+'/alpha'})
   check(result.is_error,'cancelled-task-not-resurrected')
   result,value=await call(b,'browser_snapshot',{})
   check(not result.is_error and '/beta' in value['snapshot'],'beta-survives-alpha-cancel')
 release.set()
 for task in ('alpha','beta'):
  ready=json.loads(next((root/task/'.solvio-browser').glob('ready-*.json')).read_text())
  proof=json.loads(next((root/task/'.solvio-browser').glob('closed-*.json')).read_text())
  check(proof['closed'] and proof['llm_calls']==0,'terminal-cleanup-'+task)
  check(not Path(ready['root']).exists(),'no-socket-or-profile-'+task)
 for pid,created in own:
  try:check(psutil.Process(pid).create_time()!=created or psutil.Process(pid).status()==psutil.STATUS_ZOMBIE,'own-process-ended')
  except psutil.NoSuchProcess:pass
 (root/'proof.json').write_text(json.dumps({'passed':len(checks),'checks':checks,'model_calls':0,'cancel_elapsed':cancel_elapsed}))
try:asyncio.run(main())
finally:release.set();server.shutdown();server.server_close()
'''


SAME_TASK_DRIVER = DRIVER.split("async def main():")[0] + r'''
import signal
sys.path.insert(0,str(root))
from hermes_browser import request_stop,closed_sources
async def main():
 control=root/'shared/.solvio-browser';seen=set();instances=[];owned=[]
 async with contextlib.AsyncExitStack() as stack:
  async def connect():
   streams=await stack.enter_async_context(stdio_client(params('shared')))
   session=await stack.enter_async_context(ClientSession(*streams))
   await session.initialize();await session.list_tools()
   markers={p.name for p in control.glob('ready-*.json')}
   new=markers-seen;check(len(new)==1,'one-new-instance')
   seen.update(markers);row=json.loads((control/next(iter(new))).read_text())
   check(row['task_id']=='shared','same-task-binding')
   instances.append(row);process=psutil.Process(row['pid'])
   owned.append((process.pid,process.create_time()))
   return session,row,process.create_time()
  a,ar,_=await connect()
  result,value=await call(a,'browser_navigate',{'url':origin+'/alpha'})
  check(not result.is_error,'first-instance-open')
  b,br,born=await connect()
  # Signal exactly the fresh catalogue process, with its observed identity.
  process=psutil.Process(br['pid'])
  check(process.create_time()==born and str(root/'server.py') in process.cmdline(),'catalogue-process-bound-before-signal')
  process.send_signal(signal.SIGTERM)
  ended=control/('closed-'+br['instance']+'.json')
  for _ in range(60):
   if ended.exists():break
   await asyncio.sleep(.05)
  proof=json.loads(ended.read_text())
  check(proof['closed'] is True,'catalogue-instance-cleaned')
  check(not (control/'stop').exists(),'instance-signal-does-not-stop-whole-task')
  result,value=await call(a,'browser_snapshot',{})
  check(not result.is_error and '/alpha' in value['snapshot'],'execution-instance-survives-catalogue-signal')
  c,cr,_=await connect()
  check(len({row['root'] for row in instances})==len(instances),'same-task-instance-profiles-isolated')
  result,value=await call(c,'browser_navigate',{'url':origin+'/beta'})
  check(not result.is_error,'second-live-instance-open')
  request_stop(str(root/'shared'),cancelled=True)
  sources=await asyncio.to_thread(closed_sources,str(root/'shared'),'shared')
  check(set(sources)=={origin+'/alpha',origin+'/beta'},'explicit-task-stop-closes-both-live-instances')
 for row in instances:
  proof=json.loads((control/('closed-'+row['instance']+'.json')).read_text())
  check(proof['closed'] is True and not Path(row['root']).exists(),'instance-profile-and-sockets-ended')
  check(len(proof['successful_tools'])<=5 and set(proof['successful_tools'])<={'browser_navigate','browser_snapshot'},'bounded-success-tool-names')
 for pid,born in owned:
  try:check(psutil.Process(pid).create_time()!=born or psutil.Process(pid).status()==psutil.STATUS_ZOMBIE,'own-mcp-process-ended')
  except psutil.NoSuchProcess:pass
 (root/'proof.json').write_text(json.dumps({'passed':len(checks),'checks':checks,'model_calls':0}))
try:asyncio.run(main())
finally:release.set();server.shutdown();server.server_close()
'''


def t_real_mcp_same_task_catalogue_signal_keeps_execution_and_task_stop_closes_all():
    _run_real_fixture(SAME_TASK_DRIVER, minimum=20)


def t_real_mcp_two_browsers_complete_readback_and_cancel_blocked_tool():
    _run_real_fixture(DRIVER, minimum=30)


IDLE_DRIVER = DRIVER.split("async def main():")[0] + r'''
from types import SimpleNamespace
sys.path.insert(0,str(root))
from hermes_browser import Browser,closed_sources
work=root/'idle';work.mkdir(mode=0o700)
b=Browser(SimpleNamespace(task_id='idle',workdir=str(work),browser_bin=setup['bin'],
 chrome=setup['chrome'],hermes_source=setup['hermes'],hermes_python=setup['hermes_python'],timeout=60))
b.public_url=lambda url:url==origin+'/alpha'
# A one-second override races the real daemon's cold start and fails before
# navigation. Exercise its installed idle contract after the successful read.
try:
 check(os.environ['AGENT_BROWSER_IDLE_TIMEOUT_MS']=='60000','production-idle-contract')
 value=b.dispatch('browser_navigate',{'url':origin+'/alpha'})
 check(value.get('success'),'initial-local-page-read')
 bound=list(b.bound)
 check(bool(bound),'daemon-was-bound')
 deadline=time.monotonic()+65
 while time.monotonic()<deadline and any(psutil.pid_exists(pid) for pid in bound):time.sleep(.05)
 check(not any(psutil.pid_exists(pid) for pid in bound),'browser-idle-exit-observed')
 commands=[];original=b.original_command
 def observed_command(task,name,*a,**kw):
  commands.append(name);return original(task,name,*a,**kw)
 b.original_command=observed_command
 begun=time.monotonic()
 b.close()
 check('close' not in commands,'shutdown-must-not-relaunch-expired-browser-via-cli')
 check(time.monotonic()-begun<2.5,'idle-exit-cleanup-within-proof-budget')
 check(closed_sources(str(work),'idle',timeout=0)==[origin+'/alpha'],'idle-exit-retains-source-proof')
 check(not b.root.exists(),'idle-exit-removes-own-profile')
 (root/'proof.json').write_text(json.dumps({'passed':len(checks),'model_calls':b.llm_calls}))
finally:
 b.close(cancelled=True);server.shutdown();server.server_close()
'''


def t_real_browser_idle_exit_then_close_retains_confirmed_cleanup():
    # Retain the fixture's bootstrap budget plus the real 60-second idle wait.
    _run_real_fixture(IDLE_DRIVER, minimum=8, timeout=135)


def _run_real_fixture(driver, *, minimum, timeout=75):
    require(Path(PYTHON).is_file(), "Prepared isolated MCP runtime required")
    with tempfile.TemporaryDirectory(prefix="solvio-browser-fixture-") as directory:
        root=Path(directory)
        source=Path(B.__file__).read_bytes()
        (root/"hermes_browser.py").write_bytes(source)
        require_equal((root/"hermes_browser.py").read_bytes(),source)
        (root/"server.py").write_text(SERVER_FIXTURE)
        (root/"driver.py").write_text(driver)
        (root/"setup.json").write_text(json.dumps({"hermes":HERMES,"hermes_python":HERMES_PYTHON,
                                                "bin":BIN,"chrome":CHROME}))
        result=subprocess.run([PYTHON,"-I","-B",str(root/"driver.py"),str(root)],
                              capture_output=True,text=True,timeout=timeout)
        error_lines=result.stderr.splitlines()
        require_equal(result.returncode,0,
                      (error_lines[-1] if error_lines else result.stdout[-300:])
                      + "\n" + result.stderr[-5000:]+result.stdout[-1000:])
        proof=json.loads((root/"proof.json").read_text())
        require(proof["passed"]>=minimum and proof["model_calls"]==0)


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
