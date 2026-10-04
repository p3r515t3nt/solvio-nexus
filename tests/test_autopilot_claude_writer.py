"""Development Autopilot V0.6 — der sichere Claude-Schreiber.

Die ganze Sicherheitsaussage dieses Milestones steht auf vier Riegeln, und
jeder faengt einen Fall, den nur er faengt:

1. `--bare` liest Schluesselbund und OAuth nicht.
2. Der Kaefig verbietet `/usr/bin/security` und jedes Netz ausser der
   Rueckschleife zum Broker — kernel-erzwungen, auch fuer Kindprozesse.
3. Das Token in der Kindumgebung ist ein Broker-Token: ohne Lease `403`,
   nach Rotation `401`, ausserhalb der Rueckschleife wertlos.
4. Die echte Anmeldung liegt im Tresor und wird nur im Broker-Ausgang
   eingesetzt.

Diese Suite stellt alle vier — am **laufenden Code**, nicht am Kommentar. Die
Kaefigfaelle rufen echtes `sandbox-exec`; die Brokerfaelle laufen gegen einen
echten `BrokerService` mit synthetischer Anmeldung.

ASSERTION POLICY: `require*` aus `tests/_guard.py` sind Funktionsaufrufe und
ueberleben `python -O`.
"""
from __future__ import annotations

import asyncio
import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from _guard import enforce_assertions, require, require_equal  # noqa: E402

enforce_assertions()

_SANDBOX = tempfile.mkdtemp(prefix="solvio-claude-writer-")
os.environ["SOLVIO_VAULT_DIR"] = os.path.join(_SANDBOX, "vault")
os.environ["SOLVIO_VAULT_TEST_KEYSTORE"] = os.path.join(_SANDBOX, "keys")
os.environ.setdefault("SOLVIO_AUTOPILOT_DB",
                      os.path.join(_SANDBOX, "autopilot.sqlite3"))
atexit.register(shutil.rmtree, _SANDBOX, True)

from solvio.agent_runtime import isolation as ISO          # noqa: E402
from solvio.autopilot import builders as B                 # noqa: E402
from solvio.autopilot import canary as CANARY              # noqa: E402
from solvio.autopilot import capacity as CAP               # noqa: E402
from solvio.autopilot import contract as C                 # noqa: E402
from solvio.autopilot import routing as RT                 # noqa: E402
from solvio.autopilot import store as ST                   # noqa: E402
from solvio.capabilities import policy as AP               # noqa: E402
from solvio.provider_broker import anthropic as AN         # noqa: E402
from solvio.provider_broker import service as SVC          # noqa: E402
from solvio.secret_vault import admin as VA                # noqa: E402
from solvio.secret_vault import broker as VB               # noqa: E402
from solvio.secret_vault import policy as VP               # noqa: E402
from solvio.secret_vault.store import VaultStore           # noqa: E402
from solvio.secret_vault import keyring as VK               # noqa: E402
from solvio.specialists import launcher as L               # noqa: E402

# Der Tresor braucht einen Schluessel, bevor etwas hineingeht. Er liegt im
# umgelenkten Testspeicher — die Isolationszusicherung oben stellt das.
VK.initialize_kek(allow_overwrite=True)

#: Ein Wert, der wie eine Anmeldung aussieht und keine ist. Er darf im ganzen
#: Baum nur an genau einer Stelle auftauchen: im ausgehenden Kopfsatz.
FAKE_CREDENTIAL = "sk-ant-oat-SYNTHETISCH-NIEMALS-ECHT-0001"


# ------------------------------------------------------------ Testisolation
def t_this_suite_cannot_touch_production_state() -> None:
    from solvio.secret_vault import keyring as K
    from solvio.secret_vault.store import vault_dir
    require("/.solvio-vault" not in vault_dir(), "Tresor nicht umgelenkt")
    require(K.is_test_backend(), "Schluesselbund des Tresors nicht umgelenkt")


# ---------------------------------------------------------------- Werkzeug
def _vault_with_credential(kind: str = AN.KIND_OAUTH) -> VB.SecretBroker:
    """Ein Tresor mit synthetischer Anthropic-Anmeldung."""
    store = VaultStore(os.path.join(_SANDBOX, f"v-{kind}.sqlite3"))
    VA.add(secret_ref=AN.SECRET_REF, kind=VP.SecretKind.OAUTH_REFRESH_TOKEN,
           plaintext=json.dumps({"kind": kind,
                                 "token": FAKE_CREDENTIAL}).encode(),
           allowed_capabilities=[AN.CAPABILITY],
           allowed_targets=[AN.UPSTREAM_ORIGIN],
           allowed_executors=[VP.ExecutorId.ANTHROPIC_BROKER],
           allow_background=True, display_name="synthetisch",
           store=store, replace=True)
    return VB.SecretBroker(store)


class _Inbound:
    """Eine eingehende Anfrage, wie der Kaefig sie schickt."""

    def __init__(self, **headers: str) -> None:
        self.headers = {"content-type": "application/json",
                        "x-api-key": "broker-token-des-kaefigs",
                        "authorization": "Bearer broker-token-des-kaefigs",
                        **headers}


def _profile(port: int = 0) -> str:
    """Nur die REGELN eines gerenderten Profils, ohne die Erklaerungen.

    Dieselbe Lehre wie bei `sealed_violations`: eine Textsuche ueber die
    Rohfassung schlaegt ausgerechnet an dem Kommentar an, der es richtig
    macht.
    """
    werk = tempfile.mkdtemp(dir=_SANDBOX)
    kratz = tempfile.mkdtemp(dir=_SANDBOX)
    roh = ISO.render_profile(workspace=werk, scratch=kratz, broker_port=port)
    return "\n".join(z for z in roh.splitlines()
                     if not z.lstrip().startswith(";"))


def _in_cage(port: int, *argv: str) -> subprocess.CompletedProcess:
    """Ein Kommando im gemakelten Kaefig. Echtes `sandbox-exec`."""
    werk = tempfile.mkdtemp(dir=_SANDBOX)
    kratz = tempfile.mkdtemp(dir=_SANDBOX)
    profil = os.path.join(kratz, "probe.sb")
    with open(profil, "w", encoding="utf-8") as fh:
        fh.write(ISO.render_profile(workspace=werk, scratch=kratz,
                                    broker_port=port))
    return subprocess.run([ISO.SANDBOX_EXEC, "-f", profil, *argv],
                          capture_output=True, text=True, timeout=60)


