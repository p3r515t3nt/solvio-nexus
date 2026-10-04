"""Voice UX V2 — der Satzanfang, die Turn-Identitaet und die Fluechtigkeit.

DREI DINGE, GEMESSEN BEVOR SIE GEAENDERT WURDEN.

**Der Satzanfang verschwand.** `feed_audio` gab jedes Frame auf, solange die
Sitzung nicht `active` war. Aus 44 echten Sitzungen: zwischen „der Satellit ist
da" und „der Anbieter kann hoeren" lagen 530 bis 1675 ms (Median 1111). Elf von
42 Sitzungen endeten mit `turns=0` — der Mensch sprach, und es kam nie etwas an.
Gehalten wird jetzt, nicht verworfen.

**Altes Audio sprach ueber die neue Frage.** Beim Unterbrechen ging ein `flush`
an den Satelliten, aber die Deltas der abgebrochenen Antwort wurden weiter
durchgereicht — der Anbieter hoerte auf zu erzeugen, was schon unterwegs war kam
trotzdem an. Jetzt entscheidet ein Tor plus die Kennung der toten Antwort.

**Der Puffer haelt rohes Raumaudio.** Deshalb steht hier mehr ueber das, was er
NICHT kann, als ueber das, was er kann: keine Datei, kein `repr` mit Inhalt, kein
Ueberdauern der Sitzung.

ASSERTION POLICY: `require*` aus `tests/_guard.py` sind Funktionsaufrufe und
ueberleben `-O`.

Direkt: python tests/test_voice_ux_v2.py
"""
import asyncio
import ast as _ast
import io
import tokenize
import base64
import inspect
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions  # noqa: E402
enforce_assertions()

from _guard import require, require_equal  # noqa: E402
from solvio.realtime import core_server as CS  # noqa: E402
from solvio.realtime import preroll as PR  # noqa: E402

SRC = os.path.join(os.path.dirname(__file__), "..", "src", "solvio")


def _run(coro):
    return asyncio.run(coro)


def _without_comments(source: str) -> str:
    """Quelltext ohne Kommentare — Zeichenketten bleiben.

    Noetig, weil die Kommentare hier den alten Zustand ZITIEREN, um zu
    erklaeren, was geaendert wurde. Ein Test, der nach `await sess.open()`
    sucht, faende sonst die Erklaerung statt des Codes und waere fuer immer rot.

    Die Zeichenketten muessen bleiben: der gepruefte Zweig IST eine
    (`if t == "session_start"`). Sie mit wegzuwerfen war der erste Versuch,
    und danach fand der Test seinen eigenen Suchbegriff nicht mehr.
    """
    return " ".join(token.string for token in
                    tokenize.generate_tokens(io.StringIO(source).readline)
                    if token.type != tokenize.COMMENT)


class _Satellite:
    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(data)

    async def close(self, *a, **kw):
        pass

    remote_address = ("192.168.178.194", 51000)

    def audio(self):
        return [d for d in self.sent if isinstance(d, (bytes, bytearray))]

    def messages(self):
        out = []
        for d in self.sent:
            if isinstance(d, str):
                try:
                    out.append(json.loads(d))
                except ValueError:
                    pass
        return out


class _Provider:
    """Nimmt entgegen, was der Core schickt — und merkt es sich."""

    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(json.loads(data) if isinstance(data, str) else data)

    async def close(self, *a, **kw):
        pass

    def appended_audio(self) -> bytes:
        """Alles, was als Eingangsaudio ankam — in Reihenfolge, entpackt."""
        out = b""
        for msg in self.sent:
            if isinstance(msg, dict) and msg.get("type") == "input_audio_buffer.append":
                out += base64.b64decode(msg["audio"])
        return out

    def types(self):
        return [m.get("type") for m in self.sent if isinstance(m, dict)]


def _session(*, active=False):
    server = CS.CoreServer.__new__(CS.CoreServer)
    server.dispatcher = None
    server.idle_timeout = 999
    server.credentials = None
    server.model = "gpt-realtime"
    satellite = _Satellite()
    session = CS.Session(server, satellite)
    session.active = active
    return session, satellite


# -- Der Satzanfang ------------------------------------------------------------

def t_audio_before_the_provider_is_ready_is_held_not_dropped():
    """Der gemessene Defekt: 530 bis 1675 ms Sprache fielen ins Nichts.

    Der frueher hier stehende `return` in `feed_audio` war die ganze Ursache.
    """
    session, _ = _session(active=False)
    _run(session.feed_audio(b"\x01\x02" * 400))
    _run(session.feed_audio(b"\x03\x04" * 400))
    require(not session.preroll.empty, "es wurde gehalten, nicht verworfen")
    require_equal(session.preroll.held_bytes, 1600,
                  f"und zwar alles, waren {session.preroll.held_bytes} Bytes")


