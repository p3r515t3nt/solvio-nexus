import Foundation
import XCTest
@testable import SolvioApprovalsKit

/// Golden vectors from Tests/conversation_message_vector.py — the same file the
/// Core test reproduces. Every hex value here is compared byte for byte.
@MainActor
final class ConversationMessageTests: XCTestCase {
    private func vector() throws -> [String: Any] {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "app_conversation_message_v1", withExtension: "json"))
        return try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any])
    }
    private func message(_ name: String) throws -> (AppConversationMessageBody, [String: Any]) {
        let entry = try XCTUnwrap((try vector()["messages"] as? [String: Any])?[name] as? [String: Any])
        let raw = try JSONSerialization.data(withJSONObject: XCTUnwrap(entry["message"]))
        return (try JSONDecoder().decode(AppConversationMessageBody.self, from: raw), entry)
    }
    private func hex(_ data: Data) -> String { data.map { String(format: "%02x", $0) }.joined() }
    private var keyID: String { Data(repeating: 1, count: 32).base64EncodedString() }
    private func challenge(_ body: AppConversationMessageBody, nonce: String = String(repeating: "cd", count: 32),
                           mutate: ((inout [String: Any]) -> Void)? = nil) throws -> AppConversationMessageChallenge {
        var binding = try XCTUnwrap(vector()["binding"] as? [String: Any])
        binding["nonce"] = nonce; binding["request_digest"] = body.requestDigest
        mutate?(&binding)
        let raw = try JSONSerialization.data(withJSONObject: binding, options: [.sortedKeys, .withoutEscapingSlashes])
        return AppConversationMessageChallenge(nonce: nonce, requestDigest: body.requestDigest, expiresAt: 2000000000,
                                               bindingB64: raw.base64EncodedString())
    }
    private func hash(_ challenge: AppConversationMessageChallenge, body: AppConversationMessageBody,
                      core: String = "core-test-persisted", device: String = "dev-test-iphone",
                      key: String? = nil, now: Double = 1999999990) throws -> Data {
        try challenge.clientDataHash(body: body, coreID: core, deviceID: device, appAttestKeyID: key ?? keyID, now: now)
    }

    func testCanonicalBytesAndDigestsMatchTheCoreVectorForEveryBodyShape() throws {
        for name in ["plain", "target", "document", "files"] {
            let (body, entry) = try message(name)
            XCTAssertEqual(hex(body.canonicalBytes()), entry["canonical_hex"] as? String, name)
            XCTAssertEqual(body.requestDigest, entry["request_digest"] as? String, name)
            // Encoding round trip keeps the exact field set (Codable wire form).
            let wire = try JSONDecoder().decode(AppConversationMessageBody.self, from: JSONEncoder().encode(body))
            XCTAssertEqual(wire, body, name)
            XCTAssertEqual(hex(wire.canonicalBytes()), entry["canonical_hex"] as? String, name)
        }
        let (_, targetEntry) = try message("target"), (_, plainEntry) = try message("plain")
        XCTAssertNotEqual(targetEntry["request_digest"] as? String, plainEntry["request_digest"] as? String)
    }

    func testDomainHashMatchesTheCoreVectorAndNeverAnotherDomain() throws {
        let vector = try vector(), (body, _) = try message("plain")
        let raw = try JSONSerialization.data(withJSONObject: XCTUnwrap(vector["challenge"]))
        let wire = try JSONDecoder().decode(AppConversationMessageChallenge.self, from: raw)
        XCTAssertEqual(hex(try XCTUnwrap(Data(base64Encoded: wire.binding_b64))), vector["binding_hex"] as? String)
        let signed = try hash(wire, body: body)
        XCTAssertEqual(hex(signed), vector["client_data_hash_hex"] as? String)
        let others = try XCTUnwrap(vector["wrong_domain_client_data_hash_hex"] as? [String: String])
        XCTAssertNotEqual(hex(signed), others["task_start"])
        XCTAssertNotEqual(hex(signed), others["voice_session"])
        XCTAssertEqual(AppConversationMessageChallenge.domain, Data("SOLVIO_APP_CONVERSATION_MESSAGE_V1".utf8))
    }

    func testEveryClientKnownBindingIsCheckedBeforeSigning() throws {
        let (body, _) = try message("plain"), wire = try challenge(body)
        XCTAssertNoThrow(try hash(wire, body: body))
        XCTAssertThrowsError(try hash(wire, body: body, core: "core-other"))
        XCTAssertThrowsError(try hash(wire, body: body, device: "other-device"))
        XCTAssertThrowsError(try hash(wire, body: body, key: "different-key"))
        let changed = try AppConversationMessageBody(conversationID: body.conversation_id, clientMessageID: body.client_message_id,
                                                     text: body.text + "!")
        XCTAssertThrowsError(try hash(wire, body: changed))
        let otherChat = try AppConversationMessageBody(conversationID: "c-fedcba9876543210", clientMessageID: body.client_message_id,
                                                       text: body.text)
        XCTAssertThrowsError(try hash(wire, body: otherChat))
        for wrongType in ["app_task_start_binding", "app_task_followup_binding", "voice_session_binding"] {
            let wrong = try challenge(body) { $0["type"] = wrongType }
            XCTAssertThrowsError(try hash(wrong, body: body), wrongType)
        }
        XCTAssertThrowsError(try hash(try challenge(body) { $0["protocol_version"] = 2 }, body: body))
        XCTAssertThrowsError(try hash(try challenge(body) { $0["extra"] = "x" }, body: body))
        XCTAssertThrowsError(try hash(try challenge(body) { $0.removeValue(forKey: "enrollment_id") }, body: body))
        XCTAssertThrowsError(try hash(wire, body: body, now: wire.expires_at))
        XCTAssertThrowsError(try hash(wire, body: body, now: wire.expires_at - 301))
    }

    func testTextRulesFollowTheContract() throws {
        let ok = { (text: String) in
            (try? AppConversationMessageBody(conversationID: "c-0123456789abcdef", clientMessageID: "msg-20260918-0001", text: text)) != nil
        }
        XCTAssertTrue(ok("Danke"), "five characters are a message, not a task")
        XCTAssertTrue(ok("a"))
        XCTAssertTrue(ok(String(repeating: "ä", count: 4000)))
        XCTAssertTrue(ok("Zeile eins\nZeile\tzwei"))
        XCTAssertFalse(ok(""))
        XCTAssertFalse(ok(" führendes Leerzeichen"))
        XCTAssertFalse(ok("nachgestellt\n"))
        XCTAssertFalse(ok(String(repeating: "ä", count: 4001)))
        XCTAssertFalse(ok("nul\u{0}byte"))
        XCTAssertFalse(ok("form\u{c}feed"))
        XCTAssertFalse(ok("file\u{1c}separator"))
        XCTAssertThrowsError(try AppConversationMessageBody(conversationID: "conv-1", clientMessageID: "msg-20260918-0001", text: "Hallo"))
        XCTAssertThrowsError(try AppConversationMessageBody(conversationID: "c-0123456789abcdef", clientMessageID: "short", text: "Hallo"))
        XCTAssertThrowsError(try AppConversationMessageTarget(taskID: "at-0123456789abcdef", runID: "ar-0123456789abcdef", revision: 0))
        XCTAssertThrowsError(try AppConversationMessageTarget(taskID: "task", runID: "ar-0123456789abcdef", revision: 1))
        // Wire decoding is strict: unknown keys, null optionals and a task-shaped body are refused.
        for raw in [#"{"conversation_id":"c-0123456789abcdef","client_message_id":"msg-20260918-0001","text":"Hallo","scope":"research"}"#,
                    #"{"conversation_id":"c-0123456789abcdef","client_message_id":"msg-20260918-0001","text":"Hallo","target":null}"#,
                    #"{"conversation_id":"c-0123456789abcdef","client_message_id":"msg-20260918-0001","text":"Hallo","attachments":{"operation":"other"}}"#,
                    #"{"scope":"research","objective":"Prüfe diesen Auftrag.","target_repo":"","client_request_id":"msg-20260918-0001"}"#] {
            XCTAssertThrowsError(try JSONDecoder().decode(AppConversationMessageBody.self, from: Data(raw.utf8)), raw)
        }
    }

    func testAcceptedResponseIsOnly202WithAnIdentifiedKnownDeliveryState() throws {
        // Core returns the persisted delivery state on replay, not always the
        // initial accepted state. "blocked" confirms delivery, not task success.
        for state in ["accepted", "running", "completed", "blocked"] {
            let good = AppConversationMessageAccepted(deliveryID: "cd-0123456789abcdef", status: state, messageID: "m-0123456789abcdef")
            XCTAssertEqual(try AppConversationMessageAccepted.response(data: JSONEncoder().encode(good), status: 202), good)
            for httpStatus in [200, 201, 401, 500] {
                XCTAssertThrowsError(try AppConversationMessageAccepted.response(data: JSONEncoder().encode(good), status: httpStatus))
            }
        }
        for bad in [AppConversationMessageAccepted(deliveryID: "dl-1", status: "accepted", messageID: "m-0123456789abcdef"),
                    AppConversationMessageAccepted(deliveryID: "cd-0123456789abcdef", status: "accepted", messageID: "")] {
            XCTAssertThrowsError(try AppConversationMessageAccepted.response(data: JSONEncoder().encode(bad), status: 202))
        }
        for state in ["", "succeeded", "failed", "cancelled", "unknown", "Completed"] {
            let bad = AppConversationMessageAccepted(deliveryID: "cd-0123456789abcdef", status: state, messageID: "m-0123456789abcdef")
            XCTAssertThrowsError(try AppConversationMessageAccepted.response(data: JSONEncoder().encode(bad), status: 202))
        }
    }

    func testLostAcknowledgementCanReplayACompletedDeliveryWithFreshProof() async throws {
        let (body, _) = try message("plain"), attempt = ConversationMessageAttempt()
        let retry = ConversationMessageRetryBinding(body: body, coreID: "core-test-persisted", deviceID: "dev-test-iphone")
        var submitted: [(AppConversationMessageBody, AppTaskProof)] = []
        do {
            _ = try await attempt.send(body: body, coreID: retry.coreID, deviceID: retry.deviceID, appAttestKeyID: keyID,
                challenge: { try self.challenge($0) }, sign: { _ in Data([1]) },
                submit: { message, proof in
                    submitted.append((message, proof))
                    throw URLError(.networkConnectionLost)
                }, now: { 1999999990 })
            XCTFail("the lost acknowledgement must not be presented as received")
        } catch { XCTAssertEqual((error as? URLError)?.code, .networkConnectionLost) }
        let restored = try XCTUnwrap(retry.restore(conversationID: body.conversation_id, text: body.text,
                                                   coreID: retry.coreID, deviceID: retry.deviceID))
        let completed = AppConversationMessageAccepted(deliveryID: "cd-0123456789abcdef", status: "completed", messageID: "m-0123456789abcdef")
        let result = try await attempt.send(body: restored, coreID: retry.coreID, deviceID: retry.deviceID, appAttestKeyID: keyID,
            challenge: { try self.challenge($0, nonce: String(repeating: "ef", count: 32)) }, sign: { _ in Data([2]) },
            submit: { message, proof in
                submitted.append((message, proof))
                // Exact Core replay response shape after the first delivery completed.
                return try AppConversationMessageAccepted.response(data: JSONEncoder().encode(completed), status: 202)
            }, now: { 1999999990 })
        XCTAssertEqual(result, completed)
        XCTAssertEqual(submitted.count, 2)
        XCTAssertEqual(submitted[0].0, submitted[1].0)
        XCTAssertEqual(submitted[0].0.requestDigest, submitted[1].0.requestDigest)
        XCTAssertNotEqual(submitted[0].1.nonce, submitted[1].1.nonce)
    }

    func testAttemptSendsExactlyOnceAndRefusesOverlap() async throws {
        let (body, _) = try message("plain"), attempt = ConversationMessageAttempt()
        var challenged = 0, signed: [Data] = [], submitted: [(AppConversationMessageBody, AppTaskProof)] = []
        let accepted = AppConversationMessageAccepted(deliveryID: "cd-0123456789abcdef", status: "accepted", messageID: "m-0123456789abcdef")
        var overlapRefused: ConversationMessageError?
        let result = try await attempt.send(body: body, coreID: "core-test-persisted", deviceID: "dev-test-iphone", appAttestKeyID: keyID,
            challenge: { body in
                challenged += 1
                // A second send while the first still waits for its challenge must be refused.
                do {
                    _ = try await attempt.send(body: body, coreID: "core-test-persisted", deviceID: "dev-test-iphone", appAttestKeyID: self.keyID,
                        challenge: { XCTFail("overlapping challenge"); return try self.challenge($0) }, sign: { _ in Data([1]) },
                        submit: { _, _ in XCTFail("overlapping submit"); return accepted }, now: { 1999999990 })
                } catch { overlapRefused = error as? ConversationMessageError }
                return try self.challenge(body)
            },
            sign: { signed.append($0); return Data([7]) },
            submit: { b, p in submitted.append((b, p)); return accepted }, now: { 1999999990 })
        XCTAssertEqual(result, accepted); XCTAssertEqual(challenged, 1); XCTAssertEqual(submitted.count, 1)
        XCTAssertEqual(overlapRefused, .alreadySending)
        XCTAssertEqual(submitted[0].0, body); XCTAssertEqual(submitted[0].1.nonce, String(repeating: "cd", count: 32))
        XCTAssertEqual(hex(signed[0]), hex(try hash(try challenge(body), body: body)))
        XCTAssertFalse(attempt.isSending)
        // An empty assertion never becomes a proof, and the lock is released afterwards.
        do {
            _ = try await attempt.send(body: body, coreID: "core-test-persisted", deviceID: "dev-test-iphone",
                appAttestKeyID: keyID, challenge: { try self.challenge($0) }, sign: { _ in Data() },
                submit: { _, _ in XCTFail("empty assertion submitted"); return accepted }, now: { 1999999990 })
            XCTFail("empty assertion accepted")
        } catch { XCTAssertEqual(error as? ConversationMessageError, .expired) }
        XCTAssertFalse(attempt.isSending)
        // A stale challenge (expired before signing) is never submitted.
        var clock = 1999999990.0
        do {
            _ = try await attempt.send(body: body, coreID: "core-test-persisted", deviceID: "dev-test-iphone",
                appAttestKeyID: keyID, challenge: { try self.challenge($0) }, sign: { _ in clock = 2000000001; return Data([1]) },
                submit: { _, _ in XCTFail("expired proof submitted"); return accepted }, now: { clock })
            XCTFail("expired proof accepted")
        } catch { XCTAssertEqual(error as? ConversationMessageError, .expired) }
    }
}

