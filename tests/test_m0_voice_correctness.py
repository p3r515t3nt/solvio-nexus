"""M0 Session 1 — voice correctness and error visibility.

Two concrete faults in the running system, both reproduced against the frozen baseline
(`2b2f574`) before anything was changed.

**Silent stop.** `is_silent_stop` was `len(words) <= 3 and any(word in _STOP_WORDS)`, and
`_STOP_WORDS` contained `"aus"`. In German `aus` is a separable verb particle — *das Licht
**aus**machen*, *den Fernseher **aus**schalten* — so an ordinary device command silently
ended the conversation:

    "licht aus"      -> SILENT STOP        "alarm aus"     -> SILENT STOP
    "alles aus"      -> SILENT STOP        "musik aus"     -> SILENT STOP
    "fernseher aus"  -> SILENT STOP

It was also inconsistent along an axis no user can predict — `"mach das licht aus"` (four
words) survived while `"licht aus"` (two) did not — and wrong in the OTHER direction too:
`"hör auf"`, `"abbrechen"` and `"das war's"` never matched at all, so real stop requests fell
through to normal processing. Measured, not assumed.

**Error visibility.** `session_open_failed` logged `kind=RuntimeError` and nothing else,
although `Session.open` already puts the provider's own error text into the exception it
raises. The detail was discarded at the log call. The Authorization header is passed to
`ws_connect`, so a provider exception can quote it back — redaction is not optional.

OUT OF SCOPE: satellite authentication, latency metrics, dispatcher refactor,
ConversationStore, memory integration, capability contract, HA migration, approval
integration, background tasks, S2B. Approval Security V1 is frozen and untouched.

ASSERTION POLICY: `require*` from `tests/_guard.py` are function calls and survive `-O`.

Direct: python tests/test_m0_voice_correctness.py
"""
import asyncio
import io
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

# Ordinary commands. Ending the conversation on any of these is the bug.
COMMANDS = (
    "licht aus", "alles aus", "musik aus", "fernseher aus", "alarm aus",
    "mach das licht aus", "schalte den fernseher aus", "stell den timer aus",
    "heizung aus", "alle lichter aus", "solvio licht aus", "mach die musik leise",
    "licht im wohnzimmer aus", "radio aus", "stell den wecker aus",
    # casing, punctuation and whitespace must not change the answer
    "Licht aus!", "  LICHT   AUS  ", "Licht  aus.", "ALLES AUS!!!",
    # the wake word on its own is not a request to stop
    "solvio", "hey solvio", "", "   ",
)

# Real requests to end the interaction.
STOPS = (
    "stop", "stopp", "Stopp!", "stoppen",
    "hör auf", "hoer auf", "höre auf", "hör bitte auf",
    "abbrechen", "brich ab",
    "das war's", "das war’s", "das wars", "das war alles",
    "danke, das war alles", "Danke, das war's.",
    "ruhe", "sei leise", "sei still", "sei ruhig", "leise", "schluss",
    # addressed forms the previous UX supported
    "solvio stopp", "solvio aus", "solvio halt", "solvio sei leise", "hey solvio stopp",
)


# =====================================================================
# Part A — the classifier
# =====================================================================
def t_ordinary_commands_are_not_silent_stops():
    wrong = [c for c in COMMANDS if CS.is_silent_stop(c)]
    require_equal(wrong, [], f"these ordinary utterances still end the session: {wrong}")


def t_genuine_stop_phrases_still_stop():
    missed = [c for c in STOPS if not CS.is_silent_stop(c)]
    require_equal(missed, [], f"these stop requests are not recognised: {missed}")


def t_the_particle_aus_is_no_longer_a_stop_word_on_its_own():
    """The root cause, named directly: "aus" alone is ambiguous, so it is not a stop."""
    require(not CS.is_silent_stop("aus"),
            "a bare particle still ends the session — the root cause is back")
    require(CS.is_silent_stop("solvio aus"),
            "the addressed form lost its meaning; the previous UX supported it")
    require(not CS.is_silent_stop("den fernseher aus"), "a command with an object stopped")


def t_the_rule_does_not_depend_on_utterance_length():
    """The old rule stopped on <=3 words and ignored longer ones. Same intent, same answer."""
    pairs = (("licht aus", "mach das licht aus"),
             ("musik aus", "mach bitte die musik aus"),
             ("fernseher aus", "schalte den fernseher im wohnzimmer aus"))
    for short, long in pairs:
        require_equal(CS.is_silent_stop(short), CS.is_silent_stop(long),
                      f"{short!r} and {long!r} are the same intent but classified differently")
        require(not CS.is_silent_stop(short), f"{short!r} ends the session")


