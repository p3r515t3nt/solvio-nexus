import Foundation

/// A read-only projection of the Core's interpretation. It is not a local task.
public struct ActionIntentSnapshot: Codable, Equatable, Sendable {
    public enum Status: String, Codable, Sendable {
        case interpreting, waitingUser = "waiting_user", resolved, failed
    }
    public let status: Status
    public let question: ActionIntentQuestion?
    public init(status: Status, question: ActionIntentQuestion? = nil) throws {
        guard (status == .waitingUser) == (question != nil) else { throw TaskStartError.invalidBody }
        self.status = status; self.question = question
    }
    enum CodingKeys: String, CodingKey { case status, question }
    public init(from decoder: Decoder) throws {
        let fields = try decoder.container(keyedBy: CodingKeys.self)
        try self.init(status: fields.decode(Status.self, forKey: .status),
                      question: fields.decodeIfPresent(ActionIntentQuestion.self, forKey: .question))
    }
}

public struct ActionIntentQuestion: Codable, Equatable, Sendable, Identifiable {
    public enum Field: String, Codable, Sendable { case instruction, title, when, time, duration, to, account }
    public enum InputType: String, Codable, Sendable { case text, email, number, select }
    public struct Option: Codable, Equatable, Sendable, Identifiable {
        public let value, label: String
        public var id: String { value }
        public init(value: String, label: String) { self.value = value; self.label = label }
    }
    public let id: String
    public let revision: Int
    public let digest: String
    public let field: Field
    public let prompt: String
    public let input_type: InputType
    public let placeholder: String?
    public let options: [Option]?
    /// Polling may update labels without discarding an answer being typed.
    /// A changed Core question/revision/digest is a new answer boundary.
    public var bindingID: String { "\(id):\(revision):\(digest)" }
    public var isAccountConnectionRequired: Bool {
        field == .account && input_type == .select && options?.isEmpty == true
    }
    enum CodingKeys: String, CodingKey { case id, revision, digest, field, prompt, input_type, placeholder, options }
    public init(id: String, revision: Int, digest: String, field: Field, prompt: String,
                inputType: InputType, placeholder: String? = nil, options: [Option]? = nil) throws {
        guard validQuestionID(id), (1...100).contains(revision), validAnswerDigest(digest), !prompt.isEmpty,
              !prompt.contains("\0") else { throw TaskStartError.invalidBody }
        if inputType == .select {
            guard let options, (!options.isEmpty || field == .account), Set(options.map(\.value)).count == options.count,
                  options.allSatisfy({ validAnswer($0.value) && !$0.label.isEmpty && !$0.label.contains("\0") }) else {
                throw TaskStartError.invalidBody
            }
        } else if let options, !options.isEmpty { throw TaskStartError.invalidBody }
        self.id = id; self.revision = revision; self.digest = digest; self.field = field
        self.prompt = prompt; input_type = inputType; self.placeholder = placeholder; self.options = options
    }
    public init(from decoder: Decoder) throws {
        let fields = try decoder.container(keyedBy: CodingKeys.self)
        try self.init(id: fields.decode(String.self, forKey: .id), revision: fields.decode(Int.self, forKey: .revision),
            digest: fields.decode(String.self, forKey: .digest), field: fields.decode(Field.self, forKey: .field),
            prompt: fields.decode(String.self, forKey: .prompt), inputType: fields.decode(InputType.self, forKey: .input_type),
            placeholder: fields.decodeIfPresent(String.self, forKey: .placeholder),
            options: fields.decodeIfPresent([Option].self, forKey: .options))
    }
}

