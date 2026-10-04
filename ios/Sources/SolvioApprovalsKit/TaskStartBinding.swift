// One authenticated app task, using the enrolled App Attest key. No Face ID.
import Foundation

public enum TaskStartError: Error, Equatable, LocalizedError {
    case invalidBody, invalidChallenge, expired, alreadySending
    public var errorDescription: String? {
        switch self {
        case .invalidBody: return "Beschreibe den Auftrag mit 12 bis 2000 Zeichen."
        case .invalidChallenge: return "Die Antwort passt nicht zu diesem Auftrag und Gerät."
        case .expired: return "Die Anfrage ist abgelaufen. Bitte erneut versuchen."
        case .alreadySending: return "Der Auftrag wird bereits gesendet."
        }
    }
}

public struct AppTaskBody: Codable, Equatable, Sendable {
    public let scope, objective, target_repo, client_request_id: String
    public let action_request: AppTaskActionRequest?
    public let document_request: AppTaskDocumentRequest?
    public let file_request: AppTaskFileRequest?
    public let action_intent: AppTaskActionIntent?
    /// C3: der Chat, in dem dieser Auftrag entsteht. Nie leer, wenn gesetzt;
    /// abwesend bleibt der Body byteidentisch zu jedem Body vor C3.
    public let conversation_ref: String?
    enum CodingKeys: String, CodingKey { case scope, objective, target_repo, client_request_id, action_request, document_request, file_request, action_intent, conversation_ref }

    public init(from decoder: Decoder) throws {
        try requireTaskKeys(decoder, required: ["scope", "objective", "target_repo", "client_request_id"], optional: ["action_request", "document_request", "file_request", "action_intent", "conversation_ref"])
        let fields = try decoder.container(keyedBy: CodingKeys.self)
        if fields.contains(.action_request), try fields.decodeNil(forKey: .action_request) { throw TaskStartError.invalidBody }
        if fields.contains(.document_request), try fields.decodeNil(forKey: .document_request) { throw TaskStartError.invalidBody }
        if fields.contains(.file_request), try fields.decodeNil(forKey: .file_request) { throw TaskStartError.invalidBody }
        if fields.contains(.action_intent), try fields.decodeNil(forKey: .action_intent) { throw TaskStartError.invalidBody }
        if fields.contains(.conversation_ref), try fields.decodeNil(forKey: .conversation_ref) { throw TaskStartError.invalidBody }
        try self.init(scope: fields.decode(String.self, forKey: .scope), objective: fields.decode(String.self, forKey: .objective),
            targetRepo: fields.decode(String.self, forKey: .target_repo), requestID: fields.decode(String.self, forKey: .client_request_id),
            actionRequest: fields.decodeIfPresent(AppTaskActionRequest.self, forKey: .action_request),
            documentRequest: fields.decodeIfPresent(AppTaskDocumentRequest.self, forKey: .document_request),
            fileRequest: fields.decodeIfPresent(AppTaskFileRequest.self, forKey: .file_request),
            actionIntent: fields.decodeIfPresent(AppTaskActionIntent.self, forKey: .action_intent),
            conversationRef: fields.decodeIfPresent(String.self, forKey: .conversation_ref))
    }

    public init(scope: String, objective: String, targetRepo: String, requestID: String,
                actionRequest: AppTaskActionRequest? = nil, documentRequest: AppTaskDocumentRequest? = nil, fileRequest: AppTaskFileRequest? = nil,
                actionIntent: AppTaskActionIntent? = nil, conversationRef: String? = nil) throws {
        let length = objective.unicodeScalars.count // Python len(), including emoji/combining marks
        // Ein gesetzter Ref muss die Chatkennung des Cores sein (c- + 16 hex);
        // eine leere Zeichenkette ist kein Ref und wird wie abwesend behandelt.
        if let conversationRef, !conversationRef.isEmpty, !conversationIdentifier(conversationRef) { throw TaskStartError.invalidBody }
        guard ["research", "build", "action"].contains(scope), (12...2000).contains(length),
              objective == objective.trimmingCharacters(in: .whitespacesAndNewlines),
              !objective.contains("\0"), targetRepo.unicodeScalars.count <= 4096,
              targetRepo == targetRepo.trimmingCharacters(in: .whitespacesAndNewlines),
              !targetRepo.contains("\0"), scope == "build" || targetRepo.isEmpty,
              (scope == "action") == (actionRequest != nil || actionIntent != nil),
              actionRequest == nil || actionIntent == nil,
              documentRequest == nil || scope == "research",
              fileRequest == nil || (scope == "research" && documentRequest == nil),
              requestID.range(of: #"\A[A-Za-z0-9][A-Za-z0-9._:-]{7,127}\z"#,
                              options: .regularExpression) != nil else { throw TaskStartError.invalidBody }
        self.scope = scope; self.objective = objective; target_repo = targetRepo
        client_request_id = requestID
        try actionRequest?.validate()
        action_request = actionRequest
        document_request = documentRequest
        file_request = fileRequest
        action_intent = actionIntent
        conversation_ref = (conversationRef?.isEmpty == false) ? conversationRef : nil
    }