def t_the_held_beginning_reaches_the_provider_in_order():
    """Reihenfolge ist die halbe Zusage.

    Ein Vorlauf, der in falscher Reihenfolge ankommt, ist fuer den Anbieter
    kein Satz mehr, sondern Kauderwelsch — und schlimmer als gar keiner, weil
    er auch noch plausibel klingt.
    """
    session, satellite = _session(active=False)
    first, second, third = b"\x11\x11" * 100, b"\x22\x22" * 100, b"\x33\x33" * 100
    for frame in (first, second, third):
        _run(session.feed_audio(frame))

    provider = _Provider()
    session.oa = provider
    session.active = True
    _run(session._flush_preroll())

    got = provider.appended_audio()
    require(got, "der Anbieter hat den Vorlauf bekommen")
    # Der Vorlauf wird beim Senden hochgesampelt; die Reihenfolge muss die
    # Reihenfolge der Muster bleiben.
    positions = [got.find(bytes([b, b])) for b in (0x11, 0x22, 0x33)]
    require(all(p >= 0 for p in positions),
            f"alle drei Abschnitte sind enthalten: {positions}")
    require(positions[0] < positions[1] < positions[2],
            f"und in der gesprochenen Reihenfolge: {positions}")


def t_the_preroll_is_flushed_exactly_once():
    """Zweimal gesendetes Audio ist fuer den Anbieter ein wiederholter Satz.

    Er kann nicht wissen, dass es dasselbe Sprechen war — er hoert es zweimal
    und antwortet entsprechend. Die Zusage haengt allein daran, dass `drain()`
    beim Auslesen leert; eine zusaetzliche Marke „schon abgeflossen" gab es
    kurz und war schaedlich, siehe `_flush_preroll`.
    """
    session, _ = _session(active=False)
    _run(session.feed_audio(b"\x07\x07" * 200))
    provider = _Provider()
    session.oa = provider
    session.active = True
    _run(session._flush_preroll())
    after_first = len(provider.appended_audio())
    _run(session._flush_preroll())
    require_equal(len(provider.appended_audio()), after_first,
                  "der zweite Aufruf schickt nichts mehr")
    require(session.preroll.empty, "und der Puffer ist leer")


def t_audio_held_after_a_drop_is_flushed_on_the_next_ready():
    """Der Fall, den eine zu eifrige Sperre kaputtgemacht haette.

    Faellt die Anbieterverbindung mitten im Satz, laeuft das Weitergesprochene
    wieder in den Puffer. Beim Wiederverbinden muss es hinaus — sonst ist der
    Abbruch fuer den Menschen ein Wortverlust statt einer Pause.
    """
    session, _ = _session(active=False)
    provider = _Provider()
    session.oa = provider
    session.active = True
    _run(session.feed_audio(b"\x11\x11" * 100))
    _run(session._flush_preroll())

    session.active = False                     # die Verbindung faellt
    _run(session.feed_audio(b"\x55\x55" * 100))
    require(not session.preroll.empty, "das Weitergesprochene wird gehalten")

    session.active = True                      # und kommt zurueck
    _run(session._flush_preroll())
    require(session.preroll.empty, "beim naechsten Bereitsein fliesst es ab")
    require(bytes([0x55, 0x55]) in provider.appended_audio(),
            "und erreicht den Anbieter wirklich")


def t_live_audio_after_the_flush_still_reaches_the_provider():
    """Der Puffer darf den lebenden Strom nicht ersetzen."""
    session, _ = _session(active=False)
    _run(session.feed_audio(b"\x11\x11" * 100))
    provider = _Provider()
    session.oa = provider
    session.active = True
    _run(session._flush_preroll())
    before = len(provider.appended_audio())
    _run(session.feed_audio(b"\x44\x44" * 100))
    require(len(provider.appended_audio()) > before,
            "danach gesprochenes kommt weiterhin an")


