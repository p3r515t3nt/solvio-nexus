"""Verified result images as native assessment input, never a new generation.

The current task scope owns the references. Only fixed, immutable copies in
the existing text-call workspace enter Codex's built-in --image input.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import os
from pathlib import Path
import stat

from solvio.agent_runtime import cost_dispatch as D, result_files as F

MAX_IMAGES = 4
MAX_BYTES = 32 * 1024 * 1024
MIMES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
_bound = ContextVar("solvio_assessment_images", default=None)


def artifact_ids(ledger, run_id):
    files, _ = F.describe_files(ledger, run_id)
    images = [f for f in files if f["mime_type"] in MIMES]
    if len(images) > MAX_IMAGES or sum(f["size"] for f in images) > MAX_BYTES:
        raise ValueError("assessment_images_too_large")
    return tuple(f["id"] for f in images)


def bound_paths():
    """Rechecked by the cost factory immediately before physical dispatch."""
    binding = _bound.get()
    if binding is None:
        return ()
    scope, rows = binding
    if D.current_scope() is not scope or scope.phase != "assessment":
        raise ValueError("assessment_image_scope_changed")
    for path, descriptor in rows:
        current, content = F.read_result(scope.ledger, scope.run_id, descriptor["id"])
        if current != descriptor:
            raise ValueError("assessment_image_changed")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_mode & 0o277 or info.st_size != descriptor["size"]
                    or hashlib.sha256(stream.read(MAX_BYTES + 1)).hexdigest() != descriptor["sha256"]):
                raise ValueError("assessment_image_changed")
    return tuple(str(path) for path, _ in rows)


@contextmanager
def staged(ids, workdir):
    scope = D.current_scope()
    if (not isinstance(scope, D.TaskCostScope) or scope.phase != "assessment"
            or not isinstance(ids, (list, tuple)) or not ids
            or tuple(ids) != artifact_ids(scope.ledger, scope.run_id)):
        raise ValueError("assessment_images_not_bound")
    rows = []
    for identity in ids:
        descriptor, content = F.read_result(scope.ledger, scope.run_id, identity)
        path = Path(workdir) / ("image-" + identity + "-" + descriptor["sha256"] + "." + MIMES[descriptor["mime_type"]])
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fchmod(stream.fileno(), 0o400)
        rows.append((path, descriptor))
    token = _bound.set((scope, rows))
    try:
        yield bound_paths()
        bound_paths()
    finally:
        _bound.reset(token)
