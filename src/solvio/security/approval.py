"""Approval-Broker fuer riskante, ausfuehrende Aktionen (z.B. Codex MODIFY).

Kernprinzip (Hard Gate): SOLVIO / das Sprachmodell darf eine Aktion ANFORDERN,
aber NIEMALS selbst freigeben oder ausfuehren. Die Freigabe kommt ausschliesslich
ueber einen vertrauenswuerdigen, modell-unabhaengigen Kanal (Approver). Ohne
konfigurierten Approver ist der Broker FAIL-CLOSED.

Security-Eigenschaften (S1.1):
- Authorization-Digest bindet ALLE autorisierungsrelevanten Inputs ueber eine
  KANONISCHE, eindeutig parsebare Darstellung (versioniertes UTF-8-JSON, sort_keys,
  kompakte Separatoren) mit Domain-Separation -> keine Delimiter-Mehrdeutigkeit.
- Trust ist NICHT caller-asserted: nur ein injizierter, vertrauenswuerdiger Approver
  (Control-Plane) kann freigeben. Kein Realtime/Dispatcher/LLM-Pfad erzeugt Trust.
- request_id ist IDENTIFIER, kein Secret / keine Autoritaet: ohne Approved-State
  (vom Trusted Approver gesetzt) fuehrt kein bekannter request_id etwas aus.
- Principal-Bindung: der anfordernde Principal stammt aus dem Control-/Session-
  Kontext (nie ein frei waehlbarer LLM-Tool-Parameter).
- Bounded: hoechstens `max_pending` offene Anforderungen; am Limit fail-closed OHNE
  Verdraengung; identische offene Aktion desselben Principals wird wiederverwendet;
  TTL-Cleanup.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

_DOMAIN = b"SOLVIO_APPROVAL_V1"
_MAX_PENDING = 32
DIGEST_VERSION = 1


def action_digest(*, tool_id: str, mode: str, task: str, workspace: str) -> str:
    """Kanonischer, domain-separierter Authorization-Digest.

    Kanonische Darstellung: versioniertes UTF-8-JSON mit sort_keys + kompakten
    Separatoren (keine Whitespace-/Key-Order-Abhaengigkeit). Sonderzeichen in
    Feldern (| : Newline Quotes Unicode) werden JSON-escaped -> keine Feldgrenzen-
    Mehrdeutigkeit (Boundary-Swap ausgeschlossen). Domain-Separation verhindert
    versehentliche Wiederverwendung des Hashes fuer einen anderen Zweck.
    """
    payload = {"v": DIGEST_VERSION, "tool_id": tool_id, "mode": mode,
               "task": task, "workspace": workspace}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(_DOMAIN + b"\x00" + canonical).hexdigest()


@dataclass
class ApprovalRequest:
    request_id: str
    principal: str
    tool: str
    task: str
    workspace: str
    mode: str
    digest: str
    created: float
    expiry: float
    approved: bool = False
    approved_by: str | None = None


class ApprovalLimitError(Exception):
    """Zu viele offene Anforderungen -> fail-closed, KEINE Verdraengung."""


@runtime_checkable
class Approver(Protocol):
    """Ein vertrauenswuerdiger, modell-unabhaengiger Freigabe-Kanal (Control-Plane)."""

    def is_trusted(self, request: "ApprovalRequest", identity: str) -> bool:
        ...


class ApprovalBroker:
    def __init__(self, approver: Approver | None = None, ttl: float = 120.0,
                 max_pending: int = _MAX_PENDING) -> None:
        self._approver = approver
        self.ttl = ttl
        self.max_pending = max_pending
        self._pending: dict[str, ApprovalRequest] = {}

    def _gc(self) -> None:
        now = time.monotonic()
        for rid in [rid for rid, r in self._pending.items() if now > r.expiry]:
            self._pending.pop(rid, None)

    # -- request (indirekt vom Tool; KEIN Token/Autoritaet zurueck ans Modell) --
    def request(self, *, principal: str, tool: str, task: str, workspace: str,
                mode: str) -> ApprovalRequest:
        self._gc()
        digest = action_digest(tool_id=tool, mode=mode, task=task, workspace=workspace)
        # Dedup: identische, noch offene Aktion desselben Principals wiederverwenden.
        for r in self._pending.values():
            if r.principal == principal and r.digest == digest and not r.approved:
                return r
        if len(self._pending) >= self.max_pending:
            # fail-closed: kein Angreifer kann durch Spam bestehende Requests verdraengen.
            raise ApprovalLimitError("too_many_pending_approvals")
        now = time.monotonic()
        r = ApprovalRequest(
            request_id=secrets.token_hex(16), principal=principal, tool=tool,
            task=task, workspace=workspace, mode=mode, digest=digest,
            created=now, expiry=now + self.ttl)
        self._pending[r.request_id] = r
        return r

    def pending_count(self) -> int:
        self._gc()
        return len(self._pending)

    def has_pending(self) -> bool:
        self._gc()
        return any(not r.approved for r in self._pending.values())

    def list_pending(self) -> list[dict]:
        """Control-Plane-Sicht fuer einen Approver-Kanal. request_id ist ein
        Identifier (keine Autoritaet); der Digest ist die zu bestaetigende Aktion."""
        self._gc()
        return [{"request_id": r.request_id, "principal": r.principal, "tool": r.tool,
                 "task": r.task, "workspace": r.workspace, "mode": r.mode,
                 "digest": r.digest, "approved": r.approved}
                for r in self._pending.values()]

    # -- approve (NUR vom vertrauenswuerdigen Approver; Trust nie caller-asserted) --
    def approve(self, *, request_id: str, identity: str, presented_digest: str):
        self._gc()
        r = self._pending.get((request_id or ""))
        if r is None:
            return None, "unknown"
        if time.monotonic() > r.expiry:
            self._pending.pop(r.request_id, None)
            return None, "expired"
        # Trust kommt AUSSCHLIESSLICH aus dem injizierten Approver (Control-Plane),
        # nie aus caller-gelieferten Strings/Booleans. Ohne Approver: fail-closed.
        if self._approver is None or not self._approver.is_trusted(r, identity):
            return None, "no_trusted_approver"
        if not secrets.compare_digest(r.digest, (presented_digest or "")):
            return None, "digest_mismatch"
        r.approved = True
        r.approved_by = identity
        return r, "ok"

    # -- take approved (vom Core-Executor; NICHT modell-erreichbar) -----------
    def take_approved(self, *, request_id: str, tool: str | None = None):
        """Gibt eine FREIGEGEBENE Anforderung einmalig heraus. request_id allein
        genuegt NICHT: ohne Approved-State (vom Trusted Approver) -> fail-closed."""
        self._gc()
        r = self._pending.get((request_id or ""))
        if r is None:
            return None, "unknown"
        if not r.approved:
            return None, "not_approved"
        if time.monotonic() > r.expiry:
            self._pending.pop(r.request_id, None)
            return None, "expired"
        if tool is not None and r.tool != tool:
            return None, "mismatch"
        self._pending.pop(r.request_id, None)  # einmalig
        return r, "ok"

    def cancel(self, request_id: str | None = None) -> None:
        if request_id:
            self._pending.pop(request_id, None)
        else:
            self._pending.clear()

    def clear(self) -> None:
        self._pending.clear()
