"""Vertrauenswuerdiger Endpunkt ist nicht dasselbe wie belegter Sprecher.

Diese Datei prueft den Unterschied zwischen Transportauthentisierung und
Sprecheridentitaet. Ein Raummikrofon kann Aussagen aus Medien uebertragen;
diese werden mit synthetischen Beispielen auf dem echten Produktweg geprueft.

Automatisches Lernen ist auf die ausdruecklich zugelassenen interaktiven
Endpunkte beschraenkt: attestiertes iPhone und angemeldeter Owner-Browser.
Der Accountbeleg ist keine biometrische Sprecheridentifikation. Ein
Raummikrofon gehoert nicht dazu — und
erzeugt auch keine Vorschlaege, denn ein Postfach voller Saetze aus dem
Fernsehprogramm waere keine Vorsicht, sondern eine Verlagerung des Problems.

ASSERTION POLICY: `require*` aus `tests/_guard.py` sind Funktionsaufrufe und
ueberleben `python -O`.
"""
from __future__ import annotations

import ast
import inspect
import os
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from _guard import enforce_assertions, require, require_equal, require_raises  # noqa: E402

from solvio.memory.adaptive import candidates as C  # noqa: E402
from solvio.memory.adaptive import policy as P  # noqa: E402
from solvio.memory.adaptive.candidates import CandidateStore  # noqa: E402
from solvio.memory.adaptive.extractor import parse  # noqa: E402
from solvio.memory.adaptive.pipeline import AdaptiveMemory  # noqa: E402
from solvio.memory.service import MemoryService  # noqa: E402

enforce_assertions()

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
SRC = os.path.join(os.path.dirname(__file__), "..", "src", "solvio")

#: Ein und derselbe Satz. Nur die Herkunft unterscheidet die Faelle.
SATZ = "Ich moechte am liebsten eine klare Empfehlung statt fuenf Alternativen."


class Fixture:
    """Ein Extraktor, der IMMER etwas Adoptierbares vorschlaegt.

    Damit misst diese Datei ausschliesslich die Herkunft: was hier nicht
    gelernt wird, wurde von der Quellenpolitik verhindert und nicht davon,
    dass der Vorschlag schwach war.
    """

    name = "fixture"

    def __init__(self, **overrides) -> None:
        self.overrides = overrides

    async def propose(self, turn, *, context: str = ""):
        base = dict(statement="Bevorzugt eine klare Empfehlung.", kind="stated",
                    memory_type="preference", subject="pref:empfehlung",
                    about="self", sensitivity="personal", flags=[])
        base.update(self.overrides)
        return parse({"proposals": [base]})


async def _build(**overrides):
    folder = tempfile.mkdtemp(prefix="solvio-source-")
    service = MemoryService(base_dir=folder).open()
    adaptive = AdaptiveMemory(service, CandidateStore(folder),
                              extractor=Fixture(**overrides))
    return adaptive, service


async def _teardown(adaptive, service) -> None:
    await adaptive.close()
    await service.close()


def _turn(channel: str, text: str = SATZ, **kw) -> P.OwnerTurn:
    base = dict(channel=channel, role="user", conversation_id="c1",
                session_id="s1", turn_id="t1", message_id="m1")
    base.update(kw)
    return P.OwnerTurn(text=text, **base)


async def _state(adaptive, service):
    records = await service.semantic.memory.active_records(NOW)
    stats = await adaptive.candidates.stats()
    return len(records), sum(stats[s] for s in C.STATES)


# =====================================================================
# A — dieselbe Aussage vom iPhone
# =====================================================================

async def t_a_the_same_sentence_from_the_iphone_is_learned() -> None:
    """Der Referenzfall. Ohne ihn misst diese Datei nur Ablehnung."""
    adaptive, service = await _build()
    try:
        result = await adaptive.process(_turn("voice_iphone"), now=NOW)
        require_equal(result.adopted, 1, str(result.as_dict()))
        records, _ = await _state(adaptive, service)
        require_equal(records, 1, "vom Telefon wurde nichts gelernt")
    finally:
        await _teardown(adaptive, service)


