"""Actual HTTPS delivery of result bytes, task binding, recovery and previews."""
import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
import test_agent_task_entry as T
from solvio.agent_runtime import result_files as F, store as S, notices

# A real small raster fixture is sufficient for delivery; it is not an image-generator proof.
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=')


async def producer(w, content=PNG, name='Hund.png', mime='image/png'):
    accepted = await (await w.start()).json()
    run_id = accepted['run_id']
    w.ledger.transition(run_id, S.PLANNING)
    w.ledger.transition(run_id, S.RUNNING)
    step = w.ledger.create_step(run_id=run_id, seq=1, kind='specialist', specialist_profile='researcher/hermes')
    w.ledger.update_step(step.step_id, state='running', started=True)
    descriptor = F.publish_file(w.ledger, run_id, step.step_id, content, name, mime)
    return run_id, step, descriptor


async def t_completed_bytes_survive_fresh_read_and_failed_task_without_new_work():
    async with T.world() as w:
        run_id, step, d = await producer(w)
        require_equal((await w.client.get(d['download_url'])).status, 404, 'loose output became ready')
        w.ledger.update_step(step.step_id, state='succeeded', finished=True)
        w.ledger.transition(run_id, S.FAILED, failure_category='goal_unverified')
        before = len(w.ledger.steps_for_run(run_id))
        view = await (await w.client.get('/v1/agent/runs/' + run_id)).json()
        require_equal(view['zustand_code'], 'FAILED')
        require_equal(view['dateien'], [d])
        require_equal(view['datei_hinweis'], '')
        # A fresh ledger object and a fresh authenticated browser can recover it.
        fresh = S.AgentRunLedger(w.ledger.path)
        require_equal(F.read_result(fresh, run_id, d['id']), (d, PNG))
        browser = await w.new_client()
        await w.login(browser)
        response = await browser.get(d['download_url'])
        require_equal(response.status, 200)
        require_equal(await response.read(), PNG)
        require_equal(response.headers['Content-Type'], 'image/png')
        require_equal(response.headers['Content-Length'], str(len(PNG)))
        require(response.headers['Content-Disposition'].startswith('attachment;'))
        require_equal(response.headers['Cache-Control'], 'no-store')
        preview = await browser.get(d['preview_url'])
        require_equal(await preview.read(), PNG)
        require(preview.headers['Content-Disposition'].startswith('inline;'))
        require_equal(len(w.ledger.steps_for_run(run_id)), before)


async def t_authentication_ownership_and_internal_artifacts_cannot_be_bypassed():
    async with T.world() as w:
        run_id, step, d = await producer(w)
        w.ledger.update_step(step.step_id, state='succeeded', finished=True)
        unauth = await w.new_client()
        for url in (d['download_url'], d['preview_url']):
            require_equal((await unauth.get(url)).status, 401)
        await w.login(unauth, 'different-owner')
        require_equal((await unauth.get(d['download_url'])).status, 404)
        proof = next(a for a in w.ledger.artifacts_for_run(run_id) if a.kind == 'result_receipt')
        require_equal((await w.client.get(d['download_url'].replace(d['id'], proof.artifact_id))).status, 404)
        require_equal((await w.client.get(d['preview_url'].replace(run_id, 'ar-' + '0' * 16))).status, 404)


async def t_changed_deleted_and_symlink_files_are_unavailable():
    async with T.world() as w:
        run_id, step, d = await producer(w)
        w.ledger.update_step(step.step_id, state='succeeded', finished=True)
        rows = w.ledger.artifacts_for_run(run_id)
        output = next(a for a in rows if a.kind == 'result_file')
        path = Path(output.path)
        path.chmod(0o600); path.write_bytes(PNG[:-1] + b'x'); path.chmod(0o400)
        require_equal((await w.client.get(d['download_url'])).status, 404)
        view = await (await w.client.get('/v1/agent/runs/' + run_id)).json()
        require_equal(view['dateien'], [])
        require(bool(view['datei_hinweis']))
        path.chmod(0o600); path.write_bytes(PNG); path.chmod(0o400)
        require_equal((await w.client.get(d['download_url'])).status, 200)
        path.unlink()
        path.symlink_to(next(a.path for a in rows if a.kind == 'result_receipt'))
        require_equal((await w.client.get(d['download_url'])).status, 404)
        path.unlink()
        require_equal((await w.client.get(d['download_url'])).status, 404)


