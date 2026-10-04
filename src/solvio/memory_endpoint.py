"""Shared read-only WISSEN routes for authenticated iPhone and owner browser.

Reads use existing device proof or an owner browser session. This module has
no mutation routes. The shared capabilities execute changes through the
existing authorization journal; N5's five explicit owner-browser commands are
attached separately by dashboard.commands, without a global policy override.

No transcripts, audio or model reasoning are returned. Provenance carries
bounded source references; SECRET_REFERENCE exposes existence only.
"""
from __future__ import annotations

from typing import Any

from aiohttp import web

from solvio.contracts.memory import Sensitivity
from solvio.logging_setup import get_logger
from solvio.memory.adaptive import lifecycle as L

log = get_logger("memory_endpoint")

API = "/v1/memory"

#: Wie viele Eintraege eine Antwort hoechstens traegt.
PAGE = 100


def _visible_content(record: Any) -> str:
    """Bei `SECRET_REFERENCE` nur die Existenz — nie, worauf verwiesen wird.

    Woertlich dieselbe Regel wie in `memory/embedding_text.py` und in der
    Obsidian-Projektion. Es gibt genau eine solche Regel im Projekt, und sie
    gilt ueberall gleich.
    """
    if record.sensitivity is Sensitivity.SECRET_REFERENCE:
        return "(Verweis auf ein Geheimnis — der Verweis bleibt im Core.)"
    return record.content


def _shape(record: Any) -> dict[str, Any]:
    """Ein Eintrag, wie die App ihn sieht.

    Die Lebenszyklus-Ableitung rechnet der CORE und liefert sie als Feld. Die
    App leitet nie selbst ab — das ist die Lehre aus der ersten
    Obsidian-Ausbaustufe: Sichtbarkeitslogik, die ein Client nachbaut, baut er
    falsch nach.
    """
    return {
        "id": record.id,
        "content": _visible_content(record),
        "memory_type": record.memory_type.value,
        "subject": record.subject,
        "lifecycle": L.lifecycle_of(record),
        "sensitivity": record.sensitivity.value,
        "trust_level": record.trust_level.value,
        "confidence": record.confidence,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "updated_at": record.updated_at.isoformat() if record.updated_at else None,
        "valid_until": (record.valid_until.isoformat()
                        if record.valid_until else None),
        "evidence": L.evidence_summary(record),
        "explanation": L.explain(record),
    }


def _service(request: web.Request) -> Any:
    return request.app.get("memory_service")


def _adaptive(request: web.Request) -> Any:
    return request.app.get("adaptive_memory")


async def _authed(request: web.Request) -> str | None:
    """Dieselbe Geraetepruefung wie der Sprachweg — nicht eine zweite.

    Bewusst der Aufruf der bestehenden Funktion und keine Kopie: eine zweite
    Fassung derselben Pruefung waere genau die Stelle, an der spaeter eine der
    beiden nachgeschaerft wird und die andere nicht. Sie prueft Geraetestatus,
    Attestierung, Sperre, Umgebung und die Kennung in konstanter Zeit.

    Gibt die bewiesene Geraetekennung zurueck — oder `None`.
    """
    from solvio.security.mobile_approval import browser_sessions as B
    if B.COOKIE_NAME in request.cookies:
        from solvio.dashboard.auth import owner_browser
        browser = await owner_browser(request)
        return browser.principal if browser else None
    from solvio.voice_endpoint import _owner_device
    return await _owner_device(request)


def _err(status: int, reason: str) -> web.Response:
    return web.json_response({"error": reason}, status=status)


async def h_list(request: web.Request) -> web.Response:
    """„Was weiss SOLVIO?" — aktuelle Wahrheit."""
    if await _authed(request) is None:
        return _err(401, "unauthorized")
    service = _service(request)
    if service is None or getattr(service, "semantic", None) is None:
        return _err(503, "memory_unavailable")
    try:
        limit = min(int(request.query.get("limit", PAGE)), PAGE)
    except ValueError:
        limit = PAGE
    records = await service.semantic.memory.active_records()
    lifecycle = request.query.get("lifecycle", "")
    items = [_shape(r) for r in records]
    if lifecycle:
        items = [i for i in items if i["lifecycle"] == lifecycle]
    items.sort(key=lambda i: i["created_at"] or "", reverse=True)
    return web.json_response({"memories": items[:limit], "total": len(items)})


