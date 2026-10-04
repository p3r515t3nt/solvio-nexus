"""Google Calendar ueber die offizielle REST-API — ohne SDK.

Warum kein SDK: `google-api-python-client` zieht einen ganzen Baum an
Abhaengigkeiten nach, und die Kalender-API ist schlichtes JSON ueber HTTPS. Was
gebraucht wird, sind fuenf Endpunkte und ein Token-Refresh; das traegt `aiohttp`,
das SOLVIO ohnehin hat. „SOLVIO besitzt seine Abhaengigkeiten" ist keine Parole,
wenn man an der ersten bequemen Stelle nachgibt.

Anmeldung: OAuth2 mit Refresh-Token (Client-Id, Client-Secret, Refresh-Token aus
den Settings). Das Zugriffstoken lebt nur im Speicher und wird kurz vor Ablauf
erneuert. **Kein Token, kein Secret und kein Termintext wird je protokolliert.**

Die doppelte Anlage — die klassische Kalenderpanne — verhindert Google selbst:
eine vom Aufrufer vergebene Termin-Id wird beim zweiten Mal mit `409` abgelehnt.
Diese Schicht uebersetzt das in „gibt es schon" statt in einen Fehler, und die
Faehigkeit darf denselben Aufruf deshalb gefahrlos wiederholen.
"""
from __future__ import annotations

import contextlib
import re
import time
from datetime import datetime, timedelta
from typing import Any

import aiohttp

from solvio.capabilities.calendar import (
    USER_TIMEZONE, CalendarAuthError, CalendarEvent,
)
from solvio.logging_setup import get_logger

log = get_logger("calendar")

_API = "https://www.googleapis.com/calendar/v3"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
#: Die Herkunft des Token-Endpunkts — das ZIEL, an das der Tresor bindet.
#: Bewusst aus derselben Konstante abgeleitet: zwei Schreibweisen derselben
#: Adresse waeren zwei Meinungen darueber, wohin ein Geheimnis darf.
_TOKEN_ORIGIN = "https://oauth2.googleapis.com"
_REFRESH_MARGIN = 120.0     # Sekunden vor Ablauf erneuern


def _parse_moment(payload: dict[str, Any]) -> tuple[datetime, bool]:
    """Googles `start`/`end`: entweder `dateTime` (mit Zone) oder `date` (ganztaegig)."""
    if payload.get("dateTime"):
        moment = datetime.fromisoformat(payload["dateTime"])
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=USER_TIMEZONE)
        return moment.astimezone(USER_TIMEZONE), False
    day = datetime.fromisoformat(payload["date"]).date()
    return datetime.combine(day, datetime.min.time(), tzinfo=USER_TIMEZONE), True


def event_from_api(payload: dict[str, Any]) -> CalendarEvent:
    start, all_day = _parse_moment(payload.get("start") or {})
    end, _ = _parse_moment(payload.get("end") or {})
    attendees = payload.get("attendees") or []
    return CalendarEvent(
        event_id=payload.get("id", ""),
        summary=payload.get("summary", "") or "(ohne Titel)",
        start=start, end=end, all_day=all_day,
        description=payload.get("description", "") or "",
        location=payload.get("location", "") or "",
        attendee_count=len(attendees),
        organizer=((payload.get("organizer") or {}).get("displayName", "") or ""))


