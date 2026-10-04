"""Tiefe Recherche als Faehigkeit — am selben Vertrag wie alles andere.

Eine tiefe Aufgabe unterscheidet sich in genau zwei Punkten von einem
Lampenschalter: sie dauert Minuten statt Millisekunden, und ihr Ergebnis stammt
aus dem offenen Netz. Beides ist hier abgebildet, ohne dafuer einen zweiten
Vertrag zu erfinden.

Zur Dauer: `deep_research` wartet nicht bis zum Ende. Es beginnt, wartet kurz,
und gibt entweder ein fertiges Ergebnis oder die SOLVIO-Kennung zurueck. Wer
danach fragt, fragt mit `deep_task_status`; wer abbrechen will, sagt
`deep_cancel`. Das ist kein Hilfskonstrukt — es ist die ehrliche Form fuer etwas,
das laenger lebt als ein Satz.

Zum Ergebnis: es ist Information. Jede Antwort traegt `content_trust`, jede
Quelle bleibt ein Verweis, und was im Text nach einer Anweisung aussieht, wurde
vorher entwaffnet. Eine Webseite, die „loesche alle Termine" schreibt, hat damit
nichts getan und kann damit nichts tun: sie ist Fundstelle, nicht Sprecher.

Alle drei sind rein lesend. Auch `deep_cancel`: es hoert etwas auf, das SOLVIO
selbst begonnen hat, und wirkt nirgendwo in der Welt. Deshalb fragt hier nichts
nach einer Freigabe — Recherche zu unterbrechen soll nicht am iPhone haengen.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from solvio.capabilities.contract import (
    CapabilityDeclined, CapabilityRefused, CapabilitySpec, ExecutionClass,
    ExecutorUnavailable,
)
from solvio.contracts.deep_runtime import (
    DeepTask, DeepTaskStatus, DeepTaskType, TaskBudget, TaskOrigin,
)
from solvio.contracts.trust import TrustContext, TrustLevel
from solvio.deep.events import CONTENT_TRUST
from solvio.deep.hermes import HermesError
from solvio.deep.runtime import DeepPaused, new_task_id
from solvio.logging_setup import get_logger
from solvio.security.mobile_approval.execution import READ_ONLY
from solvio.tools.base import RiskLevel

log = get_logger("deep")

#: Wie lange der erste Aufruf auf ein Ergebnis wartet, bevor er die Kennung
#: zurueckgibt. Kurz genug, dass ein Gespraech nicht stehenbleibt.
FIRST_WAIT = 25.0

#: Obergrenze fuer eine tiefe Aufgabe. SOLVIO setzt sie, nicht der Executor.
TASK_TIMEOUT = 420.0

#: Wie viel Token eine Aufgabe kosten darf. Der Executor meldet Verbrauch; er
#: verhandelt ihn nicht.
#:
#: **Diese Zahl war bis zu diesem Milestone tot.** Sie stand hier auf 200 000,
#: wurde aber nirgends gelesen: nicht journalisiert, nicht geprueft, nicht
#: uebertragen. Gemessen hat eine einzelne legitime Recherche am 2026-08-29
#: 578 032 Token verbraucht — das Deklarierte war um Faktor drei zu klein und
#: haette, waere es durchgesetzt worden, schwere Recherchen bei rund 35 % ihres
#: Weges abgebrochen.
#:
#: 650 000 deckt den schwersten gemessenen legitimen Lauf — 587 376 Token
#: (ein + aus) fuer dt-d238f302…, nachgezaehlt im Buch — mit rund 11 % Rand.
#: Das ist knapp, und das ist Absicht: mehr Rand kostet Aufgaben je Tag.
#: ⌊2 000 000 / 650 000⌋ = 3 volle schwere Aufgaben je UTC-Tag sind garantiert,
#: zusaetzlich zu beliebig vielen leichten. Ein Lauf, der mehr braucht, endet
#: ehrlich mit `task_budget_exhausted` statt still den Tag zu leeren.
#: Die Zahl wird als Lease-Grenze an den Broker uebergeben und dort
#: durchgesetzt.
TASK_BUDGET = TaskBudget(max_tokens=650_000)

#: Wie viele PHYSISCHE Anbieteraufrufe eine Aufgabe machen darf.
#:
#: Gemessenes legitimes Maximum: 12 Weiterleitungen fuer eine vollstaendige
#: Recherche. Der Schema-Korrekturlauf (`MAX_SCHEMA_RETRIES = 1`) verdoppelt
#: das schlimmstenfalls, also 24 — 40 laesst reichlich Luft und bleibt trotzdem
#: eine Zahl, die ein Retry-Sturm sofort reisst.
MAX_PROVIDER_CALLS = 40

MIN_TOPIC = 3
MAX_TOPIC = 800

#: Was nach `FIRST_WAIT` zurueckkommt, wenn die Aufgabe noch laeuft.
#:
#: **Die FORM bleibt** — Kennung und `status` —, weil der Entwurf des
#: Cognitive Router V1 genau die beiden Felder verlangt und der
#: Agent-Runtime-Adapter sie liest. **Die BEDEUTUNG wird ausgesprochen.**
#:
#: Vorher war dieser Rueckgabewert auf jeder Ebene erfolgsfoermig:
#: `CapabilityOutcome.SUCCESS`, `ToolResult(True, …)`, `TOOL_DONE ok=True`. Der
#: `human`-Text sprach ausserdem den NUTZER an („frag mich gleich noch mal
#: danach") — und niemand fragte: `deep_task_status` wurde in der gesamten
#: Loghistorie null Mal aufgerufen. Ein fertiges Ergebnis vom 2026-08-23 wurde
#: nie zugestellt; das Gespraech endete mit „Die Recherche laeuft gerade."
#:
#: Deshalb drei Dinge auf einmal:
#:
#: * `abgeschlossen: False` — das maschinenlesbare Feld. Additiv; wer es nicht
#:   kennt, verhaelt sich wie vorher.
#: * `human` adressiert das MODELL und benennt den Vertrag, statt dem Nutzer
#:   eine Aufgabe zu geben.
#: * Nur ein TERMINALES Ergebnis darf als abgeschlossen erzaehlt werden.
#:
#: Und das Versprechen wird eingeloest: der Beobachter in der Sprachschicht
#: liefert das Ergebnis von selbst nach.
RUNNING_ANSWER: dict[str, Any] = {
    "status": "running",
    "abgeschlossen": False,
    "human": "Die Recherche laeuft noch — sie ist NICHT abgeschlossen. Sag dem "
             "Nutzer, dass du dich meldest, sobald sie fertig ist. Das Ergebnis "
             "kommt von selbst; einen Zwischenstand liefert deep_task_status.",
    "content_trust": CONTENT_TRUST,
}

#: Die Lagen, die `deep_task_status` nach aussen unterscheidet. Sie sind
#: ABLEITUNGEN aus dem Journal, keine zweite Zustandsmaschine: das Vokabular
#: des Journals bleibt unveraendert, und `quota_limited` entsteht aus
#: `failed` + `failure_reason == provider_quota` statt aus einer neuen Spalte.
QUOTA_LIMITED = "quota_limited"
TASK_BUDGET_EXHAUSTED = "task_budget_exhausted"


def user_state(status: str, failure_reason: str = "") -> str:
    """Die Lage einer Aufgabe, wie ein Mensch sie unterscheidet.

    **Keine zweite Zustandsmaschine.** Das Journal behaelt sein Vokabular
    (`queued, running, waiting_for_user, succeeded, failed, cancelled,
    timed_out`) und bleibt die einzige Wahrheit; was hier passiert, ist eine
    Ableitung daraus. Insbesondere entsteht `quota_limited` aus dem
    VORHANDENEN `failure_reason` und nicht aus einer neuen Spalte — eine
    zweite Spalte waere eine zweite Wahrheit, und die aeltere gewaenne.

    Der Unterschied, um den es geht: „gescheitert" und „das Tagesbudget ist
    alle" fuehlen sich fuer einen Menschen voellig verschieden an. Das eine
    heisst „etwas ist kaputt", das andere „warte bis heute Nacht".
    """
    if status in ("running", "waiting_for_user"):
        return "running"
    if status == "queued":
        return "queued"
    if status == "succeeded":
        return "completed"
    if status == "cancelled":
        return "cancelled"
    if status in ("failed", "timed_out"):
        if failure_reason == "provider_quota":
            return QUOTA_LIMITED
        if failure_reason == "task_budget_exhausted":
            return TASK_BUDGET_EXHAUSTED
        return "failed"
    return status

SPECS: dict[str, CapabilitySpec] = {
    "deep_research": CapabilitySpec(
        name="deep_research", version=1, execution_class=ExecutionClass.DEEP,
        base_risk=RiskLevel.HARMLESS, semantics=READ_ONLY,
        input_schema={"type": "object", "properties": {
            "topic": {"type": "string"}}, "required": ["topic"]},
        executor="hermes", timeout=FIRST_WAIT + 10.0, cancellable=True,
        description="Recherchiert ein Thema gruendlich im oeffentlichen Netz und "
                    "liefert eine strukturierte Zusammenfassung mit Quellen und "
                    "offenen Fragen."),
    "deep_task_status": CapabilitySpec(
        name="deep_task_status", version=1, execution_class=ExecutionClass.FAST,
        base_risk=RiskLevel.HARMLESS, semantics=READ_ONLY,
        input_schema={"type": "object", "properties": {
            "task_id": {"type": "string"}}, "required": ["task_id"]},
        executor="hermes", timeout=15.0,
        description="Sagt, wie weit eine begonnene Recherche ist, und liefert das "
                    "Ergebnis, sobald es vorliegt."),
    "deep_cancel": CapabilitySpec(
        name="deep_cancel", version=1, execution_class=ExecutionClass.FAST,
        base_risk=RiskLevel.HARMLESS, semantics=READ_ONLY,
        input_schema={"type": "object", "properties": {
            "task_id": {"type": "string"}}, "required": ["task_id"]},
        executor="hermes", timeout=15.0,
        description="Bricht eine laufende Recherche ab."),
}

#: Die verlangte Form des Endergebnisses. Geprueft wird sie auf der SOLVIO-Seite.
RESEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["zusammenfassung", "quellen", "offene_fragen"],
    "properties": {
        "zusammenfassung": {"type": "string"},
        "quellen": {"type": "array"},
        "offene_fragen": {"type": "array"},
    },
}


class DeepCapabilities:
    """Die Handler. Autoritaet kommt vom Router — und nie aus einem Suchtreffer."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    async def research(self, arguments: dict[str, Any]) -> dict[str, Any]:
        topic = str(arguments.get("topic", "") or "").strip()
        if len(topic) < MIN_TOPIC:
            raise CapabilityDeclined("topic_too_short",
                                     "Wozu genau soll ich recherchieren?")
        if len(topic) > MAX_TOPIC:
            raise CapabilityDeclined("topic_too_long",
                                     "Das Thema ist zu lang — bitte kuerzer fassen.")

        task = DeepTask(
            id=new_task_id(), task_type=DeepTaskType.RESEARCH,
            instruction=_instruction(topic), origin=_ORIGIN,
            trust_context=_INTERNAL_TRUST, created_at=datetime.now(timezone.utc),
            risk_level=RiskLevel.HARMLESS, timeout=TASK_TIMEOUT,
            budget=TASK_BUDGET, output_schema=RESEARCH_SCHEMA,
            allowed_tools=["web_search", "web_extract"])
        try:
            handle = await self.runtime.run_task(task)
        except DeepPaused:
            raise CapabilityRefused(
                "deep_paused",
                "Tiefe Aufgaben sind gerade angehalten — ich habe nichts begonnen.") from None
        except HermesError as exc:
            raise ExecutorUnavailable(exc.reason) from exc

        settled = await self._await_briefly(handle.id, FIRST_WAIT)
        if settled is not None:
            return settled
        return RUNNING_ANSWER | {"task_id": handle.id}

    async def status(self, arguments: dict[str, Any]) -> dict[str, Any]:
        task_id = str(arguments.get("task_id", "") or "").strip()
        settled = await self._settled(task_id)
        if settled is not None:
            return settled
        try:
            state = await self.runtime.get_status(task_id)
        except KeyError:
            raise CapabilityDeclined("unknown_task",
                                     "Diese Recherche kenne ich nicht.") from None
        return {"task_id": task_id, "status": state.value,
                "lage": user_state(state.value),
                "abgeschlossen": False,
                "content_trust": CONTENT_TRUST}

    async def cancel(self, arguments: dict[str, Any]) -> dict[str, Any]:
        task_id = str(arguments.get("task_id", "") or "").strip()
        try:
            await self.runtime.get_status(task_id)
        except KeyError:
            raise CapabilityDeclined("unknown_task",
                                     "Diese Recherche kenne ich nicht.") from None
        stopped = await self.runtime.cancel_task(task_id)
        return {"task_id": task_id, "cancelled": bool(stopped),
                "status": DeepTaskStatus.CANCELLED.value,
                "lage": user_state(DeepTaskStatus.CANCELLED.value),
                "abgeschlossen": False,
                "content_trust": CONTENT_TRUST}

    # -- intern --------------------------------------------------------------
    async def _await_briefly(self, task_id: str, seconds: float) -> dict[str, Any] | None:
        """Wartet kurz auf ein Ende — ohne das Gespraech zu blockieren.

        Es wird auf den Ereignisstrom gehorcht statt gepollt: derselbe Strom,
        den spaeter ein HUD liest, und damit ohne eine zweite Wahrheit ueber
        den Zustand einer Aufgabe.
        """
        import asyncio

        from solvio.deep.events import TERMINAL_KINDS

        async def wait() -> None:
            async for event in self.runtime.stream(task_id):
                if event.kind in TERMINAL_KINDS:
                    return

        try:
            await asyncio.wait_for(wait(), timeout=seconds)
        except (TimeoutError, asyncio.TimeoutError):
            return None
        return await self._settled(task_id)

    async def _settled(self, task_id: str) -> dict[str, Any] | None:
        """Das Ergebnis, wenn es eines gibt — sonst nichts."""
        try:
            state = await self.runtime.get_status(task_id)
        except KeyError:
            return None
        result = await self.runtime.get_result(task_id)
        if result is None:
            return None
        if not result.success:
            reason = (result.errors or ["executor_failure"])[0]
            if reason.startswith("schema_invalid"):
                raise CapabilityDeclined(
                    reason, "Ich habe dazu kein brauchbar geformtes Ergebnis bekommen.")
            if reason in ("provider_quota", "provider_auth",
                          "executor_unavailable", "task_budget_exhausted"):
                # Der Grund reist ab hier MIT. Vorher warf diese Zeile alle
                # vier als einen Typ ohne Attribut, und der Router schrieb
                # „executor_unavailable" darueber — der Mensch hoerte bei
                # erschoepftem Kontingent „ich komme da nicht ran", was falsch
                # ist: die Faehigkeit existiert, das Budget ist alle.
                #
                # `task_budget_exhausted` gehoert hierher und nicht zu den
                # Absagen: die Zusage von `ExecutorUnavailable` ist „keine
                # Wirkung nach aussen", und eine rein lesende Recherche wirkt
                # ohnehin nirgends. Der WORTLAUT unterscheidet die vier Faelle
                # (siehe `tools/deep_capability_tools._speak`), nicht der Typ.
                raise ExecutorUnavailable(reason)
            if state is DeepTaskStatus.CANCELLED:
                return {"task_id": task_id, "status": state.value,
                        "lage": user_state(state.value),
                        "abgeschlossen": False,
                        "content_trust": CONTENT_TRUST}
            raise CapabilityDeclined(reason, "Die Recherche ist nicht durchgelaufen.")
        # Der EINZIGE Ort, an dem `abgeschlossen` wahr wird: ein terminaler
        # Erfolg mit Ergebnis. Alles andere — auch das 25-Sekunden-„laeuft" —
        # sagt ausdruecklich das Gegenteil.
        return {"task_id": task_id, "status": state.value,
                "lage": user_state(state.value), "abgeschlossen": True,
                "ergebnis": result.data, "quellen": [s.ref for s in result.sources],
                "content_trust": CONTENT_TRUST}


