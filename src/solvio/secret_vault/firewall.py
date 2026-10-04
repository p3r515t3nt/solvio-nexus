"""Warum ein Passwort nie ins Gedaechtnis rutscht — und wohin es stattdessen geht.

Sagt jemand „merk dir, mein Passwort ist Hund1234", darf SOLVIO daraus keinen
Gedaechtnissatz machen. Er soll etwas anderes tun: **das gehoert in den Tresor.**

Was hier neu ist, ist nicht die Erkennung. Die gab es schon
(`solvio.memory.intent.looks_like_secret`) — deutschsprachig, kontextuell und
robust gegen die Wortzerlegung der Spracherkennung. Neu ist, dass sie an ALLEN
dauerhaften Schreibwegen sitzt statt nur an zweien. Vorher galt:

| Speicher | vorher |
|---|---|
| kanonisches Gedaechtnis (ausdrueckliches Merken) | geprueft |
| Adaptive-Kandidaten | geprueft |
| Gedaechtnis ueber `memory_correct` | **ungeprueft** |
| bestaetigter Kandidat | **ungeprueft** |
| Gespraechsverlauf (90 Tage Klartext) | **ungeprueft** |
| Proaktiver Eingang | **ungeprueft** |
| Obsidian-Projektion | nur Formregeln, schwaecher |

Der Verlauf ist der unangenehmste Eintrag in dieser Tabelle: ein gesprochenes
Passwort lag dort neunzig Tage im Klartext, unabhaengig davon, ob das Gedaechtnis
es abgelehnt hat.

**Zwei Antworten, nicht eine.** Ein Speicher, dessen Zweck eine Aussage ist
(Gedaechtnis, Wissen, Eingang), VERWEIGERT — dort waere ein halber Satz
schlimmer als keiner. Ein Speicher, dessen Zweck ein Verlauf ist (Gespraech),
REDIGIERT — den ganzen Redebeitrag zu verwerfen wuerde die Gespraechshoheit
brechen, und der Core besitzt das Gespraech.

**Und ehrlich zur Reichweite.** Eine Erkennung kennt nur, was jemand
aufgeschrieben hat. `looks_like_secret` findet benannte Zugangsdaten mit Wert
und bekannte Schluesselformen. Ein Satz wie „die Zahl ist 4711" ohne jeden
Begriff findet sie nicht — und das ist keine Luecke dieses Moduls, sondern die
Grenze jeder Mustererkennung. Deshalb ist der Tresor der eigentliche Weg und
diese Datei nur der Zaun daneben.
"""
from __future__ import annotations

import re

from solvio.logging_setup import get_logger

log = get_logger("vault")

#: Was SOLVIO sagt, statt zu speichern. Produktsprache, kein Fehlercode.
TRESOR_HINWEIS = ("Das gehoert in den Tresor, nicht ins Gedaechtnis. "
                  "Leg es im iPhone unter System → Tresor ab.")

#: Was im Gespraechsverlauf stehen bleibt, wo ein Zugangsdatum stand. Der
#: Redebeitrag verschwindet nicht — sein Wert schon.
TRANSCRIPT_MARKER = "[Zugangsdaten — nicht gespeichert]"


class CredentialRefused(ValueError):
    """Dieser Inhalt sieht wie ein Zugangsdatum aus und wird nicht abgelegt.

    Traegt einen kategorischen Grund und NIE den Text, der die Ablehnung
    ausgeloest hat. Diese Ausnahme kann bis in ein Werkzeugergebnis und damit
    ins Modell laufen (`tools/dispatcher.py` stellt `str(exc)` hinein).
    """

    def __init__(self, reason: str, where: str = "") -> None:
        super().__init__(f"credential_refused:{reason}")
        self.reason = reason
        self.where = where
        self.human_message = TRESOR_HINWEIS


def is_credential(text: str) -> bool:
    """Der EINE Praedikat im Haus. Bewusst keine zweite Musterliste.

    Der Import liegt in der Funktion, weil `solvio.memory` diesen Zaun an seinen
    eigenen Schreibwegen benutzt — ein Import auf Modulebene waere ein Zyklus.
    """
    from solvio.memory.intent import looks_like_secret
    return bool(text) and looks_like_secret(text)


def reason_for(text: str) -> str:
    from solvio.memory.intent import credential_reason
    return credential_reason(text or "")


