"""Freigabezustellung und datensparsame Portal-Auswertung.

Die Kontroll- und iPhone-Sourcepruefungen erhalten die bestehenden Grenzen
fuer Transport, Freigaben und automatische Abfrage. Die Studio-Auswertung
wird gegen eine vollstaendig synthetische angemeldete Testseite geprueft.
Kontodaten, Zeitstempel und alle Entwuerfe wurden fuer diesen Test erfunden.
"""
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))
from _guard import enforce_assertions, require, require_equal  # noqa: E402
enforce_assertions()

from solvio.portal import protocol as PROTO  # noqa: E402
from solvio.portal.binding import STUDIO_BINDING, binding_for_url  # noqa: E402
from solvio.portal.build import MODULES  # noqa: E402
from solvio.portal.readout import REDUCERS, lines_of, reduce_for  # noqa: E402
from solvio.portal.studio_readout import PORTAL_ID, reduce  # noqa: E402
from solvio.realtime.control import (  # noqa: E402
    CONTROL_PRINCIPAL, HEALTH, OPERATIONS, RUN, CoreControl,
)

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "studio_dashboard.txt")
def _ios_repo() -> str:
    """Use an explicit override or the repository's ios/ source tree."""
    configured = (os.environ.get("SOLVIO_IOS_REPO") or "").strip()
    if configured:
        return os.path.expanduser(configured)
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "ios"))


IOS = _ios_repo()


def _need_ios() -> None:
    """Die App-Zusagen brauchen die App-Quellen — sonst wird uebersprungen.

    Eine fehlende Arbeitskopie ist kein gebrochenes Versprechen; sie ist eine
    Pruefung, die hier nicht stattfinden kann, und das gehoert gesagt statt
    behauptet. Die Meldung nennt den AUFGELOESTEN Pfad — sonst sucht der
    naechste Mensch an der falschen Stelle.
    """
    if not os.path.isdir(IOS):
        raise __import__("unittest").SkipTest(
            f"die iOS-Arbeitskopie fehlt unter {os.path.normpath(IOS)}")


def _page() -> dict:
    """Die vollstaendig synthetische Seite als Antwort des Arbeiters."""
    with open(FIXTURE, encoding="utf-8") as handle:
        text = "\n".join(line for line in handle.read().splitlines()
                         if not line.startswith("#"))
    return {"ok": True, "url": "https://solvio-studio.de/dashboard",
            "title": "Solvio Studio", "text": text, "authenticated": True}


