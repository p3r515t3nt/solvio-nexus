"""Lokale Tests, die ohne Netzwerk auskommen."""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))
from _guard import enforce_assertions  # noqa: E402
enforce_assertions()

import inspect

from solvio import __version__
from solvio.config import DEFAULT_REALTIME_MODEL, Settings
from solvio.health import collect_health
from solvio.realtime import RealtimeClient, RealtimeError, Timings


def test_version() -> None:
    assert __version__


def test_settings_defaults() -> None:
    s = Settings(_env_file=None)
    assert s.solvio_host == "127.0.0.1"
    assert s.solvio_port == 8765
    assert s.has_openai_key is False
    assert s.has_realtime is False
    assert s.openai_realtime_model == DEFAULT_REALTIME_MODEL


def test_secret_masking() -> None:
    s = Settings(_env_file=None, openai_api_key="sk-geheim-nicht-anzeigen")
    masked = s.masked(s.openai_api_key)
    assert "sk-" not in masked
    assert "geheim" not in masked
    assert "maskiert" in masked
    assert s.masked("") == "leer"


def test_health_without_key() -> None:
    report = collect_health(Settings(_env_file=None))
    rendered = report.render()
    assert report.status == "READY"
    assert "OpenAI Key: NOT CONFIGURED" in rendered
    assert "Realtime: NOT CONFIGURED" in rendered


def test_health_with_key() -> None:
    s = Settings(_env_file=None, openai_api_key="sk-test")
    rendered = collect_health(s).render()
    assert "OpenAI Key: CONFIGURED" in rendered
    assert "Realtime: CONFIGURED" in rendered
    assert "sk-test" not in rendered


def test_realtime_client_requires_key() -> None:
    try:
        RealtimeClient("", DEFAULT_REALTIME_MODEL)
    except RealtimeError:
        pass
    else:
        raise AssertionError("Leerer Schluessel haette abgelehnt werden muessen")


def test_realtime_url_contains_model() -> None:
    c = RealtimeClient("sk-test", DEFAULT_REALTIME_MODEL)
    assert c.url.startswith("wss://api.openai.com/v1/realtime?model=")
    assert DEFAULT_REALTIME_MODEL in c.url


def test_client_never_exposes_key_in_repr() -> None:
    c = RealtimeClient("sk-streng-geheim", DEFAULT_REALTIME_MODEL)
    assert "sk-streng-geheim" not in repr(c)
    assert "sk-streng-geheim" not in str(vars(c).get("model", ""))


def test_timings_math() -> None:
    t = Timings(t0_connect_start=1.0, t1_session_ready=1.25, t2_request_sent=2.0,
                t3_first_chunk=2.4, t4_complete=2.9)
    assert t.connection_ms == 250
    assert t.time_to_first_response_ms == 400
    assert t.total_response_ms == 900
    assert Timings().connection_ms is None


def test_error_event_parsing() -> None:
    described = RealtimeClient._describe_error(
        {"type": "error", "error": {"code": "invalid_request", "message": "kaputt"}}
    )
    assert "invalid_request" in described
    assert "kaputt" in described


def test_extract_text_fallback() -> None:
    text = RealtimeClient._extract_text(
        {"output": [{"content": [{"type": "output_text", "text": "SOLVIO ONLINE"}]}]}
    )
    assert text == "SOLVIO ONLINE"


def test_client_uses_official_endpoint() -> None:
    source = inspect.getsource(RealtimeClient)
    assert "Authorization" in source
    assert "OpenAI-Beta" not in source


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