# =====================================================================
# B — dieselbe Aussage vom Pi
# =====================================================================

async def t_b_the_same_sentence_from_the_pi_is_not_learned() -> None:
    """Wortgleich, anderer Ausgang. Die Herkunft entscheidet, nichts sonst."""
    adaptive, service = await _build()
    try:
        result = await adaptive.process(_turn("voice_satellite"), now=NOW)
        require_equal(result.adopted, 0, "aus dem Raummikrofon wurde gelernt")
        require("speaker_unverified" in result.reasons, str(result.reasons))
        records, candidates = await _state(adaptive, service)
        require_equal(records, 0, "es entstand ein Gedaechtniseintrag")
        require_equal(candidates, 0,
                      "es entstand ein Vorschlag — das Postfach fuellt sich mit "
                      "allem, was im Zimmer gesagt wird")
    finally:
        await _teardown(adaptive, service)


async def t_b2_the_pi_is_refused_before_the_extractor_runs() -> None:
    """Kein Anbieteraufruf fuer Raumklang. Sonst waere jeder Satz im Zimmer
    ein Auftrag an ein fremdes Modell — und bezahlt obendrein."""
    adaptive, service = await _build()
    try:
        accepted = adaptive.observe_turn(_turn("voice_satellite"))
        require(not accepted, "der Turn wurde angenommen")

        calls = {"n": 0}

        class Counting:
            name = "counting"

            async def propose(self, turn, *, context=""):
                calls["n"] += 1
                return parse({"proposals": []})

        adaptive.extractor = Counting()
        await adaptive.process(_turn("voice_satellite"), now=NOW)
        require_equal(calls["n"], 0, "der Extraktor lief fuer einen Raumturn")
        await adaptive.process(_turn("voice_iphone"), now=NOW)
        require_equal(calls["n"], 1, "der Extraktor lief fuer das Telefon nicht")
    finally:
        await _teardown(adaptive, service)


# =====================================================================
# C — das Modell behauptet, es sei der Besitzer
# =====================================================================

async def t_c_a_model_claiming_owner_changes_nothing_for_the_pi() -> None:
    """Ein Modell darf uns aufhalten, nie durchwinken — auch hier nicht.

    Der Vorschlag behauptet `about="self"`, keine Flags, harmlose Einstufung.
    Die Herkunft entscheidet trotzdem, und sie faellt im Core.
    """
    adaptive, service = await _build(about="self", flags=[],
                                     sensitivity="public")
    try:
        result = await adaptive.process(_turn("voice_satellite"), now=NOW)
        require_equal(result.adopted, 0, "die Modellbehauptung hat gewirkt")
        require("speaker_unverified" in result.reasons, str(result.reasons))
        records, candidates = await _state(adaptive, service)
        require_equal(records + candidates, 0, "es blieb etwas zurueck")
    finally:
        await _teardown(adaptive, service)


# =====================================================================
# D — der Fernseher im Raum
# =====================================================================

async def t_d_a_television_utterance_through_the_pi_cannot_become_memory() -> None:
    """Eine synthetische Fernsehaussage darf keinen Eintrag oder Vorschlag erzeugen."""
    adaptive, service = await _build(
        statement="Bittet, Videos an Niko Beispieler zu senden.",
        memory_type="user", subject="user:videos")
    try:
        result = await adaptive.process(_turn(
            "voice_satellite",
            "Schickt mir eure Videos, wenn ihr mich und Niko Beispieler sehen "
            "wollt, dann machen wir daraus eine Sendung."), now=NOW)
        require_equal(result.adopted, 0, "die Fernsehstimme wurde gelernt")
        records, candidates = await _state(adaptive, service)
        require_equal(records + candidates, 0, "es blieb etwas zurueck")
    finally:
        await _teardown(adaptive, service)


