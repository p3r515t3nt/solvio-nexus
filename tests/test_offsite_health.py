"""Offsite V1 B4 — was gruen heisst, und was NIE gruen heisst.

Diese Suite hat genau einen Schwerpunkt: **kein falsches Gruen.** Der
Vertrag (§12) macht dafuer eine harte Zusage, und jede Zeile hier haengt an
ihr:

> Ein Upload-Erfolg macht NIE gruen. `healthy` verlangt zusaetzlich einen
> frischen Restore-Beweis.

Die verbotenen Verwechslungen, je eine Zusicherung:

* ein gescheiterter Beweis darf nie gesund aussehen,
* „noch nie zurueckgeholt" darf nie gesund aussehen,
* ein ALTER Erfolg darf nicht die HEUTIGE Gesundheit sein,
* eine fehlende Ziel-Klasse darf nicht gruen durchgehen,
* ein unterbrochener Lauf (`uploading`) darf nicht als Erfolg gelten,
* ein unlesbares Buch und ein beschaedigter Betriebszustand sind nicht
  gruen, sondern laut.

Der Restore-Beweis selbst wird gegen einen echten, verschluesselten Satz
gefahren (lokal erzeugt, ueber den S3-Stub abgelegt) — nicht gegen eine
Attrappe: ein Beweis, der nur Attrappen prueft, beweist Attrappen.

ASSERTION POLICY: `require*` aus `tests/_guard.py` sind Funktionsaufrufe und
ueberleben `python -O`.
"""
from __future__ import annotations

import atexit
import datetime as dt
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from _guard import enforce_assertions, require, require_equal  # noqa: E402

enforce_assertions()

_SANDBOX = tempfile.mkdtemp(prefix="solvio-offsite-b4-")
os.environ["SOLVIO_VAULT_DIR"] = os.path.join(_SANDBOX, "vault")
os.environ["SOLVIO_VAULT_TEST_KEYSTORE"] = os.path.join(_SANDBOX, "keys")
os.environ["SOLVIO_OFFSITE_DIR"] = os.path.join(_SANDBOX, "offsite")
os.environ["SOLVIO_OFFSITE_TEST_KEYSTORE"] = os.path.join(_SANDBOX, "okeys")
os.environ["SOLVIO_OFFSITE_LEDGER"] = os.path.join(_SANDBOX, "offsite.sqlite3")
os.environ["SOLVIO_STORAGE_STATE_DIR"] = os.path.join(_SANDBOX, "storage")
atexit.register(shutil.rmtree, _SANDBOX, True)

# DEBT-0228: der Bestand kommt aus einer eigenen Wurzel, nicht aus dem
# laufenden Core. Ohne diese Zeile liest diese Suite `~/.solvio/*.sqlite3`,
# die Freigabe-Identitaet und `/Users/solvio/solvio-core/.env` — also
# Production. Der Bestand hier ist vollstaendig und synthetisch; geprueft wird
# unveraendert dasselbe.
import _inventory_fixture                                      # noqa: E402
_inventory_fixture.activate(os.path.join(_SANDBOX, "bestand"))

from _s3_stub import RunningStub                              # noqa: E402
from solvio.secret_vault import admin, policy as VP           # noqa: E402
from solvio.secret_vault import keyring as K                  # noqa: E402
from solvio.secret_vault.store import VaultStore              # noqa: E402
from solvio.storage.offsite import config as OC               # noqa: E402
from solvio.storage.offsite import health as OH               # noqa: E402
from solvio.storage.offsite import identity as OI             # noqa: E402
from solvio.storage.offsite import job as OJ                  # noqa: E402
from solvio.storage.offsite import ledger as OL               # noqa: E402
from solvio.storage.offsite import verify as OV               # noqa: E402

FAKE_CREDENTIAL = json.dumps({
    "access_key_id": "SYNTHETIC-DRILL-KEY-0001",
    "secret_access_key": "synthetic-drill-value-0003"}).encode()


