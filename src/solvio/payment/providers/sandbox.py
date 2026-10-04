"""Der Client zum Pruefanbieter — und die Stelle, an der Ehrlichkeit entsteht.

Dieses Modul beantwortet die eine Frage, an der eine Doppelbuchung haengt:

    Wurde die Anfrage abgesendet, oder nicht?

Die Antwort ist NICHT „es gab einen Fehler". Sie hat drei Werte, und der Client
darf nur den behaupten, den er wirklich weiss:

* **Verbindung kam nie zustande** — DNS, Verbindung abgelehnt. Nichts wurde
  abgesendet, also `ProviderUnavailable`. Das ist Wissen, kein Optimismus.
* **Abgesendet, Antwort unbekannt** — Zeitablauf, abgerissene Verbindung,
  irgendeine andere Ausnahme. `ProviderAmbiguous`. Die Belastung KANN
  stattgefunden haben.
* **Antwort gelesen** — der Anbieter hat gesprochen, und nur dann steht ein
  Ergebnis fest.

Die Asymmetrie ist Absicht und wortgleich zu `SafeExecutionFailure` im
eingefrorenen Ausfuehrungsjournal: alles, was nicht ausdruecklich als „nichts
passiert" bekannt ist, gilt als mehrdeutig.

**Klartext nur auf der Rueckschleife.** Ein Anbieter ausserhalb von
`127.0.0.1`/`::1` muss `https:` sprechen. Der Pruefanbieter darf Klartext, weil
er das Geraet nie verlaesst — und das wird geprueft, nicht angenommen.

**Das Anbietergeheimnis reist als Kopfzeile und sonst nirgends.** Es kommt vom
Aufrufer als geliehener Wert (`SecretMaterial.plaintext()`), steht in keinem
Argument, keinem Freigabetext und keiner Protokollzeile.
"""
from __future__ import annotations

import asyncio
import ipaddress
from typing import Any, Sequence
from urllib.parse import urlsplit

from solvio.logging_setup import get_logger
from solvio.payment.providers import (ChargeStatus, FailureCategory,
                                      ProviderAmbiguous, ProviderCharge,
                                      ProviderError, ProviderHealth,
                                      ProviderQuote, ProviderRefund,
                                      ProviderUnavailable)

log = get_logger("payment")

#: Wie lange auf den VERBINDUNGSAUFBAU gewartet wird. Laeuft er ab, ist sicher
#: nichts abgesendet worden.
CONNECT_TIMEOUT = 5.0

#: Wie lange insgesamt. Laeuft DIESER ab, ist der Ausgang unbekannt.
TOTAL_TIMEOUT = 20.0


def _is_loopback(host: str) -> bool:
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_base_url(base_url: str, *, allow_plaintext_loopback: bool) -> str:
    """Die Adresse des Anbieters. Klartext nur auf der Rueckschleife, und auch
    dort nur, wenn der Anbieter ausdruecklich dafuer gebaut ist."""
    parts = urlsplit(str(base_url or "").strip())
    host = parts.hostname or ""
    if not host:
        raise ProviderError(FailureCategory.PROVIDER_UNAVAILABLE, "no host")
    if parts.scheme == "https":
        return f"{parts.scheme}://{parts.netloc}"
    if parts.scheme == "http" and allow_plaintext_loopback and _is_loopback(host):
        return f"{parts.scheme}://{parts.netloc}"
    raise ProviderError(FailureCategory.PROVIDER_UNAVAILABLE,
                        "provider endpoint must be https")


