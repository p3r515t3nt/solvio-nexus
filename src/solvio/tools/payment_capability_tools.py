"""Was das Modell ueber Zahlungen darf — und was es nicht einmal nennen kann.

**Vier Werkzeuge, und keines davon bewegt Geld.** Sehen, was hinterlegt ist;
einen Kauf vorbereiten und beim Anbieter bewerten lassen; einen vorbereiteten
Kauf verwerfen; nachsehen, ob eine unklar gebliebene Zahlung durchging.

**Was hier ausdruecklich fehlt, ist der Punkt dieser Datei.** Es gibt kein
Werkzeug `purchase_place`, keines fuer Stornierung oder Erstattung und keines
fuer die Zahlungsmittelverwaltung. Diese Faehigkeiten sind am Router angemeldet,
aber sie haben keine Sprachseite: erreichbar sind sie ausschliesslich ueber den
attestierten Zahlungsweg des iPhones (`solvio.payment.endpoint`). Ein Modell
kann sie nicht aufrufen, weil es sie nicht nennen kann.

Das ist mehr als Bequemlichkeit. Der Router setzt eine Anfrage fort, wenn er zu
einer Faehigkeit eine bereits freigegebene offene Anfrage findet — gebaut fuer
„ich hab's doch gerade bestaetigt" im Gespraech. Gaebe es ein Werkzeug fuer
`purchase_place`, waere „sag es zweimal" ein Kauf. Es gibt keines.

Und es gibt erst recht kein `get_card_number`, `reveal_payment_token` oder
`execute_payment_raw`. Nicht als deaktiviertes Werkzeug, nicht als
auskommentierte Zeile, nicht unter anderem Namen. Ein Test zaehlt die
Werkzeugflaeche und faellt um, wenn das aufhoert zu stimmen.

Der Grund, warum eine LISTE trotzdem in Ordnung ist: `payment://shopping/default`
sagt, DASS es ein Zahlungsmittel gibt — nicht, welches. Genau darauf beruht die
ganze Konstruktion.
"""
from __future__ import annotations

from typing import Any

from solvio.capabilities.envelope import CapabilityOutcome, CapabilityResult
from solvio.logging_setup import get_logger
from solvio.tools.base import RiskLevel, ToolResult

log = get_logger("tools")

#: Die Werkzeugflaeche der Zahlung. Vier Zeilen. Bewusst.
_SCHEMAS: dict[str, dict[str, Any]] = {
    "payment_list_methods": {
        "description": (
            "Nennt die hinterlegten Zahlungsmittel: Verweis "
            "(payment://zweck/name), Name, Art, Zustand, erlaubte Waehrungen, "
            "erlaubte Haendler und Grenzen. Liefert NIE eine Kartennummer, "
            "Pruefziffer oder einen Anbieter-Token. Wenn der Nutzer ein "
            "Zahlungsmittel hinterlegen oder aendern will, verweise ihn auf "
            "Zahlungen in der App."),
        "parameters": {"type": "object", "properties": {}},
    },
    "payment_intent_prepare": {
        "description": (
            "Bereitet einen Kauf vor: legt den Vorgang an und laesst den "
            "verbindlichen Endbetrag beim Anbieter bestaetigen. BEZAHLT NICHTS. "
            "Der Nutzer bestaetigt danach am iPhone mit Face ID. Gib Posten als "
            "\"Bezeichnung|Menge|EinzelpreisInCent\", mehrere mit Semikolon "
            "getrennt. Betraege immer in Cent, ohne Komma."),
        "parameters": {"type": "object", "properties": {
            "zahlungsmittel": {"type": "string",
                               "description": "payment://zweck/name"},
            "haendler": {"type": "string",
                         "description": "Kennung aus payment_list_methods"},
            "zweck": {"type": "string", "description": "wofuer, in einem Satz"},
            "posten": {"type": "string",
                       "description": "Bezeichnung|Menge|EinzelpreisInCent"},
            "waehrung": {"type": "string", "description": "z. B. EUR"},
            "erwarteter_betrag": {
                "type": "string",
                "description": "was du erwartest, in Cent — wird nur VERGLICHEN"},
            "lieferung": {"type": "string",
                          "description": "sichere Bezeichnung, nie eine Adresse"}},
            "required": ["zahlungsmittel", "haendler", "zweck", "posten",
                         "waehrung"]},
    },
    "payment_intent_cancel": {
        "description": ("Verwirft einen vorbereiteten Kauf, der noch nicht "
                        "bezahlt ist."),
        "parameters": {"type": "object", "properties": {
            "vorgang": {"type": "string", "description": "die Vorgangskennung"}},
            "required": ["vorgang"]},
    },
    "payment_reconcile": {
        "description": (
            "Sieht beim Anbieter nach, ob eine unklar gebliebene Zahlung "
            "durchgegangen ist. Belastet nie. Benutze das, wenn ein Vorgang "
            "'reconciliation_required' oder 'awaiting_sca' ist."),
        "parameters": {"type": "object", "properties": {
            "vorgang": {"type": "string", "description": "die Vorgangskennung"}},
            "required": ["vorgang"]},
    },
}

