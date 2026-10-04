"""Die Notizfaehigkeit — und die Grenze, hinter der keine Notiz entsteht.

**Der Befund, den diese Suite schliesst (DEBT-0237).** In der ersten
Live-Abnahme zog der Pruefaufbau die Zielbegrenzung: ein Handler im Skript
verweigerte Pfade ausserhalb des Wirkungsordners. Das Produkt selbst hatte
keine. Gemessen war die Kette davor voellig offen — der Planer waehlt `pfad`
frei, `router._validate` prueft nur `type: string`, und
`Orchestrator._effect_allowed` laeuft **nach** dem Schreiben und entscheidet nur
ueber den BELEG.

Diese Suite prueft VERHALTEN an echten Dateien und echten Symlinks:

* das zulaessige Ziel funktioniert — der bestehende Notizauftrag bleibt gueltig;
* ein fremder Pfad, ein Symlink im Weg und ein Symlink als Zielname werden
  abgelehnt, **bevor** irgendetwas geschrieben ist;
* die Ablehnung geschieht unabhaengig von der Effektpruefung — sie ersetzt die
  Ausfuehrungsbegrenzung nicht;
* der Weg durch den ECHTEN Router mit echter Freigabe fuehrt weiterhin zu einem
  gebundenen Ausfuehrungsbeleg.

Kein Modell, kein Netz, kein Builder, keine Produktionsdaten.
"""
from __future__ import annotations

import asyncio
import atexit
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))

from _guard import enforce_assertions, require, require_equal  # noqa: E402

enforce_assertions()

_SANDBOX = tempfile.mkdtemp(prefix="solvio-notes-")
atexit.register(shutil.rmtree, _SANDBOX, True)
os.environ["SOLVIO_STATE_DIR"] = os.path.join(_SANDBOX, "state")
os.environ["SOLVIO_AGENT_RUNS_DB"] = os.path.join(_SANDBOX, "agent_runs.sqlite3")
os.environ.pop("SOLVIO_NOTES_DIR", None)

from solvio.capabilities import notes as N                      # noqa: E402

NOTIZ = "Zahnarzt Dienstag 9 Uhr"


def _frisch() -> str:
    """Eine leere, frische Ablage — jede Probe faengt bei null an."""
    wurzel = os.path.join(_SANDBOX, "state", "effects", "notes")
    shutil.rmtree(wurzel, ignore_errors=True)
    os.makedirs(wurzel, mode=0o700, exist_ok=True)
    return os.path.realpath(wurzel)


def _refused(pfad, text=NOTIZ) -> str:
    """Den Ablehnungsgrund holen — oder scheitern, wenn NICHT abgelehnt wurde."""
    from solvio.capabilities.contract import CapabilityError
    try:
        N.append_note(pfad, text)
    except CapabilityError as exc:
        return exc.reason
    raise AssertionError(f"{pfad!r} wurde NICHT abgelehnt")


# =====================================================================
# A — Die Ablage
# =====================================================================

def t_the_storage_comes_from_configuration_not_from_the_argument():
    """Die Ablage ist konfiguriert. Ein Modellargument erweitert sie nie."""
    wurzel = _frisch()
    require_equal(N.notes_root(), wurzel, "die Vorgabe zeigt woandershin")

    eigene = os.path.realpath(tempfile.mkdtemp(dir=_SANDBOX))
    os.environ[N.NOTES_DIR_ENV] = eigene
    try:
        require_equal(N.notes_root(), eigene,
                      "die Konfiguration setzt sich nicht durch")
        # Und jetzt liegt der ALTE Ort ausserhalb — obwohl er eben noch galt.
        require_equal(_refused(os.path.join(wurzel, "n.md")), "outside_notes_root")
    finally:
        os.environ.pop(N.NOTES_DIR_ENV, None)


def t_the_default_storage_lies_where_evidence_can_arise():
    """Die Vorgabe liegt im Wirkungsordner der Laufzeit.

    Ein Ausfuehrungsbeleg entsteht nur unter `Orchestrator._effect_root()`. Eine
    Notizablage ausserhalb waere eine stille Sackgasse: die Notiz entstuende,
    die Handlung bliebe unbelegt, und niemand saehe warum. Der Gleichlauf wird
    hier festgehalten, nicht per Import erzwungen — die Faehigkeitsschicht
    haengt nicht an der Laufzeit.
    """
    from solvio.agent_runtime.orchestrator import Orchestrator
    _frisch()
    require_equal(N.EFFECT_DIRNAME, Orchestrator.EFFECT_DIR,
                  "Wirkungsordner und Notizablage sind auseinandergelaufen")
    orch = Orchestrator.__new__(Orchestrator)
    wirkung = orch._effect_root()
    require(N.notes_root().startswith(wirkung + os.sep),
            f"die Ablage liegt ausserhalb des Wirkungsordners: {N.notes_root()}")


# =====================================================================
# B — Das zulaessige Ziel
# =====================================================================

def t_a_plain_name_is_derived_into_the_storage():
    """Ein blosser Dateiname wird an die Ablage gehaengt."""
    wurzel = _frisch()
    ziel = N.append_note("notizen.md", NOTIZ)
    require_equal(ziel, os.path.join(wurzel, "notizen.md"))
    require_equal(open(ziel, encoding="utf-8").read(), NOTIZ + "\n")


def t_an_absolute_path_inside_the_storage_works_and_appends():
    """**Der bestehende Notizauftrag bleibt gueltig.**

    Genau die Form, die das Modell in der Live-Abnahme lieferte: ein absoluter
    Pfad in der Ablage. Und Anhaengen bleibt Anhaengen — der alte Inhalt steht
    danach noch da.
    """
    wurzel = _frisch()
    ziel = os.path.join(wurzel, "notizen.md")
    N.append_note(ziel, "Erste Zeile")
    zurueck = N.append_note(ziel, NOTIZ)
    require_equal(zurueck, ziel)
    require_equal(open(ziel, encoding="utf-8").read(), f"Erste Zeile\n{NOTIZ}\n")


