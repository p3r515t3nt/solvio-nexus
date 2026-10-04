"""Betriebssicht auf eigene Agentenauftraege am bestehenden Core-Gateway.

Es entsteht hier **kein neuer Vertrauensanker**. Dieselbe TLS-Verbindung mit
gepinntem Zertifikat, dieselbe Geraeteregistrierung, dieselbe Transportkennung
wie beim Freigabeweg — und dieselbe scharfe Trennung:

* **Transportkennung darf lesen.** Sie beweist, dass die Anfrage vom
  registrierten Geraet des Besitzers kommt.
* **Transportkennung darf niemals freigeben oder einen Auftrag erzeugen.**
  N2 fuegt in task_endpoint einen frischen App-Auftragsbeweis bzw. eine
  HTTPS-/CSRF-gebundene Browser-Sitzung hinzu. Pi-Auftraege brauchen eine
  konkrete Dashboard-OK-/Face-ID-Entscheidung im bestehenden Journal.

`cancel` und `resume` sind SOLVIO-INTERNE Buchhaltung — dieselbe Linie wie
`run_now` im Kontrollzentrum. Sie brechen einen Lauf ab oder nehmen ihn wieder
auf; sie fuehren keine Handlung nach aussen aus. Was ein Lauf danach TUT,
prueft der Core erneut gegen seinen Auftragsscope, die aktuelle Politik und
seine Kostenregel. Eine Wiederaufnahme erweitert keine Befugnis.

**Eine `ar-`-Kennung ist ein Verweis, kein Berechtigungsnachweis.** Keine Route
hier akzeptiert sie als Autoritaet: nur das registrierte Geraet oder die
authentifizierte Browser-Sitzung darf die Buchhaltung des eigenen Prinzipals
lesen und steuern. Fremde Auftraege erscheinen auch mit bekannter Kennung nicht.

Was NICHT hinausgeht: Gedankengaenge, Prompts, Rohausgaben, Geheimnisse,
Anmeldungen. Die Antworten tragen Betriebswahrheit — was laeuft, welcher
Spezialist, welcher Schritt, worauf gewartet wird, was herauskam, was
fehlschlug.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import quote

from aiohttp import web

from solvio.agent_runtime.inquiry import REASON_WORDS, STATE_WORDS, run_view
from solvio.logging_setup import get_logger

log = get_logger("agent_runtime")

PREFIX = "/v1/agent"


def _err(status: int, code: str) -> web.Response:
    return web.json_response({"error": code}, status=status)


async def _owner_device(request: web.Request) -> str | None:
    """Dieselbe Geraetepruefung wie der Freigabeweg — nicht eine zweite.

    Bewusst der Aufruf der bestehenden Funktion und keine Kopie: eine zweite
    Fassung derselben Pruefung waere genau die Stelle, an der spaeter eine der
    beiden nachgeschaerft wird und die andere nicht.
    """
    from solvio.security.mobile_approval.gateway import _authed_device
    return await _authed_device(request)


def _orchestrator(request: web.Request):
    """Die Laufzeit — zur ANFRAGEZEIT gelesen, nicht beim Anhaengen.

    Live gefunden: der Freigabe-Gateway startet rund vier Sekunden vor dem
    Orchestrator. Ein Attach, der das Objekt einmal einsammelt, bekam deshalb
    immer `None`, haengte gar keine Route an — und `/v1/agent/runs` antwortete
    mit 404 statt mit 401. Die Routen existieren jetzt immer; ob dahinter etwas
    laeuft, entscheidet sich beim Zugriff.
    """
    provider = request.app.get("agent_runtime_provider")
    if callable(provider):
        return provider()
    return request.app.get("agent_runtime")


def _run_view(run, boundary=None) -> dict:
    """Die sichere Betriebssicht auf einen Lauf. Deutsch und ohne Rohkennungen."""
    from solvio.agent_runtime.boundaries import UserBoundary

    parsed = boundary if boundary is not None else UserBoundary.from_json(run.boundary)
    return {
        "id": run.run_id,
        "aufgabe": run.task_id,
        "zustand": _WORDS.get(run.state, run.state),
        "zustand_code": run.state,
        "begonnen": run.started_at,
        "beendet": run.finished_at,
        "ergebnis": run.result_summary,
        "grund": _REASONS.get(run.failure_category, run.failure_category),
        "spezialisten": run.specialist_count,
        "arbeitsergebnis": run.branch_ref,
        "wartet_auf": parsed.as_dict() if parsed else None,
    }


#: Die Worte fuer Zustaende und Gruende wohnen in der Auftragsauskunft
#: (`inquiry`) und werden hier mitbenutzt — EINE Liste, nicht zwei. Die
#: alten Namen bleiben, damit niemand, der sie liest, ins Leere greift.
_WORDS = STATE_WORDS
_REASONS = REASON_WORDS


def attach(app: web.Application, orchestrator: Any = None, *,
           provider: Any = None) -> web.Application:
    """Haengt die bestehende Betriebssicht und die N2-Auftragswege an.

    `provider` ist eine Funktion, die die Laufzeit bei Bedarf liefert. Sie ist
    der normale Weg: der Gateway steht frueher als der Orchestrator, und eine
    Route, die es erst danach gaebe, gaebe es nie.
    """
    app["agent_runtime"] = orchestrator
    app["agent_runtime_provider"] = provider

    async def guard(request: web.Request):
        from solvio.security.mobile_approval import browser_sessions as B
        browser = await B.actor(request, mutating=request.method not in {"GET", "HEAD"},
                                allow_observer=True)
        if browser is not None:
            return browser.principal
        if B.has_credentials(request):
            return None
        device_id = await _owner_device(request)
        if device_id is None:
            return None
        device = await request.app["control_plane"].store.get_device(device_id)
        return device["principal"] if device else None

    # -- Lesen ---------------------------------------------------------

    async def action_services(request: web.Request) -> web.Response:
        principal = await guard(request)
        if principal is None:
            return _err(401, "unauthorized")
        orch = _orchestrator(request)
        if orch is None:
            return _err(503, "agent_runtime_disabled")
        owner = getattr(getattr(orch.router, "_mobile", None), "owner_principal", "")
        if not owner or principal != owner:
            return _err(403, "owner_configuration_required")
        from solvio.capabilities.task_action import TaskServiceAction
        service = getattr(orch, "action_service", None)
        accounts = service.accounts() if type(service) is TaskServiceAction else []
        return web.json_response({"services": accounts}, headers={"Cache-Control": "no-store"})

    async def runs(request: web.Request) -> web.Response:
        principal = await guard(request)
        if principal is None:
            return _err(401, "unauthorized")
        orch = _orchestrator(request)
        if orch is None:
            return _err(503, "agent_runtime_disabled")
        # Same authenticated reader and projection; no new authority or store.
        if request.query.get("view") == "tasks":
            rows = await asyncio.to_thread(orch.ledger.task_overview_runs, principal=principal)
            return web.json_response({
                "laeufe": [await asyncio.to_thread(_complete_view, orch, r) for r in rows],
                "next_before": None,
            }, headers={"Cache-Control": "no-store"})
        query = request.query.get("q", "").strip()
        if len(query) > 200:
            return _err(400, "invalid_result_query")
        before = None
        if "before" in request.query:
            cursor = request.query["before"]
            if not re.fullmatch(r"ar-[0-9a-f]{16}", cursor):
                return _err(400, "invalid_run_cursor")
            anchor = orch.ledger.get_run(cursor)
            if anchor is None or not _owns(orch, anchor, principal):
                return _err(400, "invalid_run_cursor")
            before = (anchor.created_at, anchor.run_id)
        if query:
            views, cursor = await asyncio.to_thread(_search_result_page, orch, principal, before, query)
            return web.json_response({"laeufe": views, "next_before": cursor},
                headers={"Cache-Control": "no-store"})
        rows = await asyncio.to_thread(orch.ledger.recent_runs, limit=26,
                                      principal=principal, before=before)
        return web.json_response({
            "laeufe": [await asyncio.to_thread(_complete_view, orch, r) for r in rows[:25]],
            "next_before": rows[24].run_id if len(rows) > 25 else None,
        }, headers={"Cache-Control": "no-store"})

    async def run_detail(request: web.Request) -> web.Response:
        principal = await guard(request)
        if principal is None:
            return _err(401, "unauthorized")
        orch = _orchestrator(request)
        if orch is None:
            return _err(503, "agent_runtime_disabled")
        run_id = request.match_info["run_id"]
        run = orch.ledger.get_run(run_id)
        if run is None or not _owns(orch, run, principal):
            return _err(404, "unknown_run")
        view = await asyncio.to_thread(_complete_view, orch, run)
        task = orch.ledger.get_task(run.task_id)
        view["auftrag"] = task.objective
        view["schritte"] = [
            {"folge": s.seq, "art": s.kind, "zustand": s.state,
             "spezialist": s.specialist_profile, "faehigkeit": s.capability,
             "zusammenfassung": s.summary,
             # Verweise, nie Material.
             "artefakte": s.artifact_refs, "commits": s.commit_ref}
            for s in orch.ledger.steps_for_run(run_id)]
        view["verlauf"] = [
            {"zeit": e.at, "art": e.kind, "text": e.summary}
            for e in orch.ledger.events_for_run(run_id, limit=60)]
        from solvio.agent_runtime import task_revisions as TR
        try:
            view["task_history"] = [{"run_id": entry["run_id"], "revision": entry["revision"],
                "text": entry["text"], "state": historical.state,
                "result_summary": historical.result_summary}
                for entry in TR.history(orch.ledger, run_id)
                if (historical := orch.ledger.get_run(entry["run_id"])) is not None]
        except (ValueError, TypeError, KeyError, OSError):
            view["task_history"] = []
        return web.json_response(view)

    async def file_response(request: web.Request, *, preview=False) -> web.Response:
        principal = await guard(request)
        if principal is None:
            return _err(401, "unauthorized")
        orch = _orchestrator(request)
        if orch is None:
            return _err(503, "agent_runtime_disabled")
        run_id = request.match_info["run_id"]
        if not _owns(orch, orch.ledger.get_run(run_id), principal):
            return _err(404, "unknown_run")
        from solvio.agent_runtime.result_files import read_result
        try:
            descriptor, content = await asyncio.to_thread(
                read_result, orch.ledger, run_id, request.match_info["artifact_id"])
        except (ValueError, OSError, KeyError, TypeError):
            return _err(404, "document_result_unavailable")
        if preview and descriptor["preview_kind"] == "none":
            return _err(404, "result_preview_unavailable")
        disposition = "inline" if preview else "attachment"
        headers = {"Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(descriptor['name'], safe='')}",
                   "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                   "Content-Security-Policy": "default-src 'none'; sandbox",
                   "Accept-Ranges": "bytes"}
        status = 200
        byte_range = request.headers.get("Range")
        if byte_range:
            match = re.fullmatch(r"bytes=([0-9]{0,20})-([0-9]{0,20})", byte_range)
            size = len(content)
            if not match or not any(match.groups()):
                return web.Response(status=416, headers=headers | {"Content-Range": f"bytes */{size}"})
            start, end = match.groups()
            start, end = (int(start), min(int(end), size - 1) if end else size - 1) if start else (max(0, size - int(end)), size - 1)
            if start > end or start >= size:
                return web.Response(status=416, headers=headers | {"Content-Range": f"bytes */{size}"})
            content = content[start:end + 1]
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
            status = 206
        return web.Response(body=content, status=status, content_type=descriptor["mime_type"],
                            charset="utf-8" if descriptor["mime_type"] == "text/plain" else None,
                            headers=headers)

    async def document_download(request: web.Request) -> web.Response:
        return await file_response(request)

    async def file_preview(request: web.Request) -> web.Response:
        return await file_response(request, preview=True)

    async def running(request: web.Request) -> web.Response:
        principal = await guard(request)
        if principal is None:
            return _err(401, "unauthorized")
        orch = _orchestrator(request)
        if orch is None:
            return _err(503, "agent_runtime_disabled")
        return web.json_response(
            {"laeufe": [await asyncio.to_thread(_complete_view, orch, r) for r in orch.ledger.open_runs() if _owns(orch, r, principal)]})

    async def events(request: web.Request) -> web.Response:
        principal = await guard(request)
        if principal is None:
            return _err(401, 'unauthorized')
        orch = _orchestrator(request)
        if orch is None:
            return _err(503, 'agent_runtime_disabled')
        run_id = request.match_info['run_id']
        if not _owns(orch, orch.ledger.get_run(run_id), principal):
            return _err(404, 'unknown_run')
        try:
            page = orch.ledger.events_after(run_id, int(request.query.get('after', '0')),
                                           int(request.query.get('limit', '50')))
        except ValueError:
            return _err(400, 'invalid_event_cursor')
        import json
        page['events'] = [{'id': e.id, 'at': e.at, 'step_id': e.step_id,
                           'kind': e.kind, 'summary': e.summary,
                           'observation': json.loads(e.ref) if e.kind == 'native_progress' else None}
                          for e in page['events']]
        return web.json_response(page, headers={'Cache-Control': 'no-store'})

    # -- Handeln: SOLVIO-interne Buchhaltung, nie Aussenwirkung ---------

    async def cancel(request: web.Request) -> web.Response:
        principal = await guard(request)
        if principal is None:
            return _err(401, "unauthorized")
        orch = _orchestrator(request)
        if orch is None:
            return _err(503, "agent_runtime_disabled")
        run_id = request.match_info["run_id"]
        if not _owns(orch, orch.ledger.get_run(run_id), principal):
            return _err(404, "unknown_run")
        if not await orch.cancel(run_id):
            return _err(409, "not_cancellable")
        log.info("agent_runtime.cancelled_by_user", run_id=run_id)
        return web.json_response({"id": run_id, "zustand": "abgebrochen"})

    async def resume(request: web.Request) -> web.Response:
        principal = await guard(request)
        if principal is None:
            return _err(401, "unauthorized")
        orch = _orchestrator(request)
        if orch is None:
            return _err(503, "agent_runtime_disabled")
        run_id = request.match_info["run_id"]
        if not _owns(orch, orch.ledger.get_run(run_id), principal):
            return _err(404, "unknown_run")
        choice = {}
        if request.can_read_body:
            try:
                raw = await request.read()
                if len(raw) > 512:
                    return _err(400, "invalid_provider_selection")
                import json
                choice = json.loads(raw)
                if choice != {} and (not isinstance(choice, dict) or set(choice) != {"provider", "boundary_ref"}
                        or choice["provider"] not in {"codex", "claude-code"}
                        or not isinstance(choice["boundary_ref"], str)
                        or not re.fullmatch(r"[a-f0-9]{64}", choice["boundary_ref"])):
                    return _err(400, "invalid_provider_selection")
            except (ValueError, TypeError):
                return _err(400, "invalid_provider_selection")
        if not await orch.resume(run_id, **(dict(choice, principal=principal) if choice else {})):
            return _err(409, "not_waiting")
        log.info("agent_runtime.resumed_by_user", run_id=run_id)
        return web.json_response({"id": run_id, "zustand": "arbeitet"})

    # Pfad-exakt: keine Praefixroute, kein Platzhalter fuer die Aktion. Eine
    # Route, die `/{aktion}` entgegennaehme, waere eine Stelle, an der ein
    # Tippfehler zu einer anderen Handlung wird.
    app.add_routes([
        web.get(f"{PREFIX}/action-services", action_services),
        web.get(f"{PREFIX}/runs", runs),
        web.get(f"{PREFIX}/runs/{{run_id}}", run_detail),
        web.get(f"{PREFIX}/runs/{{run_id}}/events", events),
        web.get(f"{PREFIX}/runs/{{run_id}}/artifacts/{{artifact_id}}/download", document_download),
        web.get(f"{PREFIX}/runs/{{run_id}}/artifacts/{{artifact_id}}/preview", file_preview),
        web.get(f"{PREFIX}/running", running),
        web.post(f"{PREFIX}/runs/{{run_id}}/cancel", cancel),
        web.post(f"{PREFIX}/runs/{{run_id}}/resume", resume),
    ])
    from solvio.agent_runtime.task_endpoint import attach as attach_tasks
    attach_tasks(app, orchestrator=_orchestrator)
    from solvio.agent_runtime.action_account_endpoint import attach as attach_accounts
    attach_accounts(app, _orchestrator)
    from solvio.agent_runtime.action_resources_endpoint import attach as attach_resources
    attach_resources(app, _orchestrator)
    from solvio.agent_runtime.action_intent_endpoint import attach as attach_intent
    attach_intent(app, _orchestrator)
    from solvio.agent_runtime.task_followup_endpoint import attach as attach_followup
    attach_followup(app, _orchestrator)
    from solvio.agent_runtime.action_portal_endpoint import attach as attach_portal_sessions
    attach_portal_sessions(app, _orchestrator, guard)
    # N8/C3: die dauerhaften Chats — hinter den Auftragswegen, mit derselben
    # Geraete-/Browserpruefung und demselben Laufzeit-Provider. Der Gespraechs-
    # speicher wird zur Anfragezeit gelesen, nie hier eingesammelt.
    from solvio.conversation.endpoint import attach as attach_conversations
    attach_conversations(app, _orchestrator, guard)
    log.info("agent_runtime.endpoint_attached", prefix=PREFIX, routes=32)
    return app


def _owns(orch, run, principal):
    task = orch.ledger.get_task(run.task_id) if run is not None else None
    return task is not None and task.created_principal == principal


def _search_result_page(orch, principal, before, query):
    """Search only already visible result text/names, never artifact contents.

    Owner filtering precedes scanning and pagination. Reuse the public verified
    projection so internal paths, failed files and hidden metadata cannot match.
    """
    from solvio.agent_runtime.result_files import may_match_filename
    matches, query = [], query.casefold()
    # A query never scans the entire history in an uncancellable thread. The
    # cursor records examined rows, including nonmatches; clients can continue.
    rows = orch.ledger.recent_runs(limit=101, principal=principal, before=before)
    for index, run in enumerate(rows[:100]):
        task = orch.ledger.get_task(run.task_id)
        if (query in (task.objective if task else "").casefold()
                or query in run.result_summary.casefold()
                or may_match_filename(orch.ledger, run.run_id, query)):
            view = _complete_view(orch, run)
            files = view.get("dateien") or []
            text = [view.get("auftrag", ""), view.get("ergebnis", "")] + [f["name"] for f in files]
            if (files or (view.get("ergebnis") or "").strip()) and any(query in str(value or "").casefold() for value in text):
                matches.append(view)
                if len(matches) == 25:
                    return matches, run.run_id if index + 1 < len(rows) else None
    return matches, rows[99].run_id if len(rows) > 100 else None


def complete_view(orch, run):
    """Die vollstaendige Laufsicht eines Auftrags — Dateien mit Download-URLs,
    Revision, Folgeanweisung, Aktionsabsicht, Kontobindung. Oeffentlich, weil
    die Chat-Ansicht (N8/C3) je Auftragslink genau diese Karte ausliefert."""
    # The inquiry owns result truth. Keep the established iOS `id` alias.
    view = run_view(orch.ledger, run) | {"id": run.run_id}
    from solvio.agent_runtime.action_intent import view as intent_view
    view["action_intent"] = intent_view(orch.ledger, run.run_id)
    from solvio.agent_runtime.portal_connection import view as portal_connection_view
    view['portal_connection'] = portal_connection_view(orch.ledger, run.run_id)
    from solvio.agent_runtime.result_files import describe_files
    view["dateien"], view["datei_hinweis"] = describe_files(orch.ledger, run.run_id)
    task = orch.ledger.get_task(run.task_id)
    # Keep the original task visible; a follow-up is its own immutable message,
    # not a rewritten original objective or a new task in the list.
    if task:
        view["auftrag"] = task.objective
        view["kosten"] = orch.costs.view(task.task_id) if hasattr(orch, "costs") else None
        if (view["kosten"] or {}).get("ai_tool", {}).get("credit_usage_unmeasured"):
            view["abrechnung"] = "Abo / freigegebene Credits; Verbrauch nicht gemessen"
            view["befunde"] = [*(view.get("befunde") or []),
                "Für diesen Auftrag war die Nutzung deiner ChatGPT-Credits freigegeben. "
                "Der tatsächliche Credit-Verbrauch ist nicht gemessen; die Euroanzeige ist keine Nullkostenbestätigung."]
    from solvio.agent_runtime import task_revisions as TR
    try:
        revision = TR.revision_for_run(orch.ledger, run.run_id)
        view["task_revision"] = {key: revision[key] for key in ("revision", "digest", "text", "parent_run_id")}
        view["followup"] = TR.eligibility(orch.ledger, run.run_id)
    except (ValueError, TypeError, KeyError, OSError):
        view["task_revision"] = None
        view["followup"] = {"eligible": False, "reason": "Auftragsverlauf ist nicht vollständig bestätigt."}
    if task and task.scope == "action" and run.state == "WAITING_USER":
        from solvio.agent_runtime.action_account_endpoint import rebind_view
        view["kontobindung"] = rebind_view(orch, run.run_id)
    artifacts = orch.ledger.artifacts_for_run(run.run_id)
    for item, artifact in zip(view.get("artefakte", []), artifacts):
        if artifact.kind == "document_result":
            item["id"] = artifact.artifact_id
            item["download_url"] = (f"{PREFIX}/runs/{run.run_id}/artifacts/"
                                    f"{artifact.artifact_id}/download")
    return view


#: Der bisherige Name bleibt gueltig.
_complete_view = complete_view
