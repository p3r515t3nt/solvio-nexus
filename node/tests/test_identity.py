""" — client-identity parser (URI-SAN ownership, no CN fallback).

Fail-closed contract: EXACTLY ONE URI SAN total, and it must be
spiffe://solvio/client/<name>. Pure unit tests over constructed getpeercert() dicts;
the live end-to-end proof (rotation / cross-client / CN-spoof / no-SAN / multi-URI)
runs separately.
"""
from __future__ import annotations

import unittest

from solvio_node.jobs.identity import SAN_PREFIX, extract_client_identity

MAC = "spiffe://solvio/client/mac-core"
OTHER = "spiffe://solvio/client/ownership-test"


def cert(uri_sans, *, cn="x", dns=()):
    sans = tuple(("URI", u) for u in uri_sans) + tuple(("DNS", d) for d in dns)
    return {"subject": ((("commonName", cn),),), "subjectAltName": sans}


class TestClientIdentity(unittest.TestCase):
    def test_one_valid_uri_san(self):
        self.assertEqual(extract_client_identity(cert([MAC])), MAC)

    def test_rotation_same_san_same_identity(self):
        a = cert([MAC], cn="mac-core")
        b = cert([MAC], cn="rotated-subject")
        self.assertEqual(extract_client_identity(a), extract_client_identity(b))

    def test_cross_client_different_san_differs(self):
        self.assertNotEqual(extract_client_identity(cert([MAC])),
                            extract_client_identity(cert([OTHER])))

    def test_cn_spoof_has_no_effect(self):
        spoof = cert(["spiffe://solvio/client/spoofer"], cn="mac-core")
        self.assertNotEqual(extract_client_identity(spoof), MAC)
        self.assertEqual(extract_client_identity(spoof), "spiffe://solvio/client/spoofer")

    # -- fail-closed: exactly one URI SAN total -----------------------------
    def test_two_solvio_uri_sans_rejected(self):
        self.assertIsNone(extract_client_identity(cert([MAC, OTHER])))

    def test_solvio_plus_foreign_uri_rejected(self):
        self.assertIsNone(extract_client_identity(cert([MAC, "spiffe://other/client/x"])))
        self.assertIsNone(extract_client_identity(cert([MAC, "https://example.com/"])))

    def test_foreign_uri_only_rejected(self):
        self.assertIsNone(extract_client_identity(cert(["spiffe://evil/client/x"])))
        self.assertIsNone(extract_client_identity(cert(["https://solvio/client/x"])))

    def test_no_uri_san_rejected(self):
        self.assertIsNone(extract_client_identity(cert([])))
        self.assertIsNone(extract_client_identity(cert([], dns=("mac-core",))))

    # -- malformed / ambiguous ----------------------------------------------
    def test_wrong_trust_domain_rejected(self):
        self.assertIsNone(extract_client_identity(cert(["spiffe://solvio-evil/client/x"])))
        self.assertIsNone(extract_client_identity(cert(["spiffe://SOLVIO/client/x"])))

    def test_query_or_fragment_rejected(self):
        self.assertIsNone(extract_client_identity(cert([MAC + "?a=1"])))
        self.assertIsNone(extract_client_identity(cert([MAC + "#frag"])))

    def test_empty_or_extra_segment_rejected(self):
        self.assertIsNone(extract_client_identity(cert([SAN_PREFIX])))          # empty name
        self.assertIsNone(extract_client_identity(cert([SAN_PREFIX + "a/b"])))  # extra segment

    def test_wrong_path_rejected(self):
        self.assertIsNone(extract_client_identity(cert(["spiffe://solvio/worker/x"])))
        self.assertIsNone(extract_client_identity(cert(["spiffe://solvio/x"])))

    def test_no_cert_rejected(self):
        self.assertIsNone(extract_client_identity(None))
        self.assertIsNone(extract_client_identity({}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
