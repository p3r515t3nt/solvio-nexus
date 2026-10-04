"""M0 Session 2 — satellite LAN authentication.

THE FINDING, reproduced against the released baseline (a7d06e5) before this existed: the
satellite listener binds `0.0.0.0:8766` and accepted ANY connection. A client anywhere on the
LAN could connect and send `session_start`; the Core then opened an OpenAI Realtime session
and handed the connection the tool dispatcher — funded provider resources and Home Assistant
control, with no proof of who was asking. `hello` existed but carried only free text.

WHAT THIS ADDS: a challenge-response over a shared secret, HMAC-SHA256 from the standard
library, verified in constant time BEFORE any provider call, dispatcher access or session
state. The long-term secret never travels; the challenge is per-connection and single-use.

WHAT IT IS NOT: not TLS, not a PKI, not device attestation. It answers "is this our
satellite?" on a trusted LAN segment. Approval Security V1 is untouched — a satellite that
authenticates here still cannot approve anything.

ASSERTION POLICY: `require*` from `tests/_guard.py` are function calls and survive `-O`.

Direct: python tests/test_m0_satellite_auth.py
"""
import asyncio
import ast as _ast
import inspect
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions  # noqa: E402
enforce_assertions()

from _guard import require, require_equal  # noqa: E402
from solvio.realtime import core_server as CS  # noqa: E402
from solvio.realtime import satellite_auth as SA  # noqa: E402

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CORE_SRC = os.path.join(REPO, "src", "solvio", "realtime", "core_server.py")
SAT_ID = "pi-wohnzimmer"
SECRET = bytes(range(32)) * 1


def _creds(secret=SECRET, sat_id=SAT_ID):
    return SA.SatelliteCredentials({sat_id: secret})


class _Socket:
    """A satellite the Core talks to, scripted with the frames it should answer."""

    def __init__(self, replies=None):
        self.sent = []
        self.closed_with = None
        self._replies = list(replies or [])

    async def send(self, data):
        self.sent.append(data)

    async def recv(self):
        if not self._replies:
            await asyncio.sleep(3600)          # never answers -> handshake timeout
        reply = self._replies.pop(0)
        if callable(reply):
            reply = reply(self.challenge)
        return reply

    async def close(self, code=None, reason=None):
        self.closed_with = (code, reason)

    @property
    def challenge(self):
        for raw in self.sent:
            msg = json.loads(raw)
            if msg.get("type") == "auth_challenge":
                return msg["server_nonce"]
        return None

    remote_address = ("192.168.178.99", 51000)


def _server(credentials=None):
    srv = CS.CoreServer.__new__(CS.CoreServer)
    srv._busy = False
    srv.credentials = credentials
    srv.dispatcher = None
    srv.model = "gpt-realtime"
    srv.api_key = "sk-not-used-here"
    srv.host, srv.port = "127.0.0.1", 8766
    return srv


def _hello(challenge, *, secret=SECRET, sat_id=SAT_ID, version=SA.PROTOCOL_VERSION,
           client_nonce=None, auth=None, drop=()):
    client_nonce = client_nonce or SA.new_challenge()
    if auth is None:
        auth = SA.compute_auth(secret, protocol_version=SA.PROTOCOL_VERSION,
                               server_nonce=challenge, satellite_id=sat_id,
                               client_nonce=client_nonce)
    msg = {"type": "hello", "protocol_version": version, "satellite_id": sat_id,
           "client_nonce": client_nonce, "auth": auth}
    for key in drop:
        msg.pop(key, None)
    return json.dumps(msg)


_DEFAULT = object()


async def _authenticate(replies, credentials=_DEFAULT):
    """`credentials=None` means an UNCONFIGURED Core — distinct from "use the default"."""
    ws = _Socket(replies)
    srv = _server(_creds() if credentials is _DEFAULT else credentials)
    return await srv._authenticate(ws), ws


# =====================================================================
# Part 1 — the finding, as a permanent regression
# =====================================================================
async def t_an_unauthenticated_client_is_refused_before_anything_happens():
    """Reproduced before the fix: this client reached session_start and a provider call."""
    result, ws = await _authenticate([json.dumps({"type": "hello", "info": "pi-satellit"})])
    require(result is None, "an unauthenticated client was accepted")
    require(ws.closed_with is not None, "the connection was left open")
    require_equal(ws.closed_with[0], 4401, ws.closed_with)


