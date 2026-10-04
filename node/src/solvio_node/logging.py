"""Structured JSON logging with hard payload redaction.

The logger's API only accepts a FIXED set of safe fields. There is deliberately no
way to pass request/result CONTENT through it — only sizes and metadata. This makes
payload leakage into logs structurally impossible, not merely a convention.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# All metadata — never content. adds job-metadata keys (job_id/status/
# error_code/duration_ms/payload_size/result_size/attempt); none carry payload text.
_ALLOWED_EVENT_KEYS = {
    "kind", "detail", "count", "peer", "port", "capability",
    "job_id", "status", "error_code", "duration_ms", "payload_size",
    "result_size", "attempt",
}


class NodeLogger:
    def __init__(self, log_path: str, node_id: str, *, to_stderr: bool = True) -> None:
        self.node_id = node_id
        self._log = logging.getLogger("solvio_node")
        self._log.setLevel(logging.INFO)
        self._log.handlers.clear()
        self._log.propagate = False
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(message)s"))
        self._log.addHandler(fh)
        if to_stderr:
            sh = logging.StreamHandler()
            sh.setFormatter(logging.Formatter("%(message)s"))
            self._log.addHandler(sh)

    def _emit(self, record: dict[str, Any]) -> None:
        record = {"ts": datetime.now(timezone.utc).isoformat(),
                  "node_id": self.node_id, **record}
        self._log.info(json.dumps(record, separators=(",", ":"), ensure_ascii=False))

    def request(self, *, request_id: str, capability: str, status: str,
                duration_ms: float, payload_size: int, result_size: int,
                error_code: str | None = None, peer: str | None = None) -> None:
        """Log ONE completed request. No payload/result CONTENT — only sizes."""
        self._emit({
            "type": "request", "request_id": request_id, "capability": capability,
            "status": status, "duration_ms": round(duration_ms, 3),
            "payload_size": payload_size, "result_size": result_size,
            "error_code": error_code, "peer": peer,
        })

    def event(self, kind: str, **safe: Any) -> None:
        """Lifecycle/ops event. Only whitelisted, non-content keys are accepted."""
        bad = set(safe) - (_ALLOWED_EVENT_KEYS - {"kind"})
        if bad:
            raise ValueError(f"disallowed log keys (possible content leak): {sorted(bad)}")
        self._emit({"type": "event", "kind": kind, **safe})


def build_logger(log_path: str, node_id: str) -> NodeLogger:
    return NodeLogger(log_path, node_id,
                      to_stderr=os.environ.get("SOLVIO_NODE_STDERR", "1") != "0")
