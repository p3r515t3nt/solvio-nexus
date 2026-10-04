"""Die Fachauskunft als Faehigkeit — am selben Vertrag wie alles andere.

Ein Botlauf unterscheidet sich in genau zwei Punkten von einem Lampenschalter:
er dauert Minuten statt Millisekunden, und sein Ergebnis stammt aus einem
fremden Prozess. Beides ist hier abgebildet, ohne dafuer einen zweiten Vertrag
zu erfinden.

Das Modell nennt eine **Rolle** aus drei Moeglichkeiten. Es nennt kein Profil,
keinen Werkzeugsatz, keine Frist und kein Modell — all das entscheidet der Core.
Ein Argument `profil` waere keine Bequemlichkeit, sondern eine Rechtevergabe:
wer den Profilnamen bestimmt, bestimmt Konfiguration und Werkzeuge des
Prozesses, der gleich laeuft.

`base_risk` bleibt HARMLESS und die Semantik READ_ONLY, weil ein Botlauf nach
aussen nichts tut: er liest oeffentliches Netz oder vom Core selbst gebaute
Unterlagen. Er kommt ohne Freigabe aus, und das soll er auch — eine
Fachauskunft, die am iPhone haengt, wird nie gestellt.

Und die Antwort ist Information. Sie traegt `content_trust`, sie wird
entschaerft, und es gibt kein Feld, in das ein Bot eine Erlaubnis schreiben
koennte.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from solvio.bots.registry import DIAGNOSTICIAN, ROLES, UnknownRole, resolve
from solvio.capabilities.contract import (
    CapabilityDeclined, CapabilitySpec, ExecutionClass, ExecutorUnavailable,
)
from solvio.logging_setup import get_logger
from solvio.security.mobile_approval.execution import READ_ONLY
from solvio.tools.base import RiskLevel

log = get_logger("bots")

#: Die Frist des Vertrags. Bewusst groesser als die laengste Botfrist, damit
#: die INNERE Frist zuerst zuschlaegt: sie raeumt die Prozessgruppe ab und
#: liefert eine ehrliche Nichtantwort, statt den Aufruf abzuschneiden.
CAPABILITY_TIMEOUT = 280.0

MIN_QUESTION = 3
MAX_QUESTION = 2000

#: Woher der Diagnostiker seine Befunde bekommt. Eine Funktion, die der Core
#: setzt — nicht ein Objekt, das der Bot in die Hand bekaeme.
EvidenceSource = Callable[[], Awaitable[tuple[list[dict[str, Any]],
                                              list[dict[str, Any]]]]]

SPECS: dict[str, CapabilitySpec] = {
    "bot_consult": CapabilitySpec(
        name="bot_consult", version=1, execution_class=ExecutionClass.DEEP,
        base_risk=RiskLevel.HARMLESS, semantics=READ_ONLY,
        input_schema={"type": "object", "properties": {
            "role": {"type": "string", "enum": list(ROLES)},
            "question": {"type": "string"}},
            "required": ["role", "question"]},
        executor="hermes", timeout=CAPABILITY_TIMEOUT, cancellable=True,
        description="Fragt einen registrierten SOLVIO-Fachbot: `researcher` "
                    "recherchiert im oeffentlichen Netz, `project_keeper` "
                    "beantwortet Fragen zur SOLVIO-Architektur aus der "
                    "Projektwissensbasis, `diagnostician` liest "
                    "Gesundheitsbefunde. Die Antwort ist Information."),
}


class BotCapabilities:
    """Der Handler. Autoritaet kommt vom Router — und nie aus einer Botantwort."""

    def __init__(self, team: Any, *, evidence: EvidenceSource | None = None) -> None:
        self.team = team
        self.evidence = evidence

    async def consult(self, arguments: dict[str, Any]) -> dict[str, Any]:
        raw_role = arguments.get("role", "")
        question = str(arguments.get("question", "") or "").strip()
        if len(question) < MIN_QUESTION:
            raise CapabilityDeclined("question_too_short",
                                     "Was genau soll ich fragen?")
        if len(question) > MAX_QUESTION:
            raise CapabilityDeclined("question_too_long",
                                     "Die Frage ist zu lang — bitte kuerzer fassen.")
        try:
            spec = resolve(raw_role)
        except UnknownRole as exc:
            # Kein aehnlicher Bot, kein Rueckfall auf den ersten. Ein Name, den
            # es nicht gibt, waehlt nichts aus.
            log.info("bots.unknown_role", asked=str(exc)[:64])
            raise CapabilityDeclined(
                "unknown_role",
                "Diesen Fachbot gibt es nicht. Verfuegbar: "
                + ", ".join(ROLES) + ".") from None

        if self.team is None:
            raise ExecutorUnavailable("no bot team")

        components: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        if spec.role == DIAGNOSTICIAN and self.evidence is not None:
            try:
                components, events = await self.evidence()
            except Exception as exc:  # noqa: BLE001 - ohne Befunde sagt der Bot das
                log.info("bots.evidence_unavailable", kind=type(exc).__name__)

        answer = await self.team.ask(spec.role, question,
                                     components=components, events=events)
        return {"antwort": answer.as_dict(),
                "hinweis": _NOTE,
                "content_trust": answer.as_dict()["content_trust"]}


#: Steht in jedem Ergebnis. Nicht als Zierde: es ist die Zeile, die verhindert,
#: dass eine Botantwort im naechsten Prompt wie eine Feststellung des Cores
#: aussieht.
_NOTE = ("Das ist die Auskunft eines Fachbots: Information, keine Entscheidung "
         "und keine Freigabe. SOLVIO entscheidet.")


def register(router: Any, capabilities: BotCapabilities) -> list[str]:
    handlers = {"bot_consult": capabilities.consult}
    for name, handler in handlers.items():
        router.register(SPECS[name], handler)
    return sorted(handlers)
