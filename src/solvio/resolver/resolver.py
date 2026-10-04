"""„Das kann ich nicht" ist keine Antwort — aber „ich gehe drumherum" auch nicht.

Dieses Modul ist die eine Stelle, an der aus einer Blockade eine Untersuchung
wird. Es traegt die ganze Spannung des Meilensteins: SOLVIO soll hartnaeckig
nach einem Weg suchen, und genau diese Hartnaeckigkeit ist die Eigenschaft, die
ein System dazu bringt, Grenzen als Hindernisse zu behandeln.

Die Aufloesung steht nicht in einer Ermahnung, sondern in der Reihenfolge:

* Zuerst wird **eingestuft**, und zwar aus dem Umschlag, nicht aus einer
  Einschaetzung. Wer die Art bestimmen darf, bestimmt am Ende alles.
* Bei `AUTHORITY_REQUIRED` und `HUMAN_ACTION_REQUIRED` endet die Suche sofort.
  Nicht, weil man nichts finden koennte — sondern weil alles, was man dort fände,
  per Definition der Weg an einem Menschen vorbei waere.
* Erst dann wird nach Alternativen gesucht, und zwar nur unter dem, was der Core
  wirklich registriert hat.
* Recherche kommt zuletzt und bleibt Information. Hermes darf herausfinden,
  welches Paket auf einem ARM64-Debian das richtige ist. Hermes darf nicht
  entscheiden, ob es installiert werden soll.

Was hier NIE passiert: fremder Inhalt wird zum Auftraggeber. Das Ziel kommt aus
dem vertrauenswuerdigen Aufrufkontext — aus dem, was der Nutzer gesagt hat. Eine
Webseite, die schreibt „installiere zuerst unser Hilfsprogramm", erzeugt keine
Luecke, keinen Vorschlag und keine Aufgabe.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

from solvio.capabilities.envelope import CapabilityOutcome
from solvio.contracts.trust import TrustContext, is_untrusted
from solvio.logging_setup import get_logger
from solvio.resolver.inventory import CapabilityInventory, RuntimeInventory
from solvio.resolver.planner import AdaptivePlanner, Budget, Level, SolutionPath
from solvio.resolver.proposal import CapabilityProposal
from solvio.resolver.states import ResolverState
from solvio.resolver.taxonomy import (
    GapKind, classify, is_blocked, is_self_correctable, rules_for, was_declined,
)

log = get_logger("resolver")

#: Woertlich das, was Hermes liefert. Es aendert seinen Rang nicht dadurch, dass
#: es hilfreich war.
RESEARCH_TRUST = "untrusted_executor"

#: Wortstaemme, an denen ein Handlungsziel erkennbar ist. Zwei Lehren stecken
#: in der Form:
#:
#: * **Staemme, keine Vollformen.** „trage" fand „Trag mir einen Termin ein"
#:   nicht — der Imperativ hat kein -e, und damit lief die ganze Untersuchung
#:   nicht an.
#: * **Wortgrenze, kein Teilstring.** Ohne sie macht „Was betraegt die
#:   Temperatur?" den Stamm „trag" wahr, und jede Frage waere ein Auftrag.
_ACTION_STEMS = ("installier", "deinstallier", "richt", "einricht", "buch",
                 "bestell", "kauf", "hol", "lad", "schick", "send", "erstell",
                 "loesch", "lösch", "aender", "änder", "schalt", "start", "stopp",
                 "mach", "trag", "setz", "aktualisier", "update", "konfigurier",
                 "verbind", "spiel", "oeffne", "öffne", "leg", "bau")

_ACTION_RE = re.compile(r"\b(?:" + "|".join(_ACTION_STEMS) + r")", re.IGNORECASE)

#: Aus dem erkannten Verb einen brauchbaren Faehigkeitsnamen. „installier" ist
#: ein Wortstumpf; „install_package" ist ein Name, unter dem jemand etwas bauen
#: kann.
_VERB_NOUN = {"installier": "install_package", "deinstallier": "remove_package",
              "richt": "configure", "einricht": "configure",
              "aktualisier": "update_package", "update": "update_package",
              "start": "start_service", "stopp": "stop_service",
              "konfigurier": "configure"}

_STOPWORDS = {"mir", "mein", "meine", "meinem", "meinen", "auf", "der", "die",
              "das", "den", "dem", "ein", "eine", "einen", "und", "oder", "im",
              "in", "von", "zu", "fuer", "für", "bitte", "mal", "doch", "kannst",
              "du", "ich", "es", "ist", "sind", "wird", "werden", "nicht"}


def split_error(error: str) -> tuple[CapabilityOutcome | None, str]:
    """Zerlegt `"<ausgang>:<grund>"` — das Format, das die Bruecken erzeugen.

    Steht hier und nicht im Dispatcher: die Bedeutung eines Umschlags gehoert in
    die Schicht, die den Vertrag kennt. Der Dispatcher reicht eine Zeichenkette
    weiter und bleibt generisch.

    Was sich nicht zerlegen laesst, ergibt `None` — dann ist es kein Umschlag
    einer Faehigkeit, sondern eine Werkzeugmeldung, und dafuer gibt es nichts zu
    untersuchen. Zwei Ausnahmen sind ausdruecklich verdrahtet, weil genau sie
    „das kenne ich nicht" bedeuten.
    """
    if not error:
        return None, ""
    head, _, tail = error.partition(":")
    head = head.strip()
    if head in ("unknown_tool", "not_exposed_to_llm"):
        return CapabilityOutcome.REJECTED_BY_POLICY, "unknown_capability"
    try:
        return CapabilityOutcome(head), tail.strip()
    except ValueError:
        return None, ""


@dataclass
class Resolution:
    """Das Ergebnis einer Untersuchung — immer ein Ergebnis, nie eine Ausrede."""

    goal: str
    kind: GapKind
    state: ResolverState
    paths: list[SolutionPath] = field(default_factory=list)
    proposal: CapabilityProposal | None = None
    research: list[str] = field(default_factory=list)
    research_trust: str = ""
    open_questions: list[str] = field(default_factory=list)
    #: Konkrete Tatsachen, die dem Core fehlen — in Worten fuer einen Menschen.
    unknown_facts: list[str] = field(default_factory=list)
    #: Was das Fachteam beigetragen hat, wenn eines gefragt wurde.
    consultation: Any = None
    checked: list[str] = field(default_factory=list)
    elapsed: float = 0.0

    @property
    def best(self) -> SolutionPath | None:
        return self.paths[0] if self.paths else None

    def speak(self) -> str:
        """Ein Satz fuer den Menschen. Kein Fachbericht, wenn keiner verlangt ist."""
        best = self.best
        if self.state is ResolverState.HUMAN_ACTION_REQUIRED:
            if self.kind is GapKind.AUTHORITY_REQUIRED:
                return ("Ich kann das erledigen. Ich brauche nur deine Freigabe "
                        "auf dem iPhone.")
            step = best.human_step if best else "ein Schritt von dir"
            return f"Ich bin so weit — es fehlt nur noch: {step}."
        if self.state is ResolverState.SOLUTION_FOUND and best is None:
            # Kann nur eintreten, wenn ein Weg nachtraeglich verworfen wurde.
            # Dann ist „gefunden" die falsche Auskunft.
            return "Ich habe keinen Weg gefunden, der das sicher erreicht."
        if self.state is ResolverState.SOLUTION_FOUND and best is not None:
            if best.level is Level.EXISTING_CAPABILITY:
                return (f"Ich habe einen anderen Weg gefunden: "
                        f"Ich kann dafuer {best.capability} verwenden.")
            return f"Ich habe einen Weg gefunden: {best.summary}."
        if self.state is ResolverState.TEMPORARILY_BLOCKED:
            tail = (f" Ich koennte stattdessen {best.summary}."
                    if best is not None else "")
            return ("Der Weg ist vorhanden, aber gerade nicht erreichbar. "
                    "Ich versuche es spaeter noch einmal." + tail)
        if self.state is ResolverState.POLICY_LIMITED:
            tail = (f" Zulaessig waere: {best.summary}."
                    if best is not None else "")
            return ("Das lasse ich aus Sicherheitsgruenden nicht zu — daran "
                    "aendere ich auch nichts." + tail)
        if self.proposal is not None:
            p = self.proposal
            # Recherche wird ERWAEHNT, nicht zitiert. Sie kam aus einem
            # unvertrauten Ausfuehrenden; im Lauf, der diesen Satz veranlasst
            # hat, nannte sie ein Chrome-Paket fuer ARM64, das es so nicht gibt.
            # Ein solcher Satz darf nicht als Feststellung durchgereicht werden.
            looked = (" Ich habe dazu recherchiert; das Ergebnis liegt bei, ist "
                      "aber ungeprueft." if self.research else "")
            # Wenn Fachleute befragt wurden, gehoert das in die Antwort. Sonst
            # klingt ein sorgfaeltig abgewogener Vorschlag wie ein Schnellschuss.
            asked = ""
            consulted = getattr(self.consultation, "consulted", 0) if self.consultation else 0
            if consulted:
                roles = ", ".join(r.role for r in self.consultation.results if r.ok)
                asked = f" Ich habe {consulted} Fachmeinung(en) eingeholt ({roles})."
            missing = (f" Was ich dafuer noch wissen muss: {self.unknown_facts[0]}."
                       if self.unknown_facts else "")
            return (f"Ich kann es noch nicht sicher ausfuehren. Ich habe geprueft, "
                    f"was fehlt: {p.blocker}.{missing}{looked}{asked} Die passende "
                    f"Erweiterung waere eine kontrollierte Faehigkeit "
                    f"{p.capability_name} auf {p.target_executor} — mit deiner "
                    f"Freigabe fuer jede Systemaenderung.")
        if self.open_questions:
            return ("Ich habe keinen sicheren Weg gefunden. Offen ist noch: "
                    + "; ".join(self.open_questions[:2]) + ".")
        return "Ich habe keinen sicheren Weg gefunden, der das wirklich erreicht."

    def as_dict(self) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "ziel": self.goal, "art": self.kind.value, "zustand": self.state.value,
            "geprueft": self.checked,
            "wege": [p.as_dict() for p in self.paths],
            "antwort": self.speak(),
            "dauer_s": round(self.elapsed, 2),
        }
        if self.research:
            entry["recherche"] = self.research
            entry["recherche_trust"] = self.research_trust or RESEARCH_TRUST
        if self.open_questions:
            entry["offene_fragen"] = self.open_questions
        if self.unknown_facts:
            entry["fehlende_fakten"] = self.unknown_facts
        if self.proposal is not None:
            entry["vorschlag"] = self.proposal.as_dict()
        if self.consultation is not None:
            entry["beratung"] = self.consultation.as_dict()
        return entry


def _fold_in(proposal, consultation) -> None:
    """Uebernimmt Spezialistenwissen in den Vorschlag — ohne seine Autoritaet.

    Uebernommen werden Nebenwirkungen, verworfene Alternativen, Risikonotizen
    und Testideen: alles Dinge, die einen Vorschlag besser machen. NICHT
    uebernommen werden Semantik, Risiko und Freigabepflicht — die rechnet
    `validate()` weiterhin aus dem Vertrag, und eine Beratung aendert daran
    nichts. Deshalb wird hier auch nichts neu validiert: es gibt nichts, was
    eine erneute Rechnung veraendern koennte.
    """
    for result in consultation.results:
        if not result.usable:
            continue
        tag = f"[{result.role}, ungeprueft]"
        for note in result.risk_notes[:3]:
            proposal.side_effects.append(f"{tag} {note}"[:300])
        for rejected in result.rejected_alternatives[:3]:
            proposal.reusable_components.append(f"{tag} verworfen: {rejected}"[:300])
        for uncertainty in result.uncertainties[:2]:
            proposal.problems.append(f"{tag} offen: {uncertainty}"[:300])

def looks_actionable(text: str) -> bool:
    """Ob das ueberhaupt ein Handlungsziel war.

    Eine Faktenfrage bekommt keine Untersuchung. Der Auftrag ist ausdruecklich,
    NICHT jede gewoehnliche Fehlermeldung in eine Recherche zu verwandeln — das
    waere teuer, langsam und in den meisten Faellen sinnlos.
    """
    low = (text or "").strip().lower()
    if not low:
        return False
    if low.endswith("?") and not _ACTION_RE.search(low):
        return False
    return bool(_ACTION_RE.search(low))


def goal_terms(text: str, limit: int = 8) -> list[str]:
    words = [w.strip(".,!?;:„“\"'()").lower() for w in (text or "").split()]
    seen: list[str] = []
    for word in words:
        if len(word) > 3 and word not in _STOPWORDS and word not in seen:
            seen.append(word)
    return seen[:limit]


class GapResolver:
    """Untersucht eine Blockade und liefert den besten zulaessigen Weg."""

    def __init__(self, capabilities: CapabilityInventory,
                 runtimes: RuntimeInventory | None = None,
                 researcher: Callable[[str], Awaitable[dict]] | None = None,
                 budget: Budget | None = None,
                 team: Any = None) -> None:
        self.capabilities = capabilities
        self.runtimes = runtimes or RuntimeInventory()
        self.budget = budget or Budget()
        #: Eine Funktion, die EINE Frage beantwortet. Bewusst kein Hermes-Objekt:
        #: der Resolver soll nichts starten, abbrechen oder verwalten koennen.
        self.researcher = researcher
        #: Das Fachteam. Bleibt `None`, solange nichts verdrahtet ist — dann
        #: verhaelt sich der Resolver exakt wie in der vorigen Fassung.
        #: Eine Eskalation, keine Ersetzung.
        self.team = team
        self._research_used = 0
        self._active = False

    async def for_failed_tool(self, *, error: str, tool: str, goal: str,
                              trust: TrustContext | None = None,
                              local_only: bool = False) -> Resolution | None:
        """Der Einstieg aus dem Werkzeugpfad — nimmt den rohen Fehlertext.

        Damit bleibt die Zerlegung des Umschlags auf dieser Seite der Grenze.
        """
        outcome, reason = split_error(error)
        if outcome is None:
            return None
        return await self.resolve(goal=goal, outcome=outcome, reason=reason,
                                  capability=tool, trust=trust, local_only=local_only)

    async def resolve(self, *, goal: str, outcome: CapabilityOutcome | None,
                      reason: str = "", capability: str = "",
                      trust: TrustContext | None = None,
                      local_only: bool = False) -> Resolution | None:
        """Der ganze Ablauf. `None` heisst: hier gibt es nichts zu untersuchen.

        local_only ist eine Core-interne Einschraenkung fuer gebundene Tasks:
        Inventar und Vorschlag bleiben Information; weder die alte Recherche
        noch das Fachteam duerfen ausserhalb ihres TaskCostScope anlaufen.
        """
        if self._active:
            # Keine Untersuchung innerhalb einer Untersuchung.
            return None
        if not is_blocked(outcome):
            return None
        if is_self_correctable(reason):
            # Ein fehlendes Argument ist ein Aufruffehler, keine Luecke. Das
            # Schema lag vor; der naechste Versuch kann es richtig machen.
            log.info("resolver.self_correctable", reason=reason[:40])
            return None
        if was_declined(reason):
            # Ein Nein ist ein Ergebnis, kein Hindernis. Hier endet die Suche —
            # alles, was jetzt noch gefunden wuerde, waere der Weg daran vorbei.
            log.info("resolver.declined_no_search", reason=reason[:40])
            return None
        if trust is not None and (is_untrusted(trust.origin_trust)
                                  or not trust.user_authorized):
            # Der Auftrag kam nicht vom Nutzer. Entweder aus fremdem Inhalt —
            # E-Mail, Webseite, Portal, Hermes — oder vom Modell selbst.
            #
            # Die zweite Bedingung traegt eigenes Gewicht: `AGENT_GENERATED` gilt
            # im Trust-Vertrag nicht als „unvertraut", traegt aber genauso wenig
            # Autoritaet. Haengte der Schutz allein an der Herkunftsstufe,
            # koennte ein selbst ausgedachtes Ziel eine Untersuchung ausloesen —
            # und am Ende einen Aenderungsvorschlag, den nie ein Mensch wollte.
            log.info("resolver.untrusted_goal_ignored",
                     origin=trust.origin_trust.value,
                     authorized=trust.user_authorized)
            return None
        if not looks_actionable(goal):
            return None

        self._active = True
        started = time.monotonic()
        self._research_used = 0
        try:
            return await self._investigate(goal, outcome, reason, capability, started,
                                           local_only=local_only)
        finally:
            self._active = False

    async def _investigate(self, goal: str, outcome: CapabilityOutcome | None,
                           reason: str, capability: str,
                           started: float, *, local_only: bool = False) -> Resolution:
        kind = classify(outcome, reason)
        rules = rules_for(kind)
        planner = AdaptivePlanner(self.capabilities, self.runtimes, self.budget)
        checked = [f"Blockade eingestuft als {kind.value}",
                   f"{len(self.capabilities.names())} registrierte Faehigkeiten geprueft"]
        log.info("resolver.goal_received", kind=kind.value, capability=capability)

        # Menschliche Grenze: hier wird NICHT weitergesucht.
        if rules.needs_human and not rules.may_seek_alternative:
            log.info("resolver.human_boundary", kind=kind.value)
            return Resolution(goal=goal, kind=kind,
                              state=ResolverState.HUMAN_ACTION_REQUIRED,
                              paths=[planner.human_boundary(kind, capability=capability)],
                              checked=checked, elapsed=time.monotonic() - started)

        terms = goal_terms(goal)
        match = _ACTION_RE.search(goal or "")
        paths = planner.plan(kind, terms, action=match.group(0) if match else "",
                             blocked_capability=capability)
        checked.append(f"{len(paths)} zulaessige Alternativen gefunden")

        # Voruebergehende Stoerung: kein Grund, Code zu entwerfen.
        if rules.is_transient:
            # Und kein Grund, eine Schwester-Faehigkeit desselben Dienstes
            # anzubieten: wenn Home Assistant nicht erreichbar ist, ist
            # `ha_turn_off` genauso wenig erreichbar wie `ha_turn_on`. Ein
            # Alternativweg muss ueber einen ANDEREN Ausfuehrenden laufen, sonst
            # ist er keiner. (Der Lauf, der das zeigte, bot bei ausgefallenem HA
            # allen Ernstes an, das Licht stattdessen auszuschalten.)
            down = self._executor_of(capability)
            elsewhere = [p for p in paths if p.executor and p.executor != down]
            log.info("resolver.temporarily_blocked", kind=kind.value,
                     executor=down, alternatives=len(elsewhere))
            return Resolution(goal=goal, kind=kind,
                              state=ResolverState.TEMPORARILY_BLOCKED,
                              paths=elsewhere,
                              checked=checked + [
                                  f"Alternativen auf demselben Ausfuehrenden "
                                  f"({down or 'unbekannt'}) verworfen — der ist ja "
                                  f"der ausgefallene"],
                              elapsed=time.monotonic() - started)

        # Policy: nur regelkonforme Alternativen, nie ein Umweg um die Regel.
        if kind is GapKind.POLICY_HARD_STOP:
            # Nur klar passende Alternativen. Ein schwacher Treffer als
            # „zulaessig waere…" anzubieten ist schlimmer als keiner: er lenkt
            # von der Grenze ab und klingt nach einem Ausweg. Im Abnahmelauf
            # bot der schwache Weg `browser_back` an — fuer die Bitte, die
            # Router-Seite zu holen.
            strong = [p for p in paths if p.fidelity >= 0.9 and p.score >= 3]
            log.info("resolver.policy_limited", alternatives=len(strong))
            return Resolution(goal=goal, kind=kind,
                              state=ResolverState.POLICY_LIMITED, paths=strong,
                              checked=checked + ["Policy-Grenze bleibt unangetastet"],
                              elapsed=time.monotonic() - started)

        usable = [p for p in paths if p.executable_now and p.fidelity >= 0.75]
        # Ein Gleichstand ist kein Fund. Wenn zwei Wege gleich gut aussehen, hat
        # der Vergleich nichts entschieden — und „ich habe einen Weg gefunden"
        # waere dann geraten. Im Abnahmelauf standen so `ha_turn_on` und
        # `ha_turn_off` nebeneinander.
        if len(usable) > 1 and usable[0].fidelity <= usable[1].fidelity:
            log.info("resolver.no_clear_winner", candidates=len(usable))
            usable = []
        if usable:
            log.info("resolver.solution_found", capability=usable[0].capability)
            return Resolution(goal=goal, kind=kind,
                              state=ResolverState.SOLUTION_FOUND, paths=paths,
                              checked=checked, elapsed=time.monotonic() - started)

        if rules.needs_human:
            paths.append(planner.human_boundary(kind, capability=capability))
            return Resolution(goal=goal, kind=kind,
                              state=ResolverState.HUMAN_ACTION_REQUIRED, paths=paths,
                              checked=checked, elapsed=time.monotonic() - started)

        # Fakten fehlen. Genau hier — und nur hier — wird recherchiert.
        research: list[str] = []
        questions = self._open_questions(goal)
        asked = self._research_questions(goal)
        if not local_only and asked and self.researcher is not None:
            research = await self._research(asked[:self.budget.max_research])
            checked.append(f"{len(research)} Rechercheantwort(en) eingeholt "
                           f"(bleibt Information, entscheidet nichts)")

        if not rules.may_propose_capability:
            return Resolution(goal=goal, kind=kind,
                              state=ResolverState.CAPABILITY_GAP_IDENTIFIED,
                              paths=paths, research=research,
                              research_trust=RESEARCH_TRUST if research else "",
                              open_questions=questions, checked=checked,
                              elapsed=time.monotonic() - started)

        unknown = self._unknown_facts(goal)

        # Eskalation. Erst hier, und nur hier: an dieser Stelle wuerde sonst ein
        # Vorschlag entstehen, also die Empfehlung, etwas zu BAUEN. Genau davor
        # ist Widerspruch am wertvollsten — und genau hier ist er den Verbrauch
        # eines Abonnements wert.
        consultation = (await self._consult(goal, kind, paths, questions)
                        if not local_only else None)
        if consultation is not None and consultation.existing_capability:
            # Ein Spezialist hat eine vorhandene Faehigkeit empfohlen. Bevor der
            # ganze Vorschlag dafuer faellt, prueft der Core selbst nach — mit
            # denselben Zielbegriffen wie sonst auch, NICHT mit dem Namen der
            # Faehigkeit. Sonst waere die Pruefung zirkulaer: jeder Name findet
            # sich selbst.
            #
            # Dasselbe Prinzip wie beim Risiko: eine Behauptung wird nicht
            # geglaubt, sie wird nachgerechnet.
            named = consultation.existing_capability
            corroborated = planner.corroborates(
                named, terms, action=match.group(0) if match else "")
            confirmed = [corroborated] if corroborated is not None else []
            if confirmed:
                log.info("resolver.specialist_found_existing", capability=named)
                return Resolution(
                    goal=goal, kind=kind, state=ResolverState.SOLUTION_FOUND,
                    paths=confirmed, research=research,
                    research_trust=RESEARCH_TRUST if research else "",
                    checked=checked + [
                        f"ein Spezialist nannte {named}; der Core hat den "
                        f"Treffer bestaetigt"],
                    consultation=consultation,
                    elapsed=time.monotonic() - started)
            log.info("resolver.specialist_claim_unconfirmed", capability=named)
            checked.append(f"ein Spezialist nannte {named}, der Core konnte den "
                           f"Treffer aber nicht bestaetigen — der Vorschlag bleibt")
            consultation.existing_capability = ""
            # Die Abwaegung stammte aus der Beratung und ist damit ueberholt.
            # Sie stehen zu lassen hiesse, im Bericht eine Empfehlung zu fuehren,
            # der widersprochen wurde.
            consultation.synthesis = (
                f"{named} wurde genannt, passt nach eigener Pruefung aber nicht "
                f"auf dieses Ziel.")

        proposal = self.build_proposal(goal, kind, terms, research)
        if consultation is not None:
            _fold_in(proposal, consultation)
        gap = planner.gap_path(kind, proposal.capability_name, proposal.target_executor)
        if gap is not None:
            paths.append(gap)
        log.info("resolver.proposal_ready", capability=proposal.capability_name,
                 risk=proposal.risk, needs_approval=proposal.needs_approval)
        return Resolution(goal=goal, kind=kind, state=ResolverState.PROPOSAL_READY,
                          paths=paths, proposal=proposal, research=research,
                          research_trust=RESEARCH_TRUST if research else "",
                          open_questions=questions, unknown_facts=unknown,
                          checked=checked, consultation=consultation,
                          elapsed=time.monotonic() - started)

    def _open_questions(self, goal: str) -> list[str]:
        """Was fuer eine gute Entscheidung fehlt — aus dem Inventar, nicht geraten.

        Das sind Notizen fuer den Bericht, nicht Fragen an einen Rechercheur.
        Der Unterschied ist teuer erkauft: „pi-wohnzimmer: Erreichbarkeit nicht
        gemessen" an Hermes geschickt ergab eine ausfuehrliche Vermutung darueber,
        was `pi-wohnzimmer` wohl sei — fluessig, quellenreich und falsch. Eine
        interne Notiz ist keine Frage.
        """
        target = self._target_runtime(goal)
        notes = [q for q in self.runtimes.unknowns()
                 if target is None or q.startswith(target.key)]
        return notes[:6]

    def _research_questions(self, goal: str) -> list[str]:
        """Was ein Aussenstehender wirklich beantworten koennte."""
        target = self._target_runtime(goal)
        if target is None:
            return []
        missing = [a for a in ("os", "architektur") if a not in target.attributes]
        if not missing:
            return []
        return [
            (f"Ziel: {goal!r}. Zielsystem ist ein Geraet der Art "
             f"{target.attributes.get('rolle', target.kind)}. Welche "
             f"Betriebssysteme und CPU-Architekturen kommen dafuer in Frage?"),
            # Die zweite Frage ist die eigentlich interessante. Ein Nutzer nennt
            # ein Produkt („Chrome"); gemeint ist eine Funktion („ein Browser").
            # Ob beides auf dieser Plattform zusammenfaellt, ist eine Tatsache
            # und keine Auslegung — also wird sie erfragt, nicht angenommen.
            (f"Wird das im Ziel woertlich genannte Produkt fuer Linux auf ARM/arm64 "
             f"ueberhaupt offiziell angeboten? Falls nein: welches Paket erfuellt "
             f"dieselbe Funktion auf dieser Plattform, und wie heisst es genau? "
             f"Ziel war: {goal!r}"),
        ]

    def _unknown_facts(self, goal: str) -> list[str]:
        """Die konkreten Luecken am Zielsystem, als Satz.

        Das ist die ehrlichste Zeile des ganzen Berichts: der Core kennt vom
        Satelliten genau seine Kennung. Kein Betriebssystem, keine Architektur,
        keine Adresse. Wer daraus ein Paket ableitet, raet.
        """
        target = self._target_runtime(goal)
        if target is None:
            return []
        missing = [a for a in ("os", "architektur") if a not in target.attributes]
        if not missing:
            return []
        return [f"welches {' und welche '.join(missing)} {target.key} hat "
                f"(der Core kennt davon nur die Kennung)"]

    def _target_runtime(self, goal: str):
        """Welches Geraet dieser Satz meint — oder keines.

        Es gewinnt der laengste Treffer, nicht der erste. „mein Raspberry Pi"
        soll den Pi finden und nicht das Geraet, das zufaellig oben in der Liste
        steht.
        """
        best, best_len = None, 0
        for fact in self.runtimes.facts():
            if not fact.matches(goal):
                continue
            low = (goal or "").lower()
            length = max((len(c) for c in (fact.key.lower(), *(a.lower() for a in fact.aliases))
                          if c and c in low), default=0)
            if length > best_len:
                best, best_len = fact, length
        return best

    async def _research(self, questions: list[str]) -> list[str]:
        """Hoechstens `max_research` Fragen, und jede genau einmal."""
        answers: list[str] = []
        for question in questions:
            if self._research_used >= self.budget.max_research:
                break
            self._research_used += 1
            log.info("resolver.researching", n=self._research_used)
            try:
                reply = await self.researcher(question)
            except Exception as exc:  # noqa: BLE001
                log.info("resolver.research_failed", kind=type(exc).__name__)
                continue
            text = str((reply or {}).get("zusammenfassung", "")).strip()
            if text:
                # Bleibt Information. Der Rang aendert sich nicht dadurch, dass
                # die Antwort ueberzeugend klingt.
                answers.append(text[:600])
        return answers

    def build_proposal(self, goal: str, kind: GapKind, terms: list[str],
                       research: list[str]) -> CapabilityProposal:
        """Baut den Vorschlag — aus Core-Fakten, nicht aus Modelltext.

        Die Recherche darf den *Sachverhalt* liefern (welches Paket, welche
        Architektur). Sie liefert ausdruecklich nicht Risiko, Freigabepflicht
        oder Ausfuehrungsrecht: das rechnet `validate()` aus dem Vertrag.
        """
        target = self._target_runtime(goal)
        executor = target.key if target is not None else "mac-core"
        match = _ACTION_RE.search(goal or "")
        verb = match.group(0).lower() if match else "aktion"
        name = f"{executor.replace('-', '_')}_{_VERB_NOUN.get(verb, verb)}"[:48]
        return CapabilityProposal(
            original_goal=goal,
            underlying_goal=self._underlying_goal(goal),
            blocker=self._blocker_text(kind, executor),
            capability_name=name,
            target_executor=executor,
            semantics="NON_IDEMPOTENT_WRITE",
            required_inputs=terms[:5],
            reusable_components=[
                "Freigabeweg (iPhone/Face ID) aus Approval V1",
                "Capability Contract fuer Risiko und Semantik",
                "bestehende Ausfuehrer-Isolation",
            ],
            side_effects=["veraendert den Zustand des Zielsystems"],
            rollback="vor der Ausfuehrung den Ausgangszustand festhalten; "
                     "Rueckbau als eigener, ebenfalls freigabepflichtiger Schritt",
            trust_boundary="Recherche bleibt Information; das Ziel kommt vom Nutzer",
            tests_required=[
                "Freigabepflicht laesst sich nicht umgehen",
                "fremder Inhalt kann die Aktion nicht ausloesen",
                "Fehlschlag hinterlaesst keinen halben Zustand",
            ],
            human_setup="einmalige Einrichtung des Zugangs zum Zielsystem",
            implementation_route="regulaerer Entwicklungsweg: Vorschlag -> Umsetzung "
                                 "-> Tests -> Gate -> Release",
            claimed_risk="", claimed_needs_approval=None,
        ).validate()

    async def _consult(self, goal: str, kind: GapKind, paths: list[SolutionPath],
                       questions: list[str]):
        """Holt Rat — oder eben nicht. Die Politik entscheidet, nicht das Modell."""
        if self.team is None:
            return None
        from solvio.specialists.routing import Complexity, classify as route
        rules = rules_for(kind)
        level = route(kind, alternatives=len(paths),
                      would_propose=rules.may_propose_capability,
                      open_facts=len(questions))
        if level is Complexity.SIMPLE:
            log.info("specialists.not_needed", kind=kind.value)
            return None
        try:
            return await self.team.consult(
                goal=goal, blocker=self._blocker_text(kind, ""), level=level,
                capabilities=self.capabilities.as_dict(),
                runtimes=self.runtimes.as_dict(),
                known_names=set(self.capabilities.names()))
        except Exception as exc:  # noqa: BLE001 - Beratung darf nie das Ergebnis kippen
            log.error("specialists.consultation_failed", kind=type(exc).__name__)
            return None

    def _executor_of(self, capability: str) -> str:
        fact = self.capabilities.fact(capability) if capability else None
        return fact.executor if fact is not None else ""

    def _blocker_text(self, kind: GapKind, executor: str) -> str:
        """Warum es nicht geht — als Satz, nicht als Aufzaehlung von Enum-Namen.

        „keine registrierte Faehigkeit fuer capability_missing" war zwar wahr und
        sagte trotzdem nichts: der Grund wiederholte nur seine eigene Einstufung.
        """
        total = len(self.capabilities.names())
        writers = [f.name for f in self.capabilities.facts() if not f.read_only]
        if kind is GapKind.CAPABILITY_MISSING:
            return (f"von {total} freigegebenen Faehigkeiten veraendert keine ein "
                    f"fremdes System — {len(writers)} schreiben ueberhaupt, und alle "
                    f"nur in ihrem eigenen Dienst. Fuer {executor} gibt es keinen "
                    f"Ausfuehrenden")
        if kind is GapKind.DEPENDENCY_MISSING:
            return f"auf {executor} fehlt die noetige Software"
        if kind is GapKind.UNSUPPORTED_VARIANT:
            return "die Faehigkeit gibt es, diese Spielart beherrscht sie noch nicht"
        return f"kein Weg ueber die {total} freigegebenen Faehigkeiten"

    @staticmethod
    def _underlying_goal(goal: str) -> str:
        """Was der Nutzer wirklich will — vorsichtig, ohne Umdeutung.

        Es wird ausdruecklich nichts ersetzt. Eine sinnvolle Ersetzung („Chromium
        statt Chrome") gehoert in die Antwort, wo sie erklaert werden kann, nicht
        in eine stille Umschreibung des Auftrags.
        """
        return (goal or "").strip()
