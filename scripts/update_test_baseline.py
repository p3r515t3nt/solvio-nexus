#!/usr/bin/env python3
"""Rewrite the tracked test baseline — a DELIBERATE operator action.

P1A.8/H2. `scripts/run_tests.py` compares three sources: the tracked baseline (this file's
output), the AST inventory of the working tree, and the identities the suites actually
executed. The inventory and the manifest are both derived from the tree, so they still agree
after a test is deleted — set equality holds and the only trace is a smaller total. The
baseline is the source that lives in git and therefore makes a removal REVIEWABLE.

The runner never writes this file. Self-healing would defeat the purpose: a baseline that
repairs itself records whatever happened rather than what was intended. Removing a test is
supposed to cost a second command and show up in the diff.

Honest about what this is: review assurance, not a cryptographic control. Anyone who can
edit the tests can run this in the same commit. What it buys is visibility, not prevention.

    python3 scripts/update_test_baseline.py            # rewrite
    python3 scripts/update_test_baseline.py --check    # exit 1 if it would change
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TESTS = REPO / "tests"
BASELINE = TESTS / "test_inventory_baseline.json"

sys.path.insert(0, str(TESTS))
import _inventory  # noqa: E402


def build() -> dict:
    inventory = _inventory.discover(str(TESTS))
    tests = set()
    for path, ids in inventory.items():
        rel = Path(path).resolve().relative_to(TESTS).as_posix()
        for ident in ids:
            tests.add(f"{rel}::{ident}")
    return {"_comment": "Tracked test baseline (P1A.8/H2). Regenerate ONLY via "
                        "scripts/update_test_baseline.py; the runner never writes it.",
            "suites": len(inventory), "count": len(tests), "tests": sorted(tests)}


def main(argv: list[str]) -> int:
    payload = build()
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    if "--check" in argv:
        current = BASELINE.read_text(encoding="utf-8") if BASELINE.exists() else ""
        if current != text:
            print(f"{BASELINE.relative_to(REPO)} is out of date "
                  f"({payload['count']} tests in {payload['suites']} suites in the tree)",
                  file=sys.stderr)
            return 1
        print(f"baseline up to date: {payload['count']} tests in {payload['suites']} suites")
        return 0
    BASELINE.write_text(text, encoding="utf-8")
    print(f"wrote {BASELINE.relative_to(REPO)}: "
          f"{payload['count']} tests in {payload['suites']} suites")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
