"""Die Anbindung an den Router — und die Grenzen, die ein Lauf nicht adressiert.

Vier Fragen, jede mit einer eigenen Antwort im Code:

1. **Stempelt die Laufzeit ehrlich?** Herkunft immer `BACKGROUND_AUTOMATION`,
   Principal `agent:<id>`, Provenienz je Argument — und Spezialistenausgabe
   IMMER `UNTRUSTED_CONTENT`.
2. **Kann ein Lauf seine eigene Familie rufen?** Nein: `agent_task_*`,
   `agent_run_*` und `deep_*` stehen auf der Sperrliste VOR dem Router, und die
   Erzeugungs-Handler verweigern zusaetzlich jede nicht-interaktive Herkunft.
   Zwei unabhaengige Schranken.
3. **Gibt es einen Schreibpfad in Gedaechtnis oder Wissen?** Nein. N4 liest
   persoenlichen Kontext; dessen konkrete Servicezugriffe sind per AST auf
   Suche und aktive Einzelabrufe begrenzt. Schreiben bleibt unverdrahtet.
4. **Bleibt der Rollback moeglich?** Kein Kernmodul importiert
   `solvio.agent_runtime`; ohne den Attach-Aufruf gibt es die Faehigkeiten
   schlicht nicht.
"""
from __future__ import annotations

import ast
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))
from _guard import enforce_assertions, require, require_equal, require_raises  # noqa: E402
enforce_assertions()

_TMP = tempfile.mkdtemp(prefix="solvio-caps-")
os.environ["SOLVIO_STATE_DIR"] = _TMP
os.environ["SOLVIO_AGENT_RUNS_DB"] = os.path.join(_TMP, "agent_runs.sqlite3")

from solvio.agent_runtime import authority as A  # noqa: E402
from solvio.capabilities import agent as CAP  # noqa: E402
from solvio.capabilities.policy import ACTION_CLASS, ActionClass, OriginClass  # noqa: E402
from solvio.security.mobile_approval.execution import READ_ONLY  # noqa: E402

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
RUNTIME_DIR = os.path.join(REPO, "src", "solvio", "agent_runtime")
SRC = os.path.join(REPO, "src")


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
# 1 — die Sperrliste steht VOR dem Router
# =====================================================================

def t_every_blocked_family_is_refused_before_the_router():
    families = {
        "memory_forget": "memory", "memory_purge": "memory",
        "secret_use": "secret", "secret_store": "secret",
        "background_create": "background", "background_run_now": "background",
        "proactive_mark_read": "proactive",
        # Die ganze eigene Familie, nicht zwei Zweige davon: ein kuenftiges
        # `agent_policy_set` fiele sonst durch beide Praefixe.
        "agent_task_research": "agent", "agent_task_build": "agent",
        "agent_run_cancel": "agent", "agent_run_resume": "agent",
        "agent_policy_set": "agent", "agent_budget_set": "agent",
        "deep_research": "deep", "deep_cancel": "deep",
    }
    for name, family in families.items():
        reason = A.is_blocked(name)
        require(reason, f"{name} war nicht gesperrt")
        require(family in reason, f"{name}: falscher Grund {reason}")


def t_the_recursion_block_cannot_be_shortened_without_being_caught():
    """Die Mutation, die die Sperrliste um `agent_task_build` kuerzt, muss
    gefangen werden — sonst gebaert ein Lauf einen Lauf."""
    for name in ("agent_task_research", "agent_task_build",
                 "agent_run_status", "agent_run_cancel", "agent_run_resume"):
        require(A.is_blocked(name), f"{name} ist aus der Sperrliste gefallen")
    require("agent_" in A.BLOCKED_PREFIXES,
            "das Praefix der eigenen Familie fehlt oder wurde verengt")
    # Und ein Name, der nur zufaellig so anfaengt, bleibt erlaubt — eine
    # Sperrliste, die `agentur_termin` faengt, ist zu grob und wird umgangen.
    require_equal(A.is_blocked("agentur_termin"), "",
                  "die Sperrliste ist zu grob geworden")


def t_money_is_blocked_except_the_named_preparation_seam():
    for name in ("purchase_place", "payment_execute", "payment_refund",
                 "purchase_confirm"):
        require(A.is_blocked(name), f"{name} war erreichbar")
    require_equal(A.is_blocked(A.PAYMENT_PREPARE), "",
                  "der Vorschlagsweg wurde mitgesperrt")


