"""Shared standalone test harness — the IST side of the assurance contract.

P1A.6/§2. Each suite used to carry its own `__main__` loop. Ten of them enumerated tests and
called `fn()` unconditionally: an `async def` test there returned a coroutine that was never
awaited, raised nothing, and was reported PASS. That was reproduced in `test_approval.py` —
the suite guarding the S1 broker's digest binding — where a deliberately failing async test
printed `18/18 bestanden` and exited 0.

This harness runs every discovered test itself and reports, per test identity, whether it
started, completed, and how. Two rules matter:

  * a coroutine function is awaited via `asyncio.run`;
  * a *sync* function that RETURNS an awaitable is a failure, not a pass — that is exactly
    the un-awaited-coroutine bug, and silently discarding the object would hide it again.

Alongside the human-readable output it emits one machine-readable manifest line. The
canonical runner compares that manifest's identities against the independent AST inventory,
so a test that quietly stops being registered is a hard failure rather than a smaller green
number. The runner does not have to trust this file: the manifest is checked against source.

Dependency-free (stdlib only), and it refuses optimized Python like every other entry point.
"""
from __future__ import annotations

import asyncio
import inspect
import sys
import traceback
import unittest

from _guard import enforce_assertions

enforce_assertions()

MANIFEST_PREFIX = "##SOLVIO-TEST-MANIFEST## "   # legacy human-readable marker only
TEST_PREFIXES = ("test_", "t_")

# P1A.8/H1: the harness no longer writes the verdict anywhere. It hands the results to
# whoever ran it; `scripts/_test_worker.py` frames them onto the runner's anonymous pipe.
# There is no path, no environment variable, and nothing inside the suite process that
# addresses the channel.
_LAST_RESULTS: list | None = None


def last_results():
    """The results of the most recent run, or None if no suite ran through this harness."""
    return None if _LAST_RESULTS is None else [dict(r) for r in _LAST_RESULTS]


class _Outcome:
    __slots__ = ("identity", "started", "completed", "status", "detail")

    def __init__(self, identity: str) -> None:
        self.identity = identity
        self.started = False
        self.completed = False
        self.status = "not_run"
        self.detail = ""

    def as_dict(self) -> dict:
        return {"id": self.identity, "started": self.started, "completed": self.completed,
                "status": self.status, "detail": self.detail[:300]}


def _discover(namespace: dict, module_name: str) -> list[tuple[str, object]]:
    """Module-level test callables defined in THIS module (not imported helpers)."""
    out = []
    for name, obj in sorted(namespace.items()):
        if not name.startswith(TEST_PREFIXES) or not callable(obj):
            continue
        if getattr(obj, "__module__", None) != module_name:
            continue
        if inspect.isclass(obj):
            continue
        out.append((name, obj))
    return out


def _run_one(identity: str, fn) -> _Outcome:
    res = _Outcome(identity)
    res.started = True
    # P1A.8/H3: an `async def` containing `yield` is an ASYNC GENERATOR, not a coroutine.
    # asyncio.run() rejects it, but `inspect.iscoroutinefunction` is False for it, so the
    # sync branch below would merely CREATE the generator object — no line of the test body
    # would ever execute, and it would have been reported PASS. It is a definition error,
    # not a runtime outcome, so it fails as one.
    if inspect.isasyncgenfunction(fn):
        res.completed = True
        res.status = "invalid_definition"
        res.detail = (f"INVALID TEST DEFINITION: {identity} is an async generator "
                      f"(`async def` with `yield`) — its body never runs. Remove the yield "
                      f"or make it a plain `async def` test.")
        return res
    try:
        if inspect.iscoroutinefunction(fn):
            asyncio.run(fn())
        else:
            returned = fn()
            if inspect.isasyncgen(returned):
                returned.aclose().close()
                res.completed = True
                res.status = "invalid_definition"
                res.detail = (f"INVALID TEST DEFINITION: {identity} returned an async "
                              f"generator — its body never ran")
                return res
            if inspect.isawaitable(returned):
                # Never silently drop it: this IS the bug this harness exists to catch.
                returned.close() if hasattr(returned, "close") else None
                res.completed = True
                res.status = "incomplete_async"
                res.detail = (f"{identity} returned an awaitable without being awaited — "
                              f"declare it `async def` or await it inside the test")
                return res
        res.completed = True
        res.status = "passed"
    except unittest.SkipTest as exc:
        res.completed = True
        res.status = "skipped"
        res.detail = str(exc)
    except BaseException as exc:  # noqa: BLE001 - a test may raise anything
        res.completed = True
        res.status = "failed"
        res.detail = f"{type(exc).__name__}: {exc}"
        res.detail += "\n" + "".join(traceback.format_exception_only(type(exc), exc)).strip()
    return res


