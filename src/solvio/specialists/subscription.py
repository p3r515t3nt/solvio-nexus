"""Begrenzte Textaufrufe ueber die vorhandenen Abo-CLIs.

Planung, Bewertung und spaeter Extraktion brauchen eine Antwort, keinen
weiteren Agentenlauf. Starter, Anmeldepruefung, Frist und Bereinigung bleiben
dieselben wie bei den Spezialisten. Kein Broker und kein Anbieterwechsel.
"""
from __future__ import annotations

from dataclasses import replace
from contextlib import nullcontext
import json
import math
import re
import tempfile

from solvio.logging_setup import get_logger
from solvio.specialists import providers as P
from solvio.specialists.launcher import LauncherError

log = get_logger("specialists")

CALL_TIMEOUT = 120.0

# Lokal ueber `codex features list` am 10.09.2026 bestaetigt. Das normale
# Coding-Profil bleibt unangetastet; nur dieser Antwortaufruf braucht sie nicht.
CODEX_DISABLED_FEATURES = (
    "shell_tool", "apps", "multi_agent", "hooks", "plugins", "remote_plugin",
    "memories", "browser_use", "browser_use_external", "browser_use_full_cdp_access",
    "computer_use", "image_generation", "view_image", "skill_mcp_dependency_install",
    "skill_search", "tool_suggest", "shell_snapshot", "goals", "workspace_dependencies",
)


def text_invocation(provider, *, workdir, model="", timeout=120.0, images=(), assessment=False):
    """One shared construction path for dispatch and native cost binding."""
    if provider == "codex":
        invocation = P.codex_invocation(workdir=workdir, model=model, timeout=timeout)
        argv = list(invocation.argv[:-1]) + ["--json"]
        configs = [f"features.{name}=false" for name in CODEX_DISABLED_FEATURES]
        configs += ['web_search="disabled"', "project_doc_max_bytes=0"]
        for config in configs:
            argv += ["-c", config]
        if assessment:
            from solvio.agent_runtime.planner import ASSESSMENT_SCHEMA_PATH
            argv += ["--output-schema", str(ASSESSMENT_SCHEMA_PATH)]
        for image in images:
            argv += ["--image", image]
        return replace(invocation, argv=tuple(argv + ["-"]))
    if images:
        raise LauncherError("assessment_images_unsupported")
    if provider == "claude-code":
        invocation = P.claude_invocation(workdir=workdir, model=model, timeout=timeout)
        return replace(invocation, argv=invocation.argv + ("--tools", ""))
    raise LauncherError("provider_unavailable")


def _usage(data: object, provider: str) -> tuple[int, bool]:
    if not isinstance(data, dict):
        return 0, False
    values = [data.get("input_tokens"), data.get("output_tokens")]
    if provider == "claude-code":
        # Anthropic zaehlt Cache-Reads/-Erzeugung getrennt vom input_tokens-
        # Feld. Codex cached_input_tokens ist dagegen bereits darin enthalten.
        values += [data.get("cache_read_input_tokens", 0),
                   data.get("cache_creation_input_tokens", 0)]
    if any(type(value) is not int or value < 0 for value in values):
        return 0, False
    # Reasoning-Ausgabe ist bereits in output_tokens enthalten.
    return sum(values), True