def _block(source: str, opener: str) -> str:
    """Der Rumpf eines Swift-Blocks, an geschweiften Klammern abgezaehlt.

    Ohne das prueft ein Test nur, ob ein Wort irgendwo in der Datei steht — und
    besteht dann auch, wenn genau die Stelle fehlt, um die es geht. Zwei
    Mutationen sind hier zunaechst durchgekommen; das ist die Korrektur.
    """
    start = source.index(opener)
    depth = 0
    for index in range(start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    return source[start:]


def _read(path: str) -> str:
    full = os.path.join(IOS, path)
    if not os.path.exists(full):
        return ""
    with open(full, encoding="utf-8") as handle:
        return handle.read()


# -- Phase A: der Weg zur Freigabe ------------------------------------------

def t_control_socket_knows_exactly_the_named_operations():
    """Kein Schlupfloch: der Griff kann genau das, was hier steht.

    Der Wert dieser Naht haengt daran, dass sie klein bleibt. Ein „exec" oder
    ein „eval" hier waere eine Fernsteuerung mit Besitzerrechten — und niemand
    haette es an der Klasse gesehen, nur an einem Feldnamen.

    Die Liste ist ausdruecklich eine AUFZAEHLUNG, keine Obergrenze: eine neue
    Operation soll nicht unmoeglich sein, aber sie soll HIER auffallen und
    einen Grund bekommen. Bisher drei:

    * `health`  — was ein Abnahmelauf wissen muss, bevor er anfaengt.
    * `run_capability` — eine registrierte Faehigkeit im laufenden Core.
    * `autopilot_token` — ein Broker-Token fuer EINEN von zwei fest
      benannten Auftraggebern des Entwicklungs-Autopiloten. Kein
      Anbieterschluessel, ohne Lease wertlos, und die Namensliste ist
      geschlossen (siehe `control.AUTOPILOT_PRINCIPALS`) — mit freiem Namen
      waere es ein Token-Automat.
    * `autopilot_lease` — oeffnet und schliesst EIN Lease fuer dieselben zwei
      Auftraggeber. Es gibt ihn, weil ein Token ohne Lease nichts oeffnet und
      die Registratur im Core lebt: ohne diesen Vorgang muesste ein Treiber
      einen ZWEITEN Broker starten, und zwei Kappen sind keine Kappe. Die
      Frist setzt der Core, nicht der Anrufer.
    * `approval_status` — **liest** den Zustand einer Freigabe und ihren
      Ausfuehrungsbeleg. Er fuehrt nichts aus und legt nichts an. Es gibt ihn,
      weil `run_capability` mit Kennung KEINE Auskunft ist: er fuehrt aus,
      sobald freigegeben ist, und beantwortet „abgelaufen" wie „laengst
      erledigt" mit demselben `not_approved`.
    * `note_status` — findet eine Notiz im dauerhaften Merkzettel wieder,
      ebenfalls nur lesend. Es gibt ihn, weil ein NEUES Gespraech die alte
      Freigabekennung nicht kennt; ohne ihn waere die Rueckfrage ein neuer
      Schreibauftrag. Bei mehrdeutiger Zuordnung waehlt er nichts.
    * `agent_task_status` — findet einen Agentenauftrag wieder, ebenfalls nur
      lesend: Laeufe samt Wortlaut, Grenze und Belegen und Startfreigaben,
      aus denen (noch) kein Lauf entstand. Bei mehreren passenden Auftraegen
      waehlt er keinen.
    * `browser_session_enroll` — N2-Browserkopplung ausschliesslich ueber
      den bestehenden Owner-UID-geprueften lokalen Socket. Der Prinzipal ist
      der konfigurierte Owner; weder JSON noch ein Modell waehlen ihn. Die
      Einmalkennung erscheint nur in der direkten Antwort, nicht im Journal.
    * `browser_session_revoke` — widerruft genau eine Browser-Sitzung dieses
      Owners. Kein oeffentlicher HTTP-Aussteller und keine Aufgabenfreigabe.

    Die drei Auskunftsvorgaenge erweitern die Flaeche um **Auskunft**, nicht
    um Wirkung: sie schreiben nichts, sie autorisieren nichts, und sie geben
    keine Kennung heraus, die der Anrufer nicht ohnehin selbst erzeugt haette
    oder die der Core nicht ohnehin fuer ihn fuehrt.
    """
    from solvio.realtime.control import (APPROVAL_STATUS, AUTOPILOT_LEASE,
                                         AUTOPILOT_PRINCIPALS, AUTOPILOT_TOKEN,
                                         LEASE_ACTIONS, NOTE_STATUS, TASK_STATUS,
                                         BROWSER_ENROLL, BROWSER_REVOKE)
    require_equal(set(OPERATIONS),
                  {HEALTH, RUN, AUTOPILOT_TOKEN, AUTOPILOT_LEASE,
                   APPROVAL_STATUS, NOTE_STATUS, TASK_STATUS,
                   BROWSER_ENROLL, BROWSER_REVOKE},
                  "unerwartete Vorgangsliste am Kontrollsocket")
    require_equal(sorted(LEASE_ACTIONS), ["close", "open"],
                  "der Lease-Vorgang kann mehr als oeffnen und schliessen")
    # Wieder eine AUFZAEHLUNG mit Grund, keine Obergrenze:
    #   `autopilot-lead*`   — der Beurteiler, ueber die OpenAI-Flaeche.
    #   `autopilot-writer-*`— der schreibende Claude-Builder (V0.6), ueber die
    #                         Anthropic-Flaeche. Sein Token oeffnet ohne Lease
    #                         nichts und ausserhalb der Rueckschleife gar
    #                         nichts; die Grenze ist der Auftraggeber, nicht
    #                         der Modellname.
    require_equal(sorted(AUTOPILOT_PRINCIPALS),
                  ["autopilot-lead", "autopilot-lead-escalation",
                   "autopilot-writer-claude",
                   "autopilot-writer-claude-escalation"],
                  "die Auftraggeberliste des Token-Vorgangs ist nicht mehr "
                  "geschlossen")
    source = open(os.path.join(os.path.dirname(__file__), "..", "src", "solvio",
                               "realtime", "control.py"), encoding="utf-8").read()
    for forbidden in ("subprocess", "eval(", "exec(", "os.system", "__import__("):
        require(forbidden not in source, f"kein {forbidden} im Kontrollpfad")


def t_control_rejects_an_unknown_operation():
    control = CoreControl(_Dispatcher())
    reply = asyncio.run(control.handle({"op": "run_shell", "cmd": "rm -rf /"}))
    require(not reply.get("ok"), "unbekannter Vorgang wird abgelehnt")
    require_equal(reply.get("reason"), "unknown_operation", "und benennt das auch")


def t_control_run_goes_through_the_gate_under_its_own_principal():
    """Ein oertlicher Aufruf laeuft nicht als Sprachsitzung.

    Das ist keine Kosmetik: der Principal steht im Journal, und ein Journal, das
    einen Socket-Aufruf als gesprochenen Satz ausweist, verliert genau dann
    seinen Wert, wenn jemand nachvollziehen will, wer etwas ausgeloest hat.
    """
    dispatcher = _Dispatcher()
    reply = asyncio.run(CoreControl(dispatcher).handle(
        {"op": RUN, "capability": "portal_status", "arguments": {"session": "s"}}))
    require(reply.get("ok"), "der Aufruf kommt an")
    require_equal(dispatcher.capabilities.seen_principal, CONTROL_PRINCIPAL,
                  "unter der eigenen Kennung")
    require(dispatcher.capability_gate.turn["trust"].user_authorized,
            "der Besitzer am Rechner ist ein Nutzerakt")
    require("voice" not in dispatcher.capability_gate.turn["trust"].note.lower(),
            "aber ausdruecklich keine Sprachsitzung")


def t_control_cannot_shortcut_an_approval():
    """Der Griff umgeht die Freigabe nicht — er bedient sie nur.

    Waere das anders, waere die ganze Naht ein Selbstbedienungsladen: „fuehre
    portal_login aus" ohne iPhone. Der Test bindet die Faehigkeit an eine
    Antwort, die Freigabe verlangt, und besteht darauf, dass genau die
    durchgereicht wird.
    """
    dispatcher = _Dispatcher(outcome="approval_required")
    reply = asyncio.run(CoreControl(dispatcher).handle(
        {"op": RUN, "capability": "portal_login", "arguments": {"session": "s"}}))
    require(not reply.get("ok"), "eine kritische Aktion bleibt stehen")
    require_equal(reply.get("outcome"), "approval_required",
                  "und zwar genau am Freigabepunkt")


def t_control_socket_is_a_unix_socket_not_a_port():
    """Ein Port kennt seinen Anrufer nicht. Ein Unix-Socket schon.

    Deshalb steht hier ein Pfad und keine Portnummer, und deshalb wird der
    Anrufer geprueft, bevor das erste Byte gelesen wird.
    """
    control = CoreControl(_Dispatcher())
    require(control.socket_path.endswith(".sock"), "ein Pfad, kein Port")
    source = open(os.path.join(os.path.dirname(__file__), "..", "src", "solvio",
                               "realtime", "control.py"), encoding="utf-8").read()
    where = source.index("async def _converse")
    body = source[where:source.index("async def handle")]
    require("authenticate" in body, "der Anrufer wird ueberhaupt geprueft")
    require(body.index("authenticate") < body.index("decode"),
            "die Kennung wird vor dem Inhalt geprueft")


def t_control_authentication_rejects_a_foreign_uid():
    """Gemessen, nicht behauptet: ein fremdes uid kommt nicht durch."""
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "c.sock")
        listener = PROTO.bind_listener(path, mode=0o600)
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(path)
        served, _ = listener.accept()
        try:
            PROTO.authenticate(served, allowed_uid=os.getuid() + 1)
            require(False, "ein fremdes uid haette abgelehnt werden muessen")
        except PROTO.ProtocolError:
            pass
        require_equal(PROTO.authenticate(served, allowed_uid=os.getuid()),
                      os.getuid(), "das eigene uid kommt durch")
        client.close(); served.close(); listener.close()


