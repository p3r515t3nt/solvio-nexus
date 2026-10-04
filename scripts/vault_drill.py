#!/usr/bin/env python3
"""Die Wiederherstellungsprobe — beweist, dass die Sicherung des Tresors traegt.

Eine Sicherung, die nie zurueckgespielt wurde, ist eine Hoffnung. Diese Probe
macht daraus eine Messung, und sie beantwortet genau zwei Fragen, die
gegenlaeufig sind:

    1. Reicht die Platte ALLEIN, um den Tresor zu oeffnen?   -> muss NEIN sein.
    2. Reicht die Platte MIT der Passphrase des Besitzers?   -> muss JA sein.

Waere die erste Antwort ja, waere die verschluesselte Platte in einer fremden
Schublade eine Kopie aller Zugaenge. Waere die zweite nein, waere die Sicherung
nach einem Mac-Verlust unbrauchbar — und das faellt sonst erst im Ernstfall auf.

    python3 scripts/vault_drill.py --from "/Volumes/SSD 2TB/SOLVIO/Backups/<satz>" \\
                                   --into ~/solvio-vault-drill

**Die produktive Ablage wird nicht angefasst.** Das Ziel wird gegen dieselbe
Liste geprueft, die auch die Speicher-Wiederherstellung benutzt
(`storage.restore.PRODUCTION_PATHS`), und die Probe arbeitet mit einem EIGENEN
Schluesselspeicher — sie kann den Schluesselbund weder lesen noch schreiben.

**Kein Wert wird ausgegeben.** Der Beweis, dass ein Zugang wieder da ist, ist
seine Laenge im Vergleich mit sich selbst, nicht sein Inhalt.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))

VAULT_DB = "Vault/vault.sqlite3"
VAULT_RECOVERY = "Vault/recovery.json"


def _refuse_production(target: str) -> None:
    from solvio.storage.restore import PRODUCTION_PATHS, RestoreRefused
    real = os.path.realpath(os.path.expanduser(target))
    for path in PRODUCTION_PATHS:
        forbidden = os.path.realpath(os.path.expanduser(path))
        if real == forbidden or real.startswith(forbidden + os.sep):
            raise RestoreRefused(f"{target} liegt im produktiven Bereich ({path})")


def main(argv: list[str] | None = None) -> int:
    """Der Einstieg. Raeumt auch dann auf, wenn die Probe scheitert.

    Eine Probe, die bei jedem Fehlschlag ein Verzeichnis mit Geheimtext liegen
    laesst, sammelt genau das an, wogegen sie antritt.
    """
    return _run(argv)


def _run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from", dest="source", required=True,
                        help="Wurzel eines Sicherungssatzes")
    parser.add_argument("--into", dest="into", default="",
                        help="Wegwerf-Verzeichnis (Vorgabe: ein temporaeres)")
    parser.add_argument("--keep", action="store_true",
                        help="das Verzeichnis nicht aufraeumen")
    args = parser.parse_args(argv)

    source_db = os.path.join(args.source, VAULT_DB)
    source_env = os.path.join(args.source, VAULT_RECOVERY)
    if not os.path.exists(source_db):
        print(f"Kein Tresor in diesem Satz: {source_db}", file=sys.stderr)
        return 2
    if not os.path.exists(source_env):
        print(f"Kein Wiederherstellungsumschlag in diesem Satz: {source_env}",
              file=sys.stderr)
        print("Ohne ihn ist die Sicherung des Tresors WERTLOS. "
              "`vault_admin.py recovery-set` setzt ihn.", file=sys.stderr)
        return 2

    into = os.path.expanduser(args.into) if args.into else tempfile.mkdtemp(
        prefix="solvio-vault-drill-")
    try:
        _refuse_production(into)
    except Exception as exc:  # noqa: BLE001 - eine Absage ist ein Ergebnis
        # Kein Stacktrace fuer einen Bedienfehler. Wer eine Probe in einen
        # produktiven Pfad legen will, soll einen Satz lesen, keine Zeilennummer.
        print(f"Abgelehnt: {exc}", file=sys.stderr)
        return 2
    vault_dir = os.path.join(into, "vault")
    keys_dir = os.path.join(into, "keys")
    os.makedirs(vault_dir, mode=0o700, exist_ok=True)
    os.makedirs(keys_dir, mode=0o700, exist_ok=True)

    # Der eigene Schluesselspeicher wird gesetzt, BEVOR irgendetwas aus dem
    # Tresor importiert wird — sonst haetten die Module den produktiven Pfad
    # bereits aufgeloest.
    os.environ["SOLVIO_VAULT_DIR"] = vault_dir
    os.environ["SOLVIO_VAULT_TEST_KEYSTORE"] = keys_dir

    shutil.copy2(source_db, os.path.join(vault_dir, "vault.sqlite3"))
    shutil.copy2(source_env, os.path.join(vault_dir, "recovery.json"))
    print(f"Satz entpackt nach: {into}")

    from solvio.capabilities import policy as AP
    from solvio.secret_vault import broker as B
    from solvio.secret_vault import context as SC
    from solvio.secret_vault import keyring as K
    from solvio.secret_vault import recovery
    from solvio.secret_vault.store import VaultStore

    store = VaultStore()
    entries = store.policies()
    print(f"Im Satz: {len(entries)} Zugaenge, Datenbank "
          f"{'heil' if store.integrity_ok() else 'BESCHAEDIGT'}.")
    if not entries:
        print("Nichts zu beweisen — der Satz traegt keinen Zugang.", file=sys.stderr)
        _tidy(into, args.keep)
        return 1

    # -- Frage 1: reicht die Platte allein? ---------------------------------
    #
    # Kein `assert`: `python -O` streicht ihn, und dann liefe die Probe mit
    # einem Schluessel im Speicher weiter und meldete am Ende Erfolg. Genau
    # dieser Fehler ist in diesem Baum schon einmal teuer gewesen.
    if K.read_kek() is not None:
        print("Der Probenspeicher traegt bereits einen Schluessel — abgebrochen.",
              file=sys.stderr)
        return 2
    first = entries[0]
    probe = B.SecretBroker(store)
    denied = False
    try:
        with SC.bound(SC.UseContext(origin=AP.OriginClass.LOCAL_OWNER,
                                    capability=first.allowed_capabilities[0])):
            with probe.use(first.secret_ref, executor=first.allowed_executors[0],
                           target=first.allowed_targets[0]):
                pass
    except (B.SecretDenied, B.SecretUnavailable):
        denied = True
    print(f"  [1] Platte allein oeffnet den Tresor: "
          f"{'NEIN — richtig so' if denied else 'JA — DAS IST EIN DEFEKT'}")
    if not denied:
        _tidy(into, args.keep)
        return 1

    # -- Frage 2: reicht die Platte mit der Passphrase? ---------------------
    sys.path.insert(0, os.path.join(REPO, "scripts"))
    from vault_admin import ask_secret
    passphrase = ask_secret("Wiederherstellungs-Passphrase des SOLVIO-Tresors.\n\n"
                            "Dies ist eine PROBE. Der produktive Tresor wird nicht "
                            "angefasst.", title="SOLVIO — Wiederherstellungsprobe")
    if not passphrase:
        print("Nichts eingegeben — Probe abgebrochen.", file=sys.stderr)
        _tidy(into, args.keep)
        return 2
    envelope = recovery.read()
    try:
        kek = recovery.unwrap(envelope, passphrase)
    except recovery.RecoveryError as exc:
        print(f"  [2] Umschlag oeffnet nicht: {exc}", file=sys.stderr)
        _tidy(into, args.keep)
        return 1
    finally:
        del passphrase
    K.install_kek(kek)
    del kek
    print("  [2] Umschlag geoeffnet, Hauptschluessel im Probenspeicher.")

    # -- Frage 3: ist ein Zugang danach wirklich benutzbar? -----------------
    #
    # Ein Wegwerf-Executor beweist es, statt es zu behaupten: derselbe Weg wie
    # in der Produktion, dieselbe Policy, dasselbe Modul-Muster — nur ein
    # anderer Prozess und ein anderer Schluesselspeicher.
    recovered = 0
    for policy in entries:
        try:
            with SC.bound(SC.UseContext(origin=AP.OriginClass.LOCAL_OWNER,
                                        capability=policy.allowed_capabilities[0])):
                length = _disposable_executor(probe, policy)
        except (B.SecretDenied, B.SecretUnavailable) as exc:
            print(f"      {policy.secret_ref}: NICHT wiederhergestellt ({exc})")
            continue
        # Die Laenge, nicht der Wert. Und auch die nur als „ist da".
        print(f"      {policy.secret_ref}: wiederhergestellt "
              f"({'nicht leer' if length else 'LEER'})")
        recovered += 1 if length else 0
    print(f"  [3] {recovered} von {len(entries)} Zugaengen wieder benutzbar.")

    _tidy(into, args.keep)
    ausgang = 0 if recovered == len(entries) else 1
    _aufzeichnen(f"drill from={os.path.basename(args.source.rstrip('/'))} "
                 f"entries={len(entries)} recovered={recovered} ok={ausgang == 0}")
    return ausgang


def _aufzeichnen(zeile: str) -> None:
    """Schreibt das Ergebnis ins Betriebslog der Sicherung.

    Eine Probe, deren Ergebnis nur im Fenster steht, ist keine Aufzeichnung —
    einen Monat spaeter weiss niemand mehr, ob sie lief und was sie sagte. Der
    Eintrag traegt Zahlen und Namen, nie Inhalt; dasselbe Log rotiert bei 1 MB
    (`storage/engine.py`).
    """
    try:
        from solvio.storage.engine import _log
        _log("vault " + zeile)
    except Exception:  # noqa: BLE001 - eine Notiz darf die Probe nicht kippen
        pass


def _tidy(into: str, keep: bool) -> None:
    if keep:
        print(f"Probe behalten: {into}  (loeschen nicht vergessen)")
        return
    shutil.rmtree(into, ignore_errors=True)
    print(f"Probe aufgeraeumt: {into}")


def _disposable_executor(probe, policy) -> int:
    """Ein Executor, den es nur fuer diese Probe gibt.

    Sein Modulname kommt aus derselben Tabelle, gegen die der Tresor in der
    Produktion prueft (`policy.EXECUTOR_MODULES`) — die Probe umgeht die
    Aufrufer-Bindung also nicht, sie erfuellt sie. Das ist wichtig fuer das,
    was diese Probe behauptet UND fuer das, was sie nicht behauptet: sie
    beweist, dass der GEHEIMTEXT nach der Wiederherstellung wieder lesbar ist,
    nicht dass die Berechtigungspruefung greift. Letzteres beweist die
    Zusicherungssuite, und zwar in beide Richtungen.
    """
    import types

    from solvio.secret_vault.policy import EXECUTOR_MODULES
    prefixes = EXECUTOR_MODULES.get(policy.allowed_executors[0], ())
    if not prefixes:
        raise RuntimeError("kein Modulpraefix fuer diesen Executor")
    module = types.ModuleType(prefixes[0].rstrip(".") + "._vault_drill_probe"
                              if prefixes[0].endswith(".") else prefixes[0])
    source = ("def run(probe, policy):\n"
              "    with probe.use(policy.secret_ref,\n"
              "                   executor=policy.allowed_executors[0],\n"
              "                   target=policy.allowed_targets[0]) as material:\n"
              "        return len(material.plaintext())\n")
    exec(compile(source, "<vault-drill>", "exec"), module.__dict__)
    return module.run(probe, policy)


if __name__ == "__main__":
    raise SystemExit(main())