def t_a_subfolder_inside_the_storage_is_allowed():
    """Unterordner in der Ablage sind erlaubt und werden angelegt."""
    wurzel = _frisch()
    ziel = N.append_note(os.path.join(wurzel, "2026", "september.md"), NOTIZ)
    require_equal(ziel, os.path.join(wurzel, "2026", "september.md"))
    require(os.path.exists(ziel))


# =====================================================================
# C — Die Gegenproben: abgelehnt VOR jeder Schreibwirkung
# =====================================================================

def t_a_foreign_path_is_refused_and_nothing_is_written():
    """**Gegenprobe 1: fremder Pfad.**

    Genau der Fall aus DEBT-0237: ohne diese Schranke haette das Artefakt die
    fehlenden Verzeichnisse angelegt und die Datei geschrieben — der Lauf waere
    ohne Beleg geendet, die WIRKUNG aber geschehen.
    """
    _frisch()
    fremd = os.path.join(_SANDBOX, "fremd", "notizen.md")
    require_equal(_refused(fremd), "outside_notes_root")
    require(not os.path.exists(fremd), "die fremde Datei wurde angelegt")
    require(not os.path.isdir(os.path.dirname(fremd)),
            "der fremde Ordner wurde angelegt")


def t_a_relative_escape_is_refused():
    """**Gegenprobe 1b: `..` — beliebig tief verschachtelt.**"""
    wurzel = _frisch()
    for pfad in (os.path.join(wurzel, "..", "raus.md"),
                 os.path.join(wurzel, "a", "..", "..", "raus.md"),
                 os.path.join(wurzel, "..", "..", "etc", "raus.md")):
        require_equal(_refused(pfad), "outside_notes_root", pfad)
    require(not os.path.exists(os.path.join(os.path.dirname(wurzel), "raus.md")),
            "ein Ausbruch ueber .. hat geschrieben")


def t_a_relative_path_with_separator_is_refused():
    """Ein relativer Pfad haengt am Arbeitsverzeichnis — das steht in keinem
    Vertrag. Ein Ziel, das je nach Aufrufort woanders liegt, ist kein
    gebundener Parameter."""
    _frisch()
    require_equal(_refused("unter/notizen.md"), "relative_path_with_separator")
    require_equal(_refused("../raus.md"), "relative_path_with_separator")


def t_a_symlinked_directory_never_leads_out():
    """**Gegenprobe 2: Symlink IM WEG.**

    Ein Praefixvergleich auf der Zeichenkette saehe hier nichts: der Pfad
    beginnt mit der Ablage. Erst `realpath` des Elternordners zeigt, dass er
    hinausfuehrt.
    """
    wurzel = _frisch()
    draussen = os.path.join(_SANDBOX, "draussen")
    os.makedirs(draussen, exist_ok=True)
    os.symlink(draussen, os.path.join(wurzel, "brücke"))

    ziel = os.path.join(wurzel, "brücke", "notizen.md")
    require(ziel.startswith(wurzel + os.sep), "die Probe taugt nicht")
    require_equal(_refused(ziel), "outside_notes_root")
    require(not os.path.exists(os.path.join(draussen, "notizen.md")),
            "ueber den Symlink wurde geschrieben")


def t_a_symlinked_target_name_is_refused_before_writing():
    """**Gegenprobe 3: der ZIELNAME selbst ist ein Symlink.**

    Hier hilft `realpath` des Elternordners nicht — der Elternordner IST die
    Ablage. Nur `O_NOFOLLOW` verhindert, dass das Oeffnen dem Verweis folgt und
    an sein Ziel schreibt. Ohne ihn stuende der Text in der fremden Datei.
    """
    wurzel = _frisch()
    opfer = os.path.join(_SANDBOX, "opfer.txt")
    with open(opfer, "w", encoding="utf-8") as fh:
        fh.write("unberuehrt\n")
    os.symlink(opfer, os.path.join(wurzel, "notizen.md"))

    require_equal(_refused(os.path.join(wurzel, "notizen.md")), "target_is_symlink")
    require_equal(open(opfer, encoding="utf-8").read(), "unberuehrt\n",
                  "ueber den Symlink-Zielnamen wurde geschrieben")


def t_the_check_runs_before_anything_is_created():
    """**Die Reihenfolge ist die Aussage.** Erst pruefen, dann anlegen.

    Gelesen an der Quelle: in `append_note` steht `resolve_target` VOR
    `makedirs` und `os.open`. Eine Fassung, die erst anlegt und dann prueft,
    haette die Datei schon erzeugt, bevor jemand fragt, ob sie darf — und die
    Gegenproben oben blieben trotzdem gruen, weil sie nur den INHALT pruefen.
    """
    import inspect
    quelle = inspect.getsource(N.append_note)
    p_check = quelle.index("resolve_target(")
    p_mkdir = quelle.index("os.makedirs(")
    p_open = quelle.index("os.open(")
    require(p_check < p_mkdir < p_open,
            "die Pruefung steht nicht vor dem Anlegen und Oeffnen")


def t_empty_and_oversized_notes_are_refused():
    """Eine leere Notiz ist keine, und eine beliebig lange ist ein anderes
    Werkzeug."""
    _frisch()
    require_equal(_refused("notizen.md", "   "), "empty_text")
    require_equal(_refused("notizen.md", "x" * (N.MAX_NOTE_CHARS + 1)),
                  "text_too_long")
    require_equal(_refused(""), "empty_path")


# =====================================================================
# D — Die Schranke ist NICHT die Effektpruefung
# =====================================================================

def t_the_execution_limit_is_independent_of_the_effect_check():
    """**Die nachtraegliche Effektpruefung ersetzt die Begrenzung nicht.**

    Sie entscheidet ueber den BELEG, nicht ueber die Wirkung. Beweis am
    Verhalten: die Faehigkeit lehnt ab, obwohl kein Orchestrator, kein Lauf und
    keine Effektpruefung im Spiel ist — es gibt hier gar nichts, was hinterher
    pruefen koennte.
    """
    _frisch()
    fremd = os.path.join(_SANDBOX, "ohne-lauf.md")
    require_equal(_refused(fremd), "outside_notes_root")
    require(not os.path.exists(fremd))

    # Und die Gegenrichtung: der Wirkungsordner der Laufzeit ist WEITER als die
    # Notizablage. Ein Pfad, den `_effect_allowed` durchliesse, wird hier
    # trotzdem abgelehnt — die beiden Schranken sind nicht dieselbe.
    from solvio.agent_runtime.orchestrator import Orchestrator
    orch = Orchestrator.__new__(Orchestrator)
    im_wirkungsordner = os.path.join(orch._effect_root(), "daneben.md")
    require(orch._effect_allowed(im_wirkungsordner),
            "die Probe taugt nicht: der Pfad ist auch fuer die Laufzeit tabu")
    require_equal(_refused(im_wirkungsordner), "outside_notes_root")
    require(not os.path.exists(im_wirkungsordner),
            "ein Pfad im Wirkungsordner wurde geschrieben")


