"""Shared acceptance ledger extracted from the LiveKit prototype's abnahmebudget.

Same book and accounting, with serialized starts and fail-closed readback. This
module never chooses a budget path, creates an acceptance permission or imposes
an everyday conversation limit. Only the prototype's explicit first-start path
may request creation with its already authorized limits.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import fcntl
import json
import math
import os
import secrets
import tempfile
import time
from typing import Callable


class BudgetErschoepft(RuntimeError):
    """No confirmed remaining acceptance allowance; provider must not open."""


def _positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _validate(stand):
    if (not isinstance(stand, dict) or not _positive(stand.get("budget_s"))
            or type(stand.get("gespraeche_max")) is not int
            or stand["gespraeche_max"] <= 0 or not isinstance(stand.get("laeufe"), list)):
        raise BudgetErschoepft("Das Abnahmebuch ist ungueltig; es wird nicht erneuert.")
    for run in stand["laeufe"]:
        if (not isinstance(run, dict) or not _positive(run.get("beginn"))
                or not _positive(run.get("zuletzt")) or run["zuletzt"] < run["beginn"]
                or type(run.get("beendet")) is not bool):
            raise BudgetErschoepft("Ein Abnahmeabschnitt ist ungeklärt; kein neuer Start.")
    return stand


def lesen(pfad, *, budget_s=0.0, gespraeche_max=0, allow_create=False):
    try:
        with open(pfad, encoding="utf-8") as stream:
            return _validate(json.load(stream))
    except FileNotFoundError:
        if allow_create:
            return _validate({"budget_s": budget_s, "gespraeche_max": gespraeche_max, "laeufe": []})
        raise BudgetErschoepft("Kein freigegebenes Abnahmebuch vorhanden.") from None
    except (OSError, ValueError, TypeError):
        raise BudgetErschoepft("Das Abnahmebuch ist nicht lesbar; es wird nicht erneuert.") from None


@contextmanager
def _locked(pfad, *, allow_create=False):
    path = os.path.abspath(pfad)
    if allow_create:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    # Separate stable lock inode survives atomic replacement of the JSON book.
    try:
        fd = os.open(path + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError:
        raise BudgetErschoepft("Das Abnahmebuch kann nicht gesperrt werden.") from None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _schreiben(pfad, stand):
    directory = os.path.dirname(os.path.abspath(pfad))
    fd, temp = tempfile.mkstemp(dir=directory, prefix=".budget-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(stand, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, pfad)
    finally:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass


def verbraucht(stand):
    _validate(stand)
    return sum(run["zuletzt"] - run["beginn"] for run in stand["laeufe"])


def rest(pfad):
    stand = lesen(pfad)
    return max(0.0, stand["budget_s"] - verbraucht(stand))


def gespraeche(pfad):
    return len(lesen(pfad)["laeufe"])


@dataclass
class Lauf:
    pfad: str
    index: int
    beginn: float
    rest_bei_beginn: float
    uhr: Callable[[], float] = time.time
    token: str = ""
    _zuletzt: float = field(default=0.0, init=False)

    def __post_init__(self):
        self._zuletzt = self.beginn

    @property
    def verbraucht(self):
        return max(0.0, self._zuletzt - self.beginn)

    @property
    def rest(self):
        return max(0.0, self.rest_bei_beginn - self.verbraucht)

    def _fortschreiben(self, grund):
        with _locked(self.pfad):
            stand = lesen(self.pfad)
            runs = stand["laeufe"]
            if self.index >= len(runs):
                raise BudgetErschoepft("Der laufende Abnahmeabschnitt fehlt.")
            run = runs[self.index]
            if (not self.token or run.get("token") != self.token
                    or run["beginn"] != self.beginn):
                raise BudgetErschoepft("Das Abnahmebuch wurde waehrend des Laufs ersetzt.")
            self._zuletzt = max(self._zuletzt, run["zuletzt"], self.uhr())
            run["zuletzt"] = self._zuletzt
            if grund is not None:
                run["beendet"] = True
                run["grund"] = grund
            _schreiben(self.pfad, stand)

    def ticken(self):
        self._fortschreiben(None)
        return self.rest

    def beenden(self, grund):
        # Includes provider close time, even when that exceeds the allowance.
        self._fortschreiben(grund)


def beginnen(pfad, *, budget_s=0.0, gespraeche_max=0, uhr=time.time, allow_create=False):
    with _locked(pfad, allow_create=allow_create):
        stand = lesen(pfad, budget_s=budget_s, gespraeche_max=gespraeche_max,
                      allow_create=allow_create)
        offen = max(0.0, stand["budget_s"] - verbraucht(stand))
        zahl = len(stand["laeufe"])
        if any(not run["beendet"] for run in stand["laeufe"]):
            raise BudgetErschoepft("Ein vorheriger Abnahmeabschnitt ist noch offen oder ungeklärt.")
        if zahl >= stand["gespraeche_max"]:
            raise BudgetErschoepft(f"Der Abnahmelauf hat seine {stand['gespraeche_max']} Gespraeche verbraucht.")
        if offen <= 0:
            raise BudgetErschoepft("Vom Zeitbudget ist nichts mehr uebrig.")
        jetzt = uhr()
        if not _positive(jetzt):
            raise BudgetErschoepft("Keine gueltige Zeitmessung fuer den Abnahmestart.")
        token = secrets.token_hex(16)
        stand["laeufe"].append({"pid": os.getpid(), "beginn": jetzt, "zuletzt": jetzt,
                                "beendet": False, "grund": "", "token": token})
        _schreiben(pfad, stand)
        return Lauf(pfad, zahl, jetzt, offen, uhr, token)