private func validQuestionID(_ id: String) -> Bool {
    id.range(of: #"\Aaiq-[a-f0-9]{32}\z"#, options: .regularExpression) != nil
}
private func validAnswerDigest(_ digest: String) -> Bool {
    digest.range(of: #"\A[a-f0-9]{64}\z"#, options: .regularExpression) != nil
}
private func validAnswer(_ answer: String) -> Bool {
    (1...2000).contains(answer.unicodeScalars.count)
        && answer == answer.trimmingCharacters(in: .whitespacesAndNewlines)
        && !answer.unicodeScalars.contains { $0.value < 32 && $0.value != 9 && $0.value != 10 }
}

/// Signed answer to one immutable question revision of an existing run.
public struct AppTaskActionAnswerBody: Codable, Equatable, Sendable {
    public let run_id, question_id: String
    public let expected_revision: Int
    public let expected_digest, answer, client_request_id: String
    enum CodingKeys: String, CodingKey { case run_id, question_id, expected_revision, expected_digest, answer, client_request_id }

    public init(runID: String, questionID: String, expectedRevision: Int, expectedDigest: String,
                answer: String, requestID: String) throws {
        guard runID.range(of: #"\Aar-[a-f0-9]{16}\z"#, options: .regularExpression) != nil,
              validQuestionID(questionID), (1...100).contains(expectedRevision), validAnswerDigest(expectedDigest), validAnswer(answer),
              requestID.range(of: #"\A[A-Za-z0-9][A-Za-z0-9._:-]{7,127}\z"#, options: .regularExpression) != nil else {
            throw TaskStartError.invalidBody
        }
        run_id = runID; question_id = questionID; expected_revision = expectedRevision
        expected_digest = expectedDigest; self.answer = answer; client_request_id = requestID
    }
    public init(runID: String, question: ActionIntentQuestion, answer: String, requestID: String) throws {
        guard question.input_type != .select || question.options?.contains(where: { $0.value == answer }) == true else {
            throw TaskStartError.invalidBody
        }
        try self.init(runID: runID, questionID: question.id, expectedRevision: question.revision,
                      expectedDigest: question.digest, answer: answer, requestID: requestID)
    }
    public init(from decoder: Decoder) throws {
        try requireTaskKeys(decoder, required: ["run_id", "question_id", "expected_revision", "expected_digest", "answer", "client_request_id"])
        let fields = try decoder.container(keyedBy: CodingKeys.self)
        try self.init(runID: fields.decode(String.self, forKey: .run_id), questionID: fields.decode(String.self, forKey: .question_id),
            expectedRevision: fields.decode(Int.self, forKey: .expected_revision), expectedDigest: fields.decode(String.self, forKey: .expected_digest),
            answer: fields.decode(String.self, forKey: .answer), requestID: fields.decode(String.self, forKey: .client_request_id))
    }
    public func canonicalBytes() -> Data {
        Data(TaskCanonicalValue.object(["run_id": .string(run_id), "question_id": .string(question_id),
            "expected_revision": .number(expected_revision), "expected_digest": .string(expected_digest),
            "answer": .string(answer), "client_request_id": .string(client_request_id)]).encoded.utf8)
    }
    public var requestDigest: String { ApprovalCrypto.sha256Hex(canonicalBytes()) }
}

/// Retry identity only; no answer text, credentials, grant or question store.
public struct TaskActionAnswerRetryBinding: Codable, Equatable, Sendable {
    public let coreID, deviceID, requestID, bodyDigest: String
    public init(body: AppTaskActionAnswerBody, coreID: String, deviceID: String) {
        self.coreID = coreID; self.deviceID = deviceID; requestID = body.client_request_id; bodyDigest = body.requestDigest
    }
    public func restore(runID: String, question: ActionIntentQuestion, answer: String,
                        coreID: String, deviceID: String) -> AppTaskActionAnswerBody? {
        guard self.coreID == coreID, self.deviceID == deviceID,
              let body = try? AppTaskActionAnswerBody(runID: runID, question: question, answer: answer, requestID: requestID),
              body.requestDigest == bodyDigest else { return nil }
        return body
    }
}

public struct TaskActionAnswerDraft: Sendable {
    public private(set) var body: AppTaskActionAnswerBody?
    public init() {}
    public mutating func invalidate() { body = nil }
    public mutating func prepare(runID: String, question: ActionIntentQuestion, answer: String,
                                 retry: TaskActionAnswerRetryBinding? = nil, coreID: String = "",
                                 deviceID: String = "") throws -> AppTaskActionAnswerBody {
        if let body, let same = try? AppTaskActionAnswerBody(runID: runID, question: question,
            answer: answer, requestID: body.client_request_id), same == body { return body }
        let next = try retry?.restore(runID: runID, question: question, answer: answer, coreID: coreID, deviceID: deviceID)
            ?? AppTaskActionAnswerBody(runID: runID, question: question, answer: answer, requestID: UUID().uuidString.lowercased())
        body = next; return next
    }
}

public struct AppTaskActionAnswerChallenge: Codable, Sendable {
    public let nonce, request_digest: String
    public let expires_at: Double
    public let binding_b64: String
    public init(nonce: String, requestDigest: String, expiresAt: Double, bindingB64: String) {
        self.nonce = nonce; request_digest = requestDigest; expires_at = expiresAt; binding_b64 = bindingB64
    }
    public func clientDataHash(body: AppTaskActionAnswerBody, coreID: String, deviceID: String,
                               appAttestKeyID: String, now: Double = Date().timeIntervalSince1970) throws -> Data {
        guard expires_at.isFinite, now < expires_at, expires_at <= now + 300 else { throw TaskStartError.expired }
        guard nonce.range(of: #"\A[a-f0-9]{64}\z"#, options: .regularExpression) != nil,
              request_digest == body.requestDigest, let raw = Data(base64Encoded: binding_b64), raw.count <= 4096 else {
            throw TaskStartError.invalidChallenge
        }
        let binding = try ApprovalProtocol.strictParse(raw)
        let fields: Set<String> = ["protocol_version", "type", "core_instance_id", "principal_id", "device_id",
            "nonce", "request_digest", "enrollment_id", "app_attest_key_id", "approval_key_sha256"]
        guard Set(binding.keys) == fields, binding["protocol_version"] == .number(1),
              binding["type"] == .string("app_action_answer_binding"),
              binding["core_instance_id"] == .string(coreID), binding["device_id"] == .string(deviceID),
              binding["nonce"] == .string(nonce), binding["request_digest"] == .string(body.requestDigest),
              binding["app_attest_key_id"] == .string(appAttestKeyID),
              case let .string(principal) = binding["principal_id"], !principal.isEmpty,
              case let .string(enrollment) = binding["enrollment_id"], !enrollment.isEmpty,
              case let .string(fingerprint) = binding["approval_key_sha256"], validAnswerDigest(fingerprint) else {
            throw TaskStartError.invalidChallenge
        }
        return AppAttestBinding.domainHash(Data("SOLVIO_APP_ACTION_ANSWER_V1".utf8), raw)
    }
}

public struct AppTaskActionAnswerProof: Codable, Sendable {
    public let nonce, assertion_b64: String
    public init(nonce: String, assertion: Data) { self.nonce = nonce; assertion_b64 = assertion.base64EncodedString() }
}

public struct AppTaskActionAnswerAccepted: Codable, Equatable, Sendable {
    public let action_intent: ActionIntentSnapshot
    public init(actionIntent: ActionIntentSnapshot) { action_intent = actionIntent }
    public static func response(data: Data, status: Int) throws -> Self {
        guard status == 200 else { throw TaskStartError.invalidChallenge }
        return try JSONDecoder().decode(Self.self, from: data)
    }
}

@MainActor
public final class AppTaskActionAnswerAttempt {
    public private(set) var isSending = false
    public init() {}
    public func send(body: AppTaskActionAnswerBody, coreID: String, deviceID: String, appAttestKeyID: String,
        challenge: (AppTaskActionAnswerBody) async throws -> AppTaskActionAnswerChallenge,
        sign: (Data) async throws -> Data,
        submit: (AppTaskActionAnswerBody, AppTaskActionAnswerProof) async throws -> AppTaskActionAnswerAccepted,
        now: () -> Double = { Date().timeIntervalSince1970 }) async throws -> AppTaskActionAnswerAccepted {
        guard !isSending else { throw TaskStartError.alreadySending }
        isSending = true
        defer { isSending = false }
        let issued = try await challenge(body)
        let hash = try issued.clientDataHash(body: body, coreID: coreID, deviceID: deviceID,
                                           appAttestKeyID: appAttestKeyID, now: now())
        let assertion = try await sign(hash)
        guard !assertion.isEmpty, now() < issued.expires_at else { throw TaskStartError.expired }
        return try await submit(body, AppTaskActionAnswerProof(nonce: issued.nonce, assertion: assertion))
    }
}
