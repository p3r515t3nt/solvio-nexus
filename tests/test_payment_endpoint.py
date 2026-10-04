"""Der Zahlungsweg des iPhones — und die zwei Runden, die eine Zahlung braucht.

Diese Suite geht den Weg GANZ: Challenge, Assertion, geschlossene Liste, Nonce,
Router, Matrix, Freigabe, Face ID, zweite Runde, Wirkung. Sie existiert, weil
die kalte Abnahme genau hier einen Fehler fand, den keine Einzelpruefung sehen
konnte: `payment_method_add` konnte NIE gelingen. Der Endpunkt lagerte den
Anbieter-Token unter der Geraetekennung ein, der Handler holte ihn mit einem
Leerstring ab — und der Mensch bekam nach bestandener Face-ID-Runde
„Die Angabe ist abgelaufen."

Beide Enden waren fuer sich richtig. Nur zusammen waren sie falsch. Deshalb
prueft diese Datei den ZUSAMMENHANG und nicht die Teile.

HERMETISCH: in-process aiohttp TestClient, Fake-Kontrollebene mit echten
ECDSA-Signaturen, ECHTE Zahlungs-Specs, ECHTER Router, ECHTER Pruefanbieter auf
der Rueckschleife. Kein Netz nach draussen, kein TLS, kein echtes Telefon.

ASSERTION POLICY: `require*` aus `tests/_guard.py` sind Funktionsaufrufe und
ueberleben `python -O`.

Direkt: python tests/test_payment_endpoint.py
"""
from __future__ import annotations

import base64
import hashlib
import os
import secrets
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, os.path.dirname(__file__))

from _guard import enforce_assertions  # noqa: E402

enforce_assertions()

from _guard import require, require_equal  # noqa: E402

import mobile_attest_helper as AH                                     # noqa: E402
import payment_harness as H                                           # noqa: E402
from aiohttp import web                                               # noqa: E402
from aiohttp.test_utils import TestClient, TestServer                 # noqa: E402

from solvio import voice_session_proof as VSP                         # noqa: E402
from solvio.capabilities.approval_gateway import CapabilityApprovals  # noqa: E402
from solvio.capabilities.payment import register as register_payment  # noqa: E402
from solvio.capabilities.router import CapabilityRouter               # noqa: E402
from solvio.payment import endpoint as PE                             # noqa: E402
from solvio.payment.intent import PaymentState                        # noqa: E402
from solvio.security.mobile_approval import app_attest as AA          # noqa: E402


class _ControlPlane(H.FakeControlPlane):
    """Der Papier-Freigabepfad PLUS die Felder, die der Endpunkt liest.

    Der Geraetestand haengt am SELBEN `store` wie die Freigabeanfragen — genau
    so, wie es der echte Kontrollpfad tut. Ein zweites Objekt daneben waere
    bequemer und wuerde am Ziel vorbeipruefen: der Endpunkt liest
    `control_plane.store.get_device`, und das muss er hier auch.
    """

    def __init__(self, verifier) -> None:
        super().__init__()
        self.attest_verifier = verifier
        self.core_instance_id = "core-test"
        self.creds: dict[str, str] = {}
        self.store.devices = {}

        async def get_device(device_id):
            return self.store.devices.get(device_id)

        self.store.get_device = get_device

    async def verify_transport_cred(self, device_id, cred):
        expected = self.creds.get(device_id, "")
        return bool(expected) and secrets.compare_digest(expected, cred or "")


class _Device:
    def __init__(self, device_id: str = "dev-iphone") -> None:
        self.device_id = device_id
        self.aakey, self.x963, self.aakid = AH.aa_key()
        self.cred = "cred-" + device_id

    def headers(self) -> dict[str, str]:
        return {"X-Device-Id": self.device_id, "X-Transport-Cred": self.cred}


class _Wired:
    def __init__(self, client, cp, dev, rig, router) -> None:
        self.client = client
        self.cp = cp
        self.dev = dev
        self.rig = rig
        self.router = router


