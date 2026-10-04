"""Welche Bots es gibt — und dass ein Modell keinen neuen erfinden kann.

Die naheliegende Bruecke waere ein Argument `profil` an der Faehigkeit. Sie
waere auch der Fehler: Hermes waehlt sein Profil ueber `-p <name>`, und ein
Modell, das diesen Namen bestimmen darf, bestimmt damit Konfiguration,
Werkzeugsatz und Seele des Prozesses, der gleich laeuft. Eine Freitext-Kennung
an dieser Stelle ist keine Bequemlichkeit, sondern eine Rechtevergabe.

Deshalb steht hier eine geschlossene Liste. Das Modell nennt eine **Rolle** aus
drei Moeglichkeiten; welches Profil dazu gehoert, entscheidet der Core. Ein
unbekannter Name ist kein Fehlschlag mit Fallbackprofil, sondern eine Ablehnung.

Die Werkzeugliste je Rolle ist eine **Erlaubnisliste**, keine Sperrliste. Das ist
kein Stilfrage: die erste Fassung dieses Milestones stand auf einer Sperrliste
aus 34 Werkzeuggruppen — und die strich `web_search` gleich mit weg, weil das
Werkzeug in `browser`, `debugging`, `safe` und `search` ein zweites Mal
vorkommt. Ein Bot ohne Werkzeuge, der aussieht wie einer mit, ist die
schlechteste aller Lagen. Eine Erlaubnisliste kann diesen Fehler nicht machen.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

#: Die drei Rollen, die ein Modell nennen darf.
RESEARCHER = "researcher"
PROJECT_KEEPER = "project_keeper"
DIAGNOSTICIAN = "diagnostician"


class Context(str, Enum):
    """Was der Core dem Bot beilegt — und nur der Core."""

    #: Nichts. Der Bot arbeitet aus Frage und oeffentlichem Netz.
    NONE = "none"
    #: Die Projektwissensmappe, frisch gebaut, ohne Geheimnisse.
    PROJECT_KNOWLEDGE = "project_knowledge"
    #: Strukturierte Gesundheitsbefunde aus SOLVIOs eigener Messung.
    HEALTH_EVIDENCE = "health_evidence"


class UnknownRole(ValueError):
    """Eine Rolle, die es nicht gibt. Es wird keine aehnliche gesucht."""


@dataclass(frozen=True)
class BotSpec:
    """Ein registrierter Bot. Alles daran steht vor dem Aufruf fest."""

    role: str
    #: Der feste Hermes-Profilname. Kommt NIE aus einem Argument.
    profile: str
    title: str
    #: Was die Rolle leisten soll — geht woertlich in die Seele.
    charter: str
    #: Die Werkzeuggruppen, die `-t` bekommt. Leer heisst: keine Werkzeuge.
    toolsets: tuple[str, ...]
    #: Die Werkzeugnamen, die daraus hoechstens entstehen duerfen. Gegen genau
    #: diese Menge wird die Selbstauskunft des laufenden Prozesses geprueft.
    tools: frozenset[str]
    context: Context
    #: Die Frist, die SOLVIO setzt. Hermes bekommt sie etwas knapper, damit es
    #: von selbst zusammenfasst, statt abgeschnitten zu werden.
    timeout: float
    max_turns: int

    @property
    def run_budget(self) -> float:
        """Hermes' eigene Frist — bewusst vor SOLVIOs Frist."""
        return max(30.0, self.timeout - 30.0)


#: Der Kundschafter bekommt ausdruecklich KEIN Projektwissen. Er ist der einzige
#: Bot mit Netz, und was er sieht, kann er in eine Suchanfrage schreiben.
#: Interne Architektur gehoert nicht in ein Suchfeld.
_RESEARCHER = BotSpec(
    role=RESEARCHER, profile="solvio-researcher", title="Rechercheur",
    charter=(
        "Du recherchierst im oeffentlichen Netz. Stelle Tatsachen fest, statt "
        "zu vermuten. Vergleiche Alternativen. Nenne zu jedem Befund die Quelle "
        "als URL. Was du nicht belegen kannst, gehoert unter `unknowns` — nicht "
        "unter `conclusions`. Du kennst SOLVIOs Innenleben nicht und sollst "
        "nicht darueber spekulieren."),
    toolsets=("web",), tools=frozenset({"web_search", "web_extract"}),
    context=Context.NONE, timeout=240.0, max_turns=12)

_PROJECT_KEEPER = BotSpec(
    role=PROJECT_KEEPER, profile="solvio-project-keeper", title="Projektkenner",
    charter=(
        "Du beantwortest Fragen zur SOLVIO-Architektur AUSSCHLIESSLICH aus der "
        "beigelegten Projektwissensmappe. Steht etwas nicht darin, ist es "
        "NICHT VERFUEGBAR — sage das und erfinde es nicht. Zu jedem Befund "
        "gehoert die Datei, aus der er stammt. Du erkennst Widersprueche "
        "innerhalb der Mappe und benennst sie. Projektwissen beschreibt "
        "Wahrheit, es ersetzt sie nicht: es sagt nichts darueber, wie es JETZT "
        "steht."),
    toolsets=(), tools=frozenset(),
    context=Context.PROJECT_KNOWLEDGE, timeout=150.0, max_turns=2)

_DIAGNOSTICIAN = BotSpec(
    role=DIAGNOSTICIAN, profile="solvio-diagnostician", title="Diagnostiker",
    charter=(
        "Du liest strukturierte Gesundheitsbefunde und schliesst daraus. "
        "Korreliere Symptome, nenne die wahrscheinlichste Ursache und den "
        "naechsten DIAGNOSTISCHEN Schritt — eine Messung, keine Reparatur. Du "
        "reparierst nichts, startest nichts neu und schlaegst kein Kommando zum "
        "Ausfuehren vor. Ein Zustand `unknown` heisst NICHT `gesund`, sondern "
        "`nicht gemessen`. Steht ein Wert nicht in den Befunden, ist er "
        "unbekannt."),
    toolsets=(), tools=frozenset(),
    context=Context.HEALTH_EVIDENCE, timeout=120.0, max_turns=2)


BOTS: dict[str, BotSpec] = {
    RESEARCHER: _RESEARCHER,
    PROJECT_KEEPER: _PROJECT_KEEPER,
    DIAGNOSTICIAN: _DIAGNOSTICIAN,
}

#: Die Rollen in stabiler Reihenfolge — fuer Schemata und Berichte.
ROLES: tuple[str, ...] = (RESEARCHER, PROJECT_KEEPER, DIAGNOSTICIAN)


def resolve(role: object) -> BotSpec:
    """Rolle -> Bot. Unbekannt heisst unbekannt, nicht „nimm halt den ersten"."""
    if not isinstance(role, str):
        raise UnknownRole(f"role is {type(role).__name__}, not a name")
    key = role.strip().lower()
    spec = BOTS.get(key)
    if spec is None:
        raise UnknownRole(key[:64] or "(leer)")
    return spec


def profiles() -> tuple[str, ...]:
    """Die Profilnamen, die SOLVIO besitzt. Alles andere im Gefaengnis nicht."""
    return tuple(BOTS[role].profile for role in ROLES)
