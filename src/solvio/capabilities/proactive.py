"""Aufgaben anlegen und Meldungen lesen — als Faehigkeiten wie alle anderen.

Zwei Entscheidungen sind hier wichtiger, als sie aussehen.

**Erstens: kein Cron-Ausdruck.** Ein Feld, in das das Modell `* * * * *`
schreiben kann, ist eine Einladung. Stattdessen gibt es getippte Formen —
„in N Minuten", „taeglich um 7:30", „montags", „alle N Minuten". Was sich damit
nicht ausdruecken laesst, gibt es in V1 nicht, und das ist billiger als ein
Planer, den niemand mehr versteht.

**Zweitens: das Anlegen ist selbst freigabepflichtig-frei, die Ausfuehrung nicht.**
Eine Aufgabe anzulegen veraendert nichts an der Welt — sie schreibt eine Zeile in
SOLVIOs eigene Datenbank. Was sie spaeter TUT, wird zur Laufzeit erneut bewertet.
Deshalb ist `background_create` selbst `IDEMPOTENT_WRITE` auf eigenem Zustand und
nicht der Schluessel zu allem, was danach kommt.
"""
from __future__ import annotations

import contextlib
import hashlib
import time
from typing import Any

from solvio.capabilities.contract import CapabilitySpec, ExecutionClass, RiskLevel
from solvio.capabilities.contract import CapabilityDeclined
from solvio.logging_setup import get_logger
from solvio.proactive import schedule as SCH
from solvio.proactive import store as S
from solvio.proactive.everyday import ACTIONS as EVERYDAY_ACTIONS, validate as validate_everyday
from solvio.capabilities import preauth as PA
from solvio.security.mobile_approval.execution import IDEMPOTENT_WRITE, READ_ONLY

log = get_logger("proactive")

#: Welche Faehigkeiten eine Hintergrundaufgabe ueberhaupt aufrufen darf.
#:
#: Eine geschlossene Liste, und zwar Core-eigen: das Modell waehlt daraus, statt
#: sie zu erweitern. Schreibende Faehigkeiten stehen ausdruecklich NICHT darin —
#: nicht weil sie unmoeglich waeren (der Freigabeweg traegt sie), sondern weil
#: V1 keine dauerhafte Vollmacht erfindet. Wer eine geplante E-Mail will, soll
#: das als eigenen Meilenstein bekommen, mit eigener Abnahme.
ALLOWED_ACTIONS = (
    "calendar_list_events", "calendar_search_events", "calendar_get_event",
    "calendar_find_availability",
    "gmail_list_recent", "gmail_search", "gmail_read_message",
    "ha_get_state", "ha_list_devices",
    # Seit Approval Policy V2 (ADR-0022): gewoehnliche Haustechnik im
    # Hintergrund. Ausdruecklich KEINE allgemeine Vollmacht — jede dieser drei
    # ist nur dann ohne Face ID ausfuehrbar, wenn eine an ihre WIRKUNG gebundene
    # Erlaubnis vorliegt (Faehigkeit, aufgeloestes Geraet, gemessene Klasse,
    # Argumente, Zeitplan). Ohne sie kostet auch hier jeder Lauf eine Freigabe.
    "ha_turn_on", "ha_turn_off", "ha_set_brightness",
)

#: Aus welchen Herkuenften ueberhaupt eine Erlaubnis entstehen darf.
#:
#: Das Telefon und der Raum duerfen es beide — gewoehnliche Haustechnik soll
#: gerade vom Raummikrofon aus bequem bleiben, das ist die Entscheidung des
#: Nutzers. Der Hintergrund darf es NICHT: er kann sich sonst selbst
#: Vollmachten ausstellen. Fremder Inhalt scheitert schon vorher an der
#: Autoritaetspruefung; er steht hier nur nicht drin, damit man es sieht.
MAY_GRANT_PREAUTH: frozenset[str] = frozenset({
    "trusted_interactive_app", "room_voice", "local_owner",
})

#: Welche der zugelassenen Aktionen ueberhaupt eine Vorab-Autorisierung tragen
#: koennen. Die Klasse wird trotzdem am echten Geraet gemessen — diese Liste
#: sagt nur, wo ueberhaupt gefragt wird.
PREAUTHORIZABLE = ("ha_turn_on", "ha_turn_off", "ha_set_brightness")

MAX_TITLE = 120
MAX_TOPIC = 780

