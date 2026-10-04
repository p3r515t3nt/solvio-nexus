"""Was bei einer faelligen Gelegenheit wirklich passiert.

Der heikelste Teil ist die Frage, mit welcher Autoritaet ein Lauf ausgefuehrt
wird, der Stunden nach dem Auftrag stattfindet. Die bequeme Antwort waere: „der
Nutzer hat es doch am Dienstag erlaubt". Genau diese Antwort baut eine dauerhafte
Vollmacht, die nie jemand erteilt hat.

Die hier gebaute Antwort ist eine andere und stuetzt sich auf zwei bereits
vorhandene Regeln des Vertrags:

* `authority_refusal()` laesst **Lesendes** ohne Weiteres durch — dafuer braucht
  es keinen Menschen, weil nichts geschieht.
* Ein **schreibender** Aufruf laeuft danach in `requires_approval()` und damit in
  den Freigabeweg. Dieselbe Face-ID-Bestaetigung wie im Gespraech, nur ohne
  Gespraech.

Der Hintergrundlauf traegt deshalb `user_authorized=True` — der Nutzer hat die
Aufgabe wirklich angelegt — und gewinnt dadurch **nichts** an Befugnis: er kommt
damit nur bis zum Freigabetor statt vorher an einer anderen Stelle abzuprallen.
Der Unterschied ist wichtig, weil eine Absage aus dem falschen Grund („no_user_
authority") wie ein Systemfehler aussieht, waehrend „approval_required" die
Wahrheit sagt: es fehlt eine Bestaetigung.

Die Herkunft steht ausdruecklich in der Notiz. Ein Journal, das einen
Hintergrundlauf als gesprochenen Satz ausweist, ist genau dann wertlos, wenn man
es braucht.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any

from solvio.capabilities.contract import ArgumentSource
from solvio.capabilities.envelope import CapabilityOutcome
from solvio.contracts.trust import TrustContext, TrustLevel
from solvio.logging_setup import get_logger
from solvio.proactive import fingerprint as FP
from solvio.capabilities.policy import OriginClass
from solvio.proactive import store as S
from solvio.proactive.schedule import Schedule

log = get_logger("proactive")

#: Der Principal eines Hintergrundlaufs. Ausdruecklich nicht der eines
#: Satelliten und nicht der des oertlichen Sockets.
PRINCIPAL = "background"

#: Wie ein Lauf melden darf.
ALWAYS = "immer"
ON_CHANGE = "bei_aenderung"
ON_CONDITION = "wenn_zutrifft"

#: Rueckzug nach Fehlschlaegen: 1, 2, 4 … bis zum Deckel. Ein Anbieter, der
#: gerade nicht kann, wird nicht durch Wiederholen ueberzeugt.
BACKOFF_BASE = 300.0
BACKOFF_CEILING = 6 * 3600.0
MAX_FAILURES = 8

#: Ausgaenge, die eine Stoerung bedeuten — voruebergehend, kein Mangel.
TRANSIENT = (CapabilityOutcome.EXECUTOR_UNAVAILABLE, CapabilityOutcome.TIMEOUT)

#: Gruende, die auf fehlende Anmeldung zeigen. Wichtig fuer Phase 15: ein
#: abgelaufenes Google-Token ist KEINE fehlende Faehigkeit.
CREDENTIAL_REASONS = ("credentials_missing", "not_configured", "invalid_grant",
                      "token_expired", "unauthorized", "oauth", "reauth")


@dataclass
class RunOutcome:
    state: str
    outcome: str = ""
    detail: str = ""
    item: dict[str, Any] | None = None
    fingerprint: str = ""
    approval_request: str = ""

    @property
    def failed(self) -> bool:
        return self.state == S.FAILED


def run_id_for(task_id: str, occurrence: float) -> str:
    """Eine stabile Kennung je Aufgabe und Gelegenheit.

    Aus beiden abgeleitet, nicht zufaellig: nach einem Neustart muss dieselbe
    Gelegenheit dieselbe Kennung ergeben, sonst waere die Eindeutigkeit in der
    Datenbank wertlos.
    """
    seed = f"{task_id}|{int(occurrence)}".encode("utf-8")
    return "br-" + hashlib.sha256(seed).hexdigest()[:20]


def background_trust(created_at: float) -> TrustContext:
    """Die Herkunft eines Hintergrundlaufs — benannt, nicht beschoenigt."""
    when = time.strftime("%d.%m.%Y %H:%M", time.localtime(created_at))
    return TrustContext(
        origin_trust=TrustLevel.USER_DIRECT, user_authorized=True,
        note=f"background task created by the user on {when}")


def classify_failure(outcome: CapabilityOutcome | None, reason: str) -> str:
    """Warum es nicht ging — in denselben Worten wie beim Gap Resolver.

    Zwei Faelle sind ausdruecklich getrennt: ein abgelaufener Zugang
    (`credential_or_connection_missing`) verlangt einen Menschen, eine Stoerung
    (`device_or_service_unavailable`) verlangt Geduld. Wer beides gleich
    behandelt, weckt den Nutzer wegen eines Netzhusters.
    """
    low = (reason or "").lower()
    if any(marker in low for marker in CREDENTIAL_REASONS):
        return "credential_or_connection_missing"
    if outcome in TRANSIENT:
        return "device_or_service_unavailable"
    if outcome is CapabilityOutcome.APPROVAL_REQUIRED:
        return "approval_required"
    if outcome is CapabilityOutcome.REJECTED_BY_POLICY:
        return "policy_hard_stop"
    if outcome is CapabilityOutcome.CAPABILITY_FAILED:
        return "provider_or_quota_unavailable"
    if outcome is CapabilityOutcome.RECOVERY_REQUIRED:
        return "manual_recovery_required"
    return "unknown"


def backoff_after(failures: int) -> float:
    """Wie lange gewartet wird. Verdoppelnd, gedeckelt."""
    return min(BACKOFF_BASE * (2 ** max(0, failures - 1)), BACKOFF_CEILING)


class TaskRunner:
    """Fuehrt genau eine Gelegenheit aus. Kennt keinen Zeitplan."""

    def __init__(self, dispatcher: Any, store: S.ProactiveStore) -> None:
        self.dispatcher = dispatcher
        self.store = store

    async def execute(self, task: S.Task, occurrence: float,
                      run_id: str) -> RunOutcome:
        action = task.action or {}
        kind = str(action.get("kind", "capability"))
        if kind in ("tagesueberblick", "mail_antwort_pruefen"):
            from solvio.proactive.everyday import execute
            return await execute(self, task, action, run_id)
        if kind == "research":
            return await self._research(task, action, run_id)
        return await self._capability(task, action, run_id)

    # -- Eine Faehigkeit aufrufen -------------------------------------------

    async def _capability(self, task: S.Task, action: dict[str, Any],
                          run_id: str) -> RunOutcome:
        name = str(action.get("capability", ""))
        arguments = dict(action.get("arguments") or {})
        router = self.dispatcher.capabilities
        result = await router.execute(
            name, arguments, trust=background_trust(task.created_at),
            # Die Argumente stehen seit der Erstellung fest und stammen aus dem
            # vertrauenswuerdigen Auftrag — nicht aus einer Modellwahl in diesem
            # Lauf. `TRUSTED_CONTEXT` eskaliert das Risiko deshalb nicht.
            provenance={key: ArgumentSource.TRUSTED_CONTEXT for key in arguments},
            principal=PRINCIPAL,
            # DIE HERKUNFT EINES HINTERGRUNDLAUFS. Sie ist ein Laufzeitfakt und
            # wird hier gesetzt, nicht geerbt: dass der Mensch diese Aufgabe
            # einmal am Telefon angelegt hat, macht ihre Ausfuehrung heute Nacht
            # nicht zu einer Handlung am Telefon.
            origin=OriginClass.BACKGROUND_AUTOMATION,
            # Die Automatisierung, gegen deren gebundene Erlaubnis geprueft
            # wird. Sie ist KEINE Autorisierung — sie sagt dem Core nur, welche
            # Bindung er gegen die anstehende Wirkung nachrechnen soll.
            automation_id=task.task_id)

        if result.outcome is CapabilityOutcome.APPROVAL_REQUIRED:
            # Der ganze Beweis dieses Meilensteins: ein geplanter Schreibvorgang
            # faehrt NICHT durch, nur weil er geplant war.
            request = str((result.data or {}).get("request_id", ""))
            log.info("proactive.approval_required", task=task.task_id,
                     capability=name, request=request[:20])
            return RunOutcome(S.APPROVAL_PENDING, outcome=result.outcome.value,
                              detail="Freigabe angefordert",
                              approval_request=request)
        if not result.succeeded:
            reason = result.reason or ""
            classified = classify_failure(result.outcome, reason)
            log.info("proactive.run_failed", task=task.task_id,
                     capability=name, art=classified)
            return RunOutcome(S.FAILED, outcome=result.outcome.value,
                              detail=f"{classified}:{reason}"[:200])

        data = result.data if isinstance(result.data, dict) else {"wert": result.data}
        return self._judge(task, action, run_id, data, source=name)

    # -- Recherche ------------------------------------------------------------

    async def _research(self, task: S.Task, action: dict[str, Any],
                        run_id: str) -> RunOutcome:
        """Hermes im Hintergrund — begrenzt, abbrechbar, und Information."""
        topic = str(action.get("topic", ""))[:780]
        router = self.dispatcher.capabilities
        if "deep_research" not in set(router.names()):
            return RunOutcome(S.FAILED, outcome="executor_unavailable",
                              detail="device_or_service_unavailable:deep_runtime")
        trust = background_trust(task.created_at)
        started = await router.execute(
            "deep_research", {"topic": topic}, trust=trust,
            provenance={"topic": ArgumentSource.TRUSTED_CONTEXT},
            principal=PRINCIPAL, origin=OriginClass.BACKGROUND_AUTOMATION)
        if not started.succeeded:
            return RunOutcome(S.FAILED, outcome=started.outcome.value,
                              detail=classify_failure(started.outcome,
                                                      started.reason or ""))
        data = started.data if isinstance(started.data, dict) else {}
        task_key = str(data.get("task_id", ""))
        deadline = time.monotonic() + float(action.get("timeout", 600.0))
        status = str(data.get("status", ""))
        import asyncio
        while task_key and status not in ("succeeded", "failed", "cancelled",
                                          "timed_out"):
            if time.monotonic() > deadline:
                # Die Frist gehoert der Aufgabe, nicht dem Ausfuehrenden. Was
                # laenger braucht, wird abgebrochen statt ausgesessen.
                await router.execute("deep_cancel", {"task_id": task_key},
                                     trust=trust,
                                     provenance={"task_id":
                                                 ArgumentSource.TRUSTED_CONTEXT},
                                     principal=PRINCIPAL,
                                     origin=OriginClass.BACKGROUND_AUTOMATION)
                return RunOutcome(S.FAILED, outcome="timeout",
                                  detail="device_or_service_unavailable:research_timeout")
            await asyncio.sleep(5.0)
            polled = await router.execute(
                "deep_task_status", {"task_id": task_key}, trust=trust,
                provenance={"task_id": ArgumentSource.TRUSTED_CONTEXT},
                principal=PRINCIPAL, origin=OriginClass.BACKGROUND_AUTOMATION)
            if not polled.succeeded:
                break
            data = polled.data if isinstance(polled.data, dict) else {}
            status = str(data.get("status", ""))
        if status != "succeeded":
            return RunOutcome(S.FAILED, outcome=status or "unknown",
                              detail=f"device_or_service_unavailable:{status}")
        outcome = data.get("ergebnis")
        payload = outcome if isinstance(outcome, dict) else {"text": str(outcome)}
        return self._judge(task, action, run_id, payload,
                           source="deep_research",
                           trust_tag=str(data.get("content_trust", "")))

    # -- Bewerten und melden --------------------------------------------------

    def _judge(self, task: S.Task, action: dict[str, Any], run_id: str,
               data: dict[str, Any], *, source: str,
               trust_tag: str = "") -> RunOutcome:
        """Ist das eine Meldung wert?"""
        current = FP.of(data)
        previous = str((task.last_result or {}).get("fingerprint", ""))
        notify = str(action.get("notify", ON_CHANGE))

        condition = action.get("condition") or {}
        if condition:
            met, why = _condition_met(condition, data)
            if not met:
                log.info("proactive.run_no_change", task=task.task_id,
                         reason="condition_false")
                return RunOutcome(S.NO_CHANGE, detail=why, fingerprint=current)
            if notify == ON_CONDITION and not FP.changed(previous, current):
                log.info("proactive.run_no_change", task=task.task_id,
                         reason="same_result")
                return RunOutcome(S.NO_CHANGE, detail="unveraendert",
                                  fingerprint=current)
        elif notify == ON_CHANGE and not FP.changed(previous, current):
            log.info("proactive.run_no_change", task=task.task_id)
            return RunOutcome(S.NO_CHANGE, detail="unveraendert",
                              fingerprint=current)

        item = {
            "notification_id": "pn-" + hashlib.sha256(
                f"{run_id}|{current}".encode("utf-8")).hexdigest()[:20],
            "task_id": task.task_id, "run_id": run_id,
            "created_at": time.time(),
            "priority": str(action.get("priority", "normal")),
            "summary": _summarize(task, data),
            "findings": _findings(data),
            "source_capability": source,
            # Der Rang des Materials bleibt der des Ausfuehrenden.
            "content_trust": trust_tag or str(data.get("content_trust", "")),
            "fingerprint": current,
            "expires_at": action.get("expires_at"),
        }
        return RunOutcome(S.DONE, item=item, fingerprint=current)


def _condition_met(condition: dict[str, Any], data: dict[str, Any]
                   ) -> tuple[bool, str]:
    """Prueft eine vom Nutzer gesetzte Bedingung gegen das Ergebnis.

    Bewusst winzig: `enthaelt`, `nicht_leer`, `mindestens`. Eine Ausdruckssprache
    waere maechtiger und waere zugleich der Ort, an dem fremder Inhalt Einfluss
    auf die Auswertung bekaeme.
    """
    import json
    haystack = json.dumps(data, ensure_ascii=False).lower()
    if "enthaelt" in condition:
        needle = str(condition["enthaelt"]).lower()
        return (needle in haystack), f"'{needle}' {'gefunden' if needle in haystack else 'nicht gefunden'}"
    if "nicht_leer" in condition:
        key = str(condition["nicht_leer"])
        value = data.get(key)
        full = bool(value)
        return full, f"{key} {'hat Inhalt' if full else 'ist leer'}"
    if "mindestens" in condition:
        key, count = condition["mindestens"]
        value = data.get(key) or []
        enough = len(value) >= int(count)
        return enough, f"{key}: {len(value)} von {count}"
    return True, "keine Bedingung"


def _summarize(task: S.Task, data: dict[str, Any]) -> str:
    """Ein Satz, der auch dann etwas sagt, wenn nichts da war.

    „Keine Termine" ist eine Auskunft; ein blosser Titel ist keine. Im echten
    Abnahmelauf hatte der Kalender null Eintraege, und die Meldung lautete nur
    „Kalender heute" — das liest sich wie ein Fehlschlag, obwohl es die richtige
    Antwort war.
    """
    for key in ("zusammenfassung", "summary", "text", "antwort"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:800]
    counts = [f"{len(v)} {k}" for k, v in data.items()
              if isinstance(v, list) and v][:3]
    if counts:
        return f"{task.title}: " + ", ".join(counts)
    # Nichts gefunden — das ausdruecklich sagen, statt es wegzulassen. Ein
    # ausgewiesener Zaehler ist dabei die verlaesslichere Quelle als eine leere
    # Liste, weil er auch dann stimmt, wenn die Liste gekuerzt wurde.
    empty = [key for key, value in data.items()
             if isinstance(value, list) and not value]
    if isinstance(data.get("count"), int) and data["count"] == 0:
        return f"{task.title}: nichts gefunden"
    if empty:
        return f"{task.title}: keine {empty[0]}"
    return task.title


def _findings(data: dict[str, Any]) -> list[str]:
    """Die nuetzliche Auswahl — nicht das Material.

    Ausdruecklich gedeckelt: hier landen keine E-Mail-Texte, keine Webseiten und
    keine Hermes-Protokolle, sondern kurze Zeilen, an denen ein Mensch erkennt,
    worum es geht.
    """
    out: list[str] = []
    for key, value in data.items():
        if key in ("content_trust", "fingerprint"):
            continue
        if isinstance(value, list):
            for entry in value[:5]:
                if isinstance(entry, dict):
                    label = (entry.get("titel") or entry.get("title")
                             or entry.get("betreff") or entry.get("subject")
                             or entry.get("name") or "")
                    when = entry.get("start") or entry.get("datum") or ""
                    line = f"{label} {when}".strip()
                    if line:
                        out.append(line[:200])
                elif isinstance(entry, str):
                    out.append(entry[:200])
        elif isinstance(value, str) and 0 < len(value) <= 200:
            out.append(f"{key}: {value}")
    return out[:10]
