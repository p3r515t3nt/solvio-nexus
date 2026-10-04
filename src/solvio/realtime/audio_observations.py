"""Bounded process observations, never a microphone switch or conversation store.

Only authenticated endpoints bind devices. Each provider opening has a separate
generation: a late close must not make its successor appear closed. No audio,
transcript or credentials are retained. A lost process loses these observations.
"""
from collections import OrderedDict
from dataclasses import dataclass
import time

from .satellite_health import VERDICT_STALE_S


@dataclass
class Observation:
    device_id: str
    session_id: str
    generation: int
    channel: str
    observed_at: float
    conversation: str = "not_started"
    provider: str = "not_opened"
    received_at: float | None = None
    forwarded_at: float | None = None


class AudioObservations:
    def __init__(self, *, clock=time.time, limit=128):
        self.clock = clock
        self.limit = limit
        self._rows = OrderedDict()
        self._connections = OrderedDict()
        self.history_uncertain = False

    def connect(self, device_id, session_id, channel):
        self._connections[device_id] = (session_id, True, channel)
        self._connections.move_to_end(device_id)
        while len(self._connections) > self.limit:
            self._connections.popitem(last=False)
            self.history_uncertain = True
        self._add(device_id, session_id, 0, channel)

    def disconnect(self, device_id, session_id):
        if self._connections.get(device_id, (None,))[0] == session_id:
            self._connections[device_id] = (session_id, False, self._connections[device_id][2])

    def _add(self, device, session, generation, channel):
        key = (device, session, generation)
        self._rows[key] = Observation(device, session, generation, channel, self.clock())
        while len(self._rows) > self.limit:
            _, old = self._rows.popitem(last=False)
            if old.provider not in {"closed_confirmed", "not_opened"}:
                self.history_uncertain = True
        return self._rows[key]

    def event(self, device, session, generation, event):
        key = (device, session, generation)
        row = self._rows.get(key)
        if event == "opening":
            connection = self._connections.get(device)
            if connection is None or connection[:2] != (session, True) or row is not None:
                return
            row = self._add(device, session, generation, connection[2])
            row.conversation, row.provider = "opening", "connecting"
        if row is None:
            return
        # Terminal observations cannot be revived by an old reader/send callback.
        terminal = row.conversation == "ended"
        if terminal and event not in {"close_confirmed", "close_unknown"}:
            return
        if event == "close_unknown" and row.provider == "closed_confirmed":
            return
        if event == "ready" and not terminal:
            row.conversation, row.provider = "active", "ready"
        elif event == "received" and not terminal:
            row.received_at = self.clock()
        elif event == "forwarded" and not terminal:
            row.forwarded_at = self.clock()
        elif event == "lost" and not terminal:
            row.conversation, row.provider = "reconnecting", "unknown"
        elif event == "closing" and not terminal:
            row.conversation = "closing"
            if row.provider not in {"not_opened", "closed_confirmed"}:
                row.provider = "closing"
        elif event == "close_confirmed":
            row.provider = "closed_confirmed"
        elif event == "close_unknown" and row.provider != "closed_confirmed":
            row.provider = "unknown"
        elif event == "ended":
            row.conversation = "ended"
            if row.provider not in {"not_opened", "closed_confirmed"}:
                row.provider = "unknown"
        row.observed_at = self.clock()

    def snapshot(self, reports=()):
        now = self.clock()
        by_device = {}
        for row in self._rows.values():
            by_device.setdefault(row.device_id, []).append(row)
        report_map = {r.satellite_id: r for r in reports}
        devices = []
        for device in sorted(by_device.keys() | report_map.keys()):
            rows = by_device.get(device, [])
            row = rows[-1] if rows else None
            report = report_map.get(device)
            age = report.age(now) if report else None
            verdict_age = report.hearing.get("verdict_age_s") if report else None
            measurement_age = age + verdict_age if verdict_age is not None else None
            # IDLE means local wake-word capture, never microphone off. The age
            # includes both transport delay and the age already reported by Pi.
            fresh = (report is not None and not report.is_stale(now)
                     and measurement_age is not None and 0 <= measurement_age <= VERDICT_STALE_S)
            capture = "unknown"
            description = "Mikrofonaufnahme am Gerät nicht bestätigt."
            if fresh and report.verdict in {"healthy", "quiet"}:
                if report.state == "IDLE":
                    capture = "local_listening_reported"
                    description = "Das Gerät meldet lokales Lauschen auf das Weckwort. Das Mikrofon ist dabei aktiv."
                elif report.state == "ACTIVE":
                    capture = "capturing_reported"
                    description = "Das Gerät meldet eine aktive Audioaufnahme."
            previous_unconfirmed = sum(r.provider not in {"not_opened", "closed_confirmed"} for r in rows[:-1])
            if previous_unconfirmed:
                description += " Der Abschluss einer früheren Anbieterverbindung ist ungeklärt."
            connection = self._connections.get(device)
            devices.append({"device_id": device, "channel": row.channel if row else "voice_satellite",
                "conversation": row.conversation if row else "unknown",
                "provider": row.provider if row else "unknown", "capture": capture,
                "transport_connected": connection[1] if connection else None,
                "session_id": row.session_id if row else None,
                "generation": row.generation if row else None,
                "previous_unconfirmed": previous_unconfirmed,
                "last_received_at": row.received_at if row else None,
                "last_forwarded_at": row.forwarded_at if row else None,
                "report_age_s": age, "measurement_age_s": measurement_age,
                "description": description, "source": "Core-Verbindung und authentifizierter Gerätebericht",
                "observed_at": row.observed_at if row else report.received_at})
        return {"devices": devices, "observed_at": now,
                "history_uncertain": self.history_uncertain,
                "scope": "Core-Sprachverbindungen. Separate Sprachbrücken werden hier nicht beobachtet."}
