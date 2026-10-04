"""Public result retention after an incomplete native rework; no real providers."""
from dataclasses import replace
from pathlib import Path
import sys
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'), str(Path(__file__).resolve().parent)]
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
import test_native_task_entry as T


async def t_incomplete_rework_keeps_every_original_download_without_success():
    for variant in ('empty', 'partial', 'renamed'):
        async with T.world(rework='stale_file') as w:
            original = T.NT.run_task

            async def worker(request, **kwargs):
                outcome = await original(request, **kwargs)
                if len(w.calls) == 1 and variant == 'partial':
                    (Path(request.workdir) / 'details.txt').write_text('Additional original deliverable\n')
                    return replace(outcome, native_files=('answer.txt', 'details.txt'),
                        native_file_requirements=(('answer.txt', 'h1'), ('details.txt', 'h1')))
                if len(w.calls) == 2 and variant == 'empty':
                    return replace(outcome, native_files=(), native_file_requirements=())
                if len(w.calls) == 2 and variant == 'renamed':
                    (Path(request.workdir) / 'renamed.txt').write_bytes(
                        (Path(request.workdir) / 'answer.txt').read_bytes())
                    return replace(outcome, native_files=('renamed.txt',),
                        native_file_requirements=(('renamed.txt', 'h1'),))
                return outcome

            with patch.object(T.NT, 'run_task', side_effect=worker):
                accepted = await T.start(w)
                run = await T.drive(w, accepted['run_id'])
            require_equal((run.state, run.failure_category, len(w.calls)),
                          (T.S.FAILED, 'goal_unverified', 2), variant)
            require_equal([s.state for s in w.ledger.steps_for_run(run.run_id)
                           if s.kind == 'specialist'], ['succeeded', 'failed'], variant)
            require_equal(T.RF.superseded_artifact_ids(w.ledger, run.run_id), frozenset(), variant)
            view = await (await w.client.get('/v1/agent/runs/' + run.run_id, headers=w.headers)).json()
            expected = ['answer.txt', 'details.txt'] if variant == 'partial' else ['answer.txt']
            require_equal(sorted(f['name'] for f in view['dateien']), expected, variant)
            for file in view['dateien']:
                response = await w.client.get(file['download_url'], headers=w.headers)
                require_equal(response.status, 200)
                body = await response.read()
                require(body.startswith(b'Vorlaeufig') if file['name'] == 'answer.txt'
                        else body == b'Additional original deliverable\n', variant)
            require('Nacharbeit hat kein bestätigtes Ergebnis' in run.result_summary, run.result_summary)
            before = len(w.calls)
            await w.orch.tick()
            require_equal(len(w.calls), before, 'failed rework was repeated')


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
