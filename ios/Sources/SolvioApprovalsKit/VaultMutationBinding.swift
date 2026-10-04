// Der Aenderungsbeweis fuer den Tresor (SOLVIO SECRET & CREDENTIAL VAULT V1).
//
// Spiegelt MemoryMutationBinding: dieselbe Kanonisierung, derselbe Aufbau, ein
// EIGENER Domain-Separator. Eine Tresor-Assertion kann damit nie als
// Wissens-, Sitzungs- oder Entscheidungs-Assertion durchgehen und umgekehrt.
//
// EIN Feld mehr als beim Wissensweg, und es ist das wichtigste dieser Datei:
// `secretSHA256`. Der Wert selbst reist NICHT in den Argumenten — er wuerde
// sonst im Freigabetext, im Autorisierungs-Digest und dauerhaft in der
// Freigabe-Datenbank des Macs landen. Was in den Argumenten steht, ist eine
// Einlagerungskennung. Damit der Beweis den Wert trotzdem abdeckt, geht sein
// SHA-256 in die Bindung ein: ein nach dem Signieren vertauschter Wert aendert
// diesen Hash, damit die Bindung, damit den clientDataHash — und die Assertion
// faellt durch.
//
// Was der Beweis NICHT zeigt: WER tippt. App Attest attestiert eine
// App-Instanz, keine Person. Deshalb bleibt jede Tresor-Aenderung zusaetzlich
// biometrisch — nicht durch eine Pruefung in dieser Datei, sondern weil die
// Matrix des Macs sie als VERY_CRITICAL fuehrt.
import CryptoKit
import Foundation

/// Der Auftrag, den die Assertion beweist: WELCHE Faehigkeit, mit WELCHEN
/// Argumenten, ueber WELCHEN Wert.
///
/// Die Argumente sind bewusst nur Strings — dieselbe Entscheidung wie ueberall
/// sonst: was das Protokoll nicht ausdruecken kann, kann zwischen App und Mac
/// auch nicht verschieden kanonisiert werden.
public struct VaultMutationPayload {
    public let capability: String
    public let arguments: [String: String]
    /// SHA-256 des Wertes in Hex, oder leer bei Handlungen ohne Wert.
    public let secretSHA256: String

    public init(capability: String, arguments: [String: String],
                secretSHA256: String = "") {
        self.capability = capability
        self.arguments = arguments
        self.secretSHA256 = secretSHA256
    }

    /// Kanonische Bytes von
    /// `{"arguments": {...}, "capability": "...", "secret_sha256": "..."}`.
    ///
    /// Die Reihenfolge steht woertlich da, damit sie niemand versehentlich
    /// wegsortiert: "arguments" < "capability" < "secret_sha256".
    public func canonicalBytes() -> Data {
        var out = Data("{\"arguments\":".utf8)
        out.append(ApprovalProtocol.canonicalBytes(arguments.mapValues { .string($0) }))
        out.append(Data((",\"capability\":"
                         + ApprovalProtocol.encodeString(capability)
                         + ",\"secret_sha256\":"
                         + ApprovalProtocol.encodeString(secretSHA256) + "}").utf8))
        return out
    }

    public func sha256Hex() -> String {
        ApprovalCrypto.sha256Hex(canonicalBytes())
    }
}

/// Bindet EINE Tresor-Aenderung an Core-Instanz, Geraet, frische Nonce und den
/// exakten Auftrag.
public struct VaultMutationBinding {
    public static let domain = Data("SOLVIO_VAULT_MUTATION_V1".utf8)

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
            "type": .string("vault_mutation_binding"),
            "core_instance_id": .string(coreInstanceID),
            "device_id": .string(deviceID),
            "nonce": .string(nonce),
            "payload_sha256": .string(payloadSHA256),
        ])
    }

    public func clientDataHash() -> Data {
        AppAttestBinding.domainHash(VaultMutationBinding.domain, canonicalBytes())
    }
}
