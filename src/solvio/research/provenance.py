"""Source provenance + deterministic candidate selection (STEP 21.2H, PHASE 19/22/23).

Source ids are derived from (run, url) — a URL is never a DB primary key, and the
redirect final_url is stored separately. Selection is deterministic (no LLM): dedup by
URL, prefer domain diversity, respect provider rank, cap at max_sources.
"""
from __future__ import annotations

import hashlib
from urllib.parse import urlsplit


def canonical_domain(url: str) -> str:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        host = ""
    return host[4:] if host.startswith("www.") else host


def make_source_id(run_id: str, url: str) -> str:
    return hashlib.sha256(f"{run_id}\x00{url}".encode("utf-8")).hexdigest()[:20]


def select_candidates(candidates: list[dict], max_sources: int) -> list[dict]:
    """Deterministic selection: domain-diverse first, then fill by provider rank."""
    ordered = sorted(candidates, key=lambda c: c.get("provider_rank", 999))
    seen_urls: set[str] = set()
    selected: list[dict] = []
    domains: set[str] = set()
    # pass 1 — one per domain (diversity)
    for c in ordered:
        url = c.get("url")
        if not url or url in seen_urls:
            continue
        dom = canonical_domain(url)
        if dom in domains:
            continue
        selected.append(c)
        seen_urls.add(url)
        domains.add(dom)
        if len(selected) >= max_sources:
            return selected
    # pass 2 — fill remaining slots allowing extra per domain
    for c in ordered:
        url = c.get("url")
        if not url or url in seen_urls:
            continue
        selected.append(c)
        seen_urls.add(url)
        if len(selected) >= max_sources:
            break
    return selected
