"""run_task -> real worker/Hermes/private bridge -> real Core portal handler.

Only native RPC replies are synthetic. No model, account or productive portal
is contacted; workspace files and every ledger are temporary.
"""
import asyncio
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
import json
import hashlib
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
import test_agent_portal_cost_dispatch as P
import test_hermes_native as B
import test_native_core_tools as C
from solvio.agent_runtime import native_sessions as N, native_tools as T, cost_dispatch as D, specialists as SP
from solvio.agent_runtime.native_tool_bridge import NativeToolBridge
from solvio.specialists import hermes_native as H, native_task as NT

HELPER_SOURCE = """import csv
import json
import sys


def total(path, column):
    with open(path, newline='') as stream:
        return sum(float(row[column]) for row in csv.DictReader(stream))


if __name__ == '__main__':
    print(json.dumps({'sum': total(sys.argv[1], sys.argv[2])}))
"""

PEER = r'''
import hashlib,json,os,pathlib,sys,tomllib
root=pathlib.Path(__file__).parent
mode=(root/'mode').read_text()
home=pathlib.Path(os.environ['CODEX_HOME'])
def record(v):
 with (root/'observed.jsonl').open('a') as f:f.write(json.dumps(v)+'\n')
record({'argv':sys.argv[1:],'pid':os.getpid()})
if sys.argv[1:]==['login','status']:
 print('Logged in using ChatGPT',file=sys.stderr);raise SystemExit(0)
assert sys.argv[1]=='app-server'
def merge(a,b):
 for k,v in b.items():
  if isinstance(v,dict):merge(a.setdefault(k,{}),v)
  else:a[k]=v
overlay={}
args=sys.argv[2:]
for i,arg in enumerate(args):
 if arg=='-c':merge(overlay,tomllib.loads(args[i+1]))
 elif arg in ('--enable','--disable'):overlay.setdefault('features',{})[args[i+1]]=arg=='--enable'
U='task-turn-'+str(int((root/'counter').read_text())+1 if (root/'counter').exists() else 1)
# One native thread id per started thread: a second task in the same world binds its own.
threads=int((root/'threads').read_text()) if (root/'threads').exists() else 0
T='task-thread' if threads<=1 else 'task-thread-'+str(threads)
workspace=None
def send(v):print(json.dumps(v),flush=True)
def note(method,**params):send({'method':method,'params':{'threadId':T,'turnId':U,**params}})
def strict_violation(schema,path='$'):
 # Provider STRICT rule (measured 19.09.2026): every object lists all properties in required, closed.
 if not isinstance(schema,dict):return None
 if 'properties' in schema:
  missing=[k for k in schema['properties'] if k not in schema.get('required',[])]
  if missing:return "In context=%s, 'required' is required to be supplied and to be an array including every key in properties. Missing %r."%(path,missing[0])
  if schema.get('additionalProperties') is not False:return 'In context=%s, additionalProperties is required to be false.'%path
  for k,v in schema['properties'].items():
   found=strict_violation(v,path+'.'+k)
   if found:return found
 if 'items' in schema:return strict_violation(schema['items'],path+'[]')
 for option in schema.get('anyOf',[]):
  found=strict_violation(option,path+'<anyOf>')
  if found:return found
 return None
def finish():
 if mode=='callback_loss':os._exit(7)
 path=pathlib.Path(workspace)/('result-'+U+'.txt');path.write_text('checked synthetic result '+U)
 command='synthetic local file writer'
 output='checked synthetic result '+U
 if mode=='observation_limits':
  command='echo '+('x'*5000)
  output='🧪'*9000
 if mode=='observation_workspace_path':
  command='echo '+('x'*2397)
  output=('Traceback (most recent call last):\n  File "/private/tmp/isolated/native-task-workspaces/at-123/helper.py", line 7\nAssertionError\n').ljust(4826, ' ')
  note('item/completed',item={'type':'commandExecution','id':'short-command-'+U,'status':'completed','command':'echo '+('x'*2092),'aggregatedOutput':'second observation','exitCode':0})
 if mode=='observation_redaction':
  command="python -c \"print({'access_token': 'synthetic-sensitive-value'})\""
  output="{'refresh_token': 'synthetic-sensitive-value with spaces'}\nsecond private line"
 send({'method':'item/completed','params':{'threadId':'foreign-thread','turnId':U,'item':{'type':'commandExecution','id':'foreign-observation','status':'completed','command':'foreign command','aggregatedOutput':'FOREIGN OUTPUT','exitCode':0}}})
 note('item/completed',item={'type':'commandExecution','id':'exec-'+U,'status':'completed','command':command,'aggregatedOutput':output,'exitCode':0})
 note('item/completed',item={'type':'fileChange','id':'file-'+U,'status':'completed','changes':[{'path':str(path),'kind':{'type':'add'},'diff':'DO NOT RETAIN DIFF'}]})
 note('item/completed',item={'type':'webSearch','id':'web-'+U,'query':'official source','action':{'type':'openPage','url':'https://example.org/report'},'results':[{'url':'https://example.org/second'},{'url':'https://example.org/private?token=synthetic-sensitive-value'}]})
 if mode in ('observation_flood','result_flood'):
  for index in range(4 if mode=='observation_flood' else 2):
   note('item/completed',item={'type':'commandExecution','id':'flood-'+str(index),'status':'completed','command':'bounded command','aggregatedOutput':'x'*7900,'exitCode':0})
 if mode=='observation_sequence':
  for index in range(4):
   note('item/completed',item={'type':'commandExecution','id':'sequence-'+str(index),'status':'completed','command':'echo '+('x'*2397),'aggregatedOutput':str(index)+('y'*5299),'exitCode':0})
 if mode=='metadata_flood':
  for index in range(4):
   note('item/completed',item={'type':'webSearch','id':'large-web-'+str(index),'query':'source','action':{'type':'search'},'results':[{'url':'https://example.org/'+('x'*600)+str(index)+'/'+str(i)} for i in range(12)]})
 note('item/completed',item={'type':'dynamicToolCall','id':'call-'+U,'tool':'portal_list','namespace':None,'status':'completed','success':True})
 answer={'findings':['Portale aus dem lokalen Core gelesen'],'evidence':['Lokaler Werkzeugbeleg'],
  'assumptions':[],'uncertainties':[],'rejected_alternatives':[],'risk_notes':[],
  'confidence':'hoch','recommended_path':'Geprüfte Ergebnisdatei lesen',
  'files':[{'path':path.name,'requirement':'report'}]}
 if mode in ('helper_publish','helper_no_readback','helper_forbidden','helper_foreign_path','helper_unreadable','helper_bad_name','helper_key_word','helper_credential','helper_credential_shell'):
  tools=pathlib.Path(workspace)/'tools';tools.mkdir(exist_ok=True)
  source=(root/'helper_source').read_text()
  if mode=='helper_forbidden':source='import subprocess\n'+source
  if mode=='helper_key_word':source=source.replace('import sys\n','import sys\nCOLUMNS = {key: index for index, key in enumerate(("betrag", "datum"))}  # key: value\n'
    '\"\"\"Author: Max Mustermann\"\"\"\nclass Token:\n    secret = None\n    def __init__(self, token):\n        self.token = token\ndef get_token(line):\n    tokenizer = Token(line)\n    return tokenizer.token\ndef first_token(line):\n    token = get_token(line)\n    tokens = line.split()\n    for token in tokens:\n        return token  # parse the auth token\n    return ""\n')
  if mode=='helper_credential':source=source.replace('import sys\n','import sys\nDB_PASSWORD = "hunter2-4711"\n')
  (tools/'csv_sum.py').write_text(source)
  digest=hashlib.sha256(source.encode()).hexdigest()
  readback=digest+'  tools/csv_sum.py' if mode!='helper_no_readback' else 'checked'
  if mode=='helper_credential_shell':
   shell='#!/bin/sh\nexport TOKEN=hunter2xyz\nprintf %s "$TOKEN"\n'
   (tools/'env.sh').write_text(shell)
   readback+='\n'+hashlib.sha256(shell.encode()).hexdigest()+'  tools/env.sh'
  note('item/completed',item={'type':'commandExecution','id':'readback-'+U,'status':'completed','command':'shasum -a 256 tools/csv_sum.py','aggregatedOutput':readback,'exitCode':0})
  answer['helpers']=[{'path':'../outside.py' if mode=='helper_foreign_path' else 'tools/csv_sum.py','name':'csv_sum','purpose':'Summiert eine CSV-Spalte (Betrag)'}]
  if mode=='helper_credential_shell':answer['helpers'].append({'path':'tools/env.sh','name':'env','purpose':'Setzt eine Umgebungsvariable'})
  if mode=='helper_bad_name':
   (tools/'report_gen.py').write_text(source)
   answer['helpers']=[{'path':'tools/report_gen.py','name':'CSV- und Berichtsgenerator','purpose':'Erzeugt den Bericht'}]+answer['helpers']
  if mode=='helper_unreadable':
   (tools/'big.py').write_text('# '+'x'*(64*1024)+'\n')
   (tools/'link.py').symlink_to(tools/'csv_sum.py')
   answer['helpers']+=[{'path':'tools/big.py','name':'big','purpose':'zu gross'},{'path':'tools/link.py','name':'link','purpose':'ein Link'},{'path':'tools/fehlt.py','name':'fehlt','purpose':'Tippfehler'}]
 if mode=='helper_reuse':
  seeded=sorted((pathlib.Path(workspace)/'.solvio-helpers').glob('extension-v1-*/csv_sum.py'))
  rel=str(seeded[0].relative_to(workspace)) if seeded else 'MISSING'
  note('item/completed',item={'type':'commandExecution','id':'reuse-'+U,'status':'completed','command':'python3 '+rel+' data.csv betrag','aggregatedOutput':'{"sum": 3.0}','exitCode':0})
 if mode in ('multiple_requirements','duplicate_requirements'):
  answer['files'][0]['requirement']=['report','bundle' if mode=='multiple_requirements' else 'report']
 if mode=='result_flood':
  answer['findings']=['f'*1000]*12
  answer['evidence']=['e'*500]*8
 note('item/completed',item={'type':'agentMessage','id':'answer-'+U,'phase':'final_answer','text':json.dumps(answer)})
 thread=json.loads((root/'thread.json').read_text());thread['turns'].append({'id':U,'status':'completed','items':[],'error':None})
 (root/'thread.json').write_text(json.dumps(thread))
 note('turn/completed',turn=thread['turns'][-1])
for line in sys.stdin:
 req=json.loads(line);record(req);method=req.get('method');rid=req.get('id');p=req.get('params') or {}
 if method is None:
  if rid=='tool-request':
   record({'tool_response':req})
   finish()
  continue
 if rid is None:continue
 out={}
 if method=='initialize':out={'userAgent':'local-native-task-peer'}
 elif method=='account/read':out={'account':{'type':'chatgpt','planType':'pro'},'requiresOpenaiAuth':True}
 elif method=='account/rateLimits/read':out={'rateLimits':{'primary':{'usedPercent':1}}}
 elif method=='model/list':out={'data':[{'model':'gpt-local-protocol-test'}],'nextCursor':None}
 elif method=='config/read':
  base=tomllib.loads((home/'config.toml').read_text());effective=json.loads(json.dumps(base));merge(effective,overlay)
  effective['mcp_servers']={};effective['default_permissions']=None
  for v in effective.get('permissions',{}).values():
   v.update(description=None,extends=None,workspace_roots=None);v['filesystem']['glob_scan_max_depth']=None
   v['network'].update({k:None for k in ('proxy_url','enable_socks5','socks_url','enable_socks5_udp','allow_upstream_proxy','dangerously_allow_non_loopback_proxy','dangerously_allow_all_unix_sockets','mode','domains','unix_sockets','allow_local_binding','mitm')})
  features={'network_proxy':None,'auth_elicitation':True,'mentions_v2':True,'mcp_2026_07_28':False,'remote_control':False}
  features.update(effective.get('features',{}));effective['features']=features
  out={'config':effective,'layers':[{'name':{'type':'sessionFlags'},'config':overlay},{'name':{'type':'user'},'config':base}],'origins':{}}
  if mode=='config_policy':effective['permissions']['solvio-task']['network']['enabled']=True
 elif method=='skills/list':
  found=sorted((pathlib.Path(p['cwds'][0])/'.solvio-helpers').glob('extension-v1-*/csv_sum.py')) if mode=='helper_reuse' else []
  out={'data':[{'cwd':p['cwds'][0],'skills':[{'name':'csv_sum','path':str(f)} for f in found],'errors':[]}]}
 elif method in ('thread/start','thread/read','thread/resume'):
  if method=='thread/start':
   threads+=1;(root/'threads').write_text(str(threads));T='task-thread' if threads<=1 else 'task-thread-'+str(threads)
   thread={'id':T,'cwd':p['cwd'],'ephemeral':p['ephemeral'],'modelProvider':'openai','status':{'type':'idle'},'turns':[]}
   (root/'thread.json').write_text(json.dumps(thread))
  else:thread=json.loads((root/'thread.json').read_text());T=thread['id']
  workspace=thread['cwd']
  out={'thread':thread,'cwd':workspace,'model':'gpt-local-protocol-test','modelProvider':'openai',
   'approvalPolicy':'never','approvalsReviewer':'user','instructionSources':[],
   'activePermissionProfile':{'id':'solvio-task','extends':None},
   'sandbox':{'type':'workspaceWrite','writableRoots':[],'networkAccess':False,'excludeTmpdirEnvVar':True,'excludeSlashTmp':True}}
  if mode=='thread_policy':out['sandbox']['networkAccess']=True
 elif method=='turn/start':
  (root/'counter').write_text(U.removeprefix('task-turn-'));out={'turn':{'id':U,'status':'inProgress'}}
  schema_error=strict_violation(p.get('outputSchema'))
 elif method=='turn/interrupt':
  send({'id':rid,'result':{}});note('turn/completed',turn={'id':U,'status':'interrupted','items':[],'error':None});continue
 else:raise RuntimeError('unexpected RPC '+method)
 send({'id':rid,'result':out})
 if method=='turn/start':
  if schema_error:
   # Real behaviour (third Durchstich, 19.09.2026): the turn starts, the model API refuses the
   # response format, the turn ends without any agent message, tool call or token usage.
   note('turn/completed',turn={'id':U,'status':'failed','items':[],'error':{'codexErrorInfo':'other',
    'message':json.dumps({'type':'error','status':400,'error':{'type':'invalid_request_error','code':'invalid_json_schema',
    'param':'text.format.schema','message':"Invalid schema for response_format 'codex_output_schema': "+schema_error}})}})
   continue
  body={'threadId':T,'turnId':U,'callId':'call-'+U,'tool':'portal_list','namespace':None,'arguments':{}}
  if mode=='foreign_tool':body['tool']='portal_open'
  if mode=='foreign_thread':body['threadId']='foreign-thread'
  if mode=='foreign_turn':body['turnId']='foreign-turn'
  send({'id':'tool-request','method':'item/tool/call','params':body})
'''


