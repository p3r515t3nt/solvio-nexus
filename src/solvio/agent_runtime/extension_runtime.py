"""Canonical assembly of the task-bound document extension path."""
from pathlib import Path
import os

from solvio.agent_runtime import store as S
from solvio.agent_runtime.extension_development import ExtensionDevelopment, seed_repository
from solvio.agent_runtime.extension_activation import ExtensionActivation
from solvio.agent_runtime.workspace import WorkspaceManager
from solvio.autopilot.publisher import CheckpointPublisher
from solvio.capabilities.document_adapter import SPEC, TaskDocumentService


def configured_file_runtime():
    """Bind an explicitly configured existing runtime; never install packages.

    Changing this directory changes the measured environment and invalidates
    prior tool tests. An absent setting keeps the file executor unavailable.
    """
    prefix = os.environ.get("SOLVIO_FILE_TOOL_RUNTIME", "")
    if not prefix:
        return None
    from solvio.agent_runtime.file_tool_process import bind_runtime
    root = Path(prefix).expanduser().resolve(strict=True)
    libraries = tuple((root / "lib").glob("python3.*/site-packages"))
    if len(libraries) != 1:
        raise ValueError("file_runtime_layout_changed")
    return bind_runtime(python=str(root / "bin" / libraries[0].parent.name),
                        prefix=str(root), site_packages=str(libraries[0]))


def attach_document_runtime(orchestrator, *, directory: str = "", file_runtime=None) -> None:
    if orchestrator.development is None or orchestrator.router is None:
        raise ValueError("document_development_dependencies_missing")
    root = Path(directory or str(Path(S.state_dir()) / "document-adapter-store")).resolve()
    if file_runtime is None:
        try:
            file_runtime = configured_file_runtime()
        except (ValueError, OSError) as exc:
            from solvio.logging_setup import get_logger
            get_logger("agent_runtime").warning("agent_runtime.file_runtime_unavailable",
                                               kind=type(exc).__name__)
    repository = seed_repository(str(root))
    publisher = CheckpointPublisher(repository)
    development = ExtensionDevelopment(orchestrator.ledger, orchestrator.development,
        WorkspaceManager(allowed=(repository,)), publisher,
        quote_adapter=orchestrator.cost_quote_adapter,
        settlement_adapter=orchestrator.cost_settlement_adapter, file_runtime=file_runtime)
    activation = ExtensionActivation(orchestrator.ledger,
        development=orchestrator.development, publisher=publisher, file_runtime=file_runtime)
    orchestrator.extension_development = development
    orchestrator.extension_activation = activation
    orchestrator.router.register(SPEC, TaskDocumentService(orchestrator.ledger, activation))
    if file_runtime is not None:
        from solvio.capabilities.file_adapter import SPEC as FILE_SPEC, TaskFileService
        orchestrator.router.register(FILE_SPEC, TaskFileService(orchestrator.ledger, activation))
