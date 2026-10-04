#!/usr/bin/env python3
"""SOLVIO canonical regression runner — the ONE authoritative test path.

P1A.4/C2+H4. Before this there was no official runner, and the two ad-hoc ways of running
the suite were both wrong in ways that produced false confidence:

  * a `tests/test_*.py` glob missed `tests/memory/` entirely — 101 tracked tests were
    outside every "green" claim ever made about this repo;
  * `pytest` collected ZERO tests from ten suites, because every P1A/F5/P0 security test is
    named `t_*` and pytest's default `python_functions = test*` does not match it — and then
    reported success;
  * `python -O` strips the bare `assert` statements those suites are built on, so a suite
    with its production guard deleted still printed all-green.

This runner: refuses to run under optimized Python, discovers every `test_*.py` under
`tests/` recursively, and reports COLLECTED / EXECUTED / PASSED / FAILED / SKIPPED
separately — never "N green" for tests that never ran.

    python3 scripts/run_tests.py            # everything
    python3 scripts/run_tests.py p1a f5     # only suites whose name matches a filter
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TESTS = REPO / "tests"

# The guard must not be an `assert` — that is the very thing being defended against.
if sys.flags.optimize != 0:
    print("Security regression must not run under optimized Python.\n"
          "  -O / PYTHONOPTIMIZE strips `assert`, so the suites would report success\n"
          "  without checking anything.\n"
          f"  sys.flags.optimize = {sys.flags.optimize}",
          file=sys.stderr)
    raise SystemExit(2)

sys.path.insert(0, str(REPO / "tests"))
import _inventory  # noqa: E402  (P1A.6/§1: the independent SOLL side)

WORKER = REPO / "scripts" / "_test_worker.py"
RESULT_MAGIC = b"SOLVIO-RESULT-1 "
RESULT_PROTOCOL = 2

MANIFEST_PREFIX = "##SOLVIO-TEST-MANIFEST## "
_PRINT_STYLE = re.compile(r"===\s*(\d+)/(\d+)\s*bestanden\s*===")
_UNITTEST = re.compile(r"^Ran (\d+) tests? in", re.M)
_SKIPPED = re.compile(r"skipped=(\d+)")
_FAIL_LINE = re.compile(r"^FAIL (\S+)", re.M)


class Result:
    __slots__ = ("path", "rc", "collected", "passed", "failed", "skipped", "failures", "raw",
                 "executed_ids", "missing", "unexpected", "duplicates", "incomplete",
                 "channel", "protocol_error", "timed_out", "crashed", "invalid",
                 "unverified", "state_dir", "state_kept")

    def __init__(self, path):
        self.path = path
        self.rc = 0
        self.collected = self.passed = self.failed = self.skipped = 0
        self.failures: list[str] = []
        self.raw = ""
        self.executed_ids: list[str] = []
        self.missing: list[str] = []
        self.unexpected: list[str] = []
        self.duplicates: list[str] = []
        self.incomplete: list[str] = []
        self.channel = None            # authoritative result, runner-owned
        self.protocol_error = False
        self.timed_out = False
        self.crashed = False
        self.invalid: list[str] = []
        # P1A.9/§3: no trustworthy result at all. Distinct from "ran and failed".
        self.unverified = False
        # N8/C5: the runner-owned SOLVIO_STATE_DIR of this suite (see _suite_state_dir).
        self.state_dir = ""
        self.state_kept = False

    @property
    def executed(self) -> int:
        # P1A.9/§3: an unverified suite executed NOTHING as far as the gate can tell. The old
        # fallback `collected - skipped` reported a crashed suite's expected count as if it
        # had run, which is how 101 unexecuted tests could be reported as EXECUTED=0/Missing=0
        # instead of being named.
        if self.unverified:
            return 0
        return len([i for i in self.executed_ids]) - self.skipped \
            if self.executed_ids else self.collected - self.skipped


def _unverified(res: "Result", expected: list[str], reason: str) -> None:
    """P1A.9/§3: no usable result — so every expected identity is MISSING, by name.

    All four unverifiable paths used to `return` before the missing-set was computed, so a
    crashed suite reported `Missing=0` while its tracked tests had simply not run. The gate
    said nothing was missing at the exact moment nothing could be confirmed. Diagnostic
    numbers parsed from the suite's own stdout are discarded here too: text is never
    authority, least of all when the channel failed.
    """
    res.unverified = True
    res.failed = max(res.failed, 1)
    res.failures.append(reason)
    res.collected = len(expected)
    res.executed_ids = []
    res.passed = 0
    res.skipped = 0
    res.missing = sorted(set(expected))
    if res.missing:
        res.failures.append(f"<missing test identities: {', '.join(res.missing[:8])}"
                            f"{'…' if len(res.missing) > 8 else ''}>")


def _enforce_contract(res: "Result", expected: list[str]) -> None:
    """P1A.6/§3: EXPECTED identities must equal EXECUTED identities — not just counts.

    A count check alone cannot tell "ten tests were removed" from "ten tests were renamed",
    and it cannot see a test that started and never completed. The expected side comes from
    the tracked source (AST), so a suite cannot vouch for itself.
    """
    manifest = res.channel
    if manifest is None:
        return _unverified(res, expected,
                           "<no result on the runner-owned channel — the suite did not use "
                           "the shared harness, so it cannot be verified>")
    if not isinstance(manifest, dict) or manifest.get("protocol") != RESULT_PROTOCOL \
            or not isinstance(manifest.get("tests"), list):
        return _unverified(res, expected,
                           f"<malformed result protocol: {str(manifest)[:120]}>")

    if not manifest.get("ran"):
        return _unverified(res, expected,
                           f"<{manifest.get('worker_error', 'the suite produced no results')}>")
    entries = manifest.get("tests", [])
    if not all(isinstance(e, dict) and isinstance(e.get("id"), str) for e in entries):
        return _unverified(res, expected, "<malformed result entries>")
    ids = [e["id"] for e in entries]
    res.executed_ids = ids
    seen, dups = set(), []
    for i in ids:
        if i in seen:
            dups.append(i)
        seen.add(i)
    exp = set(expected)
    res.missing = sorted(exp - seen)
    res.unexpected = sorted(seen - exp)
    res.duplicates = sorted(set(dups))
    res.incomplete = sorted(e["id"] for e in entries
                            if e.get("status") == "incomplete_async"
                            or (e.get("started") and not e.get("completed")))
    # P1A.8/H3: an async-generator test is a DEFINITION error, reported separately so it can
    # never be read as an ordinary assertion failure someone might "fix" by deleting a check.
    res.invalid = sorted(e["id"] for e in entries
                         if e.get("status") == "invalid_definition")

    # The manifest is authoritative — it REPLACES the numbers parsed from the suite's own
    # human-readable summary rather than being max()'d with them. A skipped test would
    # otherwise be double-counted as a failure by the parsed "passed vs total" difference.
    res.collected = len(expected)
    res.skipped = sum(1 for e in entries if e.get("status") == "skipped")
    res.passed = sum(1 for e in entries if e.get("status") == "passed")
    res.failed = sum(1 for e in entries if e.get("status") not in ("passed", "skipped"))
    res.failures = [f for f in res.failures if f.startswith("<")]
    for e in entries:
        if e.get("status") not in ("passed", "skipped"):
            res.failures.append(f"{e['id']}: {e.get('detail', e.get('status'))}"
                                .splitlines()[0])
    for label, items in (("missing test identities", res.missing),
                         ("unexpected test identities", res.unexpected),
                         ("duplicate executions", res.duplicates),
                         ("incomplete/never-awaited", res.incomplete),
                         ("INVALID TEST DEFINITION", res.invalid)):
        if items:
            res.failed = max(res.failed, 1)
            res.failures.append(f"<{label}: {', '.join(items[:8])}"
                                f"{'…' if len(items) > 8 else ''}>")


def _parse_logs(res: Result) -> None:
    """Diagnostics only — never authority. Kept so a suite without the harness still shows
    something useful before it is failed for having no result channel."""
    out = res.raw
    m = _PRINT_STYLE.search(out)
    if m:
        res.passed, res.collected = int(m.group(1)), int(m.group(2))
        res.failed = res.collected - res.passed
        res.failures += _FAIL_LINE.findall(out)
        return
    m = _UNITTEST.search(out)
    if m:
        res.collected = int(m.group(1))
        sk = _SKIPPED.search(out)
        res.skipped = int(sk.group(1)) if sk else 0
        if "\nOK" in out or out.rstrip().endswith("OK"):
            res.passed = res.collected - res.skipped
        else:
            fe = re.search(r"(?:failures|errors)=(\d+)", out)
            res.failed = sum(int(x) for x in re.findall(r"(?:failures|errors)=(\d+)", out)) \
                if fe else (0 if res.rc == 0 else 1)
            res.passed = max(0, res.collected - res.skipped - res.failed)
        return
    # No recognisable summary. If the process still exited 0 this is the dangerous case:
    # a suite that ran nothing and looked fine. Report it as a failure, never as green.
    res.failed = max(res.failed, 1)
    res.failures.append("<no test summary produced — suite may have run zero tests>")


def _enforce_non_empty(res: "Result") -> None:
    """P1A.5/H3: a suite that collects or executes nothing is a FAILURE.

    The previous version only caught the "no summary at all" case, so a suite printing
    `=== 0/0 bestanden ===` — what a naming refactor produces — was reported green with
    exit 0, contradicting this file's own docstring. The most load-bearing suite in the
    branch could have vanished silently.
    """
    if res.collected <= 0:
        res.failed = max(res.failed, 1)
        res.failures.append("<zero tests collected — suite ran nothing>")
    elif res.executed <= 0:
        res.failed = max(res.failed, 1)
        res.failures.append(
            f"<zero tests executed ({res.collected} collected, {res.skipped} skipped)>")


def discover(filters: list[str]) -> list[Path]:
    paths = sorted(p for p in TESTS.rglob("test_*.py") if p.is_file())
    if filters:
        paths = [p for p in paths if any(f.lower() in p.name.lower() for f in filters)]
    return paths


SUITE_TIMEOUT = int(os.environ.get("SOLVIO_SUITE_TIMEOUT", "600"))


def _read_frame(fd: int, sink: dict) -> None:
    """Read the ONE framed result message the worker writes. Anything else is a failure."""
    chunks = []
    try:
        with os.fdopen(fd, "rb", closefd=True) as fh:
            while True:
                b = fh.read(65536)
                if not b:
                    break
                chunks.append(b)
    except OSError as exc:
        sink["error"] = f"result channel unreadable: {exc}"
        return
    blob = b"".join(chunks)
    if not blob:
        sink["error"] = "no result on the runner-owned channel"
        return
    if not blob.startswith(RESULT_MAGIC):
        sink["error"] = f"unframed bytes on the result channel: {blob[:60]!r}"
        return
    rest = blob[len(RESULT_MAGIC):]
    head, sep, body = rest.partition(b"\n")
    if not sep:
        sink["error"] = "truncated result frame (no length terminator)"
        return
    try:
        length = int(head.decode("ascii"))
    except (ValueError, UnicodeDecodeError):
        sink["error"] = f"bad result frame length: {head[:32]!r}"
        return
    if len(body) < length:
        sink["error"] = f"truncated result frame ({len(body)} of {length} bytes)"
        return
    if len(body) > length:
        # A second message — forged or duplicated — is refused, not merged or preferred.
        sink["error"] = f"{len(body) - length} trailing bytes after the result frame"
        return
    try:
        sink["payload"] = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        sink["error"] = f"malformed result JSON: {exc}"


STATE_KEEP_ENV = "SOLVIO_KEEP_TEST_STATE"
STATE_PREFIX = "solvio-st-"


def _suite_state_dir() -> str:
    """A runner-owned SOLVIO_STATE_DIR per suite.

    Measured 2026-09-18: 492 run-artifact folders of test worlds landed in the
    PRODUCTION tree ~/.solvio/agent_runs because `store.artifact_root()` and the
    other `state_dir()` defaults resolve to ~/.solvio whenever a world builds
    its ledger at a temporary path without redirecting the state directory
    (the DEBT-0223 class). The runner therefore hands every suite its own
    state directory; a suite that deliberately reads real installation state
    does so through explicit home paths, never through this default.
    """
    return tempfile.mkdtemp(prefix=STATE_PREFIX)


def _dispose_suite_state_dir(res: "Result") -> None:
    """Remove the suite's state dir after a clean pass; keep it for diagnosis otherwise."""
    if not res.state_dir:
        return
    keep = os.environ.get(STATE_KEEP_ENV) == "1"
    clean = (res.rc == 0 and not res.failures and not res.timed_out and not res.crashed
             and not res.protocol_error)
    if clean and not keep:
        shutil.rmtree(res.state_dir, ignore_errors=True)
        return
    if not keep:
        # Nothing to diagnose in an empty (or already vanished) directory: a suite that
        # failed before it touched its state must not litter the temp tree (review round
        # 5, C5-H2); a suite that removed its own directory must not crash the runner
        # (review round 6, R6-H-5).
        try:
            with os.scandir(res.state_dir) as entries:
                empty = next(entries, None) is None
        except FileNotFoundError:
            return
        if empty:
            shutil.rmtree(res.state_dir, ignore_errors=True)
            return
    res.state_kept = True
    if not clean:
        res.failures.append(f"<state directory of {res.path.name} kept for diagnosis: {res.state_dir}>")


