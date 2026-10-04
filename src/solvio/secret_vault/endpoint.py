"""Der Tresor-Weg fuers iPhone — EINE Mutation je Beweis, DURCH den Router.

Gebaut nach dem Vorbild des WISSEN-Schreibwegs (`memory_mutation_endpoint.py`),
mit denselben vier Eigenschaften und aus denselben Gruenden: je Anfrage eine
frische App-Attest-Assertion ueber eine Nonce, die der Core selbst ausgegeben
hat, gebunden an den SHA-256 der Handlung; eine GESCHLOSSENE Liste erlaubter
Faehigkeiten; die Nonce zaehlt genau einmal; und die Entscheidung faellt danach
unveraendert im `CapabilityRouter`.

**Ein eigener Endpunkt und keine Zeile mehr im WISSEN-Weg.** Dessen Liste steht
mit der Begruendung da, dass sie nie zum generischen RPC werden darf. Ein Tresor
gehoert nicht in dieselbe Liste wie „Erinnerung vergessen" — er hat einen
eigenen Domaenentrenner, einen eigenen Nonce-Topf und eine eigene Liste, damit
eine Wissens-Assertion nie eine Tresor-Mutation eroeffnen kann und umgekehrt.

**Der Wert reist genau einmal und nie als Argument.** Die erste Anfrage bringt
ihn mit, versiegelt ihn sofort unter dem Hauptschluessel und behaelt nur eine
Einlagerungskennung (`solvio.secret_vault.staging`). Was danach durch Router,
Freigabe, Anzeige, Digest und Journal laeuft, ist diese Kennung. Der Digest der
BINDUNG deckt trotzdem den Wert ab — ueber seinen SHA-256, den das iPhone
mitschickt. Ein Wert, der nach dem Signieren vertauscht wuerde, aendert diesen
Hash und faellt durch.

**Warum es eine zweite Anfrage gibt.** Tresor-Mutationen sind `VERY_CRITICAL`
(ADR-0022) und damit auch aus der attestierten App biometrisch. Der Router
antwortet daher zuerst mit `approval_required`; nach der Face-ID-Runde fragt
die App dieselbe Handlung erneut — diesmal ohne den Wert, nur mit der
Einlagerungskennung. Der Router erkennt die inzwischen freigegebene Anfrage
wieder und setzt sie fort. Der Wert wartet in dieser Zeit versiegelt im
Arbeitsspeicher, nicht auf der Platte.

**Was der Beweis nicht zeigt.** App Attest attestiert eine App-Instanz, keine
Person — dieselbe Grenze wie ueberall. Deshalb bleibt alles Biometrische
biometrisch, und zwar nicht durch eine Pruefung in dieser Datei, sondern weil
die Matrix es sagt, durch die jeder Aufruf laeuft.
"""
from __future__ import annotations

import base64
import hashlib
from typing import Any

from aiohttp import web

from solvio import voice_session_proof as VSP
from solvio.capabilities import policy as P
from solvio.capabilities.contract import ArgumentSource
from solvio.contracts.trust import TrustContext, TrustLevel
from solvio.logging_setup import get_logger
from solvio.secret_vault import health as VH
from solvio.secret_vault.staging import StagingError

log = get_logger("vault_endpoint")

API = "/v1/vault"

BINDING_PROTOCOL_VERSION = 1
DOMAIN_VAULT_MUTATION = b"SOLVIO_VAULT_MUTATION_V1"
TYPE_VAULT_MUTATION_BINDING = "vault_mutation_binding"

#: Wie lange eine Nonce gilt. Dieselben 60 Sekunden wie beim WISSEN-Weg und aus
#: demselben Grund: zwischen Challenge und Antwort steht ein Mensch, der liest.
NONCE_TTL = 60.0

#: Wie gross eine Anfrage an diesen Endpunkt hoechstens sein darf. Die
#: Anwendung selbst begrenzt auf 64 kB (`gateway.build_app`); hier ist die
#: engere Grenze, weil ein Zugang klein ist und alles Groessere ein
#: Missverstaendnis oder ein Versuch waere.
MAX_BODY = 32 * 1024

