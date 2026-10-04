"""Das iPhone als Sprachendpunkt — und die Grenzen, die dabei nicht wackeln duerfen.

Ein zweiter Endpunkt ist die Gelegenheit, an der eine Grenze leise verrutscht.
Diese Suite prueft nicht, ob das Gespraech gut klingt, sondern ob der neue Weg
etwas kann, was er nicht koennen darf — und ob der alte Weg unveraendert
geblieben ist.

Die Leitfrage steht im Vertrag des Freigabe-Gateways selbst
(`security/mobile_approval/gateway.py:7-13`): eine Transportkennung darf LESEN,
sie darf NIE freigeben. Der Sprachweg benutzt genau diese Kennung. Er darf
deshalb ein Gespraech eroeffnen — und keinen einzigen Schritt in Richtung einer
Freigabe tun.

Was hier ausdruecklich mitgeprueft wird, weil es beim Bauen fast schiefging:

* Die Nachrichtenschleife ist EINE (`pump_endpoint`). Waere sie kopiert, wuerde
  eine der beiden Fassungen irgendwann um eine Nachricht erweitert und die
  andere nicht.
* `Session` benutzt vom Transport nur `send`. Waechst das, bricht der Adapter —
  und zwar still, mitten im Gespraech.
* Eine Sperre muss ein LAUFENDES Gespraech beenden, nicht erst das naechste.
  `verify_transport_cred` laeuft sonst genau einmal, beim Verbindungsaufbau.

ASSERTION POLICY: `require*` aus `tests/_guard.py` sind Funktionsaufrufe und
ueberleben `-O`.

Direkt: python tests/test_iphone_voice_endpoint.py
"""
import asyncio
import inspect
import io
import os
import sys
import tokenize

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))
from _guard import enforce_assertions, require, require_equal  # noqa: E402
enforce_assertions()

from solvio import voice_endpoint as VE  # noqa: E402
from solvio.capabilities import approver_runtime as AR  # noqa: E402
from solvio.realtime import core_server as CS  # noqa: E402
from solvio.security.mobile_approval import gateway as G  # noqa: E402

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _code_only(*sources: str) -> str:
    """Quelltext ohne Kommentare und Zeichenketten.

    Dieses Modul ERKLAERT ausfuehrlich, warum es keinen Weg zur Freigabe gibt.
    Eine Wortsuche ueber den Rohtext schlueg deshalb genau bei der Datei an, die
    es richtig macht — also wird tokenisiert.
    """
    kept: list[str] = []
    for source in sources:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            kept.append(token.string)
    return " ".join(kept)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ------------------------------------------------------- Autoritaet

def t_the_voice_path_offers_no_route_to_approval():
    """Der teuerste Fehler waere ein Sprachweg, der freigeben kann.

    Geprueft wird die Abwesenheit als STRUKTUR, nicht als Absicht: im ganzen
    Modul kommt kein Name des Entscheidungspfades vor.
    """
    code = _code_only(inspect.getsource(VE))
    for forbidden in ("submit_decision", "apply_mobile_decision", "confirm",
                      "issue_challenge", "decision", "approve", "execute_approved"):
        require(forbidden not in code,
                f"der Sprachweg kennt {forbidden!r} — er darf es nicht kennen")


def t_the_voice_path_never_touches_the_frozen_security_tree():
    """Es gibt genau EINEN Import aus dem eingefrorenen Baum, und der ist die
    Geraetepruefung — aufgerufen, nicht kopiert."""
    source = inspect.getsource(VE)
    imports = [line.strip() for line in source.splitlines()
               if "mobile_approval" in line and "import" in line]
    require_equal(len(imports), 1, f"mehr als ein Zugriff auf den Sicherheitsbaum: {imports}")
    require("_authed_device" in imports[0],
            f"der einzige Zugriff ist nicht die Geraetepruefung: {imports[0]}")


def t_the_device_check_is_called_and_never_reimplemented():
    """Eine zweite Fassung derselben Pruefung ist die Stelle, an der spaeter
    eine der beiden nachgeschaerft wird und die andere nicht."""
    code = _code_only(inspect.getsource(VE))
    for forbidden in ("verify_transport_cred", "compare_digest", "sha256",
                      "transport_cred_hash"):
        require(forbidden not in code,
                f"{forbidden!r} steht im Sprachweg — die Pruefung wurde nachgebaut")
    require("_authed_device" in code, "die bestehende Pruefung wird aufgerufen")


