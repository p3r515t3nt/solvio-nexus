"""Die Chat-Routen (`/v1/conversations`, §2 des C3-Vertrags).

Es entsteht hier KEIN neuer Vertrauensanker. Dashboard-Anfragen laufen ueber
die bestehende Browsersitzung (`browser_sessions.actor`: HTTPS, genau ein
erlaubter Origin, gebundener CSRF-Token bei Mutationen) plus Owner-Filter; ein
vorhandenes Browser-Cookie mit fehlgeschlagener Pruefung faellt NIE auf die
Geraetekennung zurueck. Die App darf mit ihrer Transportkennung lesen und einen
Chat anlegen — dieselbe Linie wie `/v1/agent/runs`. Eine NACHRICHT senden kostet
einen Modellaufruf und kann einen Auftrag starten; dafuer verlangt die App den
frischen App-Attest-Beweis aus `conversation/message_proof.py`, exakt wie ein
Auftragsstart.

Die Annahme ist genau eine Store-Transaktion (`accept_delivery`): Replay per
Digest, Eigentum, Nachricht, Zustellzeile. Erst danach 202 — und nur bei einer
NEUEN Zeile wird der Prozessor geweckt. Der Store und die Laufzeit werden zur
Anfragezeit ueber Provider gelesen, nie beim Anhaengen eingesammelt: der
Gateway steht Sekunden vor dem Orchestrator.
"""
from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

from aiohttp import web

from solvio.conversation import message_proof as MP
from solvio.conversation.processing import ConversationProcessor, Runtime, transport_from_settings
from solvio.conversation.store import ConversationStoreError, DELIVERY_OPEN, KIND_TEXT
from solvio.dashboard.auth import OWNER
from solvio.logging_setup import get_logger
from solvio.security.mobile_approval import browser_sessions as B
from solvio.security.mobile_approval import protocol as P

log = get_logger("conversation")

PREFIX = "/v1/conversations"
PROCESSOR_KEY = "conversation_processor"
STORE_PROVIDER_KEY = "conversation_store_provider"
ROUTER_PROVIDER_KEY = "conversation_router_provider"
TRANSPORT_PROVIDER_KEY = "conversation_transport_provider"
MEMORY_PROVIDER_KEY = "conversation_memory_provider"
DEFAULT_LIST_LIMIT = 30
MAX_LIST_LIMIT = 100
_SOURCE_DASHBOARD = "dashboard"
_SOURCE_APP = "app"


def response(data, status=200):
    """Antworten mit den Headern aus `task_endpoint.response` — spaet geholt: ein
    Kernmodul importiert die Agentenlaufzeit nie auf Modulebene."""
    from solvio.agent_runtime.task_endpoint import response as respond
    return respond(data, status)


def error(status, reason):
    return response({"error": reason}, status)


async def _body(request, fields):
    from solvio.agent_runtime.task_endpoint import body
    return await body(request, fields, task_start=True)


def _proof_service(cp):
    from solvio.agent_runtime.conversation_message_proof import ConversationMessageProofService
    return ConversationMessageProofService(cp)


def _provided(app: web.Application, key: str):
    provider = app.get(key)
    if callable(provider):
        try:
            return provider()
        except Exception as exc:  # noqa: BLE001 - ein Provider darf nie eine Antwort kippen
            log.warning("conversation.provider_failed", key=key, kind=type(exc).__name__)
            return None
    return None


def _store(request: web.Request):
    return _provided(request.app, STORE_PROVIDER_KEY)


def _orchestrator_of(app: web.Application):
    """Dasselbe Provider-Muster wie `agent_runtime.endpoint._orchestrator` — am App-Objekt."""
    provider = app.get("agent_runtime_provider")
    if callable(provider):
        return provider()
    return app.get("agent_runtime")


def _default_store_provider(app: web.Application):
    def provide():
        return getattr(app.get("voice_core_server"), "conversations", None)
    return provide


