"""Der Zahlungsweg fuers iPhone — EINE Handlung je Beweis, DURCH den Router.

Gebaut nach dem Vorbild des Tresor-Weges (`secret_vault/endpoint.py`), mit
denselben Eigenschaften und aus denselben Gruenden: je Anfrage eine frische
App-Attest-Assertion ueber eine Nonce, die der Core selbst ausgegeben hat,
gebunden an den SHA-256 der Handlung; eine GESCHLOSSENE Liste erlaubter
Faehigkeiten; die Nonce zaehlt genau einmal; und die Entscheidung faellt danach
unveraendert im `CapabilityRouter`.

**Ein eigener Domaenentrenner, und das ist kein Detail.** `SOLVIO_PAYMENT_MUTATION_V1`
steht neben `SOLVIO_VAULT_MUTATION_V1`, `SOLVIO_MEMORY_MUTATION_V1`,
`SOLVIO_VOICE_SESSION_V1` und den beiden des Freigabepfades. Eine Wissens- oder
Tresor-Assertion kann damit strukturell keine Zahlung eroeffnen — nicht weil es
verboten waere, sondern weil der `clientDataHash` ein anderer ist.

**Ein eigener Nonce-Topf.** Aus demselben Grund: eine Nonce, die fuer eine
Wissensmutation ausgegeben wurde, darf keine Geldbewegung tragen, auch nicht
durch einen Buchhaltungsfehler.

**Der Anbieter-Token reist eingelagert.** Was als Argument reist, steht im
Freigabetext, im Autorisierungs-Digest und dauerhaft in der Freigabe-Datenbank.
Ein Anbieter-Token ist zwar kein Geheimnis — allein bewegt er keinen Cent —,
aber er gehoert genauso wenig dorthin wie ein Passwort. Er kommt einmal herein,
wird sofort versiegelt und bekommt eine Wegwerfkennung.

**Was der Beweis nicht zeigt.** App Attest attestiert eine App-Instanz, keine
Person. Deshalb bleibt jede Geldbewegung zusaetzlich biometrisch — nicht durch
eine Pruefung in dieser Datei, sondern weil die Matrix sie als VERY_CRITICAL
fuehrt und jeder Aufruf durch sie laeuft.
"""
from __future__ import annotations

import base64
import hashlib
import time
from typing import Any

from aiohttp import web

from solvio import voice_session_proof as VSP
from solvio.capabilities import policy as P
from solvio.capabilities.contract import ArgumentSource
from solvio.contracts.trust import TrustContext, TrustLevel
from solvio.logging_setup import get_logger
from solvio.payment.intent import PaymentState

log = get_logger("payment_endpoint")

API = "/v1/payment"

BINDING_PROTOCOL_VERSION = 1
DOMAIN_PAYMENT_MUTATION = b"SOLVIO_PAYMENT_MUTATION_V1"
TYPE_PAYMENT_MUTATION_BINDING = "payment_mutation_binding"

#: Wie lange eine Nonce gilt. Dieselben 60 Sekunden wie bei den anderen
#: attestierten Wegen und aus demselben Grund: dazwischen steht ein Mensch,
#: der liest.
NONCE_TTL = 60.0

#: Wie gross eine Anfrage hoechstens sein darf. Die Anwendung selbst begrenzt
#: auf 64 kB; hier ist die engere Grenze, weil eine Zahlung klein ist.
MAX_BODY = 32 * 1024

#: GESCHLOSSEN. Genau die Handlungen, die die App anbietet. Eine weitere kostet
#: hier eine Zeile und damit ein Review; genau so teuer soll sie sein.
#:
#: `bank_transfer` und `invest_order` stehen ausdruecklich NICHT dabei. Sie sind
#: als Namen reserviert und in diesem Milestone nicht gebaut — ein reservierter
#: Name ohne Route ist die billigste Art, eine Zusicherung zu geben.
ALLOWED_CAPABILITIES: frozenset[str] = frozenset({
    "purchase_place",
    "purchase_cancel",
    "refund_request",
    "payment_method_add",
    "payment_method_remove",
    "payment_method_disable",
    "payment_method_enable",
    "payment_method_rescope",
    "payment_limit_raise",
    "payment_limit_lower",
})

