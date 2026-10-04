"""Deterministische Retrieval-Fusion + No-Match-Regel (STEP 21.1).

Alle Strategien sind rein deterministisch — kein LLM, kein trainiertes Ranking.
Eingaben pro Query:
    fts_ids   : lexikalische Kandidaten (FTS), rangsortiert
    sem_hits  : semantische Kandidaten [(id, cosine)], rangsortiert (desc)
Ausgabe: fusionierte, rangsortierte id-Liste (Kandidaten, keine Faktenentscheidung).

Die endgueltige Sichtbarkeit prueft weiterhin der Active-Truth-Filter der
kanonischen Schicht (fail-closed) — die Fusion ist nur Ranking.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_SIG = re.compile(r"[A-Za-zÀ-ÿ0-9][A-Za-zÀ-ÿ0-9\-]*")

STRATEGIES = ("fts", "semantic", "rrf", "weighted_rrf", "semantic_dominant", "exact_boost")


@dataclass(frozen=True)
class HybridConfig:
    strategy: str = "semantic"        # Default bis DEV-Tuning einen Sieger einfriert
    rrf_k: int = 60
    semantic_weight: float = 1.0
    fts_weight: float = 1.0
    exact_boost: float = 2.0
    semantic_threshold: float = 0.0   # No-Match: min. top1-Kosinus, sonst leer
    dominant_fts_weight: float = 0.15  # Beitrag von FTS bei semantic_dominant


def exact_tokens(text: str) -> set[str]:
    """'Signifikante' Tokens fuer Exact-Match: Zahlen/IDs, Eigennamen (Grossanfang),
    lange Fachbegriffe. Bewusst konservativ, damit Allerweltswoerter nicht boosten."""
    out: set[str] = set()
    for t in _SIG.findall(text or ""):
        if any(c.isdigit() for c in t) or t[:1].isupper() or len(t) >= 7:
            out.add(t.lower())
    return out


def fuse(fts_ids: list[str], sem_hits: list[tuple[str, float]], cfg: HybridConfig,
         *, candidate_text: dict[str, str] | None = None, query: str = "") -> list[str]:
    sem_ids = [i for i, _ in sem_hits]
    if cfg.strategy == "fts":
        return list(fts_ids)
    if cfg.strategy == "semantic":
        return list(sem_ids)

    scores: dict[str, float] = {}
    k = cfg.rrf_k
    if cfg.strategy == "rrf":
        sw, fw = 1.0, 1.0
    elif cfg.strategy == "semantic_dominant":
        sw, fw = 1.0, cfg.dominant_fts_weight
    else:  # weighted_rrf, exact_boost
        sw, fw = cfg.semantic_weight, cfg.fts_weight

    for rank, rid in enumerate(sem_ids):
        scores[rid] = scores.get(rid, 0.0) + sw / (k + rank + 1)
    for rank, rid in enumerate(fts_ids):
        scores[rid] = scores.get(rid, 0.0) + fw / (k + rank + 1)

    if cfg.strategy == "exact_boost" and candidate_text:
        qtok = exact_tokens(query)
        if qtok:
            for rid, txt in candidate_text.items():
                if qtok & exact_tokens(txt):
                    scores[rid] = scores.get(rid, 0.0) + cfg.exact_boost

    # Stabile Sortierung: Score desc, dann semantische Rangordnung als Tie-Break.
    sem_rank = {rid: i for i, rid in enumerate(sem_ids)}
    return sorted(scores, key=lambda i: (-scores[i], sem_rank.get(i, 10_000)))


def is_no_match(sem_hits: list[tuple[str, float]], cfg: HybridConfig) -> bool:
    """True => kein hinreichend sicherer semantischer Treffer (No-Match).

    Verhindert, dass ein nicht vorhandener Fakt nur deshalb als plausibel gilt,
    weil er der beste (aber schwache) Vektor-Nachbar ist. Kalibriert auf DEV.
    """
    if not sem_hits:
        return True
    return sem_hits[0][1] < cfg.semantic_threshold