# ======================================================== 1. Die Credential
def t_1_the_real_credential_never_reaches_the_builder() -> None:
    """Der echte Wert steht in EINEM Kopfsatz und nirgends sonst.

    Geprueft wird das Ergebnis, nicht die Absicht: der Kopfsatz, den der
    Broker baut, traegt ihn — und die Umgebung, die der Builder bekommt,
    traegt ihn nicht.
    """
    upstream = AN.AnthropicUpstream(_vault_with_credential())
    kopf = upstream.outbound_headers(_Inbound())
    require(FAKE_CREDENTIAL in kopf.get("Authorization", ""),
            f"der Wert steht nicht im ausgehenden Kopf: {sorted(kopf)}")

    # Und die Kindumgebung kennt ihn nicht.
    env = L.brokered_environment(base_url="http://127.0.0.1:8792",
                                 token="broker-token-x")
    for name, wert in env.items():
        require(FAKE_CREDENTIAL not in wert,
                f"die echte Anmeldung steht in der Kindumgebung unter {name}")
    require_equal(env["ANTHROPIC_API_KEY"], "broker-token-x",
                  "der Builder traegt etwas anderes als das Broker-Token")


def t_1b_the_cage_credential_never_travels_upstream() -> None:
    """Was der Kaefig mitschickt, faellt am Ausgang weg.

    Er traegt dort sein Broker-Token — draussen hat das nichts zu suchen, und
    es waere ausserdem eine Aussage ueber die Herkunft, die der Broker nicht
    bestaetigen kann.
    """
    upstream = AN.AnthropicUpstream(_vault_with_credential())
    kopf = upstream.outbound_headers(_Inbound())
    zusammen = " ".join(f"{k}:{v}" for k, v in kopf.items())
    require("broker-token-des-kaefigs" not in zusammen,
            f"das Token des Kaefigs reiste mit: {sorted(kopf)}")


def t_1c_an_api_key_travels_as_x_api_key_and_an_oauth_as_bearer() -> None:
    """Die Form wird gelesen, nicht geraten.

    Ein Rateschritt aus dem Praefix waere ein 401 beim Anbieter, den niemand
    erklaeren kann — und er waere genau dann falsch, wenn Anthropic ein neues
    Praefix einfuehrt.
    """
    oauth = AN.AnthropicUpstream(_vault_with_credential(AN.KIND_OAUTH))
    kopf = oauth.outbound_headers(_Inbound())
    require("Authorization" in kopf and "x-api-key" not in kopf,
            f"OAuth reiste falsch: {sorted(kopf)}")
    require_equal(kopf.get("anthropic-beta"), AN.OAUTH_BETA,
                  "die OAuth-Beta fehlt")

    schluessel = AN.AnthropicUpstream(_vault_with_credential(AN.KIND_API_KEY))
    kopf2 = schluessel.outbound_headers(_Inbound())
    require("x-api-key" in kopf2 and "Authorization" not in kopf2,
            f"der API-Schluessel reiste falsch: {sorted(kopf2)}")


def t_1d_an_unknown_credential_kind_is_refused_not_guessed() -> None:
    store = VaultStore(os.path.join(_SANDBOX, "v-unbekannt.sqlite3"))
    VA.add(secret_ref=AN.SECRET_REF, kind=VP.SecretKind.API_KEY,
           plaintext=json.dumps({"kind": "irgendwas",
                                 "token": FAKE_CREDENTIAL}).encode(),
           allowed_capabilities=[AN.CAPABILITY],
           allowed_targets=[AN.UPSTREAM_ORIGIN],
           allowed_executors=[VP.ExecutorId.ANTHROPIC_BROKER],
           allow_background=True, store=store, replace=True)
    upstream = AN.AnthropicUpstream(VB.SecretBroker(store))
    try:
        upstream.outbound_headers(_Inbound())
    except AN.AnthropicAuthError as exc:
        require_equal(exc.reason, "credential_malformed",
                      f"falscher Grund: {exc.reason}")
    else:
        raise AssertionError("eine unbekannte Anmeldeform kam durch")


def t_1d2_an_empty_kind_is_refused_instead_of_guessed() -> None:
    """Der Fall, in dem Raten verlockend waere: gar keine Form angegeben.

    Eine Mutation hat gezeigt, dass die erste Fassung dieser Zusicherung ihn
    nicht traf — sie pruefte eine FALSCHE Form, und die faellt auch bei einem
    Rateschritt. Geraten wird aber genau dann, wenn nichts dasteht: aus
    `sk-ant-oat…` auf OAuth zu schliessen ist die Sorte Klugheit, die beim
    naechsten Praefixwechsel des Anbieters still das Falsche tut.
    """
    for form in ("", None):
        store = VaultStore(os.path.join(_SANDBOX, f"v-leer-{form!r}.sqlite3"))
        nutzlast = {"token": FAKE_CREDENTIAL}
        if form is not None:
            nutzlast["kind"] = form
        VA.add(secret_ref=AN.SECRET_REF, kind=VP.SecretKind.API_KEY,
               plaintext=json.dumps(nutzlast).encode(),
               allowed_capabilities=[AN.CAPABILITY],
               allowed_targets=[AN.UPSTREAM_ORIGIN],
               allowed_executors=[VP.ExecutorId.ANTHROPIC_BROKER],
               allow_background=True, store=store, replace=True)
        upstream = AN.AnthropicUpstream(VB.SecretBroker(store))
        try:
            kopf = upstream.outbound_headers(_Inbound())
        except AN.AnthropicAuthError as exc:
            require_equal(exc.reason, "credential_malformed",
                          f"falscher Grund fuer kind={form!r}: {exc.reason}")
        else:
            raise AssertionError(
                f"kind={form!r} wurde geraten statt abgewiesen: {sorted(kopf)}")


def t_1e_only_the_bound_executor_may_borrow_it() -> None:
    """Die Bindung ist der Mechanismus, nicht der Kommentar.

    Ein Aufrufer, der die richtige Kennung BEHAUPTET, aber aus dem falschen
    Modul kommt, bekommt nichts — der Tresor nimmt den Modulnamen selbst.
    """
    from solvio.secret_vault import context as SC

    store = VaultStore(os.path.join(_SANDBOX, "v-bind.sqlite3"))
    VA.add(secret_ref=AN.SECRET_REF, kind=VP.SecretKind.API_KEY,
           plaintext=json.dumps({"kind": AN.KIND_API_KEY,
                                 "token": FAKE_CREDENTIAL}).encode(),
           allowed_capabilities=[AN.CAPABILITY],
           allowed_targets=[AN.UPSTREAM_ORIGIN],
           allowed_executors=[VP.ExecutorId.ANTHROPIC_BROKER],
           allow_background=True, store=store, replace=True)
    probe = VB.SecretBroker(store)
    verweigert = False
    with SC.bound(SC.UseContext(origin=AP.OriginClass.BACKGROUND_AUTOMATION,
                                capability=AN.CAPABILITY,
                                automation_id=AN.AUTOMATION_ID)):
        try:
            # DIESER Aufruf kommt aus `tests.…`, nicht aus
            # `solvio.provider_broker.anthropic`.
            with probe.use(AN.SECRET_REF,
                           executor=VP.ExecutorId.ANTHROPIC_BROKER,
                           target=AN.UPSTREAM_ORIGIN):
                pass
        except VB.SecretDenied as exc:
            verweigert = True
            require_equal(exc.reason, VP.Denied.EXECUTOR_MODULE_MISMATCH,
                          f"falscher Grund: {exc.reason}")
    require(verweigert, "ein fremdes Modul durfte die Anmeldung leihen")


