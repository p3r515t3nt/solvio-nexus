"""Den lesbaren Vault umziehen — kopieren, nachrechnen, erst dann umschalten.

Der Vault ist eine PROJEKTION des Gedaechtnisses und keine Quelle. Er kann
jederzeit neu gebaut werden, und trotzdem wird er hier behandelt wie etwas
Unersetzliches: erst kopieren, dann jede Datei einzeln nachrechnen, und den
alten Ort NICHT loeschen. Der Grund ist nicht Vorsicht um ihrer selbst willen —
ein Mensch kann in diesen Dateien geschrieben haben, und was ein Mensch
geschrieben hat, steht in keiner Datenbank.

Umgeschaltet wird ueber `SOLVIO_VAULT`, die es schon gab. Dieses Modul erfindet
keinen zweiten Weg zum Vault; es bewegt Dateien und prueft sie.

Was hier ausdruecklich NICHT passiert: der alte Vault wird nicht geloescht, und
der Core wird nicht neu gestartet. Beides sind Handlungen mit Aussenwirkung, und
beide gehoeren in die Hand dessen, der den Umzug beschliesst.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from typing import Any

from solvio.storage.sqlite_snapshot import sha256_file

#: Was nicht mitzieht. `workspace.json` haelt offene Fenster und Cursorpositionen
#: — es beschreibt eine Sitzung, keinen Inhalt.
SKIP_NAMES = (".DS_Store", "workspace.json", ".Trash")


@dataclass
class MoveReport:
    source: str
    target: str
    ok: bool
    files: int = 0
    bytes: int = 0
    mismatches: list[str] = field(default_factory=list)
    skipped_symlinks: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"source": self.source, "target": self.target, "ok": self.ok,
                "files": self.files, "bytes": self.bytes,
                "mismatches": self.mismatches,
                "skipped_symlinks": self.skipped_symlinks}


def copy_vault(source: str, target: str) -> MoveReport:
    """Kopiert den Vault und rechnet jede Datei nach. Loescht nichts."""
    source = os.path.abspath(os.path.expanduser(source))
    target = os.path.abspath(os.path.expanduser(target))
    if not os.path.isdir(source):
        raise FileNotFoundError(f"kein Vault unter {source}")
    if os.path.realpath(target).startswith(os.path.realpath(source) + os.sep):
        raise ValueError("das Ziel liegt IM Quellverzeichnis")

    report = MoveReport(source=source, target=target, ok=True)
    os.makedirs(target, exist_ok=True)
    try:
        os.chmod(target, 0o700)
    except OSError:
        pass

    for root, dirs, names in os.walk(source, followlinks=False):
        keep = []
        for name in sorted(dirs):
            full = os.path.join(root, name)
            if os.path.islink(full):
                report.skipped_symlinks.append(os.path.relpath(full, source))
                continue
            if name in SKIP_NAMES:
                continue
            keep.append(name)
        dirs[:] = keep
        rel_dir = os.path.relpath(root, source)
        dest_dir = target if rel_dir == "." else os.path.join(target, rel_dir)
        os.makedirs(dest_dir, exist_ok=True)
        for name in sorted(names):
            if name in SKIP_NAMES:
                continue
            src = os.path.join(root, name)
            if os.path.islink(src):
                report.skipped_symlinks.append(os.path.relpath(src, source))
                continue
            if not os.path.isfile(src):
                continue
            dst = os.path.join(dest_dir, name)
            shutil.copy2(src, dst)                 # Rechte und Zeiten mit
            # Nachgerechnet, nicht geglaubt: eine Kopie ohne Pruefung ist eine
            # Behauptung.
            if sha256_file(src) != sha256_file(dst):
                report.ok = False
                report.mismatches.append(os.path.relpath(src, source))
                continue
            report.files += 1
            report.bytes += os.path.getsize(dst)
    return report