#: Die einzige Handlung, die einen Wert mitbringt: den Anbieter-Token.
NEEDS_VALUE = frozenset({"payment_method_add"})

#: Zustaende, die auf einen Menschen warten. `EXECUTING` steht ausdruecklich mit
#: dabei: ein Vorgang, der beim Absturz zwischen Anspruch und Antwort stehen
#: blieb, ist NICHT erledigt — er ist der gefaehrlichste Zustand ueberhaupt,
#: weil die Belastung stattgefunden haben kann. Der erste Bau hat ihn nirgends
#: gelesen; gefunden in der kalten Abnahme.
OFFENE_ZUSTAENDE = (PaymentState.READY_FOR_APPROVAL,
                    PaymentState.EXECUTING,
                    PaymentState.RECONCILIATION_REQUIRED,
                    PaymentState.AWAITING_SCA)

#: Der EINZIGE offene Zustand, dessen Frist mitzaehlt.
#:
#: Die drei anderen sind ausdruecklich AUSGENOMMEN, und das ist keine
#: Nachlaessigkeit: `EXECUTING`, `RECONCILIATION_REQUIRED` und `AWAITING_SCA`
#: warten auf einen Menschen, weil eine Belastung stattgefunden haben KANN.
#: Sie nach Fristablauf aus der Liste zu nehmen waere genau der Fehler, den die
#: kalte Abnahme oben schon einmal gefunden hat — nur schlimmer, weil er dann
#: eine moegliche Zahlung verschwinden liesse.
FRISTGEBUNDEN = (PaymentState.READY_FOR_APPROVAL,)


def _ist_offen(intent: Any, jetzt: float) -> bool:
    """Braucht dieser Vorgang noch eine Handlung — oder ist er nur noch Geschichte?

    Ein abgelaufener `READY_FOR_APPROVAL` ist KEINE offene Handlung mehr. Er
    kann nicht mehr ausgefuehrt werden (`capabilities/payment.py` weist ihn ab),
    und ihn trotzdem anzubieten heisst, einen Menschen nach Face ID fuer etwas
    zu fragen, das nie wirken kann.

    Gefunden in der Live-Abnahme von DEBT-0126 (2026-08-30): ein Vorgang vom
    27. August stand drei Tage spaeter noch als „offen" in der App und fuehrte
    zu vier Freigabekarten hintereinander.
    """
    if intent.state not in OFFENE_ZUSTAENDE:
        return False
    if intent.state in FRISTGEBUNDEN and intent.is_expired(jetzt):
        return False
    return True


def payload_sha256(capability: str, arguments: dict[str, Any],
                   token_sha256: str = "") -> str:
    """Der Digest, den die Assertion abdeckt: Faehigkeit, Argumente, Wert.

    Der Token reist als Einlagerungskennung, aber sein SHA-256 geht in die
    Bindung ein: ein nach dem Signieren vertauschter Token aendert diesen Hash,
    damit die Bindung, damit den `clientDataHash` — und die Assertion faellt
    durch.
    """
    return hashlib.sha256(VSP.canonical_bytes({
        "capability": capability,
        "arguments": arguments,
        "token_sha256": token_sha256,
    })).hexdigest()


def build_binding(*, core_instance_id: str, device_id: str, nonce: str,
                  payload_sha256: str) -> dict[str, Any]:
    return {
        "protocol_version": BINDING_PROTOCOL_VERSION,
        "type": TYPE_PAYMENT_MUTATION_BINDING,
        "core_instance_id": core_instance_id,
        "device_id": device_id,
        "nonce": nonce,
        "payload_sha256": payload_sha256,
    }


