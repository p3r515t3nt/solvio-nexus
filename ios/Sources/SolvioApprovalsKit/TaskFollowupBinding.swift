import Foundation

private func followupHex(_ value: String) -> Bool {
    value.range(of: #"\A[a-f0-9]{64}\z"#, options: .regularExpression) != nil
}
private func followupRun(_ value: String) -> Bool {
    value.range(of: #"\Aar-[a-f0-9]{16}\z"#, options: .regularExpression) != nil
}

/// This snapshot is a Core projection, not a second task/revision store.
public struct TaskRevisionSnapshot: Decodable, Equatable, Sendable {
    public let revision: Int
    public let digest, text, parent_run_id: String
    enum CodingKeys: String, CodingKey { case revision, digest, text, parent_run_id }
    public init(revision: Int, digest: String, text: String, parentRunID: String = "") throws {
        guard (1...100).contains(revision), followupHex(digest), !text.isEmpty,
              text.unicodeScalars.count <= 2000, !text.contains("\0"),
              (revision == 1 && parentRunID.isEmpty) || followupRun(parentRunID) else { throw TaskStartError.invalidBody }
        self.revision = revision; self.digest = digest; self.text = text; parent_run_id = parentRunID
    }
    public init(from decoder: Decoder) throws {
        let fields = try decoder.container(keyedBy: CodingKeys.self)
        try self.init(revision: fields.decode(Int.self, forKey: .revision), digest: fields.decode(String.self, forKey: .digest),
            text: fields.decode(String.self, forKey: .text), parentRunID: fields.decode(String.self, forKey: .parent_run_id))
    }
    public var bindingID: String { "\(revision):\(digest)" }
}

public struct TaskHistoryEntry: Decodable, Identifiable, Sendable {
    public let run_id: String
    public let revision: Int
    public let text, state, result_summary: String
    public var id: String { run_id }
}
public struct TaskFollowupAvailability: Decodable, Sendable {
    public let eligible: Bool
    public let reason: String
}

public struct AppTaskFollowupBody: Codable, Equatable, Sendable {
    public let run_id, text: String
    public let expected_revision: Int
    public let expected_digest: String
    public let input_artifact_ids: [String]
    public let client_request_id: String
    enum CodingKeys: String, CodingKey { case run_id, text, expected_revision, expected_digest, input_artifact_ids, client_request_id }
    public init(runID: String, text: String, revision: Int, digest: String, inputIDs: [String], requestID: String) throws {
        guard followupRun(runID), (1...99).contains(revision), followupHex(digest),
              (1...2000).contains(text.unicodeScalars.count), text == text.trimmingCharacters(in: .whitespacesAndNewlines),
              !text.unicodeScalars.contains(where: { ($0.value < 32 && $0.value != 9 && $0.value != 10) || $0.value == 127 }),
              inputIDs.count <= 4, Set(inputIDs).count == inputIDs.count,
              inputIDs.allSatisfy({ $0.range(of: #"\Aaa-[a-f0-9]{16}\z"#, options: .regularExpression) != nil }),
              requestID.range(of: #"\A[A-Za-z0-9][A-Za-z0-9._:-]{7,127}\z"#, options: .regularExpression) != nil else {
            throw TaskStartError.invalidBody
        }
        run_id = runID; self.text = text; expected_revision = revision; expected_digest = digest
        input_artifact_ids = inputIDs; client_request_id = requestID
    }
    public init(from decoder: Decoder) throws {
        try requireTaskKeys(decoder, required: ["run_id", "text", "expected_revision", "expected_digest", "input_artifact_ids", "client_request_id"])
        let fields = try decoder.container(keyedBy: CodingKeys.self)
        try self.init(runID: fields.decode(String.self, forKey: .run_id), text: fields.decode(String.self, forKey: .text),
            revision: fields.decode(Int.self, forKey: .expected_revision), digest: fields.decode(String.self, forKey: .expected_digest),
            inputIDs: fields.decode([String].self, forKey: .input_artifact_ids), requestID: fields.decode(String.self, forKey: .client_request_id))
    }
    public func canonicalBytes() -> Data {
        Data(TaskCanonicalValue.object(["run_id": .string(run_id), "text": .string(text),
            "expected_revision": .number(expected_revision), "expected_digest": .string(expected_digest),
            "input_artifact_ids": .array(input_artifact_ids.map(TaskCanonicalValue.string)),
            "client_request_id": .string(client_request_id)]).encoded.utf8)
    }
    public var requestDigest: String { ApprovalCrypto.sha256Hex(canonicalBytes()) }
}

/// Retry metadata stores only identifiers/digests; the Owner re-enters the text.
public struct TaskFollowupRetryBinding: Codable, Equatable, Sendable {
    public let coreID, deviceID, runID, requestID, bodyDigest: String
    public let revision: Int
    public let revisionDigest: String
    public let inputIDs: [String]
    public init(body: AppTaskFollowupBody, coreID: String, deviceID: String) {
        self.coreID = coreID; self.deviceID = deviceID; runID = body.run_id
        requestID = body.client_request_id; bodyDigest = body.requestDigest
        revision = body.expected_revision; revisionDigest = body.expected_digest; inputIDs = body.input_artifact_ids
    }
    public func restore(runID: String, text: String, revision: Int, digest: String, inputIDs: [String],
                        coreID: String, deviceID: String) -> AppTaskFollowupBody? {
        guard self.coreID == coreID, self.deviceID == deviceID, self.runID == runID,
              self.revision == revision, revisionDigest == digest, self.inputIDs == inputIDs,
              let body = try? AppTaskFollowupBody(runID: runID, text: text, revision: revision,
                digest: digest, inputIDs: inputIDs, requestID: requestID), body.requestDigest == bodyDigest else { return nil }
        return body
    }
}

public struct AppTaskFollowupChallenge: Decodable, Sendable {
    public let nonce, request_digest: String
    public let expires_at: Double
    public let binding_b64: String
    public init(nonce: String, requestDigest: String, expiresAt: Double, bindingB64: String) {
        self.nonce = nonce; request_digest = requestDigest; expires_at = expiresAt; binding_b64 = bindingB64
    }
    public func clientDataHash(body: AppTaskFollowupBody, coreID: String, deviceID: String,
                               appAttestKeyID: String, now: Double = Date().timeIntervalSince1970) throws -> Data {
        guard expires_at.isFinite, now.isFinite, now < expires_at, expires_at <= now + 300 else { throw TaskStartError.expired }
        guard followupHex(nonce), request_digest == body.requestDigest,
              let raw = Data(base64Encoded: binding_b64), raw.count <= 4096 else { throw TaskStartError.invalidChallenge }
        let binding = try ApprovalProtocol.strictParse(raw)
        let keys: Set<String> = ["protocol_version", "type", "core_instance_id", "principal_id", "device_id",
            "nonce", "request_digest", "enrollment_id", "app_attest_key_id", "approval_key_sha256"]
        guard Set(binding.keys) == keys, binding["protocol_version"] == .number(1),
              binding["type"] == .string("app_task_followup_binding"),
              binding["core_instance_id"] == .string(coreID), binding["device_id"] == .string(deviceID),
              binding["nonce"] == .string(nonce), binding["request_digest"] == .string(body.requestDigest),
              binding["app_attest_key_id"] == .string(appAttestKeyID),
              case let .string(principal) = binding["principal_id"], !principal.isEmpty,
              case let .string(enrollment) = binding["enrollment_id"], !enrollment.isEmpty,
              case let .string(fingerprint) = binding["approval_key_sha256"], followupHex(fingerprint) else {
            throw TaskStartError.invalidChallenge
        }
        return AppAttestBinding.domainHash(Data("SOLVIO_APP_TASK_FOLLOWUP_V1".utf8), raw)
    }
}

public struct AppTaskFollowupAccepted: Decodable, Equatable, Sendable {
    public let task_id, run_id, parent_run_id: String
    public let revision: Int
    public let digest, annahme: String
    public init(taskID: String, runID: String, parentRunID: String, revision: Int, digest: String, admission: String) {
        task_id = taskID; run_id = runID; parent_run_id = parentRunID; self.revision = revision
        self.digest = digest; annahme = admission
    }
    public func validate(body: AppTaskFollowupBody, taskID: String) throws {
        guard task_id == taskID, taskID.range(of: #"\Aat-[a-f0-9]{16}\z"#, options: .regularExpression) != nil,
              followupRun(run_id), run_id != body.run_id, parent_run_id == body.run_id,
              revision == body.expected_revision + 1, followupHex(digest),
              ["preparing", "ready"].contains(annahme) else { throw TaskStartError.invalidChallenge }
    }
}

@MainActor
public final class AppTaskFollowupAttempt {
    public private(set) var isSending = false
    public init() {}
    public func send(body: AppTaskFollowupBody, taskID: String, coreID: String, deviceID: String, appAttestKeyID: String,
        challenge: (AppTaskFollowupBody) async throws -> AppTaskFollowupChallenge,
        sign: (Data) async throws -> Data,
        submit: (AppTaskFollowupBody, AppTaskProof) async throws -> AppTaskFollowupAccepted,
        isCurrent: () -> Bool = { true }, now: () -> Double = { Date().timeIntervalSince1970 }) async throws -> AppTaskFollowupAccepted {
        guard !isSending else { throw TaskStartError.alreadySending }
        guard isCurrent(), !Task.isCancelled else { throw CancellationError() }
        isSending = true; defer { isSending = false }
        let issued = try await challenge(body)
        guard isCurrent(), !Task.isCancelled else { throw CancellationError() }
        let hash = try issued.clientDataHash(body: body, coreID: coreID, deviceID: deviceID,
            appAttestKeyID: appAttestKeyID, now: now())
        let assertion = try await sign(hash)
        guard isCurrent(), !Task.isCancelled else { throw CancellationError() }
        guard !assertion.isEmpty, now() < issued.expires_at else { throw TaskStartError.expired }
        let accepted = try await submit(body, AppTaskProof(nonce: issued.nonce, assertion: assertion))
        try accepted.validate(body: body, taskID: taskID)
        return accepted
    }
}