# --------------------------------------------------------------------- Werkzeug
def _fresh_world(*, enabled: bool = True) -> tuple[OC.OffsiteConfig, str]:
    root = tempfile.mkdtemp(prefix="b4-case-", dir=_SANDBOX)
    os.environ["SOLVIO_VAULT_DIR"] = os.path.join(root, "vault")
    os.environ["SOLVIO_VAULT_TEST_KEYSTORE"] = os.path.join(root, "keys")
    os.environ["SOLVIO_OFFSITE_DIR"] = os.path.join(root, "offsite")
    os.environ["SOLVIO_OFFSITE_TEST_KEYSTORE"] = os.path.join(root, "okeys")
    os.environ["SOLVIO_OFFSITE_LEDGER"] = os.path.join(root, "offsite.sqlite3")
    os.environ["SOLVIO_STORAGE_STATE_DIR"] = os.path.join(root, "storage")
    K.forget_kek()
    store = VaultStore()
    admin.initialize(store)
    line, recipient = OI.create_identity()
    OI.store_identity(line, version=1)
    base = OC.fresh(recipient)
    cfg = OC.OffsiteConfig(enabled=enabled, recipient=recipient,
                           recipient_version=1, region=base.region,
                           classes=base.classes)
    OC.save(cfg)
    admin.add(secret_ref="secret://offsite/s3",
              kind=VP.SecretKind.SERVICE_CREDENTIAL,
              plaintext=FAKE_CREDENTIAL,
              allowed_capabilities=("offsite_backup",),
              allowed_targets=tuple(cfg.bucket_url(k)
                                    for k in ("daily", "weekly", "monthly")),
              allowed_executors=(VP.ExecutorId.OFFSITE,),
              allow_background=True, store=store)
    return cfg, root


def _healthy_world() -> tuple[RunningStub, OL.OffsiteLedger, str]:
    """Ein Haus, in dem alles stimmt: echte Generation, echter Beweis.

    Der Aufrufer schliesst den Stub. Von hier aus wird jede Zusicherung
    gegen einen Zustand gefahren, der WIRKLICH gruen ist — sonst prueft man
    nur, dass rot rot bleibt.
    """
    _cfg, root = _fresh_world()
    running = RunningStub()
    running.__enter__()
    book = OL.OffsiteLedger()
    outcome = OJ.run_once(force=True, client=running.client(), book=book)
    require(outcome["ok"], f"die Vorbereitung scheiterte: {outcome}")
    proof = OV.restore_proof(client=running.client(), book=book)
    require(proof.ok, f"der Beweis in der Vorbereitung scheiterte: {proof}")
    return running, book, root


def _assess_now(**overrides) -> tuple[str, str]:
    report = OH.collect()
    report.update(overrides)
    return OH.assess(report)


# ------------------------------------------------------------ Testisolation
def t_this_suite_cannot_touch_production_state() -> None:
    require("/.solvio/offsite.sqlite3" not in OL.ledger_path(),
            "das Offsite-Buch ist nicht umgelenkt")
    require("/.solvio/offsite" not in OC.offsite_dir(),
            "das Offsite-Verzeichnis ist nicht umgelenkt")
    require(OI.is_test_backend(), "der Schluesselspeicher ist nicht umgelenkt")


# --------------------------------------------- der gruene Fall als Bezugspunkt
def t_a_fresh_upload_with_a_fresh_proof_is_healthy() -> None:
    running, book, _root = _healthy_world()
    try:
        word, reason = OH.assess()
        require_equal(word, "healthy",
                      f"ein vollstaendig gesundes Haus gilt als {word}: {reason}")
        require("Restore-Beweis" in reason,
                f"der Grund nennt den Beweis nicht: {reason}")
        summary = OH.summary()
        # §12 verlangt BEIDE Zeitstempel im Befund, nicht nur in state.json.
        require(summary["letzte_sicherung"] != "noch nie",
                "der Befund nennt die letzte Sicherung nicht")
        require(summary["letzter_restore_beweis"] != "noch nie",
                "der Befund nennt den Restore-Beweis nicht")
    finally:
        running.__exit__()


