"""M2 — erkennt ausdrückliche Merk-Absicht in einer Nutzeräußerung.

WARUM DAS HIER ENTSCHIEDEN WIRD, NICHT VOM MODELL

Das Modell darf einen Gedächtniseintrag VORSCHLAGEN. Ob die aktuelle Äußerung des
lokalen Besitzers wirklich eine dauerhafte Merk-Absicht enthält, entscheidet SOLVIO —
sonst könnte ein Modell aus einem beliebigen Satz, aus seiner eigenen Antwort oder aus
einem Tool-Ergebnis dauerhaftes Wissen erzeugen. In M2 V1 ist die Regel bewusst eng:
kein Automatismus, nur ausdrückliche Absicht.

Die Erkennung arbeitet auf dem FINALISIERTEN Transkript des Nutzerturns. Sie sieht
weder Assistententext noch Werkzeugausgaben noch abgerufene Dokumente.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_TRAILING = " \t\n,.:;-–—!?\"'„“"


def _fold(text: str) -> str:
    """Kleinschreiben und Umlaute falten, damit „Gedächtnis" wie „gedaechtnis" trifft."""
    lowered = (text or "").lower()
    for src, dst in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        lowered = lowered.replace(src, dst)
    lowered = unicodedata.normalize("NFKD", lowered)
    lowered = "".join(c for c in lowered if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", lowered).strip()


@dataclass(frozen=True)
class MemoryIntent:
    """Was SOLVIO in dieser Äußerung erkannt hat."""
    kind: str            # "remember" oder "correct"
    marker: str          # die auslösende Phrase, für die Provenienz
    payload: str         # der zu merkende Teil, im ORIGINAL-Wortlaut
    source_text: str     # die vollständige Äußerung, im Original

    @property
    def is_correction(self) -> bool:
        return self.kind == "correct"


# Ausdrückliche Merk-Absicht. Jeder Eintrag ist eine PHRASE aus ganzen Wörtern und wird
# nur an Wortgrenzen erkannt.
#
# Reproduziert, bevor das so war: die Erkennung suchte den Teilstring irgendwo im Satz.
# "Ich be_merke di_rekt einen Unterschied" und "Ich no_tiere di_rekt alles mit" erzeugten
# damit dauerhaftes Wissen — aus gewöhnlichen Sätzen, ohne dass jemand etwas merken lassen
# wollte. Das ist genau der Automatismus, den V1 ausschließen soll.
_REMEMBER_PHRASES = (
    "merk dir dauerhaft", "merke dir dauerhaft", "merk dir langfristig",
    "merke dir langfristig", "merk dir fuer immer", "merke dir fuer immer",
    "merk dir", "merke dir", "merk es dir", "merke es dir",
    # Live beobachtet: die ASR verschluckt das "dir". Gesagt wurde "Merk dir dauerhaft,
    # mein zweites Langzeit-Testwort ist Saphir 62", transkribiert kam an "MERKE
    # DAUERHAFT, mein zweites Langzeittestwort ist Saphir 62." — die Absicht wurde nicht
    # erkannt, und SOLVIO verlangte vom Nutzer ausgerechnet das "merk dir", das er gerade
    # gesagt hatte. Diese Kurzformen sind sicher, weil sie zusaetzlich einen Satz
    # eroeffnen muessen: "Ich merke dauerhaft ..." faellt weiterhin durch.
    "merk dauerhaft", "merke dauerhaft", "merk langfristig", "merke langfristig",
    "merk das dauerhaft", "merke das dauerhaft",
    # Korrekturformen, die zugleich ein Merkauftrag sind.
    "merk dir stattdessen", "merke dir stattdessen",
    "aendere die erinnerung", "andere die erinnerung", "korrigiere die erinnerung",
    "behalte im gedaechtnis", "behalt im gedaechtnis", "behalte im kopf",
    "speicher dir", "speichere dir", "speicher dauerhaft", "speichere dauerhaft",
    "notier dir", "notiere dir",
    "das sollst du dir merken", "das sollst du dir dauerhaft merken",
    "du sollst dir merken", "du sollst dir dauerhaft merken",
    "sollst du dir dauerhaft merken", "sollst du dir merken",
    "fuer spaeter wichtig", "wichtig fuer spaeter",
    "praege dir ein", "praeg dir ein",
)

# AUSDRÜCKLICHE Korrektur. "nicht mehr" steht bewusst NICHT mehr darin.
#
# Reproduziert: "Merk dir dauerhaft: Die Heizung im Bad geht nicht mehr an" wurde als
# Korrektur gelesen und löschte den unbeteiligten Eintrag "Die Heizung im Bad geht auf
# 24 Grad". "nicht mehr", "nicht", "richtig ist" sind Alltagsdeutsch, keine Ansage, dass
# eine gespeicherte Erinnerung falsch war. In einem Durchlauf zerstörten so 14 harmlose
# Sätze 9 von 12 gespeicherten Fakten.
_CORRECTION_PHRASES = (
    "korrektur", "korrigiere", "korrigier das", "zur korrektur",
    "das war falsch", "das stimmt nicht mehr", "das war ein fehler",
    "aendere die erinnerung", "andere die erinnerung", "aendere das gemerkte",
    "merk dir stattdessen", "merke dir stattdessen", "stattdessen gilt",
    "streich das und merk dir", "vergiss das und merk dir",
)

# Verneinter Auftrag: "merk dir das nicht", "das brauchst du dir nicht zu merken".
_NEGATIONS = ("nicht", "nichts", "kein", "keine", "keinen", "niemals", "bloss nicht")

_FILLER = frozenset(("das", "es", "dir", "sowas", "so", "etwas", "mal", "bitte",
                     "doch", "bloss", "blos", "davon", "daran", "ja", "eben"))


def _phrase_pattern(phrase: str) -> "re.Pattern[str]":
    """Eine Phrase als Wortfolge mit Wortgrenzen — nie als Teilstring im Wort."""
    return re.compile(r"(?<![\w])" + r"\s+".join(re.escape(w) for w in phrase.split())
                      + r"(?![\w])")


# Ein Merkauftrag steht am Satzanfang oder nach einer Satzgrenze — nicht mitten im Satz
# hinter einem Artikel. Ohne diese Bedingung las "Der Speicher dir gegenueber ist voll"
# das Substantiv "Speicher" als Imperativ und legte "gegenueber ist voll" ab.
_OPENERS = frozenset(("bitte", "und", "also", "dann", "ok", "okay", "ach", "so",
                      "ja", "nein", "hey", "solvio", "uebrigens"))


def _starts_a_clause(folded: str, start: int) -> bool:
    before = folded[:start].rstrip()
    if not before:
        return True                       # Satzanfang
    if before[-1] in ",.:;!?-–—":
        return True                       # nach einer Satzgrenze
    return before.split()[-1] in _OPENERS


_REMEMBER_RE = tuple((p, _phrase_pattern(p)) for p in _REMEMBER_PHRASES)
_CORRECTION_RE = tuple((p, _phrase_pattern(p)) for p in _CORRECTION_PHRASES)


def detect(transcript: str) -> MemoryIntent | None:
    """Ausdrückliche Merk-Absicht erkennen — oder None.

    None heißt: nichts wird dauerhaft gespeichert. Das ist der Normalfall und in V1
    ausdrücklich gewollt; ein gewöhnlicher Satz wird NICHT zu Langzeitwissen.
    """
    original = (transcript or "").strip()
    if not original:
        return None
    folded = _fold(original)
    hit = None
    for phrase, pattern in _REMEMBER_RE:      # längste Phrasen stehen zuerst
        for match in pattern.finditer(folded):
            if _starts_a_clause(folded, match.start()):
                hit = (phrase, match.start(), match.end())
                break
        if hit:
            break
    if hit is None:
        return None
    marker, start, end = hit
    payload = _extract_payload(original, folded, end)
    if not _has_substance(payload):
        return None
    # Korrekturabsicht wird NUR im Befehlsbereich gesucht — vor dem Auftrag und im
    # Auftrag selbst, nie in der Nutzlast.
    #
    # Reproduziert: die Vorfassung suchte im ganzen Satz. "Merk dir: Die KORREKTUR der
    # Physik-Klausur ist am Freitag", "Merk dir: Ich KORRIGIERE morgen die Klausuren" und
    # "Merk dir: Das war ein FEHLER von Bosch" galten damit als Korrekturen und konnten
    # fremde Fakten ueberschreiben. Das Wort stand im Inhalt, nicht im Auftrag.
    command_region = folded[:end]
    kind = "correct" if any(p.search(command_region) for _n, p in _CORRECTION_RE) else "remember"
    return MemoryIntent(kind=kind, marker=marker, payload=payload, source_text=original)


def _has_substance(payload: str) -> bool:
    """Bleibt nach Füllwörtern und Verneinungen überhaupt ein Inhalt übrig?

    "Merk dir das nicht" und "merk dir das bloss nicht" tragen keinen — sie sind eine
    Absage, kein Auftrag. "Merk dir: Anna arbeitet nicht mehr bei Siemens" trägt sehr wohl
    einen: das "nicht" gehört zum Fakt, nicht zum Auftrag. Eine frühere Fassung prüfte
    stattdessen, ob dicht hinter dem Marker irgendwo ein "nicht" steht, und verwarf damit
    den zweiten Satz — ein legitimer Merkauftrag ging verloren.
    """
    words = [w for w in re.findall(r"[\w\-]+", (payload or "").lower())
             if w not in _FILLER and w not in _NEGATIONS]
    return len(words) >= 2


def _extract_payload(original: str, folded: str, end: int) -> str:
    """Den Teil hinter dem Marker im ORIGINAL-Wortlaut herausschneiden.

    Gespeichert wird, was der Nutzer gesagt hat — nicht eine gefaltete Fassung. Weil
    das Falten Umlaute auf zwei Zeichen ausdehnt, wird die Position über die Wortzahl
    zurückgerechnet statt über den Zeichenversatz.
    """
    words_before = len(folded[:end].split())
    words = original.split()
    if words_before >= len(words):
        return ""
    return " ".join(words[words_before:]).strip(_TRAILING)


# ZUGANGSDATEN — deutschbewusst.
#
# Reproduziert an der Vorfassung: sie verlangte den Begriff als exakt gleiches Token und
# liess damit 13 von 14 realen Formen durch — "WLAN-Passwort", "WLANPasswort",
# "EC-Karten-PIN", "Bankpasswort", "Sommerregen2024 ist mein Passwort", "Meine TANs sind
# ...", "Meine PIN 4711". Deutsche Zusammensetzungen und Zuweisungen ohne Verb waren
# unsichtbar.
#
# Entscheidend ist der KOPF der Zusammensetzung, nicht ihr Anfang: "WLAN-Passwort" IST ein
# Passwort, "Passwort-Manager" ist ein Verwaltungsprogramm. Deshalb wird auf das Ende des
# zusammengezogenen Wortes geprueft und ein harmloser Kopf schlaegt den Treffer aus.

# Vollstaendige Begriffe (zusammengezogen verglichen, also auch "api-key" -> "apikey").
_CREDENTIAL_TERMS = frozenset((
    "passwort", "passwoerter", "kennwort", "kennwoerter", "password", "passwords",
    "pin", "pins", "geheimzahl", "geheimzahlen", "tan", "tans", "otp",
    "einmalcode", "einmalpasswort", "sicherheitscode", "bestaetigungscode",
    "verifizierungscode", "zugangscode", "entsperrcode", "pincode",
    "wiederherstellungscode", "wiederherstellungscodes", "backupcode", "backupcodes",
    "recoverycode", "recoverycodes", "apikey", "apikeys", "apischluessel",
    "zugangsschluessel", "geheimschluessel", "privatschluessel", "privatekey",
    "accesstoken", "refreshtoken", "zugangstoken", "sitzungstoken", "token", "bearer",
    "zugangsdaten", "anmeldedaten", "logindaten", "seed", "seedphrase",
    "passphrase", "mnemonic", "masterpasswort", "bankpasswort",
))

# Endungen, die eine Zusammensetzung eindeutig zu einem Geheimnis machen. Bewusst nur
# lange, unverwechselbare Stiele: "pin", "tan", "key" stehen NICHT hier, weil "Delphin",
# "Titan" und "Monkey" sonst faelschlich traefen.
_CREDENTIAL_HEADS = ("passwort", "passwoerter", "kennwort", "password", "passphrase",
                     "schluessel", "geheimzahl", "zugangsdaten", "anmeldedaten",
                     "logindaten", "token", "mnemonic")

# Nur mit ausdruecklicher Wortgrenze (eigenes Wort oder Teil einer Bindestrichkette).
# "code" steht bewusst NICHT hier: ein nacktes "Code" ist im Deutschen alltaeglich
# ("Die Garage oeffnet mit dem Code 4711" ist etwas, das ein Nutzer merken lassen will).
# Die benannten Formen — Zugangscode, Sicherheitscode, Recovery-Code, Backup-Code — sind
# als vollstaendige Begriffe erfasst und werden zusammengezogen erkannt.
_CREDENTIAL_PARTS = frozenset(("pin", "pins", "tan", "tans", "otp", "key", "keys",
                               "seed"))

# Koepfe, die aus einem Geheimnisbegriff etwas Harmloses machen.
_BENIGN_HEADS = ("manager", "managers", "verwaltung", "tresor", "safe", "datei", "dateien",
                 "liste", "feld", "felder", "schutz", "regel", "regeln", "richtlinie",
                 "aenderung", "wechsel", "abfrage", "eingabe", "vergessen", "generator",
                 "staerke", "laenge", "hinweis", "frage", "fragen")

# Zuweisung: mit Verb, mit Doppelpunkt — oder ganz ohne, wie in "Meine PIN 4711".
_ASSIGN_VERBS = frozenset(("ist", "sind", "lautet", "lauten", "heisst", "heissen",
                           "war", "waren", "waere", "bleibt", "gilt"))

# Aussagen ueber den ZUSTAND eines Geheimnisses geben keines preis. "Der Token ist
# abgelaufen" nennt keinen Wert; "Meine Passphrase ist Loewenzahn" schon. Die Liste ist
# bewusst klein und geschlossen — im Zweifel wird geblockt.
# Funktionswoerter duerfen NICHT Teil einer zusammengezogenen Zusammensetzung werden.
# Ohne diese Sperre verschmolz "mein passwort" zu "meinpasswort" — was auf den Kopf
# "passwort" endet — und "Mein Passwort Manager heisst Bitwarden" galt als Geheimnis.
_NO_FUSE = frozenset((
    "mein", "meine", "meinen", "meinem", "meiner", "meins", "dein", "deine", "deinen",
    "der", "die", "das", "den", "dem", "des", "ein", "eine", "einen", "einem", "eines",
    "und", "oder", "ist", "sind", "war", "waren", "im", "in", "am", "an", "auf", "fuer",
    "mit", "von", "zu", "zum", "zur", "ich", "du", "er", "sie", "es", "wir", "ihr",
))

_STATE_WORDS = frozenset((
    "abgelaufen", "ungueltig", "gueltig", "neu", "alt", "weg", "futsch", "erneuert",
    "kaputt", "falsch", "richtig", "gesperrt", "aktiv", "inaktiv", "sicher", "unsicher",
    "vergessen", "verloren", "geaendert", "zurueckgesetzt", "notwendig", "faellig",
    "leer", "voll", "kurz", "lang", "schwach", "stark", "eingerichtet", "aktiviert",
))
_VALUE = re.compile(r"^(?=.*[0-9a-z])[0-9a-z][0-9a-z_\-]{3,}$", re.IGNORECASE)

_KEY_SHAPES = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{12,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\bey[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\."),   # JWT
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    # further well-known provider forms (review round 12, H12-1f)
    re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]{16,}"),
    re.compile(r"\bglpat-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"\bhf_[A-Za-z0-9]{20,}"),
    re.compile(r"\bnpm_[A-Za-z0-9]{30,}"),
    # Ein Bearer-WERT ist tokenartig (>= 16 Zeichen aus der Token-Klasse, mindestens eine
    # Ziffer) — nie ein Woerterbuchwort („Bearer authentication"), `$VAR` oder `<PLATZHALTER>`
    # aus einer API-Dokumentation (Review Runde 14, R14-W2: eine Schluesselform verweigert
    # seit Runde 10 den ganzen Datensatz, die grobe Form kostete fertige Arbeiterturns).
    re.compile(r"\bbearer\s+(?![$<])(?=[A-Za-z0-9._~+/=\-]*[0-9])[A-Za-z0-9._~+/=\-]{16,}", re.IGNORECASE),
)
#: Die Heuristik des Hauses (Chat, Sprache, Gedaechtnis, proaktiver Speicher) bleibt fuer
#: die Bearer-Form so breit wie Produktion `aa6d2ae` (`\bbearer\s+\S{12,}`): R14-W2 hatte
#: das HAUSWEITE Praedikat verengt, und „Bearer abcdefghijklmnop" stand im Chat-Verlauf im
#: Klartext, waehrend Produktion ihn zum Marker machte (Review Runde 17, K17-1). Das
#: Werkzeugmaterial der Arbeiter (`has_key_shape`) behaelt die tokenartige Form.
_HOUSE_SHAPES = _KEY_SHAPES + (re.compile(r"\bbearer\s+\S{12,}", re.IGNORECASE),)


def _fuse(token: str) -> str:
    return token.replace("-", "").replace("_", "")


def _credential_positions(tokens: list[str]) -> list[int]:
    """Wo im Satz wird ein Geheimnis benannt — auch ueber Wortgrenzen hinweg?

    Reproduziert: die Erkennung sah nur EINZELNE Tokens. Die ASR trennt Komposita aber
    regelmaessig, und "mein recovery code ist X7712", "der zugangs code ...", "meine
    geheim zahl ..." kamen damit durch, wurden gespeichert und woertlich zurueckgesprochen.

    Deshalb werden auch benachbarte Zwei- und Dreiergruppen zusammengezogen geprueft:
    ["recovery", "code"] wird zusaetzlich als "recoverycode" bewertet. "Passwort Manager"
    ergibt "passwortmanager" — und faellt am harmlosen Kopf durch, wie es soll.
    """
    found: list[int] = []
    for index, token in enumerate(tokens):
        if _is_credential_token(token):
            # Die Zusammenziehung gilt in BEIDE Richtungen: "PIN Eingabe" ergibt
            # "pineingabe" und faellt am harmlosen Kopf durch, genau wie "Passwort-Manager".
            # Ohne das waere jede getrennte Erwaehnung eines Begriffs ein Treffer.
            if _joins_into_benign(tokens, index):
                continue
            found.append(index)
            continue
        if token in _NO_FUSE:
            continue                       # Funktionswoerter bilden keine Zusammensetzung
        for width in (2, 3):
            if index + width > len(tokens):
                continue
            group = tokens[index:index + width]
            if any(t in _NO_FUSE for t in group):
                continue
            if _is_credential_token("".join(group)):
                if _joins_into_benign(tokens, index):
                    break
                found.append(index)
                break
    return found


def _joins_into_benign(tokens: list[str], index: int) -> bool:
    """Bildet dieses Wort mit dem folgenden eine harmlose Zusammensetzung?"""
    for width in (2, 3):
        if index + width > len(tokens):
            continue
        group = tokens[index:index + width]
        if any(t in _NO_FUSE for t in group[1:]):
            continue
        joined = _fuse("".join(group))
        if any(joined.endswith(h) for h in _BENIGN_HEADS):
            return True
    return False


def _is_credential_token(token: str) -> bool:
    """Benennt dieses Wort ein Geheimnis?

    Geprueft wird das zusammengezogene Wort und sein KOPF. Ein harmloser Kopf
    ("Passwort-Manager") schlaegt den Treffer aus; kurze mehrdeutige Stiele wie "pin"
    zaehlen nur als eigenes Wort oder als Glied einer Bindestrichkette.
    """
    fused = _fuse(token)
    if any(fused.endswith(h) for h in _BENIGN_HEADS):
        return False
    if fused in _CREDENTIAL_TERMS:
        return True
    if any(fused.endswith(h) and len(fused) > len(h) for h in _CREDENTIAL_HEADS):
        return True
    parts = [p for p in token.split("-") if p]
    if len(parts) > 1 and parts[-1] in _CREDENTIAL_PARTS:
        return True
    return token in _CREDENTIAL_PARTS


def looks_like_secret(text: str) -> bool:
    """Ist das ein Zugangsdatum, das nicht ins Gedaechtnis gehoert?

    KONTEXTUELL, nicht nach Laenge. Geblockt wird, wenn ein Geheimnis BENANNT und ein Wert
    ZUGEWIESEN wird — mit Verb, mit Doppelpunkt oder ohne beides ("Meine PIN 4711") — oder
    wenn eine Zeichenkette offensichtlich ein Schluessel ist. Die Zuweisung darf auch
    VOR dem Begriff stehen ("Sommerregen2024 ist mein Passwort").

    Im Zweifel: blocken und es sagen, nie stillschweigend speichern.
    """
    raw = text or ""
    for shape in _HOUSE_SHAPES:
        if shape.search(raw):
            return True
    folded = _fold(raw)
    tokens = re.findall(r"[\w\-]+", folded)
    positions = _credential_positions(tokens)
    if not positions:
        return False
    if ":" in raw:
        return True
    for index, token in enumerate(tokens):
        if token not in _ASSIGN_VERBS:
            continue
        following = tokens[index + 1] if index + 1 < len(tokens) else ""
        if following and following not in _STATE_WORDS:
            return True
    # Verblos: dicht beim Begriff steht etwas, das wie ein Wert aussieht.
    for index in positions:
        window = tokens[max(0, index - 3):index] + tokens[index + 1:index + 5]
        for candidate in window:
            if (candidate in _ASSIGN_VERBS or candidate in _STATE_WORDS
                    or _is_credential_token(candidate)):
                continue
            if _VALUE.match(candidate) and any(c.isdigit() for c in candidate):
                return True
    return False


def credential_reason(text: str) -> str:
    """Warum abgelehnt wurde — kategorisch, ohne den Wert zu nennen."""
    for shape in _HOUSE_SHAPES:
        if shape.search(text or ""):
            return "key_shaped_value"
    return "credential_named_with_value"