def t_an_unauthorized_device_never_gets_a_socket():
    """Vor der Pruefung passiert nichts Teures — dieselbe Haltung wie beim
    Satelliten, wo ein Unauthentifizierter das Sitzungsprotokoll nie erreicht."""
    source = inspect.getsource(VE._handle)
    # Der Rueckgabetyp steht in der Signatur und ist kein Protokollwechsel —
    # gemeint ist der AUFRUF, der den Socket wirklich oeffnet.
    check = source.index("_owner_device")
    upgrade = source.index("web.WebSocketResponse(")
    require(check < upgrade,
            "der Protokollwechsel steht vor der Geraetepruefung")
    require(check < source.index("ws.prepare"), "und vor dem Upgrade")
    require("status=401" in source, "ein fremdes Geraet bekommt eine Absage")


def t_a_revoked_device_loses_a_running_conversation():
    """Eine Sperre ist als ENDGUELTIG gemeint.

    `verify_transport_cred` laeuft sonst genau einmal, beim Verbindungsaufbau —
    ein gesperrtes Geraet behielte seine offene Sitzung bis zum Zeitablauf.
    """
    require(VE.RECHECK_SECONDS > 0, "es gibt eine Wiederholungspruefung")
    require(VE.RECHECK_SECONDS <= 60,
            "die Wiederholungspruefung ist eng genug, um zu wirken")
    watch = inspect.getsource(VE._revocation_watch)
    require("_owner_device" in watch, "sie benutzt dieselbe Pruefung")
    require("ws.close" in watch, "und beendet die Verbindung, wenn sie faellt")
    require("_revocation_watch" in inspect.getsource(VE._handle),
            "und sie laeuft waehrend des Gespraechs")


async def t_revocation_watch_really_closes_with_the_installed_aiohttp_contract():
    from unittest.mock import AsyncMock, patch
    from aiohttp import web
    signature = inspect.signature(web.WebSocketResponse.close)
    for failure in (None, RuntimeError('synthetic authorization store failure')):
        class Socket:
            closed = False
            calls = []
            async def close(self, **kwargs):
                # The old reason= call fails this actual installed signature.
                signature.bind(self, **kwargs)
                require(isinstance(kwargs.get('message'), bytes))
                self.calls.append(kwargs);self.closed = True
        ws = Socket()
        check = AsyncMock(return_value=None, side_effect=failure)
        with patch.object(VE, '_owner_device', check), patch.object(VE, 'RECHECK_SECONDS', 0):
            await asyncio.wait_for(VE._revocation_watch(object(), 'test-device', ws, 'test-session'), 1)
        require(ws.closed, 'authorization loss left a running socket open')
        require_equal(len(ws.calls), 1)
        require_equal(ws.calls[0]['code'], 4401)
        require_equal(check.await_count, 1)


def t_the_transport_credential_still_cannot_approve():
    """Der Entscheidungsweg nimmt weiterhin KEINE Transportkennung entgegen.

    Das ist die Zusage aus dem Gateway-Vertrag, und der neue Weg darf sie nicht
    aufgeweicht haben.
    """
    decision = inspect.getsource(G.h_decision)
    require("_authed_device" not in decision,
            "der Entscheidungsweg akzeptiert jetzt eine Transportkennung")


# ------------------------------------------------------- Ein Gespraech

def t_only_one_conversation_at_a_time():
    """Es gibt EINE Anbietersitzung und EIN Gespraech.

    Der Riegel ist derselbe wie fuer den Satelliten — nicht ein zweiter, der
    davon nichts weiss.
    """
    source = inspect.getsource(VE._handle)
    require("_busy" in source, "der Sprachweg kennt den Riegel")
    require("status=409" in source,
            "ein zweites Gespraech bekommt eine Auskunft, keinen rohen Abbruch")
    require("server._busy = True" in source, "und setzt ihn selbst")
    require("server._busy = False" in source, "und gibt ihn wieder frei")