async def _wire(*, with_verifier: bool = True) -> _Wired:
    rig = await H.Rig().start()
    cp = _ControlPlane(AH.fake_verifier() if with_verifier else None)
    dev = _Device()
    # Der Geraetestand, wie `verify_mutation_proof` ihn liest — UND wie
    # `_owner_device` ihn liest.
    cp.store.devices[dev.device_id] = {"app_attest_public_key": dev.x963.hex(),
                                       "app_attest_counter": 0}
    cp.creds[dev.device_id] = dev.cred
    coordinator = H.FakeCoordinator(cp)
    approvals = CapabilityApprovals(coordinator, owner_principal="local-owner")
    router = CapabilityRouter(mobile=approvals, principal="test")
    register_payment(router, rig.caps)
    server = SimpleNamespace(dispatcher=SimpleNamespace(capabilities=router))
    app = web.Application()
    app["control_plane"] = cp
    # `_owner_device` liest den Geraetestand ueber die Kontrollebene; hier wird
    # dieselbe Pruefung nachgebaut, damit der Endpunkt seinen echten Weg geht.
    PE.attach(app, server, rig.caps)

    async def _owner(request):
        device_id = request.headers.get("X-Device-Id", "")
        cred = request.headers.get("X-Transport-Cred", "")
        plane = request.app.get("control_plane")
        if plane is None or not device_id:
            return None
        return device_id if await plane.verify_transport_cred(device_id, cred) else None

    PE._authed = _owner  # noqa: SLF001 - der eine Ersatz, den eine Suite braucht
    client = TestClient(TestServer(app))
    await client.start_server()
    return _Wired(client, cp, dev, rig, router)


async def _teardown(w: _Wired) -> None:
    await w.client.close()
    await w.rig.stop()


async def _challenge(w: _Wired) -> tuple[str, str]:
    r = await w.client.get(PE.API + "/mutation/challenge", headers=w.dev.headers())
    require_equal(r.status, 200, "die Challenge wurde nicht ausgestellt")
    body = await r.json()
    return body["nonce"], body["core_instance_id"]


def _assertion(dev: _Device, *, core_id: str, nonce: str, capability: str,
               arguments: dict, token_sha256: str = "", counter: int = 1) -> str:
    raw = VSP.canonical_bytes(PE.build_binding(
        core_instance_id=core_id, device_id=dev.device_id, nonce=nonce,
        payload_sha256=PE.payload_sha256(capability, arguments, token_sha256)))
    return base64.b64encode(
        AA.fake_assertion(dev.aakey, PE.client_data_hash(raw), counter)).decode()


async def _mutate(w: _Wired, *, capability: str, arguments: dict,
                  token: str = "", staging_id: str = "", counter: int = 1):
    nonce, core_id = await _challenge(w)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest() if token else ""
    if staging_id and not digest:
        digest = w.last_token_sha256
    body = {"capability": capability, "arguments": arguments, "nonce": nonce,
            "assertion_b64": _assertion(w.dev, core_id=core_id, nonce=nonce,
                                        capability=capability,
                                        arguments=arguments,
                                        token_sha256=digest, counter=counter)}
    if digest:
        body["token_sha256"] = digest
    if token:
        body["token_b64"] = base64.b64encode(token.encode("utf-8")).decode()
    elif staging_id:
        body["staging_id"] = staging_id
    return await w.client.post(PE.API + "/mutation", json=body,
                               headers=w.dev.headers())