# ------------------------------------ die verbotenen Verwechslungen (§12/§18)
def t_an_upload_without_any_proof_is_never_healthy() -> None:
    """Der Kernsatz: ein Objekt beim Anbieter ist kein Backup."""
    _cfg, _root = _fresh_world()
    with RunningStub() as running:
        book = OL.OffsiteLedger()
        outcome = OJ.run_once(force=True, client=running.client(), book=book)
        require(outcome["ok"], f"der Lauf scheiterte: {outcome}")
        word, reason = OH.assess()
        require_equal(word, "degraded",
                      f"hochgeladen ohne Beweis gilt als {word}: {reason}")
        require("NIE zurueckgeholt" in reason or "kein Backup" in reason,
                f"der Grund sagt nicht, was fehlt: {reason}")


def t_a_failed_proof_is_unavailable_not_degraded() -> None:
    """Ein gescheiterter Beweis ist lauter als „nichts da" (§12)."""
    running, book, _root = _healthy_world()
    try:
        book.record_verification(
            generation_id=book.recent(1)[0].generation_id,
            kind=OV.KIND_RESTORE, result=OV.RESULT_FAILED,
            details={"category": "restore_verification_failed"})
        word, reason = OH.assess()
        require_equal(word, "unavailable",
                      f"ein GESCHEITERTER Beweis gilt als {word}: {reason}")
        require("GESCHEITERT" in reason, f"der Grund verschweigt es: {reason}")
    finally:
        running.__exit__()


def t_a_stale_proof_is_never_todays_health() -> None:
    """Ein ALTER Erfolg ist nicht die HEUTIGE Gesundheit."""
    running, _book, _root = _healthy_world()
    try:
        stale = OH.PROOF_STALE_AFTER_SECONDS + 3600
        word, reason = _assess_now(proof_age_seconds=stale)
        require_equal(word, "degraded",
                      f"ein {int(stale/86400)} Tage alter Beweis gilt als {word}")
        require("ueberfaellig" in reason, f"der Grund nennt es nicht: {reason}")
        # Die Gegenprobe: knapp INNERHALB der Frist bleibt gruen.
        word, _reason = _assess_now(
            proof_age_seconds=OH.PROOF_STALE_AFTER_SECONDS - 3600)
        require_equal(word, "healthy",
                      "ein frischer Beweis wurde faelschlich verworfen")
    finally:
        running.__exit__()


def t_a_stale_upload_is_degraded_even_with_a_fresh_proof() -> None:
    """Der Beweis ersetzt die Sicherung nicht — beide Fristen zaehlen."""
    running, _book, _root = _healthy_world()
    try:
        word, reason = _assess_now(
            upload_age_seconds=OH.STALE_AFTER_SECONDS + 3600)
        require_equal(word, "degraded",
                      f"eine veraltete Sicherung gilt als {word}: {reason}")
        require("letzte Offsite-Sicherung" in reason,
                f"der Grund nennt das Alter nicht: {reason}")
    finally:
        running.__exit__()


def t_a_missing_destination_is_never_healthy() -> None:
    """Eine Generation, der eine Klasse fehlt, ist nicht vollstaendig."""
    running, _book, _root = _healthy_world()
    try:
        report = OH.collect()
        last = dict(report["last_generation"])
        last["destinations"] = ["daily"]        # weekly und monthly fehlen
        word, reason = OH.assess({**report, "last_generation": last})
        require_equal(word, "degraded",
                      f"eine unvollstaendige Generation gilt als {word}")
        require("fehlen Ziele" in reason, f"der Grund nennt es nicht: {reason}")
        require("weekly" in reason and "monthly" in reason,
                f"der Grund nennt nicht WELCHE: {reason}")
    finally:
        running.__exit__()


def t_an_interrupted_run_is_visible_and_not_green() -> None:
    """Ein `uploading`-Rest nach einem Absturz ist eine offene Frage (B3)."""
    running, book, _root = _healthy_world()
    try:
        book.start(generation_id="20260901T050000Z", classes=("daily",))
        book.mark("20260901T050000Z", OL.UPLOADING)
        word, reason = OH.assess()
        require_equal(word, "degraded",
                      f"ein unterbrochener Lauf gilt als {word}: {reason}")
        require("unterbrochen" in reason, f"der Grund nennt es nicht: {reason}")
        require(any(g["generation_id"] == "20260901T050000Z"
                    for g in OH.summary()["offene_laeufe"]),
                "der unterbrochene Lauf ist im Befund unsichtbar")
    finally:
        running.__exit__()


