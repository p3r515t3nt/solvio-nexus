"""Die Builder-Grenze und die Kredentialgrenze — DEBT-0128, Teil 2.

Teil 1 (`test_agent_runtime_debt_0128.py`) belegt, dass der alte Weg fort ist.
Dieser Teil belegt, dass an seine Stelle **keine** neue Schuld getreten ist. Die
Schuld waere nur verschoben, wenn aus „API-Schluessel im Codex-Prozess" ein
„wiederverwendbare Abo-Sitzung fuer beliebige Agentenwerkzeuge" geworden waere.

Die Gestalt der Antwort ist je Anbieter verschieden, weil die ABLAGE es ist —
am laufenden System gemessen, nicht aus Dokumentation abgeleitet:

* **Claude:** die Sitzung liegt nur im macOS-Schluesselbund
  (`Claude Code-credentials`); `~/.claude*` ist kredentialfrei. Die Grenze zu
  den Werkzeug-Kindern ist die **Datei-Sperre des Profils** — nicht, wie die
  Architektur hoffte, die binaergebundene Eintrags-ACL: beide Wege laufen durch
  dasselbe `/usr/bin/security`, dem die ACL vertraut. Das Gate ist binaer und
  es ist GERISSEN: das CLI liest seine eigene Sitzung ueber einen
  `security`-Unterprozess, also sperrt dieselbe Sperre, die das Kind aussperrt,
  auch das CLI aus. `builder/claude` ist damit BLOCKIERT und der
  Codex-only-Rueckfall greift — belegt in
  `t_the_claude_builder_is_blocked_and_says_why`.
* **Codex:** die Sitzung ist eine DATEI (`~/.codex/auth.json`, 0600) und fuer
  jeden Prozess derselben uid lesbar. Der native Sandkasten verhindert das
  NICHT — gemessen. Die Grenze ist deshalb zweiteilig: kein Netz fuer
  Kommandos (gepinnt, gemessen) UND eine Ernte, die kredentialtragenden Inhalt
  verweigert.

Mehrere Tests hier sind **Live-Messungen** am echten `sandbox-exec` und am
echten Schluesselbund. Sie ueberspringen sich auf einem System ohne diese
Voraussetzungen — aber sie beschoenigen nichts: wo gemessen werden kann, wird
gemessen, und ein Kontrastlauf ausserhalb des Kaefigs belegt jedes Mal, dass
der Test nicht bloss deshalb gruen ist, weil ohnehin nichts geht.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))
from _guard import enforce_assertions, require, require_equal  # noqa: E402
enforce_assertions()

_TMP = tempfile.mkdtemp(prefix="solvio-b2-")
os.environ["SOLVIO_STATE_DIR"] = _TMP

from solvio.agent_runtime import isolation as I  # noqa: E402
from solvio.specialists import launcher as L  # noqa: E402

#: Der Eintrag, den das Claude-CLI fuer seine eigene Sitzung benutzt.
CLAUDE_KEYCHAIN_ENTRY = "Claude Code-credentials"


def _jail(tool_state: tuple[str, ...] = ()) -> tuple[str, str, str]:
    """Ein gerendertes Profil in einem echten Verzeichnispaar."""
    base = tempfile.mkdtemp(prefix="solvio-jail-")
    workspace = os.path.realpath(os.path.join(base, "ws"))
    scratch = os.path.realpath(os.path.join(base, "scratch"))
    os.makedirs(workspace, mode=0o700, exist_ok=True)
    os.makedirs(scratch, mode=0o700, exist_ok=True)
    body = I.render_profile(workspace=workspace, scratch=scratch, tool_state=tool_state)
    path = os.path.join(scratch, I.PROFILE_NAME)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(body)
    return workspace, scratch, path


def _need_sandbox() -> None:
    if not I.available():
        raise unittest.SkipTest("sandbox-exec fehlt — die Messung braucht macOS")


def _in_jail(profile: str, workspace: str, script: str, timeout: float = 20.0):
    """Fuehrt eine Shell-Zeile UNTER dem Profil aus. cwd ist der Arbeitsbereich —
    sonst scheitert schon `getcwd`, und der Test misst eine andere Frage."""
    return subprocess.run(
        [I.SANDBOX_EXEC, "-f", profile, "/bin/sh", "-c", script],
        cwd=workspace, capture_output=True, text=True, timeout=timeout)


def _outside(workspace: str, script: str, timeout: float = 20.0):
    """Derselbe Befehl OHNE Kaefig. Der Kontrast ist der Beweis, dass der Test
    nicht bloss deshalb gruen ist, weil ohnehin nichts ginge."""
    return subprocess.run(["/bin/sh", "-c", script], cwd=workspace,
                          capture_output=True, text=True, timeout=timeout)


# =====================================================================
# Das gerenderte Profil
# =====================================================================

def t_the_profile_denies_by_default_and_writes_only_what_it_owns():
    workspace, scratch, path = _jail()
    body = open(path, encoding="utf-8").read()
    require("(deny default)" in body, "das Profil verweigert nicht als Vorgabe")

    section = body.split("(allow file-write*")[1].split("(allow file-write-data")[0]
    require(workspace in section, "der Arbeitsbereich ist nicht schreibbar")
    require(scratch in section, "der Scratch ist nicht schreibbar")
    for stray in ("/usr", "/System", "/private/etc"):
        require(f'(subpath "{stray}")' not in section, f"{stray} ist schreibbar")


def t_no_sealed_path_appears_in_the_rendered_rules():
    """Der Tresor, die Freigaben, `~/.codex`, `~/.ssh`, jede `.env`.

    Ein Profil ist eine ERLAUBNISLISTE — diese Pfade stehen nicht unter einem
    `deny`, sie stehen gar nicht drin. Geprueft wird ueber `realpath`: eine
    Suche nach dem Text `~/.solvio` faende nichts und waere still gruen.
    """
    _workspace, _scratch, path = _jail(tool_state=I.claude_tool_state())
    body = open(path, encoding="utf-8").read()
    require_equal(I.sealed_violations(body), [], "das Profil nennt versiegelte Pfade")


def t_the_one_line_that_would_open_everything_is_absent():
    """`(subpath "<home>")` machte Tresor, Freigaben, `~/.codex`, `~/.ssh` und
    jede `.env` mit einer einzigen Zeile auf."""
    _workspace, _scratch, path = _jail(tool_state=I.claude_tool_state())
    body = open(path, encoding="utf-8").read()
    home = os.path.expanduser("~")
    rules = "\n".join(l for l in body.splitlines() if not l.lstrip().startswith(";"))
    require(f'(subpath "{home}")' not in rules, "das Profil oeffnet das ganze Heimverzeichnis")
    require(f'(subpath "{home}/Library")' not in rules, "das Profil oeffnet ~/Library")


def t_the_builder_jail_has_no_loopback_egress_target():
    """Der Hermes-Kaefig darf genau EINEN Rueckschleifen-Port (den Broker). Ein
    Builder braucht keinen — und mit der Zeile koennte er den Broker erreichen."""
    _workspace, _scratch, path = _jail()
    body = open(path, encoding="utf-8").read()
    rules = "\n".join(l for l in body.splitlines() if not l.lstrip().startswith(";"))
    require("localhost:" not in rules, "das Builder-Profil nennt ein Rueckschleifen-Ziel")
    require("network-bind" not in rules, "der Builder darf einen Port binden")
    require("network-inbound" not in rules, "der Builder nimmt Verbindungen an")


def t_the_profile_refuses_to_launch_if_a_seal_ever_breaks():
    """Kein `assert`: unter `python -O` waere die Pruefung weg — und das Siegel
    genau dort offen, wo es am meisten schadet."""
    import ast
    source = open(os.path.join(os.path.dirname(__file__), "..", "src", "solvio",
                               "agent_runtime", "isolation.py"), encoding="utf-8").read()
    tree = ast.parse(source)
    launch = next(node for node in ast.walk(tree)
                  if isinstance(node, ast.AsyncFunctionDef) and node.name == "launch")
    raises = [n for n in ast.walk(launch) if isinstance(n, ast.Raise)]
    asserts = [n for n in ast.walk(launch) if isinstance(n, ast.Assert)]
    require(len(raises) >= 3, "launch scheitert nicht fail-closed")
    require_equal(asserts, [], "launch benutzt assert statt raise")


# =====================================================================
# Live: was ein Kind unter dem Profil wirklich kann
# =====================================================================

def t_a_child_under_the_profile_cannot_read_any_sealed_path():
    """Gemessen, nicht behauptet — mit Kontrast ausserhalb des Kaefigs.

    **Gefragt wird nach dem LESERECHT, nicht nach dem Inhalt.** Eine fruehere
    Fassung nahm `head -c 1` und schickte die Ausgabe nach `/dev/null`: sie
    zeigte nie einen Wert, aber sie las ein Byte — aus `~/.codex/auth.json`,
    aus `.env`, aus `~/.ssh`. Ein Byte ist ein Inhalt.

    Dass `test -r` denselben Beweis traegt, ist gemessen und nicht angenommen —
    an einer synthetischen Datei, nie an echten Anmeldedaten:

        draussen  test -r   : 0      im Kaefig  test -r   : 1
        draussen  head -c 1 : 0      im Kaefig  head -c 1 : 1

    Der Kaefig verweigert beides gleich. `test -r` ist damit vollwertig und
    fasst nichts an.
    """
    _need_sandbox()
    workspace, _scratch, path = _jail(tool_state=I.claude_tool_state())
    home = os.path.expanduser("~")
    targets = [os.path.join(home, "solvio-core", ".env"),
               os.path.join(home, ".codex", "auth.json"),
               os.path.join(home, ".ssh"),
               os.path.join(home, ".solvio")]
    checked = 0
    for target in targets:
        if not os.path.exists(target):
            continue
        checked += 1
        inside = _in_jail(path, workspace, f"test -r '{target}'; echo $?")
        require(inside.stdout.strip() != "0",
                f"der Kaefig darf {os.path.basename(target)} lesen")
    if checked == 0:
        raise unittest.SkipTest("keiner der versiegelten Pfade existiert auf diesem System")


def t_a_child_under_the_profile_cannot_reach_a_core_loopback_service():
    """Real sandbox, owned live target: never probe a productive Core/Broker port.

    The builder profile denies loopback as a class. The existing rendered-rule
    test also rejects every explicit localhost exception. An ephemeral fixture
    proves the denied connection against a working listener without recording
    a bogus intrusion in the owner's broker (DEBT-0345).
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    _need_sandbox()
    workspace, _scratch, path = _jail()
    requests = []

    class Probe(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"solvio-owned-loopback-fixture")

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Probe)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        command = f"curl --noproxy '*' -s -m 3 http://127.0.0.1:{port}/"
        outside = _outside(workspace, command)
        require_equal(outside.returncode, 0, "owned live fixture unreachable outside sandbox")
        require_equal(outside.stdout, "solvio-owned-loopback-fixture")
        inside = _in_jail(path, workspace, command)
        require(inside.returncode != 0, "builder sandbox reached the loopback fixture")
        require_equal(requests, ["/"], "sandbox request escaped to the fixture")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


