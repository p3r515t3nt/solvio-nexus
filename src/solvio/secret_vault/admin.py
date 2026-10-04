"""Was mit einem Zugang geschehen kann — anlegen, ersetzen, sperren, loeschen.

Diese Funktionen sind die EINZIGEN, die einen Klartext entgegennehmen. Sie sind
Core-eigen, sie sind keine Faehigkeit und kein Werkzeug, und ein Modell erreicht
sie nicht: die Faehigkeiten in `solvio.capabilities.secret_vault` rufen sie auf,
nachdem der Freigabeweg entschieden hat, und die Werte kommen aus dem
Aufnahmelager (`solvio.secret_vault.staging`), nie aus einem Argument.

Warum ein Zustandswechsel den Umschlag neu bildet: Zustand, Faehigkeiten, Ziele
und Executoren stehen in den zusaetzlichen Daten des Umschlags. Das ist die
Zusicherung, dass niemand eine widerrufene Zeile in der Datenbank auf `active`
dreht und sie danach benutzt. Der Preis ist, dass jede Aenderung den
Hauptschluessel braucht — und das ist der richtige Preis: eine Berechtigung zu
aendern IST ein Eingriff.

Zur Rotation (§25 des Auftrags): sie ist atomar, weil sie EINE Zeile ersetzt.
Es gibt keinen Zwischenzustand mit zwei gueltigen Werten, weil es ueberhaupt nie
zwei Werte gibt — der alte Geheimtext wird im selben `UPDATE` ueberschrieben.
Was bleibt, ist die Fassungsnummer und die Spur; beides traegt keinen Wert.
"""
from __future__ import annotations

from typing import Iterable, Mapping
from dataclasses import replace

from solvio.logging_setup import get_logger
from solvio.secret_vault import envelope as E
from solvio.secret_vault import keyring as K
from solvio.secret_vault import policy as VP
from solvio.secret_vault import refs as R
from solvio.secret_vault.store import (OUTCOME_MUTATED, VaultStore, utcnow_iso)

log = get_logger("vault")


class AdminError(RuntimeError):
    """Ein Verwaltungsvorgang ging nicht. Traegt nie einen Wert."""


#: Wie lang ein Geheimnis hoechstens sein darf. Die Grenze ist keine
#: Sicherheitsmassnahme, sondern eine Zusage an alles, was den Umschlag traegt —
#: und sie liegt weit unter der 64-kB-Grenze des Freigabe-Gateways.
MAX_SECRET_BYTES = 8 * 1024


def _kek_or_raise() -> bytes:
    try:
        kek = K.read_kek()
    except K.VaultLocked as exc:
        raise AdminError("vault_unavailable") from exc
    if kek is None:
        raise AdminError("vault_not_initialised")
    return kek


def initialize(store: VaultStore | None = None) -> bool:
    """Legt den Hauptschluessel an — aber nur, wenn der Tresor leer ist.

    Die Bedingung ist keine Vorsicht, sondern der Unterschied zwischen einem
    Erststart und einem stillen Totalverlust: ein neuer Schluessel neben altem
    Geheimtext macht diesen Geheimtext endgueltig unlesbar.
    """
    store = store or VaultStore()
    existing = K.read_kek()
    if existing is not None:
        return False
    if store.count() > 0:
        raise AdminError("vault_holds_entries_without_a_key")
    K.initialize_kek()
    store.meta_set("initialised_at", utcnow_iso())
    log.info("vault.initialised")
    return True


def _check_kind(kind: VP.SecretKind | str) -> VP.SecretKind:
    text = kind.value if isinstance(kind, VP.SecretKind) else str(kind)
    if text in VP.FORBIDDEN_KINDS:
        # Zahlungsmittel sind ein eigener Milestone mit eigener Sorgfaltspflicht.
        raise AdminError("kind_belongs_to_payment_capability")
    try:
        return VP.SecretKind(text)
    except ValueError as exc:
        raise AdminError("unknown_kind") from exc


