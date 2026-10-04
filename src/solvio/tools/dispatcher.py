"""Zentraler Tool-Dispatcher mit Risiko-/Bestaetigungs-Gate (Schritt 17).

Der Realtime-Core ruft NUR dispatch() auf. Der Dispatcher:
  1. lehnt unbekannte Tools ab
  2. validiert grob die Argumente
  3. prueft die Risikostufe (Level 2/3 brauchen confirmed=true)
  4. fuehrt das Tool aus und liefert ein serialisierbares Result-Dict
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from solvio.logging_setup import get_logger
from solvio.redaction import redact_text
from solvio.tools.base import RiskLevel, ToolResult

log = get_logger("tools")

_LOCAL_WAY_FORWARD_ONLY = ContextVar("solvio_local_tool_result", default=False)


@contextmanager
def local_way_forward_only():
    """Return existing Core results without starting legacy resolver inference.

    Used by the native Live bridge. Tool execution and every authority gate
    remain the same; only the optional automatic follow-up is suppressed.
    Context-local, so unrelated callers cannot inherit a global mode change.
    """
    token = _LOCAL_WAY_FORWARD_ONLY.set(True)
    try:
        yield
    finally:
        _LOCAL_WAY_FORWARD_ONLY.reset(token)



class ToolDispatcher:
    #: Der Untersucher. Bleibt `None`, solange nichts verdrahtet ist — dann
    #: verhaelt sich der Dispatcher exakt wie vorher.
    gap_resolver: Any = None
    #: Woher das eigentliche Ziel kommt. Ausdruecklich das Gate und nicht die
    #: Argumente: was der Nutzer wollte, steht im vertrauenswuerdigen Kontext,
    #: nicht in dem, was das Modell in den Aufruf geschrieben hat.
    capability_gate: Any = None

    def __init__(self) -> None:
        self._tools: dict[str, Any] = {}

    def register(self, tool: Any) -> None:
        self._tools[tool.name] = tool

    def names(self) -> list[str]:
        return sorted(self._tools)

    def tool(self, name: str) -> Any:
        """Das registrierte Werkzeug — oder `None`.

        Nur Zugriff, kein Aufruf: der eine Weg, auf dem eine Verdrahtung ein
        Werkzeug nachtraeglich anfassen darf (etwa `expose_to_llm` umlegen),
        ohne in `_tools` zu greifen. Ein Aufruf laeuft weiterhin ueber
        `dispatch` und damit durch alle Tore.
        """
        return self._tools.get(str(name or ""))

    def openai_tools(self) -> list[dict]:
        """Function-Schemas fuer session.tools der Realtime-Session."""
        return [t.schema() for t in self._tools.values() if getattr(t, 'expose_to_llm', True)]

    async def dispatch(self, name: str, arguments: dict | None) -> dict:
        """Der LLM-Pfad. Der Realtime-Core ruft ausschliesslich diese Methode.

        HYGIENE/H5: `expose_to_llm` filterte bisher nur `openai_tools()`, also die
        VEROEFFENTLICHTE Schema-Liste. Die Namensaufloesung hier prüfte es nicht, sodass ein
        nicht exponiertes Werkzeug — insbesondere `codex_confirm` — durch blosses Raten des
        Namens ueber den LLM-Dispatch erreichbar war. Ein verstecktes Schema ist kein
        Sicherheitsmechanismus; die Durchsetzung gehoert an die Aufloesung.
        """
        return await self._dispatch(name, arguments, llm=True)

    async def dispatch_trusted(self, name: str, arguments: dict | None) -> dict:
        """Der ausdrueckliche, NICHT modell-erreichbare Control-Plane-Kanal.

        Nur fuer Aufrufer, die selbst schon vertrauenswuerdig sind — das Sprachmodell hat
        keinen Weg hierher. Die Werkzeuge dahinter bleiben trotzdem hinter ihrem eigenen Gate
        (`codex_confirm` fuehrt nur aus, was der S1-Broker freigegeben hat).
        """
        return await self._dispatch(name, arguments, llm=False)

    async def _dispatch(self, name: str, arguments: dict | None, *, llm: bool) -> dict:
        args = arguments or {}
        tool = self._tools.get(name)
        if tool is not None and llm and not getattr(tool, "expose_to_llm", True):
            log.info("tools.not_exposed", name=name)
            return await self._with_way_forward(
                ToolResult(False, error=f"not_exposed_to_llm:{name}",
                           human_message="Dieses Werkzeug kann ich nicht aufrufen."
                           ).as_dict(), name)
        if tool is None:
            # Genau der Fall, um den es in diesem Meilenstein geht: das Modell
            # greift nach etwas, das es nicht gibt. Frueher endete das hier mit
            # „kenne ich nicht" — und das war die ganze Antwort.
            log.info("tools.unknown", name=name)
            return await self._with_way_forward(
                ToolResult(False, error=f"unknown_tool:{name}",
                           human_message="Dieses Werkzeug kenne ich nicht."
                           ).as_dict(), name)
        if not isinstance(args, dict):
            return ToolResult(False, error="invalid_arguments",
                              human_message="Die Argumente waren ungueltig.").as_dict()

        # Risiko-Gate: Level 2/3 brauchen ausdrueckliche Bestaetigung.
        if int(tool.risk_level) >= int(RiskLevel.MUTATING) and not bool(args.get("confirmed")):
            log.info("tools.needs_confirmation", name=name, risk=int(tool.risk_level))
            return {
                "success": False,
                "needs_confirmation": True,
                "risk_level": int(tool.risk_level),
                "human_message": (
                    "Diese Aktion braucht deine ausdrueckliche Bestaetigung. "
                    "Sag Ja, dann fuehre ich sie aus."),
            }
        try:
            result = await tool.run(args)
        except Exception as exc:  # noqa: BLE001 - nie den Core crashen
            log.error("tools.error", name=name, kind=type(exc).__name__)
            # DIESE ZEILE IST DIE SCHMALSTE STELLE ZWISCHEN EINER AUSNAHME UND
            # DEM MODELL. Das `ToolResult` wird als Ganzes in den Modellkontext
            # serialisiert (`core_server` schickt `json.dumps(result)` als
            # `function_call_output`). Ein Anbieter, der seinen Fehlerkoerper in
            # die Meldung schreibt — der Kalender tut das —, brauchte darin nur
            # einmal eine Kopfzeile stehen zu haben.
            #
            # Die Redaktion ist hier die ZWEITE Verteidigung. Die erste ist,
            # dass jede Ausnahme des Tresors ihren Grund als Kennung traegt und
            # nie einen Wert; ein Test zaehlt das nach.
            return ToolResult(
                False,
                error=redact_text(f"{type(exc).__name__}: {str(exc)[:120]}"),
                human_message="Beim Ausfuehren gab es einen Fehler.").as_dict()
        log.info("tools.ok", name=name, success=result.success)
        return await self._with_way_forward(result.as_dict(), name)

    async def _with_way_forward(self, payload: dict, capability: str) -> dict:
        """Haengt an ein blockiertes Ergebnis den untersuchten Weg nach vorn.

        Das ist die ganze Integration in den Sprachpfad — eine Stelle, durch die
        ohnehin jeder Werkzeugaufruf laeuft. Kein zweiter Pfad, kein Umbau von
        M0, und der Nutzer muss nichts sagen wie „such mal nach einem Weg".

        Das Ergebnis wird **ergaenzt**, nie ersetzt: `success` bleibt falsch,
        `error` bleibt stehen. Eine Untersuchung macht aus einem Fehlschlag
        keinen Erfolg — sie sagt nur, was jetzt sinnvoll waere.

        Diese Schicht bleibt dabei unwissend: sie kennt Werkzeuge und
        Fehlertexte, nicht die Vertragsstufe darueber. Ein Import von dort waere
        bequem gewesen und haette die Schichtung verrutschen lassen — ein
        bestehender Test haelt das fest, und zu Recht.
        """
        if _LOCAL_WAY_FORWARD_ONLY.get() or self.gap_resolver is None or payload.get("success"):
            return payload
        context = self.capability_gate.context() if self.capability_gate else None
        goal = getattr(context, "user_text", "") if context is not None else ""
        trust = getattr(context, "trust", None) if context is not None else None
        try:
            # Der Dispatcher reicht nur Zeichenketten weiter. Was ein
            # `approval_required:awaiting_user_approval` bedeutet, weiss die
            # Schicht darueber — hier bleibt es ein Fehlertext.
            resolution = await self.gap_resolver.for_failed_tool(
                error=str(payload.get("error") or ""), tool=capability,
                goal=goal, trust=trust)
        except Exception as exc:  # noqa: BLE001 - eine Untersuchung darf nie stoeren
            log.error("resolver.failed", kind=type(exc).__name__)
            return payload
        if resolution is None:
            return payload
        log.info("resolver.resolved", capability=capability,
                 state=resolution.state.value, kind=resolution.kind.value)
        payload["weiterweg"] = resolution.as_dict()
        payload["human_message"] = resolution.speak()
        return payload

    @staticmethod
    def parse_args(raw: str | dict | None) -> dict:
        if isinstance(raw, dict):
            return raw
        if not raw:
            return {}
        try:
            v = json.loads(raw)
            return v if isinstance(v, dict) else {}
        except Exception:  # noqa: BLE001
            return {}