@contextmanager
def world(mode="ok"):
    with P.world() as w, C.socket_root() as sockets:
        w.socket_root = sockets
        root = Path(w.ledger.path).parent.resolve()
        w.root = root
        w.workspace = root / "workspace"
        w.workspace.mkdir(mode=0o700)
        home = root / "native-home"
        home.mkdir(mode=0o700)
        (home / "config.toml").write_text(H.native_config_text("gpt-local-protocol-test"))
        (root / "mode").write_text(mode)
        (root / "helper_source").write_text(HELPER_SOURCE)
        binary = root / "codex-local"
        binary.write_text("#!" + str(Path(sys.executable).resolve()) + " -ISB\n" + PEER)
        binary.chmod(0o700)
        # Preserve all actual worker/helper bytes in a disjoint, same-layout
        # source directory; no Documents filesystem latency in child startup.
        source = Path(H.__file__).parent
        staged = root / "worker-source/src/solvio/specialists"
        staged.mkdir(parents=True)
        for name in ("hermes_native_worker.py", "native_task_profile.py", "native_work_policy.py", "native_home_policy.py", "native_tool_wire.py"):
            (staged / name).write_bytes((source / name).read_bytes())
        w.config = H.NativeResearchConfig(B.HERMES_PYTHON, B.HERMES_SOURCE, str(binary), str(home),
                                          "gpt-local-protocol-test", 4.0, 1)
        w.sessions = N.NativeSessions(w.ledger, authority=w.authority)
        w.session = w.sessions.bind(task_id=w.task, run_id=w.run, provider="codex", profile="worker/codex",
            policy_digest=NT.task_policy(w.config), workspace=str(w.workspace))
        w.request = SP.SpecialistRequest("worker/codex", "Lies lokale Portale und erstelle einen Bericht.", "", run_id=w.run)
        w.continuation = H.NativeContinuation(w.sessions, w.session.session_id)
        original = H.worker_invocation
        def invocation(config, workdir, **kwargs):
            actual = original(config, workdir, **kwargs)
            return replace(actual, argv=(*actual.argv[:2], str(staged / "hermes_native_worker.py"), *actual.argv[3:]))
        with patch.object(H, "worker_invocation", invocation):
            yield w


