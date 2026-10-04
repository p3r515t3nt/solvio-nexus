"""Der WISSEN-Schreibweg fuers iPhone — und warum er Approval Policy V2 nicht aufweicht.

ADR-0022 gibt der BEWIESENEN interaktiven App eine Matrixzeile, in der
NORMAL_WRITE und CRITICAL direkt laufen: wer in der attestierten App auf genau
eine Handlung tippt, hat sie damit ausgeloest. Der Endpunkt hier ist der
Transport dieses Beweises — je Anfrage eine frische App-Attest-Assertion ueber
eine Core-Nonce, gebunden an den SHA-256 der Handlung. Die Entscheidung selbst
faellt danach unveraendert im echten `CapabilityRouter` an der echten Matrix.

Der Kern der Suite ist deshalb doppelt:

* Der gute Fall MUSS direkt laufen — der Router hat hier absichtlich KEINEN
  Freigabekanal (`approvals=None`, fail-closed). Haette die Policy eine
  Freigabe verlangt, waere das Ergebnis `no_approval_channel` statt SUCCESS.
  Ein SUCCESS beweist also: die Matrix hat direkt entschieden, nicht der
  Endpunkt.
* Alles andere MUSS fallen, ohne dass der Handler je laeuft: verbrauchte und
  fremde Nonces, nach dem Signieren veraenderte Argumente, verbogene
  Assertions, fremde Domain-Separatoren — und strukturell jede Faehigkeit
  ausserhalb der geschlossenen Fuenferliste, auch wenn der Router sie kennt.
  Der Endpunkt darf nie zum generischen Capability-RPC werden.

Fail-closed heisst hier auch: fehlender Verifizierer, unbekanntes Geraet und
ein kaputter Speicher sind ein 401 und nie ein 500 — ein halb aufgebauter Core
gibt keinen Schreibweg frei und stirbt auch nicht daran.

HERMETISCH: in-process aiohttp TestClient, Fake-Kontrollebene mit
`FakeAppAttestVerifier` (echte ECDSA-Signaturen, nur die Apple-Kette fehlt)
und ein ECHTER `CapabilityRouter` mit den ECHTEN Memory-Specs und
Fake-Handlern. Kein Netz, kein TLS, kein echtes Telefon.

ASSERTION POLICY: `require*` aus `tests/_guard.py` sind Funktionsaufrufe und
ueberleben `python -O`.

Direkt: python -m tests.test_memory_mutation_endpoint
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from _guard import enforce_assertions, require, require_equal  # noqa: E402

enforce_assertions()

import mobile_attest_helper as H  # noqa: E402
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from solvio import memory_mutation_endpoint as MME  # noqa: E402
from solvio import voice_session_proof as VSP  # noqa: E402
from solvio.capabilities import policy as PL  # noqa: E402
from solvio.capabilities.contract import (  # noqa: E402
    ArgumentSource, CapabilitySpec, ExecutionClass,
)
from solvio.capabilities.memory import SPECS as MEMORY_SPECS  # noqa: E402
from solvio.capabilities.router import CapabilityRouter  # noqa: E402
from solvio.contracts.trust import TrustLevel  # noqa: E402
from solvio.security.mobile_approval import app_attest as AA  # noqa: E402
from solvio.security.mobile_approval import attest_protocol as AP  # noqa: E402
from solvio.security.mobile_approval.execution import (  # noqa: E402
    IDEMPOTENT_WRITE, NON_IDEMPOTENT_WRITE,
)
from solvio.tools.base import RiskLevel  # noqa: E402

VEC = os.path.join(os.path.dirname(__file__), "vectors", "app_attest_binding_v1.json")

#: Faehigkeiten, die der Router KENNT und der Endpunkt trotzdem nie erreichen
#: darf. Genau darum sind sie hier registriert: die geschlossene Liste muss
#: VOR dem Router greifen, nicht durch dessen Unkenntnis.
FREMDE_SPECS: dict[str, CapabilitySpec] = {
    "ha_turn_on": CapabilitySpec(
        name="ha_turn_on", version=1, execution_class=ExecutionClass.CONTROLLED,
        base_risk=RiskLevel.MUTATING, semantics=IDEMPOTENT_WRITE,
        description="Testattrappe — darf nie ueber den Schreibweg laufen"),
    "gmail_send_draft": CapabilitySpec(
        name="gmail_send_draft", version=1, execution_class=ExecutionClass.CONTROLLED,
        base_risk=RiskLevel.CRITICAL, semantics=NON_IDEMPOTENT_WRITE,
        description="Testattrappe — darf nie ueber den Schreibweg laufen"),
}


# ---------------------------------------------------------------- Aufbau ----

class _Store:
    """Der Geraetestand, wie `verify_mutation_proof` ihn liest."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    async def get_device(self, device_id):
        return self.rows.get(device_id)


