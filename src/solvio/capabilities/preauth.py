"""Gebundene Vorab-Autorisierung fuer wiederkehrende Haus-Automatisierungen.

Das Problem, das sie loest: „Mach jeden Abend um 20 Uhr das Aussenlicht an"
soll nicht jeden Abend eine Face-ID-Runde kosten. Der Nutzer hat das einmal
bewusst gesagt; jeden Abend danach zu fragen ist keine Sicherheit, sondern
Gewoehnung an sinnloses Bestaetigen — und wer zehnmal am Tag gedankenlos
freigibt, prueft beim elften Mal nicht mehr, WAS er freigibt.

Das Problem, das sie NICHT erzeugen darf: eine dauerhafte Vollmacht. Eine
Erlaubnis wie „der Hintergrund darf Home Assistant bedienen" waere genau das
und ist ausdruecklich nicht gemeint.

DESHALB IST DIE AUTORISIERUNG AN IHRE WIRKUNG GEBUNDEN, nicht an eine Aufgabe.
Gebunden werden Faehigkeit, aufgeloestes Zielgeraet, die am Ziel gemessene
Klasse (`ha_normal` — nichts anderes), die exakten Argumente, der Zeitplan und
die Identitaet der Automatisierung. Aendert sich irgendetwas davon material,
stimmt der Digest nicht mehr, und es gibt keine Direktausfuehrung. Eine
Erlaubnis fuer „Aussenlicht an um 20 Uhr" kann damit strukturell nie
„Haustuer auf um 20 Uhr" autorisieren: andere Faehigkeit, anderes Geraet,
andere Klasse, anderer Digest.

Die Autoritaet liegt in einer Core-eigenen Zeile mit eigenem Digest — nicht im
Aufgabentext, nicht im Gedaechtnis, nicht in einer Modellausgabe.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

from solvio.capabilities.policy import ActionClass

#: Domain-separiert wie jeder andere Digest im Haus: derselbe Hash darf nie
#: versehentlich fuer einen anderen Zweck gelten.
_DOMAIN = b"SOLVIO_AUTOMATION_PREAUTH_V1"
DIGEST_VERSION = 1

#: Die EINZIGE Klasse, die eine Vorab-Autorisierung ueberhaupt tragen kann.
#: Sicherheitsnahe Haustechnik ist ausdruecklich ausgenommen — dafuer braeuchte
#: es einen eigenen Entwurf, und der ist nicht Teil dieses Milestones.
ELIGIBLE_CLASS = ActionClass.HA_NORMAL


#: Welche Zeitplanarten eine Vorab-Autorisierung ueberhaupt tragen. Ein
#: einmaliger Termin braucht keine — er laeuft genau einmal, und dafuer gibt es
#: den gewoehnlichen Weg.
RECURRING_KINDS = frozenset({"daily", "weekly", "interval"})


def schedule_binding(schedule: dict[str, Any]) -> dict[str, Any]:
    """Die ZEITSTABILE Beschreibung eines Zeitplans.

    Absichtlich ohne absoluten Zeitpunkt. Dieselbe Lehre wie beim Beschreiber
    einer Freigabe: der erste Entwurf dort band den ausgerechneten Augenblick,
    und jede Fortsetzung starb an `approval_drift`. Gebunden wird die REGEL —
    „taeglich um 20:00" —, nicht der naechste Abend.

    Aendert der Mensch die Regel, aendert sich die Bindung, und die alte
    Erlaubnis traegt nicht mehr. Genau das ist gewollt.
    """
    plan = schedule or {}
    kind = str(plan.get("art", plan.get("kind", "")))
    bound: dict[str, Any] = {"art": kind, "zeitzone": str(plan.get("zeitzone", ""))}
    if kind in ("daily", "weekly"):
        bound["uhrzeit"] = str(plan.get("uhrzeit", ""))
    if kind == "weekly":
        bound["wochentage"] = sorted(int(d) for d in (plan.get("wochentage") or []))
    if kind == "interval":
        bound["abstand_s"] = int(plan.get("abstand_s") or 0)
    return bound


def preauth_digest(*, automation_id: str, capability: str,
                   action_class: str, targets, arguments: dict[str, Any],
                   schedule: dict[str, Any]) -> str:
    """Kanonischer Digest ueber ALLES, was die Wirkung bestimmt.

    Kanonisch heisst hier dasselbe wie im Freigabepfad: sortierte Schluessel,
    kompakte Trenner, UTF-8, JSON-escaped — keine Abhaengigkeit von
    Schluesselreihenfolge, keine mehrdeutigen Feldgrenzen.
    """
    payload = {
        "v": DIGEST_VERSION,
        "automation_id": automation_id,
        "capability": capability,
        "action_class": action_class,
        "targets": sorted(str(t) for t in (targets or ())),
        "arguments": arguments or {},
        # Der Zeitplan wird HIER normalisiert und nicht beim Aufrufer.
        #
        # Beim Messen der Laufzeit ist genau das aufgefallen: eine Praegung mit
        # rohem Zeitplan und eine Pruefung mit gebundenem ergaben verschiedene
        # Digests, obwohl dieselbe Regel gemeint war. In der Produktion lief
        # beides ueber dieselbe Funktion und es waere nie aufgefallen — bis
        # irgendwann ein zweiter Aufrufer dazukommt. Eine Bindung, die davon
        # abhaengt, dass zwei Aufrufer dieselbe Vorbereitung treffen, ist keine.
        "schedule": schedule_binding(schedule or {}),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(_DOMAIN + b"\x00" + canonical).hexdigest()


@dataclass(frozen=True)
class Preauthorization:
    """Eine gebundene Erlaubnis. Lesbar, pruefbar, widerrufbar."""

    preauth_id: str
    automation_id: str
    capability: str
    action_class: str
    targets: tuple[str, ...]
    digest: str
    revision: int
    created_at: float
    created_origin: str
    created_principal: str
    enabled: bool = True
    revoked_at: float | None = None
    revoked_reason: str = ""

    @property
    def active(self) -> bool:
        return self.enabled and self.revoked_at is None

    def as_dict(self) -> dict[str, Any]:
        """Fuer Anzeige und Journal — der Digest gekuerzt, nie ein Geheimnis."""
        return {
            "id": self.preauth_id, "automatisierung": self.automation_id,
            "faehigkeit": self.capability, "klasse": self.action_class,
            "ziele": list(self.targets), "revision": self.revision,
            "aktiv": self.active, "erstellt_ueber": self.created_origin,
            "erstellt_am": self.created_at,
            "widerrufen_am": self.revoked_at,
            "widerrufsgrund": self.revoked_reason,
            "bindung": self.digest[:16],
        }


class PreauthorizationError(Exception):
    """Die Erlaubnis traegt nicht. Immer mit stabilem Grund."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def mint(*, automation_id: str, capability: str, action_class: ActionClass,
         targets, arguments: dict[str, Any], schedule: dict[str, Any],
         created_origin: str, created_principal: str,
         revision: int = 1, now: float | None = None) -> Preauthorization:
    """Eine neue Erlaubnis praegen — nur fuer die eine zugelassene Klasse.

    Der Aufrufer hat die Klasse am AUFGELOESTEN Ziel gemessen; hier wird sie
    nur noch gegen die zugelassene Menge geprueft. Das ist Absicht: die Pruefung
    steht an der Stelle, die sie nicht umgehen kann.
    """
    if action_class is not ELIGIBLE_CLASS:
        raise PreauthorizationError("class_not_eligible")
    if not automation_id or not capability:
        raise PreauthorizationError("incomplete_binding")
    resolved = tuple(sorted(str(t) for t in (targets or ()) if str(t).strip()))
    if not resolved:
        # Ohne aufgeloestes Ziel gaebe es nichts zu binden, und eine Erlaubnis
        # ohne Ziel waere die Blankovollmacht, die es nicht geben soll.
        raise PreauthorizationError("no_resolved_target")
    stamp = time.time() if now is None else now
    digest = preauth_digest(automation_id=automation_id, capability=capability,
                            action_class=action_class.value, targets=resolved,
                            arguments=arguments, schedule=schedule)
    return Preauthorization(
        preauth_id="pa-" + hashlib.sha256(
            f"{automation_id}|{revision}|{digest}".encode("utf-8")).hexdigest()[:20],
        automation_id=automation_id, capability=capability,
        action_class=action_class.value, targets=resolved, digest=digest,
        revision=revision, created_at=stamp, created_origin=created_origin,
        created_principal=created_principal)


