"""Bound observations of a native worker, never execution or result authority."""
import json
import time

from solvio.agent_runtime import cost_dispatch as D, store as S
from solvio.agent_runtime.specialists import WORKER_PROFILES

# Runtime label per worker profile in the progress reference (Codex = Hermes app-server).
WORKER_RUNTIMES = {WORKER_PROFILES['codex']: 'hermes-codex-app-server',
                   WORKER_PROFILES.get('claude-code', 'worker/claude'): 'claude-bare-broker'}


class NativeProgress:
    def __init__(self, ledger, run_id, step_id):
        self.ledger, self.run_id, self.step_id = ledger, run_id, step_id
        self.invocation_id = None

    def __call__(self, event):
        kind = event['event']
        summary = ('Hermes hat den nativen Recherchelauf gestartet.' if kind == 'started'
                   else {'started': 'Öffentliche Quelle wird im Browser geprüft.',
                         'completed': 'Browser-Leseversuch beendet; Ergebnisprüfung steht aus.'}[event['status']]
                   if kind == 'browser_read'
                   else {'started': 'Native Websuche läuft.',
                         'completed': 'Native Websuche beendet; Ergebnisprüfung steht aus.'}[event['status']])
        with self.ledger._open() as connection:
            connection.execute('BEGIN IMMEDIATE')
            step = connection.execute('SELECT run_id,state,kind,specialist_profile,started_at,finished_at '
                                      'FROM agent_steps WHERE step_id=?',
                                      (self.step_id,)).fetchone()
            run = connection.execute('SELECT r.state,t.scope FROM agent_runs r '
                                     'JOIN agent_tasks t ON t.task_id=r.task_id WHERE r.run_id=?',
                                     (self.run_id,)).fetchone()
            if (not step or (step['run_id'], step['state']) != (self.run_id, 'running')
                    or not run or run['state'] not in {S.WAITING_SPECIALIST, S.RUNNING}):
                return
            claim = D.active_task_invocation(connection, self.ledger, self.run_id)
            if not claim or self.invocation_id not in (None, claim['invocation_id']):
                return
            if run['state'] == S.RUNNING:
                # General tasks keep RUNNING while the outer native worker
                # executes. Nested tool steps and unrelated active claims do
                # not inherit this exception to the research progress path.
                if (run['scope'] != S.SCOPE_TASK or step['kind'] != 'specialist'
                        or step['specialist_profile'] not in WORKER_PROFILES.values()
                        or step['started_at'] is None or step['finished_at'] is not None
                        or claim['operation_id'] != self.step_id):
                    return
                if kind == 'started':
                    summary = ('Hermes hat den nativen Auftrag gestartet.'
                               if step['specialist_profile'] == WORKER_PROFILES['codex']
                               else 'Der Claude-Arbeiter hat den nativen Auftrag gestartet.')
            ref = S._native_progress_reference(json.dumps({
                'runtime': WORKER_RUNTIMES.get(step['specialist_profile'], 'hermes-codex-app-server'),
                'invocation_id': claim['invocation_id'],
                'operation_id': claim['operation_id'], 'native_thread_id': event['thread_id'],
                'native_turn_id': event['turn_id'], 'seq': event['seq'], 'event': kind,
                'status': event.get('status', ''), 'item_id': event.get('item_id', '')}))
            if connection.execute("SELECT 1 FROM agent_events WHERE run_id=? AND step_id=? "
                                  "AND kind='native_progress' AND ref=?",
                                  (self.run_id, self.step_id, ref)).fetchone():
                return
            self.invocation_id = claim['invocation_id']
            connection.execute('INSERT INTO agent_events (at,run_id,step_id,kind,summary,ref) '
                'VALUES (?,?,?,?,?,?)', (time.time(), self.run_id, self.step_id,
                                        'native_progress', summary, ref))
            self.ledger._cap_events(connection, self.run_id)
