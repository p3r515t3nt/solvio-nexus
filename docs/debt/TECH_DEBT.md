# Technische Grenzen und offene Arbeit

Dieser öffentliche Auszug beschreibt aktuelle Grenzen der Beta. Das private
historische Fortschrittsregister mit persönlichen Testläufen und Betriebsbelegen
gehört nicht zur Veröffentlichung. Ein Eintrag hier ersetzt keinen Testbericht
und behauptet keine erfolgreiche Abnahme mit einem echten Konto oder Gerät.

## DEBT-0099 – Sprecheridentität bei ausdrücklichen Erinnerungen

Ein ausdrückliches „merk dir das“ am Raummikrofon kann weiterhin auch von
einem Gast ausgelöst werden. Die Härtung der automatischen Gedächtnisbildung
ändert diesen ausdrücklichen Weg nicht. Das Restrisiko bleibt bestehen,
solange Sprecheridentität und die Bindung einer Äußerung an den Besitzer
nicht zuverlässig nachgewiesen werden. Ein bekannter Raum oder ein verbundenes
Mikrofon identifiziert keine Person. Der Entwurfsrahmen steht unter
[Speaker Identity](../design/speaker-identity-v1/README.md).

Diese Gedächtnisgrenze erteilt keine Erlaubnis für kostenpflichtige,
kommunizierende oder andere freigabepflichtige Aktionen. Deren Autorisierung
bleibt an den vorgesehenen Core- und iPhone-Entscheidungsweg gebunden.

## Sprache und Hintergrundaufträge

Aufnahme, Transkript, angenommener Auftrag, ausgeführte Arbeit und zugestelltes
Ergebnis sind verschiedene Zustände. Ein Verbindungsabbruch darf nicht als
Erfolg ausgegeben werden. Eine gespeicherte Aufgabe kann den Gesprächskanal
überdauern; ihr Fortschritt benötigt einen laufenden Core und den eingerichteten
Arbeitsweg. Sprachabläufe und Gerätewechsel brauchen weiterhin eine gesonderte
Prüfung an den tatsächlich eingerichteten Geräten.

## Recherche und Dienstanbindungen

Die persönliche Liveabnahme der Flugsuche und die vollständige Apple-Mail-
Abdeckung sind offen. Preise, Verfügbarkeit, Buchbarkeit und tatsächlich
zugestellte Nachrichten lassen sich aus einem bestehenden Codeweg oder einer
synthetischen Suite nicht ableiten. Anbieterzugänge, Datenumfang und konkrete
Aktionen müssen zur jeweiligen Installation passen. Fehlende oder widerrufene
Zugänge dürfen keine erfolgreich ausgeführte Arbeit vortäuschen.

## Einrichtung und Geräte

Es gibt keinen universellen Ein-Klick-Installer. Der Python-Bootstrap richtet
nicht automatisch native Anbieter, laufende Dienste, Apple-Signierung oder
persönliche Geräte ein. Die Launchd-Dateien unter deploy/ sind Vorlagen:
Platzhalter werden vor einer Installation durch echte absolute Pfade ersetzt.
Launchd führt keine Shell-Erweiterung von ~ oder $HOME in diesen Feldern aus.

Der optionale Pi-Weg ist pausiert. Die öffentliche Ausgabe enthält Quellcode,
keine persönlichen Datenträgerimages oder vorab freigegebenen Identitäten.
Gerätekopplung und echte Anbieteranmeldung erfolgen bei jeder neuen
Installation über die vorgesehenen Besitzerwege.

## Netzwerk und Zugriff

Für den Fernzugriff gilt: ein eingerichteter privater Zugang (Tunnel oder VPN), keine Portfreigabe.
Ein erreichbarer Dienst und ein verbundenes Gerät ersetzen weder die
Authentisierung noch eine Freigabe für eine konkrete Aktion. Beispielhosts,
TLS-Pfade und Anbieter-Client-IDs sind durch die eigenen geprüften Werte zu
ersetzen. Private Schlüssel und persönliche Konfiguration bleiben außerhalb
öffentlicher Versionsverwaltung.

## Tests und Nachweise

Die öffentliche Testsuite enthält synthetische Kontoseiten und feste
Source-Fixtures. Die Grenzen ihrer Distribution stehen in
[PUBLIC_TEST_SCOPE.md](../../PUBLIC_TEST_SCOPE.md). Ausführungsergebnis,
explizite Skips und fehlende optionale Laufzeiten sind getrennt auszuweisen.
Ein bestandener synthetischer Testlauf ist keine persönliche Liveabnahme.
