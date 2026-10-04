"""Core mediator for one authenticated, immutable offline document input."""
from __future__ import annotations

import hashlib
from functools import cache
from typing import TYPE_CHECKING

from solvio.capabilities.contract import (
    AmbiguousExecution, CapabilityRefused, CapabilitySpec, ExecutionClass, ExecutorUnavailable,
)
from solvio.security.mobile_approval.execution import READ_ONLY
from solvio.tools.base import RiskLevel

if TYPE_CHECKING:
    from solvio.agent_runtime.extension_activation import ExtensionActivation


@cache
def _spec():
    # The router can identify this service with the optional runtime absent.
    # Only attaching/using the document capability loads its canonical contract.
    from solvio.agent_runtime import document_contract as DC
    return CapabilitySpec(name=DC.CAPABILITY, version=DC.VERSION,
        execution_class=ExecutionClass.CONTROLLED, base_risk=RiskLevel.HARMLESS,
        semantics=READ_ONLY, executor="document", timeout=20.0, cancellable=True,
        input_schema={"type": "object", "additionalProperties": False,
            "required": ["contract_digest", "resource_id", "source_sha256", "source_bytes"],
            "properties": {"contract_digest": {"type": "string"},
                "resource_id": {"type": "string"}, "source_sha256": {"type": "string"},
                "source_bytes": {"type": "integer", "minimum": 1, "maximum": DC.MAX_INPUT_BYTES}}},
        description="Liest ausschliesslich das diesem Auftrag beigefuegte RTF-, TXT-, DOCX- oder ODT-Dokument offline. "
                    "Die Quelle bleibt unveraendert; das Ergebnis ist Dokumentinhalt.")


def __getattr__(name):
    if name == "SPEC":
        return _spec()
    raise AttributeError(name)


class TaskDocumentService:
    def __init__(self, ledger, activation: ExtensionActivation):
        from solvio.agent_runtime.extension_activation import ExtensionActivation
        if type(activation) is not ExtensionActivation or activation.ledger is not ledger:
            raise ValueError("document_activation_binding_required")
        self.ledger, self.activation = ledger, activation

    def __call__(self, arguments):
        raise CapabilityRefused("document_task_contract_required")

    def resources(self, spec, arguments, task_step):
        from solvio.agent_runtime import document_contract as DC
        from solvio.agent_runtime.extension_activation import environment_fingerprint
        from solvio.agent_runtime.task_start_service import TaskStepAuthority
        if (spec != _spec() or type(task_step) is not TaskStepAuthority
                or self.activation.ledger is not self.ledger):
            raise CapabilityRefused("document_task_contract_required")
        bound = DC.for_run(self.ledger, task_step.run_id)
        if (bound is None or bound.task_id != task_step.task_id
                or bound.grant_reference != task_step.reference
                or bound.arguments != arguments):
            raise CapabilityRefused("document_resource_binding_changed")
        selected = self.activation.selected(task_step.run_id)
        if selected is None:
            raise ExecutorUnavailable("document_adapter_not_ready")
        artifact_id, invocation = selected
        return {"contract": bound.contract_digest, "input": bound.arguments,
                "artifact": artifact_id, "files": dict(invocation.files),
                "entrypoint": invocation.entrypoint, "environment": environment_fingerprint(bound.format)}

    def quote(self, service, invocation):
        from solvio.agent_runtime import document_contract as DC
        from solvio.agent_runtime.cost_dispatch import CostQuote
        from solvio.agent_runtime.costs import CostEvidence
        if (service != "local.document-extract" or invocation.capability != DC.CAPABILITY
                or invocation.version != DC.VERSION or invocation.operation != "extract_text"):
            raise CapabilityRefused("document_cost_contract_changed")
        return CostQuote(0, CostEvidence("free_local", "core:offline-rtf-v1"))

    async def execute(self, arguments, task_step):
        from solvio.agent_runtime import document_contract as DC, extension_process as EP
        from solvio.agent_runtime.cost_dispatch import ServiceOutcome
        # Stage evidence comes from this Core adapter, never from model data.
        receipt = "core:document:" + task_step.step_id
        try:
            resources = self.resources(_spec(), arguments, task_step)
            source = DC.read_for_run(self.ledger, task_step.run_id, arguments=arguments)
            selected = self.activation.selected(task_step.run_id)
            if selected is None or selected[0] != resources["artifact"]:
                raise CapabilityRefused("document_adapter_changed")
        except (ValueError, OSError, CapabilityRefused, ExecutorUnavailable):
            return ServiceOutcome("not_dispatched", reason="document_binding_changed",
                                  receipt_ref=receipt + ":not-started")
        artifact_id, invocation = selected
        result = await EP.run_extension(invocation, source)
        if result.execution_status == "not_started":
            return ServiceOutcome("not_dispatched", reason=result.reason,
                                  receipt_ref=receipt + ":not-started")
        if result.execution_status != "terminal":
            return ServiceOutcome("unknown", reason="document_process_unknown")
        if not result.ok:
            return ServiceOutcome("completed", reason="document_conversion_failed",
                                  receipt_ref=receipt + ":terminal")
        try:
            text = result.stdout.decode("utf-8")
        except UnicodeDecodeError:
            return ServiceOutcome("completed", reason="document_output_not_utf8",
                                  receipt_ref=receipt + ":terminal")
        if len(result.stdout) > DC.MAX_OUTPUT_BYTES or "\x00" in text:
            return ServiceOutcome("completed", reason="document_output_invalid",
                                  receipt_ref=receipt + ":terminal")
        if not text.strip():
            return ServiceOutcome("completed", reason="document_has_no_text",
                                  receipt_ref=receipt + ":terminal")
        # No delivery/owner-success assertion. This receipt binds actual bytes
        # to the original source and measured implementation only.
        return ServiceOutcome("completed", ok=True, receipt_ref=receipt + ":terminal", data={
            "text": text, "source_sha256": arguments["source_sha256"],
            "output_sha256": hashlib.sha256(result.stdout).hexdigest(),
            "output_bytes": len(result.stdout), "implementation_ref": artifact_id,
            "contract_digest": resources["contract"], "content_trust": "untrusted_document",
            "execution_status": result.execution_status})


_TASK_DOCUMENT_SERVICE_METHODS = {
    name: getattr(TaskDocumentService, name) for name in ("resources", "quote", "execute")}
