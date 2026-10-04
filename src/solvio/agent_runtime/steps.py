"""Die EINZIGE Stelle der Agentenlaufzeit, die `router.execute` ruft.

Das ist Absicht und wird von einem Quellscan festgenagelt: `origin=` kommt in
der ganzen Laufzeit nur hier vor. Eine zweite Stelle waere eine zweite Meinung
darueber, was eine Herkunft ist — und die erste, die jemand vergisst
nachzuschaerfen.

## Die Stempel, immer gleich

    trust    = TrustContext(USER_DIRECT, user_authorized=True, note=…)
    origin   = OriginClass.BACKGROUND_AUTOMATION
    principal= f"agent:{run_id[:12]}"
    commanded= True

**Herkunft ist immer `BACKGROUND_AUTOMATION`** — auch wenn die Aufgabe in einem
lebenden iPhone-Turn erteilt wurde. Eine Vordergrund-Erleichterung blutet
strukturell nicht in einen Lauf hinein, der Stunden spaeter handelt. Nur die
ERZEUGUNG der Aufgabe laeuft in der Herkunft des Turns. Das ist wortgleich die
Regel des Hintergrundlaeufers: „gesetzt, nicht geerbt".

Der `TrustContext` ist `USER_DIRECT`, weil ein Mensch die AUFGABE beauftragt
hat — und `user_authorized=True` beschreibt genau das und nichts weiter. Die
Strenge kommt aus zwei anderen Richtungen, und sie sind es, die wirken: die
Herkunft (Hintergrund) und die **Provenienz je Argument**.

## Warum die Provenienz die eigentliche Arbeit macht

Die Matrix strengt nur bei `UNTRUSTED_CONTENT`; `MODEL_DERIVED` hebt den
Risikowert, aendert aber keine Zelle. Deshalb fuehrt der Orchestrator eine
QUELLKETTE je Argument, und alles, was aus Spezialistenausgabe stammt, ist
`UNTRUSTED_CONTENT` — ohne Ausnahme. Ein `SpecialistResult` traegt keine
Je-Feld-Provenienz, also gibt es fuer seine Inhalte keine mildere Einstufung.

## Warum der Freigabezustand GELESEN und nie geraten wird

`router.execute` mit `approval_request_id` kollabiert PENDING, DENIED, EXPIRED,
EXECUTING, CONSUMED, FAILED und unbekannte Kennung ALLE zu `not_approved`
(`mobile_approval/bridge.py:102-104`). Daraus laesst sich eine Ablehnung nicht
erkennen — und eine Ablehnung, die man fuer „noch nicht" haelt, wird gefragt,
bis der Mensch nachgibt. Der Orchestrator liest deshalb den Zustand ueber
dieselbe Lesefunktion, die `h_status` am Gateway benutzt, und verzweigt
deterministisch.

Eine gemessene Falle dabei: **`get_request` laeuft den Verfall NICHT mit.**
`_expire_due()` haengt an `_list_pending`. Eine Zeile, deren `expires_at`
vorbei ist, liest sich also weiter als `PENDING`. Wer das nicht selbst prueft,
parkt einen Lauf fuer immer an einer Freigabe, die es nicht mehr gibt.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from solvio.agent_runtime import authority
from solvio.logging_setup import get_logger

log = get_logger("agent_runtime")

#: Zustandsworte des Freigabewegs. Hier gespiegelt, damit ein Import auf den
#: eingefrorenen Baum nicht noetig ist — und ein Test vergleicht sie gegen die
#: Quelle, damit die Spiegelung nicht auseinanderlaeuft.
PENDING = "PENDING"
APPROVED = "APPROVED"
DENIED = "DENIED"
EXECUTING = "EXECUTING"
CONSUMED = "CONSUMED"
EXPIRED = "EXPIRED"
FAILED = "FAILED"

#: Hoechstens zwei Neuanfragen nach Verfall, dann wird daraus eine Nutzergrenze.
#: „Der Mensch hat es nicht gesehen" ist kein Grund, ihn oefter zu fragen —
#: sondern einer, ihn beim naechsten Hinsehen zu erwischen.
MAX_APPROVAL_REQUESTS = 3


@dataclass
class StepOutcome:
    """Was ein Schritt ergeben hat. Der Umschlag des Routers ist die einzige
    Wahrheit ueber den Ausgang — dies ist seine Uebersetzung, nicht sein Ersatz."""

    state: str                  # succeeded | failed | denied | waiting | unknown
    reason: str = ""
    call_id: str = ""
    approval_id: str = ""
    execution_id: str = ""
    outcome_reason: str = ""
    failure_category: str = ""
    data: object = None
    human_message: str = ""
    #: Gesetzt, wenn der Schritt an einer Nutzergrenze steht.
    boundary: object = None


def agent_principal(run_id: str) -> str:
    """`agent:<gekuerzte Laufkennung>` — im Journal sofort als Lauf lesbar."""
    return f"agent:{run_id[:12]}"


def agent_trust(task_id: str, when: str):
    """Der TrustContext eines Laufs. Konstant, vom Core gesetzt, nie vom Modell.

    Er sagt: ein Mensch hat DIESE Aufgabe beauftragt. Er sagt ausdruecklich
    NICHT, dass der Mensch jeden einzelnen Schritt gesehen hat — dafuer ist die
    Herkunft `BACKGROUND_AUTOMATION` da, und die macht die Matrix streng.
    """
    from solvio.contracts.trust import TrustContext, TrustLevel

    return TrustContext(TrustLevel.USER_DIRECT, user_authorized=True,
                        note=f"agent task {task_id} commissioned by the user on {when}")


def provenance_map(sources: dict[str, str]) -> dict:
    """Je Argument eine Stufe. Unbekannte Quelle bekommt die STRENGSTE."""
    return {name: authority.provenance_for(source) for name, source in sources.items()}


def request_is_expired(row: dict, *, now: float = 0.0) -> bool:
    """Ob eine gelesene Zeile in Wahrheit schon verfallen ist.

    `get_request` sweept nicht. Ohne diese Pruefung parkt ein Lauf an einer
    Freigabe, die der Kontrollweg laengst nicht mehr einloesen wuerde.
    """
    try:
        expires = float(row.get("expires_at") or 0.0)
    except (TypeError, ValueError):
        return False
    return bool(expires) and (now or time.time()) >= expires


def classify_approval(row: dict | None, *, now: float = 0.0) -> str:
    """Der Zustand einer Freigabe — gelesen, nicht geraten.

    Unbekannte Kennung ist `EXPIRED` und nicht `DENIED`: nach einem
    Core-Neustart verfallen alle PENDING-Anfragen als
    `orphaned_by_core_restart`, und die Zeile kann fort sein. Das als Ablehnung
    zu lesen waere die falsche Endgueltigkeit.
    """
    if not row:
        return EXPIRED
    state = str(row.get("state") or "")
    if state == PENDING and request_is_expired(row, now=now):
        return EXPIRED
    return state or EXPIRED


class ApprovalQueue:
    """Freigabepflichtige Schritte sind ueber ALLE Laeufe global serialisiert.

    Der Router verdraengt eine offene Anfrage, sobald dieselbe Faehigkeit
    erneut angefragt wird (`superseded_by_new_request`). Zwischen zwei Laeufen
    waere das ein Freigabe-Kannibalismus, den niemand sieht: Lauf A wartet,
    Lauf B fragt dasselbe, A verliert seine Anfrage.

    Diese Warteschlange stuetzt sich nicht auf jene Verdraengung — sie macht
    sie **irrelevant**: hoechstens EIN offener freigabepflichtiger Schritt in
    der ganzen Laufzeit. Wartezeiten stehen als Ereignis im Ledger.
    """

    def __init__(self) -> None:
        self._holder: str = ""

    @property
    def holder(self) -> str:
        return self._holder

    def acquire(self, run_id: str) -> bool:
        if self._holder and self._holder != run_id:
            return False
        self._holder = run_id
        return True

    def release(self, run_id: str) -> None:
        if self._holder == run_id:
            self._holder = ""


#: Was dieser Weg ueberhaupt wiederholen darf. Bewusst einzelne Namen und keine
#: Regel: eine Praefixregel waere die Stelle, an der spaeter etwas dazukommt,
#: das niemand gepruft hat.
#: Die Faehigkeiten, deren Freigabe der Takt nach dem Gespraechsende einloest.
#: Zwei Auftragsstarts, das direkte Notizwerkzeug — und seit den
#: Agentenauftraegen im Gespraechsweg die Wiederaufnahme an einer
#: Nutzergrenze: „Habe ich erledigt, mach weiter" kostet vom Raummikrofon
#: und vom lokalen Rechner Face ID, und ohne diese Zeile fuehrte niemand die
#: spaeter erteilte Freigabe aus. Sie erzeugt keinen Lauf; sie nimmt einen
#: vorhandenen wieder auf — mit denselben gebundenen Argumenten.
RESUMABLE_STARTS = frozenset({"agent_task_research", "agent_task_build", "note_write",
                              "agent_run_resume", "gmail_send_draft",
                              "communication_send", "communication_confirm_binding", "background_create"})


async def start_approved_capability(router, *, name: str, arguments: dict,
                                    request_id: str, principal: str,
                                    origin, commanded: bool):
    """Einen freigegebenen START-Auftrag genau einmal wiederholen.

    Getrennt von `execute_capability`, und der Unterschied ist die HERKUNFT.
    Ein Schritt INNERHALB eines Laufs laeuft immer als
    `BACKGROUND_AUTOMATION` — dafuer ist die Klasse da, und die Matrix ist
    entsprechend streng. Ein Auftrag, der noch gar keinen Lauf hat, wurde
    dagegen unter der Herkunft des Menschen freigegeben, der ihn gab, und
    genau die geht in den Freigabe-Digest ein.

    Deshalb wird die Herkunft hier UEBERGEBEN statt gesetzt: sie stammt aus
    dem Merkzettel und wird nie neu bestimmt. Was am Raummikrofon freigegeben
    wurde, darf nicht als etwas Vertrauteres wiederkommen.

    Der Aufruf gewinnt keine Autoritaet. Er legt eine Kennung erneut vor; ob
    daraus eine Ausfuehrung wird, entscheidet unveraendert der Freigabeweg.

    **Warum hier NICHT `authority.guard` steht.** Die Sperrliste verbietet
    alles mit dem Praefix `agent_` — das ist die Rekursionssperre: ein LAUF
    soll keinen weiteren Lauf erzeugen koennen. Sie ist richtig und bleibt.
    Dieser Weg ist aber kein Lauf: er loest eine Freigabe ein, die ein Mensch
    im Gespraech ausgeloest und am Geraet bestaetigt hat.

    Die ausdrueckliche Liste umfasst die beiden Auftragsstarts, das direkte
    Notizwerkzeug und die Wiederaufnahme eines wartenden Laufs. Keine
    generische Wiederholung anderer Schreibwerkzeuge.

    Und die Zeilen, aus denen dieser Weg liest, entstehen ausschliesslich in
    den Gespraechsadaptern, mit geprueftem Prinzipal. Die Notizfortsetzung
    benoetigt keinen zweiten Werkzeugaufruf nach dem Gespraechsende.
    """
    if name not in RESUMABLE_STARTS:
        log.warning("agent_runtime.start_not_resumable", capability=name)
        raise authority.CapabilityBlocked(name, "not_a_start_capability")
    capability = name
    return await router.execute(
        capability, arguments,
        trust=agent_trust(request_id, time.strftime("%Y-%m-%d")),
        provenance={},
        approval_request_id=request_id,
        principal=principal,
        origin=origin,
        commanded=commanded)


async def execute_capability(router, *, name: str, arguments: dict,
                             sources: dict[str, str], run_id: str, task_id: str,
                             when: str, approval_request_id: str = "",
                             cancel_token=None, task_step=None) -> StepOutcome:
    """Genau ein `router.execute`. Die einzige Stelle der Laufzeit.

    Die Sperrliste greift VOR dem Router — ein gesperrter Name erreicht ihn
    nicht einmal. Das ist Verteidigung in der Tiefe, nicht Ersatz: auch ohne sie
    blieben die Matrix, der Tresor-Zaun und die Herkunftspruefungen.
    """
    from solvio.capabilities.envelope import CapabilityOutcome as OUT
    from solvio.capabilities.policy import OriginClass

    try:
        capability = authority.guard(name)
    except authority.CapabilityBlocked as exc:
        log.warning("agent_runtime.capability_blocked", run_id=run_id,
                    capability=name, reason=exc.reason)
        return StepOutcome(state="denied", reason=exc.reason,
                           failure_category="policy_denied",
                           outcome_reason=f"blocked:{exc.reason}",
                           human_message="Das darf ein Lauf nicht.")

    result = await router.execute(
        capability, arguments,
        trust=agent_trust(task_id, when),
        provenance=provenance_map(sources),
        approval_request_id=approval_request_id or None,
        principal=agent_principal(run_id),
        origin=OriginClass.BACKGROUND_AUTOMATION,
        commanded=True,
        cancel_token=cancel_token, **({"task_step": task_step} if task_step else {}))

    envelope = f"{result.outcome.value}:{result.reason}" if result.reason else \
        result.outcome.value

    if result.outcome is OUT.SUCCESS:
        return StepOutcome(state="succeeded", call_id=result.call_id,
                           outcome_reason=envelope, data=result.data,
                           human_message=result.human_message)

    if result.outcome is OUT.APPROVAL_REQUIRED:
        # Parken, nicht blockieren: keine Coroutine wartet hier auf einen
        # Menschen. Die Anfragekennung steht im Umschlag.
        approval_id = ""
        if isinstance(result.data, dict):
            approval_id = str(result.data.get("request_id")
                              or result.data.get("approval_id") or "")
        return StepOutcome(state="waiting", reason="awaiting_user_approval",
                           call_id=result.call_id, approval_id=approval_id,
                           outcome_reason=envelope,
                           human_message=result.human_message)

    if result.outcome is OUT.RECOVERY_REQUIRED:
        # Wird gebucht und gemeldet — NIE automatisch wiederholt. Ob die
        # Aussenwirkung eingetreten ist, weiss nur die Idempotenzmechanik der
        # Zahlungs-/Freigabeschicht, und die ist die einzige Wahrheit darueber.
        return StepOutcome(state="unknown", reason=result.reason,
                           call_id=result.call_id, outcome_reason=envelope,
                           failure_category="recovery_required",
                           human_message=result.human_message)

    category = {
        OUT.REJECTED_BY_POLICY: "policy_denied",
        OUT.INVALID_INPUT: "capability_failed",
        OUT.EXECUTOR_UNAVAILABLE: "capability_failed",
        OUT.CAPABILITY_FAILED: "capability_failed",
        OUT.TIMEOUT: "timeout",
        OUT.CANCELLED: "cancelled_by_user",
    }.get(result.outcome, "capability_failed")

    return StepOutcome(state="failed", reason=result.reason, call_id=result.call_id,
                       outcome_reason=envelope, failure_category=category,
                       human_message=result.human_message)


#: Was mit einem Schritt geschehen darf, dessen Freigabe die Ausfuehrung schon
#: betreten hat. Geschlossen, und jedes Wort ist eine Handlung des Cores — kein
#: Zustand der Aussenwelt.
RECOVERY_SUCCEEDED = "succeeded"
RECOVERY_NO_EFFECT = "no_effect"
RECOVERY_RETRY_SAME_KEY = "retry_same_key"
RECOVERY_MANUAL = "manual"


async def classify_execution_recovery(control_plane, approval_id: str) -> str:
    """Was das AUSFUEHRUNGSJOURNAL ueber eine schon eingeloeste Freigabe sagt.

    Der Anlass ist eine gemessene Naht, kein Vorfall: `_poll_approval` fuehrte
    `EXPIRED`, `EXECUTING`, `CONSUMED` und `FAILED` gemeinsam in den Pfad
    „neu anfragen". Ein wirklich unbenutzter Verfall und eine Anfrage, die
    bereits in die Ausfuehrung gegangen ist, sind aber nicht dasselbe: die
    ersten drei Zustaende bedeuten, dass ein Versuch beansprucht wurde, und
    dann ist die Frage nicht „nochmal fragen?", sondern „ist draussen etwas
    passiert?".

    Diese Frage beantwortet SOLVIO nicht neu. Sie ist seit P1C/F4 beantwortet,
    an genau einer Stelle: `execution_attempts` haelt Status und die
    SERVERSEITIG bei der Beanspruchung deklarierte Semantik, und
    `execution.recovery_decision()` ist die eine Politik darueber. Hier wird
    gelesen und uebersetzt — es wird nichts entschieden, was dort nicht schon
    entschieden waere, und nichts geschrieben.

    Fail-closed in jede Richtung, in der etwas unklar ist:

    * kein Kontrollweg, keine Kerninstanz, kein Journalzugriff → `manual`;
    * ein Journal, das zu einer eingeloesten Freigabe NICHTS kennt → `manual`.
      Das ist der Widerspruch zwischen zwei Buechern, und ein Widerspruch ist
      kein Freibrief;
    * `RECONCILE_FIRST` → `manual`. Es gibt in V1A keinen Abgleicher, der die
      Gegenseite fragen koennte, und „koennte man klaeren" ist nicht „ist
      geklaert".

    Nur `RETRY_SAME_KEY` erlaubt eine Wiederholung, und auch die gewinnt keine
    Autoritaet: sie legt DIESELBE Anfragekennung erneut vor, aus der dieselbe
    Ausfuehrungs- und Idempotenzkennung folgt.
    """
    from solvio.security.mobile_approval import execution as X

    core_instance = str(getattr(control_plane, "core_instance_id", "") or "")
    store = getattr(control_plane, "store", None)
    reader = getattr(store, "attempts_for", None)
    if not core_instance or not callable(reader) or not approval_id:
        log.warning("agent_runtime.execution_journal_unreadable",
                    reason="no_journal")
        return RECOVERY_MANUAL
    try:
        execution_id = X.execution_id_for(core_instance, approval_id)
        rows = list(await reader(execution_id) or [])
    except Exception as exc:  # noqa: BLE001
        log.warning("agent_runtime.execution_journal_unreadable",
                    kind=type(exc).__name__)
        return RECOVERY_MANUAL
    if not rows:
        # Die Freigabezeile sagt „in Ausfuehrung gegangen", das Journal kennt
        # keinen Versuch. Zwei Buecher, die sich widersprechen — und die
        # sichere Lesart ist die unangenehme.
        log.warning("agent_runtime.execution_journal_empty")
        return RECOVERY_MANUAL

    def _stamp(row):
        try:
            return float(row.get("claimed_at") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    latest = sorted(rows, key=_stamp)[-1]
    status = str(latest.get("status") or "")
    # Die Semantik kommt aus der ZEILE — sie wurde bei der Beanspruchung
    # serverseitig deklariert. Sie hier neu zu bestimmen hiesse, eine heutige
    # Einstufung auf einen gestrigen Versuch anzuwenden.
    semantics = str(latest.get("semantics") or X.NON_IDEMPOTENT_WRITE)
    decision = X.recovery_decision(status, semantics)
    verdict = {
        X.NO_ACTION_SUCCEEDED: RECOVERY_SUCCEEDED,
        X.CLOSED_NO_EFFECT: RECOVERY_NO_EFFECT,
        X.RETRY_SAME_KEY: RECOVERY_RETRY_SAME_KEY,
    }.get(decision, RECOVERY_MANUAL)
    log.info("agent_runtime.execution_recovery", status=status,
             semantics=semantics, decision=decision, verdict=verdict)
    return verdict


async def read_approval_state(control_plane, approval_id: str, *,
                              now: float = 0.0) -> str:
    """Der Zustand einer Anfrage, ueber die Lesefunktion des Kontrollwegs.

    Ausdruecklich NICHT ueber `router.execute`: dessen `not_approved` kann alles
    heissen. Faellt der Kontrollweg aus, ist die ehrliche Antwort `PENDING` —
    weiter parken —, nicht `DENIED`: ein Leseproblem ist keine Ablehnung.
    """
    if control_plane is None or not approval_id:
        return EXPIRED
    try:
        row = await control_plane.store.get_request(approval_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("agent_runtime.approval_read_failed",
                    kind=type(exc).__name__)
        return PENDING
    return classify_approval(row, now=now)