def t_the_control_seam_is_not_shipped_to_the_worker():
    """Der Arbeiter bekommt diesen Griff nicht. Er hat dort nichts zu suchen."""
    require("solvio/realtime/control.py" not in MODULES,
            "kein Kontrollpfad im Arbeiterbaum")
    require(not any(m.startswith("solvio/realtime/") for m in MODULES),
            "ueberhaupt nichts aus realtime/")


# -- Phase A: die App fragt von selbst --------------------------------------

def t_the_app_asks_on_its_own_and_stops_when_it_is_not_looking():
    """Die App hatte nur Ziehen-zum-Aktualisieren. Jetzt fragt sie selbst.

    Geprueft wird beides: dass es die Schleife gibt, und dass sie im
    Hintergrund endet. Eine Schleife ohne Ende ist der uebliche Weg, aus einer
    Verbesserung einen Akkufresser zu machen.
    """
    _need_ios()
    app = _read("App/App.swift")
    require(app, "App.swift gefunden")
    for needle in ("startAutoRefresh", "stopAutoRefresh", "RefreshPolicy", "scenePhase"):
        require(needle in app, f"{needle} verdrahtet")
    handler = _block(app, ".onChange(of: scenePhase)")
    require("phase == .active" in handler, "Vordergrund startet")
    require("startAutoRefresh()" in handler, "aktiv startet die Schleife")
    require("stopAutoRefresh()" in handler,
            "und jeder andere Zustand haelt sie an — im Handler selbst")
    require(handler.index("startAutoRefresh()") < handler.index("stopAutoRefresh()"),
            "aktiv startet, alles andere haelt an")
    require("else" in handler, "der Hintergrundzweig existiert")


