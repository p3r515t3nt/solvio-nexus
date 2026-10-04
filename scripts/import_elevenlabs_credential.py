#!/usr/bin/env python3
"""Der sichere Weg, den ElevenLabs-Zugang in den Tresor zu bringen.

**Der Wert wird an keiner Stelle sichtbar.** Nicht als Argument, nicht in der
Umgebung, nicht in der Schalenhistorie, nicht im Protokoll, nicht in einem
Handoff und nicht in einem Chat. Er wird verdeckt eingetippt und geht direkt in
den Tresor.

Warum das ein eigenes Skript ist und kein Chat-Schritt: ein Schluessel, der
durch ein Modell reist — auch nur als Werkzeugergebnis —, ist ein Schluessel,
der in einem Transkript steht. Der Eigentuemer fuehrt das hier in SEINEM
Terminal aus; das Modell sieht davon hoechstens „stored = true".

    python3 scripts/import_elevenlabs_credential.py

Danach steht im Tresor `secret://elevenlabs/agents-api-key`, gebunden an genau
vier Faehigkeiten, genau ein Ziel (`api.elevenlabs.io`) und genau einen
Executor (`telephony`). Wer den Anruf anstoesst, sieht ihn nie.

Zu `allow_background=True`, denn das ist die eine Entscheidung hier, die eine
Begruendung braucht: ein Anruf DARF nicht ohne anwesende Person beginnen — aber
diese Schranke sitzt in der Freigabe (Face ID, Approval Policy V2), nicht im
Tresor. Der Tresor muss den Zugang auch dann hergeben, wenn der Core nach einem
Neustart den AUSGANG eines laengst freigegebenen Gespraechs nachlesen will.
Waere `requires_user_presence` hier gesetzt, ginge genau diese
Ergebniswahrheit verloren — und SOLVIO wuesste nicht mehr, ob sein eigener
Anruf angekommen ist. Die Grenze ist die Executor-Bindung und die Freigabe,
nicht ein Prompt.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from solvio.secret_vault import admin as VA          # noqa: E402
from solvio.secret_vault import policy as VP         # noqa: E402
from solvio.telephony import elevenlabs as EL        # noqa: E402


def fingerprint(value: str) -> str:
    """Ein Abdruck, kein Wert.

    SHA-256 ueber den Wert, auf zwoelf Hex-Zeichen gekuerzt. Er reicht, um zwei
    Importe zu unterscheiden und einen versehentlichen Doppelimport zu
    erkennen — und er reicht nicht, um den Wert zu rekonstruieren.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def read_secret(prompt: str) -> str:
    """Verdeckt, und nur von einem echten Terminal.

    Ohne TTY wird abgebrochen statt von der Standardeingabe gelesen: eine
    Pipeline waere genau der Weg, auf dem der Wert doch in einer Historie,
    einem Skript oder einem Protokoll landet.
    """
    if not sys.stdin.isatty():
        raise SystemExit("FEHLER: kein Terminal. Der Wert wird ausschliesslich "
                         "verdeckt eingetippt, nie aus einer Pipeline gelesen.")
    wert = getpass.getpass(prompt)
    zweit = getpass.getpass("Zur Sicherheit noch einmal: ")
    if wert != zweit:
        raise SystemExit("FEHLER: die beiden Eingaben sind verschieden. "
                         "Nichts gespeichert.")
    return wert.strip()


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="import_elevenlabs_credential",
        description="Legt den ElevenLabs-Zugang verdeckt im Tresor ab.")
    parser.add_argument("--replace", action="store_true",
                        help="einen vorhandenen Eintrag ersetzen")
    args = parser.parse_args()

    # Der Wert steht bewusst NICHT in `args`: argparse-Werte landen in `argv`,
    # und `argv` steht in `ps`.
    wert = read_secret("ElevenLabs-API-Schluessel (verdeckt, kein Echo): ")
    if not wert:
        raise SystemExit("FEHLER: leere Eingabe. Nichts gespeichert.")

    nutzlast = wert.encode("utf-8")
    abdruck = fingerprint(wert)
    del wert

    try:
        policy = VA.add(
            secret_ref=EL.SECRET_REF,
            kind=VP.SecretKind.API_KEY,
            plaintext=nutzlast,
            allowed_capabilities=list(EL.ALLOWED_CAPABILITIES),
            allowed_targets=[EL.UPSTREAM_ORIGIN],
            allowed_executors=[VP.ExecutorId.TELEPHONY],
            display_name="ElevenLabs (Telefonie)",
            service_label=EL.UPSTREAM_HOST,
            note="Telephony Capability V1 — nur der Telefonie-Ausgang leiht "
                 "diesen Wert. Wer den Anruf anstoesst, sieht ihn nie.",
            # Siehe Modul-Docstring: die Anwesenheitspflicht sitzt in der
            # Freigabe, nicht hier. Sonst verloere der Core nach einem Neustart
            # die Wahrheit ueber den Ausgang seines eigenen Anrufs.
            allow_background=True,
            requires_user_presence=False,
            replace=args.replace)
    except VA.AdminError as exc:
        if str(exc) == "secret_already_exists":
            raise SystemExit(
                "FEHLER: es liegt bereits ein Zugang unter "
                f"{EL.SECRET_REF}. Mit --replace ersetzen, wenn das gewollt "
                "ist.") from None
        raise SystemExit(f"FEHLER: {exc}") from None
    finally:
        del nutzlast

    print("stored       = true")
    print(f"secret_ref   = {EL.SECRET_REF}")
    print(f"version      = {policy.version}")
    print(f"executor     = {VP.ExecutorId.TELEPHONY.value}")
    print(f"target       = {EL.UPSTREAM_ORIGIN}")
    print(f"capabilities = {', '.join(EL.ALLOWED_CAPABILITIES)}")
    print(f"fingerprint  = {abdruck}")
    print("value        = NEVER")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
