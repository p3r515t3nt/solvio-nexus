"""Notizen — und die Grenze, hinter der keine entsteht (DEBT-0237).

**Warum es dieses Modul gibt.** Die Notizfaehigkeit existierte bisher nur als
gebautes Artefakt und im Pruefaufbau der Live-Abnahme. Dort zog der Harness die
Zielbegrenzung — also das Testsystem, nicht das Produkt. Gemessen an der Kette
war der Zielort davor voellig offen:

* der Planer waehlt `pfad` frei,
* `router._validate` prueft nur `type: string`, keine Pfadregel,
* der Handler bekaeme den Wert unveraendert,
* `Orchestrator._effect_allowed` laeuft **nach** dem Schreiben und entscheidet
  nur ueber den BELEG,
* der Kaefig (`agent_runtime/isolation.py`) gilt fuer Spezialisten-Unterprozesse,
  nicht fuer Inline-Handler.

Waehlte ein Modell `~/notizen.md`, entstuende dort real eine Datei. Der Lauf
bliebe ohne Beleg und meldete ehrlichen Misserfolg — **die Wirkung waere
trotzdem geschehen.**

**Die nachtraegliche Effektpruefung ersetzt diese Begrenzung nicht.** Sie sagt,
ob ein Beleg entsteht; sie kann eine bereits geschriebene Datei nicht
zuruecknehmen. Deshalb steht hier eine zweite, unabhaengige Schranke VOR dem
Schreiben.

**Der Zielort wird abgeleitet, nicht uebernommen.** Die autorisierte Ablage
kommt aus der Konfiguration (`SOLVIO_NOTES_DIR`, sonst
`<zustand>/effects/notes`). Ein Modellargument kann diese Grenze nicht
erweitern — es kann innerhalb davon nur einen Namen waehlen.

**Drei Ausbruchswege, alle geschlossen:**

1. **Ein Pfad ausserhalb.** Der Elternordner wird `realpath`-aufgeloest und
   muss in der Ablage liegen. `..` ist damit erledigt, auch mehrfach
   verschachtelt.
2. **Ein Symlink im Weg.** `realpath` des Elternordners folgt ihm — zeigt er
   hinaus, faellt die Pruefung.
3. **Der Zielname selbst ist ein Symlink.** Dagegen hilft `realpath` nicht, denn
   die Datei muss ja neu entstehen duerfen. Geoeffnet wird deshalb mit
   `O_NOFOLLOW`: zeigt der letzte Namensteil auf etwas anderes, schlaegt das
   Oeffnen fehl, **bevor** ein Byte geschrieben ist.

Was hier ausdruecklich NICHT steht: eine allgemeine Dateirechte-Architektur.
Diese Schranke gilt der Notizfaehigkeit, und sie benutzt die vorhandenen
Vertrags- und Registrierungswege (`CapabilitySpec` + `register`), wie
`documents.py` es vormacht.
"""
from __future__ import annotations

import errno
import os
from typing import Any

from solvio.capabilities import policy as P
from solvio.capabilities.contract import (CapabilityDeclined, CapabilityRefused,
                                          CapabilitySpec, ExecutionClass)
from solvio.logging_setup import get_logger
from solvio.security.mobile_approval.execution import NON_IDEMPOTENT_WRITE
from solvio.tools.base import RiskLevel

log = get_logger("capabilities")

#: Wo die autorisierte Notizablage liegt, wenn der Eigentuemer sie setzt.
NOTES_DIR_ENV = "SOLVIO_NOTES_DIR"

#: Die Vorgabe liegt im Wirkungsordner der Agentenlaufzeit. Nur dort entsteht
#: ein Ausfuehrungsbeleg (`Orchestrator._effect_root`), und eine Notizablage,
#: fuer die es nie einen Beleg gaebe, waere eine stille Sackgasse. Der
#: Gleichlauf ist mit einer Zusicherung festgehalten, nicht mit einem Import:
#: `capabilities` haengt nicht an der Laufzeit.
EFFECT_DIRNAME = "effects"
NOTES_DIRNAME = "notes"

#: Wie lang eine einzelne Notizzeile hoechstens sein darf. Keine Sparmassnahme,
#: sondern eine Grenze: eine Faehigkeit, die beliebig viel schreibt, ist ein
#: anderes Werkzeug als eine, die eine Notiz anhaengt.
MAX_NOTE_CHARS = 4_000


