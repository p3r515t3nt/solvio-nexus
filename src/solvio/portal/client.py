"""Die Core-Seite der Naht.

Hier liegt alles, was Autoritaet hat: der Tresor, die Bindungen, die Freigabe.
Der Arbeiter jenseits des Sockets bekommt daraus immer nur das eine Stueck, das
er fuer den einen freigegebenen Schritt braucht.

Die Reihenfolge ist der ganze Inhalt dieses Moduls, und sie ist nicht
verhandelbar:

    Bindung nachschlagen (konfiguriert, nicht von der Seite)
      -> Seite befragen, OHNE etwas einzutragen
        -> Manifest bauen, mit Alias statt Geheimnis
          -> Freigabe am iPhone, ueber den eingefrorenen Pfad
            -> Geheimnis aus dem Tresor holen
              -> ueber den Socket, im Augenblick der Verwendung
                -> ausfuehren, wenn die Seite noch passt

Was daran auffaellt: das Geheimnis kommt **zuletzt**. Es wird geholt, nachdem ein
Mensch zugestimmt hat, und es geht direkt an den Arbeiter. Es steht in keinem
Werkzeugargument, in keinem Prompt, in keinem Freigabetext und in keinem
Protokoll — nicht weil es dort herausgefiltert wuerde, sondern weil es diese
Wege nie nimmt.
"""
from __future__ import annotations

import asyncio
import os
import socket
from typing import Any

from solvio.logging_setup import get_logger
from solvio.portal import protocol as P
from solvio.portal.build import BUILD_UNKNOWN, expected_build
from solvio.portal.binding import PortalBinding
from solvio.portal.manifest import ActionManifest

log = get_logger("portal")

#: Wo der Arbeiter lauscht. Sein Zuhause, nicht das des Core.
DEFAULT_SOCKET = "/var/solvio-portal/run/portal.sock"

CALL_TIMEOUT = 120.0

#: Der Quellbaum, gegen den der Arbeiter geprueft wird.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))


class WorkerBuildMismatch(RuntimeError):
    """Der Arbeiter laeuft aus anderem Code als der Core erwartet.

    Veraltet, manipuliert oder unvollstaendig ausgeliefert — welcher der drei
    Faelle es ist, aendert nichts an der Antwort. Eine angemeldete Sitzung auf
    Code zu beginnen, von dem der Core nicht weiss, was er tut, ist genau das,
    was diese Pruefung verhindert.
    """

    def __init__(self, expected: str, actual: str) -> None:
        super().__init__("worker build mismatch")
        self.expected = expected
        self.actual = actual


class PortalUnavailable(RuntimeError):
    """Kein Arbeiter erreichbar — dann gibt es keine angemeldeten Vorgaenge."""


