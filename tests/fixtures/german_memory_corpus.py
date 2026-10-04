"""M2 — realistischer deutscher Korpus für Politik- und Abrufmessungen.

Nicht drei erfundene Sätze. Die frühere Kalibrierung stützte sich auf drei positive und
drei negative Anfragen und war damit wertlos: auf breiterem Bestand lieferten 25 % der
unbeteiligten Fragen einen Treffer.
"""

# Dauerhafte Fakten, wie sie ein Nutzer tatsächlich merken ließe.
MEMORIES = [
    "mein zweites Langzeit-Testwort ist Saphir 62",
    "Projekt Aurora startet im September",
    "Anna arbeitet bei Siemens in Erlangen",
    "das WLAN im Gartenhaus heisst Kastanie",
    "mein Fahrrad hat die Rahmennummer WBK4471",
    "der Zahnarzttermin ist am Dienstag um halb neun",
    "die Heizung im Bad geht auf 24 Grad",
    "der Drucker steht im Arbeitszimmer neben dem Regal",
    "ich trinke morgens keinen Kaffee",
    "meine Schwester heisst Miriam und wohnt in Leipzig",
    "der Muell wird donnerstags abgeholt",
    "die Garage oeffnet mit dem Code 4711",
    "mein Auto ist ein blauer Kombi",
    "die Katze heisst Nelli und ist elf Jahre alt",
    "der Hausarzt ist Doktor Berger in der Lindenstrasse",
    "ich bin allergisch gegen Haselnuesse",
    "das Passwort-Manager-Programm heisst Bitwarden",
    "die Bestellnummer fuer die Kueche ist 4711-2026",
    "unser Hochzeitstag ist der 14. Juni",
    "Papa hat am 3. Mai Geburtstag",
    "der Sicherungskasten ist im Keller links",
    "die Fussbodenheizung im Wohnzimmer ist zu traege",
    "mein Lieblingsrestaurant ist das Chiaro in der Altstadt",
    "der Reifendruck vorne soll 2,3 bar sein",
    "das Ferienhaus in Daenemark heisst Solgaarden",
    "die Spuelmaschine braucht Tabs mit Klarspueler",
    "Thomas kommt am Wochenende zu Besuch",
    "das Buero ist freitags ab vierzehn Uhr leer",
    "der Vertrag laeuft bis Ende Maerz",
    "ich habe die Steuererklaerung im April abgegeben",
]

# Fragen MIT erwartetem Treffer — Paraphrasen, Komposita, ASR-Varianten.
RELEVANT = [
    ("Was ist mein zweites Langzeit-Testwort?", "Saphir 62"),
    ("Was ist mein zweites Langzeittestwort?", "Saphir 62"),
    ("Was war mein zweites Langzeit Testwort?", "Saphir 62"),
    ("Wann startet Projekt Aurora?", "Aurora"),
    ("Wo arbeitet Anna?", "Siemens"),
    ("Wie heisst das WLAN im Gartenhaus?", "Kastanie"),
    ("Welche Rahmennummer hat mein Fahrrad?", "WBK4471"),
    ("Wann habe ich einen Zahnarzttermin?", "Dienstag"),
    ("Auf welche Temperatur stelle ich die Heizung im Bad?", "24 Grad"),
    ("Wo steht der Drucker?", "Arbeitszimmer"),
    ("Trinke ich Kaffee am Morgen?", "Kaffee"),
    ("Wie heisst meine Schwester?", "Miriam"),
    ("Wann kommt die Muellabfuhr?", "donnerstags"),
    ("Wie lautet der Garagencode?", "4711"),
    ("Was fuer ein Auto habe ich?", "Kombi"),
    ("Wie alt ist die Katze?", "elf"),
    ("Wer ist mein Hausarzt?", "Berger"),
    ("Wogegen bin ich allergisch?", "Haselnuesse"),
    ("Wann ist unser Hochzeitstag?", "14. Juni"),
    ("Wann hat Papa Geburtstag?", "3. Mai"),
    ("Wo ist der Sicherungskasten?", "Keller"),
    ("Wie heisst mein Lieblingsrestaurant?", "Chiaro"),
    ("Welchen Reifendruck brauche ich vorne?", "2,3 bar"),
    ("Wie heisst das Ferienhaus in Daenemark?", "Solgaarden"),
    ("Wann kommt Thomas zu Besuch?", "Wochenende"),
    ("Bis wann laeuft der Vertrag?", "Maerz"),
]

