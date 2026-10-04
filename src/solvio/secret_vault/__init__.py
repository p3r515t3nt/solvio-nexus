"""SOLVIO Secret & Credential Vault — ein Verweis nach aussen, ein Wert nach innen.

Der Satz, der alles andere erklaert:

    Ein Agent darf WISSEN, dass ein Geheimnis existiert, und er darf eine
    legitime Faehigkeit anfordern, die es benutzt. Den Wert bekommt er nie.

Wer hier etwas sucht:

* `refs`      — `secret://<dienst>/<konto>`, die Form des Verweises
* `policy`    — wofuer ein Geheimnis benutzt werden darf. Vorgabe VERWEIGERN
* `envelope`  — der versionierte, authentifizierte Umschlag
* `keyring`   — der Hauptschluessel im macOS-Schluesselbund
* `store`     — Geheimtext, Policy und die Zugriffsspur in einer Datei
* `broker`    — der einzige Weg, auf dem ein Wert den Tresor verlaesst
* `admin`     — anlegen, ersetzen, sperren, loeschen
* `staging`   — wo ein Wert wartet, waehrend ein Mensch entscheidet
* `recovery`  — der passphrasengeschuetzte Umschlag fuer den Ernstfall
* `firewall`  — warum ein Passwort nie ins Gedaechtnis rutscht
* `health`    — wie es dem Tresor geht, ohne ihn aufzumachen
* `migration` — was aus `.env` hierher gehoert, und was ausdruecklich nicht

Absichtlich NICHT vorhanden, und das ist der Kern: eine Funktion, die ein Modell
aufrufen koennte, um einen Wert zu bekommen. Kein `get_secret`, kein `reveal`,
kein `export`. Auch nicht mit einem guten Argument.
"""
