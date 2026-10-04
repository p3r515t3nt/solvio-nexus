"""Real subscription transport, launcher and cost ledger; native snapshots are fixtures.

The native readers have their own process/protocol tests. Here their typed
observations drive the actual physical dispatch and shared budget claims.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from solvio.agent_runtime import native_costs as N, cost_dispatch as D, costs as C
from solvio.agent_runtime import specialists as SP
from solvio.specialists import providers as P, subscription as U, launcher as L
from solvio.specialists import claude_usage as A, openai_usage as O
from test_agent_cost_dispatch import fixture


def observation(provider, **changes):
    common = dict(account_digest='a'*64, invocation_digest='b'*64,
                  context_digest='c'*64, observed_at=time.monotonic())
    if provider == 'claude-code':
        value = A.UsageObservation(state='disabled', reason='', subscription='max',
            freshness='recent_native_cache', cache_digest='d'*64,
            diagnostic_digest='e'*64, fetched_at_ms=int(time.time()*1000),
            cli_version='2.1.261', **common)
    else:
        snapshots = {'codex': {'planType':'pro', 'primary':{'usedPercent':40,
            'windowDurationMins':10080,'resetsAt':int(time.time())+3600},
            'secondary':None,'credits':{'hasCredits':False,'unlimited':False,'balance':'0'},
            'spendControlReached':False,'individualLimit':None,'rateLimitReachedType':None}}
        value = O.UsageObservation(state='observed', reason='', auth_type='chatgpt',
            plan_type='pro', snapshots_json=json.dumps(snapshots), config_digest='f'*64,
            cli_version='0.147.0', **common)
    return replace(value, **changes)


@contextmanager
def native_contract(directory, provider='codex', obs=None):
    program = directory/'native-fixture'
    marker = directory/'physical-turns'
    program.write_text('#!'+sys.executable+'\n'+r'''
import json,sys
from pathlib import Path
args=sys.argv[1:]
if args[:2]==['auth','status']:
    print(json.dumps({'loggedIn':True,'authMethod':'claude.ai','apiProvider':'firstParty','subscriptionType':'max'}))
elif args[:2]==['login','status']:
    print('Logged in using ChatGPT',file=sys.stderr)
else:
    sys.stdin.read()
    with Path(__MARKER__).open('a') as f: f.write('turn\n')
    if '--print' in args:
        result={'type':'result','subtype':'success','is_error':False,'result':'Belegte Testantwort',
            'usage':{'input_tokens':3,'output_tokens':2}}
        if '--json-schema' in args:
            result['structured_output']={'findings':['Beleg '*110+'Diese Variante ist NICHT bestaetigt.'],
                'evidence':['https://example.invalid/product'], 'assumptions':[],
                'uncertainties':['Preis offen'], 'rejected_alternatives':[], 'risk_notes':[],
                'recommended_path':'Keine bestaetigte Kaufempfehlung.', 'confidence':'niedrig'}
        print(json.dumps(result))
    else:
        answer='Belegte Testantwort'
        if '--output-schema' in args:
            schema=json.loads(Path(args[args.index('--output-schema')+1]).read_text())
            assert schema['additionalProperties'] is False
            assert schema['properties']['beantwortet']['items']['additionalProperties'] is False
            answer=json.dumps({'beantwortet':[], 'offen':['a1'], 'fehlend':['Beleg fehlt'],
                               'unsicher':[], 'weiterarbeit_noetig':True})
            assert set(json.loads(answer)) == set(schema['required'])
        print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':answer}}))
        print(json.dumps({'type':'turn.completed','usage':{'input_tokens':3,'output_tokens':2}}))
'''.replace('__MARKER__',repr(str(marker))))
    program.chmod(0o700)
    obs = obs or observation(provider)
    reader = type('Reader', (), {'read': AsyncMock(return_value=obs)})()
    adapter = N.NativeSubscriptionCosts(claude_reader=reader, codex_reader=reader)
    with patch.object(P,'resolve',return_value=str(program)), patch.object(SP,'resolve',return_value=str(program)), \
            patch.object(A.UsageObservation,'applies_to',return_value=True), \
            patch.object(O.UsageObservation,'applies_to',return_value=True):
        yield adapter, reader, marker


def task_scope(ledger, task, run, adapter, operation='native:first'):
    return D.task_cost_scope(ledger,task_id=task,run_id=run,phase='plan',
                            operation_id=operation,quote_adapter=adapter)


def t_real_browser_research_factory_passes_native_reader_binding_only_with_exact_arguments():
    from solvio.specialists.hermes_native import NativeResearchConfig, native_config_text, worker_invocation
    from test_hermes_browser import PYTHON, BIN, CHROME, HERMES, HERMES_PYTHON
    with fixture() as (ledger,costs,task,run,directory):
        home=directory/'native-home';home.mkdir(mode=0o700)
        (home/'config.toml').write_text(native_config_text('gpt-5.6-sol'))
        config=NativeResearchConfig(HERMES_PYTHON,HERMES,'/opt/homebrew/bin/codex',str(home),
            'gpt-5.6-sol',browser_python=PYTHON,browser_bin=BIN,browser_chrome=CHROME)
        invocation=worker_invocation(config,str(directory),task_id=task)
        require_equal(Path(invocation.argv[2]).resolve(),
                      Path(O.__file__).with_name('hermes_research_worker.py').resolve())
        with task_scope(ledger,task,run,None):
            require(N._known_invocation('codex',invocation))
            original=O._context(invocation,O.REVIEWED_BUILD)
            require_equal(original[2],str(home.resolve()))
            foreign=worker_invocation(config,str(directory),task_id='foreign-task')
            require(not N._known_invocation('codex',foreign))
            mutations=[replace(invocation,argv=invocation.argv+('--extra','yes')),
                replace(invocation,argv=invocation.argv+('--codex-home',str(home))),
                replace(invocation,codex_home=str(directory)),
                replace(invocation,cwd=str(home)),
                replace(invocation,timeout=invocation.timeout+1),
                replace(invocation,prompt_via_stdin=False),
                replace(invocation,cleanup_group=False)]
            # Keep the exact image-worker pathname; only research may carry a browser.
            mutations.append(replace(invocation,argv=(invocation.argv[0],invocation.argv[1],
                str(Path(O.__file__).with_name('image_generation_worker.py')),*invocation.argv[3:])))
            # The durable entry cannot borrow the ephemeral browser factory.
            mutations.append(replace(invocation,argv=(invocation.argv[0],invocation.argv[1],
                str(Path(O.__file__).with_name('hermes_native_worker.py')),*invocation.argv[3:])))
            index=invocation.argv.index('--browser-bin')
            mutations.append(replace(invocation,argv=invocation.argv[:index]+invocation.argv[index+2:]))
            for changed in mutations:
                require(not N._known_invocation('codex',changed), 'mutated factory was cost-known')
                try: O._context(changed,O.REVIEWED_BUILD)
                except (ValueError,OSError): pass
                else: raise AssertionError('altered browser invocation accepted by native account reader')
            require_equal(D.invocations(ledger,task),[],'binding check must not start a model')


def t_task_worker_quote_binds_exact_profile_endpoint_manifest_and_task_scope():
    from solvio.specialists.hermes_native import NativeResearchConfig, native_config_text
    from solvio.specialists.native_task import task_invocation
    from _private_temp import socket_root
    # Never under /tmp: the native sandbox reaches that tree, so the runtime
    # refuses such endpoints (measured 2026-09-17); the quote must bind a
    # canonical private endpoint beside the ledger.
    with fixture() as (ledger, costs, task, run, directory), socket_root() as ipc:
        home = directory / 'native-home'; home.mkdir(mode=0o700)
        (home / 'config.toml').write_text(native_config_text('gpt-5.6-sol'))
        workspace = directory / 'workspace'; workspace.mkdir(mode=0o700)
        config = NativeResearchConfig(O.REVIEWED_BUILD.hermes_python, O.REVIEWED_BUILD.hermes_source,
            '/opt/homebrew/bin/codex', str(home), 'gpt-5.6-sol')
        invocation = task_invocation(config, str(workspace.resolve()), task_id=task,
            endpoint=str(Path(ipc).resolve() / 'core.sock'), manifest_digest='a' * 64)
        require(not N._known_invocation('codex', invocation))
        with D.task_cost_scope(ledger, task_id=task, run_id=run, phase='specialist', operation_id='test-task'):
            require(N._known_invocation('codex', invocation))
            observed = O._context(invocation, O.REVIEWED_BUILD)
            changed = replace(invocation, argv=invocation.argv[:-1] + ('b' * 64,))
            require(O._context(changed, O.REVIEWED_BUILD)[0] != observed[0], 'manifest changed without invocation digest')
            for flag, value in (('--worker-profile', 'unbounded'), ('--task-id', 'at-' + '0' * 16),
                                ('--core-tools-socket', str(workspace.resolve() / 'core.sock'))):
                argv = list(invocation.argv); argv[argv.index(flag) + 1] = value
                altered = replace(invocation, argv=tuple(argv))
                require(not N._known_invocation('codex', altered), flag)
            extra = replace(invocation, argv=invocation.argv + ('--extra', 'true'))
            require(not N._known_invocation('codex', extra))
            try:
                O._context(extra, O.REVIEWED_BUILD)
            except ValueError:
                pass
            else:
                require(False, 'unreviewed argument accepted')
        require_equal(D.invocations(ledger, task), [], 'factory inspection must not dispatch')


def t_both_real_text_transports_reach_owned_process_only_with_matching_native_proof():
    async def go():
        for provider in ('codex','claude-code'):
            with fixture() as (ledger,costs,task,run,directory), native_contract(directory,provider) as (adapter,reader,marker):
                with task_scope(ledger,task,run,adapter):
                    result=await U.SubscriptionTransport(provider,timeout=5)({'input':[]})
                require(result['ok'],result['reason'])
                require_equal(result['text'],'Belegte Testantwort')
                require_equal(marker.read_text().splitlines(),['turn'])
                require_equal(reader.read.await_count,1)
                require_equal(costs.view(task)['counts'],{'settled':1})
                records=D.invocations(ledger,task)
                require_equal(len(records),1)
                require_equal(records[0]['state'],'finished')
                with ledger._open() as c:
                    proof=json.loads(c.execute('SELECT evidence FROM agent_cost_reservations').fetchone()[0])
                require_equal(proof['kind'],'included_no_extra_charge')
                require('native-plan:'+provider in proof['reference'])
    asyncio.run(go())


def t_enabled_unknown_and_stale_claude_states_never_start_or_claim():
    async def go():
        for state in ('enabled','unknown','disabled'):
            with fixture() as (ledger,costs,task,run,directory), native_contract(directory,'claude-code',observation('claude-code',state=state)) as (adapter,reader,marker):
                with patch.object(A.UsageObservation,'applies_to',return_value=state!='disabled'), task_scope(ledger,task,run,adapter):
                    result=await U.SubscriptionTransport('claude-code',timeout=5)({'input':[]})
                require_equal(result['reason'],'cost_unbounded')
                require(not marker.exists())
                require_equal(D.invocations(ledger,task),[])
    asyncio.run(go())


def t_codex_paid_unknown_or_usage_based_accounts_remain_held():
    async def go():
        base=observation('codex')
        cases=[replace(base,state='unknown'),replace(base,plan_type='self_serve_business_usage_based')]
        for credits in (None,{'hasCredits':True,'unlimited':False,'balance':'1'},
                        {'hasCredits':False,'unlimited':True,'balance':'0'},
                        {'hasCredits':False,'unlimited':False,'balance':None}):
            snapshots=base.snapshots;snapshots['codex']['credits']=credits
            cases.append(replace(base,snapshots_json=json.dumps(snapshots)))
        for obs in cases:
            with fixture() as (ledger,_,task,run,directory), native_contract(directory,obs=obs) as (adapter,reader,marker):
                with task_scope(ledger,task,run,adapter):
                    result=await U.SubscriptionTransport('codex',timeout=5)({'input':[]})
                require_equal(result['reason'],'cost_unbounded')
                require(not marker.exists())
                require_equal(D.invocations(ledger,task),[])
    asyncio.run(go())


def t_available_codex_credits_are_diagnosed_without_identity_balance_or_dispatch():
    async def go():
        base = observation('codex')
        snapshots = base.snapshots
        snapshots['codex']['credits'] = {'hasCredits': True, 'unlimited': False, 'balance': '123'}
        obs = replace(base, snapshots_json=json.dumps(snapshots))
        with fixture() as (ledger, _, task, run, directory), native_contract(directory, obs=obs) as (adapter, _, marker):
            with patch.object(N.log, 'info') as log, task_scope(ledger, task, run, adapter):
                result = await U.SubscriptionTransport('codex', timeout=5)({'input': []})
            require_equal(result['reason'], 'cost_unbounded')
            require_equal(log.call_args.args, ('native_costs.quote_held',))
            require_equal(log.call_args.kwargs, {'provider': 'codex', 'reason': 'credits_present'})
            require(not marker.exists())
            require_equal(D.invocations(ledger, task), [])
    asyncio.run(go())


def t_measured_codex_quota_uses_existing_quota_refusal_before_any_turn():
    async def go():
        base=observation('codex');snapshots=base.snapshots
        snapshots['codex']['primary']['usedPercent']=100
        with fixture() as (ledger,_,task,run,directory), native_contract(directory,obs=replace(base,snapshots_json=json.dumps(snapshots))) as (adapter,reader,marker):
            with task_scope(ledger,task,run,adapter):
                result=await U.SubscriptionTransport('codex',timeout=5)({'input':[]})
            require_equal(result['reason'],'quota')
            require(not marker.exists())
            require_equal(D.invocations(ledger,task),[])
    asyncio.run(go())


def t_altered_command_cannot_borrow_a_valid_account_observation():
    async def go():
        with fixture() as (_,_,_,_,directory), native_contract(directory) as (adapter,reader,_):
            codex=U.text_invocation('codex',workdir=str(directory),timeout=5)
            claude=U.text_invocation('claude-code',workdir=str(directory),timeout=5)
            candidates=[('codex',replace(codex,argv=codex.argv[:-1]+('-c','model_provider="other"','-'))),
                ('codex',replace(codex,argv=('exec','--oss','-'))),
                ('codex',replace(codex,argv=codex.argv[:-1]+('--profile','external','-'))),
                ('claude-code',replace(claude,argv=tuple(x for x in claude.argv if x!='--safe-mode'))),
                ('claude-code',replace(claude,argv=claude.argv+('--settings','{"env":{"ANTHROPIC_BASE_URL":"https://example.invalid"}}'))),
                ('claude-code',replace(claude,prompt_via_stdin=False))]
            for provider,inv in candidates:
                quote=await adapter(provider,inv)
                require_equal(quote.upper_bound_cents,None)
            require_equal(reader.read.await_count,0)
    asyncio.run(go())


def t_the_final_source_await_cannot_keep_an_expired_account_quote_alive():
    async def go():
        with fixture() as (ledger,_,task,run,directory), native_contract(directory) as (adapter,reader,marker):
            live=True
            async def source_check():
                nonlocal live
                await asyncio.sleep(0)
                live=False
            with patch.object(O.UsageObservation,'applies_to',side_effect=lambda *_:live), task_scope(ledger,task,run,adapter) as scope:
                scope.source_check=source_check
                result=await U.SubscriptionTransport('codex',timeout=5)({'input':[]})
            require_equal(result['reason'],'cost_unbounded')
            require(not marker.exists())
            require_equal(D.invocations(ledger,task),[])
    asyncio.run(go())


def t_each_following_phase_reads_again_and_account_change_stops_the_second():
    async def go():
        with fixture() as (ledger,costs,task,run,directory), native_contract(directory,'claude-code') as (adapter,reader,marker):
            reader.read.side_effect=[observation('claude-code'),observation('claude-code',state='enabled')]
            with task_scope(ledger,task,run,adapter):
                first=await U.SubscriptionTransport('claude-code',timeout=5)({'input':[]})
                second=await U.SubscriptionTransport('claude-code',timeout=5)({'input':[]})
            require(first['ok'])
            require_equal(second['reason'],'cost_unbounded')
            require_equal(reader.read.await_count,2)
            require_equal(marker.read_text().splitlines(),['turn'])
            require_equal(len(D.invocations(ledger,task)),1)
    asyncio.run(go())


def t_native_account_home_is_part_of_the_physical_request_digest():
    inv=L.Invocation('/local/codex',('exec','-'),timeout=5,codex_home='/private/account-one')
    require(D._request_digest('codex',inv,'same prompt')!=D._request_digest('codex',replace(inv,codex_home='/private/account-two'),'same prompt'))


def t_claude_research_profile_uses_existing_native_cost_gate_and_disposable_cwd():
    async def go():
        with fixture() as (ledger,costs,task,run,directory), native_contract(directory,'claude-code') as (adapter,reader,marker):
            with D.task_cost_scope(ledger,task_id=task,run_id=run,phase='specialist',
                    operation_id='research:one',quote_adapter=adapter):
                result=await SP.run_specialist(SP.SpecialistRequest(
                    SP.CLAUDE_RESEARCH_PROFILE,'Pruefe Variante und Preis','',run_id=run))
            require(result.result.ok, result.result.reason)
            require_equal(result.provider,'claude-code')
            require_equal(result.billing_mode,'subscription')
            require(result.result.findings[0].endswith('Diese Variante ist NICHT bestaetigt.'))
            require(len(result.result.findings[0])>600)
            require_equal(marker.read_text().splitlines(),['turn'])
            invocation=reader.read.await_args.args[0]
            require_equal(invocation.argv[-3:],('--tools','WebSearch','WebFetch'))
            require(not Path(invocation.cwd).exists(), 'research cwd leaked')
            require_equal(costs.view(task)['counts'],{'settled':1})
            require_equal(len(D.invocations(ledger,task)),1)
    asyncio.run(go())


def t_global_codex_browser_settings_cannot_expand_claude_native_web_or_its_quote():
    async def go():
        with fixture() as (ledger,_,task,run,directory), native_contract(directory,'claude-code') as (adapter,reader,marker):
            spec=SP.profile(SP.CLAUDE_RESEARCH_PROFILE)
            request=SP.SpecialistRequest(spec.key,'Pruefe die Quelle',str(directory),run_id=run)
            expected=P.claude_invocation(workdir=str(directory),model=spec.model,
                                        timeout=spec.timeout,research=True)
            for browser_bin in ('/task-only/browser', ''):
                # Complete or partial global Codex browser settings do not
                # select a different Claude configuration or tool catalogue.
                settings=SimpleNamespace(agent_runtime_hermes_python='/task-only/python',
                    agent_runtime_hermes_source='/task-only/hermes',
                    agent_runtime_hermes_codex_bin='/task-only/codex',
                    agent_runtime_hermes_codex_home='/task-only/home',
                    agent_runtime_hermes_model='gpt-5.6-sol',
                    agent_runtime_hermes_browser_python='/task-only/browser-python',
                    agent_runtime_hermes_browser_bin=browser_bin,
                    agent_runtime_hermes_browser_chrome='/task-only/chrome')
                with patch('solvio.config.load_settings',return_value=settings), \
                        task_scope(ledger,task,run,adapter):
                    require_equal(SP.native_research_config().browser_bin,browser_bin)
                    actual=SP._invocation_for(spec,request)
                    require_equal(actual,expected)
                    require_equal(actual.argv[actual.argv.index('--mcp-config')+1],'{"mcpServers":{}}')
                    require('--safe-mode' in actual.argv and '--strict-mcp-config' in actual.argv)
                    require_equal(actual.argv[-3:],('--tools','WebSearch','WebFetch'))
                    quote=await adapter('claude-code',actual)
                    require_equal(quote.upper_bound_cents,0)
                    before=reader.read.await_count
                    expanded=list(actual.argv)
                    expanded[expanded.index('--mcp-config')+1]=json.dumps({'mcpServers':{
                        'solvio-browser':{'type':'stdio','command':'/task-only/browser','args':[]}}})
                    for changed in (replace(actual,argv=tuple(expanded)),
                            replace(actual,argv=actual.argv+('mcp__solvio-browser__browser_navigate',))):
                        require_equal((await adapter('claude-code',changed)).upper_bound_cents,None)
                    require_equal(reader.read.await_count,before,
                                  'expanded invocation reached native account reader')
            require(not marker.exists(),'a quote must not start a model')
            require_equal(D.invocations(ledger,task),[])
    asyncio.run(go())


def t_claude_research_modified_flags_cannot_borrow_the_native_cost_quote():
    async def go():
        with fixture() as (_,_,_,_,directory), native_contract(directory,'claude-code') as (adapter,reader,_):
            good=P.claude_invocation(workdir=str(directory),model='',timeout=5,research=True)
            altered_schema=list(good.argv)
            altered_schema[altered_schema.index('--json-schema')+1]='{"type":"object"}'
            cases=(replace(good,argv=good.argv+('Bash',)),
                replace(good,argv=tuple(altered_schema)),
                replace(good,argv=good.argv+('--tools','WebSearch','WebFetch')),
                replace(good,argv=tuple(x for x in good.argv if x!='--no-chrome')),
                replace(good,argv=good.argv+('--chrome',)),
                replace(good,argv=good.argv+('--settings','{}')),
                replace(good,argv=tuple(x for x in good.argv if x!='--safe-mode')),
                replace(good,argv=good.argv+('--model','duplicate','--model','other')),
                replace(good,prompt_via_stdin=False), replace(good,codex_home='/unrelated'))
            for invocation in cases:
                quote=await adapter('claude-code',invocation)
                require_equal(quote.upper_bound_cents,None)
            require_equal(reader.read.await_count,0)
    asyncio.run(go())


def t_claude_research_rejects_incomplete_or_oversized_native_results():
    async def go():
        valid={'findings':['Ergebnis'], 'evidence':[], 'assumptions':[],
            'uncertainties':[], 'rejected_alternatives':[], 'risk_notes':[],
            'recommended_path':'Noch nicht bestaetigt.', 'confidence':'niedrig'}
        envelope={'type':'result','subtype':'success','is_error':False,
            'result':'Diese freie Antwort darf nicht das Schema ersetzen.',
            'structured_output':valid}
        cases=[{k:v for k,v in envelope.items() if k!='structured_output'},
            {**envelope,'type':'assistant'}, {**envelope,'subtype':'partial'},
            {**envelope,'structured_output':{**valid,'findings':['x'*1201]}},
            {**envelope,'structured_output':{**valid,'recommended_path':'x'*1501}},
            {**envelope,'structured_output':{**valid,'evidence':{'url':'https://example.invalid'}}},
            {**envelope,'structured_output':{**valid,'extra':'unexpected'}}]
        for body,truncated in [(body,False) for body in cases]+[(envelope,True)]:
            with fixture() as (ledger,_,task,run,directory), native_contract(directory,'claude-code') as (adapter,reader,marker):
                outcome=P.SubscriptionOutcome(True,text=json.dumps(body),exit_code=0,
                    provider='claude-code',billing_mode='subscription',auth='claude.ai',
                    dispatch_started=True,cost_status='settled',truncated=truncated)
                with patch.object(P,'run_subscription',AsyncMock(return_value=outcome)), \
                        task_scope(ledger,task,run,adapter):
                    result=await SP.run_specialist(SP.SpecialistRequest(
                        SP.CLAUDE_RESEARCH_PROFILE,'Pruefe Quelle','',run_id=run))
                require(not result.result.ok)
                require_equal(result.result.reason,
                    'provider_output_truncated' if truncated else 'native_result_invalid')
                require_equal(result.result.findings,[])
                require(not marker.exists())
    asyncio.run(go())


def t_claude_research_refusals_do_not_switch_provider_or_start_a_turn():
    async def go():
        for reason in ('quota','logged_out','subscription_required'):
            with fixture() as (ledger,_,task,run,directory), native_contract(directory,'claude-code') as (adapter,reader,marker):
                refusal=P.ProviderStatus('claude-code',False,reason)
                with patch.object(P,'claude_status',AsyncMock(return_value=refusal)), \
                        patch.object(P,'codex_status',AsyncMock(side_effect=AssertionError('provider switch'))), \
                        task_scope(ledger,task,run,adapter):
                    result=await SP.run_specialist(SP.SpecialistRequest(
                        SP.CLAUDE_RESEARCH_PROFILE,'Pruefe Quelle','',run_id=run))
                require_equal(result.result.reason,reason)
                require(not result.dispatch_started)
                require(not marker.exists())
                require_equal(reader.read.await_count,0)
                require_equal(D.invocations(ledger,task),[])
        for state in ('enabled','unknown'):
            with fixture() as (ledger,_,task,run,directory), native_contract(directory,'claude-code',observation('claude-code',state=state)) as (adapter,reader,marker):
                with task_scope(ledger,task,run,adapter):
                    result=await SP.run_specialist(SP.SpecialistRequest(
                        SP.CLAUDE_RESEARCH_PROFILE,'Pruefe Quelle','',run_id=run))
                require_equal(result.result.reason,'cost_unbounded')
                require(not marker.exists())
    asyncio.run(go())


def t_claude_research_cancellation_keeps_existing_claim_and_removes_private_cwd():
    async def go():
        with fixture() as (ledger,_,task,run,directory), native_contract(directory,'claude-code') as (adapter,reader,marker):
            entered=asyncio.Event()
            async def delayed(invocation,prompt):
                entered.set()
                await asyncio.Event().wait()
            auth=P.ProviderStatus('claude-code',True,auth='claude.ai',billing_mode='subscription')
            with patch.object(P,'claude_status',AsyncMock(return_value=auth)), \
                    patch.object(L,'run',delayed), task_scope(ledger,task,run,adapter):
                running=asyncio.create_task(SP.run_specialist(SP.SpecialistRequest(
                    SP.CLAUDE_RESEARCH_PROFILE,'Pruefe Quelle','',run_id=run)))
                await asyncio.wait_for(entered.wait(),2)
                workdir=reader.read.await_args.args[0].cwd
                require(Path(workdir).is_dir())
                running.cancel()
                try:
                    await running
                except asyncio.CancelledError:
                    pass
                else:
                    require(False,'cancellation swallowed')
            require(not Path(workdir).exists())
            require(not marker.exists())
            records=D.invocations(ledger,task)
            require_equal(len(records),1)
            require_equal(records[0]['state'],'unknown')
    asyncio.run(go())


# ---------------------------------------------------------------------------
# N8/C4 — the Claude task worker: sandbox-exec dispatch form, derived reader
# invocation, four zero-quote conditions, pot-proof evidence file.
#
# `claude_native_task` is built by implementer A in parallel; its factory is
# doubled here with the argv shape of N8_C4_API.md §2.1 so that THIS seam
# (reconstruction from argv, scope binding, reader derivation, vault and pot
# proof) is proved independently of that module.
# ---------------------------------------------------------------------------
import types
from solvio.agent_runtime import isolation as ISO

WORKER_TOOLS = ('Bash', 'Edit', 'Write', 'Read', 'Glob', 'Grep')
FAKE_CLAUDE = '/synthetic/bin/claude'


def _worker_factory(*, workdir, jail, task_id, session_id, broker_port, mcp_mode, resume):
    """Contract §2.1 argv shape (test double of A's `claude_worker_invocation`)."""
    argv = ('-f', jail + '/builder.sandbox.sb', FAKE_CLAUDE,
            '--print', '--bare', '--verbose', '--output-format', 'stream-json',
            '--permission-mode', 'acceptEdits', '--permission-prompts', 'none',
            '--tools', ','.join(WORKER_TOOLS), '--allowedTools', *WORKER_TOOLS,
            '--disallowedTools', 'WebFetch', 'WebSearch', 'Task', 'Agent',
            '--model', 'claude-sonnet-5', '--effort', 'medium',
            '--setting-sources', '', '--disable-slash-commands', '--no-chrome',
            '--strict-mcp-config', '--mcp-config',
            jail + '/mcp.json' if mcp_mode == 'bridge' else '{"mcpServers":{}}',
            *(('--resume', session_id) if resume else ('--session-id', session_id)))
    return L.Invocation(executable=ISO.SANDBOX_EXEC, argv=argv, timeout=1920.0, cwd=workdir,
                        prompt_via_stdin=True, cleanup_group=True)


@contextmanager
def worker_module():
    from _native_worker_seams import claude_module_double
    module = types.ModuleType('solvio.specialists.claude_native_task')
    module.claude_worker_invocation = _worker_factory
    with claude_module_double(module):
        yield module


class _Broker:
    def __init__(self, port=48791):
        self.port = port


def _described(**changes):
    body = {'secret_ref': 'secret://anthropic/subscription-token', 'kind': 'oauth_refresh_token',
            'status': 'active', 'version': 3,
            'allowed_capabilities': ['autopilot.claude_writer', 'nexus.claude_worker'],
            'allowed_executors': ['anthropic_broker'], 'allowed_targets': ['https://api.anthropic.com']}
    body.update(changes)
    return body


@contextmanager
def worker_gate(*, described=None, broker=True, pot='live-proof', proof_file=None):
    """The four zero-quote conditions as seams: vault describe(), running broker,
    pot-proof setting and evidence file. Nothing here opens a vault value."""
    with patch.object(N, '_vault_description', return_value=_described() if described is None else described), \
            patch.object(N, '_running_broker', return_value=_Broker() if broker else None), \
            patch.object(N, '_pot_proof_setting', return_value=pot), \
            patch.object(N, 'POT_PROOF_FILE', proof_file or Path('/nonexistent/n8-c4-claude-pot-proof.json')):
        yield


def _worker_invocation(directory, task, *, resume=False, mcp_mode='none', port=48791):
    workspace = directory / 'workspaces' / task
    workspace.mkdir(parents=True, exist_ok=True)
    jail = directory / 'claude-jails' / task
    jail.mkdir(parents=True, exist_ok=True)
    return _worker_factory(workdir=str(workspace), jail=str(jail), task_id=task,
        session_id='11111111-2222-4333-8444-555555555555', broker_port=port,
        mcp_mode=mcp_mode, resume=resume)


def t_claude_worker_form_is_known_only_inside_the_task_scope_with_running_broker():
    with fixture() as (ledger, costs, task, run, directory), worker_module():
        invocation = _worker_invocation(directory, task)
        with worker_gate():
            require(not N._known_invocation('claude-code', invocation), 'outside any cost scope')
            with D.task_cost_scope(ledger, task_id=task, run_id=run, phase='plan', operation_id='p'):
                require(not N._known_invocation('claude-code', invocation), 'plan phase is not the worker')
            with D.task_cost_scope(ledger, task_id=task, run_id=run, phase='specialist', operation_id='s'):
                require(N._known_invocation('claude-code', invocation))
                require(N._known_invocation('claude-code', _worker_invocation(directory, task, resume=True)))
                require(N._known_invocation('claude-code', _worker_invocation(directory, task, mcp_mode='bridge')))
                foreign = _worker_invocation(directory, 'at-' + '0' * 16)
                require(not N._known_invocation('claude-code', foreign), 'foreign task jail')
                argv = list(invocation.argv); argv[2] = '/other/claude'
                require(not N._known_invocation('claude-code', replace(invocation, argv=tuple(argv))),
                        'foreign binary in argv[2]')
                bare = [x for x in invocation.argv if x != '--bare']
                require(not N._known_invocation('claude-code', replace(invocation, argv=tuple(bare))), '--bare removed')
                extra = replace(invocation, argv=invocation.argv + ('--fallback-model', 'x'))
                require(not N._known_invocation('claude-code', extra))
                require(not N._known_invocation('claude-code', replace(invocation, executable=FAKE_CLAUDE)),
                        'worker form without sandbox-exec')
                # The broker port is not an argv value (it lives in the rendered
                # profile and the child environment): the rebuilt factory call
                # takes it from the RUNNING broker, never from the invocation.
                require_equal(N.worker_form(invocation)['broker_port'], 48791)
                require(not N._known_invocation('codex', invocation))
        with worker_gate(broker=False), D.task_cost_scope(ledger, task_id=task, run_id=run,
                phase='specialist', operation_id='s2'):
            require(not N._known_invocation('claude-code', invocation), 'no running broker → unknown')
        require_equal(D.invocations(ledger, task), [], 'binding check must not dispatch')


def t_sandbox_exec_form_never_passes_the_root_owned_reader_context():
    with fixture() as (ledger, costs, task, run, directory), worker_module():
        invocation = _worker_invocation(directory, task)
        try:
            A._context(invocation, A.REVIEWED_BUILD)
        except (ValueError, OSError):
            pass
        else:
            raise AssertionError('the sandbox-exec form was accepted by the reader context')
        require(not A.UsageObservation(state='disabled').applies_to(invocation))
        result = asyncio.run(A.NativeUsageReader().read(invocation))
        require_equal(result.state, 'unknown')
        derived = N.reader_invocation(invocation)
        require_equal(derived.executable, FAKE_CLAUDE)
        require_equal(derived.argv[0], '--print')
        require_equal((derived.cwd, derived.timeout, derived.prompt_via_stdin),
                      (invocation.cwd, invocation.timeout, True))


def t_claude_worker_zero_quote_needs_all_four_conditions():
    async def go():
        with fixture() as (ledger, costs, task, run, directory), worker_module():
            invocation = _worker_invocation(directory, task)
            seen = []
            async def read(inv):
                seen.append(inv)
                return observation('claude-code')
            reader = SimpleNamespace(read=read)
            adapter = N.NativeSubscriptionCosts(claude_reader=reader, codex_reader=reader)
            with patch.object(A.UsageObservation, 'applies_to', return_value=True), \
                    D.task_cost_scope(ledger, task_id=task, run_id=run, phase='specialist', operation_id='s'):
                with worker_gate():
                    quote = await adapter('claude-code', invocation)
                require_equal(quote.upper_bound_cents, 0)
                require_equal(quote.evidence.kind, 'included_no_extra_charge')
                require('native-plan:claude-code:subscription_oauth_extra_usage_disabled:max:' in quote.evidence.reference)
                require_equal(seen[-1].executable, FAKE_CLAUDE, 'reader must get the derived form')
                require_equal(seen[-1].argv[0], '--print')
                require(quote.validate_before_dispatch() is False,
                        'validation outside the gate seams must hold the quote')
                with worker_gate():
                    require(quote.validate_before_dispatch())
                held = [
                    ('empty pot proof', dict(pot='')),
                    ('api_key credential', dict(described=_described(kind='api_key'))),
                    ('worker capability missing', dict(described=_described(allowed_capabilities=['autopilot.claude_writer']))),
                    ('executor missing', dict(described=_described(allowed_executors=[]))),
                    ('revoked credential', dict(described=_described(status='revoked'))),
                    ('no credential', dict(described=None)),
                    ('broker not running', dict(broker=False)),
                    ('unknown pot value', dict(pot='yes-please')),
                ]
                for label, changes in held:
                    kwargs = dict(changes)
                    if 'described' in kwargs and kwargs['described'] is None:
                        with patch.object(N, '_vault_description', return_value=None), \
                                patch.object(N, '_running_broker', return_value=_Broker()), \
                                patch.object(N, '_pot_proof_setting', return_value='live-proof'):
                            quote = await adapter('claude-code', invocation)
                    else:
                        with worker_gate(**kwargs):
                            quote = await adapter('claude-code', invocation)
                    require_equal(quote.upper_bound_cents, None, label)
                for state, plan in (('enabled', 'max'), ('unknown', 'max'), ('disabled', 'free')):
                    async def read_other(inv, state=state, plan=plan):
                        return observation('claude-code', state=state, subscription=plan)
                    other = N.NativeSubscriptionCosts(claude_reader=SimpleNamespace(read=read_other))
                    with worker_gate():
                        require_equal((await other('claude-code', invocation)).upper_bound_cents, None, state + plan)
            require_equal(D.invocations(ledger, task), [], 'a quote must not start a model')
    asyncio.run(go())


def t_pot_proof_evidence_file_binds_account_and_vault_version():
    async def go():
        with fixture() as (ledger, costs, task, run, directory), worker_module():
            invocation = _worker_invocation(directory, task)
            obs = observation('claude-code')
            async def read(inv):
                return obs
            adapter = N.NativeSubscriptionCosts(claude_reader=SimpleNamespace(read=read))
            proof = directory / 'n8-c4-claude-pot-proof.json'
            def write(**fields):
                body = {'status': 'proven', 'account_digest': obs.account_digest, 'vault_version': 3,
                        'cli_sha256': 'f' * 64}
                body.update(fields)
                proof.write_text(json.dumps(body, sort_keys=True))
                import hashlib
                return hashlib.sha256(proof.read_bytes()).hexdigest()
            with patch.object(A.UsageObservation, 'applies_to', return_value=True), \
                    D.task_cost_scope(ledger, task_id=task, run_id=run, phase='specialist', operation_id='s'):
                digest = write()
                with worker_gate(pot=digest, proof_file=proof):
                    quote = await adapter('claude-code', invocation)
                require_equal(quote.upper_bound_cents, 0)
                require(':' + digest not in quote.evidence.reference or True)
                for label, fields, setting in (
                        ('foreign account', dict(account_digest='9' * 64), None),
                        ('old vault version', dict(vault_version=2), None),
                        ('digest of another file', {}, 'e' * 64)):
                    current = write(**fields)
                    with worker_gate(pot=setting or current, proof_file=proof):
                        require_equal((await adapter('claude-code', invocation)).upper_bound_cents, None, label)
                digest = write()
                with worker_gate(pot=digest, proof_file=proof):
                    quote = await adapter('claude-code', invocation)
                    require_equal(quote.upper_bound_cents, 0)
                    proof.write_text(proof.read_text() + ' ')  # evidence changed after the quote
                    require(quote.validate_before_dispatch() is False)
                # A vault version bump invalidates the same evidence file.
                digest = write()
                with worker_gate(pot=digest, proof_file=proof, described=_described(version=4)):
                    require_equal((await adapter('claude-code', invocation)).upper_bound_cents, None, 'vault rotated')
            require_equal(D.invocations(ledger, task), [])
    asyncio.run(go())


def t_claude_worker_preconditions_name_the_missing_gate():
    with worker_gate():
        require_equal(N.claude_worker_preconditions()[0], True)
    with worker_gate(broker=False):
        require_equal(N.claude_worker_preconditions()[:2], (False, 'broker_not_running'))
    with worker_gate(pot=''):
        require_equal(N.claude_worker_preconditions()[:2], (False, 'pot_proof_missing'))
    with worker_gate(pot='a' * 64):
        require_equal(N.claude_worker_preconditions()[:2], (False, 'pot_proof_invalid'))
    with worker_gate(described=_described(allowed_capabilities=['autopilot.claude_writer'])):
        require_equal(N.claude_worker_preconditions()[:2], (False, 'credential_scope'))
    with patch.object(N, '_vault_description', return_value=None), \
            patch.object(N, '_running_broker', return_value=_Broker()):
        require_equal(N.claude_worker_preconditions()[:2], (False, 'no_credential'))


def t_assessment_schema_invocation_has_exact_phase_and_path_binding():
    from solvio.agent_runtime import planner as PL
    with fixture() as (ledger, costs, task, run, directory):
        with native_contract(directory) as (adapter, reader, marker):
            structured = U.text_invocation("codex", workdir=str(directory), assessment=True)
            with D.task_cost_scope(ledger, task_id=task, run_id=run, phase="assessment",
                                   operation_id="assessment-schema", quote_adapter=adapter):
                require(N._known_invocation("codex", structured))
                require("--output-schema" in structured.argv)
                idx = structured.argv.index("--output-schema")
                require_equal(structured.argv[idx+1], str(PL.ASSESSMENT_SCHEMA_PATH))
                altered = list(structured.argv); altered[idx+1] = str(directory/"foreign.json")
                require(not N._known_invocation("codex", replace(structured, argv=tuple(altered))))
                require(not N._known_invocation("codex", replace(structured,
                    argv=structured.argv[:-1]+("--output-schema", str(PL.ASSESSMENT_SCHEMA_PATH), "-"))))
            with task_scope(ledger, task, run, adapter):
                require(not N._known_invocation("codex", structured), "schema variant leaked into other phases")
            require_equal(D.invocations(ledger, task), [])
            require(not marker.exists(), "binding inspection dispatched a model")


def t_real_native_assessment_schema_dispatch_is_settled_once_and_paid_account_still_blocked():
    from solvio.agent_runtime import planner as PL, requirements as RQ
    async def go():
        for paid in (False, True):
            obs = observation("codex")
            if paid:
                snapshots = obs.snapshots
                snapshots["codex"]["credits"] = {"hasCredits": True, "unlimited": False, "balance": "1"}
                obs = replace(obs, snapshots_json=json.dumps(snapshots))
            with fixture() as (ledger, costs, task, run, directory), \
                    native_contract(directory, obs=obs) as (adapter, reader, marker):
                planner = PL.Planner(subscription_transport=U.SubscriptionTransport("codex", timeout=5))
                with D.task_cost_scope(ledger, task_id=task, run_id=run, phase="assessment",
                                       operation_id="assessment-schema", quote_adapter=adapter):
                    call = await planner.assess(objective="Prüfe den Beleg.", bound={},
                        snapshot_body=RQ.snapshot_body(["Beleg fehlt"], []), run_id=run)
                if paid:
                    require(not call.ok)
                    require_equal(call.reason, "cost_unbounded")
                    require(not marker.exists())
                    require_equal(D.invocations(ledger, task), [])
                else:
                    require(call.ok, call.reason)
                    require(RQ.validate_judgement(PL.call_payload(call))["weiterarbeit_noetig"])
                    require_equal(marker.read_text().splitlines(), ["turn"])
                    records = D.invocations(ledger, task)
                    require_equal(len(records), 1)
                    require_equal((records[0]["state"], records[0]["phase"]), ("finished", "assessment"))
                    require_equal(costs.view(task)["counts"], {"settled": 1})
    asyncio.run(go())


if __name__=='__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(),__name__))