# =============================================== der ganze Weg, zwei Runden ==
def t_ein_zahlungsmittel_wird_ueber_zwei_runden_wirklich_hinterlegt():
    """Der Fall, den die kalte Abnahme gefunden hat.

    Erste Runde: der Token reist EINMAL, wird versiegelt eingelagert, und die
    Matrix verlangt Face ID. Zweite Runde: dieselbe Handlung ohne den Token,
    nur mit der Einlagerungskennung — und JETZT muss das Zahlungsmittel
    wirklich dastehen.

    Der erste Bau scheiterte hier zu hundert Prozent, weil die Einlagerung an
    das GERAET gebunden ist und der Handler kein Geraet kannte.
    """
    async def case():
        w = await _wire()
        try:
            args = {"verweis": "payment://shopping/zweite",
                    "art": "virtual_card", "anbieter": "sandbox",
                    "name": "SOLVIO Zweitkarte", "waehrungen": "EUR",
                    "haendler": H.MERCHANT, "grenze_einzeln": "5000",
                    "grenze_taeglich": "10000", "zugang": H.CHARGE_REF,
                    "lesezugang": H.READ_REF}
            r = await _mutate(w, capability="payment_method_add", arguments=args,
                              token="pm_sandbox_solvio_shopping")
            require_equal(r.status, 200, await r.text())
            first = await r.json()
            require_equal(first["outcome"], "approval_required",
                          f"eine VERY_CRITICAL-Handlung lief ohne Face ID: {first}")
            staging_id = first.get("staging_id", "")
            require(staging_id, "die Einlagerungskennung kam nicht zurueck")
            require(w.rig.store.instrument("payment://shopping/zweite") is None,
                    "das Zahlungsmittel entstand VOR der Freigabe")

            # Der Mensch bestaetigt.
            w.cp.approve(w.cp.store.requests[list(w.cp.store.requests)[0]]
                         ["approval_id"])

            w.last_token_sha256 = hashlib.sha256(
                b"pm_sandbox_solvio_shopping").hexdigest()
            r2 = await _mutate(w, capability="payment_method_add", arguments=args,
                               staging_id=staging_id, counter=2)
            require_equal(r2.status, 200, await r2.text())
            second = await r2.json()
            require_equal(second["ok"], True,
                          f"die freigegebene Handlung lief nicht: {second}")
            entstanden = w.rig.store.instrument("payment://shopping/zweite")
            require(entstanden is not None,
                    "nach der Freigabe steht kein Zahlungsmittel da")
            require_equal(entstanden.provider_token, "pm_sandbox_solvio_shopping",
                          "der eingelagerte Token kam nicht an")
            require_equal(entstanden.display_name, "SOLVIO Zweitkarte")
        finally:
            await _teardown(w)
    H.run(case())


def t_eine_fremde_einlagerung_wird_nicht_eingeloest():
    """Die Kennung allein reicht nicht — sie gehoert einem Geraet."""
    async def case():
        w = await _wire()
        try:
            w.rig.caps.device_for_staging["st-fremd"] = "dev-jemand-anders"
            raised = None
            try:
                await w.rig.caps.method_add({
                    "verweis": "payment://shopping/fremd", "art": "virtual_card",
                    "anbieter": "sandbox", "waehrungen": "EUR",
                    "haendler": H.MERCHANT, "grenze_einzeln": "1000",
                    "vorgang": "st-fremd"})
            except Exception as exc:  # noqa: BLE001
                raised = exc
            require(raised is not None, "eine fremde Einlagerung wurde eingeloest")
            require(w.rig.store.instrument("payment://shopping/fremd") is None)
        finally:
            await _teardown(w)
    H.run(case())