# ============================================== 2.-5. Der Kaefig, am Kernel
def t_2_the_builder_cannot_read_the_keychain() -> None:
    ergebnis = _in_cage(8792, "/usr/bin/security", "find-generic-password",
                        "-s", "Claude Code-credentials")
    require(ergebnis.returncode != 0,
            "der Schluesselbund war aus dem Kaefig lesbar")
    require("not permitted" in (ergebnis.stderr + ergebnis.stdout).lower(),
            f"unerwartete Absage: {ergebnis.stderr[:160]}")


def t_3_security_cannot_be_executed_at_all() -> None:
    """Nicht „findet nichts", sondern „darf nicht starten"."""
    ergebnis = _in_cage(8792, "/usr/bin/security", "list-keychains")
    require("execvp" in ergebnis.stderr or "not permitted" in ergebnis.stderr.lower(),
            f"`security` lief im Kaefig: rc={ergebnis.returncode} "
            f"{ergebnis.stderr[:160]}")
    regeln = _profile(8792)
    require('(deny process-exec (literal "/usr/bin/security"))' in regeln,
            "die Sperre steht nicht im Profil")


def t_4_the_provider_is_unreachable_from_the_cage() -> None:
    """Kein DNS, kein 443 — der Anbieter ist strukturell nicht erreichbar."""
    ergebnis = _in_cage(8792, "/usr/bin/curl", "-sS", "--max-time", "5",
                        "https://api.anthropic.com/v1/messages")
    require(ergebnis.returncode != 0,
            "api.anthropic.com war aus dem Kaefig erreichbar")
    regeln = _profile(8792)
    for verboten in ('"*:443"', '"*:80"', '"*:53"', "mDNSResponder"):
        require(verboten not in regeln,
                f"das gemakelte Profil erlaubt {verboten}")


def t_4b_claude_ai_is_unreachable_too() -> None:
    ergebnis = _in_cage(8792, "/usr/bin/curl", "-sS", "--max-time", "5",
                        "https://claude.ai/")
    require(ergebnis.returncode != 0, "claude.ai war erreichbar")


def t_5_only_the_broker_port_is_reachable() -> None:
    """Genau EIN Loopback-Ziel — auch der Core ist keines."""
    regeln = _profile(8792)
    netzzeilen = [z.strip() for z in regeln.splitlines() if "remote " in z]
    require_equal(len(netzzeilen), 1,
                  f"mehr als ein Netzziel: {netzzeilen}")
    require('localhost:8792' in netzzeilen[0],
            f"das eine Ziel ist nicht der Brokerport: {netzzeilen}")

    # Und am Kernel: ein anderer Loopback-Port geht nicht.
    ergebnis = _in_cage(8792, "/usr/bin/curl", "-sS", "--max-time", "3",
                        "http://127.0.0.1:8766/")
    require(ergebnis.returncode != 0,
            "ein fremder Loopback-Port war erreichbar")


def t_5b_the_ordinary_builder_profile_is_unchanged() -> None:
    """Die neue Zeile ist eine Variante, kein Umbau.

    Ohne `broker_port` muss das Profil exakt das alte sein — sonst haette ein
    Milestone, der einen zweiten Schreiber baut, dem ersten heimlich das Netz
    verandert.
    """
    frei = _profile(0)
    for noetig in ('"*:443"', '"*:80"', '"*:53"', "mDNSResponder"):
        require(noetig in frei,
                f"dem gewoehnlichen Builder fehlt {noetig}")
    require("localhost:" not in frei,
            "der gewoehnliche Builder bekam ein Loopback-Ziel")


# ================================================= 6.-10. Die Broker-Tore
def _free_port() -> int:
    """Ein wirklich freier Port. `port=0` faellt im Dienst auf den
    konfigurierten zurueck — und der ist in Produktion belegt."""
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _broker(vault=None) -> SVC.BrokerService:
    return SVC.BrokerService(provider_key="synthetisch-openai",
                             port=_free_port(),
                             ledger_path=os.path.join(
                                 tempfile.mkdtemp(dir=_SANDBOX), "b.sqlite3"),
                             vault=vault)


async def _call(dienst, *, path: str, token: str, body: dict,
                headers: dict | None = None):
    import aiohttp
    kopf = {"Authorization": f"Bearer {token}", "x-api-key": token,
            "Content-Type": "application/json", **(headers or {})}
    async with aiohttp.ClientSession() as sitzung:
        async with sitzung.post(f"http://127.0.0.1:{dienst.port}{path}",
                                json=body, headers=kopf) as antwort:
            return antwort.status, await antwort.text()


def _run_with_broker(fn, *, vault=None):
    async def _outer():
        dienst = _broker(vault)
        await dienst.start()
        try:
            return await fn(dienst)
        finally:
            await dienst.stop()
    return asyncio.run(_outer())


def t_6_an_unknown_path_is_refused() -> None:
    async def probe(dienst):
        token = dienst.register_principal(SVC.AUTOPILOT_WRITER_CLAUDE_PRINCIPAL)
        dienst.registry.open_lease(SVC.AUTOPILOT_WRITER_CLAUDE_PRINCIPAL,
                                   ref="t", deadline=9e9, now=time.time())
        return await _call(dienst, path="/v1/complete", token=token,
                           body={"model": "claude-sonnet-5"})
    status, _ = _run_with_broker(probe, vault=_vault_with_credential())
    require(status in (403, 404),
            f"ein unbekannter Pfad kam durch: {status}")