/// Retry metadata carries identifiers and digests only; the Core is the record.
final class ConversationRetryBindingTests: XCTestCase {
    private let conversation = "c-0123456789abcdef"
    private func body(_ text: String = "Prüf bitte den Auftrag ❤️", attachments: AppConversationAttachment? = nil) throws -> AppConversationMessageBody {
        try AppConversationMessageBody(conversationID: conversation, clientMessageID: "msg-20260918-0001", text: text, attachments: attachments)
    }

    func testBindingHoldsNoTextAndRestoresOnlyTheSameMessageOnTheSameCoreAndDevice() throws {
        let document = AppConversationAttachment.document(try AppTaskDocumentRequest(format: "txt", content: Data("Geheimer Inhalt\n".utf8)))
        let original = try body(attachments: document)
        let binding = ConversationMessageRetryBinding(body: original, coreID: "core-one", deviceID: "device-one")
        let encoder = JSONEncoder(); encoder.outputFormatting = [.sortedKeys]
        let saved = String(decoding: try encoder.encode(binding), as: UTF8.self)
        XCTAssertFalse(saved.contains("Prüf")); XCTAssertFalse(saved.contains("Geheimer"))
        XCTAssertFalse(saved.contains(Data("Geheimer Inhalt\n".utf8).base64EncodedString()))
        XCTAssertTrue(saved.contains(original.requestDigest)); XCTAssertTrue(saved.contains(conversation))
        XCTAssertEqual(binding.attachmentKind, "document")
        let restored = try JSONDecoder().decode(ConversationMessageRetryBinding.self, from: Data(saved.utf8))
        XCTAssertEqual(restored.restore(conversationID: conversation, text: original.text, attachments: document,
                                        coreID: "core-one", deviceID: "device-one"), original)
        XCTAssertNil(restored.restore(conversationID: conversation, text: original.text, attachments: nil, coreID: "core-one", deviceID: "device-one"))
        XCTAssertNil(restored.restore(conversationID: conversation, text: original.text + "!", attachments: document, coreID: "core-one", deviceID: "device-one"))
        XCTAssertNil(restored.restore(conversationID: "c-fedcba9876543210", text: original.text, attachments: document, coreID: "core-one", deviceID: "device-one"))
        XCTAssertNil(restored.restore(conversationID: conversation, text: original.text, attachments: document, coreID: "core-two", deviceID: "device-one"))
        XCTAssertNil(restored.restore(conversationID: conversation, text: original.text, attachments: document, coreID: "core-one", deviceID: "device-two"))
        let withDelivery = restored.withDelivery("cd-0123456789abcdef")
        XCTAssertEqual(withDelivery.deliveryID, "cd-0123456789abcdef"); XCTAssertEqual(withDelivery.bodyDigest, restored.bodyDigest)
        XCTAssertNil(restored.deliveryID)
    }

