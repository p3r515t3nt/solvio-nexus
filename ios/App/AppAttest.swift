// Apple App Attest integration.
//
// A DCAppAttestService key — SEPARATE from the Face-ID Secure-Enclave APPROVAL key — proves
// this is a legitimate SOLVIO app instance on genuine Apple hardware. It is bound to the
// approval key at enrollment (its attestation is over the enrollment binding, which contains
// the approval-key fingerprint) and produces a fresh assertion for every approval.
//
// App Attest is app/device integrity, NEVER user authority. If App Attest is unsupported
// (e.g. Simulator) the app is not a trusted approver and approval is disabled (fail closed);
// there is no software fallback for the real approver.
import DeviceCheck
import Foundation

enum AppAttestError: Error, LocalizedError {
    case unsupported, generateFailed, attestFailed, assertFailed
    var errorDescription: String? {
        switch self {
        case .unsupported: return "App Attest wird auf diesem Gerät nicht unterstützt."
        case .generateFailed: return "App-Attest-Schlüssel konnte nicht erzeugt werden."
        case .attestFailed: return "App-Attest-Attestierung fehlgeschlagen."
        case .assertFailed: return "App-Attest-Assertion fehlgeschlagen."
        }
    }
}

enum AppAttestManager {
    private static let kKeyIdTag = "de.solvio.approvals.appAttestKeyId"

    static var isSupported: Bool { DCAppAttestService.shared.isSupported }

    static func loadKeyId() -> String? {
        guard let d = Keychain.load(tag: kKeyIdTag) else { return nil }
        return String(data: d, encoding: .utf8)
    }

    /// The persisted App Attest keyId, created once on first enrollment and reused after.
    static func ensureKeyId() async throws -> String {
        guard isSupported else { throw AppAttestError.unsupported }
        if let existing = loadKeyId() { return existing }
        let keyId = try await generateKey()
        Keychain.save(Data(keyId.utf8), tag: kKeyIdTag)
        return keyId
    }

    private static func generateKey() async throws -> String {
        try await withCheckedThrowingContinuation { cont in
            DCAppAttestService.shared.generateKey { keyId, err in
                if let keyId { cont.resume(returning: keyId) }
                else { cont.resume(throwing: err ?? AppAttestError.generateFailed) }
            }
        }
    }

    /// Attest the app-attest key over the enrollment binding's clientDataHash.
    static func attest(keyId: String, clientDataHash: Data) async throws -> Data {
        try await withCheckedThrowingContinuation { cont in
            DCAppAttestService.shared.attestKey(keyId, clientDataHash: clientDataHash) { obj, err in
                if let obj { cont.resume(returning: obj) }
                else { cont.resume(throwing: err ?? AppAttestError.attestFailed) }
            }
        }
    }

    /// Produce a fresh assertion over the decision binding's clientDataHash (2nd proof).
    static func assert(keyId: String, clientDataHash: Data) async throws -> Data {
        try await withCheckedThrowingContinuation { cont in
            DCAppAttestService.shared.generateAssertion(keyId, clientDataHash: clientDataHash) { a, err in
                if let a { cont.resume(returning: a) }
                else { cont.resume(throwing: err ?? AppAttestError.assertFailed) }
            }
        }
    }

    static func reset() { Keychain.delete(tag: kKeyIdTag) }
}
