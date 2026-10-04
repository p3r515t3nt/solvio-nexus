// Der Handlungsbeweis fuer Zahlungen (SOLVIO PAYMENT CAPABILITY V1).
//
// Spiegelt VaultMutationBinding: dieselbe Kanonisierung, derselbe Aufbau, ein
// EIGENER Domain-Separator. Eine Zahlungs-Assertion kann damit nie als Tresor-,
// Wissens-, Sitzungs- oder Entscheidungs-Assertion durchgehen und umgekehrt —
// nicht weil es verboten waere, sondern weil der clientDataHash ein anderer ist.
//
// Das Feld `tokenSHA256` ist das Gegenstueck zu `secretSHA256` beim Tresor. Der
// Anbieter-Token reist NICHT in den Argumenten: er wuerde sonst im Freigabetext,
// im Autorisierungs-Digest und dauerhaft in der Freigabe-Datenbank des Macs
// landen. Was in den Argumenten steht, ist eine Einlagerungskennung. Damit der
// Beweis den Token trotzdem abdeckt, geht sein SHA-256 in die Bindung ein: ein
// nach dem Signieren vertauschter Token aendert diesen Hash, damit die Bindung,
// damit den clientDataHash — und die Assertion faellt durch.
//
// Was der Beweis NICHT zeigt: WER tippt. App Attest attestiert eine
// App-Instanz, keine Person. Deshalb bleibt jede Geldbewegung zusaetzlich
// biometrisch — nicht durch eine Pruefung in dieser Datei, sondern weil die
// Matrix des Macs sie als VERY_CRITICAL fuehrt.
import CryptoKit
import Foundation

/// Der Auftrag, den die Assertion beweist: WELCHE Faehigkeit, mit WELCHEN
/// Argumenten, ueber WELCHEN Anbieter-Token.
///
/// Die Argumente sind bewusst nur Strings — dieselbe Entscheidung wie ueberall
/// sonst: was das Protokoll nicht ausdruecken kann, kann zwischen App und Mac
/// auch nicht verschieden kanonisiert werden.
public struct PaymentMutationPayload {
    public let capability: String
    public let arguments: [String: String]
    /// SHA-256 des Anbieter-Tokens in Hex, oder leer bei Handlungen ohne Token.
    public let tokenSHA256: String

    public init(capability: String, arguments: [String: String],
                tokenSHA256: String = "") {
        self.capability = capability
        self.arguments = arguments
        self.tokenSHA256 = tokenSHA256
    }

    /// Kanonische Bytes von
    /// `{"arguments": {...}, "capability": "...", "token_sha256": "..."}`.
    ///
    /// Die Reihenfolge steht woertlich da, damit sie niemand versehentlich
    /// wegsortiert: "arguments" < "capability" < "token_sha256".
    public func canonicalBytes() -> Data {
        var out = Data("{\"arguments\":".utf8)
        out.append(ApprovalProtocol.canonicalBytes(arguments.mapValues { .string($0) }))
        out.append(Data((",\"capability\":"
                         + ApprovalProtocol.encodeString(capability)
                         + ",\"token_sha256\":"
                         + ApprovalProtocol.encodeString(tokenSHA256) + "}").utf8))
        return out
    }

    public func sha256Hex() -> String {
        ApprovalCrypto.sha256Hex(canonicalBytes())
    }
}

/// Bindet EINE Zahlungshandlung an Core-Instanz, Geraet, frische Nonce und den
/// exakten Auftrag.
public struct PaymentMutationBinding {
    public static let domain = Data("SOLVIO_PAYMENT_MUTATION_V1".utf8)

    public let coreInstanceID, deviceID, nonce, payloadSHA256: String

    public init(coreInstanceID: String, deviceID: String, nonce: String,
                payloadSHA256: String) {
        self.coreInstanceID = coreInstanceID
        self.deviceID = deviceID
        self.nonce = nonce
        self.payloadSHA256 = payloadSHA256
    }

    public func canonicalBytes() -> Data {
        ApprovalProtocol.canonicalBytes([
            "protocol_version": .int(Int64(AppAttestBinding.bindingProtocolVersion)),
            "type": .string("payment_mutation_binding"),
            "core_instance_id": .string(coreInstanceID),
            "device_id": .string(deviceID),
            "nonce": .string(nonce),
            "payload_sha256": .string(payloadSHA256),
        ])
    }

    public func clientDataHash() -> Data {
        AppAttestBinding.domainHash(PaymentMutationBinding.domain, canonicalBytes())
    }
}
