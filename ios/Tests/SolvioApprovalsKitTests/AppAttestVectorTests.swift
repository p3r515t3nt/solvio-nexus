// App Attest binding golden-vector tests (STEP S2A.1). Proves the Swift binding
// canonicalisation + domain-separated clientDataHash match the Python control-plane against
// the SHARED tests/vectors/app_attest_binding_v1.json. The DECISION binding is built
// independently on both sides, so byte-exact agreement here freezes the App Attest contract.
import XCTest
@testable import SolvioApprovalsKit

final class AppAttestVectorTests: XCTestCase {

    private func load() throws -> [String: Any] {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "app_attest_binding_v1",
                                                  withExtension: "json"))
        let data = try Data(contentsOf: url)
        return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    private func hex(_ d: Data) -> String { d.map { String(format: "%02x", $0) }.joined() }

    func testEnrollmentBindingMatchesVector() throws {
        let e = try XCTUnwrap((try load())["enrollment"] as? [String: Any])
        let b = try XCTUnwrap(e["binding"] as? [String: Any])
        let binding = AppAttestEnrollmentBinding(
            coreInstanceID: b["core_instance_id"] as! String,
            principalID: b["principal_id"] as! String,
            deviceID: b["device_id"] as! String,
            enrollmentID: b["enrollment_id"] as! String,
            approvalKeyID: b["approval_key_id"] as! String,
            approvalPublicKeySHA256: b["approval_public_key_sha256"] as! String,
            attestationNonce: b["attestation_nonce"] as! String,
            issuedAt: (b["issued_at"] as! NSNumber).int64Value,
            expiresAt: (b["expires_at"] as! NSNumber).int64Value)
        XCTAssertEqual(hex(binding.canonicalBytes()), e["canonical_bytes_hex"] as! String)
        XCTAssertEqual(hex(binding.clientDataHash()), e["client_data_hash_hex"] as! String)
        // The production path hashes the EXACT server-issued bytes — must match too.
        let raw = try XCTUnwrap(Data(hexString: e["canonical_bytes_hex"] as! String))
        XCTAssertEqual(hex(AppAttestBinding.enrollmentClientDataHash(bindingBytes: raw)),
                       e["client_data_hash_hex"] as! String)
    }

    func testDecisionBindingMatchesVector() throws {
        let d = try XCTUnwrap((try load())["decision"] as? [String: Any])
        let b = try XCTUnwrap(d["binding"] as? [String: Any])
        let binding = AppAttestDecisionBinding(
            coreInstanceID: b["core_instance_id"] as! String,
            approvalID: b["approval_id"] as! String,
            deviceID: b["device_id"] as! String,
            decisionSHA256: b["decision_sha256"] as! String,
            challengeNonce: b["challenge_nonce"] as! String,
            approvalPublicKeySHA256: b["approval_public_key_sha256"] as! String)
        XCTAssertEqual(hex(binding.canonicalBytes()), d["canonical_bytes_hex"] as! String)
        XCTAssertEqual(hex(binding.clientDataHash()), d["client_data_hash_hex"] as! String)
    }
}

private extension Data {
    init?(hexString: String) {
        let chars = Array(hexString)
        guard chars.count % 2 == 0 else { return nil }
        var out = Data(); out.reserveCapacity(chars.count / 2)
        var i = 0
        while i < chars.count {
            guard let byte = UInt8(String(chars[i]) + String(chars[i + 1]), radix: 16) else { return nil }
            out.append(byte); i += 2
        }
        self = out
    }
}

// Approval Policy V2 (ADR-0022) — der Sitzungsbeweis des Sprachwegs.
//
// Er haengt daran, dass Mac und Telefon dieselbe Bindung BYTEGLEICH bilden.
// Tun sie es nicht, scheitert die Assertion still, das Telefon faellt auf die
// vorsichtige Freigabezeile zurueck, und niemand bemerkt, dass ein Merkmal
// weg ist. Ein Golden Vector ist der einzige Weg, das im Voraus zu wissen.
extension AppAttestVectorTests {
    func testVoiceSessionBindingMatchesTheSharedVector() throws {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "app_attest_binding_v1",
                                                  withExtension: "json"))
        let root = try XCTUnwrap(JSONSerialization.jsonObject(
            with: Data(contentsOf: url)) as? [String: Any])
        let vector = try XCTUnwrap(root["voice_session"] as? [String: Any])
        let fields = try XCTUnwrap(vector["binding"] as? [String: Any])
        let binding = VoiceSessionBinding(
            coreInstanceID: fields["core_instance_id"] as! String,
            deviceID: fields["device_id"] as! String,
            sessionNonce: fields["session_nonce"] as! String)
        XCTAssertEqual(hex(binding.canonicalBytes()),
                       vector["canonical_bytes_hex"] as! String)
        XCTAssertEqual(hex(binding.clientDataHash()),
                       vector["client_data_hash_hex"] as! String)
    }

    func testVoiceSessionAndDecisionHashesDiffer() throws {
        // Domain-Trennung, praktisch geprueft: dieselben Bytes duerfen unter
        // zwei Zwecken nie denselben Hash ergeben.
        let raw = Data("dieselben bytes".utf8)
        XCTAssertNotEqual(
            AppAttestBinding.domainHash(VoiceSessionBinding.domain, raw),
            AppAttestBinding.domainHash(AppAttestBinding.decisionDomain, raw))
    }
}