def t_an_uploaded_but_unverified_generation_is_not_a_proof_subject() -> None:
    """`SUCCESS_STATES` enthaelt `uploaded` — die Gesundheit darf sich NICHT
    darauf stuetzen. Nur `verified` ist ein Erfolg im Sinne des Vertrags."""
    _cfg, _root = _fresh_world()
    book = OL.OffsiteLedger()
    book.start(generation_id="20260901T060000Z", classes=("daily",))
    book.mark("20260901T060000Z", OL.UPLOADED,
              object_keys={"daily": {"bucket": "b", "key": "k"}})
    report = OH.collect()
    require(report["last_generation"] is None,
            "eine nie zurueckverglichene Generation gilt als Bezugspunkt")
    word, reason = OH.assess(report)
    require_equal(word, "degraded",
                  f"nur `uploaded` gilt als {word}: {reason}")
    require(OV._pick_generation(book, "") is None,
            "der Beweis wuerde eine nie verifizierte Generation pruefen")


def t_an_unreadable_ledger_is_loud_not_green() -> None:
    running, book, _root = _healthy_world()
    try:
        with open(book.path, "wb") as fh:
            fh.write(b"das ist keine datenbank")
        word, reason = OH.assess()
        require_equal(word, "unavailable",
                      f"ein zerstoertes Buch gilt als {word}: {reason}")
        require("unlesbar" in reason, f"der Grund nennt es nicht: {reason}")
    finally:
        running.__exit__()


def t_a_corrupt_state_file_is_loud_not_green() -> None:
    running, _book, _root = _healthy_world()
    try:
        with open(OJ.state_path(), "w", encoding="utf-8") as fh:
            fh.write("{kaputt")
        word, reason = OH.assess()
        require_equal(word, "unavailable",
                      f"ein beschaedigter Betriebszustand gilt als {word}")
        require("beschaedigt" in reason, f"der Grund nennt es nicht: {reason}")
    finally:
        running.__exit__()


def t_a_locked_keychain_is_auth_required_not_key_loss() -> None:
    """Gesperrt ist NICHT verloren — sonst wird aus einer Sperre eine
    Katastrophenmeldung."""
    running, _book, _root = _healthy_world()
    try:
        word, reason = _assess_now(identity_locked=True)
        require_equal(word, "auth_required",
                      f"ein gesperrter Schluesselbund gilt als {word}")
        require("gesperrt" in reason, f"der Grund nennt es nicht: {reason}")
        word, reason = _assess_now(identity=False, identity_locked=False)
        require_equal(word, "auth_required",
                      "eine fehlende Identitaet ist auch Menschensache")
        require("fehlt" in reason, f"der Grund unterscheidet nicht: {reason}")
    finally:
        running.__exit__()


def t_a_red_source_set_is_uploaded_but_never_healthy() -> None:
    """§18: durchgereicht, nicht verschwiegen — auch in der Gesundheit."""
    running, _book, _root = _healthy_world()
    try:
        report = OH.collect()
        last = {**report["last_generation"], "source_ok": False}
        word, reason = OH.assess({**report, "last_generation": last})
        require_equal(word, "degraded",
                      f"ein roter Quellsatz gilt als {word}: {reason}")
        require("nicht gesund" in reason, f"der Grund verschweigt es: {reason}")
    finally:
        running.__exit__()


def t_not_switched_on_is_unknown_not_a_defect() -> None:
    """Opt-in ist kein Defekt (DEBT-0109)."""
    _cfg, _root = _fresh_world(enabled=False)
    word, reason = OH.assess()
    require_equal(word, "unknown", f"ausgeschaltet gilt als {word}: {reason}")
    require_equal(reason, "nicht eingeschaltet", f"falscher Grund: {reason}")


# ------------------------------------------------------- der Restore-Beweis
def t_the_proof_reads_a_real_generation_and_counts_it() -> None:
    running, book, _root = _healthy_world()
    try:
        proof = OV.restore_proof(client=running.client(), book=book)
        require(proof.ok, f"der Beweis scheiterte: {proof.reason}")
        require(proof.checked_entries > 5,
                f"der Beweis prueft zu wenig: {proof.checked_entries}")
        require(proof.sqlite_checked > 0,
                "der Beweis oeffnet keinen einzigen Speicher — dann ist er "
                "nur ein Dateilisten-Vergleich")
        rows = [r for r in book.verifications() if r["kind"] == OV.KIND_RESTORE]
        require(rows and rows[0]["result"] == OV.RESULT_OK,
                "der Beweis steht nicht im Buch")
    finally:
        running.__exit__()