def t_the_preroll_is_bounded_and_drops_whole_frames():
    """Begrenzt in Sekunden, nicht in Frames — und nie ein halbes Frame.

    Ein um ein Byte verschobener PCM-Strom ist kein leiserer Ton, sondern
    Rauschen.
    """
    buffer = PR.Preroll(rate=16000, seconds=0.1)      # 3200 Bytes
    buffer.add(b"A" * 2000)
    buffer.add(b"B" * 2000)
    require(buffer.held_bytes <= 3200, "die Grenze haelt")
    require(buffer.overflowed, "und der Ueberlauf wird gemeldet")
    held = buffer.drain()
    require_equal(len(held) % PR.SAMPLE_WIDTH, 0,
                  "was bleibt, ist auf ganze Samples ausgerichtet")
    require_equal(held, b"B" * 2000, "das aelteste Frame fiel ganz heraus")


def t_the_bound_is_generous_against_the_measured_worst_case():
    """Vier Sekunden gegen gemessene 1675 ms.

    Die Zahl ist gemessen, nicht geraten: das langsamste Oeffnen von 44 echten
    Sitzungen lag bei 1675 ms. Wird die Grenze je unter das Doppelte davon
    gesenkt, faengt der Puffer den schlechten Tag nicht mehr auf.
    """
    require(PR.DEFAULT_SECONDS >= 3.35,
            f"mindestens das Doppelte des gemessenen Maximums, "
            f"sind {PR.DEFAULT_SECONDS}s")
    require(PR.DEFAULT_SECONDS <= 10.0,
            "und kurz genug, dass es kein Mitschnitt des Raumes wird")


def t_opening_the_provider_does_not_block_the_satellite_loop():
    """Der eigentliche Umbau, und der Grund, warum der Puffer ueberhaupt greift.

    Frueher lag `await sess.open()` in derselben Schleife, die die Frames
    entgegennimmt. Solange geoeffnet wurde — gemessen 530 bis 1675 ms — nahm
    diese Schleife nichts entgegen. websockets bremst den Absender dann aus
    (max_queue=16, rund 320 ms Audio); was der Satellit in seiner eigenen
    Warteschlange nicht mehr unterbringt, entscheidet er allein, und der Core
    hatte es verursacht, ohne es sehen zu koennen.

    Geprueft wird die Bauform: in dem Zweig, der `session_start` behandelt,
    darf kein `await` auf das Oeffnen stehen.

    Die Schleife ist seit dem iPhone-Sprachendpunkt EINE — `pump_endpoint` —
    und wird vom Satelliten wie vom Telefon benutzt. Diese Pruefung gilt damit
    fuer beide Endpunkte statt nur fuer den Satelliten; genau deshalb ist die
    Schleife geteilt und nicht kopiert worden.
    """
    source = _without_comments(inspect.getsource(CS.pump_endpoint))
    branch = source[source.index('"session_start"'):]
    branch = branch[:branch.index('"session_end"')]
    require("open" not in branch.replace("begin_open", ""),
            "das Oeffnen wird nicht mehr in der Schleife abgewartet")
    require("begin_open" in branch, "sondern nebenher gestartet")

    starter = inspect.getsource(CS.Session.begin_open)
    require("create_task" in starter, "und zwar als eigene Task")
    require("_open_guarded" in starter,
            "mit eigener Fehlerbehandlung — eine Task, deren Ausnahme niemand "
            "abholt, laesst den Satelliten in ACTIVE stehen")


def t_the_open_task_is_cancelled_when_the_session_closes():
    """Eine halb geoeffnete Sitzung darf nicht fertig werden.

    Sonst setzt sie nach dem Schliessen noch `active`, schickt `session_ready`
    und gibt den Vorlauf an eine Verbindung ab, die es nicht mehr gibt.

    Gepruefte Wirkung, nicht der Name: eine erste Fassung suchte nur nach
    `self._open_task` im Quelltext von `close` und blieb gruen, als die Task
    aus der Abbruchliste entfernt wurde — die Zuweisung `= None` stand ja noch
    da. Auch ein bestehender Test des Sprachpfads uebersah das.
    """
    async def scenario():
        session, _ = _session(active=True)
        running = asyncio.Event()

        async def hangs():
            running.set()
            await asyncio.sleep(3600)

        session.open = hangs
        session.begin_open()
        await running.wait()
        task = session._open_task
        await session.close(reason="disconnect")
        for _ in range(5):                     # dem Abbruch Zeit geben
            await asyncio.sleep(0)
        # INNERHALB der Schleife pruefen: `asyncio.run` bricht beim Verlassen
        # ohnehin jede uebrige Task ab. Draussen gemessen misst man den
        # Aufraeumer der Schleife und nicht `close` — die erste Fassung dieses
        # Tests blieb genau deshalb gruen, als die Task aus der Abbruchliste
        # entfernt wurde.
        cancelled = task.cancelled() or task.done()
        task.cancel()
        return cancelled

    require(_run(scenario()),
            "die haengende Oeffnung wurde von close() abgebrochen")