def refuse_if_credential(text: str, *, where: str) -> None:
    """Der Zaun fuer Speicher, deren Zweck eine Aussage ist. Wirft oder schweigt."""
    if not text or not is_credential(text):
        return
    reason = reason_for(text)
    # Nur die Tatsache und der Ort. Kein Ausschnitt, keine Laenge, kein
    # Anfangsbuchstabe — solche „harmlosen" Auskuenfte sind der uebliche Weg,
    # auf dem ein Geheimnis doch noch in ein Protokoll rutscht.
    log.warning("vault.firewall_refused", where=where, reason=reason)
    raise CredentialRefused(reason, where)


def redact_if_credential(text: str, *, where: str) -> str:
    """Der Zaun fuer Speicher, deren Zweck ein Verlauf ist. Gibt Ersatz zurueck."""
    if not text or not is_credential(text):
        return text
    log.warning("vault.firewall_redacted", where=where, reason=reason_for(text))
    return TRANSCRIPT_MARKER


def has_key_shape(text: str) -> bool:
    """Nur die Wertformen (sk-…, ghp_…, AKIA…, JWT, PEM, Bearer) — ohne die
    Aussage-Heuristik („Meine PIN 4711"), die fuer Sprach- und Gedaechtnissaetze
    kalibriert ist und in Werkzeugmaterial (Code, Dokumentation, JSON — dort
    steht IMMER ein Doppelpunkt) auf Woerter wie „keys" anschlaegt."""
    from solvio.memory.intent import _KEY_SHAPES
    return bool(text) and any(shape.search(text) for shape in _KEY_SHAPES)


def refuse_if_key_shaped(text: str, *, where: str) -> None:
    """Der Zaun fuer Aufzeichnungen aus Werkzeugmaterial: eine Schluesselform
    verweigert (fail-closed), ein blosses Wort nicht. Gemessen am dritten echten
    Durchstich (19.09.2026 12:01): die Python-csv-Dokumentation („dict whose keys
    …") im Befehlsausgabe-Beleg liess den ganzen fertigen Auftrag als
    `native_result_not_retained` verfallen."""
    if not has_key_shape(text):
        return
    log.warning("vault.firewall_refused", where=where, reason="key_shaped_value")
    raise CredentialRefused("key_shaped_value", where)


#: Die Begriffe, die ein Zugangsdatum benennen (Praefix/Suffix erlaubt: `DB_PASSWORD`,
#: `GITHUB_TOKEN`, `api_key_v2`; `\bauth` schliesst `Author` aus).
#: `pass`/`pw` (Review Runde 12, H12-1d) mit BUCHSTABEN-Grenze statt Wortgrenze: `_` ist
#: ein Wortzeichen, und `DB_PASS=`, `SMTP_PASS:`, `ADMIN_PW=` fielen sonst durch alle drei
#: Zaeune (Review Runde 13, W13-2); `passed`, `compass`, `pwd` bleiben draussen.
#: Grossbuchstaben-Komposita (`PGPASS`, `MYSQL_PWD`) sind Bezeichner, `compass` ist ein Wort.
_TERMS = (r"(?:passw(?:or)?d|passwort|kennwort|passphrase|(?<![A-Za-z])pass(?![A-Za-z])|(?-i:[A-Z]{1,8}PASS)(?![A-Za-z])|"
          r"(?<![A-Za-z])pw(?![A-Za-z])|(?-i:[A-Z]{1,8}_?PWD)(?![A-Za-z])|geheimnis|\bpin|token|secret|"
          r"api[_-]?key|access[_-]?key|private[_-]?key|schl(?:ue|ü)ssel|zugangsdaten|credentials?|authorization|"
          r"\bauth(?![A-Za-z])|bearer|cookie|session[_-]?id)")
