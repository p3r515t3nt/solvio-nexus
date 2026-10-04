# Beiträge zu SOLVIO Nexus

SOLVIO Nexus ist eine Beta mit einem maßgeblichen Mac Core. Kleine,
nachvollziehbare Änderungen sind leichter zu prüfen als ein neuer paralleler
Auftrags-, Berechtigungs- oder Gedächtnisweg.

Jede Person kann das Repository forken, ihre eigene Kopie ändern und einen
Pull Request einreichen. Direktes Schreibrecht am Original entsteht dadurch
nicht automatisch.

Beschreibe bei einem Fehler den erwarteten Ablauf, den tatsächlichen Ablauf
und die betroffene Version. Nutze synthetische Beispiele. Entferne persönliche
Inhalte, Zugangsdaten und Gerätekennungen aus Berichten und Anhängen.

Für eine Änderung:

1. Prüfe den vorhandenen Weg und grenze das Problem ein.
2. Verwende bestehende Verträge und Zustandsablagen.
3. Beschreibe die Verhaltensänderung und ihre Grenzen im Pull Request.
4. Prüfe relevante Regressionen mit dem kanonischen Core-Runner.
5. Halte erforderliche Drittanbieterhinweise und Lizenzdateien erhalten.

## Entwicklung und Prüfung

Der Core benötigt Python 3.13 oder neuer. `pyproject.toml` und `uv.lock` liegen
im Git-Repository-Root. Führe die Befehle dort aus. Der öffentliche Bootstrap
ist noch kein vollständiger Installer
für Dienststart, native Anbieter oder persönliche Geräte.

```sh
uv sync --frozen --extra dev
uv run --frozen --extra dev python scripts/run_tests.py dashboard control_center
```

Wähle Filter passend zur Änderung. Ohne Filter läuft die vollständige Suite.
`scripts/run_tests.py` ist der maßgebliche Testweg; ein direkter `pytest`-Lauf
ersetzt seine Bestands- und Ausführungsprüfung nicht. Verwende kein optimiertes
Python für die Suite. Fehlende optionale Laufzeiten, Fehler und ausdrückliche
Skips müssen im Prüfbericht sichtbar bleiben.

Die iOS-App hat einen eigenen Xcode-/Swift-Bauweg. Simulatorprüfungen ersetzen
keine App-Attest-, Secure-Enclave- oder Face-ID-Prüfung auf einem echten Gerät.
Satellite-Prüfungen benötigen bei Hardwarebezug ein getrennt eingerichtetes Gerät.

Teste keine echten Käufe, Nachrichten, Kontozugriffe oder Geräteaktionen als
Nebenwirkung eines Beitrags. Nutze vorhandene synthetische Fixtures und
begrenzte Regressionen. Eine erforderliche Liveprüfung muss separat vereinbart
und als solche dokumentiert werden.

## Grenzen einer Änderung

Eine Anzeige verleiht keine neue Befugnis. Browser, Sprache, iPhone und
Arbeitsprozesse müssen dieselben Core-Regeln verwenden. Breite Reparatur- oder
Ausführungsrechte dürfen nicht aus einer fehlenden Integration entstehen.
Änderungen an Freigaben, Datenzugriff oder Kosten müssen ihren konkreten Umfang
und die passenden negativen Fälle belegen.

Interne Pläne, private Betriebsbelege, Laufzeitdaten und lokale Zugänge gehören
nicht in einen öffentlichen Pull Request. Sachliche Modell- und Anbieternamen
sowie echte Lizenz- und Urheberhinweise bleiben erhalten.