def t_a_failed_open_still_tells_the_satellite():
    """Sonst wartet er ewig.

    Scheitert das Oeffnen, gibt es keinen Reader, kein `oa` und kein `active` —
    und genau darauf sprang die Abkuerzung in `close` an und kehrte um, ohne
    dem Satelliten etwas zu sagen. Er blieb in ACTIVE stehen und wartete auf
    ein `session_ready`, das nie kam.
    """
    session, satellite = _session(active=False)
    session.open_attempts = 1                  # es gab ein session_start
    _run(session.close(reason="open_failed"))
    ends = [m for m in satellite.messages() if m.get("type") == "session_end"]
    require(ends, f"der Satellit wurde benachrichtigt: {satellite.messages()}")
    require_equal(ends[0].get("reason"), "open_failed", "und zwar wahrheitsgemaess")


def t_a_session_that_never_started_says_nothing():
    """Die Abkuerzung selbst bleibt richtig.

    `_handle_pi` ruft `close()` im finally immer auf. Ohne `session_start` gibt
    es nichts zu beenden, und ein `session_end` waere eine Antwort auf eine
    Frage, die niemand gestellt hat.
    """
    session, satellite = _session(active=False)
    _run(session.close(reason="disconnect"))
    require(not satellite.messages(),
            f"nichts gesendet: {satellite.messages()}")


# -- Fluechtigkeit -------------------------------------------------------------

def t_the_preroll_never_touches_the_filesystem():
    """Die Zusage steht in der Bauform, nicht in einer Regel.

    Gesucht wird in der Quelle nach allem, womit man etwas ablegen koennte.
    Findet sich nichts davon, kann der Puffer nichts hinterlassen — ganz
    gleich, was jemand spaeter aufruft.
    """
    source = open(os.path.join(SRC, "realtime", "preroll.py"), encoding="utf-8").read()
    tree = _ast.parse(source)
    verboten = {"open", "write", "dump", "dumps", "save", "pickle", "flush_to_disk"}
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Call):
            name = getattr(node.func, "id", "") or getattr(node.func, "attr", "")
            require(name not in verboten,
                    f"der Puffer ruft nichts Ablegendes auf, fand {name!r}")
        if isinstance(node, (_ast.Import, _ast.ImportFrom)):
            module = getattr(node, "module", "") or ""
            names = [a.name for a in node.names]
            for candidate in [module] + names:
                require(candidate.split(".")[0] not in
                        {"os", "io", "pathlib", "shutil", "sqlite3", "json", "pickle",
                         "tempfile", "logging"},
                        f"und importiert nichts, womit man ablegt: {candidate!r}")


def t_the_preroll_does_not_show_its_content_when_printed():
    """Ein Puffer, der in einem Stacktrace auftaucht, verraet sonst genau das,
    was er schuetzen soll."""
    buffer = PR.Preroll(rate=16000, seconds=1.0)
    buffer.add(b"GEHEIMES RAUMAUDIO" * 20)
    text = repr(buffer) + str(buffer)
    require("GEHEIM" not in text, f"kein Inhalt in der Darstellung: {text[:80]!r}")
    stats = buffer.stats()
    require(all(isinstance(v, (int, float, bool)) for v in stats.values()),
            f"und die Kennzahlen sind Mengen, kein Inhalt: {stats}")


def t_closing_a_session_forgets_the_held_audio():
    """Rohes Raumaudio ueberdauert die Sitzung nicht — auch nicht im Fehlerfall.

    `close` laeuft auf jedem Weg, einschliesslich `open_failed`; deshalb steht
    das Loeschen dort und nicht am guten Ausgang.
    """
    session, _ = _session(active=False)
    _run(session.feed_audio(b"\x09\x09" * 500))
    require(not session.preroll.empty, "vorher liegt etwas drin")
    _run(session.close(reason="open_failed"))
    require(session.preroll.empty,
            "danach nichts mehr — auch auf dem Weg, auf dem die Sitzung nie "
            "geoeffnet wurde und close frueh umkehrt")