def _emit(results: list[_Outcome]) -> int:
    """Human-readable output to stdout; the authoritative result is HANDED BACK, not written.

    P1A.7/H4 stopped the runner parsing a verdict out of the suite's own stdout/stderr — a
    suite could print a second, forged manifest and turn a failing test green.
    P1A.8/H1 removes the remaining addressable channel: there is no `SOLVIO_TEST_RESULT_PATH`
    any more. The results live here until `scripts/_test_worker.py` collects them and frames
    them onto a pipe the suite never sees. stdout/stderr are logs and carry no authority.
    """
    global _LAST_RESULTS
    passed = sum(1 for r in results if r.status == "passed")
    skipped = sum(1 for r in results if r.status == "skipped")
    for r in results:
        if r.status == "passed":
            print(f"PASS {r.identity}")
        elif r.status == "skipped":
            print(f"SKIP {r.identity}: {r.detail}")
        else:
            label = "INVALID" if r.status == "invalid_definition" else "FAIL"
            print(f"{label} {r.identity}: "
                  f"{r.detail.splitlines()[0] if r.detail else r.status}")
    print(f"\n=== {passed}/{len(results)} bestanden ===")
    _LAST_RESULTS = [r.as_dict() for r in results]
    return 0 if passed + skipped == len(results) else 1


def run_module(namespace: dict, module_name: str) -> int:
    """Entry point for a module-level (`test_*` / `t_*` function) suite."""
    tests = _discover(namespace, module_name)
    return _emit([_run_one(name, fn) for name, fn in tests])


def run_unittest(namespace: dict, module_name: str) -> int:
    """Entry point for a `unittest.TestCase` suite.

    Uses unittest's own loader and runner so async `IsolatedAsyncioTestCase` tests execute
    exactly as they did, then reports the same manifest keyed `Class.method`.
    """
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for name, obj in sorted(namespace.items()):
        if inspect.isclass(obj) and issubclass(obj, unittest.TestCase) \
                and getattr(obj, "__module__", None) == module_name:
            suite.addTests(loader.loadTestsFromTestCase(obj))

    def identity(case) -> str:
        return f"{type(case).__name__}.{case._testMethodName}"

    results = {identity(c): _Outcome(identity(c)) for c in _iter_cases(suite)}

    class _Collector(unittest.TextTestResult):
        def startTest(self, test):
            super().startTest(test)
            results[identity(test)].started = True

        def stopTest(self, test):
            super().stopTest(test)
            r = results[identity(test)]
            r.completed = True
            if r.status == "not_run":
                r.status = "passed"

        def addError(self, test, err):
            super().addError(test, err)
            r = results[identity(test)]
            r.status, r.detail = "failed", f"{err[0].__name__}: {err[1]}"

        def addFailure(self, test, err):
            super().addFailure(test, err)
            r = results[identity(test)]
            r.status, r.detail = "failed", f"{err[0].__name__}: {err[1]}"

        def addSkip(self, test, reason):
            super().addSkip(test, reason)
            r = results[identity(test)]
            r.status, r.detail = "skipped", reason

        def addSubTest(self, test, subtest, err):
            # P1A.7/§13: CPython appends a failing subTest straight to self.failures without
            # routing through addFailure, so the parent test stayed "not_run" and stopTest
            # promoted it to passed. A failing subTest fails its parent.
            super().addSubTest(test, subtest, err)
            if err is not None:
                r = results[identity(test)]
                r.status = "failed"
                r.detail = f"subTest {subtest._subDescription()}: {err[0].__name__}: {err[1]}"

        def addExpectedFailure(self, test, err):
            super().addExpectedFailure(test, err)
            r = results[identity(test)]
            if r.status == "not_run":
                r.status, r.detail = "passed", "expected failure"

        def addUnexpectedSuccess(self, test):
            # A test marked @expectedFailure that passes is a release-blocking signal:
            # the assumption it encodes no longer holds.
            super().addUnexpectedSuccess(test)
            r = results[identity(test)]
            r.status = "failed"
            r.detail = "unexpected success: @expectedFailure test passed"

    runner = unittest.TextTestRunner(verbosity=0, resultclass=_Collector, stream=sys.stderr)
    outcome = runner.run(suite)
    ordered = [results[k] for k in sorted(results)]
    if not outcome.wasSuccessful() and all(r.status in ("passed", "skipped") for r in ordered):
        # unittest disagrees with our per-test view: trust unittest and fail closed.
        for r in ordered:
            if r.status == "passed":
                r.status = "failed"
                r.detail = "unittest reported the run as unsuccessful"
                break
    return _emit(ordered)


def _iter_cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _iter_cases(item)
        else:
            yield item