# =====================================================================
# E — Der Weg durch den echten Router
# =====================================================================

def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def t_the_capability_registers_through_the_intended_path():
    """`CapabilitySpec` + `register` — derselbe Weg wie `documents.py`."""
    from solvio.capabilities.router import CapabilityRouter
    router = CapabilityRouter(policy_mode="enforce")
    namen = N.register(router, N.NoteCapabilities())
    require_equal(namen, ["note_write"])
    require("note_write" in router.names(), "der Router kennt sie nicht")
    spec = router.spec("note_write")
    require(not spec.is_read_only(), "eine schreibende Faehigkeit gilt als lesend")
    require_equal(sorted(spec.input_schema["required"]), ["pfad", "text"])


def t_the_composition_registers_the_note_capability():
    """Die KOMPOSITION registriert sie — nicht nur ein Test.

    Gelesen an der Quelle von `tools/registry.py`: ohne diesen Aufruf gaebe es
    die Faehigkeit im laufenden Core nicht, und die Abnahme am echten Geraet
    haette nichts zu genehmigen.
    """
    import inspect
    from solvio.tools import registry
    quelle = inspect.getsource(registry)
    require("register_note_capabilities(d.capabilities, NoteCapabilities())"
            in quelle, "die Komposition registriert die Notizfaehigkeit nicht")


async def _freigabekette():
    """Die ECHTE Freigabekette mit einem synthetischen Geraet.

    Dasselbe Muster wie die Freigabe-Suiten. Gestellt ist nur, WER signiert —
    Kontrollebene, Anfrage, Challenge, Digestbindung und Einmaligkeit sind
    Produktionscode.
    """
    import mobile_attest_helper as H
    from solvio.security.approval import ApprovalBroker
    from solvio.security.mobile_approval import bridge as B
    from solvio.security.mobile_approval import control as C
    from solvio.security.mobile_approval import identity
    from solvio.security.mobile_approval import store as SA

    ordner = tempfile.mkdtemp(dir=_SANDBOX)
    speicher = SA.ApprovalControlStore(os.path.join(ordner, "approval.sqlite3"))
    await speicher.open()
    cp = C.MobileApprovalControlPlane(
        speicher, identity.MacSigningKey.load_or_create(ordner),
        identity.load_or_create_core_instance_id(ordner),
        attest_verifier=H.fake_verifier(),
        app_id="WQ8CG7R53R.de.solvio.approvals",
        allowed_environments={"development"})
    freigeber = B.MobileApprover()
    co = B.MobileApprovalCoordinator(cp, ApprovalBroker(approver=freigeber), freigeber)
    geraet = await H.enroll_attested(cp)
    return cp, co, geraet, H, speicher


async def _mit_freigabe(router, args, cp, co, geraet, H, zaehler):
    """Einmal anfragen, signiert freigeben, gebunden erneut ausfuehren."""
    from solvio.capabilities import policy as P
    from solvio.contracts.trust import TrustContext, TrustLevel
    from solvio.security.mobile_approval import protocol as PR

    vertrauen = TrustContext(origin_trust=TrustLevel.USER_DIRECT,
                             user_authorized=True, note="Abnahmeprobe")
    erste = await router.execute("note_write", args, trust=vertrauen,
                                 origin=P.OriginClass.LOCAL_OWNER, commanded=True)
    kennung = (erste.data or {}).get("request_id") if isinstance(erste.data, dict) else ""
    if not kennung:
        return erste, None
    draht, grund = await cp.issue_challenge(approval_id=kennung,
                                            device_id=geraet.device_id)
    assert draht is not None, f"Challenge verweigert: {grund}"
    # Der App-Attest-Zaehler muss STRENG steigen — jede Freigabe eine Stufe.
    zaehler[0] += 1
    _res, status = await co.apply_mobile_decision(
        **H.sign_decision(geraet, PR.b64d(draht["payload_b64"]),
                          counter=zaehler[0]))
    assert status == "ok", f"Freigabe: {status}"
    zweite = await router.execute("note_write", args, trust=vertrauen,
                                  origin=P.OriginClass.LOCAL_OWNER, commanded=True,
                                  approval_request_id=kennung)
    return erste, zweite