def t_a_malformed_or_unknown_name_is_refused_not_guessed():
    for name in ("", "  ", "AGENT_TASK_BUILD", "../etc/passwd", "a", "x" * 200):
        require(A.is_blocked(name), f"'{name[:20]}' passierte die Namenspruefung")


def t_the_guard_raises_instead_of_returning_a_falsy_value():
    """Fail-closed: ein Aufrufer, der den Rueckgabewert ignoriert, soll nicht
    versehentlich weiterlaufen."""
    try:
        A.guard("memory_forget")
    except A.CapabilityBlocked as exc:
        require(exc.name == "memory_forget", "der Name fehlt im Fehler")
        require(exc.reason, "der Grund fehlt im Fehler")
    else:
        require(False, "guard liess einen gesperrten Namen durch")


# =====================================================================
# 2 — die Erzeugungs-Handler verweigern jede nicht-interaktive Herkunft
# =====================================================================

def t_the_creation_origins_exclude_background_and_untrusted():
    require(OriginClass.BACKGROUND_AUTOMATION not in CAP.CREATION_ORIGINS,
            "ein Hintergrundlauf darf Auftraege anlegen — das ist die Rekursion")
    require(OriginClass.EXTERNAL_UNTRUSTED not in CAP.CREATION_ORIGINS,
            "Fremdinhalt darf Auftraege anlegen")
    require(OriginClass.UNSPECIFIED not in CAP.CREATION_ORIGINS,
            "eine unbekannte Herkunft darf Auftraege anlegen")
    require_equal(sorted(o.value for o in CAP.CREATION_ORIGINS),
                  ["local_owner", "room_voice", "trusted_dashboard", "trusted_interactive_app"],
                  "die Herkunftsliste hat sich verschoben")


def t_outside_a_router_call_the_origin_is_unspecified_and_therefore_refused():
    """Fail-closed: ohne Router-Kontext gibt es keine Herkunft, und keine
    Herkunft ist keine Erlaubnis."""
    require_equal(CAP.current_origin(), OriginClass.UNSPECIFIED,
                  "ausserhalb eines Aufrufs entstand eine Herkunft aus dem Nichts")


def t_the_origin_comes_from_the_context_not_from_an_argument():
    """Ein Modell darf die Herkunft weder setzen noch faelschen — sie steht in
    keinem Eingabeschema."""
    for name, spec in CAP.SPECS.items():
        properties = set((spec.input_schema.get("properties") or {}))
        forbidden = {"origin", "trust", "principal", "approval", "commanded"}
        require_equal(sorted(properties & forbidden), [],
                      f"{name} nimmt ein Autoritaetsfeld entgegen")


# =====================================================================
# Die Politik-Einstufung
# =====================================================================

def t_creating_autonomous_work_is_a_write_class():
    """Eine READ_ONLY-Einstufung entwaffnete beide Schranken: `authority_refusal`
    laesst Lesendes aus jeder Herkunft passieren, und selbst
    `EXTERNAL_UNTRUSTED × READ_ONLY` ist in der Matrix DIREKT."""
    require_equal(ACTION_CLASS["agent_task_research"], ActionClass.NORMAL_WRITE,
                  "die Recherche wurde zur Leseklasse")
    require_equal(ACTION_CLASS["agent_task_build"], ActionClass.NORMAL_WRITE,
                  "der Bauauftrag wurde zur Leseklasse")
    require_equal(ACTION_CLASS["agent_run_resume"], ActionClass.NORMAL_WRITE,
                  "die Wiederaufnahme wurde zur Leseklasse")
    # Und die Gegenrichtung: Lesen steht NICHT in der Registry. Es wird aus der
    # serverseitigen Spec abgeleitet — zwei getrennte Wahrheiten ueber dieselbe
    # Faehigkeit laufen frueher oder spaeter auseinander.
    for name in ("agent_run_status", "agent_run_cancel"):
        require(name not in ACTION_CLASS,
                f"{name} steht als Leseklasse in der Registry statt in der Spec")
        require_equal(CAP.SPECS[name].semantics, READ_ONLY,
                      f"{name} ist in der Spec nicht lesend")


def t_every_agent_capability_has_an_approval_label():
    """Ohne Label rendert die Freigabefrage als nackter Faehigkeitsname — und
    ein Mensch bestaetigt etwas, das er nicht gelesen hat."""
    from solvio.capabilities.approval_gateway import ACTION_LABELS
    CAP.register(_FakeRouter(), CAP.AgentCapabilities(None))
    for name in ("agent_task_research", "agent_task_build", "agent_run_resume"):
        require(name in ACTION_LABELS, f"{name} hat keinen Freigabetext")
        headline, fields = ACTION_LABELS[name]
        require(headline and not headline.startswith("agent_"),
                f"{name}: die Ueberschrift ist ein Bezeichner")