def verify(stored: Preauthorization | None, *, automation_id: str,
           capability: str, action_class: ActionClass, targets,
           arguments: dict[str, Any], schedule: dict[str, Any]) -> str:
    """Traegt die gespeicherte Erlaubnis DIESE anstehende Wirkung?

    Gibt die Kennung zurueck, wenn ja — sonst `PreauthorizationError` mit dem
    Grund. Es wird ausdruecklich NICHT geprueft, ob es die Aufgabe gibt: „die
    Aufgabe existierte" ist keine Autorisierung. Geprueft wird die Wirkung.
    """
    if stored is None:
        raise PreauthorizationError("no_preauthorization")
    if not stored.active:
        raise PreauthorizationError("preauthorization_revoked")
    if stored.automation_id != automation_id:
        raise PreauthorizationError("automation_mismatch")
    if stored.capability != capability:
        raise PreauthorizationError("capability_mismatch")
    if stored.action_class != ELIGIBLE_CLASS.value:
        raise PreauthorizationError("class_not_eligible")
    if action_class is not ELIGIBLE_CLASS:
        # Das Geraet hat sich umklassifiziert (aus einer Lampe wurde ein
        # Zutrittsgeraet) oder die Faehigkeit bedeutet etwas anderes als
        # damals. Beides endet hier und nicht an der Haustuer.
        raise PreauthorizationError("effect_reclassified")
    fresh = preauth_digest(automation_id=automation_id, capability=capability,
                           action_class=action_class.value, targets=targets,
                           arguments=arguments, schedule=schedule)
    if fresh != stored.digest:
        raise PreauthorizationError("binding_mismatch")
    return stored.preauth_id


