# Umfang der öffentlichen Tests

Diese Ausgabe enthält Produkt-, Sicherheits- und Runnerprüfungen des Core.
Sie übernimmt keine privaten Betriebsdaten, persönlichen Freigabebelege oder
die ursprüngliche private Git-Historie. Die folgenden Abgrenzungen sind
ausdrückliche Änderungen der Distribution, kein bestandener Testlauf.

## Persönliche Belegsuites

Sieben Suites mit zusammen 84 bisherigen Testidentitäten sind ausgeschlossen:

| Suite | Identitäten | Grund |
| --- | ---: | --- |
| `test_update_release_rules.py` | 32 | Persönliche Update-Helfer und gebundene Auslieferungsbelege. |
| `test_release_script_rules.py` | 15 | Persönlicher Übernahmeweg und Freigabedatensätze. |
| `test_contacts_release.py` | 6 | Kontaktdaten-Updateprüfung des privaten Auslieferungswegs. |
| `test_pi_rebuild_release.py` | 7 | Persönlicher Pi-Host, Pausenzustand und Release-Probe. |
| `test_push_release.py` | 3 | Push-Metadatenprüfung des privaten Update-Helfers. |
| `test_room_history_release.py` | 3 | Raum-Migration über private Release-Helfer und alte Git-Quellen. |
| `test_project_knowledge.py` | 18 | Private Orientierung, Fortschritt, Schuldregister und deren Historie. |

Diese Suites prüfen Helfer und Belege, die nicht zur öffentlichen Ausgabe
gehören. Sie bleiben im getrennten privaten Fortsetzungsbestand erhalten.
Die öffentlichen Produktprüfungen für Kontakte, Push, Gespräche, Raumzuordnung,
Aufträge, Berechtigungen und Satellitenprotokoll werden dadurch nicht pauschal entfernt.

Zusätzlich entfällt genau eine historische Milestone-Aussage in
`test_agent_runtime_capabilities.py`: der Vergleich zweier ursprünglicher
privater Commits auf unveränderten Sicherheitscode. Die aktuellen
Capability-, Dispatch- und Autoritätsprüfungen dieser Suite bleiben bestehen.

## Erhaltene Produkt- und Sicherheitsfälle

- Der unabhängige Secretgate aus der Orientierungssuite bleibt als eigener
  Dokumentprüfer erhalten. `test_unattended_autonomy.py` prüft sowohl die
  öffentlichen Dokumente als auch einen synthetischen positiven Gegenfall.
  Er bleibt vom Checkpoint- und Publisher-Scan unabhängig.
- Die Projektwissensmappe wird mit einer vollständig synthetischen
  Dokumentstruktur geprüft. Größenlimits, Kürzung, Herkunftsangaben,
  Auslassungen, erlaubte Pfade und Geheimnisverweigerung bleiben Gegenstand
  der vorhandenen Produktsuite.
- Die Observer-Kompatibilitätsfälle verwenden zwei einzelne frühere eigene
  Source-Module als feste, per Digest gebundene Fixtures. Der alte Authentisierer
  und der alte Store werden tatsächlich geladen; sie werden nicht nachgebildet.
- Die Migration des alten Freigabespeichers verwendet ebenfalls ein einzelnes
  früheres Store-Modul. Dafür ist kein privates Worktree oder Tag erforderlich.
- Die bestehenden Gesprächs-Rollback-Fixtures bleiben erhalten. Ihre
  Herkunftsprüfung bindet die veröffentlichten Bytes an feste Digests,
  statt Zugriff auf einen privaten Commit vorauszusetzen.
- Fünf vorhandene Produktsuites behalten ihre Sicherheitsbaumprüfung.
  Ein fester Source-Inhaltsmanifest und die expliziten überprüften
  Änderungshashes ersetzen den Zugriff auf den ursprünglichen privaten
  Freeze-Tag. Das ist eine Inhaltsbindung, keine behauptete Git-Abstammung.
- Die Studio-Portal-Fixture ist vollständig synthetisch. Kontodaten,
  Zeitstempel und sämtliche Entwurfstexte sind frei erfunden. Navigation,
  Auszug, Verweigerung und Freigabegrenzen werden weiterhin geprüft.
- Vier iPhone-Sourceprüfungen verwenden `ios/` im Repository-Root oder den
  vorhandenen Override `SOLVIO_IOS_REPO`. Fehlende Sources werden als
  nicht geprüft ausgewiesen.

Die drei Altmodule sind Source-Fixtures mit synthetisch erzeugtem Laufzeitzustand.
Sie enthalten keine alten Datenbanken, Konten, Freigaben oder privaten Git-Objekte.
Alle fünf gebundenen Altquellendateien werden vor dem Laden auf ihre festen
Digests geprüft. Die Integritätsdaten sind Testdaten, keine Produktbefugnisse.

## Kanonischer Prüfweg

`scripts/run_tests.py` bleibt unverändert der verbindliche Core-Runner.
AST-Bestand, tatsächliche Ausführung und versionierte Baseline werden weiterhin
getrennt verglichen. Die öffentliche Baseline wird ausschließlich mit dem
vorhandenen `scripts/update_test_baseline.py` an die ausdrücklich geänderten
Testidentitäten angepasst. Sie wird nicht vom Runner still repariert.

Nach den 84 Suite-Ausschlüssen und der einen historischen Aussage umfasst
der öffentliche deklarierte Bestand 5865 Identitäten. Vier behaltene
Provenienz-/Fixturefälle tragen passend zum öffentlichen Prüfgegenstand neue
Namen. Diese Zahl beschreibt die Baseline und keinen Ausführungserfolg.

Die erste Prüfung der öffentlichen Vorbereitung meldete Fehler. Nach der
Korrektur der Distribution ist eine erneute vollständige Prüfung erforderlich.
Es wird noch kein vollständiges grünes öffentliches Gate behauptet. Optionale
Anbieter-, native Laufzeit- und Gerätebedingungen müssen bei einer tatsächlichen
Prüfung ausgewiesen werden. Synthetische Tests ersetzen keine persönliche
Liveabnahme, insbesondere nicht die noch offene Flugsuche oder vollständige
Apple-Mail-Abdeckung.
