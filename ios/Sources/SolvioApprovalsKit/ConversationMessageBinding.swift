// C3: eine Textnachricht in einem dauerhaften Chat, mit App-Attest-Beweis.
//
// Der Vertrag ist der des Core-Moduls `conversation/message_proof.py`:
// kanonischer Body {conversation_id, client_message_id, text} plus optional
// genau EIN Anhang (`attachments`) und optional ein Ziel (`target`);
// Digest = sha256(kanonische Bytes); Binding-Typ
// `app_conversation_message_binding`; Domain `SOLVIO_APP_CONVERSATION_MESSAGE_V1`.
// Eine Task-/Followup-Assertion kann nie als Nachricht durchgehen, weil die
// Domain und der Binding-Typ verschieden sind — beides wird hier geprueft,
// bevor irgendetwas signiert wird.
import Foundation

public enum ConversationMessageError: Error, Equatable, LocalizedError {
    case invalidBody, invalidChallenge, expired, alreadySending, invalidResponse
    public var errorDescription: String? {
        switch self {
        case .invalidBody: return "Schreibe eine Nachricht mit 1 bis 4000 Zeichen."
        case .invalidChallenge: return "Die Antwort passt nicht zu dieser Nachricht und diesem Gerät."
        case .expired: return "Die Anfrage ist abgelaufen. Bitte erneut versuchen."
        case .alreadySending: return "Die Nachricht wird bereits gesendet."
        case .invalidResponse: return "Der Core hat die Nachricht nicht eindeutig bestätigt."
        }
    }
}