def t_even_an_approved_call_cannot_write_outside_the_storage():
    """**Die Freigabe deckt die HANDLUNG, nicht den ORT.**

    Der schaerfste Fall: das Freigabetor ist durchlaufen, der Mensch (hier sein
    Stellvertreter) hat signiert, die Bindung stimmt — und die Faehigkeit
    schreibt trotzdem nicht ausserhalb ihrer Ablage. Waere die Zielbegrenzung
    Teil der Freigabe statt der Ausfuehrung, stuende hier eine fremde Datei.
    """
    from solvio.capabilities.envelope import CapabilityOutcome as OUT
    from solvio.capabilities.approval_gateway import CapabilityApprovals
    from solvio.capabilities.router import CapabilityRouter

    async def probe():
        _frisch()
        cp, co, geraet, H, speicher = await _freigabekette()
        zaehler = [0]
        try:
            router = CapabilityRouter(
                mobile=CapabilityApprovals(co, owner_principal="local-owner"),
                policy_mode="enforce")
            N.register(router, N.NoteCapabilities())

            # (a) Das ZULAESSIGE Ziel geht durch — sonst prueft (b) nichts.
            gut = os.path.join(N.notes_root(), "notizen.md")
            _e1, gut_zweite = await _mit_freigabe(
                router, {"pfad": gut, "text": NOTIZ}, cp, co, geraet, H, zaehler)
            require_equal(gut_zweite.outcome, OUT.SUCCESS,
                          f"das erlaubte Ziel scheiterte: {gut_zweite.reason}")
            require_equal(open(gut, encoding="utf-8").read(), NOTIZ + "\n")

            # (b) Der FREMDE Pfad. Er wird abgelehnt, OHNE dass der Mensch
            # ueberhaupt gefragt wird — der Router reicht den Einwand des
            # Klassifizierers vor der Freigabefrage durch. Sein eigener
            # Kommentar: „der Mensch soll nie etwas bestaetigen, das danach
            # abgelehnt wird."
            fremd = os.path.join(_SANDBOX, "trotz-freigabe.md")
            erste, zweite = await _mit_freigabe(
                router, {"pfad": fremd, "text": NOTIZ}, cp, co, geraet, H, zaehler)
            require(not (erste.data or {}).get("request_id"),
                    "fuer ein unzulaessiges Ziel wurde eine Freigabe angefragt")
            require_equal(zweite, None, "es gab einen zweiten Aufruf")
            require_equal(erste.outcome, OUT.REJECTED_BY_POLICY,
                          f"der Router meldete {erste.outcome}: {erste.reason}")
            require_equal(erste.reason, "outside_notes_root", erste.reason)
            require(not os.path.exists(fremd),
                    "trotz Zielbegrenzung wurde ausserhalb geschrieben")

            # (c) Der Symlink-Zielname — ebenso, und ohne Freigabefrage.
            opfer = os.path.join(_SANDBOX, "opfer-mit-freigabe.txt")
            with open(opfer, "w", encoding="utf-8") as fh:
                fh.write("unberuehrt\n")
            link = os.path.join(N.notes_root(), "verweis.md")
            os.symlink(opfer, link)
            dritte, _e3 = await _mit_freigabe(
                router, {"pfad": link, "text": NOTIZ}, cp, co, geraet, H, zaehler)
            require_equal(dritte.outcome, OUT.REJECTED_BY_POLICY, dritte.reason)
            require_equal(dritte.reason, "target_is_symlink", dritte.reason)
            require_equal(open(opfer, encoding="utf-8").read(), "unberuehrt\n",
                          "ueber den Symlink wurde geschrieben")
        finally:
            await speicher.close()

    _run(probe())


def t_a_symlink_that_appears_after_the_check_is_still_refused():
    """**Das Wettrennen — der Fall, den nur `O_NOFOLLOW` fangen kann.**

    Die Vorpruefung in `resolve_target` sieht einen Symlink, der schon da ist.
    Sie sieht keinen, der zwischen Pruefung und Oeffnen entsteht. Genau diese
    Luecke schliesst `O_NOFOLLOW` beim Oeffnen.

    Gemessen, warum diese Probe noetig ist: eine Mutation, die `O_NOFOLLOW`
    entfernte, ueberlebte ALLE anderen Proben dieser Suite — die Vorpruefung
    fing den Symlink jedes Mal vorher ab. Eine Zusicherung, die durch eine
    FREMDE Schranke gruen bleibt, ist keine.

    Das Wettrennen wird hier gestellt, nicht gehofft: `resolve_target` gibt
    einmal einen Pfad zurueck, der bereits ein Symlink ist — dieselbe Lage, die
    ein Angreifer im Zeitfenster erzeugt haette.
    """
    wurzel = _frisch()
    opfer = os.path.join(_SANDBOX, "opfer-im-rennen.txt")
    with open(opfer, "w", encoding="utf-8") as fh:
        fh.write("unberuehrt\n")
    link = os.path.join(wurzel, "spaet.md")
    os.symlink(opfer, link)

    echt = N.resolve_target
    N.resolve_target = lambda pfad: link          # die Pruefung sieht nichts
    try:
        grund = _refused(link)
    finally:
        N.resolve_target = echt
    require_equal(grund, "target_is_symlink",
                  "das Oeffnen ist dem Verweis gefolgt")
    require_equal(open(opfer, encoding="utf-8").read(), "unberuehrt\n",
                  "im Wettrennen wurde ueber den Symlink geschrieben")


def t_the_existing_note_order_still_produces_a_bound_effect_record():
    """**Der bestehende Notizauftrag bleibt gueltig — bis zum Beleg.**

    Die Zielbegrenzung darf den abgenommenen Weg nicht brechen. Geprueft wird
    deshalb die ganze Naht: registrierte Produktfaehigkeit → echter Router →
    echte Freigabe → geschriebene Datei → Ausfuehrungsbeleg des Cores, an die
    Anforderung gebunden.

    Ohne Modell: der Plan ist hier eine Zeile Testcode, weil die MODELLfrage
    schon live beantwortet ist. Was hier geprueft wird, ist die Mechanik
    dahinter.
    """
    from solvio.agent_runtime import store as S
    from solvio.agent_runtime import steps as ST
    from solvio.agent_runtime.orchestrator import Orchestrator, RunContext
    from solvio.agent_runtime import budget as BU
    from solvio.capabilities.approval_gateway import CapabilityApprovals
    from solvio.capabilities.router import CapabilityRouter

    class Geplant:
        kind, capability, optional = "capability", "note_write", False
        profile = instruction = ""
        requirement = "h1"
        arguments: dict = {}

    async def probe():
        wurzel = _frisch()
        cp, co, geraet, H, speicher = await _freigabekette()
        try:
            router = CapabilityRouter(
                mobile=CapabilityApprovals(co, owner_principal="local-owner"),
                policy_mode="enforce")
            N.register(router, N.NoteCapabilities())
            ledger = S.AgentRunLedger(os.path.join(tempfile.mkdtemp(dir=_SANDBOX),
                                                   "runs.sqlite3"))
            orch = Orchestrator(ledger=ledger, router=router, control_plane=cp)
            task = ledger.create_task(objective="Schreib mir eine Notiz.",
                                      scope=S.SCOPE_RESEARCH,
                                      created_origin="local_owner",
                                      created_principal="owner")
            run = ledger.create_run(task_id=task.task_id)
            kontext = RunContext(run_id=run.run_id, task_id=task.task_id,
                                 scope=S.SCOPE_RESEARCH,
                                 ledger=BU.BudgetLedger(budget=BU.DEFAULTS["research"]))
            ziel = os.path.join(wurzel, "notizen.md")
            Geplant.arguments = {"pfad": ziel, "text": NOTIZ}

            kontext.effect_before = orch._effect_before(Geplant())
            _erste, zweite = await _mit_freigabe(
                router, Geplant.arguments, cp, co, geraet, H, [0])
            require_equal(zweite.reason or "success", "success",
                          f"der abgenommene Weg scheitert: {zweite.reason}")

            schritt = ledger.create_step(run_id=run.run_id, seq=1, kind="capability",
                                         attempt=1, capability="note_write")
            ledger.update_step(schritt.step_id, state="succeeded", finished=True)
            await orch._record_effect(run, kontext, schritt, Geplant(), zweite)

            belege = orch._verified_effects(run.run_id)
            require_equal(len(belege), 1, f"kein Ausfuehrungsbeleg: {belege}")
            beleg = next(iter(belege))
            require_equal(belege[beleg], "h1",
                          "der Beleg ist keiner Handlung zugeordnet")
            require(NOTIZ in beleg, beleg)
            require_equal(open(ziel, encoding="utf-8").read(), NOTIZ + "\n")
        finally:
            await speicher.close()

    _run(probe())


