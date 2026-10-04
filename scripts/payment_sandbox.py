#!/usr/bin/env python3
"""Der Pruefanbieter — ein ECHTER Dienst, kein Attrappenobjekt.

Warum ein eigener Prozess mit echtem HTTP statt einer Klasse mit vorgegebenen
Rueckgaben: weil genau die Dinge, die bei einer Zahlung schiefgehen, zwischen
zwei Prozessen passieren und nicht in einem. Ein Aufruf, der belastet und dann
die Verbindung verliert. Eine Idempotenzkennung, die den zweiten Versuch auf den
ersten Vorgang zeigen laesst. Eine Bank, die eine Bestaetigung verlangt. Ein
Attrappenobjekt kann keinen dieser Faelle beweisen, weil es sie nur behauptet.

Der Dienst spricht die Sprache, die ein echter tokenisierter Anbieter spricht:

    POST /v1/quote            der verbindliche Endbetrag
    POST /v1/charge           belasten, mit `Idempotency-Key`
    GET  /v1/charge?key=...   nachschlagen statt raten
    POST /v1/refund           zurueck auf DASSELBE Zahlungsmittel
    POST /v1/cancel           die Bestellung stornieren
    POST /v1/sca/complete     der Mensch hat in der Bank-App bestaetigt
    GET  /v1/health           gefahrlos, ohne Buchung

Autorisiert wird mit `Authorization: Bearer <Anbieterzugang>`, und der Dienst
kennt DREI Rollen mit wirklich verschiedener Befugnis:

    charge   belasten, erstatten, stornieren, nachschlagen, bewerten
    refund   erstatten, stornieren, nachschlagen, bewerten — NICHT belasten
    read     nachschlagen, bewerten, Gesundheit

Dass sie sich unterscheiden, ist hier durchgesetzt und nicht bloss behauptet:
ein Pruefdienst, der alle drei auf denselben Wert legt, beweist die Trennung
nicht. Die Werte kommen aus dem Tresor und werden vom Zahlungs-Executor
gesetzt; sie stehen in keinem Argument und in keinem Freigabetext.

**Der Szenarienschalter ist bewusst getrennt.** `/admin/scenario` verlangt ein
ANDERES Geheimnis als der Zahlungsweg. Koennte der Executor sein eigenes
Ergebnis bestellen, bewiese ein gruener Lauf nur, dass er sich selbst glaubt.

Kein echtes Geld, keine echte Karte, keine echte Bank. Die Token sehen aus wie
Token und sind keine.

    python3 scripts/payment_sandbox.py --port 8795
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sys
import time
from typing import Any

from aiohttp import web

#: Szenarien, die der Dienst spielen kann. Genau diese, keine Freitextregel.
SCENARIOS = (
    "normal",                 # alles laeuft
    "price_drift",            # die Kasse nennt einen anderen Endbetrag
    "declined",               # die Bank lehnt ab, NICHTS wurde belastet
    "sca_required",           # der Mensch muss in der Bank-App bestaetigen
    "merchant_failed",        # der Haendler bricht ab
    "ambiguous_after_charge", # belastet und dann die Verbindung verloren
    "slow",                   # antwortet spaet, aber vollstaendig
    "unauthorized",           # das Anbietergeheimnis gilt nicht mehr
    "unreachable",            # der Anbieter antwortet gar nicht
)

#: Kennungen, die ein Zahlungsmittel beim Anbieter hat. Sie sehen aus wie
#: Anbieter-Token und sind keine — sie oeffnen nichts und bezeichnen nur.
KNOWN_TOKENS = {
    "pm_sandbox_solvio_shopping": {"last4": "4242", "kind": "virtual_card",
                                   "active": True},
    "pm_sandbox_revoked": {"last4": "0002", "kind": "virtual_card",
                           "active": False},
}

#: Was der Testladen kostet. Der Dienst rechnet selbst — er uebernimmt NIE die
#: Summe des Aufrufers. Genau das ist der Punkt von §12.
SHIPPING_MINOR = 499
TAX_RATE_PERMILLE = 0   # Bruttopreise; die Steuer steckt bereits im Artikelpreis


class Sandbox:
    def __init__(self, *, provider_secret: str, admin_secret: str,
                 refund_secret: str = "", read_secret: str = "") -> None:
        self.provider_secret = provider_secret
        #: Der Zugang, der Geld ZURUECKGEHEN laesst — erstatten, stornieren,
        #: nachschlagen. Er kann NICHT belasten, und das ist hier wirklich so
        #: durchgesetzt und nicht bloss behauptet: ein Dienst, der beide Rollen
        #: auf denselben Wert legt, beweist die Trennung nicht.
        self.refund_secret = refund_secret or provider_secret
        #: Der lesende Zugang. Nur nachschlagen, bewerten, Gesundheit.
        self.read_secret = read_secret or provider_secret
        self.admin_secret = admin_secret
        self.scenario = "normal"
        #: Idempotenzkennung -> Vorgang. Der ganze Kern der Doppelbuchungsfrage.
        self.charges: dict[str, dict[str, Any]] = {}
        self.quotes: dict[str, dict[str, Any]] = {}
        self.refunds: dict[str, dict[str, Any]] = {}
        #: Wie oft `/v1/charge` mit einer NEUEN Kennung eine Buchung angelegt hat.
        self.charge_attempts = 0
        self.created_charges = 0

    # -- Autorisierung -------------------------------------------------------
    #: Was welcher Zugang darf. Abschliessend, absteigend nach Befugnis.
    ROLES = {"charge": ("quote", "charge", "refund", "cancel", "lookup", "health"),
             "refund": ("quote", "refund", "cancel", "lookup", "health"),
             "read": ("quote", "lookup", "health")}

    def role_of(self, request: web.Request) -> str:
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return ""
        value = header[7:]
        # Die staerkste Rolle zuerst: sind zwei Werte gleich (wie in einer
        # nachlaessigen Einrichtung), gewinnt die staerkere und der Test faellt
        # NICHT faelschlich gruen aus.
        if secrets.compare_digest(value, self.provider_secret):
            return "charge"
        if secrets.compare_digest(value, self.refund_secret):
            return "refund"
        if secrets.compare_digest(value, self.read_secret):
            return "read"
        return ""

    def may(self, request: web.Request, action: str) -> bool:
        return action in self.ROLES.get(self.role_of(request), ())

    def bearer_ok(self, request: web.Request) -> bool:
        return bool(self.role_of(request))

    def admin_ok(self, request: web.Request) -> bool:
        header = request.headers.get("X-Sandbox-Admin", "")
        return bool(header) and secrets.compare_digest(header, self.admin_secret)


def _json(payload: dict[str, Any], status: int = 200) -> web.Response:
    return web.json_response(payload, status=status)


async def _guard(box: Sandbox, request: web.Request,
                 action: str) -> web.Response | None:
    if box.scenario == "unreachable":
        # Verbindung zu, ohne Antwort. Der Aufrufer sieht keinen Statuscode.
        raise web.HTTPServiceUnavailable(reason="sandbox is playing unreachable")
    if box.scenario == "unauthorized" or not box.may(request, action):
        return _json({"error": "unauthorized"}, status=401)
    if box.scenario == "slow":
        await asyncio.sleep(2.0)
    return None


async def handle_health(request: web.Request) -> web.Response:
    box: Sandbox = request.app["box"]
    if box.scenario == "unreachable":
        raise web.HTTPServiceUnavailable(reason="sandbox is playing unreachable")
    authorized = box.scenario != "unauthorized" and box.may(request, "health")
    return _json({"ok": True, "authorized": authorized,
                  "role": box.role_of(request),
                  "instruments": sorted(t for t, v in KNOWN_TOKENS.items() if v["active"]),
                  "scenario": box.scenario})


async def handle_quote(request: web.Request) -> web.Response:
    box: Sandbox = request.app["box"]
    denied = await _guard(box, request, "quote")
    if denied is not None:
        return denied
    body = await request.json()
    items = body.get("items") or []
    if not items:
        return _json({"error": "no_items"}, status=400)
    # DER DIENST RECHNET. Was der Aufrufer als Summe mitschickt, wird ignoriert.
    items_total = sum(int(i["quantity"]) * int(i["unit_amount_minor"]) for i in items)
    if box.scenario == "price_drift":
        items_total += 250          # die Kasse ist teurer geworden
    extras = [["shipping", SHIPPING_MINOR, "Versand"]]
    total = items_total + sum(e[1] for e in extras)
    quote_ref = "q_" + secrets.token_hex(8)
    box.quotes[quote_ref] = {"total_minor": total, "currency": body.get("currency", "EUR")}
    return _json({"quote_ref": quote_ref, "currency": body.get("currency", "EUR"),
                  "items_total_minor": items_total, "extras": extras,
                  "total_minor": total, "recurring": False})


async def handle_charge(request: web.Request) -> web.Response:
    box: Sandbox = request.app["box"]
    denied = await _guard(box, request, "charge")
    if denied is not None:
        return denied
    key = request.headers.get("Idempotency-Key", "")
    if not key:
        return _json({"error": "idempotency_key_required"}, status=400)
    body = await request.json()

    # DIE IDEMPOTENZ. Dieselbe Kennung gibt DENSELBEN Vorgang zurueck — sie legt
    # keinen zweiten an, egal wie oft gefragt wird.
    box.charge_attempts += 1
    existing = box.charges.get(key)
    if existing is not None:
        return _json(dict(existing, replayed=True))

    token = str(body.get("instrument_token", ""))
    known = KNOWN_TOKENS.get(token)
    if known is None or not known["active"]:
        return _json({"status": "declined", "failure_category": "instrument_unusable",
                      "charge_ref": ""}, status=200)

    amount = int(body.get("amount_minor", 0))
    currency = str(body.get("currency", "EUR"))
    quote_ref = str(body.get("quote_ref", ""))
    quoted = box.quotes.get(quote_ref)
    if quoted is not None and quoted["total_minor"] != amount:
        # Der Anbieter belastet, was er genannt hat — nie, was jemand behauptet.
        return _json({"status": "merchant_failed",
                      "failure_category": "merchant_failure",
                      "charge_ref": "", "detail": "amount does not match the quote"})

    charge_ref = "ch_" + secrets.token_hex(8)
    record: dict[str, Any] = {
        "status": "succeeded", "charge_ref": charge_ref, "amount_minor": amount,
        "currency": currency, "failure_category": "none", "sca_pending": False,
        "order_ref": "ord_" + secrets.token_hex(6), "refunded_minor": 0,
        "charged_currency": "", "charged_total_minor": 0,
    }
    if box.scenario == "declined":
        record.update(status="declined", failure_category="declined",
                      charge_ref="", order_ref="")
    elif box.scenario == "merchant_failed":
        record.update(status="merchant_failed", failure_category="merchant_failure",
                      charge_ref="", order_ref="")
    elif box.scenario == "sca_required":
        record.update(status="sca_required", failure_category="sca_required",
                      sca_pending=True)

    box.charges[key] = record
    if record["status"] == "succeeded":
        box.created_charges += 1

    if box.scenario == "ambiguous_after_charge":
        # DER WICHTIGE FALL. Gebucht ist gebucht — die Antwort kommt nie an.
        # Genau hier entsteht die Doppelbuchung, wenn jemand blind wiederholt.
        transport = request.transport
        if transport is not None:
            transport.close()
        raise asyncio.CancelledError()

    return _json(dict(record, replayed=False))


async def handle_charge_lookup(request: web.Request) -> web.Response:
    box: Sandbox = request.app["box"]
    if box.scenario in ("unreachable",):
        raise web.HTTPServiceUnavailable(reason="sandbox is playing unreachable")
    if not box.may(request, "lookup"):
        return _json({"error": "unauthorized"}, status=401)
    key = request.query.get("key", "")
    found = box.charges.get(key)
    if found is None:
        return _json({"found": False})
    return _json({"found": True, **found})


async def handle_sca_complete(request: web.Request) -> web.Response:
    """Der Mensch hat in seiner Bank-App bestaetigt. Kein Umgehungsweg —
    dieser Endpunkt tut genau das, was ein Mensch am Telefon getan haette."""
    box: Sandbox = request.app["box"]
    if not box.admin_ok(request):
        return _json({"error": "unauthorized"}, status=401)
    body = await request.json()
    key = str(body.get("idempotency_key", ""))
    record = box.charges.get(key)
    if record is None or record["status"] != "sca_required":
        return _json({"error": "no_pending_sca"}, status=404)
    record.update(status="succeeded", failure_category="none", sca_pending=False)
    box.created_charges += 1
    return _json(dict(record))


async def handle_refund(request: web.Request) -> web.Response:
    box: Sandbox = request.app["box"]
    denied = await _guard(box, request, "refund")
    if denied is not None:
        return denied
    key = request.headers.get("Idempotency-Key", "")
    if not key:
        return _json({"error": "idempotency_key_required"}, status=400)
    if key in box.refunds:
        return _json(dict(box.refunds[key], replayed=True))
    body = await request.json()
    charge_ref = str(body.get("charge_ref", ""))
    amount = int(body.get("amount_minor", 0))
    target = None
    for record in box.charges.values():
        if record.get("charge_ref") == charge_ref:
            target = record
            break
    if target is None or target["status"] != "succeeded":
        return _json({"error": "unknown_charge"}, status=404)
    open_amount = int(target["amount_minor"]) - int(target["refunded_minor"])
    if amount <= 0 or amount > open_amount:
        return _json({"error": "amount_out_of_range"}, status=400)
    target["refunded_minor"] = int(target["refunded_minor"]) + amount
    refund = {"refund_ref": "re_" + secrets.token_hex(8), "amount_minor": amount,
              "currency": target["currency"], "status": "succeeded"}
    box.refunds[key] = refund
    return _json(dict(refund, replayed=False))


async def handle_cancel(request: web.Request) -> web.Response:
    box: Sandbox = request.app["box"]
    denied = await _guard(box, request, "cancel")
    if denied is not None:
        return denied
    body = await request.json()
    charge_ref = str(body.get("charge_ref", ""))
    for record in box.charges.values():
        if record.get("charge_ref") == charge_ref:
            # Stornieren ist NICHT erstatten. Der Haendler liefert nicht mehr;
            # ob Geld zurueckkommt, ist eine zweite, eigene Frage.
            record["order_ref"] = ""
            return _json({"status": "succeeded", "charge_ref": charge_ref,
                          "amount_minor": record["amount_minor"],
                          "currency": record["currency"],
                          "failure_category": "none", "cancelled": True,
                          "refunded_minor": record["refunded_minor"]})
    return _json({"error": "unknown_charge"}, status=404)


async def handle_scenario(request: web.Request) -> web.Response:
    box: Sandbox = request.app["box"]
    if not box.admin_ok(request):
        return _json({"error": "unauthorized"}, status=401)
    body = await request.json()
    wanted = str(body.get("scenario", "normal"))
    if wanted not in SCENARIOS:
        return _json({"error": "unknown_scenario", "known": list(SCENARIOS)}, status=400)
    box.scenario = wanted
    return _json({"scenario": box.scenario})


async def handle_stats(request: web.Request) -> web.Response:
    """Was der Dienst gesehen hat. Der Beweis fuer „genau einmal belastet"."""
    box: Sandbox = request.app["box"]
    if not box.admin_ok(request):
        return _json({"error": "unauthorized"}, status=401)
    return _json({"charge_attempts": box.charge_attempts,
                  "created_charges": box.created_charges,
                  "idempotency_keys": sorted(box.charges),
                  "scenario": box.scenario})