#: Ein Dienstname, den es im Schluesselbund garantiert nicht gibt. Damit
#: laesst sich pruefen, ob `security` ueberhaupt STARTEN darf — ohne je einen
#: echten Eintrag zu beruehren.
SYNTHETISCHER_DIENST = "SOLVIO-CANARY-nicht-vorhanden-0000"


def t_the_claude_subscription_session_is_out_of_reach_of_a_tool_child():
    """**Das binaere B2-Gate.** Gelingt der Zugriff, ist der Claude-Builder BLOCKIERT.

    **Auflage, und sie formt diesen Test:** keine echten Anmeldedaten anfassen —
    nicht lesen, nicht anzeigen, nicht kopieren. Geprueft wird der MECHANISMUS,
    nicht der Inhalt.

    Eine fruehere Fassung rief `security find-generic-password -w` auf den
    echten Claude-Eintrag. Sie zeigte nie einen Wert (nur Exit-Code und
    Byte-Zahl), aber sie LAS ihn. Das faellt unter die Auflage, auch wenn es
    sorgfaeltig gemacht war.

    Diese Fassung prueft dasselbe an zwei Stellen, ohne einen echten Wert:

    * `security` mit einem Dienstnamen, den es nicht gibt. Draussen startet das
      Programm und meldet „nicht gefunden" (Code 44). Drinnen darf es gar nicht
      erst starten — das Profil verbietet den Prozess. Zwei verschiedene
      Fehler, und genau der Unterschied ist der Beweis.
    * Die Schluesselbund-DATEI mit `test -r`: das fragt das Leserecht ab und
      liest kein Byte.

    Was dieser Test damit NICHT mehr zeigt: dass ein echter Eintrag existiert.
    Das ist hinnehmbar — es war nie die Aussage. Die Aussage ist, ob ein
    Werkzeug-Kind an den Schluesselbund herankommt.
    """
    _need_sandbox()
    workspace, _scratch, path = _jail(tool_state=I.claude_tool_state())

    # Draussen: das Programm laeuft und meldet „nicht gefunden".
    outside = _outside(workspace,
                       f'security find-generic-password -s "{SYNTHETISCHER_DIENST}"'
                       f' >/dev/null 2>&1; echo $?')
    draussen = outside.stdout.strip()
    if draussen not in ("44", "36"):
        raise unittest.SkipTest(
            f"`security` verhaelt sich hier unerwartet (Code {draussen!r})")

    # Drinnen: das Programm darf nicht einmal starten.
    inside = _in_jail(path, workspace,
                      f'security find-generic-password -s "{SYNTHETISCHER_DIENST}"'
                      f' >/dev/null 2>&1; echo $?')
    drinnen = inside.stdout.strip()
    require(drinnen != draussen,
            "ein Werkzeug-Kind erreicht den Schluesselbund genauso wie draussen "
            "— Builder BLOCKIERT")

    # Und die Datei: nur das Leserecht, kein Byte.
    keychain = os.path.expanduser("~/Library/Keychains/login.keychain-db")
    if os.path.exists(keychain):
        direct = _in_jail(path, workspace, f"test -r '{keychain}'; echo $?")
        require(direct.stdout.strip() != "0",
                "der Kaefig darf die Schluesselbund-Datei lesen")


