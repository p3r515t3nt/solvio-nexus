"""Opt-in policy and tool projection for the existing Hermes native session.

This is not an agent loop: Codex selects and executes its own native tools.
The Core offers a bounded DynamicToolSpec callback and publishes declared
workspace files separately after the provider claim is settled.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re


MAX_RECEIPT_BYTES = 24_000
MAX_COMMAND_CHARS = 4_096
MAX_OUTPUT_CHARS = 8_000


def fit_receipts(receipts, budget=MAX_RECEIPT_BYTES):
    """Keep the newest command intact; omissions remain explicit observations."""
    result = json.loads(json.dumps(receipts, ensure_ascii=True))
    commands = [item for item in result if item.get("kind") == "commandExecution"]
    for item in commands[:-1]:
        if len(json.dumps(result, ensure_ascii=True).encode()) <= budget:
            break
        for field in ("command", "output"):
            item[field]["text"] = ""
            item[field]["complete"] = False
    if len(json.dumps(result, ensure_ascii=True).encode()) > budget:
        raise ValueError("native_task_receipt_limit")
    return result


def _safe(value):
    # Reuse the worker's credential filter, including in its isolated -I entry.
    if __package__:
        from .hermes_native_worker import safe_text
    else:
        from hermes_native_worker import safe_text
    cleaned = safe_text(value, len(value))
    # Review Runde 9, F9-1: no word boundary before the term — `DB_PASSWORD=`,
    # `PGPASSWORD=`, `GITHUB_TOKEN=` and `"api_key":` are the common shapes.
    if cleaned != value or re.search(
            r"(?:access_token|refresh_token|id_token|api_key|api_token|client_secret|"
            r"password|passwd|passwort|kennwort|_token|secret|authorization|cookie)[A-Za-z0-9_\-]*[\"']?\s*[:=]", value, re.I):
        # Drop the whole field when credentials are detected: a partial regex
        # match must not leave a second token or multiline secret behind.
        return "<entfernt>"
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "<entfernt>", cleaned)


def _text(value, limit):
    """Completeness describes this native field, never the entire process stream."""
    if value is None:
        return {"text": "", "original_chars": None, "complete": False, "redacted": False}
    if type(value) is not str:
        raise ValueError("native_task_receipt_invalid")
    cleaned = _safe(value)
    redacted = cleaned != value or "<entfernt>" in cleaned
    end = min(len(cleaned), limit)
    # JSON escapes (especially Unicode/control bytes) count toward the bound.
    low, high = 0, end
    while low < high:
        middle = (low + high + 1) // 2
        if len(json.dumps(cleaned[:middle], ensure_ascii=True).encode()) <= limit:
            low = middle
        else:
            high = middle - 1
    return {"text": cleaned[:low], "original_chars": len(value),
        "complete": not redacted and low == len(cleaned), "redacted": redacted}


def _url(value):
    if __package__:
        from .hermes_native_worker import _source_url
    else:
        from hermes_native_worker import _source_url
    return _source_url(value)


#: N8/C4 §3.2: the OPTIONAL `helpers[]` declaration of the task worker. The
#: bounds mirror `agent_runtime/helper_check.py` (MAX_HELPERS, MAX_PATH,
#: MAX_NAME, MAX_PURPOSE); this module runs standalone in the worker and
#: therefore repeats the numbers instead of importing the Core module.
HELPERS_SCHEMA = {"type": "array", "maxItems": 4,
    "items": {"type": "object", "additionalProperties": False,
        "properties": {"path": {"type": "string", "minLength": 1, "maxLength": 240},
            "name": {"type": "string", "minLength": 1, "maxLength": 60},
            "purpose": {"type": "string", "minLength": 1, "maxLength": 80}},
        "required": ["path", "name", "purpose"]}}


def result_schema(research_schema):
    schema = json.loads(json.dumps(research_schema))
    schema["properties"]["files"] = {"type": "array", "maxItems": 4,
        "items": {"type": "object", "additionalProperties": False,
            "properties": {"path": {"type": "string", "minLength": 1, "maxLength": 240},
                "requirement": {"anyOf": [
                    {"type": "string", "maxLength": 100},
                    {"type": "array", "minItems": 1, "maxItems": 5,
                     "items": {"type": "string", "minLength": 1, "maxLength": 100}}]}},
            "required": ["path", "requirement"]}}
    schema["required"].append("files")
    # `helpers` is semantically optional, but the provider's STRICT structured
    # output demands every property in `required` — measured on the third real
    # Durchstich (19.09.2026 10:29): HTTP 400 `invalid_json_schema` "Missing
    # 'helpers'" before any model work, every Codex worker turn dead. The
    # closed schema (additionalProperties False) still needs the entry, or no
    # declaration the prompt asks for could pass; "no helper" is `[]`.
    schema["properties"]["helpers"] = json.loads(json.dumps(HELPERS_SCHEMA))
    schema["required"].append("helpers")
    return schema


# ADR-0040 (Kurskorrektur S1, Review S1-4): private data of the Owner and a
# channel to the outside never share one session. A manifest that names any of
# these tools starts the native session WITHOUT web search. The set mirrors
# capabilities/task_read.PRIVATE_DATA_TOOLS (a Core test keeps them equal); the
# worker cannot import the Core.
PRIVATE_DATA_TOOLS = frozenset({
    "owner_task_overview",
    "gmail_list_recent", "gmail_search", "gmail_read_message", "gmail_read_thread",
    "calendar_list_events", "calendar_get_event", "calendar_search_events", "calendar_find_availability"})


def web_search_mode(tool_names):
    return "disabled" if set(tool_names) & PRIVATE_DATA_TOOLS else "live"


class NativeTaskProfile:
    def __init__(self, args):
        # The standalone worker explicitly adds its checked sibling directory.
        from native_tool_wire import NativeToolClient
        from native_work_policy import NativeWorkPolicy, PROFILE
        if (args.session_mode != "durable" or args.browser_python
                or not args.core_tools_socket or not args.core_tools_digest):
            raise ValueError("native_task_profile_invalid")
        self.client = NativeToolClient(args.core_tools_socket, args.core_tools_digest)
        self.profile_id = PROFILE
        self.tools = self.client.manifest()
        self.names = {tool["name"] for tool in self.tools}
        self.web_search = web_search_mode(self.names)
        # A private native Home may reference the existing native auth file.
        # Resolve its path only; neither worker nor Core reads its contents.
        auth_parent = str((Path(args.codex_home) / "auth.json").resolve().parent)
        self.policy = NativeWorkPolicy(args.workdir, denied_paths=(
            str(Path(args.codex_home).resolve()),
            auth_parent,
            str(Path(args.core_tools_socket).parent.resolve()),
            str(Path(__file__).resolve().parents[3])))
        self.receipts = []
        self.workspace = Path(args.workdir)

    def _path(self, value):
        if type(value) is not str:
            raise ValueError("native_task_receipt_invalid")
        path = Path(value)
        if path.is_absolute():
            try:
                path = path.relative_to(self.workspace)
            except ValueError:
                path = None
        if (path is None or not path.parts or ".." in path.parts
                or "\\" in value or _safe(value) != value or len(str(path)) > 240):
            return {"text": "<entfernt>", "original_chars": len(value), "complete": False, "redacted": True}
        # A lexical projection of an observed path; no filesystem read or claim
        # that a patch was actually applied, or that its bytes were verified.
        return _text(path.as_posix(), 240)

    def observe(self, item, method):
        kind = item.get("type")
        if kind not in {"commandExecution", "fileChange", "dynamicToolCall", "webSearch"}:
            return False
        if kind == "dynamicToolCall" and (item.get("tool") not in self.names
                or item.get("namespace") is not None):
            return False
        if method == "item/completed":
            item_id = item.get("id")
            status = "completed" if kind == "webSearch" else item.get("status")
            if (not isinstance(item_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", item_id)
                    or _safe(item_id) != item_id
                    or status not in {"completed", "failed", "declined"}
                    or len(self.receipts) >= 100 or any(r["item_id"] == item_id for r in self.receipts)):
                raise ValueError("native_task_receipt_invalid")
            receipt = {"kind": kind, "item_id": item_id, "status": status}
            if kind == "commandExecution":
                code, command = item.get("exitCode"), item.get("command")
                if (code is not None and type(code) is not int) or type(command) is not str:
                    raise ValueError("native_task_receipt_invalid")
                receipt.update(exit_code=code, command_sha256=hashlib.sha256(command.encode()).hexdigest(),
                    command=_text(command, MAX_COMMAND_CHARS), output=_text(item.get("aggregatedOutput"), MAX_OUTPUT_CHARS))
            elif kind == "fileChange":
                changes = item.get("changes")
                if type(changes) is not list:
                    raise ValueError("native_task_receipt_invalid")
                projected = []
                for change in changes[:16]:
                    if (type(change) is not dict or type(change.get("kind")) is not dict
                            or change["kind"].get("type") not in {"add", "delete", "update"}):
                        raise ValueError("native_task_receipt_invalid")
                    move = change["kind"].get("move_path")
                    projected.append({"path": self._path(change.get("path")), "kind": change["kind"]["type"],
                        "move_path": self._path(move) if move is not None else None})
                receipt.update(changes=projected, changes_complete=len(changes) <= 16, change_count=len(changes))
            elif kind == "webSearch":
                action = item.get("action") or {}
                results = item.get("results") or []
                if type(action) is not dict or type(results) is not list:
                    raise ValueError("native_task_receipt_invalid")
                urls = []
                values = [action.get("url")] + [r.get("url") for r in results[:24] if type(r) is dict]
                complete = len(results) <= 24
                for value in values:
                    if value is None:
                        continue
                    url = _url(value)
                    if not url or (url not in urls and len(urls) >= 12):
                        complete = False
                    elif url not in urls:
                        urls.append(url)
                receipt.update(query=_text(item.get("query"), 1_000),
                    action=action.get("type") if action.get("type") in {"search", "openPage", "findInPage", "other"} else "unknown",
                    urls=urls, urls_complete=complete)
            elif kind == "dynamicToolCall":
                receipt.update(tool=item["tool"], success=item.get("success") is True)
            self.receipts = fit_receipts([*self.receipts, receipt])
        return True

    def call(self, request, *, thread_id, turn_id):
        body = request.get("params")
        if (request.get("method") != "item/tool/call" or type(body) is not dict
                or body.get("threadId") != thread_id or body.get("turnId") != turn_id
                or body.get("tool") not in self.names):
            raise ValueError("native_tool_not_allowed")
        return self.client.call(body)
