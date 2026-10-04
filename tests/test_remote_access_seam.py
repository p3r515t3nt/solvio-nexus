"""Remote Access V1 (PREBUILD) — die zehn Zusicherungen der Fernzugriffs-Naht.

Nur Standardbibliothek plus `solvio.remote_access` und die bereits freigegebenen
Contracts. KEIN Test braucht ein Netz, einen Tunnel, den Mac oder Hetzner: der
Transport ist eine Naht und wird mit Attrappen bespielt.

Vier der Zusicherungen (3-6) betreffen ausdruecklich NICHT den neuen Code,
sondern den bereits freigegebenen Sicherheitspfad. Sie stehen trotzdem hier,
weil Remote Access genau die Frage aufwirft, ob ein Vermittler daran etwas
aendert — und die Antwort muss gepinnt sein, nicht geglaubt.

Ausfuehren:  .venv/bin/python -m unittest tests.test_remote_access_seam -v
             (Windows: .venv/Scripts/python.exe -m unittest ...)
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))
from _guard import enforce_assertions  # noqa: E402

enforce_assertions()

import ast  # noqa: E402
import asyncio  # noqa: E402
import inspect  # noqa: E402
import pathlib  # noqa: E402
import re  # noqa: E402
import unittest  # noqa: E402
from dataclasses import FrozenInstanceError, fields  # noqa: E402
from unittest import IsolatedAsyncioTestCase  # noqa: E402

from solvio.capabilities import policy as P  # noqa: E402
from solvio.contracts.trust import AUTHORITY_BEARING, TrustLevel  # noqa: E402
from solvio.remote_access.config import (  # noqa: E402
    EndpointConfigError,
    endpoints_from_environment,
    parse_endpoints,
)
from solvio.remote_access.contract import (  # noqa: E402
    ConnectionState,
    PathKind,
    ProbeResult,
    RemoteAccessReport,
    RemoteEndpoint,
    RemoteTransport,
)
from solvio.remote_access.resolver import LocalFirstResolver, order_endpoints  # noqa: E402
from solvio.remote_access.tcp_probe import TcpProbeTransport, split_target  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parent.parent
PACKAGE = REPO / "src" / "solvio" / "remote_access"


def flat(text: str) -> str:
    """Ein Text ohne Zeilenumbrueche und Mehrfach-Leerraum.

    Ein Satz, der im Quelltext umbricht, ist derselbe Satz. Wer darauf mit
    `assertIn` prueft, ohne zu normalisieren, prueft die Zeilenbreite mit — und
    baut einen Test, den die naechste Formatierung rot faerbt.
    """
    return " ".join(text.split())


def code_names(path: pathlib.Path) -> set[str]:
    """Alle im CODE vorkommenden Bezeichner — ohne Docstrings und Kommentare.

    Der Unterschied ist der ganze Punkt: Dieses Paket DARF in seiner Prosa
    erklaeren, dass es `TrustContext` nicht anfassen wird. Es darf ihn nur nicht
    benutzen. Eine Textsuche kann das nicht unterscheiden, ein Syntaxbaum schon.
    """
    baum = ast.parse(path.read_text(encoding="utf-8"))
    namen: set[str] = set()
    for knoten in ast.walk(baum):
        if isinstance(knoten, ast.Name):
            namen.add(knoten.id)
        elif isinstance(knoten, ast.Attribute):
            namen.add(knoten.attr)
        elif isinstance(knoten, ast.arg):
            namen.add(knoten.arg)
        elif isinstance(knoten, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            namen.add(knoten.name)
        elif isinstance(knoten, ast.Import):
            namen.update(a.name for a in knoten.names)
        elif isinstance(knoten, ast.ImportFrom):
            namen.add(knoten.module or "")
            namen.update(a.name for a in knoten.names)
    return namen


def code_strings(path: pathlib.Path) -> list[str]:
    """Alle Zeichenketten-Literale, die KEIN Docstring sind."""
    baum = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for knoten in ast.walk(baum):
        if isinstance(knoten, ast.Module | ast.ClassDef | ast.FunctionDef
                      | ast.AsyncFunctionDef):
            erst = knoten.body[0] if knoten.body else None
            if (isinstance(erst, ast.Expr) and isinstance(erst.value, ast.Constant)
                    and isinstance(erst.value.value, str)):
                docstrings.add(id(erst.value))
    return [k.value for k in ast.walk(baum)
            if isinstance(k, ast.Constant) and isinstance(k.value, str)
            and id(k) not in docstrings]

LAN = RemoteEndpoint(name="lan", kind=PathKind.LOCAL,
                     base_url="https://core.invalid:8770")
TUNNEL = RemoteEndpoint(name="tunnel", kind=PathKind.REMOTE,
                        base_url="https://tunnel.invalid:8770")


class ScriptedTransport:
    """Attrappe: antwortet je Endpunktnamen nach Drehbuch, zaehlt Aufrufe."""

    def __init__(self, reachable: dict[str, bool], *, name: str = "scripted",
                 raises: set[str] | None = None) -> None:
        self._reachable = reachable
        self._name = name
        self._raises = raises or set()
        self.calls: list[str] = []

    @property
    def name(self) -> str:
        return self._name

    async def probe(self, endpoint: RemoteEndpoint, *, timeout_s: float) -> ProbeResult:
        self.calls.append(endpoint.name)
        if endpoint.name in self._raises:
            raise RuntimeError("Transport kaputt")
        return ProbeResult(endpoint=endpoint,
                           reachable=self._reachable.get(endpoint.name, False),
                           detail="scripted")


class HangingTransport:
    """Attrappe, die nie antwortet — der Fernweg, der einfach haengt."""

    name = "hanging"

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def probe(self, endpoint: RemoteEndpoint, *, timeout_s: float) -> ProbeResult:
        self.calls.append(endpoint.name)
        await asyncio.sleep(3600)
        raise AssertionError("unerreichbar")  # pragma: no cover


# --------------------------------------------------------------------------
# 1 · LOCAL_DIRECT bleibt funktional
# --------------------------------------------------------------------------
class TestLocalFirst(IsolatedAsyncioTestCase):

    async def test_lan_reachable_yields_local_direct(self):
        r = LocalFirstResolver(ScriptedTransport({"lan": True, "tunnel": True}),
                               [TUNNEL, LAN])
        report = await r.resolve()
        self.assertEqual(report.state, ConnectionState.LOCAL_DIRECT)
        self.assertEqual(report.active, LAN)
        self.assertIs(report.kind, PathKind.LOCAL)

    async def test_the_tunnel_is_not_even_touched_while_the_lan_stands(self):
        """Local-first ist keine Vorliebe, sondern Datensparsamkeit.

        Wuerde parallel gemessen, erfuehre der Vermittler von der Anwesenheit
        des Telefons, obwohl der lokale Weg stand.
        """
        transport = ScriptedTransport({"lan": True, "tunnel": True})
        await LocalFirstResolver(transport, [TUNNEL, LAN]).resolve()
        self.assertEqual(transport.calls, ["lan"])

    def test_local_sorts_before_remote_regardless_of_preference(self):
        spaet = RemoteEndpoint(name="lan", kind=PathKind.LOCAL,
                               base_url="https://a.invalid:1", preference=999)
        frueh = RemoteEndpoint(name="tunnel", kind=PathKind.REMOTE,
                               base_url="https://b.invalid:1", preference=0)
        self.assertEqual([e.name for e in order_endpoints([frueh, spaet])],
                         ["lan", "tunnel"])

    def test_ordering_is_stable_for_equal_endpoints(self):
        a = RemoteEndpoint(name="b", kind=PathKind.REMOTE, base_url="https://x:1")
        b = RemoteEndpoint(name="a", kind=PathKind.REMOTE, base_url="https://y:1")
        self.assertEqual([e.name for e in order_endpoints([a, b])], ["a", "b"])
        self.assertEqual([e.name for e in order_endpoints([b, a])], ["a", "b"])


# --------------------------------------------------------------------------
# 2 · Ein Ausfall des Fernwegs beschaedigt die LAN-Nutzung nicht
# --------------------------------------------------------------------------
class TestRemoteFailureDoesNotHarmLocal(IsolatedAsyncioTestCase):

    async def test_dead_tunnel_still_yields_local_direct(self):
        r = LocalFirstResolver(ScriptedTransport({"lan": True, "tunnel": False}),
                               [TUNNEL, LAN])
        self.assertEqual((await r.resolve()).state, ConnectionState.LOCAL_DIRECT)

    async def test_a_throwing_transport_does_not_abort_the_resolution(self):
        """Ein kaputter Transport darf einen spaeteren, gesunden Weg nicht verdecken."""
        transport = ScriptedTransport({"lan": True}, raises={"tunnel"})
        # Reihenfolge erzwingen: beide REMOTE waeren hier egal — es geht darum,
        # dass nach der Ausnahme weitergemessen wird.
        kaputt = RemoteEndpoint(name="tunnel", kind=PathKind.REMOTE,
                                base_url="https://t.invalid:1", preference=0)
        gut = RemoteEndpoint(name="lan", kind=PathKind.REMOTE,
                             base_url="https://l.invalid:1", preference=1)
        report = await LocalFirstResolver(transport, [kaputt, gut]).resolve()
        self.assertEqual(report.state, ConnectionState.REMOTE_CONNECTED)
        self.assertEqual(report.active, gut)
        self.assertFalse(report.probes[0].reachable)
        self.assertTrue(report.probes[0].detail.startswith("probe_error:"))

    async def test_a_hanging_transport_is_bounded_by_the_timeout(self):
        r = LocalFirstResolver(HangingTransport(), [LAN], timeout_s=0.05)
        report = await asyncio.wait_for(r.resolve(), timeout=10)
        self.assertEqual(report.state, ConnectionState.REMOTE_UNAVAILABLE)
        self.assertEqual(report.probes[0].detail, "timeout")

    async def test_nothing_reachable_is_a_state_not_an_exception(self):
        r = LocalFirstResolver(ScriptedTransport({}), [LAN, TUNNEL])
        report = await r.resolve()
        self.assertEqual(report.state, ConnectionState.REMOTE_UNAVAILABLE)
        self.assertIsNone(report.active)
        self.assertIsNone(report.kind)


# --------------------------------------------------------------------------
# 3 · Ein Vermittler kann keine Autoritaet erzeugen
# --------------------------------------------------------------------------
class TestRelayCannotCreateAuthority(unittest.TestCase):

    def test_no_type_in_the_seam_carries_authority_or_trust(self):
        """Die Naht hat die Mittel gar nicht.

        Kein Feld fuer Herkunft, Vertrauen, Freigabe, Nutzer oder Risiko — was
        nicht existiert, kann auch nicht falsch gesetzt werden.
        """
        verboten = ("trust", "origin", "authority", "approval", "approved",
                    "principal", "user", "risk", "credential", "token", "secret")
        for typ in (RemoteEndpoint, ProbeResult, RemoteAccessReport):
            for f in fields(typ):
                for wort in verboten:
                    self.assertNotIn(wort, f.name.lower(),
                                     f"{typ.__name__}.{f.name} riecht nach Autoritaet")

    def test_the_report_has_no_method_that_permits_anything(self):
        erlaubt = {"kind", "as_health"}
        oeffentlich = {n for n, _ in inspect.getmembers(RemoteAccessReport)
                       if not n.startswith("_")}
        self.assertTrue(oeffentlich <= erlaubt | set(RemoteAccessReport.__annotations__),
                        f"unerwartete oeffentliche Flaeche: {oeffentlich - erlaubt}")
        for name in oeffentlich:
            self.assertFalse(re.search(r"may_|can_|allow|authorize|approve", name),
                             f"RemoteAccessReport.{name} klingt nach Befugnis")

    def test_the_transport_protocol_offers_only_measuring(self):
        """`RemoteTransport` kann messen — nicht senden, nicht verbinden, nicht anmelden."""
        methoden = {n for n in dir(RemoteTransport) if not n.startswith("_")}
        self.assertEqual(methoden, {"name", "probe"})

    def test_the_seam_never_touches_the_frozen_security_path(self):
        """Kein Modul des Pakets importiert `solvio.security` — AST, nicht Textsuche."""
        for pfad in sorted(PACKAGE.glob("*.py")):
            baum = ast.parse(pfad.read_text(encoding="utf-8"))
            for knoten in ast.walk(baum):
                if isinstance(knoten, ast.Import):
                    namen = [a.name for a in knoten.names]
                elif isinstance(knoten, ast.ImportFrom):
                    namen = [knoten.module or ""]
                else:
                    continue
                for name in namen:
                    self.assertFalse(
                        name.startswith("solvio.security")
                        or name.startswith("solvio.capabilities"),
                        f"{pfad.name} importiert {name}")

    def test_a_report_cannot_be_mutated_after_the_fact(self):
        report = RemoteAccessReport(state=ConnectionState.LOCAL_DIRECT, active=LAN)
        with self.assertRaises(FrozenInstanceError):
            report.state = ConnectionState.REMOTE_CONNECTED  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            LAN.base_url = "https://woanders.invalid:8770"  # type: ignore[misc]


# --------------------------------------------------------------------------
# 4 · Ohne gueltige Geraeteidentitaet kein Zugang — und der Weg aendert das nicht
# --------------------------------------------------------------------------
class TestDeviceIdentityIsUnchangedByTransport(unittest.TestCase):
    """Diese Zusicherungen gehoeren dem eingefrorenen Sicherheitspfad.

    Sie stehen hier, weil Remote Access sie in Frage stellt. Faellt einer
    dieser Tests, ist nicht die Naht kaputt, sondern eine Annahme des
    Milestones falsch geworden.
    """

    def test_transport_auth_reads_two_headers_and_can_never_approve(self):
        pfad = REPO / "src/solvio/security/mobile_approval/gateway.py"
        quelle = flat(pfad.read_text(encoding="utf-8"))
        self.assertIn('request.headers.get("X-Device-Id"', quelle)
        self.assertIn('request.headers.get("X-Transport-Cred"', quelle)
        self.assertIn("It can NEVER approve", quelle)
        self.assertIn("Never bind the public internet", quelle)

    def test_an_unauthenticated_device_yields_none_not_a_default(self):
        """`_authed_device` gibt `None` zurueck — es faellt auf nichts zurueck.

        Geprueft am Syntaxbaum, nicht am Text: die Funktion hat genau zwei
        Ausgaenge, und der eine davon ist `None`. Ein spaeterer dritter
        Ausgang — etwa eine Voreinstellung fuer „unbekanntes Geraet" — waere
        genau die Sorte Bequemlichkeit, die ein Fernweg gefaehrlich macht.
        """
        baum = ast.parse((REPO / "src/solvio/security/mobile_approval/gateway.py")
                         .read_text(encoding="utf-8"))
        funktion = next(k for k in ast.walk(baum)
                        if isinstance(k, ast.AsyncFunctionDef)
                        and k.name == "_authed_device")
        rueckgaben = [k.value for k in ast.walk(funktion) if isinstance(k, ast.Return)]
        self.assertEqual(len(rueckgaben), 2, "die Funktion hat nicht mehr zwei Ausgaenge")
        self.assertTrue(
            any(isinstance(r, ast.Constant) and r.value is None for r in rueckgaben),
            "kein Ausgang gibt None zurueck")
        self.assertTrue(
            any(isinstance(r, ast.Name) and r.id == "device_id" for r in rueckgaben),
            "der gute Ausgang gibt nicht die geprüfte Kennung zurueck")

    def test_the_approval_proof_is_bound_to_core_device_and_nonce_not_to_a_route(self):
        """Kein Feld der signierten Nutzlast beschreibt einen Netzweg.

        Genau deshalb aendert ein Tunnel an der Beweiskette nichts — und genau
        deshalb kann ein Vermittler sie auch nicht faelschen.
        """
        protokoll = (REPO / "src/solvio/security/mobile_approval/protocol.py").read_text(
            encoding="utf-8")
        for feld in ("core_instance_id", "device_id", "challenge_nonce", "action_digest"):
            self.assertIn(feld, protokoll)
        for netzwort in ("remote_addr", "peer_ip", "client_ip", "source_ip",
                         "transport_path", "via_relay"):
            self.assertNotIn(netzwort, protokoll,
                             f"die signierte Nutzlast kennt {netzwort} — das waere neu")


# --------------------------------------------------------------------------
# 5 · Replay wird nicht still angenommen
# --------------------------------------------------------------------------
class TestReplayIsNotSilentlyAccepted(unittest.TestCase):

    def test_the_app_attest_counter_must_strictly_increase(self):
        quelle = (REPO / "src/solvio/security/mobile_approval/app_attest.py").read_text(
            encoding="utf-8")
        self.assertRegex(quelle, r"counter\s*<=\s*|<=\s*.*counter|counter.*strictly",
                         "der Assertion-Counter wird nicht sichtbar verglichen")

    def test_the_challenge_nonce_is_burned_in_a_conditional_update(self):
        quelle = (REPO / "src/solvio/security/mobile_approval/store.py").read_text(
            encoding="utf-8")
        self.assertIn("display_mismatch", quelle)
        self.assertIn("challenge_payload_sha256", quelle)

    def test_a_probe_result_is_not_a_credential_and_cannot_be_replayed_into_one(self):
        """Ein Messergebnis traegt nichts, was irgendwo als Nachweis gaelte."""
        r = ProbeResult(endpoint=LAN, reachable=True, detail="open", latency_ms=1.0)
        self.assertEqual({f.name for f in fields(r)},
                         {"endpoint", "reachable", "detail", "latency_ms"})


# --------------------------------------------------------------------------
# 6 · Ein Fernweg kann keine Risikoklasse senken
# --------------------------------------------------------------------------
class TestRemotePathCannotLowerRisk(unittest.TestCase):

    def test_the_channel_table_knows_no_transport_and_defaults_conservatively(self):
        """`origin_for_session` schaut auf den KANAL, nie auf den Netzweg.

        Ein iPhone bleibt `voice_iphone`, ob es im WLAN steht oder im Zug — die
        Attestierung ist dieselbe. Und ein unbekannter Kanal bekommt nicht
        etwa mehr Rechte, sondern `UNSPECIFIED`.
        """
        quelle = (REPO / "src/solvio/capabilities/policy.py").read_text(encoding="utf-8")
        self.assertIn("_CHANNEL_ORIGIN", quelle)
        for netzwort in ("remote_access", "tunnel", "wireguard", "tailscale", "vpn"):
            self.assertNotIn(netzwort, quelle.lower(),
                             f"die Freigabematrix kennt {netzwort} — Transport waere Politik")

    def test_external_untrusted_denies_everything_but_reading(self):
        zeile = P.MATRIX[P.OriginClass.EXTERNAL_UNTRUSTED]
        self.assertEqual(zeile[P.ActionClass.READ_ONLY], P.Decision.EXECUTE_DIRECTLY)
        for klasse, entscheidung in zeile.items():
            if klasse is not P.ActionClass.READ_ONLY:
                self.assertEqual(entscheidung, P.Decision.DENY,
                                 f"{klasse} waere ueber einen fremden Kanal erreichbar")

    def test_only_two_trust_levels_bear_authority(self):
        self.assertEqual(AUTHORITY_BEARING,
                         frozenset({TrustLevel.SYSTEM_TRUSTED, TrustLevel.USER_DIRECT}))

    def test_the_seam_defines_no_risk_and_no_decision_vocabulary(self):
        """Am Syntaxbaum geprueft: die Prosa darf davon reden, der Code nicht."""
        politik = {"RiskLevel", "Decision", "ActionClass", "OriginClass",
                   "TrustContext", "TrustLevel", "effective_risk",
                   "authority_refusal", "may_authorize"}
        for pfad in sorted(PACKAGE.glob("*.py")):
            treffer = code_names(pfad) & politik
            self.assertEqual(treffer, set(),
                             f"{pfad.name} benutzt {treffer} — Transport waere Politik")


# --------------------------------------------------------------------------
# 7 · Geheimnisse landen nicht beim Vermittler
# --------------------------------------------------------------------------
class TestNoSecretsReachTheRelay(IsolatedAsyncioTestCase):

    async def test_the_probe_sends_not_one_byte(self):
        """Gemessen wird der Verbindungsaufbau, nicht ein Request.

        Ein Server, der nur `accept()` macht und nie liest, gilt als
        erreichbar — genau das beweist, dass nichts gesendet wurde.
        """
        empfangen: list[bytes] = []

        async def handler(reader, writer):
            try:
                empfangen.append(await asyncio.wait_for(reader.read(64), timeout=0.3))
            except asyncio.TimeoutError:
                empfangen.append(b"")
            writer.close()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            ziel = RemoteEndpoint(name="lokal", kind=PathKind.LOCAL,
                                  base_url=f"https://127.0.0.1:{port}")
            ergebnis = await TcpProbeTransport("test").probe(ziel, timeout_s=2.0)
            await asyncio.sleep(0.4)
        self.assertTrue(ergebnis.reachable)
        self.assertEqual(empfangen, [b""], "die Messung hat Daten gesendet")

    def test_the_health_line_carries_no_address(self):
        report = RemoteAccessReport(state=ConnectionState.REMOTE_CONNECTED, active=TUNNEL)
        health = report.as_health()
        flach = repr(health)
        self.assertNotIn("tunnel.invalid", flach)
        self.assertNotIn("8770", flach)
        self.assertEqual(health["endpoint"], "tunnel")

    def test_the_seam_never_reads_a_credential_from_the_environment(self):
        erlaubt = {"SOLVIO_REMOTE_ACCESS_ENDPOINTS"}
        for pfad in sorted(PACKAGE.glob("*.py")):
            baum = ast.parse(pfad.read_text(encoding="utf-8"))
            for knoten in ast.walk(baum):
                if isinstance(knoten, ast.Constant) and isinstance(knoten.value, str):
                    if knoten.value.startswith("SOLVIO_") and knoten.value not in erlaubt:
                        self.fail(f"{pfad.name} liest {knoten.value}")


# --------------------------------------------------------------------------
# 8 · Der Transport ist sauber austauschbar
# --------------------------------------------------------------------------
class TestTransportIsReplaceable(IsolatedAsyncioTestCase):

    def test_the_shipped_transport_satisfies_the_protocol(self):
        self.assertIsInstance(TcpProbeTransport("wireguard"), RemoteTransport)

    def test_a_foreign_object_satisfies_it_too(self):
        self.assertIsInstance(ScriptedTransport({}), RemoteTransport)
        self.assertNotIsInstance(object(), RemoteTransport)

    async def test_swapping_the_transport_changes_no_line_of_the_resolver(self):
        endpunkte = [LAN, TUNNEL]
        eins = await LocalFirstResolver(ScriptedTransport({"lan": True}, name="a"),
                                        endpunkte).resolve()
        zwei = await LocalFirstResolver(ScriptedTransport({"lan": True}, name="b"),
                                        endpunkte).resolve()
        self.assertEqual(eins.state, zwei.state)
        self.assertEqual(eins.active, zwei.active)

    def test_the_resolver_does_not_import_any_provider(self):
        quelle = (PACKAGE / "resolver.py").read_text(encoding="utf-8").lower()
        for anbieter in ("wireguard", "tailscale", "cloudflare", "ssh", "wg-quick"):
            self.assertNotIn(f"import {anbieter}", quelle)


# --------------------------------------------------------------------------
# 9 · Keine Abhaengigkeit von festen Adressen
# --------------------------------------------------------------------------
class TestNoMagicAddresses(unittest.TestCase):

    IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

    def test_no_module_of_the_seam_contains_an_ip_literal(self):
        """Kein IP-Literal im Code. Docstrings duerfen erklaeren, was NICHT da ist."""
        for pfad in sorted(PACKAGE.glob("*.py")):
            for wert in code_strings(pfad):
                treffer = self.IPV4.findall(wert)
                self.assertEqual(treffer, [],
                                 f"{pfad.name} enthaelt die Adresse {treffer}")

    def test_no_module_names_a_known_solvio_network(self):
        muster = ("10.77.", "192.168.", "hetzner", "localhost", "127.0.0.1",
                  "wireguard", "tailscale")
        for pfad in sorted(PACKAGE.glob("*.py")):
            for wert in code_strings(pfad):
                for m in muster:
                    self.assertNotIn(m, wert.lower(),
                                     f"{pfad.name} verdrahtet {m!r} in {wert!r}")
            for name in code_names(pfad):
                for m in muster:
                    self.assertNotIn(m, name.lower(),
                                     f"{pfad.name} nennt {m} als Bezeichner")

    def test_endpoints_come_from_configuration_and_a_bad_one_is_loud(self):
        endpunkte = parse_endpoints(
            "lan:local:https://a.invalid:8770,tunnel:remote:https://b.invalid:8770")
        self.assertEqual([e.name for e in endpunkte], ["lan", "tunnel"])
        self.assertEqual(endpunkte[0].kind, PathKind.LOCAL)
        for kaputt in ("", "lan:local", "lan:seitwaerts:https://a.invalid:1",
                       "lan:local:", ":local:https://a.invalid:1",
                       "a:local:https://x:1,a:remote:https://y:1"):
            with self.assertRaises(EndpointConfigError, msg=f"{kaputt!r} kam durch"):
                parse_endpoints(kaputt)

    def test_an_absent_configuration_is_quiet_and_empty(self):
        alt = os.environ.pop("SOLVIO_REMOTE_ACCESS_ENDPOINTS", None)
        try:
            self.assertEqual(endpoints_from_environment(), [])
        finally:
            if alt is not None:
                os.environ["SOLVIO_REMOTE_ACCESS_ENDPOINTS"] = alt

    def test_the_target_is_derived_from_the_url_not_guessed(self):
        self.assertEqual(split_target("https://irgendwo.invalid:8770"),
                         ("irgendwo.invalid", 8770))
        self.assertEqual(split_target("https://irgendwo.invalid"),
                         ("irgendwo.invalid", 443))
        for kaputt in ("", "https://", ":::"):
            with self.assertRaises(ValueError):
                split_target(kaputt)


# --------------------------------------------------------------------------
# 10 · Kein offener Heimrouter-Port
# --------------------------------------------------------------------------
class TestNoInboundHomePortIsRequired(unittest.TestCase):

    def test_the_seam_never_listens_and_never_binds(self):
        """Sie misst ausgehend. Ein `bind`/`listen` waere genau der Fehler."""
        verboten = {"start_server", "create_server", "bind", "listen",
                    "TCPSite", "AppRunner", "start_unix_server"}
        for pfad in sorted(PACKAGE.glob("*.py")):
            treffer = code_names(pfad) & verboten
            self.assertEqual(treffer, set(), f"{pfad.name} benutzt {treffer}")
            for wert in code_strings(pfad):
                self.assertNotIn("0.0.0.0", wert, f"{pfad.name} nennt 0.0.0.0")

    def test_only_outgoing_connections_are_opened(self):
        self.assertIn("open_connection", code_names(PACKAGE / "tcp_probe.py"))

    def test_the_released_position_on_inbound_ports_still_stands(self):
        """Ein Test, der die Zusage aufbewahrt, statt sie in Prosa zu lassen.

        Faellt er, hat jemand ADR-0018 oder DEBT-0079 umgeschrieben. Das darf
        passieren — aber nicht unbemerkt, und nicht als Nebenwirkung.
        """
        adr = flat((REPO / "docs/decisions/"
                    "ADR-0018-iphone-ist-sprachendpunkt-nicht-autoritaet.md")
                   .read_text(encoding="utf-8"))
        self.assertIn("8770 wird nicht ins Internet geoeffnet", adr)
        schuld = flat((REPO / "docs/debt/TECH_DEBT.md").read_text(encoding="utf-8"))
        self.assertIn("Tunnel oder VPN), keine Portfreigabe", schuld)


# --------------------------------------------------------------------------
# Die Voraussetzungen, aus denen die Betriebsauflagen folgen
# --------------------------------------------------------------------------
class TestThePremisesBehindTheOperationalRules(unittest.TestCase):
    """Diese Tests pruefen NICHT den neuen Code, sondern seine Begruendung.

    Jede Betriebsauflage des Entwurfs folgt aus einer Eigenschaft des
    bestehenden Systems. Aendert sich die Eigenschaft, muss sich die Auflage
    aendern — und das soll auffallen, statt in einem Dokument zu veralten.
    """

    def test_the_satellite_port_would_still_bind_every_interface_by_default(self):
        """Die Vorgabe ist unveraendert — und deshalb ist die Gefahr echt.

        Der Windows-Prebuild leitete hieraus eine Routing-Auflage ab. Die
        Messung am 2026-09-04 hat gezeigt, dass die Auflage zu spaet kommt:
        der Port war da bereits ueber den seit STEP 21.2E bestehenden Tunnel
        erreichbar, ohne dass irgendjemand eine Route hinzugefuegt haette.
        Was schuetzt, ist deshalb nicht die Route, sondern die Bindeadresse der
        Produktion — siehe den naechsten Test.
        """
        quelle = flat((REPO / "src/solvio/realtime/core_server.py").read_text(
            encoding="utf-8"))
        self.assertIn('SOLVIO_SATELLITE_BIND", "0.0.0.0"', quelle,
                      "die Vorgabe hat sich geaendert — der Entwurf gehoert nachgezogen")

    def test_the_production_service_does_not_bind_the_satellite_everywhere(self):
        """Die Zusicherung, die den Klartextport wirklich zumacht.

        Faellt dieser Test, laeuft der produktive Core wieder mit einem
        Satellitenport auf ALLEN Schnittstellen — und damit auch auf der
        Overlay-Adresse des Tunnels, an deren anderem Ende ein Rechner im
        oeffentlichen Internet steht.
        """
        plist = (REPO / "deploy/com.solvio.core.plist").read_text(encoding="utf-8")
        self.assertIn("SOLVIO_SATELLITE_BIND", plist,
                      "die Dienstdefinition sagt nichts ueber die Bindeadresse")
        gebunden = re.search(
            r"<key>SOLVIO_SATELLITE_BIND</key>\s*<string>([^<]*)</string>", plist)
        self.assertIsNotNone(gebunden, "SOLVIO_SATELLITE_BIND ohne Wert")
        adressen = [a.strip() for a in gebunden.group(1).split(",") if a.strip()]
        self.assertNotIn("0.0.0.0", adressen, "die Produktion bindet jede Schnittstelle")
        self.assertTrue(adressen, "leere Bindeangabe")
        for adresse in adressen:
            self.assertNotIn("10.77.", adresse,
                             f"{adresse} ist die Tunnel-Overlay-Adresse")

    def test_the_satellite_bind_accepts_a_list_and_refuses_nothing_at_all(self):
        from solvio.realtime.core_server import satellite_bind_hosts
        self.assertEqual(satellite_bind_hosts("127.0.0.1"), ["127.0.0.1"])
        self.assertEqual(satellite_bind_hosts(" 127.0.0.1 , 192.168.178.123 "),
                         ["127.0.0.1", "192.168.178.123"])
        for kaputt in ("", "   ", ",,,"):
            with self.assertRaises(ValueError, msg=f"{kaputt!r} kam durch"):
                satellite_bind_hosts(kaputt)

    def test_the_satellite_port_still_describes_itself_as_no_pki(self):
        quelle = flat((REPO / "src/solvio/realtime/satellite_auth.py").read_text(
            encoding="utf-8"))
        self.assertIn("Not TLS, not a PKI, not device attestation", quelle)

    def test_the_gateway_certificate_still_carries_exactly_one_san(self):
        """Die Begruendung dafuer, dass die App ihre Adresse behaelt.

        Das Zertifikat entsteht EINMAL und wird danach zurueckgegeben, ohne den
        uebergebenen `host` anzusehen. Deshalb aendert die zweite Bindung auf
        der Overlay-Adresse weder Zertifikat noch Fingerabdruck — und deshalb
        muss kein Geraet neu gekoppelt werden.

        Und deshalb bleibt die App bei `https://<lan-adresse>:8770`: der SAN
        passt weiterhin zu der Adresse, die das Telefon anspricht. Der
        Vermittler schreibt das Ziel um, nicht die App.
        """
        baum = ast.parse((REPO / "src/solvio/security/mobile_approval/pairing.py")
                         .read_text(encoding="utf-8"))
        funktion = next(k for k in ast.walk(baum)
                        if isinstance(k, ast.FunctionDef)
                        and k.name == "load_or_create_gateway_cert")
        namen = {k.attr for k in ast.walk(funktion) if isinstance(k, ast.Attribute)}
        self.assertIn("SubjectAlternativeName", namen)
        # Der Zwischenspeicher: eine vorhandene Datei wird zurueckgegeben, OHNE
        # `host` anzusehen. Genau deshalb aendert eine neue Bind-Adresse das
        # Zertifikat nicht.
        quelle = flat(ast.get_source_segment(
            (REPO / "src/solvio/security/mobile_approval/pairing.py").read_text(
                encoding="utf-8"), funktion) or "")
        self.assertIn("if os.path.isfile(cpath) and os.path.isfile(kpath):", quelle)

    def test_the_transport_credential_still_has_no_expiry_column(self):
        """Die Begruendung fuer die offene Frage in §5.6 der Architektur."""
        quelle = (REPO / "src/solvio/security/mobile_approval/store.py").read_text(
            encoding="utf-8")
        self.assertIn("transport_cred_hash", quelle)
        self.assertNotIn("transport_cred_expires", quelle)


# --------------------------------------------------------------------------
# 11 · Die zweite Bindung zeigt nie ins oeffentliche Netz
# --------------------------------------------------------------------------
class TestTheExtraBindIsNeverPublic(unittest.TestCase):
    """Der eine Codeeingriff des Milestones — und sein Riegel.

    Gemessen am 2026-09-04: Der Mac kann ein Paket, dessen ABSENDER seine
    LAN-Adresse ist, nicht in den Tunnel geben. Eine Anfrage an die LAN-Adresse
    kommt an, die Antwort verlaesst den Rechner nie (fuenf TCP-SYN, zwei ICMP,
    null Antworten im Mitschnitt der Gegenseite). Deshalb schreibt der
    Vermittler das Ziel auf die Overlay-Adresse um, und deshalb muss der
    Freigabeweg dort zusaetzlich lauschen.

    Eine zweite Bindung ist genau die Sorte Erweiterung, die still gefaehrlich
    wird. Diese Zusicherungen halten sie fest.
    """

    def test_a_public_address_is_refused(self):
        from solvio.capabilities.approver_runtime import (
            ExtraHostRefused, check_extra_host,
        )
        for oeffentlich in ("46.225.177.22", "8.8.8.8", "2001:4860:4860::8888"):
            with self.assertRaises(ExtraHostRefused, msg=f"{oeffentlich} kam durch"):
                check_extra_host(oeffentlich)

    def test_binding_every_interface_is_refused(self):
        from solvio.capabilities.approver_runtime import (
            ExtraHostRefused, check_extra_host,
        )
        for unbestimmt in ("0.0.0.0", "::", "  ", ""):
            with self.assertRaises(ExtraHostRefused, msg=f"{unbestimmt!r} kam durch"):
                check_extra_host(unbestimmt)

    def test_a_name_is_refused_instead_of_resolved(self):
        """Ein Name ist eine Aussage, die ein DNS-Server spaeter aendern kann."""
        from solvio.capabilities.approver_runtime import (
            ExtraHostRefused, check_extra_host,
        )
        for name in ("localhost", "mac-mini.fritz.box", "example.com"):
            with self.assertRaises(ExtraHostRefused, msg=f"{name} wurde aufgeloest"):
                check_extra_host(name)

    def test_a_private_address_is_accepted(self):
        from solvio.capabilities.approver_runtime import check_extra_host
        for privat in ("10.77.0.2", "127.0.0.1", "192.168.178.123", "fd00::1"):
            self.assertEqual(check_extra_host(f" {privat} "), privat)

    def test_the_environment_skips_the_bad_and_keeps_the_good(self):
        from solvio.capabilities import approver_runtime as AR
        alt = os.environ.get(AR.ENV_EXTRA_HOSTS)
        try:
            os.environ[AR.ENV_EXTRA_HOSTS] = (
                "10.77.0.2, 8.8.8.8 ,0.0.0.0,,10.77.0.2, 192.168.178.123")
            self.assertEqual(
                AR.extra_hosts_from_environment(primary="192.168.178.123"),
                ["10.77.0.2"],
                "eine oeffentliche, eine unbestimmte, eine doppelte oder die "
                "Hauptadresse ist durchgerutscht")
            os.environ.pop(AR.ENV_EXTRA_HOSTS)
            self.assertEqual(
                AR.extra_hosts_from_environment(primary="192.168.178.123"), [])
        finally:
            os.environ.pop(AR.ENV_EXTRA_HOSTS, None)
            if alt is not None:
                os.environ[AR.ENV_EXTRA_HOSTS] = alt

    def test_a_failing_extra_bind_never_takes_the_approval_path_with_it(self):
        """Am Syntaxbaum: die Zusatzbindung faengt, die Hauptbindung nicht.

        Ein Tunnel, der beim Start unten ist, darf den Freigabeweg nicht
        verhindern — sonst haengt die Freigabe im Haus an einem Rechner in
        einem Rechenzentrum. Umgekehrt darf die LAN-Bindung NICHT stillschweigend
        scheitern duerfen: ohne sie gibt es keinen Freigabeweg, und das muss laut
        sein.
        """
        quelle = (REPO / "src/solvio/capabilities/approver_runtime.py").read_text(
            encoding="utf-8")
        baum = ast.parse(quelle)
        extra = next(k for k in ast.walk(baum)
                     if isinstance(k, ast.AsyncFunctionDef)
                     and k.name == "_bind_extra_hosts")
        behandler = [h for k in ast.walk(extra) if isinstance(k, ast.Try)
                     for h in k.handlers]
        self.assertTrue(behandler, "die Zusatzbindung faengt nichts ab")
        self.assertTrue(
            any(isinstance(h.type, ast.Name) and h.type.id == "OSError"
                for h in behandler),
            "die Zusatzbindung faengt keinen Bindefehler ab")

        start = next(k for k in ast.walk(baum)
                     if isinstance(k, ast.AsyncFunctionDef) and k.name == "start")
        in_try = {id(k) for t in ast.walk(start) if isinstance(t, ast.Try)
                  for k in ast.walk(t)}
        primaer = [k for k in ast.walk(start) if isinstance(k, ast.Call)
                   and isinstance(k.func, ast.Attribute)
                   and k.func.attr == "TCPSite"]
        self.assertEqual(len(primaer), 1, "die Hauptbindung ist nicht mehr eindeutig")
        self.assertNotIn(id(primaer[0]), in_try,
                         "die LAN-Bindung darf nicht stillschweigend scheitern")


# --------------------------------------------------------------------------
# 12 · SOLVIO kann sagen, dass der Fernweg nicht steht
# --------------------------------------------------------------------------
class TestTheHealthProbeIsHonest(IsolatedAsyncioTestCase):
    """RA7 in einer Zusicherung: ein ausgefallener Tunnel ist ein ZUSTAND.

    Ohne diese Sonde waere der Fernweg eine Eigenschaft, die niemand messen
    kann — und die erste Auskunft darueber waere ein Zeitablauf auf dem
    Telefon des Eigentuemers.
    """

    def _sonde(self):
        from solvio.control_center.probes import build
        sonden = {p.key: p for p in build(dispatcher=None, settings=None)}
        self.assertIn("remote_access", sonden, "es gibt keine Fernzugangs-Sonde")
        return sonden["remote_access"]

    async def test_not_configured_is_unknown_and_never_green(self):
        from solvio.control_center.health import State
        alt = os.environ.pop("SOLVIO_REMOTE_ACCESS_ENDPOINTS", None)
        try:
            zustand, grund = await self._sonde().check()
        finally:
            if alt is not None:
                os.environ["SOLVIO_REMOTE_ACCESS_ENDPOINTS"] = alt
        self.assertEqual(zustand, State.UNKNOWN)
        self.assertNotEqual(zustand, State.HEALTHY)
        self.assertIn("nicht eingerichtet", grund)

    async def test_an_unreachable_hub_is_a_state_not_an_outage(self):
        from solvio.control_center.health import State
        alt = os.environ.get("SOLVIO_REMOTE_ACCESS_ENDPOINTS")
        try:
            # Port 9 (discard) auf einer Adresse aus dem TEST-NET-1-Block:
            # garantiert nicht erreichbar und garantiert niemandes Rechner.
            os.environ["SOLVIO_REMOTE_ACCESS_ENDPOINTS"] = \
                "hub:remote:https://192.0.2.1:19997"
            zustand, grund = await self._sonde().check()
        finally:
            os.environ.pop("SOLVIO_REMOTE_ACCESS_ENDPOINTS", None)
            if alt is not None:
                os.environ["SOLVIO_REMOTE_ACCESS_ENDPOINTS"] = alt
        self.assertEqual(zustand, State.UNAVAILABLE)
        self.assertIn("nicht erreichbar", grund)

    async def test_the_reason_never_carries_an_address(self):
        """Eine Gesundheitszeile wandert auf ein Telefondisplay und in Logs."""
        from solvio.control_center.health import State
        alt = os.environ.get("SOLVIO_REMOTE_ACCESS_ENDPOINTS")
        try:
            os.environ["SOLVIO_REMOTE_ACCESS_ENDPOINTS"] = \
                "hub:remote:https://192.0.2.1:19997"
            zustand, grund = await self._sonde().check()
        finally:
            os.environ.pop("SOLVIO_REMOTE_ACCESS_ENDPOINTS", None)
            if alt is not None:
                os.environ["SOLVIO_REMOTE_ACCESS_ENDPOINTS"] = alt
        self.assertEqual(zustand, State.UNAVAILABLE)
        self.assertNotIn("192.0.2.1", grund, "die Adresse steht in der Gesundheitszeile")
        self.assertNotIn("19997", grund, "der Port steht in der Gesundheitszeile")
        self.assertNotIn("hub", grund.lower(), "der Endpunktname steht darin")

    async def test_a_broken_configuration_is_loud_but_not_fatal(self):
        from solvio.control_center.health import State
        alt = os.environ.get("SOLVIO_REMOTE_ACCESS_ENDPOINTS")
        try:
            os.environ["SOLVIO_REMOTE_ACCESS_ENDPOINTS"] = "voellig kaputt"
            zustand, grund = await self._sonde().check()
        finally:
            os.environ.pop("SOLVIO_REMOTE_ACCESS_ENDPOINTS", None)
            if alt is not None:
                os.environ["SOLVIO_REMOTE_ACCESS_ENDPOINTS"] = alt
        self.assertEqual(zustand, State.DEGRADED)
        self.assertIn("falsch eingerichtet", grund)


if __name__ == "__main__":
    from _harness import run_unittest

    raise SystemExit(run_unittest(globals(), __name__))
