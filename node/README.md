# SOLVIO Node

Optionaler Remote-Worker für SOLVIO Nexus. Der Core und die iPhone-App können
auch ohne diesen Worker betrieben werden. Der Node berechnet Ergebnisse; der
Core verwaltet Aufträge, Gedächtnis, Berechtigungen und Freigaben.

Im Code enthalten sind Health, SHA-256, das begrenzte Abrufen öffentlicher
Webseiten und öffentliche Websuche über einen konfigurierten Anbieter.
Dauerhafte Hintergrundjobs sind optional. Das ist keine Zusage eines laufenden
öffentlichen Dienstes oder eines persönlich abgenommenen Gesamtauftrags.

Der Worker bietet ausschließlich registrierte Fähigkeiten mit geprüften
Eingaben und Ergebnissen. Es gibt keinen allgemeinen Shell-Endpunkt.
Der Transport verlangt mTLS. Für Fernzugriff wird das private WireGuard-Netz
verwendet; der API-Port gehört nicht auf die öffentliche Schnittstelle.

## Lokal entwickeln

Python 3.11 oder neuer. Im Ordner `node`:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
PYTHONPATH=src python -m unittest discover -s tests -p 'test_*.py'
```

## Eigenen Worker einrichten

`deploy/config.json` ist eine Vorlage. Der öffentliche Stand bindet zunächst
an `127.0.0.1`. Für Fernzugriff die eigene private WireGuard-Adresse sowie
Zertifikat-, Schlüssel- und Datenpfade eintragen. Den Dienst erst danach
starten. `deploy/solvio-node.service` enthält die Linux-Systemd-Vorlage.
Schlüssel und Anbieterzugänge gehören außerhalb des Repositorys in die
vorgesehenen privaten Dateien beziehungsweise Systemd-Credentials.

`tools/gen_dev_certs.sh` verlangt Ausgabeverzeichnis und Serveradresse explizit.
`tools/gen_identity_certs.sh` ist ein Werkzeug zum Ausstellen von Zertifikaten
mit URI-SAN aus einer bestehenden eigenen CA. Vor dessen bewusster Verwendung
`SOLVIO_NODE_PKI` auf das eigene geschützte PKI-Verzeichnis setzen; es kann
Zertifikate in diesem Verzeichnis erneuern.

Details: [Betrieb](docs/OPERATIONS.md), [Protokoll](docs/PROTOCOL_V1.md),
[Sicherheit](docs/SECURITY.md), [Fähigkeiten](docs/CAPABILITIES.md),
[Hintergrundjobs](docs/BACKGROUND_JOBS.md).

Der eigene Node-Code steht wie das Gesamtprojekt unter der MIT-Lizenz.
Die Lizenzen verwendeter Abhängigkeiten gelten weiterhin.