def t_no_raw_audio_is_logged_anywhere_in_the_voice_path():
    """Kein Frame, kein Base64, kein Transkriptkoerper in einer Logzeile.

    Geprueft wird der Aufruf selbst: ein `log.*(...)`, dem eine Variable mit
    Audio- oder Transkriptbezug uebergeben wird, faellt auf.
    """
    source = open(os.path.join(SRC, "realtime", "core_server.py"),
                  encoding="utf-8").read()
    tree = _ast.parse(source)
    verdaechtig = {"pcm", "pcm16", "pcm24", "audio", "delta", "raw", "frame",
                   "transcript", "txt", "held"}
    for node in _ast.walk(tree):
        if not isinstance(node, _ast.Call):
            continue
        func = node.func
        if not (isinstance(func, _ast.Attribute)
                and isinstance(func.value, _ast.Name) and func.value.id == "log"):
            continue
        for argument in list(node.args) + [k.value for k in node.keywords]:
            if isinstance(argument, _ast.Name):
                require(argument.id not in verdaechtig,
                        f"log-Zeile in Zeile {node.lineno} gibt {argument.id!r} weiter")


def t_a_dead_response_does_not_close_the_living_turn():
    """Ein Folgefehler des Tores, und der unangenehmste.

    Die abgebrochene Antwort schickt noch ein `response.done`. Das traf den
    NEUEN Turn: es stempelte `response_done` auf ihn, gab ihn aus — gemessen
    2 ms, naemlich von SEINEM Sprechbeginn bis zum Ende der VORIGEN Antwort —
    und leerte ihn. Danach war jede weitere Marke wirkungslos, und jede
    Folgezeile trug `turn_id=None`. Der echte Turn bekam nie eine Zeile.

    Eine erfundene Latenz ist schlimmer als eine fehlende.
    """
    lines = []

    class _Log:
        def info(self, event, **kw):
            lines.append((event, kw))
        def warning(self, event, **kw):
            lines.append((event, kw))
        def error(self, event, **kw):
            lines.append((event, kw))

    frames = [
        json.dumps({"type": "input_audio_buffer.speech_started"}),
        json.dumps({"type": "response.created", "response": {"id": "resp_alt"}}),
        json.dumps({"type": "input_audio_buffer.speech_started"}),   # unterbricht
        json.dumps({"type": "response.done", "response": {"id": "resp_alt",
                                                          "output": []}}),
    ]

    class _Stream:
        def __init__(self, items):
            self._items = list(items)
            self.sent = []
        def __aiter__(self):
            async def gen():
                for item in self._items:
                    yield item
            return gen()
        async def send(self, data):
            self.sent.append(data)
        async def close(self, *a, **kw):
            pass

    async def scenario():
        session, _ = _session(active=True)
        session.oa = _Stream(frames)
        real = CS.log
        CS.log = _Log()
        try:
            await session._oa_reader()
        finally:
            CS.log = real
        if session._reconnect_task is not None:
            session._reconnect_task.cancel()
        return session

    session = _run(scenario())
    turns = [kw for name, kw in lines if name == "core.turn_latency"]
    require(not turns,
            f"die tote Antwort gibt keinen Turn aus: {turns}")
    require(any(name == "voice.dead_response_done" for name, _ in lines),
            "sondern wird als das gemeldet, was sie ist")
    require(session._turn, "und der lebende Turn bleibt bestehen")


def t_a_response_done_cannot_close_a_turn_that_never_started_one():
    """Im echten Sprachtest gefunden, und ohne ihn nicht zu sehen.

    Faengt der Mensch an zu sprechen, waehrend die vorige Antwort noch
    ausklingt, trifft deren `response.done` den frisch begonnenen Turn: es
    stempelte ihn ab (`turn_total_ms=0`) und leerte ihn, wonach jede Folgezeile
    `turn_id=None` trug. Die Kennung der alten Antwort half hier nicht — es gab
    keinen Barge-in, also stand sie auf keiner Totenliste.

    Der verlaessliche Beleg ist ein anderer: ein Turn, der noch keine
    `response.created` gesehen hat, kann nicht von einem `response.done`
    abgeschlossen werden.
    """
    lines = []

    class _Log:
        def info(self, event, **kw): lines.append((event, kw))
        def warning(self, event, **kw): lines.append((event, kw))
        def error(self, event, **kw): lines.append((event, kw))

    class _Stream:
        def __init__(self, items): self._items = list(items)
        def __aiter__(self):
            async def gen():
                for item in self._items:
                    yield item
            return gen()
        async def send(self, data): pass
        async def close(self, *a, **kw): pass

    frames = [
        json.dumps({"type": "input_audio_buffer.speech_started"}),   # neuer Turn
        # ... und sofort das done der VORIGEN Antwort, ohne eigenes created
        json.dumps({"type": "response.done",
                    "response": {"id": "resp_vorher", "output": []}}),
    ]

    async def scenario():
        session, _ = _session(active=True)
        session.oa = _Stream(frames)
        real = CS.log
        CS.log = _Log()
        try:
            await session._oa_reader()
        finally:
            CS.log = real
        if session._reconnect_task is not None:
            session._reconnect_task.cancel()
        return session

    session = _run(scenario())
    turns = [kw for name, kw in lines if name == "core.turn_latency"]
    require(not turns, f"kein Turn wird abgeschlossen: {turns}")
    require(session._turn, "der frische Turn bleibt bestehen")
    require(session._turn.get("turn_id"), "und behaelt seine Kennung")