# =====================================================================
# F — Der Gespraechsweg: das Sprachwerkzeug
#
# Am echten Spracheinstieg GEMESSEN (`scripts/note_routing_live.py`, ein
# Modellaufruf): der Satz „Schreib mir eine Notiz mit dem Text Zahnarzt
# Dienstag 9 Uhr." wird mit Zuversicht 0.98 als `kein_auftrag` eingeordnet,
# `outcome=handed_back`, null Agentenauftraege.
#
# Die Route ist RICHTIG — „SOLVIO kann das SELBST, hier und jetzt". Falsch war,
# dass es im Gespraech keinen Ausfuehrungsweg gab: das Modell haette „ist
# notiert" sagen koennen, ohne dass etwas geschrieben wird. Diese Sektion
# prueft, dass es ihn jetzt gibt — und dass er dieselben Schranken traegt.
# =====================================================================

def _turn(user_text=None, principal="owner"):
    """Ein echter Turn am echten Tor — wie ihn der Core fuellt."""
    from solvio.capabilities import policy as P
    from solvio.capabilities.invocation import CapabilityInvocationGate
    from solvio.contracts.trust import TrustContext, TrustLevel
    tor = CapabilityInvocationGate()
    tor.begin_turn(session_id="s-note", turn_id="s-note-t1", principal=principal,
                   trust=TrustContext(origin_trust=TrustLevel.USER_DIRECT,
                                      user_authorized=True, note="Sprachturn"),
                   user_text=(SATZ if user_text is None else user_text),
                   origin=P.OriginClass.ROOM_VOICE, conversation_id="c-note")
    return tor


SATZ = "Schreib mir eine Notiz mit dem Text Zahnarzt Dienstag 9 Uhr."


def t_f_the_conversation_now_has_an_execution_path():
    """**Der Befund, den die Routingmessung erzwungen hat.**

    `kein_auftrag` heisst „mach es selbst". Dafuer muss es im Gespraech ein
    Werkzeug geben. Vorher gab es keines — der Einstieg fuehrte ins Leere.
    """
    from solvio.tools.note_capability_tools import note_capability_tools
    werkzeuge = note_capability_tools(object(), object())
    require_equal(len(werkzeuge), 1, "es gibt kein Notiz-Sprachwerkzeug")
    w = werkzeuge[0]
    require(w.expose_to_llm, "das Werkzeug ist dem Modell nicht sichtbar")
    schema = w.schema()
    require_equal(schema["name"], "note_write")
    require_equal(sorted(schema["parameters"]["required"]), ["pfad", "text"])
    require("Notiz" in schema["description"], schema["description"][:80])


def t_f_the_composition_exposes_the_tool_to_the_model():
    """Die KOMPOSITION haengt es an — nicht nur ein Test.

    Ohne diese Zeile in `tools/registry.py` gaebe es das Werkzeug im laufenden
    Core nicht, und der gemessene Sprachweg endete weiter im Leeren.
    """
    import inspect
    from solvio.tools import registry
    quelle = inspect.getsource(registry)
    require("note_capability_tools(d.capabilities, d.capability_gate)" in quelle,
            "die Komposition reicht das Notizwerkzeug nicht an das Modell")


def t_f_the_tool_writes_through_the_real_router_after_approval():
    """**Der ganze Gespraechsweg, mit echter Freigabe.**

    Turn-Tor → Werkzeug → `router.execute` → Matrix → Freigabe → Handler →
    Datei. Nichts davon ist gestellt ausser dem signierenden Geraet.
    """
    from solvio.capabilities.approval_gateway import CapabilityApprovals
    from solvio.capabilities.router import CapabilityRouter
    from solvio.tools.note_capability_tools import note_capability_tools

    async def probe():
        wurzel = _frisch()
        cp, co, geraet, H, speicher = await _freigabekette()
        try:
            router = CapabilityRouter(
                mobile=CapabilityApprovals(co, owner_principal="local-owner"),
                policy_mode="enforce")
            N.register(router, N.NoteCapabilities())
            tor = _turn()
            werkzeug = note_capability_tools(router, tor)[0]

            # 1. Anlauf: die Matrix verlangt Face ID, es wird NICHT geschrieben.
            erste = await werkzeug.run({"pfad": "notizen.md", "text": NOTIZ})
            require(not erste.success, f"ohne Freigabe geschrieben: {erste.error}")
            require("approval_required" in (erste.error or ""), erste.error)
            ziel = os.path.join(wurzel, "notizen.md")
            require(not os.path.exists(ziel), "vor der Freigabe wurde geschrieben")

            # 2. Die Freigabe, signiert vom eingeschriebenen Geraet.
            kennung = (erste.data or {}).get("request_id")
            require(kennung, f"keine Freigabekennung: {erste.data}")
            from solvio.security.mobile_approval import protocol as PR
            draht, grund = await cp.issue_challenge(approval_id=kennung,
                                                    device_id=geraet.device_id)
            require(draht is not None, f"Challenge verweigert: {grund}")
            _r, status = await co.apply_mobile_decision(
                **H.sign_decision(geraet, PR.b64d(draht["payload_b64"])))
            require_equal(status, "ok", status)

            # 3. Derselbe Aufruf, jetzt gedeckt — und die Datei entsteht.
            zweite = await werkzeug.run({"pfad": "notizen.md", "text": NOTIZ})
            require(zweite.success, f"trotz Freigabe nicht geschrieben: {zweite.error}")
            require_equal(open(ziel, encoding="utf-8").read(), NOTIZ + "\n")
            require_equal((zweite.data or {}).get("pfad"), ziel,
                          "der beschriebene Pfad fehlt in der Antwort")
        finally:
            await speicher.close()

    _run(probe())


