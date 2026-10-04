// Golden-vector interop tests (STEP S2A / S2A.1-P0, Approval Protocol V2). Proves the Swift crypto/protocol matches the
// Python control-plane against the SHARED tests/vectors/mobile_approval_v2.json, on the
// Mac host (no simulator, no device). The Secure Enclave + Face ID gate is proven only on
// a physical iPhone (app target) — a simulator is NEVER accepted as that proof.
import XCTest
@testable import SolvioApprovalsKit

final class GoldenVectorTests: XCTestCase {

    func loadVectors() throws -> [String: Any] {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "mobile_approval_v2",
                                                  withExtension: "json"))
        let data = try Data(contentsOf: url)
        return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    func testSignatureVectorsVerifyAsExpected() throws {
        let root = try loadVectors()
        let vectors = try XCTUnwrap(root["signature_vectors"] as? [[String: Any]])
        XCTAssertFalse(vectors.isEmpty)
        for v in vectors {
            let name = v["name"] as! String
            let pub = try XCTUnwrap(Data(base64Encoded: v["pubkey_x963_b64"] as! String))
            let sig = try XCTUnwrap(Data(base64Encoded: v["signature_b64"] as! String))
            let payload = try XCTUnwrap(Data(base64Encoded: v["payload_b64"] as! String))
            let ok = ApprovalCrypto.verify(publicKeyX963: pub, signatureDER: sig, data: payload)
            XCTAssertEqual(ok, v["expect_valid"] as! Bool, "signature vector \(name)")
        }
    }

    func testParseVectorsMatchStrictParser() throws {
        let root = try loadVectors()
        let vectors = try XCTUnwrap(root["parse_vectors"] as? [[String: Any]])
        for v in vectors {
            let name = v["name"] as! String
            let payload = try XCTUnwrap(Data(base64Encoded: v["payload_b64"] as! String))
            var parsed = true
            do { _ = try ApprovalProtocol.strictParse(payload) } catch { parsed = false }
            XCTAssertEqual(parsed, v["expect_parse"] as! Bool, "parse vector \(name)")
        }
    }

    func testChallengeDecodeFromValidVector() throws {
        let root = try loadVectors()
        let vectors = try XCTUnwrap(root["signature_vectors"] as? [[String: Any]])
        let vc = try XCTUnwrap(vectors.first { ($0["name"] as? String) == "valid_challenge_unicode" })
        let payload = try XCTUnwrap(Data(base64Encoded: vc["payload_b64"] as! String))
        let obj = try ApprovalProtocol.strictParse(payload)
        let ch = try ApprovalChallenge(from: obj)
        XCTAssertEqual(ch.toolID, "codex_task")
        XCTAssertEqual(ch.mode, "modify")
        XCTAssertFalse(ch.humanSummary.isEmpty)   // unicode summary decoded
    }

    func testDecisionRoundTripAndTamper() throws {
        let signer = SoftwareSigner()
        let decision = ApprovalDecision(
            coreInstanceID: "core-1", approvalID: "ap-1",
            actionDigest: String(repeating: "a", count: 64), principalID: "local-owner",
            deviceID: "dev-1", keyID: signer.keyID, challengeNonce: "n",
            challengePayloadSHA256: String(repeating: "c", count: 64),
            decision: DECISION_APPROVE, issuedAt: 1000, challengeExpiresAt: 1120)
        let bytes = decision.canonicalBytes()
        let sig = try signer.sign(bytes)
        XCTAssertTrue(ApprovalCrypto.verify(publicKeyX963: signer.publicKeyX963,
                                            signatureDER: sig, data: bytes))
        // the Mac would strict-parse these exact bytes and read the decision
        let obj = try ApprovalProtocol.strictParse(bytes)
        if case let .string(s)? = obj["decision"] { XCTAssertEqual(s, "APPROVE") }
        else { XCTFail("decision field missing") }
        // tamper -> signature no longer verifies
        var tampered = bytes
        tampered[tampered.count / 2] ^= 0x01
        XCTAssertFalse(ApprovalCrypto.verify(publicKeyX963: signer.publicKeyX963,
                                             signatureDER: sig, data: tampered))
    }

    func testKeyIDLength() {
        let signer = SoftwareSigner()
        XCTAssertEqual(signer.keyID.count, 16)
        XCTAssertEqual(ApprovalCrypto.keyID(x963: signer.publicKeyX963), signer.keyID)
    }
}
