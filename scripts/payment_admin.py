#!/usr/bin/env python3
"""Die Zahlungsschicht einrichten — oertlich, besitzergebunden, ohne Wert im Terminal.

Gebaut nach dem Vorbild von `scripts/vault_admin.py`, mit derselben Haltung:
was Befugnis erzeugt, macht der Eigentuemer selbst, an seinem Rechner, und der
Wert wird per `getpass` erfragt statt als Argument uebergeben — ein Argument
stuende in der Shell-Historie und in jeder Prozessliste.

    python3 scripts/payment_admin.py status
    python3 scripts/payment_admin.py sandbox-up --port 8795
    python3 scripts/payment_admin.py link --ref payment://shopping/default \\
        --provider sandbox --merchants sandbox-shop --limit 5000

**Was dieses Werkzeug NIE verlangt:** eine Kartennummer, eine Pruefziffer, ein
Ablaufdatum, einen Bankzugang. Es fragt nach der KENNUNG, unter der der
Anbieter das Zahlungsmittel fuehrt (`pm_…`) — und nach dem Anbieterzugang, der
dann im Tresor landet und nie wieder herauskommt.

**Der Pruefanbieter ist ein echter Dienst.** `sandbox-up` schreibt seine Adresse
in die Anbieterkonfiguration und legt ZWEI Zugaenge im Tresor an: einen, der
belasten darf und dafuer die Anwesenheit eines Menschen verlangt, und einen,
der nur nachsehen darf. Zwei Zugaenge und nicht einer, weil `payment_reconcile`
sonst jedes Nachschlagen biometrisch machte.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import secrets
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from solvio.payment import config as PC                       # noqa: E402
from solvio.payment import merchant as PM                     # noqa: E402
from solvio.payment.health import assess                      # noqa: E402
from solvio.payment.instruments import (Instrument,           # noqa: E402
                                        InstrumentKind)
from solvio.payment.store import PaymentStore, db_path        # noqa: E402
from solvio.secret_vault import admin as VA                   # noqa: E402
from solvio.secret_vault import policy as VP                  # noqa: E402
from solvio.secret_vault.store import VaultStore              # noqa: E402


def cmd_status(args) -> int:
    word, reason = assess()
    print(f"Zustand: {word} — {reason}")
    print(f"Ablage:  {db_path()}")
    providers = PC.load()
    print(f"Anbieter: {', '.join(sorted(providers)) or 'keiner eingerichtet'}"
          f"  ({PC.config_path()})")
    if not os.path.exists(db_path()):
        return 0
    store = PaymentStore()
    for instrument in store.instruments():
        beschreibung = instrument.describe(for_model=False)
        print(f"  {instrument.payment_ref}  {beschreibung['status']}  "
              f"{beschreibung['name']}  "
              f"hoechstens {instrument.max_single_minor} Cent je Zahlung  "
              f"nur bei {', '.join(instrument.allowed_merchant_ids) or '—'}")
    offen = store.open_reconciliations()
    if offen:
        print(f"  {len(offen)} Vorgang/Vorgaenge warten auf Klaerung")
    return 0


def cmd_sandbox_up(args) -> int:
    """Richtet den Pruefanbieter ein: Adresse, drei Tresorzugaenge, Startbefehl.

    **Kein Wert erscheint auf dem Bildschirm.** Die vier Geheimnisse des
    Pruefdienstes werden erzeugt, drei davon in den Tresor gelegt, und alle in
    eine Datei mit 0600 geschrieben, die der Startbefehl einliest. Ein Wert im
    Terminal steht danach in der Bildlaufhistorie, im Fensterpuffer und
    moeglicherweise in einer Sitzungsaufzeichnung — dieselbe Ueberlegung, aus
    der `vault_admin.py` per `getpass` fragt.

    Es wird kein Prozess gestartet: der Dienst gehoert in ein eigenes Fenster,
    damit man ihm beim Arbeiten zusehen kann.

    **Drei Zugaenge, nicht einer.** Belasten, zurueckgeben, nachsehen sind drei
    verschiedene Befugnisse, und der Pruefdienst setzt den Unterschied durch.
    Ein Aufbau, der alle drei auf denselben Wert legte, bewiese die Trennung
    nicht — und ein gruener Lauf dagegen bewiese gar nichts.
    """
    base_url = f"http://127.0.0.1:{args.port}"
    charge_secret = "sbx-charge-" + secrets.token_hex(16)
    refund_secret = "sbx-refund-" + secrets.token_hex(16)
    read_secret = "sbx-read-" + secrets.token_hex(16)
    admin_secret = "sbx-admin-" + secrets.token_hex(16)

    path = PC.config_path()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    existing = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as handle:
            existing = json.load(handle)
    providers = existing.setdefault("providers", {})
    providers["sandbox"] = {"kind": "sandbox", "base_url": base_url}
    previous = os.umask(0o077)
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(existing, handle, indent=2)
    finally:
        os.umask(previous)

    vault = VaultStore()
    for ref, wert, caps, presence, name in (
            ("secret://payment-provider/sandbox-charge", charge_secret,
             ("purchase_place",), True, "Pruefanbieter — Belastung"),
            ("secret://payment-provider/sandbox-refund", refund_secret,
             ("refund_request", "purchase_cancel"), False,
             "Pruefanbieter — zurueck"),
            ("secret://payment-provider/sandbox-read", read_secret,
             ("payment_intent_prepare", "payment_reconcile", "payment_health"),
             False, "Pruefanbieter — lesend")):
        VA.add(secret_ref=ref, kind=VP.SecretKind.API_KEY,
               plaintext=wert.encode("utf-8"), allowed_capabilities=caps,
               allowed_targets=(base_url,),
               allowed_executors=(VP.ExecutorId.PAYMENT,),
               display_name=name, allow_background=False,
               requires_user_presence=presence, store=vault, replace=True)

    env_path = os.path.join(os.path.dirname(path), "payment-sandbox.env")
    previous = os.umask(0o077)
    try:
        with open(env_path, "w", encoding="utf-8") as handle:
            handle.write(f"export SOLVIO_PAYMENT_SANDBOX_SECRET={charge_secret}\n")
            handle.write(f"export SOLVIO_PAYMENT_SANDBOX_REFUND={refund_secret}\n")
            handle.write(f"export SOLVIO_PAYMENT_SANDBOX_READ={read_secret}\n")
            handle.write(f"export SOLVIO_PAYMENT_SANDBOX_ADMIN={admin_secret}\n")
    finally:
        os.umask(previous)

    print("Anbieter eingetragen:", base_url)
    print("Drei Zugaenge liegen im Tresor: belasten, zurueckgeben, nachsehen.")
    print(f"Die Geheimnisse des Pruefdienstes liegen in {env_path} (0600).")
    print()
    print("Starte den Pruefanbieter in einem eigenen Fenster:")
    print()
    print(f"  set -a && . {env_path} && set +a && \\")
    print(f"  .venv/bin/python scripts/payment_sandbox.py --port {args.port}")
    print()
    print("Das Verwaltungsgeheimnis steht in derselben Datei und wird nur")
    print("gebraucht, um Szenarien zu stellen. SOLVIO kennt es NICHT — sonst")
    print("koennte der Executor sein eigenes Ergebnis bestellen, und ein")
    print("gruener Lauf bewiese nichts.")
    return 0


def cmd_link(args) -> int:
    """Hinterlegt ein Zahlungsmittel. Fragt die Anbieterkennung per getpass."""
    unknown = [m for m in args.merchants if PM.merchant_for_id(m) is None]
    if unknown:
        print(f"Unbekannte Haendler: {', '.join(unknown)}", file=sys.stderr)
        print(f"Bekannt sind: {', '.join(sorted(PM.MERCHANTS))}", file=sys.stderr)
        return 2
    token = args.token or getpass.getpass(
        "Kennung beim Anbieter (pm_…, wird nicht angezeigt): ").strip()
    if not token:
        print("Ohne Kennung geht es nicht.", file=sys.stderr)
        return 2
    try:
        instrument = Instrument(
            payment_ref=args.ref, kind=InstrumentKind(args.kind),
            provider=args.provider, max_single_minor=args.limit,
            daily_total_minor=args.daily,
            allowed_currencies=tuple(args.currencies),
            allowed_merchant_ids=tuple(args.merchants),
            provider_secret_ref=args.charge_ref,
            provider_readonly_ref=args.read_ref,
            provider_refund_ref=args.refund_ref,
            provider_token=token, display_name=args.name,
            display_hint=args.hint,
            created_at=__import__("solvio.payment.store", fromlist=["utcnow_iso"])
            .utcnow_iso())
    except ValueError as exc:
        print(f"Das passt nicht zusammen: {exc}", file=sys.stderr)
        return 2
    finally:
        token = ""
    PaymentStore().put_instrument(instrument)
    print(f"{instrument.payment_ref} ist hinterlegt.")
    print("Die Karte selbst liegt beim Anbieter. SOLVIO hat sie nie gesehen.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    subs = parser.add_subparsers(dest="command", required=True)

    subs.add_parser("status", help="Zustand der Zahlungsschicht").set_defaults(
        func=cmd_status)

    sandbox = subs.add_parser("sandbox-up", help="Pruefanbieter einrichten")
    sandbox.add_argument("--port", type=int, default=8795)
    sandbox.set_defaults(func=cmd_sandbox_up)

    link = subs.add_parser("link", help="Ein Zahlungsmittel hinterlegen")
    link.add_argument("--ref", required=True)
    link.add_argument("--kind", default="virtual_card")
    link.add_argument("--provider", required=True)
    link.add_argument("--name", default="")
    link.add_argument("--hint", default="")
    link.add_argument("--limit", type=int, required=True,
                      help="Hoechstbetrag je Zahlung in Cent")
    link.add_argument("--daily", type=int, default=0,
                      help="Hoechstbetrag je Tag in Cent (0 = keine)")
    link.add_argument("--currencies", nargs="+", default=["EUR"])
    link.add_argument("--merchants", nargs="+", required=True)
    link.add_argument("--charge-ref", default="secret://payment-provider/sandbox-charge")
    link.add_argument("--refund-ref", default="secret://payment-provider/sandbox-refund")
    link.add_argument("--read-ref", default="secret://payment-provider/sandbox-read")
    link.add_argument("--token", default="",
                      help="NUR fuer Skripte. Ohne dieses Argument wird gefragt.")
    link.set_defaults(func=cmd_link)

    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
