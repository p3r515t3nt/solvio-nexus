"""Die Routen des Kontrollzentrums — auf dem Weg, der schon vertraut ist.

Es entsteht hier **kein neuer Vertrauensanker**. Dieselbe TLS-Verbindung mit
gepinntem Zertifikat, dieselbe Geraeteregistrierung, dieselbe Transportkennung
wie beim Freigabeweg. Der Unterschied zum Freigabepfad ist bewusst scharf und
steht im Vertrag des Gateways schon so drin:

* **Transportkennung darf lesen.** Sie beweist, dass die Anfrage vom
  registrierten Geraet des Besitzers kommt.
* **Transportkennung darf niemals freigeben.** Eine Freigabe braucht Face ID und
  eine frische App-Attest-Aussage. Daran aendert dieses Modul nichts, und es
  bietet auch keinen Weg dorthin an.

Was die Handlungen hier duerfen, ist eng gezogen: sie beruehren ausschliesslich
SOLVIOs eigene Buchhaltung — eine Aufgabe pausieren, eine Meldung als gelesen
markieren. Nichts davon wirkt nach aussen. Was eine Aufgabe spaeter TUT, wird
beim Ausfuehren erneut bewertet und landet, wenn es schreibt, wie immer als
Freigabe auf dem iPhone.

Angehaengt wird an die bereits gebaute Anwendung, statt den Freigabe-Gateway zu
veraendern. Der ist heikel genug; er soll so bleiben, wie er freigegeben wurde.
"""
from __future__ import annotations

from typing import Any

from aiohttp import web

from solvio.logging_setup import get_logger

log = get_logger("control")

PREFIX = "/v1/control"

#: Wie gross eine Anfrage an dieses Modul hoechstens sein darf. Es kommen nur
#: Kennungen — alles Groessere ist ein Missverstaendnis oder ein Versuch.
MAX_BODY = 4 * 1024


def _err(status: int, code: str) -> web.Response:
    return web.json_response({"error": code}, status=status)


async def _owner_device(request: web.Request) -> str | None:
    """Dieselbe Pruefung wie beim Freigabeweg — nicht eine eigene.

    Bewusst der Aufruf der bestehenden Funktion und keine Kopie: eine zweite
    Fassung derselben Pruefung waere genau die Stelle, an der spaeter eine der
    beiden nachgeschaerft wird und die andere nicht.
    """
    from solvio.security.mobile_approval.gateway import _authed_device
    return await _authed_device(request)


def _center(request: web.Request):
    return request.app.get("control_center")