async def run(w, *, request=None, close_on_start=False, target=None):
    target = target or w
    adapter = T.NativeCoreTools(w.ledger, w.sessions, target.session.session_id, target.run, w.router)
    bridge = NativeToolBridge(adapter, socket_root=w.socket_root)
    w.last_bridge = bridge
    def event(value):
        if close_on_start and value["event"] == "started":
            # Same public bridge lifecycle as Core cancellation; don't fake its
            # callback or the service handler to simulate a lost IPC endpoint.
            asyncio.create_task(bridge.close())
    operation = getattr(target, "specialist_step", "") or "native-task:" + str(len(D.invocations(w.ledger, target.task)))
    with D.task_cost_scope(w.ledger, task_id=target.task, run_id=target.run, phase="specialist",
            operation_id=operation, quote_adapter=lambda *_: B.FREE):
        return await NT.run_task(request or target.request, config=w.config, continuation=target.continuation,
                                 bridge=bridge, on_event=event)


def native_task(w, suffix, *, objective="Summiere die Beträge der CSV-Datei und lege einen Bericht ab."):
    """A full native task on the shared temporary ledger: grant, specialist step,
    private workspace, bound session — what native_tasks.execute has at runtime."""
    import time
    from types import SimpleNamespace
    from solvio.agent_runtime import artifact_creation as A, store as S
    from solvio.agent_runtime.task_authority import CapabilityGrant, VerifiedTaskReceipt
    task = w.ledger.create_task(objective=objective, scope=S.SCOPE_TASK,
        created_origin="trusted_interactive_app", created_principal="owner:device")
    run = w.ledger.create_run(task_id=task.task_id)
    w.ledger.transition(run.run_id, S.PLANNING)
    w.ledger.transition(run.run_id, S.RUNNING)
    w.costs.configure(task.task_id)
    grant = w.authority.issue(task.task_id, run.run_id,
        receipt=VerifiedTaskReceipt("app_session", "test:verified-native-" + suffix, "owner:device"),
        capabilities=(CapabilityGrant("portal_list", 1), A.capability_grant()), expires_at=time.time() + 3600)
    step = w.ledger.create_step(run_id=run.run_id, seq=1, kind="specialist", specialist_profile="worker/codex")
    w.ledger.update_step(step.step_id, state="running")
    workspace = w.root / ("workspace-" + suffix)
    workspace.mkdir(mode=0o700)
    session = w.sessions.bind(task_id=task.task_id, run_id=run.run_id, provider="codex", profile="worker/codex",
                              policy_digest=NT.task_policy(w.config), workspace=str(workspace))
    return SimpleNamespace(task=task.task_id, run=run.run_id, grant=grant, specialist_step=step.step_id,
        workspace=workspace, session=session, owner="owner:device",
        request=SP.SpecialistRequest("worker/codex", objective, "", run_id=run.run_id),
        continuation=H.NativeContinuation(w.sessions, session.session_id))


async def finished_turn(w, target, mode):
    """One real worker turn plus the Core retention native_tasks.execute performs."""
    from solvio.agent_runtime import native_result_files as RF, native_tasks as NTK
    (w.root / "mode").write_text(mode)
    outcome = await run(w, target=target)
    require(outcome.result.usable, outcome.result.reason)
    NTK._retain_observation(w.sessions, target.session.session_id, target.run, target.specialist_step,
                            outcome, asyncio.Event())
    candidates = await RF.publish_helper_candidates(w.sessions, session_id=target.session.session_id,
        run_id=target.run, step_id=target.specialist_step, invocation_id=outcome.cost_invocation_id,
        native_thread_id=outcome.native_thread_id, native_turn_id=outcome.native_turn_id,
        helpers=outcome.native_helpers)
    return outcome, candidates


def succeed(w, target):
    from solvio.agent_runtime import store as S
    w.ledger.update_step(target.specialist_step, state="succeeded")
    w.ledger.transition(target.run, S.VERIFYING)
    w.ledger.transition(target.run, S.SUCCEEDED)


def methods(w, name):
    return B.method_rows(w.root, name)