def t_the_classifier_matches_whole_utterances_not_contained_words():
    """A command always carries an object, so it can never BE a stop expression."""
    for stop in ("stop", "stopp", "ruhe", "leise", "schluss", "abbrechen"):
        require(CS.is_silent_stop(stop), f"{stop!r} should stop on its own")
        for carrier in (f"{stop} die musik", f"mach {stop}", f"der {stop} knopf",
                        f"{stop} taste drücken"):
            require(not CS.is_silent_stop(carrier),
                    f"{carrier!r} stopped merely because it contains {stop!r}")


def t_the_classifier_makes_no_model_call():
    """Deterministic and local: no network, no LLM, no dispatcher."""
    import inspect
    src = inspect.getsource(CS.is_silent_stop) + inspect.getsource(CS._strip_address) \
        + inspect.getsource(CS._normalise_utterance)
    for forbidden in ("await", "async", "openai", "dispatch", "requests", "http"):
        require(forbidden not in src.lower(),
                f"the stop classifier reaches for {forbidden}")


def t_no_giant_phrase_dictionary():
    """The fix must be a rule, not a list that grows with every complaint."""
    total = len(CS._STOP_UTTERANCES) + len(CS._ADDRESSED_ONLY_STOPS)
    require(total <= 40, f"the stop vocabulary grew to {total} entries — that is a dictionary")
    require(len(CS._ADDRESSED_ONLY_STOPS) <= 4, CS._ADDRESSED_ONLY_STOPS)


# =====================================================================
# Part B — the real session effect
# =====================================================================
class _FakeSocket:
    def __init__(self):
        self.sent = []
        self.closed = False

    async def send(self, data):
        self.sent.append(data)

    async def close(self, *a, **kw):
        self.closed = True

    remote_address = ("127.0.0.1", 0)


class _FakeServer:
    conversations = None          # M1: der echte Server hat dieses Attribut
    model = "gpt-realtime"
    api_key = "sk-test-not-a-real-key"
    dispatcher = None
    idle_timeout = 60.0

    def __init__(self):
        self._busy = False


def _session():
    sess = CS.Session(_FakeServer(), _FakeSocket())
    sess.oa = _FakeSocket()
    sess.active = True
    return sess


async def _feed_transcript(sess, text):
    """Drive the REAL reader branch for a completed transcription."""
    stopped = []
    real = sess._silent_stop

    async def watched():
        stopped.append(True)
        await real()

    sess._silent_stop = watched
    event = {"type": "conversation.item.input_audio_transcription.completed",
             "transcript": text}
    # exactly what _oa_reader does for this event type
    if CS.is_silent_stop(event.get("transcript", "")):
        await sess._silent_stop()
    else:
        sess.touch()
    return bool(stopped)


async def t_a_device_command_does_not_close_the_session():
    for text in ("licht aus", "alles aus", "musik aus", "fernseher aus"):
        sess = _session()
        stopped = await _feed_transcript(sess, text)
        require(not stopped, f"{text!r} triggered the silent-stop path")
        require(not sess._stopping, f"{text!r} marked the session as stopping")
        require(not sess._closing, f"{text!r} started closing the session")
        require(sess.active, f"{text!r} deactivated the session")
        require(not sess.ws.closed, f"{text!r} closed the satellite socket")
        require_equal(sess.ws.sent, [], f"{text!r} sent something to the satellite")


async def t_a_genuine_stop_closes_the_session():
    sess = _session()
    provider = sess.oa            # close() drops the reference; keep it to inspect it
    stopped = await _feed_transcript(sess, "danke, das war alles")
    require(stopped, "a genuine stop phrase did not reach the silent-stop path")
    require(sess._stopping, "the session was not marked stopping")
    # M0/3: _closing means "a close is running", not "one has happened" — it is reset once
    # the teardown is complete, so that a later session_start on the same connection can
    # open again. Assert what stays true instead: the session is down and its provider
    # socket is released.
    require(not sess.active, "the session stayed active after a stop")
    require(sess.oa is None, "the provider socket was not released by the stop")
    require(sess.reader is None and sess.timer is None and sess._tool_worker is None,
            "the stop left task references behind")
    ends = [json.loads(m) for m in sess.ws.sent if isinstance(m, str)]
    require(any(m.get("type") == "session_end" for m in ends),
            f"the satellite was never told the session ended: {ends}")
    sent = [json.loads(m) for m in sess.ws.sent if isinstance(m, str)]
    require(any(m.get("type") == "flush" for m in sent),
            f"the satellite was not flushed on stop: {sent}")
    cancels = [json.loads(m) for m in provider.sent if isinstance(m, str)]
    require(any(m.get("type") == "response.cancel" for m in cancels),
            f"the provider response was not cancelled: {cancels}")