# =====================================================================
# E — fremde Inhalte, unveraendert
# =====================================================================

async def t_e_web_mail_and_tool_content_remain_structurally_impossible() -> None:
    adaptive, service = await _build()
    try:
        for channel in ("web_page", "gmail", "hermes", "codex", "browser",
                        "home_assistant", "doctor", "portal", "owner_chat"):
            accepted = adaptive.observe_turn(_turn(channel))
            require(not accepted, f"{channel} kam durch")
            result = await adaptive.process(_turn(channel), now=NOW)
            require_equal(result.adopted, 0, channel)
            require("channel_not_eligible" in result.reasons,
                    f"{channel}: {result.reasons}")
        require(not adaptive.observe_turn(_turn("voice_iphone", role="assistant")),
                "Assistententext kam durch")
        records, candidates = await _state(adaptive, service)
        require_equal(records + candidates, 0, "Fremdes hinterliess etwas")
    finally:
        await _teardown(adaptive, service)


# =====================================================================
# F — die Herkunft steht nicht im Modellschema
# =====================================================================

async def t_f_no_model_field_can_change_the_source() -> None:
    """Herkunft ist Transportwahrheit. Sie kann nicht vorgeschlagen werden."""
    from solvio.memory.adaptive import extractor as E

    parsed = E.parse({"proposals": [dict(
        statement="Bevorzugt eine klare Empfehlung.", kind="stated",
        memory_type="preference", subject="s", about="self",
        sensitivity="personal", flags=[],
        # Alles, womit ein Modell die Herkunft umschreiben wollen koennte.
        channel="voice_iphone", source_class="verified_device_interactive",
        endpoint="iphone", device_id="4DECE0FA", speaker="owner",
        verified=True, trusted=True)]})
    require_equal(len(parsed.proposals), 1, "der Vorschlag ging verloren")
    proposal = parsed.proposals[0]
    for field in ("channel", "source_class", "endpoint", "device_id",
                  "speaker", "verified", "trusted"):
        require(not hasattr(proposal, field),
                f"ein Vorschlag traegt {field!r}")
    for field in ("channel", "endpoint", "device", "speaker", "trusted"):
        require(field not in E.SYSTEM_PROMPT.lower(),
                f"die Anweisung an das Modell erwaehnt {field!r}")


def _require_transport_channels(sources) -> None:
    bindings = []
    for path, source in sources.items():
        for node in ast.walk(ast.parse(source, filename=path)):
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Attribute) and target.attr == "channel":
                        bindings.append((path, type(node).__name__, ast.unparse(target),
                                         ast.unparse(node.value)))
    require_equal(sorted(bindings), [
        ("browser_voice_endpoint.py", "Assign", "sess.channel", "'voice_browser'"),
        ("realtime/core_server.py", "Assign", "self.channel", "'voice_satellite'"),
        ("voice_endpoint.py", "Assign", "sess.channel", "'voice_iphone'"),
    ], "Herkunft muss eine feste Klasse des benannten Transportendpunkts bleiben")


