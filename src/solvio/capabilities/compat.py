"""Bruecke vom bestehenden Tool-Layer zum Vertrag — ohne Migration.

Die ausgelieferten Werkzeuge (Home Assistant, Codex, Memory) laufen unveraendert
weiter. Diese Bruecke erlaubt es, ein bestehendes `Tool` unter dem Vertrag
auszufuehren, ohne es anzufassen: dieselbe `run()`-Methode, aber davor die
Autoritaets- und Freigabepruefung, und danach der ehrlichere Ausgang.

Sie wird in V1 bewusst NICHT in den Sprachpfad eingehaengt. Der Realtime-Core ruft
weiter den `ToolDispatcher`; ein Umbau des laufenden Sprachwegs gehoert zur ersten
echten Capability (Home Assistant) und nicht in die Vertragsschicht. Neue
Faehigkeiten ab hier bekommen den kanonischen Weg direkt.
"""
from __future__ import annotations

from typing import Any

from solvio.capabilities.contract import CapabilitySpec, ExecutionClass
from solvio.capabilities.envelope import CapabilityOutcome, CapabilityResult
from solvio.security.mobile_approval.execution import NON_IDEMPOTENT_WRITE, READ_ONLY
from solvio.tools.base import RiskLevel, ToolResult


def spec_for_tool(tool: Any, *, version: int = 1,
                  semantics: str | None = None,
                  execution_class: ExecutionClass | None = None,
                  timeout: float = 30.0) -> CapabilitySpec:
    """Leitet eine Spec aus einem bestehenden Werkzeug ab — fail-safe.

    Ohne ausdrueckliche Angabe gilt: ein harmloses Werkzeug liest, alles andere ist
    ein nicht-idempotenter Schreibvorgang. Vergessene Klassifizierung kostet damit
    Sicherheit und nicht Stille — dieselbe Regel wie im eingefrorenen Pfad.
    """
    risk = getattr(tool, "risk_level", RiskLevel.MUTATING)
    if semantics is None:
        semantics = READ_ONLY if int(risk) <= int(RiskLevel.HARMLESS) else NON_IDEMPOTENT_WRITE
    if execution_class is None:
        execution_class = (ExecutionClass.FAST if semantics == READ_ONLY
                           else ExecutionClass.CONTROLLED)
    schema = {}
    try:
        schema = (tool.schema() or {}).get("parameters") or {}
    except Exception:  # noqa: BLE001 - ein kaputtes Schema darf nicht den Start verhindern
        schema = {}
    return CapabilitySpec(
        name=tool.name, version=version, execution_class=execution_class,
        base_risk=RiskLevel(int(risk)), semantics=semantics,
        input_schema=schema, executor="inline", timeout=timeout,
        description=getattr(tool, "description", ""))


def handler_for_tool(tool: Any):
    """Macht aus `Tool.run` einen Capability-Handler.

    `ToolResult.success=False` ist ein Fehlschlag der Faehigkeit — er wird als
    Ausnahme weitergereicht, damit der Router ihn benennt statt ihn als Erfolg mit
    traurigem Inhalt durchzureichen.
    """
    async def run(arguments: dict[str, Any]) -> Any:
        result: ToolResult = await tool.run(dict(arguments))
        if not result.success:
            raise ToolFailure(result.error or "tool_failed")
        return result.data if result.data is not None else result.human_message
    return run


class ToolFailure(Exception):
    """Ein altes Werkzeug meldete `success=False`."""


def as_capability_result(result: ToolResult, *, call_id: str,
                         capability: str) -> CapabilityResult:
    """Hebt ein altes `ToolResult` in den Umschlag — fuer gemischte Aufrufwege."""
    if result.success:
        return CapabilityResult(CapabilityOutcome.SUCCESS, call_id, capability,
                                data=result.data, human_message=result.human_message)
    return CapabilityResult(CapabilityOutcome.CAPABILITY_FAILED, call_id, capability,
                            data=result.data, human_message=result.human_message,
                            reason="capability_failed", detail=result.error or "")
