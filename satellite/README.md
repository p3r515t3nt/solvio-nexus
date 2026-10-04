# SOLVIO Satellite

Optionaler, derzeit pausierter Sprachendpunkt für Raspberry Pi und passende
ALSA-Audiohardware. Der Mac Core bleibt für Zustand und Berechtigungen zuständig.
Der veröffentlichte Stand stammt aus dem letzten lokalen Wiederherstellungsstand.
Er wurde für diese Veröffentlichung nicht auf einem Pi live getestet.

Benötigt werden die Python-Pakete `websockets`, `numpy`, `openwakeword` und dessen
Modell-Laufzeit sowie die ALSA-Werkzeuge `arecord` und `aplay`. Eine vollständige
reproduzierbare Pi-Installation ist noch nicht als öffentlicher Installer enthalten.

Setze `SOLVIO_CORE_HOSTS` auf die Hostnamen deines eingerichteten Core.
Die eigene Satellite-Identität und der geheime Kopplungsschlüssel müssen über
den vorgesehenen Core-Provisionierungsweg eingerichtet werden; sie gehören
nicht in dieses Repository. Siehe `core/scripts/provision_satellite_credential.py`.

Wake-Word-Modelle werden separat benötigt. Eigene Modelle können im lokalen
Ordner `models/` oder über `SOLVIO_WAKEWORD_MODELS` bereitgestellt werden.
Das Repository verteilt keine persönlichen Sprachdaten oder ungeprüften Modelle.

`satellite.py` ist der Einstieg. Die Audio-Gerätekonfiguration und die Verbindung
müssen mit eigener Hardware geprüft werden. Starte den Endpunkt erst nach
Konfiguration und bewusster Freigabe der Mikrofonverwendung.
