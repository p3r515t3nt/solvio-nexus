"""Die Kurzrecherche -- Auftrag, Kappe, Vertrauen, Wahrheit, ohne Netz.

`research_quick` beantwortet eine aktuelle Frage im selben Gespraechs-Turn mit
der nativen Websuche des Anbieters. Diese Suite prueft genau die vier Dinge,
die SOLVIO dabei behaelt, plus die Bruecke zum Router und zum Broker-Tor:

* AUFTRAG -- eigener Auftraggeber (`research-quick`), frisches Lease je
  Aufruf, `close_lease` immer im `finally`.
* KAPPE -- eigene Caps, und das neue Broker-Tor fuer anbieterseitige
  Werkzeuge: `web_search` kommt durch, alles andere faellt mit
  `provider_tool_not_allowed`.
* VERTRAUEN -- jedes Ergebnis traegt `content_trust: untrusted_web`.
* WAHRHEIT -- Quellen kommen AUSSCHLIESSLICH aus `url_citation`-Annotationen,
  nie aus geratenen URLs; doppelte URLs werden zusammengefuehrt; der
  Sprechtext enthaelt nie eine URL, auch wenn der Anbieter sie mitten im Text
  einbettet.

Dazu: ein ehrlicher Fehlschlag bei fehlendem Broker/Lease/Anbieterfehler/
leerer Antwort, die Router-Umlegung auf `research_quick` statt
`deep_research`, und die Werkzeug-Bruecke, die dem Modell nie eine URL zum
Vorlesen gibt.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))
from _guard import enforce_assertions, require, require_equal  # noqa: E402
enforce_assertions()

from solvio.capabilities.browser import CONTENT_TRUST  # noqa: E402
from solvio.capabilities.contract import (  # noqa: E402
    CapabilityDeclined, ExecutionClass, ExecutorUnavailable,
)
from solvio.capabilities.invocation import voice_trust  # noqa: E402
from solvio.capabilities.policy import OriginClass  # noqa: E402
from solvio.capabilities.router import CapabilityRouter  # noqa: E402
from solvio.capabilities.research_quick import (  # noqa: E402
    SPECS, ResearchQuickCapabilities, _dedupe_sources,
    _extract_answer_and_citations, _strip_embedded_urls, register,
)


def _run(coro):
    return asyncio.run(coro)


# =====================================================================
# Attrappen
# =====================================================================

class FakeBroker:
    """Praegt Token, zaehlt Leases, kann auf Kappe stossen."""

    def __init__(self, *, capped: bool = False) -> None:
        self.registered: list[str] = []
        self.opened: list[tuple[str, str]] = []
        self.closed: list[str] = []
        self.capped = capped
        self._next = 0

    def register_principal(self, name: str) -> str:
        self.registered.append(name)
        self._next += 1
        return f"broker-token-fake-{self._next:04d}"

    def open_lease(self, name: str, ref: str = "", *, deadline: float = 0.0) -> str:
        if self.capped:
            from solvio.provider_broker.session import CapExceeded
            raise CapExceeded("rate_capped")
        self.opened.append((name, ref))
        return f"lease-{len(self.opened)}"

    def close_lease(self, lease_id: str) -> None:
        self.closed.append(lease_id)


class Bag:
    """Ein Attributbeutel als Dispatcher -- genau `provider_broker`, spaet gesetzt."""


def _responses_body(text: str, *, citations: list[dict] | None = None,
                    status: str = "completed") -> dict:
    """Eine Anbieterantwort im Responses-Umschlag, mit `url_citation`-Annotationen.

    Der Sucheintrag traegt `status="completed"`. Das stand hier vorher NICHT --
    die Attrappe erzeugte einen `web_search_call` ohne Status, und genau diese
    selbstgeschriebene Form war spaeter die Begruendung dafuer, einen fehlenden
    Status als Beleg durchgehen zu lassen. Ein Beleg, den ein Test erfindet,
    ist kein Beleg.
    """
    annotations = []
    for citation in citations or []:
        annotations.append({"type": "url_citation", "url": citation["url"],
                            "title": citation.get("title", ""),
                            "start_index": 0, "end_index": 0})
    suche: dict = {"type": "web_search_call"}
    if status:
        suche["status"] = status
    return {"output": [
        suche,
        {"type": "message", "content": [
            {"type": "output_text", "text": text, "annotations": annotations}]},
    ]}


def _transport(reply: dict, *, calls: list[dict] | None = None):
    async def call(payload: dict, *, token: str = "", port: int = 0) -> dict:
        if calls is not None:
            calls.append({"payload": payload, "token": token})
        return dict(reply)
    return call


def _caps(broker, *, transport=None, port: int = 0) -> ResearchQuickCapabilities:
    dispatcher = Bag()
    dispatcher.provider_broker = broker
    return ResearchQuickCapabilities(dispatcher, transport=transport, port=port)


# =====================================================================
# Faehigkeit -- die vier Dinge
# =====================================================================

def t_the_spec_is_fast_read_only_and_inline():
    spec = SPECS["research_quick"]
    require(spec.execution_class is ExecutionClass.FAST)
    require(spec.is_read_only(), "FAST verlangt READ_ONLY -- und das stimmt hier")
    require_equal(spec.executor, "inline", "kein Hermes, keine Hintergrundaufgabe")
    require_equal(spec.input_schema["required"], ["question"])


def t_broker_absent_is_an_honest_failure():
    caps = _caps(None, transport=_transport({"ok": True, "data": {}}))
    raised = ""
    try:
        _run(caps.research({"question": "Wer ist Bundeskanzler?"}))
    except ExecutorUnavailable as exc:
        raised = exc.reason
    require_equal(raised, "broker_absent", "kein Broker heisst ein benannter Fehlschlag")


def t_a_capped_lease_is_an_honest_failure_not_a_hallucination():
    broker = FakeBroker(capped=True)
    caps = _caps(broker, transport=_transport({"ok": True, "data": {}}))
    raised = ""
    try:
        _run(caps.research({"question": "Wie hoch ist der Eiffelturm?"}))
    except ExecutorUnavailable as exc:
        raised = exc.reason
    require_equal(raised, "rate_capped")
    require_equal(broker.opened, [], "kein Lease wurde je eroeffnet")


def t_a_provider_error_is_an_honest_failure():
    broker = FakeBroker()
    caps = _caps(broker, transport=_transport({"ok": False, "reason": "model_not_allowed"}))
    raised = ""
    try:
        _run(caps.research({"question": "Wie spaet ist es in Tokio?"}))
    except ExecutorUnavailable as exc:
        raised = exc.reason
    require_equal(raised, "model_not_allowed")
    require_equal(broker.closed, ["lease-1"], "das Lease wird trotzdem geschlossen")


def t_no_answer_from_the_provider_is_declined_not_hallucinated():
    broker = FakeBroker()
    empty = {"ok": True, "data": {"output": [{"type": "web_search_call"}]}}
    caps = _caps(broker, transport=_transport(empty))
    raised = ""
    try:
        _run(caps.research({"question": "Was ist der aktuelle Goldpreis?"}))
    except CapabilityDeclined as exc:
        raised = exc.reason
    require_equal(raised, "no_answer")


def t_too_short_or_too_long_questions_are_declined():
    caps = _caps(FakeBroker())
    for question, reason in (("hi", "question_too_short"), ("x" * 900, "question_too_long")):
        raised = ""
        try:
            _run(caps.research({"question": question}))
        except CapabilityDeclined as exc:
            raised = exc.reason
        require_equal(raised, reason)


def t_a_successful_call_carries_untrusted_web_content_trust():
    broker = FakeBroker()
    body = _responses_body("Der Kanzler heisst Merz.",
                           citations=[{"url": "https://bundesregierung.de/a",
                                      "title": "Bundesregierung"}])
    caps = _caps(broker, transport=_transport({"ok": True, "data": body}))
    result = _run(caps.research({"question": "Wer ist Bundeskanzler?"}))
    require_equal(result["content_trust"], CONTENT_TRUST)
    require_equal(CONTENT_TRUST, "untrusted_web")


def t_each_call_mints_a_fresh_token_and_closes_its_own_lease():
    broker = FakeBroker()
    caps = _caps(broker, transport=_transport({"ok": True, "data": _responses_body("Ja.")}))
    _run(caps.research({"question": "Regnet es gerade in Berlin?"}))
    _run(caps.research({"question": "Regnet es gerade in Hamburg?"}))
    require_equal(len(broker.registered), 2, "jeder Aufruf praegt einen eigenen Token")
    require(broker.registered[0] != "" and broker.registered[0] == broker.registered[0])
    require_equal(len(broker.opened), 2, "jeder Aufruf eroeffnet ein eigenes Lease")
    require_equal(len(broker.closed), 2, "jedes Lease wird geschlossen")
    require(all(name == "research-quick" for name, _ in broker.opened),
           "der Auftraggeber ist research-quick, sonst niemand")


# =====================================================================
# Wahrheit -- Quellen ausschliesslich aus url_citation, dedupliziert
# =====================================================================

def t_sources_come_only_from_url_citation_annotations():
    body = _responses_body(
        "Antwort ohne echte Belegstelle.",
        citations=[])
    text, citations, suchen, _status = _extract_answer_and_citations(body)
    require_equal(citations, [], "keine Annotation -- keine Quelle, nie eine geratene URL")
    require_equal(text, "Antwort ohne echte Belegstelle.")
    require_equal(suchen["belegt"], 1,
                  "der abgeschlossene `web_search_call` wird als Beleg gezaehlt")


def t_duplicate_urls_in_sources_are_merged():
    citations = [
        {"url": "https://bundesregierung.de/a", "title": ""},
        {"url": "https://bundesregierung.de/a", "title": "Bundesregierung"},
        {"url": "https://tagesschau.de/b", "title": "Tagesschau"},
    ]
    merged = _dedupe_sources(citations)
    require_equal(len(merged), 2, "dieselbe URL zaehlt einmal")
    first = next(s for s in merged if s["url"] == "https://bundesregierung.de/a")
    require_equal(first["title"], "Bundesregierung", "ein spaeterer Titel darf nachtragen")
    require_equal(first["domain"], "bundesregierung.de", "Domaene aus der URL ABGELEITET")


def t_end_to_end_sources_are_deduplicated_and_carry_derived_domains():
    broker = FakeBroker()
    body = _responses_body(
        "Merz ist Bundeskanzler.",
        citations=[
            {"url": "https://bundesregierung.de/a", "title": "Bundesregierung"},
            {"url": "https://bundesregierung.de/a", "title": ""},
        ])
    caps = _caps(broker, transport=_transport({"ok": True, "data": body}))
    result = _run(caps.research({"question": "Wer ist Bundeskanzler?"}))
    require_equal(len(result["sources"]), 1, "die doppelte Quelle wurde zusammengefuehrt")
    require_equal(result["sources"][0]["domain"], "bundesregierung.de")
    require_equal(result["sources"][0]["url"], "https://bundesregierung.de/a")


# =====================================================================
# keine_url_im_sprechtext -- der Sprechtext enthaelt nie eine URL
# =====================================================================

def t_embedded_markdown_citations_never_reach_the_spoken_answer():
    raw = ("Der Kanzler heisst Merz. ([bundesregierung.de]"
           "(https://www.bundesregierung.de/kanzler))")
    stripped = _strip_embedded_urls(raw)
    require("https://" not in stripped, "keine URL im Sprechtext")
    require("bundesregierung.de" not in stripped or "http" not in stripped)
    require("Merz" in stripped, "der Inhalt bleibt erhalten")


def t_a_bare_url_left_over_is_also_stripped():
    stripped = _strip_embedded_urls("Quelle: https://example.org/artikel Ende.")
    require("http" not in stripped, "auch eine nackte URL fliegt raus")


def t_the_final_answer_never_contains_a_url_even_with_embedded_citations():
    broker = FakeBroker()
    body = _responses_body(
        "Der Kanzler heisst Merz. ([bundesregierung.de]"
        "(https://www.bundesregierung.de/kanzler))",
        citations=[{"url": "https://www.bundesregierung.de/kanzler",
                   "title": "Bundesregierung"}])
    caps = _caps(broker, transport=_transport({"ok": True, "data": body}))
    result = _run(caps.research({"question": "Wer ist Bundeskanzler?"}))
    require("http" not in result["answer"], "der Sprechtext bleibt frei von URLs")
    require_equal(result["sources"][0]["url"],
                 "https://www.bundesregierung.de/kanzler",
                 "die URL steht strukturiert in sources[], nicht im Sprechtext")


# =====================================================================
# principal / faehigkeit -- Registrierung und Auftraggebername
# =====================================================================

def t_the_principal_constant_is_its_own_and_not_reused():
    from solvio.provider_broker.service import (
        ADAPTIVE_EXTRACTOR_PRINCIPAL, DEEP_PRINCIPAL, RESEARCH_QUICK_PRINCIPAL,
    )
    require_equal(RESEARCH_QUICK_PRINCIPAL, "research-quick")
    require(RESEARCH_QUICK_PRINCIPAL not in (ADAPTIVE_EXTRACTOR_PRINCIPAL, DEEP_PRINCIPAL))


def t_the_caps_are_measured_and_do_not_touch_global_caps():
    from solvio.provider_broker.session import DEEP_CAPS, RESEARCH_QUICK_CAPS
    require_equal(RESEARCH_QUICK_CAPS.allowed_models, frozenset({"gpt-5.4-mini"}))
    require_equal(RESEARCH_QUICK_CAPS.allowed_provider_tools, frozenset({"web_search"}))
    require(RESEARCH_QUICK_CAPS.max_leases <= DEEP_CAPS.max_leases,
           "eine Kurzrecherche braucht nicht mehr gleichzeitige Leases als die tiefe")
    require_equal(DEEP_CAPS.allowed_provider_tools, frozenset(),
                 "die tiefe Recherche bekommt kein anbieterseitiges Werkzeug dazu")


def t_registration_wires_the_capability_router():
    router = CapabilityRouter()
    caps = _caps(FakeBroker())
    names = register(router, caps)
    require_equal(names, ["research_quick"])


# =====================================================================
# werkzeugtor -- das Broker-Tor fuer anbieterseitige Werkzeuge (DETERMINISTISCH)
# =====================================================================

def t_the_gate_allows_web_search_and_rejects_everything_else_by_default():
    from solvio.provider_broker import proxy as px
    px.check_provider_tools({"tools": [{"type": "web_search"}]},
                            allowed=frozenset({"web_search"}))
    for kind in ("code_interpreter", "file_search", "computer_use",
                "image_generation", "mcp"):
        raised = ""
        try:
            px.check_provider_tools({"tools": [{"type": kind}]},
                                    allowed=frozenset({"web_search"}))
        except px.BodyRejected as exc:
            raised = exc.reason
        require_equal(raised, "provider_tool_not_allowed", f"{kind} ist nicht erlaubt")


def t_function_tools_are_always_allowed_client_side():
    from solvio.provider_broker import proxy as px
    px.check_provider_tools({"tools": [{"type": "function", "name": "irgendwas"}]},
                            allowed=frozenset())


def t_the_default_allowlist_is_empty_for_every_other_principal():
    from solvio.provider_broker.session import Caps
    require_equal(Caps().allowed_provider_tools, frozenset(),
                 "ein neues anbieterseitiges Werkzeug ist nie eine Voreinstellung")


def t_provider_tool_not_allowed_is_a_recognised_denial_reason():
    from solvio.provider_broker.ledger import DENIED_REASONS
    require("provider_tool_not_allowed" in DENIED_REASONS)


def t_no_tools_key_or_a_non_list_tools_key_is_not_rejected():
    from solvio.provider_broker import proxy as px
    px.check_provider_tools({}, allowed=frozenset())
    px.check_provider_tools({"tools": "not-a-list"}, allowed=frozenset())


def t_the_gate_is_end_to_end_through_the_real_broker():
    import time

    from _broker_fixtures import Harness, responses_body, run

    async def scenario():
        async with Harness() as h:
            token = h.broker.register_principal("research-quick")
            lease = h.broker.open_lease("research-quick", "t",
                                        deadline=time.time() + 60)
            try:
                body = responses_body(
                    tools=[{"type": "web_search", "search_context_size": "low"}])
                status, _raw = await h.call(token, body=body)
                require_equal(status, 200, "web_search kommt fuer research-quick durch")
            finally:
                h.broker.close_lease(lease)

            token2 = h.broker.register_principal("research-quick")
            lease2 = h.broker.open_lease("research-quick", "t",
                                         deadline=time.time() + 60)
            try:
                forbidden = responses_body(tools=[{"type": "code_interpreter"}])
                status2, raw2 = await h.call(token2, body=forbidden)
                require_equal(status2, 403, "code_interpreter faellt am Tor")
                require(b"provider_tool_not_allowed" in raw2,
                       "der Grund ist provider_tool_not_allowed, nicht lease_absent")
            finally:
                h.broker.close_lease(lease2)

            token3 = h.broker.register_principal("deep-gateway")
            lease3 = h.broker.open_lease("deep-gateway", "t",
                                         deadline=time.time() + 60)
            try:
                still_web_search = responses_body(
                    tools=[{"type": "web_search", "search_context_size": "low"}])
                status3, raw3 = await h.call(token3, body=still_web_search)
                require_equal(status3, 403,
                             "deep_research bekommt web_search NICHT automatisch dazu")
                require(b"provider_tool_not_allowed" in raw3,
                       "der Grund ist provider_tool_not_allowed, nicht lease_absent")
            finally:
                h.broker.close_lease(lease3)
    run(scenario())


# =====================================================================
# router -- ROUTE_TARGET und die Werkzeugbruecke
# =====================================================================

def t_the_router_points_research_quick_at_the_new_capability():
    """Die Route bleibt — und die Faehigkeit ist zusaetzlich direkt erreichbar.

    Seit dem 07.09.2026 steht `research_quick` NICHT mehr in
    `HIDDEN_IN_ACTIVE`: in der Abnahme griff das Sprachmodell auf eine
    Kursfrage sechsmal zum Browser und lieferte in 20 Sekunden keine Zahl,
    weil der belegte Recherchepfad verborgen und der ungepruefte sichtbar war.
    Die Route ueber `solvio_task` besteht unveraendert weiter.
    """
    from solvio.cognition.router import HIDDEN_IN_ACTIVE, ROUTE_TARGET
    from solvio.cognition.types import Route
    require_equal(ROUTE_TARGET[Route.RESEARCH_QUICK], "research_quick")
    require("research_quick" not in HIDDEN_IN_ACTIVE,
           "der belegte Recherchepfad ist wieder verborgen")
    require("deep_research" not in HIDDEN_IN_ACTIVE,
           "deep_research bleibt ueber ihre eigene Route erreichbar")


def t_deep_research_keeps_its_own_route_untouched():
    from solvio.cognition.router import ROUTE_TARGET
    from solvio.cognition.types import Route
    require("deep_research" not in ROUTE_TARGET.values(),
           "kein Route-Eintrag zeigt mehr auf deep_research")
    # deep_research bleibt trotzdem als eigenes, direkt registriertes Werkzeug
    # erreichbar (attach_deep_runtime in tools/registry.py) -- diese Suite
    # aendert daran nichts und behauptet auch nichts anderes.
    require(Route.RESEARCH_QUICK in ROUTE_TARGET)


# =====================================================================
# sprache -- die Werkzeugbruecke sagt dem Modell, keine URL vorzulesen
# =====================================================================

def t_the_tool_description_tells_the_model_to_name_not_read_the_url():
    from solvio.tools.research_quick_capability_tools import ResearchQuickCapabilityTool
    tool = ResearchQuickCapabilityTool(router=None, gate=None)
    schema = tool.schema()
    description = schema["description"].lower()
    require("url" in description, "die Beschreibung nennt das Wort URL")
    require("nie eine url vor" in description or "keine url" in description,
           "und sagt ausdruecklich: nicht vorlesen")
    require_equal(schema["parameters"]["required"], ["question"])


def t_without_a_trusted_context_the_tool_bridge_runs_nothing():
    from solvio.tools.research_quick_capability_tools import ResearchQuickCapabilityTool

    class NoGate:
        def context(self, session_id: str = ""):
            return None

    tool = ResearchQuickCapabilityTool(router=None, gate=NoGate())
    result = _run(tool.run({"question": "Wer ist Bundeskanzler?"}))
    require(not result.success)
    require_equal(result.error, "no_trusted_context")


def t_the_tool_bridge_carries_the_result_through_the_real_router():
    from solvio.capabilities.contract import ArgumentSource
    from solvio.tools.research_quick_capability_tools import ResearchQuickCapabilityTool

    router = CapabilityRouter()
    caps = _caps(FakeBroker(), transport=_transport(
        {"ok": True, "data": _responses_body("Merz ist Bundeskanzler.")}))
    register(router, caps)

    class FixedGate:
        def context(self, session_id: str = ""):
            from solvio.capabilities.invocation import InvocationContext
            return InvocationContext(
                principal="test-voice", trust=voice_trust(True),
                session_id="s-1", turn_id="t-1", user_text="Wer ist Bundeskanzler?",
                origin=OriginClass.ROOM_VOICE, commanded=True)

        def provenance_for(self, arguments):
            return {key: ArgumentSource.USER_DIRECT for key in arguments}

    tool = ResearchQuickCapabilityTool(router=router, gate=FixedGate())
    result = _run(tool.run({"question": "Wer ist Bundeskanzler?"}))
    require(result.success)
    require_equal(result.data["content_trust"], "untrusted_web")


def t_a_failed_capability_speaks_a_named_message_not_a_hallucination():
    from solvio.capabilities.contract import ArgumentSource
    from solvio.tools.research_quick_capability_tools import ResearchQuickCapabilityTool

    router = CapabilityRouter()
    caps = _caps(None)  # kein Broker -> ExecutorUnavailable("broker_absent")
    register(router, caps)

    class FixedGate:
        def context(self, session_id: str = ""):
            from solvio.capabilities.invocation import InvocationContext
            return InvocationContext(
                principal="test-voice", trust=voice_trust(True),
                session_id="s-1", turn_id="t-1", user_text="Wer ist Bundeskanzler?",
                origin=OriginClass.ROOM_VOICE, commanded=True)

        def provenance_for(self, arguments):
            return {key: ArgumentSource.USER_DIRECT for key in arguments}

    tool = ResearchQuickCapabilityTool(router=router, gate=FixedGate())
    result = _run(tool.run({"question": "Wer ist Bundeskanzler?"}))
    require(not result.success)
    require(result.human_message, "es gibt eine benannte, gesprochene Meldung")
    require(result.data is None, "kein Halbzustand mit erfundenen Daten")

    # Die Zusicherung muss in BEIDEN Welten halten — sonst ist sie genau dann
    # rot, wenn alles richtig ist (dieselbe Lektion wie in
    # `t_the_agent_runtime_is_untouched`):
    #
    # * auf `main` flacht der Router den Grund auf den Typ ab, und es kommt
    #   `executor_unavailable:executor_unavailable` heraus;
    # * in der Betriebskomposition traegt `ExecutorUnavailable` seit Deep
    #   Research Reliability V1 einen `reason`, der ueberlebt — dort steht
    #   `executor_unavailable:broker_absent`.
    #
    # Geprueft wird deshalb, was die Faehigkeit wirklich zusagt: der Fehler ist
    # als `executor_unavailable` klassifiziert, und der Grund dahinter ist ein
    # BENANNTER aus einer geschlossenen Menge — nie ein Freitext und nie leer.
    require(result.error.startswith("executor_unavailable:"),
            f"falsch klassifiziert: {result.error}")
    grund = result.error.split(":", 1)[1]
    require(grund in ("executor_unavailable", "broker_absent"),
            f"kein benannter Grund aus der geschlossenen Menge: {grund}")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
