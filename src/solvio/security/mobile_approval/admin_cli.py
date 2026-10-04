"""SOLVIO approval admin — TRUSTED LOCAL CONTROL PLANE (P1A).

Revocation is itself a security-critical action, so it deliberately lives here and NOT:
  - as an OpenAI/Realtime/Dispatcher tool (the language model must never revoke),
  - as a gateway HTTP route (no remote or web-content authority),
  - anywhere reachable by untrusted content.

Trust boundary for the current single-owner system: a local invocation by the owning macOS
user against the local state directory. The approval store is 0600 in a 0700 directory, so
OS file permissions are the enforcement. Nothing here is exposed over the network.

Revocations are TERMINAL. There is no un-revoke command on purpose — an explicit
owner-recovery policy is future work, and a "re-enable" verb would be the obvious thing to
abuse. To trust a device again, enroll it freshly (a revoked approval key stays blocked).

Usage:
    solvio-approval-admin devices            (alias: list-devices)
    solvio-approval-admin revocations        (alias: list-revocations)
    solvio-approval-admin revoke-device <device-id> [--reason ...]
                                             # sperrt AUCH Approval-Key + App-Attest-Key
    solvio-approval-admin revoke-key <approval-key-id-or-fingerprint> [--reason ...]
                                             (alias: revoke-approval-key)
    solvio-approval-admin revoke-app-attest-key <key-id> [--reason ...]

Without an installed entry point:
    PYTHONPATH=src .venv/bin/python3 -m solvio.security.mobile_approval.admin_cli devices
"""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
import os
import sys

from solvio.security.mobile_approval import app_attest as AA
from solvio.security.mobile_approval import control as C
from solvio.security.mobile_approval import crypto
from solvio.security.mobile_approval import execution as _X
from solvio.security.mobile_approval import identity
from solvio.security.mobile_approval import store as S


def _short(v, n=16):
    v = v or ""
    return v if len(v) <= n else v[:n] + "…"


class StateDirError(Exception):
    """The --state-dir does not point at an existing approval-control state."""


def _require_existing_state(state_dir: str) -> str:
    """P1A.5/M2: a revoke must never silently create a fresh, empty security store.

    A mistyped --state-dir used to initialise a new directory (including a new signing key),
    print "device gesperrt (terminal)" and exit 0 while the real store was untouched. With
    stderr redirected — as in any script — that is pure false assurance.
    """
    db = os.path.join(state_dir, "approval_control.sqlite3")
    if not os.path.isdir(state_dir):
        raise StateDirError(f"state-dir does not exist: {state_dir}")
    # P1A.6/§7: check the identity files BEFORE touching the database. Opening it — even
    # read-only — creates the WAL sidecars, and a command that is going to fail closed
    # should leave the directory byte-for-byte as it found it.
    for fname in (identity._ID_FILE, identity._KEY_FILE):
        if not os.path.isfile(os.path.join(state_dir, fname)):
            raise StateDirError(
                f"missing core identity file {fname} in {state_dir}. Admin commands never "
                f"create one — a new core identity would invalidate every enrolled device. "
                f"Restore the state directory or run the setup path.")
    if not os.path.isfile(db):
        raise StateDirError(f"no approval control store in {state_dir} "
                            f"(expected approval_control.sqlite3)")
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise StateDirError(f"cannot open {db}: {exc}") from None
    try:
        names = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    finally:
        con.close()
    missing = {"devices", "revocations", "approval_requests", "audit"} - names
    if missing:
        raise StateDirError(f"{db} is not an approval control store "
                            f"(missing tables: {', '.join(sorted(missing))})")
    return db


# `recover-execution` schreibt: es schliesst Versuche ab und setzt Request-Zustaende.
_WRITE_COMMANDS = {"recover-execution",
                   "revoke-device", "revoke-key", "revoke-approval-key",
                   "revoke-app-attest-key"}


async def _open(state_dir: str, *, read_only: bool):
    """P1A.7/§16: listings open READ-ONLY and never migrate.

    Opening used to run ALTER TABLE and the canonicalisation migration, so a nominally
    read-only `devices` rewrote the security database — and could mutate it before a later
    check failed the command closed. A read-only open uses a `mode=ro` connection with
    `query_only=ON` and refuses outright if a migration is pending, pointing at the
    writable startup path instead of quietly applying one.
    """
    db = _require_existing_state(state_dir)
    # Identity is fully loaded BEFORE the database is touched, so a failure here leaves the
    # directory exactly as it was — including no WAL sidecars.
    signing = identity.MacSigningKey.load(state_dir)
    core_id = identity.load_core_instance_id(state_dir)
    st = S.ApprovalControlStore(db, read_only=read_only)
    await st.open()
    cp = C.MobileApprovalControlPlane(st, signing, core_id)
    return st, cp


