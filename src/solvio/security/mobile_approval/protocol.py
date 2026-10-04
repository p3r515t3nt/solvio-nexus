"""SOLVIO Mobile Approval Protocol V2 — shared payload contract (STEP S2A / S2A.1-P0).

Wire objects are `{payload_b64, signature_b64, key_id}`: the receiver base64-decodes
`payload`, verifies the signature over those EXACT bytes, and only then parses. Parsing
is STRICT: duplicate JSON keys are rejected, the top level must be an object, unknown
fields are rejected (fail-closed), and every authorization-relevant field the human sees
is inside the signed payload.

The Mac signs an ApprovalChallenge; the iPhone (Secure Enclave, after Face ID) signs an
ApprovalDecision. `action_digest` is the S1 canonical action digest — the decision binds
the exact stored action; the Mac executes only that action, never iPhone-supplied args.

V2 (S2A.1-P0, closes audit finding F1 "human display integrity"):
- The challenge now carries the EXACT execution-determining `task` (and `device_id`), so
  the approver can be shown what actually runs. In V1 `task` was NOT signed and reached
  the phone only over the unsigned list endpoint, which made "approve A / execute B"
  possible with fully valid signatures (reproduced on real hardware).
- The decision now carries `challenge_payload_sha256` — SHA-256 over the EXACT signed
  challenge bytes the phone verified and displayed. This binds the decision to the precise
  display context, not merely to the action.
- `human_summary` remains an UNTRUSTED, model-authored convenience hint. It is signed for
  integrity-in-transit only; it is NOT an authorization input and never substitutes `task`.
- V1 payloads are rejected outright (`unsupported protocol_version`) — no silent downgrade.

DISPLAY-SAFETY POLICY: fields rendered to the human (`task`, `human_summary`, `workspace`,
`tool_id`, `mode`) must survive `validate_display_text` — see that function. Enforced at
request creation so a poisoned request can never be stored, and re-checked when a challenge
payload is validated.
"""
from __future__ import annotations

import base64
import json

PROTOCOL_VERSION = 2

TYPE_CHALLENGE = "approval_challenge"
TYPE_DECISION = "approval_decision"
TYPE_ENROLLMENT = "enrollment_request"

DECISION_APPROVE = "APPROVE"
DECISION_DENY = "DENY"

# ---- display-safety limits (authorization text shown to a human) ----------
MAX_TASK_LEN = 8192
MAX_SUMMARY_LEN = 2048
MAX_WORKSPACE_LEN = 4096
MAX_SHORT_FIELD_LEN = 128  # tool_id / mode

# Bidi controls that can visually reorder text and spoof an approval screen, plus the
# zero-width no-break space when used as content. Legitimate international text (incl.
# ZWJ/ZWNJ needed by Indic/Arabic/emoji sequences) is deliberately NOT restricted.
_BIDI_SPOOFERS = frozenset(
    "؜"              # ARABIC LETTER MARK
    "‎‏"        # LEFT-TO-RIGHT / RIGHT-TO-LEFT MARK
    "‪‫‬‭‮"   # LRE RLE PDF LRO RLO (embeddings/overrides)
    "⁦⁧⁨⁩"         # LRI RLI FSI PDI (isolates)
    "﻿"              # ZERO WIDTH NO-BREAK SPACE used as content
)
# Only TAB and LF are legitimate structure in an approval text; everything else in C0/C1
# (incl. CR, NUL and DEL) is rejected — it cannot help a human and can spoof a display.
_ALLOWED_CONTROLS = frozenset("\t\n")


class ProtocolError(Exception):
    """Malformed / non-conforming protocol payload -> fail closed."""


def validate_display_text(value: str, *, field: str, max_len: int) -> None:
    """Fail-closed check for text that will be shown to a human as authorization basis.

    Rejects: NUL, all C0 controls except TAB/LF (CR included), DEL and C1 controls, and
    Unicode bidi overrides/isolates/marks that can visually reorder the approval screen.
    Does NOT restrict ordinary international text, combining marks, ZWJ/ZWNJ or emoji.
    """
    if not isinstance(value, str):
        raise ProtocolError(f"field {field} has wrong type")
    if len(value) > max_len:
        raise ProtocolError(f"field {field} too long ({len(value)} > {max_len})")
    for ch in value:
        o = ord(ch)
        if ch in _ALLOWED_CONTROLS:
            continue
        if o < 0x20 or 0x7F <= o <= 0x9F:
            raise ProtocolError(f"field {field} contains control character U+{o:04X}")
        if ch in _BIDI_SPOOFERS:
            raise ProtocolError(f"field {field} contains bidi control U+{o:04X}")


def validate_action_display_fields(*, tool_id: str, mode: str, task: str, workspace: str,
                                   human_summary: str) -> None:
    """Apply the display-safety policy to every field an approver is shown."""
    validate_display_text(tool_id, field="tool_id", max_len=MAX_SHORT_FIELD_LEN)
    validate_display_text(mode, field="mode", max_len=MAX_SHORT_FIELD_LEN)
    validate_display_text(workspace, field="workspace", max_len=MAX_WORKSPACE_LEN)
    validate_display_text(task, field="task", max_len=MAX_TASK_LEN)
    validate_display_text(human_summary, field="human_summary", max_len=MAX_SUMMARY_LEN)


