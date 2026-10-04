"""Der Claude-Auftragsarbeiter (N8/C4): Fabrik, Policy-Digest, Stromkonsument,
MCP-Adapter und `run_task` gegen die echten Core-Naehte.

Kein Modell, keine Anmeldung, kein Schluesselbund: die CLI wird durch ein
Python-Skript gespielt, das exakt die am 2026-09-18 gemessenen stream-json-
Ereignisse (2.1.261) schreibt; `sandbox-exec` durch einen Wrapper, der das
Profil verwirft (das echte Profil misst `test_claude_worker_canary.py`). Die
Sessions, der Kostenclaim, die Werkzeugbruecke, der Core-Portalweg und der
Broker-Vertrag sind echt.
"""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _guard import enforce_assertions, require, require_equal  # noqa: E402
enforce_assertions()

from _private_temp import private_folder, socket_root  # noqa: E402
import test_agent_portal_cost_dispatch as P  # noqa: E402
import test_hermes_native as B  # noqa: E402
from solvio.agent_runtime import cost_dispatch as D, isolation as I, native_sessions as N  # noqa: E402
from solvio.agent_runtime import native_tools as T, specialists as SP  # noqa: E402
from solvio.agent_runtime.native_tool_bridge import NativeToolBridge  # noqa: E402
from solvio.specialists import claude_mcp_adapter as ADAPTER, claude_native_task as CNT  # noqa: E402
from solvio.specialists import hermes_native as H, launcher as L, native_task as NT, providers as PV  # noqa: E402

SID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
TOKEN = "sk-solvio-broker-" + "ab" * 24
BROKER_PORT = 48792


def _clean(token=TOKEN):
    return CNT.redactor(token)


# =====================================================================
# Fabrik und Umgebung
# =====================================================================

def _invocation(**overrides):
    args = dict(workdir="/private/var/w/at-1", jail="/private/var/j/at-1", task_id="at-1",
                session_id=SID, broker_port=BROKER_PORT, mcp_mode="none", resume=False)
    args.update(overrides)
    return CNT.claude_worker_invocation(**args)


def t_the_factory_is_deterministic_and_carries_the_measured_tool_layers():
    first, second = _invocation(), _invocation()
    require_equal(first, second, "zwei Aufrufe derselben Argumente unterscheiden sich")
    require_equal(first.executable, I.SANDBOX_EXEC)
    argv = list(first.argv)
    require_equal(argv[:2], ["-f", "/private/var/j/at-1/builder.sandbox.sb"])
    require_equal(argv[2], L.resolve("claude"))
    for flag in ("--print", "--bare", "--verbose", "--permission-prompts", "--disable-slash-commands",
                 "--setting-sources", "--strict-mcp-config", "--no-chrome"):
        require(flag in argv, f"{flag} fehlt")
    require_equal(argv[argv.index("--output-format") + 1], "stream-json")
    require_equal(argv[argv.index("--permission-mode") + 1], "acceptEdits")
    require_equal(argv[argv.index("--permission-prompts") + 1], "none")
    # Gemessen unter --bare 2.1.261: genau Bash, Edit, Read.
    require_equal(CNT.CLAUDE_WORKER_TOOLS, ("Bash", "Edit", "Read"))
    require_equal(argv[argv.index("--tools") + 1], "Bash,Edit,Read")
    allowed = argv[argv.index("--allowedTools") + 1:argv.index("--disallowedTools")]
    require_equal(allowed, ["Bash", "Edit", "Read"], "die Vorabfreigabe ist nicht die geschlossene Menge")
    disallowed = argv[argv.index("--disallowedTools") + 1:argv.index("--model")]
    require_equal(disallowed, ["WebFetch", "WebSearch", "Task", "Agent"])
    require_equal(argv[argv.index("--model") + 1], "claude-sonnet-5")
    require_equal(argv[argv.index("--mcp-config") + 1], '{"mcpServers":{}}')
    require_equal(argv[-2:], ["--session-id", SID])
    require("--no-session-persistence" not in argv, "ohne Persistenz gibt es keine Fortsetzung")
    require("--max-turns" not in argv and "--fallback-model" not in argv and "--bg" not in argv)
    require(first.prompt_via_stdin and first.cleanup_group)
    require_equal(first.timeout, CNT.PROFILE_TIMEOUT + CNT.PROCESS_GRACE)
    require(CNT.PROCESS_GRACE >= 240, "die CLI braucht ~180 s, um bei 401/429 selbst aufzugeben")
    # Kein Geheimnis in argv: kein Token, keine Umgebung.
    joined = " ".join(argv)
    require("sk-" not in joined and "ANTHROPIC" not in joined)


def t_mcp_names_appear_only_in_bridge_mode_and_resume_swaps_the_session_flag():
    bridge = list(_invocation(mcp_mode="bridge").argv)
    allowed = bridge[bridge.index("--allowedTools") + 1:bridge.index("--disallowedTools")]
    require_equal(allowed, ["Bash", "Edit", "Read", "mcp__solvio__portal_list", "mcp__solvio__result_files_list"])
    require_equal(bridge[bridge.index("--mcp-config") + 1], "/private/var/j/at-1/mcp.json")
    none = list(_invocation(mcp_mode="none").argv)
    require(not any(a.startswith("mcp__") for a in none), "MCP-Namen ohne Bruecke")
    resumed = list(_invocation(resume=True).argv)
    require_equal(resumed[-2:], ["--resume", SID])
    require("--session-id" not in resumed)
    for bad in (dict(session_id="nicht-uuid"), dict(mcp_mode="beides"), dict(task_id="at-2"),
                dict(jail="relative/j/at-1"), dict(workdir="relativ")):
        try:
            _invocation(**bad)
        except ValueError:
            continue
        raise AssertionError(f"angenommen: {bad}")


def t_constants_agree_with_the_profile_the_broker_and_the_principal():
    from solvio.provider_broker import anthropic as AN, service as BS
    require_equal(CNT.PROFILE, SP.CLAUDE_TASK_PROFILE)
    require_equal(CNT.PROFILE_TIMEOUT, SP.profile(SP.CLAUDE_TASK_PROFILE).timeout)
    require_equal(CNT.WRITER_MODEL, AN.WRITER_MODEL)
    require_equal(CNT.PRINCIPAL, BS.NEXUS_WORKER_CLAUDE_PRINCIPAL)
    require(SP.profile(SP.CLAUDE_TASK_PROFILE).needs_seatbelt)
    require("builder/claude" in SP.BLOCKED_PROFILES and CNT.PROFILE not in SP.BLOCKED_PROFILES)


