"""Pure primary GPT-Live WebSocket envelopes; no transport or task authority.

Contracts read 2026-09-11:
https://developers.openai.com/api/docs/guides/voice-websockets?api=live
https://developers.openai.com/api/docs/guides/live-conversations
https://developers.openai.com/api/docs/guides/live-delegation
https://developers.openai.com/api/reference/resources/live/fork-websocket
The last page supplies shared event types, not fresh-session start overrides.
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import math

LIVE_URL = "wss://api.openai.com/v1/live/sessions"
LIVE_MODEL = "gpt-live-1"
INPUT_RATE = 16000
OUTPUT_RATE = INPUT_RATE
# Deliberately conservative local byte bounds, not inferred API token counts.
MAX_APPEND_UTF8_BYTES = 500
MAX_INSTRUCTIONS_UTF8_BYTES = 16384
MAX_AUDIO_BYTES = 1024 * 1024
SUPPORTED_VOICES = frozenset({
    "alloy", "ash", "ballad", "beacon", "bossa", "cedar", "cinder", "coral",
    "delta", "echo", "gleam", "marin", "meridian", "quartz", "ripple", "sage",
    "shimmer", "stone", "tempo", "verse", "vesper", "willow",
})
_CLOSE_REASONS = frozenset({
    "close_requested", "expired", "content", "remote_hangup", "connection_lost",
})


class ProtocolError(ValueError):
    """Malformed supported envelope; messages never include its private content."""


def _text(value, *, field, max_bytes, allow_empty=False):
    if type(value) is not str:
        raise ProtocolError("invalid_" + field)
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        raise ProtocolError("invalid_" + field) from None
    if "\x00" in value or size > max_bytes or (not allow_empty and not value.strip()):
        raise ProtocolError("invalid_" + field)
    return value


def _identifier(value, field):
    result = _text(value, field=field, max_bytes=512)
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in result):
        raise ProtocolError("invalid_" + field)
    # IDs are opaque: no prefix parsing, normalization, or reconstruction.
    return result


def _number(value, field):
    if type(value) not in (int, float):
        raise ProtocolError("invalid_" + field)
    try:
        number = float(value)
    except (ValueError, OverflowError):
        raise ProtocolError("invalid_" + field) from None
    if not math.isfinite(number) or number < 0:
        raise ProtocolError("invalid_" + field)
    return number


def _event_type(event):
    if type(event) is not dict or type(event.get("type")) is not str:
        raise ProtocolError("invalid_event")
    return event["type"]


def _pcm(data):
    if type(data) is not bytes or len(data) % 2 or len(data) > MAX_AUDIO_BYTES:
        raise ProtocolError("invalid_pcm")
    return data


def session_start(voice: str, instructions: str) -> dict:
    """First JSON event; explicit client mode and no stored recording.

    Both directions are raw mono PCM16LE at 16 kHz. The caller must wait for
    session.started and check the resolved configuration before sending audio.
    """
    if type(voice) is not str or voice not in SUPPORTED_VOICES:
        raise ProtocolError("unsupported_voice")
    instructions = _text(instructions, field="instructions",
                         max_bytes=MAX_INSTRUCTIONS_UTF8_BYTES)
    return {"type": "session.start", "session": {
        "model": LIVE_MODEL, "instructions": instructions,
        "audio": {"format": {"type": "audio/pcm", "rate": INPUT_RATE},
                  "output": {"voice": voice}},
        "delegation": {"type": "client"}, "store": False,
    }}


def audio_append(pcm: bytes) -> dict:
    """Encode complete raw samples. Rate/container correctness belongs to capture."""
    return {"type": "session.input_audio.append",
            "audio": base64.b64encode(_pcm(pcm)).decode("ascii")}


def append_update(kind: str, content: str, delegation_id: str | None,
                  event_id: str) -> dict:
    """Serialize one instructions/thinking/commentary append, never a tool call.

    The API limit is 500 tokens. This module conservatively admits at most
    500 UTF-8 bytes, without estimating four characters per token or truncating.
    Context is not private: the caller must redact facts and preserve authority.
    """
    if type(kind) is not str or kind not in {"instructions", "thinking", "commentary"}:
        raise ProtocolError("invalid_append_kind")
    content = _text(content, field="content", max_bytes=MAX_APPEND_UTF8_BYTES)
    event_id = _identifier(event_id, "event_id")
    if delegation_id is not None:
        delegation_id = _identifier(delegation_id, "delegation_id")
    return {"type": "session." + kind + ".append", "content": content,
            "delegation_id": delegation_id, "event_id": event_id}


@dataclass(frozen=True, slots=True)
class TranscriptDelta:
    role: str
    delta: str
    start_ms: float
    end_ms: float
    event_id: str


def parse_transcript(event: dict) -> TranscriptDelta | None:
    """Preserve text exactly; intervals and fragments are not completed turns."""
    kind = _event_type(event)
    roles = {"session.input_transcript.delta": "user",
             "session.output_transcript.delta": "assistant"}
    if kind not in roles:
        return None
    delta = _text(event.get("delta"), field="transcript", max_bytes=65536, allow_empty=True)
    start = _number(event.get("start_ms"), "start_ms")
    end = _number(event.get("end_ms"), "end_ms")
    if end < start:
        raise ProtocolError("reversed_transcript_interval")
    return TranscriptDelta(roles[kind], delta, start, end,
                           _identifier(event.get("event_id"), "event_id"))


def decode_audio(event: dict) -> bytes | None:
    """Decode primary output only; sample rate is the configured INPUT_RATE.

    PCM validates alignment, not waveform semantics. This helper does not
    infer rate from bytes or turn optional sideband timestamps into playback.
    """
    if _event_type(event) != "session.output_audio.delta":
        return None
    delta = _text(event.get("delta"), field="audio_delta",
                  max_bytes=4 * ((MAX_AUDIO_BYTES + 2) // 3), allow_empty=True)
    try:
        audio = base64.b64decode(delta, validate=True)
    except (ValueError, binascii.Error):
        raise ProtocolError("invalid_audio_base64") from None
    return _pcm(audio)


@dataclass(frozen=True, slots=True)
class DelegationNotice:
    delegation_id: str
    offset_ms: float
    event_id: str


def parse_delegation(event: dict) -> DelegationNotice | None:
    """Client metadata only. The caller resolves intent and deduplicates effects."""
    if _event_type(event) != "session.delegation.created":
        return None
    item = event.get("delegation")
    if type(item) is not dict or item.get("type") != "delegation":
        raise ProtocolError("invalid_delegation")
    if item.get("target") != "client":
        raise ProtocolError("unexpected_delegation_target")
    return DelegationNotice(_identifier(item.get("id"), "delegation_id"),
                            _number(event.get("offset_ms"), "offset_ms"),
                            _identifier(event.get("event_id"), "event_id"))


@dataclass(frozen=True, slots=True)
class UsageSnapshot:
    seconds: float
    final: bool
    reason: str | None
    event_id: str
    session_id: str | None = None


def parse_usage(event: dict) -> UsageSnapshot | None:
    """Cumulative observed duration, never an increment or inferred zero.

    Only session.closed marks a final snapshot, including connection_lost.
    Its session.status remains active in the documented schema. The lifecycle
    owner must correlate session_id and retain uncertainty on transport loss.
    """
    kind = _event_type(event)
    if kind not in {"session.usage.updated", "session.closed"}:
        return None
    usage = event.get("usage")
    if type(usage) is not dict:
        raise ProtocolError("invalid_usage")
    seconds = _number(usage.get("seconds"), "usage_seconds")
    final = kind == "session.closed"
    reason, session_id = None, None
    if final:
        reason = event.get("reason")
        if type(reason) is not str or reason not in _CLOSE_REASONS:
            raise ProtocolError("invalid_close_reason")
        session = event.get("session")
        if type(session) is not dict:
            raise ProtocolError("missing_closed_session")
        session_id = _identifier(session.get("id"), "session_id")
    return UsageSnapshot(seconds, final, reason,
                         _identifier(event.get("event_id"), "event_id"), session_id)