async def t_two_real_workers_resume_same_native_session_and_call_existing_core_tool():
    with world() as w:
        first = await run(w)
        require(first.result.usable, first.result.reason)
        before = (w.workspace / first.native_files[0]).read_bytes()
        second = await run(w)
        require(second.result.usable, second.result.reason)
        require_equal((first.native_thread_id, second.native_thread_id), ("task-thread", "task-thread"))
        require_equal((first.native_turn_id, second.native_turn_id), ("task-turn-1", "task-turn-2"))
        require_equal(len(methods(w, "thread/start")), 1)
        require_equal(len(methods(w, "thread/read")), 1)
        require_equal(len(methods(w, "thread/resume")), 1)
        require_equal(len(methods(w, "turn/start")), 2)
        require_equal([t["name"] for t in methods(w, "thread/start")[0]["params"]["dynamicTools"]], ["portal_list"])
        require("dynamicTools" not in methods(w, "thread/resume")[0]["params"])
        for message in methods(w, "turn/start"):
            require_equal(message["params"]["permissions"], "solvio-task")
            require("sandboxPolicy" not in message["params"])
        require_equal((w.workspace / first.native_files[0]).read_bytes(), before)
        require_equal(first.native_file_requirements, (("result-task-turn-1.txt", "report"),))
        require_equal({v["kind"] for v in first.native_tool_receipts}, {"commandExecution", "fileChange", "dynamicToolCall", "webSearch"})
        observed = {v["kind"]: v for v in first.native_tool_receipts}
        require_equal(observed["commandExecution"]["command"], {"text": "synthetic local file writer", "original_chars": 27, "complete": True, "redacted": False})
        require_equal(observed["commandExecution"]["output"]["text"], before.decode())
        require(observed["commandExecution"]["output"]["complete"])
        require_equal(observed["fileChange"]["changes"][0]["path"]["text"], first.native_files[0])
        require_equal(observed["webSearch"]["urls"], ["https://example.org/report", "https://example.org/second"])
        require(not observed["webSearch"]["urls_complete"])
        require("DO NOT RETAIN DIFF" not in json.dumps(first.native_tool_receipts))
        require(w.accesses, "actual portal vault handler never ran")
        require_equal(len([v for v in D.invocations(w.ledger, w.task) if v["provider"] == "local.portal-catalog"]), 2)
        require_equal(w.costs.view(w.task)["counts"], {"settled": 4})
        require_equal(w.sessions.latest_turn(w.session.session_id).state, "terminal")
        replies = [row["tool_response"] for row in B.observed(w.root) if "tool_response" in row]
        require_equal(len(replies), 2)
        for reply in replies:
            require(reply["result"]["success"] is True)
            payload = json.loads(reply["result"]["contentItems"][0]["text"])
            require_equal(payload["state"], "succeeded")
            require_equal(len(payload["data"]["portale"]), len(P.P.BINDINGS))
        with w.ledger._open() as db:
            calls = db.execute("SELECT * FROM agent_native_tool_calls ORDER BY created_at").fetchall()
        require_equal(len(calls), 2)
        for call in calls:
            require_equal((call["session_id"], call["run_id"], call["grant_reference"], call["state"]),
                          (w.session.session_id, w.run, w.grant.reference, "completed"))
            require_equal(w.ledger.get_step(call["step_id"]).state, "succeeded")
            require_equal(w.sessions.turn(call["invocation_id"]).native_turn_id, call["native_turn_id"])
        require(not Path(w.last_bridge.endpoint).exists())


async def t_native_policy_mismatch_cannot_start_turn_or_core_tool():
    for mode in ("config_policy", "thread_policy"):
        with world(mode) as w:
            result = await run(w)
            require(not result.result.usable, mode)
            require_equal(methods(w, "turn/start"), [])
            require_equal(w.accesses, [])


async def t_forged_tool_thread_or_turn_never_reaches_core_or_silently_succeeds():
    for mode in ("foreign_tool", "foreign_thread", "foreign_turn"):
        with world(mode) as w:
            result = await run(w)
            require(not result.result.usable, mode)
            require_equal(w.accesses, [])
            require_equal(len(methods(w, "turn/start")), 1)


async def t_foreign_run_binding_fails_before_worker_dispatch():
    with world() as w:
        foreign = w.ledger.create_run(task_id=w.task)
        result = await run(w, request=replace(w.request, run_id=foreign.run_id))
        require(not result.result.usable)
        require_equal(B.observed(w.root), [])
        require_equal(D.invocations(w.ledger, w.task), [])
        require_equal(w.accesses, [])


async def t_native_peer_loss_after_real_core_callback_is_not_terminal_success_or_retry():
    with world("callback_loss") as w:
        result = await run(w)
        require(not result.result.usable)
        require(w.accesses, "expected Core effect before native peer loss")
        require_equal(w.sessions.latest_turn(w.session.session_id).state, "unknown")
        second = await run(w)
        require(not second.result.usable)
        require_equal(len(methods(w, "turn/start")), 1)
        require_equal(len([v for v in D.invocations(w.ledger, w.task) if v["provider"] == "local.portal-catalog"]), 1)


async def t_closed_private_bridge_cannot_be_reported_as_success():
    with world() as w:
        result = await run(w, close_on_start=True)
        require(not result.result.usable)
        require_equal(len(methods(w, "turn/start")), 1)
        require(not Path(w.last_bridge.endpoint).exists())


async def t_observed_text_limits_redaction_and_full_original_command_digest():
    for mode in ("observation_limits", "observation_redaction"):
        with world(mode) as w:
            result = await run(w)
            require(result.result.usable, result.result.reason)
            command = next(r for r in result.native_tool_receipts if r["kind"] == "commandExecution")
            original = "echo " + "x" * 5000 if mode == "observation_limits" else "python -c \"print({'access_token': 'synthetic-sensitive-value'})\""
            require_equal(command["command_sha256"], hashlib.sha256(original.encode()).hexdigest())
            require(not command["command"]["complete"])
            require(not command["output"]["complete"])
            require_equal(command["command"]["redacted"], mode == "observation_redaction")
            require_equal(command["output"]["redacted"], mode == "observation_redaction")
            encoded = json.dumps(result.native_tool_receipts, ensure_ascii=True)
            require(len(encoded.encode()) <= 24_000)
            require("synthetic-sensitive-value" not in encoded)
            require("FOREIGN OUTPUT" not in encoded)
            if mode == "observation_limits":
                require_equal(command["output"]["original_chars"], 9000)
                require(0 < len(command["output"]["text"]) < 9000)
            else:
                require_equal(command["output"]["text"], "<entfernt>")
            # A changed completeness flag, unexpected model-provided argument,
            # or missing-field masquerading as an empty output is not evidence.
            for mutate in (
                lambda r: r["command"].update(complete=True),
                lambda r: r.update(arguments={"claimed": "external success"}),
                lambda r: r["output"].update(original_chars=None, complete=True),
                lambda r: r["output"].update(complete=1),
            ):
                bad = json.loads(json.dumps(command)); mutate(bad)
                try:
                    NT._receipts([bad])
                except ValueError:
                    pass
                else:
                    raise AssertionError("invalid observation accepted")


async def t_workspace_traceback_and_moderate_commands_remain_complete_through_real_transport():
    with world('observation_workspace_path') as w:
        result = await run(w)
        require(result.result.usable, result.result.reason)
        commands = [r for r in result.native_tool_receipts if r['kind'] == 'commandExecution']
        require_equal([r['command']['original_chars'] for r in commands], [2097, 2402])
        for receipt in commands:
            require(receipt['command']['complete'])
            require(not receipt['command']['redacted'])
            require_equal(receipt['command_sha256'], hashlib.sha256(receipt['command']['text'].encode()).hexdigest())
        output = commands[-1]['output']
        require_equal(output['original_chars'], 4826)
        require_equal(len(output['text']), 4826)
        require(output['complete'])
        require(not output['redacted'])
        require('native-task-workspaces/at-123/helper.py' in output['text'])
        require(len(json.dumps(result.native_tool_receipts, ensure_ascii=True).encode()) < 24_000)


async def t_native_file_requirement_array_crosses_real_protocol_without_duplicate_claims():
    for mode in ('multiple_requirements', 'duplicate_requirements'):
        with world(mode) as w:
            result = await run(w)
            require_equal(len(methods(w, 'turn/start')), 1)
            if mode == 'multiple_requirements':
                require(result.result.usable, result.result.reason)
                require_equal(result.native_file_requirements,
                              (("result-task-turn-1.txt", ("report", "bundle")),))
                require_equal(len(result.native_files), 1)
            else:
                require(not result.result.usable)
                require_equal(result.native_files, ())
                require_equal(result.result.reason, 'native_result_invalid')


