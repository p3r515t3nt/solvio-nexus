"""Warum eine Kartennummer nie in die Zahlungsablage rutscht.

Der Tresor hat diesen Zaun fuer Zugangsdaten; hier steht das Gegenstueck fuer
ZAHLUNGSMATERIAL. Der Unterschied ist nicht kosmetisch: „Mein Passwort ist X"
und „4242 4242 4242 4242" sehen vollkommen verschieden aus, und die
Zugangsdaten-Erkennung findet die Karte nicht.

Erkannt wird, was ein Mensch versehentlich in ein Feld tippt oder ein Modell aus
einer Seite mitschleift:

* eine Zahlenfolge von 13 bis 19 Ziffern, die die **Luhn-Pruefung besteht** —
  das ist die Definition einer Kartennummer, nicht ein Bauchgefuehl;
* eine benannte Pruefziffer („CVV 123", „Pruefziffer: 456");
* eine IBAN in gueltiger Form;
* Magnetstreifenspuren (`%B...^`, `;...=`);
* ein Anbieter-Geheimschluessel in bekannter Form (`sk_live_`, `sk_test_`,
  `rk_live_`, `whsec_`).

**Zwei Antworten, wie beim Tresor.** Wo der Zweck eine Aussage ist (Buch,
Absicht, Zahlungsmittel), wird VERWEIGERT. Wo der Zweck ein Verlauf ist
(Anzeigetext, Protokollzeile), wird REDIGIERT.

**Ehrlich zur Reichweite.** Luhn findet eine Kartennummer, auch mit Leerzeichen
und Bindestrichen. Eine in Worten diktierte Nummer findet sie nicht. Deshalb ist
die eigentliche Antwort nicht dieser Zaun, sondern die Architektur: es gibt
keinen Weg, auf dem eine Kartennummer ueberhaupt in SOLVIO ankommt — kein Feld,
keine Faehigkeit, kein Endpunkt. Der Zaun steht daneben, fuer den Fall, dass
sich jemand einen baut.
"""
from __future__ import annotations

import re

from solvio.logging_setup import get_logger

log = get_logger("payment")

#: Was SOLVIO sagt, statt zu speichern. Produktsprache, kein Fehlercode.
ZAHLUNG_HINWEIS = ("Kartendaten gehoeren nicht zu mir. Hinterlege die Karte "
                   "direkt beim Anbieter oder beim Haendler — ich merke mir nur, "
                   "DASS es sie gibt.")

#: Was stehen bleibt, wo Zahlungsmaterial stand.
MARKER = "[Zahlungsdaten — nicht gespeichert]"

#: 13 bis 19 Ziffern, optional in Vierergruppen. Die Luhn-Pruefung entscheidet
#: danach — ohne sie wuerde jede Bestellnummer als Karte gelten.
_DIGIT_RUN = re.compile(r"(?<![0-9])(?:[0-9][ -]?){12,18}[0-9](?![0-9])")

#: Eine benannte Pruefziffer. Der NAME macht sie erkennbar, nicht die Ziffern.
_CVV = re.compile(r"(?i)\b(?:cvv|cvc|cvv2|cvc2|cid|pruef(?:z|ziffer)|"
                  r"kartenpr(?:ue|ü)fnummer|sicherheitscode)\b\D{0,12}\b[0-9]{3,4}\b")

#: IBAN in gueltiger Form. Laenderkennung, zwei Pruefziffern, dann alphanumerisch.
_IBAN = re.compile(r"(?<![A-Za-z0-9])[A-Z]{2}[0-9]{2}(?:[ ]?[A-Z0-9]{4}){2,7}"
                   r"(?:[ ]?[A-Z0-9]{1,3})?(?![A-Za-z0-9])")

#: Magnetstreifen. Track 1 und Track 2.
_TRACK = re.compile(r"%B[0-9]{12,19}\^|;[0-9]{12,19}=[0-9]{4}")

#: Anbieter-Geheimschluessel in bekannter Form. Ein oeffentlicher Schluessel
#: (`pk_`) steht bewusst NICHT dabei — er ist kein Geheimnis.
_PROVIDER_SECRET = re.compile(r"(?<![A-Za-z0-9_])(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{8,}"
                              r"|(?<![A-Za-z0-9_])whsec_[A-Za-z0-9]{8,}")

#: Eine PIN, benannt. Vier bis sechs Ziffern hinter dem Wort.
_PIN = re.compile(r"(?i)\b(?:karten-?pin|pin-?code|geheimzahl)\b\D{0,12}\b[0-9]{4,6}\b")


class PaymentMaterialRefused(ValueError):
    """Dieser Inhalt sieht wie Zahlungsmaterial aus und wird nicht abgelegt.

    Traegt einen kategorischen Grund und NIE den Text, der die Ablehnung
    ausgeloest hat — diese Ausnahme kann bis in ein Werkzeugergebnis und damit
    ins Modell laufen.
    """

    def __init__(self, reason: str, where: str = "") -> None:
        super().__init__(f"payment_material_refused:{reason}")
        self.reason = reason
        self.where = where
        self.human_message = ZAHLUNG_HINWEIS


def luhn_ok(digits: str) -> bool:
    """Die Pruefsumme, die eine Kartennummer von einer Zahlenreihe unterscheidet."""
    if not digits.isdigit() or not (13 <= len(digits) <= 19):
        return False
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = ord(char) - 48
        if index % 2:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _iban_ok(candidate: str) -> bool:
    flat = candidate.replace(" ", "").upper()
    if not (15 <= len(flat) <= 34) or not flat[:2].isalpha() or not flat[2:4].isdigit():
        return False
    rearranged = flat[4:] + flat[:4]
    converted = "".join(str(ord(c) - 55) if c.isalpha() else c for c in rearranged)
    if not converted.isdigit():
        return False
    return int(converted) % 97 == 1


def reason_for(text: str) -> str:
    """Warum es Zahlungsmaterial ist. Eine geschlossene Liste, nie ein Ausschnitt."""
    if not isinstance(text, str) or not text:
        return ""
    if _TRACK.search(text):
        return "magnetic_stripe"
    if _PROVIDER_SECRET.search(text):
        return "provider_secret"
    if _CVV.search(text):
        return "card_verification_value"
    if _PIN.search(text):
        return "card_pin"
    for match in _DIGIT_RUN.finditer(text):
        digits = re.sub(r"[^0-9]", "", match.group(0))
        if luhn_ok(digits):
            return "primary_account_number"
    for match in _IBAN.finditer(text):
        if _iban_ok(match.group(0)):
            return "iban"
    return ""


def is_payment_material(text: str) -> bool:
    return bool(reason_for(text))


def refuse_if_payment_material(text: str, *, where: str) -> None:
    """Der Zaun fuer Speicher, deren Zweck eine Aussage ist. Wirft oder schweigt."""
    reason = reason_for(text)
    if not reason:
        return
    # Nur die Tatsache und der Ort. Kein Ausschnitt, keine Laenge, keine letzten
    # vier Ziffern — solche „harmlosen" Auskuenfte sind der uebliche Weg, auf dem
    # ein Geheimnis doch noch in ein Protokoll rutscht.
    log.warning("payment.firewall_refused", where=where, reason=reason)
    raise PaymentMaterialRefused(reason, where)


def redact_if_payment_material(text: str, *, where: str) -> str:
    """Der Zaun fuer Speicher, deren Zweck ein Verlauf ist. Gibt Ersatz zurueck."""
    reason = reason_for(text)
    if not reason:
        return text
    log.warning("payment.firewall_redacted", where=where, reason=reason)
    return MARKER