def t_audio_of_a_dead_response_does_not_stamp_the_new_turn():
    """Dieselbe Wurzel, andere Marke.

    `first_audio_recv` stand VOR dem Tor. Ein Delta der abgebrochenen Antwort
    kommt an, bevor die neue Antwort ueberhaupt begonnen hat — die abgeleitete
    Dauer wurde dadurch negativ (gemessen: -5 ms).
    """
    session, _ = _session(active=True)
    session._begin_turn()
    session._audio_open = False
    session._dead_responses.append("resp_alt")

    async def scenario():
        provider = _Provider()
        session.oa = provider
        # Genau der Fall: ein Delta der toten Antwort waehrend des neuen Turns.
        event = {"type": "response.output_audio.delta", "response_id": "resp_alt",
                 "delta": base64.b64encode(b"\x01\x02" * 50).decode()}
        if not session._stopping and session._audio_wanted(event):
            session._mark("first_audio_recv")
        return "first_audio_recv" in session._turn

    require(not _run(scenario()),
            "die tote Antwort stempelt den lebenden Turn nicht")

    body = _without_comments(inspect.getsource(CS.Session._oa_reader))
    gate = body.index("_audio_wanted")
    mark = body.index('_mark ( "first_audio_recv" )')
    require(gate < mark,
            "und die Marke steht im Quelltext hinter dem Tor, nicht davor")


# -- Wiederaufbau ---------------------------------------------------------------

def t_a_lost_provider_triggers_a_bounded_reconnect():
    """Frueher endete die Leseschleife einfach, und niemand erfuhr davon.

    Die Sitzung galt weiter als aktiv, bis das naechste Audioframe in eine
    geschlossene Verbindung lief und die Nachrichtenschleife des Satelliten
    mitriss. Fuer den Menschen war das ein Geraet, das mitten im Satz
    verstummt.
    """
    async def scenario():
        session, _ = _session(active=True)
        session.reader = None
        session._on_provider_lost()
        started = session._reconnect_task is not None
        if session._reconnect_task is not None:
            session._reconnect_task.cancel()
        return started, session.active

    started, still_active = _run(scenario())
    require(started, "es wird neu aufgebaut")
    require(not still_active, "und die Sitzung gilt solange als nicht bereit")


def t_a_drop_mid_answer_cannot_wedge_the_session_open_forever():
    """Der stillste der gefundenen Fehler.

    `_timeout_loop` tastet die Sitzung bei jedem Tick an, solange `speaking`
    oder `responding` gilt. Reisst die Anbieterverbindung mitten in einer
    Antwort ab, blieb `responding` fuer immer wahr: die Sitzung lief nie in den
    Inaktivitaets-Timeout, `_busy` blieb gesetzt, und kein Satellit konnte sich
    mehr verbinden. Nur ein Neustart des Core haette das geloest — und niemand
    haette gewusst, warum.
    """
    async def scenario():
        session, _ = _session(active=True)
        session.responding = True                  # mitten in der Antwort
        session.speaking = False
        session.reader = None
        session._on_provider_lost()
        if session._reconnect_task is not None:
            session._reconnect_task.cancel()
        return session.responding, session.speaking

    responding, speaking = _run(scenario())
    require(not responding, "die Antwort gilt nicht mehr als laufend")
    require(not speaking, "und auch nicht das Sprechen")


def t_a_closing_session_is_not_reconnected():
    """Ein absichtliches Ende ist kein Abriss."""
    async def scenario():
        results = {}
        for reason in ("closing", "stopping", "inactive"):
            session, _ = _session(active=True)
            if reason == "closing":
                session._closing = True
            elif reason == "stopping":
                session._stopping = True
            else:
                session.active = False
            session._on_provider_lost()
            results[reason] = session._reconnect_task
            if session._reconnect_task is not None:
                session._reconnect_task.cancel()
        return results

    for reason, task in _run(scenario()).items():
        require(task is None, f"kein Wiederaufbau bei {reason}")


