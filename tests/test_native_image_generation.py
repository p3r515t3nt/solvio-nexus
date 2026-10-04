"""Real Hermes transport/launcher with a local Codex RPC image fixture only."""
from __future__ import annotations

import asyncio
import base64
from contextlib import contextmanager
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from solvio.agent_runtime import image_generation as G, cost_dispatch as D, costs as C, native_costs as NC
from solvio.agent_runtime.planner import PlannedStep
from solvio.specialists import image_generation as I, image_generation_worker as W
from test_hermes_native import native_fixture, RPC_PROGRAM, observed, scoped, method_rows

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZQmcAAAAASUVORK5CYII=')


@contextmanager
def fixture(mode="ok", timeout=2):
    with native_fixture(mode, timeout=timeout) as (root, config, ledger, task, run, _):
        source = RPC_PROGRAM.replace("config=tomllib.loads((home/'config.toml').read_text())",
            "config=tomllib.loads((home/'config.toml').read_text());config['web_search']='disabled';config['features']['image_generation']=True")
        begin = source.index('        for i in range(')
        end = source.index("        note('thread/tokenUsage/updated'", begin)
        source = source[:begin] + '''
        if mode != 'no_image':
            image={'type':'imageGeneration','id':'native-image-one','status':'completed',
                   'result':__PNG__, 'savedPath':None}
            if mode=='invalid_image': image['result']='not-image-base64'
            if mode=='foreign_image':
                send({'method':'item/completed','params':{'threadId':'foreign','turnId':U,'item':image}})
            else:
                note('item/started',item={**image,'status':'in_progress','result':''})
                note('item/completed',item=image)
            if mode=='two_images': note('item/completed',item={**image,'id':'second-image'})
''' .replace('__PNG__', repr(base64.b64encode(PNG).decode())) + source[end:]
        source = source.replace("if mode=='live_cancel': item('webSearch',query='local cancellation proof')", "if mode=='live_cancel': note('item/started',item={'type':'imageGeneration','id':'pending-image','status':'in_progress'})")
        Path(config.codex_bin).write_text('#!' + sys.executable + '\n' + source)
        step = ledger.create_step(run_id=run, seq=1, kind='specialist', specialist_profile=G.PROFILE)
        ledger.update_step(step.step_id, state='running', started=True)
        from solvio.agent_runtime import requirements as RQ
        objective = ledger.get_task(task).objective
        bound = RQ.validate({'auskunft':[], 'handlungen':[{'id':'h1','text':'Ein Bild erzeugen'}],
                            'unklar':[], 'belege':{'mindestens':0}}, objective=objective)
        ledger.bind_requirements(task, json.dumps(bound))
        planned = PlannedStep('specialist', profile=G.PROFILE, instruction='Erzeuge ein einziges Hundbild.', requirement='h1')
        yield root, config, ledger, task, ledger.get_run(run), ledger.get_step(step.step_id), planned


def t_real_native_image_bytes_terminal_claim_and_closed_tool_policy():
    async def go():
        with fixture() as (root, config, ledger, task, run, step, planned):
            with scoped(ledger, task, run.run_id):
                result = await G.NativeImageGenerator(config).generate(run, step, planned)
            require(result.ok, result.reason)
            require_equal(result.content, PNG)
            require_equal(result.mime_type, 'image/png')
            require_equal(result.native_proof['item_id'], 'native-image-one')
            require_equal(result.native_proof['terminal'], 'completed')
            require(result.usage_reported)
            require_equal(len(method_rows(root, 'turn/start')), 1)
            require_equal(method_rows(root, 'thread/start')[0]['params']['config']['web_search'], 'disabled')
            require_equal(method_rows(root, 'turn/start')[0]['params']['sandboxPolicy'], {'type':'readOnly','networkAccess':False})
            argv = [r['argv'] for r in observed(root) if r.get('argv', [''])[0] == 'app-server'][0]
            require(['--enable','image_generation'] == argv[argv.index('--enable'):argv.index('--enable')+2])
            for feature in W.H.DISABLED_FEATURES:
                if feature != 'image_generation':
                    index = argv.index(feature)
                    require_equal(argv[index-1], '--disable')
            require_equal(C.CostLedger(ledger).view(task)['counts'], {'settled':1})
            require_equal(D.invocations(ledger, task)[0]['state'], 'finished')
            require_equal(ledger.get_step(step.step_id).state, 'running', 'adapter must not publish/finish')
    asyncio.run(go())


def t_missing_foreign_invalid_and_multiple_images_never_return_deliverable():
    async def go():
        for mode in ('no_image', 'foreign_image', 'invalid_image', 'two_images', 'unexpected_tool'):
            with fixture(mode) as (root, config, ledger, task, run, step, planned):
                with scoped(ledger, task, run.run_id):
                    result = await G.NativeImageGenerator(config).generate(run, step, planned)
                require(not result.ok, mode)
                require_equal(result.content, b'')
                require_equal(len(method_rows(root, 'turn/start')), 1)
    asyncio.run(go())