def t_public_egress_still_works_or_the_builder_is_useless():
    """Die Gegenprobe zur Netzsperre: 443 muss hinausgehen, sonst kann das CLI
    sich nicht anmelden — und ein Test, der einen toten Kaefig prueft, ist gruen
    aus dem falschen Grund."""
    _need_sandbox()
    workspace, _scratch, path = _jail()
    outside = _outside(workspace, "curl -s -m 8 -o /dev/null https://example.com/; echo $?")
    if outside.stdout.strip() != "0":
        raise unittest.SkipTest("kein Netz auf diesem Rechner")
    inside = _in_jail(path, workspace,
                      "curl -s -m 10 -o /dev/null https://example.com/; echo $?", timeout=30)
    require_equal(inside.stdout.strip(), "0", "der Builder kommt nicht auf 443 hinaus")


def t_children_of_the_child_inherit_the_jail():
    """Ein CLI startet Helfer. Erbte der Helfer das Profil nicht, waere die
    ganze Grenze eine Zeile tief."""
    _need_sandbox()
    workspace, _scratch, path = _jail()
    home = os.path.expanduser("~")
    target = os.path.join(home, ".ssh")
    if not os.path.isdir(target):
        raise unittest.SkipTest("kein ~/.ssh auf diesem System")
    # Ein Kind, das ein Enkelkind startet, das den versiegelten Pfad versucht.
    inside = _in_jail(path, workspace,
                      f"/bin/sh -c \"ls '{target}' >/dev/null 2>&1; echo \\$?\"")
    require(inside.stdout.strip() != "0", "das Enkelkind war nicht im Kaefig")


