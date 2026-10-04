"""Real published Git objects, original document grant and offline activation."""
from __future__ import annotations
import asyncio
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
from _guard import enforce_assertions, require, require_equal

enforce_assertions()
from solvio.agent_runtime import store as S, document_contract as DC
from solvio.agent_runtime import extension_activation as E, extension_process as EP
from solvio.agent_runtime.task_start_service import TaskStartService
from solvio.agent_runtime.task_authority import TaskAuthority, VerifiedTaskReceipt
from solvio.agent_runtime.costs import CostLedger
from solvio.autopilot.publisher import CheckpointPublisher
from solvio.autopilot.store import AutopilotLedger
from solvio.autopilot.contract import Contract

ADAPTER = """import os
os.execv('/usr/bin/textutil', ['/usr/bin/textutil', '-format', 'rtf', '-convert',
    'txt', '-stdin', '-stdout', '-encoding', 'UTF-8'])
"""


def git(repo, *args):
    env = {**os.environ, 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_SYSTEM': os.devnull,
           'GIT_AUTHOR_NAME': 'Test', 'GIT_AUTHOR_EMAIL': 'test@example.invalid',
           'GIT_COMMITTER_NAME': 'Test', 'GIT_COMMITTER_EMAIL': 'test@example.invalid'}
    process = subprocess.run(['git', '-C', str(repo), *args], env=env,
                             text=True, capture_output=True, check=True)
    return process.stdout.strip()


class World:
    def __init__(self, directory):
        self.root = Path(directory).resolve()
        self.ledger = S.AgentRunLedger(str(self.root / 'runs.db'))
        self.grants = TaskAuthority(self.ledger)
        starts = TaskStartService(self.ledger, grants=self.grants, costs=CostLedger(self.ledger))
        self.task, self.run = starts.create(objective='Lies das beigefuegte RTF-Dokument.',
            scope='research', origin='trusted_dashboard', principal='local-owner',
            receipt=VerifiedTaskReceipt('dashboard_session', 'test:session', 'local-owner'),
            request_id='document-activation-001',
            document_request=DC.DocumentRequest(b'{\\rtf1\\ansi Original document.}'))
        require(starts.ready(self.run.run_id))
        self.development = AutopilotLedger(str(self.root / 'development.db'))
        self.milestone = 'document-adapter-test'
        self.development.create_milestone(Contract(self.milestone, '1.0.0',
            'Build a task-bound RTF adapter.', ()))
        self.ledger.set_run_fields(self.run.run_id, development_ref=self.milestone)
        self.canonical = self.root / 'canonical'
        self.clone = self.root / 'clone'
        self.canonical.mkdir()
        git(self.canonical, 'init', '-q', '-b', 'main')
        (self.canonical / 'README.md').write_text('Isolated adapter storage.\n')
        git(self.canonical, 'add', '.')
        git(self.canonical, 'commit', '-qm', 'seed')
        git(self.root, 'clone', '-q', '--no-hardlinks', str(self.canonical), str(self.clone))
        self.publisher = CheckpointPublisher(str(self.canonical))
        self.activation = E.ExtensionActivation(self.ledger, development=self.development,
                                                publisher=self.publisher)
        self.ordinal = 0

    def publish(self, source=ADAPTER):
        self.ordinal += 1
        (self.clone / 'adapter.py').write_text(source)
        git(self.clone, 'add', '.')
        git(self.clone, 'commit', '-qm', 'adapter', '--allow-empty')
        commit = git(self.clone, 'rev-parse', 'HEAD')
        checkpoint = 'cp-adapter-' + str(self.ordinal)
        publication = self.publisher.publish(clone=str(self.clone), commit=commit,
            milestone_id=self.milestone, checkpoint_id=checkpoint)
        self.development.record_publication(self.milestone, publication)
        return dict(milestone_id=self.milestone, commit=commit, checkpoint_id=checkpoint)

    async def candidate(self, source=ADAPTER):
        return await self.activation.prepare(self.run.run_id, **self.publish(source))


@contextmanager
def world():
    with tempfile.TemporaryDirectory(prefix='extension-activation-') as directory:
        with patch.dict(os.environ, {'SOLVIO_STATE_DIR': str(Path(directory).resolve())}):
            item = World(directory)
            try:
                yield item
            finally:
                item.development.close()