def t_automatic_discovery_did_not_become_automatic_authority():
    """Von selbst erfahren ist nicht von selbst freigeben.

    Der gefaehrlichste denkbare Fix fuer „die Freigabe kam nie an" waere, sie
    einfach zu erteilen. Der Test besteht darauf, dass keine Entscheidung ohne
    biometrische Bestaetigung entsteht.
    """
    _need_ios()
    app = _read("App/App.swift")
    for forbidden in ("autoApprove", "approveAll", "automaticApproval"):
        require(forbidden not in app, f"kein {forbidden}")
    decide = app[app.index("func decide"):] if "func decide" in app else ""
    require("challenge" in decide, "jede Entscheidung braucht eine Challenge")
    policy = _read("Sources/SolvioApprovalsKit/RefreshPolicy.swift")
    require("approve" not in policy.lower(),
            "die Kadenz weiss nichts vom Entscheiden")


def t_polling_has_a_bounded_timeout_and_backs_off():
    """Ein Abruf ohne Frist blockiert die Schleife, sobald das Netz haengt."""
    _need_ios()
    net = _read("App/Networking.swift")
    require("timeoutIntervalForRequest" in net, "Anfragefrist gesetzt")
    require("timeoutIntervalForResource" in net, "Gesamtfrist gesetzt")
    policy = _read("Sources/SolvioApprovalsKit/RefreshPolicy.swift")
    require("ceiling" in policy and "failures" in policy, "gedeckelte Ruecknahme")


