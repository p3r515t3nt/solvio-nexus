// SOLVIO App Attest binding protocol V1 — mirrors the Python attest_protocol.
//
// Two domain-separated clientDataHash constructions, computed over EXACT canonical bytes:
//   ENROLLMENT — attestKey() clientDataHash. In production the Mac issues the exact binding
//   bytes and the app hashes THOSE bytes (see enrollmentClientDataHash(bindingBytes:)); the
//   builder here mirrors the Mac for the golden-vector contract test.
//   DECISION — generateAssertion() clientDataHash. The app builds this independently and the
//   Mac rebuilds it byte-for-byte, so the canonicalisation MUST match (all fields are
//   strings). Both:  clientDataHash = SHA256(domain || 0x00 || canonicalBytes).
import CryptoKit
import Foundation

public enum AppAttestBinding {
    public static let bindingProtocolVersion = 1
    public static let enrollmentDomain = Data("SOLVIO_APP_ATTEST_ENROLLMENT_V1".utf8)
    public static let decisionDomain = Data("SOLVIO_APP_ATTEST_DECISION_V1".utf8)

    static func domainHash(_ domain: Data, _ raw: Data) -> Data {
        var m = Data()
        m.append(domain)
        m.append(0x00)
        m.append(raw)
        return Data(SHA256.hash(data: m))
    }

    /// Enrollment clientDataHash over the EXACT server-issued binding bytes (for attestKey()).
    public static func enrollmentClientDataHash(bindingBytes: Data) -> Data {
        domainHash(enrollmentDomain, bindingBytes)
    }
}

public struct AppAttestEnrollmentBinding {
    public let coreInstanceID, principalID, deviceID, enrollmentID: String
    public let approvalKeyID, approvalPublicKeySHA256, attestationNonce: String
    public let issuedAt, expiresAt: Int64

    public init(coreInstanceID: String, principalID: String, deviceID: String,
                enrollmentID: String, approvalKeyID: String, approvalPublicKeySHA256: String,
                attestationNonce: String, issuedAt: Int64, expiresAt: Int64) {
        self.coreInstanceID = coreInstanceID; self.principalID = principalID
        self.deviceID = deviceID; self.enrollmentID = enrollmentID
        self.approvalKeyID = approvalKeyID; self.approvalPublicKeySHA256 = approvalPublicKeySHA256
        self.attestationNonce = attestationNonce; self.issuedAt = issuedAt; self.expiresAt = expiresAt
    }

    public func canonicalBytes() -> Data {
        ApprovalProtocol.canonicalBytes([
            "protocol_version": .int(Int64(AppAttestBinding.bindingProtocolVersion)),
            "type": .string("app_attest_enrollment_binding"),
            "core_instance_id": .string(coreInstanceID),
            "principal_id": .string(principalID),
            "device_id": .string(deviceID),
            "enrollment_id": .string(enrollmentID),
            "approval_key_id": .string(approvalKeyID),
            "approval_public_key_sha256": .string(approvalPublicKeySHA256),
            "attestation_nonce": .string(attestationNonce),
            "issued_at": .int(issuedAt),
            "expires_at": .int(expiresAt),
        ])
    }

    public func clientDataHash() -> Data {
        AppAttestBinding.domainHash(AppAttestBinding.enrollmentDomain, canonicalBytes())
    }
}

public struct AppAttestDecisionBinding {
    public let coreInstanceID, approvalID, deviceID: String
    public let decisionSHA256, challengeNonce, approvalPublicKeySHA256: String

    public init(coreInstanceID: String, approvalID: String, deviceID: String,
                decisionSHA256: String, challengeNonce: String, approvalPublicKeySHA256: String) {
        self.coreInstanceID = coreInstanceID; self.approvalID = approvalID
        self.deviceID = deviceID; self.decisionSHA256 = decisionSHA256
        self.challengeNonce = challengeNonce; self.approvalPublicKeySHA256 = approvalPublicKeySHA256
    }

    public func canonicalBytes() -> Data {
        ApprovalProtocol.canonicalBytes([
            "protocol_version": .int(Int64(AppAttestBinding.bindingProtocolVersion)),
            "type": .string("app_attest_decision_binding"),
            "core_instance_id": .string(coreInstanceID),
            "approval_id": .string(approvalID),
            "device_id": .string(deviceID),
            "decision_sha256": .string(decisionSHA256),
            "challenge_nonce": .string(challengeNonce),
            "approval_public_key_sha256": .string(approvalPublicKeySHA256),
        ])
    }

    public func clientDataHash() -> Data {
        AppAttestBinding.domainHash(AppAttestBinding.decisionDomain, canonicalBytes())
    }
}

/// Der Sitzungsbeweis fuer den Sprachweg (Approval Policy V2, ADR-0022).
///
/// Er beantwortet eine Frage, die die Transportkennung allein nicht beantworten
/// kann: Kommt diese Sprachsitzung wirklich von der attestierten App-Instanz auf
/// dem eingeschriebenen Geraet? Die Kennung ist ein STATISCHES Geheimnis — wer
/// sie besitzt, sieht fuer den Mac aus wie das Telefon. Solange sie nur Lesen
/// erlaubte, war das vertretbar; seit die iPhone-Zeile der Freigabematrix
/// Reibung abnimmt, bis hin zur Haustuer, ist es das nicht mehr.
///
/// Was der Beweis NICHT zeigt: dass die App im Vordergrund ist, dass ein Mensch
/// tippt, oder WER spricht. Er ist ausdruecklich kein Face ID — deshalb bleibt
/// alles wirklich Folgenreiche weiterhin biometrisch.
public struct VoiceSessionBinding {
    public static let domain = Data("SOLVIO_VOICE_SESSION_V1".utf8)

    public let coreInstanceID, deviceID, sessionNonce: String
    /// C3: der Chat, an den diese Sprachsitzung gebunden wird. Nur wenn
    /// gesetzt geht er in die Bytes ein — alte Bindungen bleiben bytegleich.
    public let conversationID: String?

    public init(coreInstanceID: String, deviceID: String, sessionNonce: String, conversationID: String? = nil) {
        self.coreInstanceID = coreInstanceID
        self.deviceID = deviceID
        self.sessionNonce = sessionNonce
        self.conversationID = (conversationID?.isEmpty == false) ? conversationID : nil
    }

    public func canonicalBytes() -> Data {
        var fields: [String: CanonicalValue] = [
            "protocol_version": .int(Int64(AppAttestBinding.bindingProtocolVersion)),
            "type": .string("voice_session_binding"),
            "core_instance_id": .string(coreInstanceID),
            "device_id": .string(deviceID),
            "session_nonce": .string(sessionNonce),
        ]
        if let conversationID { fields["conversation_id"] = .string(conversationID) }
        return ApprovalProtocol.canonicalBytes(fields)
    }

    /// clientDataHash fuer `generateAssertion()`. Eigener Domain-Separator:
    /// eine Sitzungs-Assertion kann nie als Entscheidungs-Assertion durchgehen.
    public func clientDataHash() -> Data {
        AppAttestBinding.domainHash(VoiceSessionBinding.domain, canonicalBytes())
    }
}
