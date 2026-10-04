import Foundation
import XCTest
@testable import SolvioApprovalsKit

final class TaskActionIntentTests: XCTestCase {
    private let objective = "Lege morgen den Termin „Fahrrad 🚲“ von 9 bis 10 Uhr an."
    private let intent = AppTaskActionIntent()

    private func body(objective: String? = nil) throws -> AppTaskBody {
        try AppTaskBody(scope: "action", objective: objective ?? self.objective, targetRepo: "",
                        requestID: "request-natural-001", actionIntent: intent)
    }
    private func exactRequest() throws -> AppTaskActionRequest {
        try AppTaskActionRequest(actions: [AppTaskAction(actionID: "draft1", service: "gmail",
            operation: "compose_draft", account: "mail-account", target: ["mailbox": "me", "to": "friend@example.invalid"],
            payload: ["instruction": .string("Bitte freundlich nach dem Termin fragen.")])])
    }

    func testNaturalBodyHasOnlyVersionMarkerAndUnchangedUnicodeObjective() throws {
        let body = try body()
        let expected = #"{"action_intent":{"version":1},"client_request_id":"request-natural-001","objective":"Lege morgen den Termin „Fahrrad 🚲“ von 9 bis 10 Uhr an.","scope":"action","target_repo":""}"#
        XCTAssertEqual(String(decoding: body.canonicalBytes(), as: UTF8.self), expected)
        // Real Python Core canonical_task_body + request_digest, 2026-09-12.
        XCTAssertEqual(body.requestDigest, "eba73e9d66bc2261aa38e2e15066d39aac2d4d416653d43e0ad78e6ff8df0669")
        let data = try JSONEncoder().encode(body)
        XCTAssertEqual(try JSONDecoder().decode(AppTaskBody.self, from: data), body)
        let raw = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertEqual(Set(raw.keys), ["scope", "objective", "target_repo", "client_request_id", "action_intent"])
        XCTAssertEqual(raw["objective"] as? String, objective)
        XCTAssertEqual(try XCTUnwrap(raw["action_intent"] as? [String: Int]), ["version": 1])
    }

    func testMarkerRejectsOtherVersionsTypesMissingAndExtraFields() throws {
        for marker in [#"{}"#, #"{"version":0}"#, #"{"version":2}"#, #"{"version":-1}"#,
                       #"{"version":1.5}"#, #"{"version":true}"#, #"{"version":"1"}"#,
                       #"{"version":null}"#, #"{"version":1,"account":"unbound"}"#, #"[]"#] {
            XCTAssertThrowsError(try JSONDecoder().decode(AppTaskActionIntent.self, from: Data(marker.utf8)), marker)
        }
    }

    func testActionRequiresExactlyOneRouteAndOtherScopesRejectIntent() throws {
        let request = try exactRequest()
        for scope in ["research", "build"] {
            XCTAssertThrowsError(try AppTaskBody(scope: scope, objective: objective, targetRepo: "",
                requestID: "request-natural-001", actionIntent: intent))
            let old = try AppTaskBody(scope: scope, objective: objective, targetRepo: "", requestID: "request-old-001")
            XCTAssertNil(old.action_intent)
            XCTAssertFalse(String(decoding: old.canonicalBytes(), as: UTF8.self).contains("action_intent"))
        }
        XCTAssertThrowsError(try AppTaskBody(scope: "action", objective: objective, targetRepo: "",
            requestID: "request-natural-001"))
        XCTAssertThrowsError(try AppTaskBody(scope: "action", objective: objective, targetRepo: "",
            requestID: "request-natural-001", actionRequest: request, actionIntent: intent))
        XCTAssertThrowsError(try AppTaskBody(scope: "action", objective: objective, targetRepo: "repo",
            requestID: "request-natural-001", actionIntent: intent))
        let document = try AppTaskDocumentRequest(format: "txt", content: Data("Testdokument".utf8))
        XCTAssertThrowsError(try AppTaskBody(scope: "action", objective: objective, targetRepo: "",
            requestID: "request-natural-001", documentRequest: document, actionIntent: intent))
        let exact = try AppTaskBody(scope: "action", objective: objective, targetRepo: "",
            requestID: "request-natural-001", actionRequest: request)
        XCTAssertNil(exact.action_intent)
    }

