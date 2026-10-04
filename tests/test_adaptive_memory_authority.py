"""Gedaechtnis ist Information, nie Autoritaet — und die beiden Schuldenfixes.

Diese Datei beweist die Grenze, an der der ganze Milestone haengt. Sie tut das
nicht, indem sie Filter prueft, sondern indem sie zeigt, dass es **keinen
Aufrufer** gibt: kein Modul unter `security/`, `capabilities/approval*` oder
dem Freigabeweg liest persoenliches Gedaechtnis. Filter kann man vergessen,
einen fehlenden Aufrufer nicht.

Dazu die beiden Bestandsbefunde, die dieser Milestone schliesst:
DEBT-0092 (das Vault-Archiv behielt den Volltext von Vergessenem) und
DEBT-0093 (die aktive Sicht ignorierte das Gueltigkeitsfenster).

ASSERTION POLICY: `require*` aus `tests/_guard.py` sind Funktionsaufrufe und
ueberleben `python -O`.
"""
from __future__ import annotations

import inspect
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from _guard import enforce_assertions, require, require_equal  # noqa: E402

from solvio.contracts.memory import (MemoryRecord, MemoryType,  # noqa: E402
                                     ProvenanceEntry, Sensitivity)
from solvio.contracts.trust import SourceType, TrustLevel  # noqa: E402
from solvio.memory.adaptive import policy as P  # noqa: E402
from solvio.memory.adaptive.candidates import CandidateStore  # noqa: E402
from solvio.memory.adaptive.extractor import parse  # noqa: E402
from solvio.memory.adaptive.pipeline import AdaptiveMemory  # noqa: E402
from solvio.memory.service import MemoryService  # noqa: E402
from solvio.memory.store import SolvioMemory  # noqa: E402

enforce_assertions()

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(REPO, "src", "solvio")


def _record(**kw) -> MemoryRecord:
    base = dict(id="", memory_type=MemoryType.SEMANTIC, content="Ein Satz.",
                subject="thema", source="voice",
                source_type=SourceType.USER_DIRECT, created_at=NOW,
                updated_at=NOW, trust_level=TrustLevel.USER_DIRECT,
                sensitivity=Sensitivity.PERSONAL)
    base.update(kw)
    return MemoryRecord(**base)


# =====================================================================
# Autoritaet — der strukturelle Beweis
# =====================================================================

def t_no_security_module_reads_personal_memory() -> None:
    """Der eigentliche Schutz: es gibt keinen Aufrufer.

    Selbst eine perfekt gefaelschte Erinnerung haette niemanden, dem sie etwas
    sagen koennte. Diese Zusicherung ist wichtiger als jeder Filter.
    """
    # Geprueft werden die Module, die ENTSCHEIDEN — nicht die, die verdrahten.
    # `approver_runtime` haengt den Leseweg an dieselbe Anwendung wie den
    # Sprachweg; es liest dabei keinen Gedaechtnisinhalt, es reicht eine
    # Referenz weiter. Die Entscheidungsmodule duerfen das Paket nicht einmal
    # importieren.
    forbidden = ("solvio.memory", "solvio.knowledge", "adaptive")
    for folder in ("security", "capabilities/approval_gateway.py",
                   "capabilities/invocation.py", "capabilities/contract.py"):
        path = os.path.join(SRC, folder)
        files = ([path] if path.endswith(".py") else
                 [os.path.join(r, n) for r, _, fs in os.walk(path)
                  for n in fs if n.endswith(".py")])
        for file in files:
            text = open(file, encoding="utf-8").read()
            for line in text.split("\n"):
                stripped = line.strip()
                if not (stripped.startswith("import ")
                        or stripped.startswith("from ")):
                    continue
                for token in forbidden:
                    require(token not in stripped,
                            f"{os.path.relpath(file, REPO)} importiert {token}: "
                            f"{stripped[:80]}")


def t_the_wiring_module_only_attaches_and_never_reads() -> None:
    """`approver_runtime` darf anhaengen — lesen darf es nichts.

    Es ist die einzige Stelle, die Freigabeweg und Leseweg zugleich kennt.
    Genau deshalb wird hier nachgesehen: es reicht eine Referenz weiter und
    fasst keinen Gedaechtnisinhalt an.
    """
    from solvio.capabilities import approver_runtime as AR
    source = inspect.getsource(AR)
    for token in ("recall(", "active_records(", ".content", "get_provenance(",
                  "remember(", "forget(", "purge("):
        require(token not in source, f"das Verdrahtungsmodul ruft {token}")


def t_security_tree_matches_freeze_and_explicit_n2_amendment():
    from _security_amendment import require_n2_security_tree
    require_n2_security_tree(REPO)