def _default_router_provider(app: web.Application):
    def provide():
        # Nur ein AKTIVER Router traegt den Textweg: im Schatten- oder Aus-Modus
        # misst der Router und wirkt nicht — dann nimmt der Chat nichts an
        # (503 `cognitive_router_unavailable`), statt Auftraege zu starten, die
        # der Sprachweg in demselben Modus nie starten wuerde (Review Runde 3, A3-H4).
        engine = getattr(getattr(app.get("voice_core_server"), "dispatcher", None), "cognition", None)
        if engine is None or getattr(engine, "mode", "active") != "active":
            return None
        return engine
    return provide


def _default_memory_provider(app: web.Application):
    def provide():
        return getattr(getattr(app.get("voice_core_server"), "dispatcher", None), "memory", None)
    return provide


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


async def _authenticate(request: web.Request, guard, *, mutating: bool,
                        device_allowed: bool) -> tuple[str, str, Any] | None:
    """(kind, principal, actor) — `dashboard` mit Owner-Filter, sonst das Geraet.

    Ein Browser-Cookie, das nicht besteht, ist ein Nein — kein Fall fuer den
    Geraeteweg (`has_credentials`). Beobachter-Sitzungen lesen keine Chats.
    """
    if not request.secure:
        return None
    browser = await B.actor(request, mutating=mutating)
    if browser is not None:
        owner = request.app.get(OWNER)
        if owner and browser.principal == owner:
            return _SOURCE_DASHBOARD, browser.principal, browser
        return None
    if B.has_credentials(request):
        return None
    if not device_allowed:
        return None
    principal = await guard(request)
    if not principal:
        return None
    return _SOURCE_APP, principal, None


def _delivery_view(row: dict[str, Any], *, files: list | None = None,
                   source_chat: dict | None = None) -> dict[str, Any]:
    """Die Zustellsicht — mit `client_message_id` (§5.2): die Reconcile nach einem
    Neuladen kennt oft nur die vom Client erzeugte Kennung (202 ging verloren,
    `delivery_id` unbekannt); ohne das Feld bliebe „moeglicherweise nicht
    angekommen" stehen, bis der Nutzer denselben Text erneut sendet. Die Kennung
    ist eine Client-UUID, kein Geheimnis."""
    view = {"delivery_id": row["delivery_id"], "status": row["status"],
            "message_id": row["message_id"],
            "client_message_id": str(row["client_message_id"] or ""),
            "error_code": row["error_code"] or "",
            "task_id": row["task_id"] or "", "run_id": row["run_id"] or "",
            "revision": int(row["revision"] or 0)}
    if row["assistant_message_id"]:
        view["assistant_message_id"] = row["assistant_message_id"]
    if files is not None:
        view["files"] = files
    if source_chat:
        view["source_chat"] = source_chat
    return view


def _source_chat(store, row: dict, principal: str) -> dict | None:
    """A navigable origin comes from an extant owned chat/task link, not prose."""
    if row.get("status") != "completed" or not row.get("task_id") or not row.get("run_id"):
        return None
    try:
        dispatch = json.loads(row.get("dispatch") or "{}")
    except (TypeError, ValueError):
        return None
    source = dispatch.get("source_chat") if isinstance(dispatch, dict) else None
    if not isinstance(source, dict):
        return None
    source_id = source.get("conversation_id")
    if not isinstance(source_id, str) or source_id == row["conversation_id"]:
        return None
    with store.reading():
        if not store.conversation_owned(source_id, principal):
            return None
        chat = store.conversation(source_id)
        if not chat or not any(link["task_id"] == row["task_id"]
                               for link in store.task_links(source_id)):
            return None
    from solvio.specialists.launcher import redact
    return {"conversation_id": source_id, "title": redact(str(chat.get("title") or "Früheres Gespräch"))[:80]}


def _conversation_view(row: dict[str, Any]) -> dict[str, Any]:
    return {"conversation_id": row["conversation_id"], "title": row.get("title", ""),
            "kind": row.get("kind", KIND_TEXT), "created_at": row["created_at"],
            "last_activity_at": row["last_activity_at"],
            **({"read_only": True} if row.get("room_device_id") else {})}


