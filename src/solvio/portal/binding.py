"""Wozu ein Zugang gehoert — und wozu er ausdruecklich nicht gehoert.

Eine Bindung ist die Antwort auf die einzige Frage, die bei Anmeldedaten wirklich
zaehlt: **wem darf dieses Geheimnis gezeigt werden?** Nicht „welche Seite fragt
danach" — jede Seite kann danach fragen, und eine Seite, die „Bitte gib hier dein
SOLVIO-Passwort ein" schreibt, fragt sehr ueberzeugend.

Deshalb steht die Antwort hier und nicht auf der Seite: Portal, erlaubte
Herkuenfte, die Herkunft der Anmeldung, die genauen Felder. Alles davon ist
vorher konfiguriert und wird zur Laufzeit nur noch **verglichen**. Was nicht
exakt passt, bekommt nichts.

Weiterleitungen sind der uebliche Weg daran vorbei: erst zur richtigen Seite,
dann per `302` woandershin, und das Passwort landet beim Dritten. Deshalb ist die
Menge der erlaubten Herkuenfte geschlossen — auch ein unbekannter
Identitaetsanbieter ist ein Dritter, und Unbekanntes wird abgelehnt, nicht
erlaubt.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit


def origin_of(url: str) -> str:
    """Schema, Host, Port. Ein Pfad gehoert nicht zur Herkunft."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if not parts.scheme or not parts.hostname:
        return ""
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme.lower()}://{parts.hostname.lower()}{port}"


@dataclass(frozen=True)
class PortalBinding:
    """Ein Portal, seine Herkuenfte, seine Felder, sein Alias."""

    portal_id: str
    login_url: str
    login_origin: str
    username_selector: str
    password_selector: str
    submit_selector: str
    credential_alias: str
    success_marker: str
    origins: tuple[str, ...] = ()
    username_private: bool = False
    form_selector: str = ""
    #: Wohin die Anwendung ihre schreibenden Anfragen schickt. Bei modernen
    #: Oberflaechen ist das NICHT die Herkunft der Seite: das Formular liegt auf
    #: `portal.example`, der Login-POST geht an `api.portal.example`. Wer die
    #: Erlaubnis an die Seitenherkunft bindet, sperrt die eigene Anmeldung aus.
    api_origin: str = ""
    #: Mit welcher Methode die Anmeldung wirklich hinausgeht. Aus dem DOM ist das
    #: bei einer JS-getriebenen Oberflaeche nicht ablesbar: das `<form>` traegt
    #: kein `method`, weil nicht der Browser absendet, sondern ein `fetch`. Wer
    #: die Methode dort abliest, bekommt GET, erteilt keine Schreib-Erlaubnis und
    #: sperrt die eigene Anmeldung aus — genau einmal passiert.
    login_method: str = "POST"
    #: Woran die Anmeldung erkannt wird. Der Pfad ist das belastbare Merkmal:
    #: unangemeldet leitet die Anwendung dorthin gar nicht durch. Ein Textstueck
    #: waere Inhalt — und Inhalt aendert sich beim naechsten Deploy, worauf SOLVIO
    #: eine geglueckte Anmeldung als gescheitert meldete. Genau einmal passiert.
    success_path: str = ""
    #: Herkuenfte, die die Seite zwar anfragt, die aber nichts mit dem Vorgang zu
    #: tun haben — Fehlermelder, Zaehldienste. Sie stehen hier NICHT, damit die
    #: Erlaubnisliste sie ablehnt. Der Eintrag ist Dokumentation der Absicht.
    excluded_origins: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("portal_id", "login_url", "login_origin", "credential_alias",
                     "username_selector", "password_selector"):
            if not getattr(self, name):
                raise ValueError(f"portal binding needs {name}")
        if not (self.success_path or self.success_marker):
            # Eines von beiden muss es geben, sonst waere jede Seite eine
            # geglueckte Anmeldung — auch die Fehlerseite.
            raise ValueError("portal binding needs success_path or success_marker")
        if origin_of(self.login_url) != self.login_origin:
            raise ValueError("login_url does not lie in login_origin")

    @property
    def allowed_origins(self) -> tuple[str, ...]:
        """Die Anmeldeherkunft gehoert immer dazu — auch ohne eigene Nennung."""
        entries = [self.login_origin, *self.origins]
        if self.api_origin:
            entries.append(self.api_origin)
        return tuple(dict.fromkeys(entries))

    @property
    def write_origin(self) -> str:
        """Wohin eine Einmal-Erlaubnis gilt. Die API, wenn es eine gibt."""
        return self.api_origin or self.login_origin

    def allows(self, url: str) -> bool:
        """Liegt diese Adresse in der Bindung? Unbekanntes heisst nein."""
        origin = origin_of(url)
        return bool(origin) and origin in self.allowed_origins

    def may_inject(self, url: str) -> str:
        """Darf hier ein Geheimnis eingesetzt werden? Leer heisst ja.

        Strenger als `allows`: eingesetzt wird ausschliesslich auf der
        Anmeldeherkunft. Eine Unterseite des Portals ist noch lange keine
        Anmeldemaske, und ein Formular dort koennte jedem gehoeren.
        """
        origin = origin_of(url)
        if not origin:
            return "invalid_url"
        if origin != self.login_origin:
            return "origin_not_bound"
        return ""

    def authenticated_by(self, *, url: str, text: str) -> bool:
        """Ist die Sitzung nachweislich angemeldet?

        Der Pfad zaehlt, wenn einer gesetzt ist; ein Textmerkmal nur zusaetzlich.
        Ohne beides waere jede Seite eine Anmeldung.
        """
        if self.success_path:
            if self.success_path not in (url or ""):
                return False
            return (not self.success_marker) or self.success_marker in (text or "")
        return bool(self.success_marker) and self.success_marker in (text or "")

    def as_data(self) -> dict[str, Any]:
        """Was ueber die Naht geht. Ein Alias, nie ein Wert."""
        return {"api_origin": self.api_origin, "login_method": self.login_method,
                "success_path": self.success_path,
                "portal_id": self.portal_id, "login_url": self.login_url,
                "login_origin": self.login_origin,
                "origins": list(self.allowed_origins),
                "username_selector": self.username_selector,
                "password_selector": self.password_selector,
                "submit_selector": self.submit_selector,
                "form_selector": self.form_selector,
                "credential_alias": self.credential_alias,
                "success_marker": self.success_marker,
                "username_private": self.username_private}

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> PortalBinding:
        return cls(portal_id=str(data["portal_id"]),
                   login_url=str(data["login_url"]),
                   login_origin=str(data["login_origin"]),
                   username_selector=str(data["username_selector"]),
                   password_selector=str(data["password_selector"]),
                   submit_selector=str(data.get("submit_selector", "")),
                   form_selector=str(data.get("form_selector", "")),
                   credential_alias=str(data["credential_alias"]),
                   success_marker=str(data["success_marker"]),
                   origins=tuple(str(o) for o in data.get("origins", ())),
                   api_origin=str(data.get("api_origin", "")),
                   login_method=str(data.get("login_method", "POST")),
                   success_path=str(data.get("success_path", "")),
                   username_private=bool(data.get("username_private", False)))