#: Ein WERT nach dem Trennzeichen: in Anfuehrungszeichen bis zum schliessenden Zeichen
#: (auch mit Leerzeichen: `PASSWORD="my secret pass"`), in Dreifach-Anfuehrungszeichen,
#: oder nackt (mindestens vier Zeichen ohne Leerzeichen/Anfuehrungszeichen). Review
#: Runde 11, F11-1: die Wertklasse `[^\s"']{4,}` liess jeden zitierten Wert mit
#: Leerzeichen durch. Ein Praefix wie `b'…'`/`f'…'`/`r'…'` und eine oeffnende Klammer
#: gehoeren zum Literal.
#: Ein maskiertes Anfuehrungszeichen (`'hu\'nter2'`) gehoert zum Wert; ein kurzes erstes
#: Tupelglied (`auth = ('app', 'hunter2xyz')`) steht vor dem Wert (Runde 12, H12-1e).
_TUPLE_HEAD = r"(?:(?:\"[^\"\n]*\"|'[^'\n]*')[ \t]*+,[ \t]*+)?"
_QUOTED_VALUE = (r"(?:\"\"\"[^\"]{4,}|'''[^']{4,}|\"(?:[^\"\\]|\\.){4,}\"|'(?:[^'\\]|\\.){4,}')")
#: Verlaufsmaterial kennt auch typografische Anfuehrungszeichen (`PASSWORD=„my secret pass“`,
#: Runde 12, H12-1c).
_VALUE = (r"(?:[bfru]{0,2}\(?[ \t]*+" + _TUPLE_HEAD + r"(?:" + _QUOTED_VALUE
          + r"|[„“”][^„“”]{4,}[“”]|[‚‘’][^‚‘’]{4,}[‘’]|(?!//)[^\s\"'()]{4,}))")
#: Eine annotierte Zuweisung (`token: str = …`, `token: dict[str, int] = …`) zwischen
#: Begriff und Wert. Possessiv (`*+`): zwei benachbarte Wiederholungen, die beide
#: Leerzeichen nehmen, kosteten auf einer 16000-Zeichen-Zeile mehr als eine Sekunde
#: (Review Runde 12, H12-2); ein Zeilenumbruch ist kein Trennzeichen mehr.
_ANNOTATION = r"(?::[ \t]*+[A-Za-z_][A-Za-z0-9_\[\],| ]*+)?"
#: `[\]\)]?`: ein Subskript oder Aufruf um den Begriff (`headers['Authorization'] = …`,
#: `os.environ['PASSWORD'] = …`; Runde 12, H12-1e).
_SEPARATOR = r"[A-Za-z0-9_\-]*+[\"']?[\]\)]?[ \t]*+" + _ANNOTATION + r"[:=][ \t]*+"
#: Zuweisungsgestalt in Werkzeugmaterial: Begriff, dann `:` oder `=`, dann ein Wert.
#: Review Runde 9, F9-1: die erste Fassung verlangte eine Wortgrenze vor dem Begriff
#: und das Trennzeichen direkt dahinter; `.env`-, Shell- und JSON-Formen fielen durch.
_ASSIGNED_CREDENTIAL = re.compile(_TERMS + _SEPARATOR + _VALUE, re.IGNORECASE)
#: Ein Begriff MIT Trennzeichen, der eine Zeile beschliesst („Passwort:", „TOKEN=",
#: YAML-Blockskalar „password: |") — der Wert folgt dann auf der naechsten Zeile und
#: faellt mit. Das Trennzeichen ist Pflicht: ohne es traf die Regel jede Zeile, die
#: auf einen Begriff endet (`return token`, `class Token:`) und warf ganze
#: Helferkandidaten fort (Review Runde 10, W10-1).
_CREDENTIAL_LABEL_AT_END = re.compile(
    _TERMS.replace(r"\bpin|", "") + r"[A-Za-z0-9_\-]*+[\"']?[ \t]*+[:=][ \t]*+(?:[|>][+-]?[ \t]*+|\\[ \t]*+)?$", re.IGNORECASE)
#: Ein YAML-Blockskalar (`password: |`, `key: >-`) nimmt JEDE tiefer eingerueckte
#: Folgezeile, nicht nur die naechste (Review Runde 12, H12-1a).
_BLOCK_SCALAR_AT_END = re.compile(r"[:=][ \t]*+[|>][+-]?[ \t]*+$")


#: Quelltext (`prose=False`): dieselbe Zuweisungsgestalt, aber nur ein STRING-LITERAL
#: ist ein Wert. `self.token = token`, `token = get_token()`, `tokenizer = Tokenizer()`,
#: `tokens = line.split()`, `secret = None`, `api_key = os.environ['X']` sind Code —
#: die vorige Fassung nahm jeden Nicht-Literal-Wert und warf einen Tokenizer-Helfer
#: als Zugangsdatum fort (Review Runde 12, H12-4; davor Runde 11, F11-1: ein Plural
#: mit Literal, `tokens = 'hunter2secret'`, ist ein Zugangsdatum). Ein Etikett am
#: Zeilenende zaehlt nur in Anfuehrungszeichen (`"password":` in einem Literal) —
#: `class Token:` und `for token in tokens:` sind Syntax (Review Runde 10, W10-1).
_ASSIGNED_CREDENTIAL_CODE = re.compile(
    _TERMS + _SEPARATOR + r"[bfru]{0,2}\(?[ \t]*+" + _TUPLE_HEAD + _QUOTED_VALUE, re.IGNORECASE)
