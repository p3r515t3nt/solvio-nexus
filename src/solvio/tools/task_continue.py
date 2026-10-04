"""Spoken revision of an existing chat task using the existing follow-up service."""
import asyncio
import hashlib
import json
import re

from solvio.tools.base import ToolResult
from solvio.tools.personal_task import PersonalTaskTool
from solvio.capabilities.policy import OriginClass


class TaskContinueTool(PersonalTaskTool):
    name = 'task_continue'

    def schema(self):
        return {'type': 'function', 'name': self.name,
            'description': 'Ein fertiges Auftragsergebnis aus DIESEM Chat nach dem aktuellen '
                'gesprochenen Wunsch überarbeiten oder weiterverwenden, zum Beispiel „mach die '
                'Antwort kürzer“ oder „erstelle daraus eine Datei“. Gleicher Auftrag, bisherige '
                'Dateien und Kosten bleiben erhalten. Zuerst agent_task_status mit scope=current_chat '
                'lesen, falls keine eindeutige Kennung dieses Ergebnisses vorliegt. run_id aus diesem Auftragsstatus '
                'verwenden; keine Kennung erfinden. Der Originalwunsch kommt aus dem aktuellen '
                'Gespräch. Keine neuen Versand-, Kalender- oder Kaufrechte. Bei unklarem Bezug '
                'erst nachfragen. Für einen neuen eigenständigen Auftrag personal_task verwenden.',
            'parameters': {'type': 'object', 'properties': {'run_id': {'type': 'string',
                'description': 'Kennung des zu überarbeitenden fertigen Laufs aus diesem Chat.'}},
                'required': ['run_id'], 'additionalProperties': False}}

    async def run(self, arguments):
        refused = ToolResult(False, error='task_followup_unavailable',
            human_message='Dieses Ergebnis lässt sich in diesem Gespräch gerade nicht weiterbearbeiten. '
                'Bitte nenne den fertigen Auftrag aus diesem Chat; es wurde keine neue Bearbeitung gestartet.')
        if (type(arguments) is not dict or set(arguments) != {'run_id'}
                or type(arguments['run_id']) is not str
                or re.fullmatch(r'ar-[a-f0-9]{16}', arguments['run_id']) is None):
            return refused
        gate = self.dispatcher.capability_gate
        context = gate.context()
        runtime = getattr(self.dispatcher, 'agent_runtime', None)
        store = getattr(runtime, 'conversations', None)
        if (context is None or runtime is None or store is None or not context.commanded
                or context.origin not in {OriginClass.TRUSTED_INTERACTIVE_APP, OriginClass.TRUSTED_DASHBOARD}
                or not context.conversation_id):
            return refused
        ledger, run_id = runtime.ledger, arguments['run_id']

        def current():
            try:
                if (gate.context() is not context or self.dispatcher.agent_runtime is not runtime
                        or runtime.conversations is not store or runtime.ledger is not ledger
                        or not store.conversation_owned(context.conversation_id, context.principal, for_write=True)):
                    return False
                run = ledger.get_run(run_id)
                task = ledger.get_task(run.task_id) if run else None
                return bool(task and task.created_principal == context.principal
                    and any(link['task_id'] == task.task_id and link['run_id'] == run_id
                            for link in store.task_links(context.conversation_id)))
            except Exception:
                return False

        if not current():
            return refused
        from solvio.agent_runtime import task_revisions as TR
        try:
            revision = await asyncio.to_thread(TR.revision_for_run, ledger, run_id)
            identity = json.dumps([context.session_id, context.turn_id, context.conversation_id,
                                   run_id, context.user_text], ensure_ascii=False).encode()
            value = TR.canonical_followup({'run_id': run_id, 'text': context.user_text.strip(),
                'expected_revision': revision['revision'], 'expected_digest': revision['digest'],
                'input_artifact_ids': [],
                'client_request_id': 'voice-followup:' + hashlib.sha256(identity).hexdigest()})
            # Reuse the verified source/turn binding with the full immutable
            # follow-up body. This is a receipt only: never execute a task start.
            # admit_followup inherits the parent's grants; private_data here
            # binds the authenticated chat, it does not grant private tools.
            binding = await gate.authorize_task_start('agent_task_task', {'followup': value},
                context=context, private_data=True, conversation_current=current)
            if binding is None or not current():
                return refused
            try:
                admitted = await asyncio.to_thread(TR.replay, ledger, value, binding.receipt)
                if admitted is None:
                    prepared = await asyncio.to_thread(TR.prepare, ledger, value, binding.receipt)
                    # Last await before atomic admission; session, turn, device,
                    # principal, chat and exact linked parent all remain current.
                    if not await binding.dispatch_current() or not current():
                        return refused
                    _, run = runtime.task_starts.admit_followup(prepared)
                    admitted = TR.revision_for_run(ledger, run.run_id)
            except (ValueError, TypeError, KeyError, OSError):
                admitted = await asyncio.to_thread(TR.replay, ledger, value, binding.receipt)
            if admitted is None:
                return refused
        except (ValueError, TypeError, KeyError, OSError):
            return refused
        # Admission is durable even if the session closes or chat indexing fails
        # now. Report accepted, never falsely invite another new task.
        try:
            await asyncio.to_thread(store.add_task_link, context.conversation_id,
                admitted['task_id'], admitted['run_id'], revision=admitted['revision'],
                source='voice:' + context.session_id)
        except Exception as exc:
            from solvio.logging_setup import get_logger
            get_logger('agent_runtime').warning('task_followup.conversation_link_failed', kind=type(exc).__name__)
        runtime.offer_task_observation(admitted['task_id'], admitted['run_id'])
        return ToolResult(True, data={key: admitted[key] for key in ('task_id', 'run_id', 'revision')},
            human_message='Ich bearbeite das Ergebnis im selben Auftrag weiter. Die neue Fassung erscheint in diesem Chat.')
