"""Approval-Broker Security Tests (S1 Hard-Gate + S1.1 Pre-Merge-Haertung).

Beweist: kanonischer/domain-separierter Authorization-Digest (boundary-swap-fest),
Trust ist NICHT caller-asserted, request_id ist keine Autoritaet, Principal-Bindung,
bounded pending (fail-closed ohne Verdraengung), dedup, expiry, double-consume, und
dass kein Approval-Endpoint auf der LLM-Surface liegt. Kein Unterprozess, kein Netz.
Direkt: python test_approval.py."""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))
from _guard import enforce_assertions  # noqa: E402
enforce_assertions()

from solvio.security.approval import (
    ApprovalBroker,
    ApprovalLimitError,
    DIGEST_VERSION,
    action_digest,
)
from solvio.tools.dispatcher import ToolDispatcher

from _approval_fixture_tool import (  # noqa: E402
    CONFIRM_TOOL, FIXTURE_WS, TASK_TOOL, FixtureConfirmTool, FixtureExecutor,
    FixtureTaskTool,
)

WS = FIXTURE_WS


def run(c):
    return asyncio.run(c)


class TrustedApprover:
    def __init__(self, who="owner"):
        self.who = who

    def is_trusted(self, request, identity):
        return identity == self.who


def _disp(agent, broker):
    d = ToolDispatcher()
    d.register(FixtureTaskTool(agent, broker, WS, principal="A"))
    d.register(FixtureConfirmTool(agent, broker))
    return d


def _mk(b, principal="A", task="t", mode="modify"):
    return b.request(principal=principal, tool=TASK_TOOL, task=task, workspace=WS, mode=mode)


def test_digest_deterministic_and_versioned():
    a = action_digest(tool_id=TASK_TOOL, mode="modify", task="t", workspace=WS)
    assert a == action_digest(tool_id=TASK_TOOL, mode="modify", task="t", workspace=WS)
    assert len(a) == 64 and DIGEST_VERSION == 1


def test_digest_action_specific():
    base = action_digest(tool_id=TASK_TOOL, mode="modify", task="t", workspace=WS)
    assert base != action_digest(tool_id=TASK_TOOL, mode="analyze", task="t", workspace=WS)
    assert base != action_digest(tool_id=TASK_TOOL, mode="modify", task="t2", workspace=WS)
    assert base != action_digest(tool_id=TASK_TOOL, mode="modify", task="t", workspace=WS + "/x")
    assert base != action_digest(tool_id="other", mode="modify", task="t", workspace=WS)


def test_digest_ambiguity_resistant_special_chars():
    weird = "a|b:c\n\"q\"e\x1f"
    d1 = action_digest(tool_id=TASK_TOOL, mode="modify", task=weird, workspace=WS)
    d2 = action_digest(tool_id=TASK_TOOL, mode="modify", task="x", workspace=WS)
    assert d1 != d2 and len(d1) == 64


def test_digest_boundary_swap_reject():
    assert action_digest(tool_id=TASK_TOOL, mode="modify", task="a|b", workspace="c") != \
        action_digest(tool_id=TASK_TOOL, mode="modify", task="a", workspace="b|c")
    assert action_digest(tool_id=TASK_TOOL, mode="modify", task="a\x1fb", workspace="c") != \
        action_digest(tool_id=TASK_TOOL, mode="modify", task="a", workspace="\x1fb\x1fc")


def test_caller_cannot_self_assert_trusted():
    b = ApprovalBroker()
    r = _mk(b)
    for ident in ("gregor", "USER_DIRECT", "trusted", "owner", "true"):
        got, st = b.approve(request_id=r.request_id, identity=ident, presented_digest=r.digest)
        assert got is None and st == "no_trusted_approver"


def test_request_id_alone_is_not_authority():
    b = ApprovalBroker()
    r = _mk(b)
    got, st = b.take_approved(request_id=r.request_id, tool=TASK_TOOL)
    assert got is None and st == "not_approved"


def test_principal_isolation():
    b = ApprovalBroker(approver=TrustedApprover("owner"))
    ra = _mk(b, principal="A")
    rb = _mk(b, principal="B")
    assert ra.request_id != rb.request_id and b.pending_count() == 2
    b.approve(request_id=ra.request_id, identity="owner", presented_digest=ra.digest)
    okA, _ = b.take_approved(request_id=ra.request_id, tool=TASK_TOOL)
    okB, stB = b.take_approved(request_id=rb.request_id, tool=TASK_TOOL)
    assert okA is not None and okB is None and stB == "not_approved"