class PortalClient:
    """Ein Draht zum Arbeiter. Kurz, synchron gerahmt, asynchron gefahren."""

    def __init__(self, socket_path: str = DEFAULT_SOCKET, *,
                 repo_root: str = "") -> None:
        self.socket_path = socket_path
        self.worker_uid: int | None = None
        self.worker_build: str = ""
        self.repo_root = repo_root or REPO_ROOT

    def available(self) -> bool:
        return os.path.exists(self.socket_path)

    async def call(self, message: dict[str, Any], *,
                   timeout: float = CALL_TIMEOUT) -> dict[str, Any]:
        """Eine Anfrage, eine Antwort. Der Socket lebt nicht laenger als noetig."""
        loop = asyncio.get_running_loop()

        def exchange() -> dict[str, Any]:
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.settimeout(timeout)
            try:
                connection.connect(self.socket_path)
            except OSError as exc:
                raise PortalUnavailable(f"cannot reach the portal worker: {exc.errno}") from exc
            try:
                connection.sendall(P.encode(message))
                return P.decode(connection)
            finally:
                connection.close()

        try:
            return await asyncio.wait_for(loop.run_in_executor(None, exchange),
                                          timeout=timeout + 5)
        except (TimeoutError, asyncio.TimeoutError) as exc:
            # Eine ausgebliebene Antwort heisst NICHT „nichts ist passiert".
            # Wer das annimmt, wiederholt einen Schreibvorgang, der schon wirkte.
            raise AmbiguousPortalOutcome("no answer from the portal worker") from exc

    # -- Betrieb -------------------------------------------------------------
    async def ping(self) -> dict[str, Any]:
        reply = await self.call({"op": P.PING}, timeout=10)
        if reply.get("ok"):
            self.worker_uid = int(reply.get("uid", -1))
            self.worker_build = str(reply.get("build", BUILD_UNKNOWN))
        return reply

    async def verify_build(self) -> str:
        """Der Handschlag. Wirft, wenn der Arbeiter anderen Code faehrt."""
        reply = await self.ping()
        if reply.get("ok") is not True:
            raise PortalUnavailable("portal_build_unconfirmed")
        expected = expected_build(self.repo_root)
        if self.worker_build != expected:
            log.error("portal.worker_build_mismatch",
                      expected=expected[:12], actual=(self.worker_build or "?")[:12])
            raise WorkerBuildMismatch(expected, self.worker_build)
        return expected

    async def open_session(self, binding: PortalBinding, *, owner_principal: str = "", connection_ref="") -> str:
        # Vor JEDER Sitzung, nicht einmal beim Start: ein Arbeiter kann zwischen
        # zwei Sitzungen neu gestartet worden sein, und dann faehrt er
        # moeglicherweise etwas anderes als vorhin.
        await self.verify_build()
        message = {"op": P.OPEN_SESSION, "binding": binding.as_data(), "owner_principal": owner_principal}
        if connection_ref:
            message["connection_ref"] = connection_ref
        reply = await self.call(message)
        if not reply.get("ok"):
            raise PortalUnavailable(str(reply.get("reason", "open_failed")))
        return str(reply["session_id"])

    async def open_status(self, connection_ref: str, *, owner_principal: str) -> dict[str, Any]:
        return await self.call({"op": P.OPEN_STATUS, "connection_ref": connection_ref,
                               "owner_principal": owner_principal})

    async def open_connection(self, binding, *, owner_principal, connection_ref):
        """Task adapter verifies build/authority immediately before this call."""
        reply = await self.call({'op': P.OPEN_SESSION, 'binding': binding.as_data(),
            'owner_principal': owner_principal, 'connection_ref': connection_ref})
        if not reply.get('ok'):
            raise PortalUnavailable(str(reply.get('reason', 'open_unconfirmed')))
        return str(reply['session_id'])

    async def navigate(self, session_id: str, url: str, *, owner_principal=None) -> dict[str, Any]:
        message = {"op": P.NAVIGATE, "session_id": session_id, "url": url}
        if owner_principal is not None:
            message["owner_principal"] = owner_principal
        return await self.call(message)

    async def read(self, session_id: str, *, structured: bool = False,
                   owner_principal: str | None = None) -> dict[str, Any]:
        message = {"op": P.READ, "session_id": session_id, "structured": bool(structured)}
        if owner_principal is not None:
            message["owner_principal"] = owner_principal
        return await self.call(message)

    async def list_sessions(self, owner_principal: str, *, limit: int = 50) -> dict[str, Any]:
        """Fresh metadata only; no navigation, login or idle refresh."""
        build = await self.verify_build()
        reply = await self.call({"op": P.LIST_SESSIONS, "owner_principal": owner_principal,
                                 "limit": limit}, timeout=30)
        if not reply.get("ok") or reply.get("build") != build:
            raise PortalUnavailable("portal_sessions_unavailable")
        await self.verify_build()
        return reply

    async def probe(self, session_id: str, *, owner_principal=None) -> dict[str, Any]:
        """Die Seite beschreiben, ohne sie anzufassen."""
        message = {"op": P.PROBE, "session_id": session_id}
        if owner_principal is not None:
            message["owner_principal"] = owner_principal
        return await self.call(message)

    async def execute(self, session_id: str, manifest: ActionManifest, *,
                      secrets: dict[str, str] | None = None,
                      action_id: str = "", owner_principal=None) -> dict[str, Any]:
        """Fuehrt die freigegebene Aktion aus.

        `secrets` ist eine Abbildung von Alias auf Wert und existiert genau fuer
        die Dauer dieses Aufrufs. Sie wird nicht protokolliert, nicht
        zwischengespeichert und nicht zurueckgegeben.
        """
        message = {"op": P.EXECUTE, "session_id": session_id,
                                "manifest": _manifest_data(manifest),
                                "secrets": dict(secrets or {}),
                                "action_id": action_id}
        if owner_principal is not None:
            message["owner_principal"] = owner_principal
        return await self.call(message)

    async def restart(self) -> dict[str, Any]:
        """Bittet den Arbeiter, sich zu beenden — launchd startet ihn neu."""
        try:
            reply = await self.call({"op": P.RESTART}, timeout=20)
        except (PortalUnavailable, AmbiguousPortalOutcome):
            # Er beendet sich waehrend der Antwort; ein abgerissener Draht ist
            # hier das erwartete Ergebnis, kein Fehler.
            return {"ok": True, "restarting": True}
        if not reply.get("ok"):
            # Ein Arbeiter, der den Befehl nicht kennt, ist ZU alt, um sich selbst
            # abzuloesen. Das ehrlich zu sagen ist wichtiger als es zu verdecken:
            # dieser eine Fall braucht einen Neustart von aussen.
            log.warning("portal.restart_refused", reason=str(reply.get("reason", ""))[:40])
        return reply

    async def wait_for_build(self, expected: str, *, tries: int = 40) -> bool:
        """Wartet, bis der neu gestartete Arbeiter den erwarteten Bau meldet."""
        for _ in range(tries):
            try:
                await self.ping()
                if self.worker_build == expected:
                    return True
            except (PortalUnavailable, AmbiguousPortalOutcome):
                pass
            await asyncio.sleep(0.5)
        return False

    async def close_session(self, session_id: str, *, owner_principal=None) -> dict[str, Any]:
        message = {"op": P.CLOSE_SESSION, "session_id": session_id}
        if owner_principal is not None:
            message["owner_principal"] = owner_principal
        return await self.call(message, timeout=30)


class AmbiguousPortalOutcome(RuntimeError):
    """Der Ausgang ist unbekannt. Nicht wiederholen — klaeren lassen."""


def _manifest_data(manifest: ActionManifest) -> dict[str, Any]:
    return {"portal_id": manifest.portal_id, "origin": manifest.origin,
            "page_url": manifest.page_url, "action_type": manifest.action_type,
            "target": manifest.target, "method": manifest.method,
            "page_signature": manifest.page_signature,
            "credential_alias": manifest.credential_alias,
            "version": manifest.version, "principal": manifest.principal,
            "fields": [{"name": f.name, "selector": f.selector,
                        "value": f.value, "alias": f.alias} for f in manifest.fields]}