def _check_scope(capabilities: Iterable[str], targets: Iterable[str],
                 executors: Iterable[VP.ExecutorId]) -> tuple[
                     tuple[str, ...], tuple[str, ...], tuple[VP.ExecutorId, ...]]:
    caps = tuple(sorted({c.strip() for c in capabilities if c and c.strip()}))
    exes = tuple(sorted(set(executors), key=lambda e: e.value))
    normalized = []
    for target in targets:
        clean = VP.normalize_target(target)
        if not clean:
            # Ein Ziel, das sich nicht als Herkunft lesen laesst, ist kein Ziel.
            # Es stillschweigend zu ergaenzen waere die Stelle, an der aus
            # `amazon.de` irgendwann `http://amazon.de` wird.
            raise AdminError("target_is_not_an_origin")
        normalized.append(clean)
    tgt = tuple(sorted(set(normalized)))
    if not caps or not tgt or not exes:
        # Vorgabe VERWEIGERN, hier schon beim Anlegen: ein Zugang ohne Zweck ist
        # kein Zugang, sondern ein Wert, der auf eine Gelegenheit wartet.
        raise AdminError("scope_must_not_be_empty")
    return caps, tgt, exes


def add(*, secret_ref: str, kind: VP.SecretKind | str, plaintext: bytes,
        allowed_capabilities: Iterable[str], allowed_targets: Iterable[str],
        allowed_executors: Iterable[VP.ExecutorId],
        display_name: str = "", service_label: str = "", account_label: str = "",
        note: str = "", allow_background: bool = False,
        requires_user_presence: bool = False,
        store: VaultStore | None = None, replace: bool = False) -> VP.SecretPolicy:
    """Legt einen Zugang an. `replace=False` verweigert ein Ueberschreiben."""
    store = store or VaultStore()
    ref = str(R.parse(secret_ref))
    if not plaintext:
        raise AdminError("secret_is_empty")
    if len(plaintext) > MAX_SECRET_BYTES:
        raise AdminError("secret_is_too_large")
    existing = store.policy(ref)
    if existing is not None and not replace:
        raise AdminError("secret_already_exists")

    caps, tgt, exes = _check_scope(allowed_capabilities, allowed_targets,
                                   allowed_executors)
    version = (existing.version + 1) if existing is not None else 1
    now = utcnow_iso()
    policy = VP.SecretPolicy(
        secret_ref=ref, kind=_check_kind(kind), version=version,
        status=VP.Status.ACTIVE, allowed_capabilities=caps, allowed_targets=tgt,
        allowed_executors=exes, allow_background=allow_background,
        requires_user_presence=requires_user_presence,
        display_name=display_name or (existing.display_name if existing else ""),
        service_label=service_label or (existing.service_label if existing else ""),
        account_label=account_label or (existing.account_label if existing else ""),
        note=note or (existing.note if existing else ""),
        created_at=(existing.created_at if existing else now),
        rotated_at=(now if existing else ""),
        last_used_at=(existing.last_used_at if existing else ""))

    kek = _kek_or_raise()
    try:
        sealed = E.seal(kek=kek, ref=ref, version=version,
                        policy_sha256=policy.digest(), plaintext=plaintext)
    finally:
        del kek
    store.put(policy, sealed)
    store.record(secret_ref=ref, outcome=OUTCOME_MUTATED,
                 capability="secret_add" if existing is None else "secret_replace",
                 secret_version=version)
    log.info("vault.secret_stored", secret_ref=ref, version=version,
             replaced=existing is not None)
    return policy


def replace_value(*, secret_ref: str, plaintext: bytes,
                  store: VaultStore | None = None) -> VP.SecretPolicy:
    """Rotation: neuer Wert, gleiche Berechtigung, neue Fassung."""
    store = store or VaultStore()
    ref = str(R.parse(secret_ref))
    existing = store.policy(ref)
    if existing is None:
        raise AdminError("unknown_secret")
    return add(secret_ref=ref, kind=existing.kind, plaintext=plaintext,
               allowed_capabilities=existing.allowed_capabilities,
               allowed_targets=existing.allowed_targets,
               allowed_executors=existing.allowed_executors,
               display_name=existing.display_name,
               service_label=existing.service_label,
               account_label=existing.account_label, note=existing.note,
               allow_background=existing.allow_background,
               requires_user_presence=existing.requires_user_presence,
               store=store, replace=True)


def replace_value_if_current(*, secret_ref: str, plaintext: bytes,
                             expected: Mapping[str, VP.SecretPolicy],
                             store: VaultStore) -> VP.SecretPolicy:
    """Owner-Anmeldung: kein Ueberschreiben einer inzwischen neuen Bindung."""
    ref = str(R.parse(secret_ref))
    before = expected.get(ref)
    if before is None or before.status is not VP.Status.ACTIVE:
        raise AdminError("rotation_binding_invalid")
    if not plaintext or len(plaintext) > MAX_SECRET_BYTES:
        raise AdminError("secret_size_invalid")
    following = replace(before, version=before.version + 1, rotated_at=utcnow_iso())
    kek = _kek_or_raise()
    try:
        sealed = E.seal(kek=kek, ref=ref, version=following.version,
                        policy_sha256=following.digest(), plaintext=plaintext)
    finally:
        del kek
    return store.rotate_if_current(following, sealed, expected=expected)