def t_the_message_loop_exists_exactly_once():
    """Zwei Fassungen desselben Protokolls driften auseinander."""
    require(hasattr(CS, "pump_endpoint"), "die Schleife ist eine eigene Funktion")
    pi = inspect.getsource(CS.CoreServer._handle_pi)
    require("pump_endpoint" in pi, "der Satellit benutzt sie")
    require("session_start" not in pi,
            "der Satellitenzweig hat wieder eine eigene Schleife")
    endpoint = inspect.getsource(VE._handle)
    require("pump_endpoint" in endpoint, "das iPhone benutzt dieselbe")
    require("session_start" not in endpoint,
            "der Sprachweg hat eine eigene Schleife gebaut")


def t_the_session_only_ever_sends_on_its_transport():
    """Der Adapter bildet EINE Methode ab. Waechst die Nutzung, bricht er still.

    Diese Pruefung ist der Grund, warum der Adapter so klein sein darf.
    """
    source = inspect.getsource(CS.Session)
    used = set()
    for line in source.splitlines():
        marker = "self.ws."
        start = line.find(marker)
        while start >= 0:
            rest = line[start + len(marker):]
            name = ""
            for ch in rest:
                if ch.isalnum() or ch == "_":
                    name += ch
                else:
                    break
            if name:
                used.add(name)
            start = line.find(marker, start + 1)
    require_equal(used, {"send"},
                  f"Session benutzt am Transport mehr als send: {sorted(used)}")


def t_the_adapter_handles_both_frame_kinds():
    """JSON geht als Text, PCM geht als Bytes. Wer das verwechselt, schickt
    Audio als Zeichenkette — und der Empfaenger hoert Rauschen oder nichts."""
    sent: list[tuple[str, object]] = []

    class _FakeWS:
        closed = False

        async def send_bytes(self, data):
            sent.append(("bytes", data))

        async def send_str(self, text):
            sent.append(("str", text))

    socket = VE._EndpointSocket(_FakeWS())
    _run(socket.send(b"\x01\x02"))
    _run(socket.send('{"type":"flush"}'))
    require_equal([kind for kind, _ in sent], ["bytes", "str"],
                  "Rahmenart falsch zugeordnet")


def t_sending_after_close_is_not_an_error():
    """Der Abbau schickt noch ein `session_end`. Ins Leere zu senden darf ihn
    nicht kippen."""
    class _ClosedWS:
        closed = True

        async def send_bytes(self, data):  # pragma: no cover
            raise AssertionError("darf nicht senden")

        async def send_str(self, text):  # pragma: no cover
            raise AssertionError("darf nicht senden")

    socket = VE._EndpointSocket(_ClosedWS())
    _run(socket.send("x"))          # wirft nicht


# ------------------------------------------------------- Herkunft und Grenzen

def t_a_phone_turn_is_never_mistaken_for_a_satellite_turn():
    """Im Journal und in der Anfrage-Zusammenfassung muss unterscheidbar sein,
    wer gesprochen hat."""
    principal = VE.principal_for("dev-ec226bec-edea-4652-9963-59632482a1c1")
    require(principal.startswith("iphone-"), "das Telefon ist erkennbar")
    require(len(principal) <= 24, "und die Kennung bleibt kurz")
    for bad in ("\n", " ", ":", "/"):
        require(bad not in principal, f"{bad!r} in der Kennung")
    require_equal(VE.principal_for(""), "iphone",
                  "auch ohne Kennung entsteht etwas Eindeutiges")


def t_the_frame_size_is_bounded():
    """`client_max_size` begrenzt HTTP-Koerper, nicht WebSocket-Rahmen. Ohne
    eigene Grenze erlaubt aiohttp 4 MiB — ein synchroner Resample-Lauf ueber
    zwei Millionen Samples auf dem Eventloop, der die Freigaben bedient."""
    require(VE.MAX_FRAME <= 128 * 1024, "die Rahmengrenze ist eng")
    require("max_msg_size" in inspect.getsource(VE._handle),
            "und sie wird auch gesetzt")


