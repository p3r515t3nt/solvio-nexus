# Architektur

SOLVIO Nexus hat einen maßgeblichen Mac Core und mehrere Zugänge zu demselben
Zustand. Es ist eine persönliche Assistenzanwendung mit konkreten Integrationen.
Native Arbeitsprozesse bilden keine allgemeine Ausführungsplattform mit
unbegrenzten Rechten.

## Komponenten

```text
Browser-Dashboard ─┐
iPhone-App ────────┼── Core ── Fähigkeiten und geprüfte Dienstanbindungen
Sprachendpunkt ───┘      └── begrenzte native Arbeitsprozesse
```

Der Core verwaltet Gesprächsverlauf, Aufträge, Ergebnisse, Gedächtnis,
Berechtigungen und Freigabeentscheidungen. Die Oberflächen lesen daraus und
senden begrenzte Anfragen über die vorhandenen Schnittstellen. Das Schließen
eines Dashboards beendet nicht automatisch einen gespeicherten Auftrag; die
Ausführung benötigt weiterhin den verfügbaren Core und seinen Arbeitsweg.

| Bestandteil | Verantwortung |
| --- | --- |
| `src/solvio/` im Repository-Root | Python-Dienst, gemeinsame Verträge, Zustand und Ausführung. |
| Dashboard im Core | Chat, Überblick, Ideen, Aufgaben und Bibliothek. |
| `ios/` | Native iPhone-Oberfläche und gerätegebundene Freigabeentscheidungen. |
| `satellite/` | Optionaler Audiozugang zum Core. |
| `node/` | Optionaler Remote-Worker mit registrierten Fähigkeiten; der Core behält die Entscheidung. |
| Native Arbeitsprozesse | Begrenzte Auftragsarbeit und Ergebnislieferung. |

Windows nutzt den Browserzugang zum Mac Core. Mac-spezifische Integrationen
werden dadurch nicht zu Windows-Funktionen. Der Raspberry-Pi-Pfad ist optional
und derzeit pausiert; seine Einrichtung ist vom Core-Bootstrap getrennt.

## Rollen und Befugnisse

| Rolle | Grenze |
| --- | --- |
| Person | Richtet Verbindungen ein und entscheidet über erforderliche Freigaben. |
| Core | Prüft Fähigkeit, Umfang, Gültigkeit und Entscheidung vor der Aktion. |
| Browser und iPhone | Zeigen Zustand und senden gebundene Anfragen. |
| Sprachgerät | Liefert Audio; Geräteauthentisierung ist keine Sprecherfreigabe. |
| KI-Modell | Unterstützt Gespräch, Planung oder Auswertung; erteilt keine Rechte. |
| Arbeitsprozess | Bearbeitet den begrenzten Auftrag; Ergebnisse verleihen keine Rechte. |

Lesezugang, Transportauthentisierung und Zustimmung zu einer Aktion sind
getrennte Bedingungen. Freigabepflichtige iPhone-Entscheidungen werden im Core
mit den vorgesehenen Geräte- und Signaturprüfungen validiert. Umfang,
Ablauf und Widerruf einer Berechtigung dürfen nicht durch einen Modelltext,
ein Ergebnis oder einen neuen Oberflächenweg erweitert werden.

## Schnittstellen

Der Core bietet bestehende JSON-Schnittstellen für Zustand, Aufträge, Hinweise
und begrenzte Befehle. Browserzugang und iPhone-Zugang haben jeweils ihre
Authentisierung. Sprachsitzungen verwenden die vorhandenen WebSocket-Wege;
ein Satellite muss vor dem Sitzungsprotokoll authentisiert sein.

Schnittstellen sind in dieser Beta noch kein stabiler öffentlicher SDK-Vertrag.
Neue Clients sollen die bestehenden Verträge und Fehlerzustände übernehmen,
statt eine zweite Berechtigungs-, Aufgaben- oder Gedächtnisablage einzuführen.
Eine reine Zustandsanzeige darf keine erfolgreiche Ausführung behaupten.

## Auftragsweg

1. Ein Client oder eine vorhandene Regel stellt eine begrenzte Anfrage.
2. Der Core prüft verfügbaren Arbeitsweg, Datenumfang und erforderliche Freigabe.
3. Die passende Fähigkeit oder der native Arbeitsprozess bearbeitet den Auftrag.
4. Der Core übernimmt den geprüften Status und das Ergebnis in den gemeinsamen Zustand.
5. Oberflächen zeigen Fortschritt, Ergebnis, offenen Bedarf oder Fehler an.

Verbindungen, Kostenbedingungen und Anbieterzugänge müssen zum gewählten Weg
passen. Ein fehlender Weg oder eine abgelaufene Befugnis ist kein Anlass für
einen stillen Wechsel zu weitergehenden Rechten oder einem anderen Anbieter.

## Speicherung und Datenfluss

Persönlicher Zustand und Zugangsdaten liegen außerhalb der öffentlichen Sources.
Der Core kann Daten lokal speichern; konfigurierte Modelle oder Dienste können
für ihre Aufgabe ausgewählte Inhalte erhalten. Die konkrete Reichweite hängt
von der eingerichteten Verbindung und dem erlaubten Umfang ab.

Eine öffentliche Source-Installation ist kein Restore eines privaten Systems.
Konten, Geräteidentitäten, persönliche Freigaben und verschlüsselte Sicherungen
müssen getrennt behandelt werden. Sie gehören nicht in das öffentliche Repository.

## Grenzen dieser Beta

Die vollständige Apple-Mail-Abdeckung und die persönliche Liveabnahme der
Flugsuche sind offen. Ein Sourcepfad oder eine synthetische Regression belegt
keine universelle Livefunktion. Python-Paketmetadaten und Lockdatei bilden den
Core-Bootstrap im Repository-Root; native Laufzeiten, lokale Pfade, Dienststart und Geräteeinrichtung
bleiben gesonderte Betriebsaufgaben. Die bestehenden Drittanbieter-Lizenzen und
Notices sind Bestandteil der jeweiligen weitergegebenen Komponenten.