def _fingerprint(dev) -> str | None:
    pk = dev.get("public_key_x963")
    if not pk:
        return None
    try:
        return crypto.fingerprint(bytes.fromhex(pk))
    except ValueError:
        return None


async def _cmd_devices(cp, st, _args) -> int:
    devs = await st.list_devices_full()
    if not devs:
        print("keine Geräte registriert")
        return 0
    print(f"{'DEVICE_ID':40} {'STATUS':8} {'ATTESTATION':22} {'ENV':12} {'APPROVAL_KEY_ID':18}")
    for d in devs:
        print(f"{d['device_id']:40} {d['status']:8} "
              f"{(d['attestation_status'] or '-'):22} {(d['environment'] or '-'):12} "
              f"{(d['key_id'] or '-'):18}")
        fp = _fingerprint(d)
        if fp:
            print(f"{'':40} approval_key_sha256: {fp}")
        if d.get("app_attest_key_id"):
            print(f"{'':40} app_attest_key_id:   {_short(d['app_attest_key_id'], 44)}")
    return 0


async def _cmd_revocations(cp, st, _args) -> int:
    rows = await st.list_revocations()
    if not rows:
        print("keine Sperren")
        return 0
    print(f"{'KIND':16} {'VALUE':66} REASON")
    for r in rows:
        print(f"{r['kind']:16} {r['value']:66} {r['reason'] or '-'}")
    return 0


def _print_result(res) -> None:
    """P1A.5/H1: print ONLY what the committed transaction reported. No pre-read."""
    for kind, value in res.newly_revoked:
        print(f"  gesperrt        : {kind:15} {value}")
    for kind, value in res.already_revoked:
        print(f"  bereits gesperrt: {kind:15} {value}")
    print(f"  betroffene Geräte-Datensätze auf REVOKED gesetzt: {len(res.affected_devices)}")
    for did in res.affected_devices:
        print(f"    {did}")
    if len(res.identities) > 1:
        print("  Hinweis: dieses Schlüsselmaterial bleibt auch unter NEUEN device_ids gesperrt.")


async def _cmd_executions(cp, st, _args) -> int:
    """HYGIENE/H9: what an administrator needs after a restart, and nothing more.

    Read-only. It lists open execution attempts, what each one is bound to, and what the
    CENTRAL recovery policy says may be done about it. There is deliberately no
    "mark this as succeeded because I say so": that would be a way to fabricate an outcome
    nobody observed, which is the exact failure P1C exists to prevent. The safe actions live
    behind `recover-execution`, which calls the same policy the coordinator uses.
    """
    rows = await st.open_execution_attempts()
    if not rows:
        print("Keine offenen Executions.")
        return 0
    print(f"{'ATTEMPT':26} {'STATUS':17} {'SEMANTICS':21} {'CAPABILITY':16} EXECUTION")
    print("-" * 110)
    for r in rows:
        decision = _X.recovery_decision(r["status"], r["semantics"])
        print(f"{r['attempt_id']:26} {r['status']:17} {r['semantics']:21} "
              f"{(r['capability'] or ''):16} {r['execution_id']}")
        print(f"{'':26} approval={r['approval_id']}  device={r['device_id']}")
        print(f"{'':26} recovery -> {decision}"
              + (f"   detail: {r['detail']}" if r["detail"] else ""))
        if decision == _X.MANUAL_RECOVERY_REQUIRED:
            print(f"{'':26} !! Der externe Effekt KANN eingetreten sein. SOLVIO kann das "
                  f"nicht entscheiden;")
            print(f"{'':26}    prüfe die Gegenseite, bevor irgendetwas wiederholt wird.")
    return 0


async def _cmd_recover_execution(cp, st, args) -> int:
    """HYGIENE/H9: the only mutating recovery action, and it uses the central policy.

    It cannot invent an outcome. For a NON_IDEMPOTENT ambiguous attempt it will report that
    manual recovery is required and change nothing that claims knowledge.
    """
    coordinator = _RecoveryOnlyCoordinator(cp)
    out, status = await coordinator.recover_execution(args.approval_id)
    print(f"status: {status}")
    if out:
        for key in ("execution_id", "attempt_id", "status", "semantics", "decision"):
            if key in out:
                print(f"  {key}: {out[key]}")
    return 0 if status in ("already_succeeded", "closed_no_effect",
                           "manual_recovery_required", "no_open_attempt") else 1