def t_the_voice_endpoint_is_optional_and_never_takes_approvals_down():
    """Ein Sprachweg ist ein Produktzugewinn. Der Freigabeweg ist es nicht."""
    source = inspect.getsource(AR.ApproverRuntime._attach_voice_endpoint)
    require("try" in source and "except" in source,
            "ein Fehler beim Anhaengen reisst den Freigabeweg mit")
    require("voice_server" in source, "ohne CoreServer wird gar nicht angehaengt")
    start = inspect.getsource(AR.ApproverRuntime.start)
    require(start.index("_attach_control_center") < start.index("_attach_voice_endpoint"),
            "der Freigabeweg und das Kontrollzentrum stehen zuerst")


def t_the_frozen_gateway_module_is_untouched():
    """Angehaengt statt eingebaut — dieselbe Zusage wie beim Kontrollzentrum."""
    source = inspect.getsource(G)
    require("voice" not in source.lower(),
            "der eingefrorene Gateway weiss vom Sprachweg — er soll es nicht")
    require_equal(len(G.build_app.__code__.co_consts) > 0, True)


def t_the_deep_work_notice_is_information_and_bounded():
    """Die Auskunft ueber eine lange Aufgabe traegt keinen Zustand des Cores.

    Und sie kommt aus dem VERTRAG (Ausfuehrungsklasse), nicht aus einem
    Zeitgeber oder einer Schaetzung.
    """
    source = inspect.getsource(VE._long_capability_notice)
    require("execution_class" in source, "die Klasse entscheidet")
    require("deep" in source, "und zwar DEEP")
    for forbidden in ("sleep", "timer", "elapsed", "estimate"):
        require(forbidden not in source.lower(), f"{forbidden} — geschaetzt statt gewusst")
    require("sent" in source, "genau einmal je Sitzung")


def t_the_capability_hook_is_opt_in_and_leaves_the_satellite_untouched():
    """Voreingestellt gibt es den Haken nicht — fuer den Satelliten aendert sich
    dadurch kein einziges Byte."""
    source = inspect.getsource(CS.Session._handle_tool_calls)
    require("getattr(self" in source and "on_capability_started" in source,
            "der Haken wird nachgeschlagen, nicht vorausgesetzt")
    require("if notify is not None" in source, "und nur benutzt, wenn er da ist")
    session = CS.Session.__init__
    require("on_capability_started" not in inspect.getsource(session),
            "eine Sitzung bringt den Haken nicht von sich aus mit")


def t_no_new_port_and_no_second_trust_anchor():
    """Derselbe Port, dasselbe Zertifikat, dieselbe Registrierung."""
    code = _code_only(inspect.getsource(VE))
    for forbidden in ("TCPSite", "SSLContext", "load_cert_chain", "socket", "bind"):
        require(forbidden not in code, f"{forbidden} — der Sprachweg macht eine eigene Tuer auf")
    require("add_get" in code, "er haengt sich an die bestehende Anwendung")


def t_the_phone_is_less_eager_to_interrupt_than_the_satellite() -> None:
    """Die Bereitwilligkeit gilt pro Sitzung — und der Satellit merkt nichts davon.

    Ein Telefon hoert seinen Raum mit. Mit der Servereinstellung `high` hielt
    SOLVIO dort bei jedem Geraeusch an; gemessen mit einem leise laufenden
    Fernseher daneben. Der Satellit steht in genau dem Raum, fuer den `high`
    eingestellt wurde, und behaelt ihn deshalb.
    """
    # Eine frische Sitzung ueberschreibt NICHTS.
    source = inspect.getsource(CS.Session.__init__)
    require("self.eagerness: str | None = None" in source,
                   "eine Sitzung startet ohne eigene Bereitwilligkeit")

    # Und `_configure` faellt auf den Server zurueck, wenn nichts gesetzt ist.
    configure = inspect.getsource(CS.Session._configure)
    require("self.eagerness or self.server.eagerness" in configure,
                   "ohne eigenen Wert gilt der des Servers — der Pi-Pfad bleibt gleich")

    # Der Endpunkt setzt einen zurueckhaltenderen Wert.
    require(VE.PHONE_EAGERNESS in ("low", "medium", "high", "auto"),
            "ein Wert, den der Anbieter kennt")
    handler = inspect.getsource(VE._handle)
    require('getattr(server, "phone_eagerness"' in handler,
            "und der Endpunkt liest die Einstellung, statt eine Zahl festzuschreiben")

    # Zwei getrennte Einstellungen, nicht eine — sonst aendert der Wunsch des
    # Telefons den Satelliten mit.
    init = inspect.getsource(CS.CoreServer.__init__)
    require("self.phone_eagerness = phone_eagerness" in init,
            "der Server fuehrt beide Werte getrennt")
    require("phone_eagerness: str = " in init,
            "und das Geraet in der Hand hat einen eigenen Vorgabewert")


