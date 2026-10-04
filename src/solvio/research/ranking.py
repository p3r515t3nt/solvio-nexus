"""Deterministic V1 ranking (STEP 21.2H, PHASE 24/25). NO generative model.

Scores from: query relevance, provider rank, fetch success, content availability.
Relevance defaults to a deterministic lexical overlap. An optional `embedder`
(query, texts) -> [float] may supply relevance instead — intended for the LOCAL
Qwen3-Embedding-0.6B on the Mac (ephemeral embeddings only: NOTHING is written to
semantic_index.sqlite3, no memory record, no consolidation). Ranking never depends on
Qwen — the lexical path is the fallback.
"""
from __future__ import annotations

import re

from solvio.research.models import SourceRecord

_WORD = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall((text or "").lower()))


def lexical_relevance(query: str, text: str) -> float:
    q = _tokens(query)
    if not q:
        return 0.0
    return len(q & _tokens(text)) / len(q)


def rank_sources(sources: list[SourceRecord], query: str, *, embedder=None) -> list[SourceRecord]:
    """Set rank_score on each source and return them sorted best-first (deterministic)."""
    texts = [f"{s.title or ''} {s.text_excerpt or s.snippet or ''}" for s in sources]
    if embedder is not None:
        try:
            relevance = list(embedder(query, texts))
            if len(relevance) != len(sources):
                raise ValueError("embedder length mismatch")
        except Exception:  # noqa: BLE001 - Qwen optional; fall back to lexical
            relevance = [lexical_relevance(query, t) for t in texts]
    else:
        relevance = [lexical_relevance(query, t) for t in texts]

    for i, s in enumerate(sources):
        provider_score = 1.0 - (min(s.provider_rank, 20) / 20.0)
        fetch_score = 1.0 if s.fetch_status == "OK" else 0.0
        content_score = 1.0 if (s.text_excerpt or s.content_sha256) else 0.0
        s.rank_score = round(0.5 * float(relevance[i]) + 0.2 * provider_score
                             + 0.2 * fetch_score + 0.1 * content_score, 6)

    return sorted(sources, key=lambda s: (s.rank_score or 0.0, -s.provider_rank),
                  reverse=True)
