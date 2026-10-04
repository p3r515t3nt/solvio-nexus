"""Der Makler — der EINZIGE Weg, auf dem ein Wert den Tresor verlaesst.

Der Satz, an dem dieser ganze Milestone haengt:

    Ein Agent darf WISSEN, dass ein Geheimnis existiert, und er darf eine
    legitime Faehigkeit anfordern, die es benutzt. Den Wert bekommt er nie.

Damit das keine Absichtserklaerung bleibt, ist der Makler so gebaut, dass es
gar keine Funktion gibt, die ein Modell aufrufen koennte, um einen Wert zu
bekommen. Es gibt kein `get_secret`, kein `reveal`, kein `export`. Was es gibt,
ist `use()` — und das ist ein Kontextmanager, der einem VERTRAUENSWUERDIGEN
Executor fuer die Dauer genau eines Vorgangs Zugriff gibt.

Vier Schranken, in dieser Reihenfolge, jede fuer sich ausreichend zum Nein:

1. **Herkunft.** Fremder Inhalt und eine nicht gesetzte Herkunft bekommen nichts.
2. **Policy.** Faehigkeit, Executor, Ziel, Zustand — alle vier muessen passen
   (`solvio.secret_vault.policy`).
3. **Aufrufer.** Der Modulname des tatsaechlichen Aufrufers muss zum behaupteten
   Executor passen. Ein generischer Aufrufer kommt nicht durch, auch wenn er die
   richtige Kennung mitgibt.
4. **Umschlag.** Die gespeicherte Policy-Pruefsumme muss zu der passen, die aus
   der Zeile gerechnet wird, und der Umschlag muss unter genau dieser Pruefsumme
   aufgehen. Wer in der Datenbank an einer Berechtigung dreht, bekommt keinen
   erweiterten Zugriff, sondern einen Fehlschlag.

Was der Makler NICHT tut: genehmigen. Ob die HANDLUNG erlaubt ist, hat vorher
Approval Policy V2 entschieden (ADR-0022). Der Makler bindet nur, WELCHES
Geheimnis in WELCHEN Vorgang darf.

Ehrlich zur Grenze beim Speicher: Python-Zeichenketten sind unveraenderlich.
`clear()` gibt eine Referenz frei, es ueberschreibt kein RAM, und CPython gibt
keine Zusage darueber, wann der Speicher wiederverwendet wird. Der Wert lebt
kuerzer, nicht garantiert nicht. Wer etwas anderes behauptet, verkauft eine
Sicherheit, die die Laufzeit nicht hergibt.
"""
from __future__ import annotations

import sys
from typing import Any, Iterator

from solvio.capabilities import policy as AP
from solvio.logging_setup import get_logger
from solvio.secret_vault import envelope as E
from solvio.secret_vault import keyring as K
from solvio.secret_vault import policy as VP
from solvio.secret_vault import refs as R
from solvio.secret_vault.store import (OUTCOME_DENIED, OUTCOME_FAILED,
                                       OUTCOME_USED, VaultStore, describe_row)

log = get_logger("vault")


class SecretDenied(RuntimeError):
    """Die Policy sagt nein. Traegt einen Grund, nie einen Wert.

    Der Text dieser Ausnahme kann bis ins Modell laufen (`tools/dispatcher.py`
    stellt `str(exc)` in ein Werkzeugergebnis). Deshalb steht hier nur eine
    Kennung aus einer geschlossenen Liste.
    """

    def __init__(self, reason: VP.Denied, secret_ref: str = "") -> None:
        super().__init__(f"secret_denied:{reason.value}")
        self.reason = reason
        self.secret_ref = secret_ref


class SecretUnavailable(RuntimeError):
    """Der Tresor antwortet nicht — gesperrt, fehlend, beschaedigt. Nie ein Wert."""


class SecretMaterial:
    """Ein geliehener Wert. Redigiert sich selbst in jeder Darstellung.

    `plaintext()` heisst absichtlich so: der Name ist greppbar, und jede Stelle,
    die ihn aufruft, ist damit in einem Review auffindbar. Es gibt genau vier
    solche Stellen im Baum, und ein Test zaehlt sie.
    """

    __slots__ = ("_value", "_ref", "_spent")

    def __init__(self, value: str, ref: str) -> None:
        self._value = value
        self._ref = ref
        self._spent = False

    def plaintext(self) -> str:
        if self._spent:
            raise SecretUnavailable("secret material was already released")
        return self._value

    def clear(self) -> None:
        self._value = ""
        self._spent = True

    # Jede Darstellung, die Python von selbst waehlen koennte, ist redigiert.
    def __repr__(self) -> str:
        return f"<SecretMaterial {self._ref} redacted>"

    __str__ = __repr__

    def __format__(self, _spec: str) -> str:
        return self.__repr__()

    def __bool__(self) -> bool:
        return bool(self._value) and not self._spent


