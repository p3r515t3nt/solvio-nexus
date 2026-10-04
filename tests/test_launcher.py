"""Der Starter unter N8/C4: `retain_stdout=False`, `brokered_environment(config_dir=...)`
und die gepruefte Kindumgebung `run(environment=...)`.

Alles hier laeuft gegen echte Kindprozesse (Python-Skripte), nie gegen ein
Modell. Die Codex-Faelle (Vorgabe `retain_stdout=True`) bleiben Wort fuer Wort
das heutige Verhalten — ein Test hier belegt das ausdruecklich.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _guard import enforce_assertions, require, require_equal  # noqa: E402
enforce_assertions()

from solvio.specialists import launcher as L  # noqa: E402

CEILING = L.MAX_OUTPUT * 4


def _script(folder: Path, body: str) -> L.Invocation:
    source = folder / "child.py"
    source.write_text(body)
    return L.Invocation(sys.executable, (str(source),), cwd=str(folder), timeout=20)


def _run(invocation, **kwargs):
    return asyncio.run(L.run(invocation, "", **kwargs))


# =====================================================================
# retain_stdout=False — der kompakte Stromkonsument
# =====================================================================

def t_retained_false_delivers_every_line_of_a_300kb_stream_and_keeps_nothing():
    """300 KB in 40 Zeilen — weit ueber der 240-KB-Grenze des behaltenen Stroms.
    Jede Zeile kommt an, nichts wird behalten, nichts ist abgeschnitten."""
    with tempfile.TemporaryDirectory() as folder:
        invocation = _script(Path(folder),
            "import sys\n"
            "for i in range(40):\n"
            "    sys.stdout.write(('%02d' % i) + 'x' * 7500 + '\\n')\n"
            "sys.stdout.flush()\n")
        lines = []
        outcome = _run(invocation, on_stdout_line=lines.append, retain_stdout=False)
        require_equal(len(lines), 40, "nicht jede Zeile wurde zugestellt")
        require(sum(len(l) + 1 for l in lines) > CEILING, "der Strom lag unter der Grenze — kein Beweis")
        require_equal([l[:2] for l in lines], ["%02d" % i for i in range(40)], "Reihenfolge verletzt")
        require(outcome.truncated is False, "ein vollstaendig zugestellter Strom gilt als abgeschnitten")
        require_equal(outcome.text, "", "retain_stdout=False hat Text behalten")
        require_equal(outcome.exit_code, 0)
        require(outcome.ok)


def t_retained_false_discards_only_the_one_overlong_line_and_marks_truncated():
    """Eine Einzelzeile ueber `MAX_OUTPUT*4` wird verworfen (nicht gesammelt),
    `truncated` wird gesetzt — und die Zeilen DANACH kommen weiter an."""
    with tempfile.TemporaryDirectory() as folder:
        invocation = _script(Path(folder),
            "import sys\n"
            "sys.stdout.write('vorher\\n')\n"
            f"sys.stdout.write('y' * {CEILING + 5000} + '\\n')\n"
            "sys.stdout.write('nachher-1\\n')\n"
            "sys.stdout.write('nachher-2')\n"
            "sys.stdout.flush()\n")
        lines = []
        outcome = _run(invocation, on_stdout_line=lines.append, retain_stdout=False)
        require_equal(lines, ["vorher", "nachher-1", "nachher-2"],
                      "die Ueberlaenge wurde zugestellt oder Folgezeilen verloren")
        require(outcome.truncated is True, "die verworfene Ueberlaenge wurde nicht gemeldet")
        require_equal(outcome.text, "")
        require_equal(outcome.exit_code, 0)


def t_default_path_still_retains_redacts_and_truncates_like_today():
    """Der Codex-Pfad: Vorgabe `retain_stdout=True`. 600 KB → abgeschnitten,
    Text behalten und gedeckelt; eine Schluesselgestalt darin ist redigiert."""
    with tempfile.TemporaryDirectory() as folder:
        invocation = _script(Path(folder),
            "import sys\n"
            "sys.stdout.write('sk-testtesttesttesttest0000\\n')\n"
            "sys.stdout.write('x' * 600000)\n"
            "sys.stdout.flush()\n")
        lines = []
        outcome = _run(invocation, on_stdout_line=lines.append)
        require(outcome.truncated is True, "Ueberlauf ohne truncated")
        require(len(outcome.text) <= L.MAX_OUTPUT, "Text ueber MAX_OUTPUT")
        require(outcome.text.startswith(L.MASK), "behaltener Text nicht redigiert")
        require_equal(lines[0], "sk-testtesttesttesttest0000",
                      "die zugestellte Zeile ist ROH — Redaktion ist Konsumentenpflicht (§2.4)")
        # Ohne Beobachter: unveraendert `communicate`.
        plain = _run(invocation)
        require(plain.truncated is True and len(plain.text) <= L.MAX_OUTPUT)


# =====================================================================
# brokered_environment(config_dir=...) und run(environment=...)
# =====================================================================

def t_config_dir_moves_the_cli_state_into_the_jail_and_the_v06_form_is_unchanged():
    base = "http://127.0.0.1:8792"
    plain = L.brokered_environment(base_url=base, token="sk-solvio-broker-" + "0" * 48, tmpdir="/tmp/j/tmp")
    for name in ("CLAUDE_CONFIG_DIR", "DISABLE_TELEMETRY", "DISABLE_ERROR_REPORTING"):
        require(name not in plain, f"V0.6-Umgebung traegt ploetzlich {name}")
    jailed = L.brokered_environment(base_url=base, token="sk-solvio-broker-" + "0" * 48,
                                    tmpdir="/tmp/j/tmp", config_dir="/tmp/j/config")
    require_equal(jailed["CLAUDE_CONFIG_DIR"], "/tmp/j/config")
    require_equal((jailed["DISABLE_TELEMETRY"], jailed["DISABLE_ERROR_REPORTING"]), ("1", "1"))
    require_equal((jailed["TMPDIR"], jailed["CLAUDE_CODE_TMPDIR"]), ("/tmp/j/tmp", "/tmp/j/tmp"))
    require_equal(jailed["ANTHROPIC_BASE_URL"], base)
    require_equal(jailed["CLAUDE_CODE_DISABLE_FAST_MODE"], "1")
    # Sperrliste bleibt Mechanismus: nur die zwei Broker-Werte kommen aus dem Aufruf.
    leaking = [n for n in jailed if n in L.DENIED_ENV and n not in ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY")]
    require_equal(leaking, [])
    try:
        L.brokered_environment(base_url=base, token="t", config_dir="relative/config")
    except L.LauncherError as exc:
        require_equal(exc.reason, "brokered_env_config_dir_relative")
    else:
        raise AssertionError("ein relatives CLAUDE_CONFIG_DIR wurde angenommen")


def t_run_accepts_only_a_brokered_environment_and_hands_it_to_the_child():
    with tempfile.TemporaryDirectory() as folder:
        invocation = _script(Path(folder),
            "import os, json\n"
            "print(json.dumps({k: os.environ.get(k) for k in ('CLAUDE_CONFIG_DIR', 'ANTHROPIC_BASE_URL', "
            "'OPENAI_API_KEY', 'CLAUDE_CODE_DISABLE_FAST_MODE')}))\n")
        env = L.brokered_environment(base_url="http://127.0.0.1:4242", token="sk-solvio-broker-" + "1" * 48,
                                     tmpdir=folder, config_dir=folder)
        lines = []
        outcome = _run(invocation, on_stdout_line=lines.append, retain_stdout=False, environment=env)
        require_equal(outcome.exit_code, 0, outcome.stderr_note)
        import json
        seen = json.loads(lines[0])
        require_equal(seen["CLAUDE_CONFIG_DIR"], folder)
        require_equal(seen["ANTHROPIC_BASE_URL"], "http://127.0.0.1:4242")
        require_equal(seen["OPENAI_API_KEY"], None)
        require_equal(seen["CLAUDE_CODE_DISABLE_FAST_MODE"], "1")

        # Ein Sperrlisten-Name ausser den zwei Broker-Werten: Leck, kein Start.
        tainted = dict(env, OPENAI_API_KEY="sk-test-not-a-real-key-0000000000")
        refused = _run(invocation, environment=tainted)
        require(refused.process_started is False and refused.reason == "environment_leak",
                f"eine verunreinigte Umgebung startete: {refused}")
        # Eine Basis ausserhalb der Rueckschleife: kein Start.
        foreign = dict(env, ANTHROPIC_BASE_URL="https://api.anthropic.com")
        refused = _run(invocation, environment=foreign)
        require(refused.process_started is False and refused.reason == "brokered_env_not_loopback")
        # Ohne Token oder ohne Fast-Mode-Sperre: kein Start.
        for broken in (dict(env, ANTHROPIC_API_KEY=""), {k: v for k, v in env.items()
                                                           if k != "CLAUDE_CODE_DISABLE_FAST_MODE"}):
            refused = _run(invocation, environment=broken)
            require(refused.process_started is False and refused.reason == "brokered_env_incomplete")


def t_the_signature_keeps_the_command_fixed_and_adds_only_keyword_seams():
    """Kein Weg zu einem Kommando aus Text: die neuen Nahte sind keyword-only
    und haben Vorgaben, die das heutige Verhalten bedeuten."""
    import inspect
    signature = inspect.signature(L.run)
    params = signature.parameters
    require_equal(list(params)[:2], ["invocation", "prompt"])
    for name, default in (("on_stdout_line", None), ("retain_stdout", True), ("environment", None)):
        require(name in params, f"{name} fehlt")
        require_equal(params[name].kind, inspect.Parameter.KEYWORD_ONLY, f"{name} ist positional")
        require_equal(params[name].default, default, f"{name}: Vorgabe geaendert")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