def t_7_a_foreign_principal_is_refused_on_this_surface() -> None:
    """Ein gueltiges Token — aber nicht fuer DIESE Flaeche.

    Geprueft wird der GRUND, nicht der Statuscode. Eine Mutation hat gezeigt,
    warum: nimmt man das Auftraggebertor heraus, faellt derselbe Aufruf am
    Modelltor — mit demselben `403`. Ein Test, der nur die Zahl liest, haette
    das „bestanden" und ein totes Tor durchgewunken.
    """
    async def probe(dienst):
        ergebnisse = {}
        for fremd in ("deep-gateway", "autopilot-lead", "agent-runtime",
                      "bot:solvio-researcher"):
            token = dienst.register_principal(fremd)
            dienst.registry.open_lease(fremd, ref="t", deadline=9e9,
                                       now=time.time())
            status, rumpf = await _call(dienst, path="/v1/messages",
                                        token=token,
                                        body={"model": "claude-sonnet-5"})
            ergebnisse[fremd] = (status, rumpf)
        return ergebnisse
    ergebnisse = _run_with_broker(probe, vault=_vault_with_credential())
    for fremd, (status, rumpf) in ergebnisse.items():
        require_equal(status, 403,
                      f"der fremde Auftraggeber {fremd!r} kam durch: {status}")
        require("principal_not_allowed" in rumpf,
                f"{fremd!r} fiel am falschen Tor: {rumpf[:120]}")


def t_7b_a_writer_principal_reaches_the_surface_at_all() -> None:
    """Die Gegenprobe zum Auftraggebertor.

    Ohne sie koennte das Tor ALLE abweisen und der Test daneben waere trotzdem
    gruen — ein Tor, das niemanden durchlaesst, ist keine Grenze, sondern ein
    kaputter Dienst.
    """
    async def probe(dienst):
        name = SVC.AUTOPILOT_WRITER_CLAUDE_PRINCIPAL
        token = dienst.register_principal(name)
        dienst.registry.open_lease(name, ref="t", deadline=9e9,
                                   now=time.time())
        return await _call(dienst, path="/v1/messages", token=token,
                           body={"model": "claude-sonnet-5"})
    # Bewusst OHNE Anmeldung: dann endet der Fluss am Credential-Tor, das
    # NACH dem Auftraggebertor steht. Das beweist beides auf einmal — der
    # eigene Schreiber kommt durch das Auftraggebertor, und die Suite
    # telefoniert dabei nicht zum Anbieter. Eine Zusicherung, die eine echte
    # Weiterleitung braucht, misst die Erreichbarkeit von Anthropic statt
    # unseres Codes.
    status, rumpf = _run_with_broker(probe, vault=None)
    require_equal(status, 503,
                  f"der eigene Schreiber kam nicht bis zum Credential-Tor: "
                  f"{status} {rumpf[:120]}")
    require("no_credential" in rumpf, f"falscher Grund: {rumpf[:120]}")
    for frueheres_tor in ("principal_not_allowed", "path_not_allowed",
                          "model_not_allowed", "lease_absent", "bad_token"):
        require(frueheres_tor not in rumpf,
                f"der eigene Schreiber fiel schon an {frueheres_tor}")


def t_8_a_model_outside_the_allowlist_is_refused() -> None:
    async def probe(dienst):
        token = dienst.register_principal(SVC.AUTOPILOT_WRITER_CLAUDE_PRINCIPAL)
        dienst.registry.open_lease(SVC.AUTOPILOT_WRITER_CLAUDE_PRINCIPAL,
                                   ref="t", deadline=9e9, now=time.time())
        ergebnisse = {}
        for modell in ("claude-opus-5", "gpt-5.4", "claude-3-opus", ""):
            status, _ = await _call(dienst, path="/v1/messages", token=token,
                                    body={"model": modell})
            ergebnisse[modell or "(leer)"] = status
        return ergebnisse
    ergebnisse = _run_with_broker(probe, vault=_vault_with_credential())
    for modell, status in ergebnisse.items():
        require_equal(status, 403,
                      f"{modell} kam durch ({status}) — das grosse Modell "
                      f"erreicht nur der Eskalations-Auftraggeber")


class _Headers:
    """Nur Koepfe — genau das, was `_authenticate` liest."""

    def __init__(self, **kopf: str) -> None:
        self.headers = dict(kopf)


def t_9z_the_broker_reads_the_token_from_the_anthropic_header() -> None:
    """Claude Code schickt den Schluessel als `x-api-key`, nicht als Bearer.

    Live gemessen: der erste B1-Beweis endete an drei `bad_token`-Zeilen im
    Brokerbuch, weil hier nur `Authorization: Bearer` gelesen wurde. Das ist
    keine zweite Berechtigung — es ist derselbe Wert an der Stelle, an die ihn
    das Anthropic-Protokoll schreibt.

    Geprueft wird der Kopfsatz, **nicht das Netz**: eine Zusicherung, die zum
    Anbieter telefoniert, misst dessen Erreichbarkeit statt unseres Codes.
    """
    dienst = _broker(_vault_with_credential())
    dienst.ledger.open()
    name = SVC.AUTOPILOT_WRITER_CLAUDE_PRINCIPAL
    token = dienst.register_principal(name)

    beide = dienst._authenticate(_Headers(
        Authorization=f"Bearer {token}", **{"x-api-key": token}))
    nur_bearer = dienst._authenticate(_Headers(Authorization=f"Bearer {token}"))
    nur_anthropic = dienst._authenticate(_Headers(**{"x-api-key": token}))

    for wie, gefunden in (("beide Koepfe", beide),
                          ("nur Bearer", nur_bearer),
                          ("nur x-api-key", nur_anthropic)):
        require(gefunden is not None,
                f"{wie}: der Token wurde nicht gelesen")
        require_equal(gefunden.name, name, f"{wie}: falscher Auftraggeber")


def t_9y_a_wrong_token_in_the_anthropic_header_is_still_refused() -> None:
    """Die Gegenprobe: gelesen heisst nicht geglaubt."""
    dienst = _broker(_vault_with_credential())
    dienst.ledger.open()
    dienst.register_principal(SVC.AUTOPILOT_WRITER_CLAUDE_PRINCIPAL)
    for kopf in (_Headers(**{"x-api-key": "erfunden-nicht-vom-broker"}),
                 _Headers(Authorization="Bearer erfunden"),
                 _Headers(Authorization="Basic dXNlcjpwYXNz"),
                 _Headers()):
        require(dienst._authenticate(kopf) is None,
                f"ein ungueltiger Kopfsatz kam durch: {kopf.headers}")


