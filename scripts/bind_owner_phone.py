#!/usr/bin/env python3
"""Bindet die Telefonnummer des Eigentuemers — er tippt sie, niemand sonst liest sie.

Warum es dieses Skript gibt: eine Kontaktbindung beantwortet „wer ist gemeint",
und von ihrer Antwort haengt ab, wessen Telefon bei einem Anruf klingelt. Die
Nummer darf deshalb weder im Chat stehen noch in den Kontext eines Modells
geraten — beides waere vermeidbar, also wird es vermieden.

Was hier NICHT passiert:

* Kein direkter Schreibzugriff auf SQLite. Der Weg ist die bestehende Faehigkeit
  `communication_confirm_binding`, ueber den Kontrollsocket des LAUFENDEN Cores.
  Der Freigabespeicher wird ausschliesslich LESEND geoeffnet (`read_only=True`).
* Kein zweiter Kontaktspeicher. Es ist derselbe `BindingStore`, den auch E-Mail
  und Telefonie benutzen.
* Keine Umgehung der Face-ID-Autoritaet. Jede Bindung erzeugt eine Freigabe auf
  dem iPhone; dort steht die Nummer im Klartext, und der Mensch bestaetigt genau
  das, was er liest.
* Keine Ausgabe der Nummer. Weder auf stdout noch im Log dieses Skripts.

Freigaben werden anhand ihres gespeicherten Zustands beobachtet. Ein
Ergebnisumschlag ersetzt diese Zustandspruefung nicht.

    python3 scripts/bind_owner_phone.py
    python3 scripts/bind_owner_phone.py --resume ap-...   # eine offene fortsetzen
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from solvio.realtime.control import ControlClient                 # noqa: E402
from solvio.security.mobile_approval import store as AS           # noqa: E402
from solvio.storage.inventory import approval_state_dir           # noqa: E402

#: E.164, streng. Ein fuehrendes Plus, dann acht bis fuenfzehn Ziffern.
#:
#: Streng, weil eine halb erkannte Nummer schlimmer ist als eine abgelehnte:
#: `00049…` oder `0151…` sind im Inland gebraeuchlich und waeren fuer den
#: Anbieter etwas anderes als gemeint.
_E164 = re.compile(r"^\+[1-9]\d{7,14}$")

#: Zustaende des Freigabespeichers, nach Bedeutung geordnet.
#:
#: Ausdruecklich aufgezaehlt und nicht erraten: die erste Fassung dieses Skripts
#: kannte keinen einzigen davon und schloss aus Ergebnisnamen auf den Zustand.
WARTEN = {AS.PENDING}
FERTIG = {AS.APPROVED}
LAEUFT = {"EXECUTING"}
ERLEDIGT = {"CONSUMED"}
ABGELEHNT = {AS.DENIED}
VERFALLEN = {AS.EXPIRED}
GESCHEITERT = {"FAILED"}

WARTEN_SECS = 600
ABSTAND_SECS = 3.0

STANDARD_ALIASE = ("ich", "mich", "eigentuemer")
ANZEIGENAME = "Eigentümer"


def _pfad() -> str:
    return os.path.join(approval_state_dir(), "approval_control.sqlite3")


async def _zustand(approval_id: str) -> str:
    """Der Zustand aus dem Freigabespeicher — LESEND, ueber dessen eigene API."""
    speicher = AS.ApprovalControlStore(_pfad(), read_only=True)
    await speicher.open()
    try:
        zeile = await speicher.get_request(approval_id)
        return str((zeile or {}).get("state") or "WEG")
    finally:
        await speicher.close()


def _nummer_einlesen() -> str:
    """Verdeckt, zweimal, und ohne sie je auszugeben."""
    print("Deine Mobilnummer in internationaler Form, also mit +49 statt 0.")
    print("Die Eingabe ist verdeckt — sie erscheint weder hier noch im Chat.")
    print()
    sauber = lambda t: t.strip().replace(" ", "").replace("-", "").replace("/", "")
    erste = sauber(getpass.getpass("Nummer: "))
    zweite = sauber(getpass.getpass("Zur Sicherheit noch einmal: "))
    if erste != zweite:
        print("FEHLER: die beiden Eingaben sind nicht gleich. Nichts gebunden.")
        raise SystemExit(2)
    if not _E164.match(erste):
        # Auch im Fehlerfall wird die Nummer NICHT gezeigt.
        print("FEHLER: das ist keine gueltige internationale Rufnummer.")
        print("        Erwartet: ein Plus, dann 8 bis 15 Ziffern, z. B. +49...")
        raise SystemExit(2)
    return erste


def _schon_so_gebunden(antwort: dict) -> bool:
    """Der Core meldet eine unveraenderte Bindung als `binding_unchanged`."""
    return str(antwort.get("reason") or "") == "binding_unchanged"


def _argumente(alias: str, nummer: str) -> dict:
    return {"alias": alias, "display_name": ANZEIGENAME,
            "handles": [{"channel": "phone", "value": nummer}],
            "source": "owner_confirmed_locally"}


async def _warte_und_fuehre_aus(alias: str, anfrage: str, argumente: dict) -> bool:
    """Beobachtet den Zustand und fuehrt NUR bei APPROVED aus.

    Kein Rueckschluss aus einem Ergebnisumschlag. Solange `PENDING` steht, wird
    gewartet; abgebrochen wird nur bei einer ECHTEN Ablehnung.
    """
    print(f"  '{alias}': Freigabe gestellt ({anfrage[:16]}...).")
    print(f"           Sie erscheint in der App, sobald diese das naechste Mal")
    print(f"           abruft — das tut sie nur im Vordergrund. Dort steht die")
    print(f"           Nummer im Klartext; pruef sie, bevor du bestaetigst.")
    frist = time.monotonic() + WARTEN_SECS
    letzter = ""
    while time.monotonic() < frist:
        z = await _zustand(anfrage)
        if z != letzter:
            if z in WARTEN:
                print(f"           warte … (Zustand {z})")
            letzter = z
        if z in FERTIG or z in LAEUFT:
            antwort = await ControlClient().run(
                "communication_confirm_binding", argumente,
                approval_request_id=anfrage)
            if antwort.get("ok"):
                print(f"  '{alias}': bestaetigt und gebunden.")
                return True
            if _schon_so_gebunden(antwort):
                print(f"  '{alias}': steht inzwischen bereits genau so. Nichts zu tun.")
                return True
            # Bestaetigt, aber die Ausfuehrung ging schief — das ist etwas
            # anderes als eine Ablehnung und wird auch so gemeldet.
            print(f"  '{alias}': freigegeben, aber nicht ausgefuehrt — "
                  f"{antwort.get('outcome')} / {antwort.get('reason')}")
            print(f"           {antwort.get('message') or ''}")
            return False
        if z in ERLEDIGT:
            print(f"  '{alias}': diese Freigabe ist bereits verbraucht.")
            return False
        if z in ABGELEHNT:
            print(f"  '{alias}': von dir ABGELEHNT. Nichts gebunden.")
            return False
        if z in VERFALLEN:
            print(f"  '{alias}': die Freigabe ist ABGELAUFEN, bevor sie "
                  f"bestaetigt wurde. Nichts gebunden.")
            print(f"           Haeufigste Ursache: die App war nicht im "
                  f"Vordergrund. Ohne Push (DEBT-0029) sieht sie eine Freigabe")
            print(f"           erst beim naechsten eigenen Abruf. App oeffnen, "
                  f"offen lassen, neu starten.")
            return False
        if z in GESCHEITERT or z == "WEG":
            print(f"  '{alias}': unerwarteter Zustand '{z}'. Nichts gebunden.")
            return False
        await asyncio.sleep(ABSTAND_SECS)
    print(f"  '{alias}': keine Entscheidung innerhalb von {WARTEN_SECS} Sekunden.")
    return False


async def _binden(alias: str, nummer: str) -> bool:
    argumente = _argumente(alias, nummer)
    antwort = await ControlClient().run("communication_confirm_binding", argumente)
    if antwort.get("ok"):
        print(f"  '{alias}': gebunden (ohne Rueckfrage).")
        return True
    if _schon_so_gebunden(antwort):
        # Seit Contact Binding Authority Hardening V1 fragt der Core gar nicht
        # erst, wenn genau diese Bindung bereits steht — und schreibt auch
        # nichts. Das ist kein Fehlschlag, das ist der Zielzustand.
        print(f"  '{alias}': steht bereits genau so. Nichts zu tun.")
        return True
    anfrage = (antwort.get("data") or {}).get("request_id")
    if antwort.get("outcome") != "approval_required" or not anfrage:
        print(f"  '{alias}': abgelehnt — {antwort.get('reason')} "
              f"{antwort.get('message') or ''}")
        return False
    return await _warte_und_fuehre_aus(alias, anfrage, argumente)


def _app_muss_offen_sein() -> None:
    """Ohne offene App kommt keine Freigabe an — und das Fenster verbrennt.

    Es gibt KEINEN Push (DEBT-0029). Die Zustellung ist Vordergrund-Polling:
    die App ruft `GET /v1/approvals` ab, solange sie offen ist. Ist sie zu oder
    der Bildschirm gesperrt, erfaehrt sie von nichts.

    Der Vordergrund-Wartepunkt kommt vor dem Erstellen einer Anfrage,
    damit deren Frist erst danach beginnt.
    """
    print("WICHTIG: SOLVIO schickt keine Mitteilung aufs iPhone.")
    print("Eine Freigabe erreicht das Telefon nur, solange die App OFFEN und im")
    print("Vordergrund ist. Jede Freigabe laeuft nach 10 Minuten ab.")
    print()
    print("  1. Entsperr das iPhone.")
    print("  2. Oeffne die SOLVIO-App und lass sie im Vordergrund.")
    print("  3. Erst dann hier weiter.")
    print()
    try:
        input("Wenn die App offen ist: Eingabetaste druecken (Strg-C bricht ab) ")
    except (EOFError, KeyboardInterrupt):
        print("\nAbgebrochen. Nichts gestellt, nichts gebunden.")
        raise SystemExit(1)


async def run(aliase: list[str], fortsetzen: str) -> int:
    if not ControlClient().available():
        print("FEHLER: der Core laeuft nicht (Kontrollsocket nicht erreichbar).")
        return 1

    if fortsetzen:
        z = await _zustand(fortsetzen)
        print(f"Bestehende Freigabe {fortsetzen[:16]}... : Zustand {z}")
        if z in VERFALLEN or z in ABGELEHNT or z == "WEG":
            print("Diese Anfrage traegt nicht mehr. Starte ohne --resume neu.")
            return 1

    nummer = _nummer_einlesen()
    print()
    _app_muss_offen_sein()
    print(f"Es werden {len(aliase)} Bindungen angelegt — je eine Freigabe:")
    print("   " + ", ".join(f"'{a}'" for a in aliase))
    print()

    gebunden: list[str] = []
    for i, alias in enumerate(aliase):
        if i == 0 and fortsetzen:
            # Genau die offene Anfrage fortsetzen, keine zweite erzeugen.
            ok = await _warte_und_fuehre_aus(alias, fortsetzen,
                                             _argumente(alias, nummer))
        else:
            ok = await _binden(alias, nummer)
        if ok:
            gebunden.append(alias)
        else:
            print("Abgebrochen. Bereits gebundene Aliase bleiben bestehen.")
            break

    print()
    print(f"Gebunden: {len(gebunden)} von {len(aliase)} — "
          f"{', '.join(gebunden) or 'keine'}")
    return 0 if len(gebunden) == len(aliase) else 1


def main() -> int:
    global ANZEIGENAME
    p = argparse.ArgumentParser(prog="bind_owner_phone")
    p.add_argument("--alias", action="append", default=None,
                   help="Alias, mehrfach angebbar. Vorgabe: ich, mich, eigentuemer")
    p.add_argument("--resume", default="", metavar="APPROVAL_ID",
                   help="Eine bereits offene Freigabe fortsetzen, statt eine "
                        "zweite zu erzeugen.")
    p.add_argument("--display-name", default=ANZEIGENAME,
                   help="Anzeigename der eigenen Kontaktbindung.")
    args = p.parse_args()
    ANZEIGENAME = args.display_name.strip()
    if not ANZEIGENAME:
        p.error("Der Anzeigename darf nicht leer sein.")
    return asyncio.run(run(args.alias or list(STANDARD_ALIASE), args.resume))


if __name__ == "__main__":
    raise SystemExit(main())
