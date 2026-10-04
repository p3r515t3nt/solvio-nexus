"""DEBT-0128, Teil 1: der alte Weg ist weg — und kann nicht still zurueckkommen.

Der stillgelegte Weg war nicht theoretisch. `agents/codex_agent.py` hat
`dict(os.environ)` kopiert, den echten `OPENAI_API_KEY` hineingeschrieben und
damit einen `codex`-Unterprozess ohne Seatbelt und mit vollem Netzausgang
gestartet — erreichbar ueber `codex_task`, das dem Sprachmodell mit
`RiskLevel.HARMLESS` exponiert war. Ein ANALYZE-Zuruf per Stimme genuegte.

Eine Loeschung ist keine Zusicherung. Vier Dinge muessen dauerhaft gelten
(ADR-0028, Contract Delta §1, Teil 1) — und genau die stehen hier:

1. die drei Dateien existieren nicht und werden nirgends importiert;
2. im ganzen Baum setzt keine Stelle einen Sperrlisten-Namen in eine
   Kindumgebung, und kein `dict(os.environ)`-Abbild erreicht einen
   Unterprozess;
3. kein LLM-exponiertes Werkzeug und keine Faehigkeit erreicht `codex` anders
   als ueber den Spezialistenstarter;
4. der Systemprompt nennt `codex_confirm` nicht mehr.

Teil 2 (die Ersatznaht ohne wiederverwendbare Abo-Anmeldung fuer
modellgesteuerte Werkzeuge) steht in `test_agent_runtime_isolation.py`. Erst
beide Teile zusammen schliessen die Schuld: ein „API-Schluessel im
Codex-Prozess", der durch eine „Abo-Sitzung fuer beliebige Agentenwerkzeuge"
ersetzt worden waere, waere dieselbe Schuld unter neuem Namen.

Warum die Quellscans tokenisieren statt Text zu suchen: die Dateien, die es
RICHTIG machen, erklaeren ausfuehrlich, warum sie keinen Schluessel weitergeben
— und die Sperrliste selbst besteht aus genau diesen Namen als Zeichenketten.
Eine Wortsuche ueber den Rohtext schluege also bei der Loesung an und erzoege
dazu, weniger zu erklaeren.
"""
from __future__ import annotations

import ast
import io
import os
import sys
import tokenize

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))
from _guard import enforce_assertions, require, require_equal  # noqa: E402
enforce_assertions()

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(REPO, "src")
SCRIPTS = os.path.join(REPO, "scripts")

#: Die Dateien, deren Verschwinden das halbe Schliess-Kriterium IST.
RETIRED = (
    "src/solvio/agents/codex_agent.py",
    "src/solvio/tools/codex_tool.py",
    "src/solvio/agents/pending.py",
)

#: Namen, die in KEINER Kindumgebung stehen duerfen. Das ist die Sperrliste des
#: Starters — hier noch einmal als eigenstaendige Wahrheit, damit ein Test nicht
#: gruen wird, indem jemand die Sperrliste kuerzt.
FORBIDDEN_ENV = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "CLAUDE_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_ORGANIZATION",
    "CODEX_API_KEY", "AZURE_OPENAI_API_KEY",
    "HOME_ASSISTANT_TOKEN", "HOME_ASSISTANT_URL",
    "GOOGLE_CALENDAR_CLIENT_SECRET", "GOOGLE_CALENDAR_REFRESH_TOKEN",
    "GOOGLE_CALENDAR_CLIENT_ID",
)

#: Die Funktionen, mit denen in diesem Baum ueberhaupt ein Prozess entsteht.
SPAWN_NAMES = (
    "create_subprocess_exec", "create_subprocess_shell",
    "Popen", "run", "call", "check_call", "check_output",
    "spawnv", "spawnve", "spawnl", "spawnle", "execve", "execvpe",
)


