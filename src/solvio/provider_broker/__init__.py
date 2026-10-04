"""SOLVIO Provider Broker — der Kaefig bekommt ein Zeitfenster, keinen Schluessel.

Hermes ruft den Anbieter nicht mehr selbst an. Er spricht mit einem
Rueckwaertsproxy des Cores auf der Rueckschleife (`127.0.0.1:8792`), und was in
seinem Kaefig liegt, ist ein undurchsichtiger Broker-Token:

* er oeffnet **keinen** Anbieter — gegen `api.openai.com` ist er `401`,
* er oeffnet den Broker **nur**, solange der Core fuer diesen Auftraggeber ein
  Lease offen haelt,
* und er **ueberlebt den Auftrag nicht**, fuer den er gepraegt wurde.

    SOLVIO BESITZT DEN ANBIETER.
    HERMES BEKOMMT VORUEBERGEHENDEN ZUGANG ZU INFERENZ.

Entscheidung: ADR-0027. Architektur:
`docs/architecture/PROVIDER_BROKER.md`.
"""
from __future__ import annotations

from solvio.provider_broker.service import (DEEP_PRINCIPAL, BrokerService,
                                            bot_principal, configured_port)

__all__ = ["BrokerService", "DEEP_PRINCIPAL", "bot_principal",
           "configured_port"]