def t_the_frozen_gateway_knows_nothing_of_the_memory_routes() -> None:
    """Angehaengt statt eingebaut — dieselbe Probe wie beim Sprachweg."""
    from solvio.security.mobile_approval import gateway as G
    source = inspect.getsource(G).lower()
    for token in ("memory", "adaptive", "candidate"):
        require(token not in source,
                f"der eingefrorene Gateway kennt {token!r}")


def t_a_learned_memory_cannot_lower_an_approval_requirement() -> None:
    """Der Freigabe-Boden steht im Capability Contract, nicht in einer Notiz."""
    from solvio.capabilities.contract import requires_approval
    from solvio.capabilities.memory import SPECS
    from solvio.tools.base import RiskLevel

    for name, spec in SPECS.items():
        require(int(spec.base_risk) >= int(RiskLevel.MUTATING),
                f"{name} ist nicht bestaetigungspflichtig")
        require(requires_approval(spec.base_risk),
                f"{name} laeuft ohne Freigabe")
        require(not spec.is_read_only(), f"{name} gilt als lesend")


def t_an_authority_shaped_preference_is_refused_outright() -> None:
    """„Frag mich nicht mehr nach Face ID" wird nicht einmal vorgeschlagen."""
    for statement in ("Moechte nicht mehr nach Face ID gefragt werden.",
                      "Braucht keine Freigabe mehr fuer solche Aenderungen.",
                      "Will nie wieder eine Bestaetigung sehen.",
                      "SOLVIO soll das einfach machen.",
                      "Weniger Sicherheitsabfragen bitte."):
        result = P.is_permissive_rule(statement)
        require(not result.ok, f"durchgelassen: {statement!r}")
        decision = P.decide(
            P.Proposal(statement=statement, kind=P.STATED,
                       memory_type=MemoryType.RULE, subject="rule:x"),
            P.OwnerTurn(text="Ich will das so.", channel="voice_iphone"),
            evidence_conversations=0, suppressed=False, secret_hit=False)
        require_equal(decision.action, P.DISCARD, statement)


def t_adaptive_memory_has_no_method_that_could_approve_anything() -> None:
    """Die Klasse kennt Freigabe, Risiko und TrustContext ueberhaupt nicht."""
    source = inspect.getsource(AdaptiveMemory)
    for token in ("approve", "approval", "risk", "TrustContext", "trust_context",
                  "face_id", "confirm_action", "RiskLevel"):
        require(token.lower() not in source.lower(),
                f"AdaptiveMemory erwaehnt {token!r}")


def t_okf_verification_never_becomes_authorization() -> None:
    """Ein gelernter Eintrag erscheint im Buendel als Entwurf und erlaubt nichts."""
    from solvio.knowledge import compiler, okf
    record = _record(source_type=SourceType.SOLVIO_INFERENCE,
                     trust_level=TrustLevel.AGENT_GENERATED,
                     memory_type=MemoryType.PREFERENCE,
                     content="Bevorzugt kurze Antworten.", subject="pref:laenge")
    record.id = "a" * 32
    vault = tempfile.mkdtemp(prefix="solvio-okf-")
    compiler.compile_bundle([record], vault, now=NOW)
    path = [os.path.join(r, n) for r, _, fs in os.walk(vault) for n in fs
            if n.endswith(".md") and n not in okf.RESERVED][0]
    head = okf.parse_frontmatter(okf.split_frontmatter(
        open(path, encoding="utf-8").read())[0])
    require_equal(head["status"], "draft",
                  "Gelerntes erscheint als gesichertes Wissen")
    require_equal(head["solvio"]["authority_effect"], "none", "Wirkung gesetzt")
    require_equal(head["solvio"]["canonical"], False, "als kanonisch markiert")


# =====================================================================
# Freigabebindung
# =====================================================================

def t_every_memory_mutation_is_non_idempotent() -> None:
    """Hoechstens einmal — ein mehrdeutiger Ausgang geht in die Wiederherstellung."""
    from solvio.capabilities.memory import SPECS
    from solvio.security.mobile_approval.execution import NON_IDEMPOTENT_WRITE

    for name, spec in SPECS.items():
        require_equal(spec.effective_semantics(), NON_IDEMPOTENT_WRITE, name)


