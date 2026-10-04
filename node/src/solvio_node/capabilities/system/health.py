"""system.health — bounded, safe self-report.

Returns ONLY coarse operational health: process uptime, status, 1-min load, and
available RAM. It deliberately exposes NO secrets, environment variables, user
lists, SSH config, or filesystem paths. The /v1/health endpoint enriches the
response with the node id and the registered capability ids.
"""
from __future__ import annotations

import os
import time

from pydantic import BaseModel

from solvio_node import PROTOCOL_VERSION
from solvio_node.capabilities.base import (
    Capability,
    ExecutionMode,
    PrivacyClass,
    RiskLevel,
)


class HealthIn(BaseModel):
    pass


class HealthOut(BaseModel):
    status: str
    uptime_s: float
    load1: float | None = None
    ram_available_mb: int | None = None
    protocol_version: int = PROTOCOL_VERSION


def _ram_available_mb() -> int | None:
    try:  # Linux
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        return None
    return None


def _load1() -> float | None:
    try:
        return round(os.getloadavg()[0], 2)  # Unix only
    except (OSError, AttributeError):
        return None


class HealthCapability(Capability):
    id = "system.health"
    version = "1"
    description = "Bounded node health: uptime, status, load, available RAM."
    risk_level = RiskLevel.SAFE
    privacy_class = PrivacyClass.NONE
    execution_mode = ExecutionMode.INLINE
    supports_batch = False
    max_concurrency = 4
    timeout_s = 2.0
    persistent_data = False
    input_model = HealthIn
    output_model = HealthOut

    def __init__(self) -> None:
        self._t0 = time.monotonic()

    async def invoke(self, data: BaseModel) -> HealthOut:
        return HealthOut(
            status="ok",
            uptime_s=round(time.monotonic() - self._t0, 1),
            load1=_load1(),
            ram_available_mb=_ram_available_mb(),
        )
