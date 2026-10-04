#!/usr/bin/env python3
"""P1A.8/H1 — the worker that owns the result channel.

Before this the RUNNER created a temp file and named its path in `SOLVIO_TEST_RESULT_PATH`.
That was still a channel the suite could see and address: any code running inside the suite
process — including an imported module — could open that path and overwrite the verdict, and
an outer environment could pre-set the variable to redirect it. The environment is not a
capability boundary.

Now the runner creates an ANONYMOUS pipe and passes only the write end into this worker via
`pass_fds`. The suite never learns a path, and `SOLVIO_TEST_RESULT_PATH` is deleted from the
child environment. This worker — not the suite — frames and writes the single result message
after the suite has finished running, so a suite cannot emit a second, forged one: the runner
requires exactly one framed message and treats trailing bytes as a protocol failure.

This is process-level hygiene, not a sandbox. The suite runs with the same OS rights as this
worker and could, with deliberate effort, write to the inherited fd or kill the process. What
it buys is that ACCIDENTS and ordinary output can no longer be mistaken for a verdict, and
that a forged verdict has to defeat framing rather than just print a line.

Usage (runner only): _test_worker.py <suite.py> <result_fd>
"""
from __future__ import annotations

import json
import os
import runpy
import sys
from pathlib import Path

MAGIC = b"SOLVIO-RESULT-1 "
PROTOCOL = 2

REPO = Path(__file__).resolve().parent.parent


def _write_result(fd: int, payload: dict) -> None:
    blob = json.dumps(payload).encode("utf-8")
    frame = MAGIC + str(len(blob)).encode("ascii") + b"\n" + blob
    with os.fdopen(fd, "wb", closefd=True) as fh:
        fh.write(frame)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    suite, fd = argv[0], int(argv[1])
    sys.path.insert(0, str(REPO / "src"))
    # P1A.9/H-1: `tests/` itself, not just the suite's own directory. A suite directly under
    # tests/ made `_harness` importable by accident — its parent IS tests/. A suite one level
    # down (tests/memory/) got only tests/memory/ on the path, so `import _harness` below
    # raised ModuleNotFoundError, the worker died before framing a result, and the canonical
    # gate reported four crashed suites and simply left 101 tracked identities unexecuted.
    # Inserted BEFORE the suite directory so the suite's own directory still wins.
    sys.path.insert(0, str(REPO / "tests"))
    sys.path.insert(0, str(Path(suite).resolve().parent))
    sys.path.insert(0, str(REPO))

    import _harness                                    # noqa: E402  (path set above)

    rc, error = 0, None
    try:
        runpy.run_path(suite, run_name="__main__")
    except SystemExit as exc:
        rc = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    except BaseException as exc:                       # noqa: BLE001 - reported, not swallowed
        import traceback
        rc = 1
        error = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()

    results = _harness.last_results()
    payload = {"protocol": PROTOCOL, "suite": suite, "rc": rc,
               "tests": results if results is not None else [],
               "ran": results is not None}
    if error:
        payload["worker_error"] = error
    if results is None:
        # The suite never went through the shared harness: it cannot vouch for itself, and
        # an empty test list must not read as "nothing failed".
        payload["worker_error"] = payload.get("worker_error") or \
            "the suite produced no harness results — it did not use tests/_harness.py"
    _write_result(fd, payload)
    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