class _FakeRouter:
    def __init__(self) -> None:
        self.registered = []

    def register(self, spec, handler):
        self.registered.append(spec.name)


# =====================================================================
# 3 — kein Schreibpfad in Gedaechtnis oder Wissen
# =====================================================================

def _readonly_briefing(source: str):
    """Keine Modulausnahme: Importe UND ihre konkreten Empfaenger pruefen.

    Auch das Weiterreichen/Aliasieren des Stores ist kein Lesezugriff: damit
    koennte eine spaetere Hilfsfunktion ausserhalb dieser Naht schreiben.
    """
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    readers = {"_read", "_task_hits"}
    # Forwarding is safe only to these exact local definitions. Inspect every
    # Python binding form for the two names, not just Name(Store) assignments.
    for name in readers:
        definitions = [node for node in ast.walk(tree)
                       if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                       and node.name == name]
        require_equal(len(definitions), 1, f"mehrdeutige Leserbindung: {name}")
        definition = definitions[0]
        require(isinstance(definition, ast.AsyncFunctionDef) and definition in tree.body
                and not definition.decorator_list, f"ungepruefte Leserdefinition: {name}")
        positional = definition.args.posonlyargs + definition.args.args
        require(positional and positional[0].arg == "service",
                f"Leser umgeht geprueften Serviceempfaenger: {name}")
    expected = {("solvio.memory.adaptive", "lifecycle"),
                ("solvio.memory.intent", "looks_like_secret"),
                ("solvio.memory.service", "MemoryService"),
                # Existing pure tokenizer; neither import constructs a store.
                ("solvio.memory.service", "_WORD"),
                ("solvio.memory.service", "_tokens")}
    found = []

    def attr_path(node):
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return attr_path(node.value) + "." + node.attr
        return ""

    def is_none_check(node, parent):
        return (isinstance(parent, ast.Compare) and parent.left is node
                and len(parent.ops) == 1 and isinstance(parent.ops[0], ast.Is)
                and len(parent.comparators) == 1
                and isinstance(parent.comparators[0], ast.Constant)
                and parent.comparators[0].value is None)

    for node in ast.walk(tree):
        parent = parents.get(node)
        if isinstance(node, ast.Name) and node.id in readers:
            require(isinstance(node.ctx, ast.Load) and isinstance(parent, ast.Call)
                    and parent.func is node, "Leser wird umgebunden oder weitergereicht")
        if isinstance(node, ast.arg):
            require(node.arg not in readers, "Parameter verdeckt lokale Leserbindung")
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            require(not any((a.asname or a.name.split(".")[0]) in readers
                            or a.name == "*" for a in node.names),
                    "Import verdeckt lokale Leserbindung")
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            require(not readers.intersection(node.names), "Leserbindung verlaesst lokalen Scope")
        if isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)):
            require(node.name not in readers, "Ausnahme/Muster verdeckt Leserbindung")
        if isinstance(node, ast.MatchMapping):
            require(node.rest not in readers, "Muster verdeckt Leserbindung")
        if isinstance(node, ast.Import):
            require(not any(a.name.startswith(("solvio.memory", "solvio.knowledge"))
                            for a in node.names), "ungebundener Modulimport")
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                ("solvio.memory", "solvio.knowledge")):
            for alias in node.names:
                binding = (node.module, alias.name)
                require(binding in expected and alias.asname is None,
                        f"ungepruefter Memoryimport: {binding}")
                found.append(binding)
                if alias.name == "MemoryService":
                    require(isinstance(parent, ast.If)
                            and isinstance(parent.test, ast.Name)
                            and parent.test.id == "TYPE_CHECKING" and node in parent.body,
                            "MemoryService darf nur als Typ importiert werden")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            require(node.func.id not in {"MemoryService", "getattr", "setattr", "delattr",
                                         "eval", "exec", "globals", "locals", "__import__"},
                    "Storebau oder dynamischer Zugriff im lesenden Briefing")
        if isinstance(node, ast.Attribute) and attr_path(node).startswith("service."):
            path = attr_path(node)
            require(isinstance(node.ctx, ast.Load), "Serviceattribut wird veraendert")
            if path in {"service.search", "service.semantic.memory.get_visible",
                        # Delegates only to canonical memory.search with
                        # include_superseded=False; no indexing or mutation.
                        "service.semantic.lexical_recall"}:
                require(isinstance(parent, ast.Call) and parent.func is node,
                        f"Lesemethode wird weitergereicht: {path}")
            elif path == "service.semantic.memory":
                require(isinstance(parent, ast.Attribute) and parent.value is node
                        and parent.attr == "get_visible", "Storealias oder Schreibzugriff")
            elif path == "service.semantic":
                require((isinstance(parent, ast.Attribute) and parent.value is node
                         and parent.attr in {"memory", "lexical_recall"})
                        or is_none_check(node, parent),
                        "Semantischer Store wird weitergereicht")
            else:
                require_equal(path, "service.model_load_error", "ungepruefter Servicezugriff")
        if isinstance(node, ast.Name) and node.id == "service":
            require(isinstance(node.ctx, ast.Load), "Servicebindung wird veraendert")
            forwarded_to_reader = (isinstance(parent, ast.Call)
                                   and isinstance(parent.func, ast.Name)
                                   and parent.func.id in readers
                                   and parent.args[0] is node)
            require((isinstance(parent, ast.Attribute) and parent.value is node)
                    or is_none_check(node, parent) or forwarded_to_reader,
                    "Service wird aliasiert oder an ungeprueften Code weitergereicht")
        if isinstance(node, ast.Name) and node.id == "_tokens":
            require(isinstance(node.ctx, ast.Load) and isinstance(parent, ast.Call)
                    and parent.func is node,
                    "Tokenizer wird veraendert, aliasiert oder weitergereicht")
        if isinstance(node, ast.Name) and node.id == "_WORD":
            caller = parents.get(parent)
            require(isinstance(node.ctx, ast.Load) and isinstance(parent, ast.Attribute)
                    and parent.value is node and parent.attr == "findall"
                    and isinstance(caller, ast.Call) and caller.func is parent,
                    "Tokenregex wird veraendert oder ungeprueft verwendet")
        if isinstance(node, ast.Name) and node.id == "lifecycle":
            require(isinstance(parent, ast.Attribute) and parent.value is node
                    and parent.attr in {"lifecycle_of", "evidence_summary", "LEARNED"},
                    "ungepruefter Lifecyclezugriff")
            if parent.attr != "LEARNED":
                caller = parents.get(parent)
                require(isinstance(caller, ast.Call) and caller.func is parent
                        and isinstance(parent.ctx, ast.Load),
                        "Lifecycleleser wird veraendert oder weitergereicht")
    require_equal(sorted(found), sorted(expected), "Memoryimportvertrag veraendert")


