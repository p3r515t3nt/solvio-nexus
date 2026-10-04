# Adaptives Gedächtnis: technischer Vertrag

Der Core führt den maßgeblichen Gedächtnisbestand. Adaptive Verarbeitung
trennt Vorschläge von Entscheidungen: Ein Extraktor kann Aussagen vorschlagen;
die deterministische Policy prüft Herkunft, Inhalt, Evidenz und erforderliche
Bestätigung. Ein Modellvorschlag erteilt keine Berechtigung.

## Daten und Zustände

`candidates.py` speichert Annahmen und begrenzte Evidenz getrennt von bestätigten
Erinnerungen. `policy.py` entscheidet über zulässige Verarbeitung.
`extractor.py` liefert Vorschläge. `pipeline.py` verbindet den finalisierten
Turn mit diesen Schritten und dem bestehenden Memory-Service.

Offene Kandidaten sammeln Evidenz, warten auf Bestätigung oder sind strittig.
Eine Adoption wird vor dem kanonischen Schreibvorgang dauerhaft gebunden.
Ein Absturz in diesem Zustand ist ein ungelöster Vorgang, kein erfolgreicher
Gedächtniseintrag. Adoptierte, abgelehnte und abgelaufene Kandidaten sind terminal.
Unterdrückung, Korrektur, Vergessen und Purge müssen auch bei Wiederaufnahme
und abgeleiteten Indizes berücksichtigt werden.

## Herkunft und Evidenz

Nur geeignete bewusste Nutzereingaben aus verifizierten interaktiven App-
oder Accountsitzungen dürfen automatisch lernen. Assistenten, Werkzeuge und
externe Inhalte liefern keine automatische Selbstaussage des Besitzers.
Ein authentisiertes Raummikrofon belegt das Gerät, nicht den Sprecher;
seine Beiträge dürfen auf diesem automatischen Weg nicht adoptiert werden.

Eine zulässige ausdrückliche aktuelle Selbstaussage und eine abgeleitete
Vermutung werden unterschiedlich behandelt. Für eine abgeleitete Annahme
verlangt die bestehende Policy mindestens zwei unabhängige Gespräche.
Unabhängig bedeutet: verschiedene `conversation_id` und nicht am selben Tag.
Wiederholungen innerhalb einer Sitzung oder mehrere Gespräche am selben Tag
ersetzen diese Schwelle nicht. Risiko, sensible Inhalte, Regeln und Widersprüche
können weitere Sperren oder eine konkrete Bestätigung erfordern.

## Grenzen und Nachweise

Gedächtnis liefert Kontext und erweitert keine Aktionsrechte. Gerätevertrauen
ist weiterhin kein Sprecherbeweis. Der noch offene ausdrückliche „merk dir“-Weg
an Raummikrofonen ist in [TECH_DEBT](../../debt/TECH_DEBT.md) beschrieben.
Sprecheridentifikation ist ein separater Entwurf und keine fertige Zusicherung.

[Memory Contract](../../architecture/MEMORY_CONTRACT.md) beschreibt Speicherung,
Herkunft, Zeitmodell, Korrektur und Löschung. Die vorhandenen Tests prüfen sowohl
die Evidenzgrenzen als auch Zustandsübergänge und Wiederaufnahme. Ein bestandener
synthetischer Test ersetzt keine persönliche Liveabnahme.