class _BrokenStore:
    """Ein Speicher, der beim Lesen ausfaellt — Platte voll, Datei gesperrt."""

    async def get_device(self, device_id):
        raise RuntimeError("database is locked")


class _ControlPlane:
    """Genau die Felder, die der Endpunkt benutzt: Transportpruefung,
    Geraetestand, Verifizierer, Core-Kennung. Hermetisch statt SQLite."""

    def __init__(self, verifier) -> None:
        self.store = _Store()
        self.attest_verifier = verifier
        self.core_instance_id = "core-test-0001"
        self.creds: dict[str, str] = {}

    async def verify_transport_cred(self, device_id, cred):
        expected = self.creds.get(device_id, "")
        return bool(expected) and secrets.compare_digest(expected, cred or "")


class _Device:
    """Ein eingeschriebenes Telefon: App-Attest-Schluessel + Transportkennung."""

    def __init__(self, device_id: str) -> None:
        self.device_id = device_id
        self.aakey, self.x963, self.aakid = H.aa_key()
        self.cred = "cred-" + device_id

    def headers(self) -> dict[str, str]:
        return {"X-Device-Id": self.device_id, "X-Transport-Cred": self.cred}


class _Memory:
    """Die Fake-Handler hinter den ECHTEN Specs. Zeichnet jeden Aufruf auf."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def handler(self, name: str):
        async def run(arguments: dict) -> dict:
            self.calls.append((name, dict(arguments)))
            return {"done": name}
        return run


class _Wired:
    def __init__(self, client, cp, dev, memory, events, router, server) -> None:
        self.client = client
        self.cp = cp
        self.dev = dev
        self.memory = memory
        self.events = events
        self.router = router
        self.server = server

    def phases(self) -> list[str]:
        return [e.phase for e in self.events]


def _enroll(cp: _ControlPlane, device_id: str) -> _Device:
    dev = _Device(device_id)
    cp.store.rows[device_id] = {"app_attest_public_key": dev.x963.hex(),
                                "app_attest_counter": 0}
    cp.creds[device_id] = dev.cred
    return dev


async def _wire(*, with_verifier: bool = True) -> _Wired:
    cp = _ControlPlane(H.fake_verifier() if with_verifier else None)
    dev = _enroll(cp, "dev-iphone")
    memory = _Memory()
    events: list = []
    # KEIN Freigabekanal, KEIN Mobile-Pfad — absichtlich. Der Router ist dort
    # fail-closed: verlangte die Matrix eine Freigabe, faellt der Aufruf mit
    # `no_approval_channel`. Ein SUCCESS kann hier also NUR direkt entstehen.
    router = CapabilityRouter(recorder=events.append, principal="test")
    for name, spec in MEMORY_SPECS.items():
        router.register(spec, memory.handler(name))
    for name, spec in FREMDE_SPECS.items():
        router.register(spec, memory.handler(name))
    server = SimpleNamespace(dispatcher=SimpleNamespace(capabilities=router))
    app = web.Application()
    app["control_plane"] = cp
    MME.attach(app, server)
    client = TestClient(TestServer(app))
    await client.start_server()
    return _Wired(client, cp, dev, memory, events, router, server)


async def _teardown(w: _Wired) -> None:
    await w.client.close()


async def _challenge(w: _Wired, *, dev: _Device | None = None) -> tuple[str, str]:
    r = await w.client.get(MME.API + "/challenge",
                           headers=(dev or w.dev).headers())
    require_equal(r.status, 200, "die Challenge wurde nicht ausgestellt")
    body = await r.json()
    return body["nonce"], body["core_instance_id"]


def _assertion_b64(dev: _Device, *, core_id: str, nonce: str, capability: str,
                   arguments: dict, device_id: str | None = None,
                   counter: int = 1, client_data_hash: bytes | None = None) -> str:
    """Was die App vorlegen wuerde. Jeder Parameter einzeln verstellbar — die
    negativen Faelle unterscheiden sich vom guten in genau einem Feld."""
    raw = VSP.canonical_bytes(MME.build_binding(
        core_instance_id=core_id, device_id=device_id or dev.device_id,
        nonce=nonce,
        payload_sha256=MME.payload_sha256(capability, arguments)))
    cdh = MME.client_data_hash(raw) if client_data_hash is None else client_data_hash
    return base64.b64encode(AA.fake_assertion(dev.aakey, cdh, counter)).decode()


async def _post(w: _Wired, *, capability: str, arguments: dict, nonce: str,
                assertion_b64: str, dev: _Device | None = None):
    return await w.client.post(MME.API, json={
        "capability": capability, "arguments": arguments,
        "nonce": nonce, "assertion_b64": assertion_b64},
        headers=(dev or w.dev).headers())


# ----------------------------------------------- 1. der gute Fall ----------

async def t_eine_bewiesene_mutation_laeuft_direkt_und_durch_den_router():
    """Der Weg, den die echte App geht: Challenge, Assertion, direkte Wirkung.

    SUCCESS ohne Freigabekanal beweist die Direktausfuehrung (siehe Kopftext).
    Der Handler muss EXAKT die gesendeten Argumente sehen — der Router, nicht
    der Endpunkt, ist die Stelle, die sie ihm reicht.
    """
    w = await _wire()
    try:
        args = {"memory_id": "mem-0001", "statement": "Gregor trinkt Grüntee"}
        nonce, core_id = await _challenge(w)
        r = await _post(w, capability="memory_forget", arguments=args, nonce=nonce,
                        assertion_b64=_assertion_b64(
                            w.dev, core_id=core_id, nonce=nonce,
                            capability="memory_forget", arguments=args))
        require_equal(r.status, 200, await r.text())
        body = await r.json()
        require_equal(body["ok"], True, "die bewiesene Mutation lief nicht durch")
        require_equal(body["outcome"], "success", body)
        require_equal(w.memory.calls, [("memory_forget", args)],
                      "der Handler sah andere Argumente als die gesendeten")
        require("approval_required" not in w.phases(),
                "die iPhone-Zeile hat trotzdem eine Freigabe verlangt")
        require("started" in w.phases() and "finished" in w.phases(),
                "der Aufruf lief nicht durch den Router-Lebenszyklus")
    finally:
        await _teardown(w)


async def t_die_entscheidung_faellt_in_der_echten_matrix():
    """Der Endpunkt entscheidet nichts — er liefert die bewiesene Herkunft.

    Aufgezeichnet wird, was WIRKLICH beim Router ankommt: die Herkunft ist die
    iPhone-Zeile, die Autoritaet ein Nutzerakt, die Provenienz
    TRUSTED_CONTEXT (die Kennungen stammen aus der Core-Liste, nicht woertlich
    aus einem Turn), und `commanded=True`. Dass genau diese Zelle direkt
    entscheidet, sagt die Matrix selbst — nicht dieser Endpunkt.
    """
    w = await _wire()
    try:
        captured: dict = {}
        orig = w.router.execute

        async def spy(name, arguments=None, **kwargs):
            captured.update(kwargs)
            captured["name"] = name
            return await orig(name, arguments, **kwargs)

        w.server.dispatcher.capabilities = SimpleNamespace(execute=spy)

        args = {"memory_id": "mem-0002", "statement": "alt"}
        nonce, core_id = await _challenge(w)
        r = await _post(w, capability="memory_forget", arguments=args, nonce=nonce,
                        assertion_b64=_assertion_b64(
                            w.dev, core_id=core_id, nonce=nonce,
                            capability="memory_forget", arguments=args))
        require_equal(r.status, 200, await r.text())
        require_equal(captured["origin"], PL.OriginClass.TRUSTED_INTERACTIVE_APP,
                      "die Herkunft ist nicht die bewiesene iPhone-Zeile")
        require_equal(captured["trust"].origin_trust, TrustLevel.USER_DIRECT,
                      "die Autoritaet ist kein Nutzerakt")
        require(captured["trust"].user_authorized,
                "der Tipp in der App galt nicht als autorisiert")
        require_equal(captured["provenance"],
                      {k: ArgumentSource.TRUSTED_CONTEXT for k in args},
                      "die Provenienz ist nicht TRUSTED_CONTEXT je Argument")
        require_equal(captured["principal"], "iphone-dev-iphone-wissen",
                      "der Principal traegt nicht Geraet und Zweck")
        require_equal(captured["commanded"], True, "der Tipp galt nicht als Auftrag")
        # Und die Zelle selbst, beim Namen genannt: NORMAL_WRITE ist in dieser
        # Herkunft direkt. Faellt DAS, ist die Matrix geaendert — nicht dieser
        # Endpunkt.
        require_equal(
            PL.decide(PL.OriginClass.TRUSTED_INTERACTIVE_APP,
                      PL.ActionClass.NORMAL_WRITE).decision,
            PL.Decision.EXECUTE_DIRECTLY,
            "die iPhone-Zeile fuehrt NORMAL_WRITE nicht mehr direkt aus")
    finally:
        await _teardown(w)


async def t_memory_purge_ist_critical_und_laeuft_trotzdem_direkt():
    """Die haerteste erlaubte Zelle, ausdruecklich: CRITICAL x iPhone = direkt.

    `memory_purge` ist als CRITICAL registriert (unwiderruflich). ADR-0022
    fuehrt auch das direkt aus, wenn die App es bewiesen hat — wer das spaeter
    fuer ein Versehen haelt, aendert die Matrix und diese Zusicherung, nicht
    still den Endpunkt.
    """
    require_equal(PL.ACTION_CLASS["memory_purge"], PL.ActionClass.CRITICAL,
                  "memory_purge ist nicht mehr CRITICAL — Aufbau der Suite prüfen")
    w = await _wire()
    try:
        args = {"memory_id": "mem-0003", "statement": "endgueltig weg"}
        nonce, core_id = await _challenge(w)
        r = await _post(w, capability="memory_purge", arguments=args, nonce=nonce,
                        assertion_b64=_assertion_b64(
                            w.dev, core_id=core_id, nonce=nonce,
                            capability="memory_purge", arguments=args))
        require_equal(r.status, 200, await r.text())
        body = await r.json()
        require_equal(body["outcome"], "success", body)
        require_equal(w.memory.calls, [("memory_purge", args)],
                      "der Purge-Handler lief nicht oder mit fremden Argumenten")
        require("approval_required" not in w.phases(),
                "CRITICAL verlangte in der bewiesenen iPhone-Zeile eine Freigabe")
    finally:
        await _teardown(w)


# ------------------------------------------------- 2. Wiedereinspielung ----

async def t_eine_nonce_gilt_genau_einmal():
    """Hier stirbt der Wiedereinspielungsangriff.

    Die mitgeschnittene Assertion ist fuer immer kryptografisch gueltig —
    wertlos wird sie ausschliesslich dadurch, dass ihre Nonce verbraucht ist.
    """
    w = await _wire()
    try:
        args = {"memory_id": "mem-0004", "statement": "x"}
        nonce, core_id = await _challenge(w)
        mitschnitt = _assertion_b64(w.dev, core_id=core_id, nonce=nonce,
                                    capability="memory_forget", arguments=args)
        first = await _post(w, capability="memory_forget", arguments=args,
                            nonce=nonce, assertion_b64=mitschnitt)
        require_equal(first.status, 200, "schon die erste Mutation galt nicht")
        replay = await _post(w, capability="memory_forget", arguments=args,
                             nonce=nonce, assertion_b64=mitschnitt)
        require_equal(replay.status, 401, "der Mitschnitt loeste eine zweite Mutation aus")
        require_equal(len(w.memory.calls), 1,
                      "der Handler lief beim Wiedereinspielen erneut")
    finally:
        await _teardown(w)


async def t_die_nonce_eines_geraets_gilt_nicht_fuer_ein_anderes():
    """Zwei eingeschriebene Telefone teilen ihre Beweise nicht.

    Geraet B ist regulaer eingeschrieben und signiert korrekt mit dem EIGENEN
    Schluessel ueber die EIGENE Bindung — nur die Nonce stammt von A. Genau
    daran muss es scheitern.
    """
    w = await _wire()
    try:
        fremd = _enroll(w.cp, "dev-zweitgeraet")
        args = {"memory_id": "mem-0005", "statement": "x"}
        nonce, core_id = await _challenge(w)   # ausgestellt fuer dev-iphone
        r = await _post(w, capability="memory_forget", arguments=args, nonce=nonce,
                        assertion_b64=_assertion_b64(
                            fremd, core_id=core_id, nonce=nonce,
                            capability="memory_forget", arguments=args),
                        dev=fremd)
        require_equal(r.status, 401, "die Nonce von Geraet A galt fuer Geraet B")
        require_equal(w.memory.calls, [], "der Handler lief fuer ein fremdes Geraet")
        # Und der Fehlversuch von B laesst die Nonce nicht fuer A stehen —
        # dieselbe Regel wie beim Sitzungsbeweis.
        r2 = await _post(w, capability="memory_forget", arguments=args, nonce=nonce,
                         assertion_b64=_assertion_b64(
                             w.dev, core_id=core_id, nonce=nonce,
                             capability="memory_forget", arguments=args))
        require_equal(r2.status, 401,
                      "der Fehlversuch von B liess die Nonce fuer A stehen")
    finally:
        await _teardown(w)


# ----------------------------- 3. veraendert, verbogen, fremde Domaene ----

async def t_ein_nach_dem_signieren_veraendertes_argument_faellt_durch():
    """Die Bindung haengt am SHA-256 der Handlung — nicht an einer Behauptung.

    Signiert wird ueber Argumente X, gesendet werden Argumente Y. Der Core
    rechnet den Payload-Hash aus dem, was ANKAM: die Bindung passt nicht mehr,
    die Assertion faellt, und der Handler sieht Y niemals.
    """
    w = await _wire()
    try:
        signiert = {"memory_id": "mem-0006", "statement": "harmlos"}
        gesendet = {"memory_id": "mem-0666", "statement": "etwas ganz anderes"}
        nonce, core_id = await _challenge(w)
        r = await _post(w, capability="memory_forget", arguments=gesendet,
                        nonce=nonce,
                        assertion_b64=_assertion_b64(
                            w.dev, core_id=core_id, nonce=nonce,
                            capability="memory_forget", arguments=signiert))
        require_equal(r.status, 401, "veraenderte Argumente liefen unter altem Beweis")
        require_equal(w.memory.calls, [], "der Handler sah die veraenderten Argumente")
    finally:
        await _teardown(w)


async def t_eine_verbogene_assertion_faellt_durch():
    """Signatur verbogen, Blob unlesbar, gar kein Base64 — alles ein 401."""
    w = await _wire()
    try:
        args = {"memory_id": "mem-0007", "statement": "x"}
        nonce, core_id = await _challenge(w)
        echt = _assertion_b64(w.dev, core_id=core_id, nonce=nonce,
                              capability="memory_forget", arguments=args)
        verbogen = bytearray(base64.b64decode(echt))
        verbogen[-1] ^= 0xFF
        faelle = {
            "verbogene Signatur": base64.b64encode(bytes(verbogen)).decode(),
            "kein CBOR": base64.b64encode(b"nicht einmal ansatzweise cbor").decode(),
            "kein Base64": "!!!kein-base64!!!",
            "leer": "",
        }
        for name, blob in faelle.items():
            r = await _post(w, capability="memory_forget", arguments=args,
                            nonce=nonce, assertion_b64=blob)
            require_equal(r.status, 401, f"{name} wurde als Mutationsbeweis angenommen")
        require_equal(w.memory.calls, [], "ein unbewiesener Aufruf erreichte den Handler")
    finally:
        await _teardown(w)


async def t_ein_fremder_domain_separator_faellt_durch():
    """Dieselben Bytes, ein anderer Zweck — und die Signatur gilt nicht mehr.

    Ohne den Separator waere jede Sitzungs- oder Entscheidungs-Assertion des
    Geraets potenziell auch ein Mutationsbeweis.
    """
    w = await _wire()
    try:
        args = {"memory_id": "mem-0008", "statement": "x"}
        nonce, core_id = await _challenge(w)
        raw = VSP.canonical_bytes(MME.build_binding(
            core_instance_id=core_id, device_id=w.dev.device_id, nonce=nonce,
            payload_sha256=MME.payload_sha256("memory_forget", args)))
        fremde_hashes = {
            "Sitzungsdomaene": VSP.client_data_hash(raw),
            "Entscheidungsdomaene": AP.decision_client_data_hash(raw),
        }
        for name, cdh in fremde_hashes.items():
            r = await _post(w, capability="memory_forget", arguments=args,
                            nonce=nonce,
                            assertion_b64=_assertion_b64(
                                w.dev, core_id=core_id, nonce=nonce,
                                capability="memory_forget", arguments=args,
                                client_data_hash=cdh))
            require_equal(r.status, 401,
                          f"eine Assertion aus der {name} galt als Mutationsbeweis")
            nonce, core_id = await _challenge(w)
        require_equal(w.memory.calls, [], "ein fremd gebundener Beweis wirkte")
        require(MME.DOMAIN_MEMORY_MUTATION not in (
            VSP.DOMAIN_VOICE_SESSION, AP.DOMAIN_DECISION, AP.DOMAIN_ENROLLMENT),
            "der Mutations-Separator ist einer der bestehenden")
    finally:
        await _teardown(w)


# ------------------------------------------ 4. die Liste ist geschlossen ----

async def t_die_liste_ist_geschlossen_auch_fuer_bekannte_faehigkeiten():
    """DER STRUKTURPUNKT: kein generischer Capability-RPC.

    `ha_turn_on` und `gmail_send_draft` sind im Router REGISTRIERT — die
    Ablehnung darf also nicht aus dessen Unkenntnis kommen, sondern muss die
    geschlossene Liste des Endpunkts sein. Und sie faellt VOR jeder
    Kryptografie: auch ein formal perfekter Beweis oeffnet sie nicht.
    """
    w = await _wire()
    try:
        for capability in ("ha_turn_on", "gmail_send_draft", "memory_remember",
                           "voellig_unbekannt"):
            args = {"memory_id": "mem-0009", "statement": "x"}
            nonce, core_id = await _challenge(w)
            r = await _post(w, capability=capability, arguments=args, nonce=nonce,
                            assertion_b64=_assertion_b64(
                                w.dev, core_id=core_id, nonce=nonce,
                                capability=capability, arguments=args))
            require_equal(r.status, 403,
                          f"{capability} kam am geschlossenen Endpunkt vorbei")
            require_equal((await r.json())["error"], "capability_not_allowed",
                          "die Ablehnung traegt nicht den stabilen Grund")
        require_equal(w.memory.calls, [],
                      "eine Faehigkeit ausserhalb der Liste erreichte einen Handler")
        require_equal(w.phases(), [],
                      "eine Faehigkeit ausserhalb der Liste erreichte den Router")
    finally:
        await _teardown(w)


def t_die_liste_ist_exakt_die_fuenf_wissens_mutationen():
    """Festgenagelt als Menge — eine sechste Zeile soll diese Zusicherung
    brechen und damit ein Review kosten, keinen stillen Drift erlauben."""
    require_equal(MME.ALLOWED_CAPABILITIES,
                  frozenset({"memory_forget", "memory_correct",
                             "memory_confirm_candidate",
                             "memory_decline_candidate", "memory_purge"}),
                  "die geschlossene Liste hat sich veraendert")
    for name in MME.ALLOWED_CAPABILITIES:
        require(name in MEMORY_SPECS,
                f"{name} ist keine registrierte Memory-Faehigkeit")


# ------------------------------------------------------- 5. fail-closed ----

async def t_ohne_transportkennung_gibt_es_weder_challenge_noch_mutation():
    """Die statische Kennung ist die Eintrittskarte fuer den TRANSPORT —
    ohne sie gibt es nicht einmal eine Nonce."""
    w = await _wire()
    try:
        r = await w.client.get(MME.API + "/challenge")
        require_equal(r.status, 401, "eine Challenge ohne Geraetepruefung")
        r2 = await w.client.post(MME.API, json={
            "capability": "memory_forget", "arguments": {}, "nonce": "n",
            "assertion_b64": ""})
        require_equal(r2.status, 401, "eine Mutation ohne Geraetepruefung")
        falsch = {"X-Device-Id": w.dev.device_id, "X-Transport-Cred": "falsch"}
        r3 = await w.client.get(MME.API + "/challenge", headers=falsch)
        require_equal(r3.status, 401, "eine falsche Transportkennung galt")
    finally:
        await _teardown(w)


async def t_ohne_verifizierer_oder_geraet_gibt_es_401_und_nie_500():
    """Ein halb aufgebauter Core gibt keinen Schreibweg frei — und faellt
    nicht um. Fehlender Verifizierer, Geraet ohne Zeile, kaputter Speicher:
    alles ein 401, nie eine Ausnahme, die zum 500 wuerde."""
    # Kein Verifizierer.
    w = await _wire(with_verifier=False)
    try:
        args = {"memory_id": "mem-0010", "statement": "x"}
        nonce, core_id = await _challenge(w)
        r = await _post(w, capability="memory_forget", arguments=args, nonce=nonce,
                        assertion_b64=_assertion_b64(
                            w.dev, core_id=core_id, nonce=nonce,
                            capability="memory_forget", arguments=args))
        require_equal(r.status, 401, "ohne Verifizierer wurde ein Beweis angenommen")
        require_equal(w.memory.calls, [], "ohne Verifizierer lief ein Handler")
    finally:
        await _teardown(w)

    # Transport gilt, aber es gibt keine Geraetezeile (abgebrochenes Pairing).
    w = await _wire()
    try:
        geist = _Device("dev-geist")
        w.cp.creds[geist.device_id] = geist.cred
        nonce, core_id = await _challenge(w, dev=geist)
        args = {"memory_id": "mem-0011", "statement": "x"}
        r = await _post(w, capability="memory_forget", arguments=args, nonce=nonce,
                        assertion_b64=_assertion_b64(
                            geist, core_id=core_id, nonce=nonce,
                            capability="memory_forget", arguments=args),
                        dev=geist)
        require_equal(r.status, 401, "ein Geraet ohne Zeile bewies eine Mutation")
    finally:
        await _teardown(w)

    # Kaputter Speicher: 401, kein 500.
    w = await _wire()
    try:
        args = {"memory_id": "mem-0012", "statement": "x"}
        nonce, core_id = await _challenge(w)
        assertion = _assertion_b64(w.dev, core_id=core_id, nonce=nonce,
                                   capability="memory_forget", arguments=args)
        w.cp.store = _BrokenStore()
        r = await _post(w, capability="memory_forget", arguments=args,
                        nonce=nonce, assertion_b64=assertion)
        require_equal(r.status, 401, "ein kaputter Speicher wurde nicht zur Absage")
        require_equal(w.memory.calls, [], "ein kaputter Speicher liess einen Handler laufen")
    finally:
        await _teardown(w)


async def t_der_zaehler_des_entscheidungspfads_wird_nicht_bewegt():
    """Der App-Attest-Zaehler gehoert dem eingefrorenen Entscheidungspfad.

    Zwei Mutationen mit demselben Zaehlerstand sind hier in Ordnung — gegen
    Wiedereinspielung schuetzt die Nonce, nicht die Spalte. Wer das spaeter
    „repariert", greift in eine fremde Transaktion (dieselbe Aufgabenteilung
    wie beim Sitzungsbeweis).
    """
    w = await _wire()
    try:
        for i in range(2):
            args = {"memory_id": f"mem-01{i}", "statement": "x"}
            nonce, core_id = await _challenge(w)
            r = await _post(w, capability="memory_forget", arguments=args,
                            nonce=nonce,
                            assertion_b64=_assertion_b64(
                                w.dev, core_id=core_id, nonce=nonce,
                                capability="memory_forget", arguments=args,
                                counter=1))
            require_equal(r.status, 200, f"Mutation {i + 1} lief nicht")
        require_equal(w.cp.store.rows[w.dev.device_id]["app_attest_counter"], 0,
                      "der Schreibweg hat den Zaehler des Entscheidungspfads bewegt")
    finally:
        await _teardown(w)


# ------------------------------------------------------ 6. goldener Vektor -

def t_der_goldene_vektor_stimmt_mit_der_implementierung_ueberein():
    """Der Vertrag mit der iOS-Seite, byteweise.

    Beide Sprachen muessen dieselben kanonischen Bytes und denselben
    clientDataHash erzeugen — sonst faellt jede Mutation still mit 401, und
    niemand sieht, dass es an der Kanonisierung lag und nicht an der
    Kryptografie. Der Umlaut im Statement pinnt `ensure_ascii=False` + UTF-8.
    """
    with open(VEC, encoding="utf-8") as f:
        v = json.load(f)["memory_mutation"]
    require_equal(v["domain"], MME.DOMAIN_MEMORY_MUTATION.decode(),
                  "der Vektor traegt eine fremde Domaene")
    payload = v["payload"]
    digest = MME.payload_sha256(payload["capability"], payload["arguments"])
    require_equal(digest, v["binding"]["payload_sha256"],
                  "der Payload-Hash der Implementierung weicht vom Vektor ab")
    binding = MME.build_binding(
        core_instance_id=v["binding"]["core_instance_id"],
        device_id=v["binding"]["device_id"], nonce=v["binding"]["nonce"],
        payload_sha256=digest)
    require_equal(binding, v["binding"],
                  "die Bindung enthaelt andere Felder als der Vektor")
    raw = VSP.canonical_bytes(binding)
    require_equal(raw.hex(), v["canonical_bytes_hex"],
                  "die kanonischen Bytes weichen vom Vektor ab")
    require_equal(MME.client_data_hash(raw).hex(), v["client_data_hash_hex"],
                  "der clientDataHash weicht vom Vektor ab")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
