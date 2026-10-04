"""MCP-stdio-Adapter auf die vorhandene Core-Werkzeugbruecke (N8/C4, §4).

Laeuft als Kind des Claude-CLI IM Kaefig (`python -I -B <jail>/adapter/
claude_mcp_adapter.py ...`), nur Stdlib, und bildet genau drei JSON-RPC-
Methoden auf `NativeToolClient` ab:

* `initialize`   → Handschlag (Faehigkeit `tools`), kein Core-Aufruf
* `tools/list`   → `client.manifest()`  (Digest-gebunden, nur bei aktivem Claim)
* `tools/call`   → `client.call(...)`   (erst nach bestaetigtem Turnstart)

`threadId`, `turnId` kommen AUS DER ARGUMENTLISTE, die der Core schreibt — nie
aus einem Modellargument. `callId` ist die JSON-RPC-Kennung. Keine
Wiederholung, kein Zustand, kein zweiter Agentenkreis: der Adapter kennt
keine Werkzeuge ausser denen, die das Manifest des Cores nennt, und jeder
Aufruf wird im Core erneut an Claim, Turnzustand, Grant und Manifest-Digest
geprueft.

Die Datei liegt als 0o400-Kopie im Kaefig neben `native_tool_wire.py`; beide
Digests stehen im Policy-Digest des Workers.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "solvio"
MAX_LINE = 65536
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")


def _wire():
    # `-I` haelt das Skriptverzeichnis aus `sys.path` heraus; die geprueften
    # Nachbarn werden ausdruecklich und nur hier hinzugefuegt.
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    import native_tool_wire  # noqa: E402 - Nachbar im Kaefig
    return native_tool_wire


def _reply(request_id, result):
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _tools(manifest):
    tools = []
    for entry in manifest:
        if type(entry) is not dict or type(entry.get("name")) is not str:
            raise ValueError("native_tool_manifest_invalid")
        tools.append({"name": entry["name"],
                      "description": str(entry.get("description", "")),
                      "inputSchema": entry.get("inputSchema") or
                      {"type": "object", "properties": {}, "additionalProperties": False}})
    return tools


def handle(message, client, *, thread_id, turn_id):
    """Eine Anfrage → eine Antwort (oder None fuer Benachrichtigungen)."""
    if type(message) is not dict or message.get("jsonrpc") != "2.0":
        return _error(None, -32600, "invalid request")
    method = message.get("method")
    request_id = message.get("id")
    if type(method) is not str:
        return _error(request_id, -32600, "invalid request")
    if request_id is None:
        # Benachrichtigungen (`notifications/initialized`, `notifications/cancelled`)
        # haben keine Antwort — und loesen nichts aus.
        return None
    params = message.get("params") or {}
    if type(params) is not dict:
        return _error(request_id, -32602, "invalid params")
    try:
        if method == "initialize":
            return _reply(request_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": "1"}})
        if method == "ping":
            return _reply(request_id, {})
        if method == "tools/list":
            return _reply(request_id, {"tools": _tools(client.manifest())})
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments")
            if arguments is None:
                arguments = {}
            call_id = str(request_id)
            if (type(name) is not str or type(arguments) is not dict
                    or not _ID.fullmatch(call_id)):
                return _error(request_id, -32602, "invalid params")
            response = client.call({"threadId": thread_id, "turnId": turn_id,
                                    "callId": call_id, "tool": name,
                                    "arguments": arguments})
            content = [{"type": "text", "text": item["text"]}
                       for item in response["contentItems"]]
            return _reply(request_id, {"content": content,
                                       "isError": not response["success"]})
        return _error(request_id, -32601, "method not found")
    except (ValueError, TypeError, OSError, KeyError) as exc:
        # Kein Ausnahmetext ueber den Draht: der Core hat die Wahrheit ueber
        # jede zugestellte Anfrage; das Modell bekommt nur die Klasse.
        return _error(request_id, -32000, "core tool call refused: " + type(exc).__name__)


def serve(stdin, stdout, client, *, thread_id, turn_id):
    for raw in stdin:
        line = raw.strip()
        if not line:
            continue
        if len(line) > MAX_LINE:
            answer = _error(None, -32600, "line too long")
        else:
            try:
                message = json.loads(line)
            except ValueError:
                answer = _error(None, -32700, "parse error")
            else:
                answer = handle(message, client, thread_id=thread_id, turn_id=turn_id)
        if answer is not None:
            stdout.write(json.dumps(answer, ensure_ascii=False) + "\n")
            stdout.flush()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--core-tools-socket", required=True)
    parser.add_argument("--core-tools-digest", required=True)
    parser.add_argument("--thread-id", required=True)
    parser.add_argument("--turn-id", required=True)
    args = parser.parse_args(argv)
    if not (_ID.fullmatch(args.thread_id) and _ID.fullmatch(args.turn_id)
            and re.fullmatch(r"[a-f0-9]{64}", args.core_tools_digest)):
        return 2
    wire = _wire()
    client = wire.NativeToolClient(args.core_tools_socket, args.core_tools_digest)
    serve(sys.stdin, sys.stdout, client, thread_id=args.thread_id, turn_id=args.turn_id)
    return 0


if __name__ == "__main__":                     # pragma: no cover - Kindprozess
    sys.exit(main())