class GoogleCalendar:
    """Der Anbieter. Erfuellt `CalendarProvider`."""

    #: Verweise auf die Tresor-Eintraege. Namen, keine Werte.
    CLIENT_SECRET_REF = "secret://google/oauth-client"
    REFRESH_TOKEN_REF = "secret://google/refresh"

    def __init__(self, *, client_id: str, client_secret: str = "",
                 refresh_token: str = "", calendar_id: str = "primary",
                 timeout: float = 10.0, broker: Any = None) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self.calendar_id = calendar_id
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._access_token = ""
        self._expires_at = 0.0
        self._token_credential_versions = None
        self._broker = broker

    @property
    def uses_vault(self) -> bool:
        return self._broker is not None

    # -- Anmeldung -----------------------------------------------------------
    def _authorise_cached(self) -> None:
        """Der Tresor entscheidet AUCH ueber ein bereits geholtes Token.

        DEBT-0193, live gemessen: ohne diese Pruefung war ein warmes
        Zugangstoken stille Autoritaet. Eine Faehigkeit ausserhalb des Scopes
        lief durch, weil eine ANDERE, erlaubte Faehigkeit kurz zuvor auf
        derselben gemeinsamen Instanz ein Token geholt hatte. Der Riegel des
        Tresors griff nie, weil er nie gefragt wurde.

        `authorize()` steht ABSICHTLICH hier und in keinem gemeinsamen Helfer
        eines anderen Moduls: der Tresor nimmt den Modulnamen des Aufrufers per
        Rahmen-Inspektion, und ein Helfer anderswo wuerde genau diese Bindung
        aufheben — derselbe Grund wie bei `_credentials()`.

        Beide Verweise werden geprueft. Ein entzogener Scope auf nur einem von
        beiden ist ein entzogener Scope.
        """
        if not self.uses_vault:
            return
        from solvio.secret_vault.policy import ExecutorId
        for ref in (self.CLIENT_SECRET_REF, self.REFRESH_TOKEN_REF):
            self._broker.authorize(ref, executor=ExecutorId.HTTP,
                                   target=_TOKEN_ORIGIN)

    def _credential_versions(self):
        """Safe metadata binding of a derived token; no secret value or hash.

        Rotating either durable credential invalidates the old access token,
        even while its provider expiry has not arrived. This is independent
        of the permission check, which still runs for every cache hit.
        """
        if not self.uses_vault:
            return (self._client_id, ())
        versions = []
        effective_client_id = self._client_id
        for ref in (self.CLIENT_SECRET_REF, self.REFRESH_TOKEN_REF):
            metadata = self._broker.describe(ref)
            if metadata is None or type(metadata.get("version")) is not int or metadata["version"] < 1:
                raise CalendarAuthError("credentials_missing")
            if ref == self.CLIENT_SECRET_REF and "oauth_client_id" in metadata:
                candidate = metadata["oauth_client_id"]
                if not isinstance(candidate, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,256}\.apps\.googleusercontent\.com", candidate):
                    raise CalendarAuthError("credentials_invalid")
                effective_client_id = candidate
            versions.append((ref, metadata["version"], metadata.get("rotated_at", "")))
        return (effective_client_id, tuple(versions))

    async def _token(self) -> str:
        self._authorise_cached()
        versions = self._credential_versions()
        if (self._access_token and time.monotonic() < self._expires_at - _REFRESH_MARGIN
                and self._token_credential_versions == versions):
            return self._access_token
        self._access_token = ""
        self._expires_at = 0.0
        self._token_credential_versions = None
        # Die beiden dauerhaften Geheimnisse leben nur fuer die Dauer DIESER
        # Erneuerung. Das Zugangstoken danach ist kurzlebig und gehoert dem
        # Prozess — es steht nicht im Tresor, weil es dort in einer Stunde
        # falsch waere.
        #
        # Die `use()`-Aufrufe stehen ABSICHTLICH hier und nicht in einem Helfer:
        # der Tresor prueft den Modulnamen des Aufrufers gegen den behaupteten
        # Executor, und ein gemeinsamer Helfer in einem anderen Modul wuerde
        # genau diese Bindung aufheben.
        with self._credentials() as (secret, refresh):
            data = {"client_id": versions[0], "client_secret": secret,
                    "refresh_token": refresh, "grant_type": "refresh_token"}
            if self._credential_versions() != versions:
                raise CalendarAuthError("credentials_changed")
            try:
                token = await self._exchange(data)
                self._authorise_cached()
                if self._credential_versions() != versions:
                    raise CalendarAuthError("credentials_changed")
            except BaseException:
                self._access_token = ""
                self._expires_at = 0.0
                raise
            self._token_credential_versions = versions
            return token

    @contextlib.contextmanager
    def _credentials(self):
        if not self.uses_vault:
            if not (self._client_secret and self._refresh_token):
                raise CalendarAuthError("credentials_missing")
            yield self._client_secret, self._refresh_token
            return
        from solvio.secret_vault.policy import ExecutorId
        with self._broker.use(self.CLIENT_SECRET_REF, executor=ExecutorId.HTTP,
                              target=_TOKEN_ORIGIN) as secret:
            with self._broker.use(self.REFRESH_TOKEN_REF, executor=ExecutorId.HTTP,
                                  target=_TOKEN_ORIGIN) as refresh:
                yield secret.plaintext(), refresh.plaintext()

    async def _exchange(self, data: dict[str, str]) -> str:
        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            async with session.post(_TOKEN_URL, data=data) as reply:
                body = await reply.json(content_type=None)
                if reply.status != 200 or not body.get("access_token"):
                    # Der Grund ist eine Google-Fehlerkennung wie 'invalid_grant' —
                    # kein Geheimnis. Der Token taucht hier bewusst nirgends auf.
                    reason = str(body.get("error", reply.status))
                    log.error("calendar.auth_failed", reason=reason)
                    raise CalendarAuthError(reason)
                self._access_token = body["access_token"]
                self._expires_at = time.monotonic() + float(body.get("expires_in", 3600))
        return self._access_token

    async def _request(self, method: str, path: str, *, params: dict | None = None,
                       json_body: dict | None = None) -> Any:
        versions = self._credential_versions()
        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            token = await self._token()
            if self._credential_versions() != versions:
                raise CalendarAuthError("credentials_changed")
            headers = {"Authorization": f"Bearer {token}"}
            async with session.request(method, _API + path, headers=headers,
                                       params=params, json=json_body) as reply:
                if reply.status == 401:
                    self._access_token = ""
                    raise CalendarAuthError("unauthorized")
                if reply.status in (404, 410):
                    return None
                if reply.status == 409:
                    return {"__conflict__": True}
                if reply.status >= 400:
                    detail = str((await reply.json(content_type=None)) or {})[:160]
                    log.error("calendar.api_error", status=reply.status)
                    raise RuntimeError(f"calendar api {reply.status}: {detail}")
                if reply.status == 204 or not reply.content_length:
                    text = await reply.text()
                    return {} if not text.strip() else await reply.json(content_type=None)
                return await reply.json(content_type=None)

    # -- CalendarProvider ----------------------------------------------------
    async def list_events(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        params = {
            "timeMin": start.astimezone(USER_TIMEZONE).isoformat(),
            "timeMax": end.astimezone(USER_TIMEZONE).isoformat(),
            # Serien werden aufgeloest, sonst waere „morgen" bei einem woechentlichen
            # Termin leer. Die Zone geht ausdruecklich mit — ohne sie rechnet Google
            # in der Kalenderzone, die nicht die des Nutzers sein muss.
            "singleEvents": "true", "orderBy": "startTime",
            "timeZone": str(USER_TIMEZONE), "maxResults": "250",
        }
        payload = await self._request(
            "GET", f"/calendars/{self.calendar_id}/events", params=params)
        return [event_from_api(item) for item in ((payload or {}).get("items") or [])
                if item.get("status") != "cancelled"]

    async def get_event(self, event_id: str) -> CalendarEvent | None:
        payload = await self._request(
            "GET", f"/calendars/{self.calendar_id}/events/{event_id}")
        if not payload or payload.get("status") == "cancelled":
            return None
        return event_from_api(payload)

    async def create_event(self, *, summary: str, start: datetime, end: datetime,
                           all_day: bool, description: str, location: str,
                           client_id: str) -> CalendarEvent:
        body: dict[str, Any] = {"summary": summary, "id": client_id}
        if description:
            body["description"] = description
        if location:
            body["location"] = location
        if all_day:
            body["start"] = {"date": start.date().isoformat()}
            body["end"] = {"date": end.date().isoformat()}
        else:
            body["start"] = {"dateTime": start.isoformat(), "timeZone": str(USER_TIMEZONE)}
            body["end"] = {"dateTime": end.isoformat(), "timeZone": str(USER_TIMEZONE)}
        payload = await self._request(
            "POST", f"/calendars/{self.calendar_id}/events",
            # V1 laedt niemanden ein. Ohne geprueften Empfaengerweg verschickt
            # SOLVIO keine Einladungen — eine E-Mail an Fremde ist keine
            # Kalenderaenderung mehr, sondern Kommunikation.
            params={"sendUpdates": "none"}, json_body=body)
        if payload and payload.get("__conflict__"):
            # Dieselbe Anfrage war schon einmal da. Das ist kein Fehler, sondern
            # genau der Schutz, den die vergebene Kennung leisten soll.
            existing = await self.get_event(client_id)
            if existing is not None:
                log.info("calendar.create_deduplicated")
                return existing
            raise RuntimeError("calendar create conflicted but the event is absent")
        return event_from_api(payload or {})

    async def update_event(self, event_id: str, *, summary: str | None = None,
                           start: datetime | None = None, end: datetime | None = None,
                           description: str | None = None,
                           location: str | None = None) -> CalendarEvent:
        body: dict[str, Any] = {}
        if summary is not None:
            body["summary"] = summary
        if description is not None:
            body["description"] = description
        if location is not None:
            body["location"] = location
        if start is not None:
            body["start"] = {"dateTime": start.isoformat(), "timeZone": str(USER_TIMEZONE)}
        if end is not None:
            body["end"] = {"dateTime": end.isoformat(), "timeZone": str(USER_TIMEZONE)}
        payload = await self._request(
            "PATCH", f"/calendars/{self.calendar_id}/events/{event_id}",
            params={"sendUpdates": "none"}, json_body=body)
        if payload is None:
            raise RuntimeError("calendar update targeted a missing event")
        return event_from_api(payload)

    async def delete_event(self, event_id: str) -> None:
        # 404/410 kommen als None zurueck: schon weg ist der gewuenschte Zustand,
        # also kein Fehler. Zweimal loeschen ist derselbe Kalender wie einmal.
        await self._request("DELETE", f"/calendars/{self.calendar_id}/events/{event_id}",
                            params={"sendUpdates": "none"})


def from_settings(settings: Any, *, broker: Any = None) -> GoogleCalendar | None:
    """Baut den Anbieter — aus dem Tresor, sonst aus den Settings.

    Die Reihenfolge ist die Aussage: liegen die beiden Geheimnisse im Tresor,
    wird der Rueckfall aus der Konfiguration gar nicht erst gelesen. Damit ist
    „migriert" ein Zustand und keine Absicht.

    `client_id` und `calendar_id` bleiben Konfiguration. Sie sind keine
    Geheimnisse — eine Client-ID steht in jedem OAuth-Redirect, und sie in den
    Tresor zu legen machte ihn groesser, ohne irgendetwas zu schuetzen.
    """
    client_id = (getattr(settings, "google_calendar_client_id", "") or "").strip()
    calendar_id = (getattr(settings, "google_calendar_id", "") or "primary").strip()
    if not client_id:
        return None
    if broker is not None:
        have = all(broker.exists(ref) for ref in
                   (GoogleCalendar.CLIENT_SECRET_REF, GoogleCalendar.REFRESH_TOKEN_REF))
        if have:
            return GoogleCalendar(client_id=client_id, calendar_id=calendar_id,
                                  broker=broker)
    secret = (getattr(settings, "google_calendar_client_secret", "") or "").strip()
    refresh = (getattr(settings, "google_calendar_refresh_token", "") or "").strip()
    if not (secret and refresh):
        return None
    return GoogleCalendar(client_id=client_id, client_secret=secret,
                          refresh_token=refresh, calendar_id=calendar_id)