async def t_repeated_publication_one_file_pair_and_receipt_is_read_only():
    async with T.world() as w:
        run_id, step, d = await producer(w)
        require_equal(F.publish_file(w.ledger, run_id, step.step_id, PNG, 'Hund.png', 'image/png'), d)
        require_equal(len(w.ledger.artifacts_for_run(run_id)), 2)
        w.ledger.update_step(step.step_id, state='succeeded', finished=True)
        original = w.ledger._open
        @contextmanager
        def readonly():
            with original() as connection:
                connection.execute('PRAGMA query_only=ON')
                yield connection
        with patch.object(w.ledger, '_open', readonly):
            require_equal(F.describe_files(w.ledger, run_id), ([d], ''))
            require_equal(F.read_result(w.ledger, run_id, d['id'])[1], PNG)
            require_equal(len(F.completion_evidence(w.ledger, run_id)), 1)


async def t_unknown_and_active_content_is_download_only_and_filename_cannot_inject_headers():
    async with T.world() as w:
        content = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
        run_id, step, d = await producer(w, content, '../../Hund\r\nSet-Cookie:x.svg', 'image/svg+xml')
        require_equal(d['mime_type'], 'application/octet-stream')
        require_equal(d['preview_kind'], 'none')
        require_equal(d['preview_url'], None)
        w.ledger.update_step(step.step_id, state='succeeded', finished=True)
        response = await w.client.get(d['download_url'])
        require_equal(await response.read(), content)
        require_equal(response.headers['Content-Type'], 'application/octet-stream')
        require('Set-Cookie' not in response.headers)
        require_equal((await w.client.get(d['download_url'].replace('/download', '/preview'))).status, 404)


async def t_preview_range_is_bounded_and_does_not_skip_full_integrity_check():
    async with T.world() as w:
        run_id, step, d = await producer(w)
        w.ledger.update_step(step.step_id, state='succeeded', finished=True)
        response = await w.client.get(d['preview_url'], headers={'Range': 'bytes=0-7'})
        require_equal(response.status, 206)
        require_equal(await response.read(), PNG[:8])
        require_equal(response.headers['Content-Range'], f'bytes 0-7/{len(PNG)}')
        for invalid in ('bytes=0-1,3-4', 'bytes=-0', 'bytes=9999-', 'bytes=4-1',
                        'bytes=' + '9' * 5000 + '-', 'bytes=0-' + '9' * 5000):
            require_equal((await w.client.get(d['preview_url'], headers={'Range': invalid})).status, 416)
        output = next(a for a in w.ledger.artifacts_for_run(run_id) if a.kind == 'result_file')
        path = Path(output.path)
        path.chmod(0o600); path.write_bytes(PNG[:-1] + b'x'); path.chmod(0o400)
        require_equal((await w.client.get(d['preview_url'], headers={'Range': 'bytes=0-7'})).status, 404)


async def t_missing_receipt_or_producer_finish_never_proves_completion():
    async with T.world() as w:
        run_id, step, d = await producer(w)
        for completed in (False, True):
            if completed:
                w.ledger.update_step(step.step_id, state='succeeded', finished=True)
                proof = next(a for a in w.ledger.artifacts_for_run(run_id) if a.kind == 'result_receipt')
                Path(proof.path).unlink()
            try:
                F.completion_evidence(w.ledger, run_id)
            except (ValueError, OSError):
                pass
            else:
                raise AssertionError('Incomplete receipt produced success evidence')


async def t_mismatched_mime_and_cancelled_publication_are_rejected():
    async with T.world() as w:
        run_id, step, d = await producer(w)
        try:
            F.publish_file(w.ledger, run_id, step.step_id, b'Only a prompt', 'Hund.png', 'image/png')
        except ValueError:
            pass
        else:
            raise AssertionError('A prompt became an image')
        w.ledger.transition(run_id, S.CANCELLED)
        try:
            F.publish_file(w.ledger, run_id, step.step_id, PNG, 'again.png', 'image/png')
        except ValueError:
            pass
        else:
            raise AssertionError('Cancelled task published new output')


