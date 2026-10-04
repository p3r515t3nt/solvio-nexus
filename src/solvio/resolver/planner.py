"""Wege vergleichen — und zwar nur solche, die es geben darf.

Der uebliche Entwurf fuer so etwas ist: alle denkbaren Wege erzeugen, dann die
unzulaessigen herausfiltern. Das ist hier ausdruecklich **nicht** gebaut, und der
Grund ist die Erfahrung mit Filtern: ein Filter ist eine Liste von Faellen, an die
jemand gedacht hat, und jeder neue Fall ist ein Loch, bis es auffaellt.

Stattdessen ist der Erzeuger eng. Ein Kandidat kann nur aus vier Quellen
entstehen:

1. einer **registrierten** Faehigkeit aus dem Inventar,
2. dem Weg **zur** menschlichen Grenze (Freigabe, Passwort, Hand ans Geraet),
3. einem reinen **Rechercheschritt**,
4. einem **Vorschlag** fuer etwas, das es noch nicht gibt.

Es gibt keine fuenfte Quelle. Es gibt insbesondere keine, die „wie komme ich an
der Freigabe vorbei" beantworten koennte — nicht weil das verboten waere,
sondern weil kein Code existiert, der so einen Kandidaten bilden wuerde. Das ist
der Unterschied zwischen einer Regel und einer Struktur.

Bei der Reihenfolge gilt: Sicherheit schlaegt Bequemlichkeit. Ein Weg wird nie
deshalb besser, weil er weniger Freigabe braucht — sondern nur, weil er das
eigentliche Ziel besser erreicht.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

from solvio.resolver.inventory import CapabilityFact, CapabilityInventory, RuntimeInventory
from solvio.resolver.taxonomy import GapKind, rules_for

#: Umlaute auf den GRUNDVOKAL falten, nicht auf die Ersatzschreibung.
#:
#: Der Unterschied hat hier gekostet: die Beschreibungen im Repo schreiben
#: „Traegt", der Nutzer sagt „Trag" — und `trag` ist in `traegt` kein Teilstring.
#: Deutsche Verben wechseln den Vokal beim Konjugieren (tragen -> traegt, laedt,
#: faehrt), und genau die Formen stehen in den Beschreibungen. Auf `a`/`o`/`u`
#: gefaltet passt beides wieder zusammen: `traegt` -> `tragt`.
#:
#: Die Faltung ist verlustbehaftet, aber sie trifft BEIDE Seiten gleich — und
#: das ist alles, was ein Teilstring-Vergleich braucht.
_UMLAUT = str.maketrans({"ä": "a", "ö": "o", "ü": "u", "ß": "s",
                         "Ä": "a", "Ö": "o", "Ü": "u"})
_DIGRAPHS = (("ae", "a"), ("oe", "o"), ("ue", "u"))


def _fold(text: str) -> str:
    folded = (text or "").translate(_UMLAUT).lower()
    for digraph, vowel in _DIGRAPHS:
        folded = folded.replace(digraph, vowel)
    return folded


#: Ab wann ein Treffer ueberhaupt als Weg gilt. Zwei Punkte heissen: entweder das
#: Verb hat gepasst (zaehlt doppelt) oder mindestens zwei inhaltliche Begriffe.
#: Ein einzelnes gemeinsames Substantiv ist zu wenig — „Seite" steht in jeder
#: Browser-Beschreibung.
MIN_SCORE = 2


class Level(IntEnum):
    """Die Loesungsleiter. Niedriger ist naeher an dem, was schon da ist."""

    EXISTING_CAPABILITY = 1     # eine andere freigegebene Faehigkeit kann es
    OTHER_EXECUTION_PATH = 2    # anderes Geraet, andere Laufzeit
    HUMAN_STEP = 3              # es fehlt nur ein Mensch
    RESEARCH = 4                # es fehlen Fakten
    CAPABILITY_GAP = 5          # es fehlt wirklich etwas


#: Was eine Planung kosten darf. „Immer eine Loesung suchen" ohne Grenzen ist
#: eine Endlosschleife mit gutem Gewissen.
@dataclass(frozen=True)
class Budget:
    max_candidates: int = 8
    max_research: int = 2
    max_deep_tasks: int = 1
    seconds: float = 300.0

    def deadline(self) -> float:
        return time.monotonic() + self.seconds


@dataclass
class SolutionPath:
    """Ein moeglicher Weg — mit allem, was fuer den Vergleich noetig ist."""

    level: Level
    summary: str
    achieves: str
    capability: str = ""
    executor: str = ""
    requires_authority: bool = False
    human_step: str = ""
    dependencies: list[str] = field(default_factory=list)
    side_effects: list[str] = field(default_factory=list)
    reversible: bool = True
    executable_now: bool = False
    #: Wie gut dieser Weg das EIGENTLICHE Ziel trifft — RELATIV zum besten
    #: Kandidaten. Gut zum Sortieren, untauglich als Schwelle: der Beste ist
    #: immer 1.0, auch wenn er kaum passt.
    fidelity: float = 1.0
    #: Das absolute Mass: wie viele Signale wirklich getroffen haben (Verb zaehlt
    #: doppelt). Ohne dieses Feld bot der Resolver auf die Bitte, die
    #: Router-Seite zu holen, allen Ernstes `browser_back` als „zulaessigen Weg"
    #: an — es war der beste von mehreren schwachen und damit rechnerisch 1.0.
    score: int = 0
    why: str = ""
    risk: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"level": int(self.level), "stufe": self.level.name,
                "zusammenfassung": self.summary, "erreicht": self.achieves,
                "faehigkeit": self.capability, "ausfuehrer": self.executor,
                "braucht_freigabe": self.requires_authority,
                "menschlicher_schritt": self.human_step,
                "abhaengigkeiten": self.dependencies,
                "nebenwirkungen": self.side_effects,
                "umkehrbar": self.reversible,
                "jetzt_ausfuehrbar": self.executable_now,
                "zieltreue": self.fidelity, "trefferpunkte": self.score,
                "risiko": self.risk,
                "begruendung": self.why}


def rank(paths: list[SolutionPath]) -> list[SolutionPath]:
    """Sortiert die Wege — nach Nutzen, nicht nach Bequemlichkeit.

    Die Reihenfolge der Schluessel ist die eigentliche Aussage:

    1. **Zieltreue zuerst.** Ein Weg, der etwas anderes tut, gewinnt nicht, nur
       weil er sofort ginge. Sonst waere „ich mache stattdessen etwas
       Aehnliches" die beste Antwort auf jede schwierige Bitte.
    2. Dann, ob er heute geht.
    3. Dann die Stufe: was schon existiert, vor dem, was gebaut werden muesste.
    4. Umkehrbares vor Unumkehrbarem.

    Freigabepflicht steht bewusst NICHT in diesem Schluessel. Ein Weg wird nicht
    schlechter, weil er den Menschen fragt.
    """
    return sorted(paths, key=lambda p: (round(-p.fidelity, 3),
                                        not p.executable_now,
                                        int(p.level),
                                        not p.reversible,
                                        p.summary))


class AdaptivePlanner:
    """Baut und bewertet zulaessige Wege — beschraenkt und ohne Rekursion."""

    def __init__(self, capabilities: CapabilityInventory,
                 runtimes: RuntimeInventory | None = None,
                 budget: Budget | None = None) -> None:
        self.capabilities = capabilities
        self.runtimes = runtimes or RuntimeInventory()
        self.budget = budget or Budget()
        #: Verhindert, dass eine Planung eine Planung ausloest. Eine
        #: Selbstaufruf-Kette waere hier besonders unangenehm, weil jeder Schritt
        #: plausibel aussieht.
        self._planning = False

    # -- Quelle 1: eine registrierte Faehigkeit ------------------------------
    def from_capabilities(self, goal_terms: list[str], *, action: str = "",
                          exclude: str = "") -> list[SolutionPath]:
        """Wege aus dem, was es schon gibt.

        Es wird ausschliesslich ueber das Inventar iteriert. Ein Name, der dort
        nicht steht, kann hier nicht entstehen — das ist die Absicherung gegen
        erfundene Faehigkeiten.

        Das **Verb** zaehlt doppelt, und das ist keine Feinjustierung: „Trag mir
        einen Termin ein" traf sonst alle sieben Kalender-Faehigkeiten mit
        derselben Punktzahl, weil nur „Termin" passte. Unterschieden werden sie
        erst durch das, was sie TUN — und das steht als Verb in ihrer
        Beschreibung („Traegt einen Termin ein", „Sagt einen Termin ab").

        Die Zieltreue wird am besten Kandidaten normiert, nicht an der Satzlaenge.
        Sonst sinkt sie mit jedem Fuellwort, und ein voellig passender Weg faellt
        unter die Schwelle, nur weil der Nutzer hoeflich formuliert hat.
        """
        wanted = [t for t in (_fold(w) for w in goal_terms) if len(t) > 2]
        stem = _fold(action)
        scored: list[tuple[CapabilityFact, int]] = []
        for fact in self.capabilities.facts():
            if fact.name == exclude:
                continue
            haystack = _fold(f"{fact.name} {fact.description} {fact.executor}")
            score = sum(1 for term in wanted if term in haystack)
            if stem and stem in haystack:
                score += 2
            if score:
                scored.append((fact, score))
        if not scored:
            return []
        strong = [(fact, score) for fact, score in scored if score >= MIN_SCORE]
        if not strong:
            return []
        best = max(score for _fact, score in strong)
        found = [self._path_for(fact, score, best) for fact, score in strong]
        return sorted(found, key=lambda p: (-p.fidelity, -p.score))[
            :self.budget.max_candidates]

    def corroborates(self, name: str, goal_terms: list[str], *,
                     action: str = "") -> SolutionPath | None:
        """Passt eine BENANNTE Faehigkeit zum Ziel? Niedrigere Schwelle, mit Grund.

        Eine blinde Suche braucht eine hohe Schwelle, sonst schlaegt jedes
        gemeinsame Substantiv an. Eine Kandidatin, die jemand ausdruecklich
        genannt hat, braucht das nicht — hier geht es nur noch darum, ob die
        Nennung ueberhaupt etwas mit dem Ziel zu tun hat.

        Was auch hier NICHT passiert: eine Nennung ohne jede Ueberschneidung
        durchzuwinken. Im echten Lauf nannte ein Berater `browser_open` fuer
        „installiere Chrome auf dem Pi"; die Beschreibung („oeffnet eine
        oeffentliche Webseite") hat mit dem Ziel keinen Begriff gemeinsam, und
        genau deshalb wird sie verworfen.
        """
        fact = self.capabilities.fact(name)
        if fact is None:
            return None
        wanted = [t for t in (_fold(w) for w in goal_terms) if len(t) > 2]
        haystack = _fold(f"{fact.name} {fact.description} {fact.executor}")
        score = sum(1 for term in wanted if term in haystack)
        stem = _fold(action)
        if stem and stem in haystack:
            score += 2
        if score < 1:
            return None
        return self._path_for(fact, score, max(score, 1))

    def _path_for(self, fact: CapabilityFact, hits: int, total: int) -> SolutionPath:
        return SolutionPath(
            level=Level.EXISTING_CAPABILITY,
            summary=f"{fact.name} verwenden",
            achieves=fact.description or fact.name,
            capability=fact.name,
            executor=fact.executor,
            requires_authority=fact.needs_approval,
            # `available is None` heisst „nicht gemessen" — und das ist bewusst
            # NICHT dasselbe wie ausfuehrbar. Wer ungemessen als verfuegbar
            # zaehlt, empfiehlt Wege ins Leere.
            executable_now=fact.available is True,
            reversible=fact.read_only or fact.semantics != "NON_IDEMPOTENT_WRITE",
            fidelity=round(min(1.0, 0.5 + 0.5 * hits / max(1, total)), 3),
            score=hits,
            side_effects=[] if fact.read_only else ["verändert Daten im Zieldienst"],
            risk=fact.base_risk,
            why=f"bereits freigegeben, {'lesend' if fact.read_only else 'schreibend'}")

    # -- Quelle 2: die menschliche Grenze ------------------------------------
    def human_boundary(self, kind: GapKind, *, capability: str = "",
                       detail: str = "") -> SolutionPath:
        """Der kuerzeste zulaessige Weg ZU der Grenze — nicht um sie herum.

        Fuer `AUTHORITY_REQUIRED` und `HUMAN_ACTION_REQUIRED` ist das die ganze
        Antwort. Der Planer sucht hier nichts weiter; es gibt nichts zu suchen,
        ausser dem Menschen Bescheid zu sagen.
        """
        authority = kind is GapKind.AUTHORITY_REQUIRED
        return SolutionPath(
            level=Level.HUMAN_STEP,
            summary=("Freigabe auf dem iPhone" if authority
                     else detail or "ein Schritt von dir"),
            achieves="genau das ursprüngliche Ziel",
            capability=capability,
            requires_authority=authority,
            human_step=("Face ID" if authority else detail or "manuelle Handlung"),
            executable_now=True,
            fidelity=1.0,
            why=("die Fähigkeit ist da und wartet nur auf deine Zustimmung"
                 if authority else "nur ein Mensch kann diesen Schritt tun"))

    # -- Quelle 3: Recherche --------------------------------------------------
    def research_step(self, question: str) -> SolutionPath:
        return SolutionPath(
            level=Level.RESEARCH,
            summary="nachsehen, bevor entschieden wird",
            achieves="die fehlenden Fakten",
            executable_now=True,
            fidelity=0.4,
            why=question[:200])

    # -- Quelle 4: die echte Luecke ------------------------------------------
    def gap_path(self, kind: GapKind, capability_name: str,
                 executor: str) -> SolutionPath | None:
        if not rules_for(kind).may_propose_capability:
            return None
        return SolutionPath(
            level=Level.CAPABILITY_GAP,
            summary=f"neue Fähigkeit {capability_name}",
            achieves="das Ziel, sobald sie gebaut und freigegeben ist",
            capability=capability_name,
            executor=executor,
            requires_authority=True,
            executable_now=False,
            reversible=False,
            fidelity=1.0,
            why="es gibt heute keinen Ausführenden dafür")

    # -- Zusammenfuehren ------------------------------------------------------
    def plan(self, kind: GapKind, goal_terms: list[str], *, action: str = "",
             blocked_capability: str = "") -> list[SolutionPath]:
        """Die zulaessigen Wege, sortiert. Immer beschraenkt, nie rekursiv."""
        if self._planning:
            # Eine Planung in einer Planung. Es gibt keinen Fall, in dem das die
            # richtige Antwort waere, und viele, in denen es teuer endet.
            return []
        self._planning = True
        try:
            rules = rules_for(kind)
            if not rules.may_seek_alternative:
                # AUTHORITY_REQUIRED und HUMAN_ACTION_REQUIRED landen hier. Es
                # wird ausdruecklich NICHT nach einem anderen Weg gesucht: der
                # andere Weg waere per Definition der an der Zustimmung vorbei.
                return [self.human_boundary(kind, capability=blocked_capability)]
            paths = self.from_capabilities(goal_terms, action=action,
                                           exclude=blocked_capability)
            if rules.needs_human:
                paths.append(self.human_boundary(kind, capability=blocked_capability))
            return rank(paths)[:self.budget.max_candidates]
        finally:
            self._planning = False