#: Shell-, JSON- und Textdateien (`bare_values=True`) kennen keine Bezeichner als Wert:
#: `TOKEN=hunter2xyz` traegt das Zugangsdatum nackt; nur `None`/`null`/`True`/`False`
#: sind kein Wert.
#: Ein nackter Wert in Werkzeugmaterial: vor Prosa-Interpunktion nur, wenn er tokenartig
#: ist (Ziffer oder Symbol) — `Schluessel: element, zweck` ist eine Aufzaehlung,
#: `DB_PASSWORD=hunter2xyz,` ein Zugangsdatum (Runde 14, R14-H3); ein Verweis
#: (`secret://haendler/eintrag`) ist ein Pfad, kein Wert (Runde 13).
_TOKENISH = r"(?=[^\s\"'(),;]*[0-9_\-!@#$%^&*+=/\\])"
_VALUE_BARE = (r"(?:[bfru]{0,2}\(?[ \t]*+" + _TUPLE_HEAD + r"(?:" + _QUOTED_VALUE
               + r"|[„“”][^„“”]{4,}[“”]|[‚‘’][^‚‘’]{4,}[‘’]|(?!//)[^\s\"'(),;]{4,}+(?![,;])"
               + r"|(?!//)" + _TOKENISH + r"[^\s\"'(),;]{4,}+(?=[,;])))")
_ASSIGNED_CREDENTIAL_BARE = re.compile(
    _TERMS + _SEPARATOR + r"(?![bfru]{0,2}\(?[ \t]*+(?:None|null|True|False)\b)" + _VALUE_BARE, re.IGNORECASE)
_CREDENTIAL_LABEL_AT_END_CODE = re.compile(
    r"[\"']" + _TERMS.replace(r"\bpin|", "") + r"[A-Za-z0-9_\-]*+[\"'][ \t]*+[:=][ \t]*+(?:[|>][+-]?[ \t]*+)?$", re.IGNORECASE)
#: Buchspalten mit Werkzeugmaterial: hinter einem DOPPELPUNKT zaehlt ein nackter Wert nur
#: gemischt (Buchstabe UND Ziffer/Symbol, `Session-ID: 9f8e7d6c5b4a`) — „Tokens: 1200",
#: „Passwort-Feld: leer", „Schluessel: element", „Auth: fehlgeschlagen (401)" sind Prosa
#: und verweigern nichts (Review Runde 15, R15-2); eine ZUWEISUNG (`DB_PASSWORD=hunter2xyz`,
#: `refresh_token = abcdefghijklmnop`) und zitierte Werte wie bisher. Kein Tupelkopf: in
#: `{"session_id": "id", "portal": "amazon"}` ist der naechste Schluessel kein Wert (R15-4).
#: `_`, `-` und `.` sind Bezeichner-Kitt, kein Geheimniszeichen: `auth: api_key`,
#: `logged_out`, `claude.ai` sind Vokabular des Hauses (Review Runde 16, R16-W1 — der
#: Providergrenzen-Datensatz mit `auth: api_key` wurde verweigert, der Lauf endete falsch).
_TOKENISH_MIXED = r"(?=[^\s\"'(),;]*[0-9!@#$%^&*+=/\\])(?=[^\s\"'(),;]*[A-Za-z])"
#: Ein Schemawort vor dem Wert (`Authorization: Basic YWRtaW46…`, `Bearer …`) gehoert zur
#: Zeile, nicht zum Wert (R16-W2b).
_SCHEME = r"(?:(?:basic|bearer|digest|negotiate|token)[ \t]++)?"
_VALUE_MATERIAL = (r"(?:[bfru]{0,2}\(?[ \t]*+(?:" + _QUOTED_VALUE
                   + r"|[„“”][^„“”]{4,}[“”]|[‚‘’][^‚‘’]{4,}[‘’]|(?!//)" + _TOKENISH_MIXED + r"[^\s\"'(),;]{4,}+))")