def t_transport_hardening_was_not_weakened_for_convenience():
    """Erreichbarkeit wurde nicht mit Sicherheit bezahlt."""
    _need_ios()
    net = _read("App/Networking.swift")
    session = net[net.index("URLSession(configuration:"):][:220]
    require("PinningDelegate(pinned:" in session,
            "die Sitzung selbst bekommt den Pinning-Delegaten")
    require("delegate: nil" not in session, "und nicht keinen")
    require("ephemeral" in net, "keine Sitzung auf Platte")
    for forbidden in ("allowsAnyHTTPSCertificate", "NSAllowsArbitraryLoads",
                      "serverTrust, forKey"):
        require(forbidden not in net, f"kein {forbidden}")


# -- Phase B: der Studio-Auszug ---------------------------------------------

def t_the_readout_reads_a_synthetic_complete_page():
    """Konto, Bereiche und Kennzahlen aus einer synthetischen vollstaendigen Seite."""
    out = reduce(_page())
    require_equal(out["konto"]["kundennummer"], '9000001', "Kontonummer")
    require_equal(out["konto"]["name"], 'Mara Beispiel', "Name")
    require_equal(out["konto"]["arbeitsbereich"], 'Studio Testbereich', "Arbeitsbereich")
    require_equal(out["bereiche"]["WORKFLOW"],
                  ["Wochenplaner", "Advisor", "Brand Center", "Auswertung"],
                  "die Arbeitsgruppe vollstaendig")
    require_equal(out["seite"], "Wochenplaner", "die offene Seite")


def t_a_navigation_group_does_not_swallow_the_account_block():
    """Der erste Entwurf zaehlte Name und Kontonummer als Menuepunkte.

    Das sah plausibel aus und war falsch — genau die Sorte Fehler, die ein
    Bericht nicht sichtbar macht, weil er trotzdem gefuellt aussieht.
    """
    out = reduce(_page())
    konto = out["bereiche"]["KONTO"]
    require_equal(konto, ["Performance-Sets", "Abo & Billing", "API", "Team",
                          "Einstellungen", "Hilfe", "Admin"],
                  "nur echte Menuepunkte")
    for leaked in ('Mara Beispiel', '9000001', 'MB', 'Hell'):
        require(leaked not in konto, f"{leaked} gehoert nicht in die Navigation")


def t_metrics_are_only_reported_when_the_page_actually_says_them():
    """Kein Muster, kein Eintrag — und ausdruecklich keine Null.

    Eine erfundene Null in einem Kennzahlenbericht ist schlimmer als eine
    Luecke: sie wird gelesen und geglaubt.
    """
    full = {m["label"] for m in reduce(_page())["kennzahlen"]}
    require("Frühere Wochenpläne" in full, "die synthetische Seite enthaelt sie")

    page = _page()
    page["text"] = "\n".join(line for line in page["text"].splitlines()
                             if "Frühere Wochenpläne" not in line)
    thin = reduce(page)
    labels = {m["label"] for m in thin.get("kennzahlen", [])}
    require("Frühere Wochenpläne" not in labels,
            "ohne die Zeile gibt es die Kennzahl nicht")
    require("Verwendete Themen bisher" in labels, "die uebrigen bleiben")


def t_metric_values_come_from_the_page_verbatim():
    values = {m["label"]: m["wert"] for m in reduce(_page())["kennzahlen"]}
    require_equal(values["Verwendete Themen bisher"], "10", "aus '(10)'")
    require_equal(values["Plattformen im Tarif"], "99", "aus 'bis zu 99'")
    require_equal(values["Wochenthema, Zeichen genutzt"], "0 von 200", "aus '0/200'")
    require_equal(values["Plattformen ausgewählt"], "0", "aus '0 ausgewählt'")


