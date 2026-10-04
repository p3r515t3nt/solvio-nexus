"""P1A.8 — release assurance: the result channel, the tracked baseline, invalid tests.

H1  The runner created a temp file and named its path in `SOLVIO_TEST_RESULT_PATH`. That is
    still a channel the suite can ADDRESS: anything running in the suite process could open
    the path and overwrite the verdict, and an outer environment could pre-set the variable
    to redirect it. The channel is now an anonymous pipe the parent creates, passed to
    `scripts/_test_worker.py` via `pass_fds`, and the WORKER frames the single result message
    after the suite has finished. Malformed, missing, duplicated, crashed and timed-out
    channels each fail their suite — and the gate keeps going and names them all at the end.

H2  The AST inventory and the executed manifest are both derived from the working tree, so
    they agree with each other even when a test is deleted: set equality still holds and the
    only trace is a smaller total. `tests/test_inventory_baseline.json` is a third source
    that lives in git, so a removal costs a reviewable diff. It never self-heals.

H3  An `async def` containing `yield` is an async GENERATOR. `asyncio.run` rejects it and
    `inspect.iscoroutinefunction` is False for it, so the sync branch would merely construct
    the generator — no line of the body would run — and report PASS. It is a definition
    error, detected by the harness AND independently in the source, and `PYTHONWARNINGS`
    cannot silence either.

ASSERTION POLICY: `require*` from `tests/_guard.py` are function calls and survive `-O`.

Direct: python tests/test_p1a_8_release_assurance.py
"""
import contextlib
import hashlib
import importlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions  # noqa: E402
enforce_assertions()

import _harness  # noqa: E402
import _inventory  # noqa: E402
from _guard import require, require_equal  # noqa: E402

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
RUNNER_SRC = os.path.join(REPO, "scripts", "run_tests.py")
HARNESS_SRC = os.path.join(TESTS_DIR, "_harness.py")
BASELINE = os.path.join(TESTS_DIR, "test_inventory_baseline.json")


def _runner():
    sys.path.insert(0, os.path.join(REPO, "scripts"))
    return importlib.import_module("run_tests")


def _probe(tmp, body, name="test_probe_p1a8.py"):
    path = os.path.join(os.path.realpath(tmp), name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"import os, sys\nsys.path.insert(0, {TESTS_DIR!r})\n"
                 "from _guard import enforce_assertions\nenforce_assertions()\n" + body)
    return path


def _harness_suite(tmp, body, name="test_probe_p1a8.py"):
    """A probe that goes through the shared harness, like every real suite."""
    return _probe(tmp, body + ('\nif __name__ == "__main__":\n'
                               "    from _harness import run_module\n"
                               "    raise SystemExit(run_module(globals(), __name__))\n"), name)


def _run(path, **kw):
    runner = _runner()
    return runner.run_one(Path(path), _inventory.suite_expected(path),
                          invalid_defs=_inventory.suite_invalid(path), **kw)


# =====================================================================
# H1 — the channel belongs to the parent
# =====================================================================
def t_h1_the_child_never_sees_a_result_path():
    """Not even an inherited SOLVIO_TEST_RESULT_PATH survives into the suite."""
    tmp = os.path.realpath(tempfile.mkdtemp())
    marker = os.path.join(tmp, "leaked.json")
    old = os.environ.get("SOLVIO_TEST_RESULT_PATH")
    os.environ["SOLVIO_TEST_RESULT_PATH"] = marker
    try:
        suite = _harness_suite(tmp, (
            "def t_a():\n"
            "    seen = os.environ.get('SOLVIO_TEST_RESULT_PATH')\n"
            "    print('LEAK:' + repr(seen))\n"))
        res = _run(suite)
        require_equal(res.failed, 0, res.failures)
        require("LEAK:None" in res.raw,
                f"the result path leaked into the suite environment: {res.raw[:200]}")
    finally:
        if old is None:
            os.environ.pop("SOLVIO_TEST_RESULT_PATH", None)
        else:
            os.environ["SOLVIO_TEST_RESULT_PATH"] = old
        shutil.rmtree(tmp, ignore_errors=True)


