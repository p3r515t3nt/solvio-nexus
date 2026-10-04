"""Bounded native Codex account observations, never a CostQuote.

Reuses Hermes' installed app-server client. No thread, turn, login, reset,
purchase or credential-file read occurs. A current empty credit balance is
not an account-level extra-usage switch or a guarantee against a later purchase.
The caller must bind actual execution policy and decide cost authority.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from decimal import Decimal
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile
import time
import tomllib

SCHEMA = "solvio.openai-usage-observation.v1"
MAX_INPUT = 24_000
MAX_LINE = 262_144
MAX_FRAMES = 48
MAX_AGE = 5.0
READ_TIMEOUT = 25.0
PLANS = frozenset({"free", "go", "plus", "pro", "prolite", "team",
    "self_serve_business_prolite", "self_serve_business_usage_based", "business",
    "ent26", "enterprise_cbp_automation", "enterprise_cbp_usage_based",
    "enterprise", "edu", "unknown"})
REACHED = frozenset({"rate_limit_reached", "workspace_owner_credits_depleted",
    "workspace_member_credits_depleted", "workspace_owner_usage_limit_reached",
    "workspace_member_usage_limit_reached"})
_FEATURES = ("shell_tool", "apps", "multi_agent", "hooks", "plugins", "remote_plugin",
    "memories", "browser_use", "browser_use_external", "browser_use_full_cdp_access",
    "computer_use", "image_generation", "view_image", "skill_mcp_dependency_install",
    "skill_search", "tool_suggest", "shell_snapshot", "goals", "workspace_dependencies",
    "fast_mode")
_FAILURE_CODES = frozenset({"account_invalid", "logged_out", "subscription_required",
    "account_binding_missing", "unknown_plan", "native_account_changed",
    "invalid_rate_snapshot", "invalid_rate_window", "invalid_credits", "invalid_amount",
    "invalid_spend_state", "invalid_individual_limit", "rate_limits_missing",
    "invalid_rate_buckets", "invalid_rate_bucket", "rate_bucket_binding_changed",
    "codex_bucket_missing", "rate_views_conflict", "plan_binding_changed",
    "native_line_oversize", "unexpected_native_frame", "unexpected_native_response",
    "native_frame_limit", "duplicate_json_key", "invalid_json_number",
    "native_read_incomplete", "native_timeout", "native_request_rejected",
    "native_remote_control_active"})


def _failure_reason(error):
    if type(error) is ValueError and str(error) in _FAILURE_CODES:
        return str(error)
    if isinstance(error, TimeoutError):
        return "native_timeout"
    return "usage_unverified"


@dataclass(frozen=True)
class _ReviewedBuild:
    executable: str
    executable_sha256: str
    native: str
    native_sha256: str
    version: str
    hermes_source: str
    transport_sha256: str
    hermes_python: str


REVIEWED_BUILD = _ReviewedBuild(
    "/opt/homebrew/lib/node_modules/@openai/codex/bin/codex.js",
    "134063e133f0b4244fa3b251acf973d4fe4b4aeeacbdc135211bf480f59f1477",
    "/opt/homebrew/lib/node_modules/@openai/codex/node_modules/@openai/"
    "codex-darwin-arm64/vendor/aarch64-apple-darwin/bin/codex",
    "19c4f144c5226a9f17c58e6f0fa854843b0f77a6eb420f40e2745a12f10f5d37",
    "0.147.0", "/Users/solvio/.solvio-hermes/src",
    "1bd7e181aa18e7a1417708ed8b691f3523657096d4fae51a268d8fa6d7afca57",
    "/Users/solvio/.solvio-hermes/venv/bin/python")


def _digest(value):
    return hashlib.sha256(b"SOLVIO_OPENAI_USAGE_V1\0" + json.dumps(value,
        sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _file_digest(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate_json_key")
            result[key] = value
        return result
    def constant(_):
        raise ValueError("invalid_json_number")
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def _number(value, *, maximum=None):
    return (type(value) is int and value >= 0
            and (maximum is None or value <= maximum))


def _amount(value):
    if type(value) is not str or not re.fullmatch(r"\d{1,24}(?:\.\d{1,12})?", value):
        raise ValueError("invalid_amount")
    return value


def _window(value):
    if value is None:
        return None
    if (type(value) is not dict or set(value) - {"usedPercent", "windowDurationMins", "resetsAt"}
            or not _number(value.get("usedPercent"), maximum=100)):
        raise ValueError("invalid_rate_window")
    for key in ("windowDurationMins", "resetsAt"):
        if value.get(key) is not None and not _number(value[key]):
            raise ValueError("invalid_rate_window")
    return {key: value.get(key) for key in ("usedPercent", "windowDurationMins", "resetsAt")}


def _snapshot(value):
    allowed = {"limitId", "limitName", "planType", "primary", "secondary", "credits",
               "individualLimit", "spendControlReached", "rateLimitReachedType"}
    if type(value) is not dict or set(value) - allowed:
        raise ValueError("invalid_rate_snapshot")
    if value.get("planType") not in PLANS | {None}:
        raise ValueError("unknown_plan")
    reached = value.get("rateLimitReachedType")
    spend = value.get("spendControlReached")
    if reached not in REACHED | {None} or (spend is not None and type(spend) is not bool):
        raise ValueError("invalid_spend_state")
    credits = value.get("credits")
    if credits is not None:
        if (type(credits) is not dict or set(credits) - {"hasCredits", "unlimited", "balance"}
                or type(credits.get("hasCredits")) is not bool
                or type(credits.get("unlimited")) is not bool):
            raise ValueError("invalid_credits")
        balance = credits.get("balance")
        if balance is not None:
            _amount(balance)
        credits = {"hasCredits": credits["hasCredits"], "unlimited": credits["unlimited"],
                   "balance": balance}
    individual = value.get("individualLimit")
    if individual is not None:
        if (type(individual) is not dict
                or set(individual) != {"limit", "used", "remainingPercent", "resetsAt"}
                or not _number(individual["remainingPercent"], maximum=100)
                or not _number(individual["resetsAt"])):
            raise ValueError("invalid_individual_limit")
        individual = {**individual, "limit": _amount(individual["limit"]),
                      "used": _amount(individual["used"])}
    # Labels can carry arbitrary text. They are not billing evidence and never leave the reader.
    return {"planType": value.get("planType"), "primary": _window(value.get("primary")),
        "secondary": _window(value.get("secondary")), "credits": credits,
        "individualLimit": individual, "spendControlReached": spend,
        "rateLimitReachedType": reached}


def _limits(body, plan):
    if type(body) is not dict or "rateLimits" not in body:
        raise ValueError("rate_limits_missing")
    legacy = _snapshot(body["rateLimits"])
    multi = body.get("rateLimitsByLimitId")
    if multi is None:
        snapshots = {"codex": legacy}
    else:
        if type(multi) is not dict or not 1 <= len(multi) <= 16:
            raise ValueError("invalid_rate_buckets")
        snapshots = {}
        for key, value in multi.items():
            if type(key) is not str or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", key):
                raise ValueError("invalid_rate_bucket")
            if type(value) is not dict or value.get("limitId") not in (None, key):
                raise ValueError("rate_bucket_binding_changed")
            snapshots[key] = _snapshot(value)
        if "codex" not in snapshots:
            raise ValueError("codex_bucket_missing")
        legacy_id = body["rateLimits"].get("limitId") or "codex"
        if legacy_id not in snapshots or snapshots[legacy_id] != legacy:
            raise ValueError("rate_views_conflict")
    if any(item["planType"] not in (None, plan) for item in snapshots.values()):
        raise ValueError("plan_binding_changed")
    return snapshots


def _account(body):
    if type(body) is not dict or type(body.get("requiresOpenaiAuth")) is not bool:
        raise ValueError("account_invalid")
    account = body.get("account")
    if account is None:
        raise ValueError("logged_out")
    if type(account) is not dict or account.get("type") != "chatgpt":
        raise ValueError("subscription_required")
    plan, email = account.get("planType"), account.get("email")
    if plan not in PLANS - {"unknown"} or body["requiresOpenaiAuth"] is not True:
        raise ValueError("account_invalid")
    if type(email) is not str or not 1 <= len(email) <= 320 or "@" not in email:
        raise ValueError("account_binding_missing")
    return plan, _digest({"type": "chatgpt", "plan": plan, "email": email})


def _context(invocation, build):
    from solvio.specialists.launcher import Invocation
    if type(invocation) is not Invocation:
        raise ValueError("invalid_invocation")
    source = Path(build.hermes_source).resolve(strict=True)
    binary = str(Path(build.native).resolve(strict=True))
    executable = str(Path(build.executable).resolve(strict=True))
    if (_file_digest(binary) != build.native_sha256
            or _file_digest(executable) != build.executable_sha256
            or _file_digest(source / "agent/transports/codex_app_server.py") != build.transport_sha256):
        raise ValueError("unreviewed_native_build")
    requested = str(Path(invocation.executable).resolve(strict=True))
    home = invocation.codex_home or str(Path(os.environ["HOME"]) / ".codex")
    if requested not in {binary, executable}:
        argv = invocation.argv
        if (requested != str(Path(build.hermes_python).resolve(strict=True))
                or len(argv) < 3 or argv[:2] != ("-I", "-B")
                or Path(argv[2]).resolve() not in {
                    Path(__file__).with_name("hermes_native_worker.py").resolve(),
                    Path(__file__).with_name("hermes_research_worker.py").resolve(),
                    Path(__file__).with_name("image_generation_worker.py").resolve()}
                or len(argv[3:]) % 2):
            raise ValueError("invalid_native_invocation")
        options = dict(zip(argv[3::2], argv[4::2]))
        expected = {"--hermes-source", "--codex-bin", "--codex-home", "--model", "--workdir", "--timeout"}
        if "--browser-python" in options:
            expected |= {"--browser-python", "--browser-bin", "--browser-chrome", "--task-id"}
        if "--session-mode" in options:
            expected |= {"--task-id", "--session-mode", "--resume-thread", "--previous-turn"}
        if "--worker-profile" in options:
            expected |= {"--worker-profile", "--core-tools-socket", "--core-tools-digest"}
            if "--session-mode" not in options:
                raise ValueError("invalid_native_invocation")
        if (set(options) != expected
                or len(options) * 2 != len(argv[3:])
                or Path(options["--hermes-source"]).resolve() != source
                or str(Path(options["--codex-bin"]).resolve()) not in {binary, executable}
                or options["--codex-home"] != invocation.codex_home
                or options["--workdir"] != invocation.cwd):
            raise ValueError("invalid_native_invocation")
        if "--browser-python" in options or "--session-mode" in options:
            # This is the exact existing research factory, not a general
            # permission to add MCP, executables or arguments to a priced call.
            from solvio.specialists.hermes_native import NativeResearchConfig, worker_invocation
            browser = NativeResearchConfig(invocation.executable, options["--hermes-source"],
                options["--codex-bin"], invocation.codex_home, options["--model"],
                timeout_s=float(options["--timeout"]), shutdown_grace_s=invocation.shutdown_grace,
                browser_python=options.get("--browser-python", ""), browser_bin=options.get("--browser-bin", ""),
                browser_chrome=options.get("--browser-chrome", ""))
            browser.validate()
            if "--worker-profile" in options:
                from solvio.specialists.native_task import task_invocation
                expected_invocation = task_invocation(browser, invocation.cwd,
                    task_id=options["--task-id"], endpoint=options["--core-tools-socket"],
                    manifest_digest=options["--core-tools-digest"],
                    native_thread_id="" if options["--resume-thread"] == "-" else options["--resume-thread"],
                    previous_turn_id="" if options["--previous-turn"] == "-" else options["--previous-turn"])
            elif "--session-mode" in options:
                from solvio.specialists.hermes_native import continuation_invocation
                expected_invocation = continuation_invocation(browser, invocation.cwd,
                    task_id=options["--task-id"],
                    native_thread_id="" if options["--resume-thread"] == "-" else options["--resume-thread"],
                    previous_turn_id="" if options["--previous-turn"] == "-" else options["--previous-turn"])
            else:
                expected_invocation = worker_invocation(browser, invocation.cwd, task_id=options["--task-id"])
            if invocation != expected_invocation:
                raise ValueError("invalid_native_invocation")
    else:
        for index, arg in enumerate(invocation.argv):
            if (arg in {"--oss", "--local-provider", "--profile", "-p"}
                    or arg.startswith(("--local-provider=", "--profile="))
                    or (arg.startswith("-p") and not arg.startswith("--") and len(arg) > 2)):
                raise ValueError("subscription_required")
            override = None
            if arg in {"-c", "--config"}:
                if index + 1 >= len(invocation.argv):
                    raise ValueError("invalid_invocation")
                override = invocation.argv[index + 1]
            elif arg.startswith("--config="):
                override = arg.partition("=")[2]
            elif arg.startswith("-c") and not arg.startswith("--") and len(arg) > 2:
                override = arg[2:]
            if override is not None:
                value = tomllib.loads(override)
                if (value.get("model_provider", "openai") != "openai"
                        or "model_providers" in value
                        or value.get("forced_login_method", "chatgpt") != "chatgpt"):
                    raise ValueError("subscription_required")
    home_path = Path(home)
    if not home_path.is_absolute() or not home_path.is_dir() or home_path.is_symlink():
        raise ValueError("native_home_unverified")
    home = str(home_path.resolve(strict=True))
    cwd = str(Path(invocation.cwd).resolve(strict=True))
    if not Path(cwd).is_dir():
        raise ValueError("invalid_invocation")
    # Metadata only: native auth files are never opened. Home/config changes
    # invalidate the binding; auth consistency comes from the two native reads.
    metadata = {}
    for name in ("config.toml", "requirements.toml"):
        path = Path(home, name)
        if path.exists():
            info = path.stat()
            metadata[name] = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    context = _digest({"home": home, "cwd": cwd, "build": asdict(build), "config": metadata})
    return _digest(asdict(invocation)), context, home, cwd, binary


@dataclass(frozen=True)
class UsageObservation:
    state: str = "unknown"  # observed is a native snapshot, never a EUR claim
    reason: str = "usage_unverified"
    auth_type: str = ""
    plan_type: str = ""
    snapshots_json: str = "{}"
    account_digest: str = ""
    invocation_digest: str = ""
    context_digest: str = ""
    config_digest: str = ""
    observed_at: float = 0.0
    cli_version: str = ""

    @property
    def snapshots(self):
        return _json(self.snapshots_json)

    @property
    def codex_credits(self):
        return self.snapshots.get("codex", {}).get("credits")

    @property
    def empty_credit_balance(self):
        credits = self.codex_credits
        return (self.state == "observed" and credits is not None
                and all(item["hasCredits"] is False and item["unlimited"] is False
                        and item["balance"] is not None and Decimal(item["balance"]) == 0
                        for snapshot in self.snapshots.values()
                        if (item := snapshot.get("credits")) is not None))

    def applies_to(self, invocation):
        try:
            inv, context, *_ = _context(invocation, REVIEWED_BUILD)
        except (KeyError, OSError, ValueError, TypeError):
            return False
        age = time.monotonic() - self.observed_at
        return (self.state == "observed" and 0 <= age <= MAX_AGE
                and self.invocation_digest == inv and self.context_digest == context)


class NativeUsageReader:
    """Core-only reader; private build/runner seams are for local contract tests."""
    def __init__(self, *, _build=None, _runner=None, _timeout=READ_TIMEOUT):
        self._build, self._runner, self._timeout = _build or REVIEWED_BUILD, _runner, _timeout

    async def read(self, invocation):
        from solvio.specialists import launcher as L
        try:
            inv, context, home, cwd, native = _context(invocation, self._build)
            if (type(self._timeout) not in (int, float) or not math.isfinite(self._timeout)
                    or not 0.05 <= self._timeout <= READ_TIMEOUT):
                raise ValueError("invalid_reader_timeout")
            with tempfile.TemporaryDirectory(prefix="solvio-openai-usage-") as directory:
                request = {"schema": SCHEMA, "request_id": secrets.token_hex(16),
                    "build": asdict(self._build), "home": home, "cwd": cwd,
                    "native": native, "timeout": self._timeout,
                    "invocation_digest": inv, "context_digest": context}
                worker = L.Invocation(self._build.hermes_python,
                    ("-I", "-B", str(Path(__file__).resolve()), "--worker"),
                    cwd=directory, codex_home=home, timeout=self._timeout + 2, cleanup_group=True)
                started = time.monotonic()
                outcome = await (self._runner or L.run)(worker, json.dumps(request),
                    on_stdout_line=lambda line: None)
                ended = time.monotonic()
                if not outcome.ok:
                    negative = _json(outcome.text)
                    if (type(negative) is dict and set(negative) == {"schema", "state", "reason"}
                            and negative.get("schema") == SCHEMA and negative.get("state") == "unknown"
                            and negative.get("reason") in _FAILURE_CODES):
                        return UsageObservation(reason=negative["reason"])
                if (not outcome.ok or outcome.exit_code != 0 or outcome.truncated
                        or outcome.process_started is not True
                        or not 0 <= ended - started <= self._timeout + 2
                        or _context(invocation, self._build)[:2] != (inv, context)):
                    raise ValueError("native_read_incomplete")
                body = _json(outcome.text)
                expected = {"schema", "request_id", "invocation_digest", "context_digest",
                    "account_digest", "plan_type", "snapshots", "config_digest", "cli_version"}
                if (type(body) is not dict or set(body) != expected
                        or any(body[key] != request[key] for key in
                               ("schema", "request_id", "invocation_digest", "context_digest"))
                        or body["plan_type"] not in PLANS - {"unknown"}
                        or body["cli_version"] != self._build.version
                        or any(type(body[key]) is not str or not re.fullmatch(r"[a-f0-9]{64}", body[key])
                               for key in ("account_digest", "config_digest"))):
                    raise ValueError("native_observation_invalid")
                snapshots = _limits({"rateLimits": body["snapshots"].get("codex"),
                    "rateLimitsByLimitId": body["snapshots"]}, body["plan_type"])
                return UsageObservation(state="observed", reason="", auth_type="chatgpt",
                    plan_type=body["plan_type"], snapshots_json=json.dumps(snapshots, sort_keys=True),
                    account_digest=body["account_digest"], invocation_digest=inv,
                    context_digest=context, config_digest=body["config_digest"],
                    observed_at=ended, cli_version=self._build.version)
        except asyncio.CancelledError:
            raise
        except (KeyError, OSError, ValueError, TypeError, UnicodeError, AttributeError):
            return UsageObservation()


def _worker(request):
    build = _ReviewedBuild(**request["build"])
    if (request["schema"] != SCHEMA or not 0.05 <= request["timeout"] <= READ_TIMEOUT
            or _file_digest(request["native"]) != build.native_sha256
            or _file_digest(Path(build.hermes_source) / "agent/transports/codex_app_server.py")
                != build.transport_sha256):
        raise ValueError("unreviewed_native_build")
    sys.path.insert(0, build.hermes_source)
    logging.disable(logging.CRITICAL)
    from agent.transports.codex_app_server import CodexAppServerClient

    class BoundedClient(CodexAppServerClient):
        # Keep Hermes' request IDs, queues, send and lifecycle; just bound and
        # validate incoming framing before its existing dispatcher receives it.
        invalid = False
        invalid_reason = "unexpected_native_frame"

        def _read_stdout(self):
            try:
                for _ in range(MAX_FRAMES):
                    line = self._proc.stdout.readline(MAX_LINE + 1)
                    if not line:
                        return
                    if len(line) > MAX_LINE:
                        raise ValueError("native_line_oversize")
                    message = _json(line)
                    if (type(message) is dict and "id" not in message
                            and message.get("method") == "remoteControl/status/changed"):
                        # 0.147.0 emits this during initialize, without a turn.
                        # Its native machine identity is not account evidence.
                        params = message.get("params")
                        if (type(params) is not dict
                                or set(params) - {"status", "environmentId", "installationId", "serverName"}
                                or params.get("status") != "disabled"
                                or params.get("environmentId") is not None
                                or any(type(params.get(key)) is not str or not 0 < len(params[key]) <= 256
                                       for key in ("installationId", "serverName"))):
                            raise ValueError("native_remote_control_active")
                        continue
                    if type(message) is not dict or not {"id", "result"} <= set(message) or "method" in message or "error" in message:
                        raise ValueError("unexpected_native_frame")
                    with self._pending_lock:
                        if type(message["id"]) is not int or message["id"] not in self._pending:
                            raise ValueError("unexpected_native_response")
                    self._dispatch(message)
                raise ValueError("native_frame_limit")
            except Exception as error:
                self.invalid = True
                self.invalid_reason = _failure_reason(error)

        def _read_stderr(self):
            count = 0
            while chunk := self._proc.stderr.read(8192):
                count += len(chunk)
                if count > MAX_LINE:
                    self.invalid = True
                    return

    extra = ["--stdio", "--strict-config"]
    for feature in _FEATURES:
        extra += ["--disable", feature]
    extra += ["-c", 'approval_policy="never"', "-c", 'sandbox_mode="read-only"',
              "-c", 'web_search="disabled"', "-c", "project_doc_max_bytes=0"]
    client = BoundedClient(codex_bin=request["native"], codex_home=request["home"], extra_args=extra)
    try:
        deadline = time.monotonic() + request["timeout"]
        def read(method, params):
            timeout = deadline - time.monotonic()
            if timeout <= 0 or client.invalid:
                raise ValueError("native_read_incomplete")
            return client.request(method, params, timeout=min(timeout, 6))
        client.initialize(client_name="solvio_usage_reader", client_title="SOLVIO usage reader",
                          client_version="1", timeout=min(request["timeout"], 6))
        first = _account(read("account/read", {"refreshToken": False}))
        config = read("config/read", {"cwd": request["cwd"], "includeLayers": True})
        if (type(config) is not dict or type(config.get("config")) is not dict
                or config["config"].get("model_provider") not in (None, "openai")
                or config["config"].get("forced_login_method") not in (None, "chatgpt")
                or config["config"].get("model_providers")):
            raise ValueError("subscription_required")
        snapshots = _limits(read("account/rateLimits/read", {}), first[0])
        second = _account(read("account/read", {"refreshToken": False}))
        if first != second or client.invalid:
            raise ValueError("native_account_changed")
        config_digest = _digest(config)
    except Exception:
        if client.invalid:
            raise ValueError(client.invalid_reason) from None
        raise
    finally:
        client.close(timeout=1)
        client._reader.join(timeout=1)
        client._stderr_reader.join(timeout=1)
    if (client.invalid or client.is_alive() or client._reader.is_alive()
            or client._stderr_reader.is_alive()):
        raise ValueError("native_read_incomplete")
    return {key: request[key] for key in ("schema", "request_id", "invocation_digest", "context_digest")} | {
        "account_digest": first[1], "plan_type": first[0], "snapshots": snapshots,
        "config_digest": config_digest, "cli_version": build.version}


if __name__ == "__main__":
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT or sys.argv[1:] != ["--worker"]:
            raise ValueError("invalid_request")
        print(json.dumps(_worker(_json(raw)), allow_nan=False), flush=True)
    except Exception as error:
        # No raw native response, identity, config, credential or exception text.
        print(json.dumps({"schema": SCHEMA, "state": "unknown", "reason": _failure_reason(error)}), flush=True)
        raise SystemExit(1)