def t_f_the_tool_cannot_escape_the_storage_either():
    """Der Gespraechsweg erbt die Zielbegrenzung — er umgeht sie nicht.

    Das ist die Probe, die ein neuer Weg immer braucht: eine zweite Tuer in
    dasselbe Haus darf keine andere Schwelle haben.
    """
    from solvio.capabilities.approval_gateway import CapabilityApprovals
    from solvio.capabilities.router import CapabilityRouter
    from solvio.tools.note_capability_tools import note_capability_tools

    async def probe():
        _frisch()
        cp, co, geraet, H, speicher = await _freigabekette()
        try:
            router = CapabilityRouter(
                mobile=CapabilityApprovals(co, owner_principal="local-owner"),
                policy_mode="enforce")
            N.register(router, N.NoteCapabilities())
            werkzeug = note_capability_tools(router, _turn())[0]
            fremd = os.path.join(_SANDBOX, "ueber-das-werkzeug.md")
            res = await werkzeug.run({"pfad": fremd, "text": NOTIZ})
            require(not res.success, "das Werkzeug hat ausserhalb geschrieben")
            require("outside_notes_root" in (res.error or ""), res.error)
            require(not os.path.exists(fremd), "die fremde Datei entstand")
            # Und OHNE Freigabefrage: der Klassifizierer haelt vorher an.
            require(not (res.data or {}).get("request_id"),
                    "fuer ein unzulaessiges Ziel wurde eine Freigabe angefragt")
        finally:
            await speicher.close()

    _run(probe())


def t_f_without_a_trusted_turn_the_tool_writes_nothing():
    """Ohne belegten Turn schreibt das Werkzeug nichts — dieselbe Schranke wie
    bei jedem anderen Faehigkeitswerkzeug."""
    from solvio.capabilities.invocation import CapabilityInvocationGate
    from solvio.tools.note_capability_tools import note_capability_tools

    async def probe():
        wurzel = _frisch()
        leer = CapabilityInvocationGate()          # begin_turn NIE gerufen
        werkzeug = note_capability_tools(object(), leer)[0]
        res = await werkzeug.run({"pfad": "notizen.md", "text": NOTIZ})
        require(not res.success, "ohne Turn wurde geschrieben")
        require_equal(res.error, "no_trusted_context")
        require(not os.path.exists(os.path.join(wurzel, "notizen.md")))

    _run(probe())


def t_f_every_origin_still_needs_face_id():
    """**Der neue Weg lockert die Matrix nicht.**

    Gemessen fuer alle vier Herkuenfte: `note_write` ist ueberall
    `require_face_id`. Der Gespraechsweg ist also nicht der bequemere, sondern
    nur der kuerzere — eine Freigabe statt zweier, weil kein Auftrag angelegt
    wird.
    """
    from solvio.capabilities import policy as P
    spec = N.SPECS["note_write"]
    klasse = P.apply_floor(P.base_class(spec.name, read_only=spec.is_read_only()),
                           spec.base_risk)
    require_equal(klasse, P.ActionClass.UNCLASSIFIED)
    for name in ("ROOM_VOICE", "TRUSTED_INTERACTIVE_APP", "LOCAL_OWNER",
                 "BACKGROUND_AUTOMATION"):
        herkunft = getattr(P.OriginClass, name)
        require_equal(P.MATRIX[herkunft][klasse], P.Decision.REQUIRE_FACE_ID,
                      f"{name} braucht keine Freigabe mehr")


def t_f_the_approval_text_names_the_real_origin():
    """**Der Freigabetext muss sagen, VON WO gefragt wurde.**

    „Die Haustuer, angefragt ueber das Raum-Mikrofon" ist eine andere
    Entscheidung als dieselbe Bitte aus der App in der Hand — das steht so im
    Freigabe-Gateway, und die Herkunft geht deshalb in den Text, in den Digest
    und auf das Display.

    Diese Probe gibt es, weil eine Mutation sie brauchte: das Werkzeug mit
    einer FESTEN Herkunft statt der des Turns zu rufen, blieb unbemerkt — die
    Matrix verlangt fuer `note_write` aus jeder Herkunft Face ID, das Verhalten
    war also gleich. Gleich ist aber nicht dasselbe: der Mensch haette „iPhone-
    App" gelesen, wo „Raum-Mikrofon" richtig war, und genau das unterschriebe
    er dann.
    """
    from solvio.capabilities.approval_gateway import CapabilityApprovals
    from solvio.capabilities.router import CapabilityRouter
    from solvio.tools.note_capability_tools import note_capability_tools

    async def probe():
        _frisch()
        cp, co, geraet, H, speicher = await _freigabekette()
        try:
            router = CapabilityRouter(
                mobile=CapabilityApprovals(co, owner_principal="local-owner"),
                policy_mode="enforce")
            N.register(router, N.NoteCapabilities())
            werkzeug = note_capability_tools(router, _turn())[0]
            res = await werkzeug.run({"pfad": "notizen.md", "text": NOTIZ})
            kennung = (res.data or {}).get("request_id")
            require(kennung, f"keine Freigabeanfrage: {res.data}")

            zeile = await cp.store.get_request(kennung)
            text = str((zeile or {}).get("task") or "")
            require("Raum-Mikrofon" in text,
                    f"der Freigabetext nennt die falsche Herkunft: {text!r}")
            require("iPhone-App" not in text, text)
            require(NOTIZ in text, f"der Notiztext fehlt im Freigabetext: {text!r}")
        finally:
            await speicher.close()

    _run(probe())


