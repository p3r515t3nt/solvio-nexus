"""Gemeinsamer Unterbau der beiden Broker-Suiten.

Ein **falscher Anbieter** auf der Rueckschleife, damit die Tests die echte
Drahtform pruefen koennen, ohne je `api.openai.com` zu beruehren. Das Ziel des
Brokers ist im Code gepinnt; genau deshalb wird die Konstante hier fuer die
Dauer eines Tests umgebogen und danach zurueckgesetzt — in der Produktion gibt
es keinen Weg, der das koennte, und das ist der Punkt.

Der falsche Anbieter zeichnet auf, was bei ihm ankommt: Pfad, Kopfsaetze und
Rumpf. Ohne diese Aufzeichnung liessen sich die Aussagen „der eingehende
`Authorization` reist nie mit" und „der Pfad wird byte-gleich weitergereicht"
nicht belegen, sondern nur behaupten.
"""
from __future__ import annotations

import asyncio
import os
import socket
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

#: Ein erfundener Wert, der wie ein Anbieterschluessel aussieht. Er ist frei
#: erfunden und oeffnet nichts; er existiert, damit ein Test nach genau dieser
#: Zeichenkette suchen kann.
FAKE_PROVIDER_KEY = "sk-test-PROVIDER-KEY-must-never-leave-upstream"

#: Ein vollstaendiger Responses-Strom mit einem Werkzeugaufruf. Er ist die
#: Probe auf die einzige Eigenschaft, die zaehlt: der Verbraucher im Kaefig baut
#: seine Ausgabe aus den `response.output_item.done`-Ereignissen und liest
#: `response.output` aus dem Abschlussereignis nie. Wer diesen Strom uebersetzt,
#: verliert den Werkzeugaufruf still.
TOOL_CALL_STREAM = (
    b'event: response.output_item.added\n'
    b'data: {"type":"response.output_item.added","item":{"type":"function_call"}}\n\n'
    b'event: response.function_call_arguments.delta\n'
    b'data: {"type":"response.function_call_arguments.delta","delta":"{\\"q\\":"}\n\n'
    b'event: response.output_item.done\n'
    b'data: {"type":"response.output_item.done","item":{"type":"function_call",'
    b'"name":"web_search","arguments":"{\\"q\\":\\"solvio\\"}"}}\n\n'
    b'event: response.completed\n'
    b'data: {"type":"response.completed","response":{"id":"resp_1","status":"completed",'
    b'"usage":{"input_tokens":1200,"output_tokens":340}}}\n\n'
)

TEXT_STREAM = (
    b'event: response.output_text.delta\n'
    b'data: {"type":"response.output_text.delta","delta":"Hallo"}\n\n'
    b'event: response.reasoning_summary_text.delta\n'
    b'data: {"type":"response.reasoning_summary_text.delta","delta":"denke"}\n\n'
    b'event: response.output_item.done\n'
    b'data: {"type":"response.output_item.done","item":{"type":"message"}}\n\n'
    b'event: response.completed\n'
    b'data: {"type":"response.completed","response":{"id":"resp_2","status":"completed",'
    b'"usage":{"input_tokens":10,"output_tokens":5}}}\n\n'
)

#: Chat-Completions meldet `usage` nur, wenn der Kaefig `include_usage` in den
#: Rumpf schreibt — und den Rumpf baut der Kaefig, nicht der Broker.
CHAT_STREAM = (
    b'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\n'
    b'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":3}}\n\n'
    b'data: [DONE]\n\n'
)

#: Ein Strom, der VOR dem Abschlussereignis abreisst. Er ist der Beleg dafuer,
#: dass die Vorbelastung stehen bleibt, wenn nie ein `usage` kommt.
TRUNCATED_STREAM = (
    b'event: response.output_text.delta\n'
    b'data: {"type":"response.output_text.delta","delta":"halb"}\n\n'
)


def free_port() -> int:
    handle = socket.socket()
    handle.bind(("127.0.0.1", 0))
    port = int(handle.getsockname()[1])
    handle.close()
    return port


