"""One native Codex turn through Hermes' installed transport, never AIAgent.

This executable deliberately has only stdlib imports until its checked Hermes
source is loaded. It neither resolves provider credentials nor owns a task
store. The official Codex process alone opens its dedicated authentication.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import math
import os
from pathlib import Path
import re
import signal
import sys
import time
from urllib.parse import parse_qsl, urlsplit, urlunsplit

VERSION = 2
MAX_INPUT = 24_000
MAX_TEXT = 18_000
MAX_EVENTS = 24
RUNTIME = "hermes-codex-app-server"
RESULT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        # Fit complete checkpoint sections. Twelve web and twelve browser
        # evidence slots remain available for observed native source URLs.
        **{key: {"type": "array", "maxItems": 12,
                 "items": {"type": "string", "maxLength": chars}}
           for key, chars in (("findings", 1200), ("evidence", 720),
                              ("assumptions", 600), ("uncertainties", 600),
                              ("rejected_alternatives", 600), ("risk_notes", 600))},
        "recommended_path": {"type": "string", "maxLength": 1500},
        "confidence": {"type": "string", "enum": [
            "", "hoch", "mittel", "niedrig", "high", "medium", "low"]},
    },
    "required": ["findings", "evidence", "assumptions", "uncertainties",
                 "rejected_alternatives", "risk_notes", "recommended_path", "confidence"],
}
DISABLED_FEATURES = (
    "shell_tool", "apps", "multi_agent", "hooks", "plugins", "remote_plugin",
    "memories", "browser_use", "browser_use_external", "browser_use_full_cdp_access",
    "computer_use", "image_generation", "view_image", "skill_mcp_dependency_install",
    "skill_search", "tool_suggest", "shell_snapshot", "goals", "workspace_dependencies",
    "fast_mode",
)
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_AUTH_KEYS = re.compile(r'(?:\\*"|\b)(?:access_token|refresh_token|id_token|api_key|'
                        r'OPENAI_API_KEY|client_secret|authorization)(?:\\*"|\b)'
                        r'\s*[:=]\s*(?:\\*")?[^\s"\\,}]+', re.I)
# Prefixes identify complete credential-shaped lexemes, not the suffix of
# ordinary words such as native-task-workspaces in a Python traceback.
_PREFIX = re.compile(r"(?<![A-Za-z0-9_-])(?:sk-[A-Za-z0-9_-]{10,}|ghp_[A-Za-z0-9]{10,}|"
                     r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})"
                     r"(?![A-Za-z0-9_-])")


def _home_policy():
    # Exact sibling works in both the Core package and the -I standalone worker.
    spec = importlib.util.spec_from_file_location("solvio_native_home_policy",
        Path(__file__).with_name("native_home_policy.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def safe_text(value: object, limit: int = 500) -> str:
    text = _AUTH_KEYS.sub("<entfernt>", str(value))
    text = _PREFIX.sub("<entfernt>", text)
    text = re.sub(r"\bBearer\s+\S+", "Bearer <entfernt>", text, flags=re.I)
    return text[:limit]


def native_config_text(model: str) -> str:
    """Reviewed setup template; returning text never creates a native Home."""
    if not _MODEL.fullmatch(model):
        raise ValueError("native_model_required")
    lines = [f"model = {json.dumps(model)}", 'model_provider = "openai"',
             'approval_policy = "never"', 'approvals_reviewer = "user"',
             'sandbox_mode = "read-only"', 'web_search = "live"',
             'cli_auth_credentials_store = "file"', "project_doc_max_bytes = 0",
             'history.persistence = "none"', "[features]"]
    lines.extend(f"{name} = false" for name in DISABLED_FEATURES)
    return "\n".join(lines) + "\n"


def validate_home(home: str, model: str) -> None:
    """Check our configuration only. In particular, never open auth.json."""
    path = Path(home)
    if (not path.is_absolute() or path.is_symlink() or not path.is_dir()
            or path.resolve() == (Path.home() / ".codex").resolve()):
        raise ValueError("native_home_required")
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("native_home_not_private")
    config = path / "config.toml"
    if config.is_symlink() or not config.is_file() or config.stat().st_size > 128 * 1024:
        raise ValueError("native_config_required")
    _home_policy().validate_config_text(config.read_text(encoding="utf-8"), native_config_text(model), home)


def _source_url(value: object) -> str:
    if not isinstance(value, str) or len(value) > 700:
        return ""
    try:
        p = urlsplit(value)
        if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password:
            return ""
        if any(re.search(r"token|secret|password|signature|api.?key|auth|^code$", k, re.I)
               for k, _ in parse_qsl(p.query)):
            return ""
        cleaned = urlunsplit((p.scheme, p.netloc, p.path, p.query, ""))
        return cleaned if safe_text(cleaned, 700) == cleaned else ""
    except ValueError:
        return ""


def _error_reason(error: object) -> str:
    info = error.get("codexErrorInfo") if isinstance(error, dict) else None
    return {"usageLimitExceeded": "quota", "unauthorized": "logged_out",
            "sessionBudgetExceeded": "execution_budget_exhausted",
            "contextWindowExceeded": "context_limit"}.get(info if isinstance(info, str) else "", "provider_failed")


class Recorder:
    def __init__(self):
        self.thread_id = ""
        self.turn_id = ""
        self.terminal = ""
        self.failure = ""
        self.turn_requested = False
        self.final_text = ""
        self.session = None
        self.events = 0
        self.sources: list[str] = []
        self.usage: dict[str, int] = {}
        self.browser_tools: tuple[str, ...] = ()
        self.task_profile = None

    def emit(self, kind: str, **fields) -> None:
        if self.events >= MAX_EVENTS:
            return
        self.events += 1
        print(json.dumps({"schema": VERSION, "type": "event", "event": kind,
                          **fields, "seq": self.events,
                          "thread_id": self.thread_id, "turn_id": self.turn_id},
                         ensure_ascii=False), flush=True)

    def fail(self, reason: str) -> None:
        self.failure = self.failure or reason
        if self.session is not None:
            self.session.request_interrupt()

    def execution_status(self) -> str:
        if not self.turn_requested:
            return "not_started"
        if self.terminal in {"completed", "failed", "interrupted"}:
            return "terminal"
        return "unknown"

    def on_event(self, note: dict) -> None:
        params = note.get("params") or {}
        turn = params.get("turn") or {}
        if (params.get("threadId") != self.thread_id
                or (params.get("turnId") or turn.get("id")) != self.turn_id):
            return
        method = note.get("method")
        if method == "turn/completed":
            self.terminal = turn.get("status", "")
            if self.terminal == "failed":
                self.fail(_error_reason(turn.get("error")))
            elif self.terminal not in {"completed", "interrupted"}:
                self.fail("invalid_terminal")
        elif method == "error":
            reason = _error_reason(params.get("error"))
            if reason in {"quota", "logged_out", "execution_budget_exhausted"} or not params.get("willRetry"):
                self.fail(reason)
        elif method == "thread/tokenUsage/updated":
            last = (params.get("tokenUsage") or {}).get("total") or {}
            self.usage = {k: v for k, v in last.items() if k in {
                "inputTokens", "cachedInputTokens", "outputTokens",
                "reasoningOutputTokens", "totalTokens"} and type(v) is int and v >= 0}
        elif method in {"item/started", "item/completed"}:
            item = params.get("item") or {}
            kind = item.get("type")
            if kind == "webSearch":
                if self.task_profile is not None:
                    try:
                        self.task_profile.observe(item, method)
                    except ValueError:
                        self.fail("native_task_receipt_invalid")
                self.emit("web_search", status=method.split("/")[-1],
                          item_id=safe_text(item.get("id", ""), 120),
                          # Task queries have a separate fully filtered receipt;
                          # do not duplicate raw native fields into progress logs.
                          query="" if self.task_profile is not None else safe_text(item.get("query", ""), 350))
                if method == "item/completed":
                    action = item.get("action") or {}
                    values = [action.get("url")]
                    # Results are opaque in the native schema. Only explicit
                    # top-level URL fields are evidence; never guess a shape.
                    values += [v.get("url") for v in (item.get("results") or [])[:24]
                               if isinstance(v, dict)]
                    for value in values:
                        url = _source_url(value)
                        if url and url not in self.sources and len(self.sources) < 12:
                            self.sources.append(url)
            elif kind == "mcpToolCall":
                if item.get("server") != "solvio-browser" or item.get("tool") not in self.browser_tools:
                    self.fail("native_tool_not_allowed")
                else:
                    # An observed read attempt, not proof of a successful page
                    # or of task completion. Never journal URLs, arguments,
                    # browser output or a model-provided tool description.
                    self.emit("browser_read", status=method.split("/")[-1],
                              item_id=safe_text(item.get("id", ""), 120))
            elif kind == "agentMessage":
                # Hermes also projects unscoped and commentary messages. Only
                # a final item belonging to this exact native turn is evidence.
                text = item.get("text")
                if method == "item/completed" and item.get("phase") == "final_answer" and isinstance(text, str):
                    self.final_text = text[:MAX_TEXT + 1]
            elif self.task_profile is not None and kind in {"commandExecution", "fileChange", "dynamicToolCall"}:
                try:
                    if not self.task_profile.observe(item, method):
                        self.fail("native_tool_not_allowed")
                except ValueError:
                    self.fail("native_task_receipt_invalid")
            elif kind not in {"reasoning", "plan", "contextCompaction", "userMessage"}:
                self.fail("native_tool_not_allowed")


def _validate_layers(response: dict, browser: dict | None = None, task_profile=None, home="") -> None:
    """A dedicated Home may still inherit machine/project configuration.

    Reject extra routing/tool/instruction layers, without returning their
    content. The reviewed template is intentionally the only user layer.
    """
    allowed = {"model", "model_provider", "approval_policy", "approvals_reviewer",
               "sandbox_mode", "web_search", "cli_auth_credentials_store",
               "project_doc_max_bytes", "history", "features", "skills"}
    if browser is not None:
        allowed.add("mcp_servers")
    if task_profile is not None:
        allowed.add("permissions")
    if home:
        allowed.add("projects")
    layers = response.get("layers")
    if not isinstance(layers, list):
        raise ValueError("native_config_unverified")
    for layer in layers:
        config = layer.get("config") if isinstance(layer, dict) else None
        if not isinstance(config, dict) or set(config) - allowed:
            raise ValueError("native_config_unverified")
        if "projects" in config:
            _home_policy().validate_projects(config["projects"], home)
        if config.get("model_provider", "openai") != "openai":
            raise ValueError("native_config_unverified")
        if any(value is not False and not (task_profile is not None and key == "shell_tool" and value is True)
               for key, value in config.get("features", {}).items()):
            raise ValueError("native_config_unverified")
        if "mcp_servers" in config and json.dumps(config["mcp_servers"], sort_keys=True) != json.dumps(browser, sort_keys=True):
            raise ValueError("native_config_unverified")


def _effective_browser_config(browser):
    if browser is None:
        # Codex 0.147.0 config/read reports an empty map without MCP servers.
        return {}
    result = json.loads(json.dumps(browser))
    # Measured native config/read normalization, not permission to inherit an
    # arbitrary MCP transport or environment. Raw layers are checked separately.
    for entry in result.values():
        if entry.get("env_vars") == []:
            entry.pop("env_vars")
        entry["environment_id"] = "local"
        for key in ("startup_timeout_sec", "tool_timeout_sec"):
            entry[key] = float(entry[key])
    return result


def _credit_limits(raw, limits, account, credit_account):
    """Delegated exception only for the personal Codex included-plan window.

    Reuse the account/limits parser which produced the Core's cost proof.
    No provider hard limit, separate bucket or unlimited credit is waived.
    """
    from decimal import Decimal
    spec = importlib.util.spec_from_file_location("solvio_native_credit_usage",
        Path(__file__).with_name("openai_usage.py"))
    usage = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = usage  # dataclasses resolves its module during import
    spec.loader.exec_module(usage)
    try:
        plan, digest = usage._account(account)
        if (not re.fullmatch(r"[a-f0-9]{64}", credit_account)
                or digest != credit_account or plan not in {"free", "go", "plus", "pro", "prolite"}):
            raise ValueError("native_credit_binding_invalid")
        snapshots = usage._limits(limits, plan)
        umbrella = snapshots["codex"]
        credits = umbrella.get("credits") or {}
        if (credits.get("hasCredits") is not True or credits.get("unlimited") is not False
                or credits.get("balance") is None or Decimal(credits["balance"]) <= 0):
            raise ValueError("quota")
        for key, snapshot in snapshots.items():
            reached = snapshot.get("rateLimitReachedType")
            # On the personal Codex bucket this is the included-plan limit,
            # already covered by the bound positive-credit delegation above.
            # Workspace limits, separate buckets and actual turn refusals stay
            # authoritative; this does not retry or reset a provider limit.
            included_limit = key == "codex" and reached == "rate_limit_reached"
            if snapshot.get("spendControlReached") or (reached and not included_limit):
                raise ValueError("quota")
            other = snapshot.get("credits")
            if other is not None and (other["unlimited"] is not False
                    or other["balance"] is None or Decimal(other["balance"]) < 0):
                raise ValueError("native_quota_unknown")
            windows = [snapshot.get("primary"), snapshot.get("secondary")]
            if not any(window is not None for window in windows):
                raise ValueError("native_quota_unknown")
            if key != "codex" and any(window and window["usedPercent"] >= 100 for window in windows):
                raise ValueError("quota")
            individual = snapshot.get("individualLimit")
            if individual is not None and individual["remainingPercent"] == 0:
                raise ValueError("quota")
        if usage._account(raw.request("account/read", {"refreshToken": False}, timeout=8)) != (plan, digest):
            raise ValueError("native_credit_binding_invalid")
    except ValueError as exc:
        if str(exc) in {"quota", "native_credit_binding_invalid", "native_quota_unknown"}:
            raise
        raise ValueError("native_quota_unknown") from None


def read_limits(raw, *, account=None, credit_account="") -> None:
    """Shared native pre-dispatch quota check; reads no credential material."""
    limits = raw.request("account/rateLimits/read", {}, timeout=8)
    if credit_account:
        _credit_limits(raw, limits, account, credit_account)
        return
    snapshots = list((limits.get("rateLimitsByLimitId") or {}).values())
    if not snapshots:
        snapshots = [limits.get("rateLimits")]
    if not any(isinstance(s, dict) for s in snapshots):
        raise ValueError("native_quota_unknown")
    for snapshot in snapshots:
        if not isinstance(snapshot, dict):
            raise ValueError("native_quota_unknown")
        if snapshot.get("spendControlReached") or snapshot.get("rateLimitReachedType"):
            raise ValueError("quota")
        measured = False
        for window in (snapshot.get("primary"), snapshot.get("secondary")):
            if window is None:
                continue
            used = window.get("usedPercent") if isinstance(window, dict) else None
            if type(used) not in (int, float) or not math.isfinite(used) or not 0 <= used <= 100:
                raise ValueError("native_quota_unknown")
            measured = True
            if used >= 100:
                raise ValueError("quota")
        if not measured:
            raise ValueError("native_quota_unknown")


def _preflight(raw, recorder: Recorder, args) -> list[dict]:
    account_response = raw.request("account/read", {"refreshToken": False}, timeout=8)
    account = account_response.get("account")
    if not isinstance(account, dict) or account.get("type") != "chatgpt":
        raise ValueError("logged_out" if account is None else "subscription_required")
    read_limits(raw, account=account_response,
                credit_account=getattr(args, "authorized_credit_account", ""))
    models = raw.request("model/list", {"includeHidden": False, "limit": 100}, timeout=8)
    if not any(isinstance(m, dict) and m.get("model") == args.model for m in models.get("data", [])):
        raise ValueError("native_model_unavailable")
    config = raw.request("config/read", {"cwd": args.workdir, "includeLayers": True}, timeout=8)
    expected_browser = getattr(args, "browser_entry", None)
    task_profile = getattr(args, "task_profile", None)
    _validate_layers(config, expected_browser, task_profile, args.codex_home)
    if task_profile is not None:
        task_profile.policy.verify_config(config)
    effective = config.get("config") or {}
    if (effective.get("model") != args.model or effective.get("model_provider") != "openai"
            or effective.get("web_search") != (task_profile.web_search if task_profile is not None else "live")
            or effective.get("approval_policy") != "never"
            or effective.get("sandbox_mode") != "read-only"
            or effective.get("service_tier") not in (None, "")):
        raise ValueError("native_config_mismatch")
    effective_mcp = effective.get("mcp_servers")
    if effective_mcp is None:
        effective_mcp = {}
    if json.dumps(effective_mcp, sort_keys=True) != json.dumps(_effective_browser_config(expected_browser), sort_keys=True):
        raise ValueError("native_config_mismatch")
    skills = raw.request("skills/list", {"cwds": [args.workdir], "forceReload": True}, timeout=8)
    disabled = []
    for entry in skills.get("data", []):
        if entry.get("errors"):
            raise ValueError("native_skills_unverified")
        for skill in entry.get("skills", []):
            path = skill.get("path")
            if not isinstance(path, str) or not os.path.isabs(path):
                raise ValueError("native_skills_unverified")
            disabled.append({"path": path, "enabled": False})
            if len(disabled) > 256:
                raise ValueError("native_skills_unverified")
    return disabled


def continuation_ids(mode: str, thread_id: str, turn_id: str) -> tuple[str, str]:
    """Closed, version-pinned transport metadata, never model-selected context."""
    if mode == "" and not thread_id and not turn_id:
        return "", ""
    if mode != "durable":
        raise ValueError("native_session_binding_invalid")
    if thread_id == "-" and turn_id == "-":
        return "", ""
    if (not _MODEL.fullmatch(thread_id) or not _MODEL.fullmatch(turn_id)
            or thread_id == "-" or turn_id == "-"):
        raise ValueError("native_session_binding_invalid")
    return thread_id, turn_id


def verify_continuation_thread(body: dict, *, thread_id: str, turn_id: str,
                               workspace: str) -> None:
    """Read-only reconciliation before resume; never start after an unknown turn.

    The Core separately requires a settled cost claim. A native transcript is
    evidence of its turn, not authority to resume an unrelated Core task.
    """
    thread = body.get("thread")
    if (not isinstance(thread, dict) or thread.get("id") != thread_id
            or thread.get("ephemeral") is not False
            or thread.get("modelProvider") != "openai"
            or thread.get("cwd") != str(Path(workspace).resolve())
            or (thread.get("status") or {}).get("type") not in {"idle", "notLoaded"}):
        raise ValueError("native_session_binding_invalid")
    turns = thread.get("turns")
    if (not isinstance(turns, list) or not turns
            or not isinstance(turns[-1], dict) or turns[-1].get("id") != turn_id
            or any(not isinstance(t, dict) or t.get("status") not in
                   {"completed", "failed", "interrupted"} for t in turns)):
        raise ValueError("native_previous_turn_unconfirmed")


def _run(args, prompt: str) -> dict:
    validate_home(args.codex_home, args.model)
    durable = getattr(args, "session_mode", "") == "durable"
    resume_thread, previous_turn = continuation_ids(getattr(args, "session_mode", ""),
        getattr(args, "resume_thread", ""), getattr(args, "previous_turn", ""))
    if durable:
        if not re.fullmatch(r"at-[a-f0-9]{16}", getattr(args, "task_id", "")):
            raise ValueError("native_session_binding_invalid")
        workspace = Path(args.workdir)
        if (not workspace.is_absolute() or workspace.is_symlink() or not workspace.is_dir()
                or str(workspace.resolve()) != args.workdir
                or workspace.stat().st_uid != os.getuid() or workspace.stat().st_mode & 0o077):
            raise ValueError("native_workspace_invalid")
    if Path(args.workdir).resolve().is_relative_to(Path(args.codex_home).resolve()):
        raise ValueError("native_workspace_invalid")
    sys.path.insert(0, args.hermes_source)
    from agent.transports.codex_app_server import CodexAppServerClient, CodexAppServerError
    from agent.transports.codex_app_server_session import CodexAppServerSession

    # No Hermes logging payload is a Core event. Our explicit projection below
    # is the only stdout contract; error strings never copy native stderr.
    logging.disable(logging.CRITICAL)
    recorder = Recorder()
    task_profile = None
    if getattr(args, "worker_profile", ""):
        if args.worker_profile != "task":
            raise ValueError("native_task_profile_invalid")
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from native_task_profile import NativeTaskProfile
        task_profile = NativeTaskProfile(args)
        args.task_profile = recorder.task_profile = task_profile
    elif getattr(args, "core_tools_socket", "") or getattr(args, "core_tools_digest", ""):
        raise ValueError("native_task_profile_invalid")
    deadline = time.monotonic() + args.timeout
    browser_control = None
    browser_sources = []
    if getattr(args, "browser_python", ""):
        # Only the reviewed sibling module, not arbitrary installed plugins.
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from hermes_browser import SERVER, TOOLS, control_dir, mcp_entry, validate_runtime, request_stop, closed_sources, codex_mcp_args
        validate_runtime(args.browser_python, args.browser_bin, args.browser_chrome, args.hermes_source)
        args.browser_entry = {SERVER: mcp_entry(python=args.browser_python, binary=args.browser_bin,
            chrome=args.browser_chrome, hermes_python=sys.executable, source=args.hermes_source,
            task_id=args.task_id, workdir=args.workdir, timeout=args.timeout)}
        browser_control = control_dir(args.workdir)
        browser_control.mkdir(mode=0o700)
        recorder.browser_tools = TOOLS

    class GuardedClient:
        def __init__(self, **kwargs):
            extra = ["--stdio", "--strict-config"]
            for feature in DISABLED_FEATURES:
                if task_profile is None or feature != "shell_tool":
                    extra += ["--disable", feature]
            if task_profile is not None:
                extra += list(task_profile.policy.cli_args())
            web = task_profile.web_search if task_profile is not None else "live"
            if web not in ("live", "disabled"):
                raise ValueError("native_task_profile_invalid")
            extra += ["-c", 'web_search="' + web + '"', "-c", "project_doc_max_bytes=0"]
            for config in (getattr(args, "browser_entry", None) or {}).values():
                extra += codex_mcp_args(config)
            self.raw = CodexAppServerClient(**kwargs, extra_args=extra)
            self.disabled_skills = []

        def __getattr__(self, name):
            return getattr(self.raw, name)

        def initialize(self, **kwargs):
            if task_profile is not None:
                kwargs["capabilities"] = {"experimentalApi": True}
            result = self.raw.initialize(**kwargs)
            try:
                self.disabled_skills = _preflight(self.raw, recorder, args)
            except ValueError as exc:
                recorder.fail(str(exc))
                raise CodexAppServerError(-32000, recorder.failure) from None
            return result

        def request(self, method, params, **kwargs):
            logical_method = method
            if method == "thread/start":
                params = {**params, "model": args.model, "modelProvider": "openai",
                          "sandbox": "read-only", "approvalPolicy": "never",
                          "approvalsReviewer": "user", "ephemeral": not durable,
                          "config": {"web_search": task_profile.web_search if task_profile is not None else "live",
                                     "skills": {"config": self.disabled_skills}}}
                if task_profile is not None:
                    params.pop("sandbox")
                    params["permissions"] = task_profile.profile_id
                    if not resume_thread:
                        params["dynamicTools"] = task_profile.tools
                if resume_thread:
                    try:
                        verify_continuation_thread(self.raw.request("thread/read", {
                            "threadId": resume_thread, "includeTurns": True}, **kwargs),
                            thread_id=resume_thread, turn_id=previous_turn, workspace=args.workdir)
                    except ValueError as exc:
                        recorder.fail(str(exc))
                        raise CodexAppServerError(-32000, recorder.failure) from None
                    method = "thread/resume"
                    params.pop("ephemeral")
                    params["threadId"] = resume_thread
            elif method == "turn/start":
                if recorder.failure:
                    raise CodexAppServerError(-32000, recorder.failure)
                # A lost request response may already have started a turn.
                recorder.turn_requested = True
                params = {**params, "model": args.model, "approvalPolicy": "never",
                          "approvalsReviewer": "user",
                          "outputSchema": RESULT_SCHEMA,
                          "sandboxPolicy": {"type": "readOnly", "networkAccess": False}}
                if task_profile is not None:
                    from native_task_profile import result_schema
                    params.pop("sandboxPolicy")
                    params["permissions"] = task_profile.profile_id
                    params["outputSchema"] = result_schema(RESULT_SCHEMA)
            try:
                result = self.raw.request(method, params, **kwargs)
            except CodexAppServerError as exc:
                recorder.fail(_error_reason(exc.data))
                raise
            if logical_method == "thread/start":
                sandbox = result.get("sandbox") or {}
                if (result.get("model") != args.model or result.get("modelProvider") != "openai"
                        or Path(result.get("cwd", "/")).resolve() != Path(args.workdir).resolve()
                        or result.get("approvalPolicy") != "never"
                        or result.get("approvalsReviewer") != "user"
                        or (task_profile is None and (sandbox.get("type") != "readOnly"
                            or sandbox.get("networkAccess") is not False))
                        or result.get("instructionSources") or result.get("serviceTier") not in (None, "")):
                    recorder.fail("native_policy_mismatch")
                    raise CodexAppServerError(-32000, recorder.failure)
                if task_profile is not None:
                    try:
                        task_profile.policy.verify_thread(result)
                    except ValueError:
                        recorder.fail("native_policy_mismatch")
                        raise CodexAppServerError(-32000, recorder.failure) from None
                thread = result.get("thread") or {}
                if durable and (thread.get("ephemeral") is not False
                        or (resume_thread and thread.get("id") != resume_thread)):
                    recorder.fail("native_session_binding_invalid")
                    raise CodexAppServerError(-32000, recorder.failure)
                if resume_thread:
                    try:
                        verify_continuation_thread(result, thread_id=resume_thread,
                            turn_id=previous_turn, workspace=args.workdir)
                    except ValueError as exc:
                        recorder.fail(str(exc))
                        raise CodexAppServerError(-32000, recorder.failure) from None
                recorder.thread_id = str(thread.get("id") or "")
                if recorder.browser_tools:
                    catalogue = self.raw.request("mcpServerStatus/list", {
                        "threadId": recorder.thread_id, "detail": "toolsAndAuthOnly", "limit": 2}, timeout=16)
                    servers = catalogue.get("data") or []
                    if (len(servers) != 1 or servers[0].get("name") != "solvio-browser"
                            or set(servers[0].get("tools") or {}) != set(recorder.browser_tools)
                            or any((v.get("annotations") or {}).get("readOnlyHint") is not True
                                   for v in servers[0]["tools"].values())
                            or catalogue.get("nextCursor")):
                        recorder.fail("native_browser_unavailable")
                        raise CodexAppServerError(-32000, recorder.failure)
            elif method == "turn/start":
                recorder.turn_id = str((result.get("turn") or {}).get("id") or "")
                if not recorder.thread_id or not recorder.turn_id or max(len(recorder.thread_id), len(recorder.turn_id)) > 160:
                    recorder.fail("native_protocol_error")
                    raise CodexAppServerError(-32000, recorder.failure)
                recorder.emit("started", runtime=RUNTIME, model=args.model,
                              thread_id=recorder.thread_id, turn_id=recorder.turn_id)
            return result

        def take_server_request(self, timeout=0):
            request = self.raw.take_server_request(timeout=timeout)
            if request is not None:
                if task_profile is not None and request.get("method") == "item/tool/call":
                    try:
                        reply = task_profile.call(request, thread_id=recorder.thread_id, turn_id=recorder.turn_id)
                        self.raw.respond(request.get("id"), reply)
                    except Exception:
                        self.raw.respond_error(request.get("id"), code=-32000,
                                               message="Core tool receipt unavailable")
                        recorder.fail("native_tool_transport_failed")
                    return None
                # No inherited Hermes auto-accept for the hermes-tools MCP.
                self.raw.respond_error(request.get("id"), code=-32601,
                                       message="SOLVIO research does not permit this request")
                recorder.fail("native_tool_not_allowed")
            return None

    session = CodexAppServerSession(cwd=args.workdir, codex_bin=args.codex_bin,
        codex_home=args.codex_home, client_factory=GuardedClient,
        approval_callback=lambda *_a, **_kw: "deny", on_event=recorder.on_event)
    recorder.session = session
    def stop(*_):
        if browser_control is not None:
            request_stop(args.workdir, cancelled=True)
        session.request_interrupt()
    old_handler = signal.signal(signal.SIGTERM, stop)
    result = None
    reason = ""
    try:
        session.ensure_started()
        result = session.run_turn(prompt, turn_timeout=max(0.05, deadline - time.monotonic()),
                                  notification_poll_timeout=0.02,
                                  post_tool_quiet_timeout=max(0.05, args.timeout))
        reason = recorder.failure
        if not reason and recorder.terminal != "completed":
            reason = "cancelled" if result.interrupted or recorder.terminal == "interrupted" else "native_terminal_missing"
        if not reason and result.error:
            reason = "provider_failed"
        if not reason and (not recorder.final_text or len(recorder.final_text) > MAX_TEXT):
            reason = "native_result_invalid"
    except Exception:
        reason = recorder.failure or "native_protocol_error"
    finally:
        if browser_control is not None:
            request_stop(args.workdir)
        if recorder.turn_id and recorder.terminal not in {"completed", "failed", "interrupted"}:
            try:
                session._client.request("turn/interrupt", {"threadId": recorder.thread_id,
                                        "turnId": recorder.turn_id}, timeout=1)
                # Acknowledging the RPC is only receipt of the stop request.
                # Drain the actual bound completion within the remaining turn
                # budget; missing completion remains a durable unknown claim.
                stop_deadline = min(deadline, time.monotonic() + 1)
                while time.monotonic() < stop_deadline:
                    note = session._client.take_notification(timeout=min(0.02, stop_deadline - time.monotonic()))
                    if note is not None:
                        recorder.on_event(note)
                    if recorder.terminal in {"completed", "failed", "interrupted"}:
                        break
            except Exception:
                pass
        if browser_control is not None and list(browser_control.glob("ready-*.json")):
            try:
                for source in closed_sources(args.workdir, args.task_id):
                    url = _source_url(source)
                    if url:
                        browser_sources.append(url)
            except (OSError, ValueError):
                reason = "native_browser_cleanup_failed"
        session.close()
        signal.signal(signal.SIGTERM, old_handler)
    result = {"schema": VERSION, "type": "result", "status": "completed" if not reason else "failed",
            "reason": reason, "model": args.model, "thread_id": recorder.thread_id,
            "turn_id": recorder.turn_id, "text": safe_text(recorder.final_text, MAX_TEXT) if not reason else "",
            "sources": recorder.sources, "browser_sources": browser_sources, "usage": recorder.usage,
            "terminal": recorder.terminal, "runtime": RUNTIME,
            "execution_status": recorder.execution_status(),
            **({"tool_receipts": task_profile.receipts} if task_profile is not None else {})}
    if task_profile is not None and len(json.dumps(result, ensure_ascii=True).encode()) > 32_768:
        from native_task_profile import fit_receipts
        budget = (32_768 - len(json.dumps(result, ensure_ascii=True).encode())
                  + len(json.dumps(result["tool_receipts"], ensure_ascii=True).encode()))
        try:
            result["tool_receipts"] = fit_receipts(result["tool_receipts"], budget)
        except ValueError:
            # Never silently drop evidence while returning a successful turn.
            result.update(status="failed", reason="native_result_too_large", text="", tool_receipts=[])
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--authorized-credit-account", default="")
    for name in ("hermes-source", "codex-bin", "codex-home", "model", "workdir"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--timeout", type=float, required=True)
    for name in ("browser-python", "browser-bin", "browser-chrome", "task-id",
                 "session-mode", "resume-thread", "previous-turn", "worker-profile",
                 "core-tools-socket", "core-tools-digest"):
        parser.add_argument("--" + name, default="")
    args = parser.parse_args()
    try:
        if not math.isfinite(args.timeout) or not 0.05 <= args.timeout <= 3600:
            raise ValueError("native_timeout_invalid")
        payload = sys.stdin.buffer.read(MAX_INPUT + 1)
        if len(payload) > MAX_INPUT:
            raise ValueError("native_input_too_large")
        prompt = payload.decode("utf-8")
        result = _run(args, prompt)
    except ValueError as exc:
        result = {"schema": VERSION, "type": "result", "status": "failed",
                  "reason": str(exc) if str(exc).startswith("native_") else "native_protocol_error"}
    except Exception:
        result = {"schema": VERSION, "type": "result", "status": "failed", "reason": "native_protocol_error"}
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