def build_app(*, provider_secret: str, admin_secret: str,
              refund_secret: str = "", read_secret: str = "") -> web.Application:
    app = web.Application(client_max_size=64 * 1024)
    app["box"] = Sandbox(provider_secret=provider_secret, admin_secret=admin_secret,
                         refund_secret=refund_secret, read_secret=read_secret)
    app.add_routes([
        web.get("/v1/health", handle_health),
        web.post("/v1/quote", handle_quote),
        web.post("/v1/charge", handle_charge),
        web.get("/v1/charge", handle_charge_lookup),
        web.post("/v1/refund", handle_refund),
        web.post("/v1/cancel", handle_cancel),
        web.post("/v1/sca/complete", handle_sca_complete),
        web.post("/admin/scenario", handle_scenario),
        web.get("/admin/stats", handle_stats),
    ])
    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    # 8795 und nicht 8791: auf 8791 sitzt der tiefe Executor (Hermes,
    # `deep/service.py`). Zwei Dienste auf einem Port ist ein Fehler, den man
    # erst im Betrieb bemerkt — und dann an der falschen Stelle sucht.
    parser.add_argument("--port", type=int, default=8795)
    args = parser.parse_args(argv)
    if args.host not in ("127.0.0.1", "::1", "localhost"):
        print("Der Pruefanbieter bindet nur an die Rueckschleife.", file=sys.stderr)
        return 2
    provider_secret = os.environ.get("SOLVIO_PAYMENT_SANDBOX_SECRET") or ""
    admin_secret = os.environ.get("SOLVIO_PAYMENT_SANDBOX_ADMIN") or ""
    refund_secret = os.environ.get("SOLVIO_PAYMENT_SANDBOX_REFUND") or ""
    read_secret = os.environ.get("SOLVIO_PAYMENT_SANDBOX_READ") or ""
    if not provider_secret or not admin_secret:
        print("SOLVIO_PAYMENT_SANDBOX_SECRET und SOLVIO_PAYMENT_SANDBOX_ADMIN "
              "muessen gesetzt sein — und verschieden.", file=sys.stderr)
        return 2
    if provider_secret == admin_secret:
        print("Anbieter- und Verwaltungsgeheimnis muessen verschieden sein: sonst "
              "koennte der Executor sein eigenes Ergebnis bestellen.", file=sys.stderr)
        return 2
    web.run_app(build_app(provider_secret=provider_secret, admin_secret=admin_secret,
                          refund_secret=refund_secret, read_secret=read_secret),
                host=args.host, port=args.port, print=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