async def t_authentication_runs_before_the_session_protocol():
    """Authentication must succeed before either protocol is selected or pumped."""
    import textwrap

    def shape(source):
        return _ast.dump(_ast.parse(source).body[0], include_attributes=False)

    body = _ast.parse(textwrap.dedent(inspect.getsource(CS.CoreServer._handle_pi))).body[0]
    auth_shape = shape("satellite_id = await self._authenticate(ws)")
    auth = [node for node in body.body
            if _ast.dump(node, include_attributes=False) == auth_shape]
    require_equal(len(auth), 1, "the real awaited authentication result is missing or ambiguous")
    guard_at = body.body.index(auth[0]) + 1
    require(guard_at < len(body.body), "authentication has no refusal branch")
    guard = body.body[guard_at]
    require_equal(_ast.dump(guard, include_attributes=False),
                  shape("if satellite_id is None:\n    return"),
                  "a failed handshake can continue to session selection")

    # GPT-Live introduced a factory; constructing either implementation is still
    # session state, so the guard covers selection as well as protocol pumping.
    calls = [node for node in _ast.walk(body) if isinstance(node, _ast.Call)]
    for name in ("create_session", "pump_endpoint"):
        matched = [node for node in calls
                   if isinstance(node.func, _ast.Name) and node.func.id == name]
        require_equal(len(matched), 1, f"expected one explicit {name} boundary")
        require(matched[0].lineno > guard.end_lineno,
                f"{name} can run before the handshake is accepted")
    for node in calls:
        func = node.func
        if ((isinstance(func, _ast.Name) and func.id in {"Session", "LiveSession"})
                or (isinstance(func, _ast.Attribute)
                    and func.attr in {"open", "feed_audio", "dispatch"})):
            require(node.lineno > guard.end_lineno,
                    "a session/provider/tool call precedes authentication acceptance")
    busy_shape = shape("self._busy = True")
    busy = [node for node in _ast.walk(body)
            if _ast.dump(node, include_attributes=False) == busy_shape]
    connected = [node for node in _ast.walk(body)
                 if isinstance(node, _ast.Constant) and node.value == "core.PI_CONNECTED"]
    for name, nodes in (("satellite slot", busy), ("PI_CONNECTED", connected)):
        require_equal(len(nodes), 1, f"expected one {name} boundary")
        require(nodes[0].lineno > guard.end_lineno,
                f"{name} precedes authentication acceptance")

    tree = _ast.parse(textwrap.dedent(inspect.getsource(CS.CoreServer._authenticate)))
    called = {n.func.attr for n in _ast.walk(tree)
              if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Attribute)}
    for forbidden in ("open", "feed_audio", "dispatch"):
        require(forbidden not in called,
                f"the handshake itself calls {forbidden} — that is a provider/tool effect")


async def t_a_rejected_client_never_reaches_a_provider_session():
    """Behavioural: drive the real handler and prove Session.open is never called."""
    opened = []

    class _NoSession(CS.Session):
        async def open(self):
            opened.append(True)

        async def close(self, reason):
            self._closing = True

    class _WS(_Socket):
        def __aiter__(self):
            async def gen():
                yield json.dumps({"type": "session_start"})
            return gen()

    ws = _WS([json.dumps({"type": "hello", "satellite_id": "angreifer",
                          "protocol_version": SA.PROTOCOL_VERSION,
                          "client_nonce": SA.new_challenge(), "auth": "00" * 32})])
    srv = _server(_creds())
    real = CS.Session
    CS.Session = _NoSession
    try:
        await srv._handle_pi(ws)
    finally:
        CS.Session = real
    require_equal(opened, [], "a rejected client opened a provider session")
    require(not srv._busy, "a rejected client occupied the satellite slot")
    require_equal(ws.closed_with[0], 4401, ws.closed_with)


# =====================================================================
# Parts 3/4 — the handshake and its refusals
# =====================================================================
async def t_a_valid_satellite_is_accepted():
    result, ws = await _authenticate([_hello])
    require_equal(result, SAT_ID, f"the real satellite was refused: {result}")
    require(ws.closed_with is None, f"the connection was closed anyway: {ws.closed_with}")
    challenge = json.loads(ws.sent[0])
    require_equal(challenge["type"], "auth_challenge", challenge)
    require_equal(challenge["protocol_version"], SA.PROTOCOL_VERSION, challenge)
    require(len(challenge["server_nonce"]) >= 32, challenge)


