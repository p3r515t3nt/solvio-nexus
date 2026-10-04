"""Fremden Text entwaffnen — an einer Stelle, fuer alle Quellen.

Eine E-Mail, ein Kalendertitel, ein Suchtreffer, eine Webseite: verschiedene
Wege, dieselbe Frage. Kann Text, den jemand anderes geschrieben hat, sich als
Anweisung ausgeben? Die Antwort gehoert nicht in jede Faehigkeit einzeln —
sonst driften die Regeln auseinander und die schwaechste gewinnt.

Was hier passiert, ist bewusst wenig: unsichtbare Zeichen und Bidi-Steuerung
verschwinden, gefaelschte Rollenmarker und Freigabe-Behauptungen werden sichtbar
markiert statt geloescht. Der Text bleibt lesbar; er verliert nur die
Faehigkeit, sich als etwas anderes auszugeben.

Was hier NICHT passiert: aus fremdem Text vertrauenswuerdigen machen. Das kann
keine Filterfunktion, und so zu tun waere gefaehrlicher als es zu lassen. Die
Grenze, die traegt, ist strukturell — der Text landet in einem Datenfeld, und
Autoritaet entsteht ausschliesslich aus dem TrustContext des Turns.
"""
from __future__ import annotations

import re
from typing import Any

#: Grenze fuer einen einzelnen uebernommenen Textblock.
MAX_TEXT = 4000

#: Unsichtbare Zeichen: sie stehen im Text, aber nicht auf dem Bildschirm — der
#: klassische Weg, einem Menschen etwas anderes zu zeigen als dem Modell.
_INVISIBLE = re.compile(r"[​-‏⁠-⁤﻿­]")

#: Bidi-Steuerzeichen kehren die Leserichtung um; eine Zeile kann damit anders
#: aussehen, als sie ist.
_BIDI = re.compile(r"[‪-‮⁦-⁩]")

#: Rollenmarker. Kein Modellformat der Welt darf aus einem Suchtreffer oder einer
#: Webseite kommen — taucht so etwas auf, ist es ein Versuch, die Struktur zu
#: faelschen.
_ROLE_MARKER = re.compile(
    r"(?im)^\s*(?:#{0,3}\s*)?(?:\[{0,2})\s*"
    r"(system|developer|assistant|user|tool)\s*(?:\]{0,2})\s*[:>]",
)

#: Sonderformen derselben Faelschung aus verschiedenen Modellfamilien.
_DELIMITER = re.compile(
    r"(?i)<\|(?:im_start|im_end|endoftext|system|user|assistant|channel|start|end)\|>"
    r"|<\/?(?:system|developer)>"
    r"|\[/?INST\]|<<SYS>>|<\/?s>",
)

#: Behauptungen ueber den eigenen Freigabezustand. Ein Werkzeugergebnis oder eine
#: Webseite kann nicht wissen, ob der Nutzer etwas freigegeben hat — sie koennen
#: es nur behaupten.
_AUTHORITY_CLAIM = re.compile(
    r"(?i)(approved by (?:the )?(?:user|owner|gregor)"
    r"|approval (?:granted|confirmed|complete)"
    r"|user (?:has )?(?:already )?(?:authoriz|authoris)ed"
    r"|no approval (?:is )?(?:required|needed)"
    # Deutsch: der Sprecher kann vorne, hinten oder gar nicht stehen, und
    # dazwischen liegt beliebiges Fuellwort. Eine Wortgrenze allein reicht
    # hier nicht — „Der Nutzer hat das freigegeben" hat keine.
    r"|(?:der |vom |durch den )?nutzer\s+(?:hat|hatte)?[^.\n]{0,30}?"
    r"(?:freigegeben|genehmigt|bestaetigt|bestätigt|autorisiert|erlaubt)"
    r"|freigabe (?:erteilt|liegt vor|wurde erteilt)"
    r"|(?:ist|wurde) (?:bereits )?(?:freigegeben|genehmigt|autorisiert)"
    r"|keine freigabe (?:noetig|nötig|erforderlich))",
)

REPLACEMENT = "[neutralisiert]"


def neutralize(value: Any, *, limit: int = MAX_TEXT) -> str:
    """Nimmt fremdem Text die Verkleidung — und nur die."""
    text = value if isinstance(value, str) else str(value)
    text = _INVISIBLE.sub("", text)
    text = _BIDI.sub("", text)
    text = _DELIMITER.sub(REPLACEMENT, text)
    text = _ROLE_MARKER.sub(REPLACEMENT + " ", text)
    text = _AUTHORITY_CLAIM.sub(REPLACEMENT, text)
    if len(text) > limit:
        text = text[:limit] + " […]"
    return text


def as_information(value: Any, trust: str, *, limit: int = MAX_TEXT) -> dict[str, Any]:
    """Verpackt fremde Ausgabe so, dass ihre Herkunft mitreist.

    Nie ein nackter String: ein nackter String kann sich im naechsten Prompt
    nahtlos an eine Anweisung anschmiegen. Ein Feld mit `content_trust` daneben
    kann das nicht.
    """
    return {"text": neutralize(value, limit=limit), "content_trust": trust}
