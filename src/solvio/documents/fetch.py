"""Dokumentbytes mit ihrer Herkunft verbinden -- ausschliesslich im Speicher."""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from typing import Any

MAX_DOCUMENT_BYTES = 20_000_000
DOCUMENT_SOURCES = frozenset({"gmail", "file"})


@dataclass(frozen=True)
class DocumentSource:
    source: str
    message_id: str
    attachment_id: str
    filename: str
    mime_type: str
    received_at: str
    sender: str
    size_bytes: int
    sha256: str

    def as_data(self) -> dict[str, Any]:
        return asdict(self)


def bind_source(*, source: str, content: bytes, message_id: str = "",
                attachment_id: str = "", filename: str = "",
                mime_type: str = "", received_at: str = "",
                sender: str = "") -> DocumentSource:
    """Bindet Identitaet und SHA-256 an die tatsaechlich geholten Bytes."""
    if source not in DOCUMENT_SOURCES:
        raise ValueError("document_source_not_allowed")
    if not isinstance(content, bytes):
        raise TypeError("document_content_must_be_bytes")
    return DocumentSource(
        source=source, message_id=message_id, attachment_id=attachment_id,
        filename=filename, mime_type=(mime_type or "").lower(),
        received_at=received_at, sender=sender, size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest())
