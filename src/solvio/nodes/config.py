"""Konfiguration des Node-Layers (STEP 21.2E, PHASE 17).

Enthaelt NIEMALS Schluesselinhalte - nur PFADE zu Zertifikaten/Schluesseln.
Keine Secrets in Git. Die Core-`.env` wird nicht angefasst.

Quelle der Registry-Konfiguration (in dieser Reihenfolge):
  1. Umgebungsvariable SOLVIO_NODES_CONFIG (Pfad zu einer JSON-Datei)
  2. <repo>/config/nodes.json         (real, gitignored)
  3. nichts -> leere Registry (der Core startet trotzdem, PHASE 23)

`config/nodes.example.json` zeigt die Struktur und wird eingecheckt.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from solvio.config import PROJECT_ROOT
from solvio.nodes.models import (
    AvailabilityClass,
    NodeDescriptor,
    NodePlacement,
    PrivacyZone,
)

DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "nodes.json"
ENV_CONFIG_PATH = "SOLVIO_NODES_CONFIG"


@dataclass(frozen=True)
class TLSClientPaths:
    """Nur Pfade. Der Inhalt bleibt ausserhalb des Repos und ausserhalb von Git."""
    ca_cert: str
    client_cert: str
    client_key: str

    def missing(self) -> list[str]:
        """Welche referenzierten Dateien fehlen? (Fuer lazy/optionalen Start.)"""
        return [p for p in (self.ca_cert, self.client_cert, self.client_key)
                if not Path(p).expanduser().is_file()]


@dataclass(frozen=True)
class NodeConnectionConfig:
    """Transport-Parameter fuer genau einen Knoten."""
    node_id: str
    endpoint: str
    tls: TLSClientPaths
    identity: str
    protocol_version: int = 1
    connect_timeout_s: float = 5.0
    request_timeout_s: float = 10.0
    pool_limit: int = 8
    keepalive_timeout_s: float = 30.0


@dataclass
class NodesConfig:
    """Ergebnis des Ladens: Deskriptoren + zugehoerige Verbindungskonfig."""
    descriptors: list[NodeDescriptor] = field(default_factory=list)
    connections: dict[str, NodeConnectionConfig] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.descriptors


def _descriptor_from(entry: dict) -> NodeDescriptor:
    return NodeDescriptor(
        node_id=entry["node_id"],
        display_name=entry.get("display_name", entry["node_id"]),
        endpoint=entry["endpoint"],
        transport=entry.get("transport", "wireguard+mtls"),
        protocol_version=int(entry.get("protocol_version", 1)),
        expected_capabilities=list(entry.get("expected_capabilities", [])),
        availability_class=AvailabilityClass(entry["availability_class"]),
        placement=NodePlacement(entry["placement"]),
        privacy_zone=PrivacyZone(entry["privacy_zone"]),
        priority=int(entry.get("priority", 100)),
        enabled=bool(entry.get("enabled", True)),
        expected_identity=entry.get("expected_identity"),
        metadata=dict(entry.get("metadata", {})),
    )


def _connection_from(entry: dict, descriptor: NodeDescriptor) -> NodeConnectionConfig:
    tls = entry["tls"]
    return NodeConnectionConfig(
        node_id=descriptor.node_id,
        endpoint=descriptor.endpoint,
        tls=TLSClientPaths(
            ca_cert=tls["ca_cert"],
            client_cert=tls["client_cert"],
            client_key=tls["client_key"],
        ),
        identity=descriptor.identity,
        protocol_version=descriptor.protocol_version,
        connect_timeout_s=float(entry.get("connect_timeout_s", 5.0)),
        request_timeout_s=float(entry.get("request_timeout_s", 10.0)),
        pool_limit=int(entry.get("pool_limit", 8)),
        keepalive_timeout_s=float(entry.get("keepalive_timeout_s", 30.0)),
    )


def parse_nodes_config(data: dict) -> NodesConfig:
    """Wandelt geparste JSON-Daten in eine NodesConfig. Rein, testbar."""
    cfg = NodesConfig()
    for entry in data.get("nodes", []):
        descriptor = _descriptor_from(entry)
        cfg.descriptors.append(descriptor)
        if "tls" in entry:
            cfg.connections[descriptor.node_id] = _connection_from(entry, descriptor)
    return cfg


def resolve_config_path(explicit: str | os.PathLike | None = None) -> Path | None:
    """Ermittelt den Pfad der Registry-Konfig, ohne ihn zu lesen."""
    if explicit:
        return Path(explicit)
    env = os.environ.get(ENV_CONFIG_PATH)
    if env:
        return Path(env)
    if DEFAULT_CONFIG_PATH.is_file():
        return DEFAULT_CONFIG_PATH
    return None


def load_nodes_config(explicit: str | os.PathLike | None = None) -> NodesConfig:
    """Laedt die Registry-Konfig. Fehlt sie, ergibt sich eine leere Registry
    (der Core startet trotzdem, PHASE 23)."""
    path = resolve_config_path(explicit)
    if path is None or not Path(path).is_file():
        return NodesConfig()
    with open(path, "r", encoding="utf-8") as fh:
        return parse_nodes_config(json.load(fh))
