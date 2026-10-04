"""Bound task failures cannot commission legacy research or development.

Real HTTPS admission, TaskGrant, planner validation, Router and GapResolver.
Only outward model/research/driver boundaries are inert counters; all state is
in the existing temporary task-entry fixture. No provider or production call.
"""
import asyncio
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from test_agent_task_entry import world, BODY
from solvio.agent_runtime import planner as PL, store as S, cost_dispatch as CD, development as DEV
from solvio.capabilities.portal import SPECS as PORTAL
from solvio.capabilities.deep import SPECS as DEEP
from solvio.resolver.resolver import GapResolver
from solvio.resolver.inventory import CapabilityInventory, RuntimeInventory, RuntimeFact
from solvio.resolver.wiring import build_researcher
from solvio.resolver.states import ResolverState
from solvio.autopilot.store import AutopilotLedger

GOAL = 'Erstelle eine Übersicht über verfügbare Browser auf dem Raspberry Pi.'


class ObservedResolver(GapResolver):
    async def for_failed_tool(self, **kwargs):
        self.last = await super().for_failed_tool(**kwargs)
        return self.last


async def admit(w):
    research_calls, model_calls = [], []

    async def untouched_portal(arguments):
        raise AssertionError('invalid arguments reached the portal service')

    async def research(arguments):
        research_calls.append({'scope': CD.current_scope(), 'arguments': dict(arguments)})
        return {'task_id': 'synthetic-research', 'status': 'succeeded',
                'ergebnis': {'zusammenfassung': 'Only a synthetic boundary observation.'}}

    async def model(payload, **kwargs):
        model_calls.append(CD.current_scope())
        return {'ok': True, 'text': json.dumps({'schritte': [{'art': 'verify'}]}), 'tokens': 1}

    w.router.register(PORTAL['portal_list'], untouched_portal)
    w.router.register(DEEP['deep_research'], research)
    w.orch.require_task_authority = True
    w.orch.planner = PL.Planner(transport=model)
    response = await w.start(dict(BODY, objective=GOAL))
    require_equal(response.status, 201)
    accepted = await response.json()
    run = w.ledger.get_run(accepted['run_id'])
    grant = w.orch.task_authority.for_run(run.run_id)
    require_equal([(cap.name, cap.version, cap.constraints) for cap in grant.capabilities],
        [('artifact_create', 1, {'contract': 'local_artifacts_v1', 'resource': 'run_results'}),
         ('portal_list', 1, {})])
    # This local-result grant is not a new general Router capability and may
    # not authorize paths, another contract, another run or legacy research.
    require('artifact_create' not in w.router.names())
    exact = {'contract': 'local_artifacts_v1', 'resource': 'run_results'}
    require(w.orch.task_authority.verify(grant.reference, 'artifact_create', exact,
        1, task_id=run.task_id, run_id=run.run_id).allowed)
    for arguments in (dict(exact, resource='other_store'), dict(exact, contract='other_contract')):
        require(not w.orch.task_authority.verify(grant.reference, 'artifact_create', arguments,
            1, task_id=run.task_id, run_id=run.run_id).allowed)
    require(not w.orch.task_authority.verify(grant.reference, 'artifact_create', exact,
        1, task_id=run.task_id, run_id='foreign-run').allowed)
    require(not w.orch.task_authority.verify(grant.reference, 'deep_research', {},
        1, task_id=run.task_id, run_id=run.run_id).allowed)
    known = w.orch._known_capabilities(run.run_id)
    require('artifact_create' not in known)
    try:
        PL.validate({'schritte': [{'art': 'capability', 'faehigkeit': 'artifact_create',
            'argumente': dict(exact, path='/not-authorized')}]},
            scope='research', allowed_profiles=set(), known_capabilities=known, goal=GOAL)
    except PL.PlanInvalid as exc:
        require('unknown_capability' in str(exc), str(exc))
    else:
        raise AssertionError('generic artifact path entered the capability plan')
    runtimes = RuntimeInventory()
    runtimes.add(RuntimeFact(key='pi-wohnzimmer', kind='satellite',
        attributes={'rolle': 'Sprach-Satellit'}, reachable=True,
        source='synthetic local runtime fact', aliases=('raspberry pi',)))
    team = SimpleNamespace(consult=AsyncMock(return_value=None))
    resolver = ObservedResolver(CapabilityInventory(w.router), runtimes=runtimes,
        researcher=build_researcher(SimpleNamespace(capabilities=w.router)), team=team)
    w.orch.gap_resolver = resolver
    w.orch.development = AutopilotLedger(os.path.join(S.state_dir(), 'development.sqlite3'))
    w.ledger.transition(run.run_id, S.PLANNING)
    w.ledger.transition(run.run_id, S.RUNNING)
    return SimpleNamespace(run=w.ledger.get_run(run.run_id), grant=grant,
        researcher=research_calls, models=model_calls, team=team, resolver=resolver)


