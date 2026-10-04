// SOLVIO Mobile Approval Protocol V2 — protocol.
//
// Mirrors the Python contract. STRICT parse: duplicate JSON keys and non-object roots are
// rejected (fail-closed). The receiver verifies the signature over the EXACT bytes it
// received and only then parses; it never re-serialises for verification. When the app
// SIGNS a decision it produces its own canonical bytes and signs exactly those.
//
// V2 (closes audit finding F1, human display integrity): the signed challenge carries the
// EXACT execution-determining `task` plus `device_id`, and the decision binds
// `challenge_payload_sha256` — the hash of the exact signed challenge bytes the phone
// verified and showed the human. `human_summary` stays an UNTRUSTED model-authored hint.
// V1 payloads are rejected (badVersion) — no silent downgrade.
import Foundation

public let PROTOCOL_VERSION = 2
public let DECISION_APPROVE = "APPROVE"
public let DECISION_DENY = "DENY"

public enum ProtocolError: Error, Equatable {
    case malformed(String)
    case duplicateKey(String)
    case notObject
    case missingField(String)
    case wrongType(String)
    case badVersion
    case badType
}

public indirect enum JSONValue: Equatable {
    case string(String)
    case number(Double)
    case bool(Bool)
    case null
    case array([JSONValue])
    case object([String: JSONValue])
}

// MARK: - strict recursive-descent parser (rejects duplicate keys)
struct StrictParser {
    private let b: [UInt8]
    private var i = 0
    init(_ data: Data) { b = [UInt8](data) }

    mutating func parseTopObject() throws -> [String: JSONValue] {
        skipWS()
        guard i < b.count, b[i] == 0x7b else { throw ProtocolError.notObject }
        let v = try parseValue()
        skipWS()
        guard i == b.count else { throw ProtocolError.malformed("trailing") }
        guard case let .object(o) = v else { throw ProtocolError.notObject }
        return o
    }

    private mutating func skipWS() {
        while i < b.count, b[i] == 0x20 || b[i] == 0x09 || b[i] == 0x0a || b[i] == 0x0d { i += 1 }
    }

    private mutating func parseValue() throws -> JSONValue {
        skipWS()
        guard i < b.count else { throw ProtocolError.malformed("eof") }
        switch b[i] {
        case 0x7b: return try parseObject()
        case 0x5b: return try parseArray()
        case 0x22: return .string(try parseString())
        case 0x74: try literal("true"); return .bool(true)
        case 0x66: try literal("false"); return .bool(false)
        case 0x6e: try literal("null"); return .null
        default: return .number(try parseNumber())
        }
    }

    private mutating func literal(_ w: String) throws {
        for c in w.utf8 { guard i < b.count, b[i] == c else { throw ProtocolError.malformed("literal") }; i += 1 }
    }

    private mutating func parseObject() throws -> JSONValue {
        i += 1
        var dict = [String: JSONValue]()
        var seen = Set<String>()
        skipWS()
        if i < b.count, b[i] == 0x7d { i += 1; return .object(dict) }
        while true {
            skipWS()
            guard i < b.count, b[i] == 0x22 else { throw ProtocolError.malformed("key") }
            let key = try parseString()
            if seen.contains(key) { throw ProtocolError.duplicateKey(key) }
            seen.insert(key)
            skipWS()
            guard i < b.count, b[i] == 0x3a else { throw ProtocolError.malformed("colon") }
            i += 1
            dict[key] = try parseValue()
            skipWS()
            guard i < b.count else { throw ProtocolError.malformed("eof") }
            if b[i] == 0x2c { i += 1; continue }
            if b[i] == 0x7d { i += 1; break }
            throw ProtocolError.malformed("object separator")
        }
        return .object(dict)
    }

    private mutating func parseArray() throws -> JSONValue {
        i += 1
        var arr = [JSONValue]()
        skipWS()
        if i < b.count, b[i] == 0x5d { i += 1; return .array(arr) }
        while true {
            arr.append(try parseValue())
            skipWS()
            guard i < b.count else { throw ProtocolError.malformed("eof") }
            if b[i] == 0x2c { i += 1; continue }
            if b[i] == 0x5d { i += 1; break }
            throw ProtocolError.malformed("array separator")
        }
        return .array(arr)
    }