_NOT_LITERAL = r"(?![bfru]{0,2}\(?[ \t]*+(?:None|null|True|False)\b)"
_ASSIGNED_CREDENTIAL_MATERIAL = re.compile(
    r"(?:" + _TERMS + _SEPARATOR.replace("[:=]", "=") + _NOT_LITERAL + _VALUE_BARE
    + r"|" + _TERMS + _SEPARATOR.replace("[:=]", ":") + _NOT_LITERAL + _SCHEME + _VALUE_MATERIAL + r")", re.IGNORECASE)
#: Ein REIN NUMERISCHER Wert (4–12 Ziffern) hinter einem starken Begriff — Passwort, PIN,
#: TAN, Geheimzahl, Zugangscode — ist ein Zugangsdatum, keine Zaehlung (R16-W2a/R16-H3:
#: „password: 12345678", „TAN: 482913", „Das Passwort ist 12345678"); „Tokens: 1200" ist
#: keiner dieser Begriffe. Zwischen Begriff und Zahl hoechstens ein Trenner oder ein Verb.
_NUMERIC_SECRET_MATERIAL = re.compile(
    r"\b(?:passw(?:or)?d|passwort|kennwort|passphrase|passcode|pin|tan|geheimzahl|zugangscode|sicherheitscode|otp)"
    r"(?-i:(?![a-z]))(?:(?:[ \t]*+[:=]|[ \t]++(?:ist|lautet|sei|is|equals|war|sind|are)\b)[ \t]*+[0-9]{4,12}\b"
    # ohne Trenner und Verb: eine Jahreszahl ist Prosa („Passwort 2025 rotieren", Runde 18, H18-2)
    r"|[ \t]++(?!(?:19|20)[0-9]{2}\b)[0-9]{4,12}\b)",
    re.IGNORECASE)
#: Eine AUSSAGE mit Wert — „Das Passwort lautet Sommer2024x", „Meine PIN 4711" (Verb
#: optional): Begriff, hoechstens 40 Zeichen, dann ein tokenartiger Wert (>= 4 Zeichen,
#: Ziffer oder Symbol). Ohne Wert ist es ein Satz („Was ist der Unterschied zwischen
#: Basic Auth und Bearer Tokens?"), kein Zugangsdatum (Review Runde 14, R14-W3/R14-H5).
#: Drei Wertgestalten: (1) gemischt — Buchstabe UND Ziffer/Symbol („Sommer2024x", „Hund1234"),
#: mit oder ohne Verb; (2) eine PIN aus 4–8 Ziffern hinter dem Begriff `pin`; (3) hinter dem
#: Verb ein grossgeschriebenes Wort (>= 6), das den Satz beschliesst („Mein Passwort ist
#: Sonnenblume."). „Tokens 1200 verbraucht" und „… Tokens ist wichtig" sind keine Werte.
_STATED_VERB = r"(?:[^\n:=\"']{0,40}?\b(?:ist|lautet|sei|is|equals|war|sind|waren|are|were)\b)"
_STATED_VALUE = re.compile(
    "(?:" + _TERMS + r"[A-Za-z0-9_\-]*+" + _STATED_VERB + r"?[ \t]*+[\"'„“]?(?![:/=])"
    r"(?=[^\s\"'„“,;.]*[0-9!@#$%^&*_+=/\\-])(?=[^\s\"'„“,;.]*[A-Za-z])[^\s\"'„“,;.]{4,}+"
    r"|\bpin(?-i:(?![a-z]))[A-Za-z_\-]*+" + _STATED_VERB + r"?[ \t:=]*+[0-9]{4,8}\b"
    r"|" + _TERMS + r"[A-Za-z0-9_\-]*+" + _STATED_VERB + r"[ \t]*+[\"'„“]?(?-i:[A-ZÄÖÜ][a-zäöüß]{5,})[\"'“]?[ \t]*+(?:[.!?]|$))",
    re.IGNORECASE)
#: In Buchspalten ohne die dritte Gestalt: ein nackter Wert zaehlt dort nur gemischt (R15-2), „Der
#: Token ist Erneuert" ist Prosa; `pin` endet als Wort („Pinned 4711 items", Runde 15, R15-7).
_STATED_VALUE_MATERIAL = re.compile(
    "(?:" + _TERMS + r"[A-Za-z0-9_\-]*+" + _STATED_VERB + r"?[ \t]*+[\"'„“]?(?![:/=])(?![0-9]{1,2}-stellig\b)"
    r"(?=[^\s\"'„“,;.]*[0-9!@#$%^&*_+=/\\-])(?=[^\s\"'„“,;.]*[A-Za-z])[^\s\"'„“,;.]{4,}+"
    r"|\bpin(?-i:(?![a-z]))[A-Za-z_\-]*+" + _STATED_VERB + r"?[ \t:=]*+[0-9]{4,8}\b)",
    re.IGNORECASE)
