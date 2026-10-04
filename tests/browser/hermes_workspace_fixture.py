"""A5 actual installed Hermes transport, synthetic local RPC, temp HTTPS.

Two distinct native invocations belong to one real admitted Core task. The
second loses its terminal completion. No account, model provider or real window
is used, and opening the dashboard cannot create a further invocation.
"""
import asyncio
from contextlib import AsyncExitStack
from dataclasses import replace
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src')); sys.path.insert(0, str(ROOT / 'tests'))
from test_nexus_dashboard import world, BODY
from test_hermes_native import native_fixture, FREE, method_rows
from solvio.agent_runtime import store as S, cost_dispatch as D
from solvio.agent_runtime.progress import NativeProgress
from solvio.agent_runtime.specialists import SpecialistRequest
from solvio.specialists import hermes_native as N
from solvio.dashboard import window_share as W


async def main(output):
    async with AsyncExitStack() as stack:
        w = await stack.enter_async_context(world())
        rpc, config, *_ = stack.enter_context(native_fixture('ok', timeout=3))
        run = await (await w.start(dict(BODY, objective='Lokaler Nachweis zweier gebundener Hermes-Aufrufe.',
                                       client_request_id='a5-native-observation'))).json()
        empty = await (await w.start(dict(BODY, objective='Lokaler Auftrag ohne Hermes-Aufruf.',
                                         client_request_id='a5-no-native'))).json()
        rid = run['run_id']
        for state in (S.PLANNING, S.RUNNING, S.WAITING_SPECIALIST): w.ledger.transition(rid, state)
        program = (rpc / 'codex-local').read_text()
        for index in (1, 2):
            (rpc / 'codex-local').write_text(program.replace('local-thread', f'local-thread-{index}')
                                                   .replace('local-turn', f'local-turn-{index}'))
            (rpc / 'mode').write_text('ok' if index == 1 else 'live_pause')
            step = w.ledger.create_step(run_id=rid, seq=index, kind='specialist', specialist_profile='researcher/hermes')
            w.ledger.update_step(step.step_id, state='running', started=True)
            with D.task_cost_scope(w.ledger, task_id=run['task_id'], run_id=rid, phase='specialist',
                    operation_id=f'a5-local-{index}', quote_adapter=lambda *_: FREE):
                result = await N.run_research(SpecialistRequest('researcher/hermes', 'Lokaler Protokollnachweis', '', run_id=rid),
                    config=config, on_event=NativeProgress(w.ledger, rid, step.step_id))
            if index == 1:
                assert result.result.ok
                w.ledger.update_step(step.step_id, state='succeeded', finished=True)
            else:
                assert result.cost_status == 'unknown'
                w.ledger.update_step(step.step_id, state='unknown', finished=True)
        w.ledger.transition(rid, S.WAITING_USER, summary='Der zweite native Ausgang ist ungeklärt. Kein automatischer Neuversuch.')
        assert len(method_rows(rpc, 'turn/start')) == 2
        with patch('solvio.security.mobile_approval.browser_sessions.secrets.token_urlsafe',
                   return_value='n5-test-only-'.ljust(43, '0')):
            await w.sessions.issue_enrollment(principal='local-owner')
        (output / 'dashboard-url.txt').write_text(w.origin + '/dashboard/')
        (output / 'fixture.json').write_text(json.dumps({'native_run': rid, 'other_run': empty['run_id']}))
        stop = asyncio.Event()
        for signum in (signal.SIGINT, signal.SIGTERM):
            asyncio.get_running_loop().add_signal_handler(signum, stop.set)
        print('PREVIEW ' + w.origin + '/dashboard/', flush=True)
        while not stop.is_set():
            assert len(method_rows(rpc, 'turn/start')) == 2
            data = {'runs': len(w.ledger.recent_runs()), 'local_rpc_turns': 2,
                    'real_provider_turns': 0, 'window_peers': len(w.app[W.HUB].peers),
                    'native_events': len([event for event in w.ledger.events_for_run(rid) if event.kind == 'native_progress'])}
            temporary = output / 'audit-next.json'; temporary.write_text(json.dumps(data))
            temporary.replace(output / 'audit.json')
            try: await asyncio.wait_for(stop.wait(), .1)
            except asyncio.TimeoutError: pass


if __name__ == '__main__':
    output = Path(sys.argv[1]).resolve(); output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='solvio-a5-state-') as folder:
        with patch.dict(os.environ, {'SOLVIO_STATE_DIR': folder}): asyncio.run(main(output))
