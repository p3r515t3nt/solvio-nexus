"""NodeClient - transport-only mTLS-Client zu einem solvio-node (STEP 21.2E).

Eigenschaften:
  * persistente aiohttp-Session (Keep-Alive, PHASE 7) -> TLS-Handshake amortisiert
  * mTLS: verifiziert den Server gegen die SOLVIO-CA und sendet das Client-Cert
  * Identitaet ueber Cert-DNS-SAN (server_hostname = erwartete node_id), NICHT IP
    (PHASE 27). Die IP ist nur Netzwerkadresse.
  * prueft protocol_version, node_id und request_id-Echo jeder Antwort (PHASE 6)
  * payload-freie Fehler; es wird NIE Request-/Result-INHALT geloggt (nur Metadaten)
  * lazy: SSL-Context/Session entstehen erst beim ersten Aufruf (PHASE 23)

KEINE generische Shell: invoke() adressiert ausschliesslich benannte Capabilities
ueber deren feste Route. Es gibt keine Methode fuer beliebige Kommandos.
"""
from __future__ import annotations

import asyncio
import ssl
from dataclasses import dataclass
from typing import Any

import aiohttp

from solvio.logging_setup import get_logger
from solvio.nodes.config import NodeConnectionConfig
from solvio.nodes.errors import (
    NodeBusyError,
    NodeCapabilityError,
    NodeConfigError,
    NodeIdentityError,
    NodeProtocolError,
    NodeRequestMismatchError,
    NodeTimeoutError,
    NodeTLSError,
    NodeUnavailableError,
    UnknownCapabilityError,
)
from solvio.nodes.models import (
    PROTOCOL_VERSION,
    CapabilityDescriptor,
    HealthReport,
    NodeRequest,
    NodeResponse,
    ResponseStatus,
)

_log = get_logger("solvio.nodes.client")

# Node-ErrorCode -> Exception-Klasse (Capability-Fehler = sauber beantwortet).
_ERROR_CODE_EXC: dict[str, type[NodeCapabilityError]] = {
    "UNKNOWN_CAPABILITY": UnknownCapabilityError,
    "RESOURCE_BUSY": NodeBusyError,
}


@dataclass
class RawResponse:
    status: int
    body: Any | None


