"""The existing Router's typed mediator for one bound offline file tool."""
from __future__ import annotations

from functools import cache

from solvio.capabilities.contract import CapabilityRefused, CapabilitySpec, ExecutionClass, ExecutorUnavailable
from solvio.security.mobile_approval.execution import READ_ONLY
from solvio.tools.base import RiskLevel


@cache
def _spec():
    from solvio.agent_runtime import file_inputs as FI
    return CapabilitySpec(name=FI.CAPABILITY, version=FI.VERSION,
        execution_class=ExecutionClass.CONTROLLED, base_risk=RiskLevel.HARMLESS,
        semantics=READ_ONLY, executor="file_tool", timeout=90.0, cancellable=True,
        input_schema={"type":"object", "additionalProperties":False,
            "required":["contract_digest","resource_id","source_sha256","source_bytes"],
            "properties":{"contract_digest":{"type":"string"},"resource_id":{"type":"string"},
                "source_sha256":{"type":"string"},
                "source_bytes":{"type":"integer","minimum":1,"maximum":FI.MAX_MANIFEST_BYTES}}},
        description="Verarbeitet nur die unveraenderlich beigefuegten Auftragsdateien offline "
                    "mit einem gebundenen, geprueften Werkzeug. Kein Zugriff auf fremde Dateien oder Dienste.")


def __getattr__(name):
    if name == "SPEC":
        return _spec()
    raise AttributeError(name)


class TaskFileService:
    def __init__(self, ledger, activation):
        from solvio.agent_runtime.extension_activation import ExtensionActivation
        if type(activation) is not ExtensionActivation or activation.ledger is not ledger:
            raise ValueError("file_activation_binding_required")
        self.ledger, self.activation = ledger, activation

    def __call__(self, arguments):
        raise CapabilityRefused("file_task_contract_required")

    def resources(self, spec, arguments, task_step):
        from solvio.agent_runtime import table_report_contract as TC, extension_families as EF
        from solvio.agent_runtime.task_start_service import TaskStepAuthority
        if (spec != _spec() or type(task_step) is not TaskStepAuthority
                or self.activation.ledger is not self.ledger):
            raise CapabilityRefused("file_task_contract_required")
        bound = TC.for_run(self.ledger, task_step.run_id)
        if (bound is None or bound.task_id != task_step.task_id
                or bound.grant_reference != task_step.reference or bound.arguments != arguments):
            raise CapabilityRefused("file_resource_binding_changed")
        selected = self.activation.selected(task_step.run_id)
        if selected is None:
            raise ExecutorUnavailable("file_adapter_not_ready")
        artifact_id, call = selected
        from solvio.agent_runtime.file_tool_process import FileToolInvocation
        if type(call) is not FileToolInvocation or call.runtime != self.activation.file_runtime:
            raise CapabilityRefused("file_runtime_binding_changed")
        return {"contract":bound.contract_digest,"input":bound.arguments,
            "artifact":artifact_id,"files":dict(call.files),"entrypoint":call.entrypoint,
            "environment":EF.environment_fingerprint(TC.FORMAT, file_runtime=self.activation.file_runtime)}

    def quote(self, service, invocation):
        from solvio.agent_runtime import file_inputs as FI
        from solvio.agent_runtime.cost_dispatch import CostQuote
        from solvio.agent_runtime.costs import CostEvidence
        if (service != "local.file-work" or invocation.capability != FI.CAPABILITY
                or invocation.version != FI.VERSION or invocation.operation != "table_report"):
            raise CapabilityRefused("file_cost_contract_changed")
        return CostQuote(0, CostEvidence("free_local", "core:offline-table-report-v1"))

    async def execute(self, arguments, task_step):
        from solvio.agent_runtime import file_inputs as FI, table_report_contract as TC, file_tool_process as FP
        from solvio.agent_runtime.cost_dispatch import ServiceOutcome
        from solvio.agent_runtime.file_results import VerifiedFileOutput
        receipt = "core:file:" + task_step.step_id
        try:
            resources = self.resources(_spec(), arguments, task_step)
            source = TC.input_payload(FI.read_for_run(self.ledger, task_step.run_id, arguments=arguments))
            selected = self.activation.selected(task_step.run_id)
            if selected is None or selected[0] != resources["artifact"]:
                raise ValueError("file_adapter_changed")
        except (ValueError, OSError, CapabilityRefused, ExecutorUnavailable):
            return ServiceOutcome("not_dispatched", reason="file_binding_changed", receipt_ref=receipt+":not-started")
        _, call = selected
        outcome = await FP.run_file_tool(call, source)
        if outcome.execution_status == "not_started":
            return ServiceOutcome("not_dispatched", reason=outcome.reason, receipt_ref=receipt+":not-started")
        if outcome.execution_status != "terminal":
            return ServiceOutcome("unknown", reason="file_process_unknown")
        if not outcome.ok:
            return ServiceOutcome("completed", reason="file_tool_failed", receipt_ref=receipt+":terminal")
        try:
            if self.resources(_spec(), arguments, task_step) != resources:
                raise ValueError("file_binding_changed")
        except (ValueError, OSError, CapabilityRefused, ExecutorUnavailable):
            return ServiceOutcome("unknown", reason="file_binding_changed")
        try:
            checked = await TC.validate_output(call, source, outcome.stdout)
        except (ValueError, OSError) as exc:
            if str(exc) == "table_readback_unavailable":
                return ServiceOutcome("unknown", reason="file_readback_unknown")
            return ServiceOutcome("completed", reason="file_result_invalid", receipt_ref=receipt+":terminal")
        try:
            if self.resources(_spec(), arguments, task_step) != resources:
                raise ValueError("file_binding_changed")
            data = VerifiedFileOutput.from_checked(task_step, resources, checked)
        except (ValueError, OSError, CapabilityRefused, ExecutorUnavailable):
            return ServiceOutcome("unknown", reason="file_binding_changed")
        # Bytes remain transient internal data. Publication is the Orchestrator's
        # responsibility after the original service-cost claim has settled.
        return ServiceOutcome("completed", ok=True, receipt_ref=receipt+":terminal", data=data)


_TASK_FILE_SERVICE_METHODS = {
    name:getattr(TaskFileService,name) for name in ("resources","quote","execute")}
