"""Sprachwerkzeuge fuer Suche und native Analyse von Gmail-Anhaengen."""
from __future__ import annotations

from typing import Any

from solvio.capabilities.documents import SPECS, WARNING
from solvio.capabilities.envelope import CapabilityResult
from solvio.logging_setup import get_logger
from solvio.tools.base import RiskLevel, ToolResult

log = get_logger("tools")

_SCHEMAS: dict[str, dict[str, Any]] = {
    "document_find": {
        "description": "Findet den passenden Gmail-Anhang, etwa eine Rechnung von "
                       "einer Firma, ohne seinen Inhalt zu lesen. Für eine Inhaltsfrage "
                       "danach document_ask mit dem zurückgegebenen document_ref aufrufen; "
                       "bei mehreren Treffern zuerst nachfragen. " + WARNING,
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Gmail-Suchanfrage"},
            "limit": {"type": "integer"}}, "required": ["query"]}},
    "document_ask": {
        "description": "Liest einen zuvor eindeutig gefundenen PDF- oder Bildanhang "
                       "nativ und beantwortet eine Frage dazu oder fasst ihn zusammen. "
                       + WARNING,
        "parameters": {"type": "object", "properties": {
            "document_ref": {"type": "object", "additionalProperties": False,
                "description": "Verweis aus document_find; keine Kennungen erfinden.",
                "properties": {
                    "message_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "attachment_id": {"type": "string", "maxLength": 8192},
                    "filename": {"type": "string", "minLength": 1, "maxLength": 1024},
                    "mime_type": {"type": "string", "maxLength": 256},
                    "sender": {"type": "string", "maxLength": 6000},
                    "subject": {"type": "string", "maxLength": 6000},
                    "received_at": {"type": "string", "maxLength": 256},
                    "source": {"type": "string", "enum": ["gmail"]}},
                "required": ["message_id", "filename"]},
            "question": {"type": "string", "minLength": 1},
            "expected_content_type": {"type": "string"}},
            "required": ["document_ref", "question"]}},
}

DOCUMENT_READ_TOOLS = frozenset(_SCHEMAS)


def _message(data: Any) -> str:
    """Present the measured domain outcome, not only its trust warning."""
    if not isinstance(data, dict):
        return ""
    if data.get("status") == "ANSWERED" and isinstance(data.get("answer"), str):
        return data["answer"]
    if data.get("reason") == "gmail_authorization":
        return "Der Zugriff auf diesen Mailanhang ist nicht freigegeben. Bitte prüfe die Google-Verbindung und ihre Zugriffsrechte in SOLVIO."
    if data.get("human_message"):
        return str(data["human_message"])
    return {
        "DOCUMENT_FOUND": "Ich habe einen passenden Anhang gefunden. Sein Inhalt wurde noch nicht gelesen.",
        "DOCUMENT_NOT_FOUND": "Ich konnte den Anhang nicht laden. Bitte grenze die gemeinte Mail genauer ein.",
        "AMBIGUOUS": "Es gibt mehrere passende Anhänge. Welchen meinst du?",
        "UNSUPPORTED": "Diesen Anhang kann die vorhandene Dokumentanalyse nicht lesen. Möglich sind PDF und unterstützte Bilder.",
        "PARSE_FAILED": "Die Dokumentanalyse hat keine verlässliche Antwort geliefert. Es wurde nichts versendet oder verändert.",
    }.get(data.get("status"), "")


class DocumentCapabilityTool:
    risk_level = RiskLevel.HARMLESS
    expose_to_llm = True

    def __init__(self, capability: str, router: Any, gate: Any) -> None:
        self.name = capability
        self.capability = capability
        self.router = router
        self.gate = gate

    def schema(self) -> dict[str, Any]:
        entry = _SCHEMAS[self.capability]
        return {"type": "function", "name": self.name,
                "description": entry["description"], "parameters": entry["parameters"]}

    async def run(self, arguments: dict) -> ToolResult:
        args = dict(arguments or {})
        context = self.gate.context() if self.gate is not None else None
        if context is None or not context.has_principal:
            return ToolResult(False, data={"content_trust": "untrusted_document",
                                          "warning": WARNING},
                              error="no_trusted_context",
                              human_message="Ich kann gerade nicht sicher feststellen, wer fragt. " + WARNING)
        result: CapabilityResult = await self.router.execute(
            self.capability, args, trust=context.trust,
            provenance=self.gate.provenance_for(args), principal=context.principal,
            origin=context.origin, commanded=context.commanded)
        message = result.human_message or (_message(result.data) if result.succeeded else "")
        # Names and clarification messages are untrusted mail material too.
        # Scrub before the FIRST voice response, not only the later selector.
        from solvio.capabilities.task_read import task_material
        filtered = task_material(message)
        message = "\n".join(filtered) if isinstance(filtered, list) else str(filtered)
        if WARNING not in message:
            message = (message + " " + WARNING).strip()
        if result.succeeded:
            return ToolResult(True, data=result.data, human_message=message)
        return ToolResult(False, data=result.data, human_message=message,
                          error=f"{result.outcome.value}:{result.reason}"
                          if result.reason else result.outcome.value)


def document_capability_tools(router: Any, gate: Any) -> list[DocumentCapabilityTool]:
    return [DocumentCapabilityTool(name, router, gate) for name in sorted(SPECS)]