async def t_global_observation_budget_cannot_silently_claim_terminal_success():
    for mode in ("observation_flood", "result_flood", "observation_sequence", "metadata_flood"):
        with world(mode) as w:
            result = await run(w)
            require_equal(len(methods(w, "turn/start")), 1)
            if mode == 'metadata_flood':
                require(not result.result.usable)
                require_equal(result.native_tool_receipts, ())
                continue
            require(result.result.usable, result.result.reason)
            require(len(json.dumps(result.native_tool_receipts, ensure_ascii=True).encode()) <= 24_000)
            commands = [r for r in result.native_tool_receipts if r['kind'] == 'commandExecution']
            require(commands[-1]['command']['complete'])
            require(commands[-1]['output']['complete'])
            require_equal(len(commands[-1]['output']['text']), 5300 if mode == 'observation_sequence' else 7900)
            omitted = [r for r in commands[:-1] if not r['command']['text']]
            require(omitted, 'old text was not explicitly omitted within the bound')
            require_equal({r['kind'] for r in result.native_tool_receipts},
                          {'commandExecution', 'fileChange', 'webSearch', 'dynamicToolCall'})
            for receipt in omitted:
                require(not receipt['command']['complete'])
                require(not receipt['output']['complete'])
                require(receipt['command']['original_chars'] > 0)
                require(not receipt['command']['redacted'])
                require(not receipt['output']['redacted'])
                require_equal(NT._receipts([receipt]), (receipt,))
                changed = json.loads(json.dumps(receipt)); changed['command']['complete'] = True
                try:
                    NT._receipts([changed])
                except ValueError:
                    pass
                else:
                    raise AssertionError('omitted command claimed completeness')
            if mode == 'observation_sequence':
                require_equal(commands[-1]['command']['original_chars'], 2402)
                require_equal(commands[-1]['command_sha256'], hashlib.sha256(('echo '+'x'*2397).encode()).hexdigest())


def t_task_invocation_never_dispatches_an_endpoint_under_the_shared_temp_tree():
    """A /tmp endpoint is refused even when it is private and correctly named."""
    import os
    import tempfile
    with world() as w:
        digest = "a" * 64
        def refused(endpoint):
            try:
                NT.task_invocation(w.config, str(w.workspace), task_id=w.task, endpoint=endpoint, manifest_digest=digest)
            except ValueError as exc:
                require_equal(str(exc), "native_task_profile_invalid", endpoint)
                return
            require(False, "shared-temp endpoint accepted: " + endpoint)
        for shared in ("/tmp", "/private/tmp"):
            if Path(shared).is_dir():
                with tempfile.TemporaryDirectory(dir=shared) as folder:
                    os.chmod(folder, 0o700)
                    refused(str(Path(folder).resolve() / "core.sock"))
        with C.socket_root() as sockets:
            leaf = Path(sockets) / "leaf"
            leaf.mkdir(mode=0o700)
            accepted = NT.task_invocation(w.config, str(w.workspace), task_id=w.task,
                endpoint=str(leaf / "core.sock"), manifest_digest=digest)
            require("--core-tools-socket" in accepted.argv)
            os.chmod(leaf, 0o750)
            refused(str(leaf / "core.sock"))


# -- N8/C4 §3: helper publication, seeding, reuse and revocation ---------------


def helper_rows(w):
    with w.ledger._open() as db:
        return [dict(r) for r in db.execute("SELECT version_id,owner,family FROM agent_extension_versions")]


def readback_receipts(outcome):
    return [r for r in outcome.native_tool_receipts if r["kind"] == "commandExecution"
            and r["status"] == "completed" and "csv_sum.py" in r["command"]["text"]]


async def t_task_one_publishes_a_helper_only_from_a_succeeded_run_with_core_readback():
    import os
    import stat
    from solvio.agent_runtime import extension_versions as EV
    with world() as w:
        first = native_task(w, "one")
        outcome, candidates = await finished_turn(w, first, "helper_publish")
        require_equal(outcome.native_helpers, ({"path": "tools/csv_sum.py", "name": "csv_sum",
                                                "purpose": "Summiert eine CSV-Spalte (Betrag)"},))
        require_equal(len(candidates), 1)
        require_equal(readback_receipts(outcome)[0]["command"]["text"], "shasum -a 256 tools/csv_sum.py")
        versions = EV.helper_versions(w.ledger)
        # Before SUCCEEDED nothing is published, whatever the candidate says.
        try:
            versions.publish_helper(first.run, candidates[0])
        except ValueError as exc:
            require_equal(str(exc), "helper_origin_not_succeeded")
        else:
            raise AssertionError("helper published from a running origin")
        require_equal(helper_rows(w), [])
        succeed(w, first)
        version_id = versions.publish_helper(first.run, candidates[0])
        require(version_id.startswith("extension-v1-") and len(version_id) == 77, version_id)
        require_equal(version_id, EV.helper_version_id("owner:device",
            {"csv_sum.py": hashlib.sha256(HELPER_SOURCE.encode()).hexdigest()}))
        require_equal(helper_rows(w), [{"version_id": version_id, "owner": "owner:device", "family": "native_helper"}])
        available = versions.helpers_for("owner:device")
        require_equal([v.version_id for v in available], [version_id])
        require_equal(available[0].name, "csv_sum")
        require_equal(available[0].contents, {"csv_sum.py": HELPER_SOURCE.encode()})
        require_equal(available[0].origin_run_id, first.run)
        require_equal(versions.helpers_for("someone-else"), ())
        events = [e for e in w.ledger.events_for_run(first.run) if version_id in e.summary]
        require_equal(len(events), 1)
        require_equal(events[0].ref, version_id)
        require("csv_sum" in events[0].summary)
        # Publishing again is idempotent; the same bytes from ANOTHER run map onto
        # the same content-addressed version, with one row and one event per run.
        require_equal(versions.publish_helper(first.run, candidates[0]), version_id)
        third = native_task(w, "three")
        _, again = await finished_turn(w, third, "helper_publish")
        succeed(w, third)
        require_equal(versions.publish_helper(third.run, again[0]), version_id)
        require_equal(len(helper_rows(w)), 1)
        require_equal(len([e for e in w.ledger.events_for_run(third.run) if version_id in e.summary]), 1)
        # A tampered origin candidate retires the version without touching the row.
        artifact = next(a for a in w.ledger.artifacts_for_run(first.run) if a.artifact_id == candidates[0])
        path = Path(artifact.path)
        mode = stat.S_IMODE(path.stat().st_mode)
        require_equal(mode, 0o400)
        os.chmod(path, 0o600)
        path.write_bytes(path.read_bytes().replace(b"csv_sum", b"csv_SUM"))
        os.chmod(path, 0o400)
        # The first candidate is gone but the identical third one still proves the bytes.
        require_equal([v.origin_run_id for v in versions.helpers_for("owner:device")], [third.run])
        other = next(a for a in w.ledger.artifacts_for_run(third.run) if a.artifact_id == again[0])
        os.chmod(other.path, 0o600)
        Path(other.path).write_bytes(Path(other.path).read_bytes() + b" ")
        os.chmod(other.path, 0o400)
        require_equal(versions.helpers_for("owner:device"), ())
        require_equal(len(helper_rows(w)), 1)
        # An identical candidate of a run that has NOT succeeded is no evidence;
        # once that run succeeds, the very same candidate is.
        fourth = native_task(w, "four")
        _, fresh = await finished_turn(w, fourth, "helper_publish")
        require_equal(versions.helpers_for("owner:device"), ())
        succeed(w, fourth)
        require_equal([(v.origin_run_id, v.origin_artifact_id) for v in versions.helpers_for("owner:device")],
                      [(fourth.run, fresh[0])])