#: GESCHLOSSEN. Genau die Tresor-Mutationen, die die App anbietet. Eine siebte
#: Faehigkeit kostet hier eine Zeile und damit ein Review; genau so teuer soll
#: sie sein.
#:
#: Dieser Satz stimmte eine Zeit lang NICHT: `secret_rescope` stand hier,
#: waehrend die App sie nie anbot — ein Umfang liess sich dadurch ueberhaupt
#: nicht aendern, obwohl alles danach aussah. Seit 2026-09-03 gibt es den
#: Bildschirm dazu, und der Satz ist wieder wahr.
ALLOWED_CAPABILITIES: frozenset[str] = frozenset({
    "secret_add",
    "secret_replace",
    "secret_disable",
    "secret_enable",
    "secret_delete",
    "secret_rescope",
    "google_connect",
})

#: Welche davon einen Wert mitbringen. Der Rest kommt ohne aus, und ein Wert
#: waere dort ein Fehler, kein Extra.
NEEDS_VALUE: frozenset[str] = frozenset({"secret_add", "secret_replace", "google_connect"})


def payload_sha256(capability: str, arguments: dict[str, Any],
                   secret_sha256: str) -> str:
    """Der Hash der Handlung — ueber dieselbe Kanonisierung wie alle Bindungen.

    Der Core rechnet ihn aus dem, was ANKAM, nie aus dem, was die App behauptet.
    Der Wert selbst geht als SHA-256 ein und nicht im Klartext: die Bindung
    deckt ihn damit ab, ohne ihn zu tragen.
    """
    return hashlib.sha256(VSP.canonical_bytes({
        "capability": capability,
        "arguments": arguments,
        "secret_sha256": secret_sha256,
    })).hexdigest()


def build_binding(*, core_instance_id: str, device_id: str, nonce: str,
                  payload_sha256: str) -> dict[str, Any]:
    return {
        "protocol_version": BINDING_PROTOCOL_VERSION,
        "type": TYPE_VAULT_MUTATION_BINDING,
        "core_instance_id": core_instance_id,
        "device_id": device_id,
        "nonce": nonce,
        "payload_sha256": payload_sha256,
    }


def client_data_hash(binding_raw: bytes) -> bytes:
    """clientDataHash fuer `generateAssertion()` — ueber die EXAKTEN Bytes.

    Eigener Domaenentrenner: eine Tresor-Assertion kann nie als Wissens-,
    Sitzungs- oder Entscheidungs-Assertion durchgehen und umgekehrt.
    """
    return hashlib.sha256(DOMAIN_VAULT_MUTATION + b"\x00" + binding_raw).digest()


async def _authed(request: web.Request) -> str | None:
    """Dieselbe Geraetepruefung wie Sprach-, Lese- und WISSEN-Weg — keine vierte."""
    from solvio.voice_endpoint import _owner_device
    return await _owner_device(request)


def _nonces(request: web.Request) -> VSP.SessionNonces:
    """Ein eigenes Zaehlwerk. Eine Wissens-Nonce darf nie eine Tresor-Mutation
    eroeffnen, auch nicht durch einen Buchhaltungsfehler."""
    return request.app.setdefault("vault_mutation_nonces",
                                  VSP.SessionNonces(ttl=NONCE_TTL))


def _err(status: int, reason: str) -> web.Response:
    return web.json_response({"error": reason}, status=status)


def _capabilities(request: web.Request):
    return request.app.get("secret_vault_capabilities")