func conversationIdentifier(_ value: String) -> Bool {
    value.range(of: #"\Ac-[a-f0-9]{16}\z"#, options: .regularExpression) != nil
}
func deliveryIdentifier(_ value: String) -> Bool {
    value.range(of: #"\Acd-[a-f0-9]{16}\z"#, options: .regularExpression) != nil
}
private func messageIdentifier(_ value: String) -> Bool {
    value.range(of: #"\Am-[a-f0-9]{16}\z"#, options: .regularExpression) != nil
}
private func taskIdentifier(_ value: String) -> Bool {
    value.range(of: #"\Aat-[a-f0-9]{16}\z"#, options: .regularExpression) != nil
}
private func runIdentifier(_ value: String) -> Bool {
    value.range(of: #"\Aar-[a-f0-9]{16}\z"#, options: .regularExpression) != nil
}
private func requestIdentifier(_ value: String) -> Bool {
    value.range(of: #"\A[A-Za-z0-9][A-Za-z0-9._:-]{7,127}\z"#, options: .regularExpression) != nil
}

/// Genau EIN Anhang: die vorhandene Datei- oder Dokumentauswahl, in genau der
/// kanonischen Form, die der Core fuer Auftraege schon kennt.
public enum AppConversationAttachment: Codable, Equatable, Sendable {
    case files(AppTaskFileRequest)
    case document(AppTaskDocumentRequest)

    public init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if let files = try? container.decode(AppTaskFileRequest.self) { self = .files(files); return }
        self = .document(try container.decode(AppTaskDocumentRequest.self))
    }
    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case let .files(request): try container.encode(request)
        case let .document(request): try container.encode(request)
        }
    }
    var json: TaskCanonicalValue {
        switch self {
        case let .files(request): return request.json
        case let .document(request): return request.json
        }
    }
    /// Beschreibt fehlende Eingabe nach einem Neustart — nie Namen oder Inhalte.
    public var kind: String {
        switch self { case .files: return "files"; case .document: return "document" }
    }
}

/// Eine ausdrueckliche Folgeanweisung an genau diesen Auftrag.
public struct AppConversationMessageTarget: Codable, Equatable, Sendable {
    public let task_id, run_id: String
    public let revision: Int
    enum CodingKeys: String, CodingKey { case task_id, run_id, revision }
    public init(taskID: String, runID: String, revision: Int) throws {
        guard taskIdentifier(taskID), runIdentifier(runID), revision >= 1 else { throw ConversationMessageError.invalidBody }
        task_id = taskID; run_id = runID; self.revision = revision
    }
    public init(from decoder: Decoder) throws {
        try requireTaskKeys(decoder, required: ["task_id", "run_id", "revision"])
        let fields = try decoder.container(keyedBy: CodingKeys.self)
        try self.init(taskID: fields.decode(String.self, forKey: .task_id), runID: fields.decode(String.self, forKey: .run_id),
                      revision: fields.decode(Int.self, forKey: .revision))
    }
    var json: TaskCanonicalValue {
        .object(["task_id": .string(task_id), "run_id": .string(run_id), "revision": .number(revision)])
    }
}

public struct AppConversationMessageBody: Codable, Equatable, Sendable {
    public let conversation_id, client_message_id, text: String
    public let attachments: AppConversationAttachment?
    public let target: AppConversationMessageTarget?
    enum CodingKeys: String, CodingKey { case conversation_id, client_message_id, text, attachments, target }

    public static let maxTextScalars = 4000

    /// 1…4000 Unicode-Skalare, getrimmt, ohne NUL. Andere C0-Steuerzeichen als
    /// Tab, Zeilen- und Wagenruecklauf sind ebenfalls ausgeschlossen: Python
    /// serialisiert sie anders als dieser Kanonisierer (\b, \f) bzw. strippt
    /// sie (\x1c–\x1f) — ein Digest, der drueben nie passt, wird hier gar
    /// nicht erst gebildet.
    public static func validText(_ text: String) -> Bool {
        let length = text.unicodeScalars.count
        return (1...maxTextScalars).contains(length)
            && text == text.trimmingCharacters(in: .whitespacesAndNewlines)
            && !text.unicodeScalars.contains(where: { $0.value < 32 && ![9, 10, 13].contains($0.value) })
    }

    public init(conversationID: String, clientMessageID: String, text: String,
                attachments: AppConversationAttachment? = nil, target: AppConversationMessageTarget? = nil) throws {
        guard conversationIdentifier(conversationID), requestIdentifier(clientMessageID), Self.validText(text) else {
            throw ConversationMessageError.invalidBody
        }
        conversation_id = conversationID; client_message_id = clientMessageID; self.text = text
        self.attachments = attachments; self.target = target
    }

    public init(from decoder: Decoder) throws {
        try requireTaskKeys(decoder, required: ["conversation_id", "client_message_id", "text"], optional: ["attachments", "target"])
        let fields = try decoder.container(keyedBy: CodingKeys.self)
        if fields.contains(.attachments), try fields.decodeNil(forKey: .attachments) { throw ConversationMessageError.invalidBody }
        if fields.contains(.target), try fields.decodeNil(forKey: .target) { throw ConversationMessageError.invalidBody }
        try self.init(conversationID: fields.decode(String.self, forKey: .conversation_id),
                      clientMessageID: fields.decode(String.self, forKey: .client_message_id),
                      text: fields.decode(String.self, forKey: .text),
                      attachments: fields.decodeIfPresent(AppConversationAttachment.self, forKey: .attachments),
                      target: fields.decodeIfPresent(AppConversationMessageTarget.self, forKey: .target))
    }

    public func canonicalBytes() -> Data {
        var fields: [String: TaskCanonicalValue] = [
            "conversation_id": .string(conversation_id), "client_message_id": .string(client_message_id),
            "text": .string(text)]
        if let attachments { fields["attachments"] = attachments.json }
        if let target { fields["target"] = target.json }
        return Data(TaskCanonicalValue.object(fields).encoded.utf8)
    }
    public var requestDigest: String { ApprovalCrypto.sha256Hex(canonicalBytes()) }
}

/// Nur Kennungen und Digests im Schluesselbund — nie der Text, nie ein Anhang,
/// nie eine Nonce. Der Core bleibt der einzige Nachrichtenspeicher.
public struct ConversationMessageRetryBinding: Codable, Equatable, Sendable {
    public let coreID, deviceID, conversationID, clientMessageID, bodyDigest: String
    public let deliveryID: String?
    public let attachmentKind: String?

    public init(body: AppConversationMessageBody, coreID: String, deviceID: String, deliveryID: String? = nil) {
        self.coreID = coreID; self.deviceID = deviceID
        conversationID = body.conversation_id; clientMessageID = body.client_message_id
        bodyDigest = body.requestDigest; self.deliveryID = deliveryID
        attachmentKind = body.attachments?.kind
    }

    /// Dieselbe Nachricht (Chat, Text, Anhang, Ziel) auf demselben Core und
    /// Geraet bekommt dieselbe `client_message_id` zurueck — sonst nichts.
    public func restore(conversationID: String, text: String, attachments: AppConversationAttachment? = nil,
                        target: AppConversationMessageTarget? = nil, coreID: String, deviceID: String) -> AppConversationMessageBody? {
        guard self.coreID == coreID, self.deviceID == deviceID, self.conversationID == conversationID,
              let body = try? AppConversationMessageBody(conversationID: conversationID, clientMessageID: clientMessageID,
                                                         text: text, attachments: attachments, target: target),
              body.requestDigest == bodyDigest else { return nil }
        return body
    }

    public func withDelivery(_ deliveryID: String) -> Self {
        Self(coreID: coreID, deviceID: deviceID, conversationID: conversationID, clientMessageID: clientMessageID,
             bodyDigest: bodyDigest, deliveryID: deliveryID, attachmentKind: attachmentKind)
    }
    private init(coreID: String, deviceID: String, conversationID: String, clientMessageID: String,
                 bodyDigest: String, deliveryID: String?, attachmentKind: String?) {
        self.coreID = coreID; self.deviceID = deviceID; self.conversationID = conversationID
        self.clientMessageID = clientMessageID; self.bodyDigest = bodyDigest
        self.deliveryID = deliveryID; self.attachmentKind = attachmentKind
    }
}

public struct AppConversationMessageChallenge: Codable, Sendable {
    public let nonce, request_digest: String
    public let expires_at: Double
    public let binding_b64: String
    public init(nonce: String, requestDigest: String, expiresAt: Double, bindingB64: String) {
        self.nonce = nonce; request_digest = requestDigest; expires_at = expiresAt; binding_b64 = bindingB64
    }

    public static let domain = Data("SOLVIO_APP_CONVERSATION_MESSAGE_V1".utf8)
    public static let bindingType = "app_conversation_message_binding"

    public func clientDataHash(body: AppConversationMessageBody, coreID: String, deviceID: String,
                               appAttestKeyID: String, now: Double = Date().timeIntervalSince1970) throws -> Data {
        guard expires_at.isFinite, now.isFinite, now < expires_at, expires_at <= now + 300 else {
            throw ConversationMessageError.expired
        }
        guard nonce.range(of: #"\A[a-f0-9]{64}\z"#, options: .regularExpression) != nil,
              request_digest == body.requestDigest,
              let raw = Data(base64Encoded: binding_b64), raw.count <= 4096 else {
            throw ConversationMessageError.invalidChallenge
        }
        let binding = try ApprovalProtocol.strictParse(raw)
        let keys: Set<String> = ["protocol_version", "type", "core_instance_id", "principal_id", "device_id",
            "nonce", "request_digest", "enrollment_id", "app_attest_key_id", "approval_key_sha256"]
        guard Set(binding.keys) == keys, binding["protocol_version"] == .number(1),
              binding["type"] == .string(Self.bindingType),

              binding["core_instance_id"] == .string(coreID), binding["device_id"] == .string(deviceID),
              binding["nonce"] == .string(nonce), binding["request_digest"] == .string(body.requestDigest),
              binding["app_attest_key_id"] == .string(appAttestKeyID),
              case let .string(principal) = binding["principal_id"], !principal.isEmpty,
              case let .string(enrollment) = binding["enrollment_id"], !enrollment.isEmpty,
              case let .string(fingerprint) = binding["approval_key_sha256"],
              fingerprint.range(of: #"\A[a-f0-9]{64}\z"#, options: .regularExpression) != nil else {
            throw ConversationMessageError.invalidChallenge
        }
        // Die EXAKTEN Server-Bytes werden gehasht, nie eine Neuserialisierung.
        return AppAttestBinding.domainHash(Self.domain, raw)
    }
}

/// 202 bestätigt dieselbe Zustellung. Ein Replay kann bereits running,
/// completed oder blocked sein; das bestätigt keinen erfolgreichen Auftrag.
public struct AppConversationMessageAccepted: Codable, Equatable, Sendable {
    public let delivery_id, status, message_id: String
    public init(deliveryID: String, status: String, messageID: String) {
        delivery_id = deliveryID; self.status = status; message_id = messageID
    }
    public static func response(data: Data, status: Int) throws -> AppConversationMessageAccepted {
        guard status == 202, let result = try? JSONDecoder().decode(Self.self, from: data),
              deliveryIdentifier(result.delivery_id), messageIdentifier(result.message_id),
              ["accepted", "running", "completed", "blocked"].contains(result.status) else {
            throw ConversationMessageError.invalidResponse
        }
        return result
    }
}

/// Challenge → Assertion → Submit, mit `alreadySending`-Sperre. Netz und der
/// echte App-Attest-Signierer bleiben im App-Client; hier ist alles hosttestbar.
@MainActor
public final class ConversationMessageAttempt {
    public private(set) var isSending = false
    public init() {}
    public func send(body: AppConversationMessageBody, coreID: String, deviceID: String, appAttestKeyID: String,
                     challenge: (AppConversationMessageBody) async throws -> AppConversationMessageChallenge,
                     sign: (Data) async throws -> Data,
                     submit: (AppConversationMessageBody, AppTaskProof) async throws -> AppConversationMessageAccepted,
                     now: () -> Double = { Date().timeIntervalSince1970 }) async throws -> AppConversationMessageAccepted {
        guard !isSending else { throw ConversationMessageError.alreadySending }
        isSending = true
        defer { isSending = false }
        let issued = try await challenge(body)
        let hash = try issued.clientDataHash(body: body, coreID: coreID, deviceID: deviceID,
                                             appAttestKeyID: appAttestKeyID, now: now())
        let assertion = try await sign(hash)
        guard !assertion.isEmpty, now() < issued.expires_at else { throw ConversationMessageError.expired }
        return try await submit(body, AppTaskProof(nonce: issued.nonce, assertion: assertion))
    }
}
