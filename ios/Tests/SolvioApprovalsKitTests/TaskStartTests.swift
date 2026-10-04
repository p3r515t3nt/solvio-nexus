import Foundation
import XCTest
@testable import SolvioApprovalsKit

final class TaskStartTests: XCTestCase {
    private func vector() throws -> [String: Any] {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "app_task_start_v1", withExtension: "json"))
        return try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any])
    }
    private func body() throws -> AppTaskBody {
        let raw = try JSONSerialization.data(withJSONObject: XCTUnwrap(vector()["task"]))
        return try JSONDecoder().decode(AppTaskBody.self, from: raw)
    }
    private func challenge(_ body: AppTaskBody, nonce: String = String(repeating: "ab", count: 32)) throws -> AppTaskChallenge {
        var binding = try XCTUnwrap(vector()["binding"] as? [String: Any])
        binding["nonce"] = nonce; binding["request_digest"] = body.requestDigest
        let raw = try JSONSerialization.data(withJSONObject: binding, options: [.sortedKeys, .withoutEscapingSlashes])
        return AppTaskChallenge(nonce: nonce, requestDigest: body.requestDigest, expiresAt: 2000000000,
                                bindingB64: raw.base64EncodedString())
    }
    private var keyID: String { Data(repeating: 1, count: 32).base64EncodedString() }
    private func hash(_ challenge: AppTaskChallenge, body: AppTaskBody,
                       core: String = "core-test-persisted", device: String = "dev-test-iphone",
                       key: String? = nil, now: Double = 1999999990) throws -> Data {
        try challenge.clientDataHash(body: body, coreID: core, deviceID: device,
                                     appAttestKeyID: key ?? keyID, now: now)
    }
    private func hex(_ data: Data) -> String { data.map { String(format: "%02x", $0) }.joined() }

    func testCanonicalTaskAndDomainHashMatchRealPythonCoreVector() throws {
        let vector = try vector(), body = try body()
        let raw = try JSONSerialization.data(withJSONObject: XCTUnwrap(vector["challenge"]))
        let wire = try JSONDecoder().decode(AppTaskChallenge.self, from: raw)
        XCTAssertEqual(hex(body.canonicalBytes()), vector["canonical_task_hex"] as? String)
        XCTAssertEqual(body.requestDigest, vector["request_digest"] as? String)
        XCTAssertEqual(hex(try hash(wire, body: body)), vector["client_data_hash_hex"] as? String)
        XCTAssertEqual(hex(try XCTUnwrap(Data(base64Encoded: wire.binding_b64))), vector["binding_hex"] as? String)
    }

    func testEveryClientKnownBindingIsCheckedBeforeSigning() throws {
        let body = try body(), wire = try challenge(body)
        XCTAssertThrowsError(try hash(wire, body: body, core: "core-other"))
        XCTAssertThrowsError(try hash(wire, body: body, device: "other-device"))
        XCTAssertThrowsError(try hash(wire, body: body, key: "different-key"))
        for changed in [try AppTaskBody(scope: "research", objective: body.objective, targetRepo: "", requestID: body.client_request_id),
                        try AppTaskBody(scope: "build", objective: body.objective + "!", targetRepo: body.target_repo, requestID: body.client_request_id),
                        try AppTaskBody(scope: "build", objective: body.objective, targetRepo: "other", requestID: body.client_request_id),
                        try AppTaskBody(scope: "build", objective: body.objective, targetRepo: body.target_repo, requestID: "request-other") ] {
            XCTAssertThrowsError(try hash(wire, body: changed))
        }
    }

    func testDuplicateAndWrongPurposeBindingNeverSign() throws {
        let body = try body(), wire = try challenge(body)
        let raw = try XCTUnwrap(Data(base64Encoded: wire.binding_b64))
        let text = try XCTUnwrap(String(data: raw, encoding: .utf8))
        for mutated in [text.replacingOccurrences(of: "app_task_start_binding", with: "voice_session_binding"),
                        text.replacingOccurrences(of: "\"protocol_version\":1", with: "\"protocol_version\":2"),
                        "{\"nonce\":\"duplicate\"," + text.dropFirst()] {
            let wrong = AppTaskChallenge(nonce: wire.nonce, requestDigest: wire.request_digest,
                expiresAt: wire.expires_at, bindingB64: Data(mutated.utf8).base64EncodedString())
            XCTAssertThrowsError(try hash(wrong, body: body))
        }
        let signed = try hash(wire, body: body)
        XCTAssertNotEqual(signed, AppAttestBinding.domainHash(VoiceSessionBinding.domain, raw))
        XCTAssertNotEqual(signed, AppAttestBinding.domainHash(AppAttestBinding.decisionDomain, raw))
    }

    func testExpiryAndUnboundedServerLifetimeAreRejected() throws {
        let body = try body(), wire = try challenge(body)
        XCTAssertThrowsError(try hash(wire, body: body, now: wire.expires_at))
        XCTAssertThrowsError(try hash(wire, body: body, now: wire.expires_at - 301))
    }

    func testDraftRetainsIDOnRetryAndChangesItAfterEditing() throws {
        var draft = TaskStartDraft()
        let first = try draft.prepare(scope: "research", objective: "Prüfe diesen Auftrag vollständig.", targetRepo: "")
        XCTAssertEqual(first, try draft.prepare(scope: first.scope, objective: first.objective, targetRepo: ""))
        draft.invalidate()
        let edited = try draft.prepare(scope: "research", objective: first.objective + "!", targetRepo: "")
        XCTAssertNotEqual(first.client_request_id, edited.client_request_id)
        let changedRepo = try draft.prepare(scope: "build", objective: edited.objective, targetRepo: "repo")
        XCTAssertNotEqual(edited.client_request_id, changedRepo.client_request_id)
    }

    func testRestartRestoresOnlyMatchingCoreDeviceAndBodyFromMetadata() throws {
        var original = TaskStartDraft()
        let first = try original.prepare(scope: "build", objective: "Untersuche dieses private Beispielprojekt.", targetRepo: "private-repository")
        let metadata = TaskStartRetryBinding(body: first, coreID: "core-one", deviceID: "device-one")
        let saved = try JSONEncoder().encode(metadata)
        let text = String(decoding: saved, as: UTF8.self)
        XCTAssertFalse(text.contains(first.objective))
        XCTAssertFalse(text.contains(first.target_repo))
        let restored = try JSONDecoder().decode(TaskStartRetryBinding.self, from: saved)
        var restarted = TaskStartDraft()
        let retry = try restarted.prepare(scope: first.scope, objective: first.objective, targetRepo: first.target_repo,
                                          retry: restored, coreID: "core-one", deviceID: "device-one")
        XCTAssertEqual(retry, first)
        for (core, device, objective) in [("other-core", "device-one", first.objective),
                                          ("core-one", "other-device", first.objective),
                                          ("core-one", "device-one", first.objective + "!")] {
            var next = TaskStartDraft()
            let body = try next.prepare(scope: first.scope, objective: objective, targetRepo: first.target_repo,
                                        retry: restored, coreID: core, deviceID: device)
            XCTAssertNotEqual(body.client_request_id, first.client_request_id)
        }
        var deliberatelyNew = TaskStartDraft() // accepted/explicitly new: no pending metadata supplied
        XCTAssertNotEqual(try deliberatelyNew.prepare(scope: first.scope, objective: first.objective,
                                                     targetRepo: first.target_repo).client_request_id, first.client_request_id)
    }

    func testReady201AndPreparing202BothConfirmAnIdentifiedTask() throws {
        let ready = AppTaskAccepted(taskID: "at-ready", runID: "ar-ready", state: "angenommen", acceptance: "ready")
        let preparing = AppTaskAccepted(taskID: "at-preparing", runID: "ar-preparing", state: "vorbereitet", acceptance: "preparing")
        XCTAssertEqual(try AppTaskAccepted.response(data: JSONEncoder().encode(ready), status: 201), ready)
        XCTAssertEqual(try AppTaskAccepted.response(data: JSONEncoder().encode(preparing), status: 202), preparing)
        XCTAssertThrowsError(try AppTaskAccepted.response(data: JSONEncoder().encode(ready), status: 202))
        XCTAssertThrowsError(try AppTaskAccepted.response(data: JSONEncoder().encode(preparing), status: 503))
    }

    func testLocalValidationMatchesBackendBoundariesAndUnicodeScalarLength() throws {
        for count in [11, 2001] {
            XCTAssertThrowsError(try AppTaskBody(scope: "research", objective: String(repeating: "x", count: count),
                                               targetRepo: "", requestID: "request-id"))
        }
        // 1001 graphemes but 2002 Python characters must fail the backend's 2000 cap.
        XCTAssertThrowsError(try AppTaskBody(scope: "research", objective: String(repeating: "e\u{301}", count: 1001),
                                           targetRepo: "", requestID: "request-id"))
        XCTAssertThrowsError(try AppTaskBody(scope: "research", objective: "A sufficiently long task", targetRepo: "ignored", requestID: "request-id"))
        XCTAssertThrowsError(try AppTaskBody(scope: "build", objective: " A sufficiently long task", targetRepo: "", requestID: "request-id"))
        XCTAssertThrowsError(try AppTaskBody(scope: "build", objective: "A sufficiently long task", targetRepo: "", requestID: "short"))
        XCTAssertThrowsError(try AppTaskBody(scope: "build", objective: "A sufficiently long task", targetRepo: "", requestID: "request-id\n"))
    }

    @MainActor
    func testLostResponseRetryUsesSameTaskButFreshNonceAndAssertion() async throws {
        var draft = TaskStartDraft()
        let body = try draft.prepare(scope: "research", objective: "Vergleiche die Angebote mit Quellen.", targetRepo: "")
        let attempt = TaskStartAttempt()
        var issued = 0, signed = [Data](), sent = [AppTaskBody](), nonces = [String]()
        for number in 0...1 {
            do {
                _ = try await attempt.send(body: body, coreID: "core-test-persisted", deviceID: "dev-test-iphone",
                    appAttestKeyID: keyID, challenge: { requested in
                        issued += 1
                        return try self.challenge(requested, nonce: String(repeating: number == 0 ? "aa" : "bb", count: 32))
                    }, sign: { data in signed.append(data); return Data([1, 2, 3]) }, submit: { task, proof in
                        sent.append(task); nonces.append(proof.nonce)
                        if number == 0 { throw URLError(.networkConnectionLost) }
                        return AppTaskAccepted(taskID: "at-test", runID: "ar-test", state: "angenommen")
                    }, now: { 1999999990 })
                XCTAssertEqual(number, 1)
            } catch {
                XCTAssertEqual(number, 0)
            }
        }
        XCTAssertEqual(issued, 2)
        XCTAssertEqual(sent, [body, body])
        XCTAssertNotEqual(nonces[0], nonces[1])
        XCTAssertNotEqual(signed[0], signed[1])
    }

    @MainActor
    func testExpiryWhileGeneratingAssertionNeverSubmits() async throws {
        let body = try body(), attempt = TaskStartAttempt()
        var now = 1999999990.0, submitted = 0
        do {
            _ = try await attempt.send(body: body, coreID: "core-test-persisted", deviceID: "dev-test-iphone",
                appAttestKeyID: keyID, challenge: { try self.challenge($0) },
                sign: { _ in now = 2000000000; return Data([1]) },
                submit: { _, _ in submitted += 1; return AppTaskAccepted(taskID: "bad", runID: "bad", state: "bad") },
                now: { now })
            XCTFail("expired proof submitted")
        } catch { XCTAssertEqual(error as? TaskStartError, .expired) }
        XCTAssertEqual(submitted, 0)
    }

    @MainActor
    func testDuplicateTapCannotStartAnotherHandshake() async throws {
        let body = try body(), attempt = TaskStartAttempt()
        var issued = 0
        _ = try await attempt.send(body: body, coreID: "core-test-persisted", deviceID: "dev-test-iphone",
            appAttestKeyID: keyID, challenge: { requested in
                issued += 1
                do {
                    _ = try await attempt.send(body: body, coreID: "core-test-persisted", deviceID: "dev-test-iphone",
                        appAttestKeyID: self.keyID, challenge: { _ in XCTFail("second handshake"); return try self.challenge(body) },
                        sign: { _ in Data([1]) }, submit: { _, _ in AppTaskAccepted(taskID: "bad", runID: "bad", state: "bad") },
                        now: { 1999999990 })
                    XCTFail("duplicate tap accepted")
                } catch { XCTAssertEqual(error as? TaskStartError, .alreadySending) }
                return try self.challenge(requested)
            }, sign: { _ in Data([1]) },
            submit: { _, _ in AppTaskAccepted(taskID: "at-test", runID: "ar-test", state: "angenommen") },
            now: { 1999999990 })
        XCTAssertEqual(issued, 1)
        XCTAssertFalse(attempt.isSending)
    }
}
