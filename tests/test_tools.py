"""STEP 17 Tool-Layer Tests. Laeuft mit pytest ODER direkt (python test_tools.py).
Nutzt einen gemockten Home Assistant, keine echten Geraete."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))
from _guard import enforce_assertions  # noqa: E402
enforce_assertions()

from solvio.config import Settings
from solvio.tools.base import RiskLevel, ToolResult
from solvio.tools.dispatcher import ToolDispatcher
from solvio.tools.ha_tools import (
    GetState, HAContext, ListEntities, SetBrightness, TurnOff, TurnOn, ha_tools,
)


def run(coro):
    return asyncio.run(coro)


class FakeHA:
    def __init__(self, states):
        self._states = states
        self.calls = []

    async def states(self):
        return self._states

    async def call_service(self, domain, service, data=None):
        self.calls.append((domain, service, data))
        return []


STATES = [
    {"entity_id": "light.wohnzimmer", "state": "off",
     "attributes": {"friendly_name": "Wohnzimmerlampe", "brightness": 0}},
    {"entity_id": "light.kueche", "state": "on",
     "attributes": {"friendly_name": "Kueche Decke"}},
    {"entity_id": "switch.kaffee", "state": "off",
     "attributes": {"friendly_name": "Kaffeemaschine"}},
    {"entity_id": "light.wohnzimmer_2", "state": "off",
     "attributes": {"friendly_name": "Wohnzimmer Stehlampe"}},
]


def ctx():
    return HAContext(FakeHA(STATES))


def test_schemas_wellformed():
    for t in ha_tools(ctx()):
        sch = t.schema()
        assert sch["type"] == "function"
        assert sch["name"].startswith("home_assistant_")
        assert "parameters" in sch and sch["parameters"]["type"] == "object"


def test_risk_levels():
    for t in ha_tools(ctx()):
        assert t.risk_level == RiskLevel.HARMLESS


def test_unknown_tool_rejected():
    r = run(ToolDispatcher().dispatch("nope", {}))
    assert r["success"] is False and "unknown_tool" in r["error"]


def test_invalid_arguments_type():
    d = ToolDispatcher(); [d.register(t) for t in ha_tools(ctx())]
    r = run(d.dispatch("home_assistant_get_state", ["not", "a", "dict"]))
    assert r["success"] is False


def test_entity_lookup_exact():
    m = run(ctx().resolve("Kaffeemaschine"))
    assert len(m) == 1 and m[0]["entity_id"] == "switch.kaffee"


def test_entity_ambiguous():
    r = run(TurnOn(ctx()).run({"name": "Wohnzimmer"}))
    assert r.success is False and "options" in (r.data or {})


def test_entity_not_found():
    r = run(TurnOn(ctx()).run({"name": "Gibtsnicht"}))
    assert r.success is False


def test_get_state():
    r = run(GetState(ctx()).run({"name": "Kueche Decke"}))
    assert r.success and r.data["state"] == "on"


def test_turn_on_calls_service():
    c = ctx(); r = run(TurnOn(c).run({"name": "Kaffeemaschine"}))
    assert r.success
    assert c.ha.calls == [("homeassistant", "turn_on", {"entity_id": "switch.kaffee"})]


def test_turn_off_calls_service():
    c = ctx(); r = run(TurnOff(c).run({"name": "Kaffeemaschine"}))
    assert r.success and c.ha.calls[0][1] == "turn_off"


def test_set_brightness_invalid():
    r = run(SetBrightness(ctx()).run({"name": "Wohnzimmerlampe", "brightness_pct": "abc"}))
    assert r.success is False and r.error == "invalid_brightness"


def test_set_brightness_clamped():
    c = ctx(); r = run(SetBrightness(c).run({"name": "Wohnzimmerlampe", "brightness_pct": 150}))
    assert r.success
    assert c.ha.calls[0] == ("light", "turn_on",
                             {"entity_id": "light.wohnzimmer", "brightness_pct": 100})


def test_parse_args():
    d = ToolDispatcher()
    assert d.parse_args('{"a": 1}') == {"a": 1}
    assert d.parse_args("kaputt") == {}
    assert d.parse_args({"b": 2}) == {"b": 2}
    assert d.parse_args(None) == {}


def test_token_masking():
    s = Settings()
    assert "maskiert" in s.masked("supersecret") and "supersecret" not in s.masked("supersecret")
    assert s.masked("") == "leer"


def test_toolresult_as_dict():
    d = ToolResult(True, data={"x": 1}, human_message="ok").as_dict()
    assert d["success"] is True and d["data"]["x"] == 1 and "error" not in d


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
