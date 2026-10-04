# SOLVIO Nexus

Dein persönlicher Assistent. Ein gemeinsamer Ort für Chat, Sprache und Aufträge.

SOLVIO Nexus ist ein persönlicher KI-Assistent für Gespräche, Recherche und
Aufträge. Der Mac Core verwaltet den gemeinsamen Zustand, die Fähigkeiten und
die Regeln für Datenzugriff und Aktionen. Ein Browser-Dashboard und eine
native iPhone-App machen diesen Zustand zugänglich.

Das Projekt wird als Beta veröffentlicht. Der Quellcode enthält mehr Wege,
als auf einer neuen Installation bereits eingerichtet oder live abgenommen
sind. Eine sichtbare Funktion ist keine Zusage, dass jeder Anbieter, jedes
Konto und jedes Gerät damit schon funktioniert.

## English entry

SOLVIO Nexus is a beta personal AI assistant with a canonical macOS core,
a browser dashboard and a native iPhone app. Windows can use the dashboard
while the core runs on a Mac. Provider accounts and device enrollment require
separate setup. Apple Mail coverage and live flight-search acceptance are incomplete.
The main documentation below is in German.

## Was enthalten ist

- Gespräche über Text und die vorhandenen Sprachwege.
- Aufträge mit Status, Fortschritt, Ergebnissen und Hinweisen.
- Recherche und begrenzte Dateiarbeit über eingerichtete Fähigkeiten.
- Gemeinsamer Gesprächsverlauf und ein vom Core verwaltetes Gedächtnis.
- Verbindungsstatus und Freigaben für unterstützte Integrationen.
- Anzeige regelmäßiger Aufgaben mit ihrem gespeicherten Zustand.

Persistierte Aufträge können weiterlaufen, wenn das Dashboard geschlossen
wird. Dafür müssen der Core und der benötigte Arbeitsweg verfügbar bleiben.
Die Oberfläche zeigt den gelesenen Zustand; sie ersetzt keine Prüfung des
tatsächlich ausgeführten Ergebnisses.

## Oberflächen

Das Dashboard hat fünf Hauptbereiche:

| Bereich | Zweck |
| --- | --- |
| Chat | Mit SOLVIO sprechen oder schreiben. |
| Überblick | Aktuellen Zustand und relevante Ergebnisse sehen. |
| Ideen | Vorschläge prüfen, ohne sie dadurch auszuführen. |
| Aufgaben | Aufträge, Fortschritt und Ergebnisse verfolgen. |
| Bibliothek | Vorhandene Ergebnisse und Wissen wiederfinden. |

Ein zusätzliches Menü öffnet Verbindungen, Freigaben, Gedächtnis und Wissen,
Aktivitäten, Hinweise und Einstellungen. Aktivitäten zeigen den vorhandenen
Ausschnitt des Core-Zustands. Regelmäßige Aufgaben lassen sich hier ansehen;
ihre Anzeige ist keine allgemeine Verwaltungsoberfläche für Zeitpläne.

Die iPhone-App ergänzt die mobile Nutzung und konkrete Freigabeentscheidungen.
Ein angemeldeter Browser oder ein verbundenes Gerät erhält dadurch nicht
automatisch das Recht, jede Aktion auszuführen.

## Unterstützter Betriebsrahmen

| Bestandteil | Rahmen dieser Beta |
| --- | --- |
| Core | macOS ist die maßgebliche Betriebsumgebung. |
| Dashboard | Browserzugang zum eingerichteten Mac Core, auch unter Windows. |
| iPhone | Native App; Einrichtung und Signierung mit Apple-Werkzeugen. |
| Satellite | Optionaler Sprachendpunkt, kein zweiter Core. |
| Raspberry Pi | Optionaler, derzeit pausierter Installationsweg. |

Ein Windows-Browser macht den Core nicht zu einem Windows-Dienst. Native
Mac-Funktionen benötigen weiterhin den Mac und die jeweiligen Berechtigungen.
Der Pi ist für den Core-Bootstrap weder erforderlich noch automatisch aktiv.

## Grenzen und offener Stand

- Die persönliche Liveabnahme der Flugsuche ist noch offen. Angebote,
  Verfügbarkeit und Preise dürfen nicht als verlässlich bestätigt gelten,
  nur weil ein Rechercheweg Code oder Tests besitzt.
- Apple Mail ist noch nicht vollständig unterstützt und abgenommen.
  Vorhandene Mailwege hängen von Verbindung, Umfang und Aktion ab.
- Kalender, Mail und weitere Dienste benötigen eigene Kontoverbindungen und
  passende Berechtigungen. Eine neue Installation übernimmt keine Freigaben.
- Sprache, Gerätewechsel und Hintergrundaufträge benötigen Prüfungen mit
  den tatsächlich eingerichteten Geräten und Anbietern.
- Native Laufzeiten, Dienststart und einige Konfigurationspfade sind noch
  Beta-Bestand. Es gibt keinen universellen Ein-Klick-Installer.

## Aufbau des Repositorys