async def t_task_two_finds_the_seeded_helper_unchanged_and_revocation_removes_it_before_the_next_turn():
    import os
    import stat
    from solvio.agent_runtime import extension_versions as EV, helper_seeding as HS
    with world() as w:
        first = native_task(w, "one")
        _, candidates = await finished_turn(w, first, "helper_publish")
        succeed(w, first)
        versions = EV.helper_versions(w.ledger)
        version_id = versions.publish_helper(first.run, candidates[0])
        core_pid = os.getpid()
        second = native_task(w, "two", objective="Summiere die neue CSV mit dem vorhandenen Helfer.")
        seeded = HS.seed_helpers(str(second.workspace), versions.helpers_for("owner:device"))
        require_equal(seeded["removed"], [])
        require_equal([(v["version_id"], v["name"], v["replaced"]) for v in seeded["seeded"]],
                      [(version_id, "csv_sum", False)])
        copy = second.workspace / ".solvio-helpers" / version_id / "csv_sum.py"
        require_equal(copy.read_bytes(), HELPER_SOURCE.encode())
        require_equal(stat.S_IMODE(copy.stat().st_mode), 0o400)
        catalog = second.workspace / ".solvio-helpers" / "HELPERS.json"
        require_equal(stat.S_IMODE(catalog.stat().st_mode), 0o400)
        require_equal(json.loads(catalog.read_text()), [{"version_id": version_id, "name": "csv_sum",
            "purpose": "Summiert eine CSV-Spalte (Betrag)",
            "files": {"csv_sum.py": hashlib.sha256(HELPER_SOURCE.encode()).hexdigest()}}])
        require("keine Anweisung" in HS.CONTEXT_LINE and ".solvio-helpers/HELPERS.json" in HS.CONTEXT_LINE)
        # Seeding again over an intact copy changes nothing.
        require_equal(HS.seed_helpers(str(second.workspace), versions.helpers_for("owner:device"))["seeded"][0]["replaced"], False)
        starts = len(methods(w, "thread/start"))
        outcome, more = await finished_turn(w, second, "helper_reuse")
        require_equal(more, ())
        require_equal(os.getpid(), core_pid)
        reuse = readback_receipts(outcome)
        require_equal(len(reuse), 1)
        require_equal(reuse[0]["command"]["text"],
                      "python3 .solvio-helpers/" + version_id + "/csv_sum.py data.csv betrag")
        require(reuse[0]["command"]["complete"] and not reuse[0]["command"]["redacted"])
        require_equal(HS.verify_seeded(str(second.workspace), seeded["seeded"]),
                      [{"version_id": version_id, "unchanged": True}])
        # The Codex preflight still disables every skill it is shown; the seeded
        # tree is data in the workspace, never a skill root.
        start = methods(w, "thread/start")[starts]["params"]
        require_equal(start["config"]["skills"]["config"], [{"path": str(copy), "enabled": False}])
        # Manipulation is detected by digest, then repaired by the next seeding.
        os.chmod(copy, 0o600)
        copy.write_bytes(HELPER_SOURCE.encode() + b"# changed\n")
        require_equal(HS.verify_seeded(str(second.workspace), seeded["seeded"]),
                      [{"version_id": version_id, "unchanged": False}])
        (second.workspace / ".solvio-helpers" / version_id / "extra.txt").write_text("x")
        reseeded = HS.seed_helpers(str(second.workspace), versions.helpers_for("owner:device"))
        require_equal(reseeded["seeded"][0]["replaced"], True)
        require_equal(copy.read_bytes(), HELPER_SOURCE.encode())
        require_equal(stat.S_IMODE(copy.stat().st_mode), 0o400)
        extra = second.workspace / ".solvio-helpers" / version_id / "extra.txt"
        require(not extra.exists())
        require_equal(HS.verify_seeded(str(second.workspace), reseeded["seeded"]),
                      [{"version_id": version_id, "unchanged": True}])
        extra.write_text("added by the model")
        require_equal(HS.verify_seeded(str(second.workspace), reseeded["seeded"]),
                      [{"version_id": version_id, "unchanged": False}], "an added file is a change")
        extra.unlink()
        copy.unlink()
        require_equal(HS.verify_seeded(str(second.workspace), reseeded["seeded"]),
                      [{"version_id": version_id, "unchanged": False}], "a missing file is a change")
        HS.seed_helpers(str(second.workspace), versions.helpers_for("owner:device"))
        require_equal(copy.read_bytes(), HELPER_SOURCE.encode())
        # A directory that is not in the current version list is removed even
        # while other versions stay seeded (a helper revoked between two turns).
        stray = second.workspace / ".solvio-helpers" / ("extension-v1-" + "0" * 64)
        stray.mkdir()
        (stray / "old.py").write_text("print('stale')\n")
        (stray / "old.py").chmod(0o400)
        pruned = HS.seed_helpers(str(second.workspace), versions.helpers_for("owner:device"))
        require_equal(pruned["removed"], [stray.name])
        require(not stray.exists())
        require_equal([v["version_id"] for v in pruned["seeded"]], [version_id])
        require_equal([entry["version_id"] for entry in json.loads(catalog.read_text())], [version_id])
        require_equal(copy.read_bytes(), HELPER_SOURCE.encode())
        # Revocation between two turns of the same task: the copy is physically
        # gone before the next turn, and the model finds nothing to reuse.
        versions.revoke(version_id, "test_revoked")
        require_equal(versions.helpers_for("owner:device"), ())
        removed = HS.seed_helpers(str(second.workspace), versions.helpers_for("owner:device"))
        require_equal(removed, {"seeded": [], "removed": [version_id], "catalog_sha256": ""})
        require(not copy.exists())
        require(not (second.workspace / ".solvio-helpers").exists())
        third = native_task(w, "three")
        (w.root / "mode").write_text("helper_reuse")
        later = await run(w, target=third)
        require(later.result.usable, later.result.reason)
        require_equal([r["command"]["text"] for r in later.native_tool_receipts if r["item_id"].startswith("reuse-")],
                      ["python3 MISSING data.csv betrag"])
        require(version_id not in json.dumps(later.native_tool_receipts))


async def t_helpers_without_readback_or_with_forbidden_imports_are_never_published():
    from solvio.agent_runtime import extension_versions as EV
    with world() as w:
        versions = EV.helper_versions(w.ledger)
        for suffix, mode, reason in (("nr", "helper_no_readback", "helper_readback_missing"),
                                     ("fb", "helper_forbidden", "helper_static_check_failed")):
            target = native_task(w, suffix)
            outcome, candidates = await finished_turn(w, target, mode)
            require_equal(len(candidates), 1, mode)
            require_equal(len(outcome.native_helpers), 1)
            succeed(w, target)
            try:
                versions.publish_helper(target.run, candidates[0])
            except ValueError as exc:
                require_equal(str(exc), reason, mode)
            else:
                raise AssertionError("published without " + reason)
            report = versions.publish_helpers(target.run)
            require_equal(report, [{"artifact_id": candidates[0], "version_id": "", "reason": reason}])
            require(any(reason in e.summary for e in w.ledger.events_for_run(target.run)))
            require_equal(versions.helpers_for("owner:device"), ())
            require_equal(helper_rows(w), [])
        require_equal(w.ledger.get_run(target.run).state, "SUCCEEDED")


