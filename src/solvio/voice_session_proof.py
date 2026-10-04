"""Der Sitzungsbeweis des iPhones — App Attest fuer eine Sprachsitzung.

WARUM ES IHN GIBT. Die Transportkennung des Telefons ist ein STATISCHES
Bearer-Geheimnis: gespeichert wird nur ihr Hash, und jeder Code, der sie
besitzt, authentifiziert sich damit identisch. Solange sie nur Lesen und das
Abholen einer Freigabe erlaubte, war das vertretbar — die Autoritaet lag
ohnehin woanders, naemlich in der Face-ID-Signatur.

Approval Policy V2 aendert diese Rechnung. Die iPhone-Zeile der Matrix
REDUZIERT Freigabepflichten, bis hin zur Haustuer. Damit waere ein gestohlenes
statisches Geheimnis (Backup-Auszug, Keychain-Leck) auf einmal der direkte Weg
zum Schloss — ohne Face ID, ohne Geraet, ohne Menschen.

Deshalb traegt die reduzierte Zeile einen zweiten, frischen Beweis: eine
App-Attest-Assertion ueber eine Nonce, die der Core selbst ausgegeben hat. Sie
zeigt, dass die Sitzung von genau der attestierten App-Instanz auf dem
eingeschriebenen Geraet geoeffnet wurde.

WAS SIE NICHT ZEIGT, und das steht hier, damit es niemand spaeter verwechselt:
sie beweist NICHT, dass die App im Vordergrund ist, nicht, dass ein Mensch
tippt, und schon gar nicht, WER spricht. App Attest attestiert eine
App-Instanz, keine Person. Sie ist damit **nicht** Face ID — und genau deshalb
bleibt VERY_CRITICAL auch mit ihr biometrisch.

FAIL-DOWN STATT FAIL-CLOSED. Eine Sitzung ohne Beweis wird nicht abgewiesen.
Sie faellt auf die Raum-Zeile zurueck: voll benutzbar, und fuer alles
Folgenreiche mit genau dem Face-ID-Verhalten von V1. Eine aeltere App verliert
dadurch nichts, was sie heute hat.

Die Pruefung selbst benutzt den eingefrorenen Verifizierer. Neu ist hier nur
die Bindung — eigener Domain-Separator, damit dieselbe Assertion nie fuer einen
anderen Zweck gelten kann.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass

from solvio.logging_setup import get_logger

log = get_logger("voice_endpoint")

BINDING_PROTOCOL_VERSION = 1
DOMAIN_VOICE_SESSION = b"SOLVIO_VOICE_SESSION_V1"
TYPE_VOICE_SESSION_BINDING = "voice_session_binding"

#: Wie lange eine ausgegebene Nonce gilt. Kurz: sie ueberbrueckt einen
#: Handshake, keinen Gespraechsverlauf.
NONCE_TTL = 30.0

#: Wie lange der Core auf die Antwort wartet, bevor die Sitzung ohne Beweis
#: weiterlaeuft. Ein Telefon, das nicht antwortet, soll kein stummes Gespraech
#: bekommen — es soll ein gewoehnliches bekommen.
PROOF_TIMEOUT = 3.0


def build_binding(*, core_instance_id: str, device_id: str,
                  session_nonce: str, conversation_id: str = "") -> dict:
    """Die Bindung der Sitzungs-Assertion.

    N8/C3: `conversation_id` kommt NUR hinzu, wenn die App einen Chat verlangt —
    die Bytes einer alten App ohne Chatwunsch bleiben unveraendert, und eine
    Assertion ueber einen Chat gilt nie fuer eine Sitzung ohne ihn.
    """
    binding = {
        "protocol_version": BINDING_PROTOCOL_VERSION,
        "type": TYPE_VOICE_SESSION_BINDING,
        "core_instance_id": core_instance_id,
        "device_id": device_id,
        "session_nonce": session_nonce,
    }
    if conversation_id:
        binding["conversation_id"] = conversation_id
    return binding


def canonical_bytes(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def client_data_hash(binding_raw: bytes) -> bytes:
    """clientDataHash fuer `generateAssertion()` — ueber die EXAKTEN Bytes.

    Domain-separiert: eine Assertion fuer eine Sprachsitzung kann nie als
    Entscheidungs-Assertion durchgehen und umgekehrt.
    """
    return hashlib.sha256(DOMAIN_VOICE_SESSION + b"\x00" + binding_raw).digest()


def new_nonce() -> str:
    return secrets.token_hex(16)


@dataclass
class _Issued:
    device_id: str
    issued_at: float


class SessionNonces:
    """Ausgegebene Nonces — einmalig, kurzlebig, an ihr Geraet gebunden.

    In-Memory ist hier richtig und nicht bequem: eine Nonce ueberlebt keinen
    Verbindungsaufbau, und was einen Neustart ueberleben muesste, ist der
    Widerruf — der liegt dauerhaft im eingefrorenen Speicher.
    """

    def __init__(self, ttl: float = NONCE_TTL) -> None:
        self._open: dict[str, _Issued] = {}
        self._ttl = ttl

    def issue(self, device_id: str, *, now: float | None = None) -> str:
        stamp = time.monotonic() if now is None else now
        self._expire(stamp)
        nonce = new_nonce()
        self._open[nonce] = _Issued(device_id, stamp)
        return nonce

    def _expire(self, now: float) -> None:
        stale = [n for n, i in self._open.items() if now - i.issued_at > self._ttl]
        for nonce in stale:
            self._open.pop(nonce, None)

    def consume(self, nonce: str, device_id: str, *,
                now: float | None = None) -> bool:
        """Einmal und nur einmal — und nur fuer das Geraet, das sie bekam.

        Genau hier stirbt der Wiedereinspielungsangriff: eine mitgeschnittene
        Assertion traegt die Nonce ihrer eigenen Sitzung, und die ist verbraucht.
        """
        stamp = time.monotonic() if now is None else now
        self._expire(stamp)
        issued = self._open.pop(nonce or "", None)
        if issued is None:
            return False
        return issued.device_id == device_id


async def verify_session_proof(control_plane, *, device_id: str,
                               core_instance_id: str, session_nonce: str,
                               assertion: bytes, conversation_id: str = "") -> bool:
    """Ist diese Sitzung von der attestierten App-Instanz geoeffnet worden?

    Benutzt den eingefrorenen Verifizierer und den eingefrorenen Geraetestand.
    Jede Absage ist ein `False` und ein Journaleintrag — nie eine Ausnahme, die
    ein Gespraech kippt, und nie eine Auskunft an das Geraet darueber, WAS
    genau fehlte.
    """
    try:
        device = await control_plane.store.get_device(device_id)
    except Exception as exc:  # noqa: BLE001
        log.error("voice_endpoint.proof_device_unreadable", kind=type(exc).__name__)
        return False
    if device is None:
        return False
    public_hex = device["app_attest_public_key"] or ""
    if not public_hex:
        log.info("voice_endpoint.proof_no_attested_key", device=device_id[:12])
        return False
    verifier = getattr(control_plane, "attest_verifier", None)
    if verifier is None:
        log.info("voice_endpoint.proof_no_verifier")
        return False
    binding = build_binding(core_instance_id=core_instance_id, device_id=device_id,
                            session_nonce=session_nonce, conversation_id=conversation_id)
    try:
        verifier.verify_assertion(
            assertion=assertion,
            client_data_hash=client_data_hash(canonical_bytes(binding)),
            public_key_x963=bytes.fromhex(public_hex),
            # Der gespeicherte Zaehler ist die Frischegrenze. Er wird hier
            # bewusst NICHT fortgeschrieben: der eingefrorene Entscheidungspfad
            # besitzt diese Spalte, und ein Sprachweg hat in seiner Transaktion
            # nichts zu suchen. Gegen Wiedereinspielung schuetzt die einmalige
            # Nonce, nicht der Zaehler.
            prev_counter=int(device["app_attest_counter"] or 0))
    except Exception as exc:  # noqa: BLE001 - eine Absage ist ein Ergebnis
        log.info("voice_endpoint.proof_rejected", device=device_id[:12],
                 kind=type(exc).__name__)
        return False
    return True
