import Foundation
import XCTest
@testable import SolvioApprovalsKit

final class TaskFollowupTests: XCTestCase {
    private let run = "ar-1111111111111111", task = "at-1111111111111111"
    private func body(_ text: String = "Bitte ergänze März.", ids: [String] = []) throws -> AppTaskFollowupBody {
        try .init(runID: run, text: text, revision: 1, digest: String(repeating: "a", count: 64),
            inputIDs: ids, requestID: "followup-golden-001")
    }
    private func challenge(_ body: AppTaskFollowupBody, nonce: String = String(repeating: "b", count: 64),
                           type: String = "app_task_followup_binding") throws -> AppTaskFollowupChallenge {
        let raw: [String: Any] = ["protocol_version": 1, "type": type, "core_instance_id": "core-fixture",
            "principal_id": "owner-fixture", "device_id": "device-fixture", "nonce": nonce,
            "request_digest": body.requestDigest, "enrollment_id": "enroll-fixture",
            "app_attest_key_id": "key-fixture", "approval_key_sha256": String(repeating: "c", count: 64)]
        return .init(nonce: nonce, requestDigest: body.requestDigest, expiresAt: 2_000_000_000,
            bindingB64: try JSONSerialization.data(withJSONObject: raw, options: [.sortedKeys, .withoutEscapingSlashes]).base64EncodedString())
    }
    private func hash(_ challenge: AppTaskFollowupChallenge, _ body: AppTaskFollowupBody,
                      core: String = "core-fixture", device: String = "device-fixture", key: String = "key-fixture",
                      now: Double = 1_999_999_990) throws -> Data {
        try challenge.clientDataHash(body: body, coreID: core, deviceID: device, appAttestKeyID: key, now: now)
    }
    private func accepted(_ body: AppTaskFollowupBody) -> AppTaskFollowupAccepted {
        .init(taskID: task, runID: "ar-2222222222222222", parentRunID: body.run_id, revision: 2,
            digest: String(repeating: "d", count: 64), admission: "preparing")
    }