async def t_real_task_argument_failure_cannot_escape_to_unbound_research_or_build():
    async with world() as w:
        probe = await admit(w)
        nested = 0
        for _ in range(18):
            nested = [nested]
        context = w.orch._contexts[probe.run.run_id]
        # This is accepted by the real planner validator. The real Router then
        # rejects the noncanonical argument before any local service executes.
        context.plan = PL.validate({'schritte': [{'art': 'capability',
            'faehigkeit': 'portal_list', 'argumente': {'extra': nested}}]},
            scope='research', allowed_profiles=set(),
            known_capabilities=w.orch._known_capabilities(probe.run.run_id), goal=GOAL,
            capability_contracts={'portal_list': PORTAL['portal_list'].input_schema})
        await w.orch._advance(probe.run)
        step = w.ledger.steps_for_run(probe.run.run_id)[0]
        require_equal(step.outcome_reason, 'invalid_input:task_arguments_invalid')
        require_equal(probe.researcher, [], 'task failure invoked the unbound deep_research route')
        require_equal(probe.team.consult.await_count, 0, 'task failure invoked unbound specialist advice')
        require_equal(w.ledger.get_run(probe.run.run_id).development_ref, '')
        require_equal(w.orch.development.milestones(), [])
        require_equal(await w.store.list_pending(), [])
        require_equal(CD.invocations(w.ledger, probe.run.task_id), [])
        require(probe.models, 'the ordinary bounded replan must remain available')
        require(all(type(scope) is CD.TaskCostScope and scope.run_id == probe.run.run_id
                    for scope in probe.models))
        require_equal(w.orch.task_authority.for_run(probe.run.run_id), probe.grant)


async def t_local_only_resolver_preserves_real_inventory_solutions():
    async with world() as w:
        probe = await admit(w)
        resolver = ObservedResolver(CapabilityInventory(w.router, probes={'portal_list': lambda: True}),
            researcher=AsyncMock(return_value={}), team=probe.team)
        answer = await resolver.for_failed_tool(error='capability_failed:unknown_capability',
            tool='missing_portal_reader', goal='Zeige die eingerichteten Portale und oeffne die Zugänge.',
            local_only=True)
        require_equal(answer.state, ResolverState.SOLUTION_FOUND)
        require('portal_list' in [path.capability for path in answer.paths])
        require_equal(resolver.researcher.await_count, 0)
        require_equal(probe.team.consult.await_count, 0)


async def t_persisted_legacy_development_never_starts_under_a_real_task_grant():
    for with_active_job in (False, True):
        async with world() as w:
            probe = await admit(w)
            milestone = DEV.milestone_id_for(run_id=probe.run.run_id,
                capability='portal_list', kind='unsupported_variant')
            contract = DEV.contract_for(milestone_id=milestone, run_id=probe.run.run_id,
                task_id=probe.run.task_id, objective=GOAL, capability='portal_list',
                kind='unsupported_variant', reason='invalid_input:task_arguments_invalid')
            DEV.commission(w.orch.development, contract)
            w.ledger.set_run_fields(probe.run.run_id, development_ref=milestone)
            w.ledger.transition(probe.run.run_id, S.WAITING_CAPABILITY)
            starter = AsyncMock(return_value=True)
            w.orch._start_driver = starter
            job = None
            if with_active_job:
                job = asyncio.create_task(asyncio.Event().wait())
                w.orch._drivers[milestone] = job
                await asyncio.sleep(0)
            try:
                await w.orch._advance(w.ledger.get_run(probe.run.run_id))
                require_equal(starter.await_count, 0, 'legacy driver still starts for a bound task')
                final = w.ledger.get_run(probe.run.run_id)
                require_equal(final.state, S.FAILED)
                require_equal(final.failure_category, 'policy_denied')
                if job is not None:
                    require(job.cancelled(), 'already owned legacy work must be stopped')
                require_equal(probe.researcher, [])
                require_equal(CD.invocations(w.ledger, probe.run.task_id), [])
            finally:
                if job is not None and not job.done():
                    job.cancel()
                    await asyncio.gather(job, return_exceptions=True)


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
