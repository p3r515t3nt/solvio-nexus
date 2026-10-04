"""Actual image bytes cross Planner, subscription transport and the cost gate.

Provider output is synthetic here; visual model quality is a separate live proof.
"""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
import test_agent_task_entry as T
import test_agent_result_files as RF
import test_agent_image_task as IT
from test_native_subscription_costs import native_contract
from test_subscription_planner import codex_answer
from solvio.agent_runtime import image_inputs as I, native_costs as N, cost_dispatch as D
from solvio.agent_runtime import result_files as F, store as S, planner as PL
from solvio.specialists import subscription as U, launcher as L


@asynccontextmanager
async def ready():
    async with T.world() as w:
        rid, step, descriptor = await RF.producer(w)
        w.ledger.update_step(step.step_id, state='succeeded', finished=True)
        run = w.ledger.get_run(rid)
        with native_contract(Path(S.state_dir())) as (adapter, reader, marker):
            with D.task_cost_scope(w.ledger, task_id=run.task_id, run_id=rid,
                    phase='assessment', operation_id='assessment:image-input', quote_adapter=adapter):
                yield w, run, descriptor, reader, marker


def paths(invocation):
    return tuple(invocation.argv[i + 1] for i, arg in enumerate(invocation.argv) if arg == '--image')


async def t_native_process_reads_verified_bytes_and_settles_same_cost_ledger():
    async with ready() as (w, run, d, reader, marker):
        # The executable is an owned provider fixture; it reads actual CLI input.
        executable = marker.parent / 'native-fixture'
        source = executable.read_text()
        source = source.replace("sys.stdin.read()", """sys.stdin.read()
    import hashlib
    images = [args[i+1] for i, arg in enumerate(args) if arg == '--image']
    assert len(images) == 1
    assert hashlib.sha256(Path(images[0]).read_bytes()).hexdigest() == __SHA__
    assert 'features.image_generation=false' in args
    assert 'features.view_image=false' in args
""".replace('__SHA__', repr(d['sha256'])))
        executable.write_text(source)
        call = await U.SubscriptionTransport(timeout=5)({'input': [], 'core_image_artifacts': (d['id'],)})
        require(call['ok'], call.get('reason'))
        require_equal(marker.read_text().splitlines(), ['turn'])
        require_equal(reader.read.await_count, 1)
        require_equal(w.orch.costs.view(run.task_id)['counts'], {'settled': 1})
        require_equal(I.bound_paths(), ())


async def t_public_image_completion_passes_real_image_to_assessor_and_preserves_negative_answer():
    for positive in (True, False):
        async with IT.world() as w:
            captured = []
            async def runner(invocation, prompt):
                received = paths(invocation)
                require_equal(len(received), 1)
                require_equal(Path(received[0]).read_bytes(), IT.N.PNG)
                require(N._known_invocation('codex', invocation))
                require('features.image_generation=false' in invocation.argv)
                require('features.shell_tool=false' in invocation.argv)
                require('web_search="disabled"' in invocation.argv)
                messages = json.loads(prompt.split('\n', 1)[1])
                request = json.loads(messages[-1]['content'])
                evidence = next(e for e in json.loads(request['ergebnis'])['befunde'] if e.startswith('Core-Dateibeleg:'))
                require('1 Bildschritt(e) und 0 andere Spezialistenschritt(e)' in evidence)
                require(any('untrusted Material' in m['content'] for m in messages))
                captured.append(received[0])
                reply = {'beantwortet': [{'id': 'h1', 'belege': [evidence]}] if positive else [],
                    'offen': [] if positive else ['h1'],
                    'fehlend': [], 'unsicher': [] if positive else ['Das Bild erfüllt das geforderte Motiv nicht.'],
                    'weiterarbeit_noetig': not positive}
                return L.Outcome(True, text=codex_answer(json.dumps(reply)), exit_code=0, process_started=True)
            base = w.provider
            transport = U.SubscriptionTransport(timeout=5, runner=runner)
            class Combined:
                provider = 'codex'
                route = transport.route
                timeout = 5
                async def __call__(self, payload):
                    request = json.loads(payload['input'][-1]['content'])
                    return await base(payload) if 'ziel' in request else await transport(payload)
            w.orch.planner = PL.Planner(subscription_transport=Combined())
            with native_contract(Path(S.state_dir())):
                run = await IT.advance(w)
            require_equal(run.state, S.SUCCEEDED if positive else S.FAILED, run.result_summary)
            if not positive:
                judgement = json.loads(run.completion_verdict)
                require_equal(judgement['offen'], ['h1'])
                require_equal(judgement['unsicher'], ['Das Bild erfüllt das geforderte Motiv nicht.'])
            require_equal(len(captured), 1)
            require(not Path(captured[0]).exists(), 'assessment workspace not cleaned')
            require_equal(len(IT.N.method_rows(w.native, 'turn/start')), 1)
            require_equal(len(F.describe_files(w.ledger, w.run_id)[0]), 1)