def run_one(path: Path, expected=(), timeout: int | None = None,
            invalid_defs=()) -> Result:
    # Resolved at CALL time, not bound as a default: a default would freeze the value at
    # import and silently ignore SOLVIO_SUITE_TIMEOUT changes made after that.
    timeout = SUITE_TIMEOUT if timeout is None else timeout
    res = Result(path)
    # P1A.8/H3: the AST sees an async-generator test even if the suite never runs it — a
    # definition error must not depend on the harness noticing at runtime.
    for ident in invalid_defs:
        res.failures.append(f"<INVALID TEST DEFINITION (source): {ident} is an async "
                            f"generator — its body never runs>")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO / "src"), str(path.parent), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    env.pop("PYTHONOPTIMIZE", None)          # never inherit an optimization that hides bugs
    # P1A.8/H1: the channel is an ANONYMOUS PIPE the parent creates. The suite gets no path
    # and no environment variable naming it, and any inherited one is deleted here so an
    # outer environment cannot redirect the verdict.
    env.pop("SOLVIO_TEST_RESULT_PATH", None)
    # N8/C5: every suite gets its own state directory; nothing a suite writes through a
    # `state_dir()` default can reach the installation under ~/.solvio.
    res.state_dir = _suite_state_dir()
    env["SOLVIO_STATE_DIR"] = res.state_dir
    # DEBT-0317: the backup engine keeps lock, log and state.json in its own directory
    # (default ~/.solvio/storage). A suite that ran a real backup without redirecting it
    # wrote into the production backup log and, on 2026-09-23, met the real evening
    # backup on its lock. Overridden unconditionally, like SOLVIO_STATE_DIR.
    env["SOLVIO_STORAGE_STATE_DIR"] = os.path.join(res.state_dir, "storage")
    read_fd, write_fd = os.pipe()
    sink: dict = {}
    reader = threading.Thread(target=_read_frame, args=(read_fd, sink), daemon=True)
    reader.start()
    proc = None
    try:
        proc = subprocess.Popen(
            [sys.executable, str(WORKER), str(path), str(write_fd)],
            cwd=str(REPO), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, pass_fds=(write_fd,))
    finally:
        os.close(write_fd)               # the parent's copy, so the reader sees EOF
    try:
        out, err = proc.communicate(timeout=timeout)
        res.rc = proc.returncode
        res.raw = (out or "") + (err or "")
    except subprocess.TimeoutExpired:
        # P1A.7/§15 + P1A.8/H1: a hanging suite must not hang the gate.
        proc.kill()
        out, err = proc.communicate()
        res.timed_out = True
        res.rc = proc.returncode if proc.returncode is not None else -1
        res.raw = (out or "") + (err or "")
        res.failures.append(f"<suite timed out after {timeout}s>")
    reader.join(timeout=30)
    if reader.is_alive():
        sink.setdefault("error", "the result channel never reached EOF")
    if "payload" in sink:
        res.channel = sink["payload"]
    else:
        res.channel = None
        res.protocol_error = True
        res.failures.append(f"<result channel: {sink.get('error', 'no result')}>")
    if res.rc != 0 and not res.timed_out:
        # Distinguish "the suite reported failures" (rc 1 with a payload) from "the worker
        # died". A negative rc is a signal: the process was killed or crashed.
        if res.rc < 0 or res.channel is None:
            res.crashed = True
            res.failures.append(f"<worker exited with {res.rc} — crash or forced exit>")
    _parse_logs(res)
    if res.timed_out:
        res.failed = max(res.failed, 1)
    _enforce_contract(res, expected)
    # P1A.6/§4: defence in depth only — the structural contract above already catches an
    # un-awaited coroutine; this makes the interpreter's own warning fatal as well. It is
    # deliberately NOT the primary defence: PYTHONWARNINGS=ignore silences the warning, and
    # the structural check must (and does) still fail the suite.
    if "was never awaited" in res.raw:
        res.failed = max(res.failed, 1)
        res.failures.append("<RuntimeWarning: coroutine was never awaited>")
    for ident in invalid_defs:
        if ident not in res.invalid:
            res.invalid.append(ident)
    if invalid_defs:
        res.failed = max(res.failed, len(invalid_defs))
    _enforce_non_empty(res)
    if res.protocol_error or res.crashed:
        res.failed = max(res.failed, 1)
    if res.rc != 0 and res.failed == 0:
        res.failed = 1
        res.failures = res.failures or [f"<exit code {res.rc} without reported failures>"]
    _dispose_suite_state_dir(res)
    return res