def t_missing_terminal_and_disconnect_retain_unknown_without_retry():
    async def go():
        for mode in ('no_terminal','wrong_turn','disconnect'):
            with fixture(mode, timeout=.4) as (root, config, ledger, task, run, step, planned):
                with scoped(ledger, task, run.run_id):
                    result = await G.NativeImageGenerator(config).generate(run, step, planned)
                require(not result.ok)
                # Interrupt may confirm terminal in no_terminal/wrong_turn;
                # an abrupt disconnect can never be settled from worker exit.
                if mode == 'disconnect':
                    require_equal(result.reason, 'cost_recovery_required')
                    require_equal(D.invocations(ledger, task)[0]['state'], 'unknown')
                require_equal(len(method_rows(root, 'turn/start')), 1)
    asyncio.run(go())


def t_quota_and_missing_subscription_stop_without_image_retry():
    async def go():
        for mode in ('quota_before','quota_turn','quota_unknown','login_out','account_api','model_unavailable'):
            with fixture(mode) as (root, config, ledger, task, run, step, planned):
                with scoped(ledger, task, run.run_id):
                    result = await G.NativeImageGenerator(config).generate(run, step, planned)
                require(not result.ok)
                require_equal(result.content, b'')
                require_equal(len(method_rows(root, 'turn/start')), 1 if mode == 'quota_turn' else 0)
                if mode == 'quota_turn': require_equal(result.reason, 'quota')
    asyncio.run(go())


def t_unknown_quote_and_wrong_scope_never_start_image_worker():
    async def go():
        with fixture() as (root, config, ledger, task, run, step, planned):
            result = await G.NativeImageGenerator(config).generate(run, step, planned)
            require_equal(result.reason, 'cost_unbounded')
            require_equal(observed(root), [])
            with scoped(ledger, task, run.run_id, quote=None):
                result = await G.NativeImageGenerator(config).generate(run, step, planned)
            require_equal(result.reason, 'cost_unbounded')
            require_equal(len(method_rows(root, 'turn/start')), 0)
            require_equal(D.invocations(ledger, task), [])
    asyncio.run(go())


def t_exact_image_factory_is_known_but_changed_worker_or_extra_arguments_are_not():
    # The execution fixture stages the research worker with a narrow test
    # wrapper. This non-dispatching test must compare the real Core factories.
    native_factory = I.H.worker_invocation
    with fixture() as (root, config, _ledger, _task, _run, _step, _planned):
        with patch.object(I.H, 'worker_invocation', native_factory):
            invocation = I.image_invocation(config, str(root))
            require_equal(invocation.argv[2], str(Path(I.__file__).with_name('image_generation_worker.py')))
            require(NC._known_invocation('codex', invocation))
            require(not NC._known_invocation('codex', replace(invocation, argv=invocation.argv+('--api-key','synthetic'))))
            argv=list(invocation.argv);argv[2]=str(root/'not-core.py')
            require(not NC._known_invocation('codex', replace(invocation, argv=tuple(argv))))
        require_equal(observed(root), [], 'factory comparison must not start a native worker')


def t_native_cost_reader_accepts_exact_new_worker_and_rejects_changed_home():
    from test_openai_usage import fixture as reader_fixture
    from solvio.specialists.hermes_native import NativeResearchConfig
    async def go():
        with reader_fixture() as (root, home, _, reader, build):
            config=NativeResearchConfig(build.hermes_python,build.hermes_source,build.native,str(home),'gpt-local-test',10,1)
            invocation=I.image_invocation(config,str(root))
            require_equal((await reader.read(invocation)).state,'observed')
            require_equal((await reader.read(replace(invocation,codex_home=str(root)))).state,'unknown')
    asyncio.run(go())


def t_native_saved_file_fallback_is_bounded_to_real_generated_images_and_no_symlink():
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        home=Path(directory);generated=home/'generated_images';generated.mkdir()
        path=generated/'bound.png';path.write_bytes(PNG)
        require_equal(W.image_bytes({'result':'','savedPath':str(path)},str(home)),PNG)
        outside=home/'outside.png';outside.write_bytes(PNG)
        link=generated/'link.png';link.symlink_to(outside)
        for target in (outside,link,generated/'..'/'outside.png'):
            try: W.image_bytes({'result':'','savedPath':str(target)},str(home))
            except (ValueError,OSError): pass
            else: raise AssertionError('unbound path accepted')


def t_cancellation_owns_worker_children_and_retains_unclear_claim():
    async def go():
        with fixture('live_cancel', timeout=10) as (root, config, ledger, task, run, step, planned):
            with scoped(ledger, task, run.run_id):
                operation=asyncio.create_task(G.NativeImageGenerator(config).generate(run, step, planned))
                for _ in range(100):
                    if (root/'child.pid').exists(): break
                    await asyncio.sleep(.025)
                require((root/'child.pid').exists())
                pid=int((root/'child.pid').read_text())
                operation.cancel()
                try: await operation
                except asyncio.CancelledError: pass
                else: raise AssertionError('cancel swallowed')
            for _ in range(50):
                try: os.kill(pid,0)
                except ProcessLookupError: break
                await asyncio.sleep(.02)
            else: raise AssertionError('owned child survives cancellation')
            require_equal(D.invocations(ledger,task)[0]['state'],'unknown')
    asyncio.run(go())


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