    func testDecodedBodyRejectsNullMixedRoutesAndForeignTopLevelFields() throws {
        let original = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(body())) as? [String: Any])
        for change in ["null", "mixed", "scope", "extra", "missing"] {
            var raw = original
            if change == "null" { raw["action_intent"] = NSNull() }
            if change == "mixed" { raw["action_request"] = try JSONSerialization.jsonObject(with: JSONEncoder().encode(exactRequest())) }
            if change == "scope" { raw["scope"] = "research" }
            if change == "extra" { raw["grant"] = true }
            if change == "missing" { raw.removeValue(forKey: "action_intent") }
            XCTAssertThrowsError(try JSONDecoder().decode(AppTaskBody.self, from: JSONSerialization.data(withJSONObject: raw)), change)
        }
    }

    func testRestartRestoresOnlyOriginalIntentObjectiveCoreAndDevice() throws {
        let first = try body(), retry = TaskStartRetryBinding(body: first, coreID: "core", deviceID: "phone")
        let saved = try JSONEncoder().encode(retry)
        let keys = try XCTUnwrap(JSONSerialization.jsonObject(with: saved) as? [String: Any])
        XCTAssertEqual(Set(keys.keys), ["coreID", "deviceID", "requestID", "bodyDigest"])
        XCTAssertFalse(String(decoding: saved, as: UTF8.self).contains(objective))
        let restored = try JSONDecoder().decode(TaskStartRetryBinding.self, from: saved)
        var restarted = TaskStartDraft()
        XCTAssertEqual(try restarted.prepare(scope: "action", objective: objective, targetRepo: "", retry: restored,
            coreID: "core", deviceID: "phone", actionIntent: intent), first)
        for (core, phone, text) in [("other", "phone", objective), ("core", "other", objective), ("core", "phone", objective + "!")] {
            XCTAssertNil(restored.restore(scope: "action", objective: text, targetRepo: "", coreID: core,
                deviceID: phone, actionIntent: intent))
        }
        XCTAssertNil(restored.restore(scope: "action", objective: objective, targetRepo: "", coreID: "core", deviceID: "phone"))
        XCTAssertNil(restored.restore(scope: "action", objective: objective, targetRepo: "", coreID: "core",
            deviceID: "phone", actionRequest: try exactRequest()))
    }

    func testChangingBetweenNaturalAndExactOrEditingObjectiveNeverReusesID() throws {
        var draft = TaskStartDraft()
        let first = try draft.prepare(scope: "action", objective: objective, targetRepo: "", actionIntent: intent)
        XCTAssertEqual(try draft.prepare(scope: "action", objective: objective, targetRepo: "", actionIntent: intent), first)
        let exact = try draft.prepare(scope: "action", objective: objective, targetRepo: "", actionRequest: exactRequest())
        XCTAssertNotEqual(exact.client_request_id, first.client_request_id)
        let natural = try draft.prepare(scope: "action", objective: objective, targetRepo: "", actionIntent: intent)
        XCTAssertNotEqual(natural.client_request_id, exact.client_request_id)
        XCTAssertNotEqual(natural.client_request_id, first.client_request_id)
        let edited = try draft.prepare(scope: "action", objective: objective + "!", targetRepo: "", actionIntent: intent)
        XCTAssertNotEqual(edited.client_request_id, natural.client_request_id)
        let exactRetry = TaskStartRetryBinding(body: exact, coreID: "core", deviceID: "phone")
        XCTAssertNil(exactRetry.restore(scope: "action", objective: objective, targetRepo: "", coreID: "core",
            deviceID: "phone", actionIntent: intent))
    }

    @MainActor
    func testNaturalRetrySignsFreshNonceForSameBodyAndRejectsChangedObjectiveOrRoute() async throws {
        let body = try body(), key = Data(repeating: 1, count: 32).base64EncodedString()
        func challenge(_ task: AppTaskBody, nonce: String) throws -> AppTaskChallenge {
            let binding: [String: Any] = ["protocol_version": 1, "type": "app_task_start_binding",
                "core_instance_id": "core", "principal_id": "owner", "device_id": "phone", "nonce": nonce,
                "request_digest": task.requestDigest, "enrollment_id": "enrollment", "app_attest_key_id": key,
                "approval_key_sha256": String(repeating: "a", count: 64)]
            let raw = try JSONSerialization.data(withJSONObject: binding, options: [.sortedKeys, .withoutEscapingSlashes])
            return AppTaskChallenge(nonce: nonce, requestDigest: task.requestDigest, expiresAt: 2_000_000_000,
                                    bindingB64: raw.base64EncodedString())
        }
        let original = try challenge(body, nonce: String(repeating: "aa", count: 32))
        let exact = try AppTaskBody(scope: "action", objective: objective, targetRepo: "",
            requestID: body.client_request_id, actionRequest: exactRequest())
        for changed in [try self.body(objective: objective + "!"), exact] {
            XCTAssertThrowsError(try original.clientDataHash(body: changed, coreID: "core", deviceID: "phone",
                appAttestKeyID: key, now: 1_999_999_990))
        }
        let attempt = TaskStartAttempt()
        var sent = [AppTaskBody](), signatures = [Data]()
        for index in 0...1 {
            do {
                _ = try await attempt.send(body: body, coreID: "core", deviceID: "phone", appAttestKeyID: key,
                    challenge: { try challenge($0, nonce: String(repeating: index == 0 ? "aa" : "bb", count: 32)) },
                    sign: { signatures.append($0); return Data([1]) },
                    submit: { task, _ in
                        sent.append(task)
                        if index == 0 { throw URLError(.networkConnectionLost) }
                        return AppTaskAccepted(taskID: "at-existing", runID: "ar-existing", state: "CREATED", acceptance: "preparing")
                    }, now: { 1_999_999_990 })
                XCTAssertEqual(index, 1)
            } catch { XCTAssertEqual(index, 0) }
        }
        XCTAssertEqual(sent, [body, body]); XCTAssertEqual(signatures.count, 2)
        XCTAssertNotEqual(signatures[0], signatures[1])
    }
}