def t_the_proof_catches_a_flipped_byte_in_the_archive() -> None:
    running, book, _root = _healthy_world()
    try:
        generation = book.recent(1)[0]
        entry = generation.object_keys["daily"]
        key = (entry["bucket"], entry["key"])
        payload = bytearray(running.stub.objects[key])
        payload[-3] ^= 0xFF
        running.stub.objects[key] = bytes(payload)
        proof = OV.restore_proof(client=running.client(), book=book)
        require(not proof.ok, "ein gekipptes Byte blieb unbemerkt")
        require_equal(proof.category, "integrity_failed",
                      f"falsche Kategorie: {proof.category}")
    finally:
        running.__exit__()


def t_the_proof_catches_a_generation_under_a_foreign_name() -> None:
    """§8: der Objektname bindet an den Inhalt. Eine alte Generation unter
    neuem Namen faellt auf (T4-Restfall)."""
    running, book, _root = _healthy_world()
    try:
        generation = book.recent(1)[0]
        entry = generation.object_keys["daily"]
        # Dieselbe Nutzlast unter einem FREMDEN Namen ins Buch schreiben.
        fremd = "20991231T235959Z"
        running.stub.objects[(entry["bucket"],
                              f"v1/generations/{fremd}.tar.zst.age")] = \
            running.stub.objects[(entry["bucket"], entry["key"])]
        book.start(generation_id=fremd, classes=("daily",),
                   cipher_sha256=generation.cipher_sha256,
                   snapshot_manifest_sha256=generation.snapshot_manifest_sha256)
        book.mark(fremd, OL.VERIFIED, object_keys={"daily": {
            "bucket": entry["bucket"],
            "key": f"v1/generations/{fremd}.tar.zst.age"}})
        proof = OV.restore_proof(generation_id=fremd,
                                 client=running.client(), book=book)
        require(not proof.ok,
                "eine unter fremdem Namen abgelegte Generation galt als echt")
        require_equal(proof.category, "integrity_failed",
                      f"falsche Kategorie: {proof.category}")
        require("Objektname" in proof.reason,
                f"der Grund nennt die Bindung nicht: {proof.reason}")
    finally:
        running.__exit__()


def t_the_proof_counts_rows_and_checks_schema_not_just_files() -> None:
    """Der Unterschied zwischen „die Datei ist da" und einem Beweis.

    Geprueft wird die Rechenstufe selbst, an einem gestellten Satz: eine
    SQLite-Datei, deren Zeilenzahl und Schemastand vom Manifest abweichen,
    muss auffallen — sonst waere der Restore-Beweis ein Dateilisten-
    Vergleich mit feierlichem Namen.
    """
    work = tempfile.mkdtemp(prefix="b4-zaehl-", dir=_SANDBOX)
    db_path = os.path.join(work, "Memory", "probe.sqlite3")
    os.makedirs(os.path.dirname(db_path))
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE zeilen (x TEXT)")
    conn.executemany("INSERT INTO zeilen VALUES (?)", [("a",), ("b",)])
    conn.execute("PRAGMA user_version=3")
    conn.commit()
    conn.close()

    from solvio.storage.offsite import pack as OP
    entry = {"name": "probe", "kind": "sqlite", "dest": "Memory/probe.sqlite3",
             "sha256": OP.sha256_file(db_path), "bytes": 0,
             "tables": {"zeilen": 2}, "user_version": 3}

    findings: list[str] = []
    OV._check_entries(work, {"entries": [entry]}, findings)
    require_equal(findings, [], f"der gesunde Satz erzeugte Befunde: {findings}")

    # Jetzt die Behauptung verschieben: das Manifest will 5 Zeilen und
    # Schemastand 9 — die Datei hat 2 und 3.
    findings = []
    OV._check_entries(work, {"entries": [{**entry, "tables": {"zeilen": 5},
                                          "user_version": 9}]}, findings)
    require(any("2 statt 5 Zeilen" in f for f in findings),
            f"die Zeilenzahl wurde nicht nachgerechnet: {findings}")
    require(any("Schemastand" in f for f in findings),
            f"der Schemastand wurde nicht verglichen: {findings}")

    # Und eine beschaedigte Datei faellt ebenfalls auf.
    with open(db_path, "r+b") as fh:
        fh.seek(30)
        fh.write(b"\x00" * 64)
    findings = []
    OV._check_entries(work, {"entries": [entry]}, findings)
    require(findings, "eine beschaedigte Datenbank blieb unbemerkt")