```text
pyproject.toml, uv.lock  Core-Paketmetadaten und feste Python-Abhängigkeiten
src/solvio/              Python-Dienst, Dashboard und gemeinsame Verträge
scripts/                 Core-Werkzeuge und kanonischer Test-Runner
tests/                   Produkt-, Sicherheits- und Runnerprüfungen
ios/                     Native iPhone-App und Swift-Komponenten
satellite/               Optionaler Sprachendpunkt
```

`node/` enthält zusätzlich den optionalen Remote-Worker. Der Core kann ohne
Node-Konfiguration starten. Einrichtung und die privaten Transportgrenzen
stehen in [Node README](node/README.md).

Die öffentliche Ausgabe enthält Quellcode und veröffentlichbare Dokumentation.
Persönliche Daten, Kontozugänge, Geräteidentitäten und private Betriebsbelege
gehören nicht hinein. Ein privater Fortsetzungs- oder Wiederherstellungsstand
wird separat behandelt und ist kein öffentlicher Installationsweg.

## Core für die Entwicklung vorbereiten

Benötigt werden Python 3.13 oder neuer und `uv`. Führe die folgenden Befehle
im Git-Repository-Root aus, wo `pyproject.toml` und `uv.lock` liegen.

```sh
uv sync --frozen --extra dev
uv run --frozen solvio --version
uv run --frozen solvio health
```

Das ist ein Entwicklungsbootstrap. `solvio health` prüft den lokalen Zustand
und beendet sich. Es startet weder das Dashboard noch einen dauerhaften
Sprachdienst und bestätigt keine funktionierende Konto- oder Geräteverbindung.

Konfiguration wird aus Umgebungsvariablen beziehungsweise einer lokalen
`.env` gelesen. Verwende nur geprüfte Beispielkonfigurationen und eigene
Werte. Eine `.env`, Schlüssel oder Tokens dürfen nie eingecheckt werden.
Native Arbeitswege benötigen zusätzlich ihre vorgesehenen lokalen Laufzeiten;
die Python-Abhängigkeiten allein richten diese nicht ein.

## KI- und Dienstanbindungen

SOLVIO nutzt externe KI-Modelle und vorhandene native Arbeitsläufe, unter
anderem Anbindungen an OpenAI, Codex, Claude und Hermes. Welche Wege verfügbar
sind, hängt von der Konfiguration und den jeweiligen Anbieterbedingungen ab.
Es gibt keine Zusage, dass ein bestimmtes Abonnement jede Funktion abdeckt.

Anbieterzugänge und Nutzungsgrenzen müssen bewusst eingerichtet werden.
Ausgewählte Inhalte können dabei an den konfigurierten Anbieter übertragen
werden. Lokale Datenhaltung bedeutet nicht, dass jede Verarbeitung lokal bleibt.
Ein fehlender Zugang darf nicht als erfolgreich ausgeführter Auftrag erscheinen.

## Berechtigungen und Aktionen

Der Core prüft Fähigkeit, Datenumfang und erforderliche Freigabe. Modelle und
Arbeitsprozesse können Arbeit vorschlagen oder Ergebnisse liefern; sie dürfen
keine eigenen Berechtigungen erteilen. Freigabepflichtige Aktionen benötigen
den vorgesehenen, geprüften Entscheidungsweg, einschließlich der iPhone-
Freigabe dort, wo die Aktion diesen verlangt. Details: [ARCHITECTURE.md](ARCHITECTURE.md).

## Tests und Beiträge

Die Studio-Portal-Tests verwenden eine vollständig synthetische Kontoseite
mit erfundenen Kontodaten und Entwürfen. Der deklarierte öffentliche Core-Bestand
umfasst 5869 Tests; diese Zahl ist kein bestandener Testlauf. Für den optionalen Node wurden zusätzlich 131 lokale Tests bestanden.
Die Abgrenzung
steht in [PUBLIC_TEST_SCOPE.md](PUBLIC_TEST_SCOPE.md).

Der verbindliche Core-Testweg ist `scripts/run_tests.py` im Repository-Root:

```sh
uv run --frozen --extra dev python scripts/run_tests.py dashboard control_center
```

Ohne Filter läuft die vollständige Suite. Optionale Laufzeiten müssen dafür
passend eingerichtet sein. Berichte ausgeführte, fehlgeschlagene und übersprungene
Tests getrennt; ein Testlauf ersetzt keine persönliche Liveabnahme.
Beitragshinweise stehen in [CONTRIBUTING.md](CONTRIBUTING.md), Hinweise zu
Schwachstellen und privaten Daten in [SECURITY.md](SECURITY.md).

## Lizenz und Drittanbieter

Der eigene SOLVIO-Code steht unter der [MIT-Lizenz](LICENSE).
Mitgeführte Drittanbieterbestandteile behalten ihre jeweiligen Lizenzen und
Urheberhinweise. Dazu gehören die vorhandenen Hermes- und Paketnachweise unter
`src/solvio/dashboard/assets/hermes/`. Diese Hinweise dürfen bei Änderungen
oder Weitergabe nicht durch die SOLVIO-Lizenz ersetzt werden.