# ================================================= die geschlossene Liste ===
def t_eine_verbotene_faehigkeit_faellt_vor_jeder_kryptografie():
    """403 VOR der Nonce: eine strukturell verbotene Handlung soll keinen
    Beweis kosten und keinen verbrauchen."""
    async def case():
        w = await _wire()
        try:
            nonce, core_id = await _challenge(w)
            r = await w.client.post(PE.API + "/mutation", json={
                "capability": "payment_intent_prepare", "arguments": {},
                "nonce": nonce,
                "assertion_b64": _assertion(w.dev, core_id=core_id, nonce=nonce,
                                            capability="payment_intent_prepare",
                                            arguments={})},
                headers=w.dev.headers())
            require_equal(r.status, 403, await r.text())
            require_equal((await r.json())["error"], "capability_not_allowed")
            # Die Nonce ist NICHT verbraucht: sie traegt jetzt noch eine
            # erlaubte Handlung.
            r2 = await w.client.post(PE.API + "/mutation", json={
                "capability": "payment_method_disable",
                "arguments": {"verweis": H.METHOD}, "nonce": nonce,
                "assertion_b64": _assertion(
                    w.dev, core_id=core_id, nonce=nonce,
                    capability="payment_method_disable",
                    arguments={"verweis": H.METHOD})},
                headers=w.dev.headers())
            require(r2.status != 401,
                    "die verbotene Handlung hat die Nonce mit verbrannt")
        finally:
            await _teardown(w)
    H.run(case())


def t_eine_nonce_zaehlt_genau_einmal():
    async def case():
        w = await _wire()
        try:
            nonce, core_id = await _challenge(w)
            args = {"verweis": H.METHOD}
            for lauf in (1, 2):
                r = await w.client.post(PE.API + "/mutation", json={
                    "capability": "payment_method_disable", "arguments": args,
                    "nonce": nonce,
                    "assertion_b64": _assertion(
                        w.dev, core_id=core_id, nonce=nonce,
                        capability="payment_method_disable", arguments=args,
                        counter=lauf)},
                    headers=w.dev.headers())
                if lauf == 1:
                    require(r.status == 200, await r.text())
                else:
                    require_equal(r.status, 401,
                                  "dieselbe Nonce trug eine zweite Handlung")
        finally:
            await _teardown(w)
    H.run(case())


def t_veraenderte_argumente_nach_dem_signieren_fallen_durch():
    async def case():
        w = await _wire()
        try:
            nonce, core_id = await _challenge(w)
            echt = {"verweis": H.METHOD}
            gefaelscht = {"verweis": "payment://shopping/anderes"}
            r = await w.client.post(PE.API + "/mutation", json={
                "capability": "payment_method_disable", "arguments": gefaelscht,
                "nonce": nonce,
                "assertion_b64": _assertion(
                    w.dev, core_id=core_id, nonce=nonce,
                    capability="payment_method_disable", arguments=echt)},
                headers=w.dev.headers())
            require_equal(r.status, 401, await r.text())
        finally:
            await _teardown(w)
    H.run(case())


def t_ein_fremder_domaenentrenner_traegt_keine_zahlung():
    """Eine Tresor-Assertion ueber dieselben Bytes ist hier wertlos."""
    async def case():
        from solvio.secret_vault import endpoint as VE
        w = await _wire()
        try:
            nonce, core_id = await _challenge(w)
            args = {"verweis": H.METHOD}
            raw = VSP.canonical_bytes(PE.build_binding(
                core_instance_id=core_id, device_id=w.dev.device_id, nonce=nonce,
                payload_sha256=PE.payload_sha256("payment_method_disable", args)))
            fremd = base64.b64encode(AA.fake_assertion(
                w.dev.aakey, VE.client_data_hash(raw), 1)).decode()
            r = await w.client.post(PE.API + "/mutation", json={
                "capability": "payment_method_disable", "arguments": args,
                "nonce": nonce, "assertion_b64": fremd},
                headers=w.dev.headers())
            require_equal(r.status, 401, await r.text())
        finally:
            await _teardown(w)
    H.run(case())


def t_ohne_verifizierer_gibt_es_ein_401_und_keinen_absturz():
    async def case():
        w = await _wire(with_verifier=False)
        try:
            args = {"verweis": H.METHOD}
            r = await _mutate(w, capability="payment_method_disable",
                              arguments=args)
            require_equal(r.status, 401, await r.text())
        finally:
            await _teardown(w)
    H.run(case())


