// DEBT-0261: a deliberate authenticated-app cost cap, not a purchase approval.
import Foundation

private func costHex(_ value: String) -> Bool {
    value.range(of: #"\A[a-f0-9]{64}\z"#, options: .regularExpression) != nil
}

public struct AppTaskCostApprovalBody: Codable, Equatable, Sendable {
    public static let maxCents = 1_000_000_000
    public let task_id: String
    public let max_total_cents: Int
    public let client_request_id: String
    enum CodingKeys: String, CodingKey { case task_id, max_total_cents, client_request_id }
    public init(taskID: String, maxTotalCents: Int, requestID: String) throws {
        guard taskID.range(of: #"\Aat-[a-f0-9]{16}\z"#, options: .regularExpression) != nil,
              (0...Self.maxCents).contains(maxTotalCents),
              requestID.range(of: #"\A[A-Za-z0-9][A-Za-z0-9._:-]{7,127}\z"#, options: .regularExpression) != nil else {
            throw TaskStartError.invalidBody
        }
        task_id = taskID; max_total_cents = maxTotalCents; client_request_id = requestID
    }
    public init(from decoder: Decoder) throws {
        try requireTaskKeys(decoder, required: ["task_id", "max_total_cents", "client_request_id"])
        let fields = try decoder.container(keyedBy: CodingKeys.self)
        try self.init(taskID: fields.decode(String.self, forKey: .task_id),
            maxTotalCents: fields.decode(Int.self, forKey: .max_total_cents),
            requestID: fields.decode(String.self, forKey: .client_request_id))
    }
    public func canonicalBytes() -> Data {
        Data(TaskCanonicalValue.object(["task_id": .string(task_id),
            "max_total_cents": .number(max_total_cents), "client_request_id": .string(client_request_id)]).encoded.utf8)
    }
    public var requestDigest: String { ApprovalCrypto.sha256Hex(canonicalBytes()) }
}

/// Euro input is parsed as digits and cents, never rounded through Double.
public enum TaskCostAmount {
    public static func cents(_ input: String) -> Int? {
        guard input.range(of: #"\A[0-9]{1,8}(?:[.,][0-9]{1,2})?\z"#, options: .regularExpression) != nil else { return nil }
        let parts = input.replacingOccurrences(of: ",", with: ".").split(separator: ".")
        guard let euros = Int(parts[0]) else { return nil }
        let fraction = parts.count == 2 ? String(parts[1]) : ""
        let cents = euros * 100 + (Int(fraction.padding(toLength: 2, withPad: "0", startingAt: 0)) ?? 0)
        return (0...AppTaskCostApprovalBody.maxCents).contains(cents) ? cents : nil
    }
    public static func text(_ cents: Int) -> String { "\(cents / 100)," + String(format: "%02d", cents % 100) }
}

/// Retry metadata in the existing Keychain; no cached nonce, token or cost ledger.
public struct TaskCostApprovalRetryBinding: Codable, Equatable, Sendable {
    public let coreID, deviceID: String
    public let body: AppTaskCostApprovalBody
    public let bodyDigest: String
    public init(body: AppTaskCostApprovalBody, coreID: String, deviceID: String) {
        self.body = body; self.coreID = coreID; self.deviceID = deviceID; bodyDigest = body.requestDigest
    }
    public func restore(taskID: String, coreID: String, deviceID: String) -> AppTaskCostApprovalBody? {
        guard self.coreID == coreID, self.deviceID == deviceID, body.task_id == taskID,
              body.requestDigest == bodyDigest else { return nil }
        return body
    }
}

public struct AppTaskCostApprovalChallenge: Decodable, Sendable {
    public let nonce, request_digest: String
    public let expires_at: Double
    public let binding_b64: String
    public init(nonce: String, requestDigest: String, expiresAt: Double, bindingB64: String) {
        self.nonce = nonce; request_digest = requestDigest; expires_at = expiresAt; binding_b64 = bindingB64
    }
    public func clientDataHash(body: AppTaskCostApprovalBody, coreID: String, deviceID: String,
                               appAttestKeyID: String, now: Double = Date().timeIntervalSince1970) throws -> Data {
        guard expires_at.isFinite, now.isFinite, now < expires_at, expires_at <= now + 300 else { throw TaskStartError.expired }
        guard costHex(nonce), request_digest == body.requestDigest,
              let raw = Data(base64Encoded: binding_b64), raw.count <= 4096 else { throw TaskStartError.invalidChallenge }
        let binding = try ApprovalProtocol.strictParse(raw)
        let keys: Set<String> = ["protocol_version", "type", "core_instance_id", "principal_id", "device_id",
            "nonce", "request_digest", "enrollment_id", "app_attest_key_id", "approval_key_sha256"]
        guard Set(binding.keys) == keys, binding["protocol_version"] == .number(1),
              binding["type"] == .string("app_task_cost_approval_binding"),
              binding["core_instance_id"] == .string(coreID), binding["device_id"] == .string(deviceID),
              binding["nonce"] == .string(nonce), binding["request_digest"] == .string(body.requestDigest),
              binding["app_attest_key_id"] == .string(appAttestKeyID),
              case let .string(principal) = binding["principal_id"], !principal.isEmpty,
              case let .string(enrollment) = binding["enrollment_id"], !enrollment.isEmpty,
              case let .string(fingerprint) = binding["approval_key_sha256"], costHex(fingerprint) else {
            throw TaskStartError.invalidChallenge
        }
        return AppAttestBinding.domainHash(Data("SOLVIO_APP_TASK_COST_APPROVAL_V1".utf8), raw)
    }
}

public struct AppTaskCostApprovalAccepted: Decodable, Sendable {
    public let task_id, client_request_id: String
    public let max_total_cents: Int
    public let costs: AgentRun.Costs
    enum CodingKeys: String, CodingKey { case task_id, client_request_id, max_total_cents, costs }
    public init(from decoder: Decoder) throws {
        try requireTaskKeys(decoder, required: ["task_id", "client_request_id", "max_total_cents", "costs"])
        let fields = try decoder.container(keyedBy: CodingKeys.self)
        task_id = try fields.decode(String.self, forKey: .task_id)
        client_request_id = try fields.decode(String.self, forKey: .client_request_id)
        max_total_cents = try fields.decode(Int.self, forKey: .max_total_cents)
        costs = try fields.decode(AgentRun.Costs.self, forKey: .costs)
    }
    public func validate(body: AppTaskCostApprovalBody) throws {
        guard task_id == body.task_id, client_request_id == body.client_request_id,
              max_total_cents == body.max_total_cents, costs.configured == true,
              costs.task_id == body.task_id, costs.currency == "EUR",
              let approved = costs.approved_ai_cap_cents,
              (body.max_total_cents...AppTaskCostApprovalBody.maxCents).contains(approved) else { throw TaskStartError.invalidChallenge }
    }
}

@MainActor
public final class AppTaskCostApprovalAttempt {
    public private(set) var isSending = false
    public init() {}
    public func send(body: AppTaskCostApprovalBody, coreID: String, deviceID: String, appAttestKeyID: String,
        challenge: (AppTaskCostApprovalBody) async throws -> AppTaskCostApprovalChallenge,
        sign: (Data) async throws -> Data,
        submit: (AppTaskCostApprovalBody, AppTaskProof) async throws -> AppTaskCostApprovalAccepted,
        isCurrent: () -> Bool = { true }, now: () -> Double = { Date().timeIntervalSince1970 }) async throws -> AppTaskCostApprovalAccepted {
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
        try accepted.validate(body: body)
        return accepted
    }
}
