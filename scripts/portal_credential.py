#!/usr/bin/env python3
"""Einen Portalzugang hinterlegen — ohne dass das Geheimnis irgendwo auftaucht.

Das Passwort wird in einem **nativen macOS-Fenster** eingegeben, mit verdeckter
Eingabe. Von dort geht es direkt in den Tresor. Es erscheint nicht im Chat, nicht
in der Terminal-Historie, nicht in `argv`, nicht in der Umgebung, nicht in der
Zwischenablage, nicht im Protokoll und in keinem Modellkontext.

Was dieses Skript ausgibt, ist ausschliesslich: welcher Alias belegt wurde, und
ob das Zurueckschreiben und Zuruecklesen funktioniert hat. Keine Laenge, kein
Anfangsbuchstabe, keine Pruefsumme — solche „harmlosen" Auskuenfte sind der
uebliche Weg, auf dem ein Geheimnis doch noch in ein Protokoll rutscht.

    python3 scripts/portal_credential.py portal:solvio-studio
    python3 scripts/portal_credential.py --list
"""
from __future__ import annotations

import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))

from solvio.portal.binding import BINDINGS  # noqa: E402
from solvio.portal.vault import PASSWORD, USERNAME, PortalVault, VaultError  # noqa: E402

DIALOG_TIMEOUT = 300


def ask(prompt: str, *, hidden: bool) -> str:
    """Fragt ueber ein macOS-Fenster. Der Wert bleibt in diesem Prozess.

    `osascript` schreibt die Antwort auf seine Standardausgabe, die hier gelesen
    wird — mehr Weg gibt es nicht, und weniger auch nicht: jede Eingabeform
    braucht genau einen Kanal. Entscheidend ist, dass dieser Kanal hier endet.
    """
    script = (
        f'display dialog {_applescript(prompt)} default answer "" '
        f'{"with hidden answer " if hidden else ""}'
        f'with title "SOLVIO — Portalzugang" buttons {{"Abbrechen", "Speichern"}} '
        f'default button "Speichern"'
    )
    try:
        proc = subprocess.run(["/usr/bin/osascript", "-e", script],
                              capture_output=True, text=True, timeout=DIALOG_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise SystemExit("Zeitueberschreitung — nichts gespeichert.") from None
    if proc.returncode != 0:
        raise SystemExit("Abgebrochen — nichts gespeichert.")
    line = proc.stdout.strip()
    marker = "text returned:"
    return line[line.index(marker) + len(marker):].strip() if marker in line else ""


def _applescript(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def main() -> int:
    if "--list" in sys.argv:
        vault = PortalVault()
        print("Hinterlegte Aliasse:")
        for alias in vault.aliases():
            print(f"  {alias}  (Passwort: {'ja' if vault.has(alias) else 'nein'})")
        print("Bekannte Portale:")
        for portal_id, binding in sorted(BINDINGS.items()):
            print(f"  {portal_id:20s} {binding.login_origin:36s} {binding.credential_alias}")
        return 0

    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    alias = sys.argv[1].strip()
    known = {b.credential_alias for b in BINDINGS.values()}
    if alias not in known:
        print(f"Unbekannter Alias. Bekannt sind: {sorted(known)}")
        return 1
    portal = next(b for b in BINDINGS.values() if b.credential_alias == alias)

    vault = PortalVault()
    user = ask(f"Benutzername oder E-Mail fuer {portal.portal_id}\n"
               f"({portal.login_origin})", hidden=False)
    if not user:
        raise SystemExit("Kein Benutzername — nichts gespeichert.")
    secret = ask(f"Passwort fuer {portal.portal_id}\n({portal.login_origin})\n\n"
                 "Die Eingabe ist verdeckt und geht direkt in den Tresor.",
                 hidden=True)
    if not secret:
        raise SystemExit("Kein Passwort — nichts gespeichert.")

    vault.store(alias, USERNAME, user)
    vault.store(alias, PASSWORD, secret)
    # Zuruecklesen ist Pflicht: `security` legt bei einem Bedienfehler klaglos
    # einen leeren Eintrag an und meldet Erfolg.
    try:
        ok = (vault.get(alias, USERNAME) == user and vault.get(alias, PASSWORD) == secret)
    except VaultError as exc:
        print(f"Tresor meldet: {exc}")
        return 2
    finally:
        user = secret = ""      # noqa: F841 - so kurz wie moeglich im Speicher
    print(f"Gespeichert unter dem Alias {alias}. Zuruecklesen bestaetigt: {ok}")
    print("Das Passwort wurde nirgends ausgegeben.")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
