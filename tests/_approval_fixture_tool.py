"""Ein Werkzeugpaar, das es nur im Test gibt — damit die Freigabe-Suiten ihren
eigenen Gegenstand pruefen und nicht den eines geloeschten Agenten.

Bis zur Stilllegung des legacy Codex-Weges (ADR-0028) haben drei Suiten ihre
Broker-Semantik an `codex_task`/`codex_confirm` bewiesen. Ihr Pruefgegenstand
war nie Codex: er war die **Form**. Genau EIN Werkzeug, das das Sprachmodell
rufen darf und das hoechstens ANFORDERT — und genau EIN Werkzeug, das
ausfuehrt und dem Modell strukturell nicht erreichbar ist (`expose_to_llm =
False`, durchgesetzt vom Dispatcher, nicht vom Verstecken des Schemas).

Diese Form steht hier weiter — ohne Unterprozess, ohne Anbieter, ohne
Schluessel, ohne Dateisystem. Damit prueft die Suite, was sie zu pruefen
behauptet, und ueberlebt das Verschwinden jedes einzelnen Werkzeugs.

Bewusst NICHT hier: irgendeine Faehigkeit, echte Arbeit zu tun. Der Ausfuehrer
merkt sich, dass er gerufen wurde. Mehr braucht keine dieser Zusicherungen.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from solvio.security.approval import ApprovalLimitError
from solvio.tools.base import RiskLevel, ToolResult

#: Der Arbeitsbereich ist im Test ein Name, kein Ort — nichts hier fasst ihn an.
FIXTURE_WS = "/Users/solvio/solvio-core"

TASK_TOOL = "fixture_task"
CONFIRM_TOOL = "fixture_confirm"

DEFAULT_PRINCIPAL = "local-owner"


@dataclass
class FixtureTask:
    """Was der Ausfuehrer bekommt. `confirmed` ist die Zeile, auf die es ankommt."""

    task: str
    working_directory: str
    mode: str = "analyze"
    confirmed: bool = False


class FixtureExecutor:
    """Der Ausfuehrer hinter beiden Werkzeugen: er merkt sich, was ihn erreicht hat.

    `ran` ist die eigentliche Zusicherung mehrerer Tests: eine Anforderung, die
    nur angefordert hat, darf diese Liste NICHT verlaengern.
    """

    def __init__(self, workspace: str = FIXTURE_WS) -> None:
        self.busy = False
        self.workspace = workspace
        self.ran: list[FixtureTask] = []

    def workspace_ok(self, wd: str):
        return True, self.workspace

    async def run(self, task: FixtureTask):
        self.ran.append(task)
        return FixtureOutcome(confirmed=task.confirmed)


@dataclass
class FixtureOutcome:
    confirmed: bool = False
    success: bool = True
    error: str | None = None
    summary: str = "ok"
    changed_files: list = field(default_factory=lambda: ["README.md"])

    def as_dict(self) -> dict:
        return {"confirmed": self.confirmed}


class FixtureTaskTool:
    """Das anfordernde Werkzeug: modell-erreichbar, aber ohne Ausfuehrungsrecht.

    `risk_level` ist bewusst HARMLESS — die Sicherheit der schreibenden Fassung
    ist der Freigabe-Broker (das harte Gate), nicht eine Risikostufe am Werkzeug.
    Diese Zeile war schon am geloeschten Original eine ausdrueckliche Aussage
    und bleibt es hier.
    """

    name = TASK_TOOL
    risk_level = RiskLevel.HARMLESS
    expose_to_llm = True

    def __init__(self, executor, broker, default_workspace: str = FIXTURE_WS,
                 principal: str = DEFAULT_PRINCIPAL) -> None:
        self.executor = executor
        self.broker = broker
        self.default_ws = default_workspace
        # Herkunft aus dem Kontroll-/Sitzungskontext, NIE ein Modellparameter.
        self.principal = principal

    def schema(self) -> dict[str, Any]:
        return {"type": "function", "name": self.name,
                "description": ("Testwerkzeug. mode 'analyze' laeuft direkt; mode 'modify' "
                                "fordert eine Freigabe an, die nur der Nutzer an einem "
                                "vertrauenswuerdigen Geraet erteilen kann."),
                "parameters": {"type": "object", "properties": {
                    "task": {"type": "string"},
                    "mode": {"type": "string", "enum": ["analyze", "modify"]}},
                    "required": ["task"]}}

    async def run(self, arguments: dict) -> ToolResult:
        task = (arguments.get("task") or "").strip()
        mode = (arguments.get("mode") or "analyze").strip().lower()
        if not task:
            return ToolResult(False, human_message="Was genau soll getan werden?")
        if mode == "system":
            return ToolResult(False, error="system_disabled",
                              human_message="Systemaktionen sind gesperrt.")
        ok, rp = self.executor.workspace_ok(self.default_ws)
        if not ok:
            return ToolResult(False, error="workspace_not_allowed",
                              human_message="Dieses Verzeichnis ist nicht freigegeben.")
        if mode == "modify":
            # Nur ANFORDERN. Kein request_id, kein Token ans Modell — es kann
            # eine Aenderung nicht selbst freigeben.
            try:
                self.broker.request(principal=self.principal, tool=TASK_TOOL,
                                    task=task, workspace=rp, mode="modify")
            except ApprovalLimitError:
                return ToolResult(False, error="too_many_pending_approvals",
                                  human_message="Es sind schon zu viele Freigaben offen.")
            return ToolResult(False, data={"needs_approval": True, "mode": "modify",
                                           "working_directory": rp, "task": task},
                              human_message=("Das kann ich NICHT selbst freigeben. Der Nutzer "
                                             "muss es an seinem Geraet freigeben."))
        if self.executor.busy:
            return ToolResult(False, error="busy", human_message="Gerade beschaeftigt.")
        res = await self.executor.run(FixtureTask(task=task, working_directory=rp,
                                                  mode="analyze"))
        return ToolResult(True, data=res.as_dict(), human_message=res.summary)


class FixtureConfirmTool:
    """Das ausfuehrende Werkzeug: dem Modell strukturell nicht erreichbar.

    `expose_to_llm = False` ist keine Schema-Kosmetik — der Dispatcher lehnt den
    Namen auf dem LLM-Weg ab (`not_exposed_to_llm:<name>`) und laesst ihn nur
    ueber die ausdrueckliche Kontrollebene durch. Beide Haelften pruefen die
    Suiten.
    """

    name = CONFIRM_TOOL
    risk_level = RiskLevel.HARMLESS
    expose_to_llm = False

    def __init__(self, executor, broker) -> None:
        self.executor = executor
        self.broker = broker

    def schema(self) -> dict[str, Any]:
        return {"type": "function", "name": self.name,
                "description": ("Fuehrt eine zuvor angeforderte Aenderung aus, NUR nach Freigabe "
                                "durch einen vertrauenswuerdigen, modell-unabhaengigen Kanal."),
                "parameters": {"type": "object", "properties": {
                    "request_id": {"type": "string"}}, "required": ["request_id"]}}

    async def run(self, arguments: dict) -> ToolResult:
        # `request_id` allein genuegt NICHT: ohne Approved-State vom vertrauten
        # Approver ist der Broker fail-closed.
        rid = (arguments.get("request_id") or "").strip()
        req, status = self.broker.take_approved(request_id=rid, tool=TASK_TOOL)
        if req is None:
            return ToolResult(False, error=f"not_approved:{status}",
                              human_message="Diese Aenderung ist nicht freigegeben.")
        if self.executor.busy:
            return ToolResult(False, error="busy", human_message="Gerade beschaeftigt.")
        res = await self.executor.run(FixtureTask(
            task=req.task, working_directory=req.workspace,
            mode="modify", confirmed=True))
        files = ", ".join(res.changed_files) or "keine"
        return ToolResult(True, data=res.as_dict(),
                          human_message=f"Erledigt. Geaenderte Dateien: {files}.")