def t_the_phone_may_fall_silent_but_never_ends_the_turn() -> None:
    """Sofort still werden darf das Geraet. Entscheiden darf es nicht.

    Gemessen vergingen 162 bis 752 ms, bis die Erkennung des Anbieters eine
    Unterbrechung erkannte — so lange redete SOLVIO weiter, obwohl laengst
    jemand sprach. Das Telefon wird deshalb von sich aus still und meldet es.

    Die Grenze verlaeuft zwischen „leise sein" und „die Runde ist vorbei".
    Das Erste ist Hardware, das Zweite ist Gespraechswahrheit — und die gehoert
    dem Core.
    """
    import inspect
    loop = inspect.getsource(CS.pump_endpoint)
    require('elif t == "barge_in":' in loop,
            "die gemeinsame Schleife kennt die Meldung")
    require("sess.note_barge_in(" in loop,
            "und reicht sie an den Core weiter, statt selbst zu handeln")
    require("sess.responding" in loop,
            "nur waehrend SOLVIO wirklich spricht")

    # Der Core tut daraufhin GENAU das, was er auch sonst tut.
    note = inspect.getsource(CS.Session.note_barge_in)
    require("self._barge_in(" in note,
            "es ist derselbe Weg wie bei der Erkennung des Anbieters")

    # Der Satellitenpfad ruft weiterhin ohne Angabe auf — er weiss es nicht.
    reader = inspect.getsource(CS.Session._oa_reader)
    require("await self._barge_in()" in reader,
            "der Satellitenpfad bleibt, wie er war")


def t_a_guessed_number_is_worse_than_none() -> None:
    """Was der Mensch gehoert hat, wird gesagt — oder gar nichts.

    `conversation.item.truncate` schneidet das Gedaechtnis des Modells auf das
    zurueck, was wirklich zu hoeren war. Eine geratene Zahl waere schlimmer als
    keine: sie wuerde behaupten, der Mensch habe etwas gehoert, das er nie
    gehoert hat — und darauf baut das Modell dann seine naechste Antwort.
    """
    import inspect
    src = inspect.getsource(CS.Session._truncate_heard)
    require("played_ms is None" in src, "ohne Angabe passiert nichts")
    require("not self._audio_item" in src,
            "und ohne bekannten Gegenstand ebenfalls nicht")
    require("conversation.item.truncate" in src, "sonst wird geschnitten")
    require("audio_end_ms" in src, "und zwar auf das Gehoerte")


def t_the_phone_is_not_read_its_own_notifications_aloud() -> None:
    """Was auf dem Bildschirm steht, muss nicht vorgelesen werden.

    Der Hinweis auf Hintergrundmeldungen verlaengerte ausgerechnet den ERSTEN
    Turn — den, an dem sich der Eindruck bildet — um Ton, den niemand
    angefordert hatte. Auf dem Telefon steht dasselbe im Tab „Hinweise", mit
    Zaehler.

    Der Satellit behaelt es: er hat keinen Bildschirm, dort waere Weglassen
    kein Aufraeumen, sondern Informationsverlust ohne Ersatz.
    """
    import inspect
    handler = inspect.getsource(VE._handle)
    require("sess.mention_proactive = False" in handler,
            "das Telefon schaltet es ab")

    init = inspect.getsource(CS.Session.__init__)
    require("self.mention_proactive = True" in init,
            "und wer nichts sagt, bekommt es weiterhin — der Satellit merkt nichts")

    mention = inspect.getsource(CS.Session._mention_proactive)
    require("if not self.mention_proactive:" in mention,
            "die Pruefung steht VOR dem Zugriff auf die Ablage")
    require(mention.index("if not self.mention_proactive:")
            < mention.index("proactive_store"),
            "sonst wird gelesen, was nie gesagt wird")


