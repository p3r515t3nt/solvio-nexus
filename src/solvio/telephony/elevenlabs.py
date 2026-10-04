"""Was SOLVIO ueber den Telefonie-Anbieter weiss — und was es ihm erlaubt.

Hier stehen ausschliesslich **Konstanten und Vertragswissen**. Kein Geheimnis,
kein Netzverkehr, keine Entscheidung. Das Modul, das den Schluessel einsetzt,
ist `solvio.telephony.upstream` und nur dieses — so steht es auch in
`EXECUTOR_MODULES`.

Warum die Pfadliste geschlossen ist: ein Adapter, der einen beliebigen
Anbieterpfad waehlen darf, ist ein Proxy fuer die ganze Anbieter-API. Ein
Modell, das die Faehigkeit ruft, koennte dann ueber denselben Schluessel
Stimmen loeschen oder Rechnungsdaten lesen. Die Faehigkeit `telephony_call`
darf genau eine Sache: anrufen. Und sie darf danach genau eine Sache: das
Ergebnis lesen.

Die Aufteilung in LAUFZEIT und EINRICHTUNG ist Absicht. Einen Agenten anlegen
oder eine Rufnummer importieren ist eine Einrichtungshandlung des Eigentuemers,
keine Laufzeitfaehigkeit — sie steht deshalb in einer eigenen Liste, die der
Laufzeitpfad nie benutzt.
"""
from __future__ import annotations

import re

#: Die Gegenstelle. Ein Ziel, und der Tresor bindet den Schluessel darauf.
UPSTREAM_ORIGIN = "https://api.elevenlabs.io"
UPSTREAM_HOST = "api.elevenlabs.io"

#: Der Kopfsatz, der den Schluessel traegt. Kleingeschrieben, mit Bindestrichen —
#: gemessen an der OpenAPI-Spezifikation des Anbieters, nicht aus einem Beispiel
#: abgeschrieben. KEIN `Authorization: Bearer`.
AUTH_HEADER = "xi-api-key"

#: Wo der Schluessel liegt. Der Wert steht hier nie, nur die Adresse.
SECRET_REF = "secret://elevenlabs/agents-api-key"

#: Welche SOLVIO-Faehigkeiten diesen Zugang ausleihen duerfen. Bewusst
#: aufgezaehlt statt praefixgematcht: eine kuenftige Faehigkeit soll hier
#: sichtbar dazukommen muessen, nicht stillschweigend hineinrutschen.
#:
#: Die Lehre aus Secret Vault Rescope V1 steht dahinter: dort brauchte ein
#: zweiter Zugriffsweg dieselbe Faehigkeit, und weil nur einer eingetragen war,
#: scheiterte er erst im Betrieb. Ein Anruf hat DREI Wege — starten, Ergebnis
#: holen, vorab pruefen — und alle drei stehen deshalb von Anfang an hier.
CAPABILITY_CALL = "telephony_call"
CAPABILITY_RESULT = "telephony_call_result"
CAPABILITY_PREFLIGHT = "telephony_preflight"
CAPABILITY_SETUP = "telephony_setup"

ALLOWED_CAPABILITIES = (
    CAPABILITY_CALL,
    CAPABILITY_RESULT,
    CAPABILITY_PREFLIGHT,
    CAPABILITY_SETUP,
)

#: LAUFZEIT — was `telephony_call` und der Ergebnisabgleich anfassen duerfen.
#: Jeder Eintrag ist (Methode, Pfadmuster). `{}` steht fuer genau ein Segment
#: ohne Schraegstrich; ein Pfad mit einem weiteren Segment passt NICHT.
RUNTIME_PATHS: tuple[tuple[str, str], ...] = (
    ("POST", "/v1/convai/twilio/outbound-call"),
    ("GET", "/v1/convai/conversations/{}"),
)