async def h_changes(request: web.Request) -> web.Response:
    """„Was wurde neu gelernt?" — seit einem Zeitpunkt."""
    if await _authed(request) is None:
        return _err(401, "unauthorized")
    service = _service(request)
    if service is None or getattr(service, "semantic", None) is None:
        return _err(503, "memory_unavailable")
    since = request.query.get("since", "")
    records = await service.semantic.memory.active_records()
    items = [_shape(r) for r in records]
    if since:
        items = [i for i in items if (i["updated_at"] or "") > since]
    items.sort(key=lambda i: i["updated_at"] or "", reverse=True)
    return web.json_response({"changes": items[:PAGE], "cursor":
                              items[0]["updated_at"] if items else since})


async def h_provenance(request: web.Request) -> web.Response:
    """„Warum weisst du das?" — die Kette, vorlesbar, ohne Transkript."""
    if await _authed(request) is None:
        return _err(401, "unauthorized")
    service = _service(request)
    if service is None or getattr(service, "semantic", None) is None:
        return _err(503, "memory_unavailable")
    memory_id = request.match_info.get("id", "")
    record = await service.semantic.get(memory_id)
    if record is None:
        return _err(404, "unknown")
    chain = await service.semantic.memory.get_provenance(memory_id)
    return web.json_response({
        "id": memory_id,
        "lifecycle": L.lifecycle_of(record),
        "explanation": L.explain(record),
        "evidence": L.evidence_summary(record),
        "chain": [{"source_type": e.source_type.value,
                   "trust_level": e.trust_level.value,
                   "at": e.at.isoformat() if e.at else None,
                   # Bezeichner und Kurzform. Der Gespraechskoerper liegt
                   # nirgends — auch nicht hinter diesem Feld.
                   "source": e.source, "note": e.note} for e in chain]})


async def h_candidates(request: web.Request) -> web.Response:
    """„Was braucht Bestaetigung?" — NUR wartende Vorschlaege.

    Vorschlaege sind kein Gedaechtnis. Sie erscheinen ausschliesslich hier und
    nie in `/memories`, nie im Buendel, nie im Abruf.
    """
    if await _authed(request) is None:
        return _err(401, "unauthorized")
    adaptive = _adaptive(request)
    if adaptive is None:
        return web.json_response({"candidates": [], "reason": "adaptive_off"})
    service = _service(request)
    pending = await adaptive.candidates.pending_decisions()
    from solvio.memory.adaptive import candidates as C
    pending += await adaptive.candidates.list_states(C.ADOPTING)
    out = []
    for cand in pending:
        entry = {"candidate_id": cand.id, "statement": cand.statement,
                 "state": cand.state, "ask_reason": cand.ask_reason,
                 "memory_type": cand.memory_type,
                 "sensitivity": cand.sensitivity,
                 "observations": len(cand.evidence),
                 "independent_conversations": cand.independent_conversations(),
                 "first_seen": cand.first_seen, "last_seen": cand.last_seen}
        if cand.contested_memory_id and service is not None:
            current = await service.semantic.get(cand.contested_memory_id)
            if current is not None:
                entry["contradicts"] = {"id": current.id,
                                        "content": _visible_content(current),
                                        "lifecycle": L.lifecycle_of(current)}
        out.append(entry)
    return web.json_response({"candidates": out})


async def h_tombstones(request: web.Request) -> web.Response:
    """„Was wurde endgueltig geloescht?" — inhaltslose Grabsteine."""
    if await _authed(request) is None:
        return _err(401, "unauthorized")
    service = _service(request)
    if service is None or getattr(service, "semantic", None) is None:
        return _err(503, "memory_unavailable")
    stones = await service.semantic.memory.list_tombstones()
    return web.json_response({"tombstones": [
        {"subject_hash": t.subject_hash, "purged_at":
         t.purged_at.isoformat() if hasattr(t.purged_at, "isoformat")
         else str(t.purged_at), "reason": t.reason} for t in stones]})


def attach(app: web.Application, *, service: Any, adaptive: Any = None
           ) -> web.Application:
    """Die Leserouten anhaengen. Keine Zeile in `gateway.py` aendert sich."""
    app["memory_service"] = service
    app["adaptive_memory"] = adaptive
    app.router.add_get(f"{API}/memories", h_list)
    app.router.add_get(f"{API}/changes", h_changes)
    app.router.add_get(f"{API}/candidates", h_candidates)
    app.router.add_get(f"{API}/tombstones", h_tombstones)
    app.router.add_get(f"{API}/memories/{{id}}/provenance", h_provenance)
    log.info("memory_endpoint.attached", base=API)
    return app