# =====================================================================
# Codex: der native Sandkasten und das benannte Residuum
# =====================================================================

def t_the_codex_builder_invocation_pins_the_network_off():
    """Der Pin ist der Mechanismus, nicht die Vorgabe.

    `sandbox_workspace_write.network_access` steht per Vorgabe auf `false`, aber
    eine Nutzerkonfiguration kann das kippen — und der Builder liefe dann mit
    Netz, ohne dass sich eine Zeile SOLVIO-Code geaendert haette. Deshalb wird
    der Wert in der Invocation ausdruecklich gesetzt. Die Mutation, die diese
    Zeile entfernt, muss hier scheitern.
    """
    from solvio.agent_runtime import specialists as SP
    invocation = SP.codex_builder_invocation(workdir="/tmp/x")
    argv = list(invocation.argv)
    require("--sandbox" in argv, "die Invocation nennt keinen Sandkasten")
    require_equal(argv[argv.index("--sandbox") + 1], "workspace-write", "falscher Sandkasten")
    joined = " ".join(argv)
    require("sandbox_workspace_write.network_access=false" in joined,
            "der Netz-Aus-Schalter ist NICHT gepinnt — Kommandos haetten Netz")
    require("--ignore-user-config" in argv,
            "ohne --ignore-user-config entscheidet eine Nutzerdatei ueber den Kaefig")