def t_a_spoken_stop_reaches_the_phone_as_a_reason_it_can_act_on() -> None:
    """„SOLVIO, stopp" — und der Sprachraum geht zu, ohne ein Wort.

    Der Mechanismus ist der freigegebene: `is_silent_stop` liest das
    Transkript, `_silent_stop` bricht die Antwort ab, leert den Endpunkt und
    schliesst mit dem Grund `silent_stop`. Er haengt an keinem Transport und
    gilt deshalb fuer Satellit und Telefon gleichermassen — hier wird nur
    festgehalten, dass der GRUND beim Endpunkt ankommt. Ohne ihn koennte das
    Telefon einen ausdruecklichen Stopp nicht von einem Zeitablauf
    unterscheiden und wuerde beides mit „Bis spaeter" beantworten.
    """
    import inspect
    require(CS.is_silent_stop("Solvio, stop."), "der Satz wird erkannt")
    require(CS.is_silent_stop("stopp"), "auch ohne Anrede")
    require(not CS.is_silent_stop("solvio"), "die blosse Anrede ist kein Stopp")

    stop = inspect.getsource(CS.Session._silent_stop)
    require("response.cancel" in stop, "die laufende Antwort wird abgebrochen")
    require('"flush"' in stop, "und was schon gesendet ist, wird verworfen")
    require('reason="silent_stop"' in stop, "der Grund traegt bis zum Endpunkt")

    # Und der Stopp-Satz wird NICHT Teil des Gespraechs: die Pruefung steht vor
    # dem Persistieren, mit `return`.
    reader = inspect.getsource(CS.Session._oa_reader)
    require(reader.index("is_silent_stop(transcript)")
            < reader.index("_persist_message(ROLE_USER"),
            "sonst stuende der Stopp-Satz als Nutzertext im Gedaechtnis")


def t_reporting_what_was_heard_is_not_a_second_interruption() -> None:
    """Zwei verschiedene Dinge brauchen zwei verschiedene Namen.

    Merkt der ANBIETER die Unterbrechung zuerst, hat der Core bereits
    abgebrochen und `flush` geschickt. Das Geraet reicht danach nur noch die
    Wahrheit nach: wie viel von der Antwort wirklich zu hoeren war. Trug diese
    Auskunft denselben Namen wie eine Unterbrechung, brach der Core ein zweites
    Mal ab — und im Log sah eine blosse Meldung aus wie ein Ereignis.
    """
    import inspect
    loop = inspect.getsource(CS.pump_endpoint)
    require('elif t == "heard":' in loop, "die Meldung hat einen eigenen Namen")
    require("sess.note_heard(" in loop, "und einen eigenen Weg")

    heard = inspect.getsource(CS.Session.note_heard)
    require("_truncate_heard" in heard, "sie reicht nur die Wahrheit nach")
    require("_barge_in" not in heard, "und bricht NICHTS ein zweites Mal ab")

    # Und der Weg fuer die echte Unterbrechung ist davon unberuehrt.
    note = inspect.getsource(CS.Session.note_barge_in)
    require("self._barge_in(" in note, "eine echte Unterbrechung bricht weiterhin ab")


# ------------------------------------------------------- N8/C3 §7: Sprachbindung an einen Chat
#
# Echte Anwendung, echte Kontrollebene mit synthetisch attestiertem Geraet, echter
# WebSocket — nur der Anbieter ist eine Attrappe. Der Chat-Owner ist der
# GERAETE-Principal aus dem Sitzungsbeweis, nie der Anzeigename `iphone-…`.

