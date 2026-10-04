"""Datenmodelle des Core-Node-Layers (STEP 21.2E).

Zwei Gruppen:
  1. Wire-Modelle  (NodeRequest/NodeResponse/CapabilityDescriptor/HealthReport)
     spiegeln das solvio-node Protokoll V1 (Client-Seite). Tolerant gegenueber
     zusaetzlichen Feldern des Knotens (extra="ignore"), damit der Node sich
     abwaertskompatibel erweitern kann.
  2. Core-Modelle  (NodeDescriptor + Enums) beschreiben, was der Core ueber
     einen Knoten WEISS und ENTSCHEIDET. IP ist NIEMALS Identitaet (PHASE 27):
     Identitaet = erwartete node_id / Zertifikats-Identitaet.
"""
from __future__ import annotations

import uuid
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

PROTOCOL_VERSION = 1


# --------------------------------------------------------------------------
# Wire-Modelle (Protokoll V1)
# --------------------------------------------------------------------------
class ResponseStatus(str, Enum):
    OK = "ok"
    ERROR = "error"


class NodeRequest(BaseModel):
    """Was der Client an den Knoten sendet. Nur benannte Capabilities."""
    model_config = ConfigDict(extra="forbid")

    protocol_version: int = PROTOCOL_VERSION
    request_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    target_node_id: str | None = None
    capability: str | None = None
    operation: str | None = None
    timestamp: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class NodeResponse(BaseModel):
    """Antwort des Knotens. `error_message` ist per Protokoll payload-frei."""
    model_config = ConfigDict(extra="ignore")

    protocol_version: int
    request_id: str | None = None
    node_id: str | None = None
    capability: str | None = None
    status: ResponseStatus
    result: dict[str, Any] | None = None
    duration_ms: float | None = None
    error_code: str | None = None
    error_message: str | None = None


class CapabilityDescriptor(BaseModel):
    """Selbstbeschreibung einer Node-Capability (aus /v1/capabilities)."""
    model_config = ConfigDict(extra="ignore")

    id: str
    version: str | None = None
    description: str | None = None
    risk_level: str | None = None
    privacy_class: str | None = None
    execution_mode: str | None = None
    supports_batch: bool = False
    max_concurrency: int | None = None
    timeout_s: float | None = None
    persistent_data: bool = False
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None


class SystemHealth(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: str
    uptime_s: float | None = None
    load1: float | None = None
    ram_available_mb: int | None = None
    protocol_version: int | None = None


class HealthReport(BaseModel):
    """Antwort von /v1/health."""
    model_config = ConfigDict(extra="ignore")

    node_id: str
    node_instance_id: str | None = None
    protocol_version: int
    version: str | None = None
    status: str
    capabilities: list[str] = Field(default_factory=list)
    system: SystemHealth | None = None


# --------------------------------------------------------------------------
# Core-Modelle
# --------------------------------------------------------------------------
class NodeHealthState(str, Enum):
    UNKNOWN = "unknown"          # noch nie geprueft
    HEALTHY = "healthy"          # status ok, Protokoll kompatibel
    DEGRADED = "degraded"        # erreichbar, aber status != ok
    OFFLINE = "offline"          # nicht erreichbar (Netz/WG/Timeout)
    INCOMPATIBLE = "incompatible"  # Protokollversion passt nicht


class NodePlacement(str, Enum):
    LOCAL_HOST = "local_host"
    HOME_LAN = "home_lan"
    REMOTE_DATACENTER = "remote_datacenter"
    THIRD_PARTY_CLOUD = "third_party_cloud"


class AvailabilityClass(str, Enum):
    LOCAL_ALWAYS = "local_always"          # der Core-Host selbst
    LAN_INTERMITTENT = "lan_intermittent"  # z. B. Windows-Workstation
    ALWAYS_ON_REMOTE = "always_on_remote"  # 24/7 Remote-Worker (Hetzner)


class PrivacyZone(str, Enum):
    """Vertrauenszone eines KNOTENS - wie weit duerfen Daten dorthin reisen.

    Aufsteigende Reichweite. Ein Knoten deklariert seine Zone; der Router
    laesst Daten nur zu, wenn deren Datenklasse mindestens diese Reichweite
    erlaubt (PHASE 11).
    """
    HOME = "home"                            # nur der lokale Host
    LAN = "lan"                              # Heimnetz
    REMOTE_CONTROLLED = "remote_controlled"  # eigener kontrollierter Remote-Node
    THIRD_PARTY_CLOUD = "third_party_cloud"  # fremde Cloud (Policy noetig)


class DataClass(str, Enum):
    """Wie weit DIESE Daten reisen duerfen (aufsteigend, PHASE 11).

    Default fuer alles Sensible ist HOME_ONLY (fail-closed): private Memory-/
    Audio-Daten gehen NICHT nach remote, nur weil ein Knoten verfuegbar ist.
    """
    HOME_ONLY = "home_only"
    LAN_ALLOWED = "lan_allowed"
    REMOTE_CONTROLLED_ALLOWED = "remote_controlled_allowed"
    CLOUD_THIRD_PARTY_POLICY_REQUIRED = "cloud_third_party_policy_required"


_ZONE_RANK: dict[PrivacyZone, int] = {
    PrivacyZone.HOME: 0,
    PrivacyZone.LAN: 1,
    PrivacyZone.REMOTE_CONTROLLED: 2,
    PrivacyZone.THIRD_PARTY_CLOUD: 3,
}

_DATACLASS_MAX_RANK: dict[DataClass, int] = {
    DataClass.HOME_ONLY: 0,
    DataClass.LAN_ALLOWED: 1,
    DataClass.REMOTE_CONTROLLED_ALLOWED: 2,
    DataClass.CLOUD_THIRD_PARTY_POLICY_REQUIRED: 3,
}


def zone_permits(data_class: DataClass, zone: PrivacyZone) -> bool:
    """Duerfen Daten der Klasse `data_class` auf einem Knoten der Zone `zone`
    verarbeitet werden? (PHASE 11) Fail-closed: unbekannt -> False."""
    if data_class not in _DATACLASS_MAX_RANK or zone not in _ZONE_RANK:
        return False
    return _ZONE_RANK[zone] <= _DATACLASS_MAX_RANK[data_class]


class NodeDescriptor(BaseModel):
    """Core-seitiges Wissen ueber einen Knoten (nur Konfiguration, keine Secrets).

    IP ist NICHT Identitaet: `identity` (erwartete Zertifikats-/node_id) ist die
    Vertrauensbindung; `endpoint` ist nur eine Netzwerkadresse.
    """
    model_config = ConfigDict(extra="forbid")

    node_id: str
    display_name: str
    endpoint: str
    transport: str = "wireguard+mtls"
    protocol_version: int = PROTOCOL_VERSION
    expected_capabilities: list[str] = Field(default_factory=list)
    availability_class: AvailabilityClass
    placement: NodePlacement
    privacy_zone: PrivacyZone
    priority: int = 100  # kleiner = hoehere Prioritaet (PRIMARY vor SECONDARY)
    enabled: bool = True
    expected_identity: str | None = None  # Cert-DNS-SAN / CN; default = node_id
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def identity(self) -> str:
        return self.expected_identity or self.node_id
