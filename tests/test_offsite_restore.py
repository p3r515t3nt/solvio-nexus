"""Offsite V1 B5 — zurueckholen, ohne dabei etwas kaputtzumachen.

Zwei Wahrheiten, streng getrennt, und diese Suite haelt beide:

**Der Restore darf nie die laufende Installation beschaedigen.** Er schreibt
in ein leeres Wegwerfziel oder gar nicht; `PRODUCTION_PATHS` gilt
unveraendert, und der Reconcile fasst fremde Datenbanken ausschliesslich
SCHREIBGESCHUETZT an.

**Der Katastrophenpfad darf den Tresor nicht voraussetzen.** Wer nach einem
Totalverlust vor einem neuen Mac sitzt, hat Provider-Zugang, Passphrase und
Umschlag — keinen Schluesselbund und keinen Vault. Ein Restore-Weg, der
einen davon braucht, ist zirkulaer und im Ernstfall wertlos. Deshalb hier
eine eigene Zusicherung: die Identitaet kann von AUSSEN kommen.

Fail-closed heisst an jeder Stufe: ein teilweiser Restore ist KEIN Erfolg.

ASSERTION POLICY: `require*` aus `tests/_guard.py` sind Funktionsaufrufe und
ueberleben `python -O`.
"""
from __future__ import annotations

import atexit
import json
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from _guard import enforce_assertions, require, require_equal, require_raises  # noqa: E402

enforce_assertions()

_SANDBOX = tempfile.mkdtemp(prefix="solvio-offsite-b5-")
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
from solvio.storage.offsite import identity as OI             # noqa: E402
from solvio.storage.offsite import job as OJ                  # noqa: E402
from solvio.storage.offsite import ledger as OL               # noqa: E402
from solvio.storage.offsite import restore as ORS             # noqa: E402

FAKE_CREDENTIAL = json.dumps({
    "access_key_id": "SYNTHETIC-DRILL-KEY-0001",
    "secret_access_key": "synthetic-drill-value-0003"}).encode()


