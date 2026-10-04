#!/usr/bin/env python3
"""Prüft einen Offsite-Sicherungssatz in einer isolierten Wiederherstellung.

Benötigt zwei vom Besitzer bereitgestellte Dateien: den verschlüsselten
Recovery-Umschlag und die verschlüsselte Generation. Die Passphrase wird
verdeckt von stock age erfragt. Produktive Daten, Vault, Keychain und lokales
Offsite-Buch bleiben ausgeschlossen. Die Prüfung verwendet den Code und die
Abhängigkeiten aus dem gesicherten Git-Bundle und prüft dessen Herkunft.

Das Skript führt beim Aufruf eine echte lokale Wiederherstellungsprobe aus.
Nur eigene Sicherungen verwenden; Eingaben und Ergebnisse bleiben privat.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPORT = os.path.join(HERE, "reports", "disaster-proof-report.json")

#: Was es nach einem Totalverlust NICHT gaebe. Beruehrt der Beweis eines
#: davon, beweist er nichts.
VERBOTEN = ("~/.solvio", "~/.solvio-vault", "~/.solvio-approvals-production",
            "~/.solvio-deep", "~/.solvio-portal")

AGE = "/opt/homebrew/bin/age"
AGE_KEYGEN = "/opt/homebrew/bin/age-keygen"


def _say(line: str = "") -> None:
    print(line, flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(prog="b6_disaster_proof")
    parser.add_argument("--envelope", required=True,
                        help="der aus der AWS-Konsole geladene Umschlag "
                             "(v1/recovery/offsite-identity-v1.age)")
    parser.add_argument("--generation", required=True,
                        help="das aus der AWS-Konsole geladene Objekt "
                             "(v1/generations/….tar.zst.age)")
    parser.add_argument("--keep", action="store_true",
                        help="das Wegwerfverzeichnis stehen lassen")
    args = parser.parse_args()

    results: list[dict] = []

    def record(name: str, ok: bool, **extra) -> bool:
        results.append({"schritt": name, "ok": bool(ok), **extra})
        _say(f"  [{'PASS' if ok else 'FAIL'}] {name}"
             + (f" — {extra}" if extra else ""))
        return ok

    _say(__doc__.split("\n\n")[0])
    _say()
    for pfad in (args.envelope, args.generation):
        if not os.path.isfile(os.path.expanduser(pfad)):
            _say(f"Datei fehlt: {pfad}")
            return 2

    work = tempfile.mkdtemp(prefix="solvio-dr-proof-")
    _say(f"Wegwerfverzeichnis: {work}")
    _say()

    try:
        return _run(args, work, record, results)
    finally:
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)
            _say(f"\nAufgeraeumt: {work}")


def _run(args, work: str, record, results: list[dict]) -> int:
    envelope = os.path.expanduser(args.envelope)
    generation_file = os.path.expanduser(args.generation)

    # ---- 0. Die Lage stimmt: nichts Produktives ist im Spiel -------------
    _say("0. Die Ausgangslage — was es nach einem Totalverlust NICHT gibt")
    real_work = os.path.realpath(work)
    beruehrt = [p for p in VERBOTEN
                if real_work.startswith(os.path.realpath(os.path.expanduser(p)))]
    record("isoliert_vom_produktiven_baum", not beruehrt, beruehrt=beruehrt)
    # Die Umgebung wird fuer JEDEN Unterprozess entkernt: kein Zeiger auf
    # produktive Pfade, kein Tresor, kein Offsite-Verzeichnis.
    # PYTHONPATH/PYTHONHOME fliegen mit hinaus: ein geerbter Suchpfad koennte
    # ein installiertes SOLVIO vor den Klon schieben — und dann bewiese der
    # Lauf den Code dieses Rechners statt den aus der Sicherung.
    umgebung = {k: v for k, v in os.environ.items()
                if not k.startswith("SOLVIO_")
                and k not in ("PYTHONPATH", "PYTHONHOME")}
    umgebung["PYTHONNOUSERSITE"] = "1"
    umgebung["SOLVIO_VAULT_DIR"] = os.path.join(work, "kein-tresor")
    umgebung["SOLVIO_VAULT_TEST_KEYSTORE"] = os.path.join(work, "kein-kek")
    umgebung["SOLVIO_OFFSITE_DIR"] = os.path.join(work, "kein-offsite")
    umgebung["SOLVIO_OFFSITE_TEST_KEYSTORE"] = os.path.join(work, "kein-key")
    umgebung["SOLVIO_OFFSITE_LEDGER"] = os.path.join(work, "kein-buch.sqlite3")
    umgebung["SOLVIO_STORAGE_STATE_DIR"] = os.path.join(work, "kein-state")
    record("umgebung_entkernt", True,
           hinweis="kein Tresor, kein Schluesselbund, kein Buch, kein state")

    # ---- 1. Umschlag oeffnen — age fragt die Passphrase SELBST ----------
    _say("\n1. Der Umschlag (§14 Schritt 3) — age fragt gleich nach DEINER "
         "Passphrase")
    identity = os.path.join(work, "identity.txt")
    proc = subprocess.run([AGE, "-d", "-o", identity, envelope], timeout=900)
    if not record("umschlag_geoeffnet", proc.returncode == 0):
        _write(results)
        return 1
    os.chmod(identity, 0o600)

    # ---- 2. Der Fingerabdruck bindet Identitaet an Generation -----------
    keygen = subprocess.run([AGE_KEYGEN, "-y", identity],
                            capture_output=True, text=True, timeout=60)
    recipient = keygen.stdout.strip()
    if not record("recipient_abgeleitet", recipient.startswith("age1"),
                  recipient=recipient):
        _write(results)
        return 1

    # ---- 3. Generation oeffnen — stock age, stock zstd, stock tar -------
    _say("\n2. Die Generation (§14 Schritt 3, zweiter Teil)")
    satz = os.path.join(work, "satz")
    os.makedirs(satz)
    pipeline = (f'"{AGE}" -d -i "{identity}" "{generation_file}" '
                f'| zstd -d | tar -x -C "{satz}"')
    proc = subprocess.run(["/bin/sh", "-c", pipeline], capture_output=True,
                          text=True, timeout=1800)
    if not record("generation_entschluesselt", proc.returncode == 0,
                  fehler=proc.stderr.strip()[-120:] or ""):
        _write(results)
        return 1

    # Die Identitaet hat ihre Arbeit getan. Ab hier braucht sie niemand.
    subprocess.run(["rm", "-P", identity], capture_output=True)
    record("identitaet_sicher_geloescht", not os.path.exists(identity))

    # ---- 4. Gehoert der Schluessel zu DIESER Generation? ----------------
    offsite_json = os.path.join(satz, "offsite.json")
    if not record("offsite_json_vorhanden", os.path.isfile(offsite_json)):
        _write(results)
        return 1
    with open(offsite_json, encoding="utf-8") as fh:
        innen = json.load(fh)
    erwartet = innen.get("recipient_fingerprint", "")
    gerechnet = "sha256-" + hashlib.sha256(recipient.encode()).hexdigest()[:32]
    record("fingerabdruck_bindet_identitaet_an_generation",
           erwartet == gerechnet, im_satz=erwartet)

    # Und der Objektname bindet an den Inhalt (§8).
    dateiname = os.path.basename(generation_file).replace(".tar.zst.age", "")
    record("objektname_passt_zum_inhalt",
           innen.get("generation_id") == dateiname,
           objektname=dateiname, im_satz=innen.get("generation_id"))

    # ---- 5. SOLVIO aus der Sicherung selbst (§14 Schritt 4) -------------
    _say("\n3. Der Code reist mit (§14 Schritt 4)")
    bundle = os.path.join(satz, "Repos", "core.bundle")
    klon = os.path.join(work, "solvio-core")
    proc = subprocess.run(["git", "clone", "--quiet", bundle, klon],
                          capture_output=True, text=True, timeout=600)
    if not record("core_bundle_geklont", proc.returncode == 0,
                  fehler=proc.stderr.strip()[-120:] or ""):
        _write(results)
        return 1
    # Welchen Commit muss der Klon tragen? Das sagt die Generation SELBST.
    # Die Bundle-HEAD ersetzt keine Bindung an den gespeicherten Commit.
    herkunft = innen.get("provenance") or {}
    bundle_commit = (innen.get("core_bundle_commit")
                     or herkunft.get("core_bundle_commit") or "")
    if not record("generation_nennt_ihren_code_commit", bool(bundle_commit),
                  commit=bundle_commit[:12] or "(fehlt)"):
        _write(results)
        return 1
    proc = subprocess.run(["git", "-C", klon, "checkout", "--quiet",
                           "--detach", bundle_commit],
                          capture_output=True, text=True, timeout=600)
    if not record("code_commit_ausgecheckt", proc.returncode == 0,
                  commit=bundle_commit[:12],
                  fehler=proc.stderr.strip()[-120:] or ""):
        _write(results)
        return 1

    hat_restore = os.path.isfile(os.path.join(
        klon, "src", "solvio", "storage", "offsite", "restore.py"))
    if not record("restore_werkzeug_im_klon", hat_restore):
        # KEIN Rueckfall auf den Baum dieses Rechners. Im Totalverlust gibt es
        # ihn nicht — ein Beweis, der ihn benutzt, beweist das Falsche. Ohne
        # Werkzeug im Klon ist die Kette hier ehrlich zu Ende.
        _say("   Ohne Werkzeug im Klon endet der Beweis. Es gibt keinen "
             "Ersatzweg, der im Ernstfall existieren wuerde.")
        _write(results)
        return 1

    # ---- 5b. Auch die Abhaengigkeiten kommen aus dem Klon ---------------
    # Der Interpreter dieses Rechners wuerde die Bibliotheken dieses Rechners
    # mitbringen. Das Runbook sagt fuer den Ernstfall `uv sync` im Klon —
    # also tut der Beweis genau das und rechnet danach mit DESSEN Python.
    uv = shutil.which("uv")
    if not record("uv_vorhanden", bool(uv),
                  hinweis="das Runbook verlangt `uv sync` im Klon"):
        _write(results)
        return 1
    proc = subprocess.run([uv, "sync", "--frozen"], cwd=klon, env=umgebung,
                          capture_output=True, text=True, timeout=3600)
    klon_python = os.path.join(klon, ".venv", "bin", "python")
    if not record("abhaengigkeiten_aus_dem_klon",
                  proc.returncode == 0 and os.path.isfile(klon_python),
                  fehler=proc.stderr.strip()[-160:] or ""):
        _write(results)
        return 1

    # ---- 6. Nachrechnen — MIT DEM CODE AUS DER SICHERUNG ----------------
    _say("\n4. Nachrechnen (§14 Schritt 5) — mit dem Code aus dem Backup")
    code = pruefcode(klon, satz, dateiname)
    proc = subprocess.run([klon_python, "-c", code], capture_output=True,
                          text=True, timeout=1800, env=umgebung)
    if proc.returncode != 0:
        record("werkzeug_stammt_aus_dem_klon", False,
               ausgabe=proc.stdout.strip()[-160:])
        record("satz_nachgerechnet", False,
               fehler=proc.stderr.strip()[-200:])
        _write(results)
        return 1
    bericht = json.loads(proc.stdout.strip().splitlines()[-1])
    record("werkzeug_stammt_aus_dem_klon",
           str(bericht.get("werkzeug_herkunft", "")).startswith(klon),
           datei=os.path.relpath(str(bericht.get("werkzeug_herkunft", "")),
                                 klon))
    record("satz_nachgerechnet", bericht.get("ok"),
           eintraege=bericht.get("restored_entries"),
           speicher=bericht.get("sqlite_checked"),
           mb=round((bericht.get("restored_bytes") or 0) / 1024 ** 2, 1),
           grund=bericht.get("reason", "")[:80])

    # ---- 7. Und das alles ohne Tresor und ohne Schluesselbund -----------
    _say("\n5. Die Gegenprobe: nichts davon brauchte den alten Mac")
    record("kein_tresor_beruehrt",
           not os.path.exists(os.path.join(work, "kein-tresor",
                                           "vault.sqlite3")))
    record("kein_schluesselbund_beruehrt",
           not os.path.exists(os.path.join(work, "kein-key")))
    record("kein_offsite_buch_beruehrt",
           not os.path.exists(os.path.join(work, "kein-buch.sqlite3")))

    _write(results)
    fehler = [r["schritt"] for r in results if not r["ok"]]
    _say()
    _say(f"Ergebnis: {len(results) - len(fehler)}/{len(results)} PASS")
    _say("VERDIKT: " + ("DISASTER RECOVERY AM STUECK BEWIESEN"
                        if not fehler else f"NICHT BEWIESEN: {fehler}"))
    _say(f"Report: {REPORT}")
    return 1 if fehler else 0


def pruefcode(klon: str, satz: str, generation_id: str) -> str:
    """Der Code, der im Unterprozess nachrechnet — mit genau einem Riegel.

    Die Herkunftsprüfung stellt sicher: das Werkzeug
    MUSS aus dem Klon stammen. Faende Python ein installiertes SOLVIO oder
    einen Baum dieses Rechners frueher auf dem Suchpfad, bewiese der Lauf den
    Code der Maschine, die es im Ernstfall gar nicht mehr gibt. Dann bricht
    er mit Exit 3 ab, statt ein falsches Gruen zu melden.

    Als Funktion herausgezogen, damit eine Suite genau diesen Riegel stellen
    kann — nicht eine Nachbildung davon.
    """
    return (
        "import json, sys\n"
        f"sys.path.insert(0, {os.path.join(klon, 'src')!r})\n"
        "from solvio.storage.offsite import restore as R\n"
        "herkunft = R.__file__\n"
        f"if not herkunft.startswith({klon!r}):\n"
        "    print(json.dumps({'werkzeug_herkunft': herkunft, 'ok': False}))\n"
        "    raise SystemExit(3)\n"
        f"rep = R.verify_unpacked({satz!r}, "
        f"expect_generation={generation_id!r}, require_complete=True)\n"
        "out = rep.as_dict()\n"
        "out['werkzeug_herkunft'] = herkunft\n"
        "print(json.dumps(out, ensure_ascii=False))\n")


def _write(results: list[dict]) -> None:
    payload = {"meta": {
        "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "punkt": "§23.10 — simuliertes „alter Mac weg\"",
        "erlaubte_mittel": ["Provider-Zugang (Konsolen-Download)",
                            "Recovery-Passphrase (nur in stock age)",
                            "age, zstd, tar, git"],
        "hinweis": "Kein Geheimniswert in diesem Report. Die Passphrase hat "
                   "diesen Code nie beruehrt — age fragt sie selbst."},
        "results": results}
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    fd = os.open(REPORT, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


if __name__ == "__main__":
    sys.exit(main())