def t_the_target_identity_is_bound_into_the_approval() -> None:
    """Der Freigabetext traegt die Zielkennung — und der Digest bindet sie.

    Damit kann eine Freigabe fuer Erinnerung A nicht Erinnerung B ausfuehren:
    beim Einloesen wird der Digest gegen die AKTUELLEN Argumente neu gebildet.
    """
    from solvio.capabilities.approval_gateway import (approval_digest,
                                                      render_action)
    from solvio.capabilities.memory import SPECS

    spec = SPECS["memory_forget"]
    text_a = render_action(spec, {"memory_id": "aaaa", "statement": "X"})
    text_b = render_action(spec, {"memory_id": "bbbb", "statement": "X"})
    require("aaaa" in text_a, "die Kennung steht nicht im Freigabetext")
    require(text_a != text_b, "zwei Ziele ergeben denselben Text")
    require(approval_digest(spec, {"memory_id": "aaaa", "statement": "X"})
            != approval_digest(spec, {"memory_id": "bbbb", "statement": "X"}),
            "zwei Ziele ergeben denselben Digest")


def t_the_approval_text_is_readable_german() -> None:
    """Ein Mensch soll unterschreiben, was er versteht."""
    from solvio.capabilities.approval_gateway import ACTION_LABELS, render_action
    from solvio.capabilities.memory import LABELS, SPECS, register

    from solvio.capabilities.memory import MemoryCapabilities

    class _Router:
        def register(self, spec, handler): pass

    register(_Router(), MemoryCapabilities(None, None))
    for name in SPECS:
        require(name in ACTION_LABELS, f"{name} hat keine Beschriftung")
    text = render_action(SPECS["memory_purge"],
                         {"memory_id": "abc", "statement": "Mag Kaffee."})
    require("ENDGUELTIG" in text, f"keine Endgueltigkeitswarnung: {text}")
    for headline, labels in LABELS.values():
        require_equal(len(set(labels.values())), len(labels),
                      f"zwei Argumente teilen eine Beschriftung: {headline}")


def t_every_memory_capability_has_a_caller() -> None:
    """Eine gebaute Faehigkeit ohne Aufrufer ist eine Zusage ohne Wirkung.

    Gefunden nach dem ersten Produktivstart: die fuenf Faehigkeiten waren
    registriert, aber nichts konnte sie ausloesen — der WISSEN-Bildschirm ist
    ein eigener Milestone. „Vergiss das" waere eine Bitte ohne Wirkung
    geblieben. Genau dieses Muster (ein Schreibweg, den niemand ruft) ist in
    diesem Projekt schon zweimal als Schuld aufgefallen.
    """
    from solvio.capabilities.memory import SPECS
    from solvio.tools.memory_capability_tools import memory_capability_tools

    tools = {t.name for t in memory_capability_tools(None, None)}
    require_equal(tools, set(SPECS),
                  f"ohne Aufrufer: {sorted(set(SPECS) - tools)}")
    for tool in memory_capability_tools(None, None):
        schema = tool.schema()
        require_equal(schema["name"], tool.name, "Schema und Name weichen ab")
        require(schema["description"], f"{tool.name} hat keine Beschreibung")
        require("Freigabe" in schema["description"],
                f"{tool.name} verschweigt dem Modell die Freigabepflicht")

    # Und der Zusammenbau registriert sie wirklich.
    import inspect

    from solvio.tools import registry
    source = inspect.getsource(registry)
    require("memory_capability_tools" in source,
            "der Zusammenbau registriert die Bruecke nicht")


def t_every_handler_takes_exactly_the_argument_dict() -> None:
    """Die Aufrufkonvention des Routers ist `handler(args)` — ein Dict.

    Live gemessen, und es hat eine Face-ID-Freigabe verbrannt: die Handler
    standen als `def forget(self, memory_id="", statement="")` da. `_call` ruft
    `handler(args)` mit EINEM positionalen Dict — also landete das ganze Dict in
    `memory_id`, der Datenbankzugriff flog, und weil die Ausnahme NACH dem
    durablen Anspruch kam, meldete der eingefrorene Pfad korrekt
    `unknown_outcome` -> RECOVERY_REQUIRED. Der Mensch hatte freigegeben, und
    nichts geschah.

    Die frueheren Tests riefen die Handler mit `**args` — und fanden es
    deshalb nie.
    """
    import ast
    import inspect

    from solvio.capabilities import memory as M

    tree = ast.parse(inspect.getsource(M))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef)
               and n.name == "MemoryCapabilities")
    handlers = [n for n in cls.body if isinstance(n, ast.AsyncFunctionDef)]
    require(len(handlers) >= 5, f"nur {len(handlers)} Handler gefunden")
    for fn in handlers:
        names = [a.arg for a in fn.args.args]
        require_equal(names, ["self", "arguments"],
                      f"{fn.name} nimmt {names} statt (self, arguments)")
        require(fn.args.kwarg is None, f"{fn.name} hat **kwargs")
        require(not fn.args.kwonlyargs, f"{fn.name} hat Schluesselwort-Argumente")


