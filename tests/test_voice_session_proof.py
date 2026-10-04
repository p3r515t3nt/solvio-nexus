"""Der Sitzungsbeweis des iPhones — und warum die iPhone-Zeile ohne ihn nicht gilt.

Approval Policy V2 gibt dem iPhone eine Matrixzeile, die Freigabepflichten
REDUZIERT — bis hin zur Haustuer. Die Kennung, mit der sich das Telefon meldet,
ist aber ein STATISCHES Bearer-Geheimnis: gespeichert wird nur ihr Hash, und
jeder Code, der sie besitzt, sieht am Transport identisch aus. Waere sie allein
der Eintrittsschein in diese Zeile, dann waere ein Backup-Auszug oder ein
Keychain-Leck der direkte Weg zum Schloss — ohne Face ID, ohne Geraet, ohne
Menschen.

Genau das prueft diese Datei. Die reduzierte Zeile haengt an einer frischen
App-Attest-Assertion ueber eine Nonce, die der Core selbst ausgegeben hat. Der
Kern der Suite ist deshalb nicht der gluecklich verlaufende Fall, sondern die
Gegenrichtung: OHNE gueltigen Beweis muss dieselbe Sitzung auf die Raum-Zeile
fallen — also auf exakt das Face-ID-Verhalten von V1.

Zwei Verwechslungen werden hier ebenfalls festgenagelt, weil sie teuer waeren:

* Der Beweis ist NICHT Face ID. Er zeigt eine attestierte App-Instanz auf einem
  eingeschriebenen Geraet, keine Person. VERY_CRITICAL bleibt deshalb auch mit
  ihm biometrisch.
* Eine Sitzungs-Assertion ist NICHT eine Entscheidungs-Assertion. Beide sind
  Signaturen desselben Schluessels; nur der Domain-Separator trennt sie.

Fail-DOWN statt fail-closed: keine Absage darf ein Gespraech kippen. Jeder
negative Fall unten prueft deshalb ein `False`, nie eine Ausnahme.

HERMETISCH: echter Freigabe-Kontrollpfad auf einer SQLite-Datei im Temp-Ordner,
Software-P-256-Schluessel und `FakeAppAttestVerifier` statt Apple. Kein Netz,
kein Home Assistant, kein echtes Telefon.

ASSERTION POLICY: `require*` aus `tests/_guard.py` sind Funktionsaufrufe und
ueberleben `python -O`.

Direkt: python -m tests.test_voice_session_proof
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from _guard import enforce_assertions, require, require_equal  # noqa: E402

enforce_assertions()

import mobile_attest_helper as H  # noqa: E402
from solvio import voice_session_proof as VSP  # noqa: E402
from solvio.capabilities import policy as PL  # noqa: E402
from solvio.security.mobile_approval import app_attest as AA  # noqa: E402
from solvio.security.mobile_approval import attest_protocol as AP  # noqa: E402
from solvio.security.mobile_approval import control as C  # noqa: E402
from solvio.security.mobile_approval import identity  # noqa: E402
from solvio.security.mobile_approval import protocol as P  # noqa: E402
from solvio.security.mobile_approval import store as S  # noqa: E402

APP_ID = "WQ8CG7R53R.de.solvio.approvals"
DB = "approval_control.sqlite3"

#: Was aus dem Raum bewusst bequem bleiben SOLL. Lesen ist ueberall direkt, und
#: gewoehnliche Haustechnik hat der Nutzer ausdruecklich freigegeben — „mach das
#: Licht aus" soll das Raummikrofon nicht zur Face-ID-Runde machen.
BEQUEM_AUS_DEM_RAUM = (PL.ActionClass.READ_ONLY, PL.ActionClass.HA_NORMAL)

#: Alles, was der Rueckfall wieder biometrisch machen muss. HA_SECURITY steht
#: hier: die Haustuer ist genau die Zelle, die die iPhone-Zeile oeffnet.
FOLGENREICH = tuple(cls for cls in PL.ActionClass
                    if cls not in BEQUEM_AUS_DEM_RAUM)


# ---------------------------------------------------------------- Aufbau ----

class _Wired:
    """Ein echter Kontrollpfad mit einem attestierten Geraet darauf."""

    def __init__(self, folder, store, control_plane, ctx) -> None:
        self.folder = folder
        self.store = store
        self.cp = control_plane
        self.ctx = ctx

    @property
    def core_id(self) -> str:
        return self.cp.core_instance_id

    @property
    def device_id(self) -> str:
        return self.ctx.device_id


async def _wire(*, device_id: str = "dev-iphone") -> _Wired:
    folder = tempfile.mkdtemp(prefix="solvio-session-proof-")
    store = S.ApprovalControlStore(os.path.join(folder, DB))
    await store.open()
    cp = C.MobileApprovalControlPlane(
        store, identity.MacSigningKey.load_or_create(folder),
        identity.load_or_create_core_instance_id(folder),
        attest_verifier=H.fake_verifier(), app_id=APP_ID,
        allowed_environments={AA.ENV_DEVELOPMENT})
    ctx = await H.enroll_attested(cp, device_id=device_id)
    return _Wired(folder, store, cp, ctx)


async def _teardown(wired: _Wired) -> None:
    await wired.store.close()
    shutil.rmtree(wired.folder, ignore_errors=True)


async def _enrolled_but_unattested(cp, device_id: str = "dev-halbfertig"):
    """Ein Geraet, das die Einschreibung begonnen und nie attestiert hat.

    Es ist als Zeile vorhanden, traegt aber keinen App-Attest-Schluessel — der
    Fall, den ein abgebrochenes Pairing wirklich hinterlaesst.
    """
    ctx = H.new_device(device_id)
    token, _ = await cp.create_enrollment_token("local-owner")
    _, status = await cp.begin_enrollment(
        enrollment_token=token, device_id=device_id,
        approval_public_key_x963_b64=P.b64e(ctx.appr_x963),
        app_attest_key_id=ctx.aakid)
    require_equal(status, "ok", f"Aufbau der halben Einschreibung: {status}")
    return ctx


def _binding_bytes(*, core_instance_id: str, device_id: str, session_nonce: str) -> bytes:
    return VSP.canonical_bytes(VSP.build_binding(
        core_instance_id=core_instance_id, device_id=device_id,
        session_nonce=session_nonce))


def _assertion(ctx, *, core_instance_id: str, session_nonce: str,
               device_id: str | None = None, counter: int = 1,
               client_data_hash: bytes | None = None) -> bytes:
    """Was die App vorlegen wuerde: eine Assertion ueber die Sitzungsbindung.

    Jeder Parameter ist absichtlich einzeln verstellbar — die negativen Faelle
    unten unterscheiden sich vom guten Fall in genau einem Feld.
    """
    raw = _binding_bytes(core_instance_id=core_instance_id,
                         device_id=device_id or ctx.device_id,
                         session_nonce=session_nonce)
    cdh = VSP.client_data_hash(raw) if client_data_hash is None else client_data_hash
    return AA.fake_assertion(ctx.aakey, cdh, counter)


async def _accept(nonces: VSP.SessionNonces, wired: _Wired, *, issued_nonce: str,
                  claimed_nonce: str, assertion: bytes,
                  device_id: str | None = None) -> bool:
    """Die Reihenfolge des Endpunkts: erst die Nonce verbrauchen, dann pruefen.

    Sie steht hier nachgebaut, weil der Wiedereinspielungsschutz auf die
    ZUSAMMENARBEIT beider Schritte faellt — `verify_session_proof` allein kennt
    keine Einmaligkeit.
    """
    did = device_id or wired.device_id
    if not nonces.consume(claimed_nonce, did):
        return False
    return await VSP.verify_session_proof(
        wired.cp, device_id=did, core_instance_id=wired.core_id,
        session_nonce=issued_nonce, assertion=assertion)


class _Shim:
    """Ein Kontrollpfad-Stellvertreter mit genau den zwei Feldern, die zaehlen.

    `verify_session_proof` greift `attest_verifier` mit `getattr(..., None)` ab.
    Damit muss auch ein Objekt gehen, das das Feld gar nicht besitzt.
    """

    def __init__(self, store, verifier=None, *, with_verifier_attr: bool = True) -> None:
        self.store = store
        if with_verifier_attr:
            self.attest_verifier = verifier


class _BrokenStore:
    """Ein Speicher, der beim Lesen ausfaellt — Platte voll, Datei gesperrt."""

    def __init__(self) -> None:
        self.calls = 0

    async def get_device(self, device_id):
        self.calls += 1
        raise sqlite3.OperationalError("database is locked")


# ----------------------------------------------- 1. der gute Fall ----------

async def t_eine_gueltige_assertion_beweist_die_interaktive_sitzung():
    """Der Weg, den die echte App geht — sonst waere die Zeile unerreichbar.

    Faellt das hier, dann traegt KEINE Sitzung je die reduzierte iPhone-Zeile,
    und der ganze Milestone waere wirkungslos statt unsicher.
    """
    wired = await _wire()
    try:
        nonces = VSP.SessionNonces()
        nonce = nonces.issue(wired.device_id)
        assertion = _assertion(wired.ctx, core_instance_id=wired.core_id,
                               session_nonce=nonce)
        require(await _accept(nonces, wired, issued_nonce=nonce, claimed_nonce=nonce,
                              assertion=assertion),
                "die echte App wuerde ihre eigene Sitzung nicht beweisen koennen")
        require_equal(
            PL.origin_for_session("voice_iphone", interactive_proof=True),
            PL.OriginClass.TRUSTED_INTERACTIVE_APP,
            "der bewiesene Sitzungskanal fuehrt nicht in die iPhone-Zeile")
    finally:
        await _teardown(wired)


async def t_der_beweis_haengt_am_geraet_und_nicht_am_zaehler():
    """Zweimal derselbe Zaehlerstand ist hier in Ordnung — und das ist Absicht.

    Der App-Attest-Zaehler gehoert dem eingefrorenen Entscheidungspfad; der
    Sprachweg schreibt ihn bewusst nicht fort. Wer das spaeter fuer einen
    Fehler haelt und ihn hier hochzaehlen laesst, greift in eine fremde
    Transaktion. Gegen Wiedereinspielung schuetzt die Nonce (siehe unten),
    nicht diese Spalte — die Aufgabenteilung wird hier festgehalten.
    """
    wired = await _wire()
    try:
        for nonce_text in ("nonce-eins", "nonce-zwei"):
            assertion = _assertion(wired.ctx, core_instance_id=wired.core_id,
                                   session_nonce=nonce_text, counter=1)
            require(await VSP.verify_session_proof(
                wired.cp, device_id=wired.device_id, core_instance_id=wired.core_id,
                session_nonce=nonce_text, assertion=assertion),
                f"die Sitzung {nonce_text} liess sich nicht beweisen")
        row = await wired.store.get_device(wired.device_id)
        require_equal(int(row["app_attest_counter"] or 0), 0,
                      "der Sprachweg hat den Zaehler des Entscheidungspfads bewegt")
    finally:
        await _teardown(wired)


# ------------------------------- 2. ohne Beweis gilt die Raum-Zeile --------

def t_die_transportkennung_allein_traegt_die_iphone_zeile_nicht():
    """DER PUNKT DES GANZEN MECHANISMUS.

    Der Kanal `voice_iphone` wird gesetzt, NACHDEM sich das Telefon mit seiner
    statischen Kennung ausgewiesen hat. Wenn dieser Kanal allein schon die
    reduzierte Zeile traege, koennte jeder, der die Kennung kopiert hat, ohne
    Face ID die Haustuer oeffnen. Ohne Sitzungsbeweis muss die Herkunft
    deshalb auf das Raummikrofon zurueckfallen.
    """
    require_equal(
        PL.origin_for_session("voice_iphone", interactive_proof=False),
        PL.OriginClass.ROOM_VOICE,
        "die blosse Transportkennung erkaufte die reduzierte iPhone-Zeile")


def t_ohne_beweis_kostet_eine_folgenreiche_handlung_wieder_face_id():
    """Dasselbe eine Ebene weiter oben — als Wirkung, nicht als Aufzaehlung.

    Eine Herkunftskonstante fuer sich ist folgenlos. Was zaehlt, ist die
    Entscheidung, die daraus faellt: mit Beweis geht die Haustuer direkt, ohne
    Beweis kostet sie eine Face-ID-Runde — genau wie in V1.
    """
    ohne = PL.origin_for_session("voice_iphone", interactive_proof=False)
    mit = PL.origin_for_session("voice_iphone", interactive_proof=True)

    tuer_ohne = PL.decide(ohne, PL.ActionClass.HA_SECURITY)
    tuer_mit = PL.decide(mit, PL.ActionClass.HA_SECURITY)
    require_equal(tuer_ohne.decision, PL.Decision.REQUIRE_FACE_ID,
                  "die Haustuer ging ohne Sitzungsbeweis direkt auf")
    require(tuer_ohne.needs_approval, "die Absage verlangte keine Freigabe")
    require_equal(tuer_mit.decision, PL.Decision.EXECUTE_DIRECTLY,
                  "der bewiesenen Sitzung wurde die Erleichterung nicht gewaehrt")

    for cls in FOLGENREICH:
        require_equal(PL.decide(ohne, cls).decision, PL.Decision.REQUIRE_FACE_ID,
                      f"{cls.value} lief ohne Sitzungsbeweis ohne Freigabe durch")

    # Und die eine Ausnahme ausdruecklich, damit sie niemand fuer ein Versehen
    # haelt und „repariert": gewoehnliche Haustechnik bleibt aus dem Raum
    # bequem. Das hat der Nutzer entschieden, und der fehlende Sitzungsbeweis
    # ist kein Grund, ihm diese Entscheidung wieder wegzunehmen.
    require_equal(PL.decide(ohne, PL.ActionClass.HA_NORMAL).decision,
                  PL.Decision.EXECUTE_DIRECTLY,
                  "der Rueckfall hat auch gewoehnliches Licht mitgenommen")


def t_ein_satellit_gewinnt_durch_den_beweis_nichts():
    """Die Erleichterung haengt am KANAL, nicht am Wahrheitswert allein.

    Sonst koennte ein Aufrufer, der `interactive_proof=True` durchreicht, das
    Raummikrofon in die Telefonzeile heben.
    """
    require_equal(PL.origin_for_session("voice_satellite", interactive_proof=True),
                  PL.OriginClass.ROOM_VOICE,
                  "ein Satellit erbte die iPhone-Zeile")
    require_equal(PL.origin_for_session("was-auch-immer", interactive_proof=True),
                  PL.OriginClass.UNSPECIFIED,
                  "ein unbekannter Kanal bekam eine Vermutung statt des Sentinels")


# ------------------------------------------------- 3. Wiedereinspielung ----

def t_eine_nonce_gilt_genau_einmal():
    """Hier stirbt der Wiedereinspielungsangriff — und nur hier.

    Eine mitgeschnittene Assertion ist fuer immer gueltig: sie ist eine
    Signatur ueber feste Bytes. Wertlos wird sie ausschliesslich dadurch, dass
    die Nonce darin verbraucht ist.
    """
    nonces = VSP.SessionNonces()
    nonce = nonces.issue("dev-iphone")
    require(nonces.consume(nonce, "dev-iphone"), "die frische Nonce galt nicht")
    for versuch in range(3):
        require(not nonces.consume(nonce, "dev-iphone"),
                f"dieselbe Nonce galt ein {versuch + 2}. Mal")


async def t_eine_mitgeschnittene_assertion_oeffnet_keine_zweite_sitzung():
    """Derselbe Angriff, aber am nachgebauten Endpunkt statt am Zaehlwerk.

    Der Mitschnitt ist kryptografisch einwandfrei — `verify_session_proof`
    wuerde ihn erneut annehmen. Abgewiesen wird er davor, an der Nonce.
    """
    wired = await _wire()
    try:
        nonces = VSP.SessionNonces()
        nonce = nonces.issue(wired.device_id)
        mitschnitt = _assertion(wired.ctx, core_instance_id=wired.core_id,
                                session_nonce=nonce)
        require(await _accept(nonces, wired, issued_nonce=nonce, claimed_nonce=nonce,
                              assertion=mitschnitt),
                "schon die erste Sitzung galt nicht")
        require(not await _accept(nonces, wired, issued_nonce=nonce, claimed_nonce=nonce,
                                  assertion=mitschnitt),
                "der Mitschnitt oeffnete eine zweite Sitzung")
    finally:
        await _teardown(wired)


# ---------------------------------------- 4. Nonce gehoert ihrem Geraet ----

def t_die_nonce_eines_geraets_gilt_nicht_fuer_ein_anderes():
    """Zwei eingeschriebene Telefone teilen ihre Sitzungsbeweise nicht.

    Ohne die Bindung koennte ein zweites, harmloseres Geraet die Nonce des
    ersten einloesen — und die Herkunft des ersten waere fremdbestimmt.
    """
    nonces = VSP.SessionNonces()
    nonce = nonces.issue("dev-a")
    require(not nonces.consume(nonce, "dev-b"),
            "die Nonce von Geraet A galt fuer Geraet B")
    require(not nonces.consume(nonce, "dev-a"),
            "der Fehlversuch von B liess die Nonce fuer A stehen")


async def t_ein_zweites_geraet_kann_die_bindung_des_ersten_nicht_erfuellen():
    """Und dieselbe Trennung eine Ebene tiefer, in der Signatur.

    Selbst wenn ein zweites eingeschriebenes Telefon an die Nonce des ersten
    kaeme, signiert es mit seinem eigenen App-Attest-Schluessel — geprueft wird
    aber gegen den Schluessel des benannten Geraets.
    """
    wired = await _wire()
    try:
        fremd = await H.enroll_attested(wired.cp, device_id="dev-zweitgeraet")
        assertion = _assertion(fremd, core_instance_id=wired.core_id,
                               session_nonce="gemeinsame-nonce",
                               device_id=wired.device_id)
        require(not await VSP.verify_session_proof(
            wired.cp, device_id=wired.device_id, core_instance_id=wired.core_id,
            session_nonce="gemeinsame-nonce", assertion=assertion),
            "der Schluessel eines fremden Geraets bewies die Sitzung")
    finally:
        await _teardown(wired)


# ----------------------------------------------------- 5. Ablauf ----------

def t_eine_abgelaufene_nonce_gilt_nicht():
    """Die Nonce ueberbrueckt einen Handshake, keinen Gespraechsverlauf.

    Die Uhr wird eingespeist statt gewartet — ein Test, der `NONCE_TTL`
    schlaeft, waere langsam und wuerde beim ersten Verlaengern der Frist still
    aufhoeren, irgendetwas zu pruefen.
    """
    nonces = VSP.SessionNonces()
    t0 = 1_000.0
    frisch = nonces.issue("dev-iphone", now=t0)
    require(nonces.consume(frisch, "dev-iphone", now=t0 + VSP.NONCE_TTL - 0.01),
            "eine Nonce innerhalb ihrer Frist wurde abgewiesen")

    spaet = nonces.issue("dev-iphone", now=t0)
    require(not nonces.consume(spaet, "dev-iphone", now=t0 + VSP.NONCE_TTL + 0.01),
            "eine abgelaufene Nonce galt weiter")


def t_eine_abgelaufene_nonce_verschwindet_auch_ohne_versuch():
    """Ausgegebene Nonces duerfen sich nicht ansammeln.

    Der Speicher liegt im Prozess und wird von aussen befuellt: wer eine
    Verbindung oeffnet, erzeugt eine Nonce. Ohne Aufraeumen waere das ein
    langsam wachsender Posten, den niemand jemals ansieht.

    Dafuer wird ausnahmsweise in den internen Speicher gesehen — das Aufraeumen
    hat bewusst keine Aussenwirkung, und eine Zusicherung ueber das Verhalten
    allein koennte es deshalb gar nicht bemerken.
    """
    nonces = VSP.SessionNonces()
    t0 = 5_000.0
    alt = nonces.issue("dev-iphone", now=t0)
    nonces.issue("dev-iphone", now=t0 + VSP.NONCE_TTL + 1.0)
    require_equal(len(nonces._open), 1,
                  "die abgelaufene Nonce blieb im Speicher liegen")
    require(not nonces.consume(alt, "dev-iphone", now=t0 + VSP.NONCE_TTL + 1.0),
            "die aufgeraeumte Nonce galt noch")


# -------------------------------- 6. veraendert, falsch gebunden, fremd ----

async def t_eine_veraenderte_assertion_faellt_durch():
    """Signatur verbogen oder Blob unlesbar — beides ist ein `False`.

    Der unlesbare Fall zaehlt besonders: er entsteht nicht durch einen
    Angreifer, sondern durch eine aeltere App, die etwas anderes schickt. Er
    darf das Gespraech nicht kippen.
    """
    wired = await _wire()
    try:
        echt = _assertion(wired.ctx, core_instance_id=wired.core_id,
                          session_nonce="n1")
        verbogen = bytearray(echt)
        verbogen[-1] ^= 0xFF
        for name, blob in (("verbogene Signatur", bytes(verbogen)),
                           ("kein CBOR", b"nicht einmal ansatzweise cbor"),
                           ("leer", b"")):
            require(not await VSP.verify_session_proof(
                wired.cp, device_id=wired.device_id, core_instance_id=wired.core_id,
                session_nonce="n1", assertion=blob),
                f"{name} wurde als Sitzungsbeweis angenommen")
    finally:
        await _teardown(wired)


async def t_eine_assertion_auf_eine_fremde_bindung_faellt_durch():
    """Jedes Feld der Bindung ist tragend — einzeln nachgewiesen.

    Der Core rechnet die Bindung selbst neu; die App kann sie nicht mitliefern.
    Wer eine Assertion aus einem anderen Zusammenhang vorlegt (anderer Core,
    anderes Geraet, andere Nonce), scheitert an genau dieser Neuberechnung.
    """
    wired = await _wire()
    try:
        faelle = {
            "fremder Core": dict(core_instance_id="core-von-woanders",
                                 session_nonce="n1"),
            "fremdes Geraet": dict(core_instance_id=wired.core_id,
                                   session_nonce="n1",
                                   device_id="dev-irgendwer"),
            "fremde Nonce": dict(core_instance_id=wired.core_id,
                                 session_nonce="n-aus-einer-anderen-sitzung"),
        }
        for name, kwargs in faelle.items():
            assertion = _assertion(wired.ctx, **kwargs)
            require(not await VSP.verify_session_proof(
                wired.cp, device_id=wired.device_id, core_instance_id=wired.core_id,
                session_nonce="n1", assertion=assertion),
                f"eine Assertion mit {name} galt fuer diese Sitzung")
    finally:
        await _teardown(wired)


async def t_ein_fremder_domain_separator_faellt_durch():
    """Dieselben Bytes, ein anderer Zweck — und die Signatur gilt nicht mehr.

    Ohne den Separator waere jede Assertion des Geraets ueber irgendeine
    JSON-Struktur potenziell auch ein Sitzungsbeweis.
    """
    wired = await _wire()
    try:
        raw = _binding_bytes(core_instance_id=wired.core_id,
                             device_id=wired.device_id, session_nonce="n1")
        fremde_hashes = {
            "Entscheidungsdomaene": AP.decision_client_data_hash(raw),
            "Einschreibedomaene": AP.enrollment_client_data_hash(raw),
            "ohne Domaene": hashlib.sha256(raw).digest(),
        }
        for name, cdh in fremde_hashes.items():
            assertion = AA.fake_assertion(wired.ctx.aakey, cdh, 1)
            require(not await VSP.verify_session_proof(
                wired.cp, device_id=wired.device_id, core_instance_id=wired.core_id,
                session_nonce="n1", assertion=assertion),
                f"eine Assertion aus der {name} galt als Sitzungsbeweis")
    finally:
        await _teardown(wired)


# ------------------------------------------------------ 7. fail-closed ----

async def t_ohne_attestierten_schluessel_gibt_es_keinen_beweis():
    """Ein halb eingeschriebenes Geraet ist eine Zeile ohne Schluessel.

    Das ist kein Randfall: ein abgebrochenes Pairing hinterlaesst genau das.
    Gegen `NULL` darf nichts geprueft und schon gar nichts angenommen werden.
    """
    wired = await _wire()
    try:
        halb = await _enrolled_but_unattested(wired.cp)
        row = await wired.store.get_device(halb.device_id)
        require(row is not None, "die halbe Einschreibung legte keine Zeile an")
        require(not (row["app_attest_public_key"] or ""),
                "das unattestierte Geraet trug schon einen Schluessel")
        assertion = _assertion(halb, core_instance_id=wired.core_id,
                               session_nonce="n1")
        require(not await VSP.verify_session_proof(
            wired.cp, device_id=halb.device_id, core_instance_id=wired.core_id,
            session_nonce="n1", assertion=assertion),
            "ein Geraet ohne attestierten Schluessel bewies eine Sitzung")
    finally:
        await _teardown(wired)


async def t_ein_unbekanntes_geraet_beweist_nichts():
    """Eine Kennung, die es nie gab, bekommt keine Vermutung."""
    wired = await _wire()
    try:
        assertion = _assertion(wired.ctx, core_instance_id=wired.core_id,
                               session_nonce="n1", device_id="dev-gibt-es-nicht")
        require(not await VSP.verify_session_proof(
            wired.cp, device_id="dev-gibt-es-nicht", core_instance_id=wired.core_id,
            session_nonce="n1", assertion=assertion),
            "ein unbekanntes Geraet bewies eine Sitzung")
    finally:
        await _teardown(wired)


async def t_ohne_verifizierer_wird_nichts_geglaubt():
    """Kein Verifizierer heisst kein Beweis — nicht „dann eben ungeprueft".

    Ein Core ohne App-Attest-Verifizierer ist ein moeglicher Betriebszustand
    (fehlende Konfiguration). Er darf zur Raum-Zeile fuehren, nie zur
    Telefonzeile.
    """
    wired = await _wire()
    try:
        assertion = _assertion(wired.ctx, core_instance_id=wired.core_id,
                               session_nonce="n1")
        for name, shim in (
                ("Feld auf None", _Shim(wired.store, None)),
                ("Feld fehlt ganz", _Shim(wired.store, with_verifier_attr=False))):
            require(not await VSP.verify_session_proof(
                shim, device_id=wired.device_id, core_instance_id=wired.core_id,
                session_nonce="n1", assertion=assertion),
                f"ohne Verifizierer ({name}) wurde ein Beweis angenommen")
    finally:
        await _teardown(wired)


# --------------------------------- 8. eine Absage kippt kein Gespraech ----

async def t_ein_kaputter_speicher_kippt_kein_gespraech():
    """Der Beweis laeuft mitten im Verbindungsaufbau — vor dem ersten Wort.

    Wuerde ein Speicherfehler hier durchschlagen, waere aus einer verschaerften
    Freigabelage ein totes Telefon geworden. Fail-DOWN heisst: `False`, und das
    Gespraech laeuft mit der Raum-Zeile weiter.
    """
    store = _BrokenStore()
    ergebnis = await VSP.verify_session_proof(
        _Shim(store, H.fake_verifier()), device_id="dev-iphone",
        core_instance_id="core-1", session_nonce="n1", assertion=b"egal")
    require_equal(ergebnis, False, "ein kaputter Speicher wurde zum Beweis")
    require_equal(store.calls, 1, "der Speicher wurde gar nicht erst gefragt")

    # Und der Fall darunter: ein Kontrollpfad, der gar keinen Speicher hat.
    # Ein halb aufgebauter Core ist beim Start real; auch er darf nur ein
    # `False` erzeugen und keinen Abbruch.
    require_equal(await VSP.verify_session_proof(
        object(), device_id="dev-iphone", core_instance_id="core-1",
        session_nonce="n1", assertion=b"egal"),
        False, "ein Kontrollpfad ohne Speicher warf statt abzulehnen")


async def t_ein_verifizierer_der_ausrastet_kippt_kein_gespraech():
    """Dasselbe fuer die Pruefung selbst.

    Der eingefrorene Verifizierer wirft bei Ablehnung — er darf aber auch bei
    einem unerwarteten Fehler (kaputte Bibliothek, fremder Blob) nichts
    weiterreichen als ein `False`.
    """
    wired = await _wire()

    class _Rastet:
        def verify_assertion(self, **kwargs):
            raise MemoryError("etwas ganz anderes ist schiefgegangen")

    try:
        require_equal(await VSP.verify_session_proof(
            _Shim(wired.store, _Rastet()), device_id=wired.device_id,
            core_instance_id=wired.core_id, session_nonce="n1", assertion=b"egal"),
            False, "ein ausrastender Verifizierer wurde zum Beweis")
    finally:
        await _teardown(wired)


# ------------------------------------------------ 9. Domaenentrennung -----

def t_die_sitzungsbindung_ist_von_der_entscheidungsbindung_getrennt():
    """Ueber denselben Bytes drei verschiedene Hashes — sonst waere alles eins.

    Beide Wege benutzen denselben App-Attest-Schluessel. Nur der
    Domain-Separator verhindert, dass eine Sitzungs-Assertion als
    Entscheidungs-Assertion durchgeht — also dass ein Verbindungsaufbau eine
    Freigabe erzeugt.
    """
    raw = _binding_bytes(core_instance_id="core-1", device_id="dev-iphone",
                         session_nonce="n1")
    sitzung = VSP.client_data_hash(raw)
    entscheidung = AP.decision_client_data_hash(raw)
    einschreibung = AP.enrollment_client_data_hash(raw)
    require(len({sitzung, entscheidung, einschreibung}) == 3,
            "zwei Bindungsdomaenen erzeugen denselben clientDataHash")
    require(VSP.DOMAIN_VOICE_SESSION not in (AP.DOMAIN_DECISION, AP.DOMAIN_ENROLLMENT),
            "der Sitzungs-Separator ist keiner der beiden eingefrorenen")
    require_equal(sitzung, hashlib.sha256(
        VSP.DOMAIN_VOICE_SESSION + b"\x00" + raw).digest(),
        "die Sitzungsbindung hasht nicht das, was sie zu hashen behauptet")


def t_die_sitzungsbindung_traegt_ihren_eigenen_typ():
    """Auch der Inhalt der Bindung ist getrennt, nicht nur ihre Huelle.

    Ein Typfeld, das mit dem der Entscheidungsbindung zusammenfiele, waere eine
    zweite Chance auf genau die Verwechslung, die der Separator ausschliesst.
    """
    binding = VSP.build_binding(core_instance_id="core-1", device_id="dev-iphone",
                                session_nonce="n1")
    require_equal(binding["type"], VSP.TYPE_VOICE_SESSION_BINDING,
                  "die Sitzungsbindung nennt ihren Typ nicht")
    require(binding["type"] != AP.TYPE_DECISION_BINDING,
            "Sitzungs- und Entscheidungsbindung tragen denselben Typ")
    require_equal(sorted(binding),
                  ["core_instance_id", "device_id", "protocol_version",
                   "session_nonce", "type"],
                  "die Bindung enthaelt andere Felder als die, die gebunden werden")


# ------------------------------------- 10. der Beweis ist nicht Face ID ---

def t_der_beweis_ist_nicht_face_id():
    """App Attest attestiert eine App-Instanz, keine Person.

    Wer davor steht, ist damit nicht belegt — ein entsperrtes Telefon in einer
    fremden Hand legt denselben Beweis vor. Deshalb bleibt die oberste Klasse
    biometrisch, in JEDER Herkunft.
    """
    mit = PL.origin_for_session("voice_iphone", interactive_proof=True)
    require_equal(mit, PL.OriginClass.TRUSTED_INTERACTIVE_APP, "Aufbau")
    require_equal(PL.decide(mit, PL.ActionClass.VERY_CRITICAL).decision,
                  PL.Decision.REQUIRE_FACE_ID,
                  "der Sitzungsbeweis ersetzte die Biometrie")

    for origin in PL.OriginClass:
        entscheidung = PL.decide(origin, PL.ActionClass.VERY_CRITICAL).decision
        require(entscheidung is not PL.Decision.EXECUTE_DIRECTLY,
                f"{origin.value} durfte VERY_CRITICAL ohne Freigabe ausfuehren")


def t_auch_die_bewiesene_sitzung_hebt_fremden_inhalt_nicht_auf():
    """Der Beweis sagt etwas ueber den ENDPUNKT, nichts ueber den AUFTRAG.

    Eine Handlung, deren Argumente aus einer E-Mail stammen, ist auch vom
    bewiesenen Telefon aus kein Nutzerakt — erst die Anzeige auf dem Geraet und
    Face ID machen sie zu einem.
    """
    from solvio.capabilities.contract import ArgumentSource

    mit = PL.origin_for_session("voice_iphone", interactive_proof=True)
    fremd = {"name": ArgumentSource.UNTRUSTED_CONTENT}
    ergebnis = PL.decide(mit, PL.ActionClass.HA_NORMAL, provenance=fremd)
    require_equal(ergebnis.decision, PL.Decision.REQUIRE_FACE_ID,
                  "fremder Inhalt schaltete vom bewiesenen Telefon aus direkt")
    require_equal(ergebnis.reason_code, "untrusted_argument_overlay",
                  "die Verschaerfung kam aus einer anderen Regel als gedacht")



async def t_ein_geraet_ohne_attestierten_schluessel_ist_nie_bewiesen():
    """Aus einem Mutationslauf: die Pruefung auf den Schluessel ueberlebte.

    Sie ueberlebte, weil der Verifizierer der Testumgebung ohnehin ablehnte —
    also aus einem Grund, der nichts mit der Regel zu tun hat. Mit einem
    Verifizierer, der ALLES durchwinkt, zeigt sich, ob die Regel selbst traegt:
    ein Geraet ohne eingeschriebenen App-Attest-Schluessel darf nie als
    bewiesen gelten, egal wie gutmuetig die Kryptografie antwortet.
    """
    from solvio import voice_session_proof as VSP

    class _WinktAllesDurch:
        def verify_assertion(self, **kwargs):
            return 1

    class _Store:
        async def get_device(self, device_id):
            return {"app_attest_public_key": "", "app_attest_counter": 0}

    class _Ebene:
        store = _Store()
        attest_verifier = _WinktAllesDurch()

    ok = await VSP.verify_session_proof(
        _Ebene(), device_id="dev-1", core_instance_id="core-1",
        session_nonce="n", assertion=b"egal")
    require(not ok, "ein Geraet ohne attestierten Schluessel galt als bewiesen")



if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