def t_der_app_attest_zaehler_bleibt_unberuehrt():
    """Die Spalte gehoert dem eingefrorenen Entscheidungspfad. Hier schuetzt
    die einmalige Nonce, nicht der Zaehler."""
    async def case():
        w = await _wire()
        try:
            vorher = w.cp.store.devices[w.dev.device_id]["app_attest_counter"]
            await _mutate(w, capability="payment_method_disable",
                          arguments={"verweis": H.METHOD}, counter=7)
            nachher = w.cp.store.devices[w.dev.device_id]["app_attest_counter"]
            require_equal(nachher, vorher, "der Zaehler wurde fortgeschrieben")
        finally:
            await _teardown(w)
    H.run(case())


# ============================================================ die Auskunft ==
def t_die_listen_tragen_immer_dieselben_schluessel():
    """Ein einziger Vorgang ohne Betrag darf die ganze Liste nicht umbringen.

    Die App liest ein Feld-Array; fehlt EIN Schluessel in EINEM Element,
    scheitert die Umwandlung fuer ALLE — auch fuer die Vorgaenge, die ein
    Mensch aufloesen muesste. Gefunden in der kalten Abnahme.
    """
    async def case():
        w = await _wire()
        try:
            # Ein bewerteter Vorgang …
            gut = await H.prepare_intent(w.rig)
            # … und ein Entwurf, der nie bewertet wurde (Anbieter weg).
            await w.rig.scenario("unreachable")
            try:
                await H.prepare_intent(w.rig)
            except Exception:  # noqa: BLE001 - genau das soll passieren
                pass
            await w.rig.scenario("normal")

            r = await w.client.get(PE.API + "/intents", headers=w.dev.headers())
            require_equal(r.status, 200, await r.text())
            vorgaenge = (await r.json())["vorgaenge"]
            require(len(vorgaenge) >= 2, f"zu wenige Vorgaenge: {vorgaenge}")
            schluessel = {frozenset(v) for v in vorgaenge}
            require_equal(len(schluessel), 1,
                          f"die Vorgaenge tragen verschiedene Schluessel: {schluessel}")
            for v in vorgaenge:
                require("betrag_lesbar" in v and "pruefsumme" in v)
            require(any(v["payment_intent_id"] == gut["payment_intent_id"]
                        for v in vorgaenge))
        finally:
            await _teardown(w)
    H.run(case())


