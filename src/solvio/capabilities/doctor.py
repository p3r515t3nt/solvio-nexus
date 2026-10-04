"""Den Arzt fragen — und ihn bitten, das Kaputte zu richten.

Zwei Faehigkeiten, und der Unterschied zwischen ihnen ist die ganze Ueberlegung.

`system_diagnose` liest. Es beantwortet „Warum geht mein Kalender nicht?" mit
dem, was gemessen wurde, samt Zuversicht — und sagt ausdruecklich, wenn nur ein
Mensch weiterhelfen kann.

`system_heal` handelt, und **nimmt dafuer kein Argument entgegen.** Das ist
keine Bequemlichkeit, sondern der Kern: das Modell kann nicht waehlen, WAS
angefasst wird. Es kann nur sagen „richte, was du gefunden hast" — was danach
geschieht, entscheidet der Core aus seinem eigenen Befund gegen eine
geschlossene Liste von Vorgehen. Ein Modell, das den Namen der Komponente
beisteuert, waere ein Modell, das das Ziel einer Handlung bestimmt. Genau das
soll es nicht.

Deshalb steht hier auch keine eigene Freigabelogik. Die Befugnis liegt in der
Liste der Vorgehen — oertlich, umkehrbar, SOLVIO-eigene Laufzeit — und die gilt
fuer den Hintergrund wie fuer den Zuruf. Ein zweiter Freigabeweg neben dem
bestehenden waere eine zweite Wahrheit ueber Autoritaet, und das ist die
schlimmste Sorte.
"""
from __future__ import annotations

from typing import Any

from solvio.capabilities.contract import (
    CapabilityDeclined, CapabilitySpec, ExecutionClass, RiskLevel,
)
from solvio.control_center.health import State
from solvio.security.mobile_approval.execution import IDEMPOTENT_WRITE, READ_ONLY
from solvio.logging_setup import get_logger

log = get_logger("capability")

#: Wie viele Komponenten eine Antwort hoechstens aufzaehlt. Wer nach dem Grund
#: fragt, will keine Inventarliste.
MAX_NAMED = 4


SPECS: dict[str, CapabilitySpec] = {
    "system_diagnose": CapabilitySpec(
        name="system_diagnose", version=1, execution_class=ExecutionClass.FAST,
        base_risk=RiskLevel.HARMLESS, semantics=READ_ONLY, timeout=30.0,
        description="Sagt, warum etwas gerade nicht geht — mit Ursache und "
                    "Zuversicht. Ohne Angabe: alles, was nicht in Ordnung ist.",
        input_schema={"type": "object", "properties": {
            "komponente": {"type": "string",
                           "description": "kalender | email | recherche | "
                                          "browser | portal | hintergrund | "
                                          "vermittlung"}}}),
    "system_heal": CapabilitySpec(
        name="system_heal", version=1,
        execution_class=ExecutionClass.CONTROLLED, base_risk=RiskLevel.HARMLESS,
        semantics=IDEMPOTENT_WRITE, timeout=300.0,
        description="Versucht zu richten, was gerade kaputt ist. Nimmt bewusst "
                    "keine Angabe entgegen — repariert wird, was SOLVIO selbst "
                    "befundet hat.",
        input_schema={"type": "object", "properties": {}}),
}


#: Woerter, wie ein Mensch sie sagt, auf die Schluessel der Messung.
_ALIASES = {
    "auftrag": "agent_runtime", "auftraege": "agent_runtime",
    "agent": "agent_runtime", "agenten": "agent_runtime",
    "agent_runtime": "agent_runtime",
    "kalender": "calendar", "termine": "calendar", "calendar": "calendar",
    "email": "gmail", "e-mail": "gmail", "mail": "gmail", "gmail": "gmail",
    "recherche": "hermes", "hermes": "hermes", "forschung": "hermes",
    "browser": "browser", "portal": "portal", "portale": "portal",
    "hintergrund": "scheduler", "scheduler": "scheduler",
    "zuhause": "home_assistant", "home assistant": "home_assistant",
    "architekt": "claude", "claude": "claude",
    "herausforderer": "codex", "codex": "codex",
    "tresor": "vault", "vault": "vault", "zugaenge": "vault",
    "zahlung": "payment", "zahlungen": "payment", "bezahlen": "payment",
    "payment": "payment",
    # Ohne diese Zeile heisst der Zustand roh „broker" und ist per Stimme gar
    # nicht erfragbar — der Mensch kann nach etwas fragen, das keinen Namen hat.
    "vermittlung": "broker", "anbieter": "broker", "broker": "broker",
    # Dasselbe fuer die Fernsicherung: eine Komponente, die man nicht
    # ansprechen kann, kann man auch nicht erfragen.
    "fernsicherung": "offsite", "offsite": "offsite",
    "auslagerung": "offsite", "ausserhalb": "offsite",
}


def _key(spoken: str) -> str:
    cleaned = spoken.strip().lower()
    return _ALIASES.get(cleaned, cleaned)