    private mutating func parseString() throws -> String {
        i += 1 // opening quote
        var out = [UInt8]()
        while i < b.count {
            let c = b[i]; i += 1
            if c == 0x22 { return String(decoding: out, as: UTF8.self) }
            if c == 0x5c {
                guard i < b.count else { throw ProtocolError.malformed("escape") }
                let e = b[i]; i += 1
                switch e {
                case 0x22: out.append(0x22)
                case 0x5c: out.append(0x5c)
                case 0x2f: out.append(0x2f)
                case 0x62: out.append(0x08)
                case 0x66: out.append(0x0c)
                case 0x6e: out.append(0x0a)
                case 0x72: out.append(0x0d)
                case 0x74: out.append(0x09)
                case 0x75:
                    let cp = try readHex4()
                    var scalar = UInt32(cp)
                    if cp >= 0xD800 && cp <= 0xDBFF {   // surrogate pair
                        guard i + 1 < b.count, b[i] == 0x5c, b[i + 1] == 0x75 else {
                            throw ProtocolError.malformed("surrogate")
                        }
                        i += 2
                        let lo = try readHex4()
                        scalar = 0x10000 + (UInt32(cp - 0xD800) << 10) + UInt32(lo - 0xDC00)
                    }
                    guard let u = Unicode.Scalar(scalar) else { throw ProtocolError.malformed("scalar") }
                    out.append(contentsOf: Array(String(u).utf8))
                default: throw ProtocolError.malformed("bad escape")
                }
            } else {
                out.append(c)
            }
        }
        throw ProtocolError.malformed("unterminated string")
    }

    private mutating func readHex4() throws -> UInt16 {
        var v: UInt16 = 0
        for _ in 0..<4 {
            guard i < b.count else { throw ProtocolError.malformed("hex") }
            let c = b[i]; i += 1
            let d: UInt16
            switch c {
            case 0x30...0x39: d = UInt16(c - 0x30)
            case 0x61...0x66: d = UInt16(c - 0x61 + 10)
            case 0x41...0x46: d = UInt16(c - 0x41 + 10)
            default: throw ProtocolError.malformed("hex digit")
            }
            v = v << 4 | d
        }
        return v
    }

    private mutating func parseNumber() throws -> Double {
        let start = i
        while i < b.count, "-+.eE0123456789".utf8.contains(b[i]) { i += 1 }
        guard let d = Double(String(decoding: b[start..<i], as: UTF8.self)) else {
            throw ProtocolError.malformed("number")
        }
        return d
    }
}

public enum ApprovalProtocol {
    public static func strictParse(_ data: Data) throws -> [String: JSONValue] {
        var p = StrictParser(data)
        return try p.parseTopObject()
    }

    /// Mirror of the Core display-safety policy (protocol.py `validate_display_text`).
    /// Rejects NUL/C0 (except TAB, LF), DEL/C1, and Unicode bidi controls that can visually
    /// reorder an approval screen. Ordinary international text, ZWJ/ZWNJ and emoji pass.
    public static func isDisplaySafe(_ s: String) -> Bool {
        for u in s.unicodeScalars {
            if u == "\t" || u == "\n" { continue }
            if u.value < 0x20 || (u.value >= 0x7F && u.value <= 0x9F) { return false }
            switch u.value {
            case 0x061C, 0x200E, 0x200F,          // ALM, LRM, RLM
                 0x202A...0x202E,                 // LRE RLE PDF LRO RLO
                 0x2066...0x2069,                 // LRI RLI FSI PDI
                 0xFEFF:                          // ZWNBSP used as content
                return false
            default: continue
            }
        }
        return true
    }

    static func str(_ o: [String: JSONValue], _ k: String) throws -> String {
        guard let v = o[k] else { throw ProtocolError.missingField(k) }
        guard case let .string(s) = v else { throw ProtocolError.wrongType(k) }
        return s
    }

    static func num(_ o: [String: JSONValue], _ k: String) throws -> Double {
        guard let v = o[k] else { throw ProtocolError.missingField(k) }
        guard case let .number(n) = v else { throw ProtocolError.wrongType(k) }
        return n
    }

    // Canonical bytes for a decision the app SIGNS: sorted keys, compact, JSON-escaped.
    public static func canonicalBytes(_ fields: [String: CanonicalValue]) -> Data {
        var parts = [String]()
        for key in fields.keys.sorted() {
            parts.append("\(encodeString(key)):\(fields[key]!.encoded)")
        }
        return Data(("{" + parts.joined(separator: ",") + "}").utf8)
    }

    static func encodeString(_ s: String) -> String {
        var out = "\""
        for scalar in s.unicodeScalars {
            switch scalar {
            case "\"": out += "\\\""
            case "\\": out += "\\\\"
            case "\n": out += "\\n"
            case "\r": out += "\\r"
            case "\t": out += "\\t"
            case let u where u.value < 0x20:
                out += String(format: "\\u%04x", u.value)
            default: out.unicodeScalars.append(scalar)
            }
        }
        return out + "\""
    }
}