async def t_the_reader_branch_is_the_one_being_tested():
    """Guard the guard: this test mirrors the real event handling, not a paraphrase."""
    import inspect
    src = inspect.getsource(CS.Session._oa_reader)
    require("conversation.item.input_audio_transcription.completed" in src,
            "the transcription event name changed — the behaviour test is stale")
    require('transcript = ev.get("transcript", "")' in src and "is_silent_stop(transcript)" in src,
            "the reader no longer classifies the transcript the way this test does")
    # M1: derselbe finalisierte Text geht ausserdem ins Gespraech. Faellt das weg, ist
    # dieser Test wieder eine Paraphrase statt einer Spiegelung.
    require("self._persist_message(ROLE_USER, transcript)" in src,
            "the reader no longer persists the finalized user transcript")
    require("await self._silent_stop()" in src, "the stop path changed shape")


# =====================================================================
# Part C/D — provider error visibility and redaction
# =====================================================================
SECRET = "sk-proj-AbCdEf0123456789TOPSECRETvalue"
USEFUL = "HTTP 401 Unauthorized: the API key was rejected by the realtime endpoint"


def t_a_provider_error_keeps_its_diagnostic():
    text = CS.sanitize_diagnostic(f"{USEFUL}; Authorization: Bearer {SECRET}")
    require("401" in text, f"the status was lost: {text}")
    require("Unauthorized" in text, f"the reason was lost: {text}")
    require("realtime" in text, f"the endpoint context was lost: {text}")


def t_a_provider_error_never_carries_the_secret():
    for probe in (f"Authorization: Bearer {SECRET}",
                  f"api_key={SECRET}",
                  f"token: {SECRET}",
                  f"failed with {SECRET} in the query",
                  f"Bearer {SECRET}"):
        text = CS.sanitize_diagnostic(f"{USEFUL}; {probe}")
        require(SECRET not in text, f"the secret survived redaction: {text}")
        require("TOPSECRET" not in text, f"part of the secret survived: {text}")
        require("401" in text, f"redaction destroyed the diagnostic: {text}")


def t_provider_error_codes_survive_redaction():
    """Found by live validation: a blunt length rule ate the error code itself.

    `billing_hard_limit_reached` is 26 unbroken key characters — and it is precisely the
    diagnostic an operator needs. Long snake_case identifiers are prose; credentials mix
    case and digits, or are much longer.
    """
    for code in ("insufficient_quota", "billing_hard_limit_reached",
                 "invalid_request_error", "rate_limit_exceeded",
                 "server_error_please_retry_later"):
        text = CS.sanitize_diagnostic(f"provider rejected the session: code={code}")
        require(code in text, f"the provider error code {code!r} was redacted away: {text}")
    # ... while things shaped like credentials still go
    for token in ("AbCdEf0123456789TOPSECRETvalue",
                  "a3f9c2d18e7b4056a3f9c2d18e7b4056",
                  "0123456789abcdef0123456789abcdef01234567"):
        text = CS.sanitize_diagnostic(f"failed with {token} attached")
        require(token not in text, f"a credential-shaped value survived: {text}")


def t_ordinary_words_survive_redaction():
    """Over-redaction would be its own failure: the log has to stay readable."""
    text = CS.sanitize_diagnostic(
        "connection to api.openai.com refused after 20s open_timeout, model gpt-realtime")
    for word in ("api.openai.com", "refused", "open_timeout", "gpt-realtime"):
        require(word in text, f"{word!r} was redacted away: {text}")


def t_describe_exception_reports_type_and_detail():
    exc = RuntimeError(f"provider rejected the session: quota_exceeded; Bearer {SECRET}")
    info = CS.describe_exception(exc)
    require_equal(info["kind"], "RuntimeError", info)
    require("quota_exceeded" in info["detail"], f"the reason was lost: {info}")
    require(SECRET not in json.dumps(info), f"the secret leaked: {info}")


def t_describe_exception_surfaces_the_underlying_cause():
    """The real reason usually lives in the cause, not the wrapper."""
    try:
        try:
            raise OSError("connect to api.openai.com:443 refused")
        except OSError as inner:
            raise RuntimeError("session could not be opened") from inner
    except RuntimeError as exc:
        info = CS.describe_exception(exc)
    require_equal(info["cause_kind"], "OSError", info)
    require("refused" in info["cause_detail"], f"the cause detail was lost: {info}")


def t_the_old_kind_only_logging_is_gone():
    """Prove the reproduced behaviour cannot come back.

    Voice UX V2 moved the handling: the provider bring-up now runs as its own
    task (`Session.begin_open`) so the satellite message loop keeps consuming
    audio while it happens. The failure logging moved with it, into
    `Session._open_guarded`. The requirement is unchanged — stage, attempt and
    the provider detail, never the bare exception type.
    """
    import inspect
    src = inspect.getsource(CS.Session._open_guarded)
    require('log.error("core.session_open_failed", kind=type(exc).__name__)' not in src,
            "session_open_failed logs only the exception type again")
    require("describe_exception(exc)" in src,
            "session_open_failed no longer reports the provider detail")
    require("stage=self.open_stage" in src, "the failing stage is not reported")
    require("attempt=self.open_attempts" in src, "the attempt context is not reported")


