"""Native Claude usage observations, never cost authority by themselves.

The worker sends only native control frames. Its parent owns the existing Launcher
process group; native children do not start a new group. The parent owns and
deletes the temporary diagnostic directory, including on timeout/cancellation.

The experimental native response hides its seeded-cache flag. A reviewed
executable, complete control exchange, diagnostic AND freshly persisted native
account-bound usage record are needed. Diagnostics alone hide one native error
path. The native cache writer throttles for five minutes: the same native record
may support a new read with identical complete extra_usage controls for at most
300 seconds. Plan-window utilization may change independently. This is identified as
recent_native_cache, never a proven fresh server response. No cache is changed.
No observation is a CostQuote:
the integration must bind the actual inference invocation/configuration and
account-control validity separately. A later concurrent account change remains
outside what this read-only observation can prevent.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, fields
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import tempfile
import time

SCHEMA = "solvio.claude-usage-observation.v1"
MAX_INPUT = 32_768
MAX_LINE = 262_144
MAX_DEBUG = 2_000_000
MAX_CONFIG = 4_000_000
MAX_STDOUT = 786_432
MAX_AGE = 5.0
MAX_CACHE_AGE_S = 300.0
READ_TIMEOUT = 35.0
GET = "fetchUtilization: GET"
HTTP_OK = "fetchUtilization: 200 after"
BODY_ERROR = "Usage fetch returned a fieldless or non-object body (in-band error)"
FETCH_ERROR = "Failed to load usage data"


@dataclass(frozen=True)
class _ReviewedBuild:
    executable: str
    version: str
    sha256: str


REVIEWED_BUILD = _ReviewedBuild(
    "/opt/homebrew/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe",
    "2.1.261", "5efecaff231b798be3c66def9be54183623b328b80eaef17f93c43987024e82a")


def _digest(value) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True, allow_nan=False).encode()
    return hashlib.sha256(b"SOLVIO_CLAUDE_USAGE_V1\0" + payload).hexdigest()


def _file_digest(path: str) -> str:
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _finite_number(value, *, nullable=False):
    return ((nullable and value is None) or
            (type(value) in (int, float) and math.isfinite(value) and value >= 0))


def _json(raw):
    def reject_constant(_):
        raise ValueError("invalid_json_constant")

    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_json_key")
            result[key] = value
        return result

    return json.loads(raw, parse_constant=reject_constant, object_pairs_hook=unique_pairs)


def _context(invocation, build: _ReviewedBuild):
    # Digests only leave this function. Argument/cwd/HOME strings are never
    # emitted by this reader. The actual inference policy is Root-owned.
    from solvio.specialists.launcher import Invocation
    if type(invocation) is not Invocation:
        raise ValueError("invalid_invocation")
    actual = str(Path(invocation.executable).resolve(strict=True))
    expected = str(Path(build.executable).resolve(strict=True))
    if actual != expected or _file_digest(expected) != build.sha256:
        raise ValueError("unreviewed_cli_binary")
    home = str(Path(os.environ["HOME"]).resolve(strict=True))
    if not Path(home).is_dir() or not os.path.isabs(home):
        raise ValueError("native_home_unverified")
    invocation_body = {field.name: getattr(invocation, field.name)
                       for field in fields(invocation)}
    invocation_digest = _digest(invocation_body)
    # The read profile is fixed, so it cannot claim that arbitrary inference
    # flags/config were enforced. This identifies observation context only.
    context_digest = _digest({"home": home, "binary": expected,
        "sha256": build.sha256, "version": build.version,
        "profile": "isolated-native-control-v1"})
    return invocation_digest, context_digest, home, expected


@dataclass(frozen=True)
class UsageObservation:
    state: str = "unknown"  # disabled, enabled, or unknown; never EUR
    reason: str = "usage_unverified"
    subscription: str = ""
    invocation_digest: str = ""
    context_digest: str = ""
    diagnostic_digest: str = ""
    account_digest: str = ""
    cache_digest: str = ""
    fetched_at_ms: int = 0
    freshness: str = "unknown"
    observed_at: float = 0.0
    cli_version: str = ""

    def applies_to(self, invocation) -> bool:
        """A short-lived observation binding; NOT a spending authorization."""
        try:
            inv, context, home, _ = _context(invocation, REVIEWED_BUILD)
            metadata = _metadata(home)
            account_digest, cache_digest = _account_digest(metadata), _digest(metadata["cache"])
        except (KeyError, OSError, ValueError, TypeError):
            return False
        age = time.monotonic() - self.observed_at
        return (self.state in {"disabled", "enabled"} and 0 <= age <= MAX_AGE
                and 0 <= time.time() * 1000 - self.fetched_at_ms <= MAX_CACHE_AGE_S * 1000
                and self.invocation_digest == inv and self.context_digest == context
                and self.account_digest == account_digest and self.cache_digest == cache_digest)


def _account_digest(metadata):
    return _digest({key: metadata[key] for key in ("path", "uuid", "email")})


def _metadata(home: str) -> dict:
    """Only the native usage/account metadata projection, never auth storage.

    Native 2.1.261 uses ~/.claude.json, or its existing legacy
    ~/.claude/.config.json. Neither path is a token/keychain file. The global
    record can have secret siblings: standard bounded JSON parsing is followed
    immediately by projection; no raw record, sibling or error text escapes.
    """
    flags = os.O_RDONLY | os.O_NOFOLLOW
    with_fd = os.open(home, flags | os.O_DIRECTORY)
    legacy_fd = None
    try:
        name, directory, kind = ".claude.json", with_fd, "home"
        try:
            legacy_fd = os.open(".claude", flags | os.O_DIRECTORY, dir_fd=with_fd)
        except FileNotFoundError:
            pass
        if legacy_fd is not None:
            try:
                os.stat(".config.json", dir_fd=legacy_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                name, directory, kind = ".config.json", legacy_fd, "legacy"
        fd = os.open(name, flags, dir_fd=directory)
        try:
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or before.st_mode & 0o077 or not 0 < before.st_size <= MAX_CONFIG):
                raise ValueError("native_metadata_unverified")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                raw = stream.read(MAX_CONFIG + 1)
            after = os.fstat(fd)
            if (len(raw) > MAX_CONFIG or (before.st_size, before.st_mtime_ns) !=
                    (after.st_size, after.st_mtime_ns)):
                raise ValueError("native_metadata_changed")
            parsed = _json(raw)
            del raw
            if type(parsed) is not dict or type(parsed.get("oauthAccount")) is not dict:
                raise ValueError("native_account_missing")
            account = parsed["oauthAccount"]
            uuid, email = account.get("accountUuid"), account.get("emailAddress")
            if (not isinstance(uuid, str) or not re.fullmatch(r"[A-Za-z0-9-]{8,128}", uuid)
                    or not isinstance(email, str) or not 3 <= len(email) <= 320
                    or "@" not in email or any(ord(c) < 32 for c in email)):
                raise ValueError("native_account_invalid")
            return {"path": kind, "uuid": uuid, "email": email,
                    "cache": parsed.get("cachedUsageUtilization")}
        finally:
            os.close(fd)
    finally:
        if legacy_fd is not None:
            os.close(legacy_fd)
        os.close(with_fd)


def _cache_proof(before, after, init_account, usage, started_ms, ended_ms):
    if (_account_digest(before) != _account_digest(after)
            or init_account.get("email") != after["email"]):
        raise ValueError("native_account_changed")
    cache = after["cache"]
    # The native writer throttles all utilization data together. Ongoing usage
    # changes seven_day/limits between reads without changing billing policy.
    # Bind the complete extra_usage object (including nullable/unknown added
    # fields and disabled_reason); never compare only its is_enabled boolean.
    if (type(cache) is not dict or
            not {"fetchedAtMs", "accountUuid", "utilization"} <= cache.keys()
            or cache["accountUuid"] != after["uuid"]
            or type(cache["fetchedAtMs"]) is not int
            or not 0 <= ended_ms - cache["fetchedAtMs"] <= MAX_CACHE_AGE_S * 1000
            or type(cache["utilization"]) is not dict
            or cache["utilization"].get("extra_usage") != usage["rate_limits"]["extra_usage"]):
        raise ValueError("native_cache_freshness_unverified")
    old = before["cache"]
    written_during_read = (started_ms < cache["fetchedAtMs"] and
        (type(old) is not dict or old.get("fetchedAtMs") != cache["fetchedAtMs"]))
    return {"account_digest": _account_digest(after),
            "cache_digest": _digest(cache), "fetched_at_ms": cache["fetchedAtMs"],
            "freshness": "fresh_native_cache" if written_during_read else "recent_native_cache"}


def _diagnostic(raw: bytes) -> dict:
    if not raw or len(raw) > MAX_DEBUG:
        raise ValueError("diagnostic_missing_or_oversize")
    text = raw.decode("utf-8", "strict")
    # HTTP200 precedes body validation in this exact reviewed binary. Both
    # known fallback errors and *any* native warning/error invalidate success.
    if (text.count(GET) != 1 or text.count(HTTP_OK) != 1
            or text.index(GET) >= text.index(HTTP_OK)
            or BODY_ERROR in text or FETCH_ERROR in text
            or re.search(r"\[(?:ERROR|WARN)\]", text, re.IGNORECASE)):
        raise ValueError("native_fetch_not_verified")
    return {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
            "fetches": 1, "http_200": 1, "errors": 0}


def _usage(body, account) -> tuple[str, str]:
    # Absence differs from an explicit null. The old probe's .get() shape was
    # deliberately only an informational whitelist, not sufficient here.
    required = {"subscription_type", "rate_limits_available", "rate_limits",
                "session", "behaviors"}
    if type(body) is not dict or not required <= body.keys():
        raise ValueError("usage_shape_invalid")
    if type(account) is not dict or account.get("apiProvider") != "firstParty":
        raise ValueError("account_route_mismatch")
    plan = body["subscription_type"]
    if plan not in {"pro", "max"}:
        raise ValueError("unsupported_subscription")
    # Some native versions include a plan label in initialize; when present
    # it may not contradict the actual usage subscription. No invented field
    # is required from versions whose native initialize lacks it.
    init_plan = account.get("subscriptionType")
    if init_plan != {"max": "Claude Max", "pro": "Claude Pro"}[plan]:
        raise ValueError("account_plan_mismatch")
    if body["rate_limits_available"] is not True or body["behaviors"] is not None:
        raise ValueError("usage_unavailable")
    limits = body["rate_limits"]
    extra = limits.get("extra_usage") if type(limits) is dict else None
    required_extra = {"is_enabled", "monthly_limit", "used_credits", "utilization"}
    if (type(extra) is not dict or not required_extra <= extra.keys()
            or type(extra["is_enabled"]) is not bool
            or not all(_finite_number(extra[key], nullable=True)
                       for key in required_extra - {"is_enabled"})
            or extra.get("currency") not in {None, "EUR", "USD"}
            or (extra.get("disabled_reason") is not None
                and (not isinstance(extra["disabled_reason"], str)
                     or len(extra["disabled_reason"]) > 128))):
        raise ValueError("usage_shape_invalid")
    session = body["session"]
    if (type(session) is not dict or
            not {"total_cost_usd", "total_api_duration_ms", "model_usage"} <= session.keys()
            or not _finite_number(session["total_cost_usd"])
            or not _finite_number(session["total_api_duration_ms"])
            or session["total_cost_usd"] != 0 or session["total_api_duration_ms"] != 0
            or type(session["model_usage"]) is not dict or session["model_usage"]):
        raise ValueError("unexpected_model_activity")
    return ("enabled" if extra["is_enabled"] else "disabled"), plan


def _validated_result(body, *, request, started, ended, outcome):
    required = {"schema", "request_id", "invocation_digest", "context_digest",
                "state", "subscription", "observed_at", "diagnostic", "native_exit",
                "responses", "cli_version", "cli_sha256", "account_digest",
                "cache_digest", "fetched_at_ms", "freshness"}
    if (not outcome.ok or outcome.exit_code != 0 or outcome.truncated
            or type(body) is not dict or set(body) != required
            or body["schema"] != SCHEMA or body["native_exit"] != 0
            or type(body["native_exit"]) is not int or body["responses"] != 3
            or type(body["responses"]) is not int
            or body["state"] not in {"enabled", "disabled"}
            or body["subscription"] not in {"pro", "max"}
            or body["cli_version"] != request["version"]
            or body["cli_sha256"] != request["sha256"]
            or any(body[key] != request[key] for key in
                   ("request_id", "invocation_digest", "context_digest"))):
        raise ValueError("native_observation_invalid")
    observed = body["observed_at"]
    if (not _finite_number(observed) or not started <= observed <= ended
            or not 0 <= time.monotonic() - observed <= MAX_AGE):
        raise ValueError("usage_observation_stale")
    diag = body["diagnostic"]
    if (type(diag) is not dict or set(diag) != {"sha256", "bytes", "fetches", "http_200", "errors"}
            or not isinstance(diag["sha256"], str)
            or re.fullmatch(r"[a-f0-9]{64}", diag["sha256"]) is None
            or type(diag["bytes"]) is not int or not 0 < diag["bytes"] <= MAX_DEBUG
            or any(type(diag[k]) is not int for k in ("fetches", "http_200", "errors"))
            or (diag["fetches"], diag["http_200"], diag["errors"]) != (1, 1, 0)):
        raise ValueError("native_diagnostic_invalid")
    if (any(not isinstance(body[k], str) or not re.fullmatch(r"[a-f0-9]{64}", body[k])
            for k in ("account_digest", "cache_digest"))
            or type(body["fetched_at_ms"]) is not int
            or not 0 <= time.time() * 1000 - body["fetched_at_ms"] <= MAX_CACHE_AGE_S * 1000
            or body["freshness"] not in {"fresh_native_cache", "recent_native_cache"}):
        raise ValueError("native_cache_proof_invalid")
    return UsageObservation(state=body["state"], reason="",
        subscription=body["subscription"], invocation_digest=body["invocation_digest"],
        context_digest=body["context_digest"], diagnostic_digest=diag["sha256"],
        observed_at=observed, cli_version=body["cli_version"],
        account_digest=body["account_digest"], cache_digest=body["cache_digest"],
        fetched_at_ms=body["fetched_at_ms"], freshness=body["freshness"])


class NativeUsageReader:
    """Internal Core read service. There is intentionally no quote() method.

    `_build`/`_runner` are internal offline test seams, not settings, model
    arguments, HTTP fields or a configurable certificate of free usage.
    """
    def __init__(self, *, _build=None, _runner=None, _timeout=READ_TIMEOUT):
        self._build = _build or REVIEWED_BUILD
        self._runner = _runner
        self._timeout = _timeout

    async def read(self, invocation) -> UsageObservation:
        from solvio.specialists import launcher as L
        try:
            inv, context, home, executable = _context(invocation, self._build)
            if not _finite_number(self._timeout) or not 0.05 <= self._timeout <= READ_TIMEOUT:
                raise ValueError("invalid_reader_timeout")
            with tempfile.TemporaryDirectory(prefix="solvio-claude-usage-") as directory:
                directory = str(Path(directory).resolve(strict=True))
                request = {"schema": SCHEMA, "request_id": secrets.token_hex(16),
                    "invocation_digest": inv, "context_digest": context,
                    "executable": executable, "version": self._build.version,
                    "sha256": self._build.sha256, "home": home,
                    "directory": directory, "timeout": self._timeout}
                worker = L.Invocation(sys.executable,
                    ("-I", "-B", str(Path(__file__).resolve()), "--worker"),
                    cwd=directory, timeout=self._timeout + 2, cleanup_group=True)
                started = time.monotonic()
                # Bounded streaming drains and process-group cleanup are the
                # existing Launcher's responsibility, not duplicated here.
                result = await (self._runner or L.run)(worker, json.dumps(request),
                    on_stdout_line=lambda line: None)
                ended = time.monotonic()
                if _context(invocation, self._build)[:2] != (inv, context):
                    raise ValueError("usage_context_changed")
                body = _json(result.text)
                observation = _validated_result(body, request=request, started=started,
                                                ended=ended, outcome=result)
                metadata = _metadata(home)
                if (observation.account_digest != _account_digest(metadata)
                        or observation.cache_digest != _digest(metadata["cache"])):
                    raise ValueError("native_cache_changed")
                return observation
        except asyncio.CancelledError:
            raise  # Launcher has already drained the actual owned group.
        except (KeyError, OSError, ValueError, TypeError, UnicodeError):
            return UsageObservation()


def _request(raw):
    if len(raw) > MAX_INPUT:
        raise ValueError("request_oversize")
    body = _json(raw)
    expected = {"schema", "request_id", "invocation_digest", "context_digest",
                "executable", "version", "sha256", "home", "directory", "timeout"}
    if type(body) is not dict or set(body) != expected or body["schema"] != SCHEMA:
        raise ValueError("request_invalid")
    if not isinstance(body["request_id"], str) or not re.fullmatch(r"[a-f0-9]{32}", body["request_id"]):
        raise ValueError("request_invalid")
    for key in ("sha256", "context_digest", "invocation_digest"):
        if not isinstance(body[key], str) or not re.fullmatch(r"[a-f0-9]{64}", body[key]):
            raise ValueError("request_invalid")
    for key in ("executable", "home", "directory"):
        if not isinstance(body[key], str) or not os.path.isabs(body[key]):
            raise ValueError("request_invalid")
    if (not _finite_number(body["timeout"]) or not 0.05 <= body["timeout"] <= READ_TIMEOUT
            or not isinstance(body["version"], str)
            or not re.fullmatch(r"\d+\.\d+\.\d+", body["version"])):
        raise ValueError("request_invalid")
    return body


async def _worker_read(request):
    started_ms = time.time() * 1000
    home = str(Path(os.environ["HOME"]).resolve(strict=True))
    executable = str(Path(request["executable"]).resolve(strict=True))
    directory = Path(request["directory"])
    if (home != request["home"] or Path.cwd() != directory
            or directory.is_symlink() or directory.stat().st_mode & 0o077
            or executable != request["executable"]
            or _file_digest(executable) != request["sha256"]):
        raise ValueError("native_context_mismatch")
    context = _digest({"home": home, "binary": executable,
        "sha256": request["sha256"], "version": request["version"],
        "profile": "isolated-native-control-v1"})
    if context != request["context_digest"]:
        raise ValueError("native_context_mismatch")
    before_metadata = _metadata(home)
    debug = directory / "native-debug.log"
    # Parent Launcher already removed API credentials. Rebuild the same narrow
    # environment so no future parent addition quietly becomes native authority.
    environment = {k: os.environ[k] for k in
        ("HOME", "PATH", "TMPDIR", "SHELL", "USER", "LOGNAME", "LANG", "LC_ALL")
        if k in os.environ}
    environment.update(CLAUDE_CODE_DISABLE_FAST_MODE="1", DISABLE_AUTOUPDATER="1",
        DISABLE_TELEMETRY="1", DISABLE_ERROR_REPORTING="1")
    process = await asyncio.create_subprocess_exec(executable,
        "--print", "--input-format", "stream-json", "--output-format", "stream-json",
        "--verbose", "--safe-mode", "--no-session-persistence", "--tools", "",
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        "--setting-sources", "", "--disable-slash-commands", "--no-chrome",
        "--debug-file", str(debug), cwd=directory, env=environment,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, start_new_session=False, limit=MAX_LINE)
    responses, stdout_bytes = 0, 0

    async def control(subtype, **payload):
        nonlocal responses, stdout_bytes
        identifier = request["request_id"] + ":" + subtype
        frame = {"type": "control_request", "request_id": identifier,
                 "request": {"subtype": subtype, **payload}}
        process.stdin.write((json.dumps(frame) + "\n").encode())
        await process.stdin.drain()
        line = await process.stdout.readline()
        stdout_bytes += len(line)
        if not line or len(line) > MAX_LINE or stdout_bytes > MAX_STDOUT:
            raise ValueError("native_output_invalid")
        message = _json(line)
        if type(message) is not dict or message.get("type") != "control_response":
            raise ValueError("unexpected_native_frame")
        response = message.get("response")
        if (type(response) is not dict or response.get("request_id") != identifier
                or response.get("subtype") != "success"):
            raise ValueError("native_response_mismatch")
        # The real native end_session success is an ACK without a payload.
        # initialize/get_usage still require their actual structured response.
        body = response.get("response", {} if subtype == "end_session" else None)
        if type(body) is not dict:
            raise ValueError("native_response_mismatch")
        responses += 1
        return body

    async def bounded_debug():
        while True:
            if debug.exists() and debug.stat().st_size > MAX_DEBUG:
                raise ValueError("diagnostic_oversize")
            await asyncio.sleep(0.01)

    async def exchange():
        init = await control("initialize", hooks={}, sdkMcpServers=[])
        body = await control("get_usage", skip_behaviors=True)
        observed = time.monotonic()
        state, subscription = _usage(body, init.get("account"))
        await control("end_session")
        process.stdin.close()
        # A valid last response is not EOF: reject a late model/result frame.
        if await process.stdout.read(1):
            raise ValueError("unexpected_trailing_native_frame")
        await process.wait()
        if process.returncode != 0 or _file_digest(executable) != request["sha256"]:
            raise ValueError("native_exit_or_binary_changed")
        with debug.open("rb") as stream:
            diagnostic = _diagnostic(stream.read(MAX_DEBUG + 1))
        proof = _cache_proof(before_metadata, _metadata(home), init["account"], body,
                             started_ms, time.time() * 1000)
        return {"schema": SCHEMA, "request_id": request["request_id"],
            "invocation_digest": request["invocation_digest"],
            "context_digest": context, "state": state, "subscription": subscription,
            "observed_at": observed, "diagnostic": diagnostic,
            "native_exit": process.returncode, "responses": responses,
            "cli_version": request["version"], "cli_sha256": request["sha256"], **proof}

    tasks = [asyncio.create_task(exchange()), asyncio.create_task(bounded_debug())]
    try:
        done, _ = await asyncio.wait(tasks, timeout=request["timeout"],
                                     return_when=asyncio.FIRST_COMPLETED)
        if not done:
            raise ValueError("native_read_timeout")
        if tasks[1] in done:
            await tasks[1]
        return await tasks[0]
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # No private group-control implementation: every native process shares
        # the outer Launcher's group, including on this failure/timeout path.
        if process.returncode is None:
            process.kill()


def _main():
    # Only the private Launcher worker entry exists. Arbitrary public CLI flags
    # cannot select a provider, issue a model prompt or obtain a zero quote.
    if sys.argv[1:] != ["--worker"]:
        raise SystemExit(2)
    try:
        request = _request(sys.stdin.buffer.read(MAX_INPUT + 1))
        output = asyncio.run(_worker_read(request))
    except BaseException:
        output = {"schema": SCHEMA, "state": "unknown"}
    print(json.dumps(output, separators=(",", ":"), allow_nan=False), flush=True)


if __name__ == "__main__":
    _main()
