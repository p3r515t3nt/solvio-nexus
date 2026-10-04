"""Die deterministische Politik. Das Modell schlaegt vor, sie entscheidet.

Die dritte Instanz desselben Hausmusters: Adaptive-Memory-Extraktor →
Adoptionspolitik, Agent-Runtime-Planer → `validate()`, und jetzt Einschaetzer →
Routenpolitik. Was diese Datei tut, tut sie **fail-closed**: eine Antwort, die
sie nicht versteht, ist keine Route, sondern eine Ablehnung.

**Vier Riegel gegen Lenkung von aussen**, und jeder schliesst einen anderen
Kanal, ueber den eingeschleuster Inhalt eine Entscheidung faerben koennte:

1. **Autoritaetsfelder** — ein Argument, das wie Herkunft, Vertrauen, Freigabe,
   Risiko, Stufe oder Budget heisst, wird nicht still verworfen. Es ist ein
   Ablehnungsgrund. Wer so etwas vorschlaegt, hat den Vertrag missverstanden,
   und das soll auffallen.
2. **Die Ueberlappungsregel** — das `ziel` muss aus den Worten des Nutzers
   bestehen. Tut es das nicht, wird es durch den woertlichen Turn-Text
   ERSETZT. Der Einschaetzer darf kuerzen, nie dichten; damit kann ein Satz aus
   einer Zusammenfassung kein Ziel werden.
3. **Die geschlossene Praeferenz** — ein Fachmann-Wunsch wird gegen die Menge
   der tatsaechlich benutzbaren Profile geprueft und sonst fallengelassen. Er
   ist Entscheidungs-Beiwerk und wandert NIE in das Ziel.
4. **Die Register-Mitgliedschaft** — eine Fortsetzung darf nur eine Kennung
   nennen, die im Arbeitsregister oder in den intern geprueften, dargestellten
   Fremdchat-Verweisen steht. Eine Kennung bloss aus einem Inhalt zaehlt nicht.

**Die Eskalation ist ein geschlossener Ereigniskatalog**, kein Ermessen. Ein
Modell kann sie empfehlen (`zuversicht`, `schwierigkeit`); ausloesen kann sie
nur diese Datei, und budgetieren nur der Broker.
"""
from __future__ import annotations

import json
import re
from typing import Any

from solvio.cognition import models as M
from solvio.cognition.continuity import ContinuityView
from solvio.cognition.types import (CONSULT_ROLES, DIFFICULTIES, EscalationEvent,
                                    ModelTier, Route, TaskAssessment)
from solvio.logging_setup import get_logger

log = get_logger("cognition")

#: Die Namen, die in einer Modellantwort nichts zu suchen haben. Die ersten
#: zehn sind woertlich die Liste des Planers (`agent_runtime/planner.py`); die
#: letzten vier kommen dazu, weil dieser Milestone erstmals eine Stufe und ein
#: Budget kennt.
AUTHORITY_FIELDS: tuple[str, ...] = (
    "trust", "origin", "provenance", "approval", "approval_request_id",
    "execution_id", "principal", "commanded", "user_authorized", "risk",
    "model", "tier", "budget", "stufe",
)


