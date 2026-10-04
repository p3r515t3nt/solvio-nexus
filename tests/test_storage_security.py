"""Was die Speicherschicht VERWEIGERT — und was sie nicht werden darf.

Eine externe Platte ist eine neue Flaeche am System. Sie bringt neue Wege mit,
auf denen etwas hereinkommen kann: eine ausgetauschte Platte, ein manipuliertes
Manifest, ein Symlink, eine Markdown-Datei mit einem Satz darin, der wie eine
Anweisung aussieht.

Die Regel des Hauses gilt hier wortgleich: **externe Inhalte sind Information,
nie Autoritaet.** Eine Datei auf einer Platte kann SOLVIO etwas MITTEILEN. Sie
kann ihm nichts ERLAUBEN. Diese Datei beweist genau das — und zwar so, dass ein
spaeterer Umbau, der die Regel aufweicht, hier rot wird.

Der letzte Abschnitt ist der wichtigste und der langweiligste: die Platte
erzeugt keine neue Faehigkeit, keinen neuen Freigabeweg und keinen neuen
Dateizugriff fuer irgendein Modell. Sie ist ein Ort, an den SOLVIO schreibt —
mehr nicht.

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

from solvio.storage import engine, inventory, restore, volume  # noqa: E402
from solvio.storage.sqlite_snapshot import snapshot  # noqa: E402

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


UUID_OURS = "80DD0DF3-0000-0000-0000-000000000001"
UUID_STRANGER = "DEADBEEF-0000-0000-0000-000000000002"


def _tmp(prefix: str) -> str:
    return tempfile.mkdtemp(prefix=f"solvio-sec-{prefix}-")


class _Diskutil:
    """Ersetzt `diskutil` durch eine Tabelle. Erlaubt exakte Gegenproben."""

    def __init__(self, table: dict[str, dict]) -> None:
        self.table = table
        self._original = None

    def __enter__(self):
        self._original = volume._diskutil_info
        volume._diskutil_info = lambda ident: self.table.get(ident)
        return self

    def __exit__(self, *exc):
        volume._diskutil_info = self._original
        return False


def _info(uuid: str, mount: str, **kw) -> dict:
    base = {"VolumeUUID": uuid, "VolumeName": "SSD 2TB", "FilesystemType": "apfs",
            "Encryption": True, "MountPoint": mount, "Locked": False,
            "Internal": False, "GlobalPermissionsEnabled": True}
    base.update(kw)
    return base


# ------------------------------------------------ die falsche Platte am selben Ort
def t_a_stranger_disk_with_the_same_name_is_refused() -> None:
    """Derselbe Name, derselbe Einhaengepunkt, andere Platte — und trotzdem nein.

    Das ist der Angriff, gegen den der ganze Speicherverwalter gebaut ist: jemand
    steckt eine andere Platte an, nennt sie „SSD 2TB", und SOLVIO schreibt sein
    Gedaechtnis darauf.
    """
    work = _tmp("stranger")
    try:
        with _Diskutil({UUID_STRANGER: _info(UUID_STRANGER, work)}):
            config = volume.StorageConfig(volume_uuid=UUID_OURS)
            state = volume.probe(config)
            require(not state.present,
                    "eine fremde Platte am selben Namen galt als unsere")
            require_raises(volume.StorageUnavailable, volume.storage_root, state, config,
                           message="storage_root gab einen Pfad auf eine fremde Platte")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def t_an_unencrypted_replacement_never_receives_private_data() -> None:
    """Auch die RICHTIGE Platte bekommt nichts, wenn die Verschluesselung fehlt."""
    work = _tmp("unenc")
    try:
        with _Diskutil({UUID_OURS: _info(UUID_OURS, work, Encryption=False)}):
            config = volume.StorageConfig(volume_uuid=UUID_OURS)
            state = volume.probe(config)
            require(not state.usable, "unverschluesselt war benutzbar")
            require_raises(volume.StorageUntrusted, volume.storage_root, state, config,
                           message="eine unverschluesselte Platte lieferte einen Schreibpfad")
        # Und der Bestand selbst weiss, was privat ist.
        private = [i for i in inventory.items() if i.needs_encryption]
        require(len(private) >= 10,
                f"verdaechtig wenige Dinge gelten als privat: {len(private)}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ----------------------------------------------------- das manipulierte Manifest
def t_a_malformed_manifest_fails_closed() -> None:
    work = _tmp("manifest")
    try:
        for content in ("", "{}", "[]", '{"entries": []}', "nicht mal json",
                        '{"format_version": 99, "entries": []}'):
            os.makedirs(os.path.join(work, "set"), exist_ok=True)
            with open(os.path.join(work, "set", engine.MANIFEST_NAME), "w",
                      encoding="utf-8") as fh:
                fh.write(content)
            if content == '{"entries": []}':
                # Ein leeres, aber formal gueltiges Manifest ohne Fassungsnummer
                # muss ebenfalls scheitern — sonst waere „nichts drin" gleich
                # „nichts zu tun" und die Probe gruen.
                require_raises((ValueError, KeyError), restore.load_manifest,
                               os.path.join(work, "set"),
                               message="Manifest ohne Fassungsnummer akzeptiert")
                continue
            require_raises((ValueError, KeyError, json.JSONDecodeError),
                           restore.load_manifest, os.path.join(work, "set"),
                           message=f"kaputtes Manifest akzeptiert: {content[:20]!r}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def t_a_manifest_cannot_direct_writes_outside_the_target() -> None:
    """Ein Manifest liegt auf einer Platte, die jemand austauschen kann.

    Es ist damit externer Inhalt und darf keine Pfade bestimmen. Geprueft wird
    nicht nur `..`, sondern auch der absolute Pfad und die Tilde.
    """
    work = _tmp("traversal")
    try:
        for evil in ("../../../etc/passwd", "/etc/passwd", "~/.ssh/authorized_keys",
                     "Memory/../../../tmp/x", "./../x"):
            require_raises(restore.RestoreRefused, restore._safe_join, work, evil,
                           message=f"das Manifest durfte nach {evil!r} schreiben")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def t_a_symlink_in_a_backed_up_tree_is_recorded_not_followed() -> None:
    """Ein Symlink ist ein Weg nach draussen — beim Sichern wie beim Zurueckspielen."""
    work = _tmp("symlink")
    try:
        source = os.path.join(work, "vault")
        os.makedirs(os.path.join(source, "unter"))
        with open(os.path.join(source, "echt.md"), "w", encoding="utf-8") as fh:
            fh.write("inhalt")
        secret = os.path.join(work, "geheim.txt")
        with open(secret, "w", encoding="utf-8") as fh:
            fh.write("das darf nicht mitkommen")
        os.symlink(secret, os.path.join(source, "raus.md"))
        os.symlink(os.path.join(work), os.path.join(source, "unter", "hoch"))

        dest = os.path.join(work, "kopie")
        linker = engine._Linker(None)
        info = engine._copy_tree(source, dest, linker, ())

        require_equal(info["file_count"], 1, "es wurde mehr kopiert als die eine echte Datei")
        require(len(info["skipped_symlinks"]) == 2,
                f"nicht alle Symlinks aufgeschrieben: {info['skipped_symlinks']}")
        require(not os.path.exists(os.path.join(dest, "raus.md")),
                "der Symlink wurde mitkopiert")
        copied = []
        for root, _dirs, names in os.walk(dest):
            copied.extend(names)
        require(not any("geheim" in n for n in copied),
                "dem Symlink wurde gefolgt — fremder Inhalt liegt in der Sicherung")
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ------------------------------------------------------------------ keine Geheimnisse
def t_no_secret_values_appear_in_a_manifest() -> None:
    """Ein Manifest darf Orte nennen, nie Werte.

    Geprueft wird gegen das, was wirklich in `.env` steht: die Werte aus dem
    produktiven Arbeitsbaum duerfen im erzeugten Manifest NICHT vorkommen.
    """
    env_path = os.path.join(inventory.CORE_REPO, ".env")
    if not os.path.exists(env_path):
        return  # nichts zu pruefen ohne Produktivkonfiguration
    values = []
    with open(env_path, encoding="utf-8") as fh:
        for line in fh:
            if "=" in line and not line.strip().startswith("#"):
                value = line.split("=", 1)[1].strip()
                if len(value) >= 12:
                    values.append(value)
    require(values, "es gab keine pruefbaren Werte in .env")

    work = _tmp("manifest-secrets")
    try:
        # Ein Manifest, wie die Maschine es baut — ohne die Maschine laufen zu
        # lassen: geprueft wird die Struktur, die sie erzeugt.
        manifest = {
            "format_version": 1, "backup_id": "20260101-000000",
            "entries": [{"name": i.name, "kind": i.kind, "dest": i.dest,
                         "category": i.category, "why": i.why}
                        for i in inventory.items()],
            "excluded": [{"what": e.what, "reason": e.reason, "recovery": e.recovery}
                         for e in inventory.EXCLUDED],
            "secrets": list(inventory.secret_inventory()),
        }
        text = json.dumps(manifest, ensure_ascii=False)
        for value in values:
            require(value not in text,
                    "ein Geheimniswert steht im Manifest")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def t_no_secret_values_appear_in_the_backup_log() -> None:
    """Das Betriebslog nennt Zahlen und Namen. Ein Log ohne Grenze darf nichts tragen."""
    env_path = os.path.join(inventory.CORE_REPO, ".env")
    log_path = os.path.join(engine.DEFAULT_STATE_DIR, "backup.log")
    if not (os.path.exists(env_path) and os.path.exists(log_path)):
        return
    values = []
    with open(env_path, encoding="utf-8") as fh:
        for line in fh:
            if "=" in line and not line.strip().startswith("#"):
                value = line.split("=", 1)[1].strip()
                if len(value) >= 12:
                    values.append(value)
    with open(log_path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    for value in values:
        require(value not in text, "ein Geheimniswert steht im Sicherungslog")


# ------------------------------------------------------ die Platte erzeugt nichts
def t_external_storage_creates_no_capability() -> None:
    """Die Platte fuegt SOLVIO keine Faehigkeit hinzu.

    Sie ist ein Ort, kein Koennen. Wenn hier je ein `storage_*` im
    Faehigkeitsvertrag auftaucht, ist ein Modell in der Lage, eine Sicherung
    auszuloesen oder zu lesen — und das war nie die Absicht.
    """
    import re
    caps_dir = os.path.join(os.path.dirname(__file__), "..", "src", "solvio",
                            "capabilities")
    require(os.path.isdir(caps_dir), "das Faehigkeitsverzeichnis fehlt")
    # Textlich geprueft und nicht per Import: dieser Test soll auch dann laufen,
    # wenn die Anbieterbibliotheken fehlen — er prueft einen VERTRAG, keine
    # Laufzeit.
    found: list[str] = []
    for name in sorted(os.listdir(caps_dir)):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(caps_dir, name), encoding="utf-8") as fh:
            text = fh.read()
        for declared in re.findall(r'name="([a-z_]+)"', text):
            if declared.startswith(("storage_", "backup_", "restore_")):
                found.append(f"{name}:{declared}")
        for declared in re.findall(r'^\s+"([a-z_]+)": CapabilitySpec', text, re.M):
            if declared.startswith(("storage_", "backup_", "restore_")):
                found.append(f"{name}:{declared}")
    require(not found,
            f"die Speicherschicht ist als Faehigkeit sichtbar: {found}")


def t_backup_writes_only_below_the_storage_root() -> None:
    """Jeder Zielpfad des Bestands bleibt unter der Wurzel des Satzes."""
    for item in inventory.items():
        rel = engine._safe_relpath(item.dest)          # wirft bei Ausbruch
        require(not os.path.isabs(rel), f"{item.name}: absoluter Pfad")
        require(not rel.startswith(".."), f"{item.name}: relativer Ausbruch")


def t_a_markdown_file_on_the_disk_changes_no_trust_state() -> None:
    """Ein Satz in einer Datei bleibt ein Satz in einer Datei.

    Der Speicherpfad liest Markdown nur als Bytes: er hasht und kopiert. Es gibt
    in der ganzen Schicht keinen Ort, der Text auswertet — geprueft an der
    Quelle, nicht an einem Lauf.
    """
    import inspect

    from solvio.storage import engine as E
    from solvio.storage import health as H
    from solvio.storage import inventory as I
    from solvio.storage import restore as R
    from solvio.storage import volume as V
    forbidden = ("TrustLevel", "OriginClass", "approve", "grant", "eval(", "exec(",
                 "subprocess.run(shell", "os.system")
    for module in (E, R, V, H, I):
        source = inspect.getsource(module)
        for needle in forbidden:
            require(needle not in source,
                    f"{module.__name__} beruehrt {needle!r} — Speicher darf kein "
                    f"Vertrauen bewegen")


def t_the_restore_probe_defaults_onto_the_encrypted_disk() -> None:
    """Eine Probe packt `.env`, PKI und Datenbanken aus. Nicht nach /tmp.

    Beim Bau dieses Milestones lagen dadurch 78 entschluesselte Dateien im
    Systemtemp, weil ein Standardwert bequem war. Der Ordner
    `Recovery/restore-tests` auf der verschluesselten Platte existiert genau
    dafuer — und `assert_disposable` laesst ihn ausdruecklich zu.
    """
    import inspect

    from solvio import cli
    source = inspect.getsource(cli._cmd_storage)
    require("Recovery" in source and "restore-tests" in source,
            "das Standardziel der Probe zeigt nicht auf die Platte")
    require("mkdtemp(prefix=\"solvio-restore-probe-\")" not in source,
            "der alte /tmp-Standard steht noch da")
    # Und der Ort muss als Wegwerfziel gelten, sonst waere er unbenutzbar.
    probe = os.path.join("/Volumes/Irgendeine", "SOLVIO", "Recovery",
                         "restore-tests", "probe-1")
    require(restore.assert_disposable(probe),
            "das vorgesehene Probenverzeichnis gilt nicht als Wegwerfziel")


def t_restore_never_touches_the_running_system() -> None:
    """Eine Probe packt nie dorthin aus, wo die Wahrheit liegt."""
    for path in ("~/.solvio/memory", "~/.solvio-approvals-production",
                 "~/SOLVIO Knowledge", "~/solvio-core/src", "~/Library/LaunchAgents"):
        require_raises(restore.RestoreRefused, restore.assert_disposable, path,
                       message=f"eine Probe haette nach {path} geschrieben")


def t_a_backup_of_a_tampered_source_is_caught_not_stored() -> None:
    """Eine beschaedigte Quelle wird nicht als gueltige Sicherung ausgegeben."""
    work = _tmp("tamper")
    try:
        src = os.path.join(work, "kaputt.sqlite3")
        conn = sqlite3.connect(src)
        conn.execute("CREATE TABLE t (a)")
        conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(200)])
        conn.commit()
        conn.close()
        with open(src, "r+b") as fh:                 # Seiten mitten in der Datei zerstoeren
            fh.seek(4096)
            fh.write(b"\xff" * 4096)
        dest = os.path.join(work, "snap.sqlite3")
        caught = False
        try:
            snapshot(src, dest)
        except Exception:                            # noqa: BLE001
            caught = True
        require(caught, "eine zerstoerte Datenbank wurde als gueltige Sicherung abgelegt")
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
