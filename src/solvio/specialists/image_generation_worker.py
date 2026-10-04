"""One native image turn using the installed Hermes transport, not an API client.

Only a bound native image item supplies bytes. Assistant text is never a file
receipt. The normal research worker and its approved configuration stay intact.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
from pathlib import Path
import signal
import stat
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hermes_native_worker as H

RUNTIME = "hermes-codex-image-generation"
SCHEMA = "solvio.native-image.v1"
MAX_IMAGE = 32 * 1024 * 1024
OUTPUT = "generated-image.bin"


def image_type(content):
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "jpg"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp", "webp"
    raise ValueError("native_image_invalid")


def image_bytes(item, home):
    # The native protocol owns this item, not a model-written path in prose.
    result = item.get("result")
    if not isinstance(result, str) or len(result) > (MAX_IMAGE * 4 // 3) + 8:
        raise ValueError("native_image_invalid")
    if result:
        try:
            content = base64.b64decode(result, validate=True)
        except ValueError:
            raise ValueError("native_image_invalid") from None
    else:
        path = Path(item.get("savedPath") or "")
        root = Path(home) / "generated_images"
        if not path.is_absolute() or not path.is_relative_to(root):
            raise ValueError("native_image_path_invalid")
        relative = path.relative_to(root)
        if not relative.parts or any(p in {".", ".."} for p in relative.parts):
            raise ValueError("native_image_path_invalid")
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in relative.parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = child
            fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or not 1 <= info.st_size <= MAX_IMAGE:
                    raise ValueError("native_image_invalid")
                content = stream.read(MAX_IMAGE + 1)
        finally:
            os.close(directory)
    if not 1 <= len(content) <= MAX_IMAGE:
        raise ValueError("native_image_invalid")
    image_type(content)
    return content


class Recorder(H.Recorder):
    def __init__(self, args):
        super().__init__()
        self.args = args
        self.image_id = ""
        self.image = None
        self.image_started = set()

    def emit(self, *_args, **_kwargs):
        # Only the final bounded projection leaves this worker. Base64 and
        # native account/config/output paths never enter stdout or the ledger.
        pass

    def on_event(self, note):
        params = note.get("params") or {}
        turn = params.get("turn") or {}
        if (params.get("threadId") != self.thread_id
                or (params.get("turnId") or turn.get("id")) != self.turn_id):
            return
        method = note.get("method")
        item = params.get("item") or {}
        if method in {"item/started", "item/completed"} and item.get("type") == "webSearch":
            self.fail("native_tool_not_allowed")
            return
        if method in {"item/started", "item/completed"} and item.get("type") == "imageGeneration":
            identity = item.get("id")
            if not isinstance(identity, str) or not 1 <= len(identity) <= 160:
                self.fail("native_image_invalid")
                return
            self.image_started.add(identity)
            if len(self.image_started) != 1 or (method == "item/completed" and self.image is not None):
                self.fail("native_image_count_exceeded")
                return
            if method == "item/completed":
                try:
                    if item.get("status") != "completed":
                        raise ValueError("native_image_failed")
                    self.image = image_bytes(item, self.args.codex_home)
                    self.image_id = identity
                except (ValueError, OSError):
                    self.fail("native_image_invalid")
            return
        super().on_event(note)


def preflight(raw, args):
    account_response = raw.request("account/read", {"refreshToken": False}, timeout=8)
    account = account_response.get("account")
    if not isinstance(account, dict) or account.get("type") != "chatgpt":
        raise ValueError("logged_out" if account is None else "subscription_required")
    H.read_limits(raw, account=account_response,
                  credit_account=getattr(args, "authorized_credit_account", ""))
    models = raw.request("model/list", {"includeHidden": False, "limit": 100}, timeout=8)
    if not any(isinstance(m, dict) and m.get("model") == args.model for m in models.get("data", [])):
        raise ValueError("native_model_unavailable")
    config = raw.request("config/read", {"cwd": args.workdir, "includeLayers": True}, timeout=8)
    # Reuse the research layer firewall, allowing exactly the Core-enabled
    # image feature. All other enablements and custom providers are rejected.
    cleaned = json.loads(json.dumps(config))
    for layer in cleaned.get("layers", []):
        features = (layer.get("config") or {}).get("features", {})
        if "image_generation" in features:
            features["image_generation"] = False
    H._validate_layers(cleaned)
    effective = config.get("config") or {}
    features = effective.get("features") or {}
    if (effective.get("model") != args.model or effective.get("model_provider") != "openai"
            or effective.get("web_search") != "disabled" or effective.get("approval_policy") != "never"
            or effective.get("sandbox_mode") != "read-only"
            or effective.get("service_tier") not in (None, "")
            or features.get("image_generation") is not True
            or any(features.get(name) is not False for name in H.DISABLED_FEATURES if name != "image_generation")):
        raise ValueError("native_config_mismatch")
    skills = raw.request("skills/list", {"cwds": [args.workdir], "forceReload": True}, timeout=8)
    disabled = []
    for entry in skills.get("data", []):
        if entry.get("errors"):
            raise ValueError("native_skills_unverified")
        for skill in entry.get("skills", []):
            path = skill.get("path")
            if not isinstance(path, str) or not os.path.isabs(path) or len(disabled) >= 256:
                raise ValueError("native_skills_unverified")
            disabled.append({"path": path, "enabled": False})
    return disabled


def run(args, prompt):
    H.validate_home(args.codex_home, args.model)
    if Path(args.workdir).resolve().is_relative_to(Path(args.codex_home).resolve()):
        raise ValueError("native_workspace_invalid")
    sys.path.insert(0, args.hermes_source)
    from agent.transports.codex_app_server import CodexAppServerClient, CodexAppServerError
    from agent.transports.codex_app_server_session import CodexAppServerSession
    logging.disable(logging.CRITICAL)
    recorder = Recorder(args)
    deadline = time.monotonic() + args.timeout

    class GuardedClient:
        def __init__(self, **kwargs):
            extra = ["--stdio", "--strict-config"]
            for feature in H.DISABLED_FEATURES:
                extra += ["--enable" if feature == "image_generation" else "--disable", feature]
            extra += ["-c", 'web_search="disabled"', "-c", "project_doc_max_bytes=0"]
            self.raw = CodexAppServerClient(**kwargs, extra_args=extra)
            self.disabled_skills = []

        def __getattr__(self, name):
            return getattr(self.raw, name)

        def initialize(self, **kwargs):
            result = self.raw.initialize(**kwargs)
            try:
                self.disabled_skills = preflight(self.raw, args)
            except ValueError as exc:
                recorder.fail(str(exc))
                raise CodexAppServerError(-32000, recorder.failure) from None
            return result

        def request(self, method, params, **kwargs):
            if method == "thread/start":
                params = {**params, "model": args.model, "modelProvider": "openai",
                    "sandbox": "read-only", "approvalPolicy": "never", "approvalsReviewer": "user",
                    "ephemeral": True, "config": {"web_search": "disabled",
                    "skills": {"config": self.disabled_skills}}}
            elif method == "turn/start":
                if recorder.failure:
                    raise CodexAppServerError(-32000, recorder.failure)
                recorder.turn_requested = True
                params = {**params, "model": args.model, "approvalPolicy": "never", "approvalsReviewer": "user",
                    "sandboxPolicy": {"type": "readOnly", "networkAccess": False}}
            result = self.raw.request(method, params, **kwargs)
            if method == "thread/start":
                sandbox = result.get("sandbox") or {}
                if (result.get("model") != args.model or result.get("modelProvider") != "openai"
                        or Path(result.get("cwd", "/")).resolve() != Path(args.workdir).resolve()
                        or result.get("approvalPolicy") != "never" or result.get("approvalsReviewer") != "user"
                        or sandbox.get("type") != "readOnly" or sandbox.get("networkAccess") is not False
                        or result.get("instructionSources") or result.get("serviceTier") not in (None, "")):
                    recorder.fail("native_policy_mismatch")
                    raise CodexAppServerError(-32000, recorder.failure)
                recorder.thread_id = str((result.get("thread") or {}).get("id") or "")
            elif method == "turn/start":
                recorder.turn_id = str((result.get("turn") or {}).get("id") or "")
                if not recorder.thread_id or not recorder.turn_id or max(len(recorder.thread_id), len(recorder.turn_id)) > 160:
                    recorder.fail("native_protocol_error")
                    raise CodexAppServerError(-32000, recorder.failure)
            return result

        def take_server_request(self, timeout=0):
            request = self.raw.take_server_request(timeout=timeout)
            if request is not None:
                self.raw.respond_error(request.get("id"), code=-32601, message="SOLVIO image generation denies external tools")
                recorder.fail("native_tool_not_allowed")
            return None

    session = CodexAppServerSession(cwd=args.workdir, codex_bin=args.codex_bin,
        codex_home=args.codex_home, client_factory=GuardedClient,
        approval_callback=lambda *_a, **_kw: "deny", on_event=recorder.on_event)
    recorder.session = session
    old_handler = signal.signal(signal.SIGTERM, lambda *_: session.request_interrupt())
    reason = ""
    try:
        session.ensure_started()
        result = session.run_turn(prompt, turn_timeout=max(0.05, deadline - time.monotonic()),
            notification_poll_timeout=0.02, post_tool_quiet_timeout=max(0.05, args.timeout))
        reason = recorder.failure
        if not reason and recorder.terminal != "completed":
            reason = "cancelled" if result.interrupted else "native_terminal_missing"
        if not reason and (result.error or recorder.image is None):
            reason = "native_image_missing"
    except Exception:
        reason = recorder.failure or "native_protocol_error"
    finally:
        if recorder.turn_id and recorder.terminal not in {"completed", "failed", "interrupted"}:
            try:
                session._client.request("turn/interrupt", {"threadId": recorder.thread_id, "turnId": recorder.turn_id}, timeout=1)
                stop_deadline = min(deadline, time.monotonic() + 1)
                while time.monotonic() < stop_deadline:
                    note = session._client.take_notification(timeout=min(0.02, stop_deadline - time.monotonic()))
                    if note is not None:
                        recorder.on_event(note)
                    if recorder.terminal in {"completed", "failed", "interrupted"}:
                        break
            except Exception:
                pass
        session.close()
        signal.signal(signal.SIGTERM, old_handler)
    image = {}
    if not reason:
        mime, extension = image_type(recorder.image)
        with open(Path(args.workdir) / OUTPUT, "xb") as stream:
            os.chmod(stream.fileno(), 0o600)
            stream.write(recorder.image)
        image = {"item_id": recorder.image_id, "size": len(recorder.image),
            "sha256": hashlib.sha256(recorder.image).hexdigest(), "mime_type": mime, "extension": extension}
    return {"schema": SCHEMA, "status": "failed" if reason else "completed", "reason": reason,
        "model": args.model, "thread_id": recorder.thread_id, "turn_id": recorder.turn_id,
        "terminal": recorder.terminal, "execution_status": recorder.execution_status(),
        "runtime": RUNTIME, "image": image, "usage": recorder.usage}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--authorized-credit-account", default="")
    for name in ("hermes-source", "codex-bin", "codex-home", "model", "workdir"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--timeout", type=float, required=True)
    args = parser.parse_args()
    try:
        if not 0.05 <= args.timeout <= 3600:
            raise ValueError("native_timeout_invalid")
        payload = sys.stdin.buffer.read(H.MAX_INPUT + 1)
        if len(payload) > H.MAX_INPUT:
            raise ValueError("native_input_too_large")
        result = run(args, payload.decode("utf-8"))
    except Exception:
        result = {"schema": SCHEMA, "status": "failed", "reason": "native_protocol_error", "execution_status": "unknown"}
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