def client_data_hash(binding_raw: bytes) -> bytes:
    """clientDataHash fuer `generateAssertion()` — ueber die EXAKTEN Bytes."""
    return hashlib.sha256(DOMAIN_PAYMENT_MUTATION + b"\x00" + binding_raw).digest()


async def _authed(request: web.Request) -> str | None:
    """Dieselbe Geraetepruefung wie Sprach-, Wissens- und Tresor-Weg — keine fuenfte."""
    from solvio.voice_endpoint import _owner_device
    return await _owner_device(request)


def _nonces(request: web.Request) -> VSP.SessionNonces:
    return request.app.setdefault("payment_mutation_nonces",
                                  VSP.SessionNonces(ttl=NONCE_TTL))


def _err(status: int, reason: str) -> web.Response:
    return web.json_response({"error": reason}, status=status)


def _capabilities(request: web.Request):
    return request.app.get("payment_capabilities")


async def verify_mutation_proof(control_plane, *, device_id: str,
                                core_instance_id: str, nonce: str,
                                payload_digest: str, assertion: bytes) -> bool:
    """Hat die attestierte App-Instanz GENAU DIESE Handlung signiert?

    Spiegelt den Tresor-Weg Zeile fuer Zeile: eingefrorener Verifizierer,
    eingefrorener Geraetestand, jede Absage ein `False` und nie eine Ausnahme,
    die zum 500 wuerde. Der gespeicherte Zaehler ist die Frischegrenze und wird
    bewusst NICHT fortgeschrieben — die Spalte gehoert dem eingefrorenen
    Entscheidungspfad, und gegen Wiedereinspielung schuetzt hier die einmalige
    Nonce.
    """
    try:
        device = await control_plane.store.get_device(device_id)
    except Exception as exc:  # noqa: BLE001 - ein kaputter Speicher ist kein Beweis
        log.error("payment_mutation.proof_device_unreadable", kind=type(exc).__name__)
        return False
    if device is None:
        return False
    public_hex = device["app_attest_public_key"] or ""
    if not public_hex:
        log.info("payment_mutation.proof_no_attested_key", device=device_id[:12])
        return False
    verifier = getattr(control_plane, "attest_verifier", None)
    if verifier is None:
        log.info("payment_mutation.proof_no_verifier")
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
        log.info("payment_mutation.proof_rejected", device=device_id[:12],
                 kind=type(exc).__name__)
        return False
    return True


# ---------------------------------------------------------------------- Lesen
async def h_methods(request: web.Request) -> web.Response:
    """Die hinterlegten Zahlungsmittel — Beschreibung, nie Zahlungsmaterial.

    Diese Antwort ist die Datenquelle des Zahlungsbildschirms. Sie enthaelt
    ausschliesslich, was `Instrument.describe` freigibt; der Anbieter-Token und
    die Tresor-Verweise kommen dort nicht vor und koennen deshalb auch nicht
    versehentlich mitkommen.
    """
    if await _authed(request) is None:
        return _err(401, "unauthorized")
    caps = _capabilities(request)
    if caps is None:
        return _err(503, "payment_unavailable")
    try:
        entries = [i.describe(for_model=False) for i in caps.store.instruments()]
    except Exception as exc:  # noqa: BLE001
        log.error("payment_endpoint.methods_failed", kind=type(exc).__name__)
        return _err(503, "payment_unavailable")
    return web.json_response({"zahlungsmittel": entries})