class NoteTargetRefused(CapabilityRefused):
    """Der gewuenschte Zielort liegt nicht in der autorisierten Ablage.

    **Sie erbt von `CapabilityRefused` — das ist der Unterschied zwischen einer
    Aussage und einem Achselzucken.** Der Router faengt diesen Vertrag
    ausdruecklich ab und meldet `rejected_by_policy` mit dem Grund. Eine
    beliebige Ausnahme liefe stattdessen in den Mehrdeutigkeitspfad: bei
    `NON_IDEMPOTENT_WRITE` wird daraus `RECOVERY_REQUIRED` — „ich weiss nicht,
    ob es durchging". Gemessen genau so, bevor dieser Vertrag stand.

    Das waere fail-closed, aber unwahr: die Ablehnung geschieht VOR dem
    Oeffnen, es ist nachweislich nichts passiert. Wer das als ungewissen
    Ausgang meldet, schickt den Menschen zu einer Pruefung, die es nicht
    braucht — und stumpft die Meldung ab, die spaeter einmal ernst ist.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason,
                         human_message="Dahin schreibe ich keine Notiz.")


def state_dir() -> str:
    """Derselbe Zustandsordner wie ueberall — dieselbe Umleitung im Test.

    Bewusst eine eigene kleine Funktion statt eines Imports aus
    `agent_runtime`: so macht es auch `cognition/ledger.py` und
    `storage/engine.py`, und die Faehigkeitsschicht bleibt frei von der
    Laufzeit.
    """
    return os.environ.get("SOLVIO_STATE_DIR", os.path.expanduser("~/.solvio"))


def notes_root() -> str:
    """Die autorisierte Notizablage, aufgeloest.

    `realpath` schon hier: sonst verglichen wir spaeter einen aufgeloesten
    Zielpfad gegen eine unaufgeloeste Wurzel, und ein Symlink IN der Wurzel
    liesse jeden Vergleich scheitern — die Schranke waere dann nicht zu streng,
    sondern schlicht kaputt.
    """
    konfiguriert = (os.environ.get(NOTES_DIR_ENV) or "").strip()
    wurzel = konfiguriert or os.path.join(state_dir(), EFFECT_DIRNAME, NOTES_DIRNAME)
    return os.path.realpath(os.path.expanduser(wurzel))


def resolve_target(pfad: str) -> str:
    """Der Zielort — abgeleitet aus der Ablage, nicht uebernommen.

    Erlaubt sind genau zwei Formen:

    * ein blosser Dateiname (`notizen.md`) — er wird an die Ablage gehaengt;
    * ein absoluter Pfad, der bereits IN der Ablage liegt.

    Alles andere ist `NoteTargetRefused`. Insbesondere ein relativer Pfad mit
    Trennzeichen: er haenge vom Arbeitsverzeichnis des Prozesses ab, und das
    steht in keinem Vertrag — ein Ziel, das je nach Aufrufort woanders liegt,
    ist kein gebundener Parameter.

    **Diese Funktion schreibt nichts und legt nichts an.** Sie ist die Pruefung,
    die vor dem Schreiben steht.
    """
    wurzel = notes_root()
    roh = str(pfad or "").strip()
    if not roh:
        raise NoteTargetRefused("empty_path")

    erweitert = os.path.expanduser(roh)
    if os.path.isabs(erweitert):
        kandidat = os.path.abspath(erweitert)
    elif os.sep in erweitert or (os.altsep and os.altsep in erweitert):
        raise NoteTargetRefused("relative_path_with_separator")
    elif erweitert in (os.curdir, os.pardir):
        raise NoteTargetRefused("no_filename")
    else:
        kandidat = os.path.join(wurzel, erweitert)

    name = os.path.basename(kandidat)
    if not name or name in (os.curdir, os.pardir):
        raise NoteTargetRefused("no_filename")

    # Der ELTERNORDNER wird aufgeloest, nicht das Ziel: das Ziel darf noch
    # fehlen. `realpath` loest dabei jeden Symlink im vorhandenen Teil auf —
    # genau der Ausbruchsweg, den ein blosser Praefixvergleich uebersieht.
    eltern = os.path.realpath(os.path.dirname(kandidat))
    if eltern != wurzel and not eltern.startswith(wurzel + os.sep):
        raise NoteTargetRefused("outside_notes_root")

    ziel = os.path.join(eltern, name)
    # Ein Symlink als ZIELNAME faellt schon hier — damit der Klassifizierer ihn
    # sieht und der Mensch gar nicht erst gefragt wird. Das ist eine PRUEFUNG,
    # keine Garantie: zwischen ihr und dem Oeffnen koennte der Verweis
    # entstehen. Die Garantie ist `O_NOFOLLOW` in `append_note` — beides
    # zusammen, nicht eines statt des anderen.
    if os.path.islink(ziel):
        raise NoteTargetRefused("target_is_symlink")
    return ziel


def append_note(pfad: str, text: str) -> str:
    """Haengt `text` als eigene Zeile an eine Notiz in der Ablage an.

    Rueckgabe: der tatsaechlich beschriebene Pfad. Reihenfolge ist die Aussage —
    **erst pruefen, dann anlegen, dann schreiben.** Waere die Reihenfolge
    umgekehrt, bestuende die Datei schon, bevor jemand fragt, ob sie darf.
    """
    # Inhaltsfehler sind KEIN Policy-Nein: „das gibt es nicht" ist etwas
    # anderes als „das gibt es, und ich fasse es nicht an" (contract.py).
    inhalt = str(text or "")
    if not inhalt.strip():
        raise CapabilityDeclined("empty_text",
                                 human_message="Da stand kein Text drin.")
    if len(inhalt) > MAX_NOTE_CHARS:
        raise CapabilityDeclined("text_too_long",
                                 human_message="Das ist zu lang fuer eine Notiz.")

    ziel = resolve_target(pfad)                    # (1) pruefen
    os.makedirs(os.path.dirname(ziel), mode=0o700, exist_ok=True)   # (2) anlegen
    try:
        # `O_NOFOLLOW` schlaegt fehl, wenn der letzte Namensteil ein Symlink
        # ist. Ohne ihn folgte das Oeffnen ihm — und schriebe an sein Ziel,
        # das ausserhalb liegen kann. Der Elternvergleich oben sieht das nicht.
        fd = os.open(ziel, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
                     0o600)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise NoteTargetRefused("target_is_symlink") from exc
        raise
    with os.fdopen(fd, "a", encoding="utf-8") as datei:              # (3) schreiben
        datei.write(inhalt.rstrip("\n") + "\n")
    log.info("capabilities.note_written", chars=len(inhalt))
    return ziel


SPECS: dict[str, CapabilitySpec] = {
    "note_write": CapabilitySpec(
        name="note_write", version=1,
        # FAST waere ein ValueError — eine schreibende Faehigkeit darf das nicht.
        execution_class=ExecutionClass.CONTROLLED,
        base_risk=RiskLevel.MUTATING,
        # Anhaengen ist nicht wiederholbar.
        semantics=NON_IDEMPOTENT_WRITE,
        input_schema={"type": "object", "properties": {
            "pfad": {"type": "string"}, "text": {"type": "string"}},
            "required": ["pfad", "text"]},
        executor="inline",
        description=("Haengt eine Zeile an eine Notiz in der autorisierten "
                     "Notizablage an. `pfad` ist ein Dateiname darin oder ein "
                     "absoluter Pfad, der darin liegt.")),
}


def classify_note_write(arguments: dict[str, Any]) -> P.Classification:
    """Die Klasse DIESES Aufrufs — am aufgeloesten Ziel gemessen.

    **Hier steht die Zielbegrenzung ZUM ZWEITEN Mal, und das ist der Punkt.**
    Der Router ruft den Klassifizierer, bevor er die Freigabe anfragt, und
    reicht `CapabilityRefused` von dort ausdruecklich durch. Sein eigener
    Kommentar sagt warum: „der Mensch soll nie etwas bestaetigen, das danach
    abgelehnt wird."

    Ohne diese Stelle waere die Ablehnung erst im Handler gefallen — also NACH
    der Freigabe, hinter dem durablen Rand des Ausfuehrungsjournals. Gemessen:
    der Router meldete dann `RECOVERY_REQUIRED: unknown_outcome`, „ich weiss
    nicht, ob es durchging". Das ist fail-closed, aber unwahr, und es kostet
    eine Owner-Freigabe fuer etwas, das nie laufen konnte.

    Die Pruefung im Handler bleibt trotzdem stehen. Sie ist die, die zaehlt:
    ein Klassifizierer ist eine Auskunft, das Oeffnen ist die Tat.
    """
    ziel = resolve_target(arguments.get("pfad", ""))   # wirft bei Unzulaessigkeit
    return P.Classification(P.ActionClass.UNCLASSIFIED, targets=(ziel,),
                            reason="note_target_resolved")


class NoteCapabilities:
    """Die Faehigkeitsflaeche. Duenn mit Absicht — die Aussage steht oben."""

    def note_write(self, arguments: dict[str, Any]) -> dict[str, Any]:
        ziel = append_note(arguments["pfad"], arguments["text"])
        # Der beschriebene Pfad geht zurueck: ohne ihn kann der Core den Effekt
        # nicht nachlesen und die Handlung bliebe unbelegt.
        return {"pfad": ziel}


def register(router: Any, capabilities: NoteCapabilities) -> list[str]:
    handlers = {"note_write": capabilities.note_write}
    for name, handler in handlers.items():
        router.register(SPECS[name], handler,
                        classify=classify_note_write)
    return sorted(handlers)
