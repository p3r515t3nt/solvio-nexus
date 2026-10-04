#!/usr/bin/env python3
"""Den Anbieterschluessel drehen — ohne dass der Wert irgendwo auftaucht.

Der Handgriff gehoert dem Eigentuemer allein. Dieses Skript nimmt den neuen Wert
**verdeckt** entgegen (keine Anzeige, keine Kommandozeile, keine
Shell-Geschichte), schreibt ihn in `~/solvio-core/.env` und meldet danach nur
einen Fingerabdruck — nie den Wert.

Warum es das ueberhaupt gibt: der alte Wert lag ueber die gesamte Geschichte des
Hermes-Kaefigs darin und ritt zusaetzlich in unsandkastige `codex`-Unterprozesse
(DEBT-0128). Er gilt damit als historisch belichtet. Provider Broker V1 nimmt dem
Kaefig den Zugang — er macht den alten Wert aber **nicht** ungueltig. Das tut
nur eine Rotation beim Anbieter (DEBT-0129).

    python3 scripts/rotate_provider_key.py

Vorher beim Anbieter: neuen Schluessel anlegen, alten widerrufen.
Nachher: `launchctl kickstart -k gui/$(id -u)/com.solvio.core`.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
from getpass import getpass

ENV_PATH = os.path.expanduser("~/solvio-core/.env")
VARIABLE = "OPENAI_API_KEY"


def fingerprint(value: str) -> str:
    """Zwoelf Hexstellen. Genug, um zwei Werte zu unterscheiden, zu wenig, um
    einen zu rekonstruieren."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def main() -> int:
    if not os.path.exists(ENV_PATH):
        print(f"Es gibt keine {ENV_PATH}.", file=sys.stderr)
        return 2

    with open(ENV_PATH, encoding="utf-8") as handle:
        lines = handle.readlines()

    old = ""
    for line in lines:
        if line.startswith(f"{VARIABLE}="):
            old = line.split("=", 1)[1].strip()
            break
    print(f"bisheriger Wert: Fingerabdruck {fingerprint(old) if old else '(keiner)'}")

    fresh = getpass("neuer Anbieterschluessel (Eingabe bleibt unsichtbar): ").strip()
    if not fresh:
        print("Nichts eingegeben — es wurde nichts geaendert.")
        return 1
    if len(fresh) < 20:
        print("Das sieht nicht wie ein Anbieterschluessel aus. Nichts geaendert.",
              file=sys.stderr)
        return 2
    if fresh == old:
        print("Das ist derselbe Wert wie bisher. Nichts geaendert.", file=sys.stderr)
        return 2

    again = getpass("noch einmal zur Sicherheit: ").strip()
    if again != fresh:
        print("Die beiden Eingaben sind verschieden. Nichts geaendert.",
              file=sys.stderr)
        return 2

    # Erst eine Kopie, dann schreiben. Ein halb geschriebenes `.env` nimmt beim
    # naechsten Start die Stimme mit.
    backup = ENV_PATH + ".vor-rotation"
    shutil.copy2(ENV_PATH, backup)
    os.chmod(backup, 0o600)

    replaced = False
    out = []
    for line in lines:
        if line.startswith(f"{VARIABLE}="):
            out.append(f"{VARIABLE}={fresh}\n")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{VARIABLE}={fresh}\n")

    temporary = ENV_PATH + ".neu"
    previous = os.umask(0o077)
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.writelines(out)
    finally:
        os.umask(previous)
    os.chmod(temporary, 0o600)
    os.replace(temporary, ENV_PATH)

    print(f"geschrieben. neuer Fingerabdruck {fingerprint(fresh)}")
    print(f"Sicherung des alten Standes: {backup}")
    print()
    print("Jetzt noch:")
    print("  launchctl kickstart -k gui/$(id -u)/com.solvio.core")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