async def t_a_handler_called_the_router_way_actually_works() -> None:
    """Und zwar so gerufen, wie `router._call` es tut — nicht mit `**args`."""
    from solvio.capabilities.memory import MemoryCapabilities
    from solvio.capabilities.router import _call
    from solvio.memory.adaptive.candidates import CandidateStore
    from solvio.memory.adaptive.pipeline import AdaptiveMemory

    folder = tempfile.mkdtemp(prefix="solvio-handler-")
    service = MemoryService(base_dir=folder).open()
    adaptive = AdaptiveMemory(service, CandidateStore(folder))
    try:
        record_id = await service.semantic.remember(_record(
            content="Trinkt Kaffee schwarz.", subject="pref:kaffee",
            memory_type=MemoryType.PREFERENCE,
            source_type=SourceType.SOLVIO_INFERENCE,
            trust_level=TrustLevel.AGENT_GENERATED))
        caps = MemoryCapabilities(service, adaptive)
        result = await _call(caps.forget, {"memory_id": record_id,
                                           "statement": "Trinkt Kaffee schwarz."})
        require_equal(result.get("ok"), True, str(result))
        require_equal(len(await service.semantic.memory.active_records()), 0,
                      "der Eintrag ist noch da")
        # Und die Unterdrueckung haelt.
        require(await adaptive.candidates.is_suppressed(
            adaptive._dedup_key("Trinkt Kaffee schwarz.")),
            "nach dem Vergessen fehlt die Unterdrueckung")
    finally:
        await adaptive.close()
        await service.close()


async def t_a_memory_capability_refuses_without_a_proven_caller() -> None:
    """Fail-closed: ohne belegten Anrufer wird an Gedaechtnis nichts geaendert."""
    from solvio.tools.memory_capability_tools import memory_capability_tools

    for tool in memory_capability_tools(None, None):
        result = await tool.run({"memory_id": "abc"})
        require(not result.success, f"{tool.name} lief ohne Anrufer")
        require_equal(result.error, "no_trusted_context", tool.name)


async def t_search_hands_the_model_an_id_and_an_honest_reason() -> None:
    """Ohne Kennung kein Vergessen, ohne Herkunft keine wahre Antwort.

    Zwei Abnahmepunkte haengen daran: „vergiss das" braucht ein Ziel, und
    „woher weisst du das?" braucht einen Satz, den nicht das Modell erfindet.
    Frueher gab die Suche nur Inhalt und Datum heraus — gut gemeint, aber
    damit war beides unmoeglich.
    """
    from solvio.memory.intent import detect
    from solvio.tools.memory_tools import MemorySearchTool

    folder = tempfile.mkdtemp(prefix="solvio-search-")
    service = MemoryService(base_dir=folder).open()
    try:
        intent = detect("Merk dir dauerhaft, mein Testwort ist Bernstein.")
        require(intent is not None, "Vorbedingung")
        await service.remember(intent, conversation_id="c1")
        result = await MemorySearchTool(service).run({"query": "Testwort Bernstein"})
        require(result.success, str(result.error))
        hits = (result.data or {}).get("memories") or []
        require(hits, "die Suche fand nichts")
        hit = hits[0]
        require(hit.get("id"), "der Treffer traegt keine Kennung")
        require_equal(hit.get("herkunft"), "explicit", str(hit))
        require("ausdruecklich" in (hit.get("warum") or ""), str(hit))
        # Und das Schema sagt dem Modell beides: benutze die Kennung, sprich
        # sie nicht aus, und erfinde den Herkunftssatz nicht.
        description = MemorySearchTool(service).schema()["description"]
        require("memory_forget" in description, "die Kennung hat keinen Zweck")
        require("niemals aussprechen" in description, "die Kennung darf gesagt werden")
        require("erfinde nie" in description, "der Herkunftssatz ist freigestellt")
    finally:
        await service.close()


async def t_the_reason_sentence_comes_from_the_record_not_the_model() -> None:
    """Gelerntes sagt „abgeleitet", Ausdrueckliches sagt „ausdruecklich"."""
    from solvio.memory.adaptive import lifecycle as L
    from solvio.tools.memory_tools import MemorySearchTool

    folder = tempfile.mkdtemp(prefix="solvio-reason-")
    service = MemoryService(base_dir=folder).open()
    try:
        await service.semantic.remember(_record(
            content="Bevorzugt eine klare Empfehlung.", subject="pref:empfehlung",
            memory_type=MemoryType.PREFERENCE,
            source_type=SourceType.SOLVIO_INFERENCE,
            trust_level=TrustLevel.AGENT_GENERATED))
        result = await MemorySearchTool(service).run({"query": "klare Empfehlung"})
        hit = ((result.data or {}).get("memories") or [{}])[0]
        require_equal(hit.get("herkunft"), L.LEARNED, str(hit))
        require("abgeleitet" in (hit.get("warum") or ""), str(hit))
    finally:
        await service.close()