def t_platform_drafts_are_counted_by_their_length_line_not_their_name():
    """Plattformnamen stehen zweimal auf der Seite — als Angebot und als Arbeit.

    Wer nur den Namen sucht, meldet vier Entwuerfe, wo drei sind. Der Gegenbeweis
    steht auf derselben Seite: „3 Posts".
    """
    plan = reduce(_page())["wochenplan"]
    require_equal(plan["plattformen_mit_entwurf"], ["LinkedIn", "Instagram", "Facebook"],
                  "nur was wirklich einen Text hat")
    require_equal(plan["posts"], str(len(plan["plattformen_mit_entwurf"])),
                  "die Seite bestaetigt die Zahl selbst")
    require_equal(plan["status"], "Entwurf", "und der Zustand stimmt")
    require_equal(plan["stand"], '01.01.2030, 12:00', "mit Zeitstempel")


def t_the_readout_does_not_hand_over_the_page_contents():
    """Datensparsamkeit, gemessen an dem, was die Seite alles hergaebe.

    Auf der Seite stehen fertige Beitragstexte. Fuer „wie steht es" traegt ihr
    Wortlaut nichts bei, was die Anzahl nicht schon sagt — also verlaesst er die
    Seite nicht.
    """
    blob = json.dumps(reduce(_page()), ensure_ascii=False)
    for body in ('SYNTHETISCHER ENTWURF LINKEDIN', 'SYNTHETISCHER ENTWURF INSTAGRAM', 'SYNTHETISCHER ENTWURF FACEBOOK', '2033 Zeichen', 'Als Entwurf freigeben'):
        require(body not in blob, f"Beitragstext bleibt draussen: {body[:24]}")
    # Und die Regel dahinter, nicht nur die Stichprobe: keine lange Zeile der
    # Seite ueberlebt den Auszug. Eine Stichprobe altert mit dem Inhalt.
    for line in _page()["text"].splitlines():
        stripped = line.strip()
        if len(stripped) > 90:
            require(stripped not in blob, f"lange Zeile draussen: {stripped[:40]}")
    require(len(blob) < 1800, f"kompakt statt vollstaendig ({len(blob)} Zeichen)")
    page_length = len(_page()["text"])
    require(len(blob) < page_length // 2,
            f"deutlich weniger als die Seite ({len(blob)} von {page_length})")


def t_notices_name_what_is_actually_open():
    hints = reduce(_page())["hinweise"]
    require(any("Cookie" in h for h in hints), "der offene Cookie-Banner faellt auf")
    require(any("Entwurf" in h for h in hints), "der unfreigegebene Plan faellt auf")
    steps = reduce(_page())["naechste_schritte"]
    require_equal(steps, ["Nächster Schritt: Designs zum Wochenplan erstellen."],
                  "der naechste Schritt woertlich")


def t_a_blob_is_never_passed_off_as_a_heading():
    """Ein Absatz an der Stelle einer Ueberschrift ergibt kein Feld.

    Gefunden vom kanonischen Gate, nicht von mir: eine Seite aus 5000 Zeichen
    ohne Zeilenumbruch wurde als „offene Seite: xxxxx…" gemeldet. Der Wert war
    nicht erfunden — er stand woertlich da — und trotzdem falsch. Kuerzen waere
    die schlechtere Antwort gewesen: ein gekuerzter Absatz sieht aus wie eine
    Ueberschrift.
    """
    out = reduce({"text": "Abmelden\n" + "x" * 5000})
    require("seite" not in out, "kein Fliesstext als Ueberschrift")
    out = reduce({"text": "Abmelden\nWochenplaner"})
    require_equal(out["seite"], "Wochenplaner", "eine echte Ueberschrift bleibt")


