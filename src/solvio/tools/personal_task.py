"""Voice entrance to the existing private native worker; no new task authority."""
import asyncio
from solvio.tools.base import RiskLevel, ToolResult
from solvio.tools.agent_capability_tools import _speak
from solvio.capabilities.policy import OriginClass


class PersonalTaskTool:
    name = 'personal_task'
    risk_level = RiskLevel.HARMLESS
    @property
    def expose_to_llm(self):
        # Dispatch and menu use the same flag. A static False would advertise
        # the Live tool but make the real dispatcher reject every invocation.
        from solvio.voice_task_session import VerifiedAppTaskSession
        from solvio.browser_voice_session import VerifiedBrowserTaskSession
        context = self.dispatcher.capability_gate.context()
        return bool(context and (
            context.origin is OriginClass.TRUSTED_INTERACTIVE_APP
            and type(context.app_task_session) is VerifiedAppTaskSession
            or context.origin is OriginClass.TRUSTED_DASHBOARD
            and type(context.browser_task_session) is VerifiedBrowserTaskSession))

    def __init__(self, dispatcher):
        self.dispatcher = dispatcher

    def schema(self):
        return {'type': 'function', 'name': self.name,
            'description': 'Den vollständigen aktuellen persönlichen Auftrag im Hintergrund erledigen: '
                'Mails oder Kalender lesen, daraus einen Antworttext, eine Zusammenfassung oder eine '
                'lokale Datei erstellen. Für mehrstufige Wünsche wie „Lies die Mail und formuliere '
                'eine passende Antwort“ dieses Werkzeug verwenden. Es übernimmt den Originalwunsch '
                'aus dem aktuellen Gespräch. Kein Versand, kein Gmail-Entwurf, keine Kalenderänderung '
                'und keine Websuche. Das Ergebnis erscheint im selben Chat, auch nach Gesprächsende. '
                'Für ausdrücklich beauftragten Mailversand die vorhandenen Mailwerkzeuge verwenden.',
            'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False}}

    async def run(self, arguments):
        refused = ToolResult(False, error='personal_task_source_unavailable',
            human_message='Der persönliche Auftrag braucht einen aktuellen, angemeldeten Chat. Es wurde nichts gestartet.')
        if type(arguments) is not dict or arguments:
            return refused
        gate = self.dispatcher.capability_gate
        context = gate.context()
        runtime = getattr(self.dispatcher, 'agent_runtime', None)
        store = getattr(runtime, 'conversations', None)
        if (context is None or runtime is None or store is None or not context.commanded
                or context.origin not in {OriginClass.TRUSTED_INTERACTIVE_APP, OriginClass.TRUSTED_DASHBOARD}
                or not context.conversation_id):
            return refused

        def current():
            try:
                return (gate.context() is context
                    and self.dispatcher.agent_runtime is runtime
                    and runtime.conversations is store
                    and store.conversation_owned(context.conversation_id, context.principal, for_write=True))
            except Exception:
                return False

        if not current():
            return refused
        # An empty schema prevents the selector from rewriting the request or
        # supplying a private-data flag, chat identifier, recipient or approval.
        args = {'objective': context.user_text}
        start = await gate.authorize_task_start('agent_task_task', args, context=context,
            private_data=True, conversation_current=current)
        if start is None or not current():
            return refused
        result = await self.dispatcher.capabilities.execute('agent_task_task', args,
            trust=context.trust, provenance=gate.provenance_for(args), principal=context.principal,
            origin=context.origin, commanded=context.commanded, task_start=start)
        if result.succeeded:
            # Same idempotent chat index used by task_endpoint and cognition.
            # Admission already happened: a presentation failure must never
            # report "not started" and invite a duplicate task.
            data = result.data if isinstance(result.data, dict) else {}
            task_id, run_id = data.get('task_id'), data.get('run_id')
            if task_id and run_id:
                try:
                    await asyncio.to_thread(store.add_task_link, context.conversation_id,
                        task_id, run_id, revision=1, source='voice:' + context.session_id)
                except Exception as exc:
                    from solvio.logging_setup import get_logger
                    get_logger('agent_runtime').warning('personal_task.conversation_link_failed', kind=type(exc).__name__)
                runtime.offer_task_observation(task_id, run_id)
        return _speak(result)
