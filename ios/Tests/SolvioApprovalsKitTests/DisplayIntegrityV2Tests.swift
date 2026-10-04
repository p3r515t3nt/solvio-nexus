// S2A.1-P0 display-integrity tests (Approval Protocol V2), Swift side.
//
// Closes audit finding F1: the approver must see the EXACT execution-determining `task`,
// delivered inside the MAC-SIGNED challenge — never from the unsigned pending list, and
// never replaced by the untrusted model-authored `human_summary`.
//
// These run on the Mac host against the SHARED golden vectors. Secure Enclave + Face ID
// remain provable only on a physical iPhone; a simulator is never accepted as that proof.
import XCTest
@testable import SolvioApprovalsKit

final class DisplayIntegrityV2Tests: XCTestCase {

    private func loadVectors() throws -> [String: Any] {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "mobile_approval_v2",
                                                  withExtension: "json"))
        let data = try Data(contentsOf: url)
        return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    private func vector(_ root: [String: Any], _ name: String) throws -> [String: Any] {
        let vs = try XCTUnwrap(root["signature_vectors"] as? [[String: Any]])
        return try XCTUnwrap(vs.first { ($0["name"] as? String) == name })
    }

    // MARK: - the signed challenge carries the authoritative action

    func testSignedChallengeCarriesExactTaskAndToolID() throws {
        let root = try loadVectors()
        let v = try vector(root, "valid_challenge")
        let payload = try XCTUnwrap(Data(base64Encoded: v["payload_b64"] as! String))
        let ch = try ApprovalChallenge(from: try ApprovalProtocol.strictParse(payload))

        XCTAssertEqual(ch.task, root["golden_challenge_task"] as? String)
        XCTAssertEqual(ch.humanSummary, root["golden_challenge_human_summary"] as? String)
        XCTAssertEqual(ch.toolID, "codex_task")
        XCTAssertEqual(ch.mode, "modify")
        XCTAssertFalse(ch.workspace.isEmpty)
        XCTAssertFalse(ch.deviceID.isEmpty)
        // The whole point of F1: the summary is NOT the action.
        XCTAssertNotEqual(ch.task, ch.humanSummary)
    }

    func testChallengePayloadSHA256MatchesSharedVector() throws {
        let root = try loadVectors()
        let v = try vector(root, "valid_challenge")
        let payload = try XCTUnwrap(Data(base64Encoded: v["payload_b64"] as! String))
        XCTAssertEqual(ApprovalCrypto.sha256Hex(payload),
                       root["golden_challenge_payload_sha256"] as? String)
    }

    func testTamperedTaskBreaksMacSignature() throws {
        let root = try loadVectors()
        let good = try vector(root, "valid_challenge")
        let bad = try vector(root, "tampered_challenge_task")
        let pub = try XCTUnwrap(Data(base64Encoded: good["pubkey_x963_b64"] as! String))

        let goodPayload = try XCTUnwrap(Data(base64Encoded: good["payload_b64"] as! String))
        let goodSig = try XCTUnwrap(Data(base64Encoded: good["signature_b64"] as! String))
        XCTAssertTrue(ApprovalCrypto.verify(publicKeyX963: pub, signatureDER: goodSig,
                                            data: goodPayload))

        let badPayload = try XCTUnwrap(Data(base64Encoded: bad["payload_b64"] as! String))
        let badSig = try XCTUnwrap(Data(base64Encoded: bad["signature_b64"] as! String))
        XCTAssertFalse(ApprovalCrypto.verify(publicKeyX963: pub, signatureDER: badSig,
                                             data: badPayload),
                       "a task flipped after signing must NOT verify")
    }

    // MARK: - no silent downgrade to V1

    func testV1ChallengeRejectedEvenWithValidSignature() throws {
        let root = try loadVectors()
        let vs = try XCTUnwrap(root["downgrade_vectors"] as? [[String: Any]])
        XCTAssertFalse(vs.isEmpty)
        for v in vs {
            let pub = try XCTUnwrap(Data(base64Encoded: v["pubkey_x963_b64"] as! String))
            let payload = try XCTUnwrap(Data(base64Encoded: v["payload_b64"] as! String))
            let sig = try XCTUnwrap(Data(base64Encoded: v["signature_b64"] as! String))
            // the signature is genuinely valid ...
            XCTAssertEqual(ApprovalCrypto.verify(publicKeyX963: pub, signatureDER: sig,
                                                 data: payload),
                           v["expect_signature_valid"] as! Bool)
            // ... and the typed V2 parse must STILL refuse it
            var parsed = true
            do {
                _ = try ApprovalChallenge(from: try ApprovalProtocol.strictParse(payload))
            } catch { parsed = false }
            XCTAssertEqual(parsed, v["expect_challenge_parse"] as! Bool,
                           "a valid signature must never resurrect the V1 contract")
        }
    }

    // MARK: - shared display-safety policy

    func testDisplaySafetyPolicyMatchesSharedVectors() throws {
        let root = try loadVectors()
        let vs = try XCTUnwrap(root["display_vectors"] as? [[String: Any]])
        XCTAssertFalse(vs.isEmpty)
        for v in vs {
            let name = v["name"] as! String
            let text = v["text"] as! String
            XCTAssertEqual(ApprovalProtocol.isDisplaySafe(text), v["expect_safe"] as! Bool,
                           "display vector \(name)")
        }
    }

    func testChallengeWithSpoofingTaskIsRejected() throws {
        // A challenge whose task carries a bidi override must not decode at all.
        let spoofed = "{\"action_digest\":\"\(String(repeating: "a", count: 64))\","
            + "\"approval_id\":\"ap-x\",\"challenge_nonce\":\"\(String(repeating: "n", count: 64))\","
            + "\"core_instance_id\":\"core-x\",\"device_id\":\"dev-x\",\"expires_at\":1120,"
            + "\"human_summary\":\"harmlos\",\"issued_at\":1000,\"mode\":\"modify\","
            + "\"principal_id\":\"local-owner\",\"protocol_version\":2,\"task\":\"ok\u{202E}evil\","
            + "\"tool_id\":\"codex_task\",\"type\":\"approval_challenge\",\"workspace\":\"/tmp\"}"
        let obj = try ApprovalProtocol.strictParse(Data(spoofed.utf8))
        XCTAssertThrowsError(try ApprovalChallenge(from: obj),
                             "bidi override in task must fail closed")
    }

    // MARK: - the decision binds the displayed challenge

    func testDecisionBindsChallengePayloadHash() throws {
        let root = try loadVectors()
        let v = try vector(root, "valid_challenge")
        let payload = try XCTUnwrap(Data(base64Encoded: v["payload_b64"] as! String))
        let ch = try ApprovalChallenge(from: try ApprovalProtocol.strictParse(payload))
        let hash = ApprovalCrypto.sha256Hex(payload)

        let signer = SoftwareSigner()
        let decision = ApprovalDecision(
            coreInstanceID: ch.coreInstanceID, approvalID: ch.approvalID,
            actionDigest: ch.actionDigest, principalID: ch.principalID,
            deviceID: ch.deviceID, keyID: signer.keyID, challengeNonce: ch.challengeNonce,
            challengePayloadSHA256: hash, decision: DECISION_APPROVE,
            issuedAt: 1010, challengeExpiresAt: Int64(ch.expiresAt))
        let bytes = decision.canonicalBytes()

        let obj = try ApprovalProtocol.strictParse(bytes)
        guard case let .string(bound)? = obj["challenge_payload_sha256"] else {
            return XCTFail("challenge_payload_sha256 missing from the signed decision")
        }
        XCTAssertEqual(bound, hash)
        guard case let .number(ver)? = obj["protocol_version"] else {
            return XCTFail("protocol_version missing")
        }
        XCTAssertEqual(Int(ver), 2)

        let sig = try signer.sign(bytes)
        XCTAssertTrue(ApprovalCrypto.verify(publicKeyX963: signer.publicKeyX963,
                                            signatureDER: sig, data: bytes))
        // flipping the display binding invalidates the device signature
        let flipped = Data(String(data: bytes, encoding: .utf8)!
            .replacingOccurrences(of: hash, with: String(repeating: "f", count: 64)).utf8)
        XCTAssertFalse(ApprovalCrypto.verify(publicKeyX963: signer.publicKeyX963,
                                             signatureDER: sig, data: flipped))
    }

    // MARK: - long task must not lose authorization-relevant content

    func testLongTaskSurvivesRoundTripUntruncated() throws {
        let long = String(repeating: "abcdefghij", count: 400)   // 4000 chars
        XCTAssertTrue(ApprovalProtocol.isDisplaySafe(long))
        let json = "{\"action_digest\":\"\(String(repeating: "a", count: 64))\","
            + "\"approval_id\":\"ap-x\",\"challenge_nonce\":\"\(String(repeating: "n", count: 64))\","
            + "\"core_instance_id\":\"core-x\",\"device_id\":\"dev-x\",\"expires_at\":1120,"
            + "\"human_summary\":\"kurz\",\"issued_at\":1000,\"mode\":\"modify\","
            + "\"principal_id\":\"local-owner\",\"protocol_version\":2,\"task\":\"\(long)\","
            + "\"tool_id\":\"codex_task\",\"type\":\"approval_challenge\",\"workspace\":\"/tmp\"}"
        let ch = try ApprovalChallenge(from: try ApprovalProtocol.strictParse(Data(json.utf8)))
        XCTAssertEqual(ch.task.count, long.count, "task must not be truncated on decode")
        XCTAssertEqual(ch.task, long)
    }
}