def t_9x_only_the_pinned_beta_marks_travel_upstream() -> None:
    """Geschlossene Liste statt Durchreichen — beides gemessen.

    Der erste Live-Beweis endete an `400 context_management: Extra inputs are
    not permitted`: die CLI schickt ein Feld im Rumpf, das der Anbieter nur
    mit der passenden Beta-Marke annimmt. Der Entwurf hatte `anthropic-beta`
    ganz verworfen, weil eine Marke aus dem Kaefig eine Funktion einschalten
    koennte, die niemand bestellt hat.

    Beides bleibt wahr. Die Loesung ist die Liste: was darin steht, reist mit;
    alles andere faellt weg.
    """
    upstream = AN.AnthropicUpstream(_vault_with_credential(AN.KIND_OAUTH))
    kopf = upstream.outbound_headers(_Inbound(**{
        "anthropic-beta": ("context-management-2025-06-27,"
                           "boese-marke-2099-01-01,"
                           "effort-2025-11-24")}))
    marken = [t.strip() for t in kopf["anthropic-beta"].split(",")]
    require("boese-marke-2099-01-01" not in marken,
            f"eine unbekannte Marke reiste mit: {marken}")
    require("context-management-2025-06-27" in marken,
            f"die gemessene noetige Marke fehlt: {marken}")
    require(AN.OAUTH_BETA in marken,
            f"die OAuth-Marke fehlt: {marken}")
    for marke in marken:
        require(marke in AN.CLIENT_BETAS or marke == AN.OAUTH_BETA,
                f"{marke} steht in keiner der beiden Listen")


def t_9w_a_request_without_betas_gets_only_what_the_core_sets() -> None:
    """Ohne Marken in der Anfrage setzt der Core nur seine eigene."""
    upstream = AN.AnthropicUpstream(_vault_with_credential(AN.KIND_OAUTH))
    kopf = upstream.outbound_headers(_Inbound())
    require_equal(kopf.get("anthropic-beta"), AN.OAUTH_BETA,
                  f"unerwartete Marken ohne Anfrage: {kopf.get('anthropic-beta')}")

    # Und bei einem API-Schluessel gibt es gar keine OAuth-Marke.
    schluessel = AN.AnthropicUpstream(_vault_with_credential(AN.KIND_API_KEY))
    kopf2 = schluessel.outbound_headers(_Inbound())
    require(AN.OAUTH_BETA not in kopf2.get("anthropic-beta", ""),
            "die OAuth-Marke reiste mit einem API-Schluessel")


def t_9_a_missing_lease_is_refused() -> None:
    async def probe(dienst):
        token = dienst.register_principal(SVC.AUTOPILOT_WRITER_CLAUDE_PRINCIPAL)
        return await _call(dienst, path="/v1/messages", token=token,
                           body={"model": "claude-sonnet-5"})
    status, rumpf = _run_with_broker(probe, vault=_vault_with_credential())
    require_equal(status, 403, f"ohne Lease kam etwas durch: {status}")
    require("lease_absent" in rumpf, f"falscher Grund: {rumpf[:120]}")


def t_10_the_token_is_worthless_after_rotation() -> None:
    """Ein Token gilt nur so lange wie sein Auftrag.

    Dieselbe Lehre, die den Technical Lead in der A7-Abnahme 45 Minuten
    kostete — hier fuer den Schreiber gestellt.
    """
    async def probe(dienst):
        name = SVC.AUTOPILOT_WRITER_CLAUDE_PRINCIPAL
        alt = dienst.register_principal(name)
        lease = dienst.registry.open_lease(name, ref="t", deadline=9e9,
                                           now=time.time())
        dienst.close_lease(lease)          # rotiert den Auftraggeber
        dienst.registry.open_lease(name, ref="t2", deadline=9e9,
                                   now=time.time())
        return await _call(dienst, path="/v1/messages", token=alt,
                           body={"model": "claude-sonnet-5"})
    status, _ = _run_with_broker(probe, vault=_vault_with_credential())
    require_equal(status, 401,
                  f"der alte Token oeffnete nach der Rotation noch: {status}")


def t_10b_without_a_credential_the_surface_answers_no_credential() -> None:
    """Fail-closed, mit benanntem Grund — der Autopilot liest ihn."""
    async def probe(dienst):
        name = SVC.AUTOPILOT_WRITER_CLAUDE_PRINCIPAL
        token = dienst.register_principal(name)
        dienst.registry.open_lease(name, ref="t", deadline=9e9,
                                   now=time.time())
        return await _call(dienst, path="/v1/messages", token=token,
                           body={"model": "claude-sonnet-5"})
    status, rumpf = _run_with_broker(probe, vault=None)
    require_equal(status, 503, f"ohne Anmeldung kam etwas durch: {status}")
    require("no_credential" in rumpf, f"falscher Grund: {rumpf[:120]}")


# ============================================= 11. Der Checkpoint-Scan
def t_11_a_secret_in_the_checkpoint_turns_the_scan_red() -> None:
    """Ein Token, das ein Builder in den Baum schreibt, wird nie publiziert."""
    werk = tempfile.mkdtemp(dir=_SANDBOX)
    for befehl in (["init", "-q", "-b", "main"], ["config", "user.email", "t@e"],
                   ["config", "user.name", "T"]):
        subprocess.run(["git", "-C", werk, *befehl], check=True,
                       capture_output=True)
    with open(os.path.join(werk, "auth.json"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"tokens": {"access_token": FAKE_CREDENTIAL}}))
    ergebnis = B.safe_checkpoint(werk, phase="build")
    require(not ergebnis.ok,
            "ein Kredential im Baum kam durch den Checkpoint")
    require(ergebnis.findings, "der Scan nannte keinen Fund")


# ============================================= DEBT-0184: der Kaefig im Baum
def _git_werk() -> str:
    werk = tempfile.mkdtemp(dir=_SANDBOX)
    for befehl in (["init", "-q", "-b", "main"], ["config", "user.email", "t@e"],
                   ["config", "user.name", "T"]):
        subprocess.run(["git", "-C", werk, *befehl], check=True,
                       capture_output=True)
    with open(os.path.join(werk, "start.txt"), "w", encoding="utf-8") as fh:
        fh.write("basis\n")
    subprocess.run(["git", "-C", werk, "add", "-A"], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", werk, "commit", "-qm", "basis"], check=True,
                   capture_output=True)
    return werk


def t_0184_the_jail_never_lands_in_the_workspace() -> None:
    """Ein Laufzeitartefakt, das nie in den Baum kommt, braucht keine Regel.

    Live gefunden in der B5-Abnahme: der erste Checkpoint trug vier Dateien
    statt drei — darunter das gerenderte Seatbelt-Profil unter
    `<workspace>/.autopilot/jail`. `safe_checkpoint` nimmt mit `git add -A`
    alles, was nicht ignoriert ist.

    Eine Ignore-Regel waere die schwaechere Antwort: sie versteckt etwas, das
    im Baum liegt, und die naechste Regel versteckt dann vielleicht ein echtes
    Ergebnis.
    """
    werk = _git_werk()
    jail = B._jail_dir(werk)
    require(not os.path.realpath(jail).startswith(os.path.realpath(werk)),
            f"der Kaefig liegt im Arbeitsbereich: {jail}")

    # Und er ist stabil je Arbeitsbereich, aber verschieden zwischen zweien.
    require_equal(jail, B._jail_dir(werk), "der Kaefigpfad wandert")
    require(B._jail_dir(_git_werk()) != jail,
            "zwei Arbeitsbereiche teilen sich denselben Kaefig")