#: Kennungen des Hauses in Buchspalten, die der Core zusammensetzt (`"session_id":
#: "ps-…"` im Anforderungsvertrag einer Portal-Handlung): sie werden vor der Pruefung
#: maskiert, damit `session_id`/`cookie` fuer ECHTE Werte des Arbeiters Begriffe
#: bleiben (Review Runde 14, R14-H1 — enger als die Ausnahme der Begriffe).
_CORE_ID = re.compile(r"\b(?:ps-[0-9]+-[0-9]+|(?:ns|ar|at|as|aa|ac|ca|cd|pc|c)-[0-9a-f]{8,})\b")


#: Zugangsdaten OHNE Begriff (Review Runde 11, H11-1): Userinfo in einer URL
#: (`postgres://app:pw@host/db`) und `-u user:pw` eines Kommandozeilenwerkzeugs.
#: Das Schema ist begrenzt (`{0,30}`) und possessiv: `[a-z0-9+.\-]*` lief auf einer
#: 16000-Zeichen-Zeile aus Buchstaben quadratisch (Review Runde 12, H12-2).
#: Was ohne Begriff und ohne Form bleibt (`mysql -p<pw>`, `.pgpass`-Zeilen,
#: htpasswd-Hashes), ist DEBT-0291.
_URL_USERINFO = re.compile(r"(?<![a-z0-9+.\-])[a-z][a-z0-9+.\-]{0,30}+://[^/\s:@]{1,200}+:[^/\s@]{2,200}+@", re.IGNORECASE)
_CLI_USERPASS = re.compile(r"(?:^|\s)(?:-u|--user)[= ][ \t]*+[^\s:]{1,200}+:[^\s]{2,}")


