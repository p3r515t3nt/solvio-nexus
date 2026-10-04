"""Independent expected-test inventory — the SOLL side of the assurance contract.

P1A.6/§1. The canonical runner used to believe whatever a suite printed about itself. Two
failures were reproduced against that: an `async` test in a suite whose `__main__` calls
tests synchronously produced an un-awaited coroutine and was counted PASS; and editing only
a suite's enumerator line silently dropped ten tests — including every App-Attest
canonicalisation and execution-revocation-predicate test — while the runner reported
FAILED=0.

The truth therefore has to come from the tracked test SOURCE, independently of what the
suite chooses to run. This module parses the AST and answers, per suite: which test
identities are supposed to exist. It hardcodes no global count — a number like
`EXPECTED = 460` would be a second thing to forget to update.

Discovery covers the forms this repo actually contains (verified by census, not assumed):
module-level `test_*` and `t_*`, `async def` of both, and `test_*` methods on
`unittest.TestCase` subclasses (including async ones). Dependency-free: stdlib `ast` only.

Identity format: bare name for a module-level function, `Class.method` for a TestCase
method — the same string the harness reports back.
"""
from __future__ import annotations

import ast
import os

TEST_PREFIXES = ("test_", "t_")


def _is_test_name(name: str) -> bool:
    return name.startswith(TEST_PREFIXES)


def _is_async_generator(node) -> bool:
    """P1A.8/H3: an `async def` whose body yields is an ASYNC GENERATOR, not a test.

    Calling it merely constructs the generator — not one line of the body runs — and
    `inspect.iscoroutinefunction` is False for it, so a harness that only awaits coroutines
    would have reported it PASS. The AST side detects it independently of the harness, so a
    definition error cannot hide behind whichever runner happens to be used.

    `yield` inside a NESTED function or comprehension belongs to that inner scope and does
    not make the test itself a generator, so nested scopes are not descended into.
    """
    if not isinstance(node, ast.AsyncFunctionDef):
        return False
    stack = list(node.body)
    while stack:
        item = stack.pop()
        if isinstance(item, (ast.Yield, ast.YieldFrom)):
            return True
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                             ast.Lambda)):
            continue                      # a different scope's yield
        stack.extend(ast.iter_child_nodes(item))
    return False


def suite_invalid(path: str) -> list[str]:
    """Test identities in one suite that are INVALID DEFINITIONS (async generators)."""
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    bad: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and _is_test_name(node.name) \
                and _is_async_generator(node):
            bad.append(node.name)
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.AsyncFunctionDef) and _is_test_name(item.name) \
                        and _is_async_generator(item):
                    bad.append(f"{node.name}.{item.name}")
    return bad


def _base_names(node: ast.ClassDef) -> list[str]:
    out = []
    for base in node.bases:
        text = ast.unparse(base) if hasattr(ast, "unparse") else ""
        out.append(text.rsplit(".", 1)[-1])
    return out


def _own_tests(node: ast.ClassDef) -> list[str]:
    return [item.name for item in node.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            and _is_test_name(item.name)]


def suite_expected(path: str) -> list[str]:
    """Expected test identities in one suite file, in source order.

    Class handling resolves LOCAL inheritance to a fixed point: `tests/memory/` derives its
    cases from a module-private `MemoryTestBase(unittest.IsolatedAsyncioTestCase)`, so a
    check for a base literally spelled `TestCase` misses 101 real tests. Inherited test
    methods count for the subclass, because that is what unittest actually runs.
    """
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)

    classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
    is_case: set[str] = set()
    changed = True
    while changed:                       # fixed point over local base classes
        changed = False
        for name, node in classes.items():
            if name in is_case:
                continue
            bases = _base_names(node)
            if any("TestCase" in b for b in bases) or any(b in is_case for b in bases):
                is_case.add(name)
                changed = True

    def inherited(name: str, seen: set[str]) -> list[str]:
        if name in seen or name not in classes:
            return []
        seen.add(name)
        out: list[str] = []
        for b in _base_names(classes[name]):
            out += inherited(b, seen)
        out += _own_tests(classes[name])
        return out

    found: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_test_name(node.name):
            found.append(node.name)
        elif isinstance(node, ast.ClassDef) and node.name in is_case:
            names = []
            for m in inherited(node.name, set()):
                if m not in names:
                    names.append(m)
            found += [f"{node.name}.{m}" for m in names]
    return found


def discover_invalid(tests_dir: str) -> dict[str, list[str]]:
    """Every tracked suite mapped to its INVALID test definitions (usually empty)."""
    out: dict[str, list[str]] = {}
    for root, _dirs, files in os.walk(tests_dir):
        for name in sorted(files):
            if name.startswith("test_") and name.endswith(".py"):
                path = os.path.join(root, name)
                bad = suite_invalid(path)
                if bad:
                    out[os.path.abspath(path)] = bad
    return out


def discover(tests_dir: str) -> dict[str, list[str]]:
    """Every tracked suite under `tests_dir`, recursively, mapped to its expected identities."""
    out: dict[str, list[str]] = {}
    for root, _dirs, files in os.walk(tests_dir):
        for name in sorted(files):
            if name.startswith("test_") and name.endswith(".py"):
                path = os.path.join(root, name)
                out[os.path.abspath(path)] = suite_expected(path)
    return out


if __name__ == "__main__":  # pragma: no cover - operator convenience
    import json
    import sys
    base = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
    inv = discover(base)
    total = sum(len(v) for v in inv.values())
    print(json.dumps({"suites": len(inv), "tests": total}, indent=2))
    for p in sorted(inv):
        print(f"{len(inv[p]):4}  {os.path.relpath(p, base)}")
