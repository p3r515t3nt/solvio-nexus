"""Node runtime configuration.

Loaded from a JSON file (path via SOLVIO_NODE_CONFIG, default
/etc/solvio-node/config.json). Secrets do NOT belong here — TLS material lives in
separate key/cert files referenced by path, never inlined.

Bind address defaults to loopback. Set the private WireGuard address explicitly
for remote use; keep the API off the public interface.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_CONFIG_PATH = "/etc/solvio-node/config.json"


class Limits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_request_bytes: int = 65_536        # 64 KiB request body cap
    max_concurrency: int = 8               # global in-flight cap
    max_queue: int = 32                    # bounded wait queue
    request_timeout_s: float = 10.0        # per-request wall clock
    rate_limit_per_min: int = 600          # coarse global rate limit


class JobConfig(BaseModel):
    """Durable background jobs. Disabled by default; opt-in per node."""
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    db_path: str = "/var/lib/solvio-node/jobs.sqlite3"
    workers: int = 2                       # bounded concurrent executions
    max_queued: int = 100                  # bounded non-terminal jobs
    result_ttl_s: float = 72 * 3600        # result payload TTL (72h)
    metadata_ttl_s: float = 7 * 86400      # job metadata TTL (7 days)


class TLSPaths(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ca_cert: str                            # SOLVIO CA (verifies client certs)
    server_cert: str
    server_key: str


class NodeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: str = "hetzner-main"
    listen_host: str = "127.0.0.1"          # Set the private WireGuard address for remote use
    listen_port: int = 8443
    tls: TLSPaths
    limits: Limits = Field(default_factory=Limits)
    jobs: JobConfig = Field(default_factory=JobConfig)
    log_path: str = "/var/lib/solvio-node/logs/node.jsonl"
    enabled_capabilities: list[str] = Field(
        default_factory=lambda: ["system.health", "compute.sha256"])

    @classmethod
    def load(cls, path: str | None = None) -> "NodeConfig":
        p = Path(path or os.environ.get("SOLVIO_NODE_CONFIG", DEFAULT_CONFIG_PATH))
        data = json.loads(p.read_text(encoding="utf-8"))
        return cls.model_validate(data)
