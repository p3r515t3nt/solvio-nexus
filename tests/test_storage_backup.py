"""Speicher und Sicherung — was die Maschine tut und was sie verweigert.

Der Schwerpunkt liegt nicht darauf, dass eine Sicherung entsteht. Er liegt auf
den vier Verweigerungen, ohne die eine Sicherung ein Sicherheitsproblem waere:

* sie schreibt nichts Privates auf eine unverschluesselte Platte,
* sie glaubt keinem Volume-Namen, sondern nur einer UUID,
* sie packt nicht in ein produktives Verzeichnis aus,
* und sie meldet keinen Erfolg, den sie nicht nachgerechnet hat.

Dazu die eine Eigenschaft, an der der ganze Milestone haengt: **ohne die Platte
laeuft SOLVIO weiter.** Wenn ein Test hier gruen ist und die Platte fehlt, ist
das kein Zufall, sondern die Zusicherung.

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
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from _guard import enforce_assertions, require, require_equal, require_raises  # noqa: E402

from solvio.storage import engine, health, inventory, restore, volume  # noqa: E402
from solvio.storage.job import plan_retention  # noqa: E402
from solvio.storage.sqlite_snapshot import (SnapshotError, snapshot,  # noqa: E402
                                            verify_snapshot)

enforce_assertions()

# Der Betriebszustand der Sicherung wird umgelenkt, BEVOR ein Test laeuft.
# Ohne das schreibt ein Testlauf in `~/.solvio/storage/state.json` — und genau
# das ist einmal passiert: die produktive Datei verlor `last_backup_id` und trug
# danach eine erfundene Erfolgszeit. Ein Werkzeug gegen Datenverlust darf ihn
# nicht selbst verursachen.
_STATE_SANDBOX = tempfile.mkdtemp(prefix="solvio-storage-state-")
os.environ["SOLVIO_STORAGE_STATE_DIR"] = _STATE_SANDBOX
os.environ["SOLVIO_STORAGE_CONFIG"] = os.path.join(_STATE_SANDBOX, "storage.json")
atexit.register(shutil.rmtree, _STATE_SANDBOX, True)


UUID_A = "AAAAAAAA-1111-2222-3333-444444444444"
UUID_B = "BBBBBBBB-1111-2222-3333-444444444444"


# --------------------------------------------------------------------- Werkzeug
def _tmp(prefix: str) -> str:
    return tempfile.mkdtemp(prefix=f"solvio-storage-{prefix}-")


def _make_db(path: str, rows: int, *, wal: bool = True) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        if wal:
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, body TEXT)")
        conn.executemany("INSERT INTO notes (body) VALUES (?)",
                         [(f"zeile {i}",) for i in range(rows)])
        conn.commit()
    finally:
        conn.close()


def _fake_state(**kw) -> volume.VolumeState:
    base = dict(configured=True, volume_uuid=UUID_A, present=True, mounted=True,
                mount_point="/tmp/nonexistent", volume_name="Probe",
                filesystem="apfs", encrypted=True, locked=False,
                total_bytes=2 * 1024 ** 4, free_bytes=1024 ** 4)
    base.update(kw)
    return volume.VolumeState(**base)


# ------------------------------------------------------------ SQLite-Konsistenz
def t_snapshot_captures_uncheckpointed_wal_that_cp_would_lose() -> None:
    """Der Kernbeweis: `cp` verliert, was im WAL steht — der Schnappschuss nicht.

    Am lebenden System fehlten einer nackten Dateikopie des Freigabespeichers
    zwei Drittel der Audit-Spur. Hier wird derselbe Zustand nachgestellt: ein
    Schreiber haelt die Verbindung offen, das WAL ist nicht ausgecheckpointet.
    """
    work = _tmp("wal")
    try:
        src = os.path.join(work, "live.sqlite3")
        _make_db(src, 5)
        keeper = sqlite3.connect(src)          # haelt das WAL offen
        try:
            keeper.execute("PRAGMA journal_mode=WAL")
            keeper.executemany("INSERT INTO notes (body) VALUES (?)",
                               [(f"spaet {i}",) for i in range(200)])
            keeper.commit()

            naive = os.path.join(work, "naive.sqlite3")
            shutil.copyfile(src, naive)        # der falsche Weg
            naive_conn = sqlite3.connect(naive)
            try:
                naive_rows = naive_conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
            finally:
                naive_conn.close()

            good = os.path.join(work, "snap.sqlite3")
            result = snapshot(src, good)
        finally:
            keeper.close()

        require_equal(result.tables["notes"], 205,
                      "der Schnappschuss hat nicht alle Zeilen")
        require(naive_rows < 205,
                f"die nackte Kopie haette verlieren muessen, hatte aber {naive_rows}")
        require_equal(result.integrity, "ok", "integrity_check nicht ok")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def t_snapshot_falls_back_when_readonly_open_is_impossible() -> None:
    """WAL ohne `-shm`: `mode=ro` scheitert, der Schnappschuss darf es nicht.

    Genau diese Lage haben `doctor.sqlite3` und `proactive.sqlite3` am lebenden
    System — sie werden je Vorgang geoeffnet und geschlossen. Ein Werkzeug, das
    nur schreibgeschuetzt oeffnet, faellt dort aus.
    """
    work = _tmp("ro")
    try:
        src = os.path.join(work, "closed.sqlite3")
        _make_db(src, 12)
        for sfx in ("-wal", "-shm"):
            stray = src + sfx
            if os.path.exists(stray):
                os.remove(stray)
        require(not os.path.exists(src + "-shm"), "der Aufbau stimmt nicht: -shm da")

        ro_failed = False
        try:
            conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
            conn.execute("SELECT COUNT(*) FROM notes").fetchone()
            conn.close()
        except sqlite3.Error:
            ro_failed = True

        result = snapshot(src, os.path.join(work, "snap.sqlite3"))
        require_equal(result.tables["notes"], 12, "Zeilen fehlen")
        if ro_failed:
            require(not result.opened_readonly,
                    "der Schnappschuss behauptet den schreibgeschuetzten Weg, "
                    "obwohl der gar nicht geht")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def t_snapshot_leaves_no_sidecars_and_reports_them() -> None:
    work = _tmp("side")
    try:
        src = os.path.join(work, "a.sqlite3")
        _make_db(src, 3)
        dest = os.path.join(work, "b.sqlite3")
        snapshot(src, dest)
        for sfx in ("-wal", "-shm"):
            require(not os.path.exists(dest + sfx),
                    f"die Sicherung hat eine Begleitdatei {sfx}")
        require_equal(oct(os.stat(dest).st_mode & 0o777), "0o600",
                      "die Sicherung ist nicht 0600")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def t_a_snapshot_can_always_be_opened_readonly() -> None:
    """Die Sicherung einer WAL-Datenbank darf nicht selbst WAL sein.

    Die Backup-API kopiert Seiten samt Dateikopf — ohne ausdrueckliches
    Umschalten traegt die Sicherung den WAL-Modus der Quelle und ist dann
    schreibgeschuetzt nicht zu oeffnen. Eine Sicherung, die man zum Pruefen
    anfassen muss, ist keine.
    """
    work = _tmp("romode")
    try:
        src = os.path.join(work, "wal.sqlite3")
        _make_db(src, 7, wal=True)
        dest = os.path.join(work, "snap.sqlite3")
        snapshot(src, dest)
        conn = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            rows = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
        finally:
            conn.close()
        require(str(mode).lower() != "wal",
                f"die Sicherung traegt noch WAL im Kopf: {mode}")
        require_equal(rows, 7, "die schreibgeschuetzt gelesene Sicherung ist unvollstaendig")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def t_verify_snapshot_detects_a_changed_byte() -> None:
    work = _tmp("bitrot")
    try:
        src = os.path.join(work, "a.sqlite3")
        _make_db(src, 40)
        dest = os.path.join(work, "b.sqlite3")
        result = snapshot(src, dest)
        require(verify_snapshot(dest, result.sha256)["ok"], "frische Sicherung gilt nicht")
        os.chmod(dest, 0o600)
        with open(dest, "r+b") as fh:
            fh.seek(os.path.getsize(dest) - 8)
            fh.write(b"\x00\x01\x02\x03")
        check = verify_snapshot(dest, result.sha256)
        require(not check["ok"], "eine veraenderte Sicherung galt als in Ordnung")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def t_snapshot_records_schema_state_for_restore_drift() -> None:
    """§8 Offsite V1: Tabellenzaehler erkennen einen Restore in einen
    AELTEREN Codestand nicht. `user_version` und der Stand der
    Migrationstabelle muessen deshalb im Manifest landen — geurteilt ueber
    die Sicherung, nicht ueber die Quelle."""
    work = _tmp("schemastand")
    try:
        src = os.path.join(work, "a.sqlite3")
        _make_db(src, 3)
        conn = sqlite3.connect(src)
        conn.execute("PRAGMA user_version=7")
        conn.execute("CREATE TABLE schema_migrations (version TEXT)")
        conn.execute("INSERT INTO schema_migrations VALUES ('0001'), ('0002')")
        conn.commit()
        conn.close()
        dest = os.path.join(work, "b.sqlite3")
        result = snapshot(src, dest)
        require_equal(result.user_version, 7,
                      "user_version fehlt im Schnappschuss-Ergebnis")
        require(result.schema_stand is not None
                and result.schema_stand["table"] == "schema_migrations"
                and result.schema_stand["rows"] == 2
                and result.schema_stand["sha256"],
                "der Stand der Migrationstabelle fehlt im Ergebnis")
        require("user_version" in result.as_dict(),
                "user_version erreicht das Manifest nicht")
        check = verify_snapshot(dest, result.sha256)
        require_equal(check["user_version"], 7,
                      "der Restore-Beweis sieht den Schemastand nicht")
        # Ein Speicher OHNE Migrationstabelle traegt ehrlich None, nicht 0-irgendwas.
        plain = os.path.join(work, "c.sqlite3")
        _make_db(plain, 1)
        plain_result = snapshot(plain, os.path.join(work, "d.sqlite3"))
        require(plain_result.schema_stand is None,
                "ein Speicher ohne Migrationstabelle erfand einen Stand")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def t_the_manifest_carries_the_schema_state() -> None:
    """Die Naht, die die Live-Abnahme gefunden hat: `snapshot()` lieferte
    `user_version` laengst, aber die Maschine baute den Manifest-Eintrag von
    Hand und liess das Feld fallen. §8 verlangt es IM Manifest — dort liest
    es der Restore-Beweis."""
    work = _tmp("schemamanifest")
    try:
        root = os.path.join(work, "vol")
        state = _fake_state(mount_point=root)
        config = volume.StorageConfig(volume_uuid=UUID_A)
        src = os.path.join(work, "s.sqlite3")
        _make_db(src, 2)
        conn = sqlite3.connect(src)
        conn.execute("PRAGMA user_version=7")
        conn.commit()
        conn.close()
        item = inventory.Item("schema-probe", inventory.SQLITE, src,
                              "RuntimeState/s.sqlite3", "runtime", "Probe")
        original_items, original_info = inventory.items, volume._diskutil_info
        try:
            inventory.items = lambda: (item,)
            volume._diskutil_info = lambda _i: {
                "VolumeUUID": UUID_A, "VolumeName": "Probe", "FilesystemType": "apfs",
                "Encryption": True, "MountPoint": root, "Locked": False,
                "Internal": False, "GlobalPermissionsEnabled": True}
            os.makedirs(root, exist_ok=True)
            result = engine.run_backup(config=config, state=state,
                                       include_repos=False)
        finally:
            inventory.items, volume._diskutil_info = original_items, original_info
        require(result.ok, f"die Probe-Sicherung scheiterte: {result.errors}")
        with open(os.path.join(result.path, engine.MANIFEST_NAME), encoding="utf-8") as fh:
            manifest = json.load(fh)
        entry = next(e for e in manifest["entries"] if e["name"] == "schema-probe")
        require_equal(entry.get("user_version"), 7,
                      "user_version steht nicht im Manifest")
        require("schema_stand" in entry,
                "schema_stand steht nicht im Manifest")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def t_snapshot_refuses_a_missing_source() -> None:
    require_raises(SnapshotError, snapshot, "/nonexistent/nope.sqlite3", "/tmp/x.sqlite3",
                   message="ein fehlender Speicher wurde stillschweigend gesichert")


# ------------------------------------------------------------- Identitaet der Platte
def t_volume_is_identified_by_uuid_not_by_name() -> None:
    """Eine fremde Platte mit demselben Namen ist nicht die SOLVIO-Platte.

    Der Aufbau ist bewusst so, dass ein Rueckfall auf den Namen ERFOLG haette:
    unter dem Einhaengepunkt und unter dem Namen antwortet eine Platte — nur
    unter der UUID nicht. Eine frueh gefangene Mutation hat genau diesen
    Rueckfall eingebaut und ueberlebte, weil der Test ihn nicht belohnen konnte.
    """
    stranger = {"VolumeUUID": UUID_B, "VolumeName": "SSD 2TB",
                "FilesystemType": "apfs", "Encryption": True,
                "MountPoint": "/Volumes/SSD 2TB", "Locked": False,
                "Internal": False, "GlobalPermissionsEnabled": True}
    # Jede Frage AUSSER der nach unserer UUID wird beantwortet.
    table = {UUID_B: stranger, "/Volumes/SSD 2TB": stranger, "SSD 2TB": stranger,
             "disk9s1": stranger}
    original = volume._diskutil_info
    try:
        volume._diskutil_info = lambda ident: table.get(ident)
        config = volume.StorageConfig(volume_uuid=UUID_A, label_hint="SSD 2TB")
        state = volume.probe(config)
        require(not state.present, "eine fremde Platte galt als angeschlossen")
        require(not state.usable, "eine fremde Platte war benutzbar")
        require(state.volume_uuid != UUID_B,
                "die fremde UUID wurde uebernommen")
        require_raises(volume.StorageUnavailable, volume.storage_root, state, config,
                       message="es gab einen Schreibpfad auf eine fremde Platte")
    finally:
        volume._diskutil_info = original


def t_a_failed_backup_is_never_written_as_successful() -> None:
    """Ein Satz mit Fehlern traegt `ok: false` — im Manifest UND im Ergebnis.

    Der teuerste denkbare Defekt einer Sicherung ist nicht, dass sie scheitert,
    sondern dass sie das verschweigt. Deshalb laeuft hier die echte Maschine
    gegen ein echtes Verzeichnis, mit einem Bestand, dessen Pflichtstueck fehlt.
    """
    work = _tmp("failrun")
    try:
        root = os.path.join(work, "vol")
        state = _fake_state(mount_point=root)
        config = volume.StorageConfig(volume_uuid=UUID_A)
        missing = inventory.Item(
            "gibt-es-nicht", inventory.FILE, os.path.join(work, "weg.txt"),
            "Config/weg.txt", "config", "Pflichtstueck fuer die Probe", required=True)
        original_items, original_info = inventory.items, volume._diskutil_info
        try:
            inventory.items = lambda: (missing,)
            volume._diskutil_info = lambda _i: {
                "VolumeUUID": UUID_A, "VolumeName": "Probe", "FilesystemType": "apfs",
                "Encryption": True, "MountPoint": root, "Locked": False,
                "Internal": False, "GlobalPermissionsEnabled": True}
            os.makedirs(root, exist_ok=True)
            result = engine.run_backup(config=config, state=state,
                                       include_repos=False)
        finally:
            inventory.items, volume._diskutil_info = original_items, original_info

        require(not result.ok, "eine Sicherung mit fehlendem Pflichtstueck galt als gelungen")
        require(result.errors, "es wurde kein Fehler aufgeschrieben")
        with open(os.path.join(result.path, engine.MANIFEST_NAME), encoding="utf-8") as fh:
            manifest = json.load(fh)
        require_equal(manifest["ok"], False, "das Manifest behauptet Erfolg")
        require(manifest["errors"], "das Manifest verschweigt den Fehler")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def t_a_clean_backup_says_so_and_leaves_no_incoming() -> None:
    """Die Gegenprobe: ein sauberer Lauf ist gruen und hinterlaesst keinen Rest."""
    work = _tmp("okrun")
    try:
        root = os.path.join(work, "vol")
        os.makedirs(root, exist_ok=True)
        payload = os.path.join(work, "da.txt")
        with open(payload, "w", encoding="utf-8") as fh:
            fh.write("inhalt")
        item = inventory.Item("da", inventory.FILE, payload, "Config/da.txt",
                              "config", "Probe", required=True)
        original_items, original_info = inventory.items, volume._diskutil_info
        try:
            inventory.items = lambda: (item,)
            volume._diskutil_info = lambda _i: {
                "VolumeUUID": UUID_A, "VolumeName": "Probe", "FilesystemType": "apfs",
                "Encryption": True, "MountPoint": root, "Locked": False,
                "Internal": False, "GlobalPermissionsEnabled": True}
            result = engine.run_backup(config=volume.StorageConfig(volume_uuid=UUID_A),
                                       state=_fake_state(mount_point=root),
                                       include_repos=False)
        finally:
            inventory.items, volume._diskutil_info = original_items, original_info
        require(result.ok, f"ein sauberer Lauf galt als fehlerhaft: {result.errors}")
        sets_dir = os.path.dirname(result.path)
        leftovers = [n for n in os.listdir(sets_dir) if n.startswith(".incoming-")]
        require(not leftovers, f"ein halbfertiger Satz blieb liegen: {leftovers}")
        check = restore.verify_set(result.path)
        require(check["ok"], f"der frische Satz prueft nicht: {check['errors']}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def t_marker_mismatch_makes_the_volume_untrusted() -> None:
    work = _tmp("marker")
    try:
        config = volume.StorageConfig(volume_uuid=UUID_A)
        root = os.path.join(work, config.root_name)
        os.makedirs(root)
        # Eine Marke, die eine ANDERE Platte nennt.
        with open(os.path.join(root, volume.MARKER_NAME), "w", encoding="utf-8") as fh:
            json.dump({"volume_uuid": UUID_B}, fh)
        info = {"VolumeUUID": UUID_A, "VolumeName": "Probe", "FilesystemType": "apfs",
                "Encryption": True, "MountPoint": work, "Locked": False,
                "Internal": False, "GlobalPermissionsEnabled": True}
        original = volume._diskutil_info
        try:
            volume._diskutil_info = lambda _ident: info
            state = volume.probe(config)
            require(not state.usable, "eine widersprechende Marke wurde geschluckt")
            require_equal(state.marker_ok, False, "die Marke galt als in Ordnung")
            require_raises(volume.StorageUntrusted, volume.storage_root, state, config,
                           message="storage_root gab einen Pfad auf eine fremde Platte")
        finally:
            volume._diskutil_info = original
    finally:
        shutil.rmtree(work, ignore_errors=True)


def t_unencrypted_volume_is_rejected_as_a_target() -> None:
    info = {"VolumeUUID": UUID_A, "VolumeName": "Probe", "FilesystemType": "apfs",
            "Encryption": False, "MountPoint": "/tmp", "Locked": False,
            "Internal": False}
    original = volume._diskutil_info
    try:
        volume._diskutil_info = lambda _ident: info
        state = volume.probe(volume.StorageConfig(volume_uuid=UUID_A))
        require(not state.usable, "eine unverschluesselte Platte war benutzbar")
        require(any("nicht verschluesselt".lower() in p.lower() for p in state.problems),
                f"der Grund nennt die Verschluesselung nicht: {state.problems}")
    finally:
        volume._diskutil_info = original


def t_non_apfs_filesystem_is_rejected() -> None:
    info = {"VolumeUUID": UUID_A, "VolumeName": "Probe", "FilesystemType": "exfat",
            "Encryption": True, "MountPoint": "/tmp", "Locked": False}
    original = volume._diskutil_info
    try:
        volume._diskutil_info = lambda _ident: info
        state = volume.probe(volume.StorageConfig(volume_uuid=UUID_A))
        require(not state.usable, "ein exFAT-Volume war benutzbar")
    finally:
        volume._diskutil_info = original


def t_absent_volume_raises_unavailable_not_untrusted() -> None:
    """„Nicht da" und „falsch" sind zwei verschiedene Dinge.

    Das erste darf man ueberspringen. Das zweite muss auffallen. Wer beides
    gleich behandelt, faellt entweder staendig aus oder merkt einen Austausch nie.
    """
    original = volume._diskutil_info
    try:
        volume._diskutil_info = lambda _ident: None
        config = volume.StorageConfig(volume_uuid=UUID_A)
        require_raises(volume.StorageUnavailable, volume.storage_root, None, config,
                       message="eine fehlende Platte wurde als Fehler behandelt")
    finally:
        volume._diskutil_info = original


# ---------------------------------------------------------------------- Pfadschutz
def t_destination_paths_cannot_escape_the_backup_set() -> None:
    for evil in ("../oben", "/etc/passwd", "~/heimlich", "a/../../b", ""):
        require_raises(engine.BackupError, engine._safe_relpath, evil,
                       message=f"ein Ausbruchspfad wurde akzeptiert: {evil!r}")
    require_equal(engine._safe_relpath("Memory/memory.sqlite3"),
                  os.path.join("Memory", "memory.sqlite3"), "harmloser Pfad abgelehnt")


def t_manifest_paths_cannot_escape_the_restore_target() -> None:
    """Ein Manifest liegt auf einer externen Platte. Es ist Information, nie Autoritaet."""
    work = _tmp("escape")
    try:
        for evil in ("../../etc", "/etc", "~/x"):
            require_raises(restore.RestoreRefused, restore._safe_join, work, evil,
                           message=f"das Manifest durfte nach {evil!r} schreiben")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def t_restore_refuses_every_production_path() -> None:
    for path in ("~/.solvio", "~/.solvio/memory", "~/.solvio-approvals-production",
                 "~/SOLVIO Knowledge", "~/solvio-core", "~/Library/LaunchAgents", "~"):
        require_raises(restore.RestoreRefused, restore.assert_disposable, path,
                       message=f"eine Probe haette nach {path} ausgepackt")
    ok = restore.assert_disposable(os.path.join(tempfile.gettempdir(), "solvio-probe-x"))
    require(ok, "ein harmloses Wegwerfziel wurde abgelehnt")


def t_restore_refuses_a_path_that_contains_production() -> None:
    """Auch das Elternverzeichnis zaehlt: `~/` enthaelt `~/.solvio`."""
    require_raises(restore.RestoreRefused, restore.assert_disposable,
                   os.path.expanduser("~"),
                   message="das Heimatverzeichnis war als Ziel erlaubt")


# ------------------------------------------------------------------ Gesundheit
def t_missing_disk_with_a_fresh_backup_is_healthy() -> None:
    """Der Kernsatz des Milestones, als Zusicherung.

    Eine abgezogene Platte ist kein Defekt. Wenn dieser Test je rot wird, weil
    jemand `unavailable` fuer bequemer haelt, faengt SOLVIO an zu kraenkeln,
    weil ein Mensch eine Platte mitgenommen hat.
    """
    now = 1_800_000_000.0
    report = {"volume": _fake_state(present=False, mounted=False,
                                    problems=("Speicherplatte nicht angeschlossen",)).as_dict(),
              "backup": {"age_seconds": 3600.0, "consecutive_failures": 0}}
    state, reason = health.assess(report, now=now)
    require_equal(state, "healthy", f"abgezogene Platte war nicht gesund: {reason}")
    require("nicht angeschlossen" in reason, f"der Grund verschweigt die Lage: {reason}")


def t_missing_disk_with_an_old_backup_is_degraded() -> None:
    report = {"volume": _fake_state(present=False).as_dict(),
              "backup": {"age_seconds": 20 * 86400.0, "consecutive_failures": 0}}
    state, _ = health.assess(report)
    require_equal(state, "degraded", "eine 20 Tage alte Sicherung galt als gesund")


def t_locked_volume_asks_the_human() -> None:
    """Eine gesperrte Platte ist `auth_required` — dasselbe Wort wie ein
    abgelaufener Google-Zugang, und aus demselben Grund: nur ein Mensch kann das."""
    report = {"volume": _fake_state(locked=True,
                                    problems=("Speicherplatte ist gesperrt",)).as_dict(),
              "backup": {"age_seconds": 100.0, "consecutive_failures": 0}}
    state, _ = health.assess(report)
    require_equal(state, "auth_required", "eine gesperrte Platte rief nicht nach dem Menschen")


def t_unencrypted_disk_is_unavailable_not_degraded() -> None:
    report = {"volume": _fake_state(encrypted=False,
                                    problems=("Speicherplatte ist NICHT verschluesselt",)).as_dict(),
              "backup": {"age_seconds": 100.0, "consecutive_failures": 0}}
    state, reason = health.assess(report)
    require_equal(state, "unavailable", "eine unverschluesselte Platte war nur `degraded`")
    require("verschluessel" in reason.lower(), f"der Grund nennt es nicht: {reason}")


def t_repeated_failures_are_never_green() -> None:
    report = {"volume": _fake_state().as_dict(),
              "backup": {"age_seconds": 60.0, "consecutive_failures": 3}}
    state, _ = health.assess(report)
    require_equal(state, "degraded", "wiederholte Fehlschlaege galten als gesund")


def t_low_space_is_reported() -> None:
    report = {"volume": _fake_state(free_bytes=3 * 1024 ** 3).as_dict(),
              "backup": {"age_seconds": 60.0, "consecutive_failures": 0}}
    state, reason = health.assess(report)
    require_equal(state, "degraded", "eine fast volle Platte galt als gesund")
    require("frei" in reason, f"der Grund nennt den Platz nicht: {reason}")


def t_unconfigured_storage_is_unknown_not_healthy() -> None:
    report = {"volume": {"configured": False}, "backup": {"age_seconds": None}}
    state, _ = health.assess(report)
    require_equal(state, "unknown", "ohne eingerichtete Platte wurde etwas behauptet")


# ------------------------------------------------------------------ Aufbewahrung
def t_retention_keeps_daily_weekly_monthly_and_bounds_growth() -> None:
    names = []
    for day in range(1, 400):
        stamp = time.gmtime(time.mktime((2026, 1, 1, 3, 0, 0, 0, 0, -1)) + day * 86400)
        names.append(time.strftime("%Y%m%d-%H%M%S", stamp))
    keep, drop = plan_retention(names)
    require(len(keep) <= 14 + 8 + 12,
            f"die Aufbewahrung waechst unbegrenzt: {len(keep)} Saetze")
    require(len(drop) > 300, f"es wurde kaum etwas verworfen: {len(drop)}")
    require_equal(sorted(keep + drop), sorted(names),
                  "die Aufbewahrung hat Saetze verloren oder erfunden")
    require(names[-1] in keep, "der juengste Satz wurde verworfen")


def t_retention_keeps_the_oldest_monthly_anchor() -> None:
    """Eine Beschaedigung, die spaet auffaellt, braucht einen alten Stuetzpunkt."""
    names = [time.strftime("%Y%m%d-%H%M%S",
                           time.gmtime(time.mktime((2026, 1, 1, 3, 0, 0, 0, 0, -1))
                                       + d * 86400))
             for d in range(0, 200)]
    keep, _ = plan_retention(names)
    # Behalten wird je Monat der JUENGSTE Satz — das ist die uebliche Wahl und
    # die richtige: er hat die meiste Geschichte. Die Zusicherung ist, dass der
    # aelteste MONAT ueberhaupt noch vertreten ist.
    require(any(n.startswith("202601") for n in keep),
            "der aelteste Monatsstuetzpunkt fehlt — spaete Korruption waere nicht heilbar")
    require(len([n for n in keep if n.startswith("202601")]) == 1,
            "aus einem alten Monat wurde mehr als ein Stuetzpunkt behalten")


# --------------------------------------------------------------------- Bestand
def t_canonical_memory_is_never_placed_on_external_storage() -> None:
    """Die Sicherung KOPIERT das Gedaechtnis. Sie verschiebt es nie."""
    for item in inventory.items():
        require(not item.dest.startswith("/"), f"{item.name} hat einen absoluten Zielpfad")
        require(".." not in item.dest, f"{item.name} zeigt aus dem Satz heraus")
    memory = [i for i in inventory.items() if i.name == "memory"][0]
    require(memory.expanded.startswith(os.path.expanduser("~/.solvio")),
            "die Quelle des Gedaechtnisses liegt nicht mehr intern")
    require(memory.needs_encryption,
            "das Gedaechtnis darf ohne Verschluesselung gesichert werden")


def t_private_items_all_require_encryption() -> None:
    private = {"memory", "memory-candidates", "memory-privacy-ledger", "conversations",
               "approval-control", "core-env", "knowledge-vault", "deep-tasks",
               "node-pki", "satellite-auth", "portal-vault"}
    for item in inventory.items():
        if item.name in private:
            require(item.needs_encryption,
                    f"{item.name} darf auf eine unverschluesselte Platte")


def t_authority_keys_are_excluded_and_say_why() -> None:
    """SOLVIOs eigener Ausweis wird nicht kopiert — und der Plan sagt das."""
    sources = {i.expanded for i in inventory.items()}
    for key in ("core_signing_key.pem", "gateway_key.pem"):
        require(not any(key in s for s in sources),
                f"{key} liegt im Sicherungsbestand")
    excluded = " ".join(e.what for e in inventory.EXCLUDED)
    for key in ("core_signing_key.pem", "gateway_key.pem"):
        require(key in excluded, f"{key} fehlt in der Ausschlussliste")
    for entry in inventory.EXCLUDED:
        require(entry.reason and entry.recovery,
                f"{entry.what} ist ausgeschlossen ohne Grund oder ohne Rueckweg")


def t_secret_inventory_names_places_never_values() -> None:
    for entry in inventory.secret_inventory():
        require(entry["klasse"] in ("A", "B", "C", "D"),
                f"unbekannte Geheimnisklasse: {entry}")
        for value in entry.values():
            require("=" not in value or "/" in value or " " in value,
                    f"das riecht nach einem Wert statt einem Ort: {value[:60]}")


def t_approval_state_dir_comes_from_the_running_config() -> None:
    """Nicht vom Modulstandard: der zeigt auf ein Verzeichnis mit FREMDER Identitaet.

    Aufgeloest wie in Produktion — ohne die Testumlenkung aus `_guard`
    (DEBT-0223), sonst naehme die Pruefung unter Tests immer den
    Umgebungszweig und laese auf dem Mac die Core-plist nie mehr. Gelesen wird
    nur, nichts geoeffnet.
    """
    from unittest.mock import patch
    from _guard import without_test_paths
    with patch.dict(os.environ, without_test_paths(), clear=True):
        resolved = inventory.approval_state_dir()
    require(resolved.endswith("-production"),
            f"das Freigabeverzeichnis wurde falsch aufgeloest: {resolved}")


# ------------------------------------------------------------------ Testhygiene
def t_this_suite_cannot_touch_the_production_backup_state() -> None:
    """Die Zusicherung gegen genau den Fehler, der hier schon passiert ist.

    Beim ersten Einrichten des launchd-Dienstes stand im produktiven
    `~/.solvio/storage/state.json` eine erfundene Erfolgszeit und kein
    `last_backup_id` mehr — geschrieben vom Testlauf. Ein Werkzeug, das
    Datenverlust verhindern soll, darf ihn nicht selbst verursachen. Wer die
    Umlenkung am Kopf dieser Datei entfernt, macht diesen Test rot.
    """
    produktiv = os.path.realpath(engine.DEFAULT_STATE_DIR)
    for pfad in (engine.state_file(), engine.lock_file(), engine.log_file(),
                 volume.config_path()):
        echt = os.path.realpath(pfad)
        require(not echt.startswith(produktiv + os.sep) and echt != produktiv,
                f"der Testlauf greift auf den produktiven Betriebszustand: {pfad}")
    require(os.environ.get("SOLVIO_STORAGE_STATE_DIR"),
            "die Umlenkung des Betriebszustands fehlt")


def t_the_state_dir_follows_its_environment_variable() -> None:
    """Ohne diese Ablenkbarkeit gibt es keine Testhygiene — und keinen zweiten
    Speicher fuer einen kuenftigen Sonderfall."""
    work = _tmp("statedir")
    original = os.environ.get("SOLVIO_STORAGE_STATE_DIR")
    try:
        os.environ["SOLVIO_STORAGE_STATE_DIR"] = work
        require_equal(engine.state_dir(), work, "state_dir folgt der Variable nicht")
        engine.save_state({"probe": 1})
        require(os.path.isfile(os.path.join(work, "state.json")),
                "der Zustand landete nicht im umgelenkten Verzeichnis")
        require_equal(engine.load_state().get("probe"), 1, "gelesen wurde etwas anderes")
        require_equal(oct(os.stat(os.path.join(work, "state.json")).st_mode & 0o777),
                      "0o600", "der Betriebszustand ist nicht 0600")
    finally:
        if original is None:
            os.environ.pop("SOLVIO_STORAGE_STATE_DIR", None)
        else:
            os.environ["SOLVIO_STORAGE_STATE_DIR"] = original
        shutil.rmtree(work, ignore_errors=True)


# ------------------------------------------------------------------------ Melden
class _RecordingStore:
    """Ein Posteingang, der sich wie der echte verhaelt — samt der NULL-Falle.

    `UNIQUE(task_id, fingerprint)` in SQLite: NULL ist dort JEDES MAL ein neuer
    Wert. Genau daran hat der Arzt einmal doppelt gemeldet. Dieser Ersatz bildet
    das Verhalten nach, damit ein Test es fangen kann.
    """

    def __init__(self) -> None:
        self.items: list[dict] = []

    async def add_item(self, item: dict) -> bool:
        key = (item.get("task_id"), item.get("fingerprint"))
        if key[0] is None:                       # NULL ist nie gleich NULL
            self.items.append(item)
            return True
        if any((i.get("task_id"), i.get("fingerprint")) == key for i in self.items):
            return False
        self.items.append(item)
        return True


def t_a_backup_warning_uses_an_empty_task_id_not_none() -> None:
    """Sonst greift die Entdopplung der Datenbank nie — gemessen beim Arzt."""
    import asyncio

    from solvio.storage.job import notify
    store = _RecordingStore()
    asyncio.run(notify("Probe", [], kind="stale", now=1000.0, store=store))
    require_equal(store.items[0]["task_id"], "",
                  "task_id ist None — die UNIQUE-Sperre greift dann nie")
    require_equal(store.items[0]["source_capability"], "storage",
                  "die Quelle der Meldung ist nicht erkennbar")


def t_the_same_problem_is_reported_once_per_window_not_every_run() -> None:
    import asyncio

    from solvio.storage.job import QUIET_SECONDS, notify
    store = _RecordingStore()
    base = 1_000_000.0
    first = asyncio.run(notify("A", [], kind="stale", now=base, store=store))
    again = asyncio.run(notify("A", [], kind="stale", now=base + 60, store=store))
    later = asyncio.run(notify("A", [], kind="stale",
                               now=base + QUIET_SECONDS * 2, store=store))
    require(first, "die erste Meldung kam nicht an")
    require(not again, "dieselbe Lage wurde innerhalb des Fensters erneut gemeldet")
    require(later, "nach dem Fenster wurde gar nicht mehr gemeldet")


def t_a_healthy_backup_says_nothing() -> None:
    import asyncio

    from solvio.storage.job import maybe_notify
    store = _RecordingStore()
    for state in ("healthy", "unknown"):
        asyncio.run(maybe_notify(state, "alles gut", now=1000.0, store=store))
    require_equal(len(store.items), 0, "eine gesunde Sicherung hat gesprochen")


def t_a_locked_disk_speaks_and_says_what_to_do() -> None:
    import asyncio

    from solvio.storage.job import maybe_notify
    store = _RecordingStore()
    sent = asyncio.run(maybe_notify("auth_required", "gesperrt", now=1000.0, store=store))
    require(sent, "eine gesperrte Platte hat geschwiegen")
    require_equal(store.items[0]["priority"], "wichtig",
                  "die Meldung steht unter dem Kalender")
    require("Passphrase" in store.items[0]["summary"],
            f"die Meldung sagt nicht, was zu tun ist: {store.items[0]['summary']}")


# --------------------------------------------------------------------- Laufregeln
def t_a_second_concurrent_backup_run_is_refused() -> None:
    """DEBT-0160: der flock war seit Storage V1 definiert und wurde nie
    genommen — zwei parallele Laeufe teilten sich `state.json`. Seit der
    Reparatur nimmt `run_backup` den Lock als ERSTES: wer ihn haelt, sieht
    den zweiten Lauf als `BackupLocked` scheitern, BEVOR der irgendetwas
    anderes tut (hier: bevor er ueber die fehlende Konfiguration stolpert)."""
    import fcntl

    holder = open(engine.lock_file(), "w")
    try:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        require_raises(engine.BackupLocked, lambda: engine.run_backup())
    finally:
        holder.close()
    # Nach dem Loslassen faellt derselbe Aufruf am NAECHSTEN Hindernis um
    # (in dieser Sandbox: keine Speicherkonfiguration) — aber nicht mehr am
    # Lock. Der wird also genommen UND wieder freigegeben.
    try:
        engine.run_backup()
        require(False, "ein Lauf ohne Speicherkonfiguration lief durch")
    except engine.BackupLocked:
        require(False, "der Lock wurde nach dem Lauf nicht freigegeben")
    except Exception:
        pass


def t_a_named_recovery_point_survives_the_retention_rule() -> None:
    work = _tmp("label")
    try:
        from solvio.storage.job import prune
        sets_dir = os.path.join(work, "sets")
        for index, name in enumerate(("20250101-000000", "20250102-000000")):
            path = os.path.join(sets_dir, name)
            os.makedirs(path)
            with open(os.path.join(path, engine.MANIFEST_NAME), "w",
                      encoding="utf-8") as fh:
                json.dump({"format_version": 1, "backup_id": name, "entries": [],
                           "label": "vor-der-migration" if index == 0 else ""}, fh)
        result = prune(sets_dir, daily=0, weekly=0, monthly=0)
        require_equal(result["kept_by_label"], ["20250101-000000"],
                      "ein benannter Wiederherstellungspunkt wurde verworfen")
        require("20250102-000000" in result["removed"],
                "ein namenloser Satz ueberlebte eine Regel, die ihn verwerfen sollte")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def t_a_backup_run_bounces_off_when_the_last_one_is_still_fresh() -> None:
    """`StartOnMount` zuendet bei JEDEM eingehaengten Volume. Ohne Bremse waere
    jedes Disk-Image eine Sicherung."""
    import asyncio

    from solvio.storage import job as J
    original = engine.load_state
    try:
        engine.load_state = lambda: {"last_success_at": time.time() - 60}
        outcome = asyncio.run(J.run_once(notify_store=_RecordingStore()))
        require(outcome.get("skipped"), "die Bremse hat nicht gegriffen")
        require("Minuten" in str(outcome.get("reason")),
                f"der Grund ist unklar: {outcome.get('reason')}")
    finally:
        engine.load_state = original


def t_a_named_run_ignores_the_brake() -> None:
    """Ein Release-Punkt muss JETZT entstehen, nicht in sechs Stunden."""
    import asyncio

    from solvio.storage import job as J
    original_state, original_config = engine.load_state, volume.load_config
    try:
        engine.load_state = lambda: {"last_success_at": time.time() - 60}
        volume.load_config = lambda *a, **k: None      # keine Platte -> sauberer Abbruch
        outcome = asyncio.run(J.run_once(label="vor-dem-release",
                                         notify_store=_RecordingStore()))
        require("Minuten" not in str(outcome.get("reason") or ""),
                "ein benannter Lauf wurde von der Bremse abgewiesen")
    finally:
        engine.load_state, volume.load_config = original_state, original_config


# ------------------------------------------------- HA-Download-Auth (DEBT-0165)
#
# Die Regression, die 18 Saetze kostete: der Download benutzte `ha._headers`
# (nach der Tresor-Wanderung ein leerer Bearer), waehrend `backup/info` den
# kanonischen `_auth()`-Weg nahm. Diese drei Zusicherungen nageln fest, dass
# der Download denselben Weg nimmt wie der Rest — und dass der alte Weg nie
# still zurueckkommen kann.

class _AuthProbeHA:
    """Stellvertreter mit exakt der Naht, die `download()` benutzen darf."""

    def __init__(self, base: str, token: str) -> None:
        import contextlib
        self.base = base
        self._token_value = token
        self.legacy_touched = False

        @contextlib.contextmanager
        def _auth():
            yield {"Authorization": f"Bearer {token}",
                   "Content-Type": "application/json"}
        self._auth = _auth

    @property
    def _headers(self):
        self.legacy_touched = True
        return {"Authorization": "Bearer ", "Content-Type": "application/json"}


def _serve_download(seen: dict):
    """Ein lokaler HA-Stellvertreter, der den Authorization-Header festhaelt."""
    import asyncio

    from aiohttp import web

    async def handler(request):
        seen["authorization"] = request.headers.get("Authorization", "")
        if seen["authorization"].strip() in ("Bearer", "Bearer "):
            return web.Response(status=401, text="Unauthorized")
        return web.Response(body=b"tar-bytes")

    app = web.Application()
    app.router.add_get("/api/backup/download/{bid}", handler)
    return app


def t_ha_download_uses_the_canonical_auth_path() -> None:
    """Der Download traegt den Tresor-Header — nicht den leeren Legacy-Token.

    Auf dem Stand vor dem 2026-08-31 waere dieser Test rot: `download()`
    schickte `Authorization: Bearer ` und der Stellvertreter antwortet darauf
    mit 401, exakt wie das echte Home Assistant es 18 Laeufe lang tat."""
    import asyncio

    from aiohttp import web

    from solvio.storage import ha_backup

    async def scenario():
        seen: dict = {}
        runner = web.AppRunner(_serve_download(seen))
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        target = _tmp("ha-dl")
        try:
            ha = _AuthProbeHA(f"http://127.0.0.1:{port}", "probe-token-0165")
            dest = os.path.join(target, "ha.tar")
            size = await ha_backup.download(ha, "abc123", dest)
            return seen, size, os.path.exists(dest), ha.legacy_touched
        finally:
            await runner.cleanup()
            shutil.rmtree(target, ignore_errors=True)

    seen, size, exists, legacy_touched = asyncio.run(scenario())
    require_equal(seen.get("authorization"), "Bearer probe-token-0165",
                  "der Download traegt nicht den kanonischen Auth-Header")
    require(size == len(b"tar-bytes") and exists, "Download kam nicht an")
    require(not legacy_touched,
            "der Download hat den toten Legacy-Header-Weg beruehrt")


def t_ha_download_with_an_empty_bearer_fails_loudly() -> None:
    """Ein leerer Bearer bleibt ein harter Fehler, nie ein stilles Weiter.

    Sollte je wieder ein Codepfad einen leeren Header liefern, muss der
    Download krachen (401 → ClientResponseError) statt eine leere oder
    kaputte Datei als Erfolg abzulegen."""
    import asyncio
    import contextlib

    import aiohttp
    from aiohttp import web

    from solvio.storage import ha_backup

    class _EmptyHeaderHA(_AuthProbeHA):
        def __init__(self, base: str) -> None:
            super().__init__(base, "")

    async def scenario():
        seen: dict = {}
        runner = web.AppRunner(_serve_download(seen))
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        target = _tmp("ha-dl-leer")
        dest = os.path.join(target, "ha.tar")
        try:
            ha = _EmptyHeaderHA(f"http://127.0.0.1:{port}")
            try:
                await ha_backup.download(ha, "abc123", dest)
                return "kein Fehler", os.listdir(target)
            except aiohttp.ClientResponseError as exc:
                return exc.status, os.listdir(target)
        finally:
            await runner.cleanup()
            shutil.rmtree(target, ignore_errors=True)

    status, leftovers = asyncio.run(scenario())
    require_equal(status, 401, "leerer Bearer wurde nicht als 401 abgewiesen")
    require_equal(leftovers, [],
                  "ein gescheiterter Download hat eine Datei hinterlassen")


def t_ha_download_never_logs_the_secret_value() -> None:
    """Der Tokenwert erscheint in keiner Logzeile des Downloads."""
    import asyncio
    import io
    import logging

    from aiohttp import web

    from solvio.storage import ha_backup

    token = "geheim-nie-im-log-4711"
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    root = logging.getLogger()
    root.addHandler(handler)
    old_level = root.level
    root.setLevel(logging.DEBUG)

    async def scenario():
        seen: dict = {}
        runner = web.AppRunner(_serve_download(seen))
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        target = _tmp("ha-dl-log")
        try:
            ha = _AuthProbeHA(f"http://127.0.0.1:{port}", token)
            await ha_backup.download(ha, "abc123",
                                     os.path.join(target, "ha.tar"))
        finally:
            await runner.cleanup()
            shutil.rmtree(target, ignore_errors=True)

    try:
        asyncio.run(scenario())
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)
    require(token not in buf.getvalue(),
            "der Tokenwert stand in einer Logzeile")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