def t_the_file_hash_check_stands_on_its_own() -> None:
    """Jede Pruefebene muss ALLEIN tragen.

    Eine beschaedigte Datenbank faellt zweimal auf — der Pruefsumme UND dem
    `integrity_check`. Ein Test, der nur eine kaputte Datenbank benutzt,
    kann deshalb nicht sagen, WELCHE Ebene ihn gefunden hat; eine Mutation,
    die eine davon entfernt, ueberlebt. Hier steht deshalb eine Datei, die
    KEINE Datenbank ist: nur die Pruefsumme kann sie beurteilen.
    """
    work = tempfile.mkdtemp(prefix="b4-nurhash-", dir=_SANDBOX)
    path = os.path.join(work, "Config", "solvio-core.env")
    os.makedirs(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("SYNTHETIC=drill-value-0007\n")
    from solvio.storage.offsite import pack as OP
    entry = {"name": "core-env", "kind": "file",
             "dest": "Config/solvio-core.env",
             "sha256": OP.sha256_file(path), "bytes": 27}

    findings: list[str] = []
    OV._check_entries(work, {"entries": [entry]}, findings)
    require_equal(findings, [], f"die unveraenderte Datei fiel auf: {findings}")

    with open(path, "a", encoding="utf-8") as fh:
        fh.write("HEIMLICH=dazugekommen\n")
    findings = []
    OV._check_entries(work, {"entries": [entry]}, findings)
    require(any("Pruefsumme" in f for f in findings),
            f"eine veraenderte Datei blieb unbemerkt — die Pruefsumme traegt "
            f"nicht allein: {findings}")


def t_the_integrity_check_stands_on_its_own() -> None:
    """Die Gegenprobe: eine Beschaedigung, die die Pruefsumme NICHT sieht.

    Das ist der reale Fall von stillem Datenverlust auf einem Medium: die
    Datei ist genau die, die gesichert wurde — und trotzdem ist die
    Datenbank darin kaputt. Nur `integrity_check` findet das. (Hier wird die
    Pruefsumme NACH der Beschaedigung genommen, damit sie stimmt.)
    """
    work = tempfile.mkdtemp(prefix="b4-nurintegrity-", dir=_SANDBOX)
    db_path = os.path.join(work, "Memory", "kaputt.sqlite3")
    os.makedirs(os.path.dirname(db_path))
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE zeilen (x TEXT)")
    conn.execute("CREATE INDEX i_zeilen ON zeilen(x)")
    conn.executemany("INSERT INTO zeilen VALUES (?)",
                     [(f"wert-{i:06d}" * 10,) for i in range(2000)])
    conn.commit()
    conn.close()

    # Die Beschaedigung ist GEMESSEN, nicht geraten: bei Offset 4096 wirft
    # SQLite schon beim OEFFNEN, und dann faengt der Befund an der falschen
    # Stelle an (die Mutation ueberlebte genau daran). Eine Seite tiefer
    # OEFFNET die Datenbank sauber — und erst `integrity_check` sieht die
    # zerschossene B-Baum-Seite.
    with open(db_path, "r+b") as fh:
        fh.seek(20480 + 500)
        fh.write(b"\x7f" * 32)
    probe = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        verdict = str(probe.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        probe.close()
    require(verdict != "ok",
            "die gestellte Beschaedigung trifft integrity_check nicht mehr — "
            "dann prueft dieser Test nichts")

    from solvio.storage.offsite import pack as OP
    entry = {"name": "kaputt", "kind": "sqlite", "dest": "Memory/kaputt.sqlite3",
             # Die Pruefsumme des BESCHAEDIGTEN Standes — sie stimmt also,
             # und nur die Datenbankpruefung kann etwas finden.
             "sha256": OP.sha256_file(db_path), "bytes": 0, "tables": {}}
    findings: list[str] = []
    OV._check_entries(work, {"entries": [entry]}, findings)
    require(findings,
            "eine intakt aussehende, aber kaputte Datenbank blieb unbemerkt — "
            "`integrity_check` traegt nicht allein")
    require(any("integrity_check" in f for f in findings),
            f"der Befund kommt nicht von integrity_check: {findings}")


def t_the_proof_catches_a_book_that_claims_a_foreign_manifest() -> None:
    """Das Manifest im Archiv muss das gebuchte sein (§8)."""
    running, book, _root = _healthy_world()
    try:
        generation = book.recent(1)[0]
        # Direkt im Buch die Manifest-Pruefsumme verschieben — `start()`
        # wuerde eine verifizierte Zeile bewusst nicht zuruecksetzen.
        conn = sqlite3.connect(book.path)
        conn.execute("UPDATE offsite_generations SET "
                     "snapshot_manifest_sha256=? WHERE generation_id=?",
                     ("0" * 64, generation.generation_id))
        conn.commit()
        conn.close()
        proof = OV.restore_proof(generation_id=generation.generation_id,
                                 client=running.client(), book=book)
        require(not proof.ok, "ein fremdes Manifest blieb unbemerkt")
        require_equal(proof.category, "integrity_failed",
                      f"falsche Kategorie: {proof.category}")
    finally:
        running.__exit__()


def t_a_locked_keychain_makes_the_proof_auth_required_not_integrity() -> None:
    running, book, _root = _healthy_world()
    original = OI.read_identity
    try:
        def _locked(version=1):
            raise OI.OffsiteKeychainLocked("keychain refused with status 51")
        OI.read_identity = _locked
        proof = OV.restore_proof(client=running.client(), book=book)
        require(not proof.ok, "ein gesperrter Schluesselbund ging durch")
        require_equal(proof.category, "auth_failed",
                      f"ein gesperrter Schluesselbund wurde zu "
                      f"{proof.category!r} — das ist eine Katastrophenmeldung "
                      f"fuer eine Menschensache")
    finally:
        OI.read_identity = original
        running.__exit__()


# ------------------------------------------------------ Retention-Abgleich
def t_the_sweep_sees_a_generation_that_vanished_too_early() -> None:
    running, book, _root = _healthy_world()
    try:
        generation = book.recent(1)[0]
        entry = generation.object_keys["daily"]
        del running.stub.objects[(entry["bucket"], entry["key"])]
        result = OV.retention_sweep(client=running.client(), book=book)
        require(not result.ok, "ein zu frueh verschwundenes Objekt war ok")
        require(any(generation.generation_id in m
                    for m in result.missing_early),
                f"der Abgleich nennt es nicht: {result.missing_early}")
        require("vor der Zeit" in result.reason,
                f"der Grund ist unklar: {result.reason}")
    finally:
        running.__exit__()


def t_the_sweep_accepts_a_generation_that_expired_on_schedule() -> None:
    """Ein planmaessig ausgelaufenes Objekt ist KEIN Integritaetsverlust —
    sonst schriee der Abgleich jeden 22. Tag."""
    _cfg, _root = _fresh_world()
    book = OL.OffsiteLedger()
    alt = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=40))
    gen_id = alt.strftime("%Y%m%dT%H%M%SZ")
    book.start(generation_id=gen_id, classes=("daily",))
    book.mark(gen_id, OL.VERIFIED, object_keys={"daily": {
        "bucket": "solvio-offsite-daily",
        "key": f"v1/generations/{gen_id}.tar.zst.age"}})
    with RunningStub() as running:
        result = OV.retention_sweep(client=running.client(), book=book)
        require(result.ok,
                f"ein planmaessig ausgelaufenes Objekt galt als Verlust: "
                f"{result.reason}")
        require(any(gen_id in e for e in result.expired),
                f"der Abgleich hat es nicht als ausgelaufen gebucht: "
                f"{result.expired}")