SPECS: dict[str, CapabilitySpec] = {
    "background_create": CapabilitySpec(
        name="background_create", version=1,
        execution_class=ExecutionClass.CONTROLLED, base_risk=RiskLevel.HARMLESS,
        semantics=IDEMPOTENT_WRITE, timeout=20.0,
        description="Legt eine wiederkehrende oder einmalige Hintergrundaufgabe an.",
        input_schema={"type": "object", "properties": {
            "titel": {"type": "string"},
            "wann": {"type": "string",
                     "description": "in 5 minuten | taeglich 07:30 | "
                                    "montags 08:00 | alle 15 minuten"},
            "aktion": {"type": "string",
                       "description": "Name einer erlaubten Lese-Faehigkeit "
                                      "oder 'recherche'"},
            "argumente": {"type": "object"},
            "thema": {"type": "string", "description": "nur bei 'recherche'"},
            "bedingung": {"type": "string",
                          "description": "nur melden, wenn das vorkommt"},
            "melden": {"type": "string",
                       "description": "immer | bei_aenderung | wenn_zutrifft"},
        }, "required": ["titel", "wann", "aktion"]}),
    "background_list": CapabilitySpec(
        name="background_list", version=1, execution_class=ExecutionClass.FAST,
        base_risk=RiskLevel.HARMLESS, semantics=READ_ONLY, timeout=15.0,
        description="Nennt die angelegten Hintergrundaufgaben.",
        input_schema={"type": "object", "properties": {}}),
    "background_get": CapabilitySpec(
        name="background_get", version=1, execution_class=ExecutionClass.FAST,
        base_risk=RiskLevel.HARMLESS, semantics=READ_ONLY, timeout=15.0,
        description="Zeigt eine Hintergrundaufgabe mit ihren letzten Laeufen.",
        input_schema={"type": "object", "properties": {"id": {"type": "string"}},
                      "required": ["id"]}),
    "background_pause": CapabilitySpec(
        name="background_pause", version=1,
        execution_class=ExecutionClass.CONTROLLED, base_risk=RiskLevel.HARMLESS,
        semantics=IDEMPOTENT_WRITE, timeout=15.0,
        description="Pausiert eine Hintergrundaufgabe.",
        input_schema={"type": "object", "properties": {"id": {"type": "string"}},
                      "required": ["id"]}),
    "background_resume": CapabilitySpec(
        name="background_resume", version=1,
        execution_class=ExecutionClass.CONTROLLED, base_risk=RiskLevel.HARMLESS,
        semantics=IDEMPOTENT_WRITE, timeout=15.0,
        description="Nimmt eine pausierte Hintergrundaufgabe wieder auf.",
        input_schema={"type": "object", "properties": {"id": {"type": "string"}},
                      "required": ["id"]}),
    "background_delete": CapabilitySpec(
        name="background_delete", version=1,
        execution_class=ExecutionClass.CONTROLLED, base_risk=RiskLevel.HARMLESS,
        semantics=IDEMPOTENT_WRITE, timeout=15.0,
        description="Loescht eine Hintergrundaufgabe.",
        input_schema={"type": "object", "properties": {"id": {"type": "string"}},
                      "required": ["id"]}),
    "background_require_approval": CapabilitySpec(
        name="background_require_approval", version=1,
        execution_class=ExecutionClass.CONTROLLED, base_risk=RiskLevel.HARMLESS,
        semantics=IDEMPOTENT_WRITE, timeout=15.0,
        description=("Nimmt einer Automatisierung die Vorab-Freigabe. Sie laeuft "
                     "weiter, fragt aber wieder jedes Mal nach."),
        input_schema={"type": "object", "properties": {"id": {"type": "string"}},
                      "required": ["id"]}),
    "background_run_now": CapabilitySpec(
        name="background_run_now", version=1,
        execution_class=ExecutionClass.CONTROLLED, base_risk=RiskLevel.HARMLESS,
        semantics=IDEMPOTENT_WRITE, timeout=20.0,
        description="Zieht die naechste Faelligkeit einer Aufgabe auf jetzt vor.",
        input_schema={"type": "object", "properties": {"id": {"type": "string"}},
                      "required": ["id"]}),
    "proactive_list": CapabilitySpec(
        name="proactive_list", version=1, execution_class=ExecutionClass.FAST,
        base_risk=RiskLevel.HARMLESS, semantics=READ_ONLY, timeout=15.0,
        description="Nennt die ungelesenen Meldungen aus dem Hintergrund.",
        input_schema={"type": "object", "properties": {
            "anzahl": {"type": "integer"}}}),
    "proactive_get": CapabilitySpec(
        name="proactive_get", version=1, execution_class=ExecutionClass.FAST,
        base_risk=RiskLevel.HARMLESS, semantics=READ_ONLY, timeout=15.0,
        description="Zeigt eine einzelne Meldung vollstaendig.",
        input_schema={"type": "object", "properties": {"id": {"type": "string"}},
                      "required": ["id"]}),
    "proactive_mark_read": CapabilitySpec(
        name="proactive_mark_read", version=1,
        execution_class=ExecutionClass.CONTROLLED, base_risk=RiskLevel.HARMLESS,
        semantics=IDEMPOTENT_WRITE, timeout=15.0,
        description="Markiert eine Meldung als gelesen.",
        input_schema={"type": "object", "properties": {"id": {"type": "string"}},
                      "required": ["id"]}),
}


