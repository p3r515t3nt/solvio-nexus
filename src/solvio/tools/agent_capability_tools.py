"""Die Bruecke vom Modell zur Agentenlaufzeit — bewusst duenn.

Dieselbe Bauart wie bei Kalender, Home Assistant, Gmail und der tiefen
Recherche, und aus demselben Grund so duenn: das Modell nennt ein Ziel, der
Vertrag entscheidet alles andere. Kein Feld dieser Schemata traegt Autoritaet.
Es gibt keinen Parameter fuer „dringend", keinen fuer „freigegeben", keinen fuer
den Auftraggeber und keinen fuer die Herkunft — wer fragt, steht im Turn, nicht
im Argument.

`risk_level` bleibt HARMLESS, weil die Entscheidung im Vertrag faellt und nicht
hier: `agent_task_research` und `agent_task_build` sind in der Politik
`NORMAL_WRITE`, und vom Raummikrofon kostet das Face ID. Die Risikostufe am
Werkzeug ist nicht die Stelle, an der so etwas entschieden wird — das war schon
beim stillgelegten `codex_task` die Verwechslung, die teuer war.
"""
from __future__ import annotations

from typing import Any

from solvio.capabilities.agent import SPECS
from solvio.capabilities.envelope import CapabilityOutcome, CapabilityResult
from solvio.logging_setup import get_logger
from solvio.tools.base import RiskLevel, ToolResult

log = get_logger("tools")

_SCHEMAS: dict[str, dict[str, Any]] = {
    "agent_task_research": {
        "description": ("Einen Auftrag im Hintergrund erledigen, einschließlich der nativen "
                        "Erzeugung einer Bilddatei über das Abo. Für 'generiere mir ein Bild' "
                        "den vollständigen Bildwunsch als objective übergeben; einen Prompt "
                        "niemals als fertiges Bild ausgeben. Fertige Dateien sind im Auftrag "
                        "auf dem iPhone und im Dashboard verfügbar. Die Annahme ist noch kein Erfolg. "
                        "Auch etwas herausfinden, das mehrere Schritte braucht: nachsehen, "
                        "vergleichen, pruefen, gegenpruefen. SOLVIO arbeitet es im "
                        "Hintergrund ab und meldet sich mit dem Ergebnis.\n"
                        "Nimm das, sobald ein Ziel danach klingt — der Nutzer muss "
                        "nicht darum bitten und kein bestimmtes Wort sagen. "
                        "Auch 'kannst du mal schauen, warum das nicht geht', "
                        "'findest du raus, woran das liegt' oder 'pruef das mal "
                        "gruendlich' sind solche Ziele.\n"
                        "NICHT dafuer: eine schnelle Einzelfrage (deep_research), "
                        "eine Stoerung an SOLVIO selbst (system_diagnose), oder "
                        "etwas, das du direkt beantworten kannst."),
        "parameters": {"type": "object", "properties": {
            "objective": {"type": "string",
                          "description": "Das Ziel in den Worten des Nutzers — was "
                                         "erledigt oder erzeugt werden soll und wozu."}},
            "required": ["objective"]},
    },
    "agent_task_build": {
        "description": ("Etwas an Code bauen, aendern oder reparieren. Die Arbeit "
                        "passiert in einer isolierten Kopie; das Ergebnis wird "
                        "VORBEREITET, nicht uebernommen — Uebernehmen bleibt die "
                        "Entscheidung des Nutzers.\n"
                        "Nimm das, sobald ein Ziel danach klingt. Auch 'schau mal, "
                        "warum dieser Fehler auftritt und behebe ihn' oder 'mach mir "
                        "daraus eine funktionierende Loesung' sind solche Ziele."),
        "parameters": {"type": "object", "properties": {
            "objective": {"type": "string",
                          "description": "Das Ziel in den Worten des Nutzers — was "
                                         "gebaut oder repariert werden soll."},
            # Live gefunden: hier stand „Optional: welches Projekt", und das Modell
            # trug das blosse Wort „Projekt" aus dem gesprochenen Satz ein. Ein
            # Pfadfeld, das nach einem Namen fragt, bekommt einen Namen.
            "repository": {"type": "string",
                           "description": "Nur ausfuellen, wenn der Nutzer einen "
                                          "konkreten Ordnerpfad genannt hat "
                                          "(beginnend mit / oder ~). Sonst weglassen "
                                          "— dann nimmt SOLVIO sein Hauptprojekt."}},
            "required": ["objective"]},
    },
    "agent_run_status": {
        "description": "Sagt, wie weit ein Auftrag ist und was dabei herauskam.",
        "parameters": {"type": "object", "properties": {
            "run_id": {"type": "string",
                       "description": "Optional: die Kennung eines Auftrags. "
                                      "Ohne Angabe kommen die letzten."}}},
    },
    "agent_run_cancel": {
        "description": "Bricht einen laufenden Auftrag ab.",
        "parameters": {"type": "object", "properties": {
            "run_id": {"type": "string", "description": "Die Kennung des Auftrags."}},
            "required": ["run_id"]},
    },
    "agent_run_resume": {
        "description": ("Nimmt einen Auftrag wieder auf, nachdem der Nutzer die eine "
                        "Handlung erledigt hat, die nur er tun konnte."),
        "parameters": {"type": "object", "properties": {
            "run_id": {"type": "string", "description": "Die Kennung des Auftrags."}},
            "required": ["run_id"]},
    },
}