class NodeClient:
    """mTLS-Client fuer genau einen Knoten. Nicht threadsafe; pro Event-Loop nutzen."""

    def __init__(self, config: NodeConnectionConfig) -> None:
        self.config = config
        self._session: aiohttp.ClientSession | None = None

    # -- SSL / Session (lazy) ------------------------------------------------
    def _build_ssl(self) -> ssl.SSLContext:
        missing = self.config.tls.missing()
        if missing:
            # Pfadnamen sind keine Secrets, aber wir halten die Meldung generisch.
            raise NodeConfigError(f"tls material missing ({len(missing)} file(s))")
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.check_hostname = True
        ctx.load_verify_locations(self.config.tls.ca_cert)
        ctx.load_cert_chain(self.config.tls.client_cert, self.config.tls.client_key)
        return ctx

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            ssl_ctx = self._build_ssl()
            connector = aiohttp.TCPConnector(
                ssl=ssl_ctx,
                limit=self.config.pool_limit,
                limit_per_host=self.config.pool_limit,
                keepalive_timeout=self.config.keepalive_timeout_s,
            )
            timeout = aiohttp.ClientTimeout(
                total=self.config.request_timeout_s,
                connect=self.config.connect_timeout_s,
            )
            self._session = aiohttp.ClientSession(connector=connector, timeout=timeout)
        return self._session

    async def _raw_request(self, method: str, path: str, *, json_body: dict | None = None) -> RawResponse:
        """Die einzige Netz-Naht. Unit-Tests ueberschreiben genau diese Methode."""
        session = await self._ensure_session()
        url = self.config.endpoint.rstrip("/") + path
        try:
            async with session.request(
                method, url, json=json_body, server_hostname=self.config.identity
            ) as resp:
                try:
                    body: Any | None = await resp.json(content_type=None)
                except Exception:
                    body = None
                return RawResponse(resp.status, body)
        except asyncio.TimeoutError:
            raise NodeTimeoutError("node request timed out") from None
        except aiohttp.ClientConnectorCertificateError:
            raise NodeTLSError("server certificate verification failed") from None
        except aiohttp.ClientConnectorSSLError:
            raise NodeTLSError("tls handshake failed") from None
        except ssl.SSLError:
            raise NodeTLSError("tls error") from None
        except aiohttp.ClientError:
            raise NodeUnavailableError("node connection failed") from None

    # -- Identitaets-/Protokollpruefung -------------------------------------
    def _verify_identity(self, node_id: str | None) -> None:
        if node_id != self.config.node_id:
            raise NodeIdentityError("response node_id does not match expected node")

    def _verify_protocol(self, version: int | None) -> None:
        if version != self.config.protocol_version:
            raise NodeProtocolError("incompatible protocol version")

    # -- Oeffentliche API ----------------------------------------------------
    async def health(self) -> HealthReport:
        raw = await self._raw_request("GET", "/v1/health")
        if raw.status != 200 or not isinstance(raw.body, dict):
            raise NodeProtocolError(f"unexpected health response (status {raw.status})")
        report = HealthReport.model_validate(raw.body)
        self._verify_identity(report.node_id)
        self._verify_protocol(report.protocol_version)
        return report

    async def list_capabilities(self) -> list[CapabilityDescriptor]:
        raw = await self._raw_request("GET", "/v1/capabilities")
        if raw.status != 200 or not isinstance(raw.body, dict):
            raise NodeProtocolError(f"unexpected capabilities response (status {raw.status})")
        self._verify_identity(raw.body.get("node_id"))
        self._verify_protocol(raw.body.get("protocol_version"))
        return [CapabilityDescriptor.model_validate(c) for c in raw.body.get("capabilities", [])]

    async def get_capability(self, capability_id: str) -> CapabilityDescriptor:
        raw = await self._raw_request("GET", f"/v1/capabilities/{capability_id}")
        if raw.status == 404:
            raise UnknownCapabilityError("no such capability", error_code="UNKNOWN_CAPABILITY")
        if raw.status != 200 or not isinstance(raw.body, dict):
            raise NodeProtocolError(f"unexpected descriptor response (status {raw.status})")
        return CapabilityDescriptor.model_validate(raw.body)

    async def invoke(self, capability_id: str, payload: dict[str, Any] | None = None) -> NodeResponse:
        """Ruft GENAU eine benannte Capability auf. Keine beliebigen Kommandos."""
        req = NodeRequest(
            target_node_id=self.config.node_id,
            capability=capability_id,
            payload=payload or {},
        )
        raw = await self._raw_request(
            "POST",
            f"/v1/capabilities/{capability_id}/invoke",
            json_body=req.model_dump(exclude_none=True),
        )
        if not isinstance(raw.body, dict):
            if raw.status == 413:
                raise NodeCapabilityError("payload too large", error_code="PAYLOAD_TOO_LARGE")
            raise NodeProtocolError(f"malformed node response (status {raw.status})")
        resp = NodeResponse.model_validate(raw.body)
        self._verify_protocol(resp.protocol_version)
        self._verify_identity(resp.node_id)
        if resp.request_id != req.request_id:
            raise NodeRequestMismatchError("response request_id does not match request")
        if resp.status is ResponseStatus.ERROR:
            raise self._map_error(resp)
        _log.debug(
            "node.invoke",
            node_id=self.config.node_id,
            capability=capability_id,
            status=resp.status.value,
            duration_ms=resp.duration_ms,
        )
        return resp

    # -- durable background jobs (STEP 21.2G) --------------------------------
    async def submit_job(self, capability_id, payload, *, privacy_class,
                         idempotency_key, max_attempts=3):
        body = {"capability_id": capability_id, "payload": payload or {},
                "privacy_class": privacy_class, "idempotency_key": idempotency_key,
                "max_attempts": max_attempts}
        raw = await self._raw_request("POST", "/v1/jobs", json_body=body)
        if raw.status in (200, 201) and isinstance(raw.body, dict):
            return raw.body
        raise self._job_error(raw)

    async def get_job(self, job_id):
        raw = await self._raw_request("GET", f"/v1/jobs/{job_id}")
        if raw.status == 200 and isinstance(raw.body, dict):
            return raw.body
        raise self._job_error(raw)

    async def get_job_result(self, job_id):
        raw = await self._raw_request("GET", f"/v1/jobs/{job_id}/result")
        if raw.status == 200 and isinstance(raw.body, dict):
            return raw.body
        raise self._job_error(raw)

    async def cancel_job(self, job_id):
        raw = await self._raw_request("POST", f"/v1/jobs/{job_id}/cancel", json_body={})
        if raw.status == 200 and isinstance(raw.body, dict):
            return raw.body
        raise self._job_error(raw)

    def _job_error(self, raw) -> "NodeCapabilityError":
        code = raw.body.get("error_code") if isinstance(raw.body, dict) else None
        return NodeCapabilityError(code or f"job_http_{raw.status}",
                                   error_code=code or f"HTTP_{raw.status}")

    @staticmethod
    def _map_error(resp: NodeResponse) -> NodeCapabilityError:
        cls = _ERROR_CODE_EXC.get(resp.error_code or "", NodeCapabilityError)
        # error_message ist per Node-Protokoll payload-frei.
        return cls(resp.error_message or "node capability error", error_code=resp.error_code)

    # -- Lebenszyklus --------------------------------------------------------
    async def aclose(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __aenter__(self) -> "NodeClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()