class ProactiveCapabilities:
    """Die Handhabung. Kennt den Speicher, nicht den Zeitplan im Detail."""

    def __init__(self, store: S.ProactiveStore, *, scheduler: Any = None,
                 gate: Any = None, clock=None, preauth: Any = None,
                 router: Any = None) -> None:
        self.store = store
        self.scheduler = scheduler
        #: Das Aufruf-Gate. Von hier kommen Auftraggeber und Wortlaut des
        #: Auftrags — nicht aus den Argumenten.
        #:
        #: Der erste Entwurf hat beides als `_user_text` und `_principal` durch
        #: die Argumente geschmuggelt. Das scheiterte am Schema-Pruefer des
        #: Routers (`unknown_argument:_user_text`), und zwar zu Recht: Argumente
        #: sind das, was das Modell waehlt. Wer dort Herkunft hineinschreibt,
        #: laesst das Modell seine eigene Herkunft behaupten.
        self.gate = gate
        self.clock = clock or time.time
        #: Praegt und widerruft gebundene Erlaubnisse. Nicht verdrahtet heisst:
        #: es gibt keine, und jeder Hintergrundlauf kostet eine Freigabe.
        self.preauth = preauth
        #: Der Router — gebraucht, um die Klasse einer geplanten Aktion am
        #: echten Geraet zu messen, BEVOR eine Erlaubnis entsteht.
        self.router = router

    def _caller(self) -> tuple[str, str]:
        """Wer fragt, und mit welchen Worten. Aus dem vertrauenswuerdigen Kontext."""
        context = self.gate.context() if self.gate is not None else None
        if context is None:
            return "gregor", ""
        return (getattr(context, "principal", "") or "gregor",
                str(getattr(context, "user_text", ""))[:500])

    # -- Anlegen -------------------------------------------------------------

    async def create(self, arguments: dict[str, Any]) -> dict[str, Any]:
        title = str(arguments.get("titel", "")).strip()[:MAX_TITLE]
        when = str(arguments.get("wann", "")).strip()
        action_name = str(arguments.get("aktion", "")).strip()
        if not title:
            raise CapabilityDeclined("title_missing", "Wie soll die Aufgabe heissen?")

        plan, note = parse_when(when, now=self.clock())
        if plan is None:
            raise CapabilityDeclined(
                "schedule_not_understood",
                "Sag mir das anders — zum Beispiel 'in 20 Minuten', "
                "'taeglich um 7:30' oder 'alle 15 Minuten'.")

        if action_name in EVERYDAY_ACTIONS:
            action = validate_everyday(action_name, arguments, plan)
        elif action_name in ("recherche", "research", "deep_research"):
            topic = str(arguments.get("thema", "")).strip()[:MAX_TOPIC]
            if len(topic) < 8:
                raise CapabilityDeclined("topic_missing",
                                         "Wozu soll ich recherchieren?")
            action: dict[str, Any] = {"kind": "research", "topic": topic}
        elif action_name in ALLOWED_ACTIONS:
            action = {"kind": "capability", "capability": action_name,
                      "arguments": dict(arguments.get("argumente") or {})}
        else:
            # Ausdruecklich benannt statt vage: der Nutzer soll erfahren, was
            # geht, statt zu raten. Und eine schreibende Faehigkeit wird hier
            # nicht heimlich zugelassen.
            raise CapabilityDeclined(
                "action_not_allowed",
                f"Das kann ich im Hintergrund nicht. Moeglich sind: "
                f"{', '.join(ALLOWED_ACTIONS)} oder eine Recherche.",
                data={"erlaubt": list(ALLOWED_ACTIONS)})

        condition = str(arguments.get("bedingung", "")).strip()
        if condition:
            action["condition"] = {"enthaelt": condition[:120]}
        notify = str(arguments.get("melden", "")).strip()
        action["notify"] = notify if notify in ("immer", "bei_aenderung",
                                                "wenn_zutrifft") else (
            "wenn_zutrifft" if condition else "bei_aenderung")

        if action_name in EVERYDAY_ACTIONS:
            action["notify"] = "immer"
        owner, spoken = self._caller()
        if action_name in EVERYDAY_ACTIONS:
            from solvio.secret_vault.context import current
            context = current()
            if not context.principal or not context.approval_id:
                raise CapabilityDeclined('approval_required', 'Für diese regelmäßige oder spätere private Prüfung fehlt die Face-ID-Freigabe.')
            owner = context.principal
        now = self.clock()
        seed = f"{owner}|{title}|{now}"
        if action_name in EVERYDAY_ACTIONS:
            seed = f"{owner}|approval|{context.approval_id}"
        task_id = "bt-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
        retained = await self.store.get_task(task_id)
        if retained is not None:
            return {"id": retained.task_id, "titel": retained.title,
                    "zeitplan": retained.schedule, "naechster_lauf": retained.next_run_at,
                    "zustellung": ZUSTELLUNG}
        existing = await self.store.list_tasks(owner)
        if len(existing) >= 40:
            raise CapabilityDeclined("too_many_tasks",
                                     "Du hast schon sehr viele Aufgaben — "
                                     "loesch erst eine.")

        now = self.clock()
        task = S.Task(
            task_id=task_id,
            owner=owner, title=title, created_at=now,
            created_from=spoken or title,
            schedule=plan.as_dict(), action=action,
            next_run_at=plan.next_after(now))
        if action_name in EVERYDAY_ACTIONS:
            from solvio.capabilities.gmail import _require_mail_source
            await _require_mail_source()
        await self.store.put_task(task)
        granted = await self._grant_preauth(task, action)
        log.info("proactive.task_created", task=task.task_id,
                 kind=plan.kind.value, action=action.get("capability")
                 or action.get("kind"),
                 preauthorization=granted.preauth_id if granted else "")
        result = {"id": task.task_id, "titel": task.title,
                  "zeitplan": plan.as_dict(), "naechster_lauf": task.next_run_at,
                  "hinweis": note,
                  "zustellung": ZUSTELLUNG}
        if granted is not None:
            result["vorab_freigabe"] = granted.as_dict()
        return result

    async def _grant_preauth(self, task: S.Task, action: dict[str, Any]):
        """Praegt die gebundene Erlaubnis — oder eben keine.

        Keine ist kein Fehler: die Automatisierung laeuft trotzdem, sie fragt
        dann nur jedes Mal nach. Gepraegt wird ausschliesslich, wenn ALLES
        stimmt: eine der drei Haustechnik-Faehigkeiten, ein wiederkehrender
        Zeitplan, ein aufloesbares Geraet, und eine am Geraet GEMESSENE Klasse
        `ha_normal`. Ein Schloss faellt hier heraus, ohne dass es jemand
        gesondert verbieten muesste.
        """
        if self.preauth is None or self.router is None:
            return None
        capability = str(action.get("capability", ""))
        if capability not in PREAUTHORIZABLE:
            return None
        if str(task.schedule.get("art", "")) not in PA.RECURRING_KINDS:
            # Ein einmaliger Termin braucht keine dauerhafte Erlaubnis.
            return None
        arguments = dict(action.get("arguments") or {})
        try:
            spec = self.router.spec(capability)
            classification = await self.router._classify(spec, arguments)
        except Exception as exc:  # noqa: BLE001 - keine Klasse, keine Erlaubnis
            log.info("proactive.preauth_not_granted", task=task.task_id,
                     capability=capability, reason=type(exc).__name__)
            return None
        if classification.action_class is not PA.ELIGIBLE_CLASS:
            log.info("proactive.preauth_not_granted", task=task.task_id,
                     capability=capability,
                     reason=f"class:{classification.action_class.value}")
            return None
        origin = self._origin_label()
        if origin not in MAY_GRANT_PREAUTH:
            # Ein Hintergrundlauf kann sich keine Autoritaet fuer die naechste
            # Nacht ausstellen, und fremder Inhalt schon gar nicht. Eine
            # Erlaubnis entsteht nur aus einem lebenden, bewiesenen Nutzerakt.
            log.info("proactive.preauth_not_granted", task=task.task_id,
                     capability=capability, reason=f"origin:{origin or 'none'}")
            return None
        try:
            grant = await self.preauth.mint_for_task(
                task, capability=capability, action_class=classification.action_class,
                targets=classification.targets, arguments=arguments,
                created_origin=origin, created_principal=self._caller()[0])
        except PA.PreauthorizationError as exc:
            log.info("proactive.preauth_not_granted", task=task.task_id,
                     capability=capability, reason=exc.reason)
            return None
        log.info("proactive.preauth_granted", task=task.task_id,
                 preauthorization=grant.preauth_id, capability=capability,
                 targets=len(grant.targets), origin=origin)
        return grant

    def _origin_label(self) -> str:
        """Von wo aus die Erlaubnis entstanden ist — als Laufzeitfakt.

        Ein Hintergrundlauf kann sich hier keine Autoritaet selbst ausstellen:
        er hat keinen Turn-Kontext, und ohne Kontext gibt es keine Erlaubnis.
        """
        context = self.gate.context() if self.gate is not None else None
        if context is None:
            return ""
        return context.origin.value

    # -- Verwalten -----------------------------------------------------------

    async def list_tasks(self, arguments: dict[str, Any]) -> dict[str, Any]:
        tasks = await self.store.list_tasks()
        return {"aufgaben": [t.as_dict() for t in tasks], "anzahl": len(tasks)}

    async def get(self, arguments: dict[str, Any]) -> dict[str, Any]:
        task = await self._task(arguments)
        runs = await self.store.runs_for(task.task_id, limit=5)
        # Eine Vollmacht, die man nicht nachsehen kann, ist keine, die man
        # widerrufen kann. Deshalb steht sie hier — lesbar, nicht als Digest.
        grant = None
        if self.preauth is not None:
            with contextlib.suppress(Exception):
                stored = await self.store.get_preauth(task.task_id)
                grant = stored.as_dict() if stored is not None else None
        return {**task.as_dict(),
                "vorab_freigabe": grant,
                "letzte_laeufe": [{"gelegenheit": r["occurrence"],
                                   "zustand": r["state"],
                                   "detail": (r["detail"] or "")[:160]}
                                  for r in runs]}

    async def pause(self, arguments: dict[str, Any]) -> dict[str, Any]:
        task = await self._task(arguments)
        task.enabled = False
        task.state = S.PAUSED
        await self.store.put_task(task)
        log.info("proactive.task_paused", task=task.task_id)
        return {"id": task.task_id, "zustand": task.state}

    async def resume(self, arguments: dict[str, Any]) -> dict[str, Any]:
        task = await self._task(arguments)
        task.enabled = True
        task.state = S.ACTIVE
        task.consecutive_failures = 0
        task.retry_after = None
        plan = SCH.Schedule.from_dict(task.schedule)
        task.next_run_at = plan.next_after(self.clock())
        await self.store.put_task(task)
        log.info("proactive.task_resumed", task=task.task_id)
        return {"id": task.task_id, "zustand": task.state,
                "naechster_lauf": task.next_run_at}

    async def delete(self, arguments: dict[str, Any]) -> dict[str, Any]:
        task = await self._task(arguments)
        # Erst die Autoritaet, dann die Aufgabe. Die Fremdschluessel-Regel
        # raeumt die Zeile ohnehin mit weg; der ausdrueckliche Widerruf davor
        # steht da, weil eine Loeschung, die an ihrer eigenen Reihenfolge
        # haengt, keine Loeschung ist.
        if self.preauth is not None:
            with contextlib.suppress(Exception):
                await self.preauth.revoke(task.task_id, "automation_deleted")
        await self.store.delete_task(task.task_id)
        log.info("proactive.task_deleted", task=task.task_id)
        return {"id": task.task_id, "geloescht": True}

    async def require_approval(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Nimmt einer laufenden Automatisierung ihre Vorab-Freigabe.

        Der Unterschied zum Pausieren ist der, auf den es ankommt: die Aufgabe
        laeuft weiter, sie fragt nur wieder jedes Mal nach. „Mach das
        Aussenlicht weiter an, aber frag mich" ist ein Wunsch, den man haben
        darf, ohne die Automatisierung loeschen zu muessen.

        Endgueltig fuer DIESE Bindung. Eine neue entsteht nur aus einem neuen
        Nutzerakt, mit neuer Revision und neuem Digest.
        """
        task = await self._task(arguments)
        if self.preauth is None:
            return {"id": task.task_id, "vorab_freigabe": None, "widerrufen": False}
        revoked = await self.preauth.revoke(task.task_id, "user_request")
        log.info("proactive.preauth_revoked", task=task.task_id, wirksam=revoked)
        return {"id": task.task_id, "widerrufen": revoked,
                "hinweis": ("Ich frage bei dieser Aufgabe wieder jedes Mal nach."
                            if revoked else
                            "Da war keine Vorab-Freigabe, nach der ich fragen muesste.")}

    async def run_now(self, arguments: dict[str, Any]) -> dict[str, Any]:
        task = await self._task(arguments)
        task.next_run_at = self.clock()
        task.retry_after = None
        await self.store.put_task(task)
        return {"id": task.task_id, "faellig": True}

    # -- Posteingang ----------------------------------------------------------

    async def inbox(self, arguments: dict[str, Any]) -> dict[str, Any]:
        limit = max(1, min(int(arguments.get("anzahl", 5) or 5), 20))
        items = await self.store.unread(limit)
        total = await self.store.unread_count()
        return {"meldungen": items, "ungelesen": total,
                "zustellung": ZUSTELLUNG}

    async def item(self, arguments: dict[str, Any]) -> dict[str, Any]:
        found = await self.store.get_item(str(arguments.get("id", "")))
        if found is None:
            raise CapabilityDeclined("unknown_item", "Die Meldung kenne ich nicht.")
        return found

    async def mark_read(self, arguments: dict[str, Any]) -> dict[str, Any]:
        marked = await self.store.mark_read(str(arguments.get("id", "")))
        return {"id": arguments.get("id"), "gelesen": marked}

    async def _task(self, arguments: dict[str, Any]) -> S.Task:
        task = await self.store.get_task(str(arguments.get("id", "")))
        if task is None:
            raise CapabilityDeclined("unknown_task", "Die Aufgabe kenne ich nicht.")
        return task


#: Was SOLVIO ueber die Zustellung sagen darf — und was nicht. Es gibt keinen
#: Push aufs geschlossene iPhone; das zu behaupten waere die eine Luege, die
#: dieses ganze Merkmal wertlos machen wuerde.
ZUSTELLUNG = ("Meldungen findest du unter Hinweise. Mitteilungen auf dem "
              "geschlossenen iPhone benötigen einen eingerichteten Apple-Push-Zugang.")

#: Was beim ANLEGEN gilt. Beobachtet, nicht ausgedacht: der Faehigkeitsname
#: kommt im gesprochenen Satz nie vor, gilt damit als modellgewaehlt und hebt
#: den Aufruf um eine Stufe. Eine dauerhafte Automatik einzurichten kostet
#: deshalb in der Regel eine Freigabe — und das ist die richtige Richtung.
EINRICHTUNG = ("Eine dauerhafte Aufgabe einzurichten braucht meist deine "
               "Freigabe auf dem iPhone. Danach laeuft sie von selbst.")


#: Ausgeschriebene Zahlwoerter. Ein Mensch sagt „in zwei Minuten", nicht „in 2
#: Minuten" — und ein Sprachmodell gibt genau das weiter, was gesprochen wurde.
#: Der erste Entwurf kannte nur Ziffern und hat „in zwei minuten" abgelehnt.
#: Aufgefallen ist es erst im Live-Lauf, NACH einer erteilten Freigabe.
_ZAHLWORT = {
    "eine": 1, "einer": 1, "ein": 1, "zwei": 2, "drei": 3, "vier": 4,
    "fuenf": 5, "fünf": 5, "sechs": 6, "sieben": 7, "acht": 8, "neun": 9,
    "zehn": 10, "elf": 11, "zwoelf": 12, "zwölf": 12, "fuenfzehn": 15,
    "fünfzehn": 15, "zwanzig": 20, "dreissig": 30, "dreißig": 30,
    "fuenfundvierzig": 45, "fünfundvierzig": 45, "sechzig": 60,
    "halbe": 0, "einhalb": 0,
}

#: Uhrzeiten, die man ausspricht statt zu ziffern.
_UHRZEIT = {"halb acht": (7, 30), "halb sieben": (6, 30), "halb neun": (8, 30),
            "halb zehn": (9, 30), "viertel nach sieben": (7, 15),
            "sieben uhr dreissig": (7, 30), "sieben uhr dreißig": (7, 30),
            "acht uhr": (8, 0), "sieben uhr": (7, 0), "neun uhr": (9, 0),
            "sechs uhr": (6, 0), "zehn uhr": (10, 0)}


def _zahl(text: str) -> int | None:
    """Eine Zahl aus Ziffern ODER einem Zahlwort."""
    stripped = text.strip().lower()
    if stripped.isdigit():
        return int(stripped)
    return _ZAHLWORT.get(stripped)


#: Sprachformen, die ein Mensch wirklich sagt.
def parse_when(text: str, *, now: float) -> tuple[SCH.Schedule | None, str]:
    """Aus „taeglich um halb acht" einen getippten Zeitplan.

    Absichtlich eng: was hier nicht erkannt wird, wird nachgefragt statt geraten.
    Ein falsch verstandener Zeitplan meldet sich zur falschen Zeit und faellt
    tagelang nicht auf.
    """
    import re
    low = (text or "").strip().lower()
    if not low:
        return None, ""

    absolute = re.fullmatch(r"(?:am )?(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})", low)
    if absolute:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        try:
            naive = datetime.fromisoformat(absolute.group(1) + 'T' + absolute.group(2))
            value = naive.replace(tzinfo=ZoneInfo(SCH.DEFAULT_TZ))
            if datetime.fromtimestamp(value.timestamp(), value.tzinfo).replace(tzinfo=None) != naive:
                return None, ""
            if value.timestamp() <= now:
                return None, ""
            return SCH.Schedule(kind=SCH.Kind.ONE_SHOT, at_epoch=value.timestamp()), ""
        except ValueError:
            return None, ""

    number = r"(\d+|[a-zäöüß]+)"
    minutes = re.search(rf"in\s+{number}\s*(?:minute|minuten|min)\b", low)
    if minutes and (value := _zahl(minutes.group(1))):
        return SCH.in_seconds(value * 60, now=now), ""
    hours = re.search(rf"in\s+{number}\s*(?:stunde|stunden|h)\b", low)
    if hours and (value := _zahl(hours.group(1))):
        return SCH.in_seconds(value * 3600, now=now), ""

    interval = re.search(rf"alle\s+{number}\s*(?:minute|minuten|min)\b", low)
    if interval and (value := _zahl(interval.group(1))):
        seconds, note = SCH.clamp_interval(value * 60)
        return SCH.every(seconds), note
    interval_h = re.search(rf"alle\s+{number}\s*(?:stunde|stunden|h)\b", low)
    if interval_h and (value := _zahl(interval_h.group(1))):
        seconds, note = SCH.clamp_interval(value * 3600)
        return SCH.every(seconds), note

    clock = re.search(r"(\d{1,2})[:.](\d{2})", low)
    spoken_clock = next(((h, m) for phrase, (h, m) in _UHRZEIT.items()
                         if phrase in low), None)
    if clock:
        hour, minute = int(clock.group(1)), int(clock.group(2))
    elif spoken_clock:
        hour, minute = spoken_clock
    else:
        hour, minute = 7, 30

    days = {"montag": 0, "dienstag": 1, "mittwoch": 2, "donnerstag": 3,
            "freitag": 4, "samstag": 5, "sonntag": 6}
    hit = [index for name, index in days.items() if name in low]
    if hit:
        return SCH.weekly(tuple(hit), hour, minute), ""
    if any(word in low for word in ("taeglich", "täglich", "jeden tag",
                                    "jeden morgen", "morgens", "jeden abend")):
        return SCH.daily(hour, minute), ""
    if clock or spoken_clock:
        return SCH.daily(hour, minute), "Ich habe das als taeglich verstanden."
    return None, ""


async def describe_create(arguments: dict[str, Any], clock=time.time) -> dict[str, Any]:
    """Prueft den Auftrag, BEVOR jemand danach gefragt wird.

    Der Grund steht in einem Live-Lauf: „in zwei minuten" war nicht verstanden,
    der Einwand kam aber erst NACH der erteilten Freigabe — und der eingefrorene
    Kontrollpfad musste ihn als „Ausgang unbekannt" melden, weil er einen
    Einwand nicht von einem halb gelaufenen Schreibvorgang unterscheiden kann.

    Ein Mensch soll nie etwas bestaetigen, das anschliessend an einer
    Formulierung scheitert. Also wird hier geprueft — der Router ruft diesen
    Beschreiber vor der Freigabe auf, und ein Einwand endet dann sauber als
    `invalid_input` statt als Wiederherstellungsfall.
    """
    plan, note = parse_when(str(arguments.get("wann", "")), now=clock())
    if plan is None:
        raise CapabilityDeclined(
            "schedule_not_understood",
            "Sag mir das anders — zum Beispiel 'in 20 Minuten', "
            "'taeglich um 7:30' oder 'alle 15 Minuten'.")
    action_name = str(arguments.get("aktion", "")).strip()
    if action_name not in ALLOWED_ACTIONS and action_name not in EVERYDAY_ACTIONS and action_name not in (
            "recherche", "research", "deep_research"):
        raise CapabilityDeclined(
            "action_not_allowed",
            f"Das kann ich im Hintergrund nicht. Moeglich sind: "
            f"{', '.join(ALLOWED_ACTIONS)} oder eine Recherche.",
            data={"erlaubt": list(ALLOWED_ACTIONS)})
    if action_name in EVERYDAY_ACTIONS:
        validate_everyday(action_name, arguments, plan)
    # Die Beschreibung muss ZEITSTABIL sein. Sie geht in den Digest ein, gegen
    # den bei der Fortsetzung geprueft wird — und der Digest ist der Schutz
    # davor, dass nach der Zustimmung etwas anderes ausgefuehrt wird.
    #
    # Der erste Entwurf hat `plan.as_dict()` eingesetzt. Fuer „in zwei Minuten"
    # steht darin ein ABSOLUTER Zeitpunkt, und der ist bei der Fortsetzung ein
    # anderer als bei der Anfrage: der Lauf endete mit `approval_drift`. Der
    # Schutz hat funktioniert; die Beschreibung war falsch gebaut.
    #
    # Beschrieben wird deshalb, was der Mensch gesagt hat, nicht der daraus
    # gerechnete Augenblick.
    described = {"titel": str(arguments.get("titel", ""))[:MAX_TITLE],
                 "wann": str(arguments.get("wann", ""))[:80],
                 "art": plan.kind.value, "aktion": action_name}
    if action_name in EVERYDAY_ACTIONS:
        described['umfang'] = ('Termine heute, bis zu zehn ungelesene Gmail-Mails und offene eigene SOLVIO-Aufträge; kein Versand.'
            if action_name == 'tagesueberblick' else 'Einmalig auf spätere eingegangene Nachricht im ausgewählten Gmail-Verlauf prüfen; kein Versand.')
        # The continuation journal serializes keys in sorted order. The shown
        # action must have the same order before and after that round trip.
        described['argumente'] = dict(sorted((arguments.get('argumente') or {}).items()))
        import re
        if plan.kind is SCH.Kind.ONE_SHOT and re.fullmatch(r'(?:am )?\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}', str(arguments.get('wann', '')).strip()):
            described['pruefzeitpunkt'] = plan.at_epoch
    if plan.kind in (SCH.Kind.DAILY, SCH.Kind.WEEKLY):
        described["uhrzeit"] = f"{plan.hour:02d}:{plan.minute:02d}"
    if plan.kind is SCH.Kind.WEEKLY:
        described["wochentage"] = list(plan.weekdays)
    if plan.kind is SCH.Kind.INTERVAL:
        described["abstand_s"] = plan.every_seconds
    if note:
        described["hinweis"] = note
    return described


async def classify_create(arguments: dict[str, Any], *, router: Any):
    """Wie schwer wiegt es, DIESE Automatisierung anzulegen?

    So schwer wie das, was sie tun wird. Eine Aufgabe, die jeden Morgen den
    Kalender liest, ist eine Leseabsicht; eine, die abends das Aussenlicht
    schaltet, ist gewoehnliche Haustechnik; und eine, die etwas anderes taete,
    ist ein gewoehnlicher Schreibvorgang mit allem, was dazugehoert.

    Der Grund fuer diese Ableitung steht im Auftrag: gewoehnliche Haustechnik
    soll auch vom Raummikrofon aus reibungsarm bleiben — und „mach jeden Abend
    das Aussenlicht an" ist genau das. Eine Automatisierung dagegen, deren Ziel
    sich als Schloss herausstellt, erbt dessen Klasse und damit dessen Freigabe.

    Fail-closed nach unten: was sich nicht aufloesen laesst, ist ein
    gewoehnlicher Schreibvorgang und kostet eine Freigabe.
    """
    from solvio.capabilities.policy import ActionClass, Classification
    action_name = str(arguments.get("aktion", "")).strip()
    if action_name in EVERYDAY_ACTIONS:
        return Classification(ActionClass.VERY_CRITICAL, reason="scheduled_private_read_authority")
    if action_name in ("recherche", "research", "deep_research"):
        return Classification(ActionClass.READ_ONLY, reason="schedules_research")
    if action_name in PREAUTHORIZABLE:
        spec = router.spec(action_name) if router is not None else None
        if spec is None:
            return Classification(ActionClass.NORMAL_WRITE, reason="unknown_target_capability")
        # Am ECHTEN Geraet gemessen, mit derselben Funktion, die auch der Lauf
        # spaeter benutzt. Ein Schloss faellt hier heraus, ohne dass es jemand
        # gesondert verbieten muesste.
        inner = await router._classify(spec, dict(arguments.get("argumente") or {}))
        return Classification(inner.action_class, targets=inner.targets,
                              reason=f"schedules:{inner.reason}")
    if action_name in ALLOWED_ACTIONS:
        # Die uebrigen zugelassenen Aktionen sind ausnahmslos lesend; das haelt
        # eine Zusicherung fest, damit es so bleibt.
        return Classification(ActionClass.READ_ONLY, reason="schedules_read_only")
    return Classification(ActionClass.NORMAL_WRITE, reason="schedules_unknown")


def register(router: Any, capabilities: ProactiveCapabilities) -> list[str]:
    handlers = {
        "background_create": capabilities.create,
        "background_list": capabilities.list_tasks,
        "background_get": capabilities.get,
        "background_pause": capabilities.pause,
        "background_resume": capabilities.resume,
        "background_delete": capabilities.delete,
        "background_require_approval": capabilities.require_approval,
        "background_run_now": capabilities.run_now,
        "proactive_list": capabilities.inbox,
        "proactive_get": capabilities.item,
        "proactive_mark_read": capabilities.mark_read,
    }
    for name, spec in SPECS.items():
        if name == "background_create":
            router.register(spec, handlers[name],
                            describe=lambda args: describe_create(
                                args, clock=capabilities.clock),
                            classify=lambda args: classify_create(args, router=router))
        else:
            router.register(spec, handlers[name])
    return sorted(SPECS)
