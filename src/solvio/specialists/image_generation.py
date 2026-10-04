"""Native image worker factory and terminal-aware physical dispatch."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

from solvio.specialists import hermes_native as H, launcher as L

SCHEMA = "solvio.native-image.v1"
RUNTIME = "hermes-codex-image-generation"
MAX_IMAGE = 32 * 1024 * 1024
OUTPUT = "generated-image.bin"


def image_invocation(config: H.NativeResearchConfig, workdir: str):
    base = H.worker_invocation(replace(config, timeout_s=float(config.timeout_s),
        browser_python="", browser_bin="", browser_chrome=""), workdir)
    argv = list(base.argv)
    argv[2] = str(Path(__file__).with_name("image_generation_worker.py"))
    return replace(base, argv=tuple(argv))


def decode(text):
    data = json.loads(text)
    if type(data) is not dict or data.get("schema") != SCHEMA:
        raise ValueError("native_image_protocol")
    return data


async def run_worker(invocation, prompt):
    outcome = await L.run(invocation, prompt)
    if outcome.process_started is False or outcome.exit_code is None:
        return outcome
    try:
        body = decode(outcome.text)
        state = body.get("execution_status")
        terminal = body.get("terminal")
        known = (outcome.ok and not outcome.truncated and (state == "not_started"
            or state == "terminal" and terminal in {"completed", "failed", "interrupted"}))
    except (ValueError, TypeError):
        known = False
    if not known:
        # A dead subprocess is not evidence that the remote generation stopped.
        return replace(outcome, ok=False, exit_code=None, reason="native_terminal_missing")
    return outcome