def t_runtime_memory_imports_are_only_the_verified_readonly_briefing():
    offenders = []
    for path in _modules(RUNTIME_DIR):
        if path == os.path.join(RUNTIME_DIR, "personal_context.py"):
            _readonly_briefing(open(path, encoding="utf-8").read())
            continue
        for line, module in _imports(path):
            if module.startswith(("solvio.memory", "solvio.knowledge")):
                offenders.append(f"{os.path.basename(path)}:{line} {module}")
    require_equal(offenders, [],
                  f"die Agentenlaufzeit importiert Gedaechtnis/Wissen: {offenders}")


def t_readonly_briefing_guard_rejects_direct_and_aliased_writes():
    source = open(os.path.join(RUNTIME_DIR, "personal_context.py"), encoding="utf-8").read()
    _readonly_briefing(source)
    replaced_reader = source.replace("service.semantic.lexical_recall(",
                                     "service.semantic.remember(")
    require(replaced_reader != source, "die neue konkrete Lesenaht fehlt")
    require_raises(AssertionError, _readonly_briefing, replaced_reader,
                   message="Schreiben anstelle des neuen Themenabrufs passierte")
    for name in ("_read", "_task_hits"):
        for mutation in (
                source + f"\n{name} = external_writer\n",
                source + f"\nasync def {name}(target, query):\n"
                         "    await target.semantic.memory.remember('neu')\n    return []\n",
                source + f"\nfrom external import writer as {name}\n",
                source + f"\nasync def wrong({name}, service):\n    await {name}(service, '')\n",
                source.replace(f"async def {name}(", f"@external_writer\nasync def {name}("),
                source.replace(f"async def {name}(service:", f"async def {name}(target:")):
            require_raises(AssertionError, _readonly_briefing, mutation,
                           message=f"umgebundener oder fremder Leser passierte: {name}")
    for mutation in (
            "\nasync def wrong(service):\n    await service.semantic.memory.remember('neu')\n",
            "\nasync def wrong(service):\n    sink = service.semantic.memory\n    await sink.remember('neu')\n",
            "\nasync def wrong(service):\n    await external_writer(service)\n",
            "\nasync def wrong(service):\n    await service.semantic.remember('neu')\n",
            "\nasync def wrong(service):\n    sink = service.semantic\n    await sink.remember('neu')\n",
            "\nasync def wrong(service):\n    reader = service.semantic.lexical_recall\n    await reader('neu')\n",
            "\n_tokens = external_writer\n",
            "\n_WORD.findall = external_writer\n",
            "\nregex_alias = _WORD\n",
            "\ntokenizer_alias = _tokens\n",
            "\nsummary_alias = lifecycle.evidence_summary\n",
            "\nlifecycle.evidence_summary = external_writer\n",
            "\nasync def wrong(service):\n    await lifecycle.remember(service)\n"):
        require_raises(AssertionError, _readonly_briefing, source + mutation,
                       message="der Lese-Waechter liess einen Schreibweg durch")