class AutomationPreauthorizations:
    """Der Pruefer, den der Router benutzt. Haelt selbst keine Autoritaet.

    Er beantwortet genau eine Frage: traegt die gespeicherte, gebundene
    Erlaubnis DIE WIRKUNG, die jetzt anstehen wuerde? Nicht „gibt es die
    Aufgabe" — die Existenz einer Aufgabe hat noch nie etwas autorisiert.
    """

    def __init__(self, store: Any) -> None:
        self.store = store

    async def verify(self, *, automation_id: str, capability: str,
                     action_class: ActionClass, targets,
                     arguments: dict[str, Any]) -> str:
        task = await self.store.get_task(automation_id)
        if task is None:
            raise PreauthorizationError("automation_unknown")
        if not task.running:
            # Pausiert oder beendet: eine ruhende Automatisierung traegt keine
            # Erlaubnis. Wer sie wieder aufnimmt, nimmt auch die Bindung wieder
            # auf — dieselbe, unveraenderte.
            raise PreauthorizationError("automation_disabled")
        stored = await self.store.get_preauth(automation_id)
        return verify(stored, automation_id=automation_id, capability=capability,
                      action_class=action_class, targets=targets,
                      arguments=arguments,
                      schedule=schedule_binding(task.schedule))

    async def mint_for_task(self, task: Any, *, capability: str,
                            action_class: ActionClass, targets,
                            arguments: dict[str, Any], created_origin: str,
                            created_principal: str) -> "Preauthorization":
        """Praegt die Erlaubnis fuer eine gerade entstandene Automatisierung.

        Eine bereits vorhandene wird ERSETZT, nicht ergaenzt: es gibt genau eine
        gueltige Bindung je Automatisierung, und ihre Revision zaehlt hoch.
        """
        previous = await self.store.get_preauth(task.task_id)
        revision = (previous.revision + 1) if previous is not None else 1
        grant = mint(automation_id=task.task_id, capability=capability,
                     action_class=action_class, targets=targets,
                     arguments=arguments,
                     schedule=schedule_binding(task.schedule),
                     created_origin=created_origin,
                     created_principal=created_principal, revision=revision)
        await self.store.put_preauth(grant)
        return grant

    async def revoke(self, automation_id: str, reason: str) -> bool:
        return await self.store.revoke_preauth(automation_id, reason)
