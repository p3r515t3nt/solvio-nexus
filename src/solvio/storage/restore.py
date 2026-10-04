"""Wiederherstellung — und der Grund, warum sie ins Leere zielt.

Eine Sicherung, die nie zurueckgespielt wurde, ist eine Behauptung. Dieses
Modul macht daraus eine Messung: es baut einen Sicherungssatz in einem
Wegwerfverzeichnis auf, oeffnet jede Datenbank, zaehlt jede Tabelle nach und
vergleicht jede Pruefsumme mit dem Manifest.

Ins Leere, mit Absicht
----------------------
`restore_set()` weigert sich, in ein produktives Verzeichnis zu schreiben. Nicht
weil das nie noetig waere — bei einer echten Katastrophe ist es genau das, was
man will —, sondern weil eine *Probe* niemals die Wahrheit ueberschreiben darf,
die sie beweisen soll. Der Ernstfall laeuft ueber das Runbook und ueber einen
Menschen, der weiss, was er tut. Diese Funktion ist der Alltag, und der Alltag
darf nichts kaputtmachen.

Was eine bestandene Probe bedeutet
----------------------------------
Dass die Dateien vollstaendig, unversehrt und oeffenbar sind, und dass ihr
Inhalt strukturell das ist, was das Manifest behauptet. Sie bedeutet NICHT,
dass SOLVIO auf einem neuen Rechner sofort laeuft — dafuer fehlen bewusst die
betriebssystemgebundenen Teile. Das steht im Wiederherstellungsplan, nicht in
einer gruenen Zeile.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
from dataclasses import dataclass, field
from typing import Any

from solvio.storage.engine import MANIFEST_NAME
from solvio.storage.sqlite_snapshot import sha256_file, verify_snapshot
from solvio import git_binary as _GB

#: Verzeichnisse, in die eine Probe NIEMALS schreiben darf. Der Vergleich laeuft
#: ueber `realpath`, damit ein Symlink die Sperre nicht umgeht.
PRODUCTION_PATHS = (
    "~/.solvio",
    "~/.solvio-approvals",
    "~/.solvio-approvals-production",
    "~/.solvio-portal",
    "~/.solvio-vault",
    "~/.solvio-deep",
    "~/.solvio-hermes",
    "~/SOLVIO Knowledge",
    "~/solvio-core",
    "~/solvio-ios",
    "~/Library/LaunchAgents",
)


class RestoreRefused(RuntimeError):
    """Das Ziel ist produktiv. Eine Probe schreibt dort nicht hin."""


@dataclass
class RestoreReport:
    backup_id: str
    target: str
    ok: bool
    checked: int = 0
    restored_bytes: int = 0
    findings: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"backup_id": self.backup_id, "target": self.target, "ok": self.ok,
                "checked": self.checked, "restored_bytes": self.restored_bytes,
                "findings": self.findings, "details": self.details}


def _real(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


def assert_disposable(target: str) -> str:
    """Wirft, wenn `target` produktiv ist oder in etwas Produktivem liegt."""
    t = _real(target)
    for p in PRODUCTION_PATHS:
        prod = _real(p)
        if t == prod or t.startswith(prod + os.sep) or prod.startswith(t + os.sep):
            raise RestoreRefused(
                f"Wiederherstellungsprobe verweigert: {target} beruehrt {p}")
    if t in ("/", os.path.expanduser("~")):
        raise RestoreRefused(f"Wiederherstellungsprobe verweigert: {target}")
    return t


def load_manifest(set_path: str) -> dict[str, Any]:
    path = os.path.join(set_path, MANIFEST_NAME)
    with open(path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    if not isinstance(manifest, dict) or "entries" not in manifest:
        raise ValueError(f"kein gueltiges Manifest: {path}")
    if int(manifest.get("format_version", 0)) != 1:
        raise ValueError(
            f"unbekannte Manifest-Fassung {manifest.get('format_version')!r}")
    return manifest


def _safe_join(base: str, rel: str) -> str:
    """Verbindet und beweist, dass das Ergebnis unter `base` bleibt.

    Ein Manifest ist eine Datei auf einer externen Platte. Es ist Information,
    nie Autoritaet — und schon gar keine Autoritaet ueber Schreibpfade.
    """
    if not rel or rel.startswith("/") or rel.startswith("~"):
        raise RestoreRefused(f"unsicherer Pfad im Manifest: {rel!r}")
    joined = os.path.realpath(os.path.join(base, rel))
    root = os.path.realpath(base)
    if joined != root and not joined.startswith(root + os.sep):
        raise RestoreRefused(f"Pfad zeigt aus dem Ziel heraus: {rel!r}")
    return joined


def restore_tree_file_mode(path: str, entry: dict[str, Any]) -> None:
    """Restore recorded ordinary permissions on an independent copied file.

    Backup payloads deliberately use 0600 and may share hardlinked inodes.
    Their mode must not replace the original immutable 0400 document mode.
    Older manifests without this field retain the private 0600 default;
    malformed modes and special permission bits are never inferred/applied.
    """
    raw = entry.get("mode", "0o600")
    if not isinstance(raw, str) or not re.fullmatch(r"0o[0-7]{1,3}", raw):
        raise RestoreRefused("ungueltiger Dateimodus im Manifest")
    mode = int(raw, 8)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RestoreRefused("Dateimodus erfordert eine unabhaengige regulaere Kopie")
        os.fchmod(descriptor, mode)
        if stat.S_IMODE(os.fstat(descriptor).st_mode) != mode:
            raise RestoreRefused("Dateimodus wurde nicht wiederhergestellt")
    finally:
        os.close(descriptor)


def restore_set(set_path: str, target: str, *,
                categories: set[str] | None = None,
                clone_repos: bool = False) -> RestoreReport:
    """Baut einen Sicherungssatz in einem Wegwerfverzeichnis auf und prueft ihn."""
    dest_root = assert_disposable(target)
    if os.path.exists(dest_root) and (not os.path.isdir(dest_root) or os.listdir(dest_root)):
        raise RestoreRefused("Wiederherstellungsprobe braucht ein leeres Ziel")
    manifest = load_manifest(set_path)
    report = RestoreReport(backup_id=str(manifest.get("backup_id")), target=dest_root,
                           ok=True)
    os.makedirs(dest_root, exist_ok=True)

    for entry in manifest["entries"]:
        if categories and entry.get("category") not in categories:
            continue
        name = str(entry.get("name"))
        rel = str(entry.get("dest") or "")
        src = _safe_join(set_path, rel)
        dst = _safe_join(dest_root, rel)
        kind = entry.get("kind")
        report.checked += 1

        if not os.path.exists(src):
            report.ok = False
            report.findings.append(f"{name}: fehlt im Satz")
            continue

        if kind in ("sqlite",):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            report.restored_bytes += os.path.getsize(dst)
            check = verify_snapshot(dst, entry.get("sha256"))
            expected = entry.get("tables") or {}
            if not check["ok"]:
                report.ok = False
                report.findings.append(f"{name}: {'; '.join(check['errors'])}")
            elif expected and check["tables"] != expected:
                report.ok = False
                diff = sorted(set(expected) ^ set(check["tables"]))
                report.findings.append(
                    f"{name}: Tabellenstand weicht ab ({diff or 'Zeilenzahlen'})")
            report.details[name] = {"tables": check.get("tables", {}),
                                    "rows": sum(v for v in check.get("tables", {}).values()
                                                if v > 0)}

        elif kind in ("file", "git-bundle"):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            size = os.path.getsize(dst)
            report.restored_bytes += size
            digest = sha256_file(dst)
            if entry.get("sha256") and digest != entry["sha256"]:
                report.ok = False
                report.findings.append(f"{name}: Pruefsumme weicht ab")
            report.details[name] = {"bytes": size}
            if kind == "git-bundle":
                proc = subprocess.run([_GB.resolve(), "bundle", "verify", dst],
                                      capture_output=True, text=True, timeout=600,
                                      stdin=subprocess.DEVNULL)
                if proc.returncode != 0:
                    report.ok = False
                    report.findings.append(f"{name}: git bundle verify schlug fehl")
                elif clone_repos:
                    work = _safe_join(dest_root, rel + ".checkout")
                    clone = subprocess.run([_GB.resolve(), "clone", "--quiet", dst, work],
                                           capture_output=True, text=True,
                                           timeout=900, stdin=subprocess.DEVNULL)
                    if clone.returncode != 0:
                        report.ok = False
                        report.findings.append(f"{name}: Klon aus dem Buendel schlug fehl")
                    else:
                        head = subprocess.run(
                            [_GB.resolve(), "-C", work, "rev-parse", "HEAD"],
                            capture_output=True, text=True, timeout=60,
                            stdin=subprocess.DEVNULL).stdout.strip()
                        report.details[name]["head"] = head

        elif kind == "tree":
            files = entry.get("files") or []
            os.makedirs(dst, mode=0o700, exist_ok=True)
            # Additive format-v1 field. Old sets have no directory inventory.
            for relative_directory in entry.get("directories") or []:
                os.makedirs(_safe_join(dst, relative_directory),
                            mode=0o700, exist_ok=True)
            bad = 0
            for f in files:
                frel = str(f.get("rel") or "")
                fsrc = _safe_join(src, frel)
                fdst = _safe_join(dst, frel)
                if not os.path.isfile(fsrc):
                    bad += 1
                    continue
                os.makedirs(os.path.dirname(fdst), exist_ok=True)
                shutil.copy2(fsrc, fdst)
                report.restored_bytes += os.path.getsize(fdst)
                if f.get("sha256") and sha256_file(fdst) != f["sha256"]:
                    bad += 1
                else:
                    restore_tree_file_mode(fdst, f)
            if bad:
                report.ok = False
                report.findings.append(f"{name}: {bad} von {len(files)} Dateien fehlerhaft")
            report.details[name] = {"files": len(files), "bad": bad}
        else:
            report.findings.append(f"{name}: unbekannte Art {kind!r} — nicht geprueft")

    return report


def verify_set(set_path: str) -> dict[str, Any]:
    """Prueft einen Satz, OHNE ihn auszupacken. Fuer die Gesundheitspruefung.

    Liest nur Pruefsummen. Schnell genug fuer einen taeglichen Lauf, aussagekraeftig
    genug, um schleichenden Bitverfall zu bemerken.
    """
    errors: list[str] = []
    checked = 0
    try:
        manifest = load_manifest(set_path)
    except (OSError, ValueError) as exc:
        return {"ok": False, "errors": [f"Manifest unbrauchbar: {exc}"], "checked": 0}
    for entry in manifest["entries"]:
        rel = str(entry.get("dest") or "")
        try:
            path = _safe_join(set_path, rel)
        except RestoreRefused as exc:
            errors.append(str(exc))
            continue
        if entry.get("kind") == "tree":
            for relative_directory in entry.get("directories") or []:
                checked += 1
                try:
                    directory = _safe_join(path, relative_directory)
                except RestoreRefused as exc:
                    errors.append(str(exc))
                    continue
                if not os.path.isdir(directory):
                    errors.append(f"{entry.get('name')}/{relative_directory}: Verzeichnis fehlt")
            for f in entry.get("files") or []:
                fp = os.path.join(path, str(f.get("rel")))
                checked += 1
                if not os.path.isfile(fp):
                    errors.append(f"{entry.get('name')}/{f.get('rel')}: fehlt")
                elif f.get("sha256") and sha256_file(fp) != f["sha256"]:
                    errors.append(f"{entry.get('name')}/{f.get('rel')}: Pruefsumme")
        else:
            checked += 1
            if not os.path.isfile(path):
                errors.append(f"{entry.get('name')}: fehlt")
            elif entry.get("sha256") and sha256_file(path) != entry["sha256"]:
                errors.append(f"{entry.get('name')}: Pruefsumme")
    return {"ok": not errors, "errors": errors, "checked": checked,
            "backup_id": manifest.get("backup_id")}