async def t_the_embedding_model_is_never_used_concurrently() -> None:
    """Zwei Threads, ein Metal-Kontext, Absturz.

    Am 2026-08-26 ist der produktive Core zweimal mit SIGSEGV in
    `at::native::mps::copy_cast_kernel_mps` gestorben. Vorher lief die
    Einbettung ueber `asyncio.to_thread`, also den Standard-Pool mit MEHREREN
    Arbeitern. Solange nur der Gespraechspfad einbettete, kam das nie
    zusammen — mit Adaptive Memory rechnet der Lern-Arbeiter im Hintergrund,
    waehrend der Gespraechspfad abruft.

    Diese Zusicherung prueft das Verhalten, nicht die Schreibweise: viele
    gleichzeitige Aufrufe duerfen sich nie ueberlappen.
    """
    import asyncio
    import threading

    from solvio.memory.embedding import QwenLocalEmbeddingProvider

    provider = QwenLocalEmbeddingProvider.__new__(QwenLocalEmbeddingProvider)
    from concurrent.futures import ThreadPoolExecutor
    provider._pool = ThreadPoolExecutor(max_workers=1,
                                        thread_name_prefix="solvio-embed-test")
    live = {"now": 0, "max": 0}
    guard = threading.Lock()

    def fake_encode(texts, is_query):
        with guard:
            live["now"] += 1
            live["max"] = max(live["max"], live["now"])
        try:
            import time
            time.sleep(0.01)
            return [[0.0] for _ in texts]
        finally:
            with guard:
                live["now"] -= 1

    provider._encode = fake_encode
    await asyncio.gather(*[provider._run(bool(i % 2), ["x"]) for i in range(24)])
    require_equal(live["max"], 1,
                  f"{live['max']} gleichzeitige Modellaufrufe — das ist der "
                  f"Absturz von 13:20:49")
    provider._pool.shutdown(wait=True)


def t_the_embedding_provider_owns_its_own_single_worker() -> None:
    """Strukturell: kein Rueckfall auf den Standard-Pool."""
    import inspect

    from solvio.memory import embedding as E

    # Geprueft wird der CODE, nicht die Prosa: die Erklaerung im Modulkopf
    # nennt `asyncio.to_thread` als das, was frueher dort stand — ein
    # Textvergleich haette an der eigenen Begruendung angeschlagen.
    import ast

    tree = ast.parse(inspect.getsource(E))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef)
               and n.name == "QwenLocalEmbeddingProvider")
    calls = [n for n in ast.walk(cls) if isinstance(n, ast.Call)]
    names = {ast.unparse(c.func) for c in calls}
    require(any("ThreadPoolExecutor" in n for n in names),
            "der Anbieter hat keinen eigenen Einzelarbeiter")
    require(not any("to_thread" in n for n in names),
            "der Anbieter faellt auf den Standard-Pool zurueck")
    for call in calls:
        if "ThreadPoolExecutor" not in ast.unparse(call.func):
            continue
        workers = {kw.arg: ast.literal_eval(kw.value) for kw in call.keywords
                   if kw.arg == "max_workers"}
        require_equal(workers.get("max_workers"), 1,
                      "der Einbettungspool hat mehr als einen Arbeiter")


def t_there_is_no_second_write_path_past_the_gateway() -> None:
    """Der Leseweg schreibt nicht. Nirgends."""
    from solvio import memory_endpoint as ME
    source = inspect.getsource(ME)
    require("add_post" not in source, "der Leseweg hat eine POST-Route")
    require("add_put" not in source, "der Leseweg hat eine PUT-Route")
    require("add_delete" not in source, "der Leseweg hat eine DELETE-Route")
    for token in ("forget(", "purge(", "remember(", "supersede(", "confirm_candidate("):
        require(token not in source, f"der Leseweg ruft {token}")


def t_every_read_route_demands_a_proven_device() -> None:
    from solvio import memory_endpoint as ME
    handlers = [v for k, v in vars(ME).items() if k.startswith("h_")]
    require(len(handlers) >= 5, f"nur {len(handlers)} Routen gefunden")
    for handler in handlers:
        source = inspect.getsource(handler)
        require("_authed(request)" in source,
                f"{handler.__name__} prueft das Geraet nicht")
        require("unauthorized" in source,
                f"{handler.__name__} lehnt nicht ab")