# Fragen OHNE Treffer — viele teilen Allerweltswoerter wie hat/ist/heisst/mein.
UNRELATED = [
    "Wie viele Bundeslaender hat Oesterreich?",
    "Wie heisst der Praesident von Frankreich?",
    "Wie spaet ist es gerade?",
    "Was kostet ein Brot beim Baecker?",
    "Wie weit ist es nach Berlin?",
    "Wann faehrt der naechste Zug nach Hamburg?",
    "Was gibt es heute zum Abendessen?",
    "Wie wird das Wetter am Wochenende?",
    "Wer hat den Fussball-Weltmeistertitel 2014 gewonnen?",
    "Wie hoch ist der Eiffelturm?",
    "Welche Hauptstadt hat Portugal?",
    "Wie lange dauert ein Fussballspiel?",
    "Was ist die Wurzel aus 144?",
    "Wie heisst der hoechste Berg der Alpen?",
    "Wann beginnt der Sommer?",
    "Was ist der Unterschied zwischen Nebel und Dunst?",
    "Wie viele Einwohner hat Muenchen?",
    "Welches Jahr ist Goethe gestorben?",
    "Wie funktioniert ein Kuehlschrank?",
    "Was ist ein Palindrom?",
    "Wer hat das Telefon erfunden?",
    "Wie schreibt man Rhythmus richtig?",
    "Welche Farbe hat der Himmel bei Sonnenuntergang?",
    "Wie lange muss ein Ei kochen?",
    "Was bedeutet die Abkuerzung ADAC?",
    "Wie viele Kontinente gibt es?",
]

# Sätze, die AUSDRÜCKLICH merken lassen sollen.
INTENT_POSITIVE = [
    "Merk dir: Projekt Aurora startet im September.",
    "Merk dir dauerhaft: Mein zweites Langzeit-Testwort ist Saphir 62.",
    "Bitte merk dir: Der Zahnarzt ist am Dienstag.",
    "Behalte im Gedaechtnis: Anna arbeitet bei Siemens.",
    "Das sollst du dir dauerhaft merken: Die Garage hat Code 4711.",
    "Merke dir dauerhaft, mein Auto ist ein blauer Kombi.",
    "Ok, merk dir: Der Muell wird donnerstags abgeholt.",
    "Speicher dir das Datum: Der Umzug ist am 3. Mai.",
    "Notier dir: Der Reifendruck vorne soll 2,3 bar sein.",
    "Praege dir ein: Ich bin allergisch gegen Haselnuesse.",
    "Merk dir dauerhaft: Die Heizung im Bad geht nicht mehr an.",
    "Merk dir: Das Buero ist freitags ab vierzehn Uhr leer.",
]

# Gewöhnliche Sprache, die NICHTS merken lassen soll.
INTENT_NEGATIVE = [
    "Ich bemerke direkt einen Unterschied zwischen den beiden Aufnahmen.",
    "Ich notiere direkt alles mit.",
    "Er hat sich das gemerkt.",
    "Das ist bemerkenswert.",
    "Notiere ich spaeter selbst.",
    "Merk dir das nicht.",
    "Merk dir das.",
    "Wie ist das Wetter heute?",
    "Ich habe mir das gemerkt.",
    "Der Speicher dir gegenueber ist voll.",
    "Das kann ich mir gut merken.",
    "Die Notiz dir zuliebe habe ich behalten.",
    "Kannst du dir sowas ueberhaupt merken?",
    "Bemerkenswert, wie schnell das ging.",
    "Ich speichere die Datei auf dem Stick.",
    "Das Programm speichert direkt in die Datenbank.",
    "Schalte bitte das Licht im Wohnzimmer an.",
    "Wie viel Uhr ist es?",
]

# Ausdrückliche Korrekturen.
CORRECTIONS = [
    ("Korrektur, merk dir: Projekt Aurora startet im Oktober, nicht im September.",
     "Projekt Aurora startet im September"),
    ("Das war falsch. Merk dir stattdessen: Anna arbeitet bei Bosch in Erlangen.",
     "Anna arbeitet bei Siemens in Erlangen"),
]

# Zugangsdaten, die abgelehnt werden MÜSSEN.
SECRETS_BLOCK = [
    "Merk dir: Mein Kennwort fuers Onlinebanking ist Sommerregen2024",
    "Merk dir: Meine PIN fuer die EC-Karte ist 4711",
    "Merk dir: Mein Passwort ist hunter2",
    "Merk dir: Die TAN lautet 884213",
    "Merk dir: Der API-Key ist sk-proj-ABC123defGHI456jkl789",
    "Merk dir: Mein Wiederherstellungscode ist 8842-1193-2210",
    "Merk dir: Das Zugangstoken lautet abcdefgh12345678",
    "Merk dir: Der Zugangscode fuer den Tresor ist 90210",
]

# Harmloses, das NICHT abgelehnt werden darf.
SECRETS_ALLOW = [
    "Merk dir: Mein Passwort-Manager heisst Bitwarden",
    "Merk dir: Das Ticket hat die UUID 550e8400-e29b-41d4-a716-446655440000",
    "Merk dir: Mein Fahrrad hat die Rahmennummer WBK4471",
    "Merk dir: Die Bestellnummer ist 4711-2026",
    "Merk dir: Ich nutze Zwei-Faktor-Authentifizierung",
    "Merk dir: Die Vorgangsnummer lautet AB-9931",
]
