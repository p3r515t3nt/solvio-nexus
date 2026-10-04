"""Die Sicherungsmaschine.

So klein wie moeglich und keinen Deut kleiner. Kein Fremdpaket, keine
Sicherungs-Rahmenwerk-Architektur — `sqlite3`, `hashlib`, `os.link` und
`git bundle` reichen fuer das, was hier gebraucht wird.

Was ein Sicherungssatz ist
--------------------------
Ein Satz ist ein Verzeichnis mit einem Zeitstempel als Namen und einem Manifest
darin. Er ist **vollstaendig**: man kann aus ihm allein wiederherstellen, ohne
einen anderen Satz zu kennen. Und er ist **billig**: Dateien, die sich seit dem
letzten Satz nicht geaendert haben, werden nicht kopiert, sondern per Hardlink
geteilt. Zwei Saetze mit identischem Inhalt kosten den Platz von einem.

Das ist der ganze Trick, und es ist derselbe, den Time Machine benutzt. Er hat
eine Eigenschaft, die man kennen muss: ein Hardlink teilt Bloecke. Wer eine
Datei IN einem alten Satz veraendert, veraendert sie in allen. Deshalb schreibt
hier nur die Maschine, und sie schreibt nur in einen Satz, der noch nicht
fertig ist.

Atomar
------
Gebaut wird in `.incoming-<id>`, umbenannt wird erst am Ende. Ein Satz, den man
sieht, ist fertig. Ein Abbruch hinterlaesst hoechstens ein `.incoming-`, und das
raeumt der naechste Lauf weg.

Unabhaengig vom Core
--------------------
Diese Maschine importiert keinen laufenden Dienst und braucht keinen. Sie liest
Dateien und Datenbanken. Ein toter Core ist genau der Zeitpunkt, an dem eine
Sicherung am meisten wert ist.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from solvio.storage import inventory, volume
from solvio.storage.sqlite_snapshot import SnapshotError, sha256_file, snapshot
from solvio.storage.volume import (StorageConfig, StorageUnavailable,
                                   StorageUntrusted, VolumeState)
from solvio import git_binary as _GB

TOOL = "solvio-storage"
TOOL_VERSION = "1"
MANIFEST_NAME = "manifest.json"
FORMAT_VERSION = 1

#: Interner Betriebszustand der Sicherung. INTERN mit Absicht: „wann lief die
#: letzte Sicherung" muss auch dann zu beantworten sein, wenn die Platte fehlt.
#:
#: Umlenkbar ueber `SOLVIO_STORAGE_STATE_DIR`, und das ist kein Komfort: die
#: erste Fassung hatte den Pfad als Konstante, und der Testlauf hat prompt in
#: den PRODUKTIVEN Betriebszustand geschrieben — `state.json` verlor
#: `last_backup_id` und trug danach eine erfundene Erfolgszeit. Ein Werkzeug,
#: das Datenverlust verhindern soll, darf ihn nicht im Testlauf verursachen.
DEFAULT_STATE_DIR = os.path.expanduser("~/.solvio/storage")


def state_dir() -> str:
    return os.path.expanduser(os.environ.get("SOLVIO_STORAGE_STATE_DIR")
                              or DEFAULT_STATE_DIR)


def state_file() -> str:
    return os.path.join(state_dir(), "state.json")


def lock_file() -> str:
    return os.path.join(state_dir(), "backup.lock")


def log_file() -> str:
    return os.path.join(state_dir(), "backup.log")

#: Das Log rotiert bei dieser Groesse. DEBT-0056 ist die Lehre: ein Log ohne
#: Grenze ist eine Zusage, die man nicht halten kann.
LOG_MAX_BYTES = 1 << 20
LOG_KEEP = 3

#: Reserve, unter die eine Sicherung nicht schreibt. Eine volle Platte, die
#: mitten im Satz aufgibt, ist schlimmer als eine uebersprungene Sicherung.
MIN_FREE_BYTES = 5 * 1024 ** 3


class BackupError(RuntimeError):
    """Die Sicherung ist nicht zustandegekommen."""


class BackupLocked(BackupError):
    """Es laeuft schon eine. Kein Fehler, ein Zustand."""


# ------------------------------------------------------------------------- Hilfen
def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def backup_id(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("%Y%m%d-%H%M%S")


def _safe_relpath(rel: str) -> str:
    """Erzwingt, dass ein Zielpfad INNERHALB des Satzes bleibt.

    Der Bestand ist eine Datei in diesem Repository und damit vertrauenswuerdig
    — aber „vertrauenswuerdig" ist keine Eigenschaft, auf die man eine
    Pfadberechnung stuetzt. Ein Bestand mit `../../..` darf nicht schreiben
    koennen, egal wie er dorthin kam.
    """
    if not rel or rel.startswith("/") or rel.startswith("~"):
        raise BackupError(f"unsafe destination: {rel!r}")
    parts = rel.split("/")
    for p in parts:
        if p in ("", ".", ".."):
            raise BackupError(f"unsafe destination: {rel!r}")
    return os.path.join(*parts)


def _log(message: str) -> None:
    """Schreibt eine Betriebszeile. Zahlen und Namen, nie Inhalt."""
    path = log_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        if os.path.exists(path) and os.path.getsize(path) > LOG_MAX_BYTES:
            for i in range(LOG_KEEP - 1, 0, -1):
                older, newer = f"{path}.{i}", f"{path}.{i + 1}"
                if os.path.exists(older):
                    os.replace(older, newer)
            os.replace(path, path + ".1")
    except OSError:
        pass
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"{utcnow_iso()} {message}\n")


def load_state() -> dict[str, Any]:
    try:
        with open(state_file(), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(state: dict[str, Any]) -> None:
    path = state_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def _git(repo: str, *args: str) -> str | None:
    try:
        proc = subprocess.run([_GB.resolve(), "-C", repo, *args], capture_output=True,
                              text=True, timeout=60, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def _revisions() -> dict[str, Any]:
    """Welcher Stand wurde gesichert. Ohne diese Zeilen ist ein Satz ein Raetsel."""
    out: dict[str, Any] = {}
    for name, repo in (("core", inventory.CORE_REPO), ("ios", inventory.IOS_REPO)):
        if not os.path.isdir(os.path.join(repo, ".git")):
            continue
        status = _git(repo, "status", "--porcelain")
        out[name] = {
            "commit": _git(repo, "rev-parse", "HEAD"),
            "branch": _git(repo, "rev-parse", "--abbrev-ref", "HEAD"),
            "dirty": bool(status),
        }
    # Der Satellit liegt nicht in diesem Repository; seine Fassung steht in der
    # Wissensbasis. Die Herkunft wird mitgeschrieben, damit niemand die Zahl
    # fuer eine Messung haelt.
    sat = _satellite_revision()
    if sat:
        out["satellite"] = sat
    return out


def _satellite_revision() -> dict[str, Any] | None:
    path = os.path.join(inventory.CORE_REPO, "docs", "project_state.yaml")
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                s = line.strip()
                if s.startswith("revision:"):
                    return {"revision": s.split(":", 1)[1].strip(),
                            "source": "docs/project_state.yaml (Dokument, keine Messung)"}
    except OSError:
        return None
    return None


# ---------------------------------------------------------------------- Kopieren
@dataclass
class _Linker:
    """Teilt Bloecke mit dem vorigen Satz, wo der Inhalt identisch ist."""

    previous_root: str | None
    index: dict[str, str] = field(default_factory=dict)   # sha256 -> Pfad im vorigen Satz
    linked: int = 0
    linked_bytes: int = 0
    copied: int = 0
    copied_bytes: int = 0

    @classmethod
    def from_previous(cls, previous_root: str | None) -> "_Linker":
        self = cls(previous_root)
        if not previous_root:
            return self
        manifest_path = os.path.join(previous_root, MANIFEST_NAME)
        try:
            with open(manifest_path, encoding="utf-8") as fh:
                manifest = json.load(fh)
        except (OSError, ValueError):
            return self
        for entry in manifest.get("entries", []):
            if entry.get("sha256") and entry.get("dest"):
                self.index.setdefault(entry["sha256"],
                                      os.path.join(previous_root, entry["dest"]))
            for f in entry.get("files", []) or []:
                if f.get("sha256") and f.get("rel"):
                    self.index.setdefault(
                        f["sha256"], os.path.join(previous_root, entry["dest"], f["rel"]))
        return self

    def place(self, src: str, dst: str, digest: str) -> bool:
        """Legt `src` als `dst` ab. True, wenn per Hardlink geteilt wurde."""
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        candidate = self.index.get(digest)
        if candidate and os.path.isfile(candidate):
            try:
                os.link(candidate, dst)
                size = os.path.getsize(dst)
                self.linked += 1
                self.linked_bytes += size
                return True
            except OSError:
                pass  # Hardlink ueber Grenzen hinweg scheitert — dann eben kopieren.
        shutil.copy2(src, dst)
        os.chmod(dst, 0o600)
        size = os.path.getsize(dst)
        self.copied += 1
        self.copied_bytes += size
        self.index.setdefault(digest, dst)
        return False


def _copy_tree(src: str, dst: str, linker: _Linker,
               exclude: tuple[str, ...]) -> dict[str, Any]:
    """Kopiert einen Baum. Symlinks werden NICHT gefolgt und nicht angelegt.

    Ein Symlink in einer Sicherung ist ein Weg nach draussen — beim Sichern
    (folgen) und beim Wiederherstellen (anlegen). Beides ist hier unerwuenscht
    und beides waere schwer zu pruefen. Also: aufgeschrieben, uebersprungen.
    Das Manifest nennt jeden uebersprungenen Symlink; eine stille Auslassung
    waere schlimmer als gar keine Sicherung.
    """
    files: list[dict[str, Any]] = []
    directories: list[str] = []
    symlinks: list[str] = []
    total = 0
    for root, dirs, names in os.walk(src, followlinks=False):
        relative_root = os.path.relpath(root, src)
        if relative_root != ".":
            directories.append(relative_root)
            # Git needs .git/refs even when every reference is packed. Preserve
            # empty directories in the set too, including the offsite tar path.
            os.makedirs(os.path.join(dst, _safe_relpath(relative_root)),
                        mode=0o700, exist_ok=True)
        keep = []
        for d in sorted(dirs):
            full = os.path.join(root, d)
            if os.path.islink(full):
                symlinks.append(os.path.relpath(full, src))
                continue
            if d in exclude:
                continue
            keep.append(d)
        dirs[:] = keep
        for name in sorted(names):
            if name in exclude:
                continue
            full = os.path.join(root, name)
            rel = os.path.relpath(full, src)
            if os.path.islink(full):
                symlinks.append(rel)
                continue
            if not os.path.isfile(full):
                continue
            digest = sha256_file(full)
            linker.place(full, os.path.join(dst, _safe_relpath(rel)), digest)
            size = os.path.getsize(full)
            total += size
            files.append({"rel": rel, "bytes": size, "sha256": digest,
                          "mode": oct(os.stat(full).st_mode & 0o777)})
    os.makedirs(dst, exist_ok=True)
    return {"files": files, "bytes": total, "file_count": len(files),
            "directories": directories,
            "skipped_symlinks": sorted(set(symlinks))}


def _refs_digest(repo: str) -> str:
    """Fingerabdruck ueber ALLE Referenzen des Repositories.

    Gebraucht, weil `git bundle create` nicht bitgleich arbeitet: zwei Buendel
    desselben unveraenderten Repositories haben verschiedene Pruefsummen. Ohne
    diesen Fingerabdruck deduplizierte ausgerechnet der groesste Posten nie —
    gemessen: 55 von 67 MB je Satz, obwohl sich nichts geaendert hatte.

    Der Abdruck steht ueber `for-each-ref`, nicht ueber HEAD allein: ein neuer
    Zweig ist eine Aenderung, auch wenn `main` stehen bleibt.
    """
    refs = _git(repo, "for-each-ref", "--format=%(objectname) %(refname)") or ""
    return hashlib.sha256(refs.encode()).hexdigest()


def _git_bundle(repo: str, dest: str) -> dict[str, Any]:
    """Ein Buendel ist ein ganzes Repository in einer Datei.

    Kein `--mirror`-Klon: ein Buendel ist eine Datei, laesst sich pruefsummen,
    laesst sich per Hardlink teilen und klont sich zurueck. Genau das braucht
    eine Katastrophenwiederherstellung.
    """
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    proc = subprocess.run([_GB.resolve(), "-C", repo, "bundle", "create", dest, "--all"],
                          capture_output=True, text=True, timeout=600,
                          stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        raise BackupError(f"git bundle failed for {repo}")
    verify = subprocess.run([_GB.resolve(), "-C", repo, "bundle", "verify", dest],
                            capture_output=True, text=True, timeout=600,
                            stdin=subprocess.DEVNULL)
    os.chmod(dest, 0o600)
    return {"bytes": os.path.getsize(dest), "sha256": sha256_file(dest),
            "verified": verify.returncode == 0}


def _reuse_bundle(previous_root: str | None, name: str, digest: str,
                  dest: str) -> dict[str, Any] | None:
    """Teilt das Buendel des vorigen Satzes, wenn sich keine Referenz bewegt hat."""
    if not previous_root or not digest:
        return None
    try:
        with open(os.path.join(previous_root, MANIFEST_NAME), encoding="utf-8") as fh:
            previous = json.load(fh)
    except (OSError, ValueError):
        return None
    for entry in previous.get("entries", []):
        if entry.get("name") != name or entry.get("refs_digest") != digest:
            continue
        source = os.path.join(previous_root, str(entry.get("dest") or ""))
        if not os.path.isfile(source):
            return None
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        try:
            os.link(source, dest)
        except OSError:
            return None
        # Nachgerechnet, nicht geglaubt: das geteilte Buendel muss noch dieselbe
        # Pruefsumme haben wie damals. Ein Hardlink auf eine verrottete Datei
        # waere die stillste Art, eine Sicherung zu verlieren.
        actual = sha256_file(dest)
        if entry.get("sha256") and actual != entry["sha256"]:
            os.remove(dest)
            return None
        return {"bytes": os.path.getsize(dest), "sha256": actual,
                "verified": bool(entry.get("verified")),
                "reused_from": previous.get("backup_id")}
    return None


# ------------------------------------------------------------------------ Sicherung
@dataclass
class BackupResult:
    backup_id: str
    path: str | None
    ok: bool
    duration_seconds: float
    total_bytes: int
    linked_bytes: int
    entries: int
    skipped: list[dict[str, str]]
    errors: list[str]
    manifest: dict[str, Any]


class StagingUnprotected(BackupError):
    """Das Staging laege auf unverschluesseltem Grund. Fail-closed (§7).

    Eigene Klasse, weil §18 dafuer eine eigene Fehlerkategorie fuehrt
    (`staging_failed` / `staging_unprotected`) — und weil ein Aufrufer sie
    NIE mit „Platte fehlt" verwechseln darf.
    """


def _staging_target(staging_root: str) -> tuple[StorageConfig | None,
                                                VolumeState, str]:
    """Prueft und beschreibt ein Staging-Ziel. Wirft, statt zu raten.

    Drei Bedingungen, alle fail-closed:

    1. Der Pfad MUSS unter `~/.solvio/offsite/` liegen. Ein Staging
       irgendwo sonst waere ein Werkzeug, das ueberall hinschreiben darf.
    2. Das STARTVOLUME muss nachweislich FileVault-verschluesselt sein —
       hier liegen fuer Minuten die Klartext-Snapshots aller Speicher.
       `None` (nicht messbar) ist ein NEIN, nicht ein Ja.
    3. Genug Platz, gemessen am Staging-Pfad selbst (nicht an einem
       fremden Volume).

    Der zurueckgegebene `VolumeState` traegt `encrypted=True` — und das ist
    keine Behauptung, sondern das Ergebnis von Bedingung 2. Ohne dieses Feld
    wuerde die Maschine jeden privaten Eintrag ueberspringen und einen
    GRUENEN, LEEREN Satz melden: „eine Sicherung, die ihre Luecken
    verschweigt, ist die gefaehrlichste Art von gruen".
    """
    root = os.path.abspath(os.path.expanduser(staging_root))
    allowed = os.path.abspath(os.path.expanduser(
        os.environ.get("SOLVIO_OFFSITE_DIR") or "~/.solvio/offsite"))
    if not (root == allowed or root.startswith(allowed + os.sep)):
        raise StagingUnprotected(
            f"staging muss unter {allowed} liegen, nicht {root}")

    encrypted = volume.boot_volume_encrypted()
    if encrypted is not True:
        raise StagingUnprotected(
            "das Startvolume ist nicht nachweislich FileVault-verschluesselt"
            + ("" if encrypted is False else " (nicht messbar)")
            + " — im Staging laegen Klartext-Schnappschuesse offen")

    os.makedirs(root, mode=0o700, exist_ok=True)
    free = volume.free_bytes_at(root)
    return None, VolumeState(
        configured=True, present=True, mounted=True, mount_point=root,
        volume_name="Startvolume (Staging)", filesystem="apfs",
        encrypted=True, locked=False, internal=True,
        ownership_enabled=True, free_bytes=free,
        marker_ok=None), root


def _open_lock():
    path = lock_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fh = open(path, "w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        raise BackupLocked("es laeuft bereits eine Sicherung")
    return fh


def _clean_incoming(sets_dir: str) -> int:
    """Raeumt abgebrochene Saetze weg. Ein `.incoming-` ist nie eine Sicherung."""
    removed = 0
    if not os.path.isdir(sets_dir):
        return 0
    for name in os.listdir(sets_dir):
        if name.startswith(".incoming-"):
            shutil.rmtree(os.path.join(sets_dir, name), ignore_errors=True)
            removed += 1
    return removed


def list_sets(sets_dir: str) -> list[str]:
    """Fertige Saetze, aelteste zuerst."""
    if not os.path.isdir(sets_dir):
        return []
    return sorted(n for n in os.listdir(sets_dir)
                  if not n.startswith(".")
                  and os.path.isfile(os.path.join(sets_dir, n, MANIFEST_NAME)))


def run_backup(*, config: StorageConfig | None = None,
               state: VolumeState | None = None,
               now: datetime | None = None,
               include_repos: bool = True,
               label: str = "",
               extra_steps: list[Any] | None = None,
               staging_root: str = "") -> BackupResult:
    """Legt einen vollstaendigen, geprueften Sicherungssatz an.

    Haelt fuer die GESAMTE Dauer den flock auf `backup.lock` — zwei
    gleichzeitige Laeufe teilen sich `state.json` und koennten bei
    identischer Sekunden-ID kollidieren. Der Schutz war seit Storage V1
    definiert, wurde aber nie genommen (DEBT-0160); der zweite Lauf endet
    als `BackupLocked`, was der Job als stilles Ueberspringen behandelt.

    `staging_root` schaltet den STAGING-ZIELMODUS (Offsite V1 §7): der Satz
    entsteht dann unter `~/.solvio/offsite/…` auf der internen Platte statt
    auf der externen — ohne Volume-Trust-Check, ohne Marke, ohne
    Hardlink-Vorgaenger, dafuer mit FileVault-Gate. Der lokale Weg ist
    unveraendert: ohne diesen Parameter aendert sich an dieser Funktion
    nichts.
    """
    lock = _open_lock()
    try:
        return _run_backup_locked(config=config, state=state, now=now,
                                  include_repos=include_repos, label=label,
                                  extra_steps=extra_steps,
                                  staging_root=staging_root)
    finally:
        lock.close()


def _run_backup_locked(*, config: StorageConfig | None = None,
                       state: VolumeState | None = None,
                       now: datetime | None = None,
                       include_repos: bool = True,
                       label: str = "",
                       extra_steps: list[Any] | None = None,
                       staging_root: str = "") -> BackupResult:
    started = time.time()
    if staging_root:
        # STAGING-ZIELMODUS (Offsite V1 §7): der Satz entsteht auf der
        # INTERNEN Platte, weil der Offsite-Lauf an keinem der beiden
        # Geraete haengen darf, deren Verlust er versichert — die externe
        # Platte ist nach jedem Neustart gesperrt, und genau die Zeit danach
        # ist die, in der eine Sicherung fehlt.
        cfg, vol, root = _staging_target(staging_root)
    else:
        cfg = config or volume.load_config()
        if cfg is None:
            raise StorageUnavailable("keine SOLVIO-Speicherplatte eingerichtet")
        vol = state if state is not None else volume.probe(cfg)
        root = volume.storage_root(vol, cfg)  # wirft, wenn nicht vertrauenswuerdig

    if vol.free_bytes and vol.free_bytes < MIN_FREE_BYTES:
        raise BackupError(
            f"zu wenig Platz: {vol.free_bytes / 1024**3:.1f} GB frei")

    bid = backup_id(now)
    sets_dir = os.path.join(root, "Backups", "sets")
    if staging_root:
        # KEINE Volume-Marke im Staging: eine `.solvio-storage.json` mit
        # einer Volume-UUID unter `~/.solvio/offsite/` waere eine Luege
        # ueber das Medium. Angelegt wird nur die Struktur, mit 0700.
        volume.secure_dir(root)
        volume.secure_dir(os.path.join(root, "Backups"))
        volume.secure_dir(sets_dir)
    else:
        volume.create_layout(root, cfg)      # legt an, setzt 0700, setzt die Marke
    cleaned = _clean_incoming(sets_dir)

    previous = list_sets(sets_dir)
    # Im Staging gibt es bewusst KEINEN Hardlink-Vorgaenger: jede Generation
    # ist ein voller Satz, wie die lokalen Saetze auch („er braucht keinen
    # anderen Satz", §7). Ein Vorgaenger stuende ausserdem im Manifest als
    # Kette, die es offsite nicht gibt.
    previous_root = (None if staging_root
                     else (os.path.join(sets_dir, previous[-1])
                           if previous else None))
    linker = _Linker.from_previous(previous_root)

    incoming = os.path.join(sets_dir, f".incoming-{bid}")
    shutil.rmtree(incoming, ignore_errors=True)
    volume.secure_dir(incoming)

    entries: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    errors: list[str] = []
    total = 0

    for item in inventory.items():
        src = item.expanded
        dest_rel = _safe_relpath(item.dest)
        dest = os.path.join(incoming, dest_rel)
        if item.needs_encryption and not vol.encrypted:
            skipped.append({"name": item.name,
                            "reason": "Platte nicht verschluesselt — Privates bleibt hier"})
            continue
        if not os.path.exists(src):
            if item.required:
                errors.append(f"{item.name}: Quelle fehlt")
            else:
                skipped.append({"name": item.name, "reason": "nicht vorhanden"})
            continue
        try:
            if item.kind == inventory.SQLITE:
                res = snapshot(src, dest)
                entries.append({"name": item.name, "kind": item.kind,
                                "category": item.category, "dest": dest_rel,
                                "bytes": res.bytes, "sha256": res.sha256,
                                "integrity": res.integrity,
                                "opened_readonly": res.opened_readonly,
                                "source_journal_mode": res.source_journal_mode,
                                "tables": res.tables,
                                # §8 Offsite V1: Tabellenzaehler erkennen einen
                                # Restore in einen aelteren Codestand nicht.
                                "user_version": res.user_version,
                                "schema_stand": res.schema_stand,
                                "why": item.why})
                total += res.bytes
            elif item.kind == inventory.FILE:
                digest = sha256_file(src)
                linker.place(src, dest, digest)
                size = os.path.getsize(dest)
                entries.append({"name": item.name, "kind": item.kind,
                                "category": item.category, "dest": dest_rel,
                                "bytes": size, "sha256": digest, "why": item.why})
                total += size
            elif item.kind == inventory.TREE:
                info = _copy_tree(src, dest, linker, item.exclude)
                entries.append({"name": item.name, "kind": item.kind,
                                "category": item.category, "dest": dest_rel,
                                "bytes": info["bytes"], "sha256": None,
                                "file_count": info["file_count"],
                                "files": info["files"],
                                "directories": info["directories"],
                                "skipped_symlinks": info["skipped_symlinks"],
                                "why": item.why})
                total += info["bytes"]
            else:
                errors.append(f"{item.name}: unbekannte Art {item.kind}")
        except (SnapshotError, OSError, BackupError) as exc:
            errors.append(f"{item.name}: {exc}")

    if include_repos:
        for name, repo in (("core", inventory.CORE_REPO), ("ios", inventory.IOS_REPO)):
            if not os.path.isdir(os.path.join(repo, ".git")):
                skipped.append({"name": f"git-{name}", "reason": "kein Repository"})
                continue
            rel = _safe_relpath(f"Repos/{name}.bundle")
            digest = _refs_digest(repo)
            try:
                dest = os.path.join(incoming, rel)
                reused = _reuse_bundle(previous_root, f"git-{name}", digest, dest)
                if reused:
                    info = reused
                    linker.linked += 1
                    linker.linked_bytes += info["bytes"]
                else:
                    info = _git_bundle(repo, dest)
                entries.append({"name": f"git-{name}", "kind": "git-bundle",
                                "category": "repos", "dest": rel,
                                "bytes": info["bytes"], "sha256": info["sha256"],
                                "verified": info["verified"],
                                "refs_digest": digest,
                                "reused_from": info.get("reused_from"),
                                "why": "Offline-Kopie des Repositories. GitHub kann "
                                       "auch verloren gehen."})
                total += info["bytes"]
            except (BackupError, OSError, subprocess.SubprocessError) as exc:
                errors.append(f"git-{name}: {exc}")

    for step in (extra_steps or []):
        name = getattr(step, "name", "extra")
        try:
            produced = step(incoming, linker)
        except Exception as exc:          # ein Zusatzschritt darf den Satz nicht kippen
            errors.append(f"{name}: {exc}")
            continue
        # Ein Schritt, der NICHTS liefert, muss sagen warum. Der erste echte
        # Lauf hat genau hier lautlos Home Assistant weggelassen, weil die
        # Konfiguration aus einem git-Worktree gelesen wurde — im Manifest stand
        # weder ein Eintrag noch eine Auslassung. Eine Sicherung, die ihre
        # Luecken verschweigt, ist die gefaehrlichste Art von gruen.
        if not produced:
            skipped.append({"name": name, "reason": "Schritt lieferte nichts"})
        elif produced.get("skipped"):
            skipped.append({"name": name, "reason": str(produced["skipped"])})
        else:
            entries.append(produced)
            total += int(produced.get("bytes") or 0)

    finished = time.time()
    manifest = {
        "format_version": FORMAT_VERSION,
        "backup_id": bid,
        "label": label,
        "tool": TOOL,
        "tool_version": TOOL_VERSION,
        "created_at": utcnow_iso(),
        "duration_seconds": round(finished - started, 2),
        "host": socket.gethostname(),
        "storage": {
            "volume_uuid": vol.volume_uuid,
            "volume_name": vol.volume_name,
            "filesystem": vol.filesystem,
            "encrypted": vol.encrypted,
            "mount_point": vol.mount_point,
            "free_bytes_before": vol.free_bytes,
            "ownership_enabled": vol.ownership_enabled,
        },
        "revisions": _revisions(),
        # Im Staging gibt es keine Kette (kein Hardlink-Vorgaenger, §7) —
        # ein `previous_backup_id` behauptete hier eine, die nicht besteht.
        "previous_backup_id": (None if staging_root
                               else (previous[-1] if previous else None)),
        "source": "staging" if staging_root else "volume",
        "entries": entries,
        "skipped": skipped,
        "errors": errors,
        "cleaned_incoming": cleaned,
        "total_bytes": total,
        "linked_bytes": linker.linked_bytes,
        "copied_bytes": linker.copied_bytes,
        "linked_files": linker.linked,
        "copied_files": linker.copied,
        "excluded": [{"what": e.what, "secret_class": e.secret_class,
                      "reason": e.reason, "recovery": e.recovery}
                     for e in inventory.EXCLUDED],
        "restore_notes": [
            "Der Satz ist vollstaendig: er braucht keinen anderen Satz.",
            "Dateien koennen per Hardlink mit dem vorigen Satz geteilt sein. "
            "Nicht in einem alten Satz editieren.",
            "Die privaten Freigabeschluessel sind NICHT enthalten. Nach einem "
            "Verlust des Mac muss sich das iPhone neu anmelden.",
        ],
    }
    ok = not errors
    manifest["ok"] = ok

    manifest_text = json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False)
    mpath = os.path.join(incoming, MANIFEST_NAME)
    with open(mpath, "w", encoding="utf-8") as fh:
        fh.write(manifest_text + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(mpath, 0o600)

    final = os.path.join(sets_dir, bid)
    if os.path.exists(final):
        shutil.rmtree(final, ignore_errors=True)
    os.rename(incoming, final)          # ab hier ist der Satz sichtbar und fertig
    for current, dirs, _files in os.walk(final):
        for name in dirs:
            try:
                os.chmod(os.path.join(current, name), 0o700)
            except OSError:
                pass

    # Eine Zweitschrift des Manifests, damit „was liegt da eigentlich" ohne
    # Durchsuchen der Saetze zu beantworten ist.
    rec = volume.secure_dir(os.path.join(root, "Recovery", "manifests"))
    with open(os.path.join(rec, f"{bid}.json"), "w", encoding="utf-8") as fh:
        fh.write(manifest_text + "\n")

    _log(f"backup ok={ok} id={bid} bytes={total} linked={linker.linked_bytes} "
         f"entries={len(entries)} skipped={len(skipped)} errors={len(errors)}")
    return BackupResult(backup_id=bid, path=final, ok=ok,
                        duration_seconds=manifest["duration_seconds"],
                        total_bytes=total, linked_bytes=linker.linked_bytes,
                        entries=len(entries), skipped=skipped, errors=errors,
                        manifest=manifest)
