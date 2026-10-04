"""pytest integration (optional — `scripts/run_tests.py` is the canonical path).

Two things pytest needs here, both consequences of how these suites are written:
  * the security tests are named `t_*` (see pyproject `python_functions`), and
  * most of them are `async def` with no asyncio plugin installed.
The hook below runs coroutine tests directly, so no extra dependency is required.

P1A.4/C2: the optimize guard is repeated here because pytest is a second entry point and
must not be a way around it. It is not an `assert` — that would be self-defeating.
"""
import asyncio
import os
import sys

import pytest

if sys.flags.optimize != 0:  # pragma: no cover
    raise SystemExit(
        "Security regression must not run under optimized Python: -O strips `assert`, "
        "so these suites would report success without checking anything.")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem):
    fn = pyfuncitem.obj
    if asyncio.iscoroutinefunction(fn):
        kwargs = {n: pyfuncitem.funcargs[n] for n in pyfuncitem._fixtureinfo.argnames}
        asyncio.run(fn(**kwargs))
        return True
    return None