def t_the_runtime_names_no_memory_or_knowledge_capability():
    """Auch nicht als Zeichenkette: ein Name, den der Planer nennen koennte,
    waere ein Weg, den die Sperrliste zwar faengt — aber der gar nicht erst
    entstehen soll."""
    for path in _modules(RUNTIME_DIR):
        source = open(path, encoding="utf-8").read()
        tree = ast.parse(source)
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                value = node.value.strip()
                # `knowledge_proposal` ist die SCHRITTART des Vorschlagswegs und
                # gehoert hierher. Ein Faehigkeitsname der beiden Familien nicht.
                if value in ("knowledge_proposal",):
                    continue
                # The offline process runner measures physical RAM. These two
                # closed labels are only raised errors, never capability names.
                call = parents.get(node)
                if (os.path.basename(path) == "file_tool_process.py"
                        and value in {"memory_measurement_unavailable", "memory_limit"}
                        and isinstance(call, ast.Call) and call.args == [node]
                        and isinstance(parents.get(call), ast.Raise)
                        and isinstance(call.func, ast.Attribute) and call.func.attr == "_Refused"
                        and isinstance(call.func.value, ast.Name) and call.func.value.id == "E"):
                    continue
                # The table gate translates exactly that RAM failure to its
                # own closed diagnostic label, without exposing a capability.
                if (os.path.basename(path) == "table_report_contract.py" and value == "memory_limit"
                        and isinstance(call, ast.Dict) and any(key is node
                            and isinstance(mapped, ast.Constant) and mapped.value == "adapter_memory_limit"
                            for key, mapped in zip(call.keys, call.values))):
                    continue
                if value.startswith(("memory_", "knowledge_")) and len(value) > 8:
                    require(False, f"{os.path.basename(path)} nennt {value}")