async def t_the_long_term_secret_never_travels():
    result, ws = await _authenticate([_hello])
    require_equal(result, SAT_ID, result)
    wire = " ".join(ws.sent)
    require(SECRET.hex() not in wire, "the shared secret was sent over the wire")
    src = inspect.getsource(CS.CoreServer._authenticate)
    require("self.credentials.verify(" in src, "verification is not delegated to the store")
    require("_secrets" not in src, "the handshake handles raw secret material itself")


async def t_a_wrong_secret_is_refused():
    other = bytes(32)
    result, ws = await _authenticate(
        [lambda c: _hello(c, secret=other)], credentials=_creds())
    require(result is None, "a wrong secret was accepted")
    require_equal(ws.closed_with[0], 4401, ws.closed_with)


async def t_an_unknown_satellite_id_is_refused():
    result, ws = await _authenticate([lambda c: _hello(c, sat_id="fremdes-geraet")])
    require(result is None, "an unknown satellite_id was accepted")


async def t_a_malformed_hello_is_refused():
    for reply in ('{"type":"hello"',                       # broken JSON
                  json.dumps({"type": "session_start"}),   # not a hello at all
                  json.dumps({"type": "hello"}),           # no fields
                  json.dumps({"type": "hello", "satellite_id": SAT_ID,
                              "protocol_version": SA.PROTOCOL_VERSION,
                              "client_nonce": "not-hex!", "auth": "zz"}),
                  b"\x00\x01\x02"):                        # binary before auth
        result, ws = await _authenticate([reply])
        require(result is None, f"a malformed hello was accepted: {reply!r}")
        require(ws.closed_with is not None, f"connection left open for {reply!r}")


async def t_a_replayed_response_is_refused():
    """The captured response of a real satellite, replayed against a NEW connection."""
    first, ws1 = await _authenticate([_hello])
    require_equal(first, SAT_ID, "setup: the honest handshake must succeed")
    captured = json.loads(ws1.sent[-1]) if False else None
    # capture what the satellite actually sent by recomputing it against ws1's challenge
    replay = _hello(ws1.challenge)
    second, ws2 = await _authenticate([replay])
    require(second is None, "a replayed response was accepted against a fresh challenge")
    require_equal(ws2.closed_with[0], 4401, ws2.closed_with)
    require(ws1.challenge != ws2.challenge, "two connections shared one challenge")


async def t_the_challenge_is_single_use_within_a_connection():
    """A failed attempt does not get a second try on the same nonce."""
    ws = _Socket([json.dumps({"type": "hello", "satellite_id": SAT_ID,
                              "protocol_version": SA.PROTOCOL_VERSION,
                              "client_nonce": SA.new_challenge(), "auth": "11" * 32}),
                  _hello])
    srv = _server(_creds())
    result = await srv._authenticate(ws)
    require(result is None, "a second attempt on the same connection was allowed")
    require_equal(len(ws._replies), 1, "the handler read more than one hello")


async def t_a_silent_client_times_out():
    ws = _Socket([])
    srv = _server(_creds())
    real_timeout = SA.HANDSHAKE_TIMEOUT
    SA.HANDSHAKE_TIMEOUT = 0.2
    try:
        result = await srv._authenticate(ws)
    finally:
        SA.HANDSHAKE_TIMEOUT = real_timeout
    require(result is None, "a client that never answered was accepted")
    require_equal(ws.closed_with[0], 4401, ws.closed_with)


async def t_a_wrong_protocol_version_is_refused():
    result, _ = await _authenticate([lambda c: _hello(c, version=SA.PROTOCOL_VERSION + 1)])
    require(result is None, "a foreign protocol version was accepted")


async def t_an_unconfigured_core_refuses_everything():
    """Fail closed: no credentials means no satellite, not every satellite."""
    result, ws = await _authenticate([_hello], credentials=None)
    require(result is None, "an unconfigured Core accepted a connection")
    require_equal(ws.closed_with[0], 1011, ws.closed_with)


async def t_many_failed_clients_do_not_break_the_core():
    srv = _server(_creds())
    for i in range(50):
        ws = _Socket([json.dumps({"type": "hello", "satellite_id": f"bot-{i}",
                                  "protocol_version": SA.PROTOCOL_VERSION,
                                  "client_nonce": SA.new_challenge(), "auth": "22" * 32})])
        require(await srv._authenticate(ws) is None, f"attempt {i} was accepted")
        require(not srv._busy, f"attempt {i} occupied the satellite slot")
    # and the real satellite still gets in afterwards
    ok, _ = await _authenticate([_hello])
    require_equal(ok, SAT_ID, "the real satellite was locked out by failed attempts")


