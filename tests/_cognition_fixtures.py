"""Attrappen fuer den kognitiven Router — flach genau dort, wo es stimmt.

**Der Vertrag ist echt.** Es steht ein echter `CapabilityRouter` mit den
echten Spezifikationen und den echten Handlern dahinter; nur die Laufzeiten
darunter sind Attrappen. Das ist Absicht und eine Lehre: im
Agent-Runtime-Milestone verdeckten flache Testattrappen den echten Umschlag
zwei Laeufe lang, und die Naht, an der es schiefging, war genau die, die
niemand echt geprueft hatte. Herkunft, Matrix, Freigabe und Umschlag laufen
hier also unveraendert.

Attrappe ist: der Anbieter (ein Transport, der eine vorbereitete Antwort
zurueckgibt), der Orchestrator (er legt Zeilen an, er startet nichts) und der
tiefe Executor.
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Any


# =====================================================================
# Der Anbieter
# =====================================================================

def assessment_body(*, weg: str = "kurzrecherche", ziel: str = "",
                    zuversicht: float = 0.9, schwierigkeit: str = "mittel",
                    **extra: Any) -> str:
    """Eine Einschaetzung, wie sie vom Modell kaeme."""
    payload: dict[str, Any] = {"weg": weg, "ziel": ziel,
                               "zuversicht": zuversicht,
                               "schwierigkeit": schwierigkeit}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


class FakeTransport:
    """Ein Transport, der aufzeichnet und Vorbereitetes zurueckgibt.

    `calls` haelt die echten Rumpfe: das Modell auf dem Draht, die
    Ausgabekappe, der Token. Wer nur das Ergebnis prueft, prueft die Haelfte.
    """

    def __init__(self, replies: list[dict] | None = None) -> None:
        self.replies = list(replies or [])
        self.calls: list[dict] = []

    async def __call__(self, payload: dict, *, token: str = "",
                       port: int = 0) -> dict:
        self.calls.append({"payload": payload, "token": token,
                           "model": payload.get("model"),
                           "max_output_tokens": payload.get("max_output_tokens")})
        if not self.replies:
            return {"ok": True, "text": assessment_body(), "tokens": 42}
        reply = self.replies.pop(0)
        return dict(reply)

    def models(self) -> list[str]:
        return [str(call["model"]) for call in self.calls]


def text_reply(text: str, *, tokens: int = 42) -> dict:
    return {"ok": True, "text": text, "tokens": tokens}


def denial(reason: str) -> dict:
    return {"ok": False, "reason": reason}


class FakeBroker:
    """Ein Broker, der Token praegt und Leases zaehlt — und Kappen kennt."""

    def __init__(self, *, capped: set[str] | None = None) -> None:
        self.registered: list[str] = []
        self.leases: list[tuple[str, str]] = []
        self.closed: list[str] = []
        self.capped = set(capped or ())
        self._next = 0

    def register_principal(self, name: str) -> str:
        self.registered.append(name)
        self._next += 1
        return f"broker-token-fake-{self._next:04d}"

    def open_lease(self, name: str, ref: str = "", *, deadline: float = 0.0) -> str:
        if name in self.capped:
            from solvio.provider_broker.session import CapExceeded
            raise CapExceeded("rate_capped")
        self.leases.append((name, ref))
        return f"lease-{len(self.leases)}"

    def close_lease(self, lease_id: str) -> None:
        self.closed.append(lease_id)


# =====================================================================
# Die Laufzeiten
# =====================================================================

class FakeOrchestrator:
    """Legt Zeilen an. Startet nichts, plant nichts, laeuft nicht."""

    def __init__(self, ledger: Any) -> None:
        self.ledger = ledger
        self.created: list[dict] = []
        self.control_plane = None

    def create_task(self, *, objective: str, scope: str, origin: str,
                    principal: str, target_repo: str = "",
                    conversation_ref: str = "", predecessor_ref: str = ""):
        from solvio.agent_runtime import store as S

        if origin not in ("trusted_interactive_app", "room_voice", "local_owner"):
            from solvio.agent_runtime.orchestrator import CreationRefused
            raise CreationRefused(f"origin:{origin}")
        self.created.append({"objective": objective, "scope": scope,
                             "origin": origin, "repository": target_repo,
                             "conversation_ref": conversation_ref,
                             "predecessor_ref": predecessor_ref})
        task = self.ledger.create_task(
            objective=objective, scope=scope, created_origin=origin,
            created_principal=principal, target_repo=target_repo,
            conversation_ref=conversation_ref,
            predecessor_ref=predecessor_ref, budget={})
        run = self.ledger.create_run(task_id=task.task_id)
        return task, run


class FakeDeepRuntime:
    """Ein tiefer Executor, der eine Kennung zurueckgibt und laeuft."""

    def __init__(self) -> None:
        self.tasks: list[Any] = []
        self.states: dict[str, str] = {}

    async def run_task(self, task: Any):
        from solvio.contracts.deep_runtime import DeepTaskHandle, DeepTaskStatus

        identity = f"dt-{len(self.tasks) + 1:04d}"
        self.tasks.append(task)
        self.states[identity] = "running"
        return DeepTaskHandle(id=identity, status=DeepTaskStatus.RUNNING,
                              task_type=task.task_type)

    async def get_result(self, task_id: str):
        return None

    async def get_status(self, task_id: str):
        from solvio.contracts.deep_runtime import DeepTaskStatus
        return DeepTaskStatus.RUNNING

    async def stream(self, task_id: str, *, after_seq: int = -1):
        if False:  # pragma: no cover - ein Strom, der nie etwas liefert
            yield {}
        return

    async def list_tasks(self, *, status: Any = None, origin: Any = None):
        from solvio.contracts.deep_runtime import (DeepTaskHandle, DeepTaskStatus,
                                                   DeepTaskType)
        out = []
        for identity, state in self.states.items():
            out.append(DeepTaskHandle(id=identity,
                                      status=DeepTaskStatus(state),
                                      task_type=DeepTaskType.RESEARCH))
        return out


class FakeConversations:
    """Ein Gespraechsspeicher mit genau den zwei Lesearten, die zaehlen."""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    def add(self, conversation_ref: str, role: str, text: str,
            turn_id: str = "") -> None:
        self.rows.append({"conversation_ref": conversation_ref, "role": role,
                          "text": text, "source_turn_id": turn_id,
                          "sequence": len(self.rows) + 1})

    def recent_context(self, conversation_ref: str, *, max_chars: int = 3000):
        return [{"role": row["role"], "text": row["text"],
                 "sequence": row["sequence"]}
                for row in self.rows
                if row["conversation_ref"] == conversation_ref][-10:]

    def messages(self, conversation_ref: str, *, limit: int = 0):
        rows = [dict(row) for row in self.rows
                if row["conversation_ref"] == conversation_ref]
        return rows[-int(limit):] if int(limit) > 0 else rows


# =====================================================================
# Der Zusammenbau
# =====================================================================

class Bag:
    """Der Dispatcher ist ein Attributbeutel — hier genau das, nichts mehr."""


def state_dir() -> str:
    """Ein frisches Verzeichnis. Ein Test schreibt nie in das echte Buch."""
    return tempfile.mkdtemp(prefix="solvio-cognition-")


def build(*, transport: Any = None, broker: Any = None, mode: str = "active",
          with_agent: bool = True, with_deep: bool = True,
          with_doctor: bool = True, with_bots: bool = False,
          clock: Any = None) -> Any:
    """Ein Dispatcher mit echtem Vertrag und flachen Laufzeiten."""
    from solvio.agent_runtime import store as AS
    from solvio.capabilities.agent import (AgentCapabilities,
                                           register as register_agent)
    from solvio.capabilities.deep import (DeepCapabilities,
                                          register as register_deep)
    from solvio.capabilities.doctor import (DoctorCapabilities,
                                            register as register_doctor)
    from solvio.capabilities.invocation import CapabilityInvocationGate
    from solvio.capabilities.research_quick import SPECS as RESEARCH_QUICK_SPECS
    from solvio.capabilities.router import CapabilityRouter
    from solvio.cognition.ledger import CognitionLedger
    from solvio.cognition.router import CognitiveRouter
    from solvio.security.approval import ApprovalBroker

    bag = Bag()
    bag.approvals = ApprovalBroker()
    bag.capability_gate = CapabilityInvocationGate()
    bag.capabilities = CapabilityRouter(approvals=bag.approvals,
                                        policy_mode="enforce")
    bag.provider_broker = broker if broker is not None else FakeBroker()
    bag.conversations = FakeConversations()
    bag.cognitive_router_mode = mode

    if with_agent:
        agent_book = AS.AgentRunLedger()
        bag.agent_runtime = FakeOrchestrator(agent_book)
        register_agent(bag.capabilities, AgentCapabilities(bag.agent_runtime))
    if with_deep:
        bag.deep_runtime = FakeDeepRuntime()
        register_deep(bag.capabilities, DeepCapabilities(bag.deep_runtime))
        bag.research_quick_calls = []

        async def research_quick(arguments):
            bag.research_quick_calls.append(dict(arguments))
            return {"answer": "Kurz recherchiert.", "sources": [],
                    "provider": "fake", "model": "gpt-5.4-mini",
                    "searched_at": "2026-09-02T00:00:00+00:00",
                    "content_trust": "untrusted_web"}

        bag.capabilities.register(RESEARCH_QUICK_SPECS["research_quick"],
                                  research_quick)
    if with_doctor:
        register_doctor(bag.capabilities, DoctorCapabilities(_FakeDoctor()))

    bag.cognition_ledger = CognitionLedger()
    bag.cognition = CognitiveRouter(
        bag, mode=mode, ledger=bag.cognition_ledger, clock=clock,
        assessor=_assessor(transport, bag.provider_broker))
    return bag


def _assessor(transport: Any, broker: Any):
    from solvio.cognition.assessor import Assessor
    return Assessor(broker=broker,
                    transport=transport if transport is not None
                    else FakeTransport())


class _FakeDoctor:
    """Ein Arzt, der einen Befund hat und nichts repariert."""

    async def diagnose_all(self, *, requested_by_user: bool = False):
        return []

    async def diagnose(self, component: str, *, requested_by_user: bool = False):
        return None

    async def heal(self, **_kwargs):
        return []


def turn(bag: Any, text: str, *, conversation_ref: str = "c-0123456789abcdef",
         channel: str = "voice_satellite", turn_id: str = "s-1-t1",
         principal: str = "pi-wohnzimmer", proof: bool = False) -> Any:
    """Ein echter Turn am echten Tor. Herkunft aus Transportwahrheit."""
    from solvio.capabilities.invocation import voice_trust
    from solvio.capabilities.policy import origin_for_session

    bag.conversations.add(conversation_ref, "user", text, turn_id)
    bag.capability_gate.begin_turn(
        session_id="s-1", turn_id=turn_id, principal=principal,
        trust=voice_trust(True), user_text=text,
        origin=origin_for_session(channel, interactive_proof=proof),
        conversation_id=conversation_ref)
    return bag.capability_gate.context()


def redirect_state(tmp: str) -> None:
    """Alle drei Buecher in ein Wegwerfverzeichnis. VOR dem ersten Import."""
    os.environ["SOLVIO_STATE_DIR"] = tmp
    os.environ["SOLVIO_COGNITION_DB"] = os.path.join(tmp, "cognition.sqlite3")
    os.environ["SOLVIO_AGENT_RUNS_DB"] = os.path.join(tmp, "agent_runs.sqlite3")
    os.environ["SOLVIO_BROKER_DB"] = os.path.join(tmp, "broker.sqlite3")
