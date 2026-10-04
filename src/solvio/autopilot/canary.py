"""Der CLI-Kanarienvogel — der Dauertest, der `--bare` beim Wort nimmt.

Die ganze V0.6-Sicherheitsaussage haengt an einer gemessenen Eigenschaft eines
**fremden** Programms: `claude --bare` liest weder Schluesselbund noch OAuth
und sendet ausschliesslich die Anmeldung, die man ihm gibt. Gemessen am
2026-09-02 an CLI 2.1.222 — und die CLI aktualisiert sich selbst.

Eine Zusicherung, die diese Eigenschaft nur EINMAL geprueft hat, ist deshalb
keine Zusicherung, sondern ein Datum. Der Kanarienvogel wiederholt die Messung
bei jedem Gate-Lauf gegen einen lokalen Lauscher mit einem Wegwerf-Wert:

* Im Draht steht AUSSCHLIESSLICH der Wegwerf-Wert.
* Kein zweiter Anmeldekopf, kein OAuth, kein `sk-ant-oat`.
* Ohne Wert faellt die CLI geschlossen aus, statt auf das Abo zurueckzufallen.

**Rot heisst gesperrt, nicht gewarnt** (Vertrag §5). `verdict()` gibt genau
das zurueck, was `ClaudeWriterBuilder.blocked_reason()` daraus macht.

Er laeuft ohne echte Anmeldung und ohne Netz nach draussen: der Lauscher
bindet auf der Rueckschleife, und alles, was der Kanarienvogel prueft, sieht er
an seinen eigenen Anfragen.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer

from solvio.logging_setup import get_logger

log = get_logger("autopilot")

#: Der Wegwerf-Wert. Er sieht aus wie eine Anmeldung und ist keine — er oeffnet
#: nichts ausser diesem Lauscher.
PROBE_TOKEN = "solvio-canary-not-a-real-credential-0001"

#: Formen, die eine ECHTE Anmeldung haette. Taucht eine davon im Draht auf,
#: ist der Kanarienvogel rot — gleich, wie sie dorthin kam.
LEAK_MARKERS = ("sk-ant-", "sk-ant-oat", "oauth", "Bearer sk-", "claude.ai")

#: Koepfe, in denen eine Anmeldung reisen kann.
AUTH_HEADERS = ("authorization", "x-api-key", "proxy-authorization", "cookie")

CANARY_TIMEOUT = 90.0


@dataclass
class CanaryResult:
    """Was der Lauf gesehen hat. Kein Urteil ohne Beleg."""

    ok: bool
    reason: str = ""
    requests: int = 0
    #: Die beobachteten Anmeldewerte, maskiert. Nie der Rohwert.
    seen_auth: tuple[str, ...] = ()
    detail: str = ""
    fields: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "reason": self.reason, "requests": self.requests,
                "seen_auth": list(self.seen_auth), "detail": self.detail[:400]}


class _Listener(BaseHTTPRequestHandler):
    """Gibt sich als Anbieter aus und schreibt jede Anfrage mit."""

    seen: list[dict[str, str]] = []

    def log_message(self, *args) -> None:      # noqa: D102 - kein stderr-Rauschen
        pass

    def do_POST(self) -> None:                 # noqa: N802 - BaseHTTPRequestHandler
        laenge = int(self.headers.get("Content-Length", 0) or 0)
        try:
            self.rfile.read(laenge)
        except Exception:                      # noqa: BLE001
            pass
        eintrag = {"path": self.path}
        for name, wert in self.headers.items():
            if name.lower() in AUTH_HEADERS:
                eintrag[name.lower()] = wert
        type(self).seen.append(eintrag)
        antwort = json.dumps({
            "id": "msg_canary", "type": "message", "role": "assistant",
            "model": "canary",
            "content": [{"type": "text", "text": "KANARIENVOGEL"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(antwort)))
        self.end_headers()
        self.wfile.write(antwort)


def _mask(wert: str) -> str:
    """Ein Anmeldewert wird nie roh gemeldet — auch kein gefundener."""
    wert = (wert or "").strip()
    if not wert:
        return ""
    if wert.endswith(PROBE_TOKEN) or wert == PROBE_TOKEN:
        return "PROBE_TOKEN"
    return f"{wert[:8]}…(len={len(wert)})"


def _claude_binary() -> str:
    from solvio.specialists.launcher import LauncherError, resolve
    try:
        return resolve("claude")
    except LauncherError:
        return ""


def run(*, timeout: float = CANARY_TIMEOUT) -> CanaryResult:
    """Ein Lauf. Ohne CLI ist er **nicht gruen** — er ist ergebnislos.

    Die Unterscheidung ist wichtig: „keine CLI" heisst nicht „sicher", es
    heisst „ungemessen". Der Aufrufer entscheidet, ob ihn das sperrt; hier wird
    nichts beschoenigt.
    """
    binaer = _claude_binary()
    if not binaer:
        return CanaryResult(False, "cli_missing",
                            detail="claude nicht auffindbar — ungemessen, "
                                   "nicht sicher")

    _Listener.seen = []
    server = HTTPServer(("127.0.0.1", 0), _Listener)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    arbeit = tempfile.mkdtemp(prefix="solvio-canary-")
    try:
        umgebung = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", arbeit),
            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
            "ANTHROPIC_API_KEY": PROBE_TOKEN,
        }
        argv = [binaer, "--bare", "-p", "--no-session-persistence",
                "--model", "claude-sonnet-5", "Sag KANARIENVOGEL."]
        try:
            proc = subprocess.run(argv, cwd=arbeit, env=umgebung,
                                  stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True,
                                  timeout=timeout)
        except subprocess.TimeoutExpired:
            return CanaryResult(False, "cli_timeout",
                                requests=len(_Listener.seen))
        except OSError as exc:
            return CanaryResult(False, "cli_unstartable",
                                detail=type(exc).__name__)

        gesehen = list(_Listener.seen)
        werte: list[str] = []
        for eintrag in gesehen:
            for name in AUTH_HEADERS:
                if eintrag.get(name):
                    werte.append(eintrag[name])

        if not gesehen:
            # Kein Verkehr am Lauscher: entweder ist `--bare` kaputt, oder das
            # CLI ist woanders hingegangen. Beides ist rot.
            return CanaryResult(
                False, "no_traffic", requests=0,
                detail=(proc.stdout or proc.stderr or "")[:300])

        maskiert = tuple(sorted({_mask(w) for w in werte}))
        fremd = [w for w in werte
                 if PROBE_TOKEN not in w]
        if fremd:
            return CanaryResult(False, "foreign_credential",
                                requests=len(gesehen), seen_auth=maskiert,
                                detail="ein Wert im Draht war nicht der "
                                       "Wegwerf-Wert")
        verdaechtig = [m for m in LEAK_MARKERS
                       for w in werte if m.lower() in w.lower()]
        if verdaechtig:
            return CanaryResult(False, "credential_shape",
                                requests=len(gesehen), seen_auth=maskiert,
                                detail=f"Form einer echten Anmeldung: "
                                       f"{sorted(set(verdaechtig))}")
        return CanaryResult(True, "", requests=len(gesehen),
                            seen_auth=maskiert)
    finally:
        server.shutdown()
        server.server_close()
        import shutil
        shutil.rmtree(arbeit, ignore_errors=True)


def verdict(result: CanaryResult) -> str:
    """Leer heisst frei. Sonst der Sperrgrund, wortgleich fuer den Adapter."""
    if result.ok:
        return ""
    return f"cli_canary_failed:{result.reason}"


# =====================================================================
# N8/C4 — der Worker-Kanarienvogel: die Fabrik-argv im echten Kaefig
# =====================================================================
#
# Der V0.6-Kanarienvogel oben (M-C1) faehrt eine EIGENE, fest verdrahtete argv
# im Owner-HOME ohne Seatbelt. Er beweist die `--bare`-Eigenschaft der Binary,
# nicht die Worker-argv. Die Proben hier fahren exakt die argv aus
# `claude_native_task.claude_worker_invocation` im gerenderten Worker-Profil
# unter `sandbox-exec`, gegen einen gescripteten Lauscher, der das Modell
# spielt (`tool_use`-Bloecke hinein, `tool_result`-Ruecklaeufe heraus) — ohne
# Modell, ohne Anmeldung, ohne Schluesselbund. Autopilot-Verhalten und
# V0.6-argv bleiben unveraendert.

#: Ein Dienstname, den es im Schluesselbund garantiert nicht gibt: so laesst
#: sich pruefen, ob `security` ueberhaupt STARTEN darf — ohne je einen echten
#: Eintrag zu beruehren (dieselbe Auflage wie in den Isolationstests).
SYNTHETIC_KEYCHAIN_SERVICE = "SOLVIO-CANARY-nicht-vorhanden-0000"
KERNEL_DENIED = "Operation not permitted"
WORKER_PROBE_TIMEOUT = 120.0
#: Gemessen am 2026-09-18: die CLI wiederholt 429/401 zehnmal (~180 s), dann
#: endet sie selbst. Die Proben M-C5b/M-C7 brauchen mehr Frist als das.
RETRY_PROBE_TIMEOUT = 420.0
PROBE_NAMES = ("M-C2", "M-C2-noallow", "M-C3", "M-C4", "M-C5", "M-C5b", "M-C7", "M-C8", "M-C9")


@dataclass
class WorkerProbe:
    """Ergebnis einer Worker-Probe: Booleans, Zaehler, Exit-Codes, Hashes.
    Nie ein Token, nie ein Rohwert aus dem Draht."""

    name: str
    ok: bool
    reason: str = ""
    requests: int = 0
    side_requests: int = 0
    seen_auth: tuple[str, ...] = ()
    exit_code: int | None = None
    elapsed: float = 0.0
    checks: dict[str, object] = field(default_factory=dict)
    fields: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {"name": self.name, "ok": self.ok, "reason": self.reason,
                "requests": self.requests, "side_requests": self.side_requests,
                "seen_auth": list(self.seen_auth), "exit_code": self.exit_code,
                "elapsed_s": round(self.elapsed, 2), "checks": dict(self.checks),
                "fields": dict(self.fields)}


def worker_verdict(result: WorkerProbe) -> str:
    """Leer heisst frei. Sonst der Sperrgrund — rot heisst gesperrt, nicht gewarnt."""
    if result.ok:
        return ""
    return f"worker_canary_failed:{result.name}:{result.reason}"


def _is_side_request(body: dict) -> bool:
    """Die CLI stellt neben dem Turn eigene Anfragen (Titelbildung: kein
    `tools`-Feld, `<session>`-Umschlag). Sie gehoeren nicht zum Skript."""
    messages = body.get("messages") or []
    if body.get("tools") or not messages:
        return False
    first = messages[0].get("content") if isinstance(messages[0], dict) else None
    text = ""
    if isinstance(first, list) and first and isinstance(first[0], dict):
        text = str(first[0].get("text", ""))
    elif isinstance(first, str):
        text = first
    return "Write the title" in text or "<session>" in text


def _sse(message_id: str, content: list[dict], stop: str) -> bytes:
    head = {"id": message_id, "type": "message", "role": "assistant", "model": "claude-sonnet-5",
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 0}}
    events = [("message_start", {"type": "message_start", "message": head})]
    for index, block in enumerate(content):
        if block["type"] == "text":
            events.append(("content_block_start", {"type": "content_block_start", "index": index,
                                                   "content_block": {"type": "text", "text": ""}}))
            events.append(("content_block_delta", {"type": "content_block_delta", "index": index,
                                                   "delta": {"type": "text_delta", "text": block["text"]}}))
        else:
            events.append(("content_block_start", {"type": "content_block_start", "index": index,
                "content_block": {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}}))
            events.append(("content_block_delta", {"type": "content_block_delta", "index": index,
                "delta": {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}}))
        events.append(("content_block_stop", {"type": "content_block_stop", "index": index}))
    events.append(("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                                     "usage": {"output_tokens": 5}}))
    events.append(("message_stop", {"type": "message_stop"}))
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events).encode()


class ScriptedListener:
    """Spielt den Anbieter: gescriptete Antworten je Hauptanfrage, 429/401-
    Skripte, Stillstand — und schreibt jede Anfrage mit (Koepfe maskiert)."""

    def __init__(self, *, script: list[list[dict]], status_for=None, stall: bool = False):
        self.script = [list(step) for step in script]
        self.status_for = status_for
        self.stall = stall
        self.seen: list[dict] = []
        self.tool_results: dict[str, dict] = {}
        self.main = 0
        self.total = 0
        self.stop_event = threading.Event()
        self._lock = threading.Lock()
        listener = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:  # noqa: D102
                pass

            def do_POST(self) -> None:             # noqa: N802
                listener._handle(self)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> "ScriptedListener":
        self._thread.start()
        return self

    def stop(self) -> None:
        self.stop_event.set()
        self.server.shutdown()
        self.server.server_close()

    def _handle(self, handler) -> None:
        length = int(handler.headers.get("Content-Length", 0) or 0)
        try:
            body = json.loads(handler.rfile.read(length) or b"{}")
        except Exception:                      # noqa: BLE001
            body = {}
        if type(body) is not dict:
            body = {}
        side = _is_side_request(body)
        with self._lock:
            self.total += 1
            index = -1
            if not side:
                index = self.main
                self.main += 1
            entry = {"path": handler.path, "side": side, "stream": body.get("stream") is True,
                     "tools": [t.get("name") for t in body.get("tools") or [] if isinstance(t, dict)],
                     "messages": len(body.get("messages") or []),
                     "roles": [m.get("role") for m in body.get("messages") or [] if isinstance(m, dict)],
                     "tool_use_ids": [], "at": time.time()}
            for name, value in handler.headers.items():
                if name.lower() in AUTH_HEADERS:
                    entry[name.lower()] = value
            for message in body.get("messages") or []:
                content = message.get("content") if isinstance(message, dict) else None
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        entry["tool_use_ids"].append(str(block.get("id")))
                    elif block.get("type") == "tool_result":
                        raw = block.get("content")
                        text = raw if isinstance(raw, str) else "\n".join(
                            str(i.get("text", "")) for i in raw if isinstance(i, dict)) if isinstance(raw, list) else ""
                        self.tool_results[str(block.get("tool_use_id"))] = {
                            "is_error": block.get("is_error") is True,
                            "chars": len(text),
                            "sha256": hashlib.sha256(text.encode()).hexdigest(),
                            # zsh meldet „operation not permitted: security" (klein),
                            # ls/cat „Operation not permitted" — gemessen 2026-09-18.
                            "kernel_denied": KERNEL_DENIED.lower() in text.lower(),
                            # Nur eine Frage an den Text, nie der Text selbst.
                            "excerpt": _mask_excerpt(text)}
            self.seen.append(entry)
        if self.stall:
            self.stop_event.wait(600)
            return
        status = 200
        if self.status_for is not None:
            status = int(self.status_for(index, side))
        if status != 200:
            kind = "rate_limit_error" if status == 429 else "authentication_error" if status == 401 else "api_error"
            payload = json.dumps({"type": "error", "error": {"type": kind, "message": "scripted"}}).encode()
            handler.send_response(status)
            handler.send_header("Content-Type", "application/json")
            if status == 429:
                handler.send_header("retry-after", "1")
            handler.send_header("Content-Length", str(len(payload)))
            handler.end_headers()
            handler.wfile.write(payload)
            return
        if side:
            content = [{"type": "text", "text": "Titel"}]
        else:
            content = self.script[index] if index < len(self.script) else [{"type": "text", "text": "FERTIG"}]
        stop = "tool_use" if any(c.get("type") == "tool_use" for c in content) else "end_turn"
        message_id = f"msg_canary_{self.total}"
        if body.get("stream") is True:
            payload, ctype = _sse(message_id, content, stop), "text/event-stream"
        else:
            payload = json.dumps({"id": message_id, "type": "message", "role": "assistant",
                                  "model": "claude-sonnet-5", "content": content, "stop_reason": stop,
                                  "stop_sequence": None,
                                  "usage": {"input_tokens": 10, "output_tokens": 5}}).encode()
            ctype = "application/json"
        handler.send_response(200)
        handler.send_header("Content-Type", ctype)
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)
        handler.wfile.flush()


def _mask_excerpt(text: str) -> str:
    from solvio.agent_runtime.specialists import redact_specialist_output
    from solvio.specialists.native_task_profile import _safe
    return _safe(redact_specialist_output(text or ""))[:160]


def _auth_values(seen: list[dict]) -> list[str]:
    values = []
    for entry in seen:
        for name in AUTH_HEADERS:
            if entry.get(name):
                values.append(entry[name])
    return values


def _wire_verdict(seen: list[dict], token: str) -> str:
    """Dieselben drei Fragen wie beim V0.6-Kanarienvogel — an den Worker-Draht."""
    if not seen:
        return "no_traffic"
    values = _auth_values(seen)
    if [w for w in values if token not in w]:
        return "foreign_credential"
    if [m for m in LEAK_MARKERS for w in values if m.lower() in w.lower()]:
        return "credential_shape"
    return ""


class _StubBridge:
    """Ein Stellvertreter der Core-Werkzeugbruecke fuer M-C4: derselbe Draht
    (`native_tool_wire`), ein Manifest, eine Antwort — und ein Protokoll, was
    der Adapter im Kaefig wirklich geschickt hat."""

    def __init__(self, sock_root: str, *, thread_id: str, turn_id: str):
        from solvio.specialists import native_tool_wire as W
        self.tools = [{"type": "function", "name": "portal_list",
                       "description": "Kanarienvogel-Manifest (kein Core).",
                       "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}}]
        self.digest = W.digest(self.tools)
        self.thread_id, self.turn_id = thread_id, turn_id
        self.seen: list[dict] = []
        leaf = os.path.join(sock_root, secrets.token_hex(2))
        os.makedirs(leaf, mode=0o700)
        self.endpoint = os.path.join(os.path.realpath(leaf), "core.sock")
        if len(os.fsencode(self.endpoint)) >= 104:
            raise ValueError("native_tool_socket_location")
        stub = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                chunks, size = [], 0
                while True:
                    chunk = self.request.recv(4096)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > W.MAX_BYTES:
                        return
                try:
                    request = W.decode(b"".join(chunks))
                except ValueError:
                    return
                method, body = request.get("method"), request.get("body")
                stub.seen.append({"method": method,
                                  "binding_ok": bool(type(body) is dict and body.get("threadId") == stub.thread_id
                                                     and body.get("turnId") == stub.turn_id)})
                if method == "manifest":
                    response = {"tools": stub.tools}
                elif method == "call" and type(body) is dict and body.get("tool") == "portal_list" \
                        and body.get("threadId") == stub.thread_id and body.get("turnId") == stub.turn_id:
                    response = {"success": True, "contentItems": [{"type": "inputText",
                                                                    "text": json.dumps({"portals": []})}]}
                else:
                    return
                self.request.sendall(W.encode(response))

        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True

        self.server = Server(self.endpoint, Handler)
        os.chmod(self.endpoint, 0o600)
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _private_dir(path: str) -> str:
    missing = []
    probe = os.path.abspath(path)
    while not os.path.exists(probe):
        missing.append(probe)
        probe = os.path.dirname(probe)
    for folder in reversed(missing):
        os.mkdir(folder, 0o700)
    os.chmod(path, 0o700)
    return os.path.realpath(path)


def _roots(workspace_root: str | None, jail_root: str | None) -> tuple[str, str]:
    if workspace_root is None or jail_root is None:
        from solvio.config import load_settings
        settings = load_settings()
        workspace_root = workspace_root or getattr(settings, "agent_runtime_task_workspace_root",
                                                   "~/.solvio-tasks/workspaces")
        jail_root = jail_root or getattr(settings, "agent_runtime_claude_jail_root",
                                         "~/.solvio-tasks/claude-jails")
    return (_private_dir(os.path.expanduser(workspace_root)),
            _private_dir(os.path.expanduser(jail_root)))


def _result_block() -> str:
    return json.dumps({"status": "completed", "text": {
        "findings": ["Kanarienvogel"], "evidence": [], "assumptions": [], "uncertainties": [],
        "rejected_alternatives": [], "risk_notes": [], "recommended_path": "keine",
        "confidence": "hoch", "files": []}, "sources": []})


def _bash(item: str, command: str) -> list[dict]:
    return [{"type": "tool_use", "id": f"toolu_{item}", "name": "Bash", "input": {"command": command}}]


def _pids_for(marker: str) -> list[int]:
    """PIDs, deren Kommandozeile den Marker traegt — per Prozessliste, nicht
    per `pgrep`-Begriff (der stuende in der eigenen Kommandozeile)."""
    try:
        out = subprocess.run(["/bin/ps", "-axo", "pid=,command="], capture_output=True,
                             text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    pids = []
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and marker in parts[1] and "ps -axo" not in parts[1]:
            try:
                pids.append(int(parts[0]))
            except ValueError:
                pass
    return pids


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _project_entries() -> int:
    projects = os.path.expanduser("~/.claude/projects")
    try:
        return len(os.listdir(projects))
    except OSError:
        return -1


def _launch(invocation, prompt, environment, decoder, *, timeout: float, on_pids=None):
    """Der ECHTE Starter (`launcher.run`): Zeilen an den Decoder, nichts
    behalten, Gruppen-SIGKILL an der Frist."""
    from solvio.specialists import launcher as L

    async def go():
        task = asyncio.ensure_future(L.run(replace(invocation, timeout=timeout), prompt,
                                           on_stdout_line=decoder.feed, retain_stdout=False,
                                           environment=environment))
        if on_pids is not None:
            await asyncio.sleep(min(1.5, timeout / 2))
            on_pids()
        return await task
    return asyncio.run(go())


#: Eine gescriptete Abo-Anmeldung, wie die CLI sie ohne `--bare` laese —
#: KEINE echte: sie oeffnet nichts und existiert nur, damit die Gegenprobe zu
#: Mutation 3 (`--bare` entfernt) einen Fund im Draht haette.
FAKE_OAUTH_CREDENTIALS = {"claudeAiOauth": {
    "accessToken": "sk-ant-oat01-SOLVIO-CANARY-NOT-A-REAL-TOKEN-0000000000000000",
    "refreshToken": "sk-ant-ort01-SOLVIO-CANARY-NOT-A-REAL-TOKEN-0000000000000000",
    "expiresAt": 4102444800000, "scopes": ["user:inference"], "subscriptionType": "max"}}


def worker_probe(name: str, *, workspace_root: str | None = None, jail_root: str | None = None,
                 timeout: float | None = None, mutate=None, plant_credentials: bool = False) -> WorkerProbe:
    """Eine Probe aus §1.4 — modellfrei, im echten Kaefig, mit der Fabrik-argv.

    `mutate(argv) -> argv` ist die Naht fuer Gegenproben (z. B. ohne
    `--allowedTools`); die Produktion ruft ohne. `workspace_root`/`jail_root`
    erlauben einer isolierten Welt, die Wurzeln unter ihren Zustandsordner zu
    legen; die Vorgabe sind die konfigurierten Wurzeln.
    """
    from solvio.agent_runtime import isolation
    from solvio.specialists import claude_native_task as CNT
    if name not in PROBE_NAMES:
        raise ValueError("unknown worker probe")
    if not isolation.available():
        return WorkerProbe(name, False, "sandbox_missing")
    try:
        CNT.resolve_claude()
    except Exception:                          # noqa: BLE001
        return WorkerProbe(name, False, "cli_missing")
    ws_root, jail_root = _roots(workspace_root, jail_root)
    # Kurz, wegen der AF_UNIX-Grenze (104 Byte) unter Testwurzeln.
    tag = "c-" + secrets.token_hex(3)
    workspace = _private_dir(os.path.join(ws_root, tag))
    jail = _private_dir(os.path.join(jail_root, tag))
    neighbour_ws = _private_dir(os.path.join(ws_root, tag + "-nb"))
    neighbour_jail = _private_dir(os.path.join(jail_root, tag + "-nb"))
    started = time.time()
    try:
        return _probe(name, workspace=workspace, jail=jail, neighbour_ws=neighbour_ws,
                      neighbour_jail=neighbour_jail, ws_root=ws_root, jail_root=jail_root,
                      timeout=timeout, mutate=mutate, plant_credentials=plant_credentials)
    except Exception as exc:                   # noqa: BLE001 - ein Absturz ist rot, nicht gruen
        return WorkerProbe(name, False, "probe_crashed:" + type(exc).__name__,
                           elapsed=time.time() - started)
    finally:
        for folder in (workspace, jail, neighbour_ws, neighbour_jail):
            shutil.rmtree(folder, ignore_errors=True)


def _probe(name, *, workspace, jail, neighbour_ws, neighbour_jail, ws_root, jail_root,
           timeout, mutate, plant_credentials=False) -> WorkerProbe:
    import uuid
    from solvio.agent_runtime import isolation
    from solvio.specialists import claude_native_task as CNT, launcher as L
    started = time.time()
    token = PROBE_TOKEN
    task_id = os.path.basename(jail)
    session_id = str(uuid.uuid4())
    home = os.path.expanduser("~")
    witness = os.path.join(neighbour_ws, "witness.txt")
    with open(witness, "w", encoding="utf-8") as handle:
        handle.write("NACHBAR-ZEUGE\n")
    with open(os.path.join(neighbour_jail, "mcp.json"), "w", encoding="utf-8") as handle:
        handle.write('{"mcpServers":{"nachbar":{}}}\n')
    os.chmod(os.path.join(neighbour_jail, "mcp.json"), 0o600)

    # Ein zweiter Lauscher als verbotenes Rueckschleifen-Ziel — draussen
    # erreichbar (der Kontrast), drinnen nicht.
    forbidden = HTTPServer(("127.0.0.1", 0), _Listener)
    forbidden_port = forbidden.server_address[1]
    threading.Thread(target=forbidden.serve_forever, daemon=True).start()

    checks: dict[str, object] = {}
    fields: dict[str, object] = {}
    stub = None
    listener = None
    try:
        mcp = name == "M-C4"
        script: list[list[dict]] = []
        status_for = None
        stall = False
        deadline = timeout or WORKER_PROBE_TIMEOUT
        real_targets = {
            "a_prime": os.path.join(home, ".solvio-nexus", "codex", "auth.json"),
            "b": os.path.join(home, ".solvio"),
            "f": os.path.join(home, ".claude.json"),
        }
        if name in ("M-C2", "M-C2-noallow"):
            script = [
                _bash("a", f'security find-generic-password -s "{SYNTHETIC_KEYCHAIN_SERVICE}" 2>&1; echo "rc=$?"'),
                _bash("a2", f"ls -ld '{real_targets['a_prime']}' 2>&1; echo \"rc=$?\""),
                _bash("b", f"ls -ld '{real_targets['b']}' 2>&1; echo \"rc=$?\""),
                _bash("c", f"curl -s -m 3 -o /dev/null -w '%{{http_code}}' http://127.0.0.1:{forbidden_port}/ 2>&1; echo \" rc=$?\""),
                _bash("d", "echo ok > probe.txt; cat probe.txt"),
                _bash("e", f"cat '{witness}' 2>&1; echo \"rc=$?\"; cat '{neighbour_jail}/mcp.json' 2>&1; echo \"rc=$?\""),
                _bash("f", f"ls -ld '{real_targets['f']}' 2>&1; echo \"rc=$?\""),
                [{"type": "text", "text": _result_block()}]]
        elif name == "M-C3":
            script = [_bash("r1", "echo ok > probe.txt; cat probe.txt"), [{"type": "text", "text": "erster Turn"}],
                      [{"type": "text", "text": "zweiter Turn"}]]
        elif name == "M-C4":
            script = [[{"type": "tool_use", "id": "toolu_mcp", "name": "mcp__solvio__portal_list", "input": {}}],
                      [{"type": "text", "text": _result_block()}]]
        elif name == "M-C5":
            script = [_bash("s", "echo warte")]
            stall = True
            deadline = timeout or 6.0
        elif name == "M-C5b":
            script = [_bash("l1", "echo ok > probe.txt; cat probe.txt")]
            status_for = lambda index, side: 401 if (not side and index >= 1) else 200
            deadline = timeout or RETRY_PROBE_TIMEOUT
        elif name == "M-C7":
            status_for = lambda index, side: 429
            deadline = timeout or RETRY_PROBE_TIMEOUT
        elif name == "M-C8":
            script = [_bash("t1", "echo $ANTHROPIC_API_KEY"), _bash("t2", "env"),
                      [{"type": "text", "text": _result_block()}]]
        elif name == "M-C9":
            script = [_bash(f"big{i}", "head -c 60000 /dev/zero | tr '\\0' x") for i in range(10)]
            script.append([{"type": "text", "text": _result_block()}])

        listener = ScriptedListener(script=script, status_for=status_for, stall=stall).start()
        port = listener.port
        native_turn_id = f"{session_id}/canary"
        endpoint = ""
        if mcp:
            stub = _StubBridge(os.path.join(jail, CNT.JAIL_SOCK), thread_id=session_id, turn_id=native_turn_id)
            endpoint = stub.endpoint
        layout = CNT.prepare_jail(jail, workspace=workspace, broker_port=port,
                                  unix_sockets=(endpoint,) if endpoint else (),
                                  roots=isolation.sealed_roots(ws_root, jail_root))
        if mcp:
            CNT.write_mcp_config(jail, python=layout["python"], endpoint=endpoint,
                                 manifest_digest=stub.digest, thread_id=session_id, turn_id=native_turn_id)
        if plant_credentials:
            planted = os.path.join(jail, CNT.JAIL_CONFIG, ".credentials.json")
            with open(planted, "w", encoding="utf-8") as handle:
                json.dump(FAKE_OAUTH_CREDENTIALS, handle)
            os.chmod(planted, 0o600)
        environment = CNT.worker_environment(jail, broker_port=port, token=token)
        invocation = CNT.claude_worker_invocation(workdir=workspace, jail=jail, task_id=task_id,
            session_id=session_id, broker_port=port, mcp_mode="bridge" if mcp else "none", resume=False)
        if name == "M-C2-noallow":
            argv = tuple(a for a in invocation.argv)
            cut = list(argv)
            index = cut.index("--allowedTools")
            del cut[index:cut.index("--disallowedTools")]
            invocation = replace(invocation, argv=tuple(cut))
        if mutate is not None:
            invocation = replace(invocation, argv=tuple(mutate(list(invocation.argv))))
        projects_before = _project_entries()
        stream_bytes = {"n": 0}
        decoder = CNT.ClaudeStreamDecoder(session_id, workspace, CNT.redactor(token))
        feed = decoder.feed

        def counting_feed(line):
            stream_bytes["n"] += len(line.encode("utf-8")) + 1
            feed(line)
        decoder.feed = counting_feed
        pids: list[int] = []

        def snapshot():
            pids.extend(_pids_for(session_id))
        outcome = _launch(invocation, "Sag KANARIENVOGEL und fuehre die Werkzeuge aus.",
                          environment, decoder, timeout=deadline,
                          on_pids=snapshot if name == "M-C5" else None)
        elapsed = time.time() - started
        fields.update(decoder=decoder.summary(), stream_bytes=stream_bytes["n"],
                      outcome_reason=outcome.reason, truncated=outcome.truncated,
                      process_started=outcome.process_started, main_requests=listener.main,
                      total_requests=listener.total, tools_in_wire=sorted({t for e in listener.seen
                                                                           if not e["side"] for t in e["tools"]}))
        wire = _wire_verdict(listener.seen, token)
        seen_auth = tuple(sorted({_mask(w) for w in _auth_values(listener.seen)}))
        base = dict(requests=listener.main, side_requests=listener.total - listener.main,
                    seen_auth=seen_auth, exit_code=outcome.exit_code, elapsed=elapsed,
                    checks=checks, fields=fields)
        if name in ("M-C5",):
            # Der Lauscher stellt die Antwort ein; der Starter beendet die
            # Gruppe an der Frist. Beweis: die vorher gesehenen PIDs sind fort.
            checks["timeout_reported"] = outcome.reason == "timeout" and outcome.exit_code is None
            checks["pids_seen_before_kill"] = len(pids)
            checks["process_group_empty"] = bool(pids) and not any(_alive(p) for p in pids)
            checks["turn_state"] = decoder.terminal(outcome.exit_code)[0]
            ok = bool(checks["timeout_reported"] and checks["process_group_empty"]
                      and checks["turn_state"] == "unknown")
            return WorkerProbe(name, ok, "" if ok else "kill_incomplete", **base)
        if wire:
            return WorkerProbe(name, False, wire, **base)
        if name == "M-C7":
            checks["assistant_started"] = decoder.started
            checks["synthetic_errors"] = decoder.synthetic_errors
            checks["result_seen"] = decoder.result is not None
            checks["exit_code_present"] = outcome.exit_code is not None
            checks["api_error_status"] = decoder.result["api_error_status"] if decoder.result else None
            checks["nonstart_candidate"] = (not decoder.started and outcome.exit_code is not None)
            checks["retries"] = listener.main
            ok = bool(checks["nonstart_candidate"] and checks["result_seen"]
                      and checks["api_error_status"] == 429)
            return WorkerProbe(name, ok, "" if ok else "cli_429_shape_changed", **base)
        if name == "M-C5b":
            status, reason = decoder.terminal(outcome.exit_code)
            checks["assistant_started"] = decoder.started
            checks["exit_code_present"] = outcome.exit_code is not None
            checks["result_seen"] = decoder.result is not None
            checks["api_error_status"] = decoder.result["api_error_status"] if decoder.result else None
            checks["terminal"] = status
            checks["terminal_reason"] = reason
            checks["ended_before_deadline"] = outcome.reason != "timeout"
            ok = bool(checks["ended_before_deadline"] and status == "failed" and decoder.started
                      and checks["api_error_status"] == 401)
            return WorkerProbe(name, ok, "" if ok else "cli_401_shape_changed", **base)
        if name == "M-C3":
            first = listener.main
            fields["turn1_result"] = decoder.result is not None and decoder.result["subtype"] == "success"
            resumed = CNT.claude_worker_invocation(workdir=workspace, jail=jail, task_id=task_id,
                session_id=session_id, broker_port=port, mcp_mode="none", resume=True)
            if mutate is not None:
                resumed = replace(resumed, argv=tuple(mutate(list(resumed.argv))))
            decoder2 = CNT.ClaudeStreamDecoder(session_id, workspace, CNT.redactor(token))
            outcome2 = _launch(resumed, "Was stand in probe.txt?", environment, decoder2, timeout=deadline)
            later = [e for e in listener.seen if not e["side"]][first:]
            prior = bool(later) and any("toolu_r1" in e["tool_use_ids"] and e["messages"] > 1 for e in later)
            checks["turn2_exit_code"] = outcome2.exit_code
            checks["turn2_init_same_session"] = decoder2.init_seen >= 1
            checks["prior_turn_in_resume"] = prior
            checks["turn2_result"] = decoder2.result is not None
            checks["wire_clean"] = _wire_verdict(listener.seen, token) == ""
            ok = bool(prior and outcome2.exit_code == 0 and decoder2.result is not None and checks["wire_clean"])
            base["requests"] = listener.main
            return WorkerProbe(name, ok, "" if ok else "resume_without_history", **base)
        # Alle uebrigen Proben: voller Turn mit `result`, Exit 0
        result = decoder.result
        checks["result_seen"] = result is not None
        checks["exit_zero"] = outcome.exit_code == 0
        checks["assistant_started"] = decoder.started
        checks["permission_denials_empty"] = bool(result) and result["permission_denials"] == 0
        checks["declined_receipts"] = sum(1 for r in decoder.receipts if r["status"] == "declined")
        checks["owner_projects_unchanged"] = _project_entries() == projects_before
        session_files = list((Path(jail) / CNT.JAIL_CONFIG / "projects").rglob(session_id + ".jsonl")) \
            if os.path.isdir(os.path.join(jail, CNT.JAIL_CONFIG, "projects")) else []
        checks["session_in_jail_config"] = len(session_files) == 1
        expected_tools = sorted(CNT.CLAUDE_WORKER_TOOLS + (("mcp__solvio__portal_list",) if mcp else ()))
        checks["tools_exact"] = fields["tools_in_wire"] == expected_tools
        if name in ("M-C2", "M-C2-noallow"):
            tr = listener.tool_results
            def denied(item):
                entry = tr.get(f"toolu_{item}")
                return bool(entry and entry["kernel_denied"])
            checks["a_security_exec_denied"] = denied("a")
            checks["a_prime_nexus_sealed"] = denied("a2")
            checks["a_prime_target_exists"] = os.path.exists(real_targets["a_prime"])
            checks["b_solvio_sealed"] = denied("b")
            checks["b_target_exists"] = os.path.exists(real_targets["b"])
            entry_c = tr.get("toolu_c") or {}
            checks["c_loopback_refused"] = bool(entry_c) and not entry_c["excerpt"].startswith("200")
            checks["d_bash_executed"] = os.path.isfile(os.path.join(workspace, "probe.txt"))
            excerpt_e = (tr.get("toolu_e") or {}).get("excerpt", "")
            checks["e_neighbours_sealed"] = (denied("e") and "NACHBAR-ZEUGE" not in excerpt_e
                                             and "nachbar" not in excerpt_e)
            checks["f_claude_json_sealed"] = denied("f")
            checks["f_target_exists"] = os.path.exists(real_targets["f"])
            fields["tool_results"] = {k: {kk: vv for kk, vv in v.items() if kk != "excerpt"}
                                      for k, v in tr.items()}
            required = ["a_security_exec_denied", "c_loopback_refused", "d_bash_executed",
                        "e_neighbours_sealed", "result_seen", "exit_zero", "assistant_started",
                        "permission_denials_empty", "owner_projects_unchanged",
                        "session_in_jail_config", "tools_exact"]
            # Reale Siegelziele nur dort, wo sie existieren — Abwesenheit ist
            # KEIN Beweis: sie wird als eigenes Feld sichtbar gemacht, und der
            # Nachbar-Zeuge (e) beweist den Mechanismus auf jedem Rechner.
            for key, exists in (("a_prime_nexus_sealed", "a_prime_target_exists"),
                                ("b_solvio_sealed", "b_target_exists"),
                                ("f_claude_json_sealed", "f_target_exists")):
                if checks[exists]:
                    required.append(key)
            failed = [k for k in required if not checks.get(k)]
            failed += [] if checks["declined_receipts"] == 0 else ["declined_receipts"]
            ok = not failed
            return WorkerProbe(name, ok, "" if ok else "checks_failed:" + ",".join(failed), **base)
        if name == "M-C4":
            checks["mcp_connected"] = any(s["name"] == "solvio" and s["status"] == "connected"
                                          for s in decoder.mcp_servers)
            methods = [s["method"] for s in stub.seen]
            checks["stub_saw_manifest"] = "manifest" in methods
            checks["stub_saw_call"] = "call" in methods
            checks["stub_binding_ok"] = all(s["binding_ok"] for s in stub.seen if s["method"] == "call")
            entry = listener.tool_results.get("toolu_mcp") or {}
            checks["tool_result_returned"] = bool(entry) and not entry["is_error"]
            checks["dynamic_receipt"] = any(r["kind"] == "dynamicToolCall" and r["status"] == "completed"
                                            and r["success"] for r in decoder.receipts)
            required = ["mcp_connected", "stub_saw_manifest", "stub_saw_call", "stub_binding_ok",
                        "tool_result_returned", "dynamic_receipt", "result_seen", "exit_zero"]
            failed = [k for k in required if not checks.get(k)]
            ok = not failed
            return WorkerProbe(name, ok, "" if ok else "checks_failed:" + ",".join(failed), **base)
        if name == "M-C8":
            receipts = json.dumps(decoder.receipts, ensure_ascii=False)
            observation = json.dumps({"receipts": decoder.receipts,
                                      "result": result["text"] if result else ""}, ensure_ascii=False)
            outputs = [r["output"] for r in decoder.receipts if r["kind"] == "commandExecution"]
            checks["token_literal_absent"] = token not in observation
            checks["outputs_redacted"] = bool(outputs) and all(
                o["redacted"] is True and L.MASK in o["text"] and type(o["original_chars"]) is int
                for o in outputs)
            checks["leak_markers_absent"] = not any(m.lower() in observation.lower() for m in LEAK_MARKERS)
            fields["observation_sha256"] = hashlib.sha256(observation.encode()).hexdigest()
            required = ["token_literal_absent", "outputs_redacted", "leak_markers_absent", "result_seen", "exit_zero"]
            failed = [k for k in required if not checks.get(k)]
            ok = not failed
            return WorkerProbe(name, ok, "" if ok else "checks_failed:" + ",".join(failed), **base)
        if name == "M-C9":
            from solvio.specialists.native_task_profile import MAX_RECEIPT_BYTES
            checks["stream_over_launcher_ceiling"] = stream_bytes["n"] > L.MAX_OUTPUT * 4
            checks["receipts_within_budget"] = len(json.dumps(decoder.receipts, ensure_ascii=True).encode()) <= MAX_RECEIPT_BYTES
            checks["terminal"] = decoder.terminal(outcome.exit_code)[0]
            required = ["stream_over_launcher_ceiling", "receipts_within_budget", "result_seen", "exit_zero"]
            failed = [k for k in required if not checks.get(k)]
            failed += [] if checks["terminal"] == "completed" else ["terminal"]
            ok = not failed
            return WorkerProbe(name, ok, "" if ok else "checks_failed:" + ",".join(failed), **base)
        return WorkerProbe(name, False, "unhandled_probe", **base)
    finally:
        if listener is not None:
            listener.stop()
        if stub is not None:
            stub.stop()
        forbidden.shutdown()
        forbidden.server_close()


def evidence(results: list[WorkerProbe], *, cli_version: str = "") -> dict[str, object]:
    """Das Beleg-Dokument: nur Exit-Codes, Booleans, Zaehler, Hashes."""
    return {"kind": "n8-c4-claude-worker-canary", "cli_version": cli_version,
            "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "probes": [r.as_dict() for r in results],
            "all_ok": all(r.ok for r in results)}


if __name__ == "__main__":                     # pragma: no cover - Werkzeug
    ergebnis = run()
    print(json.dumps(ergebnis.as_dict(), indent=2, ensure_ascii=False))
    sys.exit(0 if ergebnis.ok else 1)
