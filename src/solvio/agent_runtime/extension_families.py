"""Two explicit Core contracts, sharing development, activation and versioning.

This is not a plugin loader. Model output cannot register a family, an import
path, a runtime or a capability. Document grant bytes retain their old shape.
The table tool has a narrower implementation contract than its immutable
offline-file grant; both are independently bound in activation manifests.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from solvio.agent_runtime import document_contract as DC, extension_process as EP


@dataclass(frozen=True)
class FamilyProfile:
    family: str
    format: str
    contract: str
    contract_digest: str
    capability: str
    version: int
    resource: str
    max_input_bytes: int


def _table(file_runtime):
    from solvio.agent_runtime.file_tool_process import FileToolRuntime
    if type(file_runtime) is not FileToolRuntime:
        raise ValueError("file_runtime_required")
    from solvio.agent_runtime import table_report_contract as TC
    return TC


def _document(selected):
    return FamilyProfile("document_extract_text", selected.format, selected.contract,
        selected.contract_digest, DC.CAPABILITY, DC.VERSION, DC.RESOURCE, selected.max_input_bytes)


def profile_for_digest(digest, *, file_runtime=None):
    try:
        return _document(DC._profile_for_digest(digest))
    except ValueError:
        pass
    TC = _table(file_runtime)
    if digest != TC.CONTRACT_DIGEST:
        raise ValueError("extension_contract_unknown")
    selected = TC.profile()
    return FamilyProfile("table_report", selected.format, selected.contract, selected.contract_digest,
        selected.capability, selected.version, selected.resource, selected.max_input_bytes)


def bound_for_run(ledger, run_id, *, file_runtime=None):
    document = DC.for_run(ledger, run_id)
    if document is not None:
        return document
    if file_runtime is None:
        return None
    return _table(file_runtime).for_run(ledger, run_id)


def invocation(bound, root, entrypoint, files, *, file_runtime=None):
    selected = profile_for_digest(bound.contract_digest, file_runtime=file_runtime)
    if selected.family == "document_extract_text":
        return EP.ExtensionInvocation(str(root), entrypoint, files,
                                      max_input_bytes=bound.max_input_bytes)
    return _table(file_runtime).invocation(str(root), entrypoint, files, file_runtime)


def environment_fingerprint(input_format, *, file_runtime=None):
    from solvio.agent_runtime import extension_activation as EA, extension_versions as EV
    if input_format in {"rtf", "txt", "docx", "odt"}:
        return EA.environment_fingerprint(input_format)
    TC = _table(file_runtime)
    if input_format != TC.FORMAT:
        raise ValueError("extension_contract_unknown")
    body = {"family": TC.environment_fingerprint(file_runtime),
        "activation": hashlib.sha256(Path(EA.__file__).read_bytes()).hexdigest(),
        "versions": hashlib.sha256(Path(EV.__file__).read_bytes()).hexdigest(),
        "families": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def gate(activation, invocation, bound):
    return await activation._gate_for(invocation, bound.format)


async def file_gate(invocation, input_format, *, file_runtime):
    TC = _table(file_runtime)
    if input_format != TC.FORMAT:
        raise ValueError("extension_contract_unknown")
    return await TC.gate(invocation)