def t_the_reader_itself_reports_a_lost_provider():
    """Nicht nur die Meldestelle, sondern der Weg dorthin.

    Eine Mutation, die den Aufruf am Ende der Leseschleife entfernte, blieb
    gruen — die Tests riefen `_on_provider_lost` selbst auf und pruefte
    niemand, ob der Reader das auch tut.
    """
    class _Ends:
        """Eine Anbieterverbindung, die sofort endet."""

        def __aiter__(self):
            async def gen():
                if False:            # nichts liefern, sofort fertig
                    yield ""
            return gen()

        async def send(self, data):
            pass

        async def close(self, *a, **kw):
            pass

    async def scenario():
        session, _ = _session(active=True)
        session.oa = _Ends()
        await session._oa_reader()
        started = session._reconnect_task is not None
        if session._reconnect_task is not None:
            session._reconnect_task.cancel()
        return started

    require(_run(scenario()),
            "das Ende der Leseschleife loest den Wiederaufbau aus")


def t_the_reconnect_stays_invisible_to_the_satellite():
    """Kein zweites `session_ready`.

    Der Satellit steht bereits in ACTIVE und sendet. Ihm eine
    Zustandsaenderung zu melden, die es nicht gab, waere eine Zusage ueber sein
    Verhalten, die von hier aus niemand pruefen kann — der Satellit ist nicht
    erreichbar. Er soll nur merken, dass es kurz still war.
    """
    signature = inspect.signature(CS.Session.open)
    require("announce" in signature.parameters,
            "das Oeffnen kennt den Unterschied")
    reconnect = _without_comments(inspect.getsource(CS.Session._reconnect))
    require("announce=False" in reconnect.replace(" ", ""),
            "und der Wiederaufbau nutzt ihn")

    body = _without_comments(inspect.getsource(CS.Session.open))
    require("if announce" in body,
            "das Signal haengt wirklich daran")


def t_giving_up_closes_cleanly_instead_of_falling_silent():
    """Sagen kann SOLVIO es nicht — jede Stimme kommt vom Anbieter.

    Was bleibt, ist ein sauberes Ende: der Satellit erfaehrt es, und das
    Gespraech bleibt beim Core. Zu behaupten, es sei verloren, waere unwahr.
    """
    reconnect = _without_comments(inspect.getsource(CS.Session._reconnect))
    require("close" in reconnect, "am Ende wird sauber geschlossen")
    require("provider_lost" in reconnect, "mit einem ehrlichen Grund")
    require_equal(CS.Session._CLOSE_REASONS.get("provider_lost"), "provider_error",
                  "der auch in der Kategorie ankommt")


def t_the_reconnect_is_bounded():
    """Ein Anbieter, der zweimal nicht antwortet, ist nicht in der dritten
    Sekunde wieder da — und der Mensch steht derweil vor einem stummen
    Geraet."""
    require(1 <= CS.RECONNECT_ATTEMPTS <= 3,
            f"wenige Versuche, sind {CS.RECONNECT_ATTEMPTS}")
    require(CS.RECONNECT_BACKOFF > 0, "mit Wartezeit dazwischen")
    total = sum(CS.RECONNECT_BACKOFF * n for n in range(1, CS.RECONNECT_ATTEMPTS + 1))
    require(total <= 3.0, f"insgesamt hoechstens drei Sekunden, sind {total:.1f}s")


def t_audio_during_the_reconnect_is_held_not_lost():
    """Der Vorlauf traegt auch hier.

    Waehrend des Wiederaufbaus ist die Sitzung nicht aktiv — dieselbe
    Bedingung wie beim ersten Oeffnen. Was gesprochen wird, laeuft in den
    Puffer und geht beim Bereitwerden hinaus.
    """
    async def scenario():
        session, _ = _session(active=True)
        session.reader = None
        session._on_provider_lost()
        during = session.active
        await session.feed_audio(b"\x66\x66" * 100)
        held = not session.preroll.empty
        if session._reconnect_task is not None:
            session._reconnect_task.cancel()
        return during, held

    during, held = _run(scenario())
    require(not during, "waehrenddessen nicht aktiv")
    require(held, "und das Weitergesprochene wird gehalten")


# -- Turn-Identitaet und Barge-in ----------------------------------------------

