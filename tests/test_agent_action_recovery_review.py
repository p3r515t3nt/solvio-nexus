"""Independent action-recovery crash-window regressions; temporary ports only."""
from __future__ import annotations
import asyncio
from copy import deepcopy
from unittest.mock import patch

from test_agent_action_execution import world, admit
from _guard import enforce_assertions, require, require_equal
from solvio.agent_runtime import action_contract as AC, store as S

enforce_assertions()


class AbruptProcessLoss(BaseException):
    pass


async def crash_after_native_outcome(w):
    await w.orch.tick()
    await w.orch.tick()
    with patch.object(w.orch, '_settle_capability', side_effect=AbruptProcessLoss):
        try:
            await w.orch.tick()
        except AbruptProcessLoss:
            return
    raise AssertionError('post-native outcome crash injection was not reached')


async def t_unknown_first_action_prevents_dispatch_of_second_after_restart():
    async with world() as w:
        other = deepcopy(w.body['action_request']['actions'][0])
        other['action_id'] = 'a2'
        other['payload']['summary'] = 'SECOND action must remain unstarted after unknown'
        w.body['action_request']['actions'].append(other)
        run_id = await admit(w)
        w.native.transport.drop_after_write = True
        await crash_after_native_outcome(w)
        require_equal(AC.read_receipts(w.ledger, run_id)[0]['status'], 'unknown')
        require_equal(len(w.native.transport.mutations), 1)
        w.native.transport.drop_after_write = False
        w.runtime()
        await w.orch.reconcile()
        await w.orch.tick()
        require_equal(len(w.native.transport.mutations), 1,
            'An unknown predecessor must stop the action task before its next effect')
        require_equal(w.ledger.get_run(run_id).failure_category, 'recovery_required')


async def t_durable_auth_nonstart_is_recovered_as_login_boundary_after_restart():
    async with world() as w:
        run_id = await admit(w)
        w.native.calendar._access_token = ''
        w.native.calendar._expires_at = 0
        w.native.transport.auth_error = 'invalid_grant'
        await crash_after_native_outcome(w)
        require_equal(AC.read_receipts(w.ledger, run_id)[0]['status'], 'not_dispatched')
        require_equal(w.native.transport.mutations, [])
        w.runtime()
        await w.orch.reconcile()
        for _ in range(3):
            if w.ledger.get_run(run_id).state == S.WAITING_USER:
                break
            await w.orch.tick()
        require_equal(w.ledger.get_run(run_id).state, S.WAITING_USER,
            'A durable no-effect auth receipt must retain the repair-and-resume path')
        require_equal(w.native.transport.mutations, [])


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