async def t_a_failing_session_open_is_logged_usefully_and_safely():
    """End to end through the real handler, with a synthetic provider failure."""
    captured = []

    class _Log:
        def info(self, event, **kw):
            captured.append((event, kw))

        def error(self, event, **kw):
            captured.append((event, kw))

    class _FailingSession(CS.Session):
        async def open(self):
            self.open_attempts += 1
            self.open_stage = "handshake"
            raise RuntimeError(
                f"provider rejected the session: {USEFUL}; Authorization: Bearer {SECRET}")

        async def close(self, reason):
            self._closing = True

    # M0/2: the handler authenticates first, so the probe has to be a satellite the Core
    # accepts. That makes this test stronger, not weaker: it now proves the useful error
    # reaches the log on the path an AUTHENTICATED satellite actually takes.
    from solvio.realtime import satellite_auth as SA
    sat_secret = bytes(range(32))
    sat_id = "pi-testprobe"

    class _WS:
        remote_address = ("127.0.0.1", 0)

        def __init__(self):
            self.challenge = None

        async def send(self, raw):
            msg = json.loads(raw)
            if msg.get("type") == "auth_challenge":
                self.challenge = msg["server_nonce"]

        async def recv(self):
            client_nonce = SA.new_challenge()
            return json.dumps({
                "type": "hello", "protocol_version": SA.PROTOCOL_VERSION,
                "satellite_id": sat_id, "client_nonce": client_nonce,
                "auth": SA.compute_auth(sat_secret,
                                        protocol_version=SA.PROTOCOL_VERSION,
                                        server_nonce=self.challenge,
                                        satellite_id=sat_id,
                                        client_nonce=client_nonce)})

        def __aiter__(self):
            async def gen():
                yield json.dumps({"type": "session_start"})
            return gen()

        async def close(self, *a, **kw):
            pass

    server = CS.CoreServer.__new__(CS.CoreServer)
    server._busy = False
    server.model = "gpt-realtime"
    server.api_key = SECRET
    server.dispatcher = None
    server.credentials = SA.SatelliteCredentials({sat_id: sat_secret})

    real_log, real_session = CS.log, CS.Session
    CS.log, CS.Session = _Log(), _FailingSession
    try:
        await server._handle_pi(_WS())
        # Voice UX V2: the provider bring-up now runs as its own task
        # (Session.begin_open), so the satellite message loop keeps consuming
        # audio while it happens — that is the whole point of the change. The
        # failure is still logged with the same fields; it is logged one tick
        # later. Nothing about the assertion below changes, only the moment at
        # which it can be made.
        for _ in range(5):
            await asyncio.sleep(0)
    finally:
        CS.log, CS.Session = real_log, real_session

    failures = [kw for event, kw in captured if event == "core.session_open_failed"]
    require_equal(len(failures), 1, f"the failure was not logged once: {captured}")
    kw = failures[0]
    blob = json.dumps(kw)
    require_equal(kw["kind"], "RuntimeError", kw)
    require("401" in blob, f"the provider status is missing: {kw}")
    require("Unauthorized" in blob, f"the provider reason is missing: {kw}")
    require_equal(kw["stage"], "handshake", kw)
    require_equal(kw["attempt"], 1, kw)
    require_equal(kw["model"], "gpt-realtime", kw)
    require(SECRET not in blob, f"the secret reached the log: {kw}")
    require("TOPSECRET" not in blob, f"part of the secret reached the log: {kw}")


# =====================================================================
# Part E — no scope creep, and the freeze is untouched
# =====================================================================
def t_security_tree_matches_freeze_and_explicit_n2_amendment():
    from _security_amendment import require_n2_security_tree
    from pathlib import Path
    require_n2_security_tree(Path(__file__).resolve().parents[1])


def t_a_stop_survives_a_polite_filler_word() -> None:
    """„Solvio, stopp mal" ist derselbe Wunsch wie „Solvio, stopp".

    Gemeldet: „er reagiert nicht auf mein Stopp, er sagt ja ich stoppe — und
    quatscht weiter." Die Erkennung verlangte, dass die Aeusserung EXAKT ein
    Stopp-Wort ist. Ein einziges „mal" oder „bitte" liess sie durchfallen, und
    der Satz ging als gewoehnliche Frage an das Modell, das brav darauf
    antwortete statt still zu werden.
    """
    from solvio.realtime.core_server import is_silent_stop
    for said in ("Solvio, stopp mal", "stopp mal", "bitte stopp", "stopp jetzt",
                 "Solvio, jetzt stopp", "sei mal still", "Solvio, sei jetzt still",
                 "stoppe", "ok stopp", "Solvio, halt mal"):
        require(is_silent_stop(said), f"nicht als Stopp erkannt: {said!r}")