def _reseal(store: VaultStore, previous: VP.SecretPolicy,
            following: VP.SecretPolicy) -> None:
    """Baut den Umschlag unter der NEUEN Policy neu — mit demselben Wert."""
    stored = store.sealed(previous.secret_ref)
    if stored is None:
        raise AdminError("unknown_secret")
    sealed, digest = stored
    if digest != previous.digest():
        raise AdminError("envelope_rejected")
    kek = _kek_or_raise()
    plaintext = b""
    try:
        plaintext = E.unseal(kek=kek, ref=previous.secret_ref,
                             version=previous.version,
                             policy_sha256=previous.digest(), sealed=sealed)
        fresh = E.seal(kek=kek, ref=following.secret_ref, version=following.version,
                       policy_sha256=following.digest(), plaintext=plaintext)
    except E.EnvelopeError as exc:
        raise AdminError("envelope_rejected") from exc
    finally:
        del kek
        del plaintext
    store.put(following, fresh)


def set_status(*, secret_ref: str, status: VP.Status,
               store: VaultStore | None = None) -> VP.SecretPolicy:
    """Aktiv, deaktiviert, widerrufen. Wirkt sofort, nicht beim naechsten Start."""
    store = store or VaultStore()
    ref = str(R.parse(secret_ref))
    previous = store.policy(ref)
    if previous is None:
        raise AdminError("unknown_secret")
    if previous.status is status:
        return previous
    following = VP.SecretPolicy(**{**previous.__dict__, "status": status,
                                   "version": previous.version + 1})
    _reseal(store, previous, following)
    store.record(secret_ref=ref, outcome=OUTCOME_MUTATED,
                 capability=f"secret_{status.value}",
                 secret_version=following.version)
    log.info("vault.status_set", secret_ref=ref, status=status.value)
    return following


def rescope(*, secret_ref: str, allowed_capabilities: Iterable[str] | None = None,
            allowed_targets: Iterable[str] | None = None,
            allowed_executors: Iterable[VP.ExecutorId] | None = None,
            allow_background: bool | None = None,
            requires_user_presence: bool | None = None,
            store: VaultStore | None = None) -> tuple[VP.SecretPolicy, bool]:
    """Aendert die Berechtigung. Gibt zurueck, OB damit erweitert wurde.

    Die zweite Rueckgabe ist keine Zierde: eine Erweiterung ist eine staerkere
    Handlung als eine Einschraenkung, und der Aufrufer entscheidet daraufhin
    ueber Reibung. Diese Funktion selbst genehmigt nichts.
    """
    store = store or VaultStore()
    ref = str(R.parse(secret_ref))
    previous = store.policy(ref)
    if previous is None:
        raise AdminError("unknown_secret")
    caps, tgt, exes = _check_scope(
        allowed_capabilities if allowed_capabilities is not None
        else previous.allowed_capabilities,
        allowed_targets if allowed_targets is not None else previous.allowed_targets,
        allowed_executors if allowed_executors is not None
        else previous.allowed_executors)
    following = VP.SecretPolicy(**{
        **previous.__dict__,
        "version": previous.version + 1,
        "allowed_capabilities": caps,
        "allowed_targets": tgt,
        "allowed_executors": exes,
        "allow_background": (previous.allow_background if allow_background is None
                             else bool(allow_background)),
        "requires_user_presence": (previous.requires_user_presence
                                   if requires_user_presence is None
                                   else bool(requires_user_presence)),
    })
    widened = VP.widens(previous, following)
    _reseal(store, previous, following)
    store.record(secret_ref=ref, outcome=OUTCOME_MUTATED,
                 capability="secret_rescope" + ("_widened" if widened else ""),
                 secret_version=following.version)
    log.info("vault.rescoped", secret_ref=ref, widened=widened,
             version=following.version)
    return following, widened


def delete(*, secret_ref: str, store: VaultStore | None = None) -> bool:
    """Entfernt einen Zugang samt Geheimtext. Die Spur bleibt — sie traegt keinen Wert."""
    store = store or VaultStore()
    ref = str(R.parse(secret_ref))
    removed = store.delete(ref)
    if removed:
        store.record(secret_ref=ref, outcome=OUTCOME_MUTATED, capability="secret_delete")
        log.info("vault.secret_deleted", secret_ref=ref)
    return removed
