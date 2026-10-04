"""Real installed Hermes Session/Client and launcher; Codex is a local RPC process.

No provider inference, native login, productive stores, or credential copying.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import tomllib
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from solvio.agent_runtime import costs as C, cost_dispatch as D, specialists as SP, store as S
from solvio.specialists import hermes_native as N, launcher as L, providers as P

HERMES_SOURCE = str(Path.home() / ".solvio-hermes/src")
HERMES_PYTHON = str(Path.home() / ".solvio-hermes/venv/bin/python")
FREE = D.CostQuote(0, C.CostEvidence("free_local", "test:local-jsonrpc-process"))

RPC_PROGRAM = r'''
import json, os, pathlib, subprocess, sys, time, tomllib
root = pathlib.Path(__file__).parent
mode = (root / 'mode').read_text()
home = pathlib.Path(os.environ['CODEX_HOME'])
def record(value):
    with (root / 'observed.jsonl').open('a') as f:
        f.write(json.dumps(value) + '\n')
record({'argv':sys.argv[1:], 'home':str(home), 'pid':os.getpid(),
        'leaks':[k for k in os.environ if k in ('OPENAI_API_KEY','ANTHROPIC_API_KEY',
          'HERMES_KANBAN_TASK','OPENAI_BASE_URL','HERMES_HOME')]})
if sys.argv[1:]==['login','status']:
    print('Not logged in' if mode=='login_out' else 'Logged in using ChatGPT', file=sys.stderr)
    raise SystemExit(1 if mode=='login_out' else 0)
assert sys.argv[1]=='app-server'
T='local-thread'; U='local-turn'; model='gpt-local-protocol-test'
def send(data):
    print(json.dumps(data), flush=True)
def note(method, **params):
    send({'method':method, 'params':{'threadId':T,'turnId':U,**params}})
def item(kind, **fields):
    note('item/completed',item={'type':kind,'id':'item-local',**fields})
def strict_violation(schema, path='$'):
    # The provider's STRICT structured output (measured 19.09.2026, real Codex worker turn):
    # every object lists ALL its properties in `required` and is closed. Same wording as the
    # real 400 so a failing suite reads like the real thing.
    if not isinstance(schema, dict): return None
    if 'properties' in schema:
        missing=[k for k in schema['properties'] if k not in schema.get('required', [])]
        if missing: return "In context=%s, 'required' is required to be supplied and to be an array including every key in properties. Missing %r." % (path, missing[0])
        if schema.get('additionalProperties') is not False: return 'In context=%s, additionalProperties is required to be false.' % path
        for k,v in schema['properties'].items():
            found=strict_violation(v, path+'.'+k)
            if found: return found
    if 'items' in schema: return strict_violation(schema['items'], path+'[]')
    for option in schema.get('anyOf', []):
        found=strict_violation(option, path+'<anyOf>')
        if found: return found
    return None
for line in sys.stdin:
    request=json.loads(line); record(request)
    method=request.get('method'); params=request.get('params') or {}; rid=request.get('id')
    if method is None or rid is None: continue
    response={}
    if method=='initialize': response={'userAgent':'local-protocol-test'}
    elif method=='account/read':
        response={'account':{'type':'apiKey'} if mode=='account_api' else {'type':'chatgpt','planType':'pro'},
                  'requiresOpenaiAuth':True}
    elif method=='account/rateLimits/read':
        response={'rateLimits':{'primary':{'usedPercent':100 if mode=='quota_before' else 3},
           'secondary':{'usedPercent':7},'credits':{'hasCredits':True,'unlimited':False,'balance':'5'}}}
        if mode=='quota_unknown': response={'rateLimits':{'primary':None,'secondary':None}}
        if mode=='quota_malformed': response={'rateLimits':{'primary':{'usedPercent':False}}}
    elif method=='model/list': response={'data':[] if mode=='model_unavailable' else [{'id':model,'model':model}],'nextCursor':None}
    elif method=='config/read':
        config=tomllib.loads((home/'config.toml').read_text())
        overrides=tomllib.loads('\n'.join(sys.argv[i+1] for i,v in enumerate(sys.argv[:-1]) if v=='-c'))
        if mode=='hidden_mcp':config['mcp_servers']={'not-permitted':{'command':'never-run'}}
        # Real 0.147.0 normalizes absent MCP to an empty effective catalogue.
        effective={'mcp_servers':{},**config,**overrides}
        for entry in effective['mcp_servers'].values():
            entry.pop('env_vars',None);entry['environment_id']='local'
        response={'config':effective,'layers':[{'config':config},{'config':overrides}],'origins':{}}
        # The raw CLI layer retains its exact empty env_vars value.
        response['layers'][1]['config']=tomllib.loads('\n'.join(sys.argv[i+1] for i,v in enumerate(sys.argv[:-1]) if v=='-c'))
    elif method=='mcpServerStatus/list':
        response={'data':[{'name':'solvio-browser','tools':{name:{'annotations':{'readOnlyHint':True}}
            for name in ('browser_navigate','browser_snapshot','browser_scroll','browser_back','browser_get_images')}}],
            'nextCursor':None}
    elif method=='skills/list':
        response={'data':[{'cwd':params['cwds'][0], 'errors':[],
                  'skills':[{'path':str(root/'foreign-skill'/'SKILL.md'),'name':'foreign'}]}]}
    elif method=='thread/start':
        response={'thread':{'id':T},'model':'wrong-model' if mode=='wrong_model' else model,
            'modelProvider':'openai','cwd':params['cwd'],'sandbox':{'type':'readOnly','networkAccess':False},
            'approvalPolicy':'never','approvalsReviewer':'user','instructionSources':[]}
    elif method=='turn/start':
        response={'turn':{'id':U,'status':'inProgress'}}
        schema_error=strict_violation(params.get('outputSchema'))
    elif method=='turn/interrupt':
        send({'id':rid,'result':{}})
        if mode=='quota_no_completion': continue
        note('turn/completed',turn={'id':U,'status':'interrupted','error':None,'items':[]})
        continue
    else: raise RuntimeError('unexpected native call: '+str(method))
    send({'id':rid,'result':response})
    if method=='turn/start':
        if schema_error:
            # Real behaviour: the turn starts, the model API refuses the request format,
            # the turn ends without any agent message or token usage.
            note('turn/completed',turn={'id':U,'status':'failed','items':[],
                 'error':{'message':json.dumps({'type':'error','status':400,'error':{'type':'invalid_request_error',
                 'code':'invalid_json_schema','param':'text.format.schema',
                 'message':"Invalid schema for response_format 'codex_output_schema': "+schema_error}}),
                 'codexErrorInfo':'other'}})
            continue
        if mode in ('cancel', 'live_cancel'):
            child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])
            (root/'child.pid').write_text(str(child.pid))
            if mode=='live_cancel': item('webSearch',query='local cancellation proof')
            continue
        if mode=='disconnect': os._exit(7)
        if mode=='server_request':
            send({'id':'server-question','method':'mcpServer/elicitation/request',
                  'params':{'serverName':'hermes-tools','message':'must not auto accept'}})
            continue
        if mode=='unexpected_tool':
            item('commandExecution',command='never execute',status='completed',aggregatedOutput='')
            continue
        if mode in ('quota_turn', 'quota_no_completion'):
            note('error',error={'message':'Synthetic quota condition','codexErrorInfo':'usageLimitExceeded'},willRetry=True)
            continue
        source_results = [{'url':'https://example.org/result'},
                          {'url':'https://example.org/private?access_token=synthetic'}]
        if (root/'sources.json').exists():
            source_results = [{'url':url} for url in json.loads((root/'sources.json').read_text())]
        for i in range(100 if mode=='many_events' else 1):
            item('webSearch',query='sk-SYNTHETIC_NOT_A_REAL_KEY_123456789 query '+('x'*3000 if mode=='many_events' else ''),
                 action={'type':'openPage','url':'https://example.org/source'},
                 results=source_results)
        if mode in ('browser_paired','browser_unpaired','browser_duplicate'):
            # Metadata only: no MCP client or browser is launched by this peer.
            for i,tool in enumerate(('browser_navigate','browser_snapshot')):
                fields={'type':'mcpToolCall','id':'browser-'+str(i),'server':'solvio-browser','tool':tool,
                    'arguments':{'url':'https://example.org/synthetic-private-argument'},
                    'result':{'isError':True},'error':{'message':'Bearer synthetic-never-project'}}
                if mode!='browser_unpaired': note('item/started',item=fields)
                note('item/completed',item=fields)
                if mode=='browser_duplicate': note('item/completed',item=fields)
            send({'method':'item/completed','params':{'threadId':'foreign-thread','turnId':'foreign-turn',
                'item':{'type':'mcpToolCall','id':'foreign-browser','server':'solvio-browser','tool':'browser_snapshot'}}})
        note('thread/tokenUsage/updated',tokenUsage={'total':{'inputTokens':120,'cachedInputTokens':20,
             'outputTokens':30,'reasoningOutputTokens':10,'totalTokens':150}})
        if mode=='live_pause':
            while not (root/'release-live').exists(): time.sleep(0.01)
        answer=json.dumps({'findings':['Lokaler Protokollnachweis'],
            'evidence':['https://example.org/model-citation'], 'assumptions':[], 'uncertainties':[],
            'recommended_path':'Ergebnis lesen','rejected_alternatives':[],'risk_notes':[], 'confidence':'hoch'})
        if (root/'answer.json').exists(): answer=(root/'answer.json').read_text()
        if mode in ('unscoped_message', 'thread_only_message', 'foreign_message'):
            scope = {} if mode == 'unscoped_message' else {'threadId':T}
            if mode == 'foreign_message': scope = {'threadId':'foreign-thread','turnId':'foreign-turn'}
            send({'method':'item/completed', 'params':{**scope,
                'item':{'type':'agentMessage','id':'foreign-item','text':answer,'phase':'final_answer'}}})
        else:
            item('agentMessage',text=answer,phase='commentary' if mode=='commentary_only' else 'final_answer')
        if mode=='no_terminal': continue
        if mode=='wrong_turn':
            send({'method':'turn/completed','params':{'threadId':T,'turn':{'id':'foreign-turn',
                  'status':'completed','error':None,'items':[]}}})
            continue
        note('turn/completed',turn={'id':U,'status':'completed','error':None,'items':[]})
'''


MCP_READER_PEER = r'''
# Synthetic stdio MCP only. Never construct Browser or launch Chrome.
import hashlib,json,pathlib,signal,sys,time
root=pathlib.Path(__file__).parent
mode=(root/'browser-mode').read_text()
argv=sys.argv[1:]
def option(name):return argv[argv.index('--'+name)+1]
task=option('task-id');work=pathlib.Path(option('workdir'))
control=work/'.solvio-browser';control.mkdir(mode=0o700,exist_ok=True)
instance='a'*24
def record(value):
 with (root/'browser-observed.jsonl').open('a') as stream:stream.write(json.dumps(value)+'\n')
record({'event':'spawn','pid':__import__('os').getpid(),'argv':argv,'workdir':str(work),'workdir_mode':work.stat().st_mode & 0o077})
if mode!='missing_ready':
 (control/('ready-'+instance+'.json')).write_text(json.dumps({'task_id':task,'instance':instance,'pid':__import__('os').getpid()}))
snapshots={};sources=[];calls=0
text=('Öffentliche synthetische Ergebnisansicht. '*450+'\nSpäte Einschränkung: NICHT buchbar, Gepäck fehlt.')
def send(rid,result):print(json.dumps({'jsonrpc':'2.0','id':rid,'result':result}),flush=True)
def tool_result(value,error=False):return {'content':[{'type':'text','text':json.dumps(value)}],'isError':error}
def stopped(*_):raise SystemExit(0)
signal.signal(signal.SIGTERM,stopped);signal.signal(signal.SIGINT,stopped)
try:
 for line in sys.stdin:
  request=json.loads(line);rid=request.get('id');method=request.get('method');params=request.get('params') or {}
  record({'event':'rpc','method':method,'params':params})
  if rid is None:continue
  if method=='initialize':
   send(rid,{'protocolVersion':params['protocolVersion'],'capabilities':{'tools':{}},'serverInfo':{'name':'solvio-browser','version':'synthetic'}})
  elif method=='tools/list':
   tools=[]
   for name in ('browser_navigate','browser_snapshot','browser_scroll','browser_back','browser_get_images'):
    props={'url':{'type':'string'}} if name=='browser_navigate' else ({'full':{'type':'boolean'},'snapshot_id':{'type':'string'},'offset':{'type':'integer'},'limit':{'type':'integer'}} if name=='browser_snapshot' else {})
    tools.append({'name':name,'description':'Synthetic public reader','inputSchema':{'type':'object','properties':props,'additionalProperties':False},'annotations':{'readOnlyHint':mode!='catalog_not_readonly','destructiveHint':False}})
   if mode=='catalog_extra':tools.append({'name':'browser_click','inputSchema':{'type':'object'},'annotations':{'readOnlyHint':False}})
   send(rid,{'tools':tools})
  elif method=='tools/call':
   calls+=1;name=params['name'];args=params.get('arguments') or {}
   if name=='browser_navigate':
    if mode=='slow':time.sleep(.35)
    if mode=='request_timeout' and sources:
     print(json.dumps({'jsonrpc':'2.0','id':rid,'error':{'code':-32001,'message':'synthetic-timeout-never-forward'}}),flush=True);continue
    if mode=='hang':
     (root/'reader-blocked').write_text('blocked')
     while not (control/'stop').exists():time.sleep(.02)
     send(rid,tool_result({'error':'native_browser_closed'},True));continue
    if mode=='tool_error':
     send(rid,tool_result({'error':'Bearer synthetic-error-never-forward'},True));continue
    sources.append(args['url']);send(rid,tool_result({'success':True,'url':args['url'],'snapshot':'Compact synthetic preview'}))
   elif name=='browser_snapshot':
    digest=hashlib.sha256(text.encode()).hexdigest();sid=task+':'+digest
    if mode=='foreign_snapshot':sid='at-foreign:'+digest
    offset=args.get('offset',0);end=min(len(text),offset+2000)
    full={'success':True,'snapshot_id':sid,'sha256':'0'*64 if mode=='bad_hash' else digest,'characters':len(text),'offset':offset,'next_offset':end if end<len(text) else None,'text':text[offset:end]}
    if mode=='bad_offset' and offset:full['offset']=offset+1
    send(rid,tool_result({'success':True,'full_snapshot':full} if args.get('full') else full))
   else:send(rid,tool_result({'error':'native_browser_tool_forbidden'},True))
  elif method=='ping':send(rid,{})
  else:raise RuntimeError('Unexpected synthetic MCP method '+str(method))
finally:
 if mode!='missing_ready':
  proof={'task_id':task,'instance':instance,'closed':mode!='cleanup_unknown','llm_calls':0,'calls':calls,'sources':sources,'successful_tools':['browser_navigate','browser_snapshot']}
  (control/('closed-'+instance+'.json')).write_text(json.dumps(proof))
  record({'event':'closed','proof':proof,'stop':(control/'stop').read_text() if (control/'stop').exists() else ''})
'''


@contextmanager
def native_fixture(mode="ok", *, timeout=3.0, browser=False, preread=""):
    require(Path(HERMES_SOURCE, "agent/transports/codex_app_server_session.py").is_file(),
            "Installed Hermes is required for this real-transport suite")
    with tempfile.TemporaryDirectory(prefix="solvio-native-protocol-") as directory:
        root = Path(directory)
        home = root / "native-home"
        home.mkdir(mode=0o700)
        model = "gpt-local-protocol-test"
        (home / "config.toml").write_text(N.native_config_text(model))
        (root / "mode").write_text(mode)
        codex = root / "codex-local"
        # This peer uses stdlib only. Resolve the interpreter out of the
        # Documents venv and exclude site/.pth/user imports and bytecode writes.
        codex.write_text("#!" + str(Path(sys.executable).resolve()) + " -ISB\n" + RPC_PROGRAM)
        codex.chmod(0o700)
        # Documents file reads have stalled during native child startup. Keep
        # the real worker bytes, but stage them before its unchanged deadline.
        worker_source = Path(N.__file__).with_name("hermes_native_worker.py")
        worker_bytes = worker_source.read_bytes()
        worker_copy = root / "hermes_native_worker.py"
        worker_copy.write_bytes(worker_bytes)
        require_equal(worker_copy.read_bytes(), worker_bytes)
        research_source = worker_source.with_name("hermes_research_worker.py")
        research_copy = root / research_source.name
        research_copy.write_bytes(research_source.read_bytes())
        require_equal(research_copy.read_bytes(), research_source.read_bytes())
        # Standalone worker's exact stdlib sibling is part of its runtime.
        home_policy = worker_source.with_name("native_home_policy.py")
        policy_bytes = home_policy.read_bytes()
        (root / home_policy.name).write_bytes(policy_bytes)
        require_equal((root / home_policy.name).read_bytes(), policy_bytes)
        usage_source = worker_source.with_name("openai_usage.py")
        (root / usage_source.name).write_bytes(usage_source.read_bytes())
        browser_source = worker_source.with_name("hermes_browser.py")
        (root / browser_source.name).write_bytes(browser_source.read_bytes())
        reader_source = worker_source.with_name("hermes_browser_reader.py")
        if reader_source.is_file():
            (root / reader_source.name).write_bytes(reader_source.read_bytes())
        if preread:
            require(browser, "The synthetic preread fixture needs the configured browser runtime")
            (root / "browser-mode").write_text(preread)
            browser_copy = root / browser_source.name
            original = browser_copy.read_text()
            marker = 'if __name__ == "__main__":\n    main()'
            require(marker in original, "Staged browser main guard changed")
            browser_copy.write_text(original.replace(marker,
                'if __name__ == "__main__":\n    exec(' + repr(MCP_READER_PEER) + ')'))
        original_invocation = N.worker_invocation

        def local_worker_invocation(native_config, workdir, *, task_id=""):
            invocation = original_invocation(native_config, workdir, task_id=task_id)
            require_equal(invocation.argv[:3], ("-I", "-B", str(research_source)))
            return replace(invocation, argv=(*invocation.argv[:2], str(research_copy),
                                             *invocation.argv[3:]))

        config = N.NativeResearchConfig(HERMES_PYTHON, HERMES_SOURCE, str(codex),
                                       str(home), model, timeout, 1.5)
        if browser:
            config = replace(config,
                browser_python=str(Path.home() / ".solvio-nexus/runtimes/hermes-browser-20260917/bin/python"),
                browser_bin=str(Path.home() / ".solvio-nexus/runtimes/hermes-browser-20260917/bin/agent-browser-0.26.0"),
                browser_chrome="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        ledger = S.AgentRunLedger(str(root / "agent.sqlite3"))
        task = ledger.create_task(objective="Recherchiere mit Quellen", scope="research",
            created_origin="trusted_interactive_app", created_principal="owner:device")
        run = ledger.create_run(task_id=task.task_id)
        ledger.transition(run.run_id, S.PLANNING)
        ledger.transition(run.run_id, S.RUNNING)
        C.CostLedger(ledger).configure(task.task_id)
        request = SP.SpecialistRequest("researcher/hermes", task.objective, "", run_id=run.run_id)
        with patch.object(N, "worker_invocation", local_worker_invocation):
            yield root, config, ledger, task.task_id, run.run_id, request


def scoped(ledger, task, run, *, quote=FREE):
    return D.task_cost_scope(ledger, task_id=task, run_id=run, phase="specialist",
        operation_id="research:local", quote_adapter=(lambda *_: quote) if quote else None)


def observed(root):
    path = root / "observed.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def method_rows(root, method):
    return [r for r in observed(root) if r.get("method") == method]


def run_case(mode, *, timeout=3):
    async def go():
        with native_fixture(mode, timeout=timeout) as (root, config, ledger, task, run, request):
            with scoped(ledger, task, run):
                result = await N.run_research(request, config=config)
            return result, observed(root), D.invocations(ledger, task)
    return asyncio.run(go())


def t_real_transport_uses_same_auth_home_native_policies_and_durable_claim():
    async def go():
        with native_fixture() as (root, config, ledger, task, run, request):
            with scoped(ledger, task, run), patch.dict(os.environ, {
                "OPENAI_API_KEY":"synthetic-never-forward", "HERMES_KANBAN_TASK":"synthetic-never-forward"}):
                result = await N.run_research(request, config=config)
            require(result.result.usable, result.result.reason)
            require_equal(result.provider, "codex")
            require_equal(result.runtime, "hermes-codex-app-server")
            require_equal(result.native_thread_id, "local-thread")
            require_equal(result.native_turn_id, "local-turn")
            starts = [r for r in observed(root) if "argv" in r]
            require_equal(len(starts), 2)
            require_equal([r["home"] for r in starts], [config.codex_home]*2)
            require_equal([r["leaks"] for r in starts], [[], []])
            thread = method_rows(root, "thread/start")[0]["params"]
            require_equal(thread["sandbox"], "read-only")
            require_equal(thread["model"], config.model)
            require_equal(thread["approvalPolicy"], "never")
            require_equal(thread["config"]["web_search"], "live")
            require_equal(thread["config"]["skills"]["config"], [
                {"path":str(root / "foreign-skill/SKILL.md"), "enabled":False}])
            turn = method_rows(root, "turn/start")[0]["params"]
            require_equal(turn["sandboxPolicy"], {"type":"readOnly","networkAccess":False})
            require_equal(turn["model"], config.model)
            require_equal(turn["outputSchema"]["type"], "object")
            require(turn["outputSchema"]["additionalProperties"] is False)
            require_equal(method_rows(root, "account/read")[0]["params"], {"refreshToken":False})
            require(result.usage_reported)
            require("Native Websuche: https://example.org/source" in result.result.evidence)
            require(not any("access_token" in e for e in result.result.evidence))
            claims = D.invocations(ledger, task)
            require_equal(len(claims), 1)
            require_equal(claims[0]["state"], "finished")
            require_equal(C.CostLedger(ledger).view(task)["counts"], {"settled":1})
            require(not (Path(config.codex_home) / "auth.json").exists())
    asyncio.run(go())


def t_unknown_cost_blocks_before_worker_but_auth_reads_the_dedicated_home():
    async def go():
        with native_fixture() as (root, config, ledger, task, run, request):
            with scoped(ledger, task, run, quote=None):
                result = await N.run_research(request, config=config)
            require_equal(result.result.reason, "cost_unbounded")
            require(not result.dispatch_started)
            require_equal(D.invocations(ledger, task), [])
            require_equal([r["argv"] for r in observed(root) if "argv" in r], [["login", "status"]])
    asyncio.run(go())


def t_auth_loss_before_dispatch_never_claims_or_launches_rpc():
    result, rows, claims = run_case("login_out")
    require_equal(result.result.reason, "logged_out")
    require(not result.dispatch_started)
    require_equal(claims, [])
    require(not any(r.get("method") == "initialize" for r in rows))


def t_native_api_account_is_rejected_before_thread_or_turn():
    result, rows, claims = run_case("account_api")
    require_equal(result.result.reason, "subscription_required")
    require_equal(len(claims), 1)
    require(not any(r.get("method") in {"thread/start", "turn/start"} for r in rows))


def t_wrong_effective_model_never_starts_a_turn():
    result, rows, _ = run_case("wrong_model")
    require_equal(result.result.reason, "native_policy_mismatch")
    require(not any(r.get("method") == "turn/start" for r in rows))


def t_hidden_mcp_config_is_rejected_before_thread_creation():
    result, rows, _ = run_case("hidden_mcp")
    require_equal(result.result.reason, "native_config_unverified")
    require(not any(r.get("method") == "thread/start" for r in rows))


def t_quota_rejects_even_available_paid_credits_without_turn_or_retry():
    result, rows, _ = run_case("quota_before")
    require_equal(result.result.reason, "quota")
    require(result.quota)
    require(not any(r.get("method") == "turn/start" for r in rows))


def t_quota_during_turn_interrupts_instead_of_native_retry():
    result, rows, _ = run_case("quota_turn")
    require_equal(result.result.reason, "quota")
    require_equal(sum(r.get("method") == "turn/start" for r in rows), 1)
    require(any(r.get("method") == "turn/interrupt" for r in rows))


def t_unknown_quota_never_starts_a_native_turn():
    for mode in ("quota_unknown", "quota_malformed"):
        result, rows, claims = run_case(mode)
        require_equal(result.result.reason, "native_quota_unknown", mode)
        require(not any(r.get("method") == "turn/start" for r in rows))
        require_equal(claims[0]["state"], "finished")


def t_quota_interrupt_acknowledgement_without_completion_retains_unknown():
    result, rows, claims = run_case("quota_no_completion")
    require_equal(result.result.reason, "cost_recovery_required")
    require_equal(claims[0]["state"], "unknown")
    require_equal(sum(r.get("method") == "turn/start" for r in rows), 1)


def t_no_terminal_and_foreign_turn_never_accept_hermes_deadline_text():
    for mode in ("no_terminal", "wrong_turn", "disconnect"):
        result, rows, claims = run_case(mode, timeout=0.8)
        require_equal(result.result.reason, "cost_recovery_required", mode)
        require(not result.result.ok)
        require_equal(claims[0]["state"], "unknown", mode)
        if mode != "disconnect":
            require(any(r.get("method") == "turn/interrupt" for r in rows))


def t_lost_native_completion_blocks_retry_after_reopening_the_ledger():
    async def go():
        with native_fixture("disconnect", timeout=0.8) as (root, config, ledger, task, run, request):
            with scoped(ledger, task, run):
                first = await N.run_research(request, config=config)
            require_equal(first.result.reason, "cost_recovery_required")
            require_equal(C.CostLedger(ledger).view(task)["counts"], {"unknown": 1})
            (root / "mode").write_text("ok")
            fresh = S.AgentRunLedger(ledger.path)
            with scoped(fresh, task, run):
                second = await N.run_research(request, config=config)
            require_equal(second.result.reason, "cost_recovery_required")
            require_equal(len(method_rows(root, "turn/start")), 1)
            require_equal(len(D.invocations(fresh, task)), 1)
    asyncio.run(go())


def t_only_final_answer_from_the_exact_native_turn_can_be_a_result():
    for mode in ("unscoped_message", "thread_only_message", "foreign_message", "commentary_only"):
        result, rows, claims = run_case(mode)
        require(not result.result.ok, mode)
        require_equal(result.result.reason, "native_result_invalid", mode)
        require_equal(claims[0]["state"], "finished", mode)
        require_equal(sum(r.get("method") == "turn/start" for r in rows), 1)


def t_hermes_tools_elicitation_and_unexpected_shell_never_auto_approve():
    for mode in ("server_request", "unexpected_tool"):
        result, rows, _ = run_case(mode)
        require_equal(result.result.reason, "native_tool_not_allowed", mode)
        require(not any((r.get("result") or {}).get("action") == "accept" for r in rows))
        require(any(r.get("method") == "turn/interrupt" for r in rows))


def t_worker_events_are_bounded_redacted_and_final_still_parseable():
    async def go():
        with native_fixture("many_events") as (root, config, _ledger, _task, _run, request):
            with tempfile.TemporaryDirectory() as workdir:
                outcome = await L.run(N.worker_invocation(config, workdir), SP.build_prompt(SP.profile(request.profile), request))
            require(outcome.ok, outcome.reason)
            require(not outcome.truncated)
            require("SYNTHETIC_NOT_A_REAL_KEY" not in outcome.text)
            body, events = N._decode(outcome.text, SP.redact_specialist_output)
            require_equal(len(events), 24)
            require_equal(body["terminal"], "completed")
            require(len(outcome.text) < 20_000)
    asyncio.run(go())


def t_credential_prefixes_match_whole_lexemes_without_erasing_workspace_paths():
    from solvio.specialists.hermes_native_worker import safe_text
    from solvio.specialists.native_task_profile import _safe
    traceback = ('Traceback (most recent call last):\n'
        '  File "/private/tmp/isolated/native-task-workspaces/at-123/helper.py", line 7\n'
        '    assert result == expected\nAssertionError\n')
    require_equal(safe_text(traceback, 8000), traceback)
    require_equal(_safe(traceback), traceback)
    # Constructed shapes only; no account, config, auth file or native process.
    keys = ('sk-' + 'x' * 24, 'sk-proj-' + 'x' * 32, 'ghp_' + 'x' * 24,
            'eyJ' + 'x' * 12 + '.' + 'y' * 16 + '.' + 'z' * 16)
    for key in keys:
        for text in (key, 'value="' + key + '"', '(' + key + ')', '/tmp/' + key,
                     traceback + '\n' + key):
            require(key not in safe_text(text, 8000))
            require_equal(_safe(text), '<entfernt>')
    for text in ('authorization=synthetic private value', 'Bearer synthetic-private-value',
                 '{"access_token":"synthetic-private-value"}'):
        require('synthetic-private-value' not in safe_text(text, 8000))
        require_equal(_safe(text), '<entfernt>')


def t_cancel_sends_real_rpc_interrupt_and_kills_descendants_retaining_claim():
    async def go():
        with native_fixture("cancel", timeout=30) as (root, config, ledger, task, run, request):
            with scoped(ledger, task, run):
                future = asyncio.create_task(N.run_research(request, config=config))
                for _ in range(250):
                    if (root / "child.pid").exists(): break
                    if future.done(): require(False, (await future).result.reason)
                    await asyncio.sleep(0.02)
                require((root / "child.pid").exists())
                child_pid = int((root / "child.pid").read_text())
                started = time.monotonic()
                future.cancel()
                try:
                    await future
                    require(False, "cancellation did not propagate")
                except asyncio.CancelledError:
                    pass
            require(time.monotonic() - started < 5)
            require(method_rows(root, "turn/interrupt"), "worker must ask native runtime to stop")
            # A killed orphan can briefly remain a zombie; it is no longer a
            # running provider child. Wait for init to reap it.
            alive = True
            for _ in range(100):
                try: os.kill(child_pid, 0)
                except ProcessLookupError:
                    alive = False
                    break
                await asyncio.sleep(0.02)
            require(not alive, "child process survived cancellation")
            require_equal(D.invocations(ledger, task)[0]["state"], "unknown")
            require_equal(C.CostLedger(ledger).view(task)["counts"], {"unknown":1})
    asyncio.run(go())


def t_unprepared_or_changed_home_has_no_cli_or_credential_side_effect():
    async def go():
        with native_fixture() as (root, config, ledger, task, run, request):
            with scoped(ledger, task, run):
                first = await N.run_research(request, config=N.NativeResearchConfig())
                (Path(config.codex_home) / "config.toml").write_text('model="different"\n')
                second = await N.run_research(request, config=config)
            require_equal(first.result.reason, "native_not_configured")
            require_equal(second.result.reason, "native_config_mismatch")
            require_equal(observed(root), [])
            require_equal(D.invocations(ledger, task), [])
    asyncio.run(go())


def native_answer():
    return {"findings": ["Belegter Befund"], "evidence": [], "assumptions": [],
            "uncertainties": [], "recommended_path": "Nach Belegen entscheiden.",
            "rejected_alternatives": [], "risk_notes": [], "confidence": "hoch"}


def large_research_briefing():
    """Two distinct, complete seven-date results that fit the real checkpoint."""
    from solvio.agent_runtime import checkpoint as CP, planner as PL
    sections = []
    qualification = "Der Endpreis ist NICHT bestätigt; die erste Preisangabe gilt nicht mehr."
    for turn, fill in ((1, "a"), (2, "b")):
        section = {"step_id": "as-synthetic-" + str(turn), "confidence": "mittel",
            "findings": [f"Recherche {turn}, Datumspaar {index}: " + fill * 1070 for index in range(7)],
            "evidence": [f"https://example.org/flight/{turn}/{index}?details=" + fill * 555 for index in range(7)],
            "recommended_path": ("Synthetischer Vergleich. " * 10).strip(),
            "uncertainties": [("Endpreisbedingungen offen. " * 10).strip()],
            "assumptions": [], "rejected_alternatives": [], "risk_notes": []}
        if turn == 2:
            section["findings"][-1] += qualification
        answer = {key: value for key, value in section.items() if key != "step_id"}
        require_equal(N._structured_text(json.dumps(answer, ensure_ascii=False)), answer)
        require(12_000 < len(json.dumps(answer, ensure_ascii=False)) < 18_000)
        sections.append(section)
    plan = PL.Plan(goal="Vergleiche sieben Datumspaare.", steps=[PL.PlannedStep(
        "specialist", profile="researcher/hermes", instruction="Andere Originalquellen prüfen.")])
    checkpoint = CP.encode(plan=plan, revision=2, cursor=0, goal_met="",
        approval_attempts=0, pending_step_id="", notes=[], findings=[], sources=[],
        invalid_signatures=[], attempts={}, low_value={}, result_sections=sections)
    restored = CP.decode(checkpoint)
    require(restored is not None and restored["result_sections_complete"] is True)
    require_equal(restored["result_sections"], sections)
    body = {"originalauftrag": "Vergleiche sieben Datumspaare für Hin- und Rückflug mit Endpreis.",
        "nutzerangaben": "Frankfurt ist der bestätigte Abflughafen.",
        "gebundene_anforderungen": {"auskunft": [{"id": "a1", "text": "Alle Datumspaare vergleichen"}],
            "handlungen": [], "unklar": [], "belege": {"mindestens": 1}},
        "bisherige_ergebnisse": sections,
        "pruefhinweise": {"offen": ["a1"], "fehlend": ["Endpreisbedingungen"], "unsicher": []}}
    return json.dumps(body, ensure_ascii=False, sort_keys=True), qualification


def t_large_checkpoint_valid_refinement_reaches_real_rpc_without_losing_late_qualification():
    async def go():
        with native_fixture(timeout=5.0) as (root, config, ledger, task, run, request):
            request.research_briefing, qualification = large_research_briefing()
            request.research_strategy = "alternative_sources"
            prompt = SP.build_prompt(SP.profile(request.profile), request)
            require(N.MAX_INPUT < len(prompt.encode()) < N.MAX_RESEARCH_INPUT)
            with scoped(ledger, task, run):
                result = await N.run_research(request, config=config)
            require(result.result.ok, result.result.reason)
            turns = method_rows(root, "turn/start")
            require_equal(len(turns), 1)
            delivered = turns[0]["params"]["input"][0]["text"]
            require_equal(delivered, prompt)
            require(request.research_briefing in delivered)
            require(qualification in delivered)
            require("ALTERNATIVE QUELLEN" in delivered)
            require(method_rows(root, "thread/start")[0]["params"]["ephemeral"] is True)
            require_equal(C.CostLedger(ledger).view(task)["counts"], {"settled": 1})
    asyncio.run(go())


def t_ephemeral_entry_stdin_limit_counts_utf8_bytes_and_never_retries_oversize():
    async def go():
        with native_fixture(timeout=5.0) as (root, config, _ledger, _task, _run, _request):
            invocation = N.worker_invocation(config, str(root))
            accepted = "🙂" * 39_999 + "é"
            require_equal(len(accepted.encode()), 159_998)
            result = await L.run(invocation, accepted)
            body, _ = N._decode(result.text, SP.redact_specialist_output)
            require_equal(body["status"], "completed", body)
            require_equal(method_rows(root, "turn/start")[0]["params"]["input"][0]["text"], accepted)
            before = len(observed(root))
            oversized = "🙂" * 40_000 + "é"
            require_equal(len(oversized.encode()), 160_002)
            result = await L.run(invocation, oversized)
            body, _ = N._decode(result.text, SP.redact_specialist_output)
            require_equal(body["reason"], "native_input_too_large")
            require_equal(len(observed(root)), before, "oversized input started a provider process")
    asyncio.run(go())


def t_ephemeral_entry_refuses_all_durable_and_task_arguments_before_native_start():
    async def go():
        with native_fixture() as (root, config, _ledger, _task, _run, _request):
            base = N.worker_invocation(config, str(root))
            for arguments in (("--session-mode", "durable"), ("--session-mode=",),
                    ("--session-m=durable",), ("--resume-thread", "local-thread"),
                    ("--previous-turn", "local-turn"), ("--worker-profile", "task"),
                    ("--worker-p=task",), ("--core-tools-socket", "/tmp/synthetic"),
                    ("--core-tools-digest", "a" * 64)):
                result = await L.run(replace(base, argv=base.argv + arguments), "synthetic")
                body, _ = N._decode(result.text, SP.redact_specialist_output)
                require_equal(body["reason"], "native_research_entry_invalid", arguments)
                require_equal(body["execution_status"], "not_started")
            require_equal(observed(root), [])
    asyncio.run(go())


def t_caller_keeps_separate_ephemeral_and_continuation_byte_limits_before_dispatch():
    async def go():
        with native_fixture() as (root, config, ledger, task, run, request):
            with scoped(ledger, task, run), patch.object(SP, "build_prompt", return_value="🙂" * 40_001):
                ephemeral = await N.run_research(request, config=config)
            require_equal(ephemeral.result.reason, "native_input_too_large")
            continuation = SimpleNamespace(binding=lambda *_: require(False, "oversized continuation bound a session"))
            with scoped(ledger, task, run), patch.object(SP, "build_prompt", return_value="é" * 12_001):
                durable = await N.run_research(request, config=config, continuation=continuation)
            require_equal(durable.result.reason, "native_input_too_large")
            require_equal(observed(root), [])
            require_equal(D.invocations(ledger, task), [])
    asyncio.run(go())


def t_continuation_retains_original_entry_and_24000_byte_stdin_envelope():
    async def go():
        with native_fixture() as (root, config, _ledger, task, _run, _request):
            invocation = N.continuation_invocation(config, str(root), task_id=task)
            require_equal(Path(invocation.argv[2]).name, "hermes_native_worker.py")
            require_equal(N.MAX_INPUT, 24_000)
            outcome = await L.run(invocation, "é" * 12_001)
            body, _ = N._decode(outcome.text, SP.redact_specialist_output)
            require_equal(body["reason"], "native_input_too_large")
            require_equal(observed(root), [])
    asyncio.run(go())


def rejected_native_answer(value):
    try:
        N._structured_text(json.dumps(value, ensure_ascii=False))
    except ValueError as error:
        require_equal(str(error), "native_result_invalid")
    else:
        require(False, "oversized or invalid native answer accepted")


def t_native_list_lengths_and_counts_fit_complete_checkpoint_sections():
    # Independent consumer limits, including twelve reserved native URL slots.
    bounds = {"findings": (12, 1200), "evidence": (12, 720),
              "assumptions": (12, 600), "uncertainties": (12, 600),
              "rejected_alternatives": (12, 600), "risk_notes": (12, 600)}
    from solvio.agent_runtime import checkpoint as CP
    for key, (count, chars) in bounds.items():
        for size in (chars - 1, chars):
            answer = native_answer()
            answer[key] = ["x" * size]
            parsed = N._structured_text(json.dumps(answer))
            require_equal(parsed, answer)
            sections, complete = CP._result_sections([dict(parsed, step_id="native-proof")])
            require(complete, key)
            require_equal(sections[0][key], answer[key])
        answer[key] = ["x" * (chars + 1)]
        rejected_native_answer(answer)
        for size in (count - 1, count):
            answer[key] = [str(index) for index in range(size)]
            require_equal(N._structured_text(json.dumps(answer)), answer)
        answer[key] = ["x"] * (count + 1)
        rejected_native_answer(answer)


def t_native_recommendation_and_whole_answer_limits_are_explicit():
    for size in (1499, 1500):
        answer = native_answer()
        answer["recommended_path"] = "x" * size
        require_equal(N._structured_text(json.dumps(answer)), answer)
    answer["recommended_path"] += "x"
    rejected_native_answer(answer)
    from solvio.specialists.hermes_native_worker import MAX_TEXT
    text = json.dumps(native_answer())
    for size in (MAX_TEXT - 1, MAX_TEXT):
        require_equal(N._structured_text(text + " " * (size - len(text))), native_answer())
    try:
        N._structured_text(text + " " * (MAX_TEXT + 1 - len(text)))
    except ValueError as error:
        require_equal(str(error), "native_result_invalid")
    else:
        require(False, "whole native result limit ignored")


def t_native_field_set_and_value_types_are_strict():
    for key in native_answer():
        missing = native_answer()
        del missing[key]
        rejected_native_answer(missing)
        for value in (None, False, 0, {}, [False]):
            answer = native_answer()
            answer[key] = value
            rejected_native_answer(answer)
    answer = native_answer()
    answer["approved"] = True
    rejected_native_answer(answer)
    answer = native_answer()
    answer.update(findings=["  "], recommended_path="\n")
    rejected_native_answer(answer)


def t_native_confidence_is_preserved_or_refused_not_coerced():
    for confidence in ("", "hoch", "mittel", "niedrig", "high", "medium", "low"):
        answer = native_answer()
        answer["confidence"] = confidence
        require_equal(N._structured_text(json.dumps(answer))["confidence"], confidence)
    for confidence in ("HIGH", "certain", " hoch ", True, 1, [], {}):
        answer["confidence"] = confidence
        rejected_native_answer(answer)


def t_real_native_result_preserves_late_negation_and_all_sections_with_twelve_sources():
    async def go():
        from solvio.agent_runtime import checkpoint as CP
        with native_fixture() as (root, config, ledger, task, run, request):
            answer = native_answer()
            answer["findings"] = ["x" * 650 + " NICHT als passendes Produkt bestätigt."] + [
                f"Weiterer Befund {index}" for index in range(11)]
            answer["evidence"] = ["x" * 650 + " Quelle bestätigt das Maß NICHT."] + [
                f"Modellbeleg {index}" for index in range(11)]
            answer["recommended_path"] = "x" * 1490 + " NICHT."
            for key in ("assumptions", "uncertainties", "rejected_alternatives", "risk_notes"):
                answer[key] = [f"{key} {index}" for index in range(12)]
            (root / "answer.json").write_text(json.dumps(answer))
            sources = [f"https://example.org/{index}/" + "x" * 675 for index in range(11)]
            (root / "sources.json").write_text(json.dumps(sources))
            browser_sources = [f"https://example.org/browser/{index}?date=2026-10-05" for index in range(12)]
            original_worker = N._run_worker
            async def worker_with_browser_observations(invocation, prompt, **kwargs):
                outcome = await original_worker(invocation, prompt, **kwargs)
                frames = [json.loads(line) for line in outcome.text.splitlines()]
                # Only the native result boundary is synthetic here; the real
                # RPC/launcher/claim still produces the twelve web sources.
                frames[-1]["browser_sources"] = browser_sources + ["https://example.org/over-browser-cap"]
                return replace(outcome, text="\n".join(json.dumps(frame) for frame in frames))
            with scoped(ledger, task, run), patch.object(SP, "native_research_config", return_value=config), \
                    patch.object(N, "_run_worker", worker_with_browser_observations):
                outcome = await SP.run_specialist(request, researcher=None)
            require(outcome.result.usable, outcome.result.reason)
            for key, value in answer.items():
                actual = getattr(outcome.result, key)
                require_equal(actual[:12] if key == "evidence" else actual, value, key)
            require_equal(len(outcome.result.evidence), 36)
            require_equal(outcome.result.evidence[12:24], ["Native Websuche: " + url for url in
                ["https://example.org/source", *sources]])
            require_equal(outcome.result.evidence[24:], ["Nativer Browser: " + url for url in browser_sources])
            section = {key: getattr(outcome.result, key) for key in answer}
            sections, complete = CP._result_sections([dict(section, step_id="native-proof")])
            require(complete, "native answer exceeded its consumer contract")
            require_equal(sections[0], dict(section, step_id="native-proof"))
            claims = D.invocations(ledger, task)
            require_equal(len(claims), 1)
            require_equal(claims[0]["state"], "finished")
            schema = method_rows(root, "turn/start")[0]["params"]["outputSchema"]
            require_equal(schema["properties"]["findings"]["items"]["maxLength"], 1200)
            require_equal(schema["properties"]["evidence"]["maxItems"], 12)
            require_equal(schema["properties"]["recommended_path"]["maxLength"], 1500)
    asyncio.run(go())


def t_real_native_oversized_result_fails_without_success_or_second_turn():
    async def go():
        with native_fixture() as (root, config, ledger, task, run, request):
            answer = native_answer()
            answer["findings"] = ["x" * 1200 + " NICHT erfüllt."]
            (root / "answer.json").write_text(json.dumps(answer))
            with scoped(ledger, task, run), patch.object(SP, "native_research_config", return_value=config):
                outcome = await SP.run_specialist(request, researcher=None)
            require(not outcome.result.ok)
            require_equal(outcome.result.reason, "native_result_invalid")
            require_equal(outcome.result.findings, [])
            require_equal(len(method_rows(root, "turn/start")), 1)
            require_equal(C.CostLedger(ledger).view(task)["counts"], {"settled": 1})
    asyncio.run(go())


def browser_provenance(result):
    observations = [value for values in (result.uncertainties, result.evidence) for value in values
                    if value.startswith("Core-Werkzeugbeobachtung: ")]
    require_equal(len(observations), 1, "native browser observation was not retained exactly once")
    observation = observations[0]
    require(len(observation) <= 600)
    require("https://" not in observation and "synthetic-never-project" not in observation)
    return observation


def t_offered_native_browser_without_read_attempt_preserves_findings_and_marks_access_unknown():
    async def go():
        with native_fixture(browser=True, timeout=5.0) as (root, config, ledger, task, run, request):
            answer = native_answer()
            answer.update(findings=["Suchauszug erhalten; Livepreis nicht bestätigt."],
                uncertainties=["Lesewerkzeug nicht erreichbar."],
                recommended_path="Nur der vorhandene Teilbefund ist belegt.")
            (root / "answer.json").write_text(json.dumps(answer))
            with scoped(ledger, task, run):
                outcome = await N.run_research(request, config=config)
            require(outcome.result.ok, outcome.result.reason)
            for key in ("findings", "recommended_path", "confidence"):
                require_equal(getattr(outcome.result, key), answer[key])
            require_equal(outcome.result.uncertainties[:1], answer["uncertainties"])
            note = browser_provenance(outcome.result)
            require("Katalog" in note and "0 begonnene" in note and "0 beendet" in note)
            require("bestätigt weder eine Zugriffssperre" in note)
            require_equal(len(method_rows(root, "mcpServerStatus/list")), 1)
            delivered = method_rows(root, "turn/start")[0]["params"]["input"][0]["text"]
            require("solvio-browser/browser_navigate" in delivered and "browser_snapshot" in delivered)
            require_equal(len(method_rows(root, "turn/start")), 1)
            require_equal(len(D.invocations(ledger, task)), 1)
            require_equal(C.CostLedger(ledger).view(task)["counts"], {"settled": 1})
        # The same configured reader must not add a mandatory page-reading
        # directive to the deliberately short public-weather effort path.
        with native_fixture(browser=True, timeout=5.0) as (root, config, ledger, task, run, request):
            request.short_public_answer = True
            original_prompt = SP.build_prompt(SP.profile(request.profile), request)
            with scoped(ledger, task, run):
                outcome = await N.run_research(request, config=config)
            require(outcome.result.ok, outcome.result.reason)
            delivered = method_rows(root, "turn/start")[0]["params"]["input"][0]["text"]
            require_equal(delivered, original_prompt)
            require("0 begonnene" in browser_provenance(outcome.result))
            require_equal(len(method_rows(root, "turn/start")), 1)
    asyncio.run(go())


def t_native_browser_counts_only_paired_current_turn_items_without_proving_success():
    async def go():
        with native_fixture("browser_paired", browser=True, timeout=5.0) as (root, config, ledger, task, run, request):
            with scoped(ledger, task, run):
                outcome = await N.run_research(request, config=config)
            require(outcome.result.ok, outcome.result.reason)
            note = browser_provenance(outcome.result)
            require("2 begonnene" in note and "2 beendet" in note)
            require("noch einen erfolgreichen Zugriff" in note)
            require_equal(outcome.result.findings, ["Lokaler Protokollnachweis"])
            require_equal(outcome.native_thread_id, "local-thread")
            require_equal(outcome.native_turn_id, "local-turn")
            require_equal(len(method_rows(root, "turn/start")), 1)
            require_equal(D.invocations(ledger, task)[0]["state"], "finished")
    asyncio.run(go())


def t_native_browser_unpaired_duplicate_capped_or_missing_progress_keeps_counts_unknown():
    async def go():
        for mode in ("browser_unpaired", "browser_duplicate", "many_events", "missing_progress"):
            with native_fixture(mode, browser=True, timeout=5.0) as (root, config, ledger, task, run, request):
                original = N._run_worker
                async def run_worker(invocation, prompt, **kwargs):
                    outcome = await original(invocation, prompt, **kwargs)
                    if mode == "missing_progress":
                        frames = [json.loads(line) for line in outcome.text.splitlines()]
                        outcome = replace(outcome, text=json.dumps(frames[-1]))
                    return outcome
                with scoped(ledger, task, run), patch.object(N, "_run_worker", run_worker):
                    outcome = await N.run_research(request, config=config)
                require(outcome.result.ok, (mode, outcome.result.reason))
                note = browser_provenance(outcome.result)
                require("unbekannt" in note and "0 begonnene" not in note, mode)
                require_equal(outcome.result.findings, ["Lokaler Protokollnachweis"])
                require_equal(len(method_rows(root, "turn/start")), 1)
    asyncio.run(go())


def t_unconfigured_legacy_native_research_does_not_invent_a_browser_catalogue():
    async def go():
        with native_fixture() as (root, config, ledger, task, run, request):
            answer = native_answer()
            answer["uncertainties"] = ["Quellenauszug beantwortet die Preisfrage nicht."]
            (root / "answer.json").write_text(json.dumps(answer))
            with scoped(ledger, task, run):
                outcome = await N.run_research(request, config=config)
            require(outcome.result.ok, outcome.result.reason)
            require_equal(outcome.result.uncertainties, answer["uncertainties"])
            require(not any(value.startswith("Core-Werkzeugbeobachtung: ")
                for value in outcome.result.evidence))
            require_equal(method_rows(root, "mcpServerStatus/list"), [])
    asyncio.run(go())


def t_browser_provenance_preserves_twelve_full_model_qualifications_in_checkpoint():
    async def go():
        from solvio.agent_runtime import checkpoint as CP
        with native_fixture(browser=True, timeout=5.0) as (root, config, ledger, task, run, request):
            answer = native_answer()
            answer["uncertainties"] = [str(index) + "x" * (600 - len(str(index))) for index in range(12)]
            answer["evidence"] = ["Modellbeleg " + str(index) for index in range(12)]
            (root / "answer.json").write_text(json.dumps(answer))
            with scoped(ledger, task, run):
                outcome = await N.run_research(request, config=config)
            require(outcome.result.ok, outcome.result.reason)
            require_equal(outcome.result.uncertainties, answer["uncertainties"])
            require_equal(outcome.result.evidence[:12], answer["evidence"])
            require("0 begonnene" in browser_provenance(outcome.result))
            section = {key: getattr(outcome.result, key) for key in answer}
            sections, complete = CP._result_sections([dict(section, step_id="browser-provenance")])
            require(complete)
            require_equal(sections[0]["uncertainties"], answer["uncertainties"])
    asyncio.run(go())


def t_browser_provenance_retains_all_36_evidence_and_12_qualifications_in_one_core_slot():
    async def go():
        from solvio.agent_runtime import checkpoint as CP
        from solvio.specialists.result import NATIVE_BROWSER_OBSERVATION_PREFIX
        require_equal(NATIVE_BROWSER_OBSERVATION_PREFIX, "Core-Werkzeugbeobachtung: ")
        with native_fixture(browser=True, timeout=5.0) as (root, config, ledger, task, run, request):
            answer = native_answer()
            answer["uncertainties"] = [str(index) + "x" * (600 - len(str(index))) for index in range(12)]
            answer["evidence"] = ["Modellbeleg " + str(index) for index in range(12)]
            (root / "answer.json").write_text(json.dumps(answer))
            (root / "sources.json").write_text(json.dumps(["https://example.org/web/" + str(i) for i in range(11)]))
            browser_sources = ["https://example.org/browser/" + str(i) for i in range(12)]
            original = N._run_worker
            async def run_worker(invocation, prompt, **kwargs):
                outcome = await original(invocation, prompt, **kwargs)
                frames = [json.loads(line) for line in outcome.text.splitlines()]
                # A browser source with no observed read is inconsistent
                # metadata; it cannot make zero calls or twelve free slots true.
                frames[-1]["browser_sources"] = browser_sources
                return replace(outcome, text="\n".join(json.dumps(frame) for frame in frames))
            with scoped(ledger, task, run), patch.object(N, "_run_worker", run_worker):
                outcome = await N.run_research(request, config=config)
            require(outcome.result.ok, outcome.result.reason)
            require_equal(outcome.result.uncertainties, answer["uncertainties"])
            require_equal(len(outcome.result.evidence), 37)
            require_equal(outcome.result.evidence[:12], answer["evidence"])
            require_equal(outcome.result.evidence[24:36], ["Nativer Browser: " + value for value in browser_sources])
            require("unbekannt" in browser_provenance(outcome.result))
            section = dict({key: getattr(outcome.result, key) for key in answer}, step_id="browser-full")
            plan = CP.PL.Plan(goal="Gebundener Vergleich", steps=[CP.PL.PlannedStep(
                "specialist", profile="researcher/hermes", instruction="Direkte Quellen prüfen.")])
            text = CP.encode(plan=plan, revision=0, cursor=0, goal_met="", approval_attempts=0,
                pending_step_id="", notes=[], findings=[], sources=[], invalid_signatures=[],
                attempts={}, low_value={}, result_sections=[section])
            restored = CP.decode(text)
            require(restored is not None and restored["result_sections_complete"] is True)
            require_equal(restored["result_sections"], [section])
            require_equal(len(method_rows(root, "turn/start")), 1)
    asyncio.run(go())


def _direct_preread_request(request, urls):
    # Attribute assignment keeps the red fixture executable on the old request
    # dataclass; the missing behavior, rather than an import, must fail it.
    request.research_strategy = "direct_sources"
    request.direct_source_urls = tuple(urls)
    return request


def _browser_rows(root):
    path = root / "browser-observed.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@contextmanager
def _capture_worker_results():
    class CapturedBodies(list):
        pass
    bodies = CapturedBodies()
    bodies.events = []
    original = N._run_worker
    async def capture(*args, **kwargs):
        outcome = await original(*args, **kwargs)
        if outcome.text:
            body, events = N._decode(outcome.text, SP.redact_specialist_output)
            bodies.append(body)
            bodies.events.extend(events)
        return outcome
    with patch.object(N, "_run_worker", capture):
        yield bodies


def _preread_receipt(bodies):
    require_equal(len(bodies), 1, "Exactly one existing research worker must return")
    receipt = bodies[0].get("browser_preread")
    require(isinstance(receipt, dict), "The worker omitted the separately bound pre-turn reader receipt")
    return receipt


def _one_core_observation(result):
    from solvio.specialists.result import NATIVE_BROWSER_OBSERVATION_PREFIX
    notes = [value for value in result.uncertainties + result.evidence
             if value.startswith(NATIVE_BROWSER_OBSERVATION_PREFIX)]
    require_equal(len(notes), 1, "Pre-turn and native observations must share one Core slot")
    require(len(notes[0]) <= 600)
    return notes[0]


def t_direct_source_preread_uses_fixed_sdk_and_complete_chunks_in_one_existing_turn():
    async def go():
        with native_fixture(browser=True, preread="ok", timeout=60) as (root, config, ledger, task, run, request):
            urls = ("https://example.org/fra-waw?outbound=2026-10-05&adults=1",
                    "https://example.org/fra-waw?outbound=2026-10-06&adults=1")
            _direct_preread_request(request, urls)
            answer = {"findings":["Teilfund bleibt erhalten"], "evidence":["https://example.org/model"],
                "assumptions":[], "uncertainties":[str(i)+"u"*598 for i in range(12)],
                "recommended_path":"Gebundenen Vergleich lesen", "rejected_alternatives":[],
                "risk_notes":[], "confidence":"niedrig"}
            (root / "answer.json").write_text(json.dumps(answer))
            with scoped(ledger, task, run), _capture_worker_results() as bodies:
                result = await N.run_research(request, config=config)
            require(result.result.usable, result.result.reason)
            rows = _browser_rows(root)
            require_equal(len([r for r in rows if r.get("event") == "spawn"]), 1,
                          "Direct-source phase never started the fixed SDK reader")
            calls = [r["params"] for r in rows if r.get("method") == "tools/call"]
            require_equal([r["arguments"]["url"] for r in calls if r["name"] == "browser_navigate"], list(urls))
            require({r["name"] for r in calls} <= {"browser_navigate", "browser_snapshot"})
            require(len([r for r in calls if "snapshot_id" in r.get("arguments", {})]) > 10,
                    "The late qualification requires complete saved-snapshot paging")
            turn = method_rows(root, "turn/start")
            require_equal(len(turn), 1)
            text = turn[0]["params"]["input"][0]["text"]
            require(request.objective in text and "untrusted" in text.lower())
            require_equal(text.count("Späte Einschränkung: NICHT buchbar, Gepäck fehlt."), 2)
            require_equal(result.result.uncertainties, answer["uncertainties"])
            note = _one_core_observation(result.result)
            require("Vorablesung" in note and "Preis" in note)
            receipt = _preread_receipt(bodies)
            require_equal(receipt.get("task_id"), task)
            require_equal(receipt.get("phase"), "direct_sources")
            require_equal(receipt.get("complete_snapshots"), 2)
            require_equal(receipt.get("started_calls"), len(calls))
            require_equal(receipt.get("completed_calls"), len(calls))
            require_equal(receipt.get("cleanup"), "closed")
            require(not any("browser_read" == event.get("event") for event in bodies.events),
                    "A pre-turn SDK receipt must not invent native turn items")
            require(not any(url in json.dumps(receipt) for url in urls))
            require_equal(len(D.invocations(ledger, task)), 1)
            require_equal(C.CostLedger(ledger).view(task)["counts"], {"settled":1})
            spawn = [r for r in rows if r.get("event") == "spawn"][0]
            require_equal(spawn["workdir_mode"], 0)
            require(not any(url in arg for url in urls for arg in spawn["argv"]))
    asyncio.run(go())


def t_direct_source_preread_keeps_original_deadline_and_single_cost_claim():
    async def go():
        with native_fixture(browser=True, preread="slow", timeout=60) as (root, config, ledger, task, run, request):
            _direct_preread_request(request, ("https://example.org/fra-waw",))
            original_factory = N.worker_invocation
            outer = []
            def factory(*args, **kwargs):
                invocation = original_factory(*args, **kwargs)
                outer.append(invocation)
                return invocation
            begun = time.monotonic()
            with scoped(ledger, task, run), patch.object(N, "worker_invocation", factory), _capture_worker_results() as bodies:
                result = await N.run_research(request, config=config)
            require(result.result.usable, result.result.reason)
            receipt = _preread_receipt(bodies)
            require_equal(receipt.get("complete_snapshots"), 1)
            require_equal(len(outer), 1)
            require_equal(outer[0].timeout, config.timeout_s + config.shutdown_grace_s + 1)
            require_equal(float(outer[0].argv[outer[0].argv.index("--timeout") + 1]), config.timeout_s)
            process = [r for r in observed(root) if r.get("argv", [])[:1] == ["app-server"]]
            require_equal(len(process), 1, "A pre-read cannot spawn a second native app-server")
            argv = process[0]["argv"]
            overrides = tomllib.loads("\n".join(argv[i+1] for i, value in enumerate(argv[:-1]) if value == "-c"))
            args = overrides["mcp_servers"]["solvio-browser"]["args"]
            remaining = float(args[args.index("--timeout") + 1])
            require(0 < remaining <= config.timeout_s - .3,
                    "The existing native turn received a reset timeout after browser pre-read")
            require(time.monotonic() - begun < outer[0].timeout)
            require_equal(len(method_rows(root, "turn/start")), 1)
            require_equal(len(D.invocations(ledger, task)), 1)
    asyncio.run(go())


def t_direct_source_preread_is_phase_bound_and_refuses_oversize_or_secret_urls():
    async def go():
        for strategy in ("", "alternative_sources"):
            with native_fixture(browser=True, preread="ok", timeout=60) as (root, config, ledger, task, run, request):
                request.research_strategy = strategy
                request.direct_source_urls = ("https://example.org/fra-waw",)
                with scoped(ledger, task, run):
                    result = await N.run_research(request, config=config)
                require(result.result.usable, result.result.reason)
                require_equal(_browser_rows(root), [])
                require_equal(len(method_rows(root, "turn/start")), 1)
        for urls in (tuple("https://example.org/"+str(i) for i in range(4)),
                     ("https://example.org/result?access_token=synthetic-never-forward",),
                     ("https://user:synthetic@example.org/",),
                     ("Found this source https://example.org/in-prose",)):
            with native_fixture(browser=True, preread="ok", timeout=60) as (root, config, ledger, task, run, request):
                _direct_preread_request(request, urls)
                with scoped(ledger, task, run):
                    result = await N.run_research(request, config=config)
                require(not result.result.ok, "Unbound reader inputs were silently ignored")
                require_equal(method_rows(root, "turn/start"), [])
                require_equal(_browser_rows(root), [])
                require_equal(D.invocations(ledger, task), [])
    asyncio.run(go())


def t_direct_source_preread_rejects_catalog_snapshot_task_offsets_and_hash_before_model():
    async def go():
        for mode in ("catalog_extra", "catalog_not_readonly", "foreign_snapshot", "bad_offset", "bad_hash"):
            with native_fixture(browser=True, preread=mode, timeout=60) as (root, config, ledger, task, run, request):
                _direct_preread_request(request, ("https://example.org/fra-waw",))
                with scoped(ledger, task, run), _capture_worker_results() as bodies:
                    result = await N.run_research(request, config=config)
                require(not result.result.ok, mode+": invalid reader proof reached a successful model result")
                require_equal(method_rows(root, "turn/start"), [], mode)
                require_equal(bodies[0].get("execution_status"), "not_started", mode)
                receipt = _preread_receipt(bodies)
                require_equal(receipt.get("complete_snapshots"), 0, mode)
                require_equal(receipt.get("cleanup"), "closed", mode)
                if mode.startswith("catalog"):
                    require(not any(r.get("method") == "tools/call" for r in _browser_rows(root)))
                require_equal(len(D.invocations(ledger, task)), 1)
                require_equal(D.invocations(ledger, task)[0]["state"], "finished")
    asyncio.run(go())


def t_direct_source_preread_known_tool_failure_and_missing_cleanup_remain_distinct():
    async def go():
        with native_fixture(browser=True, preread="tool_error", timeout=60) as (root, config, ledger, task, run, request):
            _direct_preread_request(request, ("https://example.org/fra-waw",))
            with scoped(ledger, task, run), _capture_worker_results() as bodies:
                result = await N.run_research(request, config=config)
            require(result.result.usable, result.result.reason)
            receipt = _preread_receipt(bodies)
            require_equal(receipt.get("tool_errors"), 1)
            require_equal(receipt.get("complete_snapshots"), 0)
            require_equal(receipt.get("cleanup"), "closed")
            require_equal(len(method_rows(root, "turn/start")), 1)
            note = _one_core_observation(result.result)
            require("Vorablesung" in note and "Fehler" in note)
            prompt = method_rows(root, "turn/start")[0]["params"]["input"][0]["text"]
            require("synthetic-error-never-forward" not in prompt + json.dumps(receipt) + note)
        with native_fixture(browser=True, preread="request_timeout", timeout=60) as (root, config, ledger, task, run, request):
            _direct_preread_request(request, ("https://example.org/first", "https://example.org/slow"))
            with scoped(ledger, task, run), _capture_worker_results() as bodies:
                result = await N.run_research(request, config=config)
            require(result.result.usable, "A known SDK request timeout discarded the verified first page")
            receipt = _preread_receipt(bodies)
            require_equal(receipt.get("status"), "timeout")
            require_equal(receipt.get("complete_snapshots"), 1)
            require_equal(receipt.get("cleanup"), "closed")
            require_equal(receipt.get("started_calls"), receipt.get("completed_calls") + 1)
            require_equal(len(method_rows(root, "turn/start")), 1)
            prompt = method_rows(root, "turn/start")[0]["params"]["input"][0]["text"]
            require("Späte Einschränkung: NICHT buchbar, Gepäck fehlt." in prompt)
            require("synthetic-timeout-never-forward" not in prompt + json.dumps(receipt))
            require_equal(len(D.invocations(ledger, task)), 1)
        for mode in ("missing_ready", "cleanup_unknown"):
            with native_fixture(browser=True, preread=mode, timeout=60) as (root, config, ledger, task, run, request):
                _direct_preread_request(request, ("https://example.org/fra-waw",))
                with scoped(ledger, task, run), _capture_worker_results() as bodies:
                    result = await N.run_research(request, config=config)
                require(not result.result.ok, mode+": empty/unknown teardown proof was treated as closed")
                require_equal(method_rows(root, "turn/start"), [], mode)
                require_equal(bodies[0].get("execution_status"), "not_started", mode)
                require_equal(_preread_receipt(bodies).get("cleanup"), "unknown", mode)
    asyncio.run(go())


def t_direct_source_preread_cancellation_stops_sdk_peer_without_model_or_new_budget():
    async def go():
        with native_fixture(browser=True, preread="hang", timeout=60) as (root, config, ledger, task, run, request):
            _direct_preread_request(request, ("https://example.org/fra-waw",))
            with scoped(ledger, task, run):
                future = asyncio.create_task(N.run_research(request, config=config))
                for _ in range(300):
                    if (root / "reader-blocked").exists():break
                    if future.done():
                        require(False, "The direct reader did not run before the completed native turn")
                    await asyncio.sleep(.02)
                require((root / "reader-blocked").exists(), "Synthetic MCP navigation never began")
                future.cancel()
                try:
                    await future
                except asyncio.CancelledError:
                    pass
            rows = _browser_rows(root)
            require_equal(len(method_rows(root, "turn/start")), 0)
            require_equal(len([r for r in rows if r.get("event") == "spawn"]), 1)
            closed = [r for r in rows if r.get("event") == "closed"]
            require_equal(len(closed), 1, "Cancellation left the separate SDK MCP peer alive")
            require_equal(closed[0]["proof"]["closed"], True)
            require_equal(closed[0]["proof"]["llm_calls"], 0)
            require_equal(closed[0]["stop"], "cancel")
            claims = D.invocations(ledger, task)
            require_equal(len(claims), 1)
            require_equal(claims[0]["state"], "unknown")
            fresh = S.AgentRunLedger(ledger.path)
            with scoped(fresh, task, run):
                blocked = await N.run_research(request, config=config)
            require_equal(blocked.result.reason, "cost_recovery_required")
            require_equal(len(D.invocations(fresh, task)), 1)
            require_equal(len(method_rows(root, "turn/start")), 0)
    asyncio.run(go())
    # A cancel arriving after SDK teardown must still stop before the first
    # model turn. Exercise the actual entry boundary at its redaction step;
    # no process, SDK server, browser or model is started by this subcase.
    from solvio.specialists import hermes_research_worker as W
    with tempfile.TemporaryDirectory(prefix="solvio-postread-cancel-") as directory:
        args = SimpleNamespace(workdir=directory, task_id="at-" + "a" * 16,
            timeout=60.0, browser_python="synthetic-python", hermes_source="synthetic-source",
            browser_bin="synthetic-browser", browser_chrome="synthetic-chrome")
        envelope = {"urls": ["https://example.org/first"], "run_id": "ar-" + "b" * 16,
                    "prompt": "Originalauftrag mit allen Einschränkungen"}
        receipt = W._empty_receipt(args.task_id, envelope["run_id"], 1)
        receipt.update(started_calls=2, completed_calls=2, complete_snapshots=1,
                       hash_verified_snapshots=1, cleanup="closed", status="completed")
        body = json.dumps({"receipt": receipt, "materials": [{"source": envelope["urls"][0],
            "text": "Unbestätigter Teilfund, keine Buchbarkeit."}]}).encode()
        child = SimpleNamespace(returncode=0, communicate=lambda *a, **k: (body, b""),
                                poll=lambda: 0)
        def cancel_during_redaction(text, limit):
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            return text
        with patch.object(W.subprocess, "Popen", return_value=child):
            observed, prompt, reason = W._preread(
                SimpleNamespace(safe_text=cancel_during_redaction), args, envelope, time.monotonic())
        require_equal(reason, "cancelled", "Cancel after SDK receipt still admitted the model turn")
        require_equal(prompt, "")
        require_equal(observed["status"], "cancelled")
        require_equal(observed["cleanup"], "closed")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
