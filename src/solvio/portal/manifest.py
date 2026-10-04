"""Was genau passieren soll — festgeschrieben, bevor irgendetwas passiert.

Ein Formular auf einer fremden Seite ist kein Knopf, sondern ein Versprechen der
Seite, und die Seite kann es brechen. Zwischen „der Nutzer hat zugestimmt" und
„es wird ausgefuehrt" liegen Sekunden, in denen fremdes JavaScript das Ziel
austauschen, ein verstecktes Feld aendern oder die Methode umstellen kann.

Das Manifest ist die Antwort darauf: eine exakte, unveraenderliche Beschreibung
der Aktion, die freigegeben wurde. Es traegt keine Zusammenfassung — eine
Zusammenfassung freizugeben und etwas anderes auszufuehren ist genau die
Taeuschung, gegen die das hier steht.

Zwei Sicherungen, die verschiedene Dinge tun:

* **Argument-Drift** faengt der eingefrorene Freigabepfad: er rechnet den Digest
  aus den Argumenten neu und vergleicht ihn mit dem gespeicherten. Aendert sich,
  was SOLVIO ausfuehren will, passt der Digest nicht mehr.
* **Seiten-Drift** faengt dieses Modul: unmittelbar vor der Ausfuehrung wird die
  lebende Seite noch einmal befragt und gegen den Fingerabdruck gehalten, der bei
  der Freigabe galt. Aendert sich, worauf ausgefuehrt wuerde, passiert nichts.

Und der Punkt, der leicht uebersehen wird: ein Geheimnis steht **nie** im
Manifest. Wo ein Passwort hingehoert, steht sein Alias. Das Manifest wird
gehasht, angezeigt, protokolliert und gespeichert — alles Orte, an denen ein
Passwort nichts verloren hat.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

#: Aktionsarten, die diese Stufe kennt. Bewusst zwei.
LOGIN = "login"
FORM_SUBMIT = "form_submit"
ACTION_TYPES = frozenset({LOGIN, FORM_SUBMIT})

#: So sieht ein Wert aus, der aus dem Tresor kommt. Der Alias steht im Manifest,
#: der Wert niemals.
SECRET_PLACEHOLDER = "«{alias}»"

#: Felder, deren Inhalt nie in einen Freigabetext darf — auch dann nicht, wenn
#: jemand sie versehentlich als gewoehnliches Formularfeld fuehrt.
NEVER_DISPLAYED = ("password", "passwort", "kennwort", "pin", "otp", "totp",
                   "secret", "token", "cvv", "cvc", "iban", "card")


@dataclass(frozen=True)
class FieldBinding:
    """Ein Feld, sein Ziel auf der Seite und der Wert, der hineinsoll."""

    name: str
    selector: str
    value: str = ""
    alias: str = ""

    @property
    def is_secret(self) -> bool:
        return bool(self.alias)

    def displayed(self) -> str:
        """Was auf dem iPhone steht. Ein Geheimnis zeigt seinen Alias."""
        if self.alias:
            return SECRET_PLACEHOLDER.format(alias=self.alias)
        lowered = self.name.casefold()
        if any(word in lowered for word in NEVER_DISPLAYED):
            return "«verborgen»"
        return self.value


@dataclass(frozen=True)
class ActionManifest:
    """Die exakte Aktion. Unveraenderlich, hashbar, ohne Geheimnisse."""

    portal_id: str
    origin: str
    page_url: str
    action_type: str
    target: str
    method: str
    page_signature: str
    fields: tuple[FieldBinding, ...] = ()
    credential_alias: str = ""
    version: int = 1
    risk: str = "critical"
    principal: str = ""

    def __post_init__(self) -> None:
        if self.action_type not in ACTION_TYPES:
            raise ValueError(f"unknown portal action: {self.action_type!r}")
        if not self.origin.startswith("https://") and not self.origin.startswith("http://"):
            raise ValueError("origin must be an absolute http(s) origin")

    # -- Bindung -------------------------------------------------------------
    def binding(self) -> dict[str, Any]:
        """Die kanonische Form. Genau das, worueber der Digest laeuft.

        Sortiert und ohne Werte aus Geheimnissen: derselbe Vorgang muss zweimal
        denselben Digest ergeben, und ein Passwort darf keinen davon beeinflussen
        — sonst waere der Digest ein Orakel.
        """
        return {
            "portal": self.portal_id,
            "origin": self.origin,
            "action": self.action_type,
            "target": self.target,
            "method": self.method.upper(),
            "page": self.page_signature,
            "alias": self.credential_alias,
            "version": self.version,
            "fields": [{"name": f.name, "selector": f.selector,
                        "value": f.displayed()} for f in self.fields],
        }

    def digest(self) -> str:
        payload = json.dumps(self.binding(), sort_keys=True, ensure_ascii=False,
                             separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    # -- Anzeige -------------------------------------------------------------
    def human_action(self) -> str:
        """Der Text, den der Nutzer auf dem iPhone bestaetigt.

        Lesbar, vollstaendig, ohne Geheimnis. Wer hier eine Zusammenfassung
        einsetzte, haette die Freigabe wertlos gemacht.
        """
        head = ("Auf einer Webseite anmelden" if self.action_type == LOGIN
                else "Auf einer Webseite abschicken")
        lines = [head,
                 f'Portal: "{self.portal_id}"',
                 f'Adresse: "{self.origin}"',
                 f'Seite: "{self.page_url}"',
                 f'Aktion: "{self.method.upper()} {self.target}"']
        if self.credential_alias:
            lines.append(f'Zugang: "{self.credential_alias}"')
        for entry in self.fields:
            lines.append(f'{entry.name}: "{entry.displayed()}"')
        return "\n".join(lines)

    def approval_arguments(self) -> dict[str, Any]:
        """Die Argumente, ueber die der eingefrorene Freigabepfad den Digest bildet.

        Flach und lesbar, weil `render_action` daraus Zeile fuer Zeile den Text
        macht, den der Nutzer auf dem iPhone sieht. Was hier steht, steht dort —
        und was dort steht, ist gebunden. Ein Passwort steht in beidem nicht.
        """
        felder = "; ".join(f"{f.name}={f.displayed()}" for f in self.fields)
        return {"portal": self.portal_id, "adresse": self.origin,
                "seite": self.page_url,
                "aktion": f"{self.method.upper()} {self.target}",
                "zugang": self.credential_alias,
                "felder": felder, "pruefsumme": self.page_signature}

    def as_arguments(self) -> dict[str, Any]:
        """Die Argumente, mit denen der Freigabepfad rechnet.

        Bewusst genau die Bindung: der eingefrorene Digest laeuft dann ueber
        dasselbe, was hier steht, und Argument-Drift ist damit dieselbe Frage wie
        Manifest-Drift.
        """
        return {"manifest": self.digest(), "portal": self.portal_id,
                "origin": self.origin, "action": self.action_type}


def page_signature(*, origin: str, form_action: str, method: str,
                   field_names, target: str) -> str:
    """Der Fingerabdruck der Seite, so wie sie bei der Freigabe stand.

    Absichtlich schmal: Herkunft, Formularziel, Methode, die Namen der Felder und
    das bediente Element. Der sichtbare Text gehoert NICHT dazu — er aendert sich
    bei jedem Seitenaufruf (Zeitstempel, Zaehler, Werbung), und ein
    Fingerabdruck, der staendig ohne Grund abweicht, wird abgeschaltet.
    """
    payload = json.dumps({
        "origin": origin,
        "form": form_action,
        "method": (method or "GET").upper(),
        "fields": sorted(str(n) for n in field_names),
        "target": target,
    }, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def drifted(approved: ActionManifest, live: dict[str, Any]) -> str:
    """Vergleicht die freigegebene Aktion mit der Seite, wie sie jetzt ist.

    Liefert den Grund der Abweichung — oder leer, wenn alles passt. Die
    Reihenfolge ist die der Schwere: die Herkunft zuerst, denn eine andere
    Herkunft macht jede weitere Uebereinstimmung bedeutungslos.
    """
    if str(live.get("origin", "")) != approved.origin:
        return "origin_changed"
    # Die Methode wird NICHT einzeln verglichen: `method` im Manifest ist die
    # deklarierte Schreibmethode der Bindung, `live["method"]` das, was im DOM
    # steht — bei einer JS-Oberflaeche zwangslaeufig verschieden. Was die Seite
    # sagt, steckt ohnehin im Fingerabdruck, und eine geaenderte Methode aendert
    # ihn. Ein Vergleich zweier verschiedener Dinge waere kein Schutz, sondern
    # ein Dauerfehlalarm — und ein Alarm, der immer schlaegt, wird abgeschaltet.
    if str(live.get("target", "")) != approved.target:
        return "target_changed"
    if str(live.get("page_signature", "")) != approved.page_signature:
        return "page_changed"
    return ""