async def t_real_published_adapter_is_activated_for_the_original_bound_document():
    with world() as w:
        require(w.activation.selected(w.run.run_id) is None)
        before = git(w.canonical, 'rev-parse', 'HEAD')
        candidate = await w.candidate()
        require(w.activation.selected(w.run.run_id) is None, 'publication/testing is not activation')
        activated = await w.activation.activate(w.run.run_id, candidate)
        require(activated.ok, activated.reason)
        selected, invocation = w.activation.selected(w.run.run_id)
        require_equal(selected, candidate)
        source = DC.read_for_run(w.ledger, w.run.run_id)
        result = await EP.run_extension(invocation, source)
        require(result.ok, result.reason)
        require_equal(result.stdout.strip(), b'Original document.')
        require_equal(git(w.canonical, 'rev-parse', 'HEAD'), before)
        require_equal(git(w.canonical, 'status', '--porcelain'), '')
        reopened = E.ExtensionActivation(S.AgentRunLedger(w.ledger.path),
                                        development=w.development, publisher=w.publisher)
        require_equal(reopened.selected(w.run.run_id)[0], candidate)
        require_equal(w.ledger.get_run(w.run.run_id).state, S.CREATED,
                      'activation does not complete the user task')


async def t_failed_gate_does_not_replace_a_working_version():
    with world() as w:
        old = await w.candidate()
        require((await w.activation.activate(w.run.run_id, old)).ok)
        try:
            await w.candidate("print('plausible but wrong text')\n")
        except E.ActivationRefused as exc:
            require_equal(str(exc), 'extension_gate_failed')
        else:
            raise AssertionError('broken candidate accepted')
        require_equal(w.activation.selected(w.run.run_id)[0], old)
        fixed = await w.candidate(ADAPTER + '# corrected second version\n')
        result = await w.activation.activate(w.run.run_id, fixed)
        require(result.ok)
        require_equal(result.previous_artifact, old)
        require_equal(w.activation.selected(w.run.run_id)[0], fixed)


async def t_activation_failure_rolls_back_the_exact_prior_version():
    with world() as w:
        old = await w.candidate()
        require((await w.activation.activate(w.run.run_id, old)).ok)
        new = await w.candidate(ADAPTER + '# candidate two\n')
        original = EP.run_extension
        calls = 0
        async def local_failure(invocation, payload):
            nonlocal calls
            calls += 1
            if calls == 2:
                return EP.ExtensionOutcome(False, reason='nonzero_exit', process_started=True,
                                           execution_status='terminal', exit_code=1)
            return await original(invocation, payload)
        with patch.object(EP, 'run_extension', local_failure):
            result = await w.activation.activate(w.run.run_id, new)
        require(not result.ok)
        require(result.rolled_back)
        require_equal(w.activation.selected(w.run.run_id)[0], old)


async def t_cancelled_activation_and_restart_restore_prior_selection():
    with world() as w:
        old = await w.candidate()
        require((await w.activation.activate(w.run.run_id, old)).ok)
        new = await w.candidate(ADAPTER + '# another candidate\n')
        new_directory = w.activation.candidate(w.run.run_id, new).artifact_dir
        original = EP.run_extension
        entered = asyncio.Event()
        async def waiting(invocation, payload):
            if invocation.artifact_dir != new_directory:
                return await original(invocation, payload)
            entered.set()
            await asyncio.Event().wait()
        with patch.object(EP, 'run_extension', waiting):
            task = asyncio.create_task(w.activation.activate(w.run.run_id, new))
            await entered.wait()
            require(w.activation.selected(w.run.run_id) is None, 'pending activation was advertised')
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        require_equal(w.activation.selected(w.run.run_id)[0], old)
        # Persisted process-loss window: no in-memory state participates.
        with w.ledger._open() as connection:
            connection.execute('UPDATE agent_extension_selection SET pending_artifact=?, '
                               'previous_artifact=? WHERE run_id=?', (new, old, w.run.run_id))
        reopened = E.ExtensionActivation(S.AgentRunLedger(w.ledger.path),
                                        development=w.development, publisher=w.publisher)
        require(await reopened.recover(w.run.run_id))
        require_equal(reopened.selected(w.run.run_id)[0], old)


async def t_revocation_while_testing_prevents_activation():
    with world() as w:
        new = await w.candidate()
        original = EP.run_extension
        async def revoke(invocation, payload):
            result = await original(invocation, payload)
            w.grants.revoke(w.grants.for_run(w.run.run_id).reference, 'test:owner-stop')
            return result
        with patch.object(EP, 'run_extension', revoke):
            try:
                await w.activation.activate(w.run.run_id, new)
            except ValueError:
                pass
            else:
                raise AssertionError('revoked task activated code')
        require(w.activation.selected(w.run.run_id) is None)


async def t_source_mutation_or_unpublished_commit_never_becomes_available():
    with world() as w:
        publication = w.publish()
        bad = dict(publication, checkpoint_id='cp-not-published')
        try:
            await w.activation.prepare(w.run.run_id, **bad)
        except E.ActivationRefused as exc:
            require_equal(str(exc), 'published_commit_required')
        else:
            raise AssertionError('unpublished candidate accepted')
        candidate = await w.activation.prepare(w.run.run_id, **publication)
        invocation = w.activation.candidate(w.run.run_id, candidate)
        path = Path(invocation.artifact_dir) / 'adapter.py'
        path.chmod(0o600)
        path.write_text("print('replacement')\n")
        try:
            await w.activation.activate(w.run.run_id, candidate)
        except E.ActivationRefused as exc:
            require_equal(str(exc), 'candidate_source_changed')
        else:
            raise AssertionError('mutated candidate activated')
        require(w.activation.selected(w.run.run_id) is None)