# =====================================================================
# Der Leseweg gibt kein Rohmaterial heraus
# =====================================================================

def t_the_read_path_never_returns_raw_material() -> None:
    """Geprueft wird die AUSGABE, nicht die Prosa.

    Eine frueher Fassung dieses Tests suchte Woerter im Quelltext und schlug
    an der eigenen Erklaerung an („kein Audio") — sie mass die Dokumentation
    statt der Funktion.
    """
    from solvio import memory_endpoint as ME
    shaped = ME._shape(_record())
    allowed = {"id", "content", "memory_type", "subject", "lifecycle",
               "sensitivity", "trust_level", "confidence", "created_at",
               "updated_at", "valid_until", "evidence", "explanation"}
    require_equal(set(shaped), allowed,
                  f"die Ausgabe hat sich veraendert: {sorted(set(shaped) - allowed)}")
    evidence = shaped["evidence"]
    require_equal(set(evidence), {"observations", "kinds", "first_at", "last_at"},
                  f"die Evidenzlage traegt mehr als Zahlen: {sorted(evidence)}")


def t_a_secret_reference_yields_only_its_existence() -> None:
    from solvio import memory_endpoint as ME
    record = _record(sensitivity=Sensitivity.SECRET_REFERENCE,
                     content="keychain://router/admin-passwort",
                     subject="Router-Zugang")
    shown = ME._visible_content(record)
    require("keychain://router" not in shown, "der Verweis wurde herausgegeben")
    require("Geheimnis" in shown, "die Existenz wird nicht benannt")


def t_the_lifecycle_is_computed_by_the_core_not_the_client() -> None:
    """Sichtbarkeitslogik, die ein Client nachbaut, baut er falsch nach."""
    from solvio import memory_endpoint as ME
    from solvio.memory.adaptive import lifecycle as L
    shaped = ME._shape(_record())
    require("lifecycle" in shaped, "der Core liefert die Ableitung nicht mit")
    require("explanation" in shaped, "der Core liefert die Begruendung nicht mit")
    require("evidence" in shaped, "der Core liefert die Evidenzlage nicht mit")
    require_equal(shaped["lifecycle"], L.RECORDED, str(shaped))


def t_the_explanation_comes_from_provenance_never_from_a_model() -> None:
    from solvio.memory.adaptive import lifecycle as L
    explicit = _record(metadata={"explicit_intent": True})
    learned = _record(source_type=SourceType.SOLVIO_INFERENCE,
                      trust_level=TrustLevel.AGENT_GENERATED)
    require("ausdruecklich" in L.explain(explicit), L.explain(explicit))
    require("abgeleitet" in L.explain(learned), L.explain(learned))
    require_equal(L.lifecycle_of(explicit), L.EXPLICIT, "falsch abgeleitet")
    require_equal(L.lifecycle_of(learned), L.LEARNED, "falsch abgeleitet")


# =====================================================================
# DEBT-0093 — das Gueltigkeitsfenster
# =====================================================================

async def t_expired_memory_is_no_longer_active_truth() -> None:
    memory = SolvioMemory(tempfile.mkdtemp())
    try:
        live = await memory.remember(_record(subject="gilt"))
        expired = await memory.remember(_record(
            subject="abgelaufen", valid_until=NOW - timedelta(days=1)))
        future = await memory.remember(_record(
            subject="spaeter", valid_from=NOW + timedelta(days=1)))
        active = {r.id for r in await memory.active_records(NOW)}
        require(live in active, "Gueltiges fehlt")
        require(expired not in active, "Abgelaufenes gilt als aktuell")
        require(future not in active, "Zukuenftiges gilt als aktuell")
        require_equal(await memory.active_ids(NOW), active,
                      "die beiden aktiven Sichten sind sich uneinig")
    finally:
        await memory.close()


async def t_the_two_current_truth_views_agree() -> None:
    """`recall()` und `active_records()` duerfen nicht auseinanderlaufen.

    Genau das war DEBT-0093: `recall` verwarf einen abgelaufenen Record,
    waehrend Semantik-Index und Obsidian-Buendel ihn weiter als aktuell
    zeigten.
    """
    memory = SolvioMemory(tempfile.mkdtemp())
    try:
        await memory.remember(_record(subject="abgelaufen", content="galt bis gestern",
                                      valid_until=NOW - timedelta(days=1)))
        recalled = await memory.recall("galt bis gestern")
        active = await memory.active_records(NOW)
        require_equal(len(recalled), 0, "recall liefert Abgelaufenes")
        require_equal(len(active), 0, "die aktive Sicht liefert Abgelaufenes")
    finally:
        await memory.close()


# =====================================================================
# DEBT-0092 — das Archiv vergisst mit
# =====================================================================