def t_the_environment_is_brokered_with_the_cli_state_inside_the_jail():
    env = CNT.worker_environment("/private/var/j/at-1", broker_port=BROKER_PORT, token=TOKEN)
    require_equal(env["CLAUDE_CONFIG_DIR"], "/private/var/j/at-1/config")
    require_equal(env["TMPDIR"], "/private/var/j/at-1/tmp")
    require_equal(env["ANTHROPIC_BASE_URL"], f"http://127.0.0.1:{BROKER_PORT}")
    require_equal(env["ANTHROPIC_API_KEY"], TOKEN)
    leaking = sorted(n for n in env if n in L.DENIED_ENV and n not in ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY"))
    require_equal(leaking, [], "ein Sperrlisten-Name ausser den zwei gesetzten")
    require_equal(env["CLAUDE_CODE_DISABLE_FAST_MODE"], "1")
    require("CLAUDE_CODE_OAUTH_TOKEN" not in env and "HOME" in env)


# =====================================================================
# Policy-Digest
# =====================================================================

def _config(folder, **overrides):
    jail = Path(folder) / "at-1"
    jail.mkdir(mode=0o700, exist_ok=True)
    args = dict(jail=str(jail.resolve()), broker_port=BROKER_PORT, mcp_mode="none", session_mode="resume")
    args.update(overrides)
    return CNT.ClaudeWorkerConfig(**args)


def t_the_policy_digest_binds_adapter_decoder_profile_modes_and_tool_lists():
    with tempfile.TemporaryDirectory() as folder:
        base = _config(folder)
        digest = CNT.claude_task_policy(base)
        require(len(digest) == 64 and digest == CNT.claude_task_policy(base))
        require(digest != CNT.claude_task_policy(_config(folder, mcp_mode="bridge")), "MCP-Modus ungebunden")
        require(digest != CNT.claude_task_policy(_config(folder, session_mode="handover")), "Sitzungsmodus ungebunden")
        with patch.object(CNT, "CLAUDE_WORKER_TOOLS", ("Bash",)):
            require(digest != CNT.claude_task_policy(base), "Werkzeugliste ungebunden")
        with patch.object(CNT, "DISALLOWED_TOOLS", ()):
            require(digest != CNT.claude_task_policy(base), "Sperrliste ungebunden")
        with patch.object(I, "_PROFILE", I._PROFILE + "\n;; mutiert"):
            require(digest != CNT.claude_task_policy(base), "Profilvorlage ungebunden")
        original = CNT._file_digest

        def other(path):
            return "f" * 64 if path.name == CNT.ADAPTER_NAME else original(path)
        with patch.object(CNT, "_file_digest", other):
            require(digest != CNT.claude_task_policy(base), "Adapter ungebunden")

        def decoder_changed(path):
            return "e" * 64 if path.name == "claude_native_task.py" else original(path)
        with patch.object(CNT, "_file_digest", decoder_changed):
            require(digest != CNT.claude_task_policy(base), "Decoder ungebunden")
        with patch("solvio.specialists.native_task.tools_description", return_value="andere"):
            require(digest == CNT.claude_task_policy(base), "ohne Bruecke zaehlt das Manifest nicht")
            require(CNT.claude_task_policy(_config(folder, mcp_mode="bridge"))
                    != CNT.claude_task_policy(_config(folder, mcp_mode="bridge")) or True)
        bridge = CNT.claude_task_policy(_config(folder, mcp_mode="bridge"))
        with patch("solvio.specialists.native_task.tools_description", return_value="andere"):
            require(bridge != CNT.claude_task_policy(_config(folder, mcp_mode="bridge")),
                    "das Manifest ist im Brueckenmodus ungebunden")


def t_config_validation_refuses_shared_symlinked_or_foreign_jails():
    with tempfile.TemporaryDirectory() as folder:
        good = _config(folder)
        good.validate()
        os.chmod(good.jail, 0o755)
        try:
            good.validate()
        except ValueError as exc:
            require_equal(str(exc), "native_jail_not_private")
        else:
            raise AssertionError("ein gruppenlesbarer Kaefig wurde angenommen")
        os.chmod(good.jail, 0o700)
        for bad in (dict(mcp_mode="x"), dict(session_mode="x"), dict(model="claude-opus-5"),
                    dict(broker_port=0), dict(timeout_s=0.5), dict(jail="/")):
            try:
                _config(folder, **bad).validate()
            except ValueError:
                continue
            raise AssertionError(f"angenommen: {bad}")


# =====================================================================
# Der Stromkonsument
# =====================================================================

def _init(sid=SID, mcp=()):
    return json.dumps({"type": "system", "subtype": "init", "cwd": "/w", "session_id": sid,
                       "tools": ["Bash", "Edit", "Read"], "mcp_servers": list(mcp), "model": "claude-sonnet-5",
                       "permissionMode": "acceptEdits", "apiKeySource": "ANTHROPIC_API_KEY",
                       "claude_code_version": "2.1.261", "uuid": "u-init"})


def _assistant(blocks, sid=SID, synthetic=False):
    message = {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5",
               "content": blocks, "stop_reason": "tool_use",
               "usage": {"input_tokens": 10, "output_tokens": 5}}
    event = {"type": "assistant", "message": message, "parent_tool_use_id": None, "session_id": sid, "uuid": "u-a"}
    if synthetic:
        message["model"] = "<synthetic>"
        event.update(error="rate_limit", is_api_error_message=True)
    return json.dumps(event)


def _bash_ok(stdout):
    """Die gemessene Form eines Exit-0-Bash-Ergebnisses (Build 2.1.261, 19.09.2026)."""
    return {"stdout": stdout, "stderr": "", "interrupted": False, "isImage": False, "noOutputExpected": False}


def _tool_result(tool_use_id, content, *, is_error=False, sid=SID, tool_use_result=None):
    if tool_use_result is None:
        # Gemessen: Exit ≠ 0 traegt `Error: …` als Text; Exit 0 das stdout-Objekt.
        tool_use_result = ("Error: " + content) if is_error and isinstance(content, str) else (
            _bash_ok(content) if isinstance(content, str) else {})
    return json.dumps({"type": "user", "message": {"role": "user", "content": [
        {"tool_use_id": tool_use_id, "type": "tool_result", "content": content, "is_error": is_error}]},
        "parent_tool_use_id": None, "session_id": sid, "uuid": "u-u", "tool_use_result": tool_use_result})


def _result(text, *, subtype="success", is_error=False, denials=(), sid=SID, status=None):
    body = {"type": "result", "subtype": subtype, "is_error": is_error, "duration_ms": 5, "num_turns": 2,
            "result": text, "session_id": sid, "total_cost_usd": 0, "terminal_reason": "completed",
            "usage": {"input_tokens": 20, "output_tokens": 10}, "permission_denials": list(denials), "uuid": "u-r"}
    if status is not None:
        body["api_error_status"] = status
        body["terminal_reason"] = "api_error"
    return json.dumps(body)


def _decoder(**kwargs):
    events = []
    started = []
    decoder = CNT.ClaudeStreamDecoder(SID, "/w", _clean(), on_event=events.append,
                                      on_started=lambda: started.append(True), **kwargs)
    decoder.thread_id, decoder.turn_id = SID, SID + "/pc-1"
    return decoder, events, started


def t_a_foreign_session_id_in_init_is_a_binding_change():
    decoder, _, _ = _decoder()
    try:
        decoder.feed(_init(sid="00000000-0000-4000-8000-000000000000"))
    except ValueError as exc:
        require_equal(str(exc), "native_thread_binding_changed")
    else:
        raise AssertionError("eine fremde Sitzung wurde angenommen")
    decoder, _, _ = _decoder()
    decoder.feed(_init())
    decoder.feed(json.dumps({"type": "system", "subtype": "api_retry", "session_id": SID, "attempt": 1}))
    require_equal((decoder.init_seen, decoder.system_events), (1, {"api_retry": 1}))
    require(not decoder.started, "init ist ein lokales CLI-Ereignis, kein Start")


def t_the_turn_starts_at_the_first_real_assistant_never_at_a_synthetic_error():
    decoder, events, started = _decoder()
    decoder.feed(_init())
    decoder.feed(_assistant([{"type": "text", "text": "API Error: Request rejected (429)"}], synthetic=True))
    require(not decoder.started and not started and not events, "ein synthetischer Fehler zaehlte als Start")
    require_equal(decoder.synthetic_errors, 1)
    decoder.feed(_assistant([{"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "echo hi"}}]))
    require(decoder.started and started == [True])
    require_equal(events, [{"event": "started", "thread_id": SID, "turn_id": SID + "/pc-1", "seq": 1}])
    decoder.feed(_assistant([{"type": "tool_use", "id": "toolu_2", "name": "Bash", "input": {"command": "true"}}]))
    require_equal(len(events), 1, "mehr als ein Startereignis")


def t_receipts_from_tool_use_and_tool_result_validate_and_denials_are_declined():
    decoder, _, _ = _decoder()
    decoder.feed(_init())
    decoder.feed(_assistant([
        {"type": "tool_use", "id": "toolu_b", "name": "Bash", "input": {"command": "echo ok > a.txt; cat a.txt"}},
        {"type": "tool_use", "id": "toolu_e", "name": "Edit", "input": {"file_path": "/w/a.txt", "old_string": "ok", "new_string": "ja"}},
        {"type": "tool_use", "id": "toolu_r", "name": "Read", "input": {"file_path": "/w/a.txt"}},
        {"type": "tool_use", "id": "toolu_m", "name": "mcp__solvio__portal_list", "input": {}},
        {"type": "tool_use", "id": "toolu_x", "name": "Bash", "input": {"command": "ls /Users"}}]))
    decoder.feed(_tool_result("toolu_b", "ok"))
    decoder.feed(_tool_result("toolu_e", "The file has been updated."))
    decoder.feed(_tool_result("toolu_r", "1 ok"))
    decoder.feed(_tool_result("toolu_m", [{"type": "text", "text": "{\"portals\": []}"}]))
    decoder.feed(_result("fertig", denials=[{"tool_name": "Bash", "tool_use_id": "toolu_x", "tool_input": {}}]))
    kinds = {r["item_id"]: (r["kind"], r["status"]) for r in decoder.receipts}
    require_equal(kinds, {"toolu_b": ("commandExecution", "completed"), "toolu_e": ("fileChange", "completed"),
                          "toolu_m": ("dynamicToolCall", "completed"), "toolu_x": ("commandExecution", "declined")})
    bash = next(r for r in decoder.receipts if r["item_id"] == "toolu_b")
    require_equal(bash["command_sha256"], hashlib.sha256(b"echo ok > a.txt; cat a.txt").hexdigest())
    require_equal(bash["output"], {"text": "ok", "original_chars": 2, "complete": True, "redacted": False})
    require_equal(bash["exit_code"], 0, "ein Exit-0-Ergebnis in der gemessenen CLI-Form traegt exit_code 0")
    edit = next(r for r in decoder.receipts if r["item_id"] == "toolu_e")
    require_equal(edit["changes"], [{"path": {"text": "a.txt", "original_chars": 5, "complete": True, "redacted": False},
                                     "kind": "update", "move_path": None}])
    mcp = next(r for r in decoder.receipts if r["item_id"] == "toolu_m")
    require_equal((mcp["tool"], mcp["success"]), ("portal_list", True))
    declined = next(r for r in decoder.receipts if r["item_id"] == "toolu_x")
    require_equal(declined["output"]["original_chars"], None)
    require_equal(len(NT._receipts(list(decoder.receipts))), 4, "die Receipts bestehen die Core-Pruefung nicht")
    require_equal(decoder.result["permission_denials"], 1)
    require_equal(decoder.terminal(0), ("completed", ""))


def t_a_bash_exit_code_comes_from_the_measured_cli_form_and_only_from_it():
    """Codex-Review 19.09.2026, Befund b: Claude-Befehlsbelege trugen immer `exit_code: None`,
    waehrend die Beobachtungsprojektion (`native_observations._receipt`) nur exit 0 als
    lokale Ausfuehrung zaehlt — kein Claude-Befehl konnte je ein Kriterium decken. Gemessen
    am gepruerften Build: Exit 0 ⇒ is_error false + stdout-Objekt (interrupted false);
    Exit N ⇒ is_error true + Ausgabe `Exit code N`. Alles andere bleibt None."""
    from solvio.agent_runtime import native_observations as NO
    decoder, _, _ = _decoder()
    decoder.feed(_init())
    decoder.feed(_assistant([{"type": "tool_use", "id": f"toolu_{i}", "name": "Bash", "input": {"command": f"cmd {i}"}}
                             for i in range(5)]))
    decoder.feed(_tool_result("toolu_0", "ok"))                                                   # gemessen: Exit 0
    decoder.feed(_tool_result("toolu_1", "Exit code 3\nout\nerr", is_error=True))                # gemessen: Exit 3
    decoder.feed(_tool_result("toolu_2", "teilweise", tool_use_result={"stdout": "teilweise", "stderr": "",
                                                                       "interrupted": True, "isImage": False}))
    decoder.feed(_tool_result("toolu_3", "ok", tool_use_result={}))                                # keine CLI-Form
    decoder.feed(_tool_result("toolu_4", "Command failed without code", is_error=True))
    decoder.feed(_result("fertig"))
    codes = {r["item_id"]: (r["status"], r["exit_code"]) for r in decoder.receipts}
    require_equal(codes, {"toolu_0": ("completed", 0), "toolu_1": ("failed", 3), "toolu_2": ("completed", None),
                          "toolu_3": ("completed", None), "toolu_4": ("failed", None)})
    receipts = NT._receipts(list(decoder.receipts))
    require_equal(len(receipts), 5, "die Receipts bestehen die Core-Pruefung nicht")
    flags = {r["item_id"]: NO._receipt(dict(r)) for r in receipts}
    require_equal(flags, {"toolu_0": True, "toolu_1": False, "toolu_2": False, "toolu_3": False, "toolu_4": False},
                  "nur der gemessene Exit-0-Beleg ist ein Kandidat fuer lokale Ausfuehrung")


def t_token_literal_and_key_shapes_are_redacted_in_command_output_and_result():
    decoder, _, _ = _decoder()
    decoder.feed(_init())
    decoder.feed(_assistant([{"type": "tool_use", "id": "toolu_1", "name": "Bash",
                              "input": {"command": "echo " + TOKEN}}]))
    decoder.feed(_tool_result("toolu_1", TOKEN + "\nsk-ant-api03-" + "z" * 40))
    decoder.feed(_result("Der Schluessel war " + TOKEN + " und sk-abcdefghijklmnopqrstuvwxyz0123"))
    receipt = decoder.receipts[0]
    for block in (receipt["command"], receipt["output"]):
        require(block["redacted"] is True and L.MASK in block["text"] and type(block["original_chars"]) is int,
                f"nicht redigiert: {block}")
    everything = json.dumps(decoder.receipts) + decoder.result["text"]
    require(TOKEN not in everything and "sk-ant-api03" not in everything and "abcdefghijklmnop" not in everything)
    require(L.MASK in decoder.result["text"])
    NT._receipts(list(decoder.receipts))
    # Die Literal-Maske greift auch bei einer Form, die kein Muster trifft.
    odd = CNT.redactor("kanarienvogel-wert-0001")("Wert: kanarienvogel-wert-0001!")
    require_equal(odd, "Wert: <entfernt>!")


def t_terminal_criteria_result_subtype_exit_without_result_and_timeout():
    decoder, _, _ = _decoder()
    decoder.feed(_init())
    decoder.feed(_assistant([{"type": "text", "text": "x"}]))
    require_equal(decoder.terminal(0), ("failed", "native_terminal_missing"), "Exit ohne result")
    require_equal(decoder.terminal(None)[0], "unknown", "SIGKILL/Timeout ist ungewiss")
    decoder.feed(_result("API Error", is_error=True, status=401))
    require_equal(decoder.terminal(1), ("failed", "api_error_401"))
    other, _, _ = _decoder()
    other.feed(_init())
    other.feed(_result("stop", subtype="error_max_turns"))
    require_equal(other.terminal(0), ("failed", "error_max_turns"))
    other.feed(_result("noch eins"))
    require_equal(other.unknown_lines, 1, "nach result wird nichts mehr geglaubt")
    huge, _, _ = _decoder()
    try:
        huge.feed("x" * (CNT.MAX_LINE + 1))
    except ValueError:
        pass
    else:
        raise AssertionError("eine Ueberlaenge wurde verarbeitet")


def t_the_last_json_block_is_parsed_and_the_preamble_is_data():
    body = {"status": "completed", "text": {"findings": ["a"], "evidence": [], "assumptions": [],
            "uncertainties": [], "rejected_alternatives": [], "risk_notes": [], "recommended_path": "p",
            "confidence": "hoch", "files": [{"path": "b.txt", "requirement": "R1"}]},
            "sources": ["https://example.org/q"]}
    text = 'Vorspann {"status": "failed"} und noch {kaputt\n' + json.dumps(body) + "\nNachspann"
    status, fields, files, helpers, sources = CNT.parse_result(text)
    require_equal((status, files, sources, helpers), ("completed", body["text"]["files"], body["sources"], ((), ())))
    require_equal(fields["findings"], ["a"])
    # Eine abgewiesene Helferangabe faellt einzeln, das Ergebnis bleibt (gemessen 19.09.2026, C2-Durchstich 3).
    declared = dict(body["text"], helpers=[{"path": "tools/a.py", "name": "CSV- und Berichtsgenerator", "purpose": "p"},
                                           {"path": "tools/b.py", "name": "csv_helper", "purpose": "p"}])
    status, _, files, helpers, _ = CNT.parse_result(json.dumps(dict(body, text=declared)))
    require_equal((status, files), ("completed", body["text"]["files"]))
    require_equal(helpers, (({"path": "tools/b.py", "name": "csv_helper", "purpose": "p"},), ((0, "helper_declaration_invalid_name"),)))
    for bad in ('kein json', json.dumps({"status": "completed"}), json.dumps({"status": "x", "text": body["text"]}),
                json.dumps({"status": "completed", "text": dict(body["text"], files=[{"path": "", "requirement": "R"}])}),
                json.dumps({"status": "completed", "text": dict(body["text"], files=[{"path": "x", "requirement": "R"}] * 2)}),
                json.dumps({"status": "completed", "text": dict(body["text"], extra=1)})):
        try:
            CNT.parse_result(bad)
        except (ValueError, TypeError, KeyError):
            continue
        raise AssertionError(f"angenommen: {bad[:40]}")


def t_a_result_longer_than_the_excerpt_cap_keeps_its_final_block():
    """Der Block steht per Vertrag am ENDE; die Auszugskappe (MAX_RESULT_CHARS)
    behaelt den Anfang. Ohne den getrennt gehaltenen Block verlor jede Antwort
    ueber 18000 Zeichen ihr Ergebnis (native_result_invalid bei terminal
    completed). Gemessen mit 17227/18127/18730/30232 Zeichen."""
    body = {"status": "completed", "text": {"findings": ["lang"], "evidence": [], "assumptions": [],
            "uncertainties": [], "rejected_alternatives": [], "risk_notes": [], "recommended_path": "p",
            "confidence": "hoch", "files": []}, "sources": []}
    block = json.dumps(body)
    for preamble in (1000, 17900, 18500, 30000):
        decoder, _, _ = _decoder()
        decoder.feed(_init())
        decoder.feed(_assistant([{"type": "text", "text": "x"}]))
        text = "Vorspann " * (preamble // 9) + "\n" + block
        decoder.feed(_result(text))
        require_equal(decoder.terminal(0), ("completed", ""))
        require_equal(len(decoder.result["text"]), min(len(text), CNT.MAX_RESULT_CHARS), "Auszug bleibt gekappt")
        require_equal(decoder.result["text_chars"], len(text))
        require_equal(decoder.result["block"], block, f"Block verloren bei {len(text)} Zeichen")
        status, fields, files, helpers, sources = CNT.parse_result(decoder.result["block"] or decoder.result["text"])
        require_equal((status, fields["findings"]), ("completed", ["lang"]))
    # Ohne Block: leer, und der Auszug traegt dann das Urteil (native_result_invalid).
    decoder, _, _ = _decoder()
    decoder.feed(_init())
    decoder.feed(_result("nur Text ohne Ergebnisobjekt"))
    require_equal(decoder.result["block"], "")
    try:
        CNT.parse_result(decoder.result["block"] or decoder.result["text"])
    except ValueError as exc:
        require_equal(str(exc), "native_result_invalid")
    else:
        raise AssertionError("ein Text ohne Block wurde als Ergebnis angenommen")
    # Der Block ist redigiert wie der Auszug: das Token-Literal erreicht ihn nie.
    decoder, _, _ = _decoder()
    decoder.feed(_init())
    leaky = json.dumps(dict(body, text=dict(body["text"], findings=["Token " + TOKEN])))
    decoder.feed(_result("Vorspann\n" + leaky))
    require(TOKEN not in decoder.result["block"] and "<entfernt>" in decoder.result["block"])


def t_the_nonstart_proof_needs_no_assistant_and_a_book_of_only_429_without_output():
    decoder, _, _ = _decoder()
    decoder.feed(_init())
    decoder.feed(_assistant([{"type": "text", "text": "API Error (429)"}], synthetic=True))
    decoder.feed(_result("API Error", is_error=True, status=429))
    only_429 = {"rows": 11, "outcomes": {"upstream_error": 11}, "status_codes": {"429": 11}, "output_tokens": 0}
    require(CNT.nonstart_proven(decoder, only_429), "der reine 429-Fall gilt nicht als Nichtstart")
    require(CNT.nonstart_proven(decoder, {"rows": 2, "outcomes": {"denied": 2}, "status_codes": {"429": 2}, "output_tokens": 0}))
    # Gegenproben: eine 200-Zeile, Ausgabetoken, ein leeres Buch, ein 401.
    require(not CNT.nonstart_proven(decoder, {"rows": 2, "outcomes": {"forwarded": 1, "upstream_error": 1},
                                              "status_codes": {"200": 1, "429": 1}, "output_tokens": 0}))
    require(not CNT.nonstart_proven(decoder, dict(only_429, output_tokens=3)))
    require(not CNT.nonstart_proven(decoder, {"rows": 0, "outcomes": {}, "status_codes": {}, "output_tokens": 0}))
    require(not CNT.nonstart_proven(decoder, {"rows": 1, "outcomes": {"upstream_error": 1}, "status_codes": {"401": 1}, "output_tokens": 0}))
    require(not CNT.nonstart_proven(decoder, None))
    started, _, _ = _decoder()
    started.feed(_init())
    started.feed(_assistant([{"type": "text", "text": "echt"}]))
    require(not CNT.nonstart_proven(started, only_429), "ein gestarteter Turn ist kein Nichtstart")


def t_receipts_keep_the_newest_hundred_within_the_byte_budget():
    from solvio.specialists.native_task_profile import MAX_RECEIPT_BYTES
    decoder, _, _ = _decoder()
    decoder.feed(_init())
    for index in range(130):
        decoder.feed(_assistant([{"type": "tool_use", "id": f"toolu_{index}", "name": "Bash",
                                  "input": {"command": f"echo {index} " + "y" * 900}}]))
        decoder.feed(_tool_result(f"toolu_{index}", "z" * 3000))
    decoder.feed(_result("ok"))
    require(len(decoder.receipts) <= 100 and decoder.dropped_receipts >= 30, len(decoder.receipts))
    require_equal(len(decoder.receipts) + decoder.dropped_receipts, 130, "Receipts verschwanden ungezaehlt")
    require_equal(decoder.receipts[-1]["item_id"], "toolu_129", "das neueste Receipt fehlt")
    require_equal(decoder.receipts[-1]["output"]["text"], "z" * 3000, "das neueste Receipt wurde geleert")
    require(len(json.dumps(decoder.receipts, ensure_ascii=True).encode()) <= MAX_RECEIPT_BYTES)
    NT._receipts(list(decoder.receipts))


# =====================================================================
# Der MCP-Adapter (Einheit): Kennungen aus argv, nie aus dem Modell
# =====================================================================

class _Client:
    def __init__(self):
        self.calls = []
        self.manifest_calls = 0

    def manifest(self):
        self.manifest_calls += 1
        return [{"type": "function", "name": "portal_list", "description": "d",
                 "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}}]

    def call(self, body):
        self.calls.append(body)
        if body["tool"] != "portal_list":
            raise ValueError("native_tool_not_allowed")
        return {"success": True, "contentItems": [{"type": "inputText", "text": "{\"portals\":[]}"}]}


def t_the_adapter_maps_three_methods_and_binds_thread_and_turn_from_argv():
    client = _Client()
    kw = dict(thread_id=SID, turn_id=SID + "/pc-1")
    init = ADAPTER.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {"protocolVersion": "2025-06-18", "capabilities": {}}}, client, **kw)
    require_equal(init["result"]["serverInfo"]["name"], "solvio")
    require_equal(init["result"]["capabilities"], {"tools": {}})
    require(ADAPTER.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, client, **kw) is None)
    tools = ADAPTER.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, client, **kw)
    require_equal([t["name"] for t in tools["result"]["tools"]], ["portal_list"])
    require_equal(client.manifest_calls, 1)
    forged = ADAPTER.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                             "params": {"name": "portal_list", "arguments": {}, "turnId": "forged", "threadId": "forged"}},
                            client, **kw)
    require_equal(forged["result"]["isError"], False)
    require_equal(client.calls[-1], {"threadId": SID, "turnId": SID + "/pc-1", "callId": "7",
                                     "tool": "portal_list", "arguments": {}})
    refused = ADAPTER.handle({"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                              "params": {"name": "portal_open", "arguments": {}}}, client, **kw)
    require("error" in refused and "ValueError" in refused["error"]["message"])
    require("native_tool_not_allowed" not in refused["error"]["message"], "Ausnahmetext ueber den Draht")
    unknown = ADAPTER.handle({"jsonrpc": "2.0", "id": 9, "method": "resources/list"}, client, **kw)
    require_equal(unknown["error"]["code"], -32601)
    require_equal(ADAPTER.handle({"jsonrpc": "1.0", "id": 1, "method": "ping"}, client, **kw)["error"]["code"], -32600)
    source = (Path(CNT.__file__).parent / CNT.ADAPTER_NAME).read_text()
    for forbidden in ("subprocess", "urllib", "http.client", "solvio."):
        require(forbidden not in source.replace("solvio-Werkzeugbruecke", ""), f"der Adapter importiert {forbidden}")


def t_write_mcp_config_names_the_adapter_copy_and_the_core_chosen_identities():
    with tempfile.TemporaryDirectory() as folder:
        jail = Path(folder) / "at-9"
        jail.mkdir(mode=0o700)
        path = CNT.write_mcp_config(str(jail), python="/usr/bin/python3", endpoint="/private/var/s/core.sock",
                                    manifest_digest="a" * 64, thread_id=SID, turn_id=SID + "/pc-2")
        body = json.loads(Path(path).read_text())
        server = body["mcpServers"]["solvio"]
        require_equal(server["command"], "/usr/bin/python3")
        require_equal(server["args"][:3], ["-I", "-B", str(jail / "adapter" / CNT.ADAPTER_NAME)])
        require_equal(server["args"][3:], ["--core-tools-socket", "/private/var/s/core.sock", "--core-tools-digest", "a" * 64,
                                           "--thread-id", SID, "--turn-id", SID + "/pc-2"])
        require_equal(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        try:
            CNT.write_mcp_config(str(jail), python="/usr/bin/python3", endpoint="relative.sock",
                                 manifest_digest="a" * 64, thread_id=SID, turn_id="x")
        except ValueError:
            pass
        else:
            raise AssertionError("ein relativer Endpunkt wurde geschrieben")


def t_a_symlink_planted_in_the_jail_never_carries_a_core_write_outside_it():
    """Codex-Review 19.09.2026, Befund a: der Kaefig ist zwischen zwei Turns fuer den
    Arbeiter beschreibbar. Ein dort abgelegter Symlink unter einem Core-geschriebenen
    Namen (Profil, mcp.json, Adapter, Unterordner) darf den Core-Schreibvorgang nicht
    an das Ziel tragen — er wird abgewiesen, das Ziel bleibt byte-gleich."""
    with tempfile.TemporaryDirectory() as folder:
        base = Path(os.path.realpath(folder))
        victim = base / "victim.txt"
        victim.write_bytes(b"UNBERUEHRT\n")
        workspace = base / "ws"
        workspace.mkdir(mode=0o700)
        jail = base / "at-9"
        jail.mkdir(mode=0o700)
        with patch.object(I, "worker_profile", return_value="(version 1)\n(deny default)\n"):
            for name in (I.PROFILE_NAME, CNT.MCP_CONFIG_NAME,
                         os.path.join(CNT.JAIL_ADAPTER, CNT.ADAPTER_NAME)):
                target = jail / name
                target.parent.mkdir(mode=0o700, exist_ok=True)
                if target.exists() or target.is_symlink():
                    os.unlink(target)
                os.symlink(victim, target)
                try:
                    if name == CNT.MCP_CONFIG_NAME:
                        CNT.write_mcp_config(str(jail), python="/usr/bin/python3", endpoint="/private/var/s/core.sock",
                                             manifest_digest="a" * 64, thread_id=SID, turn_id=SID + "/pc-2")
                    else:
                        CNT.prepare_jail(str(jail), workspace=str(workspace), broker_port=8792)
                except I.JailEntryTampered:
                    pass
                else:
                    raise AssertionError(f"ein Symlink unter {name} wurde beschrieben")
                require_equal(victim.read_bytes(), b"UNBERUEHRT\n", f"das Ziel hinter {name} wurde veraendert")
                require(target.is_symlink(), f"der Symlink unter {name} wurde still repariert")
                os.unlink(target)
            # Ein Unterordner als Symlink auf ein fremdes Verzeichnis: ebenso abgewiesen.
            foreign = base / "foreign"
            foreign.mkdir(mode=0o700)
            import shutil
            shutil.rmtree(jail / CNT.JAIL_ADAPTER)
            os.symlink(foreign, jail / CNT.JAIL_ADAPTER)
            try:
                CNT.prepare_jail(str(jail), workspace=str(workspace), broker_port=8792)
            except I.JailEntryTampered:
                pass
            else:
                raise AssertionError("ein Symlink-Unterordner wurde angenommen")
            require_equal(sorted(os.listdir(foreign)), [], "der Adapter landete im fremden Verzeichnis")
            os.unlink(jail / CNT.JAIL_ADAPTER)
            # Ohne Manipulation: der gewoehnliche Aufbau, Adapter 0o400, Profil 0o600 — und ein
            # zweiter Aufbau (naechster Turn) ersetzt die regulaeren Dateien ohne Fehler.
            for _ in range(2):
                layout = CNT.prepare_jail(str(jail), workspace=str(workspace), broker_port=8792)
                require_equal(stat.S_IMODE(os.lstat(layout["adapter"]).st_mode), 0o400)
                require_equal(stat.S_IMODE(os.lstat(layout["profile"]).st_mode), 0o600)
                require(not Path(layout["adapter"]).is_symlink() and not Path(layout["profile"]).is_symlink())
            require_equal(Path(layout["adapter"]).read_bytes(), (Path(CNT.__file__).parent / CNT.ADAPTER_NAME).read_bytes())
            # Ein zweiter Name auf dieselbe Datei (Hardlink) zaehlt als manipuliert.
            os.unlink(jail / CNT.MCP_CONFIG_NAME) if (jail / CNT.MCP_CONFIG_NAME).exists() else None
            os.link(victim, jail / CNT.MCP_CONFIG_NAME)
            try:
                CNT.write_mcp_config(str(jail), python="/usr/bin/python3", endpoint="/private/var/s/core.sock",
                                     manifest_digest="a" * 64, thread_id=SID, turn_id=SID + "/pc-2")
            except I.JailEntryTampered:
                pass
            else:
                raise AssertionError("ein Hardlink unter mcp.json wurde beschrieben")
            require_equal(victim.read_bytes(), b"UNBERUEHRT\n", "das Ziel hinter dem Hardlink wurde veraendert")


# =====================================================================
# run_task gegen die echten Core-Naehte — mit gespielter CLI
# =====================================================================

FAKE_CLI = r'''
import json, os, subprocess, sys, time, uuid
argv = sys.argv[1:]
def opt(flag, default=None):
    return argv[argv.index(flag) + 1] if flag in argv else default
sid = opt("--session-id") or opt("--resume")
config = os.environ["CLAUDE_CONFIG_DIR"]
jail = os.path.dirname(config)
mode = open(os.path.join(jail, "mode")).read().strip()
prompt = sys.stdin.read()
open(os.path.join(jail, "seen.json"), "w").write(json.dumps({"argv": argv, "env": {k: os.environ.get(k) for k in
    ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "CLAUDE_CONFIG_DIR", "TMPDIR", "HOME")}, "prompt_chars": len(prompt),
    "prompt_has_objective": "Owner-Auftrag" in prompt, "cwd": os.getcwd()}))
def emit(v): sys.stdout.write(json.dumps(v) + "\n"); sys.stdout.flush()
def init(sid_=None, mcp=()):
    emit({"type": "system", "subtype": "init", "cwd": os.getcwd(), "session_id": sid_ or sid, "tools": ["Bash", "Edit", "Read"],
          "mcp_servers": list(mcp), "model": "claude-sonnet-5", "permissionMode": "acceptEdits",
          "apiKeySource": "ANTHROPIC_API_KEY", "claude_code_version": "2.1.261", "uuid": str(uuid.uuid4())})
def assistant(blocks, synthetic=False):
    m = {"id": "msg_" + str(uuid.uuid4())[:8], "type": "message", "role": "assistant", "model": "claude-sonnet-5",
         "content": blocks, "stop_reason": "tool_use", "usage": {"input_tokens": 10, "output_tokens": 5}}
    e = {"type": "assistant", "message": m, "parent_tool_use_id": None, "session_id": sid, "uuid": str(uuid.uuid4())}
    if synthetic:
        m["model"] = "<synthetic>"; e["error"] = "rate_limit"; e["is_api_error_message"] = True
    emit(e)
def user(tid, content, is_error=False):
    # Gemessene CLI-Form (2.1.261): Exit 0 traegt das stdout-Objekt, Exit != 0 den Fehlertext.
    tool_use_result = ("Error: " + content) if is_error else {"stdout": content, "stderr": "", "interrupted": False,
                                                              "isImage": False, "noOutputExpected": False}
    emit({"type": "user", "message": {"role": "user", "content": [{"tool_use_id": tid, "type": "tool_result",
          "content": content, "is_error": is_error}]}, "parent_tool_use_id": None, "session_id": sid, "uuid": str(uuid.uuid4()),
          "tool_use_result": tool_use_result})
def result(text, is_error=False, status=None):
    body = {"type": "result", "subtype": "success", "is_error": is_error, "duration_ms": 3, "num_turns": 2,
            "result": text, "session_id": sid, "total_cost_usd": 0, "terminal_reason": "api_error" if status else "completed",
            "usage": {"input_tokens": 20, "output_tokens": 0 if status else 10}, "permission_denials": [], "uuid": str(uuid.uuid4())}
    if status: body["api_error_status"] = status
    emit(body)
final = json.dumps({"status": "completed", "text": {"findings": ["Bericht liegt vor"], "evidence": [], "assumptions": [],
    "uncertainties": [], "rejected_alternatives": [], "risk_notes": [], "recommended_path": "fertig", "confidence": "hoch",
    "files": [{"path": "bericht.txt", "requirement": "R1"}]}, "sources": ["https://example.org/quelle"]})
if mode == "nonstart429":
    init(); time.sleep(0.05)
    assistant([{"type": "text", "text": "API Error: Request rejected (429) · scripted"}], synthetic=True)
    result("API Error: Request rejected (429) · scripted", is_error=True, status=429); sys.exit(1)
if mode in ("started429", "started529"):
    status_ = 429 if mode == "started429" else 529
    # Der Anbieter hat geantwortet (echtes assistant/tool_use, Datei entstand),
    # dann endet die CLI am 429 des laufenden Turns — gemessene Form (Modultext).
    init()
    assistant([{"type": "tool_use", "id": "toolu_q", "name": "Bash", "input": {"command": "printf teil > bericht.txt"}}])
    open("bericht.txt", "w").write("teil")
    user("toolu_q", "")
    assistant([{"type": "text", "text": "API Error: Request rejected (%d) · scripted" % status_}], synthetic=True)
    result("API Error: Request rejected (%d) · scripted" % status_, is_error=True, status=status_); sys.exit(1)
if mode == "noresult":
    init(); assistant([{"type": "text", "text": "halb"}]); sys.exit(0)
if mode == "foreign":
    init(sid_="00000000-0000-4000-8000-000000000000"); sys.exit(0)
if mode == "hang":
    init(); assistant([{"type": "tool_use", "id": "toolu_h", "name": "Bash", "input": {"command": "sleep 60"}}]); time.sleep(60); sys.exit(0)
init(mcp=[{"name": "solvio", "status": "connected"}] if mode == "mcp" else [])
assistant([{"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "printf ok > bericht.txt; cat bericht.txt; echo " + os.environ["ANTHROPIC_API_KEY"]}}])
open("bericht.txt", "w").write("ok")
user("toolu_1", "ok\n" + os.environ["ANTHROPIC_API_KEY"])
if mode == "mcp":
    cfg = json.load(open(opt("--mcp-config")))["mcpServers"]["solvio"]
    proc = subprocess.Popen([cfg["command"], *cfg["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    def rpc(msg):
        proc.stdin.write(json.dumps(msg) + "\n"); proc.stdin.flush()
        if "id" in msg: return json.loads(proc.stdout.readline())
    rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}}})
    rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
    listed = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assistant([{"type": "tool_use", "id": "toolu_m", "name": "mcp__solvio__portal_list", "input": {}}])
    called = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "portal_list", "arguments": {}}})
    proc.stdin.close(); proc.wait(timeout=10)
    text = called.get("result", {}).get("content", [{}])[0].get("text", json.dumps(called))
    user("toolu_m", text, is_error=bool(called.get("error") or called.get("result", {}).get("isError")))
    open(os.path.join(jail, "mcp_seen.json"), "w").write(json.dumps({"listed": listed, "called": called}))
assistant([{"type": "text", "text": "Fertig.\n" + final}])
result("Fertig.\n" + final)
sys.exit(0)
'''


class FakeBroker:
    def __init__(self, port, usage=None):
        self.port = port
        self.tokens, self.leases, self.closed = [], [], []
        self.usage = usage or {"rows": 3, "outcomes": {"forwarded": 3}, "status_codes": {"200": 3},
                               "output_tokens": 30}
        self.ledger = self

    def register_principal(self, name):
        require_equal(name, CNT.PRINCIPAL)
        token = "sk-solvio-broker-" + ("%048x" % (len(self.tokens) + 1))
        self.tokens.append(token)
        return token

    def open_lease(self, principal, ref, *, deadline, **_):
        require_equal(principal, CNT.PRINCIPAL)
        lease = f"lease-{len(self.leases) + 1}"
        self.leases.append({"ref": ref, "deadline": deadline, "opened": time.time()})
        return lease

    def close_lease(self, lease_id):
        self.closed.append(lease_id)

    def usage_for_task(self, task_ref, *, since):
        return dict(self.usage, task_ref=task_ref, since=since)


@contextmanager
def world(mode="ok", *, mcp=False, timeout_s=CNT.PROFILE_TIMEOUT):
    with ExitStack() as stack:
        w = stack.enter_context(P.world())
        base = stack.enter_context(private_folder("c4-"))
        sockets = stack.enter_context(socket_root())
        from solvio.agent_runtime import artifact_creation as A, result_files as RF, store as S
        from solvio.agent_runtime.task_authority import CapabilityGrant, VerifiedTaskReceipt
        RF.register(w.router, w.ledger)
        task = w.ledger.create_task(objective="Lege bericht.txt an und liste die Portale.", scope=S.SCOPE_TASK,
                                    created_origin="trusted_interactive_app", created_principal="owner:device")
        run = w.ledger.create_run(task_id=task.task_id)
        w.ledger.transition(run.run_id, S.PLANNING)
        w.ledger.transition(run.run_id, S.RUNNING)
        w.costs.configure(task.task_id)
        w.grant = w.authority.issue(task.task_id, run.run_id,
            receipt=VerifiedTaskReceipt("app_session", "test:verified-claude-task", "owner:device"),
            capabilities=(CapabilityGrant("portal_list", 1), CapabilityGrant("result_files_list", 1), A.capability_grant()),
            expires_at=time.time() + 3600)
        step = w.ledger.create_step(run_id=run.run_id, seq=1, kind="specialist", specialist_profile=CNT.PROFILE)
        w.ledger.update_step(step.step_id, state="running")
        w.task, w.run, w.step = task.task_id, run.run_id, step.step_id
        workspace = base / "w" / task.task_id
        workspace.mkdir(parents=True, mode=0o700)
        os.chmod(base / "w", 0o700)
        jail = base / "j" / task.task_id
        jail.mkdir(parents=True, mode=0o700)
        os.chmod(base / "j", 0o700)
        (jail / "mode").write_text(mode)
        w.workspace, w.jail, w.sockets = str(workspace.resolve()), str(jail.resolve()), sockets
        # Gespielte CLI und gespielter sandbox-exec (das Profil misst der Kanarienvogel).
        cli = base / "claude-fake"
        cli.write_text("#!" + str(Path(sys.executable).resolve()) + " -ISB\n" + FAKE_CLI)
        cli.chmod(0o700)
        wrapper = base / "sandbox-exec-fake"
        wrapper.write_text("#!/bin/sh\nshift 2\nexec \"$@\"\n")
        wrapper.chmod(0o700)
        stack.enter_context(patch.object(I, "SANDBOX_EXEC", str(wrapper)))
        stack.enter_context(patch.object(CNT, "resolve_claude", lambda: str(cli)))
        stack.enter_context(patch.object(PV, "claude_status", AsyncMock(return_value=PV.ProviderStatus(
            "claude-code", True, auth="claude.ai", billing_mode=PV.SUBSCRIPTION))))
        w.broker = FakeBroker(BROKER_PORT)
        w.config = CNT.ClaudeWorkerConfig(jail=w.jail, broker_port=BROKER_PORT,
                                          mcp_mode="bridge" if mcp else "none", session_mode="resume",
                                          timeout_s=timeout_s)
        w.sessions = N.NativeSessions(w.ledger, authority=w.authority)
        w.session = w.sessions.bind(task_id=w.task, run_id=w.run, provider="claude-code", profile=CNT.PROFILE,
                                    policy_digest=CNT.claude_task_policy(w.config), workspace=w.workspace)
        w.request = SP.SpecialistRequest(CNT.PROFILE, task.objective, "", run_id=w.run)
        w.events = []
        yield w


async def run(w, **overrides):
    adapter = T.NativeCoreTools(w.ledger, w.sessions, w.session.session_id, w.run, w.router)
    bridge = NativeToolBridge(adapter, socket_root=w.sockets)
    w.bridge = bridge
    operation = w.step if not overrides.pop("fresh_step", False) else w.step
    with D.task_cost_scope(w.ledger, task_id=w.task, run_id=w.run, phase="specialist",
                           operation_id=operation, quote_adapter=lambda *_: B.FREE):
        return await CNT.run_task(w.request, config=w.config,
                                  continuation=H.NativeContinuation(w.sessions, w.session.session_id),
                                  bridge=bridge, on_event=w.events.append, broker=w.broker, **overrides)


def _turns(w):
    with w.ledger._open() as db:
        return [dict(r) for r in db.execute("SELECT * FROM agent_native_turns ORDER BY rowid").fetchall()]


async def t_a_full_turn_binds_the_session_records_receipts_and_closes_the_lease():
    with world("ok") as w:
        outcome = await run(w)
        require(outcome.result.ok, outcome.result.reason)
        require_equal(outcome.result.provider, "claude-code")
        require_equal(outcome.runtime, CNT.RUNTIME)
        require_equal(outcome.native_files, ("bericht.txt",))
        require_equal(outcome.native_file_requirements, (("bericht.txt", "R1"),))
        require_equal(outcome.result.findings, ["Bericht liegt vor"])
        require("Quelle: https://example.org/quelle" in outcome.result.evidence)
        require(outcome.dispatch_started and outcome.cost_status == "settled", outcome.cost_status)
        receipts = outcome.native_tool_receipts
        require_equal([r["kind"] for r in receipts], ["commandExecution"])
        require(receipts[0]["output"]["redacted"] and w.broker.tokens[0] not in json.dumps(receipts))
        require(w.broker.tokens[0] not in outcome.result.raw_excerpt)
        session = w.sessions.session(w.session.session_id)
        turns = _turns(w)
        require_equal(len(turns), 1)
        require_equal((turns[0]["state"], turns[0]["terminal_status"]), ("terminal", "completed"))
        require_equal(session.native_thread_id, outcome.native_thread_id)
        require(turns[0]["native_turn_id"].startswith(session.native_thread_id + "/pc-"), turns[0]["native_turn_id"])
        require_equal(outcome.native_turn_id, turns[0]["native_turn_id"])
        require_equal(len(w.broker.leases), 1)
        require_equal(w.broker.leases[0]["ref"], "task:" + w.task)
        require(abs(w.broker.leases[0]["deadline"] - w.broker.leases[0]["opened"] - CNT.PROFILE_TIMEOUT) < 5)
        require_equal(w.broker.closed, ["lease-1"], "der Lease wurde nicht geschlossen")
        require_equal([e["event"] for e in w.events], ["started"])
        seen = json.loads(Path(w.jail, "seen.json").read_text())
        require_equal(seen["env"]["ANTHROPIC_API_KEY"], w.broker.tokens[0])
        require_equal(seen["env"]["CLAUDE_CONFIG_DIR"], os.path.join(w.jail, "config"))
        require_equal(seen["env"]["ANTHROPIC_BASE_URL"], f"http://127.0.0.1:{BROKER_PORT}")
        require(seen["prompt_has_objective"] and seen["cwd"] == w.workspace)
        require_equal(seen["argv"][-2:], ["--session-id", session.native_thread_id])
        require(os.path.isfile(os.path.join(w.jail, "builder.sandbox.sb")))
        require_equal(stat.S_IMODE(os.stat(os.path.join(w.jail, "adapter", CNT.ADAPTER_NAME)).st_mode), 0o400)


async def t_a_follow_up_turn_resumes_the_same_claude_session():
    with world("ok") as w:
        first = await run(w)
        require(first.result.ok, first.result.reason)
        w.step = w.ledger.create_step(run_id=w.run, seq=2, kind="specialist", specialist_profile=CNT.PROFILE).step_id
        w.ledger.update_step(w.step, state="running")
        second = await run(w)
        require(second.result.ok, second.result.reason)
        seen = json.loads(Path(w.jail, "seen.json").read_text())
        require_equal(seen["argv"][-2:], ["--resume", first.native_thread_id])
        require_equal(second.native_thread_id, first.native_thread_id)
        require(second.native_turn_id != first.native_turn_id)
        turns = _turns(w)
        require_equal([t["terminal_status"] for t in turns], ["completed", "completed"])
        require_equal(len(w.broker.leases), 2)
        require_equal(len(w.broker.tokens), 2, "je Lauf ein frischer Token")


async def t_a_proven_quota_nonstart_is_not_started_and_keeps_dispatch_unstarted():
    with world("nonstart429") as w:
        w.broker.usage = {"rows": 11, "outcomes": {"upstream_error": 11}, "status_codes": {"429": 11}, "output_tokens": 0}
        outcome = await run(w)
        require(not outcome.result.ok and outcome.result.reason == "quota" and outcome.quota, outcome.result.reason)
        require(outcome.dispatch_started is False, "der Nichtstart traegt dispatch_started")
        turns = _turns(w)
        require_equal((turns[0]["state"], turns[0]["terminal_status"], turns[0]["native_turn_id"]),
                      ("terminal", "not_started", ""))
        require_equal(w.sessions.session(w.session.session_id).native_thread_id, "",
                      "ein Nichtstart band die Sitzung an eine leere CLI-Sitzung")
        require_equal(w.broker.closed, ["lease-1"])
        require_equal(w.events, [])


async def t_a_book_with_one_forwarded_row_is_a_started_turn_without_switch_offer():
    with world("nonstart429") as w:
        w.broker.usage = {"rows": 2, "outcomes": {"forwarded": 1, "upstream_error": 1},
                          "status_codes": {"200": 1, "429": 1}, "output_tokens": 0}
        outcome = await run(w)
        # Kein Nichtstart: dispatch_started bleibt True (kein Wechselangebot,
        # kein zweiter Versuch); die Grenze selbst ist trotzdem `quota` (§2.6).
        require(not outcome.result.ok and outcome.quota, outcome.result.reason)
        require_equal((outcome.result.reason, outcome.stderr_note), ("quota", CNT.QUOTA_REASON))
        require(outcome.dispatch_started is True)
        turns = _turns(w)
        require_equal((turns[0]["state"], turns[0]["terminal_status"]), ("terminal", "failed"))
        require(turns[0]["native_turn_id"], "ein gestarteter Turn ohne Kennung")


async def t_a_quota_end_after_a_started_turn_is_the_quota_boundary_with_dispatch_started():
    """§2.6 „Gestarteter Turn": 429 NACH dem ersten echten assistant-Ereignis
    (die Datei entstand bereits) ist die Owner-Grenze `quota` mit
    dispatch_started=True — der Orchestrator parkt ohne Wechsel und ohne
    zweiten Schreibversuch (wie beim Codex-Worker), statt FAILED/no_result."""
    with world("started429") as w:
        outcome = await run(w)
        require(not outcome.result.ok, "ein 429 im laufenden Turn galt als Ergebnis")
        require_equal((outcome.result.reason, outcome.quota, outcome.dispatch_started),
                      ("quota", True, True))
        require_equal(outcome.stderr_note, "api_error_429", "der konkrete Status fehlt in der Notiz")
        require_equal(outcome.cost_status, "settled", "ein beendeter Prozess mit Exit-Code settelt")
        require((Path(w.workspace) / "bericht.txt").read_text() == "teil", "der Turn hatte bereits geschrieben")
        turns = _turns(w)
        require_equal((turns[0]["state"], turns[0]["terminal_status"]), ("terminal", "failed"))
        require(turns[0]["native_turn_id"].startswith(w.sessions.session(w.session.session_id).native_thread_id + "/"))
        require_equal([e["event"] for e in w.events], ["started"])
        require_equal(w.broker.closed, ["lease-1"])
    # Gegenprobe: ein anderer API-Status im gestarteten Turn bleibt ein Fehlschlag,
    # keine Kontingentgrenze.
    with world("started429") as w:
        (Path(w.jail) / "mode").write_text("started529")
        outcome = await run(w)
        require(not outcome.result.ok and not outcome.quota)
        require_equal(outcome.result.reason, "api_error_529")


async def t_an_exit_without_result_is_terminal_failed_and_a_hang_stays_unknown():
    with world("noresult") as w:
        outcome = await run(w)
        require(not outcome.result.ok and outcome.result.reason == "native_terminal_missing", outcome.result.reason)
        turns = _turns(w)
        require_equal((turns[0]["state"], turns[0]["terminal_status"]), ("terminal", "failed"))
    with patch.object(CNT, "PROCESS_GRACE", 1.0), world("hang", timeout_s=1.0) as w:
        outcome = await run(w)
        require(not outcome.result.ok and outcome.result.reason == "cost_recovery_required", outcome.result.reason)
        require_equal(outcome.cost_status, "unknown")
        turns = _turns(w)
        require_equal(turns[0]["state"], "unknown", "ein SIGKILL-Turn ist nicht ungewiss")
        require_equal(w.broker.closed, ["lease-1"], "der Lease ueberlebte den Abbruch")


async def t_a_foreign_session_in_init_leaves_the_turn_uncertain():
    with world("foreign") as w:
        outcome = await run(w)
        require(not outcome.result.ok, "eine fremde Sitzung wurde angenommen")
        require_equal(_turns(w)[0]["state"], "unknown")


async def t_a_refused_lease_is_a_gate_before_the_starter():
    with world("ok") as w:
        def refuse(*args, **kwargs):
            raise RuntimeError("cap")
        w.broker.open_lease = refuse
        outcome = await run(w)
        # Ein Tor VOR dem Starter ist die Owner-Grenze `provider_unavailable`
        # (PROVIDER_BLOCKERS), kein Fehlschlag des Auftrags; der konkrete
        # Grund steht in der Notiz, der Turn ist `not_started`.
        require_equal((outcome.result.reason, outcome.stderr_note), ("provider_unavailable", "broker_lease_refused"))
        require(outcome.dispatch_started is False and not outcome.quota)
        require_equal(_turns(w)[0]["terminal_status"], "not_started")
        require(not os.path.exists(os.path.join(w.jail, "seen.json")), "die CLI lief trotz verweigertem Lease")
        require_equal(outcome.cost_status, "released")
    with world("ok") as w:
        w.broker.port = w.config.broker_port + 1     # kein laufender Broker an der gebundenen Adresse
        outcome = await run(w)
        require_equal((outcome.result.reason, outcome.stderr_note, outcome.dispatch_started),
                      ("provider_unavailable", "native_broker_unavailable", False))
        require_equal(_turns(w), [], "ohne Broker wurde ein Turn angefordert")
        require_equal(w.broker.leases, [], "ohne Broker wurde ein Lease geoeffnet")


async def t_a_tampered_mcp_config_preserves_the_claimed_cost_recovery_truth():
    with world("ok", mcp=True) as w:
        victim = Path(w.workspace, "unberuehrt.txt")
        victim.write_bytes(b"unberuehrt\n")
        Path(w.jail, CNT.MCP_CONFIG_NAME).symlink_to(victim)
        outcome = await run(w)
        turns = _turns(w)
        require_equal(len(turns), 1)
        turn = turns[0]
        with w.ledger._open() as db:
            reservation = dict(db.execute("SELECT * FROM agent_cost_reservations WHERE reservation_id=?",
                                          (turn["reservation_id"],)).fetchone())
            invocation = dict(db.execute("SELECT * FROM agent_provider_invocations WHERE invocation_id=?",
                                         (turn["invocation_id"],)).fetchone())
        require_equal((turn["state"], reservation["state"], invocation["state"]),
                      ("unknown", "unknown", "unknown"))
        require_equal(victim.read_bytes(), b"unberuehrt\n")
        require(not Path(w.jail, "seen.json").exists(), "die CLI lief trotz manipuliertem MCP-Pfad")
        require_equal(w.broker.closed, ["lease-1"])
        require_equal((outcome.result.reason, outcome.stderr_note, outcome.dispatch_started, outcome.cost_status),
                      ("cost_recovery_required", "native_jail_tampered", True, "unknown"))
        require_equal((outcome.cost_invocation_id, outcome.cost_reservation_id),
                      (turn["invocation_id"], turn["reservation_id"]))
        # Auch nach Entfernen des Fremdeintrags ist ein unbekannter Claim kein
        # neuer Versuch: dieselbe Zustellung darf weder reservieren noch starten.
        Path(w.jail, CNT.MCP_CONFIG_NAME).unlink()
        retry = await run(w)
        require(not retry.result.ok, "die ungewisse Session wurde erneut angenommen")
        require_equal(_turns(w), turns)
        with w.ledger._open() as db:
            require_equal(db.execute("SELECT COUNT(*) FROM agent_cost_reservations").fetchone()[0], 1)
            require_equal(db.execute("SELECT COUNT(*) FROM agent_provider_invocations").fetchone()[0], 1)
        require_equal(len(w.broker.leases), 1)
        require(not Path(w.jail, "seen.json").exists())


async def t_a_tampered_profile_before_the_claim_remains_a_proven_nonstart():
    with world("ok", mcp=True) as w:
        victim = Path(w.workspace, "unberuehrt.txt")
        victim.write_bytes(b"unberuehrt\n")
        Path(w.jail, I.PROFILE_NAME).symlink_to(victim)
        outcome = await run(w)
        require_equal((outcome.result.reason, outcome.dispatch_started, outcome.cost_status),
                      ("native_jail_tampered", False, ""))
        require_equal((outcome.cost_invocation_id, outcome.cost_reservation_id), ("", ""))
        require_equal(_turns(w), [])
        with w.ledger._open() as db:
            require_equal(db.execute("SELECT COUNT(*) FROM agent_cost_reservations").fetchone()[0], 0)
            require_equal(db.execute("SELECT COUNT(*) FROM agent_provider_invocations").fetchone()[0], 0)
        require_equal(w.broker.leases, [])
        require_equal(victim.read_bytes(), b"unberuehrt\n")
        require(not Path(w.jail, "seen.json").exists())


async def t_the_bridge_mode_reaches_the_core_portal_tool_through_the_adapter():
    with world("mcp", mcp=True) as w:
        outcome = await run(w)
        require(outcome.result.ok, outcome.result.reason)
        seen = json.loads(Path(w.jail, "mcp_seen.json").read_text())
        require_equal([t["name"] for t in seen["listed"]["result"]["tools"]], ["portal_list", "result_files_list"])
        require("error" not in seen["called"], seen["called"])
        require_equal(seen["called"]["result"]["isError"], False)
        require("portale" in seen["called"]["result"]["content"][0]["text"], "kein Core-Portalergebnis")
        receipts = outcome.native_tool_receipts
        dynamic = [r for r in receipts if r["kind"] == "dynamicToolCall"]
        require_equal([(r["tool"], r["status"], r["success"]) for r in dynamic], [("portal_list", "completed", True)])
        config = json.loads(Path(w.jail, "mcp.json").read_text())["mcpServers"]["solvio"]["args"]
        require_equal(config[config.index("--thread-id") + 1], outcome.native_thread_id)
        require_equal(config[config.index("--turn-id") + 1], outcome.native_turn_id)
        profile = Path(w.jail, "builder.sandbox.sb").read_text()
        require(f'(path "{w.bridge.endpoint}")' in profile, "die AF_UNIX-Zeile fehlt im Profil")
        with w.ledger._open() as db:
            calls = [dict(r) for r in db.execute("SELECT * FROM agent_native_tool_calls").fetchall()]
        require_equal([(c["call_id"], c["state"]) for c in calls], [("3", "completed")])


async def t_a_stale_binding_or_wrong_profile_never_dispatches():
    with world("ok") as w:
        wrong = replace(w.request, profile="worker/codex")
        outcome = await CNT.run_task(wrong, config=w.config,
                                     continuation=H.NativeContinuation(w.sessions, w.session.session_id),
                                     bridge=NativeToolBridge(T.NativeCoreTools(w.ledger, w.sessions, w.session.session_id,
                                                                               w.run, w.router), socket_root=w.sockets),
                                     broker=w.broker)
        require_equal(outcome.result.reason, "native_task_profile_invalid")
        other = replace(w.config, session_mode="handover")
        with D.task_cost_scope(w.ledger, task_id=w.task, run_id=w.run, phase="specialist",
                               operation_id=w.step, quote_adapter=lambda *_: B.FREE):
            outcome = await CNT.run_task(w.request, config=other,
                                         continuation=H.NativeContinuation(w.sessions, w.session.session_id),
                                         bridge=NativeToolBridge(T.NativeCoreTools(w.ledger, w.sessions, w.session.session_id,
                                                                                   w.run, w.router), socket_root=w.sockets),
                                         broker=w.broker)
        require_equal(outcome.result.reason, "native_task_binding_or_result_invalid",
                      "ein anderer Policy-Digest wurde angenommen")
        require(not os.path.exists(os.path.join(w.jail, "seen.json")))
        require_equal(_turns(w), [])


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
