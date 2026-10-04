"""Wann ueberhaupt jemand gefragt wird — und wie viele.

Der teuerste Fehler waere, bei jeder Bitte drei Berater zu wecken. Das kostet
Kontingent, das dem Nutzer gehoert, und liefert bei den meisten Blockaden nichts,
was der Resolver nicht schon weiss.

Deshalb ist die Voreinstellung **niemand**. Ein Berater kommt hinzu, wenn
Tatsachen fehlen; alle drei nur, wenn wirklich mehrere Wege denkbar sind und die
Entscheidung schwer rueckgaengig zu machen waere.

Die Politik liest ausschliesslich Core-eigene Angaben: die Art der Blockade, ob
Alternativen gefunden wurden, ob ein Vorschlag entstuende. Kein Modell darf die
Stufe waehlen — sonst waere die bequemste Antwort immer „das ist komplex".
"""
from __future__ import annotations

from enum import IntEnum

from solvio.resolver.taxonomy import GapKind, rules_for


class Complexity(IntEnum):
    SIMPLE = 0      # niemand
    MEDIUM = 1      # einer
    COMPLEX = 2     # bis zu drei


#: Die Obergrenze. Fest, nicht einstellbar, und ausdruecklich nicht vom Modell
#: erreichbar: es gibt keinen Parameter „spawn so viele wie noetig".
MAX_SPECIALISTS = 3

#: Eine Runde. Kein Berater darf einen weiteren Berater rufen.
MAX_ROUNDS = 1


def classify(kind: GapKind, *, alternatives: int, would_propose: bool,
             open_facts: int) -> Complexity:
    """Wie viel Beratung diese Blockade rechtfertigt.

    Die Reihenfolge der Pruefungen ist die Aussage:

    1. Wo ein Mensch entscheidet, wird niemand gefragt. Ein Berater koennte
       dort nur eines beitragen — einen Weg an dem Menschen vorbei.
    2. Eine Stoerung geht vorbei. Beratung aendert daran nichts.
    3. Gibt es bereits einen klaren Weg, ist die Frage beantwortet.
    4. Erst wenn wirklich etwas fehlt und die Folgen schwer umkehrbar waeren,
       lohnt sich der volle Blickwinkel.
    """
    rules = rules_for(kind)
    if rules.needs_human and not rules.may_seek_alternative:
        return Complexity.SIMPLE
    if rules.is_transient:
        return Complexity.SIMPLE
    if kind is GapKind.POLICY_HARD_STOP:
        # Die Grenze steht fest. Ein Berater duerfte hier nur nach einem
        # regelkonformen Ersatz suchen — und den findet der Resolver selbst.
        return Complexity.SIMPLE
    if alternatives > 0 and not would_propose:
        return Complexity.SIMPLE
    if would_propose:
        # Hier entstuende sonst ein Aenderungsvorschlag — also eine Empfehlung,
        # etwas zu bauen. Genau davor lohnt sich Widerspruch am meisten.
        return Complexity.COMPLEX
    if open_facts > 0:
        return Complexity.MEDIUM
    return Complexity.SIMPLE


def team_size(level: Complexity) -> int:
    return {Complexity.SIMPLE: 0, Complexity.MEDIUM: 1,
            Complexity.COMPLEX: MAX_SPECIALISTS}[level]