def t_ein_abgelaufener_vorgang_steht_nicht_mehr_als_offen():
    """Ein toter Kauf verschwindet aus der Liste — ein gefaehrlicher NIE.

    **Gefunden in der Live-Abnahme von DEBT-0126 am 2026-08-30.** `offen` hing
    allein am Zustand; die Frist wurde nicht angesehen. Ein `READY_FOR_APPROVAL`
    vom 27. August stand deshalb drei Tage spaeter noch als offene Handlung in
    der App, liess sich antippen und erzeugte vier Freigabekarten hintereinander.

    Die Gegenprobe steht ausdruecklich mit im selben Fall: `EXECUTING` bleibt
    offen, auch weit nach der Frist. Ein Vorgang, der zwischen Anspruch und
    Antwort stehen blieb, ist der gefaehrlichste Zustand ueberhaupt — die
    Belastung KANN stattgefunden haben. Ihn nach Fristablauf auszublenden waere
    derselbe Fehler wie in der kalten Abnahme, nur teurer: dann verschwaende
    eine moegliche Zahlung aus dem Blick eines Menschen.

    **Weitergestellt wird die Uhr, nicht die Frist.** `store.put_intent` nimmt
    `expires_at` bewusst NICHT in seine `DO UPDATE`-Liste auf — eine Absicht ist
    ab Anlage unveraenderlich, und das ist richtig so. Ein Fall, der die Frist
    zurueckdatieren wollte, wuerde hier still nichts tun und gruen bleiben.
    """
    async def case():
        w = await _wire()
        echte_zeit = PE.time
        try:
            frisch = await H.prepare_intent(w.rig)
            r = await w.client.get(PE.API + "/intents", headers=w.dev.headers())
            require_equal(r.status, 200, await r.text())
            zeilen = {v["payment_intent_id"]: v
                      for v in (await r.json())["vorgaenge"]}
            require_equal(zeilen[frisch["payment_intent_id"]]["offen"], True,
                          "ein gueltiger Vorgang gilt nicht als offen")

            # Genau die Lage vom 27. August: der Vorgang steht noch da, die
            # Frist ist laengst vorbei.
            jetzt = echte_zeit.time()
            PE.time = SimpleNamespace(time=lambda: jetzt + 481.0)   # INTENT_TTL + 1 s

            r = await w.client.get(PE.API + "/intents", headers=w.dev.headers())
            zeilen = {v["payment_intent_id"]: v
                      for v in (await r.json())["vorgaenge"]}
            require_equal(zeilen[frisch["payment_intent_id"]]["offen"], False,
                          "ein abgelaufener Vorgang wird weiter als offen "
                          "angeboten — und kostet einen Menschen Face ID")
            # Er verschwindet nicht aus der Liste: der Verlauf bleibt lesbar.
            require(frisch["payment_intent_id"] in zeilen,
                    "der abgelaufene Vorgang ist ganz aus der Auskunft gefallen")

            # GEGENPROBE: EXECUTING bleibt offen, Frist hin oder her.
            import dataclasses
            gespeichert = w.rig.store.intent(frisch["payment_intent_id"])
            w.rig.store.put_intent(dataclasses.replace(
                gespeichert, state=PaymentState.EXECUTING))
            r = await w.client.get(PE.API + "/intents", headers=w.dev.headers())
            zeilen = {v["payment_intent_id"]: v
                      for v in (await r.json())["vorgaenge"]}
            require_equal(zeilen[frisch["payment_intent_id"]]["offen"], True,
                          "ein EXECUTING-Vorgang ist nach Fristablauf aus der "
                          "Liste gefallen — eine moegliche Belastung, die "
                          "niemand mehr sieht")
        finally:
            PE.time = echte_zeit
            await _teardown(w)
    H.run(case())


def t_das_zahlungsbuch_nennt_seine_zeilennummer():
    """Ohne `id` liest die App den Verlauf nicht — und zeigt still einen leeren."""
    async def case():
        w = await _wire()
        try:
            view = await H.prepare_intent(w.rig)
            await H.pay(w.rig, view)
            r = await w.client.get(PE.API + "/ledger", headers=w.dev.headers())
            require_equal(r.status, 200, await r.text())
            zeilen = (await r.json())["eintraege"]
            require(zeilen, "das Zahlungsbuch kam leer zurueck")
            for zeile in zeilen:
                require("id" in zeile, f"eine Zeile ohne Nummer: {sorted(zeile)}")
                require(isinstance(zeile["id"], int))
        finally:
            await _teardown(w)
    H.run(case())


def t_die_auskunft_traegt_kein_zahlungsmaterial():
    async def case():
        from solvio.payment.firewall import is_payment_material
        w = await _wire()
        try:
            view = await H.prepare_intent(w.rig)
            await H.pay(w.rig, view)
            for pfad in ("/methods", "/intents", "/ledger"):
                r = await w.client.get(PE.API + pfad, headers=w.dev.headers())
                text = await r.text()
                require(not is_payment_material(text), f"{pfad} traegt Zahlungsmaterial")
                for wort in ("secret://", "pm_sandbox", w.rig.provider_secret,
                             w.rig.refund_secret, w.rig.read_secret):
                    require(wort not in text, f"{pfad} nennt {wort[:16]}")
        finally:
            await _teardown(w)
    H.run(case())


def t_ohne_geraet_gibt_es_gar_nichts():
    async def case():
        w = await _wire()
        try:
            for pfad in ("/methods", "/intents", "/ledger", "/mutation/challenge"):
                r = await w.client.get(PE.API + pfad)
                require_equal(r.status, 401, f"{pfad} antwortete ohne Geraet")
        finally:
            await _teardown(w)
    H.run(case())


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