class _RecoveryOnlyCoordinator:
    """The coordinator's recovery half, without an S1 broker or an executor.

    The admin CLI must never be able to START an execution — only to classify and close one.
    Passing no executor and no reconciler means `RETRY_SAME_KEY` and `RECONCILE_FIRST` report
    what they need instead of doing it.
    """

    def __init__(self, cp) -> None:
        from . import bridge as _bridge
        self._inner = _bridge.MobileApprovalCoordinator(cp, None, _bridge.MobileApprover())

    async def recover_execution(self, approval_id):
        return await self._inner.recover_execution(approval_id)


async def _cmd_revoke_device(cp, st, args) -> int:
    """P1A.5/H1+H2: revokes the device AND its bound key material, and reports exactly the
    scope the committed transaction produced — the previous version printed a scope read
    before the lock, so a concurrent re-enrollment made it name identities it had not
    revoked."""
    try:
        device_id = C.canonical_device_id(args.device_id)
    except ValueError as exc:
        print(f"FEHLER: {exc}", file=sys.stderr)
        return 2
    known = device_id in {d["device_id"] for d in await st.list_devices_full()}
    res = await cp.revoke_device(device_id, reason=args.reason)
    print(f"device gesperrt (terminal): {device_id}")
    _print_result(res)
    if not known:
        print("  WARNUNG: diese device_id war nicht registriert — es ist KEIN "
              "Schlüsselmaterial bekannt, das mitgesperrt werden konnte.", file=sys.stderr)
    return 0


_SHA256_HEX = 64
_KEY_ID_HEX = 16


async def _cmd_revoke_key(cp, st, args) -> int:
    """P1A.1/F1: EXACT matching only.

    A prefix match with "first hit wins" could revoke a DIFFERENT key than the operator
    meant while printing success — the worst possible failure for a revoke tool, because
    the operator then believes the stolen device is locked out. So: no prefixes, no
    first-match-wins. Accepted identifiers are exactly two, both printed verbatim by
    `devices`: the full 64-hex approval-key sha256, or the exact 16-hex key_id.
    0 matches -> fail closed. >1 distinct match -> fail closed. Success is printed only
    after the durable revoke actually happened.
    """
    needle = args.key.strip().lower()
    if len(needle) not in (_SHA256_HEX, _KEY_ID_HEX) or any(c not in "0123456789abcdef"
                                                            for c in needle):
        print(f"FEHLER: {args.key!r} ist kein gültiger Identifier. Erlaubt ist EXAKT einer "
              f"von beiden: approval_key_sha256 ({_SHA256_HEX} hex) oder key_id "
              f"({_KEY_ID_HEX} hex). Keine Präfixe.", file=sys.stderr)
        return 2

    devices = await st.list_devices_full()
    if len(needle) == _SHA256_HEX:
        matches = {needle}          # the fingerprint IS the identity; no lookup needed
    else:
        matches = {fp for d in devices
                   if (fp := _fingerprint(d)) and d["key_id"] == needle}
    if not matches:
        print(f"FEHLER: kein Approval-Key mit key_id {needle!r} registriert. "
              f"`devices` zeigt die gültigen Identifier.", file=sys.stderr)
        return 2
    if len(matches) > 1:
        print(f"FEHLER: key_id {needle!r} ist mehrdeutig ({len(matches)} verschiedene "
              f"Approval-Keys). Gib den vollen approval_key_sha256 an.", file=sys.stderr)
        for fp in sorted(matches):
            print(f"  Kandidat: {fp}", file=sys.stderr)
        return 2
    fingerprint = matches.pop()
    known = any(_fingerprint(d) == fingerprint for d in devices)
    if not known:
        # A mistyped 64-hex value would otherwise report success while the real key stays
        # active — the same false-assurance failure the prefix match had. Pre-emptively
        # revoking a not-yet-enrolled key is legitimate, so record it, but say so loudly.
        print(f"WARNUNG: kein registriertes Gerät nutzt diesen Approval-Key. Die Sperre "
              f"wird vorsorglich eingetragen — bitte prüfe auf Tippfehler.", file=sys.stderr)
    res = await cp.revoke_approval_key(fingerprint, reason=args.reason)
    print("approval key gesperrt (terminal):")
    _print_result(res)
    return 0


