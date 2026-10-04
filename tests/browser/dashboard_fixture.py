"""Local temporary dashboard fixture. No production store or provider calls."""
import asyncio
from contextlib import AsyncExitStack
import os
from pathlib import Path
import signal
import sys
import tempfile
from unittest.mock import patch
from types import SimpleNamespace
root=Path(__file__).resolve().parents[2]
output=Path(sys.argv[1]).resolve(); output.mkdir(parents=True,exist_ok=True)
sys.path.insert(0,str(root/'src'));sys.path.insert(0,str(root/'tests'))
from test_nexus_dashboard import world, record, BODY
from solvio.agent_runtime import store as S
from test_browser_memory_commands import candidate
from solvio.memory.adaptive.observations import AdaptiveObservations, observation_digest
from solvio.memory.adaptive.extractor import ExtractionResult

async def main():
    async with AsyncExitStack() as stack:
        w=await stack.enter_async_context(world())
        # Read the actual server-side artifact configuration, not a browser
        # assertion. Browser admission tests refuse missing/non-temporary roots.
        (output/'fixture-state-root.txt').write_text(S.state_dir())
        stop=asyncio.Event()
        for sig in (signal.SIGINT,signal.SIGTERM):
            asyncio.get_running_loop().add_signal_handler(sig,stop.set)
        observations = AdaptiveObservations(w.adaptive, w.ledger, owner_principal='local-owner')
        async def local_empty_extraction(*a, **kw): return ExtractionResult(reason='nothing_usable')
        w.adaptive.extractor = SimpleNamespace(propose=local_empty_extraction, last_status={'state':'completed'})
        for text in ('Testprofil: Ich bevorzuge ruhige Hotels mit guter Bahnanbindung.',
                     'Testprofil: Bei Empfehlungen helfen mir kurze Begründungen und Quellen.',
                     'Testprofil: Projekttermine plane ich bevorzugt am Vormittag.'):
            await w.memory.semantic.remember(record(text))
        await candidate(w)
        for index, (objective, finished) in enumerate([
            ('Testauftrag: Drei Reiseoptionen für Hamburg vergleichen.', True),
            ('Testauftrag: Eine übersichtliche Wochenplanung vorbereiten.', False)]):
            accepted=await (await w.start(dict(BODY,objective=objective,client_request_id='preview-task-'+str(index)))).json()
            rid=accepted['run_id']
            if index == 0:
                turn, source = observations._task_observation(accepted['task_id'], rid)
                binding = observations.activities.admit(source, content_digest=observation_digest(turn),
                    task_id=accepted['task_id'], run_id=rid)
                observations.activities.hold(binding.activity_id, 'extractor_interrupted')
            w.ledger.transition(rid,S.PLANNING)
            if finished:
                w.ledger.transition(rid,S.RUNNING)
                if '--files' in sys.argv:
                    import json
                    from solvio.agent_runtime.result_files import publish_file
                    step = w.ledger.create_step(run_id=rid, seq=1, kind='specialist', specialist_profile='researcher/hermes')
                    w.ledger.update_step(step.step_id, state='running', started=True)
                    descriptor = publish_file(w.ledger, rid, step.step_id,
                        'Lokale Dateiprobe.\n<img src=x onerror="window.fileInjection=true">\nVollständiger Text im Download.\n'.encode(),
                        'Dokumenttext.txt', 'text/plain')
                    w.ledger.update_step(step.step_id, state='succeeded', finished=True)
                    (output/'actual-result-file.json').write_text(json.dumps(dict(run_id=rid, file=descriptor)))
                w.ledger.transition(rid,S.SUCCEEDED,result_summary='Lokaler Oberflächentest abgeschlossen. Diese temporäre Beispielaufgabe hat keinen Anbieter aufgerufen. Im echten Betrieb stehen hier das geprüfte Ergebnis und die zugehörigen Quellen.',outcome='success')
                if '--files' in sys.argv:
                    from solvio.agent_runtime import notices
                    await w.center.store.add_item(notices.build_item(notices.Notice(
                        run_id=rid, kind='completed', summary='Die lokale Testdatei steht bereit.')))
            else:
                w.ledger.transition(rid,S.WAITING_USER,summary='Die Testumgebung startet keine Anbieteraufrufe.')
        w.orch.memory_observations = observations
        native_task=None
        if '--native' in sys.argv:
            from test_hermes_native import native_fixture, scoped
            from solvio.agent_runtime.progress import NativeProgress
            from solvio.specialists import hermes_native as N, providers as P
            # Actual installed Hermes transport; only Codex RPC is local.
            native_root, config, _, _, _, _ = stack.enter_context(native_fixture('live_pause',timeout=180))
            accepted=await (await w.start(dict(BODY,objective='Live-Nachweis: Native Hermes-Werkzeugmeldungen lesen.',client_request_id='native-workroom'))).json()
            rid=accepted['run_id']
            for state in (S.PLANNING,S.RUNNING,S.WAITING_SPECIALIST):w.ledger.transition(rid,state)
            step=w.ledger.create_step(run_id=rid,seq=1,kind='specialist',specialist_profile='researcher/hermes')
            w.ledger.update_step(step.step_id,state='running',started=True)
            from solvio.agent_runtime.specialists import SpecialistRequest
            async def native():
                with scoped(w.ledger,accepted['task_id'],rid):
                    return await N.run_research(SpecialistRequest('researcher/hermes','Lokaler Live-Nachweis','',run_id=rid),
                        config=config,on_event=NativeProgress(w.ledger,rid,step.step_id))
            native_task=asyncio.create_task(native())
            (output/'native-run.txt').write_text(rid)
        await w.center.store.add_item({'notification_id':'preview-item','summary':'Dein isolierter Arbeitsraum ist für die Oberflächenprüfung vorbereitet.','fingerprint':'preview-item-v1'})
        # Deliberately synthetic credential for a temporary loopback test account.
        with patch('solvio.security.mobile_approval.browser_sessions.secrets.token_urlsafe', return_value='n5-test-only-'.ljust(43,'0')):
            await w.sessions.issue_enrollment(principal='local-owner')
        if '--window' in sys.argv:
            for prefix in ('n6-sender-test-', 'n6-viewer-test-'):
                with patch('solvio.security.mobile_approval.browser_sessions.secrets.token_urlsafe',return_value=prefix.ljust(43,'0')):
                    await w.sessions.issue_enrollment(principal='local-owner')
        (output/'dashboard-url.txt').write_text(w.origin+'/dashboard/')
        print('PREVIEW '+w.origin+'/dashboard/',flush=True)
        try:
            await stop.wait()
        finally:
            if native_task is not None:
                (native_root/'release-live').write_text('fixture ending')
                try:await asyncio.wait_for(native_task,10)
                except (asyncio.CancelledError,Exception):pass

with tempfile.TemporaryDirectory(prefix='solvio-dashboard-state-') as state_folder:
    # world() supplies temporary DBs, but artifact paths also use this global
    # server configuration. Bind it before constructing any Core component.
    with patch.dict(os.environ, {'SOLVIO_STATE_DIR': state_folder}):
        asyncio.run(main())