def _conversation_continuation(decision="approve", tamper=False):
    """No second tool call: only the Core tick may consume the signed approval."""
    from solvio.capabilities.approval_gateway import CapabilityApprovals
    from solvio.capabilities.router import CapabilityRouter
    from solvio.tools.note_capability_tools import note_capability_tools
    from solvio.tools.dispatcher import ToolDispatcher
    from solvio.tools.registry import attach_agent_runtime
    from solvio.agent_runtime.orchestrator import Orchestrator
    from solvio.agent_runtime.store import AgentRunLedger
    from solvio.security.mobile_approval import protocol as PR

    class Inbox:
        def __init__(self):
            self.items = []

        async def add_item(self, item):
            self.items.append(item)
            return True

    async def probe():
        root = _frisch()
        cp, co, device, H, storage = await _freigabekette()
        try:
            router = CapabilityRouter(
                mobile=CapabilityApprovals(co, owner_principal="local-owner"),
                policy_mode="enforce")
            N.register(router, N.NoteCapabilities())
            gate = _turn()
            dispatcher = ToolDispatcher()
            dispatcher.capabilities = router
            dispatcher.capability_gate = gate
            dispatcher.register(note_capability_tools(router, gate)[0])
            ledger = AgentRunLedger(os.path.join(
                tempfile.mkdtemp(dir=_SANDBOX), "runs.sqlite3"))
            inbox = Inbox()
            runtime = Orchestrator(ledger=ledger, router=router,
                                   control_plane=cp, proactive=inbox)
            attach_agent_runtime(dispatcher, runtime)
            first = await dispatcher.tool("note_write").run(
                {"pfad": "notizen.md", "text": NOTIZ})
            request_id = (first.data or {}).get("request_id")
            require(request_id, first.error)
            gate.clear()
            target = os.path.join(root, "notizen.md")
            await runtime.tick()
            require(not os.path.exists(target), "execution before approval")
            wire, reason = await cp.issue_challenge(
                approval_id=request_id, device_id=device.device_id)
            require(wire is not None, reason)
            _, status = await co.apply_mobile_decision(
                **H.sign_decision(device, PR.b64d(wire["payload_b64"]),
                                  decision=(PR.DECISION_DENY if decision == "deny"
                                            else PR.DECISION_APPROVE)))
            require_equal(status, "ok")
            if decision == "expired":
                import sqlite3
                with sqlite3.connect(storage.path) as db:
                    db.execute("UPDATE approval_requests SET expires_at=1 WHERE approval_id=?",
                               (request_id,))
            if tamper:
                import sqlite3
                import json
                with sqlite3.connect(ledger.path) as db:
                    db.execute("UPDATE pending_starts SET arguments=? WHERE request_id=?",
                               (json.dumps({"pfad": "notizen.md", "text": "changed"}),
                                request_id))
            # Reconstructed runtime, with no conversation or tool invocation.
            runtime = Orchestrator(ledger=AgentRunLedger(ledger.path),
                                   router=router, control_plane=cp, proactive=inbox)
            attach_agent_runtime(dispatcher, runtime)
            require(dispatcher.tool("note_write").ledger is runtime.ledger,
                    "reattachment retained the old ledger")
            await runtime.tick()
            if decision != "approve" or tamper:
                await runtime.tick()
                require(not os.path.exists(target), "invalid approval wrote a note")
                require_equal(runtime.ledger.waiting_starts(), [])
                if tamper or decision == "expired":
                    require_equal(len(inbox.items), 1)
                    satz = inbox.items[0]["summary"]
                    # Beide Faelle enden `REJECTED_BY_POLICY` — der Umschlag
                    # sagt `had_no_effect`, also ist mit SICHERHEIT nichts
                    # geschrieben. Frueher stand hier „konnte nicht bestaetigt
                    # werden": derselbe Satz wie bei einem ungewissen Ausgang.
                    # Wer beides gleich benennt, stumpft die Meldung ab, die
                    # spaeter einmal ernst ist.
                    require("nicht geschrieben" in satz, satz)
                    require("weiss nicht sicher" not in satz, satz)
                else:
                    require_equal(inbox.items, [])
                return
            require(os.path.exists(target), "approved note stranded after conversation")
            await runtime.tick()
            require_equal(open(target, encoding="utf-8").read(), NOTIZ + "\n")
            require_equal(len(inbox.items), 1, "missing or duplicate completion notice")
            require_equal(inbox.items[0]["run_id"], request_id)
        finally:
            await storage.close()

    _run(probe())


def t_f_approval_after_conversation_ends_is_executed_once():
    _conversation_continuation()


def t_f_denied_note_is_never_continued():
    _conversation_continuation(decision="deny")


def t_f_expired_note_is_never_continued():
    _conversation_continuation(decision="expired")


def t_f_continuation_cannot_change_the_approved_note():
    _conversation_continuation(tamper=True)


