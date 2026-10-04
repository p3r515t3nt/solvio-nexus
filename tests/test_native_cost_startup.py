"""Execute the actual Core runtime-start block, with only external wiring isolated."""
from __future__ import annotations

import ast
import asyncio
from copy import deepcopy
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal, RequirementFailed
enforce_assertions()

from solvio.agent_runtime import extension_runtime as E, planner as P, store as S, workspace as W
from solvio.agent_runtime.native_costs import NativeSubscriptionCosts
from solvio.agent_runtime.orchestrator import Orchestrator
from solvio.tools import registry as R
from solvio.config import Settings


def _entry(*, wrong_import=False):
    source = Path(__file__).parents[1] / 'src/solvio/realtime/core_server.py'
    tree = ast.parse(source.read_text(), filename=str(source))
    serve = next(node for node in ast.walk(tree)
                 if isinstance(node, ast.AsyncFunctionDef) and node.name == 'serve')
    lifetime = next(node for node in serve.body if isinstance(node, ast.Try) and node.finalbody)
    blocks = [node for node in lifetime.body if isinstance(node, ast.If)
              and any(isinstance(value, ast.Constant) and value.value == 'SOLVIO_AGENT_RUNTIME'
                      for value in ast.walk(node.test))]
    require_equal(len(blocks), 1)
    block = deepcopy(blocks[0])
    prefix = []
    if wrong_import:
        # Recreate the exact former bug in memory: an import local to a
        # preceding nested helper cannot bind the name used by startup.
        imports = [node for node in ast.walk(block) if isinstance(node, ast.ImportFrom)
                   and node.module == 'solvio.agent_runtime.native_costs']
        require_equal(len(imports), 1)
        broken = imports[0]
        class RemoveImport(ast.NodeTransformer):
            def visit_ImportFrom(self, node):
                return None if node is broken else node
        block = RemoveImport().visit(block)
        prefix = ast.parse('def _development_wiring():\n    pass\n_development_wiring()').body
        prefix[0].body = [broken]
    function = ast.parse('async def startup(self):\n    agent_runtime = None\n    return agent_runtime').body[0]
    function.body = prefix + function.body[:1] + [block] + function.body[-1:]
    module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    return compile(module, str(source) + ':runtime-start-block', 'exec')


async def _run(*, enabled=True, wrong_import=False, live=False):
    with tempfile.TemporaryDirectory(prefix='solvio-runtime-startup-') as directory:
        ledger = S.AgentRunLedger(str(Path(directory) / 'agent.sqlite3'))
        workspace = W.WorkspaceManager(allowed=(directory,))
        planner = SimpleNamespace(route={'provider': 'claude-code', 'billing_mode': 'subscription'},
                                  plan=AsyncMock(side_effect=AssertionError('unexpected model call')))
        log, registration, extension = Mock(), Mock(), Mock()
        self = SimpleNamespace(dispatcher=SimpleNamespace(), conversations=object(),
                               model='gpt-live-1' if live else 'gpt-realtime-2.1')
        settings = Settings(_env_file=None, agent_runtime_subscription_provider='codex',
                            agent_runtime_subscription_model='')
        environment = {'os': os, 'log': log, '_load_settings': lambda: settings,
                       '_dev_buch': None, '_dev_fabrik': None}
        # In particular this is NOT populated from test globals, which would
        # accidentally repair a missing production import for the test.
        require('NativeSubscriptionCosts' not in environment)
        exec(_entry(wrong_import=wrong_import), environment)
        runtime = None
        with patch.dict(os.environ, {'SOLVIO_AGENT_RUNTIME': '1' if enabled else 'off'}), \
            patch.object(S, 'AgentRunLedger', return_value=ledger) as create_ledger, \
            patch.object(W, 'WorkspaceManager', return_value=workspace), \
            patch.object(P, 'planner_from_settings', return_value=planner), \
            patch.object(E, 'attach_document_runtime', extension), \
            patch.object(R, 'attach_agent_runtime', registration):
            runtime = await environment['startup'](self)
            try:
                if not enabled:
                    require_equal(runtime, None)
                    require_equal(create_ledger.call_count, 0)
                    require_equal(registration.call_count, 0)
                    require_equal(extension.call_count, 0)
                    require_equal(log.error.call_count, 0)
                    return
                if runtime is None:
                    failures = [call.kwargs.get('kind') for call in log.error.call_args_list]
                    if wrong_import:
                        require_equal(failures, ['NameError'])
                    require(False, 'real Core start returned no agent runtime')
                require(type(runtime) is Orchestrator)
                require(runtime.conversations is self.conversations)
                require(type(runtime.cost_quote_adapter) is NativeSubscriptionCosts)
                require(runtime.require_task_authority)
                require(runtime.ledger is ledger and runtime.workspaces is workspace)
                require(runtime._task is not None and not runtime._task.done())
                require_equal(registration.call_count, 1)
                require_equal(registration.call_args.args, (self.dispatcher, runtime))
                require_equal(extension.call_args.args, (runtime,))
                require_equal(log.error.call_count, 0)
                if live:
                    from solvio.agent_runtime.voice_delegate import LiveBackend
                    backend = self.dispatcher.live_backend
                    require(type(backend) is LiveBackend)
                    require(backend.ledger is ledger)
                    require(backend.quote_adapter is runtime.cost_quote_adapter)
                    require(backend.settlement_adapter is runtime.cost_settlement_adapter)
                else:
                    require(not hasattr(self.dispatcher, 'live_backend'))
                planner.plan.assert_not_awaited()
                require_equal(ledger.active_runs(), [])
            finally:
                if runtime is not None:
                    await runtime.stop()
                    require_equal(runtime._task, None)


def t_actual_start_imports_native_costs_and_old_nested_import_mutation_fails():
    async def go():
        await _run()
        try:
            await _run(wrong_import=True)
        except RequirementFailed as error:
            require_equal(str(error), 'real Core start returned no agent runtime')
        else:
            require(False, 'nested-import mutation survived real runtime startup')
    asyncio.run(go())


def t_runtime_off_constructs_and_registers_nothing():
    asyncio.run(_run(enabled=False))


def t_live_backend_uses_existing_runtime_ledger_and_cost_adapters():
    asyncio.run(_run(live=True))


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