async def h_intents(request: web.Request) -> web.Response:
    """Die offenen und juengsten Vorgaenge — sichere Sicht, keine Anbieterantwort."""
    if await _authed(request) is None:
        return _err(401, "unauthorized")
    caps = _capabilities(request)
    if caps is None:
        return _err(503, "payment_unavailable")
    try:
        intents = caps.store.intents(limit=25)
    except Exception as exc:  # noqa: BLE001
        log.error("payment_endpoint.intents_failed", kind=type(exc).__name__)
        return _err(503, "payment_unavailable")
    from solvio.payment.intent import format_amount
    jetzt = time.time()
    out = []
    for intent in intents:
        view = intent.safe_view()
        # JEDER Vorgang traegt DIESELBEN Schluessel, auch der unbewertete.
        # Sonst faellt ein einziger Entwurf ohne Betrag der App beim Lesen der
        # ganzen Liste um die Ohren — samt der Vorgaenge, die ein Mensch
        # aufloesen muesste. Gefunden in der kalten Abnahme.
        view["betrag_lesbar"] = (format_amount(intent.quote.total_minor,
                                               intent.quote.currency)
                                 if intent.quote is not None else "")
        view["pruefsumme"] = (intent.economic_digest()
                              if intent.quote is not None else "")
        view["offen"] = _ist_offen(intent, jetzt)
        out.append(view)
    return web.json_response({"vorgaenge": out})


async def h_ledger(request: web.Request) -> web.Response:
    """Das Zahlungsbuch — sichere Wahrheit, nie Zahlungsmaterial."""
    if await _authed(request) is None:
        return _err(401, "unauthorized")
    caps = _capabilities(request)
    if caps is None:
        return _err(503, "payment_unavailable")
    try:
        rows = caps.store.ledger(limit=50)
    except Exception as exc:  # noqa: BLE001
        log.error("payment_endpoint.ledger_failed", kind=type(exc).__name__)
        return _err(503, "payment_unavailable")
    # `id` steht ausdruecklich mit dabei. Ohne ihn scheitert die App beim Lesen
    # der Liste — und zwar STILL: sie faengt den Fehler, behaelt den alten Stand
    # und zeigt einen leeren, gesund aussehenden Verlauf. Gefunden in der kalten
    # Abnahme.
    return web.json_response({"eintraege": [
        {k: row[k] for k in ("id", "at", "payment_intent_id", "event",
                             "merchant_id", "description", "amount_minor",
                             "currency", "instrument_ref", "status",
                             "failure_category", "refunded_minor")}
        for row in rows]})