async def _iphone_world():
    import base64
    import json
    import tempfile
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import patch
    from aiohttp import WSMsgType, web
    from aiohttp.test_utils import TestClient, TestServer
    import mobile_attest_helper as H
    from test_dashboard_task_approval import _wire
    from test_m0_realtime_latency import _FakeProvider, _openable_session
    from solvio import voice_session_proof as VSP
    from solvio.conversation import ConversationStore

    @asynccontextmanager
    async def world():
        with tempfile.TemporaryDirectory(prefix="solvio-iphone-voice-bind-") as folder:
            with patch.dict(os.environ, {"SOLVIO_STATE_DIR": folder}):
                store, cp, co, _ = await _wire(folder)
                device = await H.enroll_attested(cp, device_id="iphone-bind-device", transport_cred="temporary-voice-transport")
                chats = ConversationStore(os.path.join(folder, "conversations.sqlite3")).open()
                server = _openable_session().server
                server._busy = False
                server._closing = False
                server.conversations = chats
                app = web.Application()
                app["control_plane"] = cp
                VE.attach(app, server)
                http = TestServer(app)
                await http.start_server()
                client = TestClient(http)
                await client.start_server()
                providers, sessions = [], []
                actual_session = CS.Session

                def session(*args, **kwargs):
                    value = actual_session(*args, **kwargs)
                    sessions.append(value)
                    return value

                async def connect(*args, **kwargs):
                    provider = _FakeProvider()
                    providers.append(provider)
                    return provider
                headers = {"X-Device-Id": device.device_id, "X-Transport-Cred": "temporary-voice-transport"}

                async def open_socket(*, conversation_id="", prove=True):
                    ws = await client.ws_connect(VE.PATH, headers=headers)
                    challenge = json.loads((await ws.receive()).data)
                    require_equal(challenge["type"], "session_challenge")
                    binding = VSP.canonical_bytes(VSP.build_binding(
                        core_instance_id=challenge["core_instance_id"], device_id=device.device_id,
                        session_nonce=challenge["session_nonce"], conversation_id=conversation_id))
                    from solvio.security.mobile_approval import app_attest as AA
                    assertion = AA.fake_assertion(device.aakey, VSP.client_data_hash(binding), 1) if prove else b"junk"
                    answer = {"type": "session_assertion", "session_nonce": challenge["session_nonce"],
                              "assertion": base64.b64encode(assertion).decode()}
                    if prove == "unreadable":
                        answer["assertion"] = "%%%not-base64%%%"
                    if conversation_id:
                        answer["conversation_id"] = conversation_id
                    await ws.send_str(json.dumps(answer))
                    return ws

                async def closed_with(ws, code):
                    for _ in range(20):
                        msg = await asyncio.wait_for(ws.receive(), 2)
                        if msg.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                            require_equal(ws.close_code, code, f"closed with {ws.close_code}, not {code}")
                            return
                    raise AssertionError("socket did not close")

                async def receive(ws, kind):
                    for _ in range(20):
                        msg = await asyncio.wait_for(ws.receive(), 2)
                        if msg.type == WSMsgType.TEXT and json.loads(msg.data).get("type") == kind:
                            return json.loads(msg.data)
                        if msg.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                            raise AssertionError("socket ended before " + kind)
                    raise AssertionError("no " + kind)
                w = SimpleNamespace(cp=cp, device=device, chats=chats, server=server, app=app, client=client,
                                    providers=providers, sessions=sessions, open_socket=open_socket,
                                    closed_with=closed_with, receive=receive, principal="local-owner")
                with patch.object(CS, "Session", session), patch.object(CS, "ws_connect", connect):
                    try:
                        yield w
                    finally:
                        await client.close()
                        await http.close()
                        chats.close()
                        await store.close()
    return world