def _owns(orch, run, principal) -> bool:
    from solvio.agent_runtime.endpoint import _owns as owns
    return owns(orch, run, principal)


def _open_task_count(orch, links: list[dict[str, Any]], principal: str) -> int:
    """Wie viele verlinkte Auftraege noch laufen — aus dem Agentenbuch, je Auftrag."""
    if orch is None:
        return 0
    count = 0
    for task_id in {link["task_id"] for link in links}:
        try:
            task = orch.ledger.get_task(task_id)
            runs = orch.ledger.runs_for_task(task_id)
        except Exception:  # noqa: BLE001
            continue
        if task is None or task.created_principal != principal or not runs:
            continue
        if not getattr(runs[-1], "terminal", False):
            count += 1
    return count


def _task_views(orch, links: list[dict[str, Any]], principal: str) -> list[dict[str, Any]]:
    """Je Link die juengste Laufsicht — und nur, wenn der Auftrag dem Principal gehoert."""
    if orch is None:
        return []
    from solvio.agent_runtime.endpoint import complete_view
    views: list[dict[str, Any]] = []
    seen: set[str] = set()
    for link in links:
        run_id = link["run_id"]
        if run_id in seen:
            continue
        seen.add(run_id)
        run = orch.ledger.get_run(run_id)
        if run is None or not _owns(orch, run, principal):
            log.warning("conversation.link_not_owned", task_id=link["task_id"], run_id=run_id)
            continue
        try:
            views.append(complete_view(orch, run))
        except Exception as exc:  # noqa: BLE001 - eine unlesbare Karte faellt weg, der Chat nicht
            log.warning("conversation.task_view_failed", run_id=run_id, kind=type(exc).__name__)
    return views


def _files(orch, run_id: str, principal: str) -> list[dict[str, Any]]:
    if orch is None or not run_id:
        return []
    run = orch.ledger.get_run(run_id)
    if run is None or not _owns(orch, run, principal):
        return []
    from solvio.agent_runtime.result_files import describe_files
    files, _ = describe_files(orch.ledger, run_id)
    return files


