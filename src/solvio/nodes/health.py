"""Node-Health: Zustaende, Cache und Monitor (PHASE 13).

Nicht jede Voice-Anfrage soll erst einen frischen Health-Request ausloesen:
der Monitor haelt einen Snapshot mit Zeitstempel und TTL vor. Der Circuit
Breaker (PHASE 14) wird aus Health- und Invoke-Ergebnissen gespeist.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from solvio.nodes.errors import NodeProtocolError
from solvio.nodes.models import HealthReport, NodeHealthState
from solvio.nodes.resilience import CircuitBreaker, CircuitState


@dataclass
class HealthSnapshot:
    state: NodeHealthState
    checked_at: float
    detail: str = ""


def classify_report(report: HealthReport, expected_protocol: int) -> tuple[NodeHealthState, str]:
    """Leitet aus einem Health-Report einen Zustand ab."""
    if report.protocol_version != expected_protocol:
        return (NodeHealthState.INCOMPATIBLE,
                f"protocol {report.protocol_version} != {expected_protocol}")
    if (report.status or "").lower() == "ok":
        return NodeHealthState.HEALTHY, "ok"
    return NodeHealthState.DEGRADED, f"status={report.status}"


def classify_error(err: Exception) -> tuple[NodeHealthState, str]:
    """Ein Protokollfehler bedeutet INCOMPATIBLE, sonst OFFLINE."""
    if isinstance(err, NodeProtocolError):
        return NodeHealthState.INCOMPATIBLE, "protocol error"
    return NodeHealthState.OFFLINE, "unreachable"


class NodeHealthMonitor:
    """Kapselt Health-Cache + Circuit Breaker fuer einen Knoten."""

    def __init__(
        self,
        node_id: str,
        client,
        *,
        expected_protocol: int = 1,
        ttl_s: float = 15.0,
        breaker: CircuitBreaker | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.node_id = node_id
        self.client = client
        self.expected_protocol = expected_protocol
        self.ttl_s = ttl_s
        self.clock = clock
        self.breaker = breaker or CircuitBreaker(clock=clock)
        self._snapshot = HealthSnapshot(NodeHealthState.UNKNOWN, 0.0, "not checked")

    def cached_state(self) -> NodeHealthState:
        return self._snapshot.state

    @property
    def snapshot(self) -> HealthSnapshot:
        return self._snapshot

    def is_fresh(self) -> bool:
        if self._snapshot.state is NodeHealthState.UNKNOWN:
            return False
        return (self.clock() - self._snapshot.checked_at) < self.ttl_s

    def available(self) -> bool:
        """Routing-Eignung ohne Netz und ohne einen Half-Open-Versuch zu verbrauchen."""
        if self.breaker.state is CircuitState.OPEN:
            return False
        return self._snapshot.state not in (NodeHealthState.OFFLINE, NodeHealthState.INCOMPATIBLE)

    async def refresh(self) -> HealthSnapshot:
        try:
            report = await self.client.health()
        except Exception as err:  # noqa: BLE001 - jede Ausfallart -> Zustand + Breaker
            state, detail = classify_error(err)
            self._snapshot = HealthSnapshot(state, self.clock(), detail)
            self.breaker.record_failure()
            return self._snapshot
        state, detail = classify_report(report, self.expected_protocol)
        self._snapshot = HealthSnapshot(state, self.clock(), detail)
        if state in (NodeHealthState.HEALTHY, NodeHealthState.DEGRADED):
            self.breaker.record_success()
        else:
            self.breaker.record_failure()
        return self._snapshot

    async def state(self, *, force: bool = False) -> NodeHealthState:
        """Zustand; nutzt den Cache, solange er frisch ist."""
        if force or not self.is_fresh():
            await self.refresh()
        return self.cached_state()

    def record_invoke_success(self) -> None:
        self.breaker.record_success()

    def record_invoke_failure(self) -> None:
        self.breaker.record_failure()