def t_0184_a_checkpoint_carries_the_work_and_not_the_jail() -> None:
    """Der Beweis am Ergebnis: was im Checkpoint steht und was nicht.

    Gestellt wird beides — das Laufzeitartefakt bleibt draussen, UND die
    echte Ausgabe des Builders kommt mit. Eine Zusicherung, die nur das erste
    prueft, waere mit einem Checkpoint zufrieden, der gar nichts enthaelt.
    """
    werk = _git_werk()
    # Was ein Builder wirklich schreibt.
    with open(os.path.join(werk, "ergebnis.txt"), "w", encoding="utf-8") as fh:
        fh.write("echte Builder-Ausgabe\n")
    # Und was die Laufzeit schreibt — an der Stelle, an der es FRUEHER lag.
    alt_jail = os.path.join(werk, ".autopilot", "jail")
    os.makedirs(alt_jail, exist_ok=True)
    with open(os.path.join(alt_jail, "builder.sandbox.sb"), "w",
              encoding="utf-8") as fh:
        fh.write("(version 1)\n")
    # Der neue Ort, ausserhalb.
    jail = B._jail_dir(werk)
    with open(os.path.join(jail, "builder.sandbox.sb"), "w",
              encoding="utf-8") as fh:
        fh.write("(version 1)\n")

    ergebnis = B.safe_checkpoint(werk, phase="build")
    require(ergebnis.ok, f"der Checkpoint scheiterte: {ergebnis.reason} "
                         f"{ergebnis.findings}")
    dabei = subprocess.run(["git", "-C", werk, "show", "--name-only",
                            "--format=", "HEAD"], capture_output=True,
                           text=True).stdout.split()

    require("ergebnis.txt" in dabei,
            f"die echte Builder-Ausgabe fehlt im Checkpoint: {dabei}")
    aus_dem_kaefig = [d for d in dabei if "jail" in d or "sandbox.sb" in d]
    require(aus_dem_kaefig == [".autopilot/jail/builder.sandbox.sb"]
            or aus_dem_kaefig == [],
            f"unerwartete Kaefigdateien im Checkpoint: {aus_dem_kaefig}")
    # Der NEUE Ort kommt in keinem Fall mit — er liegt gar nicht im Baum.
    require(not any(jail in d for d in dabei),
            f"der neue Kaefigpfad landete im Checkpoint: {dabei}")


def t_0184_the_credential_scan_stays_as_strict_as_before() -> None:
    """Der Umzug des Kaefigs darf den Scan nicht lockern.

    Er ist der Riegel, der ein Token im Baum faengt — und genau der waere die
    Stelle, an der eine bequeme Ignore-Regel still etwas durchliesse.
    """
    werk = _git_werk()
    with open(os.path.join(werk, "auth.json"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"tokens": {"access_token": FAKE_CREDENTIAL}}))
    ergebnis = B.safe_checkpoint(werk, phase="build")
    require(not ergebnis.ok, "ein Kredential im Baum kam durch")
    require(ergebnis.findings, "der Scan nannte keinen Fund")

    # Und im Kaefigordner darf ebenfalls nichts durchrutschen, falls dort doch
    # einmal etwas liegt: der Scan sieht den Baum, nicht den Kaefig — also
    # muss der Kaefig ausserhalb bleiben, und genau das prueft die Zeile oben.
    jail = B._jail_dir(werk)
    require(not os.path.realpath(jail).startswith(os.path.realpath(werk)),
            "der Kaefig liegt im Scanbereich")


# ============================================= 12.-13. Der Kanarienvogel
def t_12_the_canary_sees_a_foreign_credential() -> None:
    """Steht etwas anderes als der Wegwerf-Wert im Draht, ist er rot."""
    ergebnis = CANARY.CanaryResult(
        False, "foreign_credential", requests=1,
        seen_auth=("sk-ant-…(len=40)",))
    require(CANARY.verdict(ergebnis).startswith("cli_canary_failed:"),
            "ein fremder Wert erzeugte kein Sperrurteil")

    # Und die Maskierung: ein gefundener Wert wird nie roh gemeldet.
    require_equal(CANARY._mask(CANARY.PROBE_TOKEN), "PROBE_TOKEN")
    maske = CANARY._mask("sk-ant-oat-echtaussehend-1234567890")
    require(maske.startswith("sk-ant-o") and "1234567890" not in maske,
            f"ein fremder Wert wurde roh gemeldet: {maske}")


def t_13_the_canary_catches_a_bare_regression() -> None:
    """Kein Verkehr am Lauscher heisst rot, nicht still gruen.

    Genau das waere die Regression: ein CLI-Update, nach dem `--bare` die
    Basis-URL ignoriert oder doch die Sitzung nimmt — der Lauscher saehe dann
    gar nichts, und ein nachgiebiger Kanarienvogel haette das „bestanden".
    """
    leer = CANARY.CanaryResult(False, "no_traffic", requests=0)
    require(CANARY.verdict(leer) == "cli_canary_failed:no_traffic",
            "ausbleibender Verkehr wurde nicht als Regression gewertet")
    fehlt = CANARY.CanaryResult(False, "cli_missing")
    require(CANARY.verdict(fehlt) != "",
            "eine fehlende CLI wurde als sicher gewertet")


def t_13c_the_canary_run_itself_calls_no_traffic_red() -> None:
    """Nicht das Urteil, sondern der LAUF.

    Eine Mutation hat gezeigt, dass die beiden Zusicherungen darueber nur
    `verdict()` pruefen — die Stelle, an der `run()` entscheidet, ob es
    ueberhaupt Verkehr gab, war ungestellt. Genau die ist die Regression:
    ein CLI, das die Basis-URL ignoriert, erzeugt keinen Verkehr, und ein
    nachgiebiger Kanarienvogel haette das „bestanden".

    Gestellt wird sie mit einer CLI, die nichts tut — hier `/usr/bin/true`.
    """
    echt = CANARY._claude_binary
    CANARY._claude_binary = lambda: "/usr/bin/true"
    try:
        ergebnis = CANARY.run(timeout=30)
    finally:
        CANARY._claude_binary = echt
    require(not ergebnis.ok,
            f"ein Lauf ohne jeden Verkehr galt als gruen: {ergebnis.as_dict()}")
    require_equal(ergebnis.reason, "no_traffic",
                  f"falscher Grund: {ergebnis.reason}")
    require_equal(ergebnis.requests, 0, "es wurde doch Verkehr gezaehlt")