async def t_cross_task_receipt_and_changed_requirements_cannot_rebind_existing_bytes():
    async with T.world() as w:
        run_id, step, d = await producer(w)
        w.ledger.update_step(step.step_id, state='succeeded', finished=True)
        proof = next(a for a in w.ledger.artifacts_for_run(run_id) if a.kind == 'result_receipt')
        original = Path(proof.path).read_bytes()
        # Even a correctly hashed receipt cannot claim a different task/run.
        for field, value in (('task_id', 'at-' + 'f' * 16), ('run_id', 'ar-' + 'f' * 16)):
            changed = json.loads(original); changed[field] = value
            raw = json.dumps(changed).encode()
            Path(proof.path).chmod(0o600); Path(proof.path).write_bytes(raw); Path(proof.path).chmod(0o400)
            with w.ledger._open() as connection:
                connection.execute('UPDATE agent_artifacts SET sha256=?,bytes=? WHERE artifact_id=?',
                    (hashlib.sha256(raw).hexdigest(), len(raw), proof.artifact_id))
            require_equal((await w.client.get(d['download_url'])).status, 404)
        Path(proof.path).chmod(0o600); Path(proof.path).write_bytes(original); Path(proof.path).chmod(0o400)
        with w.ledger._open() as connection:
            connection.execute('UPDATE agent_artifacts SET sha256=?,bytes=? WHERE artifact_id=?',
                (proof.sha256, proof.bytes, proof.artifact_id))
        require_equal((await w.client.get(d['download_url'])).status, 200)
        from solvio.agent_runtime import requirements as RQ
        task = w.ledger.get_task(w.ledger.get_run(run_id).task_id)
        bound = RQ.validate({'handlungen': [{'id': 'h1', 'text': 'Ein anderes Ergebnis'}]}, objective=task.objective)
        with w.ledger._open() as connection:
            connection.execute('UPDATE agent_tasks SET requirements=? WHERE task_id=?',
                (json.dumps(bound), task.task_id))
        require_equal((await w.client.get(d['download_url'])).status, 404)
        require_equal(F.describe_files(w.ledger, run_id)[0], [])


async def t_failure_notice_leads_with_state_even_if_provider_returns_a_long_success_sounding_prompt():
    item = notices.build_item(notices.Notice(run_id='ar-' + '1' * 16, kind='failed',
        summary='Ein tolles Hundbild! ' * 200 + ' Es wurde jedoch kein Bild erstellt.'))
    require(item['summary'].startswith('Auftrag fehlgeschlagen. '))
    require(len(item['summary']) <= notices.MAX_SUMMARY)


async def t_inbox_returns_only_agent_run_references_and_link_reads_without_starting_work():
    from test_nexus_dashboard import world
    async with world() as w:
        accepted = await (await w.start()).json()
        run_id = accepted['run_id']
        item = notices.build_item(notices.Notice(run_id=run_id, kind='completed',
                                                summary='Die Ergebnisdatei steht bereit.'))
        await w.center.store.add_item(item)
        for index, (source, reference) in enumerate([
                ('note_write', run_id), ('agent_runtime', 'https://example.invalid/result'),
                ('agent_runtime', run_id + '/cancel')]):
            await w.center.store.add_item(dict(item, notification_id='invalid-' + str(index),
                fingerprint='invalid-' + str(index), source_capability=source, run_id=reference))
        inbox = await (await w.client.get('/v1/control/inbox')).json()
        own = next(row for row in inbox['meldungen'] if row['id'] == item['notification_id'])
        require_equal(own['lauf'], run_id)
        for row in inbox['meldungen']:
            detail = await (await w.client.get('/v1/control/inbox/' + row['id'])).json()
            if row['id'] == item['notification_id']:
                require_equal(detail['lauf'], run_id)
            else:
                require('lauf' not in row and 'lauf' not in detail,
                        'A foreign source or malformed reference became a task link')
        runs_before = len(w.ledger.recent_runs())
        state_before = w.ledger.get_run(run_id).state
        linked = await (await w.client.get('/v1/agent/runs/' + own['lauf'])).json()
        require_equal(linked['id'], run_id)
        require_equal(len(w.ledger.recent_runs()), runs_before)
        require_equal(w.ledger.get_run(run_id).state, state_before)
        require_equal(w.ledger.steps_for_run(run_id), [])
        guest = await w.new_client()
        for url in ('/v1/control/inbox', '/v1/control/inbox/' + own['id'],
                    '/v1/agent/runs/' + own['lauf']):
            require_equal((await guest.get(url)).status, 401)


