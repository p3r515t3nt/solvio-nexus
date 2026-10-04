"""A completed quality review is not an account outage; local CLI peer only."""
import json
from pathlib import Path
import sys
from unittest.mock import patch
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'src'),str(Path(__file__).resolve().parent)]
from _guard import enforce_assertions,require,require_equal
enforce_assertions()
import test_agent_document_execution as DOC
from solvio.agent_runtime import store as S

async def check_review(verdict,action):
    original=DOC.cli
    def cli(*args,**kwargs):
        result=original(*args,**kwargs)
        source=result.read_text()
        anchor="\nprint(json.dumps({'type':'item.completed'"
        replacement="\nif phase=='extension_review': reply="+repr({'verdict':verdict,'next_action':action,
            'rationale':'The quality gate needs a technical diagnosis; the provider answered normally.',
            'escalation_event':'ROOT_CAUSE_REQUESTED','findings':[],'close_findings':[],'proven':[]})
        require(anchor in source)
        result.write_text(source.replace(anchor,replacement+anchor,1))
        return result
    with patch.object(DOC,'cli',cli):
        async with DOC.world(repair_first=True) as w:
            response=await w.start(DOC.body(DOC.DOCUMENT));require_equal(response.status,201)
            run_id=(await response.json())['run_id']
            run=await DOC.advance(w.orch,run_id,limit=1000)
            require_equal(run.state,S.FAILED,run.result_summary)
            require_equal(run.failure_category,'capability_failed')
            require('Werkzeugentwicklung' in run.result_summary)
            require('provider_wait' not in (run.boundary or ''))
            decisions=w.development.decisions(run.development_ref)
            decision=next(item for item in decisions if item['verdict']==verdict)
            require('quality gate' in decision['rationale'])
            calls=w.calls()
            require_equal([item['phase'] for item in calls],['plan','extension_build','extension_review'])
            with w.ledger._open() as db:
                require_equal(db.execute("SELECT COUNT(*) FROM agent_provider_invocations WHERE state!='finished'").fetchone()[0],0)

async def t_review_escalation_keeps_actual_decision_without_claiming_provider_outage():
    await check_review('ESCALATE','root_cause')

async def t_human_quality_decision_does_not_create_a_fake_quota_or_account_wait():
    await check_review('NEEDS_HUMAN','human_required')

if __name__=='__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(),__name__))
