"""Shared test seam for the N8/C4 Claude task worker.

The former `claude_claim_visible()` seam (a temporary double of
`cost_dispatch.active_task_invocation` while the product still pinned the
physical claim lookup to `provider='codex'`) is retired: the product names
both native worker providers itself (cost_dispatch.py, `provider IN
('codex','claude-code')`), and the Claude suites run against that product
path with no patch. Only the module double for A's `claude_native_task`
surface remains here.
"""
from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

WORKER_PROVIDERS = ("codex", "claude-code")


@contextmanager
def claude_module_double(module):
    """Install a double of `solvio.specialists.claude_native_task` for BOTH
    lookup paths: `sys.modules` and the package attribute (`from
    solvio.specialists import claude_native_task` prefers the attribute once
    the real module was imported anywhere in the process)."""
    import sys
    import solvio.specialists as package
    with patch.dict(sys.modules, {"solvio.specialists.claude_native_task": module}), \
            patch.object(package, "claude_native_task", module, create=True):
        yield module