class AgentCapabilityTool:
    risk_level = RiskLevel.HARMLESS
    expose_to_llm = True

    def __init__(self, capability: str, router: Any, gate: Any,
                 ledger: Any = None) -> None:
        self.name = capability
        self.capability = capability
        self.router = router
        self.gate = gate
        # Nur Faehigkeiten mit einem Schema in `_SCHEMAS` sind dem Sprachmodell
        # als Werkzeug sichtbar. Gemessen 2026-09-18 (unabhaengiger Review A1):
        # `agent_task_action` (seit 11.09.) und `agent_task_task` hatten keinen
        # Eintrag, `openai_tools()` warf beim Sitzungsaufbau KeyError — und die
        # Sprach-Delegation an die Agentenlaufzeit scheiterte seit 12.09. still
        # (`live.delegation_failed kind=KeyError`). Beide Faehigkeiten haben
        # ihren eigenen, autorisierten Eingang (TaskStartService, App/Dashboard);
        # ohne Startbeleg wuerde ein Modellaufruf ohnehin verweigert.
        self.expose_to_llm = capability in _SCHEMAS
        #: Nur fuer die zwei Faehigkeiten, die einen Auftrag ERZEUGEN. Ohne ihn
        #: verhaelt sich das Werkzeug wie zuvor — die Freigabe wird angefragt,
        #: aber niemand nimmt sie spaeter auf.
        self.ledger = ledger

    def schema(self) -> dict[str, Any]:
        entry = _SCHEMAS.get(self.capability)
        if entry is None:
            raise LookupError("capability without an LLM schema: " + self.capability)
        return {"type": "function", "name": self.name,
                "description": entry["description"], "parameters": entry["parameters"]}

    async def run(self, arguments: dict) -> ToolResult:
        args = dict(arguments or {})
        context = self.gate.context() if self.gate is not None else None
        if context is None or not context.has_principal:
            log.warning("agent_capability.no_trusted_context", capability=self.capability)
            return ToolResult(False, error="no_trusted_context",
                              human_message="Ich kann gerade nicht sicher feststellen, "
                                            "wer fragt — deshalb mache ich nichts.")
        start = None
        from solvio.capabilities.invocation import is_information_question
        if (self.capability == "agent_task_research" and not context.commanded
                and set(args) == {"objective"} and is_information_question(context.user_text)):
            # The live model chooses a tool, not the scope of question authority.
            # Bind the exact current human question; never promote the whole turn.
            args = {"objective": context.user_text.strip()}
        authorize = getattr(self.gate, "authorize_task_start", None)
        if self.capability in CREATION_CAPABILITIES and callable(authorize):
            start = await authorize(self.capability, args, context=context)
        from solvio.capabilities.policy import OriginClass
        if (self.capability in CREATION_CAPABILITIES
                and context.origin is OriginClass.TRUSTED_DASHBOARD and start is None):
            # A missing/expired browser voice proof is not a room-microphone
            # request. Never replace logout with a new phone approval.
            return ToolResult(False, error="task_start_authorization_missing",
                human_message="Diese Sprachsitzung oder ihr Auftragsturn ist nicht mehr aktiv.")
        result: CapabilityResult = await self.router.execute(
            self.capability, args, trust=context.trust,
            provenance=self.gate.provenance_for(args), principal=context.principal,
            origin=context.origin, commanded=context.commanded,
            **({"task_start": start} if start is not None else {}))
        self._remember_if_waiting(result, args, context)
        return _speak(result)

    def _remember_if_waiting(self, result: CapabilityResult, args: dict,
                             context: Any) -> None:
        """Der Merkzettel — hier fuer dieses Werkzeug, gemeinsam gehalten.

        Die Semantik wohnt in `remember_pending_start` weiter unten, weil sie
        seit Cognitive Router V1 ZWEI Aufrufer hat: dieses Werkzeug und die
        Kommission des Routers. Zwei Fassungen desselben Zettels waeren zwei
        Wahrheiten ueber die eine Frage, ob eine Freigabe den Auftrag noch
        startet — und die Antwort darauf war schon einmal teuer.
        """
        remember_pending_start(self.ledger, capability=self.capability,
                               result=result, arguments=args, context=context)