def t_a_sentence_that_merely_contains_a_stop_word_is_not_one() -> None:
    """Der Vergleich bleibt EXAKT — Fuellwoerter fallen weg, Inhalt nicht.

    Das ist die Gegenprobe zur Nachsicht oben. Wer nach einem Stoppschild
    fragt, will nicht, dass das Gespraech endet.
    """
    from solvio.realtime.core_server import is_silent_stop
    for said in ("erzaehl mir was ueber stopp schilder",
                 "ich wollte nicht dass du aufhoerst",
                 "wie stoppe ich den Timer",
                 "was heisst stopp auf englisch",
                 "solvio"):
        require(not is_silent_stop(said), f"faelschlich als Stopp erkannt: {said!r}")


def t_the_address_may_come_last() -> None:
    """„Stopp, Solvio" ist dieselbe Bitte wie „Solvio, stopp".

    Die Anrede wurde nur VORNE abgeschnitten. Live gemessen kam daraufhin
    `stop_near_miss addressed=False` bei Aeusserungen, die SOLVIO sehr wohl
    ansprachen — nur eben am Satzende. Wer jemanden bittet aufzuhoeren,
    sortiert nicht nach Satzbau.
    """
    from solvio.realtime.core_server import is_silent_stop
    for said in ("Stop, Solvio", "Stopp Solvio", "stop mal solvio",
                 "sei still solvio", "halt solvio", "aus solvio"):
        require(is_silent_stop(said), f"nicht als Stopp erkannt: {said!r}")
    # Und eine gewoehnliche Frage mit nachgestellter Anrede bleibt eine Frage.
    require(not is_silent_stop("Wie meinst du das solvio"),
            "eine Frage mit Anrede ist kein Stopp")




def t_a_single_word_starting_with_stop_is_a_stop() -> None:
    """Welche Beugung ankommt, entscheidet der Transkribierer — nicht der Mensch.

    Genau eine Wortform zu verlangen hiess, sich auf seine Laune zu verlassen.
    Ein EINZELNES Wort, das mit „stop" anfaengt, ist unmissverstaendlich.
    """
    from solvio.realtime.core_server import is_silent_stop
    for said in ("stop", "stopp", "stoppe", "stoppt", "stoppen", "Solvio stoppt"):
        require(is_silent_stop(said), f"nicht als Stopp erkannt: {said!r}")


def t_a_stop_word_inside_a_sentence_is_still_not_a_stop() -> None:
    """Die Regel gilt fuer EIN Wort, nicht fuer jedes Wort in einem Satz."""
    from solvio.realtime.core_server import is_silent_stop
    for said in ("stoppuhr stellen", "wie stoppe ich den timer",
                 "erzaehl mir was ueber stoppschilder", "stoppen sie den wagen"):
        require(not is_silent_stop(said), f"faelschlich als Stopp erkannt: {said!r}")



def t_a_thing_that_can_be_stopped_is_not_the_conversation() -> None:
    """Die Regel greift nur, wenn das zweite Wort nach dem NAMEN klingt.

    Sonst wuerde „Musik stoppen" das Gespraech beenden statt die Musik. Das
    Geruest aus Konsonanten trennt beides: „musik" hat keins davon.
    """
    from solvio.realtime.core_server import is_silent_stop
    for said in ("timer stoppen", "musik stoppen", "stoppuhr stellen",
                 "wie stoppe ich den timer", "kannst du das stoppen bitte"):
        require(not is_silent_stop(said), f"faelschlich als Stopp erkannt: {said!r}")



def t_stopping_a_thing_is_not_stopping_the_conversation() -> None:
    """Die einzige Grenze der umgedrehten Regel — und sie muss halten.

    Wer „Musik stoppen" sagt, meint die Musik. Diese Liste ist die einzige
    Stelle, an der eine kurze Aeusserung mit einem Stopp-Wort das Gespraech
    NICHT beendet, und sie gehoert deshalb kurz gehalten und geprueft.
    """
    from solvio.realtime.core_server import is_silent_stop
    for said in ("timer stoppen", "musik stoppen", "stoppuhr stellen",
                 "stopp die musik", "wie stoppe ich den timer",
                 "kannst du das stoppen bitte",
                 "erzaehl mir was ueber stoppschilder"):
        require(not is_silent_stop(said), f"faelschlich als Stopp erkannt: {said!r}")