    public func canonicalBytes() -> Data {
        var fields: [String: TaskCanonicalValue] = [
            "scope": .string(scope), "objective": .string(objective),
            "target_repo": .string(target_repo), "client_request_id": .string(client_request_id)]
        if let action_request { fields["action_request"] = action_request.json }
        if let document_request { fields["document_request"] = document_request.json }
        if let file_request { fields["file_request"] = file_request.json }
        if let action_intent { fields["action_intent"] = action_intent.json }
        if let conversation_ref, !conversation_ref.isEmpty { fields["conversation_ref"] = .string(conversation_ref) }
        return Data(TaskCanonicalValue.object(fields).encoded.utf8)
    }
    public var requestDigest: String { ApprovalCrypto.sha256Hex(canonicalBytes()) }
}

/// A retry reuses this exact task. Editing invalidates the ID; a nonce is never cached.
public struct TaskStartDraft: Sendable {
    public private(set) var body: AppTaskBody?
    public init() {}
    public mutating func invalidate() { body = nil }
    public mutating func prepare(scope: String, objective: String, targetRepo: String,
                                 retry: TaskStartRetryBinding? = nil, coreID: String = "",
                                 deviceID: String = "", actionRequest: AppTaskActionRequest? = nil,
                                 documentRequest: AppTaskDocumentRequest? = nil, fileRequest: AppTaskFileRequest? = nil,
                                 actionIntent: AppTaskActionIntent? = nil, conversationRef: String? = nil) throws -> AppTaskBody {
        try actionRequest?.validate()
        let ref = (conversationRef?.isEmpty == false) ? conversationRef : nil
        if let body, body.scope == scope, body.objective == objective, body.target_repo == targetRepo,
           body.action_request == actionRequest, body.document_request == documentRequest, body.file_request == fileRequest,
           body.action_intent == actionIntent, body.conversation_ref == ref {
            return body
        }
        let restored = retry?.restore(scope: scope, objective: objective, targetRepo: targetRepo,
                                      coreID: coreID, deviceID: deviceID, actionRequest: actionRequest,
                                      documentRequest: documentRequest, fileRequest: fileRequest, actionIntent: actionIntent,
                                      conversationRef: ref)
        let next = try restored ?? AppTaskBody(scope: scope, objective: objective, targetRepo: targetRepo,
                                               requestID: UUID().uuidString.lowercased(), actionRequest: actionRequest,
                                               documentRequest: documentRequest, fileRequest: fileRequest, actionIntent: actionIntent,
                                               conversationRef: ref)
        body = next
        return next
    }
}

/// Transport retry metadata only: no objective, repository, task result, credential,
/// approval or body. The Core remains the sole task record. Persist before dispatch.
public struct TaskStartRetryBinding: Codable, Equatable, Sendable {
    public let coreID, deviceID, requestID, bodyDigest: String
    /// Describes missing input after restart, never stores names or contents.
    public let attachmentKind: String?
    public init(body: AppTaskBody, coreID: String, deviceID: String) {
        self.coreID = coreID; self.deviceID = deviceID
        requestID = body.client_request_id; bodyDigest = body.requestDigest
        attachmentKind = body.file_request != nil ? "files" : body.document_request != nil ? "document" : nil
    }
    public func restore(scope: String, objective: String, targetRepo: String,
                         coreID: String, deviceID: String, actionRequest: AppTaskActionRequest? = nil,
                         documentRequest: AppTaskDocumentRequest? = nil, fileRequest: AppTaskFileRequest? = nil,
                         actionIntent: AppTaskActionIntent? = nil, conversationRef: String? = nil) -> AppTaskBody? {
        // Der Digest deckt auch den Chatbezug: ein anderer Chat ist eine andere Anfrage.
        guard self.coreID == coreID, self.deviceID == deviceID,
              let body = try? AppTaskBody(scope: scope, objective: objective, targetRepo: targetRepo,
                                          requestID: requestID, actionRequest: actionRequest, documentRequest: documentRequest, fileRequest: fileRequest,
                                          actionIntent: actionIntent, conversationRef: conversationRef), body.requestDigest == bodyDigest else { return nil }
        return body
    }
}

