"""Der WISSEN-Schreibweg fuers iPhone — EINE Mutation je Beweis, DURCH den Router.

WARUM ES JETZT EINEN SCHREIBWEG GIBT. `memory_endpoint.py` sagt bis heute:
Lesen beweist das Geraet, Schreiben laeuft ueber den Freigabeweg mit Face ID.
Das war die richtige Haltung, solange jede Mutation nach V1 freigabepflichtig
war. Approval Policy V2 (ADR-0022) hat diese Rechnung geaendert — bewusst und
freigegeben: die Matrixzeile der BEWIESENEN interaktiven App fuehrt
NORMAL_WRITE und CRITICAL direkt aus, weil ein Mensch, der in der attestierten
App auf genau diese eine Handlung tippt, sie damit bereits ausgeloest hat.
Eine Face-ID-Runde obendrauf waere Reibung ohne Sicherheitsgewinn — die
Autoritaet kommt aus der Herkunft, nicht aus der Wiederholung.

WAS DIESER ENDPUNKT IST — UND WAS NICHT. Er ist der Transport, der genau diese
Herkunft BEWEIST: je Anfrage eine frische App-Attest-Assertion ueber eine
Nonce, die der Core selbst ausgegeben hat, gebunden an den SHA-256 der
Handlung. Das ist ein STAERKERER interaktiver Beweis als der Sitzungsbeweis
des Sprachwegs — dort gilt eine Assertion fuer eine ganze Sitzung, hier fuer
genau eine Mutation. Er ist KEINE Abkuerzung: die Entscheidung faellt danach
unveraendert im `CapabilityRouter` an der eingefrorenen Matrix. Der Endpunkt
stuft nichts herab, erzeugt keine Freigabe und umgeht nichts — er liefert dem
Router die Herkunft, die er selbst bewiesen hat, und der Router entscheidet.

DIE LISTE IST GESCHLOSSEN, UND DAS IST STRUKTUR. Genau die fuenf
Wissens-Mutationen, sonst nichts. Ohne diese Liste waere der Endpunkt ein
generischer Capability-RPC — und jede kuenftige Faehigkeit waere vom Telefon
aus erreichbar, ohne dass es jemand entschieden haette. Eine sechste
Faehigkeit kostet hier eine Codezeile und damit ein Review; genau so teuer
soll sie sein.

WAS DER BEWEIS NICHT ZEIGT. App Attest attestiert eine App-Instanz, keine
Person — dieselbe Grenze wie beim Sitzungsbeweis. Deshalb bleibt alles, was
VERY_CRITICAL ist, auch von hier aus biometrisch: nicht durch eine Pruefung in
dieser Datei, sondern weil die Matrix es sagt, durch die jeder Aufruf laeuft.

Angehaengt statt eingebaut: dieselbe TLS, dieselbe Geraetekennung, derselbe
gepinnte Anker — und `gateway.py` weiss nichts davon.
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

log = get_logger("memory_mutation_endpoint")

API = "/v1/memory/mutation"

BINDING_PROTOCOL_VERSION = 1
DOMAIN_MEMORY_MUTATION = b"SOLVIO_MEMORY_MUTATION_V1"
TYPE_MEMORY_MUTATION_BINDING = "memory_mutation_binding"

#: Laenger als die 30 s des Sitzungsbeweises, und das mit Grund: dort
#: ueberbrueckt die Nonce einen Handshake zwischen zwei Maschinen, hier liegt
#: zwischen Challenge und Antwort ein Mensch, der liest, was er gleich tut.
NONCE_TTL = 60.0

#: GESCHLOSSEN. Genau die Wissens-Mutationen, die die App anbietet — jede
#: weitere Faehigkeit braucht eine neue Zeile HIER, nicht nur eine
#: Registrierung im Router. Der Endpunkt darf nie zum generischen RPC werden.
ALLOWED_CAPABILITIES: frozenset[str] = frozenset({
    "memory_forget",
    "memory_correct",
    "memory_confirm_candidate",
    "memory_decline_candidate",
    "memory_purge",
})


def payload_sha256(capability: str, arguments: dict[str, Any]) -> str:
    """Der Hash der Handlung — ueber dieselbe Kanonisierung wie alle Bindungen.

    Der Core rechnet ihn aus dem, was ANKAM, nie aus dem, was die App
    behauptet: ein Argument, das nach dem Signieren veraendert wurde, aendert
    diesen Hash, damit die Bindung, damit den clientDataHash — und die
    Assertion faellt durch.
    """
    return hashlib.sha256(VSP.canonical_bytes(
        {"capability": capability, "arguments": arguments})).hexdigest()


def build_binding(*, core_instance_id: str, device_id: str, nonce: str,
                  payload_sha256: str) -> dict[str, Any]:
    return {
        "protocol_version": BINDING_PROTOCOL_VERSION,
        "type": TYPE_MEMORY_MUTATION_BINDING,
        "core_instance_id": core_instance_id,
        "device_id": device_id,
        "nonce": nonce,
        "payload_sha256": payload_sha256,
    }


def client_data_hash(binding_raw: bytes) -> bytes:
    """clientDataHash fuer `generateAssertion()` — ueber die EXAKTEN Bytes.

    Eigener Domain-Separator: eine Mutations-Assertion kann nie als
    Sitzungs- oder Entscheidungs-Assertion durchgehen und umgekehrt.
    """
    return hashlib.sha256(DOMAIN_MEMORY_MUTATION + b"\x00" + binding_raw).digest()


async def _authed(request: web.Request) -> str | None:
    """Dieselbe Geraetepruefung wie Sprach- und Leseweg — nicht eine dritte.

    Bewusst der Aufruf der bestehenden Funktion und keine Kopie: eine weitere
    Fassung derselben Pruefung waere genau die Stelle, an der spaeter eine
    nachgeschaerft wird und die anderen nicht.
    """
    from solvio.voice_endpoint import _owner_device
    return await _owner_device(request)


def _nonces(request: web.Request) -> VSP.SessionNonces:
    """Ein eigenes Zaehlwerk, nicht das des Sprachwegs.

    Dieselbe Klasse mit derselben Begruendung (einmalig, kurzlebig, an ihr
    Geraet gebunden, in-memory) — aber ein eigener Topf: eine Sprach-Nonce darf
    nie eine Mutation eroeffnen, auch nicht durch einen Buchhaltungsfehler.
    """
    return request.app.setdefault("memory_mutation_nonces",
                                  VSP.SessionNonces(ttl=NONCE_TTL))


def _err(status: int, reason: str) -> web.Response:
    return web.json_response({"error": reason}, status=status)


async def verify_mutation_proof(control_plane, *, device_id: str,
                                core_instance_id: str, nonce: str,
                                payload_digest: str, assertion: bytes) -> bool:
    """Hat die attestierte App-Instanz GENAU DIESE Mutation signiert?

    Spiegelt `voice_session_proof.verify_session_proof` — eingefrorener
    Verifizierer, eingefrorener Geraetestand, jede Absage ein `False` und nie
    eine Ausnahme, die zum 500 wuerde. Der gespeicherte Zaehler ist die
    Frischegrenze und wird bewusst NICHT fortgeschrieben: die Spalte gehoert
    dem eingefrorenen Entscheidungspfad, und gegen Wiedereinspielung schuetzt
    hier die einmalige Nonce, nicht der Zaehler.
    """
    try:
        device = await control_plane.store.get_device(device_id)
    except Exception as exc:  # noqa: BLE001 - ein kaputter Speicher ist kein Beweis
        log.error("memory_mutation.proof_device_unreadable",
                  kind=type(exc).__name__)
        return False
    if device is None:
        return False
    public_hex = device["app_attest_public_key"] or ""
    if not public_hex:
        log.info("memory_mutation.proof_no_attested_key", device=device_id[:12])
        return False
    verifier = getattr(control_plane, "attest_verifier", None)
    if verifier is None:
        log.info("memory_mutation.proof_no_verifier")
        return False
    binding = build_binding(core_instance_id=core_instance_id,
                            device_id=device_id, nonce=nonce,
                            payload_sha256=payload_digest)
    try:
        verifier.verify_assertion(
            assertion=assertion,
            client_data_hash=client_data_hash(VSP.canonical_bytes(binding)),
            public_key_x963=bytes.fromhex(public_hex),
            prev_counter=int(device["app_attest_counter"] or 0))
    except Exception as exc:  # noqa: BLE001 - eine Absage ist ein Ergebnis
        log.info("memory_mutation.proof_rejected", device=device_id[:12],
                 kind=type(exc).__name__)
        return False
    return True


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
    """Eine einzelne Wissens-Mutation — bewiesen, dann DURCH den Router.

    Reihenfolge mit Absicht: erst das Geraet (401), dann die Form (400), dann
    die geschlossene Liste (403, VOR jeder Kryptografie — eine strukturell
    verbotene Faehigkeit soll keinen Beweis kosten und keinen verbrauchen),
    dann Nonce und Assertion (401), und erst zuletzt der Router, der die
    eigentliche Entscheidung faellt.
    """
    device_id = await _authed(request)
    if device_id is None:
        return _err(401, "unauthorized")
    try:
        body = await request.json()
        capability = str(body["capability"])
        arguments = body["arguments"]
        nonce = str(body["nonce"])
        assertion_b64 = str(body["assertion_b64"])
    except Exception:  # noqa: BLE001 - unlesbar ist unlesbar
        return _err(400, "bad_request")
    if (not isinstance(arguments, dict)
            or any(not isinstance(k, str) for k in arguments)):
        return _err(400, "bad_request")
    if capability not in ALLOWED_CAPABILITIES:
        log.warning("memory_mutation.capability_not_allowed",
                    device=device_id[:12], capability=capability[:64])
        return _err(403, "capability_not_allowed")
    try:
        assertion = base64.b64decode(assertion_b64, validate=True)
    except Exception:  # noqa: BLE001 - kein Base64 ist kein Beweis
        return _err(401, "invalid_proof")
    control_plane = request.app.get("control_plane")
    if control_plane is None:
        # Ohne Kontrollebene gibt es keinen Beweis — und damit keinen Schreibweg.
        return _err(401, "invalid_proof")
    core_id = str(getattr(control_plane, "core_instance_id", "") or "")
    # Die Nonce zaehlt genau einmal, und nur fuer das Geraet, das sie bekam —
    # auch ein danach scheiternder Beweis gibt sie nicht zurueck.
    if not _nonces(request).consume(nonce, device_id):
        log.info("memory_mutation.proof_rejected", device=device_id[:12],
                 kind="nonce")
        return _err(401, "invalid_proof")
    ok = await verify_mutation_proof(
        control_plane, device_id=device_id, core_instance_id=core_id,
        nonce=nonce, payload_digest=payload_sha256(capability, arguments),
        assertion=assertion)
    if not ok:
        return _err(401, "invalid_proof")

    server = request.app.get("voice_core_server")
    router = getattr(getattr(server, "dispatcher", None), "capabilities", None)
    if router is None:
        return _err(503, "capabilities_unavailable")

    # DURCH den Router, nicht an ihm vorbei: Autoritaetspruefung, Klassifikation,
    # Matrixentscheidung und Journal laufen unveraendert. Der Endpunkt liefert
    # nur die Herkunft, die er selbst bewiesen hat — die Provenienz ist
    # TRUSTED_CONTEXT, nicht USER_DIRECT: die Kennungen stammen aus der Liste,
    # die der Core der App gezeigt hat, nicht woertlich aus einem Nutzer-Turn.
    result = await router.execute(
        capability, arguments,
        trust=TrustContext(origin_trust=TrustLevel.USER_DIRECT,
                           user_authorized=True,
                           note="deliberate tap in registered app"),
        provenance={k: ArgumentSource.TRUSTED_CONTEXT for k in arguments},
        principal=f"iphone-{device_id[:12]}-wissen",
        origin=P.OriginClass.TRUSTED_INTERACTIVE_APP,
        commanded=True)
    log.info("memory_mutation.dispatched", device=device_id[:12],
             capability=capability, outcome=result.outcome.value,
             call_id=result.call_id)
    return web.json_response({"ok": result.succeeded,
                              "outcome": result.outcome.value,
                              "reason": result.reason,
                              "human_message": result.human_message})


def attach(app: web.Application, server: Any) -> web.Application:
    """Haengt den Schreibweg an die bestehende Anwendung.

    `server` ist der laufende `CoreServer`; gelesen wird nur sein Dispatcher —
    der Weg zum `CapabilityRouter`. Keine Zeile in `gateway.py` aendert sich.
    """
    app["voice_core_server"] = server
    app.router.add_get(f"{API}/challenge", h_challenge)
    app.router.add_post(API, h_mutation)
    log.info("memory_mutation_endpoint.attached", base=API)
    return app