def t_the_model_can_end_the_conversation_but_owns_nothing_else() -> None:
    """Das Modell versteht die Bitte — es hatte nur kein Mittel, sie auszufuehren.

    Fuenf Zeilen Log, fuenf Transkripte desselben gesprochenen Namens:
    „solve your", „zovi", „olwe", „fang ich", „halt er". Auf Worte zu warten,
    die so schwanken, ist aussichtslos; die Regeln weit genug zu machen hiess,
    „alles aus" und „mach stop" zu Gespraechsenden zu machen — das Gate hat es
    gefangen.

    Das Modell dagegen erkannte die Absicht jedes Mal und ANTWORTETE darauf
    („alles klar, ich stoppe jetzt"), weil ihm nichts anderes blieb.

    Das ist keine Autoritaet: ein Gespraech zu beenden hat keine Wirkung in der
    Welt. Der Core fuehrt es aus, nicht das Modell — und der Aufruf erreicht den
    Dispatcher gar nicht.
    """
    import inspect
    from solvio.realtime import core_server as CS

    require(CS.END_CONVERSATION_TOOL["name"] == "end_conversation",
            "das Werkzeug heisst, was es tut")
    require(CS.END_CONVERSATION_TOOL["parameters"]["properties"] == {},
            "es nimmt nichts entgegen — es gibt nichts zu entscheiden")

    handler = inspect.getsource(CS.Session._handle_tool_calls)
    require('"end_conversation"' in handler, "der Core faengt den Aufruf ab")
    require(handler.index('"end_conversation"') < handler.index("disp ="),
            "und zwar BEVOR der Dispatcher ueberhaupt geholt wird")
    require("_silent_stop()" in handler, "und fuehrt den stillen Stopp aus")

    # Die Anweisung sagt ausdruecklich, dass nichts dazu gesagt wird.
    require("NICHTS" in CS.END_CONVERSATION_TOOL["description"],
            "sonst antwortet es auf eine Bitte um Ruhe")


# =====================================================================
# Die Kontrollspur des Cores
# =====================================================================

def t_the_control_lane_works_without_knowing_the_name() -> None:
    """Fuenf gemessene Transkripte desselben gesprochenen Namens muessen enden.

    Der Name kam bei fuenf Versuchen fuenfmal anders an. Eine Erkennung, die
    auf ihn wartet, wartet vergebens; eine Liste, die ihm hinterherlaeuft, ist
    nie fertig. Verlaesslich ist das STOPP-Wort selbst — was davor steht, darf
    Rauschen sein und muss NICHT verstanden werden.
    """
    from solvio.realtime.core_server import is_conversation_stop
    for said in ("solve your stop", "zovi stop", "olwe stopp",
                 "fang ich stopp", "halt er stopp",
                 "stop", "stopp", "Solvio, stopp.", "stopp mal", "bitte stopp"):
        require(is_conversation_stop(said), f"beendet nicht: {said!r}")


def t_the_control_lane_never_swallows_a_device_command() -> None:
    """Deutsch stellt den Gegenstand voran und laesst das Verb weg.

    „Musik stopp", „Timer stopp", „Rollladen stopp" — genau diese Gestalt hat
    ein deutscher Kurzbefehl, und genau sie hatte die Sperrliste nie gesehen:
    sie war entlang der WORTART gebaut (Fragewoerter, Redeverben,
    Befehlsformen), nicht entlang der SATZROLLE.

    Der vorige Test an dieser Stelle prueft nur Infinitiv („musik stoppen")
    und Verb-zuerst („mach stop") — Formen, die strukturell gar nicht
    ausloesen KOENNEN. Er hat damit nichts gemessen.

    „Alexa, stopp" steht mit drin: das gilt einem anderen Geraet im selben
    Raum und geht SOLVIO nichts an.
    """
    from solvio.realtime.core_server import is_conversation_stop
    for said in ("musik stopp", "timer stopp", "wecker stopp", "radio stopp",
                 "rollladen stopp", "staubsauger stopp", "alexa stopp",
                 "die musik stopp", "musik bitte stopp", "fernseher stopp",
                 "licht stopp", "aufnahme stopp",
                 # und weiterhin die Formen, die auch vorher schon scheiterten
                 "alles aus", "mach bitte alles aus", "fernseher aus",
                 "licht aus", "stoppe den fernseher", "timer stoppen",
                 "musik stoppen", "stoppuhr stellen", "mach stop"):
        require(not is_conversation_stop(said), f"beendet faelschlich: {said!r}")



def t_the_control_lane_never_swallows_a_question_or_quotation() -> None:
    """Nach dem Wort zu fragen oder es zu zitieren ist keine Anweisung."""
    from solvio.realtime.core_server import is_conversation_stop
    for said in ("was bedeutet stop", "was heisst stopp", "wie schreibt man stopp",
                 "er sagt stopp", "wenn ich stopp sage", "nicht stopp",
                 "das war kein stopp", "erzaehl mir was ueber stoppschilder"):
        require(not is_conversation_stop(said), f"beendet faelschlich: {said!r}")