async def h_challenge(request: web.Request) -> web.Response:
    """Eine frische Nonce fuer genau eine Handlung — an ihr Geraet gebunden."""
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
    """Eine einzelne Zahlungshandlung — bewiesen, dann DURCH den Router.

    Die Reihenfolge ist dieselbe wie beim Tresor-Weg und aus demselben Grund:
    erst das Geraet (401), dann die Form (400), dann die geschlossene Liste
    (403, VOR jeder Kryptografie — eine strukturell verbotene Faehigkeit soll
    keinen Beweis kosten und keinen verbrauchen), dann Nonce und Assertion
    (401), und erst zuletzt der Router, der die eigentliche Entscheidung faellt.

    Was hier NICHT passiert: der Anfragekoerper wird nie protokolliert, nie in
    eine Fehlermeldung gestellt und nie zurueckgegeben.
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
        token_sha256 = str(body.get("token_sha256") or "")
        token_b64 = str(body.get("token_b64") or "")
        staging_id = str(body.get("staging_id") or "")
    except Exception:  # noqa: BLE001 - unlesbar ist unlesbar
        return _err(400, "bad_request")
    if (not isinstance(arguments, dict)
            or any(not isinstance(k, str) for k in arguments)
            or any(not isinstance(v, str) for v in arguments.values())):
        # Flach und nur Zeichenketten — dieselbe Form auf beiden Seiten der
        # Bindung. Eine verschachtelte Angabe waere eine zweite Kanonisierung.
        return _err(400, "bad_request")
    if capability not in ALLOWED_CAPABILITIES:
        log.warning("payment_mutation.capability_not_allowed",
                    device=device_id[:12], capability=capability[:64])
        return _err(403, "capability_not_allowed")
    if "vorgang" in arguments and capability in NEEDS_VALUE:
        # Die Einlagerungskennung setzt der Core, nicht die App. Sonst koennte
        # eine Anfrage eine fremde Einlagerung anfordern.
        return _err(400, "bad_request")

    caps = _capabilities(request)
    if caps is None:
        return _err(503, "payment_unavailable")

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
        log.info("payment_mutation.proof_rejected", device=device_id[:12], kind="nonce")
        return _err(401, "invalid_proof")
    digest = payload_sha256(capability, arguments, token_sha256)
    if not await verify_mutation_proof(control_plane, device_id=device_id,
                                       core_instance_id=core_id, nonce=nonce,
                                       payload_digest=digest, assertion=assertion):
        return _err(401, "invalid_proof")

    # -- Der Anbieter-Token. Ab hier, und nur hier, gibt es ihn ueberhaupt. --
    needs_value = capability in NEEDS_VALUE
    if not needs_value and (token_b64 or staging_id or token_sha256):
        return _err(400, "bad_request")
    if needs_value:
        from solvio.secret_vault.staging import StagingError
        if token_b64:
            try:
                raw = base64.b64decode(token_b64, validate=True)
            except Exception:  # noqa: BLE001
                return _err(400, "bad_request")
            if not raw or hashlib.sha256(raw).hexdigest() != token_sha256:
                return _err(400, "bad_request")
            try:
                staging_id = caps.staging.stage(raw, device_id=device_id,
                                                payload_sha256=token_sha256)
            except StagingError as exc:
                log.info("payment_mutation.staging_refused", kind=type(exc).__name__)
                return _err(503, "payment_unavailable")
            finally:
                del raw
            # Das Lager bindet den Wert an dieses Geraet. Der Handler laeuft im
            # Router und kennt kein Geraet — ohne diese Zeile koennte er ihn nie
            # abholen. Wortgleich zum Tresor-Weg und aus demselben Grund.
            caps.device_for_staging[staging_id] = device_id
        elif staging_id:
            try:
                if caps.staging.peek(staging_id, device_id=device_id) != token_sha256:
                    return _err(400, "bad_request")
            except StagingError:
                return _err(409, "staging_expired")
        else:
            return _err(400, "bad_request")
        arguments = {**arguments, "vorgang": staging_id}

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
        principal=f"iphone-{device_id[:12]}-zahlung",
        origin=P.OriginClass.TRUSTED_INTERACTIVE_APP,
        commanded=True)

    payload: dict[str, Any] = {
        "ok": result.succeeded,
        "outcome": result.outcome.value,
        "reason": result.reason,
        "human_message": result.human_message,
    }
    if result.outcome.value == "approval_required":
        payload["request_id"] = str((result.data or {}).get("request_id") or "")
        if staging_id:
            payload["staging_id"] = staging_id
    elif staging_id and not result.succeeded:
        caps.staging.drop(staging_id)
        caps.device_for_staging.pop(staging_id, None)
    if result.succeeded and isinstance(result.data, dict):
        payload["ergebnis"] = result.data
    log.info("payment_mutation.dispatched", device=device_id[:12],
             capability=capability, outcome=result.outcome.value,
             call_id=result.call_id)
    return web.json_response(payload)


def attach(app: web.Application, server: Any, capabilities: Any) -> web.Application:
    """Haengt den Zahlungsweg an die bestehende Anwendung.

    Angehaengt statt eingebaut: dieselbe TLS, dieselbe Geraetekennung, derselbe
    gepinnte Anker — und keine Zeile in `gateway.py` aendert sich.
    """
    app["voice_core_server"] = server
    app["payment_capabilities"] = capabilities
    app.router.add_get(f"{API}/methods", h_methods)
    app.router.add_get(f"{API}/intents", h_intents)
    app.router.add_get(f"{API}/ledger", h_ledger)
    app.router.add_get(f"{API}/mutation/challenge", h_challenge)
    app.router.add_post(f"{API}/mutation", h_mutation)
    log.info("payment_endpoint.attached", base=API)
    return app
