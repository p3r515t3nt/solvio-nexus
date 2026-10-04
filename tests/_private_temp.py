"""Short owner-private temp bases outside /tmp for tests that bind AF_UNIX sockets.

Two constraints meet here: AF_UNIX endpoints are limited to 103 bytes, and the
native tool bridge refuses socket roots under /tmp because the native sandbox
can reach that tree (measured 2026-09-17, Codex 0.147.0). A test's own temp
folder is usually too deep, and a per-suite TMPDIR may itself live under /tmp,
so the base is chosen independently of TMPDIR.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

MAX_BASE_BYTES = 70


def short_private_base() -> Path:
    candidates = []
    if sys.platform == "darwin":
        # The per-user temp folder; python exposes no confstr name for it.
        probe = subprocess.run(["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"], capture_output=True, text=True)
        candidates.append(probe.stdout.strip() if probe.returncode == 0 else "")
    candidates += [os.environ.get("XDG_RUNTIME_DIR"), tempfile.gettempdir(), "/dev/shm"]
    for value in candidates:
        if not value:
            continue
        path = Path(value).resolve()
        if (path.is_relative_to("/tmp") or path.is_relative_to("/private/tmp")
                or not path.is_dir() or not os.access(path, os.W_OK)
                or len(os.fsencode(str(path))) > MAX_BASE_BYTES):
            continue
        return path
    raise unittest.SkipTest("no short owner-writable temp base outside /tmp for a Unix socket")


@contextmanager
def socket_root():
    """A canonical, owner-private socket root as the runtime derives it beside the ledger."""
    with tempfile.TemporaryDirectory(prefix="sk-", dir=str(short_private_base())) as folder:
        yield str(Path(folder).resolve())


@contextmanager
def private_folder(prefix: str):
    """A short owner-private working folder outside /tmp (0o700)."""
    with tempfile.TemporaryDirectory(prefix=prefix, dir=str(short_private_base())) as folder:
        yield Path(folder).resolve()