# =====================================================================
# Crypto details
# =====================================================================
def t_verification_is_constant_time():
    src = inspect.getsource(SA.SatelliteCredentials.verify)
    require("hmac.compare_digest" in src, "verification is not constant-time")
    require("== auth" not in src and "auth ==" not in src,
            "verification compares the digest with ==")


def t_the_canonical_material_is_versioned_and_unambiguous():
    a = SA.canonical_material(protocol_version=1, server_nonce="ab" * 16,
                              satellite_id="pi-a", client_nonce="cd" * 16)
    require(a.startswith(b"solvio-satellite-auth-v1"), a[:40])
    # field boundaries cannot be shifted between neighbours
    b = SA.canonical_material(protocol_version=1, server_nonce="ab" * 16,
                              satellite_id="pi", client_nonce="a" + "cd" * 16)
    require(a != b, "two different bindings produced the same material")
    for bad in ({"satellite_id": "pi\nadmin"}, {"satellite_id": ""},
                {"server_nonce": "nothex"}, {"client_nonce": "zz"}):
        kwargs = {"protocol_version": 1, "server_nonce": "ab" * 16,
                  "satellite_id": "pi-a", "client_nonce": "cd" * 16, **bad}
        try:
            SA.canonical_material(**kwargs)
            require(False, f"{bad} was accepted into the canonical material")
        except SA.SatelliteAuthError:
            pass


def t_no_custom_crypto_primitive():
    src = open(os.path.join(REPO, "src", "solvio", "realtime", "satellite_auth.py"),
               encoding="utf-8").read()
    require("import hmac" in src and "sha256" in src, "the standard primitives are gone")
    for homegrown in ("def _xor", "def _mix", "def _rotate", "random.random"):
        require(homegrown not in src, f"a hand-rolled primitive appeared: {homegrown}")
    require("secrets.token_hex" in src, "the nonce is not from a CSPRNG")


# =====================================================================
# Part 5 — secret storage
# =====================================================================
def t_a_world_readable_credential_file_is_refused():
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        path = os.path.join(tmp, "satellite_auth.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"satellites": {SAT_ID: SECRET.hex()}}, fh)
        os.chmod(path, 0o644)
        try:
            SA.load_credentials(path)
            require(False, "a world-readable credential file was accepted")
        except SA.SatelliteAuthError as exc:
            require("readable by others" in str(exc), str(exc))
        os.chmod(path, 0o600)
        creds = SA.load_credentials(path)
        require_equal(creds.satellite_ids, (SAT_ID,), creds.satellite_ids)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_a_weak_or_missing_credential_is_refused():
    try:
        SA.SatelliteCredentials({})
        require(False, "an empty credential set was accepted")
    except SA.SatelliteAuthError:
        pass
    try:
        SA.SatelliteCredentials({SAT_ID: b"short"})
        require(False, "a short secret was accepted")
    except SA.SatelliteAuthError as exc:
        require("32 bytes" in str(exc), str(exc))


def t_the_provisioning_script_writes_a_private_file_and_hides_the_secret():
    tmp = os.path.realpath(tempfile.mkdtemp())
    try:
        path = os.path.join(tmp, "creds", "satellite_auth.json")
        out = subprocess.run(
            [sys.executable, os.path.join(REPO, "scripts",
                                          "provision_satellite_credential.py"),
             "pi-test", "--path", path],
            capture_output=True, text=True, cwd=REPO)
        require_equal(out.returncode, 0, out.stdout + out.stderr)
        mode = stat.S_IMODE(os.stat(path).st_mode)
        require_equal(mode, 0o600, f"credential file mode is {mode:04o}")
        with open(path, encoding="utf-8") as fh:
            secret_hex = json.load(fh)["satellites"]["pi-test"]
        require(len(secret_hex) >= 64, "the generated secret is too short")
        require(secret_hex not in out.stdout, "the secret was printed without --show-once")
        require("fingerprint" in out.stdout, out.stdout)
        # a second run must refuse to overwrite silently
        again = subprocess.run(
            [sys.executable, os.path.join(REPO, "scripts",
                                          "provision_satellite_credential.py"),
             "pi-test", "--path", path], capture_output=True, text=True, cwd=REPO)
        require(again.returncode != 0, "an existing credential was silently replaced")
        require("--rotate" in again.stderr, again.stderr)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_no_secret_material_is_committed():
    out = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True)
    tracked = out.stdout.split()
    for name in tracked:
        require("satellite_auth.json" not in name,
                f"a credential file is tracked in git: {name}")
    src = open(os.path.join(REPO, "src", "solvio", "realtime", "satellite_auth.py"),
               encoding="utf-8").read()
    require("DEFAULT_CREDENTIAL_PATH" in src, "the credential path is not configurable")
    require("~/.solvio/" in src, "the credential default is not out of the repository")