def _python_files(*roots: str) -> list[str]:
    out: list[str] = []
    for root in roots:
        for base, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in ("__pycache__", ".venv", "node_modules")]
            for name in sorted(files):
                if name.endswith(".py"):
                    out.append(os.path.join(base, name))
    return sorted(out)


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _code_only(path: str) -> str:
    """Der Quelltext ohne Kommentare und Zeichenketten — siehe Modul-Docstring."""
    kept: list[str] = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(_read(path)).readline):
            if token.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            kept.append(token.string)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return _read(path)
    return " ".join(kept)


def _rel(path: str) -> str:
    return os.path.relpath(path, REPO)


# =====================================================================
# 1 — die Dateien sind fort, und niemand importiert sie
# =====================================================================

def t_the_retired_files_do_not_exist():
    """Die erste Haelfte ist buchstaeblich: es gibt sie nicht mehr."""
    present = [path for path in RETIRED if os.path.exists(os.path.join(REPO, path))]
    require_equal(present, [], f"stillgelegte Dateien sind wieder da: {present}")


def t_the_agents_package_is_gone_entirely():
    """`agents/` hatte nach der Stilllegung keinen Zweck mehr — auch keinen leeren.

    Geprueft wird der Inhalt, nicht das Verzeichnis: `git rm` laesst ein leeres
    Verzeichnis stehen, git verfolgt es nicht, und ein Arbeitsbaum, in dem es
    aus Versehen herumsteht, ist kein Sicherheitsbefund. Ein `.py` darin waere
    einer — auch ein leeres `__init__.py`, denn dann gaebe es das Paket wieder.
    """
    folder = os.path.join(SRC, "solvio", "agents")
    modules = [_rel(p) for p in _python_files(folder)] if os.path.isdir(folder) else []
    require_equal(modules, [], f"das agents-Paket traegt wieder Code: {modules}")


