"""SOLVIO Tool-Layer - Basis-Typen (Schritt 17).

Realtime spricht NUR mit dem Dispatcher, nie ein Tool direkt mit dem Betriebs-
system. Jedes Tool hat eine Risikostufe und ein OpenAI-Function-Schema.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Protocol, runtime_checkable


class RiskLevel(IntEnum):
    """Risiko-/Bestaetigungsstufen."""
    HARMLESS = 1   # harmlose Abfragen, Licht an/aus, Helligkeit, lesen -> direkt
    MUTATING = 2   # Kommunikation, Dateien/Config aendern, Dienste neu starten -> Bestaetigung
    CRITICAL = 3   # loeschen, Kaeufe, Schloesser, Sicherheit, destructive, credentials -> immer Bestaetigung


@dataclass
class ToolRequest:
    tool_name: str
    action: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    risk_level: RiskLevel = RiskLevel.MUTATING
    confirmation_required: bool = True


@dataclass
class ToolResult:
    success: bool
    data: Any = None
    human_message: str = ""
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"success": self.success}
        if self.data is not None:
            d["data"] = self.data
        if self.human_message:
            d["human_message"] = self.human_message
        if self.error:
            d["error"] = self.error
        return d


@runtime_checkable
class Tool(Protocol):
    """Schnittstelle eines Tools."""
    name: str
    risk_level: RiskLevel

    def schema(self) -> dict[str, Any]:
        """OpenAI-Realtime-Function-Schema {type:function,name,description,parameters}."""
        ...

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        ...
