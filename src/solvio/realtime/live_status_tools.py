"""Read-only voice adapters for the Core's existing public status operations."""
import asyncio
from solvio.tools.base import RiskLevel, ToolResult


class CoreStatusTool:
    risk_level = RiskLevel.HARMLESS
    expose_to_llm = True

    def __init__(self, control, name):
        if name not in {"note_status", "agent_task_status"}:
            raise ValueError("not_a_voice_read_operation")
        self.control, self.name = control, name

    def schema(self):
        noun = "Notiz" if self.name == "note_status" else "Auftrag"
        properties = {"text": {"type": "string", "description": "Bekannter Wortlaut; leer sucht die jüngsten Vorgänge."}}
        if self.name == "agent_task_status":
            properties["key"] = {"type": "string", "description": "Nur eine zuvor vom Core gelieferte Kennung."}
            properties["scope"] = {"type": "string", "enum": ["all", "current_chat", "current_question"],
                "description": "Für Antworten auf eine Recherche-Rückfrage: current_question (nur offene Recherchefragen dieses Chats). Für Ergebnisbezüge: current_chat. Für alle Gespräche: all."}
        return {"type": "function", "name": self.name,
                "description": f"Liest den Zustand einer früheren {noun}. Bei Rückfragen immer lesen, niemals neu anlegen. Bei mehreren Treffern nachfragen.",
                "parameters": {"type": "object", "properties": properties, "additionalProperties": False}}

    async def run(self, arguments):
        properties = self.schema()["parameters"]["properties"]
        if (not isinstance(arguments, dict) or set(arguments) - set(properties)
                or any(not isinstance(v, str) or len(v) > 12000 for v in arguments.values())):
            return ToolResult(False, error="invalid_status_arguments")
        if 'scope' in arguments and arguments['scope'] not in {'all', 'current_chat', 'current_question'}:
            return ToolResult(False, error="invalid_status_arguments")
        if arguments.get('scope') in {'current_chat', 'current_question'}:
            return await self._current_chat(arguments)
        # `handle` enforces the same registered public operation contract.
        result = await self.control.handle({"op": self.name,
            **{k: v for k, v in arguments.items() if k != 'scope'}})
        return ToolResult(bool(result.get("ok")), data=result,
                          error=None if result.get("ok") else "status_unavailable")

    async def _current_chat(self, arguments):
        from solvio.capabilities.policy import OriginClass
        from solvio.browser_voice_session import VerifiedBrowserTaskSession
        from solvio.voice_task_session import VerifiedAppTaskSession
        from solvio.agent_runtime import inquiry
        refused = ToolResult(False, error='chat_status_unavailable',
            human_message='Der Auftragsstatus dieses Chats ist gerade nicht verlässlich lesbar. Es wurde nichts gestartet.')
        dispatcher = self.control.dispatcher
        gate = getattr(dispatcher, 'capability_gate', None)
        context = gate.context() if gate else None
        runtime = getattr(dispatcher, 'agent_runtime', None)
        store = getattr(runtime, 'conversations', None)
        if context is None or store is None or not context.conversation_id:
            return refused
        if context.origin is OriginClass.TRUSTED_DASHBOARD and type(context.browser_task_session) is VerifiedBrowserTaskSession:
            proof = context.browser_task_session
        elif context.origin is OriginClass.TRUSTED_INTERACTIVE_APP and type(context.app_task_session) is VerifiedAppTaskSession:
            proof = context.app_task_session
        else:
            return refused
        ledger = runtime.ledger
        async def current():
            return (await proof.current()
                and gate.context() is context and dispatcher.agent_runtime is runtime
                and runtime.conversations is store and runtime.ledger is ledger
                and proof.principal == context.principal and proof.session_id == context.session_id
                and store.conversation_owned(context.conversation_id, context.principal))
        try:
            if not await current():
                return refused
            def read():
                links = store.task_links(context.conversation_id)
                if arguments.get('scope') == 'current_question':
                    from solvio.agent_runtime import research_question as Q, store as S
                    def pending(link):
                        run = ledger.get_run(link['run_id'])
                        task = ledger.get_task(run.task_id) if run else None
                        return (task and task.task_id == link['task_id']
                            and task.state == S.TASK_ACTIVE and task.scope == S.SCOPE_RESEARCH
                            and task.created_principal == context.principal
                            and task.conversation_ref == context.conversation_id
                            and Q.view(ledger, run.run_id) is not None)
                    links = [link for link in links if pending(link)]
                return inquiry.find_linked(ledger, links,
                    principal=context.principal, text=arguments.get('text', ''), key=arguments.get('key', ''))
            result = await asyncio.to_thread(read)
            if not await current():
                return refused
        except Exception:
            return refused
        return ToolResult(True, data=result)


def attach(control):
    for name in ("note_status", "agent_task_status"):
        control.dispatcher.register(CoreStatusTool(control, name))