async def t_foreign_or_model_supplied_paths_never_reach_provider():
    async with ready() as (w, run, d, reader, marker):
        for ids in (('/etc/passwd',), ('aa-' + '0'*16,), (d['id'], d['id']), d['id']):
            result = await U.SubscriptionTransport(timeout=5)({'input': [], 'core_image_artifacts': ids})
            require(not result['ok'])
            require(not result['dispatch_started'])
        require_equal(reader.read.await_count, 0)
        require(not marker.exists())


async def t_scope_phase_and_out_of_context_invocations_cannot_borrow_binding():
    async with ready() as (w, run, d, reader, marker):
        with tempfile.TemporaryDirectory() as directory:
            with I.staged((d['id'],), directory) as images:
                inv = U.text_invocation('codex', workdir=directory, images=images)
                require(N._known_invocation('codex', inv))
                with D.task_cost_scope(w.ledger, task_id=run.task_id, run_id=run.run_id,
                        phase='plan', operation_id='foreign', quote_adapter=None):
                    require(not N._known_invocation('codex', inv))
                    result = await U.SubscriptionTransport(timeout=5)({'input': [], 'core_image_artifacts': (d['id'],)})
                    require(not result['dispatch_started'])
            require(not N._known_invocation('codex', inv))
        require(not marker.exists())


async def t_changed_staged_bytes_or_symlink_fail_final_native_cost_binding():
    async with ready() as (w, run, d, reader, marker):
        for kind in ('bytes', 'symlink'):
            with tempfile.TemporaryDirectory() as directory:
                try:
                    with I.staged((d['id'],), directory) as images:
                        inv = U.text_invocation('codex', workdir=directory, images=images)
                        target = Path(images[0])
                        if kind == 'bytes':
                            target.chmod(0o600); target.write_bytes(b'changed'); target.chmod(0o400)
                        else:
                            target.unlink(); target.symlink_to('/etc/passwd')
                        require(not N._known_invocation('codex', inv))
                except (ValueError, OSError):
                    pass
                else:
                    raise AssertionError('changed input accepted after assessment')
        require_equal(reader.read.await_count, 0)


async def t_file_changed_during_physical_call_keeps_cost_and_effect_metadata():
    async with ready() as (w, run, d, reader, marker):
        async def runner(invocation, prompt):
            target = Path(paths(invocation)[0])
            target.chmod(0o600); target.write_bytes(b'changed'); target.chmod(0o400)
            return L.Outcome(True, text=codex_answer('positive'), exit_code=0, process_started=True)
        result = await U.SubscriptionTransport(timeout=5, runner=runner)({'input': [], 'core_image_artifacts': (d['id'],)})
        require_equal((result['ok'], result['text'], result['reason']), (False, '', 'assessment_image_changed'))
        require(result['dispatch_started'])
        require_equal(result['cost_status'], 'settled')
        require_equal(w.orch.costs.view(run.task_id)['counts'], {'settled': 1})


async def t_deleted_canonical_image_cannot_be_assessed_from_an_old_descriptor():
    async with ready() as (w, run, d, reader, marker):
        artifact = next(a for a in w.ledger.artifacts_for_run(run.run_id) if a.kind == 'result_file')
        Path(artifact.path).unlink()
        result = await U.SubscriptionTransport(timeout=5)({'input': [], 'core_image_artifacts': (d['id'],)})
        require(not result['ok'])
        require(not result['dispatch_started'])
        require_equal(reader.read.await_count, 0)
        require(not marker.exists())


async def t_unsupported_provider_and_attachment_bounds_never_dispatch_or_switch():
    async with ready() as (w, run, d, reader, marker):
        result = await U.SubscriptionTransport('claude-code', timeout=5)({'input': [], 'core_image_artifacts': (d['id'],)})
        require_equal(result['reason'], 'assessment_images_unsupported')
        require(not result['dispatch_started'])
        for field, value in (('MAX_IMAGES', 0), ('MAX_BYTES', len(RF.PNG)-1)):
            with patch.object(I, field, value):
                result = await U.SubscriptionTransport(timeout=5)({'input': [], 'core_image_artifacts': (d['id'],)})
                require(not result['dispatch_started'])
        require_equal(reader.read.await_count, 0)
        require(not marker.exists())


async def t_generic_file_receipt_never_invents_native_generation_proof():
    async with ready() as (w, run, d, reader, marker):
        evidence = F.completion_evidence(w.ledger, run.run_id)[0].evidence
        require('Nativer Erzeugungsbeleg' not in evidence)


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
