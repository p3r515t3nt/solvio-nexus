# ADR-0018: Das iPhone wird Sprachendpunkt, ohne dabei Autoritaet zu werden

Status: Akzeptiert
Datum: 2026-08-24 (Milestone *SOLVIO iPhone Experience Implementation V1*,
Basis `bc9715f`; Sicherheitskern eingefroren seit
`approval-security-v1-hygiene`)

## Kontext

Das iPhone hatte bis hierher genau eine Aufgabe: **beweisen, dass der Mensch
zustimmt.** Face ID im Secure Enclave, eine frische App-Attest-Aussage, ein
gepinntes Zertifikat, eine widerrufbare Geraeteregistrierung. Sprache lief
woanders — ueber den Satelliten am Klartext-Port 8766.

Dieser Milestone gibt dem Telefon eine zweite Aufgabe: **ein Gespraech
eroeffnen.** Das ist genau die Art Erweiterung, bei der eine Grenze leise
verrutscht, weil beide Faehigkeiten auf demselben Geraet sitzen und sich
deshalb anfuehlen, als waeren sie dieselbe Befugnis.

Sie sind es nicht.

## Entscheidung

**Der Sprachweg haengt sich an den bestehenden Freigabeweg, statt einen
zweiten aufzumachen.** `/v1/voice` ist eine WebSocket-Route am schon
laufenden Freigabe-Gateway auf Port 8770 — dieselbe TLS-Verbindung mit
demselben gepinnten Zertifikat, dieselbe Geraeteregistrierung, dieselbe
Transportkennung. Angehaengt ueber das freigegebene Muster von
`control_center/routes.py`, nicht eingebaut: `src/solvio/security/` ist Zeile
fuer Zeile unveraendert, und `_authed_device` wird **aufgerufen**, nicht
nachgebaut.

**Nicht ueber 8766.** Der Satellitenport ist Klartext — `satellite_auth.py`
sagt das selbst ueber sich: keine PKI, keine Geraeteattestierung. Ein Telefon
traegt sein Mikrofon durch die Wohnung und ins fremde WLAN. Dazu kaeme ein
zweites Geheimnis, das irgendwie auf das Geraet muesste: die Kopplungsnutzlast
ist eingefroren, und der Sprachweg soll keine manuelle Uebertragung eines weiteren
gemeinsamen Geheimnisses voraussetzen. Und eine Satellitenkennung kennt keinen Widerruf.

**Was die Transportkennung beweist, und was nicht.** Sie beweist: ein
registriertes, attestiertes, nicht gesperrtes Geraet in einer erlaubten
Umgebung. Sie beweist **nicht**, dass ein Mensch etwas genehmigt hat. Eine
folgenreiche Handlung geht deshalb weiterhin denselben Weg wie vorher — als
Freigabe auf das iPhone, mit Face ID und frischer App-Attest-Aussage. Es gibt
keinen „per Sprache genehmigt"-Pfad, und gesprochene Worte sind keine
Biometrie. Dass die Kennung jetzt auch ein Gespraech eroeffnen darf, ist eine
bewusste Ausweitung ihrer Befugnis — aufgeschrieben und mit Tests
festgehalten, nicht nebenbei passiert.

**Ein Widerruf beendet ein LAUFENDES Gespraech.** `verify_transport_cred` lief
sonst genau einmal, beim Verbindungsaufbau; ein gesperrtes Geraet haette
weitergeredet, bis es von selbst auflegt. Die Pruefung wiederholt sich
waehrend der Sitzung alle 20 Sekunden.

**Die Sitzung ist die freigegebene `Session`.** Kein zweites Gehirn auf dem
Telefon, keine zweite Zustandsmaschine, kein direkter Modellaufruf aus Swift.
Was ein zweiter Endpunkt brauchte, war erstaunlich wenig: `Session` benutzt
von ihrem Transport genau eine Methode (`send`). Die Nachrichtenschleife ist
deshalb **zusammengelegt** worden (`pump_endpoint`) statt kopiert — zwei
Fassungen desselben Protokolls waeren zwei Fassungen, von denen bald nur eine
gepflegt wird.

## Bewusst NICHT entschieden

**Kein App Attest je Sprachsitzung.** Es wuerde eine Aenderung am Zaehlerverhalten der Attest-Aussagen **innerhalb
des eingefrorenen `control.py`** verlangen — das waere ein Auftauen des
Sicherheitskerns fuer eine Verbesserung, die den Freigabeweg nicht betrifft.
Als Schuld notiert, nicht heimlich getan.

**Kein schwaecherer TrustContext fuer die Telefonstimme.** Die Grenze bleibt unveraendert: das iPhone-Merkmal ist attestiert,
widerrufbar und TLS-gepinnt und damit strikt **staerker** als die geteilte
Geheimnisdatei des Satelliten. Ein eigener, schwaecherer Kontext haette eine
Unterscheidung erfunden, die die Tatsachen nicht hergeben.

**Kein Fernzugriff.** Der Sprachweg funktioniert im Heimnetz. 8770 wird nicht
ins Internet geoeffnet — weder fuer Sprache noch fuer sonst etwas.

## Folgen

Ein zweiter Endpunkt teilt sich den `_busy`-Riegel mit dem Satelliten: es
spricht genau eines von beiden. Ein besetzter Core antwortet dem Telefon mit
409, und die App sagt das in Menschenworten statt es wegzuschlucken.

Die zusammengelegte Schleife bedeutet: ein Fehler im Protokoll trifft ab jetzt
beide Endpunkte. Das ist der Preis dafuer, dass eine Verbesserung ebenfalls
beide erreicht — und `tests/test_iphone_voice_endpoint.py` haelt fest, dass es
bei genau einer Schleife bleibt.