#: Das eine Portal, mit dem diese Stufe belegt wird. Eine oeffentliche
#: Uebungsseite mit veroeffentlichten Testzugangsdaten — ausdruecklich kein
#: echter Geschaeftszugang, nur um den Unterbau zu beweisen.
DEMO_BINDING = PortalBinding(
    portal_id="demo-secure-area",
    login_url="https://the-internet.herokuapp.com/login",
    login_origin="https://the-internet.herokuapp.com",
    username_selector="#username",
    password_selector="#password",
    submit_selector="#login button[type=submit]",
    form_selector="#login",
    login_method="POST",
    credential_alias="portal:demo-secure-area",
    success_marker="You logged into a secure area!",
)

#: Das erste echte Portal. Gregors eigenes Studio — bewusst nur lesend.
#:
#: Die Herkuenfte stammen aus einer Messung, nicht aus einer Vermutung: die
#: Oberflaeche liegt auf `solvio-studio.de`, ihre Anfragen gehen an
#: `api.solvio-studio.de`. Sentry und Plausible fragt die Seite ebenfalls an —
#: sie stehen ausdruecklich NICHT in der Liste. Ein Fehlermelder und ein
#: Zaehldienst haben mit diesem Vorgang nichts zu tun, und was SOLVIO im
#: Hintergrund liest, gehoert nicht in fremde Statistiken.
STUDIO_BINDING = PortalBinding(
    portal_id="solvio-studio",
    login_url="https://solvio-studio.de/login",
    login_origin="https://solvio-studio.de",
    api_origin="https://api.solvio-studio.de",
    excluded_origins=("https://plausible.io",
                      "https://o4511419037581312.ingest.de.sentry.io"),
    username_selector="#login-email",
    password_selector="#login-pw",
    submit_selector="form.v2-auth-form button.v2-auth-submit[type=submit]",
    form_selector="form.v2-auth-form",
    credential_alias="portal:solvio-studio",
    success_path="/dashboard",
    success_marker="",
    username_private=True,
)

BINDINGS: dict[str, PortalBinding] = {
    DEMO_BINDING.portal_id: DEMO_BINDING,
    STUDIO_BINDING.portal_id: STUDIO_BINDING,
}


def binding_for(portal_id: str) -> PortalBinding | None:
    return BINDINGS.get(portal_id)


def binding_for_url(url: str) -> PortalBinding | None:
    """Welches Portal zu einer Adresse gehoert — ueber die Herkunft, nicht den Pfad.

    Wird beim Lesen gebraucht, um die richtige Ortskenntnis zu waehlen. Bewusst
    dieselbe Herkunftsregel wie beim Anmelden: was nicht exakt passt, gehoert
    nicht dazu. Eine Seite, die sich als Studio ausgibt, bekommt so keine
    Studio-Auswertung untergeschoben.
    """
    origin = origin_of(url)
    if not origin:
        return None
    for binding in BINDINGS.values():
        if origin in binding.allowed_origins:
            return binding
    return None