def t_the_output_filter_catches_auth_json_shaped_material():
    """Das benannte Residuum wird verengt, nicht verschwiegen.

    Gemessen: ein Kommando im nativen Codex-Sandkasten KANN `~/.codex/auth.json`
    lesen. Netz hat es nicht (gemessen), also ist der einzige verbleibende Kanal
    der Modellkontext → die Antwort. Der laeuft durch die Redaktion des
    Starters PLUS ein auth.json-eigenes Muster.
    """
    from solvio.agent_runtime import specialists as SP
    samples = (
        '{"tokens": {"access_token": "eyJhbGciOiJIUzI1NiJ9.Q0FOQVJZbm90cmVhbA.Q0FOQVJZc2ln"}}',
        '"refresh_token": "aVeryLongLookingRefreshValue0123456789"',
        '"id_token":"eyJraWQiOiJzMSJ9.payloadpayload.signaturesig"',
        '{"OPENAI_API_KEY": "sk-proj-AAAAAAAAAAAAAAAAAAAAAAAAAAAA"}',
    )
    for sample in samples:
        cleaned = SP.redact_specialist_output(sample)
        require("<entfernt>" in cleaned, f"nicht redigiert: {sample[:30]}")
        for leak in ("abcdefghij", "aVeryLongLookingRefreshValue0123456789",
                     "payloadpayload", "sk-proj-AAAAAAAAAAAAAAAAAAAAAAAAAAAA"):
            require(leak not in cleaned, f"Material blieb stehen: {leak[:16]}")


def t_the_auth_filename_alone_is_enough_to_redact():
    """Ein Kommando, das die Datei bloss NENNT, ist harmlos. Eines, das ihren
    Inhalt zurueckgibt, nicht — und der Inhalt traegt ihren Namen nicht mit.
    Deshalb greift das Muster an der STRUKTUR (`tokens`-Objekt, `*_token`-Feld),
    nicht am Dateinamen."""
    from solvio.agent_runtime import specialists as SP
    harmless = "Ich habe ~/.codex/auth.json nicht gelesen."
    require_equal(SP.redact_specialist_output(harmless), harmless,
                  "eine blosse Erwaehnung wurde unnoetig verstuemmelt")


def t_the_claude_builder_is_blocked_and_says_why():
    """Das gerissene B2-Gate steht als Zustand im Code, nicht als Fussnote.

    Gemessen am 2026-08-29: das Claude-CLI liest seine Abo-Sitzung ueber einen
    `security`-UNTERPROZESS. Unter dem versiegelten Profil scheitert das mit
    `EPERM: posix_spawn 'security'`. Macht man `security` ausfuehrbar UND den
    Schluesselbund lesbar — die einzige Fassung, in der sich das CLI anmelden
    koennte —, dann liest ein `/bin/sh`-Kind dieselbe Sitzung (gemessen: 510
    Byte). Beides zugleich geht nicht, also ist der Builder blockiert.

    Die Annahme der Architektur, die binaergebundene ACL wuerde ein `bash`-Kind
    aussperren, traegt NICHT: beide Wege laufen durch dasselbe
    `/usr/bin/security`, dem die ACL vertraut. Was tatsaechlich hielt, ist die
    Datei-Sperre des Profils — und genau die sperrt das CLI mit aus.
    """
    from solvio.agent_runtime import specialists as SP
    require("builder/claude" in SP.BLOCKED_PROFILES,
            "der Claude-Builder gilt wieder als freigegeben — ohne neue Messung?")
    ok, reason = SP.builder_available(SP.profile("builder/claude"))
    require(not ok, "ein blockiertes Profil meldet sich als verfuegbar")
    require("keychain" in reason, f"der Grund ist nicht der gemessene: {reason}")
    require("builder/claude" not in SP.usable_profiles(),
            "ein Lauf koennte das blockierte Profil waehlen")


def t_a_builder_remains_or_the_release_must_say_so():
    """Wenn KEIN Builder uebrig bleibt, darf BUILD nicht als freigegeben gelten.

    Der Test erzwingt keine Freigabe — er erzwingt, dass die Lage sichtbar ist:
    entweder es gibt ein nutzbares Builder-Profil, oder die Laufzeit weiss, dass
    sie keines hat.
    """
    from solvio.agent_runtime import specialists as SP
    builders = [key for key, value in SP.usable_profiles().items()
                if value.mode == SP.BUILDER]
    if not builders:
        raise unittest.SkipTest("kein Builder freigegeben — BUILD ist abgeschaltet")
    for key in builders:
        ok, reason = SP.builder_available(SP.profile(key))
        require(ok, f"{key} gilt als nutzbar, ist es aber nicht: {reason}")


