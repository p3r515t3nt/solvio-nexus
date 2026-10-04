"""Deterministische Ableitung MemoryRecord -> embedding_text (STEP 21, PHASE 7).

Ein Embedding wird ausschliesslich aus fachlichem Inhalt gebildet, NICHT aus
Metadaten. Bewusst eingebettet:
    - subject
    - content
    - relevante tags (sortiert, dedupliziert)

Bewusst NICHT eingebettet (kein Rank-/Retrieval-Nutzen, teils sensibel):
    - trust_level / source_type          (Trust ist Autoritaet, nicht Semantik)
    - ids, timestamps                     (kein semantischer Gehalt als Text)
    - sensitivity / retention / provenance / tombstones / audit

SECRET_REFERENCE (PHASE 8): Der Inhalt verweist nur auf ein extern verwaltetes
Secret. Um selbst den Verweis nicht in einen abgeleiteten Index zu ziehen, wird
fuer solche Records NUR subject (+ tags) eingebettet, niemals der content.

Gleicher Record -> deterministisch gleicher embedding_text -> gleicher content_hash
(SHA-256). Aendert sich der Inhalt, aendert sich der Hash -> Re-Embedding (PHASE 18).
"""
from __future__ import annotations

import hashlib

from solvio.contracts.memory import MemoryRecord, Sensitivity

EMBEDDING_TEXT_FORMAT_VERSION = "1"


def _norm_tags(tags: list[str]) -> list[str]:
    seen, out = set(), []
    for t in tags:
        t = (t or "").strip().lower()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return sorted(out)


def embedding_text(record: MemoryRecord) -> str:
    """Kanonischer, deterministischer Einbettungstext eines Records.

    Format (Version 1), Zeilen mit \n verbunden:
        subject: <subject>
        <content>                 # bei SECRET_REFERENCE ausgelassen
        tags: <t1, t2, ...>       # nur falls tags vorhanden
    """
    lines = [f"subject: {record.subject.strip()}"]
    if record.sensitivity != Sensitivity.SECRET_REFERENCE:
        content = (record.content or "").strip()
        if content:
            lines.append(content)
    tags = _norm_tags(record.tags)
    if tags:
        lines.append("tags: " + ", ".join(tags))
    return "\n".join(lines)


def content_hash(text: str) -> str:
    """SHA-256 des embedding_text; identifiziert die eingebettete Inhaltsversion."""
    return hashlib.sha256(("solvio-emb-v" + EMBEDDING_TEXT_FORMAT_VERSION + ":" + text)
                          .encode("utf-8")).hexdigest()


def record_embedding_text_and_hash(record: MemoryRecord) -> tuple[str, str]:
    txt = embedding_text(record)
    return txt, content_hash(txt)