BASELINE = TESTS / "test_inventory_baseline.json"


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(REPO))
    except ValueError:
        return str(path)


def _check_baseline(inventory: dict, paths: list[Path]) -> list[str]:
    """P1A.8/H2: the TRACKED baseline is a third, reviewable source.

    The AST inventory and the executed manifest are both derived from the working tree, so
    they agree with each other even when a test was deleted — set equality holds and the
    only visible trace is a smaller total, which is easy to miss in a review. The baseline is
    a file in git: removing a test now requires an explicit, reviewable diff.

    It NEVER self-heals. `scripts/update_test_baseline.py` is the only writer, and it is a
    deliberate operator action.

    Honest about what this is: review assurance, not a cryptographic control. Anyone who can
    edit the tests can edit the baseline in the same commit. What it buys is that doing so is
    VISIBLE in the diff instead of silent.
    """
    problems: list[str] = []
    if not BASELINE.exists():
        return [f"the tracked test baseline is missing: {_rel(BASELINE)} "
                f"(create it with the update script)"]
    try:
        data = json.loads(BASELINE.read_text(encoding="utf-8"))
        recorded = set(data["tests"])
        if not isinstance(data["tests"], list) or not all(isinstance(x, str) for x in recorded):
            raise TypeError("tests must be a list of identity strings")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return [f"the tracked test baseline is unreadable: {exc}"]

    if paths != sorted(p for p in TESTS.rglob("test_*.py") if p.is_file()):
        return []                       # a filtered run cannot judge the whole baseline

    current = baseline_identities(inventory)
    for ident in sorted(recorded - current):
        problems.append(f"test removed or renamed since the baseline: {ident}")
    for ident in sorted(current - recorded):
        problems.append(f"test not in the baseline: {ident}")
    return problems


