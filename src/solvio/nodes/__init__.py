"""SOLVIO Core Node-Layer (STEP 21.2E) - optional, compute-only.

MAC CORE = AUTHORITY. NODE = COMPUTE ONLY. Ein Knoten liefert nur Ergebnisse;
ob und wie sie genutzt werden, welchen Trust-Level sie erhalten und ob daraus
eine Aktion folgt, entscheidet ALLEIN der Core. Node-Identitaet verleiht niemals
Autoritaet.

Dieses Paket wird in STEP 21.2E NICHT automatisch beim Core-Start geladen und
NICHT von Voice/Realtime eingebunden (PHASE 23/24). Der reine Import hat keine
Seiteneffekte: keine Verbindung, kein Cert-Zugriff, kein Netzwerk. Client und
Health-Monitor entstehen erst bei tatsaechlicher Nutzung.
"""
from __future__ import annotations

from solvio.nodes.client import NodeClient, RawResponse
from solvio.nodes.config import (
    NodeConnectionConfig,
    NodesConfig,
    TLSClientPaths,
    load_nodes_config,
    parse_nodes_config,
    resolve_config_path,
)
from solvio.nodes.errors import (
    CircuitOpenError,
    NoSuitableNodeError,
    NodeBusyError,
    NodeCapabilityError,
    NodeConfigError,
    NodeError,
    NodeIdentityError,
    NodeProtocolError,
    NodeRequestMismatchError,
    NodeTimeoutError,
    NodeTLSError,
    NodeUnavailableError,
    PrivacyRoutingError,
    UnknownCapabilityError,
)
from solvio.nodes.health import (
    HealthSnapshot,
    NodeHealthMonitor,
    classify_error,
    classify_report,
)
from solvio.nodes.models import (
    PROTOCOL_VERSION,
    AvailabilityClass,
    CapabilityDescriptor,
    DataClass,
    HealthReport,
    NodeDescriptor,
    NodeHealthState,
    NodePlacement,
    NodeRequest,
    NodeResponse,
    PrivacyZone,
    ResponseStatus,
    SystemHealth,
    zone_permits,
)
from solvio.nodes.registry import ManagedNode, NodeRegistry, build_registry
from solvio.nodes.resilience import Backoff, CircuitBreaker, CircuitState
from solvio.nodes.router import STATIC_ROUTES, NodeRouter, RoutingDecision
from solvio.nodes.trust_map import node_result_trust, result_bears_authority

__all__ = [
    # client
    "NodeClient", "RawResponse",
    # config
    "NodeConnectionConfig", "NodesConfig", "TLSClientPaths",
    "load_nodes_config", "parse_nodes_config", "resolve_config_path",
    # errors
    "NodeError", "NodeConfigError", "NodeUnavailableError", "NodeTLSError",
    "NodeTimeoutError", "CircuitOpenError", "NodeProtocolError", "NodeIdentityError",
    "NodeRequestMismatchError", "NodeCapabilityError", "UnknownCapabilityError",
    "NodeBusyError", "PrivacyRoutingError", "NoSuitableNodeError",
    # health
    "HealthSnapshot", "NodeHealthMonitor", "classify_report", "classify_error",
    # models
    "PROTOCOL_VERSION", "AvailabilityClass", "CapabilityDescriptor", "DataClass",
    "HealthReport", "NodeDescriptor", "NodeHealthState", "NodePlacement",
    "NodeRequest", "NodeResponse", "PrivacyZone", "ResponseStatus", "SystemHealth",
    "zone_permits",
    # registry
    "ManagedNode", "NodeRegistry", "build_registry",
    # resilience
    "Backoff", "CircuitBreaker", "CircuitState",
    # router
    "STATIC_ROUTES", "NodeRouter", "RoutingDecision",
    # trust
    "node_result_trust", "result_bears_authority",
]