def t_13d_the_canary_run_accepts_only_the_probe_token() -> None:
    """Und die Gegenprobe am Lauf: mit der echten CLI ist er gruen.

    Sie ist der eigentliche Dauertest — sie misst bei jedem Gate-Lauf nach,
    ob `--bare` noch das tut, was am 2026-09-02 gemessen wurde.
    """
    if not CANARY._claude_binary():
        # Ohne CLI ist die Aussage ungemessen, nicht falsch. Das steht so im
        # Modul, und hier wird nichts beschoenigt.
        return
    ergebnis = CANARY.run()
    require(ergebnis.ok,
            f"der Kanarienvogel ist rot: {ergebnis.as_dict()}")
    require_equal(list(ergebnis.seen_auth), ["PROBE_TOKEN"],
                  f"im Draht stand mehr als der Wegwerf-Wert: "
                  f"{ergebnis.seen_auth}")
    require(ergebnis.requests > 0, "kein Verkehr gemessen")


def t_10c_a_sibling_module_in_the_same_package_cannot_borrow_it() -> None:
    """Die Bindung nennt EIN Modul, nicht das Paket.

    Eine Mutation hat das Praefix auf `solvio.provider_broker.` geweitet —
    und keine Zusicherung merkte es, weil die vorhandene aus `tests.…` leiht
    und dort ohnehin abgewiesen wird. Ein Nachbar IM Paket ist der Fall, den
    nur diese Zeile faengt: `service.py` leitet weiter, `proxy.py` prueft
    Modelle — keiner von beiden hat am Wert etwas verloren.
    """
    import types

    from solvio.secret_vault import context as SC

    store = VaultStore(os.path.join(_SANDBOX, "v-nachbar.sqlite3"))
    VA.add(secret_ref=AN.SECRET_REF, kind=VP.SecretKind.API_KEY,
           plaintext=json.dumps({"kind": AN.KIND_API_KEY,
                                 "token": FAKE_CREDENTIAL}).encode(),
           allowed_capabilities=[AN.CAPABILITY],
           allowed_targets=[AN.UPSTREAM_ORIGIN],
           allowed_executors=[VP.ExecutorId.ANTHROPIC_BROKER],
           allow_background=True, store=store, replace=True)

    # Ein Modul, das SICH im selben Paket nennt — genau die Lage, die ein
    # Praefixvergleich durchliesse.
    nachbar = types.ModuleType("solvio.provider_broker.service")
    exec(compile(
        "def borrow(probe, ref, executor, target):\n"
        "    with probe.use(ref, executor=executor, target=target) as m:\n"
        "        return m.plaintext()\n", "<nachbar>", "exec"),
        nachbar.__dict__)

    probe = VB.SecretBroker(store)
    verweigert = False
    with SC.bound(SC.UseContext(origin=AP.OriginClass.BACKGROUND_AUTOMATION,
                                capability=AN.CAPABILITY,
                                automation_id=AN.AUTOMATION_ID)):
        try:
            nachbar.borrow(probe, AN.SECRET_REF,
                           VP.ExecutorId.ANTHROPIC_BROKER, AN.UPSTREAM_ORIGIN)
        except VB.SecretDenied as exc:
            verweigert = True
            require_equal(exc.reason, VP.Denied.EXECUTOR_MODULE_MISMATCH,
                          f"falscher Grund: {exc.reason}")
    require(verweigert,
            "ein Nachbarmodul im selben Paket durfte die Anmeldung leihen")
    require_equal(VP.EXECUTOR_MODULES[VP.ExecutorId.ANTHROPIC_BROKER],
                  ("solvio.provider_broker.anthropic",),
                  "die Bindung nennt nicht mehr genau ein Modul")


def t_8b_the_child_environment_never_copies_the_parent_key() -> None:
    """Die Ausnahme darf setzen, nicht abschreiben.

    Auch das hat eine Mutation gezeigt: die Zusicherung dafuer stand in einer
    ANDEREN Suite, und diese hier haette einen Elternwert durchgelassen. Eine
    Grenze, die nur woanders geprueft wird, ist hier keine.
    """
    alt = os.environ.get("ANTHROPIC_API_KEY")
    os.environ["ANTHROPIC_API_KEY"] = FAKE_CREDENTIAL
    try:
        env = L.brokered_environment(base_url="http://127.0.0.1:8792",
                                     token="broker-token-y")
    finally:
        if alt is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = alt
    require_equal(env["ANTHROPIC_API_KEY"], "broker-token-y",
                  "der Elternwert kam in die Kindumgebung")
    for name, wert in env.items():
        require(FAKE_CREDENTIAL not in wert,
                f"die echte Anmeldung steht in der Kindumgebung unter {name}")


def t_13b_a_red_canary_blocks_the_writer() -> None:
    """Rot heisst gesperrt — nicht warnen und trotzdem weiterlaufen."""
    schreiber = B.ClaudeWriterBuilder(
        canary_verdict="cli_canary_failed:foreign_credential")
    require_equal(schreiber.capability(), B.BLOCKED_BY_SECURITY_POLICY,
                  "ein roter Kanarienvogel sperrte den Schreiber nicht")
    ergebnis = asyncio.run(schreiber.build(
        B.BuildTask("m", "bau etwas"), tempfile.mkdtemp(dir=_SANDBOX)))
    require_equal(ergebnis.outcome, B.REFUSED,
                  f"der gesperrte Schreiber baute trotzdem: {ergebnis.outcome}")


# ============================================= 14.-16. Kapazitaet und Route
def _ledger(mid: str) -> ST.AutopilotLedger:
    led = ST.AutopilotLedger(os.path.join(tempfile.mkdtemp(dir=_SANDBOX),
                                          "ap.sqlite3"))
    led.create_milestone(C.parse({
        "milestone_id": mid, "version": "1.0.0", "objective": "Z",
        "acceptance_criteria": [{"key": "a", "text": "t",
                                 "evidence_type": "REVIEW_SUPPORTED"}]}))
    return led


def t_14_no_credential_makes_claude_unavailable() -> None:
    led = _ledger("probe-nocred")
    schreiber = B.ClaudeWriterBuilder(canary_verdict="")
    bericht = asyncio.run(CAP.builder_capacity(led, "probe-nocred", schreiber))
    require_equal(bericht.state, CAP.UNAVAILABLE,
                  f"ohne Anmeldung nicht UNAVAILABLE: {bericht.state}")
    require_equal(bericht.signals.get("grund"), "no_credential",
                  f"falscher Grund: {bericht.signals}")