async def t_stale_activation_cannot_finish_or_rollback_a_new_generation_of_same_candidate():
    """Actual published/gated artifacts, then a controlled ABA scheduling race.

    A resumed Core may reconcile an old pending selection while its previous
    activation still returns. The candidate ID can be identical; only the
    generation distinguishes which probe owns the pending transaction.
    """
    with world() as w:
        old = await w.candidate()
        require((await w.activation.activate(w.run.run_id, old)).ok)
        candidate = await w.candidate(ADAPTER + '# ABA candidate\n')
        candidate_directory = w.activation.candidate(w.run.run_id, candidate).artifact_dir
        original_gate = w.activation._gate
        entered = [asyncio.Event(), asyncio.Event()]
        release = [asyncio.Event(), asyncio.Event()]
        calls = 0

        async def gate(_invocation):
            nonlocal calls
            if _invocation.artifact_dir != candidate_directory:
                # The old version's recovery probe remains a real native run.
                return await original_gate(_invocation)
            index = calls
            calls += 1
            entered[index].set()
            await release[index].wait()
            return index == 0  # stale gate passes; current gate fails

        tasks = []
        with patch.object(w.activation, '_gate', gate):
            try:
                tasks.append(asyncio.create_task(w.activation.activate(w.run.run_id, candidate)))
                await asyncio.wait_for(entered[0].wait(), 2)
                require(await w.activation.recover(w.run.run_id))
                tasks.append(asyncio.create_task(w.activation.activate(w.run.run_id, candidate)))
                await asyncio.wait_for(entered[1].wait(), 2)
                release[0].set()
                stale = await tasks[0]
                require(not stale.ok, 'the stale activation committed a newer pending generation')
                require(w.activation.selected(w.run.run_id) is None,
                        'the candidate became available before its current probe ended')
                release[1].set()
                current = await tasks[1]
                require(not current.ok)
                require(current.rolled_back, 'the stale completion destroyed the current rollback')
                require_equal(w.activation.selected(w.run.run_id)[0], old,
                              'the failed candidate remained selected')
            finally:
                for event in release:
                    event.set()
                await asyncio.gather(*tasks, return_exceptions=True)


async def t_a_later_failed_gate_of_the_same_candidate_revokes_its_availability():
    """A stored positive gate cannot overrule a newer negative measurement.

    Same run, input, published commit, checkpoint, environment and source hash;
    only the newer Core measurement changes. Reusing the already active ID
    must not bypass this evidence through activate's idempotent-return path.
    """
    with world() as w:
        publication = w.publish()
        candidate = await w.activation.prepare(w.run.run_id, **publication)
        require((await w.activation.activate(w.run.run_id, candidate)).ok)
        calls = 0

        async def failed_probe(_invocation, _payload):
            nonlocal calls
            calls += 1
            return EP.ExtensionOutcome(False, reason='nonzero_exit', process_started=True,
                                       execution_status='terminal', exit_code=1)

        with patch.object(EP, 'run_extension', failed_probe):
            try:
                await w.activation.prepare(w.run.run_id, **publication)
            except E.ActivationRefused as exc:
                require_equal(str(exc), 'extension_gate_failed')
            else:
                raise AssertionError('the new failed gate was accepted')
            require_equal(calls, 1)
            require(w.activation.selected(w.run.run_id) is None,
                    'a newer failed gate left the old positive candidate advertised')
            try:
                await w.activation.activate(w.run.run_id, candidate)
            except E.ActivationRefused:
                pass
            else:
                raise AssertionError('the old active ID bypassed its newer failed gate')


async def t_unusable_prior_version_is_probed_and_never_advertised_as_rolled_back():
    """A pointer rollback is not a proven return to a working implementation."""
    with world() as w:
        old = await w.candidate()
        require((await w.activation.activate(w.run.run_id, old)).ok)
        old_directory = w.activation.candidate(w.run.run_id, old).artifact_dir
        new = await w.candidate(ADAPTER + '# failed replacement\n')
        new_directory = w.activation.candidate(w.run.run_id, new).artifact_dir
        calls = []

        async def unavailable(invocation, _payload):
            calls.append(invocation.artifact_dir)
            return EP.ExtensionOutcome(False, reason='sandbox_unavailable',
                                       execution_status='not_started')

        with patch.object(EP, 'run_extension', unavailable):
            result = await w.activation.activate(w.run.run_id, new)
            require(not result.ok)
            require(old_directory in calls and new_directory in calls,
                    'the prior implementation was restored without any functional probe')
            require(not result.rolled_back, 'a nonfunctional previous implementation was called restored')
            require(w.activation.selected(w.run.run_id) is None,
                    'a failed rollback was exposed as an available implementation')


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
