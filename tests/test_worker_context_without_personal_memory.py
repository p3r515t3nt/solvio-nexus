"""A worker step never carries the personal-memory briefing (C4 §1.5, E8 DEFER; review B-2, 19.09.2026).

Measured before this change: `_run_specialist_step` built `call_context` with
`personal_context.for_call(self.personal_memory, …)` for EVERY specialist step,
including `worker/claude` and `worker/codex`; `native_tasks.execute` appended the
context to the requirement contract and the worker prompts rendered it under
"Gezielt ausgewählter Core-Kontext" — memory hits (PII) went to the provider and
into the CLI transcript under <jail>/config/projects/. The briefing belongs to
the planner and the in-house specialists; a worker gets the working findings
(history), never the briefing.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))
from _guard import enforce_assertions, require, require_equal  # noqa: E402
enforce_assertions()

import test_agent_runtime_orchestrator as H  # noqa: E402
from solvio.agent_runtime import native_tasks as NT  # noqa: E402
from solvio.agent_runtime import personal_context  # noqa: E402
from solvio.agent_runtime import planner as PL  # noqa: E402
from solvio.agent_runtime import specialists as SP  # noqa: E402
from solvio.contracts.memory import MemoryRecord, MemoryType, SourceType, TrustLevel  # noqa: E402
from solvio.specialists.result import SpecialistResult  # noqa: E402

PII = "TEST-PII: Owner wohnt Musterstrasse 12 Hamburg, Hausarzt Dr. Beispiel"


def _memory_service():
    now = datetime.now(timezone.utc)
    record = MemoryRecord(id="mem-1", memory_type=MemoryType.USER, content=PII, subject="owner",
                          source="chat", source_type=SourceType.USER_DIRECT, created_at=now,
                          updated_at=now, trust_level=TrustLevel.USER_DIRECT)

    class Memory:
        async def get_visible(self, memory_id):
            return record if memory_id == record.id else None

    class Semantic:
        memory = Memory()

        async def lexical_recall(self, term, limit=3):
            return [SimpleNamespace(id=record.id)]

    class Service:
        semantic = Semantic()
        model_load_error = None

        async def search(self, query, top_k=3, max_chars=1800):
            return [SimpleNamespace(memory_id=record.id, content=PII)]

    return Service()


def _worker_request(profile: str, *, with_memory: bool):
    """Run one real specialist step for `profile`; only native_tasks.execute is captured."""
    captured = {}

    async def fake_execute(orch, run, step, request, progress):
        captured["request"] = request
        return SP.SpecialistRun(result=SpecialistResult(role="worker", provider="claude-code",
            question=request.objective, ok=False, reason="probe_stop"), provider="claude-code",
            billing_mode="subscription", auth="subscription", dispatch_started=False)

    kw = {"planner": H.FakePlanner(steps=[PL.PlannedStep(kind="capability", capability="ha_light_set")])}
    if with_memory:
        kw["personal_memory"] = _memory_service()
    orch = H._orch(**kw)
    with patch.object(orch, "_build_available", return_value=True):
        _task, run = orch.create_task(objective="Vergleiche drei Hotels in Hamburg", scope="build",
                                     origin="local_owner", principal="local-owner")
    orch.ledger.set_run_fields(run.run_id, workspace_path=tempfile.mkdtemp(prefix="solvio-b2-"))
    for _ in range(2):
        H._run(orch.tick())
    context = orch._contexts[run.run_id]
    planned = PL.PlannedStep(kind="specialist", profile=profile, instruction="Erstelle einen Vergleich")
    with patch.object(NT, "execute", fake_execute), patch.object(NT, "check_plan", lambda *a, **k: None):
        try:
            H._run(orch._run_specialist_step(orch.ledger.get_run(run.run_id), context, planned, 1))
        except Exception:  # noqa: BLE001 - der Schritt endet an der Attrappe; der Request ist gefangen
            pass
    return captured.get("request")


def t_the_briefing_reaches_an_in_house_call_but_never_a_worker_request():
    # Der Rahmen selbst — so wird ein Briefing im Kontext erkannt, nicht ueber den PII-Text allein.
    frame = H._run(personal_context.for_call(_memory_service(), query="Hotels", max_chars=4000))
    require(PII in frame and "BRIEFING" in frame, "the fake memory service does not brief")
    for profile in (SP.CLAUDE_TASK_PROFILE, SP.TASK_PROFILE):
        request = _worker_request(profile, with_memory=True)
        require(request is not None, "the worker step did not reach native_tasks.execute: " + profile)
        require_equal(request.profile, profile)
        require(PII not in (request.context or ""), "memory text reached the worker request: " + profile)
        require("BRIEFING" not in (request.context or ""), "the briefing frame reached the worker: " + profile)


def t_without_a_memory_service_the_worker_context_is_unchanged():
    request = _worker_request(SP.CLAUDE_TASK_PROFILE, with_memory=False)
    require(request is not None)
    require("BRIEFING" not in (request.context or "") and PII not in (request.context or ""))


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
