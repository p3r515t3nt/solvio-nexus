"""Was ein Spezialist zu sehen bekommt — eine Mappe, kein Dateisystem.

Der naheliegende Entwurf waere, Claude Code oder Codex einfach im Projektordner
laufen zu lassen. Er ist auch der gefaehrlichste: in `/Users/solvio/solvio-core`
liegt `.env`, und darin stehen der OpenAI-Schluessel, das Home-Assistant-Token und
die Google-Zugangsdaten. Ein Berater mit Lesezugriff auf das Projekt ist ein
Berater mit Lesezugriff auf alle Geheimnisse — und zwar unabhaengig davon, wie
brav er sich verhaelt, denn die Grenze waere dann seine Gutmuetigkeit.

Also bekommt er ein eigenes Verzeichnis, in dem ausschliesslich steht, was der
Core selbst zusammengestellt hat: das Ziel, die Faehigkeiten, die Laufzeiten, die
Architekturdokumente. Keine Konfiguration, keine Schluessel, kein Quellcode, in
dem beides zufaellig nebeneinander liegt.

Das ist zugleich der Grund, warum die Mappe frisch gebaut und danach geloescht
wird: sie soll nichts aufheben.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from typing import Any

from solvio.logging_setup import get_logger

log = get_logger("specialists")

#: Architekturdokumente, die einen Berater wirklich weiterbringen. Bewusst eine
#: Liste und kein Verzeichnis: was nicht darauf steht, kommt nicht mit.
DOCUMENTS = (
    "docs/architecture/CAPABILITY_CONTRACT.md",
    "docs/architecture/TRUST_BOUNDARY.md",
    "docs/architecture/CAPABILITY_GAP_RESOLVER.md",
    "docs/architecture/SOLVIO_APPROVALS.md",
    "docs/architecture/HERMES_DEEP_RUNTIME.md",
)

#: Namen, die in einer Mappe nichts zu suchen haben. Wird nach dem Bauen
#: geprueft — eine Liste, die niemand kontrolliert, ist eine Absichtserklaerung.
FORBIDDEN = (".env", "credentials", "auth.json", "token", "secret", ".git",
             "config.py", "vault")


class Briefing:
    """Ein Verzeichnis, das genau so lange existiert wie die Beratung."""

    def __init__(self, path: str) -> None:
        self.path = path

    def cleanup(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)

    def __enter__(self) -> "Briefing":
        return self

    def __exit__(self, *_exc) -> None:
        self.cleanup()


def build(*, goal: str, capabilities: list[dict[str, Any]],
          runtimes: list[dict[str, Any]], blocker: str = "",
          repo_root: str = "") -> Briefing:
    """Legt die Mappe an und prueft danach, dass nichts Verbotenes darin liegt."""
    root = tempfile.mkdtemp(prefix="solvio-briefing-")
    os.chmod(root, 0o700)

    _write(root, "ZIEL.md",
           f"# Das Ziel des Nutzers\n\n{goal.strip()}\n\n"
           f"# Warum der direkte Weg blockiert ist\n\n{blocker or 'unbekannt'}\n")
    _write(root, "faehigkeiten.json",
           json.dumps(capabilities, ensure_ascii=False, indent=1))
    _write(root, "laufzeiten.json",
           json.dumps(runtimes, ensure_ascii=False, indent=1))
    _write(root, "LIES_MICH.md", _readme())

    if repo_root:
        docs = os.path.join(root, "architektur")
        os.makedirs(docs, mode=0o700, exist_ok=True)
        for relative in DOCUMENTS:
            source = os.path.join(repo_root, relative)
            if os.path.isfile(source):
                shutil.copy2(source, os.path.join(docs, os.path.basename(relative)))

    leaking = _forbidden_entries(root)
    if leaking:
        shutil.rmtree(root, ignore_errors=True)
        raise RuntimeError(f"briefing carries forbidden entries: {leaking[:3]}")
    log.info("specialist.briefing_built", entries=_count(root))
    return Briefing(root)


def _write(root: str, name: str, text: str) -> None:
    with open(os.path.join(root, name), "w", encoding="utf-8") as handle:
        handle.write(text)


def _count(root: str) -> int:
    return sum(len(files) for _dir, _subdirs, files in os.walk(root))


def _forbidden_entries(root: str) -> list[str]:
    found: list[str] = []
    for directory, subdirs, files in os.walk(root):
        for name in list(subdirs) + files:
            low = name.lower()
            if any(bad in low for bad in FORBIDDEN):
                found.append(os.path.join(directory, name))
    return found


def _readme() -> str:
    return (
        "# Worum es geht\n\n"
        "Du bist als Fachberater fuer SOLVIO hinzugezogen — einen lokal "
        "laufenden Sprachassistenten.\n\n"
        "In diesem Verzeichnis findest du:\n\n"
        "* `ZIEL.md` — was der Nutzer erreichen will und warum es blockiert ist\n"
        "* `faehigkeiten.json` — jede heute freigegebene Faehigkeit mit "
        "Ausfuehrungsklasse, Risiko, Semantik und Verfuegbarkeit\n"
        "* `laufzeiten.json` — die Geraete und Laufzeiten, die SOLVIO kennt. "
        "Fehlende Angaben sind ECHT unbekannt und duerfen nicht geraten werden\n"
        "* `architektur/` — die massgeblichen Vertraege\n\n"
        "## Was von dir NICHT erwartet wird\n\n"
        "* Nichts ausfuehren, nichts installieren, nichts aendern.\n"
        "* Keine Risikoeinstufung und keine Aussage darueber, ob eine Freigabe "
        "noetig ist — das entscheidet SOLVIO anhand seiner eigenen Vertraege, "
        "und deine Einschaetzung dazu aendert daran nichts.\n"
        "* Keine erfundenen Tatsachen ueber Geraete. Steht ein Wert nicht in "
        "`laufzeiten.json`, ist er unbekannt; sage das, statt zu schaetzen.\n\n"
        "Deine Antwort ist **Information**, keine Entscheidung.\n"
    )