    func testTaskBodyWithoutConversationRefStaysByteIdenticalAndWithRefGainsExactlyOneKey() throws {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "app_conversation_message_v1", withExtension: "json"))
        let vector = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any])
        let tasks = try XCTUnwrap(vector["task_body"] as? [String: Any])
        let without = try XCTUnwrap(tasks["without_ref"] as? [String: Any]), with = try XCTUnwrap(tasks["with_ref"] as? [String: Any])
        let base = try JSONDecoder().decode(AppTaskBody.self, from: JSONSerialization.data(withJSONObject: XCTUnwrap(without["task"])))
        XCTAssertNil(base.conversation_ref)
        XCTAssertEqual(base.canonicalBytes().map { String(format: "%02x", $0) }.joined(), without["canonical_task_hex"] as? String)
        XCTAssertEqual(base.requestDigest, without["request_digest"] as? String)
        // The N2 vector digest is the unchanged proof that pre-C3 bodies keep their bytes.
        XCTAssertEqual(base.requestDigest, "9f0be1c35c81d92d8265d9299298a61e265ce78479abae035dd23935dde14886")
        let explicitNil = try AppTaskBody(scope: base.scope, objective: base.objective, targetRepo: base.target_repo,
                                          requestID: base.client_request_id, conversationRef: nil)
        let emptyRef = try AppTaskBody(scope: base.scope, objective: base.objective, targetRepo: base.target_repo,
                                       requestID: base.client_request_id, conversationRef: "")
        XCTAssertEqual(explicitNil.canonicalBytes(), base.canonicalBytes()); XCTAssertEqual(emptyRef.canonicalBytes(), base.canonicalBytes())
        XCTAssertFalse(String(decoding: try JSONEncoder().encode(emptyRef), as: UTF8.self).contains("conversation_ref"))
        let bound = try JSONDecoder().decode(AppTaskBody.self, from: JSONSerialization.data(withJSONObject: XCTUnwrap(with["task"])))
        XCTAssertEqual(bound.conversation_ref, conversation)
        XCTAssertEqual(bound.canonicalBytes().map { String(format: "%02x", $0) }.joined(), with["canonical_task_hex"] as? String)
        XCTAssertEqual(bound.requestDigest, with["request_digest"] as? String)
        XCTAssertNotEqual(bound.requestDigest, base.requestDigest)
        XCTAssertThrowsError(try AppTaskBody(scope: base.scope, objective: base.objective, targetRepo: base.target_repo,
                                             requestID: base.client_request_id, conversationRef: "chat-1"))
        XCTAssertThrowsError(try JSONDecoder().decode(AppTaskBody.self, from: Data(
            #"{"scope":"research","objective":"Prüfe diesen Auftrag.","target_repo":"","client_request_id":"request-20260910","conversation_ref":null}"#.utf8)))
        // Retry metadata binds the ref through the digest: another chat is another request.
        let metadata = TaskStartRetryBinding(body: bound, coreID: "core-one", deviceID: "device-one")
        XCTAssertEqual(metadata.restore(scope: bound.scope, objective: bound.objective, targetRepo: bound.target_repo,
                                        coreID: "core-one", deviceID: "device-one", conversationRef: conversation), bound)
        XCTAssertNil(metadata.restore(scope: bound.scope, objective: bound.objective, targetRepo: bound.target_repo,
                                      coreID: "core-one", deviceID: "device-one"))
        XCTAssertNil(metadata.restore(scope: bound.scope, objective: bound.objective, targetRepo: bound.target_repo,
                                      coreID: "core-one", deviceID: "device-one", conversationRef: "c-fedcba9876543210"))
        var draft = TaskStartDraft()
        let prepared = try draft.prepare(scope: bound.scope, objective: bound.objective, targetRepo: bound.target_repo,
                                         retry: metadata, coreID: "core-one", deviceID: "device-one", conversationRef: conversation)
        XCTAssertEqual(prepared, bound)
        XCTAssertNotEqual(try draft.prepare(scope: bound.scope, objective: bound.objective, targetRepo: bound.target_repo,
                                            retry: metadata, coreID: "core-one", deviceID: "device-one").client_request_id, bound.client_request_id)
    }

    func testVoiceSessionBindingAddsConversationOnlyWhenBound() throws {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "app_conversation_message_v1", withExtension: "json"))
        let vector = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any])
        let voice = try XCTUnwrap(vector["voice_session_binding"] as? [String: Any])
        let plain = try XCTUnwrap(voice["without_conversation"] as? [String: String]), bound = try XCTUnwrap(voice["with_conversation"] as? [String: String])
        let hex = { (data: Data) in data.map { String(format: "%02x", $0) }.joined() }
        let legacy = VoiceSessionBinding(coreInstanceID: "core-test-persisted", deviceID: "dev-test-iphone", sessionNonce: String(repeating: "ab", count: 32))
        XCTAssertEqual(hex(legacy.canonicalBytes()), plain["canonical_hex"])
        XCTAssertEqual(hex(legacy.clientDataHash()), plain["client_data_hash_hex"])
        let empty = VoiceSessionBinding(coreInstanceID: "core-test-persisted", deviceID: "dev-test-iphone", sessionNonce: String(repeating: "ab", count: 32), conversationID: "")
        XCTAssertEqual(empty.canonicalBytes(), legacy.canonicalBytes())
        let withChat = VoiceSessionBinding(coreInstanceID: "core-test-persisted", deviceID: "dev-test-iphone",
                                           sessionNonce: String(repeating: "ab", count: 32), conversationID: conversation)
        XCTAssertEqual(hex(withChat.canonicalBytes()), bound["canonical_hex"])
        XCTAssertEqual(hex(withChat.clientDataHash()), bound["client_data_hash_hex"])
        XCTAssertNotEqual(withChat.clientDataHash(), legacy.clientDataHash())
    }
}