def attach(app: web.Application, orchestrator, guard, *, store_provider=None,
           router_provider=None, transport_provider=None,
           memory_provider=None) -> ConversationProcessor:
    """Die Routen anhaengen und den Prozessor bereitstellen.

    `orchestrator` ist der Provider der Agentenlaufzeit (`endpoint._orchestrator`),
    `guard` die Geraete-/Browserpruefung des Agent-Endpunkts. Die uebrigen
    Provider werden nur gesetzt, wenn die App noch keinen kennt — ein Test darf
    seinen eigenen Speicher hereinreichen, auch nach dem Anhaengen.
    """
    app.setdefault(STORE_PROVIDER_KEY, store_provider or _default_store_provider(app))
    app.setdefault(ROUTER_PROVIDER_KEY, router_provider or _default_router_provider(app))
    app.setdefault(MEMORY_PROVIDER_KEY, memory_provider or _default_memory_provider(app))
    if transport_provider is not None:
        app[TRANSPORT_PROVIDER_KEY] = transport_provider
    transport_cache: dict[str, Any] = {}

    def resolve() -> Runtime | None:
        orch = _orchestrator_of(app)
        store = _provided(app, STORE_PROVIDER_KEY)
        router = _provided(app, ROUTER_PROVIDER_KEY)
        if orch is None or store is None or router is None:
            return None
        transport = _provided(app, TRANSPORT_PROVIDER_KEY)
        if transport is None:
            transport = transport_cache.get("default")
            if transport is None:
                transport = transport_cache["default"] = transport_from_settings()
        return Runtime(orchestrator=orch, store=store, router=router,
                       control_plane=app.get("control_plane"),
                       browser_service=app.get(B._SERVICE), transport=transport,
                       personal_memory=_provided(app, MEMORY_PROVIDER_KEY),
                       owner_principal=str(app.get(OWNER) or ""))

    processor = ConversationProcessor(resolve)
    app[PROCESSOR_KEY] = processor

    async def start_processor(_app):
        processor.start()

    async def stop_processor(_app):
        await processor.shutdown()

    app.on_startup.append(start_processor)
    app.on_shutdown.append(stop_processor)

    def orch_of(request: web.Request):
        return orchestrator(request)

    # -- Chats ---------------------------------------------------------

    async def create(request: web.Request) -> web.Response:
        auth = await _authenticate(request, guard, mutating=True, device_allowed=True)
        if auth is None:
            return error(401, "unauthorized")
        _, principal, _ = auth
        store = _store(request)
        if store is None:
            return error(503, "conversation_store_unavailable")
        try:
            data = await request.json()
            if type(data) is not dict or not {"client_request_id"} <= set(data) <= {"client_request_id", "title"}:
                raise ValueError("invalid_fields")
            from solvio.agent_runtime.task_start_service import request_identifier
            request_identifier(data["client_request_id"])
            title = data.get("title", "")
            if type(title) is not str:
                raise ValueError("invalid_title")
        except (ValueError, TypeError):
            return error(400, "invalid_conversation")
        try:
            row, created = await asyncio.to_thread(
                store.create_conversation, owner_principal=principal, kind=KIND_TEXT,
                title=title, client_request_id=data["client_request_id"])
        except ConversationStoreError as exc:
            code = str(exc)
            if code == "invalid_title":
                return error(400, "invalid_title")
            if code == "conversation_deleted":
                return error(410, "conversation_deleted")
            log.error("conversation.create_failed", code=code[:80])
            return error(503, "conversation_store_unavailable")
        return response(_conversation_view(row), 201 if created else 200)

    async def index(request: web.Request) -> web.Response:
        auth = await _authenticate(request, guard, mutating=False, device_allowed=True)
        if auth is None:
            return error(401, "unauthorized")
        _, principal, _ = auth
        store = _store(request)
        if store is None:
            return error(503, "conversation_store_unavailable")
        limit = max(1, min(MAX_LIST_LIMIT, _int(request.query.get("limit"), DEFAULT_LIST_LIMIT)))
        orch = orch_of(request)

        def read():
            rows = store.list_conversations(principal, limit=limit)
            out = []
            for row in rows:
                links = store.task_links(row["conversation_id"])
                out.append(_conversation_view(row) | {
                    "message_count": int(row.get("message_count", 0)),
                    "open_delivery_count": int(row.get("open_delivery_count", 0)),
                    "open_task_count": _open_task_count(orch, links, principal)})
            return out
        try:
            conversations = await asyncio.to_thread(read)
        except ConversationStoreError as exc:
            log.error("conversation.list_failed", code=str(exc)[:80])
            return error(503, "conversation_store_unavailable")
        return response({"conversations": conversations})

    async def detail(request: web.Request) -> web.Response:
        auth = await _authenticate(request, guard, mutating=False, device_allowed=True)
        if auth is None:
            return error(401, "unauthorized")
        _, principal, _ = auth
        store = _store(request)
        if store is None:
            return error(503, "conversation_store_unavailable")
        conversation_id = request.match_info["conversation_id"]
        after = request.query.get("after_sequence")
        after_sequence = _int(after, -1) if after is not None else None
        if after is not None and after_sequence < 0:
            return error(400, "invalid_sequence")
        orch = orch_of(request)

        def read():
            # EIN Stand (§9.1 Fall 16, Abschluss): Eigentum, Gespraech, Nachrichten,
            # Zustellungen und Verweise unter einem Lock-Abschnitt. Sonst commitet
            # ein `complete_delivery` zwischen `messages()` und `deliveries()`, und
            # die Sicht sagt `completed` ohne die Assistentennachricht — beide
            # Clients fielen dann auf den langsamen Takt zurueck bzw. raeumten die
            # Reconcile, obwohl der Verlauf den Text noch nicht zeigt. Die
            # Auftragskarten aus dem Agentenbuch werden AUSSERHALB gelesen.
            with store.reading():
                if not store.conversation_owned(conversation_id, principal):
                    return None
                conversation = store.conversation(conversation_id)
                messages = store.messages(conversation_id, after_sequence=after_sequence)
                deliveries = store.deliveries(conversation_id)
                links = store.task_links(conversation_id)
            by_message = {row["message_id"]: row for row in deliveries}
            out = []
            for message in messages:
                item = {"message_id": message["message_id"], "sequence": message["sequence"],
                        "role": message["role"], "text": message["text"],
                        "created_at": message["created_at"]}
                delivery = by_message.get(message["message_id"])
                if delivery is not None:
                    item["delivery"] = _delivery_view(delivery,
                        source_chat=_source_chat(store, delivery, principal))
                out.append(item)
            return {"conversation": _conversation_view(conversation),
                    "messages": out,
                    "auftraege": _task_views(orch, links, principal),
                    "deliveries_open": sum(1 for row in deliveries if row["status"] in DELIVERY_OPEN)}
        try:
            data = await asyncio.to_thread(read)
        except ConversationStoreError as exc:
            log.error("conversation.detail_failed", code=str(exc)[:80])
            return error(503, "conversation_store_unavailable")
        if data is None:
            return error(404, "unknown_conversation")
        return response(data)

    async def rename(request: web.Request) -> web.Response:
        auth = await _authenticate(request, guard, mutating=True, device_allowed=False)
        if auth is None:
            return error(401, "unauthorized")
        _, principal, _ = auth
        store = _store(request)
        if store is None:
            return error(503, "conversation_store_unavailable")
        conversation_id = request.match_info["conversation_id"]
        try:
            data = await request.json()
            if type(data) is not dict or set(data) != {"title"} or type(data["title"]) is not str:
                raise ValueError("invalid_title")
        except (ValueError, TypeError):
            return error(400, "invalid_title")

        def write():
            if not store.conversation_owned(conversation_id, principal):
                return None
            return store.update_title(conversation_id, data["title"])
        try:
            row = await asyncio.to_thread(write)
        except ConversationStoreError as exc:
            code = str(exc)
            if code == "invalid_title":
                return error(400, "invalid_title")
            if code == "unknown_conversation":
                return error(404, "unknown_conversation")
            log.error("conversation.rename_failed", code=code[:80])
            return error(503, "conversation_store_unavailable")
        if row is None:
            return error(404, "unknown_conversation")
        return response({"conversation": _conversation_view(row)})

    async def delete(request: web.Request) -> web.Response:
        auth = await _authenticate(request, guard, mutating=True, device_allowed=False)
        if auth is None:
            return error(401, "unauthorized")
        _, principal, _ = auth
        store = _store(request)
        if store is None:
            return error(503, "conversation_store_unavailable")
        conversation_id = request.match_info["conversation_id"]

        def write():
            if not store.conversation_owned(conversation_id, principal):
                return False
            store.delete_conversation(conversation_id)
            return True
        try:
            done = await asyncio.to_thread(write)
        except ConversationStoreError as exc:
            code = str(exc)
            if code == "deliveries_open":
                return error(409, "deliveries_open")
            log.error("conversation.delete_failed", code=code[:80])
            return error(503, "conversation_store_unavailable")
        if not done:
            return error(404, "unknown_conversation")
        log.info("conversation.deleted", conversation_id=conversation_id)
        return web.Response(status=204, headers={"Cache-Control": "no-store"})

    # -- Nachrichten ---------------------------------------------------

    async def challenge(request: web.Request) -> web.Response:
        cp = request.app.get("control_plane")
        if cp is None or not request.secure or B.has_credentials(request):
            return error(401, "unauthorized")
        from solvio.security.mobile_approval.gateway import _authed_device
        device_id = await _authed_device(request)
        device = await cp.store.get_device(device_id) if device_id else None
        if device is None or not device.get("principal"):
            return error(401, "unauthorized")
        store = _store(request)
        if store is None:
            return error(503, "conversation_store_unavailable")
        conversation_id = request.match_info["conversation_id"]
        try:
            data = await _body(request, {"message"})
            message = MP.canonical_message_body(data["message"])
            if message["conversation_id"] != conversation_id:
                raise ValueError("conversation_mismatch")
        except (ValueError, TypeError, UnicodeError):
            return error(400, "invalid_message")
        try:
            owned = await asyncio.to_thread(store.conversation_owned, conversation_id, device["principal"])
        except ConversationStoreError:
            owned = False
        if not owned:
            return error(404, "unknown_conversation")
        result = await _proof_service(cp).issue(
            device_id=device_id, transport_cred=request.headers.get("X-Transport-Cred", ""),
            task_body=message)
        return response(result.as_dict()) if result else error(401, "unauthorized")

    async def send(request: web.Request) -> web.Response:
        if not request.secure:
            return error(401, "unauthorized")
        browser = await B.actor(request, mutating=True)
        if browser is not None:
            owner = request.app.get(OWNER)
            if not owner or browser.principal != owner:
                return error(401, "unauthorized")
        elif B.has_credentials(request):
            return error(401, "unauthorized")
        conversation_id = request.match_info["conversation_id"]
        cp = request.app.get("control_plane")
        verified = proof_service = None
        # Erst die nonce-freie Geraeteidentitaet (wie `challenge`), VOR dem Lesen des
        # Bodys (bis ~11 MiB): vor einer bekannten Identitaet sagt dieser Weg nichts
        # und liest nichts — auch keine 503 (Review Runde 3 F3-2, Runde 6 B6-H-2).
        if browser is None:
            if cp is None:
                return error(401, "unauthorized")
            from solvio.security.mobile_approval.gateway import _authed_device
            device_id = await _authed_device(request)
            device = await cp.store.get_device(device_id) if device_id else None
            if device is None or not device.get("principal"):
                return error(401, "unauthorized")
        try:
            data = await _body(request, {"message"} if browser else {"message", "proof"})
            message = MP.canonical_message_body(data["message"])
            if message["conversation_id"] != conversation_id:
                raise ValueError("conversation_mismatch")
        except (ValueError, TypeError, UnicodeError):
            return error(400 if browser else 401, "invalid_message" if browser else "unauthorized")
        # Verfuegbarkeit VOR dem Beweis: der App-Beweis verbraucht seine Nonce beim
        # Pruefen; eine 503 danach kostete eine neue Challenge (Review Runde 2, F-4).
        store = _store(request)
        if store is None:
            return error(503, "conversation_store_unavailable")
        orch = orch_of(request)
        if orch is None or getattr(orch, "router", None) is None:
            return error(503, "agent_runtime_disabled")
        if _provided(request.app, ROUTER_PROVIDER_KEY) is None:
            # Ohne kognitiven Router loest der Prozessor keine Laufzeit auf
            # (`resolve()` → None) und die Zustellung laege angenommen, aber
            # unbearbeitet — ein 202, das nichts verspricht (Review-Hinweis A2).
            return error(503, "cognitive_router_unavailable")
        if browser is None:
            if cp is None:
                return error(401, "unauthorized")
            try:
                proof = data["proof"]
                if type(proof) is not dict or set(proof) != {"nonce", "assertion_b64"}:
                    raise ValueError("invalid_proof")
                raw = base64.b64decode(proof["assertion_b64"], validate=True)
                proof_service = _proof_service(cp)
                verified = await proof_service.verify(
                    device_id=request.headers.get("X-Device-Id", ""), nonce=proof["nonce"],
                    task_body=message, assertion=raw)
            except (ValueError, TypeError):
                verified = None
            if verified is None:
                return error(401, "invalid_proof")
        if browser is not None:
            from solvio.browser_voice_session import browser_generation
            principal, source_kind = browser.principal, _SOURCE_DASHBOARD
            source_ref, device_id = "browser:" + browser.session_id, ""
            generation = await browser_generation(request.app.get(B._SERVICE),
                                                  browser.session_id, principal)
            if generation is None:
                return error(401, "unauthorized")
            core_id = str(getattr(cp if cp is not None else request.app.get(B._SERVICE),
                                  "core_instance_id", "") or "")
        else:
            from solvio.voice_task_session import device_generation
            principal, source_kind = verified.principal, _SOURCE_APP
            source_ref = "chat:" + verified.core_instance_id + ":" + verified.nonce
            device_id = verified.device_id
            actual = await device_generation(cp, device_id)
            if actual is None or actual[0] != principal or not await proof_service.still_current(verified):
                return error(401, "unauthorized")
            generation = MP.source_fingerprint(actual)
            core_id = verified.core_instance_id
        if not core_id:
            return error(401, "unauthorized")
        attachments = P.canonical_bytes(message["attachments"]).decode("utf-8") if "attachments" in message else ""
        target = P.canonical_bytes(message["target"]).decode("utf-8") if "target" in message else ""
        try:
            row, created = await asyncio.to_thread(
                store.accept_delivery, conversation_id=conversation_id, principal=principal,
                client_message_id=message["client_message_id"], text=message["text"],
                digest=MP.request_digest(message), source_kind=source_kind, source_ref=source_ref,
                core_id=core_id, source_generation=generation, device_id=device_id,
                attachments=attachments, target=target)
        except ConversationStoreError as exc:
            code = str(exc)
            if code in {"message_conflict", "room_conversation_read_only"}:
                return error(409, code)
            if code == "unknown_conversation":
                return error(404, "unknown_conversation")
            if code == "empty_message" or code.startswith("invalid_delivery"):
                return error(400, "invalid_message")
            log.error("conversation.accept_failed", code=code[:80])
            return error(503, "conversation_store_unavailable")
        if created:
            processor_now = request.app.get(PROCESSOR_KEY)
            if processor_now is not None:
                processor_now.wake(conversation_id)
            log.info("conversation.delivery_accepted", delivery_id=row["delivery_id"],
                     conversation_id=conversation_id, source_kind=source_kind)
        return response({"delivery_id": row["delivery_id"], "status": row["status"],
                         "message_id": row["message_id"]}, 202)

    async def delivery(request: web.Request) -> web.Response:
        auth = await _authenticate(request, guard, mutating=False, device_allowed=True)
        if auth is None:
            return error(401, "unauthorized")
        _, principal, _ = auth
        store = _store(request)
        if store is None:
            return error(503, "conversation_store_unavailable")
        conversation_id = request.match_info["conversation_id"]
        delivery_id = request.match_info["delivery_id"]
        orch = orch_of(request)

        def read():
            with store.reading():
                if not store.conversation_owned(conversation_id, principal):
                    return "unknown_conversation"
                row = store.delivery(conversation_id, delivery_id)
            if row is None:
                return "unknown_delivery"
            files = _files(orch, row["run_id"], principal) if row["run_id"] else []
            return _delivery_view(row, files=files, source_chat=_source_chat(store, row, principal))
        try:
            data = await asyncio.to_thread(read)
        except ConversationStoreError as exc:
            log.error("conversation.delivery_read_failed", code=str(exc)[:80])
            return error(503, "conversation_store_unavailable")
        if isinstance(data, str):
            return error(404, data)
        return response(data)

    app.add_routes([
        web.post(PREFIX, create),
        web.get(PREFIX, index),
        web.get(PREFIX + "/{conversation_id}", detail),
        web.patch(PREFIX + "/{conversation_id}", rename),
        web.delete(PREFIX + "/{conversation_id}", delete),
        web.post(PREFIX + "/{conversation_id}/messages/challenge", challenge),
        web.post(PREFIX + "/{conversation_id}/messages", send),
        web.get(PREFIX + "/{conversation_id}/deliveries/{delivery_id}", delivery),
    ])
    log.info("conversation.endpoint_attached", prefix=PREFIX, routes=8)
    return processor


__all__ = ["attach", "PREFIX", "PROCESSOR_KEY", "STORE_PROVIDER_KEY", "ROUTER_PROVIDER_KEY",
           "TRANSPORT_PROVIDER_KEY", "MEMORY_PROVIDER_KEY"]