public struct AppTaskChallenge: Codable, Sendable {
    public let nonce, request_digest: String
    public let expires_at: Double
    public let binding_b64: String
    public init(nonce: String, requestDigest: String, expiresAt: Double, bindingB64: String) {
        self.nonce = nonce; request_digest = requestDigest; expires_at = expiresAt
        binding_b64 = bindingB64
    }

    public func clientDataHash(body: AppTaskBody, coreID: String, deviceID: String,
                               appAttestKeyID: String, now: Double = Date().timeIntervalSince1970) throws -> Data {
        guard expires_at.isFinite, now < expires_at, expires_at <= now + 300 else {
            throw TaskStartError.expired
        }
        guard nonce.range(of: #"\A[a-f0-9]{64}\z"#, options: .regularExpression) != nil,
              request_digest == body.requestDigest,
              let raw = Data(base64Encoded: binding_b64), raw.count <= 4096 else {
            throw TaskStartError.invalidChallenge
        }
        let binding = try ApprovalProtocol.strictParse(raw)
        let fields: Set<String> = ["protocol_version", "type", "core_instance_id", "principal_id",
            "device_id", "nonce", "request_digest", "enrollment_id", "app_attest_key_id", "approval_key_sha256"]
        guard Set(binding.keys) == fields, binding["protocol_version"] == .number(1),
              binding["type"] == .string("app_task_start_binding"),
              binding["core_instance_id"] == .string(coreID), binding["device_id"] == .string(deviceID),
              binding["nonce"] == .string(nonce), binding["request_digest"] == .string(body.requestDigest),
              binding["app_attest_key_id"] == .string(appAttestKeyID),
              case let .string(principal) = binding["principal_id"], !principal.isEmpty,
              case let .string(enrollment) = binding["enrollment_id"], !enrollment.isEmpty,
              case let .string(fingerprint) = binding["approval_key_sha256"],
              fingerprint.range(of: #"\A[a-f0-9]{64}\z"#, options: .regularExpression) != nil else {
            throw TaskStartError.invalidChallenge
        }
        // Hash the EXACT server bytes after validating their binding, never reserialize.
        return AppAttestBinding.domainHash(Data("SOLVIO_APP_TASK_START_V1".utf8), raw)
    }
}

public struct AppTaskProof: Codable, Sendable {
    public let nonce, assertion_b64: String
    public init(nonce: String, assertion: Data) {
        self.nonce = nonce; assertion_b64 = assertion.base64EncodedString()
    }
}

public struct AppTaskAccepted: Codable, Equatable, Sendable {
    public let task_id, run_id, zustand: String
    public let annahme: String?
    public init(taskID: String, runID: String, state: String, acceptance: String? = nil) {
        task_id = taskID; run_id = runID; zustand = state; annahme = acceptance
    }
    public var isPreparing: Bool { annahme == "preparing" }
    public static func response(data: Data, status: Int) throws -> AppTaskAccepted {
        guard [201, 202].contains(status) else { throw TaskStartError.invalidChallenge }
        let result = try JSONDecoder().decode(Self.self, from: data)
        guard !result.task_id.isEmpty, !result.run_id.isEmpty, !result.zustand.isEmpty,
              (status == 202) == result.isPreparing else { throw TaskStartError.invalidChallenge }
        return result
    }
}

/// The shared flow is host-testable. Network/TLS and the real App Attest signer
/// stay in the existing app client. No approval-key signer is part of this API.
@MainActor
public final class TaskStartAttempt {
    public private(set) var isSending = false
    public init() {}
    public func send(body: AppTaskBody, coreID: String, deviceID: String, appAttestKeyID: String,
        challenge: (AppTaskBody) async throws -> AppTaskChallenge,
        sign: (Data) async throws -> Data,
        submit: (AppTaskBody, AppTaskProof) async throws -> AppTaskAccepted,
        now: () -> Double = { Date().timeIntervalSince1970 }) async throws -> AppTaskAccepted {
        guard !isSending else { throw TaskStartError.alreadySending }
        isSending = true
        defer { isSending = false }
        let issued = try await challenge(body)
        let hash = try issued.clientDataHash(body: body, coreID: coreID, deviceID: deviceID,
                                           appAttestKeyID: appAttestKeyID, now: now())
        let assertion = try await sign(hash)
        guard !assertion.isEmpty, now() < issued.expires_at else { throw TaskStartError.expired }
        return try await submit(body, AppTaskProof(nonce: issued.nonce, assertion: assertion))
    }
}