def t_the_sweep_reports_a_foreign_object() -> None:
    """T4: ein Objekt, das das Buch nicht kennt, schreit — angefasst wird
    es nicht (SOLVIO hat kein Loeschrecht)."""
    running, book, _root = _healthy_world()
    try:
        running.stub.objects[("solvio-offsite-daily",
                              "v1/generations/20991231T000000Z.tar.zst.age")] \
            = b"fremd"
        before = dict(running.stub.objects)
        result = OV.retention_sweep(client=running.client(), book=book)
        require(not result.ok, "ein Fremdobjekt war ok")
        require(any("20991231T000000Z" in f for f in result.foreign),
                f"der Abgleich nennt es nicht: {result.foreign}")
        require_equal(running.stub.objects, before,
                      "der Abgleich hat etwas angefasst — er darf nur nachsehen")
    finally:
        running.__exit__()


# --------------------------------------------------- Verdrahtung und Playbook
def t_the_probe_is_registered_with_a_human_label() -> None:
    import asyncio

    from solvio.control_center import probes as P

    class _Dispatcher:
        def __getattr__(self, _name):
            return None

    board = P.build(_Dispatcher())
    by_key = {p.key: p for p in board}
    require("offsite" in by_key,
            f"die Fernsicherung fehlt in der Tafel: {sorted(by_key)}")
    require_equal(by_key["offsite"].label, "Fernsicherung",
                  "die Fernsicherung traegt einen anderen Namen")
    state, reason = asyncio.run(by_key["offsite"].check())
    require(state.value in ("healthy", "degraded", "unavailable",
                            "auth_required", "unknown"),
            f"die Sonde liefert ein fremdes Zustandswort: {state}")
    require(bool(reason), "die Sonde liefert keinen Grund")


