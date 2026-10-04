"""The live voice toolkit must survive the attached agent runtime (independent review A1, 18.09.2026).

Measured: `agent_capability_tools()` registered an LLM tool for EVERY name in
`capabilities.agent.SPECS`, while `schema()` read a fixed table that lacked
`agent_task_action` (since 11.09.) and `agent_task_task` (this candidate).
`dispatcher.openai_tools()` — and with it `live_session.live_toolkit()` — raised
KeyError as soon as the agent runtime was attached, and the voice path could
not delegate an order to the runtime any more (production log:
`live.delegation_failed kind=KeyError`). No suite called the toolkit after the
attachment. This one does.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()


def _attached_dispatcher(folder: str):
    from solvio.config import Settings
    from solvio.tools.registry import build_dispatcher, attach_agent_runtime
    from solvio.agent_runtime.store import AgentRunLedger
    from solvio.agent_runtime.orchestrator import Orchestrator
    dispatcher = build_dispatcher(Settings(openai_api_key="sk-test-not-a-real-key-0000",
                                           cognitive_router_mode="active"))
    ledger = AgentRunLedger(os.path.join(folder, "agent_runs.sqlite3"))
    orchestrator = Orchestrator(ledger=ledger, require_task_authority=True, router=dispatcher.capabilities)
    attach_agent_runtime(dispatcher, orchestrator)
    return dispatcher


def t_the_live_toolkit_builds_after_the_agent_runtime_is_attached():
    from solvio.realtime.live_session import live_toolkit
    from solvio.capabilities.agent import SPECS
    from solvio.tools.agent_capability_tools import _SCHEMAS
    with tempfile.TemporaryDirectory(prefix="solvio-live-toolkit-") as folder, \
            patch.dict(os.environ, {"SOLVIO_STATE_DIR": folder}):
        dispatcher = _attached_dispatcher(folder)
        toolkit = live_toolkit(dispatcher)            # used to raise KeyError
        names = {tool["name"] for tool in dispatcher.openai_tools()}
        # Every agent capability WITH a schema is offered; those without one are
        # registered (dispatchable through their own authorised entrances) but
        # never shown to the model.
        for name in SPECS:
            if name in _SCHEMAS:
                require(name in names, f"{name} missing from the model toolkit")
            else:
                require(name not in names, f"{name} exposed to the model without a schema")
                require(dispatcher.tool(name) is not None, f"{name} must still be registered")
        require("agent_task_task" not in names and "agent_task_action" not in names)
        require(toolkit is not None)


def t_a_capability_without_schema_is_never_exposed_even_when_asked_directly():
    from solvio.tools.agent_capability_tools import AgentCapabilityTool
    tool = AgentCapabilityTool("agent_task_task", router=None, gate=None)
    require_equal(tool.expose_to_llm, False)
    try:
        tool.schema()
    except LookupError:
        return
    require(False, "schema() of a schemaless capability must refuse, not invent")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