def t_no_reusable_subscription_token_is_ever_put_into_a_child_environment():
    """Der Rueckfall, der schlimmer waere als das Problem.

    `CLAUDE_CODE_OAUTH_TOKEN` in die Kindumgebung zu legen, wuerde den
    Claude-Builder lauffaehig machen — und dabei JEDEM Kind die
    wiederverwendbare Sitzung geben. Genau das ist DEBT-0128 Teil 2. Der Name
    steht auf der Sperrliste des Starters, und kein Modul der Agentenlaufzeit
    darf ihn setzen.
    """
    import ast
    folder = os.path.join(os.path.dirname(__file__), "..", "src", "solvio",
                          "agent_runtime")
    offenders = []
    for base, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in sorted(files):
            if not name.endswith(".py"):
                continue
            path = os.path.join(base, name)
            tree = ast.parse(open(path, encoding="utf-8").read())
            for node in ast.walk(tree):
                targets = node.targets if isinstance(node, ast.Assign) else []
                for target in targets:
                    if isinstance(target, ast.Subscript) and \
                            isinstance(target.slice, ast.Constant) and \
                            target.slice.value in L.DENIED_ENV:
                        offenders.append(f"{name}:{node.lineno}")
    require_equal(offenders, [], f"die Laufzeit setzt einen Sperrlisten-Namen: {offenders}")
    require("CLAUDE_CODE_OAUTH_TOKEN" in L.DENIED_ENV,
            "die Abo-Sitzungsvariable steht nicht auf der Sperrliste")


def t_the_denylist_still_strips_a_name_that_someone_added_to_the_allowlist():
    """Warum es beide Listen braucht — die Mutation, die eine entfernt, faengt
    sonst niemand.

    Heute ist die Sperrliste redundant: keiner ihrer Namen steht auf der
    Erlaubnisliste, also entfernt sie nie etwas. Genau das sagt der
    Modul-Docstring des Starters auch („theoretisch ueberfluessig — bis jemand
    der Erlaubnisliste einen Namen hinzufuegt").

    Dieser Test stellt genau diesen Tag her. Ohne ihn waere „Sperrliste
    entfernt" eine aequivalente Mutation, und die zweite Schicht koennte still
    verschwinden — bemerkt wuerde es erst an dem Tag, an dem sie gebraucht wird.
    """
    saved_allow = L.ENV_ALLOWLIST
    saved_value = os.environ.get("OPENAI_API_KEY")
    try:
        L.ENV_ALLOWLIST = saved_allow + ("OPENAI_API_KEY",)
        os.environ["OPENAI_API_KEY"] = "sk-test-not-a-real-key-0000000000"
        env = L.child_environment()
        require("OPENAI_API_KEY" not in env,
                "die Sperrliste hat den Namen NICHT entfernt, obwohl er erlaubt war")
    finally:
        L.ENV_ALLOWLIST = saved_allow
        if saved_value is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = saved_value


def t_the_seal_check_reports_a_profile_that_names_a_sealed_path():
    """Dieselbe Frage fuer die Siegelliste.

    Das gerenderte Profil nennt `~/.codex` heute nicht — es ist eine
    Erlaubnisliste, und der Pfad steht schlicht nicht drin. Die Siegelliste ist
    deshalb ein WAECHTER fuer den Tag, an dem jemand ihn hinzufuegt. Ohne
    diesen Test koennte der Waechter verschwinden, ohne dass etwas rot wird.
    """
    home = os.path.expanduser("~")
    for sealed, path in (("~/.codex", os.path.join(home, ".codex")),
                         ("~/.ssh", os.path.join(home, ".ssh")),
                         ("~/solvio-core", os.path.join(home, "solvio-core"))):
        require(sealed in I.SEALED_PATHS, f"{sealed} fehlt in der Siegelliste")
        tampered = f'(version 1)\n(deny default)\n(allow file-read*\n  (subpath "{path}"))\n'
        require(I.sealed_violations(tampered),
                f"ein Profil mit {sealed} wurde nicht beanstandet")
    # Und die Gegenprobe: ein sauberes Profil bleibt sauber, sonst waere der
    # Waechter nur ein Alarm, der immer schrillt.
    _workspace, _scratch, path = _jail(tool_state=I.claude_tool_state())
    require_equal(I.sealed_violations(open(path, encoding="utf-8").read()), [],
                  "das echte Profil wurde beanstandet")


def t_forbidden_env_never_reaches_a_specialist_child():
    """Der Mechanismus des Starters, hier an der Agentenlaufzeit noch einmal:
    auch mit absichtlich gesetztem Schluessel im Elternprozess."""
    saved = {name: os.environ.get(name) for name in L.DENIED_ENV}
    try:
        for name in L.DENIED_ENV:
            os.environ[name] = "sk-test-not-a-real-key-0000000000"
        env = L.child_environment()
        leaked = [name for name in L.DENIED_ENV if name in env]
        require_equal(leaked, [], f"Sperrlisten-Namen im Kind: {leaked}")
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


# =====================================================================
# N8/C4: das Profil des Claude-AUFTRAGSARBEITERS — Siegel, Wurzeln, AF_UNIX
# =====================================================================

