"""Real Hermes RPC client and owned processes; only the Codex server is local.

No native account, credential file, provider turn or production store is read.
An observed balance is deliberately not a cost authorization.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from solvio.specialists import openai_usage as U, launcher as L, hermes_native as N

RPC = r'''
import json,os,pathlib,subprocess,sys,time,tomllib
root=pathlib.Path(__file__).parent
mode=(root/'mode').read_text()
count=0
def log(data):
    with (root/'requests.jsonl').open('a') as f: f.write(json.dumps(data)+'\n')
log({'argv':sys.argv[1:],'home':os.environ.get('CODEX_HOME'), 'pid':os.getpid(),
     'leaks':[k for k in ('OPENAI_API_KEY','OPENAI_BASE_URL','ANTHROPIC_API_KEY','HERMES_KANBAN_TASK') if k in os.environ]})
if sys.argv[1]!='app-server': raise RuntimeError('unexpected command')
for line in sys.stdin:
    item=json.loads(line);log(item)
    if 'id' not in item:
        if item.get('method')!='initialized': raise RuntimeError('unexpected notification')
        continue
    method=item['method'];result={}
    if method=='initialize':
        result={'userAgent':'codex-cli 0.147.0'}
        print(json.dumps({'method':'remoteControl/status/changed','params':{
          'status':'connected' if mode=='remote_active' else 'disabled','environmentId':None,
          'installationId':'local-synthetic-machine','serverName':'synthetic-only'}}),flush=True)
    elif method=='account/read':
        count+=1
        account={'type':'chatgpt','planType':'pro','email':'synthetic@example.invalid'}
        if mode=='account_api':account={'type':'apiKey'}
        if mode=='logged_out':account=None
        if mode=='account_changed' and count==2:account['email']='other@example.invalid'
        if mode=='email_missing':account['email']=None
        result={'account':account,'requiresOpenaiAuth':True}
    elif method=='config/read':
        config=tomllib.loads((pathlib.Path(os.environ['CODEX_HOME'])/'config.toml').read_text())
        if mode=='config_api':config['model_provider']='different-provider'
        result={'config':config,'layers':[{'config':config}]}
    elif method=='account/rateLimits/read':
        if mode=='sleep':
            child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])
            (root/'child.pid').write_text(str(child.pid))
            (root/'ready').write_text('ready')
            time.sleep(60)
        if mode=='oversize':print('x'*300000,flush=True);continue
        if mode=='duplicate_json':
            print('{"id":'+str(item['id'])+',"result":{},"result":{}}',flush=True);continue
        if mode=='server_request':
            print(json.dumps({'id':987,'method':'account/chatgptAuthTokens/refresh','params':{}}),flush=True);continue
        if mode=='notification':
            print(json.dumps({'method':'thread/started','params':{}}),flush=True)
        result=json.loads((root/'limits.json').read_text())
    else:raise RuntimeError('unexpected method '+method)
    print(json.dumps({'id':item['id'],'result':result}),flush=True)
'''


def limits():
    codex={"limitId":"codex", "planType":"pro", "primary":{"usedPercent":3,
        "windowDurationMins":300,"resetsAt":1789650000}, "secondary":None,
        "credits":{"hasCredits":False,"unlimited":False,"balance":"0"},
        "spendControlReached":False,"individualLimit":None,"rateLimitReachedType":None}
    other={"limitId":"codex_other", "planType":"pro", "primary":{"usedPercent":7},
           "credits":None}
    return {"rateLimits":codex,"rateLimitsByLimitId":{"codex":dict(codex),"codex_other":other}}


@contextmanager
def fixture(mode="ok", *, timeout=2.0):
    with tempfile.TemporaryDirectory(prefix="solvio-openai-reader-test-") as directory:
        root=Path(directory);home=root/'native-home';home.mkdir(mode=0o700)
        (home/'config.toml').write_text(N.native_config_text('gpt-local-test'))
        exe=root/'codex-local';exe.write_text('#!'+sys.executable+'\n'+RPC);exe.chmod(0o700)
        (root/'mode').write_text(mode);(root/'limits.json').write_text(json.dumps(limits()))
        sha=hashlib.sha256(exe.read_bytes()).hexdigest()
        build=replace(U.REVIEWED_BUILD,executable=str(exe),native=str(exe),
                      executable_sha256=sha,native_sha256=sha)
        invocation=L.Invocation(str(exe),('exec','--json'),timeout=10,cwd=str(root),codex_home=str(home))
        reader=U.NativeUsageReader(_build=build,_timeout=timeout)
        yield root,home,invocation,reader,build


def observed(root):
    path=root/'requests.jsonl'
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def t_real_native_read_has_only_five_read_requests_and_no_identity_output():
    async def go():
        with fixture() as (root,home,invocation,reader,build):
            with patch.dict(os.environ,{"OPENAI_API_KEY":"synthetic-only","HERMES_KANBAN_TASK":"synthetic-only"}):
                result=await reader.read(invocation)
            require_equal(result.state,'observed')
            require_equal(result.auth_type,'chatgpt');require_equal(result.plan_type,'pro')
            require(result.empty_credit_balance)
            rows=observed(root)
            require_equal([r['method'] for r in rows if 'id' in r],
                ['initialize','account/read','config/read','account/rateLimits/read','account/read'])
            require_equal(rows[0]['home'],str(home.resolve()));require_equal(rows[0]['leaks'],[])
            for row in rows:
                if row.get('method')=='account/read':require_equal(row['params'],{'refreshToken':False})
                if row.get('method')=='config/read':require_equal(row['params']['cwd'],str(root.resolve()))
            public=json.dumps(asdict(result))
            require('synthetic@example.invalid' not in public and str(home) not in public)
            require(not hasattr(result,'upper_bound_cents') and not hasattr(reader,'quote'))
            require(not any(r.get('method','').startswith(('turn/','thread/','account/login')) for r in rows))
    asyncio.run(go())


def t_missing_optional_fields_remain_unknown_instead_of_zero():
    async def go():
        with fixture() as (root,home,invocation,reader,build):
            body=limits()
            body['rateLimits']['credits']=None
            body['rateLimits']['spendControlReached']=None
            body['rateLimitsByLimitId']['codex']=body['rateLimits']
            (root/'limits.json').write_text(json.dumps(body))
            result=await reader.read(invocation)
            require_equal(result.state,'observed');require(result.empty_credit_balance is False)
            require(result.codex_credits is None)
            require(result.snapshots['codex']['spendControlReached'] is None)
            require(result.snapshots['codex_other']['credits'] is None)
    asyncio.run(go())


def t_positive_unlimited_or_unknown_balance_never_means_empty_credit_balance():
    for credits in ({'hasCredits':True,'unlimited':False,'balance':'8.5'},
                    {'hasCredits':False,'unlimited':True,'balance':'0'},
                    {'hasCredits':False,'unlimited':False,'balance':None},
                    {'hasCredits':False,'unlimited':False,'balance':'1'}):
        value=U.UsageObservation(state='observed',snapshots_json=json.dumps({'codex':{'credits':credits}}))
        require(value.empty_credit_balance is False)
    snapshot={'codex':{'credits':{'hasCredits':False,'unlimited':False,'balance':'0'}},
              'other':{'credits':{'hasCredits':True,'unlimited':False,'balance':'1'}}}
    require(not U.UsageObservation(state='observed',snapshots_json=json.dumps(snapshot)).empty_credit_balance)


def t_strict_credit_spend_and_plan_shapes_reject_coercion_or_contradictory_views():
    mutations=[
        lambda x:x['rateLimits']['credits'].update(hasCredits=0),
        lambda x:x['rateLimits']['credits'].update(unlimited='false'),
        lambda x:x['rateLimits']['credits'].update(balance='NaN'),
        lambda x:x['rateLimits']['credits'].update(balance='-1'),
        lambda x:x['rateLimits'].update(spendControlReached=0),
        lambda x:x['rateLimits'].update(primary={'usedPercent':True}),
        lambda x:x['rateLimits'].update(primary={'usedPercent':101}),
        lambda x:x['rateLimits'].update(individualLimit={'limit':'0'}),
        lambda x:x['rateLimitsByLimitId']['codex'].update(planType='plus'),
        lambda x:x['rateLimitsByLimitId']['codex'].update(limitId='foreign'),
        lambda x:x['rateLimitsByLimitId'].pop('codex'),
    ]
    for mutate in mutations:
        body=limits();mutate(body)
        try:U._limits(body,'pro')
        except ValueError:pass
        else:raise AssertionError('malformed native snapshot was accepted')


def t_native_spend_limit_is_preserved_without_assuming_currency_or_enforcement():
    body=limits()
    spend={'limit':'100.00','used':'8.12','remainingPercent':92,'resetsAt':1789650000}
    body['rateLimits']['individualLimit']=spend
    body['rateLimitsByLimitId']['codex']=body['rateLimits']
    result=U._limits(body,'pro')
    require_equal(result['codex']['individualLimit'],spend)
    require('currency' not in result['codex']['individualLimit'])


def t_wrong_auth_missing_identity_or_switched_account_never_yields_observation():
    async def go():
        for mode in ('account_api','logged_out','email_missing','account_changed','config_api'):
            with fixture(mode) as (root,home,invocation,reader,build):
                result=await reader.read(invocation)
                require_equal(result.state,'unknown',mode)
                require(not result.empty_credit_balance)
    asyncio.run(go())


def t_unexpected_native_frames_and_oversized_bytes_fail_closed():
    async def go():
        for mode in ('oversize','duplicate_json','server_request','notification','remote_active'):
            with fixture(mode,timeout=.7) as (root,home,invocation,reader,build):
                result=await reader.read(invocation)
                require_equal(result.state,'unknown',mode)
                require(not result.empty_credit_balance)
    asyncio.run(go())


def t_changed_binary_or_unsupported_inference_provider_stops_before_native_read():
    async def go():
        with fixture() as (root,home,invocation,reader,build):
            for args in (('exec','--oss'),('exec','-c','model_provider="other"'),
                         ('exec','-c','forced_login_method="api"'),
                         ('exec','-cmodel_provider="other"'),('exec','--profile','paid'),
                         ('exec','--profile=paid'),('exec','-ppaid')):
                result=await reader.read(replace(invocation,argv=args))
                require_equal(result.state,'unknown')
            require_equal(observed(root),[])
            Path(build.native).write_text('changed')
            require_equal((await reader.read(invocation)).state,'unknown')
            require_equal(observed(root),[])
    asyncio.run(go())


def t_native_hermes_invocation_uses_its_exact_home_and_rejects_rebinding():
    async def go():
        with fixture() as (root,home,invocation,reader,build):
            config=N.NativeResearchConfig(build.hermes_python,build.hermes_source,build.native,
                                           str(home),'gpt-local-test',10,1)
            native=N.worker_invocation(config,str(root))
            require_equal(Path(native.argv[2]).resolve(),
                          Path(U.__file__).with_name('hermes_research_worker.py').resolve())
            result=await reader.read(native)
            require_equal(result.state,'observed')
            changed=replace(native,codex_home=str(root))
            require_equal((await reader.read(changed)).state,'unknown')
            copied=root/'hermes_research_worker.py';copied.write_text('# synthetic unused entry\n')
            foreign=replace(native,argv=(*native.argv[:2],str(copied),*native.argv[3:]))
            require_equal((await reader.read(foreign)).state,'unknown')
            require_equal((await reader.read(replace(native,
                argv=native.argv+('--extra','yes')))).state,'unknown')
            require_equal((await reader.read(replace(native,
                argv=native.argv+('--codex-home',str(home))))).state,'unknown')
            with patch.object(U,'REVIEWED_BUILD',build):
                require(result.applies_to(native))
                require(not result.applies_to(foreign))
                # These fields remain inside the actual invocation digest,
                # even when the native home and account are unchanged.
                for rebound in (replace(native,timeout=native.timeout+1),
                                replace(native,cleanup_group=False),
                                replace(native,shutdown_grace=0)):
                    require(not result.applies_to(rebound))
            # The old durable entry is still recognized, but its arguments
            # cannot move to the larger ephemeral entry.
            durable=N.continuation_invocation(replace(config,timeout_s=10.0),str(root),
                                             task_id='at-'+'a'*16)
            require_equal(Path(durable.argv[2]).resolve(),
                          Path(U.__file__).with_name('hermes_native_worker.py').resolve())
            require_equal((await reader.read(durable)).state,'observed')
            transplanted=replace(durable,argv=(*durable.argv[:2],native.argv[2],*durable.argv[3:]))
            require_equal((await reader.read(transplanted)).state,'unknown')
            require_equal(len([r for r in observed(root) if r.get('method')=='initialize']),2)
    asyncio.run(go())


def t_observation_expires_and_binds_actual_invocation_and_config_metadata():
    async def go():
        with fixture() as (root,home,invocation,reader,build):
            result=await reader.read(invocation)
            with patch.object(U,'REVIEWED_BUILD',build):
                require(result.applies_to(invocation))
                require(not result.applies_to(replace(invocation,argv=('exec','different'))))
                require(not replace(result,observed_at=time.monotonic()-6).applies_to(invocation))
                require(not replace(result,observed_at=time.monotonic()+6).applies_to(invocation))
                (home/'config.toml').write_text('model_provider="other"\n')
                require(not result.applies_to(invocation))
    asyncio.run(go())


def _gone(pid):
    try:os.kill(pid,0)
    except ProcessLookupError:return True
    return False


def t_timeout_or_cancellation_cleans_the_actual_native_process_group():
    async def go(cancel):
        with fixture('sleep',timeout=3 if cancel else .4) as (root,home,invocation,reader,build):
            task=asyncio.create_task(reader.read(invocation))
            for _ in range(200):
                if (root/'ready').exists():break
                await asyncio.sleep(.01)
            require((root/'ready').exists(),'local native process never entered read')
            if cancel:
                task.cancel()
                try:await task
                except asyncio.CancelledError:pass
                else:raise AssertionError('cancel did not propagate')
            else:require_equal((await task).state,'unknown')
            parent=observed(root)[0]['pid'];child=int((root/'child.pid').read_text())
            for _ in range(100):
                if _gone(parent) and _gone(child):break
                await asyncio.sleep(.02)
            require(_gone(parent) and _gone(child),'owned native processes survived cleanup')
    asyncio.run(go(False));asyncio.run(go(True))


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