class AssessmentInvalid(ValueError):
    """Der Vorschlag hat die Politik nicht ueberstanden.

    Traegt einen Grund aus geschlossenem Vokabular und NIE den Text, an dem er
    gescheitert ist — die Meldung wandert in ein Buch und in ein Log.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"assessment_invalid:{reason}")
        self.reason = reason
        self.detail = detail


_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")
_NON_WORD = re.compile(r"[^0-9a-zA-ZÀ-ɏ]+")


def extract_json(text: str) -> dict:
    """Tolerant gegenueber Rahmung, streng gegenueber Inhalt."""
    raw = str(text or "").strip()
    if not raw:
        raise AssessmentInvalid("not_json")
    raw = _FENCE.sub("", raw).strip()
    try:
        parsed = json.loads(raw)
    except ValueError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            raise AssessmentInvalid("not_json") from None
        try:
            parsed = json.loads(raw[start:end + 1])
        except ValueError:
            raise AssessmentInvalid("not_json") from None
    if not isinstance(parsed, dict):
        raise AssessmentInvalid("not_an_object")
    return parsed


def tokens(text: str) -> list[str]:
    """Inhaltstoken ab drei Zeichen — dieselbe Zaehlweise wie das Provenienz-Tor."""
    words = _NON_WORD.sub(" ", str(text or "").lower()).split()
    return [word for word in words if len(word) >= M.MIN_TOKEN_LENGTH]


def overlap(objective: str, scope: str) -> float:
    """Wie viel des Ziels aus den Worten des Nutzers besteht."""
    goal = tokens(objective)
    if not goal:
        return 0.0
    spoken = set(tokens(scope))
    hits = sum(1 for token in goal if token in spoken)
    return hits / len(goal)


def usable_preferences() -> frozenset[str]:
    """Das geschlossene Vokabular fuer `praeferenz`.

    Die Anbieternamen der tatsaechlich benutzbaren Profile plus die drei
    Botrollen. Die Laufzeit wird dabei INNERHALB der Funktion importiert: ein
    Modulimport von `solvio.agent_runtime` im Kern waere genau die Zusage, die
    `SOLVIO_AGENT_RUNTIME=off` bricht.
    """
    names: set[str] = set(CONSULT_ROLES)
    try:
        from solvio.agent_runtime.specialists import usable_profiles
        for key, profile in usable_profiles().items():
            names.add(key)
            provider = getattr(profile, "provider", None)
            names.add(str(getattr(provider, "value", provider) or "").lower())
    except Exception as exc:  # noqa: BLE001 - ohne Laufzeit bleiben die Rollen
        log.info("cognition.profiles_unreadable", kind=type(exc).__name__)
    names.discard("")
    return frozenset(names)


def _clarification(text: str) -> str:
    """Genau ein Satz, redigiert und gedeckelt, bevor er gesprochen wird."""
    raw = " ".join(str(text or "").split())
    if not raw:
        return ""
    try:
        from solvio.specialists.launcher import redact
        raw = redact(raw)
    except Exception:  # noqa: BLE001 - ohne zweites Netz bleibt die Kappe
        pass
    # Ein Satz. Was danach kaeme, waere eine zweite Frage.
    for stop in (". ", "? ", "! "):
        cut = raw.find(stop)
        if cut > 0:
            raw = raw[:cut + 1]
            break
    return raw[:M.MAX_CLARIFICATION_CHARS]


def validate(raw_text: str, *, view: ContinuityView, scope_text: str,
             tier: ModelTier, fallback_text: str = "") -> TaskAssessment:
    """Aus Modelltext wird ein Vorschlag — oder eine Ablehnung."""
    payload = extract_json(raw_text)

    for forbidden in AUTHORITY_FIELDS:
        if forbidden in payload:
            raise AssessmentInvalid("authority_field_in_assessment", forbidden)

    weg = str(payload.get("weg", "") or "")
    try:
        route = Route(weg)
    except ValueError:
        raise AssessmentInvalid("unknown_route", weg[:40]) from None

    rolle = str(payload.get("fachbot_rolle", "") or "")
    if rolle and rolle not in CONSULT_ROLES:
        raise AssessmentInvalid("unknown_consult_role", rolle[:40])
    if route is Route.CONSULT and not rolle:
        raise AssessmentInvalid("consult_role_missing")
    if route is not Route.CONSULT:
        rolle = ""

    # **Zwei Texte, und der Unterschied ist der ganze Punkt.**
    #
    # `scope_text` ist der PRUEFUMFANG: woran gemessen wird, ob das
    # vorgeschlagene Ziel aus dem Gespraech stammt. Er darf SOLVIOs eigene
    # konkrete Rueckfrage enthalten — auf „Meinst du den DAX von gestern?"
    # antwortet ein Mensch mit „Genau das.", und ohne die Frage im Umfang
    # zerstoert blosse Wortueberlappung genau diese Bestaetigung.
    #
    # `fallback_text` ist der ERSATZTEXT: was das Ziel wird, wenn der Vorschlag
    # durchfaellt. Er besteht AUSSCHLIESSLICH aus Worten des Menschen. SOLVIOs
    # eigener Satz wird nie zum Auftrag — sonst haette sich das Haus selbst
    # beauftragt.
    ersatz = str(fallback_text if fallback_text else scope_text).strip()
    ziel = str(payload.get("ziel", "") or "").strip()
    vorschlag = ziel
    ersetzt = overlap(ziel, scope_text) < M.OBJECTIVE_OVERLAP_MIN
    if ersetzt:
        # Kein Fehlschlag, sondern eine Korrektur: die Worte des Nutzers
        # gewinnen. Genau hier scheitert eingeschleuster Fortsetzungsinhalt.
        log.info("cognition.objective_replaced", route=route.value,
                 overlap=round(overlap(ziel, scope_text), 2))
        ziel = ersatz

    schwierigkeit = str(payload.get("schwierigkeit", "") or "mittel")
    if schwierigkeit not in DIFFICULTIES:
        schwierigkeit = "mittel"

    try:
        zuversicht = float(payload.get("zuversicht", 0.0))
    except (TypeError, ValueError):
        raise AssessmentInvalid("confidence_not_a_number") from None
    zuversicht = max(0.0, min(1.0, zuversicht))

    frage = _clarification(payload.get("klaerungsfrage", ""))
    if route is Route.CLARIFY and not frage:
        raise AssessmentInvalid("clarification_missing")
    if route is not Route.CLARIFY:
        frage = ""

    praeferenz = str(payload.get("praeferenz", "") or "").strip().lower()
    if praeferenz and praeferenz not in usable_preferences():
        log.info("cognition.preference_dropped")
        praeferenz = ""

    fortsetzung = str(payload.get("fortsetzung_von", "") or "").strip()
    proposed_continuation = bool(fortsetzung)
    if fortsetzung and fortsetzung not in view.known_ids():
        # Auch eine Kennung, die in einem Inhalt stand. Sie findet im Register
        # nichts. Im erweiterten Textweg muss ein solcher Fortsetzungswunsch
        # geklaert werden; der bisherige Pfad behandelt ihn wie zuvor als neu.
        log.info("cognition.continuation_dropped")
        fortsetzung = ""

    bezug = payload.get("auftragsbezug")
    if bezug is not None and bezug not in ("neu", "fortsetzen", "unklar"):
        raise AssessmentInvalid("unknown_reference_intent")
    if bezug is None:
        bezug = ("fortsetzen" if fortsetzung or (
            view.related_enabled and proposed_continuation) else "neu")
    if view.related_enabled:
        if bezug == "neu":
            fortsetzung = ""
        current_ids = {entry.work_id for entry in view.entries}
        recent_ambiguous = (len(view.related_entries) > 1
                            and not view.related_selection_pending
                            and all(entry.match_kind == "recent"
                                    for entry in view.related_entries))
        needs_reference = (bezug == "unklar" or (
            bezug == "fortsetzen" and fortsetzung not in current_ids and (
                not fortsetzung or recent_ambiguous or view.related_incomplete)))
        if needs_reference:
            # Kein weiterer Modellaufruf und keine Wortliste: die semantische
            # Aussage kommt aus derselben Einschaetzung, die Auswahlgrenze
            # aus den geprueften und tatsaechlich dargestellten Kandidaten.
            route = Route.CLARIFY
            rolle = ""
            fortsetzung = ""
            frage = frage or "Welchen frueheren Auftrag meinst du?"

    profile = payload.get("auftragsprofil", "")
    if "auftragsprofil" in payload and profile not in ("allgemein", "spezialisiert", "projekt", "persoenlich"):
        raise AssessmentInvalid("unknown_task_profile")

    return TaskAssessment(route=route, objective=ziel, consult_role=rolle,
                          continuation_of=fortsetzung,
                          difficulty=schwierigkeit, confidence=zuversicht,
                          clarification=frage, preference=praeferenz,
                          tier=tier, objective_replaced=ersetzt,
                          proposed_objective=vorschlag, reference_intent=bezug,
                          task_profile=profile)


# =====================================================================
# Der Ereigniskatalog
# =====================================================================

#: E2 — die Abwertungstabelle. Sie ist der Grund, warum eine unsichere
#: Einschaetzung nicht in einer Rueckfrage endet: was ein harmloser Blick
#: beantworten kann, wird nachgesehen. Ein Bauauftrag hingegen kostet
#: Nutzerautoritaet — bei ihm ist die Rueckfrage das Richtige.
DOWNGRADE: dict[Route, Route] = {
    Route.AGENT_RESEARCH: Route.RESEARCH_QUICK,
    Route.AGENT_BUILD: Route.CLARIFY,
}


def low_confidence(assessment: TaskAssessment) -> bool:
    """Der E2-Ausloeser: unsicher, oder der eine benannte Widerspruch.

    Der Widerspruch ist benannt und nicht erfunden: „das ist Gespraech" bei
    zugleich „das ist schwer" sind zwei Aussagen, die nicht zusammenpassen —
    eine davon ist falsch, und welche, weiss das kleine Modell offenbar nicht.
    """
    if assessment.confidence < M.ESCALATE_BELOW:
        return True
    return (assessment.route is Route.HAND_BACK
            and assessment.difficulty == "hoch")


def reason_is_hard(assessment: TaskAssessment) -> bool:
    """Der E3-Ausloeser: nachdenken UND hoch. Beides, nicht eines."""
    return (assessment.route is Route.REASON
            and assessment.difficulty == "hoch")


def downgrade(route: Route) -> Route:
    """Was gilt, wenn auch das grosse Modell unsicher blieb."""
    return DOWNGRADE.get(route, route)


def tier_for_reason(assessment: TaskAssessment) -> tuple[ModelTier, EscalationEvent]:
    """Welche Stufe der Weg `nachdenken` bekommt."""
    if reason_is_hard(assessment):
        return ModelTier.LARGE, EscalationEvent.REASON_HARD
    return ModelTier.MINI, EscalationEvent.NONE


def planning_tier(event_ordinal: int, attempt: int) -> tuple[ModelTier, EscalationEvent]:
    """E4/E5 — welche Stufe ein Planungsaufruf der Agentenlaufzeit bekommt.

    `event_ordinal` ist 0 fuer die erste Planung, 1 fuer die erste
    Nachplanung, 2 fuer die zweite. `attempt` ist 1 oder 2 innerhalb eines
    Ereignisses.

    * E5 zuerst: ab der ZWEITEN Nachplanung laufen beide Aufrufe des
      Ereignisses gross. Wer zweimal nachplanen musste, hat kein
      Formatproblem.
    * E4 danach: die eine Nachfrage eines Ereignisses laeuft gross.

    Die Anzahl der Aufrufe aendert sich durch beides NICHT — es sind dieselben
    Aufrufe auf einer anderen Stufe, innerhalb derselben Sechserkappe.
    """
    if int(event_ordinal) >= 2:
        return ModelTier.LARGE, EscalationEvent.SECOND_REPLANNING
    if int(attempt) >= 2:
        return ModelTier.LARGE, EscalationEvent.PLAN_REPAIR
    return ModelTier.MINI, EscalationEvent.NONE


def escalation_available(assessment: TaskAssessment, *, already: bool) -> bool:
    """Eine Kommission eskaliert die Einschaetzung HOECHSTENS EINMAL.

    E1 und E2 teilen sich dieses eine Mal. Es gibt keine Kante von einer
    Eskalation zur naechsten, und die Ausgabe des grossen Modells wird nie von
    einem weiteren Modellaufruf beurteilt.
    """
    del assessment
    return not already
