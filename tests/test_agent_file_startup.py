"""Optional existing Office runtime does not disable the original document path."""
import os
import sys
from pathlib import Path
from unittest.mock import patch
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'src'),str(Path(__file__).resolve().parent)]
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
import test_agent_task_entry as E
from test_agent_file_execution import body
from solvio.agent_runtime import extension_runtime as R, store as S
from solvio.autopilot.store import AutopilotLedger


async def t_missing_office_runtime_preserves_text_adapter_and_refuses_table_execution():
    async with E.world() as w:
        development=AutopilotLedger(str(Path(w.ledger.path).parent/'development.sqlite3'))
        w.orch.development=development
        with patch.dict(os.environ, {'SOLVIO_FILE_TOOL_RUNTIME':str(Path(w.ledger.path).parent/'not-installed')}):
            R.attach_document_runtime(w.orch)
        require(w.router.spec('document_extract_text') is not None)
        require(w.router.spec('file_process') is None)
        require(w.orch.extension_activation.file_runtime is None)
        response=await w.start(body())
        require_equal(response.status,201)
        run_id=(await response.json())['run_id']
        for _ in range(5):
            await w.orch.tick()
            if w.ledger.get_run(run_id).terminal: break
        require_equal(w.ledger.get_run(run_id).state,S.FAILED)
        require_equal(w.ledger.get_run(run_id).failure_category,'capability_failed')
        with w.ledger._open() as db:
            require_equal(db.execute('SELECT COUNT(*) FROM agent_provider_invocations').fetchone()[0],0)
        await w.orch.stop();development.close()


async def t_absent_setting_is_no_install_or_accidental_runtime_selection():
    with patch.dict(os.environ, {'SOLVIO_FILE_TOOL_RUNTIME':''}), patch.object(R.Path,'resolve',side_effect=AssertionError('must not search host runtime')):
        require_equal(R.configured_file_runtime(),None)


if __name__=='__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(),__name__))