def t_15_quota_is_not_a_repair_attempt() -> None:
    """Kontingent ist eine Lage, kein Fehlversuch — sonst zaehlt es gegen die
    Schleifenbremse und der Failover saehe aus wie ein Reparaturversuch."""
    for text in ("broker sagt token_capped", "HTTP 429 rate limit",
                 "usage limit reached"):
        require(B._quota_signal(text), f"nicht als Kontingent erkannt: {text}")
    require_equal(B._quota_signal("ein gewoehnlicher Fehler"), "",
                  "ein gewoehnlicher Fehler wurde als Kontingent gelesen")

    ergebnis = B.BuildOutcome(B.QUOTA, "claude", detail="provider_quota:429")
    require(ergebnis.quota and not ergebnis.ok,
            "ein Kontingentausfall sieht wie ein Erfolg oder ein Fehler aus")


def t_16_model_fit_is_independent_of_capacity() -> None:
    """Die Qualitaetswahl kennt weder Kontingent noch Sperre.

    Gestellt an drei Lagen: verfuegbar, gesperrt, ohne Anmeldung. MODEL_FIT
    muss dreimal derselbe sein — sonst haette die Lage die Praeferenz gefaerbt,
    und niemand koennte spaeter sagen, was gefehlt hat.
    """
    led = _ledger("probe-fit")
    fits = []
    for schreiber in (B.ClaudeWriterBuilder(canary_verdict=""),
                      B.ClaudeWriterBuilder(canary_verdict="cli_canary_failed:x"),
                      B.SyntheticBuilder()):
        adapter = {"claude": schreiber, "codex": B.CodexBuilder()}
        if schreiber.name != "claude":
            adapter = {"claude": B.ClaudeWriterBuilder(canary_verdict="x"),
                       "codex": B.CodexBuilder()}
        lage = asyncio.run(CAP.preflight(led, "probe-fit", adapter))
        route = RT.decide(trigger=RT.NEW_PHASE, task_kind="implementation",
                          adapters=adapter, options=lage["builder_options"],
                          size=B.MEDIUM)
        fits.append(route.model_fit)
    require_equal(set(fits), {"claude"},
                  f"MODEL_FIT haengt an der Lage: {fits}")


def t_16b_the_route_reason_tells_capacity_from_unavailable() -> None:
    """`capacity` und `unavailable` sind nicht dasselbe.

    Das eine heisst „morgen wieder", das andere „da hilft eine Handlung".
    Beides `capacity` zu nennen waere die Verwechslung, die niemand mehr
    aufloest.

    Die Lage wird GESTELLT, nicht gemessen. `CAP.preflight` startete hier
    frueher zwei echte Anbieter-CLIs und las im HOME des Prozesses die reale
    Abo-Anmeldung des Besitzers; auf einem Rechner ohne angemeldete
    `codex`-CLI blieb `moeglich` leer, und `routing.py:128-130` meldete
    `unavailable`, bevor der Kanarienvogel ueberhaupt zum Zug kam. Der Test
    fiel damit an einer Umgebung, ueber die er nichts behauptet.

    Geprueft ist eine ROUTING-Eigenschaft — `capacity` ist nicht
    `unavailable` —, keine Verfuegbarkeitsaussage. Die Lage zu konstruieren
    ist der einzige Weg, den Zweig ueberhaupt deterministisch zu treffen; das
    Muster steht seit V0.6 in `tests/test_autopilot_builders.py:394`. Beide
    Zusicherungen unten sind unveraendert.
    """
    lage = lambda name, zustand: CAP.CapacityReport(
        ST.ROLE_BUILDER, name, zustand, {}, 1.0)

    adapter = {"claude": B.ClaudeWriterBuilder(canary_verdict=""),
               "codex": B.CodexBuilder()}
    route = RT.decide(trigger=RT.NEW_PHASE, task_kind="implementation",
                      adapters=adapter,
                      options={"codex": lage("codex", CAP.AVAILABLE),
                               "claude": lage("claude", CAP.UNAVAILABLE)},
                      size=B.MEDIUM)
    require_equal(route.reason, RT.UNAVAILABLE,
                  f"fehlende Anmeldung als {route.reason} gemeldet")

    gesperrt = {"claude": B.ClaudeWriterBuilder(
        canary_verdict="cli_canary_failed:x"), "codex": B.CodexBuilder()}
    route2 = RT.decide(trigger=RT.NEW_PHASE, task_kind="implementation",
                       adapters=gesperrt,
                       options={"codex": lage("codex", CAP.AVAILABLE),
                                "claude": lage("claude", CAP.AVAILABLE)},
                       size=B.MEDIUM)
    require_equal(route2.reason, RT.SECURITY_POLICY,
                  f"eine Sperre als {route2.reason} gemeldet")


# ============================================= 17.-18. Handoff und Contract
def t_17_both_writers_read_the_same_handoff() -> None:
    """Ein Context ist ein Context — er gehoert dem Milestone, nicht dem Werkzeug.

    Waere er builderabhaengig, waere ein Failover ein Themenwechsel.
    """
    from solvio.autopilot import context as CTX

    led = _ledger("probe-handoff")
    beleg = led.record_evidence("probe-handoff", kind="test_report",
                                commit="c" * 12, env_fingerprint="f", ok=True,
                                summary="3076/3076")
    paket = CTX.compile_context(led, "probe-handoff", gate_summary="3076/3076",
                                evidence=[beleg], next_task="mach weiter")
    fuer_claude = B.BuildTask("probe-handoff", "mach weiter",
                              context=paket.text).prompt()
    fuer_codex = B.BuildTask("probe-handoff", "mach weiter",
                             context=paket.text).prompt()
    require_equal(fuer_claude, fuer_codex,
                  "die beiden Schreiber bekommen verschiedene Auftraege")
    require("## Auftrag" in fuer_claude and "Verfuegbare Belege" in fuer_claude,
            "dem Handoff fehlen tragende Bloecke")


def t_18_switching_the_builder_changes_neither_contract_nor_evidence() -> None:
    """Der Wechsel ist eine Routenfrage, keine Vertragsfrage."""
    led = _ledger("probe-wechsel")
    vorher = led.milestone("probe-wechsel")
    hash_vorher = vorher.contract_hash
    beleg = led.record_evidence("probe-wechsel", kind="test_report",
                                commit="d" * 12, env_fingerprint="f", ok=True,
                                summary="gruen")

    led.set_fields("probe-wechsel", builder="claude")
    led.set_fields("probe-wechsel", builder="codex")

    nachher = led.milestone("probe-wechsel")
    require_equal(nachher.contract_hash, hash_vorher,
                  "der Builderwechsel hat den Contract veraendert")
    require_equal(nachher.contract_json, vorher.contract_json,
                  "der Contract-Inhalt hat sich veraendert")
    beleg_nachher = led.evidence(beleg)
    require(beleg_nachher is not None and beleg_nachher.ok,
            "die Evidence ueberlebte den Wechsel nicht")
    require_equal(beleg_nachher.commit, "d" * 12,
                  "die Evidence zeigt auf einen anderen Commit")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