// SOLVIO PRESENCE V2 — der Mutationsbeweis des WISSEN-Schreibwegs.
//
// Dieselbe Lehre wie beim Sitzungsbeweis: die Bindung haengt daran, dass Mac
// und Telefon BYTEGLEICH kanonisieren — diesmal einschliesslich des inneren
// Auftrags (payload_sha256). Der Umlaut im Vektor-Statement ist Absicht: er
// pinnt die UTF-8-Kodierung ohne ASCII-Escape. Weicht eine Seite ab, faellt
// jede Mutation mit 401 — und niemand saehe der Kryptografie an, warum.
extension AppAttestVectorTests {
    func testMemoryMutationBindingMatchesTheSharedVector() throws {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "app_attest_binding_v1",
                                                  withExtension: "json"))
        let root = try XCTUnwrap(JSONSerialization.jsonObject(
            with: Data(contentsOf: url)) as? [String: Any])
        let vector = try XCTUnwrap(root["memory_mutation"] as? [String: Any])

        // Erst der innere Auftrag …
        let payload = try XCTUnwrap(vector["payload"] as? [String: Any])
        let mutation = MemoryMutationPayload(
            capability: payload["capability"] as! String,
            arguments: payload["arguments"] as! [String: String])
        let fields = try XCTUnwrap(vector["binding"] as? [String: Any])
        XCTAssertEqual(mutation.sha256Hex(), fields["payload_sha256"] as! String,
                       "der kanonisierte Auftrag weicht ab — jede Mutation fiele mit 401")

        // … dann die aeussere Bindung darueber.
        let binding = MemoryMutationBinding(
            coreInstanceID: fields["core_instance_id"] as! String,
            deviceID: fields["device_id"] as! String,
            nonce: fields["nonce"] as! String,
            payloadSHA256: fields["payload_sha256"] as! String)
        XCTAssertEqual(hex(binding.canonicalBytes()),
                       vector["canonical_bytes_hex"] as! String)
        XCTAssertEqual(hex(binding.clientDataHash()),
                       vector["client_data_hash_hex"] as! String)
    }

    /// Der Zahlungsbeweis — gegen genau dieselbe geteilte Tabelle.
    ///
    /// Der Tresor hat diesen Test NICHT: `vault_mutation` fehlt in der
    /// Vektordatei, und damit gibt es fuer seine Bindung keine Gegenprobe aus
    /// Swift. Diese Luecke wurde beim Uebernehmen der Vorlage gefunden und
    /// hier ausdruecklich nicht mitkopiert — bei Geld waere sie teurer.
    func testPaymentMutationBindingMatchesTheSharedVector() throws {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "app_attest_binding_v1",
                                                  withExtension: "json"))
        let root = try XCTUnwrap(JSONSerialization.jsonObject(
            with: Data(contentsOf: url)) as? [String: Any])
        let vector = try XCTUnwrap(root["payment_mutation"] as? [String: Any])

        // Erst der innere Auftrag …
        let payload = try XCTUnwrap(vector["payload"] as? [String: Any])
        let mutation = PaymentMutationPayload(
            capability: payload["capability"] as! String,
            arguments: payload["arguments"] as! [String: String],
            tokenSHA256: (payload["token_sha256"] as? String) ?? "")
        let fields = try XCTUnwrap(vector["binding"] as? [String: Any])
        XCTAssertEqual(mutation.sha256Hex(), fields["payload_sha256"] as! String,
                       "der kanonisierte Auftrag weicht ab — jede Zahlung fiele mit 401")

        // … dann die aeussere Bindung darueber.
        let binding = PaymentMutationBinding(
            coreInstanceID: fields["core_instance_id"] as! String,
            deviceID: fields["device_id"] as! String,
            nonce: fields["nonce"] as! String,
            payloadSHA256: fields["payload_sha256"] as! String)
        XCTAssertEqual(hex(binding.canonicalBytes()),
                       vector["canonical_bytes_hex"] as! String)
        XCTAssertEqual(hex(binding.clientDataHash()),
                       vector["client_data_hash_hex"] as! String)
    }

    /// Dieselben Bytes, vier verschiedene Haende — und vier verschiedene Hashes.
    ///
    /// Das ist der Grund, warum eine Wissens- oder Tresor-Assertion strukturell
    /// keine Zahlung eroeffnen kann. Nicht weil es verboten waere: weil der
    /// clientDataHash ein anderer ist.
    func testPaymentDomainDiffersFromEveryOtherDomain() throws {
        let raw = Data("dieselben bytes".utf8)
        let payment = AppAttestBinding.domainHash(PaymentMutationBinding.domain, raw)
        XCTAssertNotEqual(payment,
                          AppAttestBinding.domainHash(VaultMutationBinding.domain, raw))
        XCTAssertNotEqual(payment,
                          AppAttestBinding.domainHash(MemoryMutationBinding.domain, raw))
        XCTAssertNotEqual(payment,
                          AppAttestBinding.domainHash(VoiceSessionBinding.domain, raw))
        XCTAssertNotEqual(payment,
                          AppAttestBinding.domainHash(AppAttestBinding.decisionDomain, raw))
    }

    func testMutationDomainDiffersFromSessionAndDecision() throws {
        let raw = Data("dieselben bytes".utf8)
        let mutation = AppAttestBinding.domainHash(MemoryMutationBinding.domain, raw)
        XCTAssertNotEqual(mutation,
                          AppAttestBinding.domainHash(VoiceSessionBinding.domain, raw))
        XCTAssertNotEqual(mutation,
                          AppAttestBinding.domainHash(AppAttestBinding.decisionDomain, raw))
    }
}