def t_the_component_has_a_name_in_every_label_dictionary() -> None:
    from solvio.capabilities.doctor import _ALIASES, _LABELS as CL
    from solvio.control_center.activity import _COMPONENTS as AC
    from solvio.doctor.supervisor import _LABELS as SL

    for name, mapping in (("Chronik", AC), ("Ueberwachung", SL),
                          ("Arzt-Faehigkeit", CL)):
        require("offsite" in mapping, f"{name} kennt die Fernsicherung nicht")
    require_equal(len({AC["offsite"], SL["offsite"], CL["offsite"]}), 1,
                  "die drei Woerterbuecher nennen sie verschieden")
    require_equal(_ALIASES.get("fernsicherung"), "offsite",
                  "der gesprochene Name fuehrt nicht zur Komponente")


def t_offsite_has_no_repair_playbook_and_that_is_written_down() -> None:
    """§12: kein Playbook — und dieses Haus schreibt so etwas HIN, statt es
    wegzulassen (Muster Freigabe-Gateway)."""
    from solvio.doctor import playbooks as P

    require_equal(P.for_component("offsite"), [],
                  "es gibt ein Reparatur-Playbook fuer die Fernsicherung")
    require("offsite" in P.FORBIDDEN_RESTARTS,
            "das Verbot steht nicht in FORBIDDEN_RESTARTS — dann erfindet "
            "es spaeter jemand")
    source = open(P.__file__, encoding="utf-8").read()
    marker = source.split("FORBIDDEN_RESTARTS =")[0]
    require("`offsite` steht hier" in marker,
            "das Verbot ist nicht begruendet — weglassen ohne Grund ist "
            "genau das, was dieses Projekt nicht tut")


def t_the_offsite_probe_reads_only_local_sources() -> None:
    """§12: die Auskunft gilt auch ohne Netz. Die Bewertung darf deshalb
    keinen Anbieter befragen."""
    import ast

    source = open(OH.__file__, encoding="utf-8").read()
    tree = ast.parse(source)
    imported = {n.module for n in ast.walk(tree)
                if isinstance(n, ast.ImportFrom) and n.module}
    require("solvio.storage.offsite.s3" not in imported,
            "die Gesundheit importiert den S3-Client — dann haengt eine "
            "oertliche Auskunft am Netz")
    for forbidden in ("S3Client", "list_objects", "head_object"):
        require(forbidden not in source,
                f"die Gesundheit benutzt {forbidden}")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