def t_f_an_ambiguous_outcome_is_never_reported_as_a_non_event():
    """**Ergebniswahrheit bei einem mehrdeutigen Ausgang.**

    Der Handler schreibt die Zeile und scheitert DANACH. Genau diesen Fall
    fuehrt der eingefrorene Sicherheitspfad als „die Wirkung KANN eingetreten
    sein" — der Router liefert `RECOVERY_REQUIRED` und hat den ehrlichen Satz
    schon gebildet.

    `note_write` ist `NON_IDEMPOTENT_WRITE`. In dieser Klasse darf ein
    mehrdeutiger Ausgang **niemals** als Fehlschlag erzaehlt werden: der Mensch
    liest „nicht geschrieben", verlangt die Notiz noch einmal — und bekommt
    eine zweite Zeile. Das ist der einzige verbleibende Weg zu einem Duplikat,
    und er fuehrt ueber eine falsche Auskunft.
    """
    from solvio.capabilities.approval_gateway import CapabilityApprovals
    from solvio.capabilities.contract import CapabilitySpec, ExecutionClass
    from solvio.capabilities.router import CapabilityRouter
    from solvio.security.mobile_approval import protocol as PR
    from solvio.security.mobile_approval.execution import NON_IDEMPOTENT_WRITE
    from solvio.tools.base import RiskLevel
    from solvio.tools.dispatcher import ToolDispatcher
    from solvio.tools.note_capability_tools import note_capability_tools
    from solvio.tools.registry import attach_agent_runtime
    from solvio.agent_runtime.orchestrator import Orchestrator
    from solvio.agent_runtime.store import AgentRunLedger

    class Inbox:
        def __init__(self):
            self.items = []

        async def add_item(self, item):
            self.items.append(item)
            return True

    async def probe():
        root = _frisch()
        cp, co, device, H, storage = await _freigabekette()
        try:
            router = CapabilityRouter(
                mobile=CapabilityApprovals(co, owner_principal="local-owner"),
                policy_mode="enforce")

            # Dieselbe Spezifikation wie im Produkt — nur der Handler schreibt
            # und scheitert DANACH. Das ist die Lage, nicht ihre Nachbildung.
            geschrieben: list = []

            def handler(arguments):
                geschrieben.append(N.append_note(arguments["pfad"],
                                                 arguments["text"]))
                raise RuntimeError("Rueckweg verloren, nachdem die Zeile stand")

            router.register(N.SPECS["note_write"], handler,
                            classify=N.classify_note_write)

            gate = _turn()
            dispatcher = ToolDispatcher()
            dispatcher.capabilities = router
            dispatcher.capability_gate = gate
            dispatcher.register(note_capability_tools(router, gate)[0])
            ledger = AgentRunLedger(os.path.join(
                tempfile.mkdtemp(dir=_SANDBOX), "runs.sqlite3"))
            inbox = Inbox()
            runtime = Orchestrator(ledger=ledger, router=router,
                                   control_plane=cp, proactive=inbox)
            attach_agent_runtime(dispatcher, runtime)

            first = await dispatcher.tool("note_write").run(
                {"pfad": "notizen.md", "text": NOTIZ})
            request_id = (first.data or {}).get("request_id")
            require(request_id, first.error)
            gate.clear()
            wire, reason = await cp.issue_challenge(
                approval_id=request_id, device_id=device.device_id)
            require(wire is not None, reason)
            _, status = await co.apply_mobile_decision(
                **H.sign_decision(device, PR.b64d(wire["payload_b64"])))
            require_equal(status, "ok")

            runtime = Orchestrator(ledger=AgentRunLedger(ledger.path),
                                   router=router, control_plane=cp,
                                   proactive=inbox)
            attach_agent_runtime(dispatcher, runtime)
            await runtime.tick()

            ziel = os.path.join(root, "notizen.md")
            require(os.path.exists(ziel),
                    "die Probe taugt nicht: der Handler hat nicht geschrieben")
            require_equal(len(inbox.items), 1, f"Posteingang: {inbox.items}")
            satz = inbox.items[0]["summary"]

            # Die Zeile LIEGT auf der Platte. Ein Satz, den ein Mensch als
            # „nichts geschrieben" liest, ist hier falsch — und gefaehrlich.
            require("konnte nicht bestaetigt werden" not in satz,
                    f"ein mehrdeutiger Ausgang wurde als Nicht-Ereignis "
                    f"gemeldet: {satz!r}")
            require("nachsehen" in satz or "weiss nicht" in satz
                    or "nicht sicher" in satz,
                    f"der Satz nennt die Ungewissheit nicht: {satz!r}")

            # Und kein zweiter Versuch aus der Maschine.
            await runtime.tick()
            require_equal(len(geschrieben), 1,
                          f"ein zweiter Schreibversuch: {geschrieben}")
        finally:
            await storage.close()

    _run(probe())


def t_f_a_lost_answer_is_uncertain_not_a_non_event():
    """**Der zweite ungewisse Fall: die Antwort geht verloren.**

    Nicht der Handler scheitert, sondern der Weg dorthin — `result` bleibt
    `None`. Ob die Wirkung eintrat, weiss an dieser Stelle niemand: der Aufruf
    war unterwegs.

    Diese Probe gibt es, weil eine Mutation sie brauchte. `result is None` als
    „mit Sicherheit nichts passiert" zu behandeln blieb unbemerkt, solange nur
    der Router-Weg geprueft war — und es waere dieselbe falsche Auskunft mit
    demselben Ausgang: eine zweite Notiz, verlangt von einem falsch
    informierten Menschen.
    """
    from solvio.agent_runtime.orchestrator import Orchestrator

    class Inbox:
        def __init__(self):
            self.items = []

        async def add_item(self, item):
            self.items.append(item)
            return True

    async def probe():
        inbox = Inbox()
        orch = Orchestrator.__new__(Orchestrator)
        orch.proactive = inbox
        # Genau der Zweig: `start_approved_capability` wirft, `result` bleibt
        # `None`. Was davor in der Welt geschah, ist von hier aus nicht zu sehen.
        satz = Orchestrator._note_outcome_sentence(None, False)
        require("weiss nicht sicher" in satz or "nachsehen" in satz,
                f"ein verlorener Rueckweg wurde als Nicht-Ereignis gemeldet: {satz!r}")
        require("nicht geschrieben" not in satz, satz)

        # Und die Gegenrichtung bleibt scharf: ein SICHER wirkungsloser Ausgang
        # sagt das auch, sonst waere jede Meldung ein Achselzucken.
        from solvio.capabilities.envelope import CapabilityOutcome, CapabilityResult
        sicher = CapabilityResult(CapabilityOutcome.REJECTED_BY_POLICY, "c", "note_write",
                                  reason="approval_drift")
        require_equal(Orchestrator._note_outcome_sentence(sicher, False),
                      "Die freigegebene Notiz wurde nicht geschrieben.")
        gut = CapabilityResult(CapabilityOutcome.SUCCESS, "c", "note_write")
        require_equal(Orchestrator._note_outcome_sentence(gut, True),
                      "Die freigegebene Notiz wurde gespeichert.")
        # Und der Satz des Routers gewinnt, wenn er einen hat.
        ungewiss = CapabilityResult(CapabilityOutcome.RECOVERY_REQUIRED, "c", "note_write",
                                    human_message="Ich weiss nicht sicher, ob das durchging.")
        require("durchging" in Orchestrator._note_outcome_sentence(ungewiss, False))

    _run(probe())


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
