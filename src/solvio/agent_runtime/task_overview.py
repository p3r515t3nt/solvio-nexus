"""A personal worker reads current open orders from the existing Core ledger.

The authenticated task supplies the owner; the model supplies no identity or
query. This is a snapshot, never a new task store or permission to continue work.
"""
from dataclasses import dataclass
import time

from solvio.agent_runtime import store as S
from solvio.capabilities.contract import CapabilitySpec, ExecutionClass, CapabilityRefused
from solvio.capabilities.task_read import task_material, fit
from solvio.security.mobile_approval.execution import READ_ONLY
from solvio.tools.base import RiskLevel

NAME = 'owner_task_overview'
ROUTE = ('local.owner-tasks', 'overview')
CONTRACT = 'solvio:owner-task-overview:v1'
LIMIT = 20
SPEC = CapabilitySpec(name=NAME, version=1, execution_class=ExecutionClass.FAST,
    base_risk=RiskLevel.HARMLESS, semantics=READ_ONLY, executor='local', timeout=15.0,
    input_schema={'type':'object','properties':{}},
    description='Liest offene SOLVIO-Aufträge desselben Owners für den Tagesüberblick. '
                'Keine Fortsetzung, keine Freigabe und kein Nachrichtenversand.')


def overview(ledger, task_id, run_id):
    # One read transaction: totals and rows describe the same snapshot. Filter
    # by owner before limiting; other owners cannot hide a person's old work.
    with ledger._open() as db:
        db.execute('BEGIN')
        source = db.execute('SELECT t.created_principal FROM agent_tasks t '
            'JOIN agent_runs r ON r.task_id=t.task_id WHERE t.task_id=? AND r.run_id=?',
            (task_id,run_id)).fetchone()
        if not source or not source[0]:
            raise ValueError('local_service_binding_invalid')
    return overview_for_owner(ledger, source[0], exclude_task_id=task_id)


def overview_for_owner(ledger, owner, *, exclude_task_id=''):
    if not isinstance(owner, str) or not owner:
        raise ValueError('local_service_binding_invalid')
    with ledger._open() as db:
        db.execute('BEGIN')
        args = (owner,exclude_task_id,*sorted(S.TERMINAL_STATES))
        where = (' FROM agent_runs r JOIN agent_tasks t ON t.task_id=r.task_id '
            'WHERE t.created_principal=? AND t.task_id<>? '
            'AND r.state NOT IN ('+','.join('?' for _ in S.TERMINAL_STATES)+') '
            'AND NOT EXISTS (SELECT 1 FROM agent_runs n WHERE n.task_id=r.task_id '
            'AND (n.created_at>r.created_at OR (n.created_at=r.created_at AND n.rowid>r.rowid)))')
        total = db.execute('SELECT COUNT(*)'+where,args).fetchone()[0]
        rows = db.execute('SELECT r.run_id,t.task_id,t.objective,r.state,r.updated_at'+where+
            " ORDER BY CASE WHEN r.state IN ('WAITING_USER','WAITING_APPROVAL') THEN 0 ELSE 1 END,"
            ' r.created_at ASC,r.run_id LIMIT ?',(*args,LIMIT)).fetchall()
        from solvio.agent_runtime.inquiry import STATE_WORDS
        data = {'stand':time.time(),'offen_gesamt':total,'gekuerzt':total>len(rows),
            'auftraege':[{'lauf':r['run_id'],'auftrag':r['task_id'],
                'anliegen':r['objective'], 'anliegen_gekuerzt':len(r['objective'])>500,
                'zustand':r['state'],'bedeutung':STATE_WORDS.get(r['state'],'unbekannt'),
                'aktualisiert':r['updated_at']} for r in rows],
            'grenze':'Nur offene Auftragsläufe. Einzelne Mailfreigaben, Hintergrundaufgaben '
                     'und Termine sind hier nicht enthalten. Kein Auftrag wurde fortgesetzt.'}
    return fit(task_material(data),500)


@dataclass(frozen=True)
class OwnerTaskOverview:
    ledger: object

    def resources(self,spec,arguments,task_step=None):
        from solvio.agent_runtime.task_start_service import TaskStepAuthority
        if (spec is not SPEC or type(arguments) is not dict or arguments
                or type(self.ledger) is not S.AgentRunLedger
                or type(task_step) is not TaskStepAuthority):
            raise ValueError('local_service_binding_invalid')
        return {'contract':CONTRACT,'ledger_path':self.ledger.path,
                'task_id':task_step.task_id,'run_id':task_step.run_id}

    def quote(self,service,invocation):
        from solvio.agent_runtime.cost_dispatch import CostQuote
        from solvio.agent_runtime.costs import CostEvidence
        if (service != ROUTE[0] or invocation.capability != NAME
                or invocation.version != 1 or invocation.operation != ROUTE[1]):
            raise ValueError('local_service_binding_invalid')
        return CostQuote(0,CostEvidence('free_local',CONTRACT))

    async def execute(self,arguments,task_step):
        self.resources(SPEC,arguments,task_step)
        return overview(self.ledger,task_step.task_id,task_step.run_id)

    async def __call__(self,arguments):
        raise CapabilityRefused('action_task_authority_required',
            'Diese Übersicht gehört zu einem gebundenen persönlichen Auftrag.')


SERVICE_METHODS = {name:getattr(OwnerTaskOverview,name) for name in ('resources','quote','execute')}


def register(router,ledger):
    if router.spec(NAME) is not None:
        raise ValueError('capability already registered: '+NAME)
    router.register(SPEC,OwnerTaskOverview(ledger))
