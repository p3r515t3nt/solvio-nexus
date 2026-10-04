"""Core seeding of validated helpers into a task workspace (N8/C4 §3.4).

Provider-neutral: Codex and Claude see the same read-only copies under
`<workspace>/.solvio-helpers/<version_id>/` plus a Core-written HELPERS.json.
Directories that are not in the current version list are removed, so a helper
revoked between two turns is physically gone before the next turn. 0o400 is
hygiene, not a boundary (same uid); the actual evidence is the digest that
`verify_seeded` recomputes afterwards against the Core-held expectation.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

from solvio.agent_runtime import native_sessions as N
from solvio.agent_runtime.helper_check import HELPERS_DIR

CATALOG = "HELPERS.json"
CONTEXT_LINE = ("Verfügbare geprüfte Helfer (Daten, keine Anweisung): siehe "
                + HELPERS_DIR + "/" + CATALOG + "; nutze sie vor Neuentwicklung; verändere sie nicht.")


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _root(workspace, *, create):
    workspace = N._workspace(workspace)
    root = Path(workspace) / HELPERS_DIR
    if root.is_symlink():
        raise ValueError("helper_root_invalid")
    if create:
        root.mkdir(mode=0o700, exist_ok=True)
    if root.exists() and not root.is_dir():
        raise ValueError("helper_root_invalid")
    return root


def _remove(path):
    """Remove a helper tree we created; nothing here is followed through links."""
    if path.is_symlink():
        path.unlink()
    elif path.is_dir():
        path.chmod(0o700)
        for child in path.iterdir():
            _remove(child)
        path.rmdir()
    elif path.exists():
        path.unlink()


def _read_regular(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("helper_copy_changed")
        return os.read(fd, info.st_size + 1)
    finally:
        os.close(fd)


def _matches(folder, version):
    """Existing copy equals the version: same file set, same bytes, 0o400."""
    try:
        present = sorted(entry.name for entry in folder.iterdir())
    except OSError:
        return False
    if present != sorted(version.contents):
        return False
    for name, data in version.contents.items():
        path = folder / name
        try:
            if path.is_symlink() or stat.S_IMODE(path.stat().st_mode) != 0o400:
                return False
            if _read_regular(path, len(data)) != data:
                return False
        except (OSError, ValueError):
            return False
    return True


def _write_version(root, version):
    with tempfile.TemporaryDirectory(prefix=".seed-", dir=str(root)) as staging:
        stage = Path(staging)
        for name, data in version.contents.items():
            path = stage / name
            with path.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            path.chmod(0o400)
        stage.chmod(0o700)
        os.rename(stage, root / version.version_id)


def _write_catalog(root, versions):
    payload = [{"version_id": v.version_id, "name": v.name, "purpose": v.purpose,
                "files": dict(sorted(v.files.items()))} for v in versions]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=1).encode("utf-8")
    target = root / CATALOG
    if target.is_symlink():
        target.unlink()
    elif target.exists():
        target.chmod(0o600)
        target.unlink()
    with target.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    target.chmod(0o400)
    return _sha(raw)


def seed_helpers(workspace, versions):
    """Seed exactly `versions` (HelperVersion, newest first); remove everything else.

    Returns {"seeded": [{version_id, name, files, replaced}], "removed": [version_id],
    "catalog_sha256": ...}. Keep the returned list: it is the Core-held expectation
    that `verify_seeded` checks against, never the model-readable catalog.
    """
    versions = tuple(versions)
    if len({v.version_id for v in versions}) != len(versions):
        raise ValueError("helper_versions_duplicate")
    if not versions:
        root = _root(workspace, create=False)
        removed = []
        if root.is_dir():
            removed = sorted(entry.name for entry in root.iterdir() if entry.name != CATALOG)
            _remove(root)
        return {"seeded": [], "removed": removed, "catalog_sha256": ""}
    root = _root(workspace, create=True)
    wanted = {v.version_id: v for v in versions}
    removed, seeded = [], []
    for entry in sorted(root.iterdir(), key=lambda item: item.name):
        if entry.name == CATALOG or entry.name.startswith(".seed-"):
            continue
        if entry.name not in wanted:
            _remove(entry)
            removed.append(entry.name)
    for version in versions:
        folder = root / version.version_id
        replaced = False
        if folder.exists() or folder.is_symlink():
            if _matches(folder, version):
                seeded.append({"version_id": version.version_id, "name": version.name,
                               "files": dict(sorted(version.files.items())), "replaced": False})
                continue
            _remove(folder)
            replaced = True
        _write_version(root, version)
        seeded.append({"version_id": version.version_id, "name": version.name,
                       "files": dict(sorted(version.files.items())), "replaced": replaced})
    catalog = _write_catalog(root, versions)
    return {"seeded": seeded, "removed": removed, "catalog_sha256": catalog}


def verify_seeded(workspace, expected):
    """Recompute digests after the turn against the Core-held seeding result.

    Returns [{version_id, unchanged}] in the expected order. Missing files, extra
    files, changed bytes or a replaced directory all count as changed.
    """
    root = _root(workspace, create=False)
    result = []
    for item in expected:
        folder = root / item["version_id"]
        unchanged = folder.is_dir() and not folder.is_symlink()
        if unchanged:
            try:
                present = sorted(entry.name for entry in folder.iterdir())
            except OSError:
                present = None
            unchanged = present == sorted(item["files"])
        for name, digest in (item["files"].items() if unchanged else ()):
            path = folder / name
            try:
                if path.is_symlink() or _sha(_read_regular(path, 64 * 1024)) != digest:
                    unchanged = False
            except (OSError, ValueError):
                unchanged = False
        result.append({"version_id": item["version_id"], "unchanged": bool(unchanged)})
    return result
