"""Node identity.

`node_id` is a stable, human-meaningful name (e.g. "hetzner-main"), NOT the IP.
`node_instance_id` is fresh per process start (useful for tracing restarts).

Identity is descriptive only — it confers NO authority. The Core decides trust and
actions; the node merely names itself in responses/logs.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{1,62}$")


@dataclass(frozen=True)
class NodeIdentity:
    node_id: str
    node_instance_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self) -> None:
        if not _ID_RE.match(self.node_id):
            raise ValueError(
                "node_id must be lowercase [a-z0-9-], 2..63 chars, e.g. 'hetzner-main'")