def t_a_foreign_page_gets_no_studio_interpretation():
    """Eine Seite, die sich als Studio ausgibt, bekommt keine Studio-Auswertung."""
    require(binding_for_url("https://solvio-studio.de.evil.example/dashboard") is None,
            "ein aehnlicher Name ist eine andere Herkunft")
    require(binding_for_url("http://solvio-studio.de/dashboard") is None,
            "auch das Schema gehoert zur Herkunft")
    require_equal(binding_for_url("https://solvio-studio.de/dashboard").portal_id,
                  PORTAL_ID, "die echte Herkunft trifft")


def t_a_broken_reducer_never_breaks_the_read():
    """Ein Auszug ist eine Zugabe. Faellt er aus, bleibt der Bericht stehen."""
    def explode(_reply):
        raise RuntimeError("umgebaut")
    REDUCERS["kaputt"] = explode
    try:
        require(reduce_for("kaputt", _page()) is None, "der Fehler wird zum Nichts")
    finally:
        REDUCERS.pop("kaputt", None)
    require(reduce_for("gibt-es-nicht", _page()) is None, "unbekannt heisst nichts")


def t_the_readout_survives_a_page_that_lost_its_shape():
    """Kein Abmelden, keine Nummer, nichts — und trotzdem kein Absturz."""
    for text in ("", "nur eine Zeile", "\n\n\n", "Abmelden"):
        out = reduce({"text": text})
        require_equal(out["portal"], PORTAL_ID, "die Herkunft steht immer")
        require("konto" not in out, "kein erfundenes Konto")
        require("wochenplan" not in out, "kein erfundener Plan")


def t_studio_knowledge_stays_out_of_the_generic_foundation():
    """Die Grundlage bleibt frei von der Handschrift eines Anbieters."""
    base = os.path.join(os.path.dirname(__file__), "..", "src", "solvio", "portal")
    generic = open(os.path.join(base, "readout.py"), encoding="utf-8").read()
    for studio in ("Wochenplaner", "Performance-Set", "WORKFLOW", "Abmelden"):
        require(studio not in generic, f"kein '{studio}' in der Grundlage")
    require_equal(lines_of("a\n\n b \n"), ["a", "b"], "die Grundlage kann nur Zeilen")
    require(PORTAL_ID in REDUCERS, "und Studio meldet sich selbst an")


def t_the_readout_is_not_shipped_to_the_worker():
    """Ortskenntnis bleibt im Core. Der Arbeiter liest, er deutet nicht."""
    for module in ("solvio/portal/readout.py", "solvio/portal/studio_readout.py"):
        require(module not in MODULES, f"{module} nicht im Arbeiterbaum")


# -- Hilfen ------------------------------------------------------------------

class _Trust:
    def __init__(self, note, authorized=True):
        self.note = note
        self.user_authorized = authorized
        self.origin_trust = None


class _Gate:
    def __init__(self):
        self.turn = {}

    def begin_turn(self, **kwargs):
        self.turn = kwargs

    def context(self, session_id=""):
        # Wie der echte Kontext: Herkunft und Befehlscharakter gehoeren dazu,
        # seit die Freigabepolitik beides liest (ADR-0022).
        return type("Ctx", (), {"principal": self.turn.get("principal", ""),
                                "trust": self.turn.get("trust"),
                                "origin": self.turn.get("origin"),
                                "commanded": self.turn.get("commanded", True)})()

    def provenance_for(self, arguments):
        return {key: "control" for key in arguments}


class _Result:
    def __init__(self, outcome):
        self.outcome = type("O", (), {"value": outcome})()
        self.succeeded = outcome == "ok"
        self.reason = ""
        self.human_message = ""
        self.data = {}


class _Capabilities:
    def __init__(self, outcome="ok"):
        self.outcome = outcome
        self.seen_principal = None

    def names(self):
        return ["portal_status", "portal_login"]

    async def execute(self, name, arguments, **kwargs):
        self.seen_principal = kwargs.get("principal")
        return _Result(self.outcome)


class _Dispatcher:
    def __init__(self, outcome="ok"):
        self.capabilities = _Capabilities(outcome)
        self.capability_gate = _Gate()
        self.approver_runtime = None


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