#: Namen, die hier NIE ein Schema bekommen duerfen. Steht als Liste da, damit
#: ein Test sie zaehlen kann, statt auf Abwesenheit zu hoffen.
NEVER_EXPOSED: frozenset[str] = frozenset({
    "purchase_place", "payment_send", "bank_transfer", "invest_order",
    "purchase_cancel", "refund_request",
    "payment_method_add", "payment_method_replace", "payment_method_remove",
    "payment_method_enable", "payment_method_disable", "payment_method_rescope",
    "payment_limit_raise", "payment_limit_lower",
})


class PaymentTool:
    """Vorbereiten und nachsehen. Bezahlen steht hier nicht.

    **`HARMLESS` ist hier kein Urteil ueber die Handlung, sondern ueber DIESE
    Klasse.** Ein Faehigkeits-Werkzeug reicht weiter und entscheidet nichts;
    welche Reibung eine Handlung kostet, sagt Approval Policy V2 am Router —
    `payment_intent_prepare` ist dort `CRITICAL`, und aus dem Raum kostet es
    deshalb Face ID.

    Der erste Bau setzte hier die Stufe der Faehigkeit ein, und das war ein
    Fehler mit Ansage: der Dispatcher traegt noch die V1-Schranke, die bei
    `risk_level >= MUTATING` ein Argument `confirmed` verlangt. Ein
    Sprachaufruf bringt das nie mit. Drei der vier Werkzeuge waren damit
    strukturell tot — `tools.needs_confirmation`, danach `ok=False` in null
    Millisekunden, und die Faehigkeit wurde nie angefragt. Gefunden in der
    ersten Minute der Live-Abnahme, von keinem der 2355 gruenen Tests.

    Sicherheit geht dabei keine verloren: die Matrix entscheidet unveraendert,
    und die geldbewegenden Faehigkeiten haben ueberhaupt kein Werkzeug.
    """

    risk_level = RiskLevel.HARMLESS       # die Stufe der FAEHIGKEIT gilt, nicht diese
    expose_to_llm = True

    def __init__(self, capability: str, router: Any, gate: Any) -> None:
        self.name = capability
        self.capability = capability
        self.router = router
        self.gate = gate

    def schema(self) -> dict[str, Any]:
        entry = _SCHEMAS[self.capability]
        return {"type": "function", "name": self.name,
                "description": entry["description"], "parameters": entry["parameters"]}

    async def run(self, arguments: dict) -> ToolResult:
        args = dict(arguments or {})
        context = self.gate.context() if self.gate is not None else None
        if context is None or not context.has_principal:
            log.warning("payment.no_trusted_context", capability=self.capability)
            return ToolResult(False, error="no_trusted_context",
                              human_message="Ich kann gerade nicht sicher feststellen, "
                                            "wer fragt — deshalb bereite ich nichts vor.")
        result: CapabilityResult = await self.router.execute(
            self.capability, args, trust=context.trust,
            provenance=self.gate.provenance_for(args), principal=context.principal,
            origin=context.origin, commanded=context.commanded)
        return _speak(result)


def _speak(result: CapabilityResult) -> ToolResult:
    if result.succeeded:
        return ToolResult(True, data=result.data,
                          human_message=result.human_message or "")
    if result.outcome is CapabilityOutcome.EXECUTOR_UNAVAILABLE:
        message = "Der Zahlungsanbieter ist gerade nicht erreichbar."
    elif result.outcome is CapabilityOutcome.RECOVERY_REQUIRED:
        message = ("Ich weiss nicht sicher, ob das durchgegangen ist. Ich sehe "
                   "nach, statt es noch einmal zu versuchen.")
    else:
        message = result.human_message or "Das mache ich so nicht."
    return ToolResult(False, data=result.data, human_message=message,
                      error=f"{result.outcome.value}:{result.reason}" if result.reason
                      else result.outcome.value)


def payment_capability_tools(router: Any, gate: Any) -> list[PaymentTool]:
    return [PaymentTool(name, router, gate) for name in sorted(_SCHEMAS)]