    func testActualPublicCoreGoldenVectorBindsSameTaskRevisionAndSeparateProofDomain() throws {
        let body = try body(), issued = try challenge(body)
        // Actual Python public App-Attest test: test_agent_task_followup_entry golden vector.
        XCTAssertEqual(body.requestDigest, "e07158911a9ddd8748efba06cba4f6e264209d2407a912ba269f0f20aaeb6d79")
        XCTAssertEqual(try hash(issued, body).map { String(format: "%02x", $0) }.joined(),
            "33a47361996c86965f099ef1faa61e161c8ef5a0e3daa84684be50d71bb88619")
        XCTAssertEqual(try JSONDecoder().decode(AppTaskFollowupBody.self, from: JSONEncoder().encode(body)), body)
        let raw = try XCTUnwrap(Data(base64Encoded: issued.binding_b64))
        XCTAssertNotEqual(try hash(issued, body), AppAttestBinding.domainHash(Data("SOLVIO_APP_TASK_START_V1".utf8), raw))
        XCTAssertNotEqual(try hash(issued, body), AppAttestBinding.domainHash(Data("SOLVIO_APP_ACTION_ANSWER_V1".utf8), raw))
    }
    func testCanonicalBodyRejectsMutationExtraMissingAndUnboundedInput() throws {
        let raw = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(body())) as? [String: Any])
        let bad: [(String, Any)] = [("run_id", "../x"), ("run_id", run + "\n"), ("text", ""),
            ("text", " hello"), ("text", "hello\r\nworld"), ("text", String(repeating: "a", count: 2001)),
            ("expected_revision", true), ("expected_revision", 0), ("expected_revision", 100),
            ("expected_digest", String(repeating: "A", count: 64)), ("input_artifact_ids", ["/tmp/private"]),
            ("input_artifact_ids", ["aa-1111111111111111", "aa-1111111111111111"]),
            ("input_artifact_ids", (1...5).map { "aa-" + String(repeating: String($0), count: 16) }),
            ("client_request_id", "short"), ("question_id", "extra")]
        for (key, value) in bad {
            var changed = raw; changed[key] = value
            XCTAssertThrowsError(try JSONDecoder().decode(AppTaskFollowupBody.self, from: JSONSerialization.data(withJSONObject: changed)), key)
        }
        for key in raw.keys {
            var changed = raw; changed.removeValue(forKey: key)
            XCTAssertThrowsError(try JSONDecoder().decode(AppTaskFollowupBody.self, from: JSONSerialization.data(withJSONObject: changed)), key)
        }
        XCTAssertNoThrow(try body(String(repeating: "😀", count: 2000)))
    }
    func testRetryMetadataContainsNoTextAndOnlyRestoresExactParentInputsAndIdentity() throws {
        let ids = ["aa-1111111111111111", "aa-2222222222222222"]
        let body = try body(ids: ids), retry = TaskFollowupRetryBinding(body: body, coreID: "core", deviceID: "phone")
        let raw = try JSONEncoder().encode(retry)
        XCTAssertFalse(String(decoding: raw, as: UTF8.self).contains(body.text))
        let saved = try JSONDecoder().decode(TaskFollowupRetryBinding.self, from: raw)
        let restore: (String, String, Int, String, [String], String, String) -> AppTaskFollowupBody? = {
            saved.restore(runID: $0, text: $1, revision: $2, digest: $3, inputIDs: $4, coreID: $5, deviceID: $6)
        }
        XCTAssertEqual(restore(run, body.text, 1, body.expected_digest, ids, "core", "phone"), body)
        XCTAssertNil(restore(run, body.text + "!", 1, body.expected_digest, ids, "core", "phone"))
        XCTAssertNil(restore(run, body.text, 2, body.expected_digest, ids, "core", "phone"))
        XCTAssertNil(restore(run, body.text, 1, body.expected_digest, ids.reversed(), "core", "phone"))
        XCTAssertNil(restore(run, body.text, 1, body.expected_digest, ids, "other", "phone"))
        XCTAssertNil(restore(run, body.text, 1, body.expected_digest, ids, "core", "other"))
        XCTAssertNil(restore("ar-2222222222222222", body.text, 1, body.expected_digest, ids, "core", "phone"))
    }
    func testChallengeCannotCrossTaskAnswerStartOrOwnerScopeAndChecksExpiry() throws {
        let body = try body(), issued = try challenge(body)
        for purpose in ["app_task_start_binding", "app_action_answer_binding"] {
            XCTAssertThrowsError(try hash(challenge(body, type: purpose), body))
        }
        XCTAssertThrowsError(try hash(issued, self.body("Anderer Inhalt")))
        XCTAssertThrowsError(try hash(issued, body, core: "other"))
        XCTAssertThrowsError(try hash(issued, body, device: "other"))
        XCTAssertThrowsError(try hash(issued, body, key: "other"))
        XCTAssertThrowsError(try hash(issued, body, now: issued.expires_at))
        XCTAssertThrowsError(try hash(issued, body, now: issued.expires_at - 301))
        let raw = String(decoding: try XCTUnwrap(Data(base64Encoded: issued.binding_b64)), as: UTF8.self)
        let duplicate = "{\"nonce\":\"duplicate\"," + raw.dropFirst()
        XCTAssertThrowsError(try hash(.init(nonce: issued.nonce, requestDigest: body.requestDigest,
            expiresAt: issued.expires_at, bindingB64: Data(duplicate.utf8).base64EncodedString()), body))
    }
    func testAcceptedResponseRequiresExactParentTaskAndNewRevisionIdentity() throws {
        let body = try body(), valid = accepted(body)
        XCTAssertNoThrow(try valid.validate(body: body, taskID: task))
        for changed in [
            AppTaskFollowupAccepted(taskID: "at-2222222222222222", runID: valid.run_id, parentRunID: run, revision: 2, digest: valid.digest, admission: "ready"),
            .init(taskID: task, runID: run, parentRunID: run, revision: 2, digest: valid.digest, admission: "ready"),
            .init(taskID: task, runID: valid.run_id, parentRunID: valid.run_id, revision: 2, digest: valid.digest, admission: "ready"),
            .init(taskID: task, runID: valid.run_id, parentRunID: run, revision: 1, digest: valid.digest, admission: "ready"),
            .init(taskID: task, runID: valid.run_id, parentRunID: run, revision: 2, digest: "bad", admission: "ready"),
            .init(taskID: task, runID: valid.run_id, parentRunID: run, revision: 2, digest: valid.digest, admission: "done")
        ] { XCTAssertThrowsError(try changed.validate(body: body, taskID: task)) }
    }
    func testOldRunAndNewRevisionProjectionBothDecodeWithoutReplacingOriginalObjective() throws {
        var raw: [String: Any] = ["id": run, "aufgabe": task, "auftrag": "Originalauftrag", "zustand": "Fertig", "zustand_code": "COMPLETED", "offen": false]
        let decode: () throws -> AgentRun = { try JSONDecoder().decode(AgentRun.self, from: JSONSerialization.data(withJSONObject: raw)) }
        XCTAssertNil(try decode().task_revision)
        raw["task_revision"] = ["revision": 2, "digest": String(repeating: "d", count: 64), "text": "Ergänzung", "parent_run_id": run]
        raw["followup"] = ["eligible": true, "reason": ""]
        XCTAssertEqual(try decode().auftrag, "Originalauftrag")
        XCTAssertEqual(try decode().task_revision?.text, "Ergänzung")
        XCTAssertEqual(try decode().followup?.eligible, true)
        // Older nested runs may already have a parent without a user revision.
        XCTAssertNoThrow(try TaskRevisionSnapshot(revision: 1, digest: String(repeating: "a", count: 64), text: "Original", parentRunID: run))
    }
    @MainActor
    func testUnknownRetryKeepsBodyWithFreshProofAndConcurrentTapNeverDispatches() async throws {
        let body = try body(), attempt = AppTaskFollowupAttempt()
        var sent: [AppTaskFollowupBody] = [], signed: [Data] = [], nested = 0
        for i in 0...1 {
            do {
                _ = try await attempt.send(body: body, taskID: task, coreID: "core-fixture", deviceID: "device-fixture", appAttestKeyID: "key-fixture",
                    challenge: { body in
                        do {
                            _ = try await attempt.send(body: body, taskID: self.task, coreID: "core-fixture", deviceID: "device-fixture", appAttestKeyID: "key-fixture",
                                challenge: { nested += 1; return try self.challenge($0) }, sign: { _ in Data([1]) }, submit: { b, _ in self.accepted(b) })
                            XCTFail("Concurrent follow-up escaped")
                        } catch { XCTAssertEqual(error as? TaskStartError, .alreadySending) }
                        return try self.challenge(body, nonce: String(repeating: i == 0 ? "b" : "c", count: 64))
                    }, sign: { signed.append($0); return Data([1]) }, submit: { b, _ in
                        sent.append(b); if i == 0 { throw URLError(.networkConnectionLost) }; return self.accepted(b)
                    }, now: { 1_999_999_990 })
                XCTAssertEqual(i, 1)
            } catch { XCTAssertEqual(i, 0) }
        }
        XCTAssertEqual(sent, [body, body]); XCTAssertEqual(nested, 0); XCTAssertEqual(signed.count, 2); XCTAssertNotEqual(signed[0], signed[1])
    }
    @MainActor
    func testStaleScopeAfterChallengeAndAfterSigningNeverSubmits() async throws {
        let body = try body()
        for staleAfterSigning in [false, true] {
            var current = true, signed = 0, sent = 0
            do {
                _ = try await AppTaskFollowupAttempt().send(body: body, taskID: task, coreID: "core-fixture", deviceID: "device-fixture", appAttestKeyID: "key-fixture",
                    challenge: { b in if !staleAfterSigning { current = false }; return try self.challenge(b) },
                    sign: { _ in signed += 1; current = false; return Data([1]) },
                    submit: { b, _ in sent += 1; return self.accepted(b) }, isCurrent: { current }, now: { 1_999_999_990 })
                XCTFail("Stale scope submitted")
            } catch { XCTAssertTrue(error is CancellationError) }
            XCTAssertEqual(signed, staleAfterSigning ? 1 : 0); XCTAssertEqual(sent, 0)
        }
    }
}