def t_f2_the_channel_is_assigned_only_from_transport_truth() -> None:
    """Drei feste Transport-Schreibstellen; keine Klasse aus Modell/HTTP-Daten.

    Die Vorgabe ist die VORSICHTIGERE Klasse. Wer einen neuen Endpunkt
    anhaengt und das Setzen vergisst, lernt nichts — statt zu viel.
    """
    sources = {}
    for folder, _, files in os.walk(SRC):
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(folder, name)
            with open(path, encoding="utf-8") as stream:
                sources[os.path.relpath(path, SRC)] = stream.read()
    _require_transport_channels(sources)

    from solvio.realtime import core_server as CS
    from solvio import voice_endpoint as VE
    from solvio import browser_voice_endpoint as BVE
    require('self.channel = "voice_satellite"' in inspect.getsource(CS),
            "die Vorgabe ist nicht mehr die vorsichtige Klasse")
    endpoint = inspect.getsource(VE._handle)
    require('sess.channel = "voice_iphone"' in endpoint,
            "der Sprachendpunkt setzt die Klasse nicht")
    # Und erst NACH der bewiesenen Geraetepruefung.
    require(endpoint.index("_owner_device") < endpoint.index('sess.channel = "voice_iphone"'),
            "die Klasse wird vor der Geraetepruefung gesetzt")
    browser = inspect.getsource(BVE._handle)
    boundary = browser.index('sess.channel = "voice_browser"')
    require(browser.index("await _owner(request)") < boundary
            and browser.index("not await _same_owner(request, actor)") < boundary
            and browser.index("endpoint.nonces.consume(") < boundary,
            "Browserklasse wird vor Owner-/Nonce-Pruefung gesetzt")
    require(browser.index("sess.browser_task_session is None") < browser.index("pump_endpoint("),
            "Browseraudio erreicht die Sitzung vor der geprueften Aufgabenbindung")
    for key, before, after in (
        ("browser_voice_endpoint.py", 'sess.channel = "voice_browser"', 'sess.channel = body["channel"]'),
        ("realtime/core_server.py", 'self.channel = "voice_satellite"', 'self.channel = "voice_browser"'),
    ):
        require(before in sources[key])
        mutant = dict(sources, **{key:sources[key].replace(before, after)})
        require_raises(AssertionError, _require_transport_channels, mutant)
    require_raises(AssertionError, _require_transport_channels,
                   dict(sources, **{"tools/forged.py":'sess.channel = "voice_browser"'}))


def t_f3_the_reader_passes_its_own_channel_and_nothing_else() -> None:
    from solvio.realtime import core_server as CS
    source = inspect.getsource(CS.Session._offer_adaptive)
    require("channel=self.channel" in source,
            "der Reader reicht nicht die eigene Herkunft weiter")
    for forbidden in ("arguments", "args", "ev.get", "payload"):
        require(forbidden not in source,
                f"der Reader liest {forbidden!r}")


# =====================================================================
# G — ein Neustart aendert nichts
# =====================================================================

async def t_g_the_source_policy_survives_a_restart_unchanged() -> None:
    """Die Politik liegt im Code, nicht im Zustand — und darf nirgends driften."""
    folder = tempfile.mkdtemp(prefix="solvio-source-restart-")
    service = MemoryService(base_dir=folder).open()
    adaptive = AdaptiveMemory(service, CandidateStore(folder), extractor=Fixture())
    try:
        await adaptive.process(_turn("voice_satellite"), now=NOW)
        await adaptive.process(_turn("voice_iphone", conversation_id="c2",
                                     message_id="m2"), now=NOW)
        before = await _state(adaptive, service)
        require_equal(before, (1, 1), f"Vorbedingung: {before}")
    finally:
        await _teardown(adaptive, service)

    service2 = MemoryService(base_dir=folder).open()
    again = AdaptiveMemory(service2, CandidateStore(folder), extractor=Fixture())
    try:
        require(not P.may_auto_learn("voice_satellite"),
                "nach dem Neustart lernt das Raummikrofon")
        require(P.may_auto_learn("voice_iphone"),
                "nach dem Neustart lernt das Telefon nicht")
        result = await again.process(
            _turn("voice_satellite", conversation_id="c3", message_id="m3"),
            now=NOW)
        require_equal(result.adopted, 0, "nach dem Neustart wurde vom Pi gelernt")
        after = await _state(again, service2)
        require_equal(after, before, f"der Zustand hat sich veraendert: {after}")
    finally:
        await _teardown(again, service2)


# =====================================================================
# Was der Pi WEITERHIN darf
# =====================================================================

