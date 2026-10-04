"""Die Zahlungsschicht — eine eigene Autoritaetsdomaene, kein Geheimnistyp.

    SOLVIO darf beauftragt werden zu bezahlen.
    Ein Agent besitzt dabei nie das Zahlungsmittel.

Drei Dinge werden hier durchgehend auseinandergehalten:

    ABSICHT            was jemand kaufen moechte      — ein Modell darf sie vorschlagen
    BETRAGSWAHRHEIT    was es tatsaechlich kostet     — nur der Executor darf sie liefern
    BEFUGNIS           dass es bezahlt werden darf    — nur ein Gesicht darf sie geben

Was hier NIE liegt: Kartennummer, Pruefziffer, Magnetstreifendaten,
Bankzugang, Anbieter-Hauptschluessel. SOLVIO ist nicht der Kartentresor und
will es nicht sein — das Material liegt beim Anbieter, beim Haendler oder im
Wallet, und SOLVIO haelt einen undurchsichtigen Verweis darauf.

Die Module, in Abhaengigkeitsordnung:

    refs        `payment://<zweck>/<name>` — undurchsichtig, ohne Befugnis
    merchant    wer das Geld bekommt: Kennung UND exakte Herkunft
    firewall    was niemals in eine Zeile geschrieben wird
    intent      Absicht, Kostenvoranschlag, Zustandsmaschine, Digest
    instruments Zahlungsmittel und seine Grenzen — Grenzen SENKEN nur
    store       Zahlungsmittel, Absichten, Zahlungsbuch (append-only)
    config      wo ein Anbieter erreichbar ist — Adressen, nie Geheimnisse
    providers   der Anbietervertrag und der Client zum Pruefanbieter
    executor    die EINZIGE Stelle, die Anbieterbefugnis anfassen darf
    health      wie es steht — ohne dafuer Geld zu bewegen
    notify      wann SOLVIO von sich aus etwas sagt
    endpoint    der attestierte Weg vom iPhone

Entscheidung: ADR-0026. Architektur: docs/architecture/PAYMENT_CAPABILITY.md.
"""
