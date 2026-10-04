"""compute.sha256 — stateless proof of remote compute.

Hashes caller-supplied text and returns the digest. Nothing is persisted; the input
is processed in RAM only and never logged. The input length is capped both here
(schema) and by the global request-size limit.
"""
from __future__ import annotations

import hashlib

from pydantic import BaseModel, Field

from solvio_node.capabilities.base import (
    Capability,
    ExecutionMode,
    PrivacyClass,
    RiskLevel,
)

MAX_TEXT_LEN = 16_384


class Sha256In(BaseModel):
    text: str = Field(max_length=MAX_TEXT_LEN)


class Sha256Out(BaseModel):
    algorithm: str = "sha256"
    hex: str
    input_length: int


class Sha256Capability(Capability):
    id = "compute.sha256"
    version = "1"
    description = "SHA-256 of caller-supplied text (stateless, not persisted)."
    risk_level = RiskLevel.SAFE
    privacy_class = PrivacyClass.SYNTHETIC
    execution_mode = ExecutionMode.INLINE
    supports_batch = False
    max_concurrency = 8
    timeout_s = 2.0
    persistent_data = False
    input_model = Sha256In
    output_model = Sha256Out

    async def invoke(self, data: BaseModel) -> Sha256Out:
        assert isinstance(data, Sha256In)
        raw = data.text.encode("utf-8")
        return Sha256Out(hex=hashlib.sha256(raw).hexdigest(), input_length=len(raw))