async def t_library_pages_keep_equal_timestamps_new_arrivals_and_owner_boundary():
    async with T.world() as w:
        own, foreign = [], []
        for i in range(57):
            task = w.ledger.create_task(objective="Eigener Bibliothekseintrag", scope="research",
                created_origin="trusted_dashboard", created_principal="local-owner")
            own.append(w.ledger.create_run(task_id=task.task_id).run_id)
        for i in range(28):
            task = w.ledger.create_task(objective="Fremder Bibliothekseintrag", scope="research",
                created_origin="trusted_dashboard", created_principal="different-owner")
            foreign.append(w.ledger.create_run(task_id=task.task_id).run_id)
        with w.ledger._open() as conn:
            conn.execute("UPDATE agent_runs SET created_at=1000")
        first_response = await w.client.get('/v1/agent/runs')
        require_equal(first_response.headers['Cache-Control'], 'no-store')
        first = await first_response.json()
        require_equal(len(first['laeufe']), 25)
        received = [r['id'] for r in first['laeufe']]
        task = w.ledger.create_task(objective="Neuer Auftrag während des Blätterns", scope="research",
            created_origin="trusted_dashboard", created_principal="local-owner")
        fresh = w.ledger.create_run(task_id=task.task_id)
        page = first
        while page['next_before']:
            page = await (await w.client.get('/v1/agent/runs?before='+page['next_before'])).json()
            received.extend(r['id'] for r in page['laeufe'])
        require_equal(received, sorted(own, reverse=True))
        require(fresh.run_id not in received)
        for cursor in [foreign[0], 'ar-'+'0'*16, 'not-a-run', '']:
            response = await w.client.get('/v1/agent/runs', params={'before':cursor})
            require_equal(response.status, 400)
            require_equal(await response.json(), {'error':'invalid_run_cursor'})
        guest = await w.new_client()
        require_equal((await guest.get('/v1/agent/runs?before='+own[0])).status, 401)
        await w.login(guest, 'different-owner')
        require_equal((await guest.get('/v1/agent/runs?before='+own[0])).status, 400)
        require_equal(len(w.ledger.recent_runs(limit=100)), 86)


async def t_task_overview_retains_old_waiting_tasks_and_filters_owner_before_selection():
    async with T.world() as w:
        def make(title, owner="local-owner", state="FAILED"):
            task = w.ledger.create_task(objective=title, scope="research",
                created_origin="trusted_dashboard", created_principal=owner)
            run = w.ledger.create_run(task_id=task.task_id)
            with w.ledger._open() as db:
                db.execute("UPDATE agent_runs SET state=? WHERE run_id=?", (state, run.run_id))
            return run
        waiting = make("Alte offene Frage", state="WAITING_USER")
        superseded = make("Alter inzwischen erledigter Auftrag", state="WAITING_USER")
        newer = w.ledger.create_run(task_id=superseded.task_id, attempt=2)
        with w.ledger._open() as db:
            db.execute("UPDATE agent_runs SET state='SUCCEEDED' WHERE run_id=?", (newer.run_id,))
        for i in range(35):
            make("Neuere abgeschlossene Aufgabe")
        foreign = make("Nicht meine Frage", owner="different-owner", state="WAITING_USER")
        result = await w.client.get('/v1/agent/runs?view=tasks')
        require_equal(result.status, 200)
        ids = [r['id'] for r in (await result.json())['laeufe']]
        require_equal(ids[0], waiting.run_id)
        require_equal(len(ids), 26)
        require(foreign.run_id not in ids and superseded.run_id not in ids)
        guest = await w.new_client()
        require_equal((await guest.get('/v1/agent/runs?view=tasks')).status, 401)
        await w.login(guest, 'different-owner')
        require_equal([r['id'] for r in (await (await guest.get('/v1/agent/runs?view=tasks')).json())['laeufe']], [foreign.run_id])


