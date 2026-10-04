"""Completed document download: real HTTPS, same owner, immutable result receipt."""
from __future__ import annotations
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
import test_agent_document_execution as T
from solvio.agent_runtime import document_contract as DC
from solvio.agent_runtime.document_results import read_result, restore_context


async def t_full_document_download_requires_owner_and_verified_immutable_output():
    async with T.world() as w:
        accepted = await (await w.start(T.body(T.DOCUMENT))).json()
        run_id = accepted['run_id']
        run = await T.advance(w.orch, run_id)
        require_equal(run.state, T.S.SUCCEEDED, run.result_summary)
        view = await (await w.client.get('/v1/agent/runs/'+run_id)).json()
        descriptor = next(a for a in view['artefakte'] if a['art']=='document_result')
        url = descriptor['download_url']
        response = await w.client.get(url)
        require_equal(response.status, 200)
        require_equal((await response.read()).strip(), b'Actual original input.')
        require_equal(response.headers['Cache-Control'], 'no-store')
        require_equal(response.headers['X-Content-Type-Options'], 'nosniff')
        require(response.headers['Content-Type'].startswith('text/plain'))
        require(response.headers['Content-Disposition'].startswith('attachment;'))
        rows = w.ledger.artifacts_for_run(run_id)
        output = next(a for a in rows if a.kind=='document_result')
        proof = next(a for a in rows if a.kind=='document_receipt')
        receipt = json.loads(Path(proof.path).read_bytes())
        resource = next(a for a in rows if a.kind=='task_input')
        require_equal(receipt['source_sha256'], resource.sha256)
        require_equal(receipt['output_sha256'], output.sha256)
        require_equal(receipt['contract_digest'], DC.CONTRACT_DIGEST)
        require_equal(receipt['grant_reference'], w.orch.task_authority.for_run(run_id).reference)
        require(any(a.artifact_id==receipt['implementation_ref'] and a.kind=='extension_candidate' for a in rows))
        # Revocation stops new work; it does not erase this Owner's history.
        w.orch.task_authority.revoke(receipt['grant_reference'], 'fixture:after-completion')
        require_equal((await w.client.get(url)).status, 200)
        original_open = w.ledger._open

        @contextmanager
        def read_only():
            with original_open() as connection:
                connection.execute('PRAGMA query_only=ON')
                yield connection

        with patch.object(w.ledger, '_open', read_only):
            require_equal(read_result(w.ledger, run_id, output.artifact_id).strip(), b'Actual original input.')
            context = SimpleNamespace(findings=[], sources=[])
            restore_context(w.ledger, run_id, context)
            require_equal(context.findings, ['Actual original input.'])
        no_session = await w.new_client()
        require_equal((await no_session.get(url)).status, 401)
        await w.login(no_session, 'another-owner')
        require_equal((await no_session.get(url)).status, 404)
        for hidden in (resource.artifact_id, proof.artifact_id, 'aa-unrelated'):
            require_equal((await w.client.get(url.replace(descriptor['id'], hidden))).status, 404)
        before = len(w.calls())
        path = Path(output.path)
        original = path.read_bytes()
        path.chmod(0o600)
        path.write_bytes(b'Substituted output.')
        path.chmod(0o400)
        require_equal((await w.client.get(url)).status, 404)
        path.chmod(0o600)
        path.write_bytes(original)
        path.chmod(0o400)
        require_equal((await w.client.get(url)).status, 200)
        path.unlink()
        path.symlink_to(Path(proof.path))
        require_equal((await w.client.get(url)).status, 404)
        require_equal(len(w.calls()), before, 'download triggered work')


class ProcessLoss(BaseException):
    pass