def t_a_knowledge_proposal_is_an_artifact_not_a_write():
    """Ein Vorschlag erzeugt eine Datei und eine Meldung. Der Wissens-Compiler
    liest weiterhin nur `active_records()` — es gibt keinen Aufrufpfad hinein."""
    source = open(os.path.join(RUNTIME_DIR, "orchestrator.py"), encoding="utf-8").read()
    tree = ast.parse(source)
    func = next(n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "_run_proposal_step")
    body = ast.dump(func)
    require("add_artifact" in body, "der Vorschlag erzeugt kein Artefakt")
    # Geprueft wird der AUFRUF, nicht das Wort: `knowledge_proposal` ist die
    # Schrittart und soll dastehen. Was nicht dastehen darf, ist ein Aufruf in
    # den Wissens- oder Gedaechtnispfad.
    called = {n.func.attr for n in ast.walk(func)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    for forbidden in ("remember", "promote", "compile", "learn", "write_record",
                      "put_record", "add_record"):
        require(forbidden not in called,
                f"der Vorschlagsschritt ruft {forbidden}")


# =====================================================================
# 4 — Rollback und die eine Router-Stelle
# =====================================================================

def t_no_core_module_imports_the_agent_runtime():
    """Die Rollback-Zusage: `SOLVIO_AGENT_RUNTIME=off` (oder ein Revert der
    Attach-Commits) entfernt Faehigkeiten, Endpunkt, Probe und Takt
    vollstaendig. Ein Import auf Modulebene irgendwo im Kern wuerde das
    unmoeglich machen."""
    offenders = []
    for path in _modules(SRC):
        if path.startswith(RUNTIME_DIR):
            continue
        tree = ast.parse(open(path, encoding="utf-8").read())
        # NUR Importe auf Modulebene: `registry.attach_agent_runtime` und der
        # Start in `core_server.serve()` importieren absichtlich INNERHALB einer
        # Funktion — genau das macht den Rollback moeglich, weil der Import erst
        # beim Anhaengen passiert und ohne ihn nie.
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
                  f"ein Kernmodul importiert die Laufzeit auf Modulebene: {offenders}")
    # A helper wrapping an immediate import would evade the AST inventory.
    # The router must also identify the document service in a fresh process
    # where the runtime is genuinely unavailable, before requesting its SPEC.
    import subprocess
    import sys
    probe = '''
import importlib.abc
import sys
class NoRuntime(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "solvio.agent_runtime" or fullname.startswith("solvio.agent_runtime."):
            raise ModuleNotFoundError("runtime intentionally unavailable", name=fullname)
sys.meta_path.insert(0, NoRuntime())
from solvio.capabilities import document_adapter as adapter
from solvio.capabilities.router import _task_service_route
if _task_service_route(object()) is not None:
    raise AssertionError("unknown service received a task route")
if set(adapter._TASK_DOCUMENT_SERVICE_METHODS) != {"resources", "quote", "execute"}:
    raise AssertionError("document service recognition changed")
try:
    adapter.SPEC
except ModuleNotFoundError:
    pass
else:
    raise AssertionError("document contract was copied instead of loaded lazily")
'''
    completed = subprocess.run([sys.executable, "-c", probe], cwd=REPO,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(
            [SRC, os.environ.get("PYTHONPATH", "")])},
        capture_output=True, text=True, timeout=20)
    require_equal(completed.returncode, 0,
                  f"Dokumentdienst braucht die Laufzeit schon beim Import: {completed.stderr}")


def t_origin_is_stamped_in_exactly_one_module():
    """N2 trennt authentifizierte Annahme von spaeteren Hintergrundschritten.

    Der neue HTTP-Eingang darf App/Browser stempeln; Schritte bleiben immer
    BACKGROUND. Die echten Auth-/Faelschungsproben stehen in agent_task_entry.
    """
    # Gemeint ist der STEMPEL, nicht das Wort: `create_task(origin=...)` traegt
    # die Herkunft der ERZEUGUNG in das Buch und ist etwas anderes als die
    # Herkunft, mit der gehandelt wird. Geprueft wird deshalb, wo `OriginClass`
    # ueberhaupt vorkommt — dort faellt die Entscheidung.
    offenders = []
    for path in _modules(RUNTIME_DIR):
        source = open(path, encoding="utf-8").read()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and \
                    isinstance(node.value, ast.Name) and \
                    node.value.id == "OriginClass":
                offenders.append(f"{os.path.basename(path)}:{node.lineno}")
    require_equal(sorted(set(o.split(":")[0] for o in offenders)), ["steps.py", "task_endpoint.py"],
                  f"ungepruefte neue Herkunftsstelle: {offenders}")
    tree = ast.parse(open(os.path.join(RUNTIME_DIR, "steps.py"), encoding="utf-8").read())
    stamped = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
               and isinstance(n.value, ast.Name) and n.value.id == "OriginClass"}
    require_equal(stamped, {"BACKGROUND_AUTOMATION"}, "Schritte geben sich als interaktiver Eingang aus")


def t_the_runtime_calls_router_execute_in_exactly_one_module():
    # `connection.execute(...)` im Buch ist SQL und hat mit dem Router nichts zu
    # tun. Gezaehlt wird nur ein `execute` auf einem Empfaenger, der `router`
    # heisst — das ist die Naht, um die es geht.
    offenders = []
    for path in _modules(RUNTIME_DIR):
        tree = ast.parse(open(path, encoding="utf-8").read())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "execute"):
                continue
            receiver = node.func.value
            name = receiver.id if isinstance(receiver, ast.Name) else (
                receiver.attr if isinstance(receiver, ast.Attribute) else "")
            if "router" in name.lower():
                offenders.append(os.path.basename(path))
    require_equal(sorted(set(offenders)), ["steps.py", "task_endpoint.py"],
                  f"ungepruefte neue Routerstelle: {set(offenders)}")




if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