class SandboxProvider:
    """Spricht mit `scripts/payment_sandbox.py` — ueber echtes HTTP.

    Der Client kennt kein Szenario und keinen Verwaltungsweg. Er kann sein
    eigenes Ergebnis nicht bestellen; wer das koennte, bewiese nichts.
    """

    name = "sandbox"

    def __init__(self, base_url: str, *, secret: str,
                 allow_plaintext_loopback: bool = True) -> None:
        self.base_url = validate_base_url(
            base_url, allow_plaintext_loopback=allow_plaintext_loopback)
        self._secret = secret

    # -- Naht zu aiohttp ------------------------------------------------------
    def _headers(self, idempotency_key: str = "") -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._secret}",
                   "Content-Type": "application/json"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    async def _call(self, method: str, path: str, *, payload: Any = None,
                    params: dict[str, str] | None = None,
                    idempotency_key: str = "",
                    mutating: bool) -> dict[str, Any]:
        """Ein Aufruf. `mutating` entscheidet, was ein Fehler BEDEUTEN darf."""
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=TOTAL_TIMEOUT,
                                        sock_connect=CONNECT_TIMEOUT)
        url = self.base_url + path
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request(
                        method, url, json=payload, params=params,
                        headers=self._headers(idempotency_key)) as response:
                    if response.status == 401:
                        raise ProviderError(FailureCategory.PROVIDER_UNAUTHORIZED,
                                            "provider rejected the credential")
                    if response.status >= 500:
                        # Der Anbieter hat die Anfrage GESEHEN. Bei einer
                        # Belastung heisst das: unbekannt, nicht fehlgeschlagen.
                        if mutating:
                            raise ProviderAmbiguous("server error after send",
                                                    idempotency_key)
                        raise ProviderUnavailable(f"server error {response.status}")
                    body = await response.json()
                    if not isinstance(body, dict):
                        raise ProviderError(FailureCategory.UNKNOWN_RESULT,
                                            "malformed provider response")
                    return body
        except (ProviderError,):
            raise
        except aiohttp.ClientConnectorError as exc:
            # Verbindung kam nie zustande. Das ist die EINZIGE Ausnahme, bei der
            # „nichts ist passiert" wirklich bekannt ist.
            raise ProviderUnavailable(type(exc).__name__) from exc
        except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
            if mutating:
                raise ProviderAmbiguous(type(exc).__name__, idempotency_key) from exc
            raise ProviderUnavailable(type(exc).__name__) from exc

    # -- Die fuenf Verben -----------------------------------------------------
    async def quote(self, *, merchant_id: str, merchant_origin: str,
                    items: Sequence[dict[str, Any]], currency: str,
                    shipping_label: str = "") -> ProviderQuote:
        body = await self._call("POST", "/v1/quote", mutating=False, payload={
            "merchant_id": merchant_id, "merchant_origin": merchant_origin,
            "items": [{"description": i["description"], "quantity": i["quantity"],
                       "unit_amount_minor": i["unit_amount_minor"],
                       "item_id": i.get("item_id", "")} for i in items],
            "currency": currency, "shipping_label": shipping_label})
        extras = tuple((str(k), int(a), str(label))
                       for k, a, label in body.get("extras", []))
        return ProviderQuote(
            quote_ref=str(body.get("quote_ref", "")),
            currency=str(body.get("currency", currency)),
            items_total_minor=int(body.get("items_total_minor", 0)),
            extras=extras, total_minor=int(body.get("total_minor", 0)),
            charged_currency=str(body.get("charged_currency", "")),
            charged_total_minor=int(body.get("charged_total_minor", 0)),
            recurring=bool(body.get("recurring", False)))

    async def charge(self, *, idempotency_key: str, instrument_token: str,
                     merchant_id: str, amount_minor: int, currency: str,
                     quote_ref: str = "", description: str = "") -> ProviderCharge:
        body = await self._call("POST", "/v1/charge", mutating=True,
                                idempotency_key=idempotency_key, payload={
                                    "instrument_token": instrument_token,
                                    "merchant_id": merchant_id,
                                    "amount_minor": int(amount_minor),
                                    "currency": currency, "quote_ref": quote_ref,
                                    "description": description})
        return _charge_from_body(body)

    async def lookup(self, *, idempotency_key: str) -> ProviderCharge | None:
        body = await self._call("GET", "/v1/charge", mutating=False,
                                params={"key": idempotency_key})
        if not body.get("found"):
            return None
        return _charge_from_body(body)

    async def refund(self, *, idempotency_key: str, charge_ref: str,
                     amount_minor: int) -> ProviderRefund:
        body = await self._call("POST", "/v1/refund", mutating=True,
                                idempotency_key=idempotency_key, payload={
                                    "charge_ref": charge_ref,
                                    "amount_minor": int(amount_minor)})
        if "refund_ref" not in body:
            raise ProviderError(FailureCategory.MERCHANT_FAILURE,
                                str(body.get("error", "refund_failed"))[:60])
        return ProviderRefund(refund_ref=str(body["refund_ref"]),
                              amount_minor=int(body.get("amount_minor", 0)),
                              currency=str(body.get("currency", "")),
                              status=str(body.get("status", "succeeded")))

    async def cancel(self, *, idempotency_key: str, charge_ref: str) -> ProviderCharge:
        body = await self._call("POST", "/v1/cancel", mutating=True,
                                idempotency_key=idempotency_key,
                                payload={"charge_ref": charge_ref})
        if body.get("error"):
            raise ProviderError(FailureCategory.MERCHANT_FAILURE,
                                str(body["error"])[:60])
        return _charge_from_body(body)

    async def health(self) -> ProviderHealth:
        try:
            body = await self._call("GET", "/v1/health", mutating=False)
        except ProviderError as exc:
            # Nicht erreichbar ODER nicht autorisiert — in beiden Faellen ist
            # `authorized` FALSCH, denn bestaetigt wurde es nicht. Eine
            # Gesundheitspruefung, die im Zweifel gruen meldet, ist keine.
            return ProviderHealth(reachable=not isinstance(exc, ProviderUnavailable),
                                  authorized=False, detail=exc.category.value)
        return ProviderHealth(reachable=True, authorized=bool(body.get("authorized")),
                              detail="", instruments=tuple(body.get("instruments", ())))


def _charge_from_body(body: dict[str, Any]) -> ProviderCharge:
    """Die Anbieterantwort in eine sichere Form. Unbekanntes wird streng, nie mild."""
    raw_status = str(body.get("status", ""))
    try:
        status = ChargeStatus(raw_status)
    except ValueError:
        # Ein Status, den wir nicht kennen, ist kein Erfolg. Er ist unbekannt.
        raise ProviderError(FailureCategory.UNKNOWN_RESULT,
                            f"unknown status {raw_status[:24]}") from None
    try:
        category = FailureCategory(str(body.get("failure_category", "none")))
    except ValueError:
        category = FailureCategory.UNKNOWN_RESULT
    return ProviderCharge(
        status=status, charge_ref=str(body.get("charge_ref", "")),
        amount_minor=int(body.get("amount_minor", 0)),
        currency=str(body.get("currency", "")),
        charged_currency=str(body.get("charged_currency", "")),
        charged_total_minor=int(body.get("charged_total_minor", 0)),
        failure_category=category, sca_pending=bool(body.get("sca_pending", False)),
        order_ref=str(body.get("order_ref", "")),
        refunded_minor=int(body.get("refunded_minor", 0)))
