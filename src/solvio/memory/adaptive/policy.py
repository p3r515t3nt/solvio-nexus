"""Die Entscheidung — deterministisch, im Core, ohne Modellbeteiligung.

HIER LIEGT DIE GRENZE. Alles, was der Extraktor liefert, ist ein Vorschlag.
Was daraus wird, entscheidet dieses Modul — als Funktion, mit Tests und
Mutationsproben. Ein Modell hat in diesem Modul keinen Aufrufer.

DIE ASYMMETRIE, auf der die Sicherheitsrechnung beruht:

* Ein RISIKO-Flag des Modells blockiert verlaesslich. Meldet der Extraktor
  `hypothetical`, wird nicht adoptiert — Punkt.
* Ein UNBEDENKLICH-Urteil des Modells genuegt allein NIE. Jede Adoption muss
  zusaetzlich durch die deterministischen Wachen dieses Moduls.

Anders gesagt: das Modell darf uns aufhalten, aber es darf uns nie durchwinken.
Der Restfehler eines zu gutglaeubigen Extraktors wird nicht wegdefiniert,
sondern von den nachgelagerten Netzen aufgefangen — Sichtbarkeit in „Neu
gelernt", Ein-Satz-Korrektur, und vor allem: keinerlei Autoritaet.

DIE WICHTIGSTE WACHE ist `owner_self_assertion()`. Sie beantwortet die eine
Frage, an der die Ein-Aeusserungs-Regel haengt: Hat der authentifizierte
Besitzer das gerade UEBER SICH und in der GEGENWART behauptet — oder zitiert
er, spekuliert er, spricht er ueber jemand anderen, oder erzaehlt er von
frueher? Wer diese Wache aufweicht, macht aus „meine Frau mag Kaffee" eine
Vorliebe des Nutzers.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from solvio.contracts.memory import MemoryType, Sensitivity

# =====================================================================
# Quellen-Gate: WOHER darf etwas kommen — und was folgt daraus
# =====================================================================
#
# DIE UNTERSCHEIDUNG, DIE HIER GETROFFEN WIRD:
#
#     VERTRAUENSWUERDIGER ENDPUNKT  !=  BELEGTER SPRECHER
#
# Ein registrierter, attestierter Satellit beweist, dass DIESES GERAET
# spricht. Er beweist NICHT, dass der Mensch spricht, dem das Gedaechtnis
# gehoert. Ein Raummikrofon hoert alles: Gaeste, Kinder, den Fernseher.
#
# Ein Raummikrofon kann Aussagen von Gaesten oder aus Medien uebertragen.
# Die Authentisierung des Kanals macht daraus keine Selbstaussage des
# Besitzers. Solche Beitraege duerfen nicht automatisch gelernt werden.
#
# Bis es Sprecheridentifikation gibt (SPEAKER_IDENTITY_V1.md), gilt deshalb
# eine Klasse pro Herkunft — und automatisches Lernen nur fuer die Klasse, in
# der ein Mensch das Geraet bewusst in der Hand hat.


class SourceClass(str, Enum):
    """Was eine Herkunft ueber den SPRECHER belegt — nicht ueber den Kanal."""

    #: Entsperrtes, registriertes, attestiertes Geraet in einer laufenden
    #: interaktiven Sitzung. Belegt immer noch keine Stimme — aber jemand haelt
    #: es in der Hand und hat die Sitzung absichtlich geoeffnet. Ein Fernseher
    #: tut das nicht.
    VERIFIED_DEVICE_INTERACTIVE = "verified_device_interactive"

    #: Bewusste Eingabe in einer verifizierten Owner-Browsersitzung. Kein
    #: App-Attest-/Sprecherbeleg; der Account und die konkrete Eingabe sind belegt.
    VERIFIED_ACCOUNT_INTERACTIVE = "verified_account_interactive"

    #: Raummikrofon. Der Endpunkt ist belegt, der Sprecher nicht. Alles, was
    #: im Raum Geraeusche macht, kommt hier an.
    ROOM_MICROPHONE = "room_microphone"

    #: Alles andere. Hat ohnehin keinen Pfad.
    EXTERNAL = "external"


#: Welcher Kanal welche Klasse traegt. Die Zuordnung faellt im CORE und
#: ausschliesslich aus der Transportwahrheit — nie aus Modellangaben.
CHANNEL_CLASS: dict[str, SourceClass] = {
    # `/v1/voice` am Freigabe-Gateway: TLS-gepinnt, App Attest, registriertes
    # Geraet, laufende WebSocket-Sitzung.
    "voice_iphone": SourceClass.VERIFIED_DEVICE_INTERACTIVE,
    "task_iphone": SourceClass.VERIFIED_DEVICE_INTERACTIVE,
    "task_dashboard": SourceClass.VERIFIED_ACCOUNT_INTERACTIVE,
    "chat_iphone": SourceClass.VERIFIED_DEVICE_INTERACTIVE,
    "chat_dashboard": SourceClass.VERIFIED_ACCOUNT_INTERACTIVE,
    "voice_browser": SourceClass.VERIFIED_ACCOUNT_INTERACTIVE,
    # Der Pi im Wohnzimmer: HMAC-authentifiziert, aber ein Raummikrofon.
    "voice_satellite": SourceClass.ROOM_MICROPHONE,
}

#: Bewusste App-/Dashboard-Eingaben duerfen automatisch lernen.
#:
#: `ROOM_MICROPHONE` fehlt mit Absicht und nicht aus Versehen: es gibt keinen
#: Weg, aus einem Raumturn stillschweigend Gedaechtnis zu machen. Auch keinen
#: Kandidaten — ein Postfach voller Vorschlaege aus dem Fernsehprogramm waere
#: keine Vorsicht, sondern eine Verlagerung des Problems.
AUTO_LEARNING_CLASSES = frozenset({SourceClass.VERIFIED_DEVICE_INTERACTIVE,
                                   SourceClass.VERIFIED_ACCOUNT_INTERACTIVE})

#: Und die einzige Rolle. `assistant`, `tool`, `system` sind hier nicht
#: vergessen worden — sie duerfen nicht vorkommen.
ELIGIBLE_ROLE = "user"


def class_of(channel: str) -> SourceClass:
    """Die Klasse eines Kanals. Unbekanntes ist `EXTERNAL` — fail-closed."""
    return CHANNEL_CLASS.get(channel or "", SourceClass.EXTERNAL)


def may_auto_learn(channel: str) -> bool:
    """Darf aus diesem Kanal automatisch Gedaechtnis entstehen?"""
    return class_of(channel) in AUTO_LEARNING_CLASSES


@dataclass(frozen=True)
class OwnerTurn:
    """Ein finalisierter Turn eines authentifizierten Geraets.

    Der Name sagt „Owner", und genau das ist die Stelle, an der man sich irren
    kann: belegt ist das GERAET, nicht der MENSCH. Wie viel das jeweils wert
    ist, entscheidet `SourceClass` — und die setzt der Core aus der
    Transportwahrheit, nie ein Modell.
    """

    text: str
    channel: str
    role: str = ELIGIBLE_ROLE
    conversation_id: str = ""
    session_id: str = ""
    turn_id: str = ""
    message_id: str = ""

    @property
    def source_class(self) -> SourceClass:
        return class_of(self.channel)

    def is_eligible(self) -> tuple[bool, str]:
        """Darf aus diesem Turn automatisch Gedaechtnis entstehen?

        Vier Bedingungen, und die dritte ist die neue: der Kanal muss eine
        Klasse tragen, die ueber den Sprecher genug aussagt.
        """
        if self.role != ELIGIBLE_ROLE:
            return False, "not_owner_role"
        klass = self.source_class
        if klass is SourceClass.EXTERNAL:
            return False, "channel_not_eligible"
        if klass not in AUTO_LEARNING_CLASSES:
            # Der Kanal ist echt, der Sprecher ist es womoeglich nicht.
            return False, "speaker_unverified"
        if not (self.text or "").strip():
            return False, "empty_turn"
        return True, ""


# =====================================================================
# Normalisierung
# =====================================================================

def fold(text: str) -> str:
    """Kleinschreiben, Umlaute falten — damit „möchte" wie „moechte" trifft.

    Dieselbe Faltung wie in `memory/intent.py`; sie ist dort seit M2 im Einsatz
    und hat sich an echter ASR bewaehrt.
    """
    lowered = (text or "").lower()
    for src, dst in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        lowered = lowered.replace(src, dst)
    lowered = unicodedata.normalize("NFKD", lowered)
    lowered = "".join(c for c in lowered if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", lowered).strip()


def _has_stem(text: str, stems) -> str:
    """Wie `_has_phrase`, aber nur der ANFANG muss eine Wortgrenze sein.

    Deutsch beugt und setzt zusammen: `Sicherheitsabfrage` wird zu
    `Sicherheitsabfragen`, `Freigabe` zu `Freigaben`. Eine Wache mit strenger
    Wortgrenze an beiden Enden verliert genau diese Formen — gemessen an
    „Wuenscht sich weniger Sicherheitsabfragen", das damit durchkam.

    Die Grenze am ANFANG bleibt: sonst faende `ask` sich in `task` wieder.
    """
    for stem in stems:
        if re.search(r"(?<![a-z0-9])" + re.escape(stem), text):
            return stem
    return ""


def _has_phrase(text: str, phrases) -> str:
    """Die erste Phrase, die an einer WORTGRENZE trifft — oder "".

    Wortgrenzen sind hier kein Detail. Die M2-Erfahrung steht im Repository:
    eine Teilstringsuche machte aus „Ich be_merke di_rekt einen Unterschied"
    dauerhaftes Wissen. Derselbe Fehler waere hier teurer.
    """
    for phrase in phrases:
        if re.search(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])", text):
            return phrase
    return ""


# =====================================================================
# Die Wachen — deterministisch, im Core
# =====================================================================

#: Spekulation. „Stell dir vor, ich moechte Kaffee" ist keine Vorliebe.
HYPOTHETICAL = (
    "stell dir vor", "stell dir mal vor", "angenommen", "hypothetisch",
    "was waere wenn", "was waere, wenn", "mal angenommen", "gesetzt den fall",
    "nehmen wir an", "wenn ich mal", "spielen wir durch", "rein theoretisch",
    "theoretisch koennte", "imagine", "suppose", "hypothetically",
    "what if", "pretend",
)

#: Zitat und Bericht. Wer jemanden wiedergibt, behauptet nicht selbst.
REPORTED = (
    "er sagt", "sie sagt", "er sagte", "sie sagte", "er meint", "sie meint",
    "sie sagen", "man sagt", "es heisst", "laut", "angeblich", "hat gesagt",
    "haben gesagt", "hat behauptet", "steht geschrieben", "steht da",
    "steht drin", "im internet", "auf der webseite", "in der mail",
    "in der email", "in dem artikel", "zitat", "schreibt", "geschrieben",
    "he said", "she said", "they say", "according to", "the website says",
    "quote",
)

#: Dritte. Das Repository merkt sich Fakten UEBER Personen — aber es macht
#: daraus nie eine Vorliebe des Besitzers.
THIRD_PARTY = (
    "meine frau", "mein mann", "meine partnerin", "mein partner",
    "meine freundin", "mein freund", "meine mutter", "mein vater",
    "meine eltern", "meine tochter", "mein sohn", "meine kinder",
    "mein kind", "meine schwester", "mein bruder", "meine kollegin",
    "mein kollege", "meine chefin", "mein chef", "mein nachbar",
    "meine nachbarin", "mein hund", "meine katze", "meine schwiegermutter",
    "mein schwiegervater", "meine oma", "mein opa",
    "my wife", "my husband", "my partner", "my mother", "my father",
    "my daughter", "my son", "my sister", "my brother", "my colleague",
    "my boss", "my neighbor", "my neighbour",
)

#: Vergangenheit ohne Gegenwartsbezug. „Frueher mochte ich Kaffee" sagt
#: ausdruecklich, dass es NICHT mehr gilt.
PAST = (
    "frueher", "damals", "als kind", "in der vergangenheit", "seinerzeit",
    "ehemals", "einst", "mochte ich mal", "habe ich mal", "hatte ich mal",
    "war ich mal", "used to", "back then", "i once", "formerly",
)

#: Was nur JETZT gilt. Ein Gedaechtnis dafuer waere eine Luege ueber die
#: Lebensdauer der Aussage.
TRANSIENT = (
    "heute", "gerade", "im moment", "momentan", "jetzt gerade", "soeben",
    "eben", "gleich", "vorhin", "heute abend", "heute morgen", "heute nacht",
    "gerade eben", "aktuell", "in diesem moment", "right now", "today",
    "at the moment", "currently", "just now",
)

#: Ironie-Verdacht. Schwach erkennbar; die Liste ist ehrlich klein.
SARCASM = (
    "ja klar", "na klar", "wie toll", "ganz toll", "super toll",
    "ich liebe es ja", "natuerlich liebe ich", "ironie", "sarkasmus",
    "yeah right", "as if",
)

#: Erste Person, Gegenwart. OHNE einen dieser Marker gibt es keine
#: Selbstaussage — das ist die Bedingung, nicht ein Indiz.
FIRST_PERSON = (
    "ich", "mir", "mich", "mein", "meine", "meinen", "meinem", "meiner",
    "meins", "i", "me", "my", "mine",
)

#: Abloesungsmarker: die Aussage ersetzt ausdruecklich eine fruehere.
SUPERSESSION = (
    "nicht mehr", "inzwischen", "mittlerweile", "jetzt lieber", "ab jetzt",
    "ab sofort", "neuerdings", "seit neuestem", "doch lieber", "stattdessen",
    "anymore", "no longer", "these days", "now i prefer", "instead",
)

#: Sensible Lebensbereiche. Das Lexikon ist bewusst knapp und deckt die
#: Klassen, die der Contract als `SENSITIVE` fuehrt: Gesundheit, Finanzen,
#: Recht, Intimes. Der Core nimmt das MAXIMUM aus Scan, Lexikon und
#: Modell-Label — ein Modell kann verschaerfen, nie senken.
SENSITIVE_LEXICON = (
    # Gesundheit
    "diagnose", "krankheit", "krank", "diabetes", "krebs", "depression",
    "therapie", "therapeut", "psychiater", "psycholog", "medikament",
    "tabletten", "arzt", "aerztin", "klinik", "krankenhaus", "operation",
    "schwanger", "hiv", "allergie", "chronisch", "behinderung", "sucht",
    "alkoholiker", "burnout", "angststoerung", "blutdruck", "rezept",
    # Finanzen
    "gehalt", "einkommen", "schulden", "kredit", "insolvenz", "konto",
    "kontostand", "iban", "steuer", "steuern", "vermoegen", "erbe",
    "hypothek", "miete zahlen", "pfaendung", "hartz", "buergergeld",
    # Recht
    "anwalt", "anwaeltin", "gericht", "klage", "verfahren", "strafe",
    "vorstrafe", "scheidung", "sorgerecht", "testament", "polizei",
    # Intimes / Identitaet
    "sexuell", "sexualitaet", "beziehungsprobleme", "affaere",
    "therapiesitzung", "trauer", "gestorben", "tod meines", "tod meiner",
    "selbstmord", "suizid", "homosex", "transgender", "abtreibung",
    # Weltanschauung — nach dem WORT gesucht war zu wenig.
    #
    # Gemessen am ersten Produktivlauf: „religion", „glaube" und „politisch"
    # standen in der Liste, und trotzdem wurden Meinungen UEBER Religion und
    # Politik still adoptiert — weil ein Satz ueber den Islam das Wort
    # „Religion" nicht enthaelt. Die Architektur nennt genau diese Klasse
    # (§6.4: „Religion, Politik — im Zweifel immer die hoehere Stufe"), und
    # das Lexikon hat sie verfehlt.
    #
    # Jetzt wird nach dem THEMA gesucht. Die Liste faengt bewusst zu viel:
    # ein zu Unrecht zurueckgehaltener Satz kostet eine Rueckfrage, ein zu
    # Unrecht adoptierter kostet eine Meinung im Gedaechtnis, die niemand
    # hineingelegt hat.
    "religion", "religio", "glaube", "glaeubig", "konfession", "gott",
    "islam", "muslim", "moschee", "koran", "sunnit", "schiit", "scharia",
    "kopftuch", "christentum", "christlich", "kirche", "bibel", "judentum",
    "juedisch", "juden", "synagoge", "buddhis", "hinduis", "atheis",
    "politisch", "partei", "gewaehlt", "demokrat", "diktatur", "waehler",
    "migration", "migrant", "fluechtling", "asyl", "rassis", "nazi",
    "faschis", "kommunis", "sozialis", "kapitalis", "konservativ",
    "liberal", "populis", "ideolog", "patriot", "feminis", "gender",
    "klimawandel", "impfpflicht", "verschwoerung", "toleranz",
)


@dataclass(frozen=True)
class GuardResult:
    """Was die Wache gesehen hat. `reason` benennt die Kategorie, nie den Satz."""

    ok: bool
    reason: str = ""
    marker: str = ""


def owner_self_assertion(text: str) -> GuardResult:
    """Ist das eine GEGENWAERTIGE Aussage des Besitzers UEBER SICH SELBST?

    Die Reihenfolge ist Absicht: zuerst die Formen, die eine Aussage
    ENTKRAEFTEN (Spekulation, Zitat, Dritte, Vergangenheit), dann die
    Bedingung, die sie TRAGEN muss (erste Person). Ein Satz muss alle vier
    Huerden nehmen UND die Bedingung erfuellen.

    Wer diese Funktion aufweicht, macht aus „meine Frau mag Kaffee" eine
    Vorliebe des Nutzers. Sie ist deshalb mutationsgetestet.
    """
    folded = fold(text)
    if not folded:
        return GuardResult(False, "empty")

    marker = _has_phrase(folded, HYPOTHETICAL)
    if marker:
        return GuardResult(False, "hypothetical", marker)
    marker = _has_phrase(folded, REPORTED)
    if marker:
        return GuardResult(False, "reported_speech", marker)
    marker = _has_phrase(folded, THIRD_PARTY)
    if marker:
        return GuardResult(False, "third_party", marker)
    marker = _has_phrase(folded, SARCASM)
    if marker:
        return GuardResult(False, "sarcasm_possible", marker)

    # Vergangenheit zaehlt nur, wenn KEIN Abloesungsmarker sie in die Gegenwart
    # holt: „frueher mochte ich Kaffee, inzwischen nicht mehr" ist eine Aussage
    # ueber jetzt.
    marker = _has_phrase(folded, PAST)
    if marker and not _has_phrase(folded, SUPERSESSION):
        return GuardResult(False, "past", marker)

    if not _has_phrase(folded, FIRST_PERSON):
        # Keine erste Person: der Satz sagt nichts ueber den Besitzer aus.
        # Fail-closed — Abwesenheit eines Belegs ist kein Beleg.
        return GuardResult(False, "no_first_person")
    return GuardResult(True)


def is_transient(text: str) -> GuardResult:
    """Gilt das nur jetzt? Dann gehoert es nicht in eine Datei, die bleibt."""
    marker = _has_phrase(fold(text), TRANSIENT)
    return GuardResult(not marker, "transient" if marker else "", marker)


def has_supersession_marker(text: str) -> bool:
    """Sagt der Satz ausdruecklich, dass er etwas Frueheres abloest?"""
    return bool(_has_phrase(fold(text), SUPERSESSION))


def lexical_sensitivity(text: str) -> Sensitivity:
    """Die Einstufung aus dem Core-Lexikon. Nur `PERSONAL` oder `SENSITIVE`.

    Sie ist bewusst grob: sie soll die haeufigen Klassen deterministisch
    fangen, nicht Feinheiten beurteilen. Feinheiten darf das Modell
    beisteuern — aber nur nach oben.
    """
    folded = fold(text)
    for word in SENSITIVE_LEXICON:
        if word in folded:
            return Sensitivity.SENSITIVE
    return Sensitivity.PERSONAL


_RANK = {Sensitivity.PUBLIC: 0, Sensitivity.PERSONAL: 1,
         Sensitivity.SENSITIVE: 2, Sensitivity.SECRET_REFERENCE: 3}


def strictest(*values: Sensitivity) -> Sensitivity:
    """Das Maximum. Ein Modell kann verschaerfen, nie senken."""
    best = Sensitivity.PUBLIC
    for value in values:
        if value is not None and _RANK[value] > _RANK[best]:
            best = value
    return best


# =====================================================================
# Nuetzlichkeit
# =====================================================================

#: Untergrenzen fuer eine Aussage. Bewusst NIEDRIG: „Mag Kaffee." ist eine
#: brauchbare Vorliebe, und eine Laengenhuerde, die sie verwirft, waere kein
#: Nuetzlichkeitstest, sondern ein Zufallsgenerator. Was wirklich nichts sagt,
#: faengt die Fuellwortliste — die prueft Bedeutung statt Zeichen.
MIN_STATEMENT_CHARS = 8
MIN_STATEMENT_WORDS = 2
MAX_STATEMENT_CHARS = 400

#: Fuellwoerter, die allein keinen Inhalt ergeben.
#: EINZELNE Token, keine Phrasen: geprueft wird Wort fuer Wort, und eine
#: zweiwoertige Eintragung wie „alles klar" haette nie treffen koennen.
FILLER_ONLY = (
    "ja", "nein", "ok", "okay", "gut", "danke", "bitte", "hallo", "tschuess",
    "genau", "vielleicht", "hm", "ach", "so", "alles", "klar", "na", "also",
    "eben", "halt", "mal", "schon", "doch",
)


#: Handlungen, die von der BEDIENUNG SOLVIOs handeln, nicht vom Leben des
#: Menschen. „Der Besitzer hat freigegeben" ist kein Wissen ueber ihn.
INTERACTION_VERBS = (
    "freigegeben", "freigabe", "bestaetigt", "bestaetigen", "zugestimmt",
    "genehmigt", "abgelehnt", "gemerkt", "gespeichert", "geloescht",
    "vergessen", "korrigiert", "wiederholt", "gefragt", "geantwortet",
    "erlaubt", "quittiert", "face", "id", "approved", "confirmed",
)

#: Woerter, die in so einem Satz nur den Rahmen bilden.
_META_FRAME = (
    "der", "die", "das", "besitzer", "nutzer", "er", "sie", "es", "ich",
    "hat", "habe", "hab", "ist", "wurde", "worden", "gerade", "soeben",
    "eben", "schon", "bereits", "jetzt", "mir", "mich", "sein", "seine",
    "seinen", "mein", "meine", "und", "mit", "per", "via", "dem", "den",
)


def is_interaction_meta(statement: str) -> GuardResult:
    """Handelt dieser Satz von der BEDIENUNG statt vom Menschen?

    Live gemessen: „Ich habe freigegeben" wurde zu „Der Besitzer hat
    freigegeben." — ein dauerhafter Eintrag ueber einen Knopfdruck. Er hat
    jede Wache passiert: erste Person, Gegenwart, nicht fluechtig, nicht
    sensibel. Der Nuetzlichkeitstest hat ihn durchgelassen, weil er nach
    Zeichen und Fuellwoertern sucht, nicht nach Bedeutung.

    Die Pruefung ist bewusst ENG: sie schlaegt nur an, wenn NICHTS uebrig
    bleibt ausser Rahmenwoertern und einem Bedienungsverb. „Ich habe meiner
    Frau gesagt, dass ich Kaffee mag" behaelt „frau" und „kaffee" und kommt
    durch — eine breitere Regel wuerde echte Aussagen mitverwerfen.
    """
    words = [w.strip(".,;:!?-\"'") for w in fold(statement).split()]
    words = [w for w in words if w]
    if not words:
        return GuardResult(True)
    rest = [w for w in words if w not in _META_FRAME]
    if not rest:
        return GuardResult(True)     # nur Rahmen: faengt schon `is_useful`
    if all(w in INTERACTION_VERBS for w in rest):
        return GuardResult(False, "interaction_meta", rest[0])
    return GuardResult(True)


def is_useful(statement: str) -> GuardResult:
    """Wird das in kuenftigen Gespraechen wahrscheinlich gebraucht?

    Deterministische Anteile: Laenge, Spezifitaet, kein reines Fuellwort, kein
    Transient-Marker. Was hier durchfaellt, war nie ein Kandidat.
    """
    text = (statement or "").strip()
    if len(text) < MIN_STATEMENT_CHARS:
        return GuardResult(False, "too_short")
    if len(text) > MAX_STATEMENT_CHARS:
        return GuardResult(False, "too_long")
    # Satzzeichen abstreifen, BEVOR gegen die Fuellwortliste geprueft wird.
    # Ohne das war „genau." kein Fuellwort und „Ja genau." galt als Wissen.
    words = [w.strip(".,;:!?-\"'") for w in fold(text).split()]
    words = [w for w in words if w]
    if len(words) < MIN_STATEMENT_WORDS:
        return GuardResult(False, "too_few_words")
    if all(w in FILLER_ONLY for w in words):
        return GuardResult(False, "filler_only")
    transient = is_transient(text)
    if not transient.ok:
        return GuardResult(False, "transient", transient.marker)
    meta = is_interaction_meta(text)
    if not meta.ok:
        return GuardResult(False, meta.reason, meta.marker)
    return GuardResult(True)


# =====================================================================
# Kategorien
# =====================================================================

#: Was aus einer eigenen Aussage automatisch gelernt werden darf.
AUTO_STATED = frozenset({
    MemoryType.PREFERENCE, MemoryType.PROJECT, MemoryType.USER,
    MemoryType.PEOPLE,
})

#: Was aus einem Muster automatisch gelernt werden darf. Nur Vorlieben:
#: eine Inferenz hat keinen Wortlaut des Nutzers hinter sich, und
#: Identitaetsfakten oder Aussagen ueber Dritte verdienen mehr als ein Muster.
AUTO_INFERRED = frozenset({MemoryType.PREFERENCE})

#: Verhaltenssteuernd — nie still. Hoechstens eine Frage.
ASK_ONLY = frozenset({MemoryType.RULE, MemoryType.STANDING_INTENT})

#: Nicht in dieser Ausbaustufe. Weltwissen und Ereignisse bleiben dem
#: ausdruecklichen Weg vorbehalten; `WORKING` hat die Lebensdauer „Session".
NOT_IN_V1 = frozenset({MemoryType.SEMANTIC, MemoryType.EPISODIC,
                       MemoryType.WORKING})

#: Regeln, die Vorsicht SENKEN wuerden, schlaegt die Pipeline gar nicht vor.
#: TRUST_BOUNDARY: ein Memory-Record kann restriktiver machen, nie
#: freischalten. Eine Regel „frag nicht mehr" waere der Versuch, genau das zu
#: kippen — und existiert nur ueber den ausdruecklichen „merk dir"-Weg, wo sie
#: ebenfalls keine Freigabepflicht unterschreiten kann.
#: Begriffe, die von einer Rueckversicherung handeln. Eine Regel, die einen
#: davon abschwaecht, wuerde Vorsicht senken.
GUARDRAIL_WORDS = (
    "fragen", "frag", "frage", "gefragt", "nachfrage", "nachfragen",
    "rueckfrage", "rueckfragen", "bestaetigung", "bestaetigen", "bestaetigt",
    "freigabe", "freigaben", "freigeben", "genehmigung", "genehmigen",
    "erlaubnis", "face id", "faceid", "sicherheitsabfrage", "warnung",
    "hinweis", "confirmation", "confirm", "approval", "approve", "ask",
    "asking", "permission",
)

#: Und die Worte, die etwas davon WEGNEHMEN.
WEAKENING_WORDS = (
    "nicht", "nie", "niemals", "kein", "keine", "keinen", "keiner", "ohne",
    "weniger", "aufhoeren", "unterlassen", "weglassen", "ueberspringen",
    "ueberspring", "skip", "spar", "erspare", "verzichte", "einfach",
    "direkt", "sofort", "no", "not", "never", "stop", "without", "less",
)

#: Und Formen, die auch ohne Verneinung Vorsicht senken.
PERMISSIVE_PHRASES = (
    "mach einfach", "mach das einfach", "einfach machen", "einfach ausfuehren",
    "immer erlauben", "immer zulassen", "automatisch ausfuehren",
    "always allow", "just do it", "auto approve", "auto-approve",
)


def is_permissive_rule(text: str) -> GuardResult:
    """Wuerde diese Regel Vorsicht SENKEN? Dann wird sie nie vorgeschlagen.

    NICHT ueber eine Liste fertiger Saetze. Die erste Fassung tat das und liess
    „Moechte nicht mehr nach Freigaben gefragt werden" durch — dieselbe Absicht,
    andere Wortstellung, und schon war eine Regel zur Rueckfrage geworden, die
    Rueckfragen abschaffen will. Eine Phrasenliste ist gegen Umformulierung
    wehrlos.

    Stattdessen strukturell: kommt ein Begriff aus der Rueckversicherung
    (`fragen`, `Freigabe`, `Face ID`, `Bestaetigung` …) mit irgendeiner
    Abschwaechung (`nicht`, `kein`, `ohne`, `weniger` …) im selben Satz vor,
    gilt die Regel als vorsichtssenkend. Das faengt auch Formulierungen, die
    niemand vorhergesehen hat — und im Zweifel faengt es zu viel. Genau
    richtig: eine faelschlich verweigerte Regel kostet eine Rueckfrage, eine
    faelschlich uebernommene kostet die Rueckversicherung selbst.
    """
    folded = fold(text)
    marker = _has_phrase(folded, PERMISSIVE_PHRASES)
    if marker:
        return GuardResult(False, "permissive_rule", marker)
    guard = _has_stem(folded, GUARDRAIL_WORDS)
    if not guard:
        return GuardResult(True)
    weaken = _has_stem(folded, WEAKENING_WORDS)
    if weaken:
        return GuardResult(False, "permissive_rule", f"{weaken}+{guard}")
    return GuardResult(True)


# =====================================================================
# Die Entscheidung
# =====================================================================

#: Was die Policy anordnen kann. Mehr gibt es nicht.
ADOPT = "adopt"          # kanonisch als solvio_inference schreiben
ASK = "ask"              # ASK_PENDING — nur der Mensch darf adoptieren
CONTEST = "contest"      # CONTESTED — widerspricht aktivem Record
GATHER = "gather"        # liegen lassen, Evidenz sammelt sich
DISCARD = "discard"      # verwerfen, rueckstandsfrei


@dataclass(frozen=True)
class Decision:
    """Was mit einem Vorschlag geschieht — und warum, kategorisch benannt.

    `reason` ist ein Code, nie ein Satz des Menschen. Er darf ins Log.
    """

    action: str
    reason: str
    sensitivity: Sensitivity = Sensitivity.PERSONAL
    ask_reason: str = ""
    contested_memory_id: str = ""
    marker: str = ""

    @property
    def creates_candidate(self) -> bool:
        return self.action in (ADOPT, ASK, CONTEST, GATHER)


@dataclass
class Proposal:
    """Der validierte Modellvorschlag, wie die Policy ihn sieht.

    Er ist bereits durch `extractor.parse()` gegangen: Felder existieren, Typen
    stimmen, unbekannte Werte sind verworfen. Was hier ankommt, ist
    wohlgeformt — aber deswegen noch lange nicht wahr.
    """

    statement: str
    kind: str                                  # stated | inferred
    memory_type: MemoryType
    subject: str
    about: str = "self"                        # self | third_party | world
    sensitivity: Sensitivity = Sensitivity.PERSONAL
    flags: frozenset[str] = field(default_factory=frozenset)
    valid_until: str = ""


#: Flags des Modells, die verlaesslich blockieren. Das Modell darf uns
#: aufhalten — es darf uns nie durchwinken.
BLOCKING_FLAGS = frozenset({"hypothetical", "quote", "sarcasm_possible",
                            "affect", "past"})

STATED = "stated"
INFERRED = "inferred"

#: Wie viele unabhaengige Gespraeche eine Inferenz braucht.
INFERRED_MIN_CONVERSATIONS = 2


def decide(proposal: Proposal, turn: OwnerTurn, *, evidence_conversations: int,
           suppressed: bool, secret_hit: bool,
           conflict: dict[str, Any] | None = None) -> Decision:
    """Die eine Entscheidungsfunktion. Deterministisch, ohne Seiteneffekt.

    Reihenfolge nach Schwere: was rueckstandsfrei verworfen gehoert, faellt
    zuerst; erst danach die Frage, ob genug Evidenz da ist.
    """
    # 1. Zugangsdaten. Nicht einmal ein Kandidat — nur ein inhaltsloser Zaehler.
    if secret_hit:
        return Decision(DISCARD, "secret_shaped")

    # 2. Was der Mensch abgelehnt oder vergessen hat, wird nicht neu gefragt.
    if suppressed:
        return Decision(DISCARD, "suppressed")

    # 3. Risiko-Flags des Modells blockieren verlaesslich.
    blocking = proposal.flags & BLOCKING_FLAGS
    if blocking:
        return Decision(DISCARD, "model_flag", marker=sorted(blocking)[0])

    # 4. Kategorien, die es in dieser Ausbaustufe nicht gibt.
    if proposal.memory_type in NOT_IN_V1:
        return Decision(DISCARD, "category_not_in_v1")

    # 5. Die deterministische Selbstaussage-Wache. Sie laeuft auf dem ECHTEN
    #    Turntext, nicht auf der Modellzusammenfassung — sonst koennte eine
    #    geschoente Normalisierung die Wache umgehen.
    #
    #    Diese Pruefung steht VOR dem Nuetzlichkeitstest, und das ist Absicht.
    #    Als sie danach stand, fiel „Meine Frau mag Kaffee" mit dem Grund
    #    `too_short` heraus — richtiges Ergebnis, falsche Begruendung. Ein
    #    Test, der das als bestandene Dritte-Person-Wache liest, misst nichts:
    #    eine geaenderte Laengengrenze haette daraus stillschweigend eine
    #    Vorliebe des Nutzers gemacht. Sicherheitsgruende zuerst.
    if proposal.about == "world":
        # Zuerst: geht es ueberhaupt um den Besitzer? Das ist die
        # grundlegendere Frage als „steht eine erste Person im Satz".
        return Decision(DISCARD, "not_about_owner")

    guard = owner_self_assertion(turn.text)
    if proposal.about != "third_party" and not guard.ok:
        return Decision(DISCARD, guard.reason, marker=guard.marker)
    if proposal.about == "third_party":
        # Fakten UEBER Dritte sind erlaubt — aber nie als Aussage ueber den
        # Besitzer, und nie, wenn sie sensibel sind (Dritte koennen nicht
        # zustimmen).
        if proposal.memory_type is not MemoryType.PEOPLE:
            return Decision(DISCARD, "third_party_not_people")
    # 6. Sensitivitaet: Maximum aus Lexikon und Modell-Label.
    sensitivity = strictest(lexical_sensitivity(proposal.statement),
                            lexical_sensitivity(turn.text),
                            proposal.sensitivity)
    if sensitivity is Sensitivity.SECRET_REFERENCE:
        return Decision(DISCARD, "secret_reference_never_adaptive")

    # 7. Erst JETZT die Nuetzlichkeit: sie entscheidet zwischen Dingen, die
    #    schon als zulaessig eingeordnet sind — nie darueber, ob etwas
    #    gefaehrlich war.
    useful = is_useful(proposal.statement)
    if not useful.ok:
        return Decision(DISCARD, useful.reason, sensitivity, marker=useful.marker)

    # 8. Regeln: nur vorsichtserhoehend, und nie still.
    if proposal.memory_type in ASK_ONLY:
        permissive = is_permissive_rule(proposal.statement)
        if not permissive.ok:
            return Decision(DISCARD, "permissive_rule", marker=permissive.marker)
        return Decision(ASK, "rule_needs_confirmation", sensitivity,
                        ask_reason="rule")

    # 9. Widerspruch zu aktiver kanonischer Wahrheit.
    if conflict:
        if conflict.get("ambiguous"):
            # Der Mensch hat gesagt, dass etwas ersetzt werden soll — nur ist
            # unklar was. Zwei aktive Wahrheiten waeren die schlechteste
            # Antwort darauf; eine Frage ist die richtige.
            return Decision(ASK, "ambiguous_supersession", sensitivity,
                            ask_reason="contradiction")
        target = str(conflict.get("memory_id") or "")
        if conflict.get("user_direct"):
            # Nutzerwort gegen Nutzerwort: nie still ueberschreiben.
            return Decision(CONTEST, "contradicts_user_direct", sensitivity,
                            ask_reason="contradiction", contested_memory_id=target)
        if proposal.kind == STATED:
            # Nutzerwort schlaegt Maschine — der einzige automatische
            # Supersede, den es gibt.
            return Decision(ADOPT, "supersedes_learned", sensitivity,
                            contested_memory_id=target)
        return Decision(CONTEST, "inferred_contradicts", sensitivity,
                        ask_reason="contradiction", contested_memory_id=target)

    # 10. Sensibles: niemals still.
    if sensitivity is Sensitivity.SENSITIVE:
        return Decision(ASK, "sensitive_needs_confirmation", sensitivity,
                        ask_reason="sensitive")

    # 11. Evidenzschwelle und Kategorie.
    if proposal.kind == STATED:
        if proposal.memory_type in AUTO_STATED:
            return Decision(ADOPT, "stated_self_assertion", sensitivity)
        return Decision(ASK, "category_needs_confirmation", sensitivity,
                        ask_reason="category")
    if proposal.kind == INFERRED:
        if proposal.memory_type not in AUTO_INFERRED:
            return Decision(GATHER, "inferred_category_no_auto", sensitivity)
        if evidence_conversations < INFERRED_MIN_CONVERSATIONS:
            return Decision(GATHER, "insufficient_independent_evidence", sensitivity)
        return Decision(ADOPT, "inferred_pattern", sensitivity)
    return Decision(DISCARD, "unknown_kind")