async def t_forgetting_removes_the_content_from_the_vault_entirely() -> None:
    """Volltext, Titel UND Dateiname verschwinden — auch aus dem Archiv."""
    from solvio.knowledge import compiler

    secret_ish = "mein Lieblingsort ist die Huette am Silbersee"
    memory = SolvioMemory(tempfile.mkdtemp())
    vault = tempfile.mkdtemp(prefix="solvio-vault-")
    try:
        gone = await memory.remember(_record(content=secret_ish,
                                             subject="lieblingsort silbersee"))
        await memory.remember(_record(content="etwas harmloses", subject="harmlos"))
        compiler.compile_bundle(await memory.active_records(NOW), vault, now=NOW)
        require(_vault_contains(vault, "silbersee"), "Vorbedingung: nicht projiziert")

        await memory.forget(gone, reason="user_request")
        known = compiler.known_ids(vault) - {r.id for r in
                                             await memory.active_records(NOW)}
        reasons = await memory.removal_reasons(known)
        require_equal(reasons.get(gone), "forgotten", str(reasons))
        compiler.compile_bundle(await memory.active_records(NOW), vault, now=NOW,
                                removal_reasons=reasons)

        require(not _vault_contains(vault, "silbersee"),
                "der Volltext lebt im Vault weiter")
        require(not _vault_contains(vault, "lieblingsort"),
                "das Subjekt lebt im Vault weiter")
        require_equal(compiler.lint(vault, now=NOW), [],
                      "das Buendel ist nach dem Vergessen nicht konform")
    finally:
        await memory.close()


async def t_the_archive_keeps_a_content_free_marker() -> None:
    """Nichts verschwindet unbemerkt — aber es bleibt keine Silbe Inhalt."""
    from solvio.knowledge import compiler, okf

    memory = SolvioMemory(tempfile.mkdtemp())
    vault = tempfile.mkdtemp(prefix="solvio-vault-")
    try:
        gone = await memory.remember(_record(content="Huette am Silbersee",
                                             subject="lieblingsort"))
        compiler.compile_bundle(await memory.active_records(NOW), vault, now=NOW)
        await memory.forget(gone, reason="user_request")
        known = compiler.known_ids(vault) - {r.id for r in
                                             await memory.active_records(NOW)}
        compiler.compile_bundle(await memory.active_records(NOW), vault, now=NOW,
                                removal_reasons=await memory.removal_reasons(known))
        archive = os.path.join(vault, "99 Archiv")
        stones = [n for n in os.listdir(archive)
                  if n.endswith(".md") and n != okf.INDEX]
        require_equal(len(stones), 1, f"kein Grabstein: {stones}")
        text = open(os.path.join(archive, stones[0]), encoding="utf-8").read()
        head = okf.parse_frontmatter(okf.split_frontmatter(text)[0])
        require_equal(head["solvio"]["removal_reason"], "forgotten", str(head))
        require_equal(head["solvio"]["content_erased"], True, str(head))
        require_equal(head["solvio"]["memory_id"], gone, "die Kennung fehlt")
        require_equal(head["status"], "deprecated", str(head))
    finally:
        await memory.close()


async def t_a_superseded_memory_keeps_its_title_but_loses_its_body() -> None:
    """Abloesung ist kein Vergessen — aber die Historie gehoert dem Core."""
    from solvio.knowledge import compiler, okf

    memory = SolvioMemory(tempfile.mkdtemp())
    vault = tempfile.mkdtemp(prefix="solvio-vault-")
    try:
        old = await memory.remember(_record(content="Trinkt Espresso am Morgen.",
                                            subject="pref:kaffee"))
        compiler.compile_bundle(await memory.active_records(NOW), vault, now=NOW)
        await memory.supersede(old, _record(content="Trinkt Cappuccino am Morgen.",
                                            subject="pref:kaffee"))
        known = compiler.known_ids(vault) - {r.id for r in
                                             await memory.active_records(NOW)}
        compiler.compile_bundle(await memory.active_records(NOW), vault, now=NOW,
                                removal_reasons=await memory.removal_reasons(known))
        require(not _vault_contains(vault, "espresso"),
                "der abgeloeste Rumpf steht noch im Vault")
        require(_vault_contains(vault, "cappuccino"),
                "die neue Wahrheit fehlt")
        require_equal(compiler.lint(vault, now=NOW), [], "nicht konform")
    finally:
        await memory.close()