async def t_unreadable_helper_declarations_are_rejected_one_by_one_and_leave_the_result_intact():
    """§3.2: a declared file the Core cannot take as a candidate (over
    MAX_BYTES, a symlink, a missing path) is rejected WITH its reason as an
    event on the run; the valid declaration beside it still becomes a
    candidate and the result files stay published — nothing raises."""
    from solvio.agent_runtime import extension_versions as EV
    with world() as w:
        target = native_task(w, "ur")
        outcome, candidates = await finished_turn(w, target, "helper_unreadable")
        require_equal([h["path"] for h in outcome.native_helpers],
                      ["tools/csv_sum.py", "tools/big.py", "tools/link.py", "tools/fehlt.py"])
        require_equal(len(candidates), 1, "only the readable declaration is a candidate")
        artifact = next(a for a in w.ledger.artifacts_for_run(target.run) if a.artifact_id == candidates[0])
        require_equal(json.loads(Path(artifact.path).read_text())["path"], "tools/csv_sum.py")
        # (Result-file publication itself is native_tasks.execute's work — the
        # product path is covered in test_native_task_entry; here the outcome
        # still names its deliverable and the turn is terminal/completed.)
        require_equal(outcome.native_files, ("result-task-turn-1.txt",))
        require_equal(w.sessions.latest_turn(target.session.session_id).state, "terminal")
        events = [e for e in w.ledger.events_for_run(target.run) if "nicht als Kandidat" in e.summary]
        require_equal([(e.kind, e.step_id) for e in events], [("helper_published", target.specialist_step)] * 3)
        summaries = "\n".join(e.summary for e in events)
        # A symlink is refused by the kernel (O_NOFOLLOW → ELOOP), i.e. unreadable
        # like a missing file; the oversize file is a regular-file bound refusal.
        for path, reason in (("tools/big.py", "native_result_file_invalid"), ("tools/link.py", "helper_file_unreadable"),
                             ("tools/fehlt.py", "helper_file_unreadable")):
            require(any(path in e.summary and reason in e.summary for e in events), path + ": " + summaries)
        succeed(w, target)
        report = EV.helper_versions(w.ledger).publish_helpers(target.run)
        require_equal([r["reason"] for r in report], [""], "the valid candidate must still publish")


async def t_a_helper_declaration_the_worker_cannot_hand_over_falls_alone_and_the_result_survives():
    """Measured 19.09.2026 11:30 (third real Durchstich, attempt 3): the Codex
    worker read the docs, called portal_list, wrote both files and declared its
    helper as "CSV- und Berichtsgenerator" — the strict whole-list check made the
    result native_result_invalid, the run ended `no_result`, every file lost.
    Through the real transport: the bad declaration is a rejection with its
    reason, the valid one beside it a candidate, files and result intact."""
    with world() as w:
        target = native_task(w, "bn")
        outcome, candidates = await finished_turn(w, target, "helper_bad_name")
        require(outcome.result.ok, outcome.result.reason)
        require_equal(outcome.native_files, ("result-task-turn-1.txt",))
        require_equal([h["path"] for h in outcome.native_helpers], ["tools/csv_sum.py"])
        require_equal(outcome.native_helper_rejections, ((0, "helper_declaration_invalid_name"),))
        require_equal(len(candidates), 1, "the valid declaration beside the refused one must still be a candidate")
        require_equal(w.sessions.latest_turn(target.session.session_id).state, "terminal")


async def t_helper_code_with_the_word_key_is_a_candidate_and_an_assigned_password_is_refused_alone():
    """Measured 19.09.2026 (third real Durchstich, attempt 5): the statement heuristic of
    the memory firewall ("a credential WORD plus any colon") refused whole records of
    tool material — code and documentation always contain both. Helper code with
    `key: index` is a candidate; a helper carrying `password = "…"` is refused as THIS
    candidate with its reason, the result and the run untouched."""
    from solvio.agent_runtime import extension_versions as EV
    with world() as w:
        # Review round 10, W10-1: `return token`, `class Token:`, `for token in tokens:`,
        # `tokens = line.split()`, `Author:` are code, never a credential shape.
        target = native_task(w, "kw")
        outcome, candidates = await finished_turn(w, target, "helper_key_word")
        require(outcome.result.ok, outcome.result.reason)
        require_equal(len(candidates), 1, "helper code with the word key/token must be a candidate")
    with world() as w:
        target = native_task(w, "cr")
        outcome, candidates = await finished_turn(w, target, "helper_credential")
        require(outcome.result.ok, outcome.result.reason)
        require_equal(outcome.native_files, ("result-task-turn-1.txt",))
        require_equal(candidates, ())
        events = [e.summary for e in w.ledger.events_for_run(target.run) if "nicht als Kandidat" in e.summary]
        require_equal(events, ["Helfer nicht als Kandidat übernommen (tools/csv_sum.py): helper_credential_shape."])
        require("hunter2" not in "\n".join(json.dumps(a.__dict__, default=str) for a in w.ledger.artifacts_for_run(target.run)))
    # Review round 12, H12-4: Python knows identifiers as values (`self.token = token` is code,
    # in the key_word fixture above); a SHELL helper does not — `export TOKEN=hunter2xyz` is
    # refused alone, the Python candidate beside it survives.
    with world() as w:
        target = native_task(w, "cs")
        outcome, candidates = await finished_turn(w, target, "helper_credential_shell")
        require(outcome.result.ok, outcome.result.reason)
        require_equal([h["path"] for h in outcome.native_helpers], ["tools/csv_sum.py", "tools/env.sh"])
        require_equal(len(candidates), 1, "the Python helper must stay a candidate")
        events = [e.summary for e in w.ledger.events_for_run(target.run) if "nicht als Kandidat" in e.summary]
        require_equal(events, ["Helfer nicht als Kandidat übernommen (tools/env.sh): helper_credential_shape."])
        require("hunter2" not in "\n".join(json.dumps(a.__dict__, default=str) for a in w.ledger.artifacts_for_run(target.run)))


async def t_helper_declaration_with_a_foreign_path_is_refused_alone_and_never_becomes_a_candidate():
    """A declared path outside the workspace is a refused declaration
    (helper_declaration_invalid_path), not a failed result: nothing was written
    there (the sandbox), nothing is read from there (no candidate)."""
    with world("helper_foreign_path") as w:
        target = native_task(w, "fp")
        outcome, candidates = await finished_turn(w, target, "helper_foreign_path")
        require(outcome.result.ok, outcome.result.reason)
        require_equal(outcome.native_files, ("result-task-turn-1.txt",))
        require_equal(outcome.native_helpers, (), "a foreign path must never be a helper candidate")
        require_equal(outcome.native_helper_rejections, ((0, "helper_declaration_invalid_path"),))
        require_equal(candidates, ())


def t_receipts_accept_dynamic_tool_calls_only_for_manifest_tools():
    base = {"kind": "dynamicToolCall", "item_id": "call-1", "status": "completed", "success": True}
    for tool in ("portal_list", "result_files_list"):
        require_equal(NT._receipts([dict(base, tool=tool)]), (dict(base, tool=tool),))
    for tool in ("memory_recall", "secret_list", "portal_open", "", None):
        try:
            NT._receipts([dict(base, tool=tool)])
        except ValueError as exc:
            require_equal(str(exc), "native_task_receipt_invalid", tool)
        else:
            raise AssertionError("foreign dynamic tool receipt accepted: " + str(tool))