def test_dedup_same_principal_same_action():
    b = ApprovalBroker()
    r1 = _mk(b)
    r2 = _mk(b)
    assert r1.request_id == r2.request_id and b.pending_count() == 1


def test_pending_limit_fail_closed_no_eviction():
    b = ApprovalBroker(max_pending=3)
    ids = [_mk(b, task=f"t{i}").request_id for i in range(3)]
    raised = False
    try:
        _mk(b, task="overflow")
    except ApprovalLimitError:
        raised = True
    assert raised and b.pending_count() == 3
    have = [p["request_id"] for p in b.list_pending()]
    assert all(rid in have for rid in ids)


def test_expired_request_rejected():
    b = ApprovalBroker(approver=TrustedApprover("owner"))
    r = _mk(b)
    r.expiry = time.monotonic() - 1
    got, st = b.approve(request_id=r.request_id, identity="owner", presented_digest=r.digest)
    assert got is None and st in ("expired", "unknown")
    assert b.pending_count() == 0


def test_double_consume_rejected():
    b = ApprovalBroker(approver=TrustedApprover("owner"))
    r = _mk(b)
    b.approve(request_id=r.request_id, identity="owner", presented_digest=r.digest)
    first, _ = b.take_approved(request_id=r.request_id, tool=TASK_TOOL)
    second, _ = b.take_approved(request_id=r.request_id, tool=TASK_TOOL)
    assert first is not None and second is None


def test_trusted_approve_requires_matching_digest():
    b = ApprovalBroker(approver=TrustedApprover("owner"))
    r = _mk(b)
    assert b.approve(request_id=r.request_id, identity="owner", presented_digest="dead")[1] == "digest_mismatch"
    assert b.approve(request_id=r.request_id, identity="owner", presented_digest=r.digest)[1] == "ok"


def test_no_approval_endpoint_on_llm_surface():
    d = _disp(FixtureExecutor(), ApprovalBroker())
    names = [t["name"] for t in d.openai_tools()]
    assert TASK_TOOL in names
    assert CONFIRM_TOOL not in names
    assert not any(("approv" in n.lower()) or ("confirm" in n.lower()) for n in names)
    assert not hasattr(ToolDispatcher, "approve") and not hasattr(d, "approve")


def test_llm_cannot_self_execute_modify():
    agent = FixtureExecutor()
    d = _disp(agent, ApprovalBroker())
    r1 = run(d.dispatch(TASK_TOOL, {"task": "edit README", "mode": "modify"}))
    assert (r1.get("data") or {}).get("needs_approval") is True
    r2 = run(d.dispatch_trusted(CONFIRM_TOOL, {"request_id": "whatever"}))
    assert r2["success"] is False and "not_approved" in r2["error"]
    assert agent.ran == []


def test_requesting_tool_leaks_no_token():
    b = ApprovalBroker()
    agent = FixtureExecutor()
    res = run(FixtureTaskTool(agent, b, WS, principal="A").run(
        {"task": "edit x", "mode": "modify", "confirmed": True}))
    dd = res.data or {}
    assert res.success is False and dd.get("needs_approval") is True
    assert "request_id" not in repr(dd) and "confirmation_id" not in repr(dd)
    assert b.has_pending() is True and agent.ran == []


def test_requesting_tool_respects_limit():
    b = ApprovalBroker(max_pending=1)
    agent = FixtureExecutor()
    d = _disp(agent, b)
    run(d.dispatch(TASK_TOOL, {"task": "first", "mode": "modify"}))
    r = run(d.dispatch(TASK_TOOL, {"task": "second", "mode": "modify"}))
    assert r["success"] is False and "too_many_pending" in (r.get("error") or "")
    assert agent.ran == []


def test_trusted_approval_enables_single_execution():
    b = ApprovalBroker(approver=TrustedApprover("owner"))
    agent = FixtureExecutor()
    d = _disp(agent, b)
    run(d.dispatch(TASK_TOOL, {"task": "edit README", "mode": "modify"}))
    pend = b.list_pending()
    assert len(pend) == 1
    rid, dig = pend[0]["request_id"], pend[0]["digest"]
    assert b.approve(request_id=rid, identity="owner", presented_digest=dig)[1] == "ok"
    r = run(d.dispatch_trusted(CONFIRM_TOOL, {"request_id": rid}))
    assert r["success"] is True and len(agent.ran) == 1 and agent.ran[0].confirmed is True
    r2 = run(d.dispatch_trusted(CONFIRM_TOOL, {"request_id": rid}))
    assert r2["success"] is False and len(agent.ran) == 1


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