def t_the_control_lane_survives_nothing_at_all() -> None:
    """Leer, nur Zeichensetzung, nur Leerraum — nichts davon beendet etwas."""
    from solvio.realtime.core_server import is_conversation_stop
    for said in ("", "   ", "...", "\n", "!!!", "123"):
        require(not is_conversation_stop(said), f"beendet faelschlich: {said!r}")


def t_the_control_lane_is_bounded_in_length() -> None:
    """Ein Satz ist keine Anweisung — auch wenn er auf stopp endet.

    Diese Zusicherung ist die Mutationsprobe: wer `_STOP_MAX_WORDS` erhoeht,
    um mehr Verhoerer aufzufangen, laesst genau damit ganze Saetze durch.
    """
    from solvio.realtime.core_server import is_conversation_stop, _STOP_MAX_WORDS
    require_equal(_STOP_MAX_WORDS, 3, "die Grenze ist bewusst eng")
    require(not is_conversation_stop("ich glaube er meinte damit wirklich stopp"),
            "ein ganzer Satz ist keine Anweisung")
    require(not is_conversation_stop("also gut dann eben stopp"),
            "vier bedeutungstragende Woerter sind zu viele")


def t_the_control_lane_runs_before_the_model_ever_sees_it() -> None:
    """Der Core entscheidet zuerst — nicht das Modell.

    Die Spur laeuft vor dem Persistieren, vor jedem Werkzeugweg und vor jeder
    Zustellung. Waere sie danach, haette das Modell den Turn bereits.
    """
    import inspect
    from solvio.realtime import core_server as CS
    reader = inspect.getsource(CS.Session._oa_reader)
    require("is_conversation_stop(transcript)" in reader, "die Spur laeuft im Reader")
    require(reader.index("is_conversation_stop(transcript)")
            < reader.index("_persist_message(ROLE_USER"),
            "und VOR dem Persistieren")
    require(reader.index("is_conversation_stop(transcript)")
            < reader.index("is_silent_stop(transcript)"),
            "die deterministische Spur kommt zuerst")


def t_a_stop_invalidates_audio_that_is_already_in_flight() -> None:
    """Nichts aus der abgebrochenen Antwort darf danach noch sprechen."""
    import inspect
    from solvio.realtime import core_server as CS
    stop = inspect.getsource(CS.Session._silent_stop)
    require("self._audio_open = False" in stop, "das Tor geht zu")
    require(stop.index("self._audio_open = False") < stop.index("response.cancel"),
            "und zwar BEVOR irgendetwas gesendet wird")
    require("_dead_responses" in stop, "die laufende Antwort gilt als tot")


def t_the_name_is_no_longer_guessed() -> None:
    """Keine Alias-Pflege mehr — sie war der falsche Weg und ist entfernt."""
    from solvio.realtime import core_server as CS
    joined = " ".join(CS._ADDRESS_PREFIXES)
    for alias in ("solve your", "zovi", "olwe", "salvio", "silvio", "solveo"):
        require(alias not in joined, f"Verhoerer wird wieder gepflegt: {alias!r}")
    require(not hasattr(CS, "_sounds_like_the_name"),
            "die Lautform-Erkennung des Namens ist entfernt")


def t_the_control_lane_reads_german_as_it_is_written() -> None:
    """Eine deutsche Spracherkennung schreibt ss-Ligatur, die Liste stand in ASCII.

    Dadurch waren „heisst" und „heissen" TOTE Eintraege: die Normalisierung
    erhielt die Ligatur, der Vergleich sah sie nie. Die Mutationsprobe des
    Angreifers war eindeutig — beide ersatzlos zu entfernen aenderte keinen
    einzigen Testfall.
    """
    from solvio.realtime.core_server import is_conversation_stop
    for said in ("hei\u00dft das stopp", "das hei\u00dft stopp", "was hei\u00dft stopp",
                 "das war stopp", "das ist stopp"):
        require(not is_conversation_stop(said), f"beendet faelschlich: {said!r}")


def t_the_control_lane_never_swallows_a_spoken_instruction() -> None:
    """Mit einem Assistenten spricht man im Imperativ.

    „Sag stopp" heisst, er soll das Wort SAGEN — nicht aufhoeren. Fuer
    Geraetebefehle standen die Imperativformen von Anfang an in der Liste,
    fuer Redeverben nicht.
    """
    from solvio.realtime.core_server import is_conversation_stop
    for said in ("sag stopp", "schreib stopp", "nenn es stopp",
                 "antworte stopp", "wiederhole stopp", "buchstabiere stopp"):
        require(not is_conversation_stop(said), f"beendet faelschlich: {said!r}")