class FakeUpstream:
    """Ein Anbieter, der nur aufschreibt, was bei ihm ankommt."""

    def __init__(self, *, body: bytes = TOOL_CALL_STREAM, status: int = 200,
                 location: str = "", chunk_pause: float = 0.0,
                 abort_mid_stream: bool = False) -> None:
        self.body = body
        self.status = status
        self.location = location
        #: Eine Pause zwischen den Stromstuecken. Nur so laesst sich ein Lease
        #: schliessen, WAEHREND eine Weiterleitung noch laeuft.
        self.chunk_pause = chunk_pause
        #: Die Verbindung reisst MITTEN im Strom ab — kein `write_eof`, kein
        #: sauberer Abschluss. Der Unterschied zu einem kurzen Rumpf ist
        #: entscheidend: ein kurzer Rumpf, der ordentlich endet, IST eine
        #: gelieferte Antwort; ein Abriss ist keine. Genau daran haengt, ob
        #: eine Wiederholung ein legitimer Transport-Retry oder ein
        #: Doppelgaenger ist.
        self.abort_mid_stream = abort_mid_stream
        self.calls: list[dict] = []
        self._runner = None
        self.origin = ""

    async def start(self) -> str:
        from aiohttp import web

        async def handle(request):
            raw = await request.read()
            self.calls.append({
                "path": request.rel_url.raw_path,
                "query": request.rel_url.query_string,
                "method": request.method,
                "headers": {k.lower(): v for k, v in request.headers.items()},
                "body": raw,
            })
            if self.location:
                return web.Response(status=self.status,
                                    headers={"Location": self.location})
            response = web.StreamResponse(
                status=self.status,
                headers={"Content-Type": "text/event-stream",
                         "Set-Cookie": "upstream=leak"})
            await response.prepare(request)
            if self.abort_mid_stream:
                await response.write(self.body[:len(self.body) // 2])
                # Kein `write_eof`: die Verbindung faellt mitten im Stueck weg.
                # Der Leser auf der anderen Seite bekommt einen Nutzlastfehler.
                request.transport.abort()
                return response
            if self.chunk_pause:
                for piece in self.body.split(b"\n\n"):
                    if not piece:
                        continue
                    await response.write(piece + b"\n\n")
                    await asyncio.sleep(self.chunk_pause)
            else:
                await response.write(self.body)
            await response.write_eof()
            return response

        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", handle)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        port = free_port()
        site = web.TCPSite(self._runner, "127.0.0.1", port)
        await site.start()
        self.origin = f"http://127.0.0.1:{port}"
        return self.origin

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None


class Harness:
    """Broker plus falscher Anbieter, sauber wieder abgebaut."""

    def __init__(self, *, body: bytes = TOOL_CALL_STREAM, status: int = 200,
                 location: str = "", clock=None, chunk_pause: float = 0.0,
                 abort_mid_stream: bool = False) -> None:
        self.upstream = FakeUpstream(body=body, status=status, location=location,
                                     chunk_pause=chunk_pause,
                                     abort_mid_stream=abort_mid_stream)
        self.broker = None
        self.base = ""
        self._pinned = ""
        self._clock = clock
        self._db = ""
        self._previous_db: str | None = None

    async def __aenter__(self):
        from solvio.provider_broker import upstream as up
        from solvio.provider_broker.service import BrokerService

        origin = await self.upstream.start()
        self._pinned = up.UPSTREAM_ORIGIN
        up.UPSTREAM_ORIGIN = origin

        self._db = os.path.join(tempfile.mkdtemp(), "broker.sqlite3")
        # Wiederhergestellt, nicht geloescht: ohne Variable faellt das naechste
        # Buch dieser Suite auf `~/.solvio/broker.sqlite3` (DEBT-0223).
        self._previous_db = os.environ.get("SOLVIO_BROKER_DB")
        os.environ["SOLVIO_BROKER_DB"] = self._db
        self.broker = BrokerService(provider_key=FAKE_PROVIDER_KEY,
                                    port=free_port(), clock=self._clock)
        await self.broker.start()
        self.base = f"http://127.0.0.1:{self.broker.port}"
        return self

    async def __aexit__(self, *exc):
        from solvio.provider_broker import upstream as up

        if self.broker is not None:
            await self.broker.stop()
        await self.upstream.stop()
        up.UPSTREAM_ORIGIN = self._pinned
        if self._previous_db is None:
            os.environ.pop("SOLVIO_BROKER_DB", None)
        else:
            os.environ["SOLVIO_BROKER_DB"] = self._previous_db
        return False

    async def call(self, token: str, *, path: str = "/v1/responses",
                   method: str = "POST", body: bytes | None = None,
                   headers: dict | None = None) -> tuple[int, bytes]:
        import aiohttp

        payload = body if body is not None else responses_body()
        sent = {"Content-Type": "application/json"}
        if token:
            sent["Authorization"] = f"Bearer {token}"
        sent.update(headers or {})
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(method, self.base + path,
                                       data=payload if method == "POST" else None,
                                       headers=sent) as response:
                return response.status, await response.read()

    async def call_full(self, token: str, *, path: str = "/v1/responses",
                        method: str = "POST", body: bytes | None = None,
                        headers: dict | None = None) -> tuple[int, dict, bytes]:
        """Wie `call`, aber mit den Antwortkopfsaetzen.

        `Retry-After` ist eine Zusage an den Client, und eine Zusage laesst
        sich nur pruefen, wenn ein Test sie sehen kann.
        """
        import aiohttp

        payload = body if body is not None else responses_body()
        sent = {"Content-Type": "application/json"}
        if token:
            sent["Authorization"] = f"Bearer {token}"
        sent.update(headers or {})
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(method, self.base + path,
                                       data=payload if method == "POST" else None,
                                       headers=sent) as response:
                return (response.status, dict(response.headers),
                        await response.read())

    def rows(self, limit: int = 200) -> list[dict]:
        return self.broker.ledger.rows(limit=limit)


def responses_body(model: str = "gpt-5.4-mini", **extra) -> bytes:
    import json

    payload = {"model": model, "input": "eine Frage"}
    payload.update(extra)
    return json.dumps(payload).encode("utf-8")


def run(coro):
    return asyncio.run(coro)


def bot_principal_name(profile: str) -> str:
    from solvio.provider_broker.service import bot_principal

    return bot_principal(profile)
