// Device signing and biometrics.
//
// The ONLY approval authority is a Secure-Enclave P-256 key whose use is gated by
// `biometryCurrentSet` (Face ID). Its private key never leaves the Enclave; only the
// encrypted key blob (dataRepresentation) is stored in the keychain, ThisDeviceOnly. If
// the enrolled biometric set changes, `biometryCurrentSet` invalidates the key -> the app
// must re-pair (fail closed). On the Simulator the Secure Enclave is unavailable and a
// software key stands in ONLY so the UI/flow can be exercised — it is marked non-attested
// and must never be treated as the real gate.
import CryptoKit
import Foundation
import LocalAuthentication
import SolvioApprovalsKit

enum SignerError: Error { case secureEnclaveUnavailable, keyCreationFailed, notEnrolled }

protocol DeviceSigner: ApprovalSigner {
    var attested: Bool { get }
}

private let kKeyTag = "de.solvio.approvals.enclaveKeyBlob"

enum Keychain {
    static func save(_ data: Data, tag: String) {
        let q: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                                kSecAttrAccount as String: tag]
        SecItemDelete(q as CFDictionary)
        var add = q
        add[kSecValueData as String] = data
        add[kSecAttrAccessible as String] = kSecAttrAccessibleWhenUnlockedThisDeviceOnly
        SecItemAdd(add as CFDictionary, nil)
    }
    static func load(tag: String) -> Data? {
        let q: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                                kSecAttrAccount as String: tag,
                                kSecReturnData as String: true,
                                kSecMatchLimit as String: kSecMatchLimitOne]
        var out: AnyObject?
        guard SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess else { return nil }
        return out as? Data
    }
    static func delete(tag: String) {
        SecItemDelete([kSecClass as String: kSecClassGenericPassword,
                       kSecAttrAccount as String: tag] as CFDictionary)
    }
}

enum Biometrics {
    static var available: Bool {
        var err: NSError?
        return LAContext().canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: &err)
    }
    static var faceIDName: String {
        let ctx = LAContext()
        _ = ctx.canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: nil)
        return ctx.biometryType == .faceID ? "Face ID" : (ctx.biometryType == .touchID ? "Touch ID" : "Biometrie")
    }
}

/// Secure-Enclave, Face-ID-gated signer. Real authority on a physical device.
final class SecureEnclaveSigner: DeviceSigner {
    let attested = true
    private let blob: Data
    private let cachedPub: Data

    init(create: Bool) throws {
        guard SecureEnclave.isAvailable else { throw SignerError.secureEnclaveUnavailable }
        if let existing = Keychain.load(tag: kKeyTag) {
            let key = try SecureEnclave.P256.Signing.PrivateKey(dataRepresentation: existing)
            blob = existing
            cachedPub = key.publicKey.x963Representation
            return
        }
        guard create else { throw SignerError.notEnrolled }
        guard let access = SecAccessControlCreateWithFlags(
            nil, kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
            [.privateKeyUsage, .biometryCurrentSet], nil) else {
            throw SignerError.keyCreationFailed
        }
        let key = try SecureEnclave.P256.Signing.PrivateKey(accessControl: access)
        Keychain.save(key.dataRepresentation, tag: kKeyTag)
        blob = key.dataRepresentation
        cachedPub = key.publicKey.x963Representation
    }

    var publicKeyX963: Data { cachedPub }
    var keyID: String { ApprovalCrypto.keyID(x963: cachedPub) }

    /// Signing reloads the key with an authenticated LAContext -> triggers Face ID. A
    /// cancelled/failed Face ID throws, so no signature and no approval are produced.
    func sign(_ data: Data) throws -> Data {
        let ctx = LAContext()
        ctx.localizedReason = "SOLVIO-Aktion freigeben"
        let key = try SecureEnclave.P256.Signing.PrivateKey(
            dataRepresentation: blob, authenticationContext: ctx)
        return try key.signature(for: data).derRepresentation
    }

    static func reset() { Keychain.delete(tag: kKeyTag) }
    static var hasKey: Bool { Keychain.load(tag: kKeyTag) != nil }
}

/// Simulator/dev fallback. NON-attested; the UI must show it is not the real gate.
final class DevSoftwareSigner: DeviceSigner {
    let attested = false
    private let inner = SoftwareSigner()
    var publicKeyX963: Data { inner.publicKeyX963 }
    var keyID: String { inner.keyID }
    func sign(_ data: Data) throws -> Data { try inner.sign(data) }
}