#: PREFLIGHT — nur lesen, und nur das, was die Einsatzbereitschaft belegt.
PREFLIGHT_PATHS: tuple[tuple[str, str], ...] = (
    ("GET", "/v1/convai/agents"),
    # Der Agent im Einzelnen — noetig, um zu pruefen, ob die freigegebene
    # Hoechstdauer am Agenten ueberhaupt uebernommen werden darf und ob sonst
    # eine Ueberschreibung offensteht. Lesend, wie alles in dieser Liste.
    ("GET", "/v1/convai/agents/{}"),
    ("GET", "/v1/convai/phone-numbers"),
    ("GET", "/v1/convai/phone-numbers/{}"),
)

#: EINRICHTUNG — ausdruecklich NICHT Teil der Laufzeitflaeche. Diese Pfade
#: erreicht nur ein Einrichtungsskript, das der Eigentuemer selbst startet.
SETUP_PATHS: tuple[tuple[str, str], ...] = (
    ("POST", "/v1/convai/agents/create"),
    ("PATCH", "/v1/convai/agents/{}"),
    # Lesen, was man aendert — vorher und nachher. Ohne diesen Eintrag koennte
    # ein Einrichtungsskript seine eigene Aenderung nicht gegenpruefen, und
    # genau die Gegenprobe ist der Zweck: nach dem Schreiben muss belegt sein,
    # dass GENAU EIN Schalter offen ist und kein zweiter mitgekippt wurde.
    ("GET", "/v1/convai/agents/{}"),
    ("POST", "/v1/convai/phone-numbers"),
)


#: Was als Kennungssegment durchgeht. Bewusst eine ERLAUBTE Menge statt einer
#: Liste verbotener Zeichen: die Verbotsliste hatte `.` und `..` und `%`, und
#: `..\\..\\user`, `..;` und Vollbreitenpunkte kamen trotzdem durch. Nicht
#: ausnutzbar — der Host blieb derselbe und die Bibliothek kuerzte nichts —
#: aber eine Liste, die man erweitern muss, sobald jemand ein neues Zeichen
#: findet, ist die falsche Art von Liste.
_IDENT = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _matches(pattern: str, path: str) -> bool:
    """Segmentweiser Vergleich. `{}` deckt genau EIN Segment ab.

    Kein regulaerer Ausdruck und keine Praefixpruefung: `startswith` haette
    `/v1/convai/agents` auch auf `/v1/convai/agents/xyz/delete` passen lassen,
    und genau solche Erweiterungen sind der Grund fuer diese Liste.
    """
    erwartet = pattern.split("/")
    tatsaechlich = path.split("/")
    if len(erwartet) != len(tatsaechlich):
        return False
    for teil, wert in zip(erwartet, tatsaechlich):
        if teil == "{}":
            # Ein Kennungssegment ist eine Kennung — kein Wegstueck.
            #
            # `..` erfuellte die Bedingung "nichtleer, kein Schraegstrich" und
            # kam durch; die HTTP-Bibliothek verkuerzte den Pfad danach auf
            # `/v1/convai/`, also auf etwas, das die Liste nie erlaubt hat.
            # Dasselbe gilt kodiert (`%2e%2e`), deshalb faellt hier auch jedes
            # Prozentzeichen.
            if not _IDENT.match(wert):
                return False
            continue
        if teil != wert:
            return False
    return True


def path_allowed(method: str, path: str, *, scope: str) -> bool:
    """Darf dieser Aufruf raus? Vorgabe NEIN.

    `scope` ist eine der Faehigkeiten oben. Ein unbekannter Bereich, eine
    unbekannte Methode oder ein Pfad mit Abfrageteil kommen nicht durch — der
    Abfrageteil gehoert in die Parameter des Aufrufs, nicht in den Pfad, und
    ein Pfad mit `?` waere der einfachste Weg, diese Pruefung zu umgehen.
    """
    if not method or not path or "?" in path or "#" in path:
        return False
    method = method.upper()
    if scope == CAPABILITY_CALL:
        erlaubt = (RUNTIME_PATHS[0],)
    elif scope == CAPABILITY_RESULT:
        erlaubt = (RUNTIME_PATHS[1],)
    elif scope == CAPABILITY_PREFLIGHT:
        erlaubt = PREFLIGHT_PATHS
    elif scope == CAPABILITY_SETUP:
        erlaubt = SETUP_PATHS
    else:
        return False
    return any(m == method and _matches(p, path) for m, p in erlaubt)
