// SOLVIO Mobile Approval Protocol V1 — crypto.
//
// Interop with the Python `cryptography` control-plane:
//  - Public keys: X9.63 uncompressed points (P256 `.x963Representation`).
//  - Signatures: DER ECDSA P-256 over SHA-256 of the EXACT bytes (`.derRepresentation`).
//  - Verify over the exact received bytes; never re-serialise for verification.
import CryptoKit
import Foundation

public enum ApprovalCrypto {
    /// Verify a DER ECDSA P-256/SHA-256 signature over exact bytes with an X9.63 public key.
    public static func verify(publicKeyX963: Data, signatureDER: Data, data: Data) -> Bool {
        guard let pub = try? P256.Signing.PublicKey(x963Representation: publicKeyX963),
              let sig = try? P256.Signing.ECDSASignature(derRepresentation: signatureDER)
        else { return false }
        return pub.isValidSignature(sig, for: data)   // hashes `data` with SHA-256 internally
    }

    /// Stable, non-secret key identifier: sha256(x963)[:16] hex — matches the Python side.
    public static func keyID(x963: Data) -> String {
        String(sha256Hex(x963).prefix(16))
    }

    public static func sha256Hex(_ data: Data) -> String {
        SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }
}

/// The single approval authority: something that signs decision bytes with a P-256 key.
/// On a real iPhone this is the Secure-Enclave, Face-ID-gated key (see the app target).
public protocol ApprovalSigner {
    var publicKeyX963: Data { get }
    var keyID: String { get }
    func sign(_ data: Data) throws -> Data   // DER ECDSA P-256/SHA-256
}

/// DEV/TEST ONLY software key — NEVER the real authority. The Secure Enclave signer in the
/// app target is the only signer that counts as approval authority on a physical device.
public struct SoftwareSigner: ApprovalSigner {
    private let key: P256.Signing.PrivateKey
    public init() { key = P256.Signing.PrivateKey() }
    public init(pem: String) throws { key = try P256.Signing.PrivateKey(pemRepresentation: pem) }
    public var publicKeyX963: Data { key.publicKey.x963Representation }
    public var keyID: String { ApprovalCrypto.keyID(x963: publicKeyX963) }
    public func sign(_ data: Data) throws -> Data {
        try key.signature(for: data).derRepresentation
    }
}
