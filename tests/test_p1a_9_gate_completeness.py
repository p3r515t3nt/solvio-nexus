"""P1A.9 — the canonical gate must actually RUN every tracked suite, and say so honestly.

H-1, reproduced against 52bb527. `scripts/_test_worker.py` put the REPO root, the suite's own
directory and `src/` on `sys.path` — but never `tests/`. A suite that sits directly under
`tests/` made `import _harness` work by accident, because its own directory IS `tests/`. A
suite one level down did not:

    ModuleNotFoundError: No module named '_harness'

The worker then died before it could frame a result. Measured on 52bb527 with a clean
environment (no inherited PYTHONPATH):

    Suites: 34   EXPECTED=458 EXECUTED=457 PASSED=457 FAILED=4 SKIPPED=1
    Missing=0 ... WorkerProtocolErrors=4 Crashes=4

101 tracked identities under `tests/memory/` never ran, and the gate reported **Missing=0**
while doing it — because every unverifiable path in `_enforce_contract` returned before the
missing set was computed. Two separate defects: the worker could not run nested suites, and
the report could not tell that anything was absent.

This suite defends both, and deliberately does NOT depend on `tests/memory/` continuing to
exist: it builds its own nested suites at several depths.

ASSERTION POLICY: `require*` from `tests/_guard.py` are function calls and survive `-O`.

Direct: python tests/test_p1a_9_gate_completeness.py
"""
import contextlib
import importlib
import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions  # noqa: E402
enforce_assertions()

import _inventory  # noqa: E402
from _guard import require, require_equal  # noqa: E402

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
WORKER_SRC = os.path.join(REPO, "scripts", "_test_worker.py")
RUNNER_SRC = os.path.join(REPO, "scripts", "run_tests.py")

# A suite that helps ITSELF onto sys.path would defend nothing: the whole point is that the
# trusted worker must make `_guard` and `_harness` reachable. Kept as a constant so the
# absence of any path manipulation is visible, and asserted below.
UNAIDED_SUITE = (
    "from _guard import enforce_assertions\n"
    "enforce_assertions()\n"
    "\n"
    "def t_nested_actually_ran():\n"
    "    pass\n"
    "\n"
    'if __name__ == "__main__":\n'
    "    from _harness import run_module\n"
    "    raise SystemExit(run_module(globals(), __name__))\n"
)


def _runner():
    sys.path.insert(0, os.path.join(REPO, "scripts"))
    return importlib.import_module("run_tests")


def _nested_suite(root, depth):
    """Write an unaided suite `depth` directories below `root`."""
    d = os.path.realpath(root)
    for i in range(depth):
        d = os.path.join(d, f"level{i}")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "test_probe_nested.py")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(UNAIDED_SUITE)
    return path


def _run(path):
    runner = _runner()
    return runner.run_one(Path(path), _inventory.suite_expected(path),
                          invalid_defs=_inventory.suite_invalid(path))


# =====================================================================
# H-1 — nested suites run through the real worker
# =====================================================================
def t_h1_the_probe_suite_is_genuinely_unaided():
    """Guard the guard: the probe must not put anything on sys.path itself."""
    require("sys.path" not in UNAIDED_SUITE,
            "the nested probe helps itself onto sys.path and defends nothing")
    require("import sys" not in UNAIDED_SUITE, "the nested probe imports sys")
    require("_harness" in UNAIDED_SUITE and "_guard" in UNAIDED_SUITE,
            "the probe no longer exercises the shared harness")