async def verify_mutation_proof(control_plane, *, device_id: str,
                                core_instance_id: str, nonce: str,
                                payload_digest: str, assertion: bytes) -> bool:
    """Hat die attestierte App-Instanz GENAU DIESE Mutation signiert?

    Spiegelt den WISSEN-Weg Zeile fuer Zeile: eingefrorener Verifizierer,
    eingefrorener Geraetestand, jede Absage ein `False` und nie eine Ausnahme,
    die zum 500 wuerde. Der gespeicherte Zaehler ist die Frischegrenze und wird
    bewusst NICHT fortgeschrieben — die Spalte gehoert dem eingefrorenen
    Entscheidungspfad, und gegen Wiedereinspielung schuetzt hier die einmalige
    Nonce.
    """
    try:
        device = await control_plane.store.get_device(device_id)
    except Exception as exc:  # noqa: BLE001 - ein kaputter Speicher ist kein Beweis
        log.error("vault_mutation.proof_device_unreadable", kind=type(exc).__name__)
        return False
    if device is None:
        return False
    public_hex = device["app_attest_public_key"] or ""
    if not public_hex:
        log.info("vault_mutation.proof_no_attested_key", device=device_id[:12])
        return False
    verifier = getattr(control_plane, "attest_verifier", None)
    if verifier is None:
        log.info("vault_mutation.proof_no_verifier")
        return False
    binding = build_binding(core_instance_id=core_instance_id, device_id=device_id,
                            nonce=nonce, payload_sha256=payload_digest)
    try:
        verifier.verify_assertion(
            assertion=assertion,
            client_data_hash=client_data_hash(VSP.canonical_bytes(binding)),
            public_key_x963=bytes.fromhex(public_hex),
            prev_counter=int(device["app_attest_counter"] or 0))
    except Exception as exc:  # noqa: BLE001 - eine Absage ist ein Ergebnis
        log.info("vault_mutation.proof_rejected", device=device_id[:12],
                 kind=type(exc).__name__)
        return False
    return True


# ---------------------------------------------------------------------- Lesen
async def h_entries(request: web.Request) -> web.Response:
    """Was im Tresor liegt — Beschreibung, nie ein Wert.

    Diese Antwort ist die Datenquelle des Tresor-Bildschirms. Sie enthaelt
    ausschliesslich, was `store.describe_row` freigibt; die Spalten mit
    Geheimtext kommen dort nicht vor, und sie koennen hier deshalb auch nicht
    versehentlich mitkommen.
    """
    if await _authed(request) is None:
        return _err(401, "unauthorized")
    caps = _capabilities(request)
    if caps is None:
        return _err(503, "vault_unavailable")
    word, reason = VH.assess()
    from solvio.secret_vault.broker import SecretBroker
    entries = SecretBroker(caps.store).catalogue()

    # Die Namen, die es ueberhaupt gibt. Ohne sie koennte die App einen Umfang
    # nur um das erweitern, was schon drinsteht — und `document_find` steht
    # eben noch nicht drin. Freitext waere die Alternative gewesen: ein
    # Tippfehler ergaebe dort eine Berechtigung, die niemand je benutzt, oder
    # schlimmer eine, die jemand anderes benutzt.
    #
    # Kein Geheimnis: Faehigkeitsnamen stehen ohnehin in jedem Werkzeugkatalog.
    server = request.app.get("voice_core_server")
    router = getattr(getattr(server, "dispatcher", None), "capabilities", None)
    bekannt = sorted(router.names()) if router is not None else []
    return web.json_response({
        "zustand": word,
        "grund": reason,
        "zugaenge": entries,
        "bekannte_faehigkeiten": bekannt,
    })


async def h_google(request: web.Request) -> web.Response:
    """Authenticated setup metadata only; no token use, exchange or activation."""
    if await _authed(request) is None:
        return _err(401, "unauthorized")
    caps = _capabilities(request)
    if caps is None:
        return _err(503, "vault_unavailable")
    try:
        preview = caps.google_connection.preview()
    except Exception:
        return _err(503, "google_mobile_setup_unavailable")
    return web.json_response(preview, headers={"Cache-Control": "no-store"})


async def h_ledger(request: web.Request) -> web.Response:
    """Die Zugriffsspur. Zeitpunkt, Verweis, Executor, Ziel — nie ein Wert."""
    if await _authed(request) is None:
        return _err(401, "unauthorized")
    caps = _capabilities(request)
    if caps is None:
        return _err(503, "vault_unavailable")
    ref = str(request.query.get("verweis") or "")
    rows = caps.store.ledger(secret_ref=ref, limit=50)
    return web.json_response({"spur": rows})