async def t_library_search_covers_old_results_with_stable_pages_and_owner_boundary():
    async with T.world() as w:
        matches = []
        for i in range(83):
            owner = "different-owner" if i % 3 == 0 else "local-owner"
            task = w.ledger.create_task(objective="Ältere Übersicht", scope="research",
                created_origin="trusted_dashboard", created_principal=owner)
            run = w.ledger.create_run(task_id=task.task_id)
            with w.ledger._open() as db:
                db.execute("UPDATE agent_runs SET state='FAILED', result_summary=?, workspace_path=? WHERE run_id=?",
                    ("Passendes Ergebnis Straße" if i < 60 else "Kein Ergebnis dazu", "internal-hidden-marker", run.run_id))
            if owner == "local-owner" and i < 60:
                matches.insert(0, run.run_id)
        response = await w.client.get('/v1/agent/runs', params={'q':'STRASSE'})
        require_equal(response.status, 200)
        page = await response.json()
        received = [r['id'] for r in page['laeufe']]
        while page['next_before']:
            page = await (await w.client.get('/v1/agent/runs', params={'q':'STRASSE', 'before':page['next_before']})).json()
            received.extend(r['id'] for r in page['laeufe'])
        require_equal(received, matches)
        require_equal((await (await w.client.get('/v1/agent/runs', params={'q':'internal-hidden-marker'})).json())['laeufe'], [])
        require_equal((await w.client.get('/v1/agent/runs', params={'q':'x'*201})).status, 400)
        guest = await w.new_client()
        require_equal((await guest.get('/v1/agent/runs?q=Straße')).status, 401)


async def t_search_bounds_scan_and_does_not_verify_files_for_nonmatching_text():
    from solvio.agent_runtime import endpoint as E
    async with T.world() as w:
        for i in range(130):
            task = w.ledger.create_task(objective="Anderer Titel", scope="research",
                created_origin="trusted_dashboard", created_principal="local-owner")
            w.ledger.create_run(task_id=task.task_id)
        with patch.object(E, '_complete_view', side_effect=AssertionError('nonmatch must not verify file bytes')), \
             patch.object(F, 'may_match_filename', return_value=False) as names:
            first = await (await w.client.get('/v1/agent/runs?q=unbekannt')).json()
            require_equal(first['laeufe'], [])
            require(first['next_before'])
            require_equal(names.call_count, 100)
            second = await (await w.client.get('/v1/agent/runs', params={'q':'unbekannt','before':first['next_before']})).json()
            require_equal(second, {'laeufe':[], 'next_before':None})
            require_equal(names.call_count, 130)


async def t_filename_search_prefilter_never_replaces_actual_file_integrity():
    async with T.world() as w:
        run_id, step, descriptor = await producer(w, name='Ältere-Zeichnung.png')
        w.ledger.update_step(step.step_id, state='succeeded', finished=True)
        checked = []
        original = F._read_file
        def read(directory, filename, *args, **kwargs):
            checked.append(filename)
            return original(directory, filename, *args, **kwargs)
        with patch.object(F, '_read_file', side_effect=read):
            require_equal(F.may_match_filename(w.ledger, run_id, 'unbekannt'), False)
        require(checked and all(name.endswith('.json') for name in checked))
        result = await (await w.client.get('/v1/agent/runs', params={'q':'ÄLTERE-ZEICHNUNG'})).json()
        require_equal([r['id'] for r in result['laeufe']], [run_id])
        artifact = next(a for a in w.ledger.artifacts_for_run(run_id) if a.artifact_id == descriptor['id'])
        Path(artifact.path).chmod(0o600)
        Path(artifact.path).write_bytes(b'changed bytes')
        Path(artifact.path).chmod(0o400)
        result = await (await w.client.get('/v1/agent/runs', params={'q':'ÄLTERE-ZEICHNUNG'})).json()
        require_equal(result['laeufe'], [])


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