def attach(app: web.Application, center: Any) -> web.Application:
    """Haengt die Routen an die bestehende Anwendung."""
    app["control_center"] = center

    async def guard(request: web.Request):
        from solvio.security.mobile_approval import browser_sessions as B
        if B.COOKIE_NAME in request.cookies:
            # N5 exposes reads and explicit inbox acknowledgement only. Existing
            # scheduler/repair endpoints receive no implicit browser expansion.
            mutating = request.method not in {"GET", "HEAD"}
            if mutating and not (request.path.startswith(PREFIX + "/inbox/")
                                 and request.path.endswith("/read")):
                return None
            from solvio.dashboard.auth import owner_browser
            browser = await owner_browser(request, mutating=mutating)
            return browser.principal if browser else None
        device = await _owner_device(request)
        if device is None:
            return None
        return device

    # -- Lesen ---------------------------------------------------------------

    async def overview(request: web.Request) -> web.Response:
        if await guard(request) is None:
            return _err(401, "unauthorized")
        return web.json_response(await _center(request).overview())

    async def tasks(request: web.Request) -> web.Response:
        if await guard(request) is None:
            return _err(401, "unauthorized")
        return web.json_response(await _center(request).tasks())

    async def task(request: web.Request) -> web.Response:
        if await guard(request) is None:
            return _err(401, "unauthorized")
        found = await _center(request).task(request.match_info["task_id"])
        if found is None:
            return _err(404, "unknown_task")
        return web.json_response(found)

    async def inbox(request: web.Request) -> web.Response:
        if await guard(request) is None:
            return _err(401, "unauthorized")
        return web.json_response(await _center(request).inbox())

    async def item(request: web.Request) -> web.Response:
        if await guard(request) is None:
            return _err(401, "unauthorized")
        found = await _center(request).item(request.match_info["item_id"])
        if found is None:
            return _err(404, "unknown_item")
        return web.json_response(found)

    async def activity(request: web.Request) -> web.Response:
        if await guard(request) is None:
            return _err(401, "unauthorized")
        return web.json_response(await _center(request).activity())

    async def system(request: web.Request) -> web.Response:
        if await guard(request) is None:
            return _err(401, "unauthorized")
        return web.json_response(await _center(request).system())

    async def running(request: web.Request) -> web.Response:
        if await guard(request) is None:
            return _err(401, "unauthorized")
        return web.json_response(await _center(request).running())

    async def diagnoses(request: web.Request) -> web.Response:
        if await guard(request) is None:
            return _err(401, "unauthorized")
        return web.json_response(await _center(request).diagnoses())

    async def diagnose(request: web.Request) -> web.Response:
        if await guard(request) is None:
            return _err(401, "unauthorized")
        found = await _center(request).diagnose(request.match_info["component"])
        if found is None:
            return _err(404, "unknown_component")
        return web.json_response(found)

    async def repair(request: web.Request) -> web.Response:
        """Reparieren — auf EINER Komponente, benannt im Pfad.

        Dieselbe Regel wie bei den Aufgaben: die Kennung steht im Pfad, nicht in
        einer Liste. Eine Aktualisierung zwischen Tippen und Senden kann die
        Handlung nicht auf etwas anderes umlenken.
        """
        if await guard(request) is None:
            return _err(401, "unauthorized")
        component = request.match_info["component"]
        result = await _center(request).repair(component)
        log.info("control.repair_requested", component=component[:24],
                 ok=bool(result.get("ok")))
        if not result.get("ok") and result.get("grund") in ("kein_arzt",
                                                            "kein_vorgehen"):
            return web.json_response(result, status=409)
        return web.json_response(result)

    # -- Handeln --------------------------------------------------------------

    async def _action(request: web.Request, name: str) -> web.Response:
        """Eine Handlung des Besitzers auf einer EXAKTEN Kennung.

        Die Kennung steht im Pfad, nicht in einer Liste und nicht in einem Index.
        Zwischen dem Tippen und dem Ausfuehren kann sich die Liste beliebig
        veraendern — die Handlung trifft trotzdem genau das, was auf dem Schirm
        stand.
        """
        if await guard(request) is None:
            return _err(401, "unauthorized")
        center = _center(request)
        target = request.match_info.get("task_id") or request.match_info.get(
            "item_id", "")
        handler = {"pause": center.pause, "resume": center.resume,
                   "run_now": center.run_now, "delete": center.delete,
                   "mark_read": center.mark_read}[name]
        result = await handler(target)
        if not result.get("ok"):
            reason = str(result.get("grund", "abgelehnt"))
            status = 404 if reason == "unbekannte_aufgabe" else 409
            return web.json_response({"error": reason}, status=status)
        log.info("control.action", action=name, target=target[:24])
        return web.json_response(result)

    app.add_routes([
        web.get(f"{PREFIX}/overview", overview),
        web.get(f"{PREFIX}/tasks", tasks),
        web.get(f"{PREFIX}/tasks/{{task_id}}", task),
        web.get(f"{PREFIX}/inbox", inbox),
        web.get(f"{PREFIX}/inbox/{{item_id}}", item),
        web.get(f"{PREFIX}/activity", activity),
        web.get(f"{PREFIX}/system", system),
        web.get(f"{PREFIX}/running", running),
        web.get(f"{PREFIX}/diagnoses", diagnoses),
        web.get(f"{PREFIX}/system/{{component}}/diagnose", diagnose),
        web.post(f"{PREFIX}/system/{{component}}/repair", repair),
        web.post(f"{PREFIX}/tasks/{{task_id}}/pause",
                 lambda r: _action(r, "pause")),
        web.post(f"{PREFIX}/tasks/{{task_id}}/resume",
                 lambda r: _action(r, "resume")),
        web.post(f"{PREFIX}/tasks/{{task_id}}/run_now",
                 lambda r: _action(r, "run_now")),
        web.post(f"{PREFIX}/tasks/{{task_id}}/delete",
                 lambda r: _action(r, "delete")),
        web.post(f"{PREFIX}/inbox/{{item_id}}/read",
                 lambda r: _action(r, "mark_read")),
    ])
    log.info("control.routes_attached", prefix=PREFIX, count=16)
    return app