def decode(provider: str, outcome: P.SubscriptionOutcome) -> dict:
    """CLI-Umschlag lesen; nur ein beendeter Turn liefert eine Antwort.

    Fehlermeldungen bleiben Kategorien. Ein erfolgreicher Satz ueber ein
    Kontingent ist kein Kontingentfehler. Fehlende Tokenmessung ist unbekannt.
    """
    result = {"ok": False, "reason": outcome.reason,
              "provider": outcome.provider, "billing_mode": outcome.billing_mode,
              "auth": outcome.auth, "dispatch_started": outcome.dispatch_started,
              "cost_reservation_id": outcome.cost_reservation_id,
              "cost_invocation_id": outcome.cost_invocation_id,
              "cost_status": outcome.cost_status,
              "tokens": 0, "usage_reported": False, "text": ""}
    if outcome.cost_status == "unknown":
        # Preserve the local failure category without copying CLI output or
        # weakening the unresolved cost claim. In particular, a timeout must
        # remain diagnosable even though the public refusal is cost recovery.
        cause = outcome.reason if outcome.reason in {
            "timeout", "communication_failed", "nonzero_exit", "cost_recovery_required"
        } else "unclassified"
        elapsed = outcome.elapsed
        elapsed_ms = (round(elapsed * 1000) if type(elapsed) in (int, float)
                      and math.isfinite(elapsed) and 0 <= elapsed <= 86400 else None)
        invocation_id = outcome.cost_invocation_id
        log.warning("subscription.call_unresolved",
                    provider=provider if provider in {"codex", "claude-code"} else "unknown",
                    cause=cause, elapsed_ms=elapsed_ms,
                    invocation_id=invocation_id if isinstance(invocation_id, str)
                    and re.fullmatch(r"pc-[0-9a-f]{64}", invocation_id) else "")
        result["reason"] = "cost_recovery_required"
        return result
    if not outcome.ok:
        result["reason"] = P.provider_error_reason(
            outcome.text + " " + outcome.stderr_note, outcome.reason)
        # The launcher's note is already redacted and short; without it a
        # non-zero exit had no trace anywhere (attempt ab, 20.09.2026).
        # … and through the vault's line fence, so a credential-shaped stderr line
        # becomes a marker, not a log line (review round 17, B17-H4).
        from solvio.secret_vault.firewall import redact_lines_if_credential
        note = redact_lines_if_credential(str(outcome.stderr_note or "")[:200], where="subscription.call_failed")[0]
        log.warning("subscription.call_failed", provider=provider, reason=result["reason"],
                    exit_code=getattr(outcome, "exit_code", None), note=note)
        return result
    if outcome.truncated:
        result["reason"] = "provider_output_truncated"
        return result
    try:
        if provider == "claude-code":
            data = json.loads(outcome.text)
            if not isinstance(data, dict) or data.get("type") != "result":
                raise ValueError("missing_result")
            if data.get("is_error") or data.get("subtype") != "success":
                result["reason"] = P.provider_error_reason(
                    str(data.get("result", "")) + " " + str(data.get("errors", "")),
                    "provider_failed")
                return result
            reply = data.get("result")
            tokens, reported = _usage(data.get("usage"), provider)
        elif provider == "codex":
            reply, completed = None, False
            tokens, reported = 0, False
            for line in outcome.text.splitlines():
                data = json.loads(line)
                if not isinstance(data, dict):
                    raise ValueError("invalid_event")
                kind = data.get("type")
                if kind in {"turn.failed", "error"}:
                    result["reason"] = P.provider_error_reason(
                        json.dumps(data), "provider_failed")
                    return result
                if kind == "item.completed":
                    item = data.get("item") or {}
                    if item.get("type") == "agent_message":
                        reply = item.get("text")
                elif kind == "turn.completed":
                    completed = True
                    tokens, reported = _usage(data.get("usage"), provider)
            if not completed:
                raise ValueError("incomplete_turn")
        else:
            raise ValueError("unknown_provider")
        if not isinstance(reply, str) or not reply.strip():
            raise ValueError("missing_answer")
    except (ValueError, TypeError, AttributeError):
        result["reason"] = "provider_output_invalid"
        return result
    result.update(ok=True, reason="", text=reply, tokens=tokens,
                  usage_reported=reported)
    return result


class SubscriptionTransport:
    def __init__(self, provider: str = "codex", *, model: str = "",
                 timeout: float = CALL_TIMEOUT, runner=None) -> None:
        self.provider = provider
        self.model = model
        self.timeout = timeout
        self._runner = runner

    @property
    def route(self) -> dict:
        return {"provider": self.provider, "billing_mode": "subscription"}

    async def __call__(self, payload: dict) -> dict:
        # Frischer Arbeitsort und keine persistente CLI-Sitzung. Auftrag und
        # Ergebnisdaten liefert der Core, niemals ein bestehender CLI-Chat.
        prompt = ("Bearbeite ausschliesslich die folgenden Nachrichten. "
                  "Fuehre keine Werkzeuge aus. Gib nur die angeforderte Antwort aus.\n"
                  + json.dumps(payload["input"], ensure_ascii=False))
        with tempfile.TemporaryDirectory(prefix="solvio-subscription-") as workdir:
            outcome = None
            try:
                ids = payload.get("core_image_artifacts", ())
                if ids and self.provider != "codex":
                    raise LauncherError("assessment_images_unsupported")
                from solvio.agent_runtime.image_inputs import staged
                with staged(ids, workdir) if ids else nullcontext(()) as images:
                    invocation = text_invocation(self.provider, workdir=workdir,
                                                 model=self.model, timeout=self.timeout, images=images,
                                                 assessment=payload.get("core_assessment_schema") is True)
                    outcome = await P.run_subscription(
                        self.provider, invocation, prompt, runner=self._runner)
            except LauncherError as exc:
                return {"ok": False, "reason": exc.reason,
                        "provider": self.provider, "billing_mode": "unknown",
                        "auth": "", "dispatch_started": False}
            except (ValueError, OSError, TypeError, KeyError):
                if outcome is not None:
                    result = decode(self.provider, outcome)
                    result.update(ok=False, text="", reason="cost_recovery_required"
                                  if outcome.cost_status == "unknown" else "assessment_image_changed")
                    return result
                return {"ok": False, "reason": "assessment_image_changed",
                        "provider": self.provider, "billing_mode": "unknown",
                        "auth": "", "dispatch_started": False}
        return decode(self.provider, outcome)