def t_h1_a_nested_suite_runs_through_the_real_worker():
    """The regression for H-1, at the depth `tests/memory/` sits at."""
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        res = _run(_nested_suite(tmp, 1))
        require_equal(res.failed, 0, f"a nested suite could not run: {res.failures}\n{res.raw}")
        require(not res.crashed, f"the worker crashed on a nested suite: {res.raw[-400:]}")
        require(not res.protocol_error, f"no result from a nested suite: {res.failures}")
        require_equal(res.executed_ids, ["t_nested_actually_ran"],
                      f"the nested test did not execute: {res.executed_ids}")
        require_equal(res.passed, 1, f"nested pass count: {res.passed}")
        require_equal(res.missing, [], res.missing)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_h1_nested_suites_run_at_any_depth():
    """Depth 0 worked by accident before the fix; 1, 2 and 3 are the real test."""
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        for depth in (0, 1, 2, 3):
            res = _run(_nested_suite(tmp, depth))
            require_equal(res.failed, 0,
                          f"depth {depth} failed: {res.failures}\n{res.raw[-300:]}")
            require_equal(res.executed_ids, ["t_nested_actually_ran"],
                          f"depth {depth} executed {res.executed_ids}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_h1_the_worker_puts_the_tests_directory_on_the_path():
    """Structural companion: the behaviour above must come from the worker, not luck."""
    src = open(WORKER_SRC, encoding="utf-8").read()
    require('sys.path.insert(0, str(REPO / "tests"))' in src,
            "the worker no longer puts tests/ on sys.path")
    # Line-based: prose that merely MENTIONS `import _harness` must not be mistaken for the
    # statement. Only executable lines count.
    lines = src.splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("def main("))
    code = [(i, l) for i, l in enumerate(lines[start:], start)
            if l.strip() and not l.strip().startswith("#")]
    insert_at = next(i for i, l in code if 'sys.path.insert(0, str(REPO / "tests"))' in l)
    import_at = next(i for i, l in code if l.strip().startswith("import _harness"))
    require(insert_at < import_at,
            f"tests/ is added to sys.path (line {insert_at + 1}) after _harness is "
            f"imported (line {import_at + 1})")


def t_h1_every_tracked_subdirectory_suite_is_reachable():
    """If the tree HAS nested suites, they must be accounted for by the gate.

    Not a dependency on `tests/memory/` — if nothing is nested this simply passes. It exists
    so that a future nested directory cannot silently repeat H-1.
    """
    nested = [p for p in sorted(_inventory.discover(TESTS_DIR))
              if os.path.dirname(os.path.abspath(p)) != TESTS_DIR]
    for path in nested:
        expected = _inventory.suite_expected(path)
        require(expected, f"{os.path.relpath(path, TESTS_DIR)} declares no tests")
        res = _run(path)
        rel = os.path.relpath(path, TESTS_DIR)
        require(not res.crashed, f"{rel}: worker crashed — {res.raw[-300:]}")
        require(not res.protocol_error, f"{rel}: no result channel — {res.failures}")
        require_equal(res.missing, [], f"{rel}: unexecuted identities {res.missing[:5]}")


# =====================================================================
# §3 — an unverifiable suite reports its tests as MISSING, by name
# =====================================================================
def _unverifiable(channel, expected):
    runner = _runner()
    res = runner.Result(Path("probe.py"))
    res.channel = channel
    runner._enforce_contract(res, expected)
    return res


def t_missing_is_computed_when_the_channel_is_absent():
    res = _unverifiable(None, ["t_a", "t_b", "t_c"])
    require_equal(res.missing, ["t_a", "t_b", "t_c"],
                  f"a suite with no result reported Missing={res.missing}")
    require_equal(res.collected, 3, f"expected count lost: {res.collected}")
    require_equal(res.executed, 0, f"an unrun suite reported EXECUTED={res.executed}")
    require_equal(res.passed, 0, f"an unrun suite reported PASSED={res.passed}")
    require(res.failed >= 1, "an unverifiable suite was not failed")


def t_missing_is_computed_when_the_protocol_is_wrong():
    res = _unverifiable({"protocol": 99, "tests": [], "ran": True}, ["t_a", "t_b"])
    require_equal(res.missing, ["t_a", "t_b"], res.missing)
    require_equal(res.executed, 0, res.executed)


def t_missing_is_computed_when_the_suite_never_ran():
    res = _unverifiable({"protocol": 2, "ran": False, "tests": [],
                         "worker_error": "no harness results"}, ["t_a"])
    require_equal(res.missing, ["t_a"], res.missing)
    require_equal(res.executed, 0, res.executed)


def t_missing_is_computed_when_the_entries_are_malformed():
    res = _unverifiable({"protocol": 2, "ran": True, "tests": [{"nope": 1}]}, ["t_a"])
    require_equal(res.missing, ["t_a"], res.missing)
    require_equal(res.executed, 0, res.executed)