def baseline_identities(inventory: dict) -> set[str]:
    """`relative_path::qualified_test_name` for every test the tracked source declares."""
    out = set()
    for path, ids in inventory.items():
        rel = Path(path).resolve().relative_to(TESTS).as_posix()
        for ident in ids:
            out.add(f"{rel}::{ident}")
    return out


def main(argv: list[str]) -> int:
    paths = discover(argv)
    if not paths:
        print("no test files found", file=sys.stderr)
        return 2
    inventory = _inventory.discover(str(TESTS))
    invalid = _inventory.discover_invalid(str(TESTS))
    baseline_problems = _check_baseline(inventory, paths)
    results = [run_one(p, inventory.get(str(p.resolve()), []),
                       invalid_defs=invalid.get(str(p.resolve()), []))
               for p in paths]
    width = max(len(str(r.path.relative_to(TESTS))) for r in results)

    print(f"{'SUITE'.ljust(width)}  EXP   EXEC  PASS  FAIL  SKIP")
    print("-" * (width + 32))
    for r in results:
        name = str(r.path.relative_to(TESTS)).ljust(width)
        flag = "" if r.failed == 0 else "   <<< FAIL"
        print(f"{name}  {r.collected:4}  {r.executed:4}  {r.passed:4}  "
              f"{r.failed:4}  {r.skipped:4}{flag}")
        for f in r.failures:
            print(f"    {f}")

    tot = lambda k: sum(getattr(r, k) for r in results)  # noqa: E731
    print("-" * (width + 32))
    print(f"{'TOTAL'.ljust(width)}  {tot('collected'):4}  {tot('executed'):4}  "
          f"{tot('passed'):4}  {tot('failed'):4}  {tot('skipped'):4}")
    miss = sum(len(r.missing) for r in results)
    unex = sum(len(r.unexpected) for r in results)
    dup = sum(len(r.duplicates) for r in results)
    inc = sum(len(r.incomplete) for r in results)
    print(f"\nSuites: {len(results)}   "
          f"EXPECTED={tot('collected')} EXECUTED={tot('executed')} "
          f"PASSED={tot('passed')} FAILED={tot('failed')} SKIPPED={tot('skipped')}")
    invalid = sum(len(r.invalid) for r in results)
    perr = sum(1 for r in results if r.protocol_error)
    tmo = sum(1 for r in results if r.timed_out)
    crash = sum(1 for r in results if r.crashed)
    print(f"Missing={miss} Unexpected={unex} Duplicates={dup} IncompleteAsync={inc} "
          f"InvalidDefinitions={invalid} WorkerProtocolErrors={perr} Timeouts={tmo} "
          f"Crashes={crash} BaselineDrift={len(baseline_problems)}")
    kept = sum(1 for r in results if r.state_kept)
    print(f"StateDirs: removed={len(results) - kept} kept={kept}")

    # P1A.8/H1: every suite that could not be verified is named, not just counted. The gate
    # runs all suites first and reports here — one broken suite must not hide the rest.
    def _name(r):
        return str(r.path.relative_to(TESTS))
    sections = (
        ("RESULT-CHANNEL FAILURES", [f"{_name(r)}: {'; '.join(r.failures[:2])}"
                                     for r in results if r.protocol_error]),
        ("CRASHED WORKERS", [f"{_name(r)}: exit {r.rc}" for r in results if r.crashed]),
        ("TIMEOUTS", [f"{_name(r)}: timed out" for r in results if r.timed_out]),
        ("MISSING TEST IDENTITIES", [f"{_name(r)}: {i}" for r in results for i in r.missing]),
        ("UNEXPECTED TEST IDENTITIES",
         [f"{_name(r)}: {i}" for r in results for i in r.unexpected]),
        ("DUPLICATE EXECUTIONS", [f"{_name(r)}: {i}" for r in results for i in r.duplicates]),
        ("INVALID TEST DEFINITIONS", [f"{_name(r)}: {i}" for r in results for i in r.invalid]),
        ("BASELINE DRIFT", baseline_problems),
    )
    for title, items in sections:
        if items:
            print(f"\n{title} ({len(items)}):")
            for line in items:
                print(f"  - {line}")
    return 0 if tot("failed") == 0 and not baseline_problems else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
