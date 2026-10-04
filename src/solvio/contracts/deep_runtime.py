"""SOLVIO Deep-Runtime-Contract - Referenz-Typen (STEP 19B.3, DESIGN-ONLY).

Framework-neutrale Schnittstelle fuer laenger laufende Agenten-Aufgaben.
Siehe docs/architecture/DEEP_RUNTIME_CONTRACT.md.

Seit Hermes Deep Runtime V1 ist dieser Contract produktiv: `solvio.deep.runtime`
implementiert ihn, der Core importiert ihn. Die Regel aus §10 bleibt trotzdem
die Regel — der Core kennt nur diese Typen, nie einen konkreten Executor.
Hermes ist eine Implementierung und keine Abhaengigkeit.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from solvio.contracts.memory import MemoryRecord, MemoryType
from solvio.contracts.trust import TrustContext, TrustLevel
from solvio.tools.base import RiskLevel  # Wiederverwendung der bestehenden Risk-Achse


class DeepTaskType(str, Enum):
    RESEARCH = "research"
    AUTOMATION = "automation"
    CODE = "code"
    STANDING_INTENT_RUN = "standing_intent_run"
    CONSOLIDATION = "consolidation"


class DeepTaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_USER = "waiting_for_user"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class TaskOrigin(str, Enum):
    USER_VOICE = "user_voice"
    STANDING_INTENT = "standing_intent"
    SCHEDULED = "scheduled"
    AGENT_CHAINED = "agent_chained"


@dataclass
class MemoryScope:
    """Welche Teile des Gedaechtnisses ein Task sehen/schreiben darf."""
    readable: list[MemoryType] = field(default_factory=list)
    writable: list[MemoryType] = field(default_factory=list)
    subjects: list[str] = field(default_factory=list)


@dataclass
class TaskBudget:
    max_tokens: int | None = None
    max_cost_usd: float | None = None


@dataclass
class TaskCost:
    tokens: int = 0
    cost_usd: float = 0.0


@dataclass
class Source:
    ref: str
    trust_level: TrustLevel
    note: str = ""


@dataclass
class ActionRecord:
    """Eine tatsaechlich gewirkte Aktion (auditierbar)."""
    tool: str
    summary: str
    risk_level: RiskLevel
    confirmed: bool


@dataclass
class DeepTask:
    """Auftrag an eine Deep-Runtime (DEEP_RUNTIME_CONTRACT.md §4).

    Autoritaets-Kopplung: privilegierte allowed_tools/allowed_memory_scope sind
    nur zulaessig, wenn trust_context.may_authorize() gilt.
    """
    id: str
    task_type: DeepTaskType
    instruction: str
    origin: TaskOrigin
    trust_context: TrustContext
    created_at: datetime
    risk_level: RiskLevel = RiskLevel.HARMLESS
    confirmation_required: bool = False
    context: dict[str, Any] = field(default_factory=dict)
    allowed_tools: list[str] = field(default_factory=list)
    allowed_memory_scope: MemoryScope = field(default_factory=MemoryScope)
    timeout: float = 120.0
    budget: TaskBudget = field(default_factory=TaskBudget)
    metadata: dict[str, Any] = field(default_factory=dict)
    # Gewuenschte Form des Endergebnisses. Geprueft wird sie auf der SOLVIO-Seite:
    # ein Executor, der behauptet, sein Ergebnis passe, hat es damit nicht bewiesen.
    output_schema: dict[str, Any] | None = None


@dataclass
class DeepTaskResult:
    """Ergebnis eines Deep-Tasks (DEEP_RUNTIME_CONTRACT.md §6)."""
    success: bool
    summary: str = ""
    data: Any = None
    sources: list[Source] = field(default_factory=list)
    actions_taken: list[ActionRecord] = field(default_factory=list)
    # Vorschlaege fuers Gedaechtnis - KEIN Auto-Commit als user_direct.
    memory_candidates: list[MemoryRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    duration: float = 0.0
    cost: TaskCost = field(default_factory=TaskCost)


@dataclass
class DeepTaskHandle:
    id: str
    status: DeepTaskStatus
    task_type: DeepTaskType


@runtime_checkable
class DeepRuntime(Protocol):
    """Framework-neutrale Deep-Agenten-Schnittstelle. Alle Operationen async.

    Konformitaet erfordert die Zuverlaessigkeits-/Recovery-Garantien aus
    DEEP_RUNTIME_CONTRACT.md §9 (kein Task-Verlust, keine Doppelwirkung,
    konsistenter Status nach Neustart, harte Abbrechbarkeit).
    """

    async def run_task(self, task: DeepTask) -> DeepTaskHandle:
        """Task validieren (Scope/Trust/Risk), einreihen, Handle liefern."""
        ...

    async def get_status(self, id: str) -> DeepTaskStatus:
        """Aktueller Status; muss Neustart ueberleben."""
        ...

    async def get_result(self, id: str) -> DeepTaskResult | None:
        """Ergebnis, oder None solange nicht abgeschlossen."""
        ...

    async def cancel_task(self, id: str) -> bool:
        """Harter, idempotenter Abbruch."""
        ...

    async def resume_task(self, id: str) -> bool:
        """Fortsetzen nach WAITING_FOR_USER (Bestaetigung erhalten) oder Recovery."""
        ...

    async def list_tasks(self, *, status: DeepTaskStatus | None = None,
                         origin: TaskOrigin | None = None) -> list[DeepTaskHandle]:
        """Tasks auflisten, optional gefiltert."""
        ...

    async def health(self) -> dict[str, Any]:
        """Erreichbarkeit, aktive Tasks, Auslastung (analog tools_health)."""
        ...
