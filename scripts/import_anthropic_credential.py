#!/usr/bin/env python3
"""Der sichere Weg, die Anthropic-Anmeldung in den Tresor zu bringen.

**Der Wert wird an keiner Stelle sichtbar.** Nicht als Argument, nicht in der
Umgebung, nicht in der Schalenhistorie, nicht im Protokoll, nicht in einem
Handoff und nicht in einem Chat. Er wird verdeckt eingetippt und geht direkt in
den Tresor.

Warum das ein eigenes Skript ist und kein Chat-Schritt: ein Token, das durch
ein Modell reist — auch nur als Werkzeugergebnis —, ist ein Token, das in einem
Transkript steht. Der Eigentuemer fuehrt das hier in SEINEM Terminal aus; das
Modell sieht davon hoechstens „stored = true".

    python3 scripts/import_anthropic_credential.py --kind subscription_oauth

Danach steht im Tresor `secret://anthropic/subscription-token`, gebunden an
genau einen Executor (`anthropic_broker`) und genau ein Ziel
(`api.anthropic.com`). Der schreibende Claude-Builder ist dort ausdruecklich
nicht gebunden — er sieht nur ein Broker-Token.

N8/C4 — der zweite Modus, ohne jede Werteingabe:

    python3 scripts/import_anthropic_credential.py --rescope-add nexus.claude_worker

erweitert den bestehenden Grant um genau diese Faehigkeit (der Nexus-
Claude-Arbeiter leiht am Broker-Ausgang unter ihr, nie unter der des
Autopilot-Schreibers). Der Wert wird dabei NICHT eingegeben und NICHT
gelesen; die KEK-Oeffnung bleibt die Owner-Handlung im Terminal. Zusaetzlich
gibt der Modus den `account_digest` des lokalen Claude-Kontos
(`~/.claude.json`, Metadatenprojektion ohne Anmeldematerial) aus — er wandert
in die Belegdatei des Topfnachweises (`docs/plan/evidence/`).
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from solvio.provider_broker import anthropic as AN   # noqa: E402
from solvio.secret_vault import admin as VA          # noqa: E402
from solvio.secret_vault import policy as VP         # noqa: E402

#: Nur diese beiden Formen. `kind` wird NICHT aus dem Wert geraten — ein
#: Rateschritt hier waere ein 401 beim Anbieter, den niemand erklaeren kann.
KINDS = {
    AN.KIND_OAUTH: VP.SecretKind.OAUTH_REFRESH_TOKEN,
    AN.KIND_API_KEY: VP.SecretKind.API_KEY,
}


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


#: Faehigkeiten, die dieser Modus dem Grant hinzufuegen darf. Geschlossene
#: Menge: eine beliebige Zeichenkette am Tresor waere eine Berechtigung aus
#: einer Kommandozeile heraus.
RESCOPE_CAPABILITIES = ("nexus.claude_worker",)


def account_digest() -> str:
    """Der Kontoabdruck des lokalen Claude-Kontos — dieselbe Projektion, die
    der Nutzungsleser bildet (Pfad, UUID, E-Mail als SHA-256), nie ein Wert."""
    from solvio.specialists import claude_usage as CU
    home = os.path.expanduser("~")
    try:
        return CU._account_digest(CU._metadata(home))
    except (KeyError, OSError, ValueError, TypeError):
        return ""


def rescope_add(capability: str, *, store=None) -> tuple[object, bool]:
    """Erweitert den Grant um EINE Faehigkeit. Kein Wert wird beruehrt."""
    if capability not in RESCOPE_CAPABILITIES:
        raise SystemExit(f"FEHLER: unbekannte Faehigkeit {capability!r}.")
    from solvio.secret_vault.store import VaultStore
    store = store or VaultStore()
    policy = store.policy(AN.SECRET_REF)
    if policy is None:
        raise SystemExit(f"FEHLER: keine Anmeldung unter {AN.SECRET_REF}. "
                         "Zuerst --kind subscription_oauth importieren.")
    current = list(policy.allowed_capabilities)
    if capability in current:
        return None, False
    try:
        return VA.rescope(secret_ref=AN.SECRET_REF,
                          allowed_capabilities=current + [capability], store=store)
    except VA.AdminError as exc:
        raise SystemExit(f"FEHLER: {exc}") from None


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="import_anthropic_credential",
        description="Legt die Anthropic-Anmeldung verdeckt im Tresor ab.")
    parser.add_argument("--kind", choices=sorted(KINDS),
                        help="subscription_oauth (aus `claude setup-token`) "
                             "oder api_key (Anthropic Console)")
    parser.add_argument("--replace", action="store_true",
                        help="einen vorhandenen Eintrag ersetzen")
    parser.add_argument("--rescope-add", choices=RESCOPE_CAPABILITIES, default="",
                        metavar="CAPABILITY",
                        help="den bestehenden Grant um diese Faehigkeit erweitern "
                             "(kein Wert wird eingegeben oder gelesen)")
    args = parser.parse_args()

    if args.rescope_add:
        if args.kind or args.replace:
            raise SystemExit("FEHLER: --rescope-add steht allein (ohne --kind/--replace).")
        policy, widened = rescope_add(args.rescope_add)
        print("rescoped        = " + ("true" if policy is not None else "false (bereits enthalten)"))
        print(f"secret_ref      = {AN.SECRET_REF}")
        print(f"capability      = {args.rescope_add}")
        if policy is not None:
            print(f"version         = {policy.version}")
            print(f"widened         = {'true' if widened else 'false'}")
        print(f"account_digest  = {account_digest() or 'UNKNOWN'}")
        print("value           = NEVER")
        return 0
    if not args.kind:
        parser.error("--kind ist erforderlich (oder --rescope-add CAPABILITY)")

    # Der Wert steht bewusst NICHT in `args`: argparse-Werte landen in `argv`,
    # und `argv` steht in `ps`.
    wert = read_secret(f"Anthropic-{args.kind} (verdeckt, kein Echo): ")
    if not wert:
        raise SystemExit("FEHLER: leere Eingabe. Nichts gespeichert.")

    nutzlast = json.dumps({"kind": args.kind, "token": wert},
                          separators=(",", ":")).encode("utf-8")
    abdruck = fingerprint(wert)
    del wert

    try:
        policy = VA.add(
            secret_ref=AN.SECRET_REF,
            kind=KINDS[args.kind],
            plaintext=nutzlast,
            allowed_capabilities=[AN.CAPABILITY],
            allowed_targets=[AN.UPSTREAM_ORIGIN],
            allowed_executors=[VP.ExecutorId.ANTHROPIC_BROKER],
            display_name="Anthropic (Claude Writer)",
            service_label=AN.UPSTREAM_HOST,
            note="Development Autopilot V0.6 — nur der Broker-Ausgang leiht "
                 "diesen Wert. Der Builder sieht ihn nie.",
            # Ein unbeaufsichtigter Bauablauf ist genau der Zweck; eine
            # frische Nutzerentscheidung je Anfrage waere das Gegenteil von
            # 24/7. Die Grenze ist die Executor-Bindung, nicht ein Prompt.
            allow_background=True,
            requires_user_presence=False,
            replace=args.replace)
    except VA.AdminError as exc:
        if str(exc) == "secret_already_exists":
            raise SystemExit(
                "FEHLER: es liegt bereits eine Anmeldung unter "
                f"{AN.SECRET_REF}. Mit --replace ersetzen, wenn das gewollt "
                "ist.") from None
        raise SystemExit(f"FEHLER: {exc}") from None
    finally:
        del nutzlast

    print("stored      = true")
    print(f"secret_ref  = {AN.SECRET_REF}")
    print(f"kind        = {args.kind}")
    print(f"version     = {policy.version}")
    print(f"executor    = {VP.ExecutorId.ANTHROPIC_BROKER.value}")
    print(f"target      = {AN.UPSTREAM_ORIGIN}")
    print(f"fingerprint = {abdruck}")
    print("value       = NEVER")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
