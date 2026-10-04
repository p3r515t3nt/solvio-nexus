"""NodeRegistry - explizite, konfigurationsbasierte Knotenliste (PHASE 8).

Enthaelt NUR Konfiguration. Es gibt KEIN automatisches Netzwerk-Discovery:
Knoten werden ausschliesslich aus der Konfiguration registriert. Kein Scannen
des LAN, kein Zuvertrauen unbekannter Geraete.

Ein `ManagedNode` bindet einen Deskriptor lazy an Client + Health-Monitor.
Ohne Verbindungskonfiguration bleibt ein Knoten rein deskriptiv (z. B. in Tests
oder fuer noch nicht verbundene Zukunftsknoten).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from solvio.nodes.client import NodeClient
from solvio.nodes.config import NodeConnectionConfig, NodesConfig, load_nodes_config
from solvio.nodes.errors import NodeConfigError
from solvio.nodes.health import NodeHealthMonitor
from solvio.nodes.models import NodeDescriptor, NodeHealthState


@dataclass
class ManagedNode:
    descriptor: NodeDescriptor
    client: NodeClient | None = None
    monitor: NodeHealthMonitor | None = None


class NodeRegistry:
    def __init__(self, *, client_factory: Callable[[NodeConnectionConfig], NodeClient] | None = None) -> None:
        self._descriptors: dict[str, NodeDescriptor] = {}
        self._connections: dict[str, NodeConnectionConfig] = {}
        self._managed: dict[str, ManagedNode] = {}
        self._client_factory = client_factory or (lambda c: NodeClient(c))

    # -- Registrierung -------------------------------------------------------
    def register(self, descriptor: NodeDescriptor,
                 connection: NodeConnectionConfig | None = None) -> None:
        if descriptor.node_id in self._descriptors:
            raise ValueError(f"duplicate node_id: {descriptor.node_id}")
        self._descriptors[descriptor.node_id] = descriptor
        if connection is not None:
            self._connections[descriptor.node_id] = connection

    # -- Abfragen ------------------------------------------------------------
    def get(self, node_id: str) -> NodeDescriptor:
        try:
            return self._descriptors[node_id]
        except KeyError:
            raise NodeConfigError(f"unknown node_id: {node_id}") from None

    def all(self) -> list[NodeDescriptor]:
        return list(self._descriptors.values())

    def enabled(self) -> list[NodeDescriptor]:
        return [d for d in self._descriptors.values() if d.enabled]

    def node_ids(self) -> list[str]:
        return list(self._descriptors)

    def has_connection(self, node_id: str) -> bool:
        return node_id in self._connections

    # -- Managed (lazy Client + Monitor) ------------------------------------
    def managed(self, node_id: str) -> ManagedNode:
        existing = self._managed.get(node_id)
        if existing is not None:
            return existing
        descriptor = self.get(node_id)
        conn = self._connections.get(node_id)
        if conn is None:
            raise NodeConfigError(f"no connection config for node_id: {node_id}")
        client = self._client_factory(conn)
        monitor = NodeHealthMonitor(node_id, client, expected_protocol=descriptor.protocol_version)
        managed = ManagedNode(descriptor, client, monitor)
        self._managed[node_id] = managed
        return managed

    def health_state(self, node_id: str) -> NodeHealthState:
        """Zwischengespeicherter Zustand ohne Netz; UNKNOWN wenn nie geprueft."""
        managed = self._managed.get(node_id)
        if managed is None or managed.monitor is None:
            return NodeHealthState.UNKNOWN
        return managed.monitor.cached_state()

    async def aclose(self) -> None:
        for managed in self._managed.values():
            if managed.client is not None:
                await managed.client.aclose()

    # -- Konstruktion aus Konfiguration -------------------------------------
    @classmethod
    def from_config(cls, cfg: NodesConfig,
                    *, client_factory: Callable[[NodeConnectionConfig], NodeClient] | None = None
                    ) -> "NodeRegistry":
        reg = cls(client_factory=client_factory)
        for descriptor in cfg.descriptors:
            reg.register(descriptor, cfg.connections.get(descriptor.node_id))
        return reg


def build_registry(cfg: NodesConfig | None = None) -> NodeRegistry:
    """Baut die Registry aus der (optionalen) Datei-Konfiguration.

    Fehlt die Konfiguration, ergibt sich eine leere Registry - der Core startet
    trotzdem (PHASE 23)."""
    return NodeRegistry.from_config(cfg if cfg is not None else load_nodes_config())
