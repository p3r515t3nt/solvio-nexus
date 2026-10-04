"""NodeRouter - deterministische Capability-Wahl (PHASE 9-11).

Waehlt einen geeigneten Knoten fuer eine Capability. KEINE KI-Entscheidung -
rein deterministisch und nachvollziehbar.

Filterreihenfolge (fail-closed):
  1. Knoten aktiviert und bietet die Capability an
  2. Protokoll kompatibel
  3. Verfuegbarkeitsklasse (falls gefordert)
  4. PRIVACY: darf die Datenklasse ueberhaupt zu diesem Knoten? (PHASE 11)
     -> wenn Privacy der einzige Blocker ist: PrivacyRoutingError
  5. Health/Circuit: OFFLINE/INCOMPATIBLE/Circuit-open werden uebersprungen
Sortierung: statische Route (PHASE 10) -> bevorzugte Platzierung -> priority.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from solvio.nodes.errors import NoSuitableNodeError, PrivacyRoutingError
from solvio.nodes.models import (
    PROTOCOL_VERSION,
    AvailabilityClass,
    DataClass,
    NodeDescriptor,
    NodeHealthState,
    NodePlacement,
    zone_permits,
)
from solvio.nodes.registry import NodeRegistry

# Statische Routing-Tabelle (PHASE 10). Jetzt nur die zwei Foundation-Caps.
# research.* / agent.* / embedding.* sind bewusst NUR dokumentiert, nicht aktiv.
STATIC_ROUTES: dict[str, list[str]] = {
    "system.health": ["hetzner-main"],
    "compute.sha256": ["hetzner-main"],
    "research.fetch_public_url": ["hetzner-main"],
}

# Zustaende, in denen ein Knoten fuer die Auswahl in Frage kommt.
_ELIGIBLE_STATES = (
    NodeHealthState.HEALTHY,
    NodeHealthState.DEGRADED,
    NodeHealthState.UNKNOWN,  # noch nie geprueft -> Versuch erlaubt
)


@dataclass(frozen=True)
class RoutingDecision:
    node: NodeDescriptor
    capability_id: str
    data_class: DataClass
    reason: str


class NodeRouter:
    def __init__(
        self,
        registry: NodeRegistry,
        *,
        health_provider: Callable[[str], NodeHealthState] | None = None,
        circuit_provider: Callable[[str], bool] | None = None,
    ) -> None:
        self.registry = registry
        self._health = health_provider or registry.health_state
        self._circuit_ok = circuit_provider or (lambda node_id: True)

    def select(
        self,
        capability_id: str,
        *,
        data_class: DataClass,
        required_availability: AvailabilityClass | None = None,
        preferred_placement: NodePlacement | None = None,
    ) -> NodeDescriptor:
        return self.decide(
            capability_id,
            data_class=data_class,
            required_availability=required_availability,
            preferred_placement=preferred_placement,
        ).node

    def decide(
        self,
        capability_id: str,
        *,
        data_class: DataClass,
        required_availability: AvailabilityClass | None = None,
        preferred_placement: NodePlacement | None = None,
    ) -> RoutingDecision:
        # 1. Capability angeboten?
        candidates = [d for d in self.registry.enabled()
                      if capability_id in d.expected_capabilities]
        if not candidates:
            raise NoSuitableNodeError(f"no node offers capability '{capability_id}'")

        # 2. Protokoll kompatibel
        candidates = [d for d in candidates if d.protocol_version == PROTOCOL_VERSION]
        if not candidates:
            raise NoSuitableNodeError("no protocol-compatible node")

        # 3. Verfuegbarkeitsklasse
        if required_availability is not None:
            candidates = [d for d in candidates if d.availability_class == required_availability]
            if not candidates:
                raise NoSuitableNodeError("no node meets availability requirement")

        # 4. PRIVACY (PHASE 11) - vor Health, damit eine Verletzung als solche gemeldet wird.
        privacy_ok = [d for d in candidates if zone_permits(data_class, d.privacy_zone)]
        if not privacy_ok:
            raise PrivacyRoutingError(
                f"data class '{data_class.value}' not permitted on any capable node"
            )

        # 5. Health / Circuit
        healthy = [d for d in privacy_ok
                   if self._circuit_ok(d.node_id) and self._health(d.node_id) in _ELIGIBLE_STATES]
        if not healthy:
            raise NoSuitableNodeError("no healthy node available")

        healthy.sort(key=lambda d: (
            self._route_rank(capability_id, d.node_id),
            0 if (preferred_placement is not None and d.placement is preferred_placement) else 1,
            d.priority,
            d.node_id,
        ))
        chosen = healthy[0]
        return RoutingDecision(chosen, capability_id, data_class, "selected")

    @staticmethod
    def _route_rank(capability_id: str, node_id: str) -> int:
        order = STATIC_ROUTES.get(capability_id)
        if order and node_id in order:
            return order.index(node_id)
        return len(order) if order else 1_000