def _vault_contains(vault: str, needle: str) -> bool:
    """Sucht in Inhalten UND Dateinamen — ein Name ist auch Inhalt."""
    needle = needle.lower()
    for root, dirs, files in os.walk(vault):
        for name in files:
            if needle in name.lower():
                return True
            path = os.path.join(root, name)
            try:
                body = open(path, encoding="utf-8", errors="ignore").read().lower()
            except OSError:
                continue
            # Der feste Kopftext des Verlaufs enthaelt das Wort „Geheimnisse";
            # gesucht wird Inhalt, nicht die Ueberschrift.
            for line in body.split("\n"):
                if needle in line and not line.startswith("Was sich am Wissen"):
                    return True
    return False


# =====================================================================
# reinforce
# =====================================================================

async def t_reinforce_refuses_a_user_direct_record() -> None:
    """Seine Herkunft um eine Maschinenbeobachtung zu ergaenzen hiesse,
    sie zu verwaessern — und an der Herkunft haengt die Autoritaetsachse."""
    memory = SolvioMemory(tempfile.mkdtemp())
    try:
        record_id = await memory.remember(_record())
        entry = ProvenanceEntry(source_type=SourceType.SOLVIO_INFERENCE,
                                source="conversation:c1",
                                trust_level=TrustLevel.AGENT_GENERATED,
                                at=NOW, note="observed: x")
        try:
            await memory.reinforce(record_id, entry)
        except ValueError:
            return
        raise AssertionError("ein user_direct-Record wurde verstaerkt")
    finally:
        await memory.close()


async def t_reinforce_appends_and_never_replaces() -> None:
    memory = SolvioMemory(tempfile.mkdtemp())
    try:
        record_id = await memory.remember(_record(
            source_type=SourceType.SOLVIO_INFERENCE,
            trust_level=TrustLevel.AGENT_GENERATED,
            provenance=[ProvenanceEntry(
                source_type=SourceType.SOLVIO_INFERENCE, source="conversation:c0",
                trust_level=TrustLevel.AGENT_GENERATED, at=NOW,
                note="stated: erste Beobachtung")]))
        for index in range(3):
            await memory.reinforce(record_id, ProvenanceEntry(
                source_type=SourceType.SOLVIO_INFERENCE,
                source=f"conversation:c{index + 1}",
                trust_level=TrustLevel.AGENT_GENERATED, at=NOW,
                note=f"observed: Beobachtung {index}"))
        chain = await memory.get_provenance(record_id)
        require_equal(len(chain), 4, f"Kette hat {len(chain)} Eintraege")
        require_equal(chain[0].note, "stated: erste Beobachtung",
                      "der erste Eintrag wurde ueberschrieben")
        record = await memory.get(record_id)
        require_equal(record.source_type, SourceType.SOLVIO_INFERENCE,
                      "Verstaerken aenderte die Herkunft")
        require_equal(record.trust_level, TrustLevel.AGENT_GENERATED,
                      "Verstaerken aenderte die Vertrauensstufe")
    finally:
        await memory.close()


async def t_provenance_holds_identifiers_never_transcripts() -> None:
    """„Warum weisst du das?" wird aus Bezeichnern beantwortet, nicht aus Kopien."""
    folder = tempfile.mkdtemp(prefix="solvio-prov-")
    service = MemoryService(base_dir=folder).open()
    adaptive = AdaptiveMemory(service, CandidateStore(folder))

    class Fixed:
        name = "fixed"

        async def propose(self, turn, *, context=""):
            return parse({"proposals": [dict(
                statement="Bevorzugt kurze Antworten.", kind="stated",
                memory_type="preference", subject="pref:laenge", about="self",
                sensitivity="personal", flags=[])]})

    adaptive.extractor = Fixed()
    spoken = ("Ich moechte kurze Antworten, und ausserdem erzaehle ich dir jetzt "
              "eine sehr lange private Geschichte ueber meinen Urlaub in Kroatien.")
    try:
        await adaptive.process(P.OwnerTurn(
            text=spoken, channel="voice_iphone", conversation_id="c1",
            message_id="m1"), now=NOW)
        records = await service.semantic.memory.active_records(NOW)
        require_equal(len(records), 1, "nichts gelernt")
        chain = await service.semantic.memory.get_provenance(records[0].id)
        for entry in chain:
            require("Kroatien" not in (entry.note or ""),
                    "das Transkript steht in der Provenienz")
            require(len(entry.note or "") <= 160,
                    f"eine Kurzform ist {len(entry.note)} Zeichen lang")
            require((entry.source or "").startswith(("conversation:", "voice_turn",
                                                     "message:", "adaptive:")),
                    f"die Quelle ist kein Bezeichner: {entry.source!r}")
        require("Kroatien" not in records[0].content,
                "das Transkript steht im Inhalt")
    finally:
        await adaptive.close()
        await service.close()


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
