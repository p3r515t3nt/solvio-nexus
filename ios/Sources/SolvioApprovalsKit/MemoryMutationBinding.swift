// Der Aenderungsbeweis fuer das Gedaechtnis (SOLVIO PRESENCE V2).
//
// Spiegelt VoiceSessionBinding: dieselbe Kanonisierung, derselbe Aufbau, ein
// EIGENER Domain-Separator. Er beantwortet die Frage, die die Transportkennung
// allein nicht beantworten kann: Kommt diese Aenderung wirklich von der
// attestierten App-Instanz auf dem eingeschriebenen Geraet — und meint sie
// GENAU diesen Auftrag? Deshalb steckt der SHA-256 des kanonisierten Auftrags
// mit im Binding: eine unterwegs vertauschte Kennung wuerde den Beweis brechen.
//
// Was der Beweis NICHT zeigt: WER tippt. Er ist ausdruecklich kein Face ID —
// unter Approval Policy V2 ist die registrierte App ein vertrauenswuerdiger
// interaktiver Ursprung fuer Gedaechtnis-Pflege, und alles wirklich
// Folgenreiche bleibt weiterhin biometrisch.
import CryptoKit
import Foundation

/// Der Auftrag, den die Assertion beweist: WELCHE Faehigkeit, mit WELCHEN
/// Argumenten.
///
/// Die Argumente sind bewusst nur Strings — dieselbe Entscheidung wie im
/// Entscheidungsprotokoll: was das Protokoll nicht ausdruecken kann, kann
/// zwischen App und Mac auch nicht verschieden kanonisiert werden.
public struct MemoryMutationPayload {
    public let capability: String
    public let arguments: [String: String]

    public init(capability: String, arguments: [String: String]) {
        self.capability = capability
        self.arguments = arguments
    }

    /// Kanonische Bytes von `{"arguments": {...}, "capability": "..."}`.
    ///
    /// `ApprovalProtocol.canonicalBytes` kennt absichtlich keine
    /// verschachtelten Objekte. Das aeussere Objekt hat genau zwei Schluessel,
    /// und "arguments" < "capability" IST die sortierte Reihenfolge — sie steht
    /// hier woertlich, damit sie niemand versehentlich wegsortiert. Das innere
    /// Objekt kanonisiert dieselbe Routine wie ueberall sonst.
    public func canonicalBytes() -> Data {
        var out = Data("{\"arguments\":".utf8)
        out.append(ApprovalProtocol.canonicalBytes(arguments.mapValues { .string($0) }))
        out.append(Data((",\"capability\":"
                         + ApprovalProtocol.encodeString(capability) + "}").utf8))
        return out
    }

    public func sha256Hex() -> String {
        ApprovalCrypto.sha256Hex(canonicalBytes())
    }
}

/// Bindet EINE Gedaechtnis-Aenderung an Core-Instanz, Geraet, frische Nonce
/// und den exakten Auftrag.
public struct MemoryMutationBinding {
    public static let domain = Data("SOLVIO_MEMORY_MUTATION_V1".utf8)

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
            "type": .string("memory_mutation_binding"),
            "core_instance_id": .string(coreInstanceID),
            "device_id": .string(deviceID),
            "nonce": .string(nonce),
            "payload_sha256": .string(payloadSHA256),
        ])
    }

    /// clientDataHash fuer `generateAssertion()`. Eigener Domain-Separator:
    /// eine Mutations-Assertion kann nie als Entscheidungs- oder
    /// Sitzungs-Assertion durchgehen — und umgekehrt.
    public func clientDataHash() -> Data {
        AppAttestBinding.domainHash(MemoryMutationBinding.domain, canonicalBytes())
    }
}
