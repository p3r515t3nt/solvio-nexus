#!/usr/bin/env python3
"""M0/2 — create or rotate a satellite credential. Never prints the secret.

    python3 scripts/provision_satellite_credential.py <satellite_id>          # Mac side
    python3 scripts/provision_satellite_credential.py <satellite_id> --show-once

The secret lives OUTSIDE the repository, owner-readable only, written atomically. It is not
passed on a command line, so it never appears in a process listing or in shell history.
`--show-once` prints it exactly once so it can be installed on the satellite; without it the
value is only ever written to the file.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import tempfile
from hashlib import sha256

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from solvio.realtime import satellite_auth as SA  # noqa: E402

SECRET_BYTES = 32


def fingerprint(secret_hex: str) -> str:
    """A short, non-reversible label so both sides can be compared without exposing them."""
    return sha256(bytes.fromhex(secret_hex)).hexdigest()[:16]


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="provision a SOLVIO satellite credential")
    ap.add_argument("satellite_id")
    ap.add_argument("--path", default=None, help="credential file (default: %s)"
                    % SA.DEFAULT_CREDENTIAL_PATH)
    ap.add_argument("--show-once", action="store_true",
                    help="print the secret once so it can be installed on the satellite")
    ap.add_argument("--rotate", action="store_true",
                    help="replace an existing entry for this satellite_id")
    args = ap.parse_args(argv)

    path = os.path.expanduser(args.path or SA.credential_path())
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)

    data = {"satellites": {}}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        data.setdefault("satellites", {})
    if args.satellite_id in data["satellites"] and not args.rotate:
        print(f"{args.satellite_id} already has a credential "
              f"(fingerprint {fingerprint(data['satellites'][args.satellite_id])}). "
              f"Use --rotate to replace it.", file=sys.stderr)
        return 1

    secret_hex = secrets.token_hex(SECRET_BYTES)
    data["satellites"][args.satellite_id] = secret_hex

    # Atomic write with the right mode from the start — never a moment where it is readable.
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".satellite_auth-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)

    print(f"wrote {path} (mode 0600)")
    print(f"satellite_id: {args.satellite_id}")
    print(f"fingerprint:  {fingerprint(secret_hex)}")
    if args.show_once:
        print("\nInstall this on the satellite, then clear your scrollback:")
        print(secret_hex)
    else:
        print("\nRe-run with --show-once to display the secret for installation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
