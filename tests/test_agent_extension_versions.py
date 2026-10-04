"""Actual published code reused by distinct current grants, never old rights.

All stores and Git repositories are temporary. Adapter gates and conversions
are real sandboxed textutil processes. One integration case uses the existing
local CLI fixture for the first build; no real provider or account is used.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()

from test_agent_extension_activation import world, ADAPTER
from solvio.agent_runtime import extension_activation as E, extension_process as EP
from solvio.agent_runtime import document_contract as DC, store as S
from solvio.agent_runtime.task_start_service import TaskStartService
from solvio.agent_runtime.task_authority import VerifiedTaskReceipt
from solvio.agent_runtime.costs import CostLedger


def another(w, *, suffix='second', owner='local-owner', text=b'{\\rtf1\\ansi Second private document.}',
            format='rtf'):
    starts = TaskStartService(w.ledger, grants=w.grants, costs=CostLedger(w.ledger))
    _, run = starts.create(objective='Lies auch diese eigene Datei.', scope='research',
        origin='trusted_dashboard', principal=owner,
        receipt=VerifiedTaskReceipt('dashboard_session', 'test:session:' + suffix, owner),
        request_id='reuse-' + suffix, document_request=DC.DocumentRequest(text, format))
    require(starts.ready(run.run_id))
    # This run has no development milestone at all. Its own grant is sufficient
    # for the exact semantic operation; a donor development_ref is not needed.
    require_equal(run.development_ref, '')
    return run


async def version(w, source=ADAPTER):
    candidate = await w.candidate(source)
    require((await w.activation.activate(w.run.run_id, candidate)).ok)
    return w.activation.versions.publish(w.run.run_id, candidate), candidate


def refused(call, *args, **kwargs):
    try:
        call(*args, **kwargs)
    except (ValueError, OSError):
        return
    raise AssertionError('invalid code/binding was accepted')


async def refused_async(call, *args, **kwargs):
    try:
        await call(*args, **kwargs)
    except (ValueError, OSError):
        return
    raise AssertionError('invalid code/binding was accepted')


async def t_finished_origin_reuses_exact_code_with_new_input_grant_and_real_gate():
    with world() as w:
        key, old = await version(w)
        require_equal(w.activation.versions.publish(w.run.run_id, old), key)
        first_grant = w.grants.for_run(w.run.run_id)
        w.ledger.transition(w.run.run_id, S.PLANNING)
        w.ledger.transition(w.run.run_id, S.RUNNING)
        w.ledger.transition(w.run.run_id, S.VERIFYING)
        w.ledger.transition(w.run.run_id, S.SUCCEEDED)
        refused(DC.for_run, w.ledger, w.run.run_id)
        second = another(w)
        reopened = E.ExtensionActivation(w.ledger, development=w.development, publisher=w.publisher)
        matches = reopened.versions.compatible(second.run_id)
        require_equal([v.version_id for v in matches], [key])
        require_equal(matches[0].owner, 'local-owner')
        calls = []
        actual = EP.run_extension
        async def native(invocation, payload):
            calls.append(payload)
            return await actual(invocation, payload)
        with patch.object(EP, 'run_extension', native):
            own = await reopened.prepare_reuse(second.run_id, key)
            require_equal(len(calls), len(E.gate_cases()), 'reuse must actually test its own snapshot')
            require(own != old)
            require(reopened.selected(second.run_id) is None)
            require((await reopened.activate(second.run_id, own)).ok)
            require_equal(len(calls), 2 * len(E.gate_cases()))
        invocation = reopened.selected(second.run_id)[1]
        source = DC.read_for_run(w.ledger, second.run_id)
        result = await actual(invocation, source)
        require(result.ok)
        require_equal(result.stdout.strip(), b'Second private document.')
        _, manifest = reopened.versions._artifact(second.run_id, own)
        require_equal(manifest['run_id'], second.run_id)
        require_equal(manifest['grant_reference'], w.grants.for_run(second.run_id).reference)
        require(manifest['grant_reference'] != first_grant.reference)
        require_equal(manifest['arguments'], DC.for_run(w.ledger, second.run_id).arguments)
        require_equal(w.ledger.get_run(w.run.run_id).state, S.SUCCEEDED)
        with w.ledger._open() as db:
            require_equal(db.execute('SELECT COUNT(*) FROM agent_provider_invocations').fetchone()[0], 0)


async def t_cross_owner_and_wrong_contract_never_prepare_code():
    with world() as w:
        key, _ = await version(w)
        other = another(w, owner='different-owner')
        different = another(w, suffix='txt', text=b'plain text', format='txt')
        for run in (other, different):
            require_equal(w.activation.versions.compatible(run.run_id), ())
            await refused_async(w.activation.prepare_reuse, run.run_id, key)
            require_equal(w.ledger.artifacts_for_run(run.run_id)[-1].kind, 'task_input')


async def t_foreign_active_artifact_and_tampered_new_input_are_not_reusable():
    with world() as w:
        key, old = await version(w)
        second = another(w)
        refused(w.activation.candidate, second.run_id, old)
        bound = DC.for_run(w.ledger, second.run_id)
        artifact = next(a for a in w.ledger.artifacts_for_run(second.run_id) if a.kind == 'task_input')
        Path(artifact.path).chmod(0o600)
        Path(artifact.path).write_bytes(b'{\\rtf1\\ansi Modified content.}')
        await refused_async(w.activation.prepare_reuse, second.run_id, key)
        require(w.activation.selected(second.run_id) is None)
        require(bool(bound.grant_reference))


async def t_revoked_target_before_and_during_reuse_cannot_gain_selection():
    with world() as w:
        key, _ = await version(w)
        second = another(w)
        actual = EP.run_extension
        async def revoke(invocation, payload):
            result = await actual(invocation, payload)
            w.grants.revoke(w.grants.for_run(second.run_id).reference, 'test:owner-stop')
            return result
        with patch.object(EP, 'run_extension', revoke):
            await refused_async(w.activation.prepare_reuse, second.run_id, key)
        refused(w.activation.versions.compatible, second.run_id)
        await refused_async(w.activation.prepare_reuse, second.run_id, key)
        require(w.activation.selected(second.run_id) is None)


async def t_original_manifest_and_source_tampering_withdraws_reuse():
    for filename in ('adapter.py', 'manifest'):
        with world() as w:
            key, old = await version(w)
            second = another(w)
            artifact, _ = w.activation.versions._artifact(w.run.run_id, old)
            path = Path(artifact.path) if filename == 'manifest' else Path(artifact.path).parent / filename
            path.chmod(0o600)
            path.write_bytes(b'changed source or provenance')
            require_equal(w.activation.versions.compatible(second.run_id), ())
            await refused_async(w.activation.prepare_reuse, second.run_id, key)


async def t_environment_drift_and_version_record_mutation_are_refused():
    with world() as w:
        key, _ = await version(w)
        second = another(w)
        with patch.object(E, 'environment_fingerprint', return_value='changed-environment'):
            require_equal(w.activation.versions.compatible(second.run_id), ())
            await refused_async(w.activation.prepare_reuse, second.run_id, key)
        with w.ledger._open() as db:
            db.execute('UPDATE agent_extension_versions SET owner=? WHERE version_id=?', ('different-owner', key))
        refused(w.activation.versions.get, key)


async def t_later_negative_origin_probe_withdraws_already_reused_selection():
    with world() as w:
        key, old = await version(w)
        second = another(w)
        own = await w.activation.prepare_reuse(second.run_id, key)
        require((await w.activation.activate(second.run_id, own)).ok)
        _, body = w.activation.versions._artifact(w.run.run_id, old)
        w.development.record_evidence(body['milestone_id'], kind=E.gate_kind(), commit=body['commit'],
            env_fingerprint=body['environment'], ok=False, summary='new actual failing gate',
            payload={'candidate_digest': body['candidate_digest'], 'run_id': w.run.run_id,
                     'contract_digest': body['contract_digest']})
        require(w.activation.selected(second.run_id) is None)
        await refused_async(w.activation.activate, second.run_id, own)


async def t_failed_reuse_probe_is_durable_and_prevents_retry_or_other_task_use():
    with world() as w:
        key, _ = await version(w)
        second = another(w)
        actual = EP.run_extension
        async def fail(invocation, payload):
            # Real sandboxed process; a synthetic wrong expected input makes
            # this native readback fail the Core-owned semantic gate.
            return await actual(invocation, b'{\\rtf1\\ansi Wrong gate output.}')
        with patch.object(EP, 'run_extension', fail):
            await refused_async(w.activation.prepare_reuse, second.run_id, key)
        reopened = E.ExtensionActivation(w.ledger, development=w.development, publisher=w.publisher)
        third = another(w, suffix='third')
        require_equal(reopened.versions.compatible(third.run_id), ())
        await refused_async(reopened.prepare_reuse, third.run_id, key)
        with w.ledger._open() as db:
            require_equal(db.execute('SELECT COUNT(*) FROM agent_extension_version_probes WHERE ok=0').fetchone()[0], 1)


async def t_failed_reuse_activation_rolls_back_to_actual_previous_version():
    with world() as w:
        key, _ = await version(w)
        second = another(w)
        first = await w.activation.prepare_reuse(second.run_id, key)
        require((await w.activation.activate(second.run_id, first)).ok)
        key2, _ = await version(w, ADAPTER + '# another measured version\n')
        next_candidate = await w.activation.prepare_reuse(second.run_id, key2)
        next_directory = w.activation.candidate(second.run_id, next_candidate).artifact_dir
        seen = []
        actual = EP.run_extension
        async def fail_next(invocation, payload):
            seen.append(invocation.artifact_dir)
            return await actual(invocation, b'{\\rtf1\\ansi Wrong result.}'
                                if invocation.artifact_dir == next_directory else payload)
        with patch.object(EP, 'run_extension', fail_next):
            result = await w.activation.activate(second.run_id, next_candidate)
        require(not result.ok)
        require(result.rolled_back)
        require_equal(w.activation.selected(second.run_id)[0], first)
        require(len(set(seen)) == 2, 'rollback must really run the prior code')
        require_equal([v.version_id for v in w.activation.versions.compatible(second.run_id)], [key])


async def t_concurrent_tasks_receive_distinct_manifests_and_revocation_hides_both():
    with world() as w:
        key, _ = await version(w)
        second, third = another(w), another(w, suffix='third', text=b'{\\rtf1\\ansi Third.}')
        candidates = await asyncio.gather(*(w.activation.prepare_reuse(run.run_id, key) for run in (second, third)))
        require(candidates[0] != candidates[1])
        outcomes = await asyncio.gather(*(w.activation.activate(run.run_id, candidate)
                                         for run, candidate in zip((second, third), candidates)))
        require(all(outcome.ok for outcome in outcomes))
        w.activation.versions.revoke(key, 'owner_withdrawal')
        require(all(w.activation.selected(run.run_id) is None for run in (second, third)))
        require(w.activation.selected(w.run.run_id) is None,
                'the original consumer must not bypass withdrawal of its exported code')
        with w.ledger._open() as db:
            require_equal(db.execute('SELECT COUNT(*) FROM agent_extension_versions').fetchone()[0], 1)


async def t_changed_reuse_gate_and_foreign_manifest_binding_are_refused():
    with world() as w:
        key, _ = await version(w)
        second, third = another(w), another(w, suffix='third')
        own = await w.activation.prepare_reuse(second.run_id, key)
        artifact, body = w.activation.versions._artifact(second.run_id, own)
        wrong = dict(body, grant_reference=w.grants.for_run(third.run_id).reference)
        refused(w.activation.versions.validate_candidate, second.run_id, artifact, wrong)
        with w.development._db:
            w.development._db.execute('UPDATE evidence SET summary=? WHERE evidence_id=?',
                                     ('replaced original measurement', body['gate_evidence']))
        refused(w.activation.candidate, second.run_id, own)
        require_equal(w.activation.versions.compatible(third.run_id), ())


async def t_failed_original_rollback_withdraws_exported_code_from_other_runs():
    with world() as w:
        key, old = await version(w)
        second = another(w)
        own = await w.activation.prepare_reuse(second.run_id, key)
        require((await w.activation.activate(second.run_id, own)).ok)
        changed = await w.candidate(ADAPTER + '# replacement\n')
        actual = EP.run_extension
        seen = []
        async def fail(invocation, payload):
            seen.append(invocation.artifact_dir)
            return await actual(invocation, b'{\\rtf1\\ansi Invalid expected output.}')
        with patch.object(EP, 'run_extension', fail):
            result = await w.activation.activate(w.run.run_id, changed)
        require(not result.ok and not result.rolled_back)
        require_equal(len(set(seen)), 2)
        require(w.activation.selected(second.run_id) is None)
        refused(w.activation.versions.get, key)


async def t_orchestrator_reuses_without_new_milestone_or_coder_and_reopens_idempotently():
    from solvio.agent_runtime import capability_need as CN, extension_development as ED
    from solvio.agent_runtime.orchestrator import Orchestrator
    from solvio.agent_runtime.workspace import WorkspaceManager
    from test_agent_capability_need import checkpoint
    from unittest.mock import AsyncMock
    with world() as w:
        key, _ = await version(w)
        second = another(w)
        w.ledger.transition(second.run_id, S.PLANNING)
        w.ledger.transition(second.run_id, S.RUNNING)
        CN.park(w.ledger, run_id=second.run_id, seq=1, attempt=1, checkpoint=checkpoint())
        extension = ED.ExtensionDevelopment(w.ledger, w.development,
            WorkspaceManager(allowed=(str(w.canonical),)), w.publisher)
        def runtime():
            return Orchestrator(ledger=w.ledger, development=w.development,
                extension_development=extension,
                extension_activation=E.ExtensionActivation(w.ledger, development=w.development, publisher=w.publisher),
                require_task_authority=True)
        with (patch.object(w.development, 'create_milestone', side_effect=AssertionError('new development')),
                patch.object(extension, 'drive', AsyncMock(side_effect=AssertionError('coder called')))):
            result = await runtime()._drive_document_extension(second.run_id)
            require_equal(result.state, 'ready')
            require_equal(w.activation.versions.get(key).origin_run_id, w.run.run_id)
            require_equal((await runtime()._drive_document_extension(second.run_id)).artifact_id, result.artifact_id)
        require_equal(w.ledger.get_run(second.run_id).state, S.WAITING_CAPABILITY,
                      'a ready implementation is not yet a finished document task')
        with w.ledger._open() as db:
            require_equal(db.execute('SELECT COUNT(*) FROM agent_provider_invocations').fetchone()[0], 0)


async def t_orchestrator_build_publishes_then_second_task_uses_that_exact_native_code():
    from solvio.agent_runtime import capability_need as CN
    from solvio.agent_runtime.orchestrator import Orchestrator
    from test_agent_extension_development import world as development_world
    from test_agent_capability_need import checkpoint
    with development_world() as w:
        # The actual original Core intent uses its deterministic milestone.
        w.milestone = CN.milestone_id(w.run.run_id)
        w.ledger.set_run_fields(w.run.run_id, development_ref=w.milestone)
        activation = E.ExtensionActivation(w.ledger, development=w.development, publisher=w.publisher)
        runtime = Orchestrator(ledger=w.ledger, development=w.development,
            extension_development=w.service, extension_activation=activation, require_task_authority=True)
        ready = await runtime._drive_document_extension(w.run.run_id)
        require_equal(ready.state, 'ready')
        require_equal([call['kind'] for call in w.calls()], ['build', 'review'])
        second = another(w)
        w.ledger.transition(second.run_id, S.PLANNING)
        w.ledger.transition(second.run_id, S.RUNNING)
        CN.park(w.ledger, run_id=second.run_id, seq=1, attempt=1, checkpoint=checkpoint())
        counts = w.development._db.execute('SELECT COUNT(*) FROM milestones').fetchone()[0]
        reused = await runtime._drive_document_extension(second.run_id)
        require_equal(reused.state, 'ready')
        require_equal(w.development._db.execute('SELECT COUNT(*) FROM milestones').fetchone()[0], counts)
        require_equal([call['kind'] for call in w.calls()], ['build', 'review'])
        original = activation.candidate(w.run.run_id, ready.artifact_id)
        selected = activation.candidate(second.run_id, reused.artifact_id)
        require_equal(dict(selected.files), dict(original.files))
        result = await EP.run_extension(selected, DC.read_for_run(w.ledger, second.run_id))
        require_equal(result.stdout.strip(), b'Second private document.')


async def t_orchestrator_catalog_race_falls_back_once_but_revoked_task_never_does():
    from solvio.agent_runtime import capability_need as CN, extension_development as ED
    from solvio.agent_runtime.orchestrator import Orchestrator
    from solvio.agent_runtime.workspace import WorkspaceManager
    from test_agent_capability_need import checkpoint
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    for revoke_task in (False, True):
        with world() as w:
            # This test reaches the real development preflight; unlike the
            # activation-only fixture it must use the Core-owned seed text.
            from test_agent_extension_activation import git
            (w.canonical / 'README.md').write_text(ED.SEED_README)
            git(w.canonical, 'add', 'README.md')
            git(w.canonical, 'commit', '-qm', 'Core-owned development seed')
            key, _ = await version(w)
            second = another(w)
            w.ledger.transition(second.run_id, S.PLANNING)
            w.ledger.transition(second.run_id, S.RUNNING)
            CN.park(w.ledger, run_id=second.run_id, seq=1, attempt=1, checkpoint=checkpoint())
            extension = ED.ExtensionDevelopment(w.ledger, w.development,
                WorkspaceManager(allowed=(str(w.canonical),)), w.publisher)
            runtime = Orchestrator(ledger=w.ledger, development=w.development,
                extension_development=extension, extension_activation=w.activation, require_task_authority=True)
            original, calls = EP.run_extension, []
            async def racing(invocation, payload):
                result = await original(invocation, payload)
                calls.append(payload)
                if len(calls) == 1:
                    if revoke_task:
                        w.grants.revoke(w.grants.for_run(second.run_id).reference, 'test:owner-stop')
                    else:
                        w.activation.versions.revoke(key, 'new_measurement_withdrawn')
                return result
            paused = SimpleNamespace(state='paused', reason='fixture_stop_before_provider')
            with (patch.object(EP, 'run_extension', racing),
                  patch.object(w.development, 'create_milestone', wraps=w.development.create_milestone) as create,
                  patch.object(extension, 'drive', AsyncMock(return_value=paused)) as drive):
                if revoke_task:
                    await refused_async(runtime._drive_document_extension, second.run_id)
                    require_equal((create.call_count, drive.await_count), (0, 0))
                else:
                    result = await runtime._drive_document_extension(second.run_id)
                    require_equal(result.state, 'paused', 'a failed version is not a finished task')
                    require_equal((create.call_count, drive.await_count), (1, 1))
                require_equal(len(calls), len(E.gate_cases()), 'only one selected version was attempted')
            require(w.activation.selected(second.run_id) is None)


# -- N8/C4 §3.3: the helper family shares the tables, never the adapter lineage


async def t_helper_family_shares_the_ledger_but_never_the_adapter_lineage_or_its_gates():
    from solvio.agent_runtime import extension_versions as EV
    with world() as w:
        key, _ = await version(w)
        helpers = EV.helper_versions(w.ledger)
        require(helpers.ledger is w.ledger)
        # The adapter version is invisible to the helper family and vice versa.
        require_equal(helpers.helpers_for('local-owner'), ())
        require_equal(w.activation.versions.helpers_for('local-owner'), ())
        digests = {'csv_sum.py': 'a' * 64}
        helper_id = EV.helper_version_id('local-owner', digests)
        require(helper_id != key and helper_id.startswith('extension-v1-'))
        # Content addressing: same bytes and owner -> same id, whatever the order
        # or the run; a different owner, file name or byte digest -> another id.
        require_equal(helper_id, EV.helper_version_id('local-owner', {'csv_sum.py': 'a' * 64}))
        two = {'b.py': 'b' * 64, 'a.py': 'a' * 64}
        require_equal(EV.helper_version_id('local-owner', two), EV.helper_version_id('local-owner', dict(reversed(two.items()))))
        require(len({helper_id, EV.helper_version_id('other', digests), EV.helper_version_id('local-owner', {'x.py': 'a' * 64}),
                     EV.helper_version_id('local-owner', {'csv_sum.py': 'b' * 64})}) == 4)
        provenance = EV.helper_provenance('local-owner', digests)
        require_equal(set(provenance), {'v', 'family', 'owner', 'contract_digest', 'environment', 'files'})
        require_equal((provenance['v'], provenance['family']), (1, 'native_helper'))
        for bad in ((None, digests), ('', digests), ('local-owner', {}), ('local-owner', {'x.py': 'zz'}),
                    ('local-owner', {'x.py': 1}), ('local-owner', [('x.py', 'a' * 64)])):
            refused(EV.helper_provenance, *bad)
        # Rows a helper never wrote: revoke (unchanged) and validation refuse them.
        refused(helpers.revoke, helper_id, 'test_revoked')
        refused(helpers._validated_helper, helper_id)
        refused(helpers.publish_helper, w.run.run_id, 'aa-0000000000000000')
        # The adapter lineage is untouched by the family's presence.
        require_equal([v.version_id for v in w.activation.versions.compatible(w.run.run_id)], [key])
        with w.ledger._open() as db:
            require_equal(db.execute('SELECT COUNT(*) FROM agent_extension_versions').fetchone()[0], 1)


def t_helper_environment_is_the_static_checker_itself():
    import tempfile
    from unittest.mock import patch
    from solvio.agent_runtime import extension_versions as EV, helper_check as HC
    first = EV.helper_environment()
    require_equal(first, EV.helper_environment())
    require_equal(EV.helper_contract_digest(), EV._sha(b'solvio:native-helper:v1'))
    digests = {'h.py': 'c' * 64}
    before = EV.helper_version_id('local-owner', digests)
    with tempfile.TemporaryDirectory() as folder:
        edited = Path(folder) / 'helper_check.py'
        edited.write_bytes(Path(HC.__file__).read_bytes() + b'\n# a changed rule\n')
        with patch.object(HC, '__file__', str(edited)):
            require(EV.helper_environment() != first, 'an edited checker must change the environment')
            require(EV.helper_version_id('local-owner', digests) != before, 'and therefore every helper id')
    require_equal(EV.helper_version_id('local-owner', digests), before)


async def t_helpers_for_seeds_the_newest_versions_first_and_never_more_than_the_cap():
    """§3.3: newest first, at most MAX_HELPER_VERSIONS. Proven on the query and the cap
    with validated rows of the helper family (validation itself is proven by the
    transport suite; here it is held constant so only order and count can fail)."""
    from solvio.agent_runtime import extension_versions as EV
    with world() as w:
        helpers = EV.helper_versions(w.ledger)
        total = EV.MAX_HELPER_VERSIONS + 3
        with w.ledger._open() as connection:
            for index in range(total):
                connection.execute("INSERT INTO agent_extension_versions VALUES (?,?,?,?,?,?,?)",
                    ('extension-v1-' + ('%064x' % index), 'local-owner', EV.HELPER_FAMILY,
                     'c' * 64, 'e' * 64, '{}', 1000.0 + index))
            connection.execute("INSERT INTO agent_extension_versions VALUES (?,?,?,?,?,?,?)",
                ('extension-v1-' + ('%064x' % 999), 'other-owner', EV.HELPER_FAMILY, 'c' * 64, 'e' * 64, '{}', 5000.0))
        with patch.object(helpers, '_validated_helper', lambda version_id: version_id):
            seeded = helpers.helpers_for('local-owner')
        require_equal(len(seeded), EV.MAX_HELPER_VERSIONS)
        newest = ['extension-v1-' + ('%064x' % index) for index in range(total - 1, total - 1 - EV.MAX_HELPER_VERSIONS, -1)]
        require_equal(list(seeded), newest, 'helpers_for must seed the newest versions first')
        require('extension-v1-' + ('%064x' % 999) not in seeded, 'a foreign owner never leaks into the seeding')


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