HOME = os.path.expanduser("~")
TASK_WS = os.path.join(HOME, ".solvio-tasks", "workspaces", "at-0123456789abcdef")
TASK_JAIL = os.path.join(HOME, ".solvio-tasks", "claude-jails", "at-0123456789abcdef")
SOCK = os.path.join(TASK_JAIL, "sock", "tmpabcdefgh", "core.sock")


def _rules(body: str) -> str:
    return "\n".join(l for l in body.splitlines() if not l.lstrip().startswith(";"))


def t_the_worker_profile_has_broker_only_network_and_exactly_one_af_unix_line():
    body = I.worker_profile(workspace=TASK_WS, jail=TASK_JAIL, broker_port=8792, unix_sockets=(SOCK,))
    rules = _rules(body)
    network = rules.split("(allow network-outbound")[1:]
    require_equal(len(network), 2, "genau ein Netzblock plus genau eine AF_UNIX-Zeile")
    require('(remote tcp "localhost:8792")' in rules, "die Rueckschleife zum Broker fehlt")
    require(f'(path "{SOCK}")' in rules, "die AF_UNIX-Zeile fehlt")
    require_equal(rules.count("(path "), 1, "mehr als eine AF_UNIX-Zeile")
    for forbidden in ('"*:443"', '"*:80"', '"*:53"', "mDNSResponder", "network-bind", "network-inbound"):
        require(forbidden not in rules, f"das Worker-Profil erlaubt {forbidden}")
    require('(deny process-exec (literal "/usr/bin/security"))' in rules, "security ist ausfuehrbar")
    # Ohne Werkzeugbruecke: keine AF_UNIX-Zeile.
    plain = _rules(I.worker_profile(workspace=TASK_WS, jail=TASK_JAIL, broker_port=8792))
    require_equal(plain.count("(path "), 0)
    # Eine AF_UNIX-Zeile gibt es NUR mit dem gemakelten Netzblock.
    try:
        I.render_profile(workspace=TASK_WS, scratch=TASK_JAIL, unix_sockets=(SOCK,))
    except ValueError:
        pass
    else:
        raise AssertionError("ein Egress-Profil bekam eine AF_UNIX-Zeile")


def t_the_worker_profile_under_the_task_roots_names_no_sealed_path_and_no_root():
    body = I.worker_profile(workspace=TASK_WS, jail=TASK_JAIL, broker_port=8792, unix_sockets=(SOCK,))
    require_equal(I.sealed_violations(body, sealed=I.SEALED_PATHS + I.WORKER_SEALED), [])
    require_equal(I.sealed_violations(body), [])
    rules = _rules(body)
    require(f'(subpath "{HOME}")' not in rules, "das Profil oeffnet das Heimverzeichnis")
    require(TASK_WS in rules and TASK_JAIL in rules, "die Task-Blaetter fehlen")
    for root in I.SEALED_ROOTS:
        resolved = os.path.realpath(os.path.expanduser(root))
        require(f'"{resolved}"' not in rules and f'"{resolved}/"' not in rules,
                f"das Profil nennt die Wurzel {root}")
    # `~/.claude` bleibt draussen: `tool_state=()` — der CLI-Zustand liegt im Kaefig.
    require(os.path.join(HOME, ".claude") not in rules, "das Worker-Profil bindet ~/.claude ein")


def t_a_workspace_under_a_sealed_home_refuses_the_jail_instead_of_rendering_it():
    for workspace in (os.path.join(HOME, ".solvio", "native-task-workspaces", "at-1"),
                      os.path.join(HOME, ".solvio-nexus", "workspaces", "at-1"),
                      os.path.join(HOME, ".claude", "at-1")):
        try:
            I.worker_profile(workspace=workspace, jail=TASK_JAIL, broker_port=8792)
        except I.BuilderJailUnavailable as exc:
            require("sealed" in str(exc), str(exc))
        else:
            raise AssertionError(f"ein Arbeitsraum unter {workspace} wurde gerendert")