# --------------------------------------------------------------------- Werkzeug
def _world() -> tuple[RunningStub, OL.OffsiteLedger, str, str]:
    """Eine echte Generation im Stub — der Aufrufer schliesst den Stub.

    Gibt (Stub, Buch, Wurzel, Identitaetszeile) zurueck. Die Identitaet
    reist mit, weil der Katastrophenfall sie von aussen bekommt.
    """
    root = tempfile.mkdtemp(prefix="b5-case-", dir=_SANDBOX)
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
    cfg = OC.OffsiteConfig(enabled=True, recipient=recipient,
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
    running = RunningStub()
    running.__enter__()
    book = OL.OffsiteLedger()
    outcome = OJ.run_once(force=True, client=running.client(), book=book)
    require(outcome["ok"], f"die Vorbereitung scheiterte: {outcome}")
    return running, book, root, line


def _target(root: str, name: str = "ziel") -> str:
    return os.path.join(root, name)


# ------------------------------------------------------------ Testisolation
def t_this_suite_cannot_touch_production_state() -> None:
    require("/.solvio/offsite.sqlite3" not in OL.ledger_path(),
            "das Offsite-Buch ist nicht umgelenkt")
    require(OI.is_test_backend(), "der Schluesselspeicher ist nicht umgelenkt")


# ----------------------------------------------- die oberste B5-Invariante
def t_a_restore_into_a_production_path_is_refused() -> None:
    """Ein Restore, der die laufende Installation beruehrt, ist keiner."""
    running, book, _root, _line = _world()
    try:
        for path in ("~/.solvio", "~/.solvio-vault", "~/solvio-core",
                     "~/.solvio/offsite", "~/Library/LaunchAgents"):
            report = ORS.restore(target=path, client=running.client(),
                                 book=book)
            require(not report.ok,
                    f"ein Restore nach {path} wurde zugelassen")
            require("verweigert" in report.reason.lower()
                    or "beruehrt" in report.reason.lower(),
                    f"{path}: unklarer Grund: {report.reason}")
    finally:
        running.__exit__()


def t_a_restore_into_a_nonempty_target_is_refused() -> None:
    """Zwei Wahrheiten in einem Baum sind schlimmer als keine."""
    running, book, root, _line = _world()
    try:
        target = _target(root, "belegt")
        os.makedirs(target)
        with open(os.path.join(target, "alt.txt"), "w") as fh:
            fh.write("etwas Altes")
        report = ORS.restore(target=target, client=running.client(), book=book)
        require(not report.ok, "in ein belegtes Ziel wurde restauriert")
        require("nicht leer" in report.reason,
                f"unklarer Grund: {report.reason}")
        require(os.path.isfile(os.path.join(target, "alt.txt")),
                "der abgelehnte Restore hat trotzdem etwas angefasst")
    finally:
        running.__exit__()


def t_the_reconcile_opens_foreign_databases_readonly_only() -> None:
    """§13: pruefen, zaehlen, berichten — NIE fremde Zustaende schreiben."""
    import ast

    source = open(ORS.__file__, encoding="utf-8").read()
    require("mode=ro" in source,
            "der Reconcile oeffnet fremde Datenbanken nicht schreibgeschuetzt")
    tree = ast.parse(source)
    writers: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "execute":
            for arg in node.args:
                text = ast.get_source_segment(source, arg) or ""
                upper = text.upper()
                if any(verb in upper for verb in ("INSERT", "UPDATE", "DELETE",
                                                  "DROP", "ALTER", "CREATE")):
                    writers.append(text[:60])
    require_equal(writers, [],
                  f"der Restore schreibt in fremde Datenbanken: {writers}")


def t_the_reconcile_writes_exactly_one_marker_and_counts_the_rest() -> None:
    running, book, root, _line = _world()
    try:
        target = _target(root)
        report = ORS.restore(target=target, client=running.client(), book=book)
        require(report.ok, f"der Restore scheiterte: {report.reason}")
        before = {p for p in _walk(target)}
        marker = ORS.finalize(target, book=book)
        after = {p for p in _walk(target)}
        neu = sorted(after - before)
        require_equal(neu, [ORS.MARKER_NAME],
                      f"der Reconcile hat mehr als seine Marke geschrieben: {neu}")
        require_equal(marker["restored_from"], report.generation_id,
                      "die Marke nennt die falsche Generation")
        for name in ("approvals", "payment", "agent_runs"):
            require(name in marker["counts"],
                    f"der Bericht zaehlt {name} nicht")
        payment = marker["counts"]["payment"]
        if payment.get("present"):
            require("owner_action" in payment,
                    "der Bericht sagt nicht, dass die Revalidierung ansteht")
        require(any("expiriert" in n for n in marker["notes"]),
                "der Bericht erklaert die Freigabe-Grenze nicht")
    finally:
        running.__exit__()


def _walk(root: str) -> list[str]:
    out = []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            out.append(os.path.relpath(os.path.join(dirpath, name), root))
    return out


# ------------------------------------------- die Kette, fail-closed an jeder Stufe
def t_a_full_restore_proves_the_whole_chain() -> None:
    running, book, root, _line = _world()
    try:
        target = _target(root)
        report = ORS.restore(target=target, client=running.client(), book=book)
        require(report.ok, f"der Restore scheiterte: {report.reason}")
        require(report.restored_entries > 5,
                f"zu wenig wiederhergestellt: {report.restored_entries}")
        require(report.sqlite_checked > 0,
                "kein einziger Speicher geprueft — dann ist es ein Kopiervorgang")
        require(report.source.get("key", "").startswith("v1/generations/"),
                f"die Quelle ist nicht gebunden: {report.source}")
        require(report.source.get("version_id"),
                "die Provider-VersionId wurde nicht festgehalten")
        for expected in ("manifest.json", "offsite.json"):
            require(os.path.isfile(os.path.join(target, expected)),
                    f"{expected} fehlt im Ziel — der Satz waere nicht mehr "
                    f"pruefbar")
        rows = [r for r in book.verifications()
                if r["kind"] == "restore_run"]
        require(rows and rows[0]["result"] == "ok",
                "der Restore-Lauf steht nicht im Buch")
    finally:
        running.__exit__()


def t_a_wrong_ciphertext_hash_stops_the_restore() -> None:
    """Der Hash-Vergleich muss ALLEIN tragen.

    Ein gekipptes Byte faellt auch der Entschluesselung auf (age
    authentifiziert) — ein Test, der nur Bytes kippt, prueft deshalb die
    age-Bibliothek, nicht diese Zeile. Eine Mutation, die den Vergleich
    entfernte, ueberlebte genau daran. Hier ist deshalb das OBJEKT in
    Ordnung und das BUCH behauptet eine andere Pruefsumme: der Restore
    muss stehenbleiben, BEVOR er entschluesselt.
    """
    running, book, root, _line = _world()
    try:
        generation = book.recent(1)[0]
        conn = sqlite3.connect(book.path)
        conn.execute("UPDATE offsite_generations SET cipher_sha256=? "
                     "WHERE generation_id=?",
                     ("b" * 64, generation.generation_id))
        conn.commit()
        conn.close()
        report = ORS.restore(target=_target(root), client=running.client(),
                             book=book)
        require(not report.ok,
                "ein Geheimtext, der nicht dem Buch entspricht, wurde "
                "restauriert")
        require_equal(report.category, "integrity_failed",
                      f"falsche Kategorie: {report.category}")
        require("Geheimtext weicht ab" in report.reason,
                f"der Grund nennt den Pruefsummenvergleich nicht: "
                f"{report.reason}")
    finally:
        running.__exit__()


def t_a_tampered_object_also_fails_at_decryption() -> None:
    """Die zweite, unabhaengige Ebene: age authentifiziert je Chunk."""
    running, book, root, _line = _world()
    try:
        generation = book.recent(1)[0]
        entry = generation.object_keys["daily"]
        payload = bytearray(running.stub.objects[(entry["bucket"],
                                                  entry["key"])])
        payload[-3] ^= 0xFF
        running.stub.objects[(entry["bucket"], entry["key"])] = bytes(payload)
        report = ORS.restore(target=_target(root, "kipp"),
                             client=running.client(), book=book)
        require(not report.ok, "ein veraenderter Geheimtext wurde restauriert")
        require_equal(report.category, "integrity_failed",
                      f"falsche Kategorie: {report.category}")
    finally:
        running.__exit__()


def t_an_unverified_generation_is_not_a_restore_source() -> None:
    """Aus einer Behauptung stellt man nichts wieder her."""
    running, book, root, _line = _world()
    try:
        book.start(generation_id="20260901T090000Z", classes=("daily",))
        book.mark("20260901T090000Z", OL.UPLOADED,
                  object_keys={"daily": {"bucket": "b", "key": "k"}})
        report = ORS.restore(generation_id="20260901T090000Z",
                             target=_target(root, "z2"),
                             client=running.client(), book=book)
        require(not report.ok, "eine nur hochgeladene Generation war Quelle")
        require("verifizierte" in report.reason,
                f"unklarer Grund: {report.reason}")
        require_raises(ORS.RestoreError, ORS.pick_generation, book,
                       "20260901T099999Z",
                       message="eine unbekannte Generation wurde akzeptiert")
    finally:
        running.__exit__()


def t_a_foreign_generation_id_inside_stops_the_restore() -> None:
    """§8: der Objektname bindet an den Inhalt."""
    running, book, root, _line = _world()
    try:
        generation = book.recent(1)[0]
        entry = generation.object_keys["daily"]
        fremd = "20991231T235959Z"
        key = f"v1/generations/{fremd}.tar.zst.age"
        running.stub.objects[(entry["bucket"], key)] = \
            running.stub.objects[(entry["bucket"], entry["key"])]
        book.start(generation_id=fremd, classes=("daily",),
                   cipher_sha256=generation.cipher_sha256)
        book.mark(fremd, OL.VERIFIED, object_keys={
            "daily": {"bucket": entry["bucket"], "key": key}})
        report = ORS.restore(generation_id=fremd, target=_target(root, "z3"),
                             client=running.client(), book=book)
        require(not report.ok, "eine umbenannte Generation wurde restauriert")
        require_equal(report.category, "integrity_failed",
                      f"falsche Kategorie: {report.category}")
        require("Objektname" in report.reason,
                f"der Grund nennt die Bindung nicht: {report.reason}")
    finally:
        running.__exit__()


def t_an_incomplete_set_is_a_failure_not_a_partial_success() -> None:
    """Ein teilweiser Restore ist KEIN Erfolg — er ist ein Fehlschlag mit
    Resten. Geprueft wird gegen die INVENTUR, nicht gegen das Manifest
    selbst: ein Manifest ist immer vollstaendig in Bezug auf sich."""
    running, _book, root, _line = _world()
    try:
        target = _target(root, "unvollstaendig")
        os.makedirs(target)
        with open(os.path.join(target, "manifest.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"format_version": 1, "backup_id": "probe",
                       "entries": [], "skipped": []}, fh)
        with open(os.path.join(target, "offsite.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"offsite_format": 1,
                       "generation_id": "20260901T000000Z"}, fh)
        report = ORS.verify_unpacked(target, require_complete=True)
        require(not report.ok, "ein leerer Satz galt als vollstaendig")
        require(any("Pflichtstueck" in f for f in report.findings),
                f"die Vollstaendigkeit wurde nicht gegen die Inventur "
                f"geprueft: {report.findings[:3]}")
    finally:
        running.__exit__()


def t_an_unknown_manifest_version_is_refused_not_guessed() -> None:
    """Fail-closed nach BEIDEN Seiten — auch nach oben."""
    root = tempfile.mkdtemp(prefix="b5-fassung-", dir=_SANDBOX)
    for version in (0, 2, 99):
        target = os.path.join(root, f"v{version}")
        os.makedirs(target)
        with open(os.path.join(target, "manifest.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"format_version": version, "entries": []}, fh)
        report = ORS.verify_unpacked(target, require_complete=False)
        require(not report.ok,
                f"Manifest-Fassung {version} wurde stillschweigend gelesen")
        require("Fassung" in report.reason,
                f"unklarer Grund: {report.reason}")


def t_a_broken_sqlite_in_the_set_fails_the_restore() -> None:
    running, book, root, _line = _world()
    try:
        target = _target(root, "kaputt")
        report = ORS.restore(target=target, client=running.client(),
                             book=book, keep_archive=True)
        require(report.ok, f"die Vorbereitung scheiterte: {report.reason}")
        # Jetzt eine Datenbank IM ENTPACKTEN Satz beschaedigen und die
        # Pruefung erneut fahren: sie muss anschlagen.
        db = os.path.join(target, "Memory", "memory.sqlite3")
        require(os.path.isfile(db), "die Probe-Datenbank fehlt")
        with open(db, "r+b") as fh:
            fh.seek(40)
            fh.write(b"\x00" * 128)
        again = ORS.verify_unpacked(target, require_complete=False)
        require(not again.ok, "eine beschaedigte Datenbank blieb unbemerkt")
        require(any("memory" in f for f in again.findings),
                f"der Befund nennt sie nicht: {again.findings[:3]}")
    finally:
        running.__exit__()


def t_only_restores_just_what_was_asked_for() -> None:
    running, book, root, _line = _world()
    try:
        target = _target(root, "nur-memory")
        report = ORS.restore(target=target, only=("memory",),
                             client=running.client(), book=book)
        require(report.ok, f"der Teil-Restore scheiterte: {report.reason}")
        require(os.path.isfile(os.path.join(target, "Memory",
                                            "memory.sqlite3")),
                "der angefragte Speicher fehlt")
        require(not os.path.exists(os.path.join(target, "Vault")),
                "es wurde mehr wiederhergestellt als angefragt")
        # Und ein Name, den es nicht gibt, ist ein Fehlschlag, kein Nichts.
        leer = ORS.restore(target=_target(root, "gibt-es-nicht"),
                           only=("gibt-es-nicht",), client=running.client(),
                           book=book)
        require(not leer.ok, "ein unbekannter Eintrag galt als Erfolg")
        require("nicht im Satz" in leer.reason,
                f"unklarer Grund: {leer.reason}")
    finally:
        running.__exit__()


# ------------------------------------------- die Katastrophe: kein Vault noetig
def t_the_disaster_path_needs_no_keychain_and_no_vault() -> None:
    """Die Zirkelfreiheit, als Zusicherung.

    Wer vor einem neuen Mac sitzt, hat Provider-Zugang, Passphrase und
    Umschlag — sonst nichts. Ein Restore-Weg, der den Schluesselbund oder
    den Tresor braucht, um an die Identitaet zu kommen, ist im Ernstfall
    wertlos. Hier wird der Schluesselbund ABGERAEUMT und die Identitaet von
    aussen gegeben — genau wie im Runbook.
    """
    running, book, root, line = _world()
    try:
        generation = book.recent(1)[0]
        entry = generation.object_keys["daily"]
        archive = os.path.join(root, "dr.tar.zst.age")
        with open(archive, "wb") as fh:
            fh.write(running.stub.objects[(entry["bucket"], entry["key"])])

        OI.forget_identity(1)
        require(OI.read_identity(1) is None,
                "der Schluesselbund ist nicht leer — der Test prueft nichts")

        # Ohne Identitaet von aussen: sauberer, benannter Abbruch.
        ohne = os.path.join(root, "dr-ohne")
        error = require_raises(
            ORS.RestoreError, ORS.open_generation, archive,
            dest_dir=ohne, cfg=OC.load(),
            message="ohne jede Identitaet wurde entpackt")
        require_equal(error.category, "key_unavailable",
                      f"falsche Kategorie: {error.category}")
        require("Umschlag" in str(error),
                f"der Fehler verweist nicht auf den Umschlag: {error}")

        # MIT Identitaet von aussen: der Katastrophenweg traegt.
        mit = os.path.join(root, "dr-mit")
        ORS.open_generation(archive, dest_dir=mit, identity_line=line,
                            cfg=OC.load())
        report = ORS.verify_unpacked(mit,
                                     expect_generation=generation.generation_id,
                                     require_complete=True)
        require(report.ok,
                f"der Katastrophenweg scheiterte: {report.reason} "
                f"{report.findings[:2]}")
        require(report.sqlite_checked > 0,
                "der Katastrophenweg prueft keine Datenbank")
    finally:
        running.__exit__()


def t_the_recovery_envelope_is_never_inside_the_archive_it_opens() -> None:
    """§5, bindend: der Umschlag ist ein SEPARATES Objekt.

    Ein Umschlag INNERHALB der Huelle, die er oeffnet, waere ein Schloss im
    eigenen Tresor. Die Kopie, die ueber das Inventur-Item in einen Satz
    geraet, ist beilaeufige Redundanz — nie der Recovery-Pfad.
    """
    from solvio.storage.offsite import config as _OC
    require_equal(_OC.RECOVERY_PREFIX, "v1/recovery/",
                  "der Recovery-Prefix hat sich verschoben")
    require(_OC.RECOVERY_PREFIX != _OC.GENERATIONS_PREFIX,
            "Umschlag und Generationen liegen unter demselben Prefix")

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
    try:
        import offsite_admin as OA
    finally:
        sys.path.pop(0)
    source = open(OA.__file__, encoding="utf-8").read()
    block = source.split("def cmd_envelope_publish")[1].split("\ndef ")[0]
    require("RECOVERY_PREFIX" in block,
            "der Umschlag wird nicht unter v1/recovery/ abgelegt")
    require('bucket("monthly")' in block,
            "der Umschlag landet nicht im monthly-Bucket (laengster Schutz)")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