def t_urgency_does_not_make_the_rule_stricter() -> None:
    """„Stopp! Stopp! Stopp! Stopp!" — je draengender, desto mehr Woerter.

    Die Laengengrenze machte die Regel ausgerechnet dann strenger, wenn der
    Mensch am deutlichsten wird. Eine Aeusserung, die NUR aus Stopp-Woertern
    besteht, kann kein Geraetebefehl, keine Frage und keine Wiedergabe sein:
    dort steht immer ein Gegenstand oder ein Fragewort dabei.
    """
    from solvio.realtime.core_server import is_conversation_stop
    for said in ("stopp stopp", "stopp stopp stopp", "stopp stopp stopp stopp",
                 "stop stopp stop stopp stop"):
        require(is_conversation_stop(said), f"beendet nicht: {said!r}")


def t_the_near_miss_report_keeps_its_promise() -> None:
    """Der Kommentar verspricht „nie der Satz" — der Code hielt es nicht.

    Er loggte bis zu 48 Zeichen Gespraechstext, ausgeloest von einer
    TEILSTRING-Suche ueber „auf" und „halt". Beide stecken in gewoehnlicher
    Rede („was laeuft auf netflix"), also landete ein spuerbarer Anteil
    normaler Aeusserungen woertlich im Betriebslog. Dieselbe Datei begruendet
    bei `assistant_utterance` ausdruecklich, warum Gespraechstext dort nicht
    hingehoert.
    """
    import inspect
    from solvio.realtime import core_server as CS
    src = inspect.getsource(CS.is_silent_stop)
    require("said=" not in src, "der Wortlaut geht nicht mehr ins Log")
    require("matched=" in src, "stattdessen nur das getroffene Wort")
    require("k in words" in src, "Wortgleichheit statt Teilstring")


def t_the_model_may_ask_to_stop_but_the_core_refuses_a_device_command() -> None:
    """„Fernseher aus" beendete zweimal das Gespraech — ueber den Modellweg.

    Nicht ueber die Kontrollspur: die laesst den Befehl korrekt durch. Das
    MODELL deutete ihn als Abschied und rief `end_conversation`. Der zweite
    Weg hatte keine Bremse, und ein Werkzeugaufruf ist eine Bitte, keine
    Entscheidung.
    """
    import inspect
    from solvio.realtime import core_server as CS
    from solvio.realtime.core_server import looks_like_a_device_command as d

    for said in ("fernseher aus", "alles aus", "licht aus", "mach das licht an",
                 "musik stopp", "rollladen runter", "heizung hoeher",
                 "fernseher an"):
        require(d(said), f"nicht als Geraetebefehl erkannt: {said!r}")

    for said in ("das wars fuer heute", "wir koennen aufhoeren",
                 "beende bitte unser gespraech", "danke das reicht",
                 "stopp", "ich bin fertig"):
        require(not d(said), f"faelschlich als Geraetebefehl erkannt: {said!r}")

    handler = inspect.getsource(CS.Session._handle_tool_calls)
    require("looks_like_a_device_command" in handler,
            "der Core prueft, bevor er dem Modell folgt")
    require("END_CONVERSATION_REFUSED" in handler,
            "und sagt es, wenn er sich weigert")


def t_nobody_is_interrupted_before_they_have_said_anything() -> None:
    """Die Schonfrist steht im Core — sonst gaelte sie nur fuer einen Endpunkt.

    Gemeldet: „faengt an, sagt einen Satz, bricht ab, faengt von vorn an."
    Acht abgebrochene Antworten in einer Sitzung, davon KEINE vom Endgeraet:
    die Spracherkennung des Anbieters hoerte den auslaufenden Satz des
    Menschen, waehrend die Antwort schon lief.

    Der Fix stand zuerst im Telefon — und der Satellit hatte ihn dadurch nicht,
    obwohl der Fehler zuletzt genau dort auftrat. Die Regel gehoert an die
    Stelle, durch die BEIDE Endpunkte laufen.
    """
    import inspect
    from solvio.realtime import core_server as CS

    require(CS.UTTERANCE_GUARD_SECONDS > 0, "es gibt eine Schonfrist")
    require(CS.UTTERANCE_GUARD_SECONDS <= 1.0,
            "aber sie ist kurz — laenger waere ein taubes Ohr")

    feed = inspect.getsource(CS.Session.feed_audio)
    require("_within_utterance_guard()" in feed, "sie wirkt im gemeinsamen Weg")
    require("bytes(len(pcm16))" in feed,
            "es geht STILLE hinaus, nicht nichts — der Strom darf nicht abreissen")

    guard = inspect.getsource(CS.Session._within_utterance_guard)
    require("self.responding" in guard, "nur waehrend SOLVIO spricht")
    require("first_audio_sent" in guard, "gemessen ab dem ersten hoerbaren Ton")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