public enum CanonicalValue {
    case string(String)
    case int(Int64)
    var encoded: String {
        switch self {
        case let .string(s): return ApprovalProtocol.encodeString(s)
        case let .int(n): return String(n)
        }
    }
}

// MARK: - typed views
/// V2: carries the EXACT execution-determining `task` and the `device_id` the challenge was
/// issued to. `task` — not `humanSummary` — is what the approver authorizes.
public struct ApprovalChallenge {
    public let coreInstanceID, approvalID, actionDigest, principalID: String
    public let deviceID, toolID, mode, workspace, task, humanSummary, challengeNonce: String
    public let issuedAt, expiresAt: Double

    public init(from object: [String: JSONValue]) throws {
        let allowed = Set(["protocol_version", "type", "core_instance_id", "approval_id",
                           "action_digest", "principal_id", "device_id", "tool_id", "mode",
                           "workspace", "task", "human_summary", "challenge_nonce",
                           "issued_at", "expires_at"])
        for k in object.keys where !allowed.contains(k) { throw ProtocolError.wrongType(k) }
        if try ApprovalProtocol.num(object, "protocol_version") != Double(PROTOCOL_VERSION) {
            throw ProtocolError.badVersion
        }
        if try ApprovalProtocol.str(object, "type") != "approval_challenge" { throw ProtocolError.badType }
        coreInstanceID = try ApprovalProtocol.str(object, "core_instance_id")
        approvalID = try ApprovalProtocol.str(object, "approval_id")
        actionDigest = try ApprovalProtocol.str(object, "action_digest")
        principalID = try ApprovalProtocol.str(object, "principal_id")
        deviceID = try ApprovalProtocol.str(object, "device_id")
        toolID = try ApprovalProtocol.str(object, "tool_id")
        mode = try ApprovalProtocol.str(object, "mode")
        workspace = try ApprovalProtocol.str(object, "workspace")
        task = try ApprovalProtocol.str(object, "task")
        humanSummary = try ApprovalProtocol.str(object, "human_summary")
        challengeNonce = try ApprovalProtocol.str(object, "challenge_nonce")
        issuedAt = try ApprovalProtocol.num(object, "issued_at")
        expiresAt = try ApprovalProtocol.num(object, "expires_at")
        // Defense in depth: mirror the Core display-safety policy. A challenge whose
        // human-facing fields could spoof the approval screen is rejected outright.
        for (name, value) in [("tool_id", toolID), ("mode", mode), ("workspace", workspace),
                              ("task", task), ("human_summary", humanSummary)] {
            guard ApprovalProtocol.isDisplaySafe(value) else { throw ProtocolError.wrongType(name) }
        }
    }
}

public struct ApprovalDecision {
    public var coreInstanceID, approvalID, actionDigest, principalID: String
    public var deviceID, keyID, challengeNonce, challengePayloadSHA256, decision: String
    public var issuedAt, challengeExpiresAt: Int64

    public init(coreInstanceID: String, approvalID: String, actionDigest: String,
                principalID: String, deviceID: String, keyID: String, challengeNonce: String,
                challengePayloadSHA256: String, decision: String, issuedAt: Int64,
                challengeExpiresAt: Int64) {
        self.coreInstanceID = coreInstanceID; self.approvalID = approvalID
        self.actionDigest = actionDigest; self.principalID = principalID
        self.deviceID = deviceID; self.keyID = keyID; self.challengeNonce = challengeNonce
        self.challengePayloadSHA256 = challengePayloadSHA256
        self.decision = decision; self.issuedAt = issuedAt; self.challengeExpiresAt = challengeExpiresAt
    }

    /// Exact bytes to sign (and send). The Mac verifies over these bytes and strict-parses.
    /// V2 binds `challenge_payload_sha256` — the hash of the exact signed challenge bytes
    /// this device verified and DISPLAYED — so consent is tied to the display context.
    public func canonicalBytes() -> Data {
        ApprovalProtocol.canonicalBytes([
            "protocol_version": .int(Int64(PROTOCOL_VERSION)),
            "type": .string("approval_decision"),
            "core_instance_id": .string(coreInstanceID),
            "approval_id": .string(approvalID),
            "action_digest": .string(actionDigest),
            "principal_id": .string(principalID),
            "device_id": .string(deviceID),
            "key_id": .string(keyID),
            "challenge_nonce": .string(challengeNonce),
            "challenge_payload_sha256": .string(challengePayloadSHA256),
            "decision": .string(decision),
            "issued_at": .int(issuedAt),
            "challenge_expires_at": .int(challengeExpiresAt),
        ])
    }
}