def remember_pending_start(ledger: Any, *, capability: str,
                           result: CapabilityResult, arguments: dict,
                           context: Any) -> None:
    """Eine Freigabe, die den Auftrag erst erzeugt, braucht einen Merkzettel.

    **Live gefunden, und es war der schwerste Fund des Agent-Runtime-
    Milestones.** Der Nutzer gab per Face ID frei — und nichts geschah. Die
    Anfrage lief auf EXPIRED, mit null Ausfuehrungsversuchen.

    Der Grund ist eine Henne-Ei-Luecke: ein Lauf, der auf eine Freigabe wartet,
    hat einen Poller im Takt. Der Auftrag, der den Lauf erst erzeugt, hatte
    keinen — die Anfragekennung stand im Umschlag und starb mit dem
    Gespraechszug. Wer danach nicht zufaellig nochmal dasselbe sagte, wartete
    auf ein Ergebnis, das nie kam.

    Der Merkzettel traegt keine Autoritaet. Er haelt genau das, was noetig ist,
    um denselben Aufruf UNVERAENDERT zu wiederholen — Argumente, Prinzipal,
    Herkunft. Die Herkunft geht in den Freigabe-Digest ein und wird deshalb
    gespeichert statt neu bestimmt. Und die Argumente sind die, die
    tatsaechlich hinausgingen: eine nachtraeglich normalisierte Fassung waere
    ein anderer Digest und damit eine wertlose Freigabe.

    `capability` ist immer die ZIELFAEHIGKEIT. Ein Zettel unter einem Namen,
    den `start_approved_capability` nicht kennt, wuerde beim naechsten Takt
    geschlossen und dann abgelehnt — die Freigabe waere verbraucht, ohne dass
    etwas lief.
    """
    if ledger is None or capability not in CONTINUABLE_CAPABILITIES:
        return
    if result.outcome is not CapabilityOutcome.APPROVAL_REQUIRED:
        return
    request_id = str((result.data or {}).get("request_id") or "")
    if not request_id:
        log.warning("agent_capability.approval_without_id", capability=capability)
        return
    try:
        from solvio.conversation.mail import voice_pending
        pending = voice_pending(ledger, context) if capability == 'gmail_send_draft' else None
        (pending or ledger).remember_pending_start(
            request_id=request_id, capability=capability,
            arguments=arguments, principal=context.principal,
            origin=getattr(context.origin, "value", str(context.origin)),
            commanded=bool(context.commanded))
    except Exception as exc:  # noqa: BLE001
        log.warning("agent_capability.remember_failed",
                    capability=capability, kind=type(exc).__name__)


def _speak(result: CapabilityResult) -> ToolResult:
    if result.succeeded:
        return ToolResult(True, data=result.data, human_message=result.human_message or "")
    if result.outcome is CapabilityOutcome.EXECUTOR_UNAVAILABLE:
        message = "Die Agentenlaufzeit ist gerade nicht verfuegbar — es laeuft nichts."
    elif result.outcome is CapabilityOutcome.APPROVAL_REQUIRED:
        message = ("Dafuer brauche ich deine Freigabe — sie liegt auf deinem iPhone.")
    else:
        message = result.human_message or "Das habe ich nicht ausgefuehrt."
    return ToolResult(False, data=result.data,
                      human_message=result.human_message or message,
                      error=f"{result.outcome.value}:{result.reason}" if result.reason
                      else result.outcome.value)


#: Die zwei Faehigkeiten, die einen Auftrag ERZEUGEN. `status` und `cancel`
#: beziehen sich auf etwas, das es schon gibt, und laufen ohne Freigabe.
CREATION_CAPABILITIES = frozenset({"agent_task_research", "agent_task_build"})
#: Was ausserdem einen Merkzettel braucht: das direkte Notizwerkzeug und die
#: Wiederaufnahme an einer Nutzergrenze. Beide kosten aus dem Raum und vom
#: lokalen Rechner Face ID — und beide wuerden ohne Zettel nach dem
#: Gespraechsende mit einer erteilten Freigabe ins Leere laufen. Die Liste
#: ist DIESELBE wie `steps.RESUMABLE_STARTS`; ein Test haelt das fest.
#: Seit Stufe S2 auch der Mailversand (ADR-0041): er wartet immer auf Face ID.
CONTINUABLE_CAPABILITIES = CREATION_CAPABILITIES | {"note_write", "agent_run_resume",
                                                    "gmail_send_draft", "communication_send",
                                                    "communication_confirm_binding", "background_create"}


def agent_capability_tools(router: Any, gate: Any,
                           ledger: Any = None) -> list[AgentCapabilityTool]:
    return [AgentCapabilityTool(name, router, gate, ledger)
            for name in sorted(SPECS)]
