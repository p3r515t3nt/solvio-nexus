"""Ob sich wirklich etwas geaendert hat — oder nur die Formulierung.

Eine Beobachtung, die bei jedem Nachsehen meldet, ist keine Hilfe, sondern
Laerm. Der naive Weg, den Text zweier Laeufe zu vergleichen, versagt in beide
Richtungen: ein Sprachmodell formuliert dieselbe Erkenntnis beim zweiten Mal
anders (falscher Alarm), und zwei verschiedene Termine koennen sich zufaellig
gleich lesen (verpasste Meldung).

Deshalb wird, wo es sie gibt, an **stabilen Kennungen** gemessen: Termin-IDs,
Nachrichten-IDs, Entitaet plus Zustand. Die aendern sich genau dann, wenn sich
etwas geaendert hat. Nur wo es keine gibt — bei Fliesstext aus einer Recherche —
wird auf einen normalisierten Textabdruck zurueckgegriffen, und der ist
ausdruecklich die schlechtere Wahl, nicht die bequemere.

Wichtig ist auch die Gegenrichtung: es darf nichts unterdrueckt werden, nur weil
die Form gleich blieb. Ein Termin, der von 9:00 auf 7:00 rutscht, hat dieselbe
Kennung und einen anderen Anfang — beides gehoert in den Abdruck.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

#: Schluessel, deren Werte etwas stabil bezeichnen. Bewusst eine Liste: was
#: hier fehlt, faellt in den Textabdruck zurueck und meldet dann eher zu viel
#: als zu wenig — die richtige Richtung fuer einen Fehler.
IDENTIFYING = ("id", "ids", "event_id", "message_id", "thread_id", "uid",
               "entity_id", "kennung", "start", "ende", "end", "beginn",
               "state", "zustand", "status", "titel", "title", "betreff",
               "subject", "von", "from", "absender", "datum", "date", "wert",
               "value", "helligkeit", "brightness")

#: Rauschen, das sich bei jedem Lauf aendert und nichts bedeutet.
VOLATILE = ("abgerufen", "fetched_at", "timestamp", "zeitstempel", "generated",
            "dauer", "elapsed", "call_id", "run_id", "task_id", "content_trust",
            "session", "cursor", "next_page", "etag")

MAX_PARTS = 200


def of(data: Any) -> str:
    """Der Abdruck eines Ergebnisses. Gleicher Abdruck = nichts Neues."""
    parts = sorted(set(_identifiers(data)))[:MAX_PARTS]
    if parts:
        payload = "\n".join(parts)
        return "id:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return "txt:" + _text_digest(data)


def _identifiers(node: Any, path: str = "") -> list[str]:
    """Sammelt Werte unter bezeichnenden Schluesseln, rekursiv."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            low = str(key).lower()
            if any(v in low for v in VOLATILE):
                continue
            if isinstance(value, (dict, list)):
                found.extend(_identifiers(value, f"{path}{low}."))
            elif any(low == name or low.endswith("_" + name)
                     for name in IDENTIFYING):
                found.append(f"{path}{low}={_scalar(value)}")
    elif isinstance(node, list):
        for entry in node[:100]:
            found.extend(_identifiers(entry, path))
    return found


def _scalar(value: Any) -> str:
    if isinstance(value, float):
        # Sekundenbruchteile sind Rauschen; ganze Sekunden sind Bedeutung.
        return str(int(value))
    return str(value)[:200]


#: Was beim Textabdruck als bedeutungslos gilt.
_WHITESPACE = re.compile(r"\s+")
_PUNCTUATION = re.compile(r"[^\wäöüß ]+", re.UNICODE)


def _text_digest(data: Any) -> str:
    """Ein normalisierter Abdruck von Fliesstext.

    Klein geschrieben, Satzzeichen weg, Leerraum vereinheitlicht — damit eine
    andere Zeilenbreite keine Neuigkeit ist. Mehr Normalisierung waere gefaehrlich:
    wer Zahlen oder Namen wegwirft, unterdrueckt genau das, worauf es ankommt.
    """
    if isinstance(data, str):
        text = data
    else:
        try:
            text = json.dumps(data, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            text = str(data)
    lowered = _PUNCTUATION.sub(" ", text.lower())
    normalized = _WHITESPACE.sub(" ", lowered).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


def changed(previous: str, current: str) -> bool:
    """Ob das eine Meldung wert ist. Ohne Vorgeschichte: ja."""
    return not previous or previous != current