async def _cmd_revoke_app_attest_key(cp, st, args) -> int:
    try:
        # P1A.4/C1: identity is the decoded bytes. An alternate base64 spelling of the same
        # Apple key must resolve to the same revocation, not a second one that misses.
        key = AA.canonical_app_attest_key_id(args.key)
    except (AA.AppAttestIdentityError, ValueError) as exc:
        print(f"FEHLER: {exc}", file=sys.stderr)
        return 2
    known = any(d.get("app_attest_key_id") == key for d in await st.list_devices_full())
    if not known:
        print("WARNUNG: kein registriertes Gerät nutzt diesen App-Attest-Key. Die Sperre "
              "wird vorsorglich eingetragen — bitte prüfe auf Tippfehler.", file=sys.stderr)
    res = await cp.revoke_app_attest_key(key, reason=args.reason)
    print("app attest key gesperrt (terminal):")
    _print_result(res)
    return 0


_COMMANDS = {
    "executions": _cmd_executions,
    "list-executions": _cmd_executions,
    "recover-execution": _cmd_recover_execution,
    "devices": _cmd_devices,
    "list-devices": _cmd_devices,          # alias
    "revocations": _cmd_revocations,
    "list-revocations": _cmd_revocations,  # alias
    "revoke-device": _cmd_revoke_device,
    "revoke-key": _cmd_revoke_key,
    "revoke-approval-key": _cmd_revoke_key,  # alias
    "revoke-app-attest-key": _cmd_revoke_app_attest_key,
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="solvio-approval-admin",
        description="SOLVIO trusted local approval admin (Sperren sind endgültig)")
    p.add_argument("--state-dir", default=identity.DEFAULT_STATE_DIR,
                   help="Verzeichnis der Approval-Control-Plane")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("devices", aliases=["list-devices"], help="registrierte Geräte anzeigen")
    sub.add_parser("executions", aliases=["list-executions"],
                   help="offene Executions und die sichere Recovery-Entscheidung anzeigen")
    rex = sub.add_parser("recover-execution",
                         help="eine offene Execution über die zentrale Recovery-Policy klären")
    rex.add_argument("approval_id")
    sub.add_parser("revocations", aliases=["list-revocations"],
                   help="bestehende Sperren anzeigen")

    rd = sub.add_parser("revoke-device",
                        help="ein Gerät UND sein gebundenes Schlüsselmaterial endgültig "
                             "sperren (Approval-Key + App-Attest-Key)")
    rd.add_argument("device_id")
    rd.add_argument("--reason", default=None)

    rk = sub.add_parser("revoke-key", aliases=["revoke-approval-key"],
                        help="einen Approval-Key endgültig sperren (über device_ids hinweg)")
    rk.add_argument("key", help="EXAKT: approval_key_sha256 (64 hex) ODER key_id "
                                "(16 hex) — keine Präfixe")
    rk.add_argument("--reason", default=None)

    ra = sub.add_parser("revoke-app-attest-key", help="einen App-Attest-Key endgültig sperren")
    ra.add_argument("key", help="Apple App Attest key id (base64, 32 Byte). Äquivalente "
                               "Schreibweisen ergeben dieselbe Identität.")
    ra.add_argument("--reason", default=None)
    return p


async def _run(args) -> int:
    st, cp = await _open(args.state_dir, read_only=args.command not in _WRITE_COMMANDS)
    try:
        return await _COMMANDS[args.command](cp, st, args)
    finally:
        await st.close()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except S.ApprovalStoreCorrupt as exc:
        print(f"FEHLER: Approval-Store beschädigt: {exc}", file=sys.stderr)
        return 3
    except S.StateMigrationRequired as exc:
        print(f"FEHLER: {exc}\n  Es wurde NICHTS geändert und keine Migration angewendet.",
              file=sys.stderr)
        return 5
    except identity.MissingCoreIdentity as exc:
        print(f"FEHLER: {exc}\n  Es wurde KEINE Sperre eingetragen und keine Identität erzeugt.",
              file=sys.stderr)
        return 2
    except StateDirError as exc:
        print(f"FEHLER: {exc}\n  Es wurde KEINE Sperre eingetragen und kein Store angelegt.",
              file=sys.stderr)
        return 2
    except sqlite3.Error as exc:
        print(f"FEHLER: Approval-Store nicht verfügbar: {exc}\n"
              f"  Es wurde KEINE Sperre eingetragen.", file=sys.stderr)
        return 4
    except (OSError, ValueError) as exc:
        print(f"FEHLER: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
