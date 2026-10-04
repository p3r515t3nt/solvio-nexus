import Foundation
import XCTest
@testable import SolvioApprovalsKit

final class TaskActionAnswerTests: XCTestCase {
    private let runID = "ar-0123456789abcdef"
    private let keyID = Data(repeating: 1, count: 32).base64EncodedString()
    private func question(revision: Int = 1, digest: String = String(repeating: "a", count: 64),
                          prompt: String = "Wie lange dauert der Termin?") throws -> ActionIntentQuestion {
        try ActionIntentQuestion(id: "aiq-" + String(repeating: "b", count: 32), revision: revision,
            digest: digest, field: .duration, prompt: prompt, inputType: .number, placeholder: "60")
    }
    private func body(answer: String = "60") throws -> AppTaskActionAnswerBody {
        try AppTaskActionAnswerBody(runID: runID, question: question(), answer: answer, requestID: "answer-request-001")
    }
    private func challenge(_ body: AppTaskActionAnswerBody, nonce: String = String(repeating: "ab", count: 32),
                           type: String = "app_action_answer_binding", spaced: Bool = false) throws -> AppTaskActionAnswerChallenge {
        let binding: [String: Any] = ["protocol_version": 1, "type": type, "core_instance_id": "core",
            "principal_id": "owner", "device_id": "phone", "nonce": nonce, "request_digest": body.requestDigest,
            "enrollment_id": "enrollment", "app_attest_key_id": keyID, "approval_key_sha256": String(repeating: "c", count: 64)]
        let raw = try JSONSerialization.data(withJSONObject: binding,
            options: spaced ? [.sortedKeys, .prettyPrinted, .withoutEscapingSlashes] : [.sortedKeys, .withoutEscapingSlashes])
        return AppTaskActionAnswerChallenge(nonce: nonce, requestDigest: body.requestDigest,
            expiresAt: 2_000_000_000, bindingB64: raw.base64EncodedString())
    }
    private func hash(_ challenge: AppTaskActionAnswerChallenge, _ body: AppTaskActionAnswerBody,
                      core: String = "core", device: String = "phone", key: String? = nil,
                      now: Double = 1_999_999_990) throws -> Data {
        try challenge.clientDataHash(body: body, coreID: core, deviceID: device, appAttestKeyID: key ?? keyID, now: now)
    }

    func testCanonicalAnswerIncludesRunQuestionRevisionDigestAndUnicodeExactly() throws {
        let body = try body(answer: "München 🚲\n60 Minuten")
        let expected = #"{"answer":"München 🚲\n60 Minuten","client_request_id":"answer-request-001","expected_digest":""#
            + String(repeating: "a", count: 64) + #"","expected_revision":1,"question_id":"aiq-"#
            + String(repeating: "b", count: 32) + #"","run_id":"ar-0123456789abcdef"}"#
        XCTAssertEqual(String(decoding: body.canonicalBytes(), as: UTF8.self), expected)
        // Real Python Core action_intent.canonical_answer + protocol.canonical_bytes.
        XCTAssertEqual(body.requestDigest, "30674c3899ae8fc9d060c984a3cd43f254d06decc6acdb4ab7eac75720cc5344")
        // Real TaskAnswerProofService._binding + its separate client_data_hash.
        XCTAssertEqual(try hash(challenge(body), body).map { String(format: "%02x", $0) }.joined(),
                       "ea92c4d30ffdc23fdec3489ad50ef7288b2c9bb4b6d2c3da9bc6122a4610724c")
        XCTAssertEqual(try JSONDecoder().decode(AppTaskActionAnswerBody.self, from: JSONEncoder().encode(body)), body)
    }