async def t_committed_document_before_checkpoint_restores_findings_without_reexecution():
    async with T.world() as w:
        accepted = await (await w.start(T.body(T.DOCUMENT))).json()
        run_id = accepted['run_id']
        original = w.ledger.update_step

        def crash_after_step(*args, **kwargs):
            result = original(*args, **kwargs)
            if kwargs.get('outcome_reason') == 'document_text_verified':
                raise ProcessLoss()
            return result

        with patch.object(w.ledger, 'update_step', crash_after_step):
            try:
                await T.advance(w.orch, run_id)
            except ProcessLoss:
                pass
            else:
                raise AssertionError('The durable-step crash point was missed')
        before = len(w.calls())
        result = next(a for a in w.ledger.artifacts_for_run(run_id) if a.kind=='document_result')
        old_checkpoint = json.loads(w.ledger.get_run(run_id).plan_checkpoint)
        require_equal(old_checkpoint['befunde'], [])
        fresh = T.fresh_runtime(w)
        w.orch = fresh
        await fresh.reconcile()
        context = fresh._rebuild_context(fresh.ledger.get_run(run_id))
        require_equal(context.findings, ['Actual original input.'])
        require_equal(len(context.sources), 1)
        require(context.sources[0].startswith('Dokumentquelle SHA-256 '))
        restore_context(fresh.ledger, run_id, context)
        require_equal(context.findings, ['Actual original input.'], 'recovery duplicated its finding')
        require_equal(len(context.sources), 1)
        completed = await T.advance(fresh, run_id)
        require_equal(completed.state, T.S.SUCCEEDED, completed.result_summary)
        require_equal([c['phase'] for c in w.calls()[before:]], ['assessment'])
        document_steps = [s for s in fresh.ledger.steps_for_run(run_id)
                          if s.kind=='capability' and s.capability==DC.CAPABILITY]
        require_equal(len(document_steps), 1, 'recovery dispatched a second converter')
        require_equal(document_steps[0].state, 'succeeded')
        url = f'/v1/agent/runs/{run_id}/artifacts/{result.artifact_id}/download'
        response = await w.client.get(url)
        require_equal(response.status, 200)
        require_equal((await response.read()).strip(), b'Actual original input.')
        count = len(w.calls())
        w.orch = T.fresh_runtime(w)
        await w.orch.tick()
        require_equal((await w.client.get(url)).status, 200)
        require_equal(len(w.calls()), count)


async def t_output_without_completed_receipt_never_restores_success_or_repeats_work():
    async with T.world() as w:
        accepted = await (await w.start(T.body(T.DOCUMENT))).json()
        run_id = accepted['run_id']
        original = w.ledger.add_artifact

        def crash_before_receipt(*args, **kwargs):
            result = original(*args, **kwargs)
            if kwargs.get('kind') == 'document_result':
                raise ProcessLoss()
            return result

        with patch.object(w.ledger, 'add_artifact', crash_before_receipt):
            try:
                await T.advance(w.orch, run_id)
            except ProcessLoss:
                pass
            else:
                raise AssertionError('The incomplete-receipt crash point was missed')
        rows = w.ledger.artifacts_for_run(run_id)
        result = next(a for a in rows if a.kind=='document_result')
        require_equal(Path(result.path).read_text().strip(), 'Actual original input.')
        require_equal([a for a in rows if a.kind=='document_receipt'], [])
        before = len(w.calls())
        fresh = T.fresh_runtime(w)
        w.orch = fresh
        await fresh.reconcile()
        context = fresh._rebuild_context(fresh.ledger.get_run(run_id))
        require_equal(context.findings, [])
        require_equal(context.sources, [])
        completed = await T.advance(fresh, run_id)
        require_equal((completed.state, completed.failure_category), (T.S.FAILED, 'recovery_required'))
        url = f'/v1/agent/runs/{run_id}/artifacts/{result.artifact_id}/download'
        require_equal((await w.client.get(url)).status, 404)
        require_equal(len(w.calls()), before)
        require_equal(len([s for s in fresh.ledger.steps_for_run(run_id)
                           if s.kind=='capability' and s.capability==DC.CAPABILITY]), 1)


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