def t_the_exact_root_rule_reports_the_root_but_not_the_task_leaf():
    ws_root = os.path.join(HOME, ".solvio-tasks", "workspaces")
    tampered = f'(version 1)\n(deny default)\n(allow file-read*\n  (subpath "{ws_root}"))\n'
    require("root:~/.solvio-tasks/workspaces" in I.sealed_violations(tampered),
            "die exakte Wurzel-Nennung wurde nicht beanstandet")
    tampered = f'(version 1)\n(deny default)\n(allow file-read*\n  (subpath "{ws_root}/"))\n'
    require(I.sealed_violations(tampered), "die Wurzel mit Schraegstrich wurde nicht beanstandet")
    leaf = f'(version 1)\n(deny default)\n(allow file-read*\n  (subpath "{ws_root}/at-1"))\n'
    require_equal(I.sealed_violations(leaf), [], "das Task-Blatt wurde faelschlich beanstandet")
    # Konfigurierte Wurzeln einer isolierten Welt: dieselbe exakte Regel.
    custom = "/private/var/tmp/solvio-test-roots/workspaces"
    tampered = f'(version 1)\n(deny default)\n(allow file-read*\n  (subpath "{custom}"))\n'
    require_equal(I.sealed_violations(tampered), [], "eine fremde Wurzel gilt ohne Konfiguration nicht")
    require(I.sealed_violations(tampered, roots=I.sealed_roots(custom)),
            "die konfigurierte Wurzel wurde nicht beanstandet")
    try:
        I.worker_profile(workspace=custom, jail="/private/var/tmp/solvio-test-roots/jails/at-1",
                         broker_port=8792, roots=I.sealed_roots(custom))
    except I.BuilderJailUnavailable:
        pass
    else:
        raise AssertionError("ein Arbeitsraum, der die Wurzel selbst ist, wurde gerendert")


def t_solvio_nexus_and_the_configured_codex_home_are_sealed():
    require("~/.solvio-nexus" in I.SEALED_PATHS, "das native Codex-Home ist nicht versiegelt")
    nexus = os.path.join(HOME, ".solvio-nexus", "codex")
    tampered = f'(version 1)\n(deny default)\n(allow file-read*\n  (subpath "{nexus}"))\n'
    require("~/.solvio-nexus" in I.sealed_violations(tampered),
            "ein Profil mit ~/.solvio-nexus/... wurde nicht beanstandet (Pfadgrenzen-Luecke)")
    # Das konfigurierte Codex-Home an einem anderen Ort ist ebenfalls versiegelt.
    from unittest.mock import patch
    from types import SimpleNamespace
    elsewhere = "/private/var/tmp/solvio-elsewhere/codex-home"
    with patch("solvio.config.load_settings",
               return_value=SimpleNamespace(agent_runtime_hermes_codex_home=elsewhere)):
        require(elsewhere in I.sealed_paths(), "das konfigurierte Codex-Home fehlt in den Siegeln")
        tampered = f'(version 1)\n(deny default)\n(allow file-read*\n  (subpath "{elsewhere}/x"))\n'
        require(elsewhere in I.sealed_violations(tampered))
    # Die Worker-Siegel sind NICHT global — das Abo-Builder-Profil bindet ~/.claude ein.
    require_equal(I.WORKER_SEALED, ("~/.claude", "~/.claude.json"))
    require("~/.claude" not in I.SEALED_PATHS, "ein globales ~/.claude-Siegel faerbte den Abo-Builder rot")


def t_a_child_under_the_worker_profile_cannot_reach_the_nexus_home_or_a_neighbour():
    """Live am echten sandbox-exec: der Nachbar-Zeuge beweist den Mechanismus
    auf jedem Rechner; `~/.solvio-nexus` dort, wo es existiert."""
    _need_sandbox()
    base = tempfile.mkdtemp(prefix="solvio-worker-jail-")
    workspace = os.path.realpath(os.path.join(base, "ws"))
    jail = os.path.realpath(os.path.join(base, "jail"))
    neighbour = os.path.realpath(os.path.join(base, "neighbour"))
    for folder in (workspace, jail, neighbour):
        os.makedirs(folder, mode=0o700)
    with open(os.path.join(neighbour, "witness.txt"), "w") as handle:
        handle.write("ZEUGE\n")
    body = I.worker_profile(workspace=workspace, jail=jail, broker_port=8792)
    path = I.install_profile(jail, body)
    inside = _in_jail(path, workspace, f"cat '{neighbour}/witness.txt' 2>&1; echo rc=$?")
    require("ZEUGE" not in inside.stdout and "rc=0" not in inside.stdout,
            f"der Nachbar ist lesbar: {inside.stdout[:80]}")
    outside = _outside(workspace, f"cat '{neighbour}/witness.txt'; echo rc=$?")
    require("ZEUGE" in outside.stdout, "der Kontrast fehlt: der Zeuge ist draussen nicht lesbar")
    nexus = os.path.join(HOME, ".solvio-nexus")
    if os.path.isdir(nexus):
        inside = _in_jail(path, workspace, f"ls -ld '{nexus}' 2>&1; echo rc=$?")
        require("rc=0" not in inside.stdout, "der Kaefig sieht ~/.solvio-nexus")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