    func testAnswerRejectsMissingExtraInvalidBindingAndUntrimmedOrOversizedText() throws {
        let raw = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(body())) as? [String: Any])
        let invalid: [(String, Any)] = [("run_id", "ar-0123456789abcdef\n"), ("run_id", "../other"),
            ("question_id", "aiq-short"), ("expected_revision", 0), ("expected_revision", 101), ("expected_revision", true),
            ("expected_revision", "1"), ("expected_revision", 1.5), ("expected_digest", String(repeating: "A", count: 64)),
            ("answer", ""), ("answer", " 60"), ("answer", "60\n"), ("answer", "a\0b"), ("answer", "a\r\nb"),
            ("answer", String(repeating: "x", count: 2001)), ("client_request_id", "short")]
        for (field, value) in invalid {
            var bad = raw; bad[field] = value
            XCTAssertThrowsError(try JSONDecoder().decode(AppTaskActionAnswerBody.self, from: JSONSerialization.data(withJSONObject: bad)), field)
        }
        for field in raw.keys {
            var bad = raw; bad.removeValue(forKey: field)
            XCTAssertThrowsError(try JSONDecoder().decode(AppTaskActionAnswerBody.self, from: JSONSerialization.data(withJSONObject: bad)), field)
        }
        var extra = raw; extra["action_request"] = [:]
        XCTAssertThrowsError(try JSONDecoder().decode(AppTaskActionAnswerBody.self, from: JSONSerialization.data(withJSONObject: extra)))
        XCTAssertNoThrow(try body(answer: String(repeating: "😀", count: 2000)))
    }

    func testChoiceRequiresActualOfferedValueAndQuestionIdentityIgnoresOnlyPresentation() throws {
        let a = try question(), relabeled = try question(prompt: "Wie viele Minuten?")
        XCTAssertEqual(a.bindingID, relabeled.bindingID)
        XCTAssertNotEqual(a.bindingID, try question(revision: 2).bindingID)
        XCTAssertNotEqual(a.bindingID, try question(digest: String(repeating: "c", count: 64)).bindingID)
        let select = try ActionIntentQuestion(id: a.id, revision: a.revision, digest: a.digest,
            field: .account, prompt: "Welches Konto?", inputType: .select,
            options: [.init(value: "account-one", label: "Privat"), .init(value: "account-two", label: "Arbeit")])
        XCTAssertThrowsError(try AppTaskActionAnswerBody(runID: runID, question: select, answer: "Privat", requestID: "answer-request-001"))
        XCTAssertNoThrow(try AppTaskActionAnswerBody(runID: runID, question: select, answer: "account-one", requestID: "answer-request-001"))
        XCTAssertThrowsError(try ActionIntentQuestion(id: a.id, revision: a.revision, digest: a.digest,
            field: .account, prompt: "Konto?", inputType: .select,
            options: [.init(value: "a", label: "A"), .init(value: "a", label: "B")]))
        XCTAssertThrowsError(try ActionIntentQuestion(id: a.id, revision: a.revision, digest: a.digest,
            field: .duration, prompt: "Dauer?", inputType: .select, options: []))
    }

    func testMissingAccountQuestionPreservesRunWithoutInventingAnAnswerChoice() throws {
        let q = try question()
        let raw: [String: Any] = ["id": runID, "aufgabe": "Termin", "auftrag": "Lege den Termin an.",
            "zustand": "Wartet", "zustand_code": "WAITING_USER", "offen": true,
            "action_intent": ["status": "waiting_user", "question": ["id": q.id, "revision": q.revision,
                "digest": q.digest, "field": "account", "prompt": "Verbinde zuerst ein Kalenderkonto.",
                "input_type": "select", "options": []]]]
        let run = try JSONDecoder().decode(AgentRun.self, from: JSONSerialization.data(withJSONObject: raw))
        let missing = try XCTUnwrap(run.action_intent?.question)
        XCTAssertTrue(missing.isAccountConnectionRequired)
        XCTAssertEqual(missing.options, [])
        XCTAssertFalse(run.canResume) // The app offers an explicit account recheck, not generic resume.
        XCTAssertThrowsError(try AppTaskActionAnswerBody(runID: runID, question: missing,
            answer: "invented-account", requestID: "answer-request-001"))
        XCTAssertFalse(q.isAccountConnectionRequired)
        let populated = try ActionIntentQuestion(id: q.id, revision: q.revision, digest: q.digest,
            field: .account, prompt: "Welches Konto?", inputType: .select, options: [.init(value: "one", label: "Privat")])
        XCTAssertFalse(populated.isAccountConnectionRequired)
    }

    func testAgentRunAcceptsOldSnapshotAndDisablesResumeOnlyForBoundQuestion() throws {
        let old: [String: Any] = ["id": runID, "aufgabe": "Prüfe", "auftrag": "Prüfe den Termin.",
                                 "zustand": "Wartet", "zustand_code": "WAITING_USER", "offen": true]
        let decode: ([String: Any]) throws -> AgentRun = { try JSONDecoder().decode(AgentRun.self, from: JSONSerialization.data(withJSONObject: $0)) }
        XCTAssertTrue(try decode(old).canResume); XCTAssertNil(try decode(old).action_intent)
        var current = old
        let snapshot = try ActionIntentSnapshot(status: .waitingUser, question: question())
        current["action_intent"] = try JSONSerialization.jsonObject(with: JSONEncoder().encode(snapshot))
        let run = try decode(current)
        XCTAssertEqual(run.action_intent, snapshot); XCTAssertFalse(run.canResume)
        XCTAssertThrowsError(try ActionIntentSnapshot(status: .waitingUser))
        XCTAssertThrowsError(try ActionIntentSnapshot(status: .resolved, question: question()))
    }

    func testFreshDraftRestoresExactAnswerMetadataWithoutStoringText() throws {
        let first = try body(), q = try question()
        let retry = TaskActionAnswerRetryBinding(body: first, coreID: "core", deviceID: "phone")
        let data = try JSONEncoder().encode(retry)
        let saved = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertEqual(Set(saved.keys), ["coreID", "deviceID", "requestID", "bodyDigest"])
        let restored = try JSONDecoder().decode(TaskActionAnswerRetryBinding.self, from: data)
        var draft = TaskActionAnswerDraft()
        XCTAssertEqual(try draft.prepare(runID: runID, question: q, answer: "60", retry: restored, coreID: "core", deviceID: "phone"), first)
        for (core, device, run, answer) in [("other", "phone", runID, "60"), ("core", "other", runID, "60"),
                                          ("core", "phone", "ar-1111111111111111", "60"), ("core", "phone", runID, "90")] {
            XCTAssertNil(restored.restore(runID: run, question: q, answer: answer, coreID: core, deviceID: device))
        }
        XCTAssertNil(restored.restore(runID: runID, question: try question(revision: 2), answer: "60", coreID: "core", deviceID: "phone"))
        XCTAssertNil(restored.restore(runID: runID, question: try question(digest: String(repeating: "d", count: 64)),
            answer: "60", coreID: "core", deviceID: "phone"))
    }

    func testDraftKeepsRetryOnPollButChangesRequestIdentityForNewAnswerOrQuestion() throws {
        var draft = TaskActionAnswerDraft()
        let first = try draft.prepare(runID: runID, question: question(), answer: "60")
        XCTAssertEqual(try draft.prepare(runID: runID, question: question(prompt: "Andere Beschriftung"), answer: "60"), first)
        let edited = try draft.prepare(runID: runID, question: question(), answer: "90")
        XCTAssertNotEqual(edited.client_request_id, first.client_request_id)
        let revised = try draft.prepare(runID: runID, question: question(revision: 2), answer: "90")
        XCTAssertNotEqual(revised.client_request_id, edited.client_request_id)
        draft.invalidate(); XCTAssertNil(draft.body)
    }

    func testChallengeChecksPurposeExactBindingBytesAndEveryOwnerKnownIdentity() throws {
        let body = try body(), compact = try challenge(body), spaced = try challenge(body, spaced: true)
        let raw = try XCTUnwrap(Data(base64Encoded: spaced.binding_b64))
        XCTAssertEqual(try hash(spaced, body), AppAttestBinding.domainHash(Data("SOLVIO_APP_ACTION_ANSWER_V1".utf8), raw))
        XCTAssertNotEqual(try hash(compact, body), try hash(spaced, body))
        XCTAssertNotEqual(try hash(spaced, body), AppAttestBinding.domainHash(Data("SOLVIO_APP_TASK_START_V1".utf8), raw))
        for type in ["app_task_start_binding", "voice_session_binding"] {
            XCTAssertThrowsError(try hash(challenge(body, type: type), body))
        }
        XCTAssertThrowsError(try hash(compact, body, core: "other"))
        XCTAssertThrowsError(try hash(compact, body, device: "other"))
        XCTAssertThrowsError(try hash(compact, body, key: "other"))
        let changedBodies = [
            try self.body(answer: "90"),
            try AppTaskActionAnswerBody(runID: "ar-1111111111111111", questionID: body.question_id,
                expectedRevision: body.expected_revision, expectedDigest: body.expected_digest,
                answer: body.answer, requestID: body.client_request_id),
            try AppTaskActionAnswerBody(runID: body.run_id, questionID: "aiq-" + String(repeating: "d", count: 32),
                expectedRevision: body.expected_revision, expectedDigest: body.expected_digest,
                answer: body.answer, requestID: body.client_request_id),
            try AppTaskActionAnswerBody(runID: body.run_id, questionID: body.question_id,
                expectedRevision: 2, expectedDigest: body.expected_digest, answer: body.answer, requestID: body.client_request_id),
            try AppTaskActionAnswerBody(runID: body.run_id, questionID: body.question_id,
                expectedRevision: body.expected_revision, expectedDigest: String(repeating: "d", count: 64),
                answer: body.answer, requestID: body.client_request_id),
            try AppTaskActionAnswerBody(runID: body.run_id, questionID: body.question_id,
                expectedRevision: body.expected_revision, expectedDigest: body.expected_digest,
                answer: body.answer, requestID: "different-request")]
        for changed in changedBodies { XCTAssertThrowsError(try hash(compact, changed)) }
        XCTAssertThrowsError(try hash(compact, body, now: compact.expires_at))
        XCTAssertThrowsError(try hash(compact, body, now: compact.expires_at - 301))
        let duplicate = "{\"nonce\":\"duplicate\"," + String(decoding: raw, as: UTF8.self).dropFirst()
        let bad = AppTaskActionAnswerChallenge(nonce: spaced.nonce, requestDigest: body.requestDigest,
            expiresAt: spaced.expires_at, bindingB64: Data(duplicate.utf8).base64EncodedString())
        XCTAssertThrowsError(try hash(bad, body))
    }

    @MainActor
    func testLostResponseRetriesSameAnswerWithFreshNonceAndNoSecondConcurrentDispatch() async throws {
        let body = try body(), attempt = AppTaskActionAnswerAttempt()
        var sent = [AppTaskActionAnswerBody](), signed = [Data](), nestedCalls = 0
        let accepted = try AppTaskActionAnswerAccepted(actionIntent: ActionIntentSnapshot(status: .interpreting))
        for index in 0...1 {
            do {
                _ = try await attempt.send(body: body, coreID: "core", deviceID: "phone", appAttestKeyID: keyID,
                    challenge: { task in
                        do {
                            _ = try await attempt.send(body: task, coreID: "core", deviceID: "phone", appAttestKeyID: self.keyID,
                                challenge: { nestedCalls += 1; return try self.challenge($0) }, sign: { _ in Data([1]) },
                                submit: { _, _ in accepted }, now: { 1_999_999_990 })
                            XCTFail("Concurrent answer was not rejected")
                        } catch { XCTAssertEqual(error as? TaskStartError, .alreadySending) }
                        return try self.challenge(task, nonce: String(repeating: index == 0 ? "aa" : "bb", count: 32))
                    }, sign: { signed.append($0); return Data([1]) },
                    submit: { task, proof in
                        sent.append(task); XCTAssertEqual(proof.assertion_b64, "AQ==")
                        if index == 0 { throw URLError(.networkConnectionLost) }
                        return accepted
                    }, now: { 1_999_999_990 })
                XCTAssertEqual(index, 1)
            } catch { XCTAssertEqual(index, 0) }
            XCTAssertFalse(attempt.isSending)
        }
        XCTAssertEqual(sent, [body, body]); XCTAssertEqual(nestedCalls, 0)
        XCTAssertEqual(signed.count, 2); XCTAssertNotEqual(signed[0], signed[1])
    }

    @MainActor
    func testExpiredDuringSigningNeverSubmitsAndRejectedChallengeNeverSigns() async throws {
        let body = try body(), attempt = AppTaskActionAnswerAttempt()
        var signed = 0, sent = 0, now = 1_999_999_990.0
        for wrongPurpose in [true, false] {
            do {
                _ = try await attempt.send(body: body, coreID: "core", deviceID: "phone", appAttestKeyID: keyID,
                    challenge: { try self.challenge($0, type: wrongPurpose ? "app_task_start_binding" : "app_action_answer_binding") },
                    sign: { _ in signed += 1; now = 2_000_000_000; return Data([1]) },
                    submit: { _, _ in sent += 1; return try AppTaskActionAnswerAccepted(actionIntent: ActionIntentSnapshot(status: .interpreting)) },
                    now: { now })
                XCTFail("Invalid/expired answer was submitted")
            } catch { XCTAssertEqual(error as? TaskStartError, wrongPurpose ? .invalidChallenge : .expired) }
        }
        XCTAssertEqual(signed, 1); XCTAssertEqual(sent, 0); XCTAssertFalse(attempt.isSending)
    }

    func testOnlySuccessfulAnswerResponseIsAccepted() throws {
        let data = Data(#"{"action_intent":{"status":"interpreting","question":null}}"#.utf8)
        XCTAssertEqual(try AppTaskActionAnswerAccepted.response(data: data, status: 200).action_intent.status, .interpreting)
        for status in [201, 202, 401, 409, 500] { XCTAssertThrowsError(try AppTaskActionAnswerAccepted.response(data: data, status: status)) }
        XCTAssertThrowsError(try AppTaskActionAnswerAccepted.response(data: Data("{}".utf8), status: 200))
    }
}