def canonical_bytes(payload: dict) -> bytes:
    """Deterministic UTF-8 JSON (sort_keys, compact). Used by the SENDER to produce the
    exact bytes it signs; the receiver verifies over the received bytes, not a re-encode."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def b64d(s: str) -> bytes:
    try:
        return base64.b64decode(s.encode("ascii"), validate=True)
    except Exception as exc:  # noqa: BLE001
        raise ProtocolError(f"invalid base64: {exc}") from None


def _no_duplicate_keys(pairs):
    seen = {}
    for k, v in pairs:
        if k in seen:
            raise ProtocolError(f"duplicate JSON key: {k!r}")
        seen[k] = v
    return seen


def strict_parse(raw: bytes) -> dict:
    """Parse EXACT payload bytes. Rejects duplicate keys and non-object roots."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProtocolError(f"payload not utf-8: {exc}") from None
    try:
        obj = json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except ProtocolError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ProtocolError(f"invalid json: {exc}") from None
    if not isinstance(obj, dict):
        raise ProtocolError("payload root is not an object")
    return obj


def _require(payload: dict, fields: dict) -> None:
    """Exact field set + type check. Unknown or missing fields -> fail closed."""
    extra = set(payload) - set(fields)
    if extra:
        raise ProtocolError(f"unknown fields: {sorted(extra)}")
    for name, typ in fields.items():
        if name not in payload:
            raise ProtocolError(f"missing field: {name}")
        if not isinstance(payload[name], typ):
            raise ProtocolError(f"field {name} has wrong type")


_CHALLENGE_FIELDS = {
    "protocol_version": int, "type": str, "core_instance_id": str, "approval_id": str,
    "action_digest": str, "principal_id": str, "device_id": str, "tool_id": str,
    "mode": str, "workspace": str, "task": str, "human_summary": str,
    "challenge_nonce": str, "issued_at": (int, float), "expires_at": (int, float),
}

_DECISION_FIELDS = {
    "protocol_version": int, "type": str, "core_instance_id": str, "approval_id": str,
    "action_digest": str, "principal_id": str, "device_id": str, "key_id": str,
    "challenge_nonce": str, "challenge_payload_sha256": str, "decision": str,
    "issued_at": (int, float), "challenge_expires_at": (int, float),
}


def build_challenge_payload(*, core_instance_id, approval_id, action_digest, principal_id,
                            device_id, tool_id, mode, workspace, task, human_summary,
                            challenge_nonce, issued_at, expires_at) -> dict:
    """V2. `task` is the EXACT stored task the action_digest was computed over — it is the
    authoritative thing the human approves. `human_summary` is an untrusted hint."""
    return {
        "protocol_version": PROTOCOL_VERSION, "type": TYPE_CHALLENGE,
        "core_instance_id": core_instance_id, "approval_id": approval_id,
        "action_digest": action_digest, "principal_id": principal_id,
        "device_id": device_id, "tool_id": tool_id, "mode": mode,
        "workspace": workspace, "task": task, "human_summary": human_summary,
        "challenge_nonce": challenge_nonce, "issued_at": issued_at,
        "expires_at": expires_at,
    }


def validate_challenge_payload(payload: dict) -> None:
    _require(payload, _CHALLENGE_FIELDS)
    if payload["protocol_version"] != PROTOCOL_VERSION:
        raise ProtocolError("unsupported protocol_version")
    if payload["type"] != TYPE_CHALLENGE:
        raise ProtocolError("not an approval_challenge")
    # Re-assert the display-safety policy on the receiving side (defense in depth).
    validate_action_display_fields(
        tool_id=payload["tool_id"], mode=payload["mode"], task=payload["task"],
        workspace=payload["workspace"], human_summary=payload["human_summary"])


def build_decision_payload(*, core_instance_id, approval_id, action_digest, principal_id,
                           device_id, key_id, challenge_nonce, challenge_payload_sha256,
                           decision, issued_at, challenge_expires_at) -> dict:
    """V2. `challenge_payload_sha256` binds this decision to the EXACT signed challenge
    bytes the phone verified and displayed — proving consent to that display context."""
    if decision not in (DECISION_APPROVE, DECISION_DENY):
        raise ProtocolError("decision must be APPROVE or DENY")
    return {
        "protocol_version": PROTOCOL_VERSION, "type": TYPE_DECISION,
        "core_instance_id": core_instance_id, "approval_id": approval_id,
        "action_digest": action_digest, "principal_id": principal_id,
        "device_id": device_id, "key_id": key_id, "challenge_nonce": challenge_nonce,
        "challenge_payload_sha256": challenge_payload_sha256, "decision": decision,
        "issued_at": issued_at, "challenge_expires_at": challenge_expires_at,
    }


def validate_decision_payload(payload: dict) -> None:
    _require(payload, _DECISION_FIELDS)
    if payload["protocol_version"] != PROTOCOL_VERSION:
        raise ProtocolError("unsupported protocol_version")
    if payload["type"] != TYPE_DECISION:
        raise ProtocolError("not an approval_decision")
    if payload["decision"] not in (DECISION_APPROVE, DECISION_DENY):
        raise ProtocolError("invalid decision value")