class DoctorCapabilities:
    """Die Handler. Der Arzt selbst haengt am Kontrollzentrum."""

    def __init__(self, doctor: Any) -> None:
        self.doctor = doctor

    async def diagnose(self, arguments: dict[str, Any]) -> dict[str, Any]:
        spoken = str(arguments.get("komponente", "") or "").strip()
        if spoken:
            component = _key(spoken)
            found = await self.doctor.diagnose(
                component, requested_by_user=True)
            if found.observed is State.UNKNOWN:
                raise CapabilityDeclined(
                    "unbekannte_komponente",
                    f"Das kenne ich nicht als Teil von mir: {spoken}")
            return {"befunde": [found.as_dict()], "antwort": _sentence([found])}

        found = await self.doctor.diagnose_all(requested_by_user=True)
        return {"befunde": [f.as_dict() for f in found],
                "antwort": _sentence(found)}

    async def heal(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Richtet, was gefunden wurde. Ohne Angabe — siehe Modulkopf."""
        results = await self.doctor.heal_all(requested_by_user=True)
        healed, failed, human, helpless = [], [], [], []
        for diagnosis, attempt in results:
            label = diagnosis.component
            if attempt is not None and attempt.recovered:
                healed.append(label)
            elif diagnosis.human_action_required:
                human.append((label, diagnosis.probable_cause))
            elif attempt is not None:
                # Es lief ein Vorgehen, und es hat nicht geholfen.
                failed.append(label)
            else:
                # Es gab gar kein Vorgehen. Das ist etwas anderes als ein
                # Fehlschlag, und es so zu nennen behauptet einen Versuch,
                # den es nie gab.
                helpless.append(label)
        log.info("doctor.heal_requested", healed=len(healed), failed=len(failed),
                 human=len(human), helpless=len(helpless))
        return {"behoben": healed, "fehlgeschlagen": failed,
                "kein_mittel": helpless,
                "braucht_dich": [name for name, _ in human],
                "antwort": _heal_sentence(healed, failed, human, helpless)}


def _sentence(found: list[Any]) -> str:
    """Eine Antwort, wie man sie sagt — nicht eine Liste von Zustaenden."""
    broken = [f for f in found if f.observed is not State.HEALTHY]
    if not broken:
        return "Bei mir ist gerade alles in Ordnung."
    parts = []
    for diagnosis in broken[:MAX_NAMED]:
        name = _LABELS.get(diagnosis.component, diagnosis.component)
        cause = diagnosis.probable_cause or "ich weiss noch nicht, warum"
        if diagnosis.human_action_required:
            parts.append(f"{name}: {cause}. Das kann nur jemand von Hand.")
        elif diagnosis.repair_available:
            parts.append(f"{name}: {cause}. Dafuer habe ich ein Mittel.")
        else:
            parts.append(f"{name}: {cause}. Dafuer habe ich kein Mittel.")
    rest = len(broken) - MAX_NAMED
    if rest > 0:
        parts.append(f"und {rest} weitere")
    return " ".join(parts)


def _heal_sentence(healed: list[str], failed: list[str],
                   human: list[tuple[str, str]], helpless: list[str]) -> str:
    if not healed and not failed and not human and not helpless:
        return "Es war nichts zu richten."
    parts = []
    if healed:
        parts.append(_join([_LABELS.get(k, k) for k in healed])
                     + (" laeuft wieder." if len(healed) == 1
                        else " laufen wieder."))
    for name, cause in human[:MAX_NAMED]:
        parts.append(f"{_LABELS.get(name, name)} braucht dich: {cause}.")
    if failed:
        parts.append(_join([_LABELS.get(k, k) for k in failed])
                     + " habe ich versucht, ohne Erfolg.")
    if helpless:
        # Bewusst anders formuliert als ein Fehlschlag: „nicht hinbekommen"
        # klaenge nach einem Versuch, und es gab keinen.
        parts.append("Fuer " + _join([_LABELS.get(k, k) for k in helpless])
                     + " habe ich kein Mittel.")
    return " ".join(parts)


def _join(names: list[str]) -> str:
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " und " + names[-1]


#: Dieselben Namen wie in der Chronik und der Ueberwachung.
_LABELS = {"agent_runtime": "Die Auftraege", "hermes": "Die Recherche", "portal": "Der Portal-Zugang",
           "browser": "Der Browser", "scheduler": "Der Hintergrund",
           "calendar": "Der Kalender", "gmail": "Die E-Mail",
           "home_assistant": "Dein Zuhause", "claude": "Der Architekt",
           "codex": "Der Herausforderer", "gateway": "Der Freigabeweg",
           "core": "SOLVIO", "storage": "Die Sicherung",
           "offsite": "Die Fernsicherung", "vault": "Der Tresor",
           "payment": "Die Zahlungen", "broker": "Die Anbieter-Vermittlung"}


def register(router: Any, capabilities: DoctorCapabilities) -> list[str]:
    handlers = {"system_diagnose": capabilities.diagnose,
                "system_heal": capabilities.heal}
    for name, spec in SPECS.items():
        router.register(spec, handlers[name])
    return sorted(SPECS)