def t_task_policy_digest_binds_the_tool_manifest_version():
    from unittest.mock import patch
    from solvio.agent_runtime import native_tools as NTOOLS
    with world() as w:
        current = NT.task_policy(w.config)
        # S1 reads plus the personal owner-task overview (28.09.2026).
        require_equal(NT.tools_description(), "Core-owned task-granted calendar_find_availability v1, "
                      "calendar_get_event v1, calendar_list_events v1, calendar_search_events v1, "
                      "gmail_list_recent v1, gmail_read_message v1, gmail_read_thread v1, gmail_search v1, "
                      "owner_task_overview v1, portal_list v1, result_files_list v1")
        without_overview = {k: v for k, v in NTOOLS.TOOLS.items() if k != "owner_task_overview"}
        with patch.dict(NTOOLS.TOOLS, without_overview, clear=True):
            require(NT.task_policy(w.config) != current, "adding overview must invalidate the old manifest binding")
        with patch.dict(NTOOLS.TOOLS, {"portal_list": 1}, clear=True):
            require_equal(NT.tools_description(), "Core-owned task-granted portal_list v1")
            previous = NT.task_policy(w.config)
        require(previous != current, "a changed manifest must change the policy digest")
        require_equal(NT.task_policy(w.config), current)


def _conforms(schema, value):
    """Minimal structural check of the closed schemas this worker hands the
    app server (object/array/string, additionalProperties False, required,
    bounds, enum, anyOf) — enough to prove what the outputSchema admits."""
    if "anyOf" in schema:
        return any(_conforms(option, value) for option in schema["anyOf"])
    kind = schema.get("type")
    if kind == "object":
        if type(value) is not dict:
            return False
        if schema.get("additionalProperties") is False and set(value) - set(schema.get("properties", {})):
            return False
        if any(key not in value for key in schema.get("required", ())):
            return False
        return all(_conforms(schema["properties"][key], item) for key, item in value.items()
                   if key in schema.get("properties", {}))
    if kind == "array":
        if type(value) is not list or len(value) > schema.get("maxItems", len(value)) \
                or len(value) < schema.get("minItems", 0):
            return False
        return all(_conforms(schema["items"], item) for item in value)
    if kind == "string":
        return (type(value) is str and schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", len(value))
                and ("enum" not in schema or value in schema["enum"]))
    return False


def _strict_violation(schema, path="$"):
    """The provider's STRICT structured-output rule as measured on the third real
    Durchstich (19.09.2026 10:29, HTTP 400 `invalid_json_schema`): every object
    lists ALL its properties in `required` and is closed — at every level."""
    if not isinstance(schema, dict):
        return None
    if "properties" in schema:
        missing = [key for key in schema["properties"] if key not in schema.get("required", [])]
        if missing:
            return f"{path}: required misses {missing}"
        if schema.get("additionalProperties") is not False:
            return f"{path}: not closed"
        for key, value in schema["properties"].items():
            found = _strict_violation(value, path + "." + key)
            if found:
                return found
    if "items" in schema:
        return _strict_violation(schema["items"], path + "[]")
    for option in schema.get("anyOf", []):
        found = _strict_violation(option, path + "<anyOf>")
        if found:
            return found
    return None


def t_worker_output_schemas_satisfy_the_provider_strict_rule():
    """Measured 19.09.2026 10:29 (third real Durchstich, attempt 2): the task
    worker's outputSchema carried `helpers` as a JSON-Schema-optional property;
    the provider's strict structured output answered HTTP 400 `invalid_json_schema`
    "Missing 'helpers'" before any model work — every Codex worker turn was dead
    while 5332 tests were green. JSON-Schema-optional is not provider-optional:
    an optional field is a REQUIRED field with an empty value."""
    from solvio.specialists.hermes_native_worker import RESULT_SCHEMA
    from solvio.specialists.native_task_profile import result_schema
    require(_strict_violation(RESULT_SCHEMA) is None, _strict_violation(RESULT_SCHEMA))
    task_schema = result_schema(RESULT_SCHEMA)
    require(_strict_violation(task_schema) is None, _strict_violation(task_schema))
    require(set(task_schema["required"]) == set(task_schema["properties"]), "top level drifted")
    # The rule detector itself must see the measured defect.
    broken = json.loads(json.dumps(task_schema))
    broken["required"].remove("helpers")
    require_equal(_strict_violation(broken), "$: required misses ['helpers']")


async def t_the_task_output_schema_handed_to_the_app_server_admits_the_helper_declaration():
    """N8/C4 §3.2/§3.5: the prompt asks the Codex worker to name a helper in
    `helpers[]`, so the CLOSED outputSchema of `turn/start` must admit that
    field (≤ 4, {path, name, purpose} with helper_check bounds) — otherwise no
    Codex helper can ever be published. Unknown fields stay refused. Under the
    provider's strict rule the field is REQUIRED; "no helper" is `[]`, and a
    result that omits the key is refused (the worker always emits the list)."""
    from solvio.agent_runtime import helper_check as HC
    with world() as w:
        outcome = await run(w)
        require(outcome.result.usable, outcome.result.reason)
        schema = methods(w, "turn/start")[0]["params"]["outputSchema"]
        require(schema["additionalProperties"] is False)
        require("files" in schema["required"] and "helpers" in schema["required"],
                "helpers must be required (provider strict rule), empty list = none")
        base = {"findings": ["x"], "evidence": [], "assumptions": [], "uncertainties": [], "rejected_alternatives": [],
                "risk_notes": [], "recommended_path": "fertig", "confidence": "hoch",
                "files": [{"path": "bericht.txt", "requirement": "R1"}]}
        declaration = {"path": "tools/csv_sum.py", "name": "csv_sum", "purpose": "Summiert eine CSV-Spalte (Betrag)"}
        require(not _conforms(schema, base), "a result without the helpers key must not conform (strict)")
        require(_conforms(schema, dict(base, helpers=[])), "the empty declaration list (= no helper) is refused")
        base = dict(base, helpers=[])
        require(_conforms(schema, base), "the base result no longer fits its own schema")
        require(_conforms(schema, dict(base, helpers=[declaration])), "a helper declaration is refused by the schema")
        require(not _conforms(schema, dict(base, helpers=[dict(declaration, extra="x")])), "a foreign helper field passed")
        require(not _conforms(schema, dict(base, helpers=[{"path": "tools/csv_sum.py", "name": "csv_sum"}])), "purpose is required")
        require(not _conforms(schema, dict(base, helpers=[declaration] * (HC.MAX_HELPERS + 1))), "the helper count is unbounded")
        require(not _conforms(schema, dict(base, helpers=[dict(declaration, purpose="p" * (HC.MAX_PURPOSE + 1))])))
        require(not _conforms(schema, dict(base, helpers=[dict(declaration, name="n" * (HC.MAX_NAME + 1))])))
        require(not _conforms(schema, dict(base, helpers=[dict(declaration, path="p" * (HC.MAX_PATH + 1))])))
        require(not _conforms(schema, dict(base, memory=[])), "the schema is no longer closed")
        # The bounds are the Core's (helper_check); the worker module repeats
        # them because it runs standalone — they must not drift apart.
        items = schema["properties"]["helpers"]["items"]["properties"]
        require_equal((schema["properties"]["helpers"]["maxItems"], items["path"]["maxLength"],
                       items["name"]["maxLength"], items["purpose"]["maxLength"]),
                      (HC.MAX_HELPERS, HC.MAX_PATH, HC.MAX_NAME, HC.MAX_PURPOSE))


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
