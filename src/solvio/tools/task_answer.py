"""Current authenticated spoken answer -> existing research question journal.

The model selects the Core-issued question, never the answer or authority.
No new task, grant, cost policy or approval.
"""
import hashlib
import json

from solvio.capabilities.policy import OriginClass
from solvio.tools.base import ToolResult
from solvio.tools.personal_task import PersonalTaskTool


class TaskAnswerTool(PersonalTaskTool):
    name = 'task_answer'

    def schema(self):
        return {'type': 'function', 'name': self.name,
            'description': 'Die aktuelle gesprochene Antwort auf eine offene Recherche-Rückfrage '
                'aus DIESEM Chat übernehmen und denselben Auftrag fortsetzen. Zuerst '
                'agent_task_status mit scope=current_question lesen; run_id und sämtliche '
                'Fragefelder genau aus dessen research_question übernehmen. Nur verwenden, '
                'wenn der Nutzer die dort gestellte Frage beantwortet. Den Wortlaut der '
                'Antwort liest der Core aus dem aktuellen Gespräch. Keine Kennung erfinden, '
                'keinen neuen Auftrag starten. Kosten-, Versand- oder Freigabefragen sind '
                'hiermit nicht beantwortbar. Bei unklarem Bezug nachfragen.',
            'parameters': {'type': 'object', 'properties': {
                'run_id': {'type': 'string'}, 'question_id': {'type': 'string'},
                'expected_revision': {'type': 'integer'}, 'expected_digest': {'type': 'string'}},
                'required': ['run_id', 'question_id', 'expected_revision', 'expected_digest'],
                'additionalProperties': False}}

    async def run(self, arguments):
        from solvio.agent_runtime import action_intent as AI, store as S
        refused = ToolResult(False, error='research_answer_unavailable',
            human_message='Diese Antwort konnte ich der offenen Recherchefrage nicht sicher zuordnen. '
                'Bitte lass mich den aktuellen Auftrag prüfen. Es wurde kein neuer Auftrag gestartet.')
        if type(arguments) is not dict or set(arguments) != set(self.schema()['parameters']['required']):
            return refused
        gate = self.dispatcher.capability_gate
        context = gate.context()
        runtime = getattr(self.dispatcher, 'agent_runtime', None)
        store = getattr(runtime, 'conversations', None)
        if (context is None or runtime is None or store is None or not context.commanded
                or context.origin not in {OriginClass.TRUSTED_INTERACTIVE_APP, OriginClass.TRUSTED_DASHBOARD}
                or not context.conversation_id):
            return refused
        ledger = runtime.ledger
        try:
            identity = json.dumps([context.session_id, context.turn_id, context.conversation_id,
                arguments, context.user_text], ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
            body = AI.canonical_answer({**arguments, 'answer': context.user_text.strip(),
                'client_request_id': 'voice-answer:' + hashlib.sha256(identity).hexdigest()})
            if len(body['answer']) > 400:
                return refused
            run_id = body['run_id']

            def current():
                if (gate.context() is not context or self.dispatcher.agent_runtime is not runtime
                        or runtime.conversations is not store or runtime.ledger is not ledger
                        or not store.conversation_owned(context.conversation_id, context.principal, for_write=True)):
                    return False
                run = ledger.get_run(run_id)
                task = ledger.get_task(run.task_id) if run else None
                return bool(task and task.scope == S.SCOPE_RESEARCH and task.state == S.TASK_ACTIVE
                    and task.created_principal == context.principal
                    and task.conversation_ref == context.conversation_id
                    and any(link['task_id'] == task.task_id and link['run_id'] == run_id
                            for link in store.task_links(context.conversation_id)))

            if not current():
                return refused
            # As in task_continue: bind a receipt only, do not execute a start.
            binding = await gate.authorize_task_start('agent_task_task', {'research_answer': body},
                context=context, private_data=True, conversation_current=current)
            if binding is None or not await binding.dispatch_current() or not current():
                return refused
            # No await between final source/turn/chat check and atomic answer.
            # The existing journal verifies exact question/revision and replay.
            result = AI.answer(ledger, body, binding.receipt)
            run = ledger.get_run(run_id)
        except (ValueError, TypeError, KeyError, OSError):
            return refused
        runtime.offer_task_observation(run.task_id, run_id)
        return ToolResult(True, data={'task_id': run.task_id, 'run_id': run_id,
            'revision': body['expected_revision'], 'action_intent': result},
            human_message='Die Antwort ist übernommen. Ich recherchiere im selben Auftrag weiter.')
