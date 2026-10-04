"""Offline-Nachweis am installierten Hermes, ohne ihn zu veraendern.

Aufruf mit einem Interpreter, der Core- UND Hermes-Abhaengigkeiten kennt:
  python scripts/verify_hermes_context.py --hermes-source /pfad/hermes/src

Echt: provisionierte Konfiguration, AIAgent.run_conversation, automatische
Vorlaufverdichtung, Abschnittszusammenfassungen und Auxiliary-Provideraufloesung.
Gestellt: grosse Suchantworten, zwei zuletzt notierte Befunde und Modellantworten.
Jede Netzverbindung ist gesperrt. Alle Hermes-Dateien liegen temporaer.

Das beweist Kontextfuehrung, keine Recherchequalitaet und keine echten Kosten.
Die letzten Befunde muessen wie in SYSTEM_INSTRUCTION angefordert notiert sein:
Hermes' Druckkuerzung ersetzt rohe Tail-Werkzeugantworten vor der Verdichtung.
Lean bewahrt Rohtexte des aelteren Zusammenfassungsbereichs fuer Digests, ist
aber kein verlustfreies Quellenarchiv. Der Live-Ergebnisnachweis bleibt noetig.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import socket
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from solvio.deep.executor import ExecutorConfig, provision
from solvio.deep.runtime import SYSTEM_INSTRUCTION


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def response(text):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content=text, reasoning_content=None, reasoning=None, tool_calls=None),
        finish_reason="stop")], model="gpt-5.4-mini", usage=None)


def verify(source: Path) -> dict:
    def no_network(*args, **kwargs):
        raise RuntimeError("network forbidden in offline Hermes proof")

    with tempfile.TemporaryDirectory(prefix="solvio-hermes-context-") as folder:
        root = Path(folder)
        cfg = ExecutorConfig(jail=folder, venv_bin=folder, python_root=folder)
        provision(cfg, api_key="synthetic-gateway", broker_token="synthetic-broker")
        import yaml
        written = yaml.safe_load(Path(cfg.home, "config.yaml").read_text())
        with patch.dict(os.environ, {"HERMES_HOME": cfg.home}), \
             patch.object(socket.socket, "connect", no_network), \
             patch.object(socket, "create_connection", no_network):
            sys.path.insert(0, str(source.resolve()))
            from run_agent import AIAgent
            from hermes_state import SessionDB
            from agent import auxiliary_client as aux
            history = [{"role": "user", "content": "Vergleiche drei Hotels mit Quellen."}]
            expected = {}
            for i in range(12):
                url = f"https://hotel-{i}.example/source"
                fact = f"Hotel {i}: {101+i} EUR, availability unverified"
                expected[url] = fact
                history.extend([
                    {"role": "assistant", "content": None, "tool_calls": [{
                        "id": f"call_{i}", "type": "function", "function": {
                            "name": "web_search", "arguments": json.dumps({"query": f"Hotel {i}"})}}]},
                    {"role": "tool", "tool_call_id": f"call_{i}", "content": json.dumps({
                        "url": url, "fact": fact, "text": "irrelevant search material " * 2500})}])
            # Bereits gewonnene aktuelle Befunde; kein fertiges Vergleichsergebnis.
            history.append({"role": "assistant", "content": "Letzte Befunde: " + "; ".join(
                f"{expected[url]} {url}" for url in list(expected)[-2:])})

            def run_case(name, config, *, fail_summary=False):
                with patch("hermes_cli.config.load_config", return_value=config), \
                     patch("hermes_cli.config.load_config_readonly", return_value=config), \
                     patch("run_agent.get_tool_definitions", return_value=[]), \
                     patch("run_agent.check_toolset_requirements", return_value={}), \
                     patch("run_agent.OpenAI"):
                    agent = AIAgent(
                        base_url="http://127.0.0.1:1/v1", api_key="synthetic-broker",
                        model="gpt-5.4-mini", provider="openai-api", api_mode="chat_completions",
                        enabled_toolsets=[], disabled_toolsets=[], quiet_mode=True,
                        skip_memory=True, skip_context_files=True,
                        session_db=SessionDB(db_path=root / f"{name}.db"), session_id=name)
                    agent.client = MagicMock()
                    agent._cached_system_prompt = SYSTEM_INSTRUCTION
                    agent._use_prompt_caching = False
                    agent._disable_streaming = True
                    agent.tool_delay = 0
                    agent.save_trajectories = False
                    agent.client.chat.completions.create.return_value = response(
                        "Die gesammelten Belege stehen zur Bewertung bereit.")
                    summary_sizes = []
                    def summarize(client, kwargs, task=None, **options):
                        require(str(client.base_url) == "http://127.0.0.1:1/v1/", "Auxiliary umgeht Basisadresse")
                        require(client.api_key == "synthetic-broker", "Auxiliary wechselt Zugang")
                        require(kwargs.get("model") == "gpt-5.4-mini", "Auxiliary wechselt Modell")
                        prompt = kwargs["messages"][0]["content"]
                        summary_sizes.append(len(prompt))
                        if fail_summary:
                            return response("")
                        # Nur Befunde, die der native Digest wirklich erhalten hat.
                        # Kein Zugriff auf expected, kein fertig geliefertes Ergebnis.
                        pairs = re.findall(r'"url": "(https://hotel-\d+\.example/source)", "fact": "([^"]+)"', prompt)
                        lines = [f"{fact} {url}" for url, fact in pairs]
                        return response("## Active Task\nVergleiche Hotels mit Quellen.\n## Completed Actions\n" + "\n".join(lines))
                    with patch.object(aux, "_create_with_progress", side_effect=summarize):
                        result = agent.run_conversation(
                            "Vergleiche die gesammelten Belege; erhalte Preise, Quellen und Einschraenkungen.",
                            conversation_history=copy.deepcopy(history))
                    calls = agent.client.chat.completions.create.call_args_list
                    payload = json.dumps(calls[-1].kwargs, default=str) if calls else ""
                    return {"completed": result.get("completed"), "summary_calls": len(summary_sizes),
                            "main_calls": len(calls), "main_chars": len(payload),
                            "auxiliary_chars": sum(summary_sizes),
                            "preserved": sum(url in payload and fact in payload for url, fact in expected.items()),
                            "threshold": agent.context_compressor.threshold_tokens}

            candidate = run_case("candidate", written)
            require(candidate["threshold"] == 24000, "provisionierte Fruehschwelle fehlt")
            require(candidate["summary_calls"] > 1, "keine nativen Abschnittsdigests")
            require(candidate["preserved"] == len(expected), f"Befundverlust: {candidate}")
            require(candidate["main_calls"] == 1 and candidate["completed"], str(candidate))
            require(candidate["main_chars"] < len(json.dumps(history)) // 10, "keine wirksame Verdichtung")
            require(candidate["main_calls"] + candidate["summary_calls"] < 40, "Testablauf braucht zu viele Aufrufe")
            # Nur eine Groessenschaetzung; keine gemessenen Anbieter-Token.
            require((candidate["main_chars"] + candidate["auxiliary_chars"]) // 4 < 650000,
                    "schon die Eingabeschaetzung sprengt das bestehende Aufgabenbudget")
            legacy_config = copy.deepcopy(written)
            legacy_config["compression"]["tail_mode"] = "legacy"
            legacy = run_case("legacy", legacy_config)
            require(legacy["preserved"] < len(expected), "Gegenprobe trifft Quellenverlust nicht")
            failed = run_case("summary_failure", written, fail_summary=True)
            # abort_on_summary_failure stoppt die VERDICHTUNG. Hermes darf
            # mit Originalkontext weiterfragen; das bestehende Broker-Lease
            # entscheidet dann weiterhin ueber das verbleibende Aufgabenbudget.
            require(failed["preserved"] == len(expected) and
                    failed["main_chars"] >= len(json.dumps(history)),
                    f"leere Zusammenfassung ersetzte die Originalbelege: {failed}")
            return {"candidate": candidate, "legacy_counterexample": legacy,
                    "summary_failure": failed, "network": "forbidden", "source": str(source.resolve())}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-source", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = verify(args.hermes_source)
    body = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        args.output.write_text(body + "\n")
    print(body)