def looks_like_credential_line(line: str, *, prose: bool = True, bare_values: bool = False,
                               core_material: bool = False, chat: bool = False) -> bool:
    """Eine Zeile Werkzeugmaterial, die ein Zugangsdatum TRAEGT oder benennt:
    Wertform, Zuweisungsgestalt (auch als Etikett am Zeilenende) und — bei
    Verlaufsmaterial (`prose=True`) — die Aussage-Heuristik des Gedaechtnisses
    („Mein Passwort ist Hund1234"); je Zeile, damit ein Treffer nur diese Zeile
    kostet und nie den ganzen Datensatz. Fuer Quelltext (`prose=False`) gilt sie
    nicht, und Zuweisung wie Etikett folgen der Code-Fassung: `{key: index}`,
    `return token`, `class Token:`, `self.token = token` sind Code, kein
    Zugangsdatum — ausser die Datei kennt keine Bezeichner (`bare_values=True`:
    Shell, JSON, Text), dann zaehlt auch ein nackter Wert (`TOKEN=hunter2xyz`).
    `core_material=True` (Buchspalten, die der Core zusammensetzt): Kennungen des
    Hauses (`ps-…`) sind maskiert, eine Aussage MIT Wert zaehlt, die
    Aussage-Heuristik ohne Wert nicht. `chat=True` (getippte Nachrichten und
    Titel): Wertformen, Zuweisungen, Etiketten und Aussagen mit Wert — nicht die
    fuer Sprach- und Merksaetze kalibrierte Verbregel, die eine Wissensfrage zum
    Zugangsdatum machte (Review Runde 14, R14-H5)."""
    if not line:
        return False
    if has_key_shape(line) or _URL_USERINFO.search(line) or _CLI_USERPASS.search(line):
        return True
    if chat:
        return bool(_ASSIGNED_CREDENTIAL.search(line) or _CREDENTIAL_LABEL_AT_END.search(line)
                    or _STATED_VALUE.search(line))
    if prose:
        return bool(_ASSIGNED_CREDENTIAL.search(line) or _CREDENTIAL_LABEL_AT_END.search(line)) or is_credential(line)
    if core_material:
        masked = _CORE_ID.sub("id", line)
        return bool(_CREDENTIAL_LABEL_AT_END_CODE.search(masked) or _ASSIGNED_CREDENTIAL_MATERIAL.search(masked)
                    or _STATED_VALUE_MATERIAL.search(masked) or _NUMERIC_SECRET_MATERIAL.search(masked))
    assigned = _ASSIGNED_CREDENTIAL_BARE if bare_values else _ASSIGNED_CREDENTIAL_CODE
    return bool(_CREDENTIAL_LABEL_AT_END_CODE.search(line) or assigned.search(line))


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def redact_lines_if_credential(text: str, *, where: str, chat: bool = False) -> tuple[str, bool]:
    """Zeilenweise Redaktion fuer Verlaufsmaterial: jede Zeile, die ein
    Zugangsdatum traegt oder benennt, wird durch den Marker ersetzt — und nach
    einem blossen Etikett („Passwort:") auch die naechste nichtleere Zeile, die
    kein Kommentar ist, denn dort steht der Wert; nach einem YAML-Blockskalar („password: |") jede tiefer
    eingerueckte Folgezeile, denn der Wert ist der ganze Block (Review Runde 12,
    H12-1a). Der Rest bleibt. Liefert (Text, ob etwas ersetzt wurde).

    `chat=True` (getippte Nachrichten und Titel): die Formen des Zeilenzauns plus
    Aussagen MIT Wert, ohne die Verbregel des Gedaechtnisses (Runde 14, R14-H5)."""
    if not text:
        return text, False
    lines = text.split("\n")
    changed = False
    take_next = False
    block_indent: int | None = None
    for index, line in enumerate(lines):
        if block_indent is not None:
            if not line.strip():
                continue
            if _indent(line) > block_indent:
                lines[index] = TRANSCRIPT_MARKER
                changed = True
                continue
            block_indent = None
        if take_next and line.strip():
            if line.lstrip().startswith("#"):
                continue   # a comment between label and value (Runde 12, H12-1b)
            lines[index] = TRANSCRIPT_MARKER
            changed = True
            take_next = False
            continue
        if looks_like_credential_line(line, chat=chat) if chat else looks_like_credential_line(line):
            label = bool(_CREDENTIAL_LABEL_AT_END.search(line))
            if label and _BLOCK_SCALAR_AT_END.search(line):
                block_indent = _indent(line)
                take_next = False
            else:
                take_next = label
            lines[index] = TRANSCRIPT_MARKER
            changed = True
    if changed:
        log.warning("vault.firewall_redacted", where=where, reason="line_redacted")
    return "\n".join(lines), changed


def refuse_if_credential_material(text: str, *, where: str) -> None:
    """Der Zaun der Buchspalten mit Werkzeugmaterial (ADR-0029: verweigern, nie
    bereinigen): eine Schluesselform oder eine STRUKTURELLE Zugangsdaten-Zeile
    (Zuweisung mit Wert, Etikett in Anfuehrungszeichen, URL-Userinfo,
    `-u user:pw`) verweigert die Zeile; die Aussage-Heuristik des Gedaechtnisses
    (Begriff + irgendein Doppelpunkt) gilt hier nicht — sie traf in JSON jeden
    Begriff und den Doppelpunkt, den der Core selbst setzt („Empfehlung des
    Spezialisten: … Schluessel …"), und liess fertige Auftraege verfallen
    (Anlaeufe y/z, Review Runde 13 F13-1/K13-1). Ein Wort verweigert nichts
    mehr; ein WERT — als Zuweisung, Etikett, URL-Userinfo, Schluesselform oder
    Aussage mit tokenartigem Wert („Das Passwort lautet Sommer2024x") —
    weiterhin. Was ohne Wertform bleibt, ist DEBT-0291."""
    if not text:
        return
    refuse_if_key_shaped(text, where=where)
    for line in text.split("\n"):
        if looks_like_credential_line(line, prose=False, core_material=True):
            log.warning("vault.firewall_refused", where=where, reason="credential_named_with_value")
            raise CredentialRefused("credential_named_with_value", where)


def any_credential(*texts: str) -> str:
    """Der erste Text, der wie ein Zugangsdatum aussieht — oder leer.

    Fuer Speicher mit mehreren Feldern (Eingang, Aufgabe), damit der Aufrufer
    nicht selbst eine Schleife baut und dabei ein Feld vergisst.
    """
    for text in texts:
        if text and is_credential(text):
            return text
    return ""