def t_h1_no_component_addresses_the_channel_by_path():
    """Structural: neither the harness nor the runner may name a result file again."""
    harness = open(HARNESS_SRC, encoding="utf-8").read()
    require("RESULT_PATH_ENV" not in harness, "the harness names a result path again")
    require("open(path" not in harness, "the harness writes a result file again")
    runner = open(RUNNER_SRC, encoding="utf-8").read()
    require('env["SOLVIO_TEST_RESULT_PATH"]' not in runner,
            "the runner sets a result path again")
    require('env.pop("SOLVIO_TEST_RESULT_PATH", None)' in runner,
            "the runner must delete an inherited result path")
    require("pass_fds=" in runner, "the runner no longer passes the pipe by fd")
    require("os.pipe()" in runner, "the runner no longer creates the channel itself")


def t_h1_a_fake_pass_on_stdout_is_ignored():
    """Phase 15/A: a genuinely failing test plus an all-green manifest on stdout."""
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        suite = _harness_suite(tmp, (
            "def t_a():\n"
            "    raise AssertionError('this test really fails')\n"
            "print('##SOLVIO-TEST-MANIFEST## '"
            " + '{\"protocol\": 2, \"ran\": true, \"tests\": []}')\n"
            "print('=== 1/1 bestanden ===')\n"))
        res = _run(suite)
        require(res.failed >= 1, "a forged stdout manifest turned a failure green")
        require_equal(res.passed, 0, f"stdout produced passing tests: {res.passed}")
        require(any("t_a" in f for f in res.failures), res.failures)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_h1_a_fake_pass_on_stderr_is_ignored():
    """Phase 15/B: the same forgery on stderr, which used to win by concatenation order."""
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        suite = _harness_suite(tmp, (
            "def t_a():\n"
            "    raise AssertionError('this test really fails')\n"
            "print('=== 1/1 bestanden ===', file=sys.stderr)\n"
            "print('##SOLVIO-TEST-MANIFEST## '"
            " + '{\"protocol\": 2, \"ran\": true, \"tests\": []}', file=sys.stderr)\n"))
        res = _run(suite)
        require(res.failed >= 1, "a forged stderr manifest turned a failure green")
        require_equal(res.passed, 0, f"stderr produced passing tests: {res.passed}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_h1_an_imported_module_finds_no_result_path():
    """Phase 15/C: the old vector, exercised from an IMPORTED module inside the suite."""
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        with open(os.path.join(tmp, "fake_product.py"), "w", encoding="utf-8") as fh:
            fh.write("import json, os\n"
                     "SEEN = os.environ.get('SOLVIO_TEST_RESULT_PATH')\n"
                     "if SEEN:\n"
                     "    open(SEEN, 'w').write(json.dumps(\n"
                     "        {'protocol': 2, 'ran': True, 'tests': []}))\n")
        suite = _harness_suite(tmp, (
            f"sys.path.insert(0, {tmp!r})\n"
            "import fake_product\n"
            "def t_a():\n"
            "    raise AssertionError('this test really fails')\n"
            "print('SEEN:' + repr(fake_product.SEEN))\n"))
        res = _run(suite)
        require("SEEN:None" in res.raw, f"a result path was reachable: {res.raw[:200]}")
        require(res.failed >= 1, "the imported module changed the verdict")
        require_equal(res.passed, 0, "the forged result was believed")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_h1_a_child_process_cannot_reach_the_old_result_vector():
    """Phase 15/D: a spawned child cannot find, or influence, the verdict.

    The assertion is on the PROPERTY — the child learns no channel and the verdict is
    unchanged — not on the contents of the shared temp directory. Counting stray files by
    name prefix would make this test depend on global machine state.
    """
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        suite = _harness_suite(tmp, (
            "import json, subprocess\n"
            "def t_a():\n"
            "    raise AssertionError('this test really fails')\n"
            "_child = (\n"
            "  \"import os;\"\n"
            "  \"print('CHILD_ENV:' + repr(os.environ.get('SOLVIO_TEST_RESULT_PATH')))\"\n"
            ")\n"
            "out = subprocess.run([sys.executable, '-c', _child],\n"
            "                     capture_output=True, text=True)\n"
            "print(out.stdout.strip())\n"))
        res = _run(suite)
        require("CHILD_ENV:None" in res.raw,
                f"a child process inherited a result path: {res.raw[:300]}")
        require(res.failed >= 1, "a child process changed the verdict")
        require_equal(res.passed, 0, "the real failure was reported as passing")
        require_equal(res.failures and "t_a" in res.failures[0], True, res.failures)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_h1_a_second_forged_frame_is_refused():
    """A suite that writes its own all-green frame must not be believed."""
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        suite = _harness_suite(tmp, (
            "import json\n"
            "def t_a():\n"
            "    raise AssertionError('this test really fails')\n"
            "def _forge():\n"
            "    b = json.dumps({'protocol': 2, 'ran': True, 'rc': 0, 'tests': [\n"
            "        {'id': 't_a', 'started': True, 'completed': True,\n"
            "         'status': 'passed', 'detail': ''}]}).encode()\n"
            "    os.write(int(sys.argv[2]),\n"
            "             b'SOLVIO-RESULT-1 ' + str(len(b)).encode() + b'\\n' + b)\n"
            "_forge()\n"))
        res = _run(suite)
        require(res.protocol_error, "a duplicated result message was accepted")
        require(res.failed >= 1, "the forged green result was believed")
        require_equal(res.passed, 0, "a forged frame produced passing tests")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_h1_a_silently_exiting_suite_fails():
    """os._exit skips the worker's framing entirely — there is simply no verdict.

    P1A.9/§4: the probe must FLUSH before `os._exit`, or its own all-green summary never
    reaches the pipe and the diagnostic parser fails the suite for "no summary produced"
    instead. The test then passed vacuously: removing the fail-closed contract path left it
    green. With the flush the suite presents a credible green summary on stdout, so only the
    channel check can refuse it.
    """
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        suite = _probe(tmp, ("def t_a():\n    pass\n"
                             'if __name__ == "__main__":\n'
                             "    print('=== 1/1 bestanden ===')\n"
                             "    sys.stdout.flush()\n"
                             "    os._exit(0)\n"))
        res = _run(suite)
        require("bestanden" in res.raw,
                f"the probe's forged summary never reached the runner: {res.raw!r}")
        require(res.protocol_error, "a suite that exited silently was accepted")
        require(res.failed >= 1, f"exit 0 with no result was green: {res.failures}")
        require_equal(res.passed, 0,
                      f"the forged stdout summary supplied {res.passed} passing tests")
        require_equal(res.missing, ["t_a"], f"the unrun test was not named: {res.missing}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_h1_a_crashing_worker_fails_the_suite():
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        suite = _probe(tmp, ("def t_a():\n    pass\n"
                             'if __name__ == "__main__":\n'
                             "    os.kill(os.getpid(), 9)\n"))
        res = _run(suite)
        require(res.crashed, f"a killed worker was not reported as a crash: rc={res.rc}")
        require(res.failed >= 1, "a crashed suite was green")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_h1_a_hanging_suite_times_out_without_hanging_the_gate():
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        suite = _probe(tmp, ("import time\n"
                             "def t_a():\n    pass\n"
                             'if __name__ == "__main__":\n'
                             "    time.sleep(300)\n"))
        res = _run(suite, timeout=3)
        require(res.timed_out, "a hanging suite was not timed out")
        require(res.failed >= 1, "a timed-out suite was green")
        require(any("timed out" in f for f in res.failures), res.failures)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_h1_a_reader_failure_cannot_crash_the_gate():
    """The channel is read on a side thread, so a bug there degrades to "no verdict".

    That is the property M9 probes: a raise while parsing the frame must fail THAT suite,
    not abort the run. Asserted directly, because a thread that dies cannot be observed by
    watching the exit code alone.
    """
    runner = _runner()
    tmp = os.path.realpath(tempfile.mkdtemp())
    original = runner._read_frame
    try:
        suite = _harness_suite(tmp, "def t_a():\n    pass\n")

        def exploding(fd, sink):
            os.close(fd)
            raise RuntimeError("deliberate reader failure")

        runner._read_frame = exploding
        try:
            res = runner.run_one(Path(suite), _inventory.suite_expected(suite))
        finally:
            runner._read_frame = original
        require(res.protocol_error, "a reader failure was not reported as a channel failure")
        require(res.failed >= 1, "a suite with no readable verdict was green")
    finally:
        runner._read_frame = original
        shutil.rmtree(tmp, ignore_errors=True)


def t_h1_the_gate_runs_every_suite_and_names_the_broken_ones():
    """One unverifiable suite must not stop the gate or hide the others."""
    runner = _runner()
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        good = _harness_suite(tmp, "def t_ok():\n    pass\n", "test_probe_good.py")
        _probe(tmp, ("def t_a():\n    pass\n"
                     'if __name__ == "__main__":\n    os._exit(0)\n'),
               "test_probe_silent.py")
        _probe(tmp, ("import time\n"
                     "def t_a():\n    pass\n"
                     'if __name__ == "__main__":\n    time.sleep(120)\n'),
               "test_probe_hang.py")
        _probe(tmp, ("def t_a():\n    pass\n"
                     'if __name__ == "__main__":\n'
                     "    os.write(int(sys.argv[2]), b'not a frame at all')\n"
                     "    raise SystemExit(0)\n"),
               "test_probe_malformed.py")
        base = os.path.join(tmp, "baseline.json")
        old = (runner.TESTS, runner.BASELINE, runner.SUITE_TIMEOUT)
        runner.TESTS, runner.BASELINE, runner.SUITE_TIMEOUT = Path(tmp), Path(base), 3
        inv = _inventory.discover(tmp)
        ids = sorted(f"{Path(p).resolve().relative_to(Path(tmp)).as_posix()}::{i}"
                     for p, v in inv.items() for i in v)
        Path(base).write_text(json.dumps({"tests": ids}), encoding="utf-8")
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                rc = runner.main([])
        finally:
            runner.TESTS, runner.BASELINE, runner.SUITE_TIMEOUT = old
        out = buf.getvalue()
        require_equal(rc, 1, f"a gate with two broken suites exited 0:\n{out}")
        require("test_probe_good.py" in out, "the healthy suite was not reported")
        require("Suites: 4" in out, f"the gate stopped early:\n{out[-800:]}")
        require("RESULT-CHANNEL FAILURES" in out, f"no channel section:\n{out[-800:]}")
        require("TIMEOUTS" in out, f"no timeout section:\n{out[-800:]}")
        channel_section = out.split("RESULT-CHANNEL FAILURES")[-1]
        require("test_probe_silent.py" in channel_section, "the silent suite was not named")
        require("test_probe_malformed.py" in channel_section,
                "the malformed suite was not named")
        require("test_probe_hang.py" in out.split("TIMEOUTS")[-1],
                "the hanging suite was not named")
        require_equal(os.path.basename(good), "test_probe_good.py", "probe setup")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _contract(entries, expected):
    """Drive the runner's contract directly with a chosen manifest."""
    runner = _runner()
    res = runner.Result(Path("probe.py"))
    res.channel = {"protocol": 2, "ran": True, "rc": 0, "tests": entries}
    runner._enforce_contract(res, expected)
    return res


def _entry(ident, status="passed"):
    return {"id": ident, "started": True, "completed": True, "status": status, "detail": ""}


def t_p16_duplicate_executions_fail():
    """Phase 16: the same identity reported twice is a failure, not a doubled pass."""
    res = _contract([_entry("t_a"), _entry("t_a")], ["t_a"])
    require(res.failed >= 1, "a duplicated execution was accepted")
    require_equal(res.duplicates, ["t_a"], res.duplicates)
    require(any("duplicate" in f for f in res.failures), res.failures)


def t_p16_missing_execution_fails_with_the_exact_name():
    res = _contract([_entry("t_a")], ["t_a", "t_a_security_test"])
    require(res.failed >= 1, "a missing test was accepted")
    require_equal(res.missing, ["t_a_security_test"], res.missing)
    require(any("t_a_security_test" in f for f in res.failures), res.failures)


def t_p16_unexpected_execution_fails():
    res = _contract([_entry("t_a"), _entry("t_not_in_source")], ["t_a"])
    require(res.failed >= 1, "an unexpected identity was accepted")
    require_equal(res.unexpected, ["t_not_in_source"], res.unexpected)


def t_p16_incomplete_execution_fails():
    started_never_finished = {"id": "t_a", "started": True, "completed": False,
                              "status": "not_run", "detail": ""}
    res = _contract([started_never_finished], ["t_a"])
    require(res.failed >= 1, "a test that started and never completed was accepted")
    require_equal(res.incomplete, ["t_a"], res.incomplete)


def t_p16_the_optimize_guard_covers_every_suite():
    """Phase 16: every standalone suite must still refuse optimized Python, loudly."""
    quiet = []
    for path in sorted(_inventory.discover(TESTS_DIR)):
        out = subprocess.run([sys.executable, "-O", path], cwd=REPO,
                             capture_output=True, text=True,
                             env={**os.environ, "PYTHONPATH": f"{REPO}/src:{TESTS_DIR}"})
        if out.returncode == 0:
            quiet.append(os.path.relpath(path, TESTS_DIR))
    require_equal(quiet, [], f"suites that stayed silent under -O: {quiet}")


def t_p16_the_worker_refuses_optimized_python():
    out = subprocess.run([sys.executable, "-O", os.path.join(REPO, "scripts",
                                                             "_test_worker.py"),
                          os.path.join(TESTS_DIR, "test_smoke.py"), "9"],
                         cwd=REPO, capture_output=True, text=True)
    require(out.returncode != 0, "the worker ran under -O")


# =====================================================================
# H2 — the tracked baseline
# =====================================================================
def t_h2_the_baseline_matches_the_tree():
    out = subprocess.run([sys.executable, os.path.join(REPO, "scripts",
                                                       "update_test_baseline.py"), "--check"],
                         cwd=REPO, capture_output=True, text=True)
    require_equal(out.returncode, 0,
                  f"the tracked baseline is stale — run scripts/update_test_baseline.py:\n"
                  f"{out.stdout}{out.stderr}")


def t_h2_baseline_uses_path_qualified_identities():
    data = json.loads(open(BASELINE, encoding="utf-8").read())
    require(isinstance(data.get("tests"), list) and data["tests"], "baseline has no tests")
    require(data["tests"] == sorted(data["tests"]), "the baseline is not sorted (noisy diffs)")
    bad = [t for t in data["tests"] if "::" not in t or not t.endswith(t.split("::")[-1])]
    require_equal(bad, [], f"identities are not relative_path::qualified_name: {bad[:5]}")
    require(any(t.startswith("memory/") for t in data["tests"]),
            "tests/memory/ is missing from the baseline")


def t_h2_a_removed_test_is_caught():
    """Deleting a test keeps AST == executed. Only the tracked baseline sees it."""
    runner = _runner()
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        _harness_suite(tmp, "def t_keep():\n    pass\n", "test_probe_base.py")
        base = os.path.join(tmp, "baseline.json")
        Path(base).write_text(json.dumps(
            {"tests": ["test_probe_base.py::t_keep",
                       "test_probe_base.py::t_a_security_test_someone_deleted"]}),
            encoding="utf-8")
        old = (runner.TESTS, runner.BASELINE)
        runner.TESTS, runner.BASELINE = Path(tmp), Path(base)
        try:
            inv = _inventory.discover(tmp)
            paths = sorted(p for p in Path(tmp).rglob("test_*.py") if p.is_file())
            problems = runner._check_baseline(inv, paths)
        finally:
            runner.TESTS, runner.BASELINE = old
        require_equal(len(problems), 1, problems)
        require("t_a_security_test_someone_deleted" in problems[0], problems)
        require("removed or renamed" in problems[0], problems)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_h2_an_untracked_new_test_is_caught():
    runner = _runner()
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        _harness_suite(tmp, "def t_keep():\n    pass\ndef t_new():\n    pass\n",
                       "test_probe_base.py")
        base = os.path.join(tmp, "baseline.json")
        Path(base).write_text(json.dumps({"tests": ["test_probe_base.py::t_keep"]}),
                              encoding="utf-8")
        old = (runner.TESTS, runner.BASELINE)
        runner.TESTS, runner.BASELINE = Path(tmp), Path(base)
        try:
            paths = sorted(p for p in Path(tmp).rglob("test_*.py") if p.is_file())
            problems = runner._check_baseline(_inventory.discover(tmp), paths)
        finally:
            runner.TESTS, runner.BASELINE = old
        require_equal(len(problems), 1, problems)
        require("t_new" in problems[0] and "not in the baseline" in problems[0], problems)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_h2_a_renamed_test_reports_both_sides():
    """Phase 15/J: a rename must show BOTH the removed old name and the added new one."""
    runner = _runner()
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        _harness_suite(tmp, "def t_renamed_to_this():\n    pass\n", "test_probe_base.py")
        base = os.path.join(tmp, "baseline.json")
        Path(base).write_text(json.dumps({"tests": ["test_probe_base.py::t_was_named_this"]}),
                              encoding="utf-8")
        old = (runner.TESTS, runner.BASELINE)
        runner.TESTS, runner.BASELINE = Path(tmp), Path(base)
        try:
            paths = sorted(p for p in Path(tmp).rglob("test_*.py") if p.is_file())
            problems = runner._check_baseline(_inventory.discover(tmp), paths)
        finally:
            runner.TESTS, runner.BASELINE = old
        require_equal(len(problems), 2, problems)
        removed = [p for p in problems if "removed or renamed" in p]
        added = [p for p in problems if "not in the baseline" in p]
        require_equal(len(removed), 1, problems)
        require_equal(len(added), 1, problems)
        require("t_was_named_this" in removed[0], removed)
        require("t_renamed_to_this" in added[0], added)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_h2_a_missing_baseline_fails_the_gate():
    runner = _runner()
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        _harness_suite(tmp, "def t_keep():\n    pass\n", "test_probe_base.py")
        old = (runner.TESTS, runner.BASELINE)
        runner.TESTS = Path(tmp)
        runner.BASELINE = Path(tmp) / "does_not_exist.json"
        try:
            paths = sorted(p for p in Path(tmp).rglob("test_*.py") if p.is_file())
            problems = runner._check_baseline(_inventory.discover(tmp), paths)
        finally:
            runner.TESTS, runner.BASELINE = old
        require_equal(len(problems), 1, problems)
        require("missing" in problems[0], problems)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_h2_the_runner_never_writes_the_baseline():
    """No self-healing: the gate may read the baseline, never repair it."""
    src = open(RUNNER_SRC, encoding="utf-8").read()
    for writer in ("BASELINE.write", "BASELINE.open", "write_text(", "open(BASELINE"):
        require(writer not in src, f"the runner writes the baseline ({writer})")
    before = hashlib.sha256(open(BASELINE, "rb").read()).hexdigest()
    out = subprocess.run([sys.executable, os.path.join(REPO, "scripts", "run_tests.py"),
                          "p1a_2"], cwd=REPO, capture_output=True, text=True)
    after = hashlib.sha256(open(BASELINE, "rb").read()).hexdigest()
    require_equal(after, before, "a runner invocation modified the tracked baseline")
    require_equal(out.returncode, 0, f"{out.stdout[-400:]}{out.stderr[-400:]}")


# =====================================================================
# H3 — an async generator is an INVALID TEST DEFINITION
# =====================================================================
def t_h3_an_async_generator_test_is_invalid_in_the_harness():
    async def t_gen():
        yield 1
        raise AssertionError("this body must never be credited as passing")

    out = _harness._run_one("t_gen", t_gen)
    require_equal(out.status, "invalid_definition", f"{out.status}: {out.detail}")
    require("INVALID TEST DEFINITION" in out.detail, out.detail)


def t_h3_a_sync_function_returning_an_async_generator_is_invalid():
    async def _gen():
        yield 1

    def t_sync():
        return _gen()

    out = _harness._run_one("t_sync", t_sync)
    require_equal(out.status, "invalid_definition", f"{out.status}: {out.detail}")


def t_h3_an_awaitable_return_is_still_incomplete():
    """P1A.6 must survive: a sync test returning a coroutine is not a pass."""
    async def _coro():
        return 1

    def t_sync():
        return _coro()

    out = _harness._run_one("t_sync", t_sync)
    require_equal(out.status, "incomplete_async", f"{out.status}: {out.detail}")


def t_h3_the_source_side_detects_it_independently():
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        path = os.path.join(tmp, "test_probe_gen.py")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("async def t_gen():\n    yield 1\n"
                     "async def t_fine():\n    pass\n"
                     "def t_plain():\n    pass\n"
                     "async def t_nested_yield_is_fine():\n"
                     "    def inner():\n        yield 1\n"
                     "    list(inner())\n"
                     "import unittest\n"
                     "class TestX(unittest.TestCase):\n"
                     "    async def test_gen(self):\n        yield 1\n")
        require_equal(_inventory.suite_invalid(path), ["t_gen", "TestX.test_gen"],
                      _inventory.suite_invalid(path))
        require("t_gen" in _inventory.suite_expected(path),
                "an invalid test must still be inventoried, not silently dropped")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_h3_pythonwarnings_ignore_cannot_disable_the_guard():
    """The structural check is not a warning, so silencing warnings changes nothing."""
    tmp = os.path.realpath(tempfile.mkdtemp())
    old = os.environ.get("PYTHONWARNINGS")
    os.environ["PYTHONWARNINGS"] = "ignore"
    try:
        suite = _harness_suite(tmp, "async def t_gen():\n    yield 1\n")
        res = _run(suite)
        require(res.failed >= 1, "an async-generator suite was green under PYTHONWARNINGS")
        require("t_gen" in res.invalid, f"not reported as invalid: {res.invalid}")
        require(any("INVALID TEST DEFINITION" in f for f in res.failures), res.failures)
        require_equal(res.passed, 0, "the async generator was counted as passing")
    finally:
        if old is None:
            os.environ.pop("PYTHONWARNINGS", None)
        else:
            os.environ["PYTHONWARNINGS"] = old
        shutil.rmtree(tmp, ignore_errors=True)


def t_h3_source_detection_fires_even_if_the_suite_never_runs_it():
    """The AST side is independent: a suite that hides the test still fails."""
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        suite = _probe(tmp, (
            "async def t_gen():\n    yield 1\n"
            "def t_ok():\n    pass\n"
            'if __name__ == "__main__":\n'
            "    from _harness import run_module\n"
            "    ns = {k: v for k, v in globals().items() if k != 't_gen'}\n"
            "    raise SystemExit(run_module(ns, __name__))\n"))
        res = _run(suite)
        require("t_gen" in res.invalid, f"the source side missed it: {res.invalid}")
        require(res.failed >= 1, "a hidden invalid definition was green")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_h3_the_tree_contains_no_invalid_definitions():
    bad = _inventory.discover_invalid(TESTS_DIR)
    require_equal(bad, {}, f"invalid test definitions in the tree: {bad}")


def t_no_tracked_file_carries_a_merge_conflict_marker():
    """Ein einkommittierter Konfliktmarker ist bis in die Produktion gelangt.

    Am 2026-09-01, beim Release von Offsite Encrypted Backup V1: der Merge des
    neuen `main` in die Betriebskomposition hatte Konflikte in `ROADMAP.md` und
    `docs/debt/TECH_DEBT.md`, deren Meldungen in einer gekuerzten Ausgabe
    verschwanden. `git add -A` hat die Marker mitgenommen, und sie standen
    danach im ausgecheckten Produktionsbaum.

    Gefangen hat es niemand. Das volle Gate war gruen — zu Recht, denn beide
    Dateien sind Dokumentation; ein Marker in Python waere als Syntaxfehler
    aufgefallen. Genau diese Luecke schliesst diese Zusicherung: sie prueft
    JEDE getrackte Datei, nicht nur die, die zufaellig geparst werden.

    Geprueft wird der Zeilenanfang, nicht das Vorkommen: `=======` als
    Markdown-Unterstreichung ist erlaubt, solange es nicht mit den beiden
    anderen Markern zusammen auftritt.
    """
    wurzel = os.path.dirname(TESTS_DIR)
    dateien = subprocess.run(["git", "-C", wurzel, "ls-files"],
                             capture_output=True, text=True,
                             check=False).stdout.split()
    require(len(dateien) > 100,
            f"git ls-files lieferte nur {len(dateien)} Dateien — "
            f"laeuft die Pruefung ueberhaupt im Repository?")
    muster = re.compile(r"^(<{7} |>{7} |={7}$)", re.M)
    getroffen = {}
    for rel in dateien:
        pfad = os.path.join(wurzel, rel)
        if not os.path.isfile(pfad) or os.path.getsize(pfad) > 4 * 1024 * 1024:
            continue
        try:
            with open(pfad, encoding="utf-8") as fh:
                text = fh.read()
        except (UnicodeDecodeError, OSError):
            continue
        # Ein einzelnes `=======` ist eine Unterstreichung. Ein Konflikt hat
        # immer alle drei Marker.
        if ("<<<<<<< " in text or ">>>>>>> " in text) and muster.search(text):
            getroffen[rel] = len(muster.findall(text))
    require_equal(getroffen, {},
                  f"einkommittierte Konfliktmarker: {getroffen}")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
