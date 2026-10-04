"""Was der kognitive Router NICHT kann — und warum es strukturell so ist.

Diese Suite prueft Grenzen, nicht Verhalten. Sie geht ueber den Quelltext und
ueber den echten Broker, weil beides Zusagen sind, die man nicht durch
Zusehen einloest:

* Der Router hat GENAU EINE Stelle, an der er wirkt.
* Er importiert weder Gedaechtnis noch Wissen noch Tresor — nicht gefiltert,
  sondern nicht verdrahtet.
* Er entscheidet keine Aeusserung nach ihrem Wortlaut.
* `gpt-5.4` erreicht genau zwei Auftraggeber, und beide gehoeren dem Core.
* `solvio_task` ist ein WERKZEUG und keine Faehigkeit — eine Faehigkeit ohne
  Eintrag in der Klassenliste waere `UNCLASSIFIED` und kostete Face ID fuer
  jede Aeusserung, auch fuer „das ist Gespraech".

ASSERTION POLICY: `require*` aus `tests/_guard.py` sind Funktionsaufrufe und
ueberleben `-O`.

Direkt: python tests/test_cognitive_router_boundary.py
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))
from _guard import enforce_assertions, require, require_equal  # noqa: E402
enforce_assertions()

_TMP = tempfile.mkdtemp(prefix="solvio-cognition-boundary-")
os.environ["SOLVIO_STATE_DIR"] = _TMP
os.environ["SOLVIO_COGNITION_DB"] = os.path.join(_TMP, "cognition.sqlite3")
os.environ["SOLVIO_AGENT_RUNS_DB"] = os.path.join(_TMP, "agent_runs.sqlite3")

from _broker_fixtures import Harness, responses_body, run  # noqa: E402

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(REPO, "src")
COGNITION_DIR = os.path.join(SRC, "solvio", "cognition")


def _modules(folder: str) -> list[str]:
    out = []
    for base, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        out += [os.path.join(base, f) for f in sorted(files) if f.endswith(".py")]
    return out


def _imports(path: str) -> list[tuple[int, str]]:
    tree = ast.parse(open(path, encoding="utf-8").read())
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(node.lineno, a.name) for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append((node.lineno, node.module))
    return found


# =====================================================================
# 1 — Der Import-Bann
# =====================================================================

def t_the_router_imports_neither_memory_nor_knowledge_nor_vault():
    """Nicht gefiltert, sondern nicht verdrahtet.

    Das ist der Unterschied zwischen „wir passen auf" und „es geht nicht".
    """
    offenders = []
    for path in _modules(COGNITION_DIR):
        for line, module in _imports(path):
            if module.startswith(("solvio.memory", "solvio.knowledge",
                                  "solvio.secret_vault", "solvio.payment",
                                  "solvio.security")):
                offenders.append(f"{os.path.basename(path)}:{line} {module}")
    require_equal(offenders, [],
                  f"der Router importiert, was er nie brauchen darf: {offenders}")


def t_the_router_never_names_the_provider_key():
    """Der Weg zum Modell ist der Broker. Ein Schluessel kommt nicht vor."""
    offenders = []
    for path in _modules(COGNITION_DIR) + [
            os.path.join(SRC, "solvio", "tools", "cognition_tools.py")]:
        quelle = open(path, encoding="utf-8").read()
        for marker in ("openai_api_key", "OPENAI_API_KEY", "sk-proj"):
            if marker in quelle:
                offenders.append(f"{os.path.basename(path)}: {marker}")
    require_equal(offenders, [],
                  f"ein Anbieterschluessel wird genannt: {offenders}")


def t_no_module_level_import_of_the_agent_runtime():
    """Die Rollback-Zusage der Agentenlaufzeit gilt auch ueber dem Router.

    `SOLVIO_AGENT_RUNTIME=off` muss die Laufzeit vollstaendig entfernen. Ein
    Import auf Modulebene machte das unmoeglich — die Wege des Routers
    erreichen sie deshalb ueber einen FAEHIGKEITSNAMEN oder ueber einen Import
    innerhalb einer Funktion.
    """
    offenders = []
    for path in _modules(COGNITION_DIR):
        tree = ast.parse(open(path, encoding="utf-8").read())
        for node in tree.body:
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module]
            for module in mods:
                if module.startswith("solvio.agent_runtime"):
                    offenders.append(f"{os.path.relpath(path, REPO)}:{node.lineno}")
    require_equal(offenders, [],
                  f"der Router importiert die Laufzeit auf Modulebene: {offenders}")


# =====================================================================
# 2 — Die eine Wirkstelle
# =====================================================================

def t_the_router_calls_capabilities_execute_in_exactly_one_module():
    """Ein zweiter Wirkkanal waere ein zweiter Autoritaetsweg.

    Gezaehlt wird ein `execute` auf einem Empfaenger, der nach dem
    Faehigkeitsrouter aussieht — SQL-`execute` im Buch ist etwas anderes und
    hat mit dieser Naht nichts zu tun.
    """
    offenders = []
    for path in _modules(COGNITION_DIR):
        tree = ast.parse(open(path, encoding="utf-8").read())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "execute"):
                continue
            receiver = node.func.value
            name = receiver.id if isinstance(receiver, ast.Name) else (
                receiver.attr if isinstance(receiver, ast.Attribute) else "")
            lowered = name.lower()
            if "capabilit" in lowered or "router" in lowered:
                offenders.append(os.path.basename(path))
    require_equal(sorted(set(offenders)), ["router.py"],
                  f"`capabilities.execute` steht ausserhalb von router.py: "
                  f"{set(offenders)}")


def t_the_route_table_is_the_whole_capability_vocabulary():
    """`memory_*`, `secret_*`, Zahlungsnamen und `background_*` sind keine
    Routen — und damit unerreichbar, nicht bloss unerwuenscht."""
    from solvio.cognition.router import ROUTE_TARGET

    ziele = set(ROUTE_TARGET.values())
    require_equal(sorted(ziele),
                  ["agent_task_build", "agent_task_research", "bot_consult",
                   "research_quick", "system_diagnose"],
                  f"die Routentabelle nennt etwas anderes: {sorted(ziele)}")
    for verboten in ("memory_remember", "memory_forget", "secret_read",
                     "payment_execute", "background_create", "system_heal",
                     "approval_policy_set", "device_revoke"):
        require(verboten not in ziele, f"{verboten} ist als Route erreichbar")

    # Und die Gegenprobe im Quelltext: eine verbotene Faehigkeit darf nirgends
    # im Paket als Zeichenkette STEHEN, die man aufrufen koennte. Geprueft
    # werden Konstanten, nicht Prosa — ein Docstring, der sagt „`memory_*` ist
    # keine Route", ist die Dokumentation dieser Grenze und nicht ihr Bruch.
    verbotene = ("memory_remember", "memory_forget", "memory_search",
                 "secret_read", "secret_write", "payment_execute",
                 "payment_intent_prepare", "background_create",
                 "proactive_create", "system_heal", "approval_policy_set",
                 "device_revoke", "vault_admin")
    treffer = []
    for path in _modules(COGNITION_DIR):
        baum = ast.parse(open(path, encoding="utf-8").read())
        docstrings = set()
        for knoten in ast.walk(baum):
            if isinstance(knoten, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                   ast.AsyncFunctionDef)):
                erste = ast.get_docstring(knoten, clean=False)
                if erste is not None:
                    docstrings.add(erste)
        for knoten in ast.walk(baum):
            if not (isinstance(knoten, ast.Constant)
                    and isinstance(knoten.value, str)):
                continue
            if knoten.value in docstrings:
                continue
            for name in verbotene:
                if name in knoten.value:
                    treffer.append(f"{os.path.basename(path)}:"
                                   f"{knoten.lineno}: {name}")
    require_equal(treffer, [],
                  f"eine verbotene Faehigkeit steht als Zeichenkette da: {treffer}")


def t_the_work_surface_is_a_tool_and_never_a_capability():
    """Eine Faehigkeit ohne Eintrag in der Klassenliste ist `UNCLASSIFIED`.

    Das bedeutet Face ID aus JEDER Herkunft — auch fuer „das ist Gespraech".
    Deshalb ist `solvio_task` ein Werkzeug und wird nie am Vertrag registriert.
    """
    from solvio.capabilities.policy import ACTION_CLASS, VERY_CRITICAL_BY_BIRTH
    from solvio.tools.cognition_tools import TOOL_NAME

    require(TOOL_NAME not in ACTION_CLASS,
            "solvio_task steht in der Klassenliste — dann ist es eine "
            "Faehigkeit geworden")
    require(TOOL_NAME not in VERY_CRITICAL_BY_BIRTH, "und auch dort nicht")
    quelle = open(os.path.join(SRC, "solvio", "tools", "registry.py"),
                  encoding="utf-8").read()
    require("capabilities.register" not in quelle.split("attach_cognition")[1][:4000],
            "attach_cognition registriert eine Faehigkeit")


# =====================================================================
# 3 — Kein Stichwort entscheidet
# =====================================================================

#: Aeusserungen ueber alle acht Wege, jede zweimal verschieden gesagt. Keine
#: davon enthaelt ein Routing-Kommando.
NATUERLICHE_AEUSSERUNGEN = (
    "Wie spaet ist es eigentlich?",
    "Was haeltst du davon?",
    "Kannst du das mal machen?",
    "Ich weiss nicht genau, was ich brauche.",
    "Wie soll ich das grundsaetzlich angehen?",
    "Ueberleg dir das mal in Ruhe.",
    "Wann kam Debian 13 raus?",
    "Sag mir kurz, wie viel das kostet.",
    "Was weisst du ueber mein Projekt?",
    "Frag mal jemanden, der sich damit auskennt.",
    "Warum spinnt SOLVIO gerade?",
    "Bei dir laeuft was nicht rund.",
    "Verschaff mir einen Ueberblick und melde dich, wenn du durch bist.",
    "Geh dem mal gruendlich nach, das dauert bestimmt.",
    "Mach mir daraus eine funktionierende Loesung.",
    "Schau, warum der Fehler auftritt, und behebe ihn.",
)


def t_no_utterance_is_routed_by_the_words_it_happens_to_contain():
    """Kein Codepfad darf eine Aeusserung nach ihrem Wortlaut sortieren.

    Der Weg zur Route laeuft ueber eine strukturierte Einschaetzung, nicht
    ueber Mustervergleich. Dieser Test sucht die Gegenprobe im gesamten Baum:
    gibt es irgendwo eine Stichwortliste, die ueber den Router entscheidet?
    """
    ausloeser = ("recherche", "kurzrecherche", "nachdenken", "klaerung",
                 "fachbot", "diagnose", "auftrag", "kein auftrag",
                 "auftrag_recherche", "auftrag_bau", "solvio_task")
    treffer: list[str] = []
    for path in _modules(os.path.join(SRC, "solvio")):
        quelle = open(path, encoding="utf-8").read()
        if "solvio_task" not in quelle and "cognition" not in quelle:
            continue
        baum = ast.parse(quelle, filename=path)
        for knoten in ast.walk(baum):
            # `"recherche" in text` — ein Wortvergleich, der etwas entscheidet.
            if isinstance(knoten, ast.Compare) and any(
                    isinstance(op, ast.In) for op in knoten.ops):
                links = knoten.left
                if isinstance(links, ast.Constant) and isinstance(links.value, str):
                    if links.value.lower() in ausloeser:
                        treffer.append(f"{os.path.basename(path)}:"
                                       f"{knoten.lineno}: {links.value!r}")
    require_equal(treffer, [],
                  f"Ausloeserwoerter entscheiden ueber den Router: {treffer}")


def t_no_natural_utterance_is_special_cased_anywhere():
    """Keine der sechzehn Aeusserungen steht als Zeichenkette im Quelltext."""
    treffer = []
    for path in _modules(COGNITION_DIR):
        quelle = open(path, encoding="utf-8").read().lower()
        for satz in NATUERLICHE_AEUSSERUNGEN:
            probe = satz.lower().rstrip("?.!")
            if probe in quelle:
                treffer.append(f"{os.path.basename(path)}: {satz!r}")
    require_equal(treffer, [],
                  f"eine Formulierung steht als Sonderfall im Code: {treffer}")


def t_the_instruction_draws_the_line_between_solvio_and_the_house():
    """Gemessen im Schatten, nicht ausgedacht.

    „Warum oeffnet mein Nuki nicht mehr automatisch?" landete mit 0.95
    Zuversicht auf `diagnose`. Der Einschaetzer hat nicht falsch GELESEN — ihm
    fehlte die Grenze: `diagnose` hiess „SOLVIO selbst funktioniert nicht wie
    erwartet" und zog damit auch Geraete an, die nicht SOLVIO sind. Und
    `kein_auftrag` sprach nur vom ANTWORTEN, nicht vom Nachsehen.

    Der Vertrag hat den Fall laengst: §4 nennt Geraetesteuerung ausdruecklich
    als schnellen Weg, und `kein_auftrag` als die Selbstheilung fuer genau
    diese Ueberbeauftragung. Es fehlte keine Route — es fehlte eine Definition.

    Dieser Test haelt die GRENZE fest, nicht die Formulierung: kein Geraetename,
    keine Ausloeseliste, nichts, was nur diesen einen Satz trifft.
    """
    from solvio.cognition.prompt import INSTRUCTION

    diagnose = INSTRUCTION.split("diagnose — ", 1)[1].split("\n\n", 1)[0]
    require("SELBST" in diagnose,
            "die Diagnose grenzt sich nicht auf SOLVIOs eigene Teile ein")
    require("NICHT dafuer" in diagnose,
            "die Diagnose sagt nicht, was NICHT dazugehoert — und ein Weg ohne "
            "Rand zieht an, wofuer er nicht gedacht ist")
    require("Haus" in diagnose,
            "die Grenze zum Haus ist nicht benannt")

    kein = INSTRUCTION.split("kein_auftrag — ", 1)[1].split("\n\n", 1)[0]
    require("nachsehen" in kein,
            "`kein_auftrag` spricht nur vom Antworten — dann wirkt jede Sache, "
            "die man erst ansehen muss, wie ein Auftrag")
    require("Haus" in kein, "das Haus gehoert ausdruecklich hierher")
    require("Im Zweifel dieser Weg" in kein,
            "die Vorfahrt gegen Ueberbeauftragung fehlt")

    # Und die Gegenprobe: die Korrektur darf keine Geraeteliste geworden sein.
    for verboten in ("nuki", "hue", "shelly", "zigbee", "home assistant",
                     "homeassistant", "entity_id", "schloss", "tuer"):
        require(verboten not in INSTRUCTION.lower(),
                f"die Anweisung nennt ein konkretes Geraet: {verboten!r} — das "
                f"waere Keyword-Routing mit anderen Mitteln")


def t_the_assessor_instruction_names_kinds_of_goal_not_phrases():
    from solvio.cognition.prompt import INSTRUCTION

    for satz in NATUERLICHE_AEUSSERUNGEN:
        require(satz not in INSTRUCTION,
                f"die Anweisung ist eine Ausloeseliste geworden: {satz!r}")
    require("Du entscheidest NICHT" in INSTRUCTION,
            "die Anweisung sagt nicht, was der Einschaetzer NICHT entscheidet")
    for wort in ("erlaubt", "Freigabe", "riskant"):
        require(wort in INSTRUCTION, f"die Abgrenzung nennt {wort!r} nicht")


def t_the_realtime_instruction_bounds_the_diagnosis_to_solvio_itself():
    """Live gefunden, und es wirkt AUCH bei `cognitive_router_mode=off`.

    „Kannst du mal ueberpruefen, warum ich mein Nuki zu Hause nicht nutzen
    kann?" — das Modell nahm `system_diagnose`, bekam „bei mir ist alles in
    Ordnung", und riet danach ueber ein fremdes Schloss: Verbindung, Bridge,
    Batterie. Es hat NICHT nachgesehen, obwohl es die Haus-Werkzeuge hat.

    Die Anweisung hiess „Fragt der Nutzer, warum etwas nicht geht, nutze
    system_diagnose" — ohne Rand. Das ist ein Produktdefekt der Sprachschicht
    und gehoert nicht dem Router: er steht in BEIDEN Anweisungsfassungen und
    wirkt auch, wenn der Router gar nicht existiert.
    """
    from solvio.realtime.core_server import (COGNITION_TOOL_INSTRUCTIONS,
                                             TOOL_INSTRUCTIONS)

    for name, anweisung in (("aus", TOOL_INSTRUCTIONS),
                            ("aktiv", COGNITION_TOOL_INSTRUCTIONS)):
        require("an DIR selbst" in anweisung,
                f"[{name}] die Diagnose ist nicht auf SOLVIO selbst begrenzt")
        require("NICHT deine Diagnose" in anweisung,
                f"[{name}] der Rand zum Haus fehlt")
        require("rate nicht" in anweisung,
                f"[{name}] es steht nicht da, dass Raten verboten ist — genau "
                f"das war der Live-Befund")
        require("keine Faehigkeit" in anweisung,
                f"[{name}] der ehrliche Ausweg fehlt")
        require("system_diagnose" in anweisung and "system_heal" in anweisung,
                f"[{name}] die zwei Wege sind verschwunden")
        # Auf WORTGRENZEN, nicht auf Teilzeichenketten. Die erste Fassung
        # dieser Pruefung suchte `"schloss" in text` und schlug an
        # „abgeschlossen" an — im komponierten Produktivstand, wo der
        # Deep-Strang genau dieses Wort in die Anweisung schreibt. Ein Test,
        # der bei einer harmlosen Silbe rot wird, erzieht dazu, ihn zu
        # ignorieren.
        import re as _re
        for geraet in ("nuki", "hue", "shelly", "zigbee", "bridge", "schloss",
                       "thermostat", "rollladen"):
            require(_re.search(rf"\b{geraet}\b", anweisung.lower()) is None,
                    f"[{name}] die Anweisung nennt ein Geraet: {geraet!r}")


def t_the_shadow_observer_writes_no_transcript_into_the_book():
    """Breiter beobachten heisst nicht mehr speichern.

    Der Turn wird fuer die Anfrage TRANSIENT verarbeitet und wandert nicht ins
    Buch: dort steht ein Abdruck und ein Verweis.
    """
    from solvio.cognition import ledger as L

    quelle = open(os.path.join(COGNITION_DIR, "router.py"), encoding="utf-8").read()
    beobachter = quelle.split("async def observe_turn", 1)[1].split("\n    async def", 1)[0]
    for feld in ("user_text=user_text", "text=user_text"):
        require(f"RoutingDecision(\n            decision_id" not in beobachter
                or feld not in beobachter.split("RoutingDecision(", 1)[1].split(")", 1)[0],
                "der Aeusserungstext wandert in die Buchzeile")
    require("objective_digest(user_text)" in beobachter,
            "der Abdruck wird nicht aus dem Text gebildet — dann steht "
            "entweder Text drin oder gar nichts")

    spalten = []
    for zeile in L.SCHEMA.splitlines():
        teile = zeile.strip().split()
        if len(teile) >= 2 and teile[1].upper() == "TEXT":
            spalten.append(teile[0].lower())
    for name in spalten:
        require(not name.endswith(("_text", "_transcript", "_prompt",
                                   "_utterance", "_answer")),
                f"eine Spalte sieht nach Rohtext aus: {name}")
    require("observed_kind" in spalten,
            "die Art des Beobachteten fehlt — dann ist ein Turn ohne Werkzeug "
            "nicht von einem ungemessenen Turn zu unterscheiden")


# =====================================================================
# 4 — Der Broker: wer das grosse Modell erreicht
# =====================================================================

def _ask(harness, principal: str, model: str):
    async def go():
        token = harness.broker.register_principal(principal)
        harness.broker.open_lease(principal, "t", deadline=__import__("time").time() + 60)
        return await harness.call(token, body=responses_body(model=model))
    return go


def t_the_cage_cannot_request_the_large_model():
    """`gpt-5.4` erreicht den Hermes-Kaefig strukturell nie.

    Nicht, weil niemand ihn nennt — sondern weil das Modelltor die Liste DES
    AUFTRAGGEBERS liest.
    """
    async def go():
        async with Harness() as h:
            import time as _t
            for principal in ("deep-gateway", "bot:solvio-researcher",
                              "agent-runtime", "cognitive-router"):
                token = h.broker.register_principal(principal)
                h.broker.open_lease(principal, "t", deadline=_t.time() + 60)
                status, payload = await h.call(
                    token, body=responses_body(model="gpt-5.4"))
                require_equal(status, 403,
                              f"{principal} bekam das grosse Modell")
                code = json.loads(payload)["error"]["code"]
                require_equal(code, "model_not_allowed",
                              f"{principal}: der Grund stimmt nicht")
            require_equal(len(h.upstream.calls), 0,
                          "und zwar OHNE einen Anruf nach draussen")
            gruende = [r["denied_reason"] for r in h.rows()]
            require_equal(gruende.count("model_not_allowed"), 4,
                          "jede Absage steht im Buch")
    run(go())


def t_only_the_two_escalation_principals_pass_the_large_model():
    async def go():
        async with Harness() as h:
            import time as _t
            for principal in ("cognitive-router-escalation",
                              "agent-runtime-escalation"):
                token = h.broker.register_principal(principal)
                h.broker.open_lease(principal, "t", deadline=_t.time() + 60)
                status, _ = await h.call(token,
                                         body=responses_body(model="gpt-5.4"))
                require_equal(status, 200, f"{principal} kam nicht durch")
            require_equal(len(h.upstream.calls), 2, "beide erreichten den Anbieter")
    run(go())


def t_an_escalation_principal_cannot_request_the_small_model():
    """Die Liste ist eine MENGE, keine Untergrenze. Wer gross darf, darf gross —
    und der Weg fuer klein bleibt der kleine Auftraggeber, mit seinem Buch und
    seiner Kappe."""
    async def go():
        async with Harness() as h:
            import time as _t
            token = h.broker.register_principal("cognitive-router-escalation")
            h.broker.open_lease("cognitive-router-escalation", "t",
                                deadline=_t.time() + 60)
            status, payload = await h.call(
                token, body=responses_body(model="gpt-5.4-mini"))
            require_equal(status, 403, "die Menge ist geschlossen in beide Richtungen")
            require_equal(json.loads(payload)["error"]["code"], "model_not_allowed",
                          "der Grund stimmt")
    run(go())


def t_the_model_list_is_filtered_per_principal():
    """Die eine authentifizierte, lease-freie Route darf kein Fenster verraten.

    Sonst waere die Zusage „geschlossene Menge JE AUFTRAGGEBER" ausgerechnet
    dort unwahr, wo ein Kaefig die Fensterlaengen abfragt.
    """
    async def go():
        async with Harness() as h:
            token = h.broker.register_principal("deep-gateway")
            status, payload = await h.call(token, path="/v1/models", method="GET")
            require_equal(status, 200, "die Liste kommt")
            namen = [row["id"] for row in json.loads(payload)["data"]]
            require_equal(namen, ["gpt-5.4-mini"],
                          f"der Kaefig sieht mehr als er darf: {namen}")

            gross = h.broker.register_principal("cognitive-router-escalation")
            status, payload = await h.call(gross, path="/v1/models", method="GET")
            eintraege = json.loads(payload)["data"]
            require_equal([row["id"] for row in eintraege], ["gpt-5.4"],
                          "der Eskalations-Auftraggeber sieht genau seines")
            require_equal(eintraege[0]["context_length"], 1_050_000,
                          "mit der GEMESSENEN Fensterlaenge")
            require(len(eintraege) >= 1,
                    "eine leere Liste braeche die Fensteraufloesung stillschweigend")
    run(go())


def t_the_escalation_caps_never_borrow_from_the_mini_principals():
    from solvio.provider_broker import session as sess

    require(sess.COGNITION_ESCALATION_CAPS.tokens_per_day
            < sess.COGNITION_CAPS.tokens_per_day,
            "die Eskalationskappe ist nicht enger als die kleine")
    require_equal(sess.COGNITION_ESCALATION_CAPS.max_inflight, 1,
                  "zwei Bahnen, die gleichzeitig eskalieren, reihen sich")
    require_equal(sess.AGENT_ESCALATION_CAPS.max_inflight, 1, "ebenso")
    require_equal(sess.COGNITION_CAPS.allowed_models,
                  frozenset({"gpt-5.4-mini"}), "der kleine darf nur klein")
    require_equal(sess.DEEP_CAPS.allowed_models, frozenset({"gpt-5.4-mini"}),
                  "der Kaefig ebenso")
    require_equal(sess.BOT_CAPS.allowed_models, frozenset({"gpt-5.4-mini"}),
                  "die Bots ebenso")
    require_equal(sess.AGENT_CAPS.allowed_models, frozenset({"gpt-5.4-mini"}),
                  "und der Planer auf seiner Normalstufe ebenso")


# =====================================================================
# 5 — E4 und E5 in der Agentenlaufzeit
# =====================================================================

def t_the_planning_event_catalogue_is_closed_and_bounded():
    from solvio.agent_runtime.planner import _tier_for, _transport_for
    from solvio.cognition.policy import planning_tier
    from solvio.cognition.types import EscalationEvent, ModelTier

    require_equal(planning_tier(0, 1), (ModelTier.MINI, EscalationEvent.NONE),
                  "die erste Planung laeuft klein")
    require_equal(planning_tier(0, 2),
                  (ModelTier.LARGE, EscalationEvent.PLAN_REPAIR),
                  "E4: die eine Nachfrage laeuft gross")
    require_equal(planning_tier(1, 1), (ModelTier.MINI, EscalationEvent.NONE),
                  "die erste Nachplanung faengt wieder klein an")
    require_equal(planning_tier(2, 1),
                  (ModelTier.LARGE, EscalationEvent.SECOND_REPLANNING),
                  "E5: ab der zweiten Nachplanung beide Aufrufe gross")
    require_equal(planning_tier(2, 2),
                  (ModelTier.LARGE, EscalationEvent.SECOND_REPLANNING), "beide")

    require_equal(_transport_for(_tier_for(0, 2)),
                  ("agent-runtime-escalation", "gpt-5.4"),
                  "und das grosse Modell nur unter dem Eskalations-Auftraggeber")
    require_equal(_transport_for(_tier_for(0, 1)),
                  ("agent-runtime", "gpt-5.4-mini"), "sonst der Normalweg")


def t_the_worst_case_stays_inside_the_six_call_budget():
    """Vier grosse Aufrufe je Lauf, im UNVERAENDERTEN Sechserbudget."""
    from solvio.agent_runtime.budget import MAX_PLANNER_CALLS_PER_RUN
    from solvio.cognition.policy import planning_tier
    from solvio.cognition.types import ModelTier

    gross = 0
    gesamt = 0
    for ordinal in (0, 1, 2):
        for attempt in (1, 2):
            gesamt += 1
            if planning_tier(ordinal, attempt)[0] is ModelTier.LARGE:
                gross += 1
    require_equal(gesamt, MAX_PLANNER_CALLS_PER_RUN,
                  "der Katalog erfindet keinen zusaetzlichen Aufruf")
    require_equal(gross, 4, f"hoechstens vier grosse Aufrufe je Lauf, nicht {gross}")


def t_a_capped_escalation_plans_on_mini_instead_of_failing_more_often():
    """Ein gekappter Lauf darf nie OEFTER scheitern als vor diesem Milestone."""
    from solvio.agent_runtime import planner as P

    gesehen: list[str] = []

    class Broker:
        def register_principal(self, name):
            return f"tok-{name}"

        def open_lease(self, name, ref="", *, deadline=0.0):
            gesehen.append(name)
            if name.endswith("-escalation"):
                from solvio.provider_broker.session import CapExceeded
                raise CapExceeded("rate_capped")
            return "lease-1"

        def close_lease(self, lease_id):
            return None

    async def transport(payload, *, token="", port=0):
        gesehen.append(str(payload.get("model")))
        return {"ok": True, "text": json.dumps(
            {"schritte": [], "hinweis": ""}), "tokens": 7}

    async def go():
        planner = P.Planner(broker=Broker(), transport=transport)
        call = await planner._call(
            goal="Etwas herausfinden", scope="research", run_id="ar-1",
            allowed_profiles={"researcher/hermes"}, known_capabilities=set(),
            context="", repair=True, hint="not_json", tier="large")
        require(call.ok, "der gekappte Aufruf lief nicht auf der kleinen Stufe weiter")
        require("agent-runtime-escalation" in gesehen,
                "die Eskalation wurde nicht einmal versucht")
        require("gpt-5.4-mini" in gesehen,
                "der Rueckfall lief nicht auf dem kleinen Modell")
        require("gpt-5.4" not in gesehen,
                "das grosse Modell ging trotz Kappe hinaus")
    run(go())


# =====================================================================
# 6 — Der Merkzettel hat genau ein Zuhause
# =====================================================================

def t_the_pending_start_memo_lives_in_exactly_one_place():
    """Zwei Fassungen desselben Zettels waeren zwei Wahrheiten ueber die eine
    Frage, ob eine Freigabe den Auftrag noch startet.

    Die Liste zaehlt AUFRUFER auf, nicht Fassungen: geschrieben wird der
    Zettel weiterhin an genau einer Stelle (`store.remember_pending_start`),
    und alle Aufrufer nehmen denselben Helfer. Neu dazu kommt
    `realtime/control.py` — der Kontrollsocket. Ohne ihn endete jede
    freigabepflichtige Faehigkeit ueber diesen Weg in der Luft: die
    Anfragekennung starb mit dem Aufrufer, und eine spaetere Freigabe fuehrte
    nichts aus. Genau die Luecke, die dieser Helfer fuer den Werkzeugpfad
    schon geschlossen hatte."""
    treffer = []
    for path in _modules(SRC):
        quelle = open(path, encoding="utf-8").read()
        if "remember_pending_start(" in quelle:
            treffer.append(os.path.relpath(path, SRC))
    require_equal(sorted(treffer),
                  ["solvio/agent_runtime/store.py",
                   "solvio/cognition/router.py",
                   "solvio/contacts_endpoint.py",
                   "solvio/conversation/mail.py",
                   "solvio/everyday_endpoint.py",
                   "solvio/realtime/control.py",
                   "solvio/tools/agent_capability_tools.py",
                   "solvio/tools/communication_capability_tools.py",
                   "solvio/tools/gmail_capability_tools.py",
                   "solvio/tools/mail_followup.py",
                   "solvio/tools/note_capability_tools.py",
                   "solvio/tools/proactive_capability_tools.py"],
                  f"der Merkzettel wird woanders geschrieben: {treffer}")

    # Und der Kontrollsocket nimmt denselben Helfer, statt selbst zu schreiben.
    quelle = open(os.path.join(SRC, "solvio", "realtime", "control.py"),
                  encoding="utf-8").read()
    require("from solvio.tools.agent_capability_tools import remember_pending_start"
            in quelle,
            "der Kontrollsocket schreibt seinen eigenen Zettel")

    # Der App-Kontaktweg muss dieselbe dauerhafte Face-ID-Fortsetzung benutzen.
    quelle = open(os.path.join(SRC, "solvio", "contacts_endpoint.py"),
                  encoding="utf-8").read()
    require("from solvio.tools.agent_capability_tools import remember_pending_start"
            in quelle, "der Kontaktweg schreibt seinen eigenen Zettel")

    # Die Zeitplan-Eingaenge behalten Freigaben im selben Auftragsbuch.
    # Die exakte Aufruferliste bleibt geschlossen; keine zweite Ablage zulassen.
    for relative in ("everyday_endpoint.py", "tools/proactive_capability_tools.py",
                     "tools/mail_followup.py"):
        quelle = open(os.path.join(SRC, "solvio", relative), encoding="utf-8").read()
        require("from solvio.tools.agent_capability_tools import remember_pending_start"
                in quelle, "der Zeitplanweg schreibt seinen eigenen Zettel")

    quelle = open(os.path.join(COGNITION_DIR, "router.py"), encoding="utf-8").read()
    require("from solvio.tools.agent_capability_tools import remember_pending_start"
            in quelle,
            "der Router schreibt seinen eigenen Zettel, statt den Helfer zu nehmen")

    from solvio.cognition.router import CREATION_TARGETS
    from solvio.tools.agent_capability_tools import CREATION_CAPABILITIES
    require_equal(CREATION_TARGETS, CREATION_CAPABILITIES,
                  "zwei verschiedene Mengen — ein Zettel unter einem fremden "
                  "Namen verbraucht die Freigabe des Menschen fuer nichts")
    from solvio.tools.agent_capability_tools import CONTINUABLE_CAPABILITIES
    from solvio.agent_runtime.steps import RESUMABLE_STARTS
    # Zwei Zusaetze zu den Erzeugern, beide mit Grund: das direkte Notizwerkzeug
    # (Notizfortsetzung) und die Wiederaufnahme an einer Nutzergrenze
    # (Agentenauftraege im Gespraechsweg) — beide kosten aus dem Raum und vom
    # lokalen Rechner Face ID, und beide liefen ohne Zettel nach dem
    # Gespraechsende mit erteilter Freigabe ins Leere.
    # Seit Stufe S2 (ADR-0041) auch der Mailversand: er wartet immer auf Face ID,
    # und eine nach dem Gespraechsende erteilte Freigabe darf nicht ins Leere laufen.
    # Kontaktbestaetigung verwendet jetzt dieselbe Fortsetzung nach Face ID.
    require_equal(CONTINUABLE_CAPABILITIES,
                  CREATION_CAPABILITIES | {"note_write", "agent_run_resume",
                                           "gmail_send_draft", "communication_send",
                                           "communication_confirm_binding", "background_create"})
    require_equal(CONTINUABLE_CAPABILITIES, RESUMABLE_STARTS)
    note_source = open(os.path.join(SRC, "solvio/tools/note_capability_tools.py"),
                       encoding="utf-8").read()
    require("from solvio.tools.agent_capability_tools import remember_pending_start"
            in note_source, "der Notizadapter muss denselben Helfer verwenden")
    mail_source = open(os.path.join(SRC, "solvio/tools/gmail_capability_tools.py"),
                       encoding="utf-8").read()
    require("from solvio.tools.agent_capability_tools import remember_pending_start"
            in mail_source, "der Mailadapter muss denselben Helfer verwenden")
    contact_source = open(os.path.join(SRC, "solvio/tools/communication_capability_tools.py"),
                          encoding="utf-8").read()
    require("from solvio.tools.agent_capability_tools import remember_pending_start"
            in contact_source, "der Kontaktadapter muss denselben Helfer verwenden")


def t_the_continuity_arguments_are_absent_from_the_llm_schema():
    from solvio.capabilities.agent import SPECS
    from solvio.tools.agent_capability_tools import _SCHEMAS

    for name in ("agent_task_research", "agent_task_build"):
        vertrag = set(SPECS[name].input_schema["properties"])
        modell = set(_SCHEMAS[name]["parameters"]["properties"])
        for feld in ("conversation_ref", "predecessor"):
            require(feld in vertrag, f"{name}: {feld} fehlt im Vertrag")
            require(feld not in modell,
                    f"{name}: {feld} steht im Schema, das das Modell sieht")
        require(feld not in SPECS[name].input_schema["required"],
                f"{name}: ein optionales Feld wurde verpflichtend")


def t_no_schema_of_the_router_declares_an_origin_field():
    """Dieselbe Regel wie fuer jede Faehigkeit: kein Herkunftsfeld, nirgends."""
    from solvio.capabilities.agent import SPECS
    from solvio.cognition.prompt import ASSESSMENT_SCHEMA
    from solvio.tools.cognition_tools import SCHEMA

    verboten = {"origin", "origin_class", "herkunft", "herkunftsklasse",
                "quelle", "commanded", "beauftragt", "trust", "trusted",
                "vertrauen", "authorized", "autorisiert", "approved",
                "freigegeben", "face_id", "interactive_proof", "channel",
                "kanal", "risk", "risiko", "model", "modell", "tier", "stufe",
                "budget"}
    for label, schema in (("solvio_task", SCHEMA["parameters"]),
                          ("assessment", ASSESSMENT_SCHEMA),
                          ("agent_task_research",
                           SPECS["agent_task_research"].input_schema),
                          ("agent_task_build",
                           SPECS["agent_task_build"].input_schema)):
        keys = set(schema.get("properties") or {})
        gefunden = keys & verboten
        require_equal(gefunden, set(),
                      f"{label} deklariert ein Autoritaetsfeld: {gefunden}")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