# ------------------------------------------------------------------ Schreiben
async def h_challenge(request: web.Request) -> web.Response:
    """Eine frische Nonce fuer genau eine Mutation — an ihr Geraet gebunden."""
    device_id = await _authed(request)
    if device_id is None:
        return _err(401, "unauthorized")
    control_plane = request.app.get("control_plane")
    core_id = str(getattr(control_plane, "core_instance_id", "") or "")
    if control_plane is None or not core_id:
        return _err(503, "approval_control_unavailable")
    return web.json_response({"nonce": _nonces(request).issue(device_id),
                              "core_instance_id": core_id})


async def h_mutation(request: web.Request) -> web.Response:
    """Eine einzelne Tresor-Mutation — bewiesen, dann DURCH den Router.

    Die Reihenfolge ist dieselbe wie beim WISSEN-Weg und aus demselben Grund:
    erst das Geraet (401), dann die Form (400), dann die geschlossene Liste
    (403, VOR jeder Kryptografie — eine strukturell verbotene Faehigkeit soll
    keinen Beweis kosten und keinen verbrauchen), dann Nonce und Assertion
    (401), und erst zuletzt der Router, der die eigentliche Entscheidung faellt.

    Was hier NICHT passiert: der Anfragekoerper wird nie protokolliert, nie in
    eine Fehlermeldung gestellt und nie zurueckgegeben. Jeder Fehlerpfad
    antwortet mit einer Kennung aus einer geschlossenen Liste.
    """
    device_id = await _authed(request)
    if device_id is None:
        return _err(401, "unauthorized")
    if (request.content_length or 0) > MAX_BODY:
        return _err(413, "too_large")
    try:
        body = await request.json()
        capability = str(body["capability"])
        arguments = body["arguments"]
        nonce = str(body["nonce"])
        assertion_b64 = str(body["assertion_b64"])
        secret_sha256 = str(body.get("secret_sha256") or "")
        secret_b64 = str(body.get("secret_b64") or "")
        staging_id = str(body.get("staging_id") or "")
    except Exception:  # noqa: BLE001 - unlesbar ist unlesbar, und der Koerper
        # wird ausdruecklich NICHT mitgeteilt: in ihm koennte ein Wert stehen.
        return _err(400, "bad_request")
    if (not isinstance(arguments, dict)
            or any(not isinstance(k, str) for k in arguments)
            or any(not isinstance(v, str) for v in arguments.values())):
        # Flach und nur Zeichenketten — dieselbe Form auf beiden Seiten der
        # Bindung. Eine verschachtelte Angabe waere eine zweite Kanonisierung.
        return _err(400, "bad_request")
    if capability not in ALLOWED_CAPABILITIES:
        log.warning("vault_mutation.capability_not_allowed",
                    device=device_id[:12], capability=capability[:64])
        return _err(403, "capability_not_allowed")
    if "staging_id" in arguments:
        # Die Kennung setzt der Core, nicht die App. Sonst koennte eine Anfrage
        # eine fremde Einlagerung anfordern.
        return _err(400, "bad_request")

    caps = _capabilities(request)
    if caps is None:
        return _err(503, "vault_unavailable")

    try:
        assertion = base64.b64decode(assertion_b64, validate=True)
    except Exception:  # noqa: BLE001 - kein Base64 ist kein Beweis
        return _err(401, "invalid_proof")
    control_plane = request.app.get("control_plane")
    if control_plane is None:
        return _err(401, "invalid_proof")
    core_id = str(getattr(control_plane, "core_instance_id", "") or "")

    # Die Nonce zaehlt genau einmal, und nur fuer das Geraet, das sie bekam —
    # auch ein danach scheiternder Beweis gibt sie nicht zurueck.
    if not _nonces(request).consume(nonce, device_id):
        log.info("vault_mutation.proof_rejected", device=device_id[:12], kind="nonce")
        return _err(401, "invalid_proof")
    digest = payload_sha256(capability, arguments, secret_sha256)
    if not await verify_mutation_proof(control_plane, device_id=device_id,
                                       core_instance_id=core_id, nonce=nonce,
                                       payload_digest=digest, assertion=assertion):
        return _err(401, "invalid_proof")

    # -- Der Wert. Ab hier, und nur hier, gibt es ihn ueberhaupt. -----------
    needs_value = capability in NEEDS_VALUE
    if not needs_value and (secret_b64 or staging_id or secret_sha256):
        # Ein Wert bei einer Handlung, die keinen braucht, ist ein Fehler und
        # kein Extra.
        return _err(400, "bad_request")
    if needs_value:
        if secret_b64:
            try:
                raw = base64.b64decode(secret_b64, validate=True)
            except Exception:  # noqa: BLE001
                return _err(400, "bad_request")
            if not raw or hashlib.sha256(raw).hexdigest() != secret_sha256:
                # Der Wert passt nicht zu dem, was signiert wurde.
                return _err(400, "bad_request")
            try:
                staging_id = caps.staging.stage(raw, device_id=device_id,
                                                payload_sha256=secret_sha256)
            except StagingError as exc:
                log.info("vault_mutation.staging_refused", kind=type(exc).__name__,
                         reason=str(exc)[:40])
                return _err(503, "vault_unavailable")
            finally:
                del raw
            caps.device_for_staging[staging_id] = device_id
        elif staging_id:
            try:
                if caps.staging.peek(staging_id, device_id=device_id) != secret_sha256:
                    return _err(400, "bad_request")
            except StagingError:
                return _err(409, "staging_expired")
        else:
            return _err(400, "bad_request")
        arguments = {**arguments, "staging_id": staging_id}

    server = request.app.get("voice_core_server")
    router = getattr(getattr(server, "dispatcher", None), "capabilities", None)
    if router is None:
        return _err(503, "capabilities_unavailable")

    # DURCH den Router, nicht an ihm vorbei: Autoritaetspruefung, Klassifikation,
    # Matrixentscheidung und Journal laufen unveraendert. Der Endpunkt liefert
    # nur die Herkunft, die er selbst bewiesen hat — die Provenienz ist
    # TRUSTED_CONTEXT, nicht USER_DIRECT: die Angaben stammen aus der Liste, die
    # der Core der App gezeigt hat, nicht woertlich aus einem Nutzer-Turn.
    result = await router.execute(
        capability, arguments,
        trust=TrustContext(origin_trust=TrustLevel.USER_DIRECT,
                           user_authorized=True,
                           note="deliberate tap in registered app"),
        provenance={k: ArgumentSource.TRUSTED_CONTEXT for k in arguments},
        principal=f"iphone-{device_id[:12]}-tresor",
        origin=P.OriginClass.TRUSTED_INTERACTIVE_APP,
        commanded=True)

    payload: dict[str, Any] = {
        "ok": result.succeeded,
        "outcome": result.outcome.value,
        "reason": result.reason,
        "human_message": result.human_message,
    }
    if result.outcome.value == "approval_required":
        # Die Einlagerung bleibt stehen, bis der Mensch entschieden hat. Die
        # App bekommt ihre Kennung zurueck und fragt nach der Face-ID-Runde
        # dieselbe Handlung erneut — dann ohne den Wert.
        payload["request_id"] = str((result.data or {}).get("request_id") or "")
        if staging_id:
            payload["staging_id"] = staging_id
    elif staging_id and not result.succeeded:
        # Endgueltig gescheitert: die Einlagerung soll nicht ihre Frist absitzen.
        caps.staging.drop(staging_id)
        caps.device_for_staging.pop(staging_id, None)
    log.info("vault_mutation.dispatched", device=device_id[:12],
             capability=capability, outcome=result.outcome.value,
             call_id=result.call_id)
    return web.json_response(payload)


def attach(app: web.Application, server: Any, capabilities: Any) -> web.Application:
    """Haengt den Tresor-Weg an die bestehende Anwendung.

    Angehaengt statt eingebaut: dieselbe TLS, dieselbe Geraetekennung, derselbe
    gepinnte Anker — und keine Zeile in `gateway.py` aendert sich.
    """
    app["voice_core_server"] = server
    app["secret_vault_capabilities"] = capabilities
    app.router.add_get(f"{API}/google", h_google)
    app.router.add_get(f"{API}/entries", h_entries)
    app.router.add_get(f"{API}/ledger", h_ledger)
    app.router.add_get(f"{API}/mutation/challenge", h_challenge)
    app.router.add_post(f"{API}/mutation", h_mutation)
    log.info("vault_endpoint.attached", base=API)
    return app