def t_a_forged_stdout_summary_cannot_survive_an_unverifiable_channel():
    """The diagnostic parser is not allowed to supply numbers for a suite that never ran."""
    runner = _runner()
    res = runner.Result(Path("probe.py"))
    res.raw = "=== 12/12 bestanden ==="
    runner._parse_logs(res)
    require_equal(res.passed, 12, "setup: the diagnostic parser should have read stdout")
    res.channel = None
    runner._enforce_contract(res, ["t_a", "t_b"])
    require_equal(res.passed, 0, f"stdout supplied {res.passed} passing tests with no channel")
    require_equal(res.executed, 0, f"stdout supplied EXECUTED={res.executed}")
    require_equal(res.missing, ["t_a", "t_b"], res.missing)


def t_a_crashed_suite_names_its_unexecuted_tests_end_to_end():
    """Through the real runner: a worker that dies must not report Missing=0."""
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        path = os.path.join(tmp, "test_probe_crash.py")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"import os, sys\nsys.path.insert(0, {TESTS_DIR!r})\n"
                     "from _guard import enforce_assertions\nenforce_assertions()\n"
                     "def t_one():\n    pass\n"
                     "def t_two():\n    pass\n"
                     'if __name__ == "__main__":\n'
                     "    os.kill(os.getpid(), 9)\n")
        res = _run(path)
        require(res.crashed, f"the probe did not crash: rc={res.rc}")
        require_equal(res.missing, ["t_one", "t_two"],
                      f"a crashed suite reported Missing={res.missing}")
        require_equal(res.collected, 2, res.collected)
        require_equal(res.executed, 0, res.executed)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_the_gate_totals_account_for_every_tracked_identity():
    """EXECUTED + MISSING + SKIPPED must equal EXPECTED across the whole run.

    Driven over a small synthetic tree so it does not re-run the real suite, but through the
    real `main()` — the arithmetic that failed on 52bb527 was in the totals, not in one suite.
    """
    runner = _runner()
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        good = os.path.join(tmp, "test_probe_good.py")
        with open(good, "w", encoding="utf-8") as fh:
            fh.write(f"import sys\nsys.path.insert(0, {TESTS_DIR!r})\n"
                     "from _guard import enforce_assertions\nenforce_assertions()\n"
                     "def t_ok():\n    pass\n"
                     'if __name__ == "__main__":\n'
                     "    from _harness import run_module\n"
                     "    raise SystemExit(run_module(globals(), __name__))\n")
        dead = os.path.join(tmp, "test_probe_dead.py")
        with open(dead, "w", encoding="utf-8") as fh:
            fh.write(f"import os, sys\nsys.path.insert(0, {TESTS_DIR!r})\n"
                     "from _guard import enforce_assertions\nenforce_assertions()\n"
                     "def t_never_ran():\n    pass\n"
                     'if __name__ == "__main__":\n'
                     "    os.kill(os.getpid(), 9)\n")
        base = os.path.join(tmp, "baseline.json")
        inv = _inventory.discover(tmp)
        ids = sorted(f"{Path(p).resolve().relative_to(Path(tmp)).as_posix()}::{i}"
                     for p, v in inv.items() for i in v)
        Path(base).write_text(json.dumps({"tests": ids}), encoding="utf-8")
        old = (runner.TESTS, runner.BASELINE)
        runner.TESTS, runner.BASELINE = Path(tmp), Path(base)
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                rc = runner.main([])
        finally:
            runner.TESTS, runner.BASELINE = old
        out = buf.getvalue()
        require_equal(rc, 1, f"a gate with an unrun suite exited 0:\n{out}")
        line = [x for x in out.splitlines() if x.startswith("Suites:")]
        require(line, f"no summary line:\n{out}")
        nums = dict(kv.split("=") for kv in line[0].split() if "=" in kv)
        exp, ex, sk = int(nums["EXPECTED"]), int(nums["EXECUTED"]), int(nums["SKIPPED"])
        miss_line = [x for x in out.splitlines() if x.startswith("Missing=")][0]
        miss = int(dict(kv.split("=") for kv in miss_line.split() if "=" in kv)["Missing"])
        require_equal(miss, 1, f"the unrun test was not counted as missing:\n{out}")
        require_equal(ex + miss + sk, exp,
                      f"EXECUTED({ex}) + MISSING({miss}) + SKIPPED({sk}) != EXPECTED({exp})")
        require("t_never_ran" in out, f"the unexecuted test was not named:\n{out}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