async def t_the_pi_keeps_conversation_and_recall() -> None:
    """Nicht lernen heisst nicht taub. Abruf und Gespraech bleiben unberuehrt.

    Sonst waere die Haertung ein Rueckschritt: der Satellit im Wohnzimmer ist
    der Hauptweg, ueber den SOLVIO benutzt wird.
    """
    from solvio.memory.intent import detect

    folder = tempfile.mkdtemp(prefix="solvio-pi-recall-")
    service = MemoryService(base_dir=folder).open()
    adaptive = AdaptiveMemory(service, CandidateStore(folder), extractor=Fixture())
    try:
        # Ausdrueckliches Merken ueber den Satelliten: unveraendert erlaubt.
        intent = detect("Merk dir dauerhaft, mein Testwort ist Bernstein.")
        require(intent is not None, "Vorbedingung")
        result = await service.remember(intent, conversation_id="pi1")
        require(result.ok, str(result))

        # Und der Abruf funktioniert vom Satelliten aus genauso.
        hits = await service.search("Testwort Bernstein")
        require(hits, "der Abruf ueber den Satelliten fand nichts")

        # Nur automatisch gelernt wird nichts.
        before = await _state(adaptive, service)
        await adaptive.process(_turn("voice_satellite"), now=NOW)
        require_equal(await _state(adaptive, service), before,
                      "der Satellit hat doch etwas hinterlassen")
    finally:
        await _teardown(adaptive, service)


def t_an_explicit_turn_belongs_to_the_explicit_path_alone() -> None:
    """Ein Turn, eine Autoritaet.

    Live gemessen: „Merk dir dauerhaft, meine Lieblingsfarbe ist Petrol"
    erzeugte ZWEI Eintraege — den ausdruecklichen (`user_direct`, conf 1.0) und
    einen gelernten („Seine Lieblingsfarbe ist Petrol.", `solvio_inference`).
    Die Entdopplung greift nicht, weil der Extraktor in die dritte Person
    umschreibt und der Dedup-Schluessel damit ein anderer ist.

    Zwei aktive Wahrheiten ueber dasselbe, mit verschiedener Vertrauensstufe —
    und die schwaechere haette ein Vergessen der staerkeren ueberlebt.
    """
    from solvio.realtime import core_server as CS

    source = inspect.getsource(CS.Session._offer_adaptive)
    require("if explicit:" in source,
            "der Lernpfad laesst einen ausdruecklichen Turn nicht in Ruhe")
    require("skipped_explicit_turn" in source,
            "das Ueberspringen wird nicht protokolliert")

    # Und der Reader reicht die erkannte Absicht wirklich weiter.
    reader = inspect.getsource(CS.Session._oa_reader)
    require("explicit=detected is not None" in reader,
            "der Reader meldet die ausdrueckliche Absicht nicht")
    intent = inspect.getsource(CS.Session._offer_memory_intent)
    require("return detected" in intent,
            "die erkannte Absicht wird nicht zurueckgegeben")


def t_the_residual_risk_is_written_down() -> None:
    """Was die Haertung NICHT loest, muss dastehen.

    Ein ausdrueckliches „merk dir das" durch das Raummikrofon wirkt weiterhin —
    auch wenn es ein Gast sagt. Das ist bewusst so gelassen (der Milestone
    aendert den ausdruecklichen Weg nicht) und deshalb aufgeschrieben, statt
    stillschweigend in Kauf genommen zu werden.
    """
    repo = os.path.join(os.path.dirname(__file__), "..")
    debt = open(os.path.join(repo, "docs/debt/TECH_DEBT.md"),
                encoding="utf-8").read()
    require("DEBT-0099" in debt, "die Sprecherfrage ist nicht erfasst")
    for phrase in ("merk dir", "Raummikrofon"):
        require(phrase.lower() in debt.lower(),
                f"das Restrisiko {phrase!r} ist nicht aufgeschrieben")
    design = os.path.join(repo, "docs/design/speaker-identity-v1")
    require(os.path.isdir(design),
            "es gibt keine Entwurfsseite fuer Speaker Identity V1")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