async def t_c3_an_assertion_with_a_conversation_binds_the_device_principals_chat_not_the_display_name():
    world = await _iphone_world()
    async with world() as w:
        chat, _ = w.chats.create_conversation(owner_principal=w.principal, kind="text", client_request_id="chat-0001")
        cid = chat["conversation_id"]
        w.chats.add_message(cid, "user", "Merk dir das Testwort Zaunkoenig.")
        before = w.chats.conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        ws = await w.open_socket(conversation_id=cid)
        await ws.send_str('{"type": "session_start"}')
        await w.receive(ws, "session_ready")
        sess = w.sessions[0]
        require(sess.interactive_proof, "the proof was not accepted")
        require_equal(sess.satellite_id, VE.principal_for(w.device.device_id))
        require(sess.satellite_id != w.principal, "this test needs a display name that is not the principal")
        require_equal(sess.conversation_principal(), w.principal)
        require_equal((sess.bound_conversation_id, sess.conversation_id, sess.conversation_mode), (cid, cid, "active"))
        require_equal(w.chats.conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0], before,
                      "a linger conversation was created next to the bound chat")
        require_equal([row["session_id"] for row in w.chats.sessions_of(cid)], [sess.session_id])
        await ws.send_str('{"type": "session_end"}')
        await ws.close()
    # Alte Bindungsbytes ohne Chatwunsch bleiben unveraendert.
    from solvio import voice_session_proof as VSP
    plain = VSP.build_binding(core_instance_id="core", device_id="dev", session_nonce="n")
    require_equal(plain, VSP.build_binding(core_instance_id="core", device_id="dev", session_nonce="n", conversation_id=""))
    require("conversation_id" not in plain)
    require_equal(VSP.build_binding(core_instance_id="core", device_id="dev", session_nonce="n",
                                    conversation_id="c-0123456789abcdef")["conversation_id"], "c-0123456789abcdef")


async def t_c3_a_chat_request_without_proof_or_for_a_foreign_chat_closes_4401_and_binds_nothing():
    world = await _iphone_world()
    async with world() as w:
        mine, _ = w.chats.create_conversation(owner_principal=w.principal, kind="text", client_request_id="chat-0001")
        foreign, _ = w.chats.create_conversation(owner_principal="someone-else", kind="text", client_request_id="chat-0002")
        voice_id, _ = w.chats.begin_session("s-voice-elsewhere")
        # Auch eine unbrauchbare Kennung ist ein VERLANGTER Chat: 4401, nie ungebunden weiter
        # (Review Runde 4, B-H3 — vorher lief die Sitzung dann im Fail-down ohne Bindung).
        for conversation_id, prove in ((mine["conversation_id"], False), (foreign["conversation_id"], True),
                                       (voice_id, True), ("c-00000000000000ff", True),
                                       ("not-a-chat-id", True), (mine["conversation_id"], "unreadable")):
            ws = await w.open_socket(conversation_id=conversation_id, prove=prove)
            await w.closed_with(ws, 4401)
            await asyncio.sleep(0.05)
            require_equal(w.providers, [], "a refused binding opened a provider session")
            require(not w.server._busy)
            require_equal(w.sessions[-1].conversation_mode, "refused")
            require_equal(w.sessions[-1].bound_conversation_id, "")
        # Nichts wurde gebunden oder angelegt: die Chats tragen keine Sitzung.
        for cid in (mine["conversation_id"], foreign["conversation_id"]):
            require_equal(w.chats.sessions_of(cid), [])
        require_equal(w.chats.conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0], 3)
        # Ohne Chatwunsch: Fail-down wie bisher — und das Linger-Gespraech einer Sitzung
        # OHNE Beweis hat einen leeren Owner (nie `iphone-…`).
        ws = await w.open_socket(prove=False)
        await ws.send_str('{"type": "session_start"}')
        await w.receive(ws, "session_ready")
        sess = w.sessions[-1]
        require(not sess.interactive_proof)
        require_equal(sess.conversation_principal(), "")
        require_equal(sess.bound_conversation_id, "")
        linger = w.chats.conversation(sess.conversation_id)
        require_equal((linger["owner_principal"], linger["explicit"], linger["kind"]), ("", 0, "voice"))
        await ws.close()
        # Ein bewiesener Beweis ohne Chatwunsch: Linger-Gespraech mit dem GERAETE-Principal.
        await asyncio.sleep(0.05)
        w.chats.conn.execute("UPDATE conversations SET last_activity_at = 0")
        ws = await w.open_socket(prove=True)
        await ws.send_str('{"type": "session_start"}')
        await w.receive(ws, "session_ready")
        sess = w.sessions[-1]
        require(sess.interactive_proof)
        linger = w.chats.conversation(sess.conversation_id)
        require_equal((linger["owner_principal"], linger["explicit"]), (w.principal, 0))
        require(linger["owner_principal"] != sess.satellite_id)
        await ws.close()


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