class _Use:
    """Der Leihvorgang. Schreibt die Spur, egal wie er endet."""

    def __init__(self, broker: "SecretBroker", ref: str,
                 request: VP.UseRequest) -> None:
        self.broker = broker
        self.ref = ref
        self.request = request
        self.material: SecretMaterial | None = None

    def __enter__(self) -> SecretMaterial:
        self.material = self.broker._open(self.ref, self.request)
        return self.material

    def __exit__(self, kind, value, traceback) -> bool:
        if self.material is not None:
            self.material.clear()
        return False


class SecretBroker:
    """Haelt Ablage und Schluessel zusammen — und gibt nichts heraus."""

    def __init__(self, store: VaultStore | None = None) -> None:
        self._store = store if store is not None else VaultStore()

    @property
    def store(self) -> VaultStore:
        return self._store

    # -- Was ein Agent sehen darf -------------------------------------------
    def catalogue(self, *, capability: str = "",
                  include_inactive: bool = True) -> list[dict[str, Any]]:
        """Die sichere Beschreibung aller Zugaenge. Nie ein Wert, nie ein Umschlag."""
        out: list[dict[str, Any]] = []
        for policy in self._store.policies():
            row = self._store.row(policy.secret_ref)
            if row is None:
                continue
            if not include_inactive and policy.status is not VP.Status.ACTIVE:
                continue
            described = describe_row(row)
            if capability and capability not in described["allowed_capabilities"]:
                continue
            out.append(described)
        return out

    def describe(self, secret_ref: str) -> dict[str, Any] | None:
        row = self._store.row(secret_ref)
        if row is None:
            return None
        described = describe_row(row)
        if secret_ref == "secret://google/oauth-client":
            client_id = self._store.meta_get("google_oauth_client_id")
            if client_id:
                # Public OAuth registration ID, atomically switched with the
                # two existing encrypted credentials; never a secret value.
                described["oauth_client_id"] = client_id
        return described

    def exists(self, secret_ref: str) -> bool:
        return self._store.row(secret_ref) is not None

    # -- Der einzige Weg zu einem Wert --------------------------------------
    def use(self, secret_ref: str, *, executor: VP.ExecutorId, target: str,
            capability: str = "", origin: AP.OriginClass | None = None,
            approval_id: str = "", execution_id: str = "",
            user_present: bool | None = None, automation_id: str = "") -> _Use:
        """Leiht einem vertrauenswuerdigen Executor EINEN Wert fuer EINEN Vorgang.

        **Herkunft und Faehigkeit kommen aus dem laufenden Vorgang**, nicht vom
        Aufrufer (`solvio.secret_vault.context`). Der Router setzt sie, bevor er
        einen Executor ruft; ausserhalb eines Router-Vorgangs ist die Vorgabe
        `UNSPECIFIED`, und die verweigert der Tresor. Ein Executor kann sich
        seine Herkunft damit nicht aussuchen — er kann sie nur nicht haben.

        **Der Modulname des Aufrufers wird HIER genommen**, nicht vom Aufrufer
        behauptet: `sys._getframe(1)` ist der Rahmen dessen, der `use()`
        aufgerufen hat. Deshalb ist `use()` eine gewoehnliche Methode und kein
        Generator — bei einem `@contextmanager` waere Rahmen 1 die Bibliothek.
        """
        from solvio.secret_vault import context as SC
        running = SC.current()
        try:
            caller = str(sys._getframe(1).f_globals.get("__name__", ""))
        except (ValueError, AttributeError):  # pragma: no cover - Interpreterfrage
            caller = ""
        request = VP.UseRequest(
            capability=capability or running.capability,
            executor=executor, target=target,
            origin=origin if origin is not None else running.origin,
            caller_module=caller,
            approval_id=approval_id or running.approval_id,
            execution_id=execution_id or running.execution_id,
            user_present=(running.user_present if user_present is None
                          else user_present),
            automation_id=automation_id or running.automation_id)
        return _Use(self, str(secret_ref), request)

    def authorize(self, secret_ref: str, *, executor: VP.ExecutorId, target: str,
                  capability: str = "", origin: AP.OriginClass | None = None) -> None:
        """Darf dieser Vorgang dieses Geheimnis benutzen? Ohne es zu oeffnen.

        Fuer den einen Fall, den `use()` nicht abdeckt: ein Aufrufer haelt ein
        ABGELEITETES, kurzlebiges Zugangstoken im Speicher und will es erneut
        verwenden. Ohne diese Pruefung waere ein warmes Token stille Autoritaet
        — gemessen am 2026-09-03: eine Faehigkeit, die nicht im Scope stand,
        lief trotzdem durch, weil eine ANDERE, erlaubte Faehigkeit kurz zuvor
        ein Token geholt hatte (DEBT-0193).

        Dieselbe Entscheidungskette wie `_open()`, dieselbe Rahmen-Inspektion,
        derselbe Ablehnungs-Beleg — nur ohne Entschluesselung.

        **Eine Erlaubnis wird NICHT als `used` gebucht.** Es wurde nichts
        geoeffnet; eine Zugriffsspur, die jeden Cache-Treffer als
        Geheimnisnutzung fuehrt, waere unwahr. Eine ABLEHNUNG wird gebucht —
        sie ist das Sicherheitsereignis, auf das jemand schauen will.
        """
        from solvio.secret_vault import context as SC
        running = SC.current()
        try:
            caller = str(sys._getframe(1).f_globals.get("__name__", ""))
        except (ValueError, AttributeError):  # pragma: no cover
            caller = ""
        request = VP.UseRequest(
            capability=capability or running.capability,
            executor=executor, target=target,
            origin=origin if origin is not None else running.origin,
            caller_module=caller,
            approval_id=running.approval_id, execution_id=running.execution_id,
            user_present=running.user_present, automation_id=running.automation_id)
        try:
            parsed = str(R.parse(secret_ref))
        except R.InvalidSecretRef:
            raise self._deny(secret_ref[:64], request, VP.Denied.UNKNOWN_SECRET) from None
        row = self._store.row(parsed)
        if row is None:
            raise self._deny(parsed, request, VP.Denied.UNKNOWN_SECRET)
        policy = VP.from_row(row)
        verdict = VP.evaluate(policy, request)
        if not verdict.allowed:
            raise self._deny(parsed, request, verdict.reason or VP.Denied.UNKNOWN_SECRET,
                             policy.version)

    # -- Innen ---------------------------------------------------------------
    def _deny(self, ref: str, request: VP.UseRequest, reason: VP.Denied,
              version: int = 0) -> SecretDenied:
        self._store.record(secret_ref=ref, outcome=OUTCOME_DENIED,
                           capability=request.capability,
                           origin=request.origin.value,
                           executor=request.executor.value,
                           target=VP.normalize_target(request.target),
                           approval_id=request.approval_id,
                           execution_id=request.execution_id,
                           denied_reason=reason.value, secret_version=version)
        log.info("vault.denied", secret_ref=ref, reason=reason.value,
                 capability=request.capability[:64], origin=request.origin.value)
        return SecretDenied(reason, ref)

    def _open(self, ref: str, request: VP.UseRequest) -> SecretMaterial:
        try:
            parsed = str(R.parse(ref))
        except R.InvalidSecretRef:
            raise self._deny(ref[:64], request, VP.Denied.UNKNOWN_SECRET) from None

        row = self._store.row(parsed)
        if row is None:
            raise self._deny(parsed, request, VP.Denied.UNKNOWN_SECRET)
        policy = VP.from_row(row)

        verdict = VP.evaluate(policy, request)
        if not verdict.allowed:
            raise self._deny(parsed, request, verdict.reason or VP.Denied.UNKNOWN_SECRET,
                             policy.version)

        # Die GESPEICHERTE Pruefsumme gegen die GERECHNETE. Weichen sie ab, wurde
        # an der Zeile gedreht — dann wird gar nicht erst entschluesselt.
        stored = self._store.sealed(parsed)
        if stored is None:
            raise self._deny(parsed, request, VP.Denied.UNKNOWN_SECRET, policy.version)
        sealed, stored_digest = stored
        if stored_digest != policy.digest():
            raise self._deny(parsed, request, VP.Denied.ENVELOPE_REJECTED, policy.version)

        try:
            kek = K.read_kek()
        except K.VaultLocked as exc:
            self._store.record(secret_ref=parsed, outcome=OUTCOME_FAILED,
                               capability=request.capability,
                               origin=request.origin.value,
                               executor=request.executor.value,
                               target=VP.normalize_target(request.target),
                               denied_reason=VP.Denied.VAULT_UNAVAILABLE.value,
                               secret_version=policy.version)
            raise SecretUnavailable("vault key is not available") from exc
        if kek is None:
            raise self._deny(parsed, request, VP.Denied.VAULT_UNAVAILABLE, policy.version)

        try:
            plaintext = E.unseal(kek=kek, ref=parsed, version=policy.version,
                                 policy_sha256=policy.digest(), sealed=sealed)
        except E.EnvelopeError:
            raise self._deny(parsed, request, VP.Denied.ENVELOPE_REJECTED,
                             policy.version) from None
        finally:
            del kek

        self._store.touch_used(parsed)
        self._store.record(secret_ref=parsed, outcome=OUTCOME_USED,
                           capability=request.capability,
                           origin=request.origin.value,
                           executor=request.executor.value,
                           target=VP.normalize_target(request.target),
                           approval_id=request.approval_id,
                           execution_id=request.execution_id,
                           secret_version=policy.version)
        log.info("vault.used", secret_ref=parsed, capability=request.capability[:64],
                 executor=request.executor.value,
                 target=VP.normalize_target(request.target)[:64],
                 origin=request.origin.value)
        return SecretMaterial(plaintext.decode("utf-8"), parsed)