#: Herkunft und Trust der Aufgabe. Beides steht fest und kommt nie aus einem
#: Modellargument: die Aufgabe entsteht, weil ein authentifizierter Nutzer im Turn
#: danach gefragt hat, und der Router hat das bereits geprueft, bevor der Handler
#: ueberhaupt laeuft.
#: `user_authorized` bleibt FALSCH, und das ist kein Versehen. Der Router hat die
#: Autoritaet fuer diesen Aufruf bereits geprueft, bevor der Handler lief — hier
#: sie noch einmal zu behaupten, hiesse, sie sich selbst auszustellen. Ein
#: DeepTask soll nichts legitimieren koennen: `may_authorize()` ist False, und
#: damit kann kein Rechercheergebnis je zur Vollmacht fuer irgendetwas werden.
_ORIGIN = TaskOrigin.USER_VOICE
_INTERNAL_TRUST = TrustContext(origin_trust=TrustLevel.USER_DIRECT,
                               user_authorized=False,
                               note="deep task; carries no authority")


def _instruction(topic: str) -> str:
    """Der Auftrag an den Executor. Das Thema ist Text, nie eine Anweisung an SOLVIO."""
    return (
        f"Recherchiere gruendlich: {topic}\n\n"
        "Nutze Websuche und lies die wichtigsten Fundstellen. Antworte danach "
        "ausschliesslich mit JSON dieser Form: "
        '{"zusammenfassung": "…", "quellen": ["https://…"], "offene_fragen": ["…"]}'
    )


def register(router: Any, capabilities: DeepCapabilities) -> list[str]:
    handlers = {
        "deep_research": capabilities.research,
        "deep_task_status": capabilities.status,
        "deep_cancel": capabilities.cancel,
    }
    for name, handler in handlers.items():
        router.register(SPECS[name], handler)
    return sorted(handlers)