def t_audio_from_a_cancelled_response_is_not_forwarded():
    """Der Kern von Barge-in V2.

    Der Anbieter hoert nach `response.cancel` auf zu erzeugen — was schon
    unterwegs war, kommt trotzdem an. Frueher wurde es weitergereicht, und der
    Satellit spielte die alte Stimme ueber die neue Frage.
    """
    session, _ = _session(active=True)
    session._response_id = "resp_alt"
    session._audio_open = False
    session._dead_responses.append("resp_alt")
    require(not session._audio_wanted({"response_id": "resp_alt"}),
            "das Delta der abgebrochenen Antwort wird verworfen")


def t_a_late_delta_is_rejected_even_after_a_new_response_started():
    """Die zweite Sperre, und der Grund, warum es zwei gibt.

    Beginnt die naechste Antwort, geht das Tor wieder auf. Ein Delta der ALTEN
    Antwort, das erst jetzt eintrudelt, faende es offen — es faellt nur ueber
    seine Kennung auf.
    """
    session, _ = _session(active=True)
    session._dead_responses.append("resp_alt")
    session._audio_open = True                 # die neue Antwort laeuft schon
    session._response_id = "resp_neu"
    require(not session._audio_wanted({"response_id": "resp_alt"}),
            "das Nachzuegler-Delta bleibt tot")
    require(session._audio_wanted({"response_id": "resp_neu"}),
            "die neue Antwort darf sprechen")


def t_a_delta_without_an_id_follows_the_gate():
    """Fehlt die Kennung, entscheidet das Tor — in die vorsichtige Richtung."""
    session, _ = _session(active=True)
    session._audio_open = False
    require(not session._audio_wanted({}), "ohne Kennung und mit zu: nein")
    session._audio_open = True
    require(session._audio_wanted({}), "ohne Kennung und mit offen: ja")


def t_barge_in_stops_the_satellite_before_it_asks_the_provider():
    """Die Reihenfolge ist die Latenz.

    Der Mensch hoert die Stille, wenn der Satellit leert — nicht, wenn der
    Anbieter antwortet. Erst die Sperre und der `flush`, dann die Bitte nach
    oben.
    """
    body = inspect.getsource(CS.Session._barge_in)
    gate = body.index("_audio_open = False")
    flush = body.index('"flush"')
    cancel = body.index("response.cancel")
    require(gate < flush < cancel,
            "Tor zu, Satellit leeren, dann erst den Anbieter bitten")


def t_barge_in_asks_the_provider_to_stop():
    """`semantic_vad` unterbricht selbst — die ausdrueckliche Bitte ist die
    guenstigere Doppelung, weil sie nicht davon abhaengt, dass beide Seiten
    denselben Moment meinen."""
    session, satellite = _session(active=True)
    provider = _Provider()
    session.oa = provider
    session.responding = True
    session._response_id = "resp_alt"
    _run(session._barge_in())
    require("response.cancel" in provider.types(),
            f"der Anbieter wurde gebeten aufzuhoeren: {provider.types()}")
    require(any(m.get("type") == "flush" for m in satellite.messages()),
            "und der Satellit hat geleert")
    require(not session._audio_open, "das Tor ist zu")
    require("resp_alt" in session._dead_responses, "die alte Antwort ist tot")


def t_a_failing_cancel_does_not_take_down_the_session():
    """Ein Abbruch, der nicht ankommt, darf nicht schlimmer sein als keiner."""
    class _Broken(_Provider):
        async def send(self, data):
            raise ConnectionResetError("weg")

    session, _ = _session(active=True)
    session.oa = _Broken()
    session.responding = True
    session._response_id = "resp_alt"
    _run(session._barge_in())                       # darf nicht fliegen
    require(not session._audio_open,
            "das Tor bleibt trotzdem zu — das ist der Teil, der lokal wirkt")


def t_speaking_without_a_running_response_is_not_a_barge_in():
    """Der normale Fall: der Mensch faengt an, waehrend SOLVIO schweigt.

    Dann gibt es nichts abzubrechen, und eine `response.cancel` ins Leere waere
    eine Bitte, die der Anbieter mit einem Fehler beantwortet.
    """
    source = inspect.getsource(CS.Session._oa_reader)
    require("was_responding = self.responding" in source,
            "es wird gemerkt, ob ueberhaupt gesprochen wurde")
    require("if was_responding:" in source,
            "und nur dann unterbrochen")


def t_the_dead_list_cannot_grow_without_bound():
    """Eine Liste, die nie vergisst, ist in einer langen Sitzung ein Leck."""
    session, _ = _session(active=True)
    for index in range(50):
        session._dead_responses.append(f"resp_{index}")
    require(len(session._dead_responses) <= 8,
            f"begrenzt, sind {len(session._dead_responses)}")
    require("resp_49" in session._dead_responses, "die juengsten bleiben")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
