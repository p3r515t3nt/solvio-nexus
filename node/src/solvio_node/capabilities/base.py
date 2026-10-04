"""Capability contract.

A capability is the ONLY unit of work a node can perform. There is no generic
shell/exec path. Each capability declares a typed input/output schema; the runtime
validates input BEFORE calling `invoke` and validates output AFTER, so a capability
can never be driven with an unvalidated payload.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, ClassVar

from pydantic import BaseModel


class RiskLevel(str, Enum):
    SAFE = "safe"          # read-only, no side effects, no persistence
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class PrivacyClass(str, Enum):
    NONE = "none"          # touches no personal data
    SYNTHETIC = "synthetic"  # processes caller-supplied ephemeral data, not persisted
    SENSITIVE = "sensitive"  # would touch personal data — NOT permitted on a node


class ExecutionMode(str, Enum):
    INLINE = "inline"      # runs in-process, bounded, no external side effects


class Capability(ABC):
    id: ClassVar[str]
    version: ClassVar[str]
    description: ClassVar[str]
    risk_level: ClassVar[RiskLevel]
    privacy_class: ClassVar[PrivacyClass]
    execution_mode: ClassVar[ExecutionMode]
    supports_batch: ClassVar[bool] = False
    #: opt-in for durable background execution. Default False so no
    # existing capability becomes background-runnable implicitly.
    supports_background: ClassVar[bool] = False
    max_concurrency: ClassVar[int] = 4
    timeout_s: ClassVar[float] = 5.0
    persistent_data: ClassVar[bool] = False
    input_model: ClassVar[type[BaseModel]]
    output_model: ClassVar[type[BaseModel]]

    @abstractmethod
    async def invoke(self, data: BaseModel) -> BaseModel:
        """Run the capability on a validated input model; return an output model."""

    async def health(self) -> dict[str, Any]:
        return {"id": self.id, "ok": True}

    @classmethod
    def descriptor(cls) -> dict[str, Any]:
        return {
            "id": cls.id, "version": cls.version, "description": cls.description,
            "risk_level": cls.risk_level.value, "privacy_class": cls.privacy_class.value,
            "execution_mode": cls.execution_mode.value, "supports_batch": cls.supports_batch,
            "supports_background": cls.supports_background,
            "max_concurrency": cls.max_concurrency, "timeout_s": cls.timeout_s,
            "persistent_data": cls.persistent_data,
            "input_schema": cls.input_model.model_json_schema(),
            "output_schema": cls.output_model.model_json_schema(),
        }
