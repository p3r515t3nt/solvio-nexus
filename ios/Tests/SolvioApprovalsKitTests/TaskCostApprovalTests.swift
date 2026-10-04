import Foundation
import XCTest
@testable import SolvioApprovalsKit

final class TaskCostApprovalTests: XCTestCase {
    private let task = "at-1111111111111111"
    private func body(_ cents: Int = 2000, requestID: String = "cost-golden-001") throws -> AppTaskCostApprovalBody {
        try .init(taskID: task, maxTotalCents: cents, requestID: requestID)
    }
    private func challenge(_ body: AppTaskCostApprovalBody, changes: [String: Any] = [:]) throws -> AppTaskCostApprovalChallenge {
        var fields: [String: Any] = ["protocol_version": 1, "type": "app_task_cost_approval_binding",
            "core_instance_id": "core-fixture", "principal_id": "owner-fixture", "device_id": "device-fixture",
            "nonce": String(repeating: "b", count: 64), "request_digest": body.requestDigest,
            "enrollment_id": "enroll-fixture", "app_attest_key_id": "key-fixture", "approval_key_sha256": String(repeating: "c", count: 64)]
        fields.merge(changes) { _, new in new }
        return .init(nonce: String(repeating: "b", count: 64), requestDigest: body.requestDigest,
            expiresAt: 2_000_000_000, bindingB64: try JSONSerialization.data(withJSONObject: fields, options: [.sortedKeys]).base64EncodedString())
    }
    private func hash(_ issued: AppTaskCostApprovalChallenge, _ body: AppTaskCostApprovalBody,
                      now: Double = 1_999_999_990) throws -> Data {
        try issued.clientDataHash(body: body, coreID: "core-fixture", deviceID: "device-fixture", appAttestKeyID: "key-fixture", now: now)
    }
    private func accepted(_ body: AppTaskCostApprovalBody, cap: Int = 2000, changes: [String: Any] = [:]) throws -> AppTaskCostApprovalAccepted {
        var raw: [String: Any] = ["task_id": body.task_id, "client_request_id": body.client_request_id,
            "max_total_cents": body.max_total_cents, "costs": ["configured": true, "task_id": body.task_id, "currency": "EUR", "approved_ai_cap_cents": cap]]
        raw.merge(changes) { _, new in new }
        return try JSONDecoder().decode(AppTaskCostApprovalAccepted.self, from: JSONSerialization.data(withJSONObject: raw))
    }
    func testCoreGoldenFramingAndExactCanonicalAmount() throws {
        let b = try body(), issued = try challenge(b)
        XCTAssertEqual(String(decoding: b.canonicalBytes(), as: UTF8.self),
            #"{"client_request_id":"cost-golden-001","max_total_cents":2000,"task_id":"at-1111111111111111"}"#)
        XCTAssertEqual(b.requestDigest, "5a4481cc18e9c17775c5eae5c5f1f76938049782065612c93a7c0183b0652de0")
        XCTAssertEqual(try hash(issued, b).map { String(format: "%02x", $0) }.joined(),
            "528afa01cf05240f32e8bced59b075096f7976f67e3f1dfbd40cfb86e27bbf20")
        let raw = try XCTUnwrap(Data(base64Encoded: issued.binding_b64))
        XCTAssertNotEqual(try hash(issued, b), AppAttestBinding.domainHash(Data("SOLVIO_APP_TASK_FOLLOWUP_V1".utf8), raw))
        XCTAssertEqual(try JSONDecoder().decode(AppTaskCostApprovalBody.self, from: JSONEncoder().encode(b)), b)
    }
    func testClosedBodyAndExactIntegerLimits() throws {
        let b = try body()
        let raw = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(b)) as? [String: Any])
        for (key, value) in [("task_id", "../x" as Any), ("task_id", task + "\n"), ("max_total_cents", true),
                            ("max_total_cents", -1), ("max_total_cents", 1_000_000_001), ("max_total_cents", 20.01),
                            ("max_total_cents", "2000"), ("client_request_id", "short"), ("run_id", "extra")] {
            var changed = raw; changed[key] = value
            XCTAssertThrowsError(try JSONDecoder().decode(AppTaskCostApprovalBody.self, from: JSONSerialization.data(withJSONObject: changed)), key)
        }
        for key in raw.keys {
            var changed = raw; changed.removeValue(forKey: key)
            XCTAssertThrowsError(try JSONDecoder().decode(AppTaskCostApprovalBody.self, from: JSONSerialization.data(withJSONObject: changed)))
        }
        XCTAssertNoThrow(try body(0)); XCTAssertNoThrow(try body(AppTaskCostApprovalBody.maxCents))
    }
    func testEuroTextHasNoRoundingGroupingExponentsOrHiddenWhitespace() {
        for (value, expected) in [("0",0), ("0,01",1), ("20,1",2010), ("20.01",2001), ("10000000,00",1_000_000_000)] {
            XCTAssertEqual(TaskCostAmount.cents(value), expected, value)
            XCTAssertEqual(TaskCostAmount.cents(TaskCostAmount.text(expected)), expected)
        }
        for bad in ["", "20,", "1.000,00", "1,000", "0.001", "-1", "+1", "1e3", " 20", "20\n", "NaN", "10000000,01", "１２"] {
            XCTAssertNil(TaskCostAmount.cents(bad), bad)
        }
    }
    func testChallengeRejectsCrossPurposeActorChangedBodyAndDuplicateBindingKeys() throws {
        let b = try body(), issued = try challenge(b)
        for (key, value) in [("type", "app_task_followup_binding" as Any), ("device_id", "other"),
                            ("core_instance_id", "other"), ("app_attest_key_id", "other"),
                            ("principal_id", ""), ("enrollment_id", ""), ("nonce", "wrong"),
                            ("protocol_version", true), ("extra", "field")] {
            XCTAssertThrowsError(try hash(challenge(b, changes: [key:value]), b), key)
        }
        XCTAssertThrowsError(try hash(issued, body(2001)))
        XCTAssertThrowsError(try hash(issued, body(requestID: "another-request")))
        XCTAssertThrowsError(try hash(issued, b, now: issued.expires_at))
        XCTAssertThrowsError(try hash(issued, b, now: issued.expires_at - 301))
        let raw = String(decoding: try XCTUnwrap(Data(base64Encoded: issued.binding_b64)), as: UTF8.self)
        let duplicate = "{\"nonce\":\"duplicate\"," + raw.dropFirst()
        XCTAssertThrowsError(try hash(.init(nonce: issued.nonce, requestDigest: b.requestDigest,
            expiresAt: issued.expires_at, bindingB64: Data(duplicate.utf8).base64EncodedString()), b))
    }
    func testResponseNeedsExactOwnRequestEvenWhenCurrentCapIsHigher() throws {
        let b = try body()
        XCTAssertNoThrow(try accepted(b, cap: 3000).validate(body: b))
        for (key,value) in [("task_id", "at-2222222222222222" as Any), ("client_request_id", "another-request"),
                           ("max_total_cents", 3000), ("costs", ["configured":true,"task_id":b.task_id,"currency":"EUR","approved_ai_cap_cents":1999]),
                           ("costs", ["configured":true,"task_id":"at-2222222222222222","currency":"EUR","approved_ai_cap_cents":5000]),
                           ("costs", ["configured":true,"task_id":b.task_id,"currency":"USD","approved_ai_cap_cents":5000]),
                           ("costs", ["configured":true,"task_id":b.task_id,"approved_ai_cap_cents":5000]),
                           ("costs", ["configured":true,"currency":"EUR","approved_ai_cap_cents":5000])] {
            XCTAssertThrowsError(try accepted(b, cap: 5000, changes: [key:value]).validate(body: b))
        }
        XCTAssertThrowsError(try accepted(b, changes: ["extra":"ignored"]))
    }
    func testRetryRestoresOnlyExactTaskCoreAndDevice() throws {
        let b = try body(), retry = TaskCostApprovalRetryBinding(body: b, coreID: "core", deviceID: "phone")
        let saved = try JSONDecoder().decode(TaskCostApprovalRetryBinding.self, from: JSONEncoder().encode(retry))
        XCTAssertEqual(saved.restore(taskID: task, coreID: "core", deviceID: "phone"), b)
        XCTAssertNil(saved.restore(taskID: task, coreID: "other", deviceID: "phone"))
        XCTAssertNil(saved.restore(taskID: task, coreID: "core", deviceID: "other"))
        XCTAssertNil(saved.restore(taskID: "at-2222222222222222", coreID: "core", deviceID: "phone"))
        var raw = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(retry)) as? [String: Any])
        raw["bodyDigest"] = String(repeating: "0", count: 64)
        let changed = try JSONDecoder().decode(TaskCostApprovalRetryBinding.self, from: JSONSerialization.data(withJSONObject: raw))
        XCTAssertNil(changed.restore(taskID: task, coreID: "core", deviceID: "phone"))
    }
}