def t_nothing_in_the_tree_imports_the_retired_modules():
    """Ein Import auf eine geloeschte Datei waere ein Absturz — ein Import auf eine
    WIEDERGEKEHRTE waere die Schuld zurueck. Der Scan faengt beides."""
    dead = ("solvio.agents", "solvio.tools.codex_tool")
    offenders: list[str] = []
    for path in _python_files(SRC, SCRIPTS, os.path.join(REPO, "tests")):
        try:
            tree = ast.parse(_read(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if any(alias.name == d or alias.name.startswith(d + ".") for d in dead):
                        offenders.append(f"{_rel(path)}:{node.lineno} import {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                if any(node.module == d or node.module.startswith(d + ".") for d in dead):
                    offenders.append(f"{_rel(path)}:{node.lineno} from {node.module}")
    require_equal(offenders, [], f"stillgelegte Module werden importiert: {offenders}")


def t_the_registry_builds_without_the_codex_block():
    """Der Dispatcher darf den Agenten nicht einmal mehr als Attribut kennen —
    ein `getattr(d, 'codex_agent', None)` waere die Stelle, an der ein Rueckbau
    beginnt."""
    from solvio.tools import registry as R
    source = _code_only(os.path.join(SRC, "solvio", "tools", "registry.py"))
    for token in ("CodexAgent", "CodexTaskTool", "CodexConfirmTool",
                  "CODEX_WORKSPACES", "CODEX_BIN", "codex_agent"):
        require(token not in source, f"registry.py nennt {token} noch")
    require(not hasattr(R, "CODEX_WORKSPACES"), "CODEX_WORKSPACES lebt noch")


# =====================================================================
# 2 — kein Schluessel und keine ganze Umgebung in einem Kindprozess
# =====================================================================

def _env_kwarg_sources(tree: ast.AST) -> list[tuple[int, ast.AST]]:
    """Jedes `env=<ausdruck>` an einem Prozessstart, mit Zeilennummer."""
    found: list[tuple[int, ast.AST]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.attr if isinstance(node.func, ast.Attribute) else (
            node.func.id if isinstance(node.func, ast.Name) else "")
        if name not in SPAWN_NAMES:
            continue
        for keyword in node.keywords:
            if keyword.arg == "env":
                found.append((node.lineno, keyword.value))
    return found


def t_no_process_start_receives_a_copy_of_the_whole_environment():
    """Der Kern von DEBT-0128, als dauerhafte Regel.

    `env=dict(os.environ)` und `env=os.environ.copy()` sind die beiden Formen,
    in denen die vollstaendige Prozessumgebung — samt Anbieterschluessel — in ein
    fremdes Programm wandert. Beide sind hier verboten, egal von wem.

    Ausdruecklich NICHT geprueft wird ein Start ganz OHNE `env=`: der erbt
    implizit und ist ein benanntes, vorbestehendes Residuum (Contract Delta §1)
    — unter launchd traegt die Elternumgebung den Schluessel nicht.
    """
    offenders: list[str] = []
    for path in _python_files(SRC, SCRIPTS):
        try:
            tree = ast.parse(_read(path))
        except SyntaxError:
            continue
        for lineno, value in _env_kwarg_sources(tree):
            text = ast.dump(value)
            copies_environ = (
                # dict(os.environ) / dict(environ)
                (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                 and value.func.id == "dict"
                 and any("environ" in ast.dump(arg) for arg in value.args))
                # os.environ.copy()
                or (isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute)
                    and value.func.attr == "copy" and "environ" in ast.dump(value.func.value))
                # env=os.environ  (aliasing the live mapping)
                or (isinstance(value, ast.Attribute) and value.attr == "environ")
                # {**os.environ, ...}
                or (isinstance(value, ast.Dict) and any(
                    key is None and "environ" in ast.dump(val)
                    for key, val in zip(value.keys, value.values)))
            )
            if copies_environ:
                offenders.append(f"{_rel(path)}:{lineno} env={text[:60]}")
    require_equal(offenders, [],
                  f"ein Unterprozess bekaeme die ganze Umgebung: {offenders}")


def t_no_source_line_writes_a_forbidden_name_into_a_child_environment():
    """Ein Subscript-Schreibzugriff auf ein env-Dict mit einem Sperrlisten-Namen.

    Die alte Zeile war woertlich `env["OPENAI_API_KEY"] = self.api_key`. Diese
    Gestalt — Zuweisung an ein Subscript, dessen Schluessel ein Sperrlisten-Name
    ist — ist im ganzen Baum verboten. `os.environ[...] = ...` faellt mit
    darunter: den Schluessel in die EIGENE Umgebung zu schreiben ist genau der
    Weg, ihn danach implizit zu vererben.

    **Die eine Ausnahme, und warum sie eine ist (V0.6):** der gemakelte
    Claude-Builder bekommt `ANTHROPIC_BASE_URL` und `ANTHROPIC_API_KEY`
    gesetzt — mit Werten, die NICHT aus `os.environ` stammen, sondern aus dem
    Aufruf: die Rueckschleife zum eigenen Broker und ein kurzlebiges
    Broker-Token, das ausserhalb dieser Rueckschleife nichts oeffnet.
    Der Zweck der Sperrliste ist, dass die Umgebung des CORES nicht
    durchsickert; ein Wert, der dort nie stand, kann nicht durchsickern.

    Die Ausnahme ist deshalb **an genau eine Funktion gebunden** und wird HIER
    geprueft: sie muss `brokered_environment` in `launcher.py` heissen. Wer den
    Namen anderswo verwendet, faellt weiter auf.
    """
    #: Genau eine Funktion darf es, und nur in dieser Datei.
    ERLAUBT = ("src/solvio/specialists/launcher.py", "brokered_environment")

    offenders: list[str] = []
    for path in _python_files(SRC, SCRIPTS):
        try:
            tree = ast.parse(_read(path))
        except SyntaxError:
            continue
        # Welche Zeilen liegen in der einen erlaubten Funktion?
        erlaubte_zeilen: set[int] = set()
        if _rel(path) == ERLAUBT[0]:
            for node in ast.walk(tree):
                if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and node.name == ERLAUBT[1]):
                    erlaubte_zeilen = set(range(
                        node.lineno, (node.end_lineno or node.lineno) + 1))
        for node in ast.walk(tree):
            targets: list = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                targets = [node.target]
            for target in targets:
                if not isinstance(target, ast.Subscript):
                    continue
                key = target.slice
                if not (isinstance(key, ast.Constant)
                        and key.value in FORBIDDEN_ENV):
                    continue
                if node.lineno in erlaubte_zeilen:
                    continue
                offenders.append(f"{_rel(path)}:{node.lineno} -> {key.value}")
    require_equal(offenders, [],
                  f"ein Sperrlisten-Name wird in eine Umgebung geschrieben: {offenders}")


def t_the_one_exception_never_reads_the_forbidden_name_from_the_environment():
    """Die Gegenprobe zur Ausnahme: sie darf setzen, nicht abschreiben.

    `brokered_environment` darf `ANTHROPIC_*` schreiben — aber nur mit Werten
    aus dem Aufruf. Liest sie einen dieser Namen aus `os.environ`, waere sie
    genau der Vererbungsweg, gegen den die Sperrliste geschrieben wurde.
    """
    import inspect

    from solvio.specialists import launcher as L

    quelle = inspect.getsource(L.brokered_environment)
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"):
        require(f'environ["{name}"]' not in quelle
                and f"environ.get(\"{name}\"" not in quelle,
                f"{name} wird aus der eigenen Umgebung abgeschrieben")
    require("child_environment()" in quelle,
            "die Ausnahme baut nicht auf der gesaeuberten Umgebung auf")

    # Und am laufenden Code: ein gesetzter Elternwert kommt NICHT durch.
    import os as _os
    alt = _os.environ.get("ANTHROPIC_API_KEY")
    _os.environ["ANTHROPIC_API_KEY"] = "sk-ant-TEST-ELTERNWERT-NOT-A-REAL"
    try:
        env = L.brokered_environment(base_url="http://127.0.0.1:8792",
                                     token="broker-token-x")
    finally:
        if alt is None:
            _os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            _os.environ["ANTHROPIC_API_KEY"] = alt
    require_equal(env["ANTHROPIC_API_KEY"], "broker-token-x",
                  "der Elternwert kam in die Kindumgebung")


def t_the_launcher_denylist_still_covers_every_forbidden_name():
    """Der Mechanismus selbst — der Test oben waere wertlos, wenn die Sperrliste schrumpft."""
    from solvio.specialists.launcher import DENIED_ENV
    missing = [name for name in FORBIDDEN_ENV if name not in DENIED_ENV]
    require_equal(missing, [], f"die Sperrliste des Starters hat Luecken: {missing}")


def t_the_child_environment_is_free_of_the_key_even_when_the_parent_has_it():
    """Der Beweis am laufenden Code, nicht am Quelltext.

    Der Elternprozess bekommt hier absichtlich einen Schluessel gesetzt. Kaeme er
    in der Kindumgebung an, waere die Stilllegung wertlos.
    """
    from solvio.specialists.launcher import child_environment

    saved = {name: os.environ.get(name) for name in FORBIDDEN_ENV}
    try:
        for name in FORBIDDEN_ENV:
            os.environ[name] = "sk-test-not-a-real-key-0000000000"
        env = child_environment()
        leaked = [name for name in FORBIDDEN_ENV if name in env]
        require_equal(leaked, [], f"Sperrlisten-Namen im Kind: {leaked}")
        require("HOME" in env, "HOME fehlt — das Werkzeug findet seine eigene Sitzung nicht")
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def t_the_child_can_find_the_interpreter_its_tool_needs():
    """Live gemessen, und es kostete drei Bauversuche und eine Budgetgrenze.

    `codex` ist ein Node-Skript mit `#!/usr/bin/env node`. `resolve()` findet
    das Skript auch dann, wenn `PATH` Homebrew nicht kennt — aber `env node`
    sucht den INTERPRETER im `PATH` des Kindes. Unter launchd ist der
    `/usr/bin:/bin:/usr/sbin:/sbin`, und jeder Bauauftrag scheiterte mit
    `env: node: No such file or directory`. Aus einer Shell gestartet lief
    derselbe Code — der schlimmste Fall von „bei mir geht es".

    Dieser Test setzt genau den launchd-`PATH` und verlangt, dass das Kind den
    Interpreter trotzdem findet.
    """
    import shutil
    from solvio.specialists.launcher import STANDARD_BINARIES, child_environment

    saved = os.environ.get("PATH")
    try:
        os.environ["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
        env = child_environment()
        for folder in STANDARD_BINARIES:
            if os.path.isdir(folder):
                require(folder in env["PATH"].split(os.pathsep),
                        f"{folder} fehlt im Suchpfad des Kindes")
        require("/usr/bin" in env["PATH"].split(os.pathsep),
                "der vererbte Suchpfad wurde ersetzt statt ergaenzt")
        vorhanden = shutil.which("node")
        if vorhanden:
            require(shutil.which("node", path=env["PATH"]),
                    "node ist installiert, aber fuer das Kind nicht auffindbar")
    finally:
        if saved is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = saved


def t_the_search_path_is_code_and_cannot_be_set_by_a_caller():
    """Ein Suchpfad, den ein Aufrufer bestimmt, waere ein Weg, SOLVIO ein
    untergeschobenes Werkzeug ausfuehren zu lassen.

    Kein Name aus der Umgebung kann die Liste erweitern. Die Ergaenzung aus
    `STANDARD_BINARIES` haengt HINTEN an, damit ein vererbter Eintrag Vorrang
    behaelt.

    **Eine einzige Ausnahme steht vorne**, und sie ist hier ausdruecklich
    gestellt statt stillschweigend geduldet: das Verzeichnis der gepruefte
    git-Binary (P0.2). `/usr/bin/git` ist auf macOS kein git, sondern der
    xcselect-Weiterleiter — stuende er zuerst, oeffnete jeder unbeaufsichtigte
    Aufruf im Kaefig den GUI-Installer. Dieser Pfad kommt NICHT aus der
    Umgebung, sondern aus `git_binary.resolve()`, und der prueft vorher
    Eigentuemer, Rechte, Signatur und Probelauf.

    Geprueft wird deshalb schaerfer als vorher: vor dem vererbten Pfad darf
    **hoechstens** dieses eine, verifizierte Verzeichnis stehen — und nichts
    sonst.
    """
    from solvio import git_binary as GB
    from solvio.specialists import launcher as L

    saved = dict(os.environ)
    try:
        os.environ["PATH"] = "/usr/bin"
        for schmuggel in ("SOLVIO_TOOL_PATH", "TOOL_PATH", "NODE_PATH",
                          "PATH_EXTRA", "STANDARD_BINARIES", "SOLVIO_GIT"):
            os.environ[schmuggel] = "/tmp/boese"
        env = L.child_environment()
        teile = env["PATH"].split(os.pathsep)
        require("/tmp/boese" not in teile,
                "ein Umgebungsname hat den Suchpfad des Kindes erweitert")

        vorne = teile[:teile.index("/usr/bin")]
        require(len(vorne) <= 1,
                f"mehr als eine Ausnahme draengt sich vor den vererbten Pfad: {vorne}")
        if vorne:
            geprueft = os.path.dirname(GB.resolve())
            require_equal(vorne[0], geprueft,
                          "vor dem vererbten Pfad steht ein ungeprueftes Verzeichnis")
            require(not GB.is_forwarder(os.path.join(vorne[0], "git")),
                    "das vorangestellte Verzeichnis traegt den Weiterleiter")
    finally:
        os.environ.clear()
        os.environ.update(saved)


# =====================================================================
# 3 — `codex` ist nur noch ueber den Starter erreichbar
# =====================================================================

def t_only_the_launcher_layer_names_the_codex_binary():
    """Wer `codex` startet, tut es ueber `resolve()` im Starter — sonst niemand.

    Geprueft wird die Gestalt „Zeichenkette 'codex' als Programmname": ein
    Prozessstart, dessen erstes Argument woertlich `codex` oder ein Pfad darauf
    ist. Erlaubt ist das nur in `specialists/` und im Agentenlaufzeit-Adapter,
    die beide durch `launcher.run` gehen.
    """
    allowed_prefixes = (
        os.path.join(SRC, "solvio", "specialists"),
        os.path.join(SRC, "solvio", "agent_runtime"),
    )
    offenders: list[str] = []
    for path in _python_files(SRC):
        if path.startswith(allowed_prefixes):
            continue
        try:
            tree = ast.parse(_read(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else (
                node.func.id if isinstance(node.func, ast.Name) else "")
            if name not in SPAWN_NAMES or not node.args:
                continue
            first = node.args[0]
            literals: list[str] = []
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                literals.append(first.value)
            elif isinstance(first, (ast.List, ast.Tuple)) and first.elts:
                head = first.elts[0]
                if isinstance(head, ast.Constant) and isinstance(head.value, str):
                    literals.append(head.value)
            for literal in literals:
                if os.path.basename(literal) in ("codex", "claude"):
                    offenders.append(f"{_rel(path)}:{node.lineno} -> {literal}")
    require_equal(offenders, [],
                  f"ein externes Agenten-CLI wird ausserhalb des Starters gestartet: {offenders}")


def t_no_llm_exposed_tool_is_named_codex():
    """Der eigentliche Leck-Pfad war ein LLM-exponiertes Werkzeug. Es gibt keins mehr.

    Geprueft wird der gebaute Dispatcher, nicht eine Liste: was das Modell sieht,
    ist genau `openai_tools()`.
    """
    from solvio.config import Settings
    from solvio.tools.registry import build_dispatcher

    dispatcher = build_dispatcher(Settings(openai_api_key="sk-test-not-a-real-key-0000"))
    names = [tool["name"] for tool in dispatcher.openai_tools()]
    offenders = [name for name in names if "codex" in name.lower()]
    require_equal(offenders, [], f"codex-Werkzeuge sind dem Modell exponiert: {offenders}")
    require("codex_task" not in dispatcher.names(),
            "codex_task ist noch registriert — auch unexponiert waere es ein Weg")
    require("codex_confirm" not in dispatcher.names(), "codex_confirm ist noch registriert")


def t_the_frozen_policy_rows_stay_as_a_fail_closed_reservation():
    """Ausdruecklich das Gegenteil eines Aufraeumens.

    `codex_task`/`codex_modify` bleiben in der eingefrorenen Klassifikation als
    VERY_CRITICAL stehen. Ein Name, den es nicht gibt, darf streng eingestuft
    bleiben — und wer ihn je wieder einfuehrt, findet die strenge Zeile vor.
    `src/solvio/security/` wird von diesem Milestone nicht angefasst.
    """
    from solvio.capabilities.policy import ACTION_CLASS, ActionClass
    for name in ("codex_task", "codex_modify"):
        require_equal(ACTION_CLASS.get(name), ActionClass.VERY_CRITICAL,
                      f"{name} ist nicht mehr fail-closed reserviert")


# =====================================================================
# 4 — der Systemprompt verlangt kein Werkzeug mehr, das es nicht gibt
# =====================================================================

def t_the_system_prompt_no_longer_mentions_codex():
    """Der Prompt hat dem Modell ein Werkzeug befohlen, das es strukturell nicht
    rufen konnte (`codex_confirm`, `expose_to_llm=False`). Der Absatz ist fort."""
    from solvio.realtime.core_server import TOOL_INSTRUCTIONS
    low = TOOL_INSTRUCTIONS.lower()
    for token in ("codex", "confirmation_id"):
        require(token not in low, f"der Systemprompt nennt noch '{token}'")


def t_the_realtime_package_carries_no_codex_instruction_anywhere():
    """Nicht nur die eine Konstante: das ganze Sprachpaket."""
    offenders: list[str] = []
    realtime = os.path.join(SRC, "solvio", "realtime")
    for path in _python_files(realtime):
        for number, line in enumerate(_read(path).splitlines(), start=1):
            if "codex" in line.lower():
                offenders.append(f"{_rel(path)}:{number}")
    require_equal(offenders, [], f"das Sprachpaket nennt codex noch: {offenders}")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