# =====================================================================
# Part 8 — what a refusal may say
# =====================================================================
async def t_a_refusal_logs_a_category_and_never_key_material():
    captured = []

    class _Log:
        def info(self, event, **kw):
            captured.append((event, kw))

        def warning(self, event, **kw):
            captured.append((event, kw))

        def error(self, event, **kw):
            captured.append((event, kw))

    forged = "ff" * 32
    ws = _Socket([json.dumps({"type": "hello", "satellite_id": SAT_ID,
                              "protocol_version": SA.PROTOCOL_VERSION,
                              "client_nonce": SA.new_challenge(), "auth": forged})])
    srv = _server(_creds())
    real = CS.log
    CS.log = _Log()
    try:
        await srv._authenticate(ws)
    finally:
        CS.log = real
    rejects = [kw for event, kw in captured if event == "core.satellite_auth_rejected"]
    require_equal(len(rejects), 1, captured)
    kw = rejects[0]
    blob = json.dumps(kw)
    require_equal(kw["reason"], "bad_auth", kw)
    require_equal(kw["satellite_id"], SAT_ID, kw)
    require("192.168.178.99" in blob, f"the source address is missing: {kw}")
    require(SECRET.hex() not in blob, "the shared secret reached the log")
    require(forged not in blob, "the presented HMAC reached the log")
    require(ws.challenge not in blob, "the challenge nonce reached the log")


def t_the_refusal_reasons_are_categories_not_material():
    creds = _creds()
    reasons = set()
    for kwargs in (
        {"satellite_id": SAT_ID, "protocol_version": 99},
        {"satellite_id": "nope\n", "protocol_version": SA.PROTOCOL_VERSION},
        {"satellite_id": "unbekannt", "protocol_version": SA.PROTOCOL_VERSION},
        {"satellite_id": SAT_ID, "protocol_version": SA.PROTOCOL_VERSION, "auth": "zz"},
        {"satellite_id": SAT_ID, "protocol_version": SA.PROTOCOL_VERSION},
    ):
        ok, reason = creds.verify(server_nonce="ab" * 16, client_nonce="cd" * 16,
                                  auth=kwargs.pop("auth", "00" * 32), **kwargs)
        require(not ok, kwargs)
        reasons.add(reason)
        require(len(reason) < 40 and " " not in reason, f"not a category: {reason}")
    require(len(reasons) >= 4, f"refusals are conflated: {reasons}")


# =====================================================================
# Parts 2/9 — bind configuration and versioning
# =====================================================================
def t_the_bind_address_is_configuration_not_source():
    src = open(CORE_SRC, encoding="utf-8").read()
    import re as _re
    hardcoded = _re.findall(r"\b(?:192\.168|10\.\d+|172\.(?:1[6-9]|2\d|3[01]))\.\d+\.\d+",
                            src)
    require_equal(hardcoded, [], f"a user-specific address is baked into the source: {hardcoded}")
    require("SOLVIO_SATELLITE_BIND" in src, "the bind host is not configurable by environment")
    require("socket.getaddrinfo" in src, "an invalid bind address is not rejected at startup")
    require("core.satellite_listener" in src, "the bind address is not logged at startup")


def t_the_protocol_is_versioned():
    require(isinstance(SA.PROTOCOL_VERSION, int), SA.PROTOCOL_VERSION)
    src = inspect.getsource(CS.CoreServer._authenticate)
    require("SA.PROTOCOL_VERSION" in src, "the challenge does not announce a version")
    ok, reason = _creds().verify(satellite_id=SAT_ID, server_nonce="ab" * 16,
                                 client_nonce="cd" * 16, protocol_version=None,
                                 auth="00" * 32)
    require(not ok and reason == "protocol_version_mismatch", reason)


def t_security_tree_matches_freeze_and_explicit_n2_amendment():
    from _security_amendment import require_n2_security_tree
    require_n2_security_tree(REPO)


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
