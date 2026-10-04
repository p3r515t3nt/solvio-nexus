import Foundation
import XCTest
@testable import SolvioApprovalsKit

final class TaskPortalTests: XCTestCase {
    let account = "portal-" + String(repeating: "a", count: 32)
    func raw(label: String = "Studio", authenticated: Bool = true, age: Double = 0) -> [String: Any] {
        ["service": "portal", "observed_at": Date().timeIntervalSince1970 - age, "truncated": false,
         "items": [["account": account, "target": ["portal_id": "studio", "session_id": "ps-123-1"],
                    "label": label, "origin": "https://portal.example.invalid", "authenticated": authenticated, "expires_in_s": 300]]]
    }
    func catalogue(_ raw: [String: Any]) throws -> TaskPortalSessions {
        try TaskPortalSessions.decode(JSONSerialization.data(withJSONObject: raw))
    }
    func testExplicitExistingSelectionProducesOnlyBoundStatus() throws {
        var form = TaskActionForm(); form.kind = .portal
        try form.portal.accept(catalogue(raw()))
        XCTAssertNil(form.portal.selectedSession)
        XCTAssertThrowsError(try form.request(accounts: []))
        try form.portal.select(account)
        let action = try XCTUnwrap(form.request(accounts: []).actions.first)
        XCTAssertEqual(action.operation, "status"); XCTAssertEqual(action.service, "portal")
        XCTAssertEqual(action.account, account); XCTAssertEqual(action.target, ["portal_id": "studio", "session_id": "ps-123-1"])
        XCTAssertEqual(action.payload, [:])
    }
    func testUnknownAndUnauthenticatedSessionsNeverBecomeSelected() throws {
        var form = TaskPortalForm(); try form.accept(catalogue(raw(authenticated: false)))
        XCTAssertThrowsError(try form.select(account))
        XCTAssertThrowsError(try form.select("portal-" + String(repeating: "b", count: 32)))
        XCTAssertNil(form.selectedSession); XCTAssertThrowsError(try form.action())
    }
    func testEquallyNamedConnectionsAreDistinguishableWithoutChangingTheirBinding() throws {
        var input = raw(), rows = input["items"] as! [[String: Any]], second = rows[0]
        let secondAccount = "portal-" + String(repeating: "b", count: 32)
        second["account"] = secondAccount; second["target"] = ["portal_id": "studio", "session_id": "ps-123-2"]
        rows.append(second); input["items"] = rows
        let list = try catalogue(input)
        XCTAssertEqual(list.displayLabel(for: list.items[0]), "Studio · Verbindung 1")
        XCTAssertEqual(list.displayLabel(for: list.items[1]), "Studio · Verbindung 2")
        var form = TaskPortalForm(); try form.accept(list); try form.select(secondAccount)
        XCTAssertEqual(try form.action().target["session_id"], "ps-123-2")
        let before = form
        input["items"] = rows.reversed().map { $0 }; try form.accept(catalogue(input))
        XCTAssertTrue(form.hasSameInput(as: before)); XCTAssertEqual(try form.action(), try before.action())
        XCTAssertEqual(form.catalogue?.displayLabel(for: try XCTUnwrap(form.selectedSession)), "Studio · Verbindung 2")
    }
    func testCatalogueMetadataDoesNotEditFrozenInput() throws {
        var form = TaskActionForm(); form.kind = .portal
        try form.portal.accept(catalogue(raw())); try form.portal.select(account)
        let before = form, action = try form.request(accounts: []), objective = form.objective
        form.portal.invalidate()
        XCTAssertTrue(before.hasSameInput(as: form)); XCTAssertThrowsError(try form.request(accounts: []))
        try form.portal.accept(catalogue(raw(label: "Neuer Name")))
        XCTAssertTrue(before.hasSameInput(as: form)); XCTAssertEqual(form.objective, objective)
        XCTAssertEqual(try form.request(accounts: []), action)
    }
    func testChangedAccountCannotReplaceSelectedSessionOnRefresh() throws {
        var form = TaskPortalForm(); try form.accept(catalogue(raw())); try form.select(account)
        var changed = raw(), row = (changed["items"] as! [[String: Any]])[0]
        row["account"] = "portal-" + String(repeating: "b", count: 32); changed["items"] = [row]
        try form.accept(catalogue(changed))
        XCTAssertNil(form.selectedSession); XCTAssertEqual(form.selectedAccount, account)
        XCTAssertThrowsError(try form.action())
    }
    func testResponseValidationRejectsStaleDuplicateAndInjectedFields() throws {
        XCTAssertThrowsError(try catalogue(raw(age: 61)))
        XCTAssertThrowsError(try catalogue(raw(age: -30)))
        for field in ["credentials", "login_url", "secret"] {
            var invalid = raw(); invalid[field] = "untrusted"
            XCTAssertThrowsError(try catalogue(invalid))
        }
        var duplicate = raw(); let items = duplicate["items"] as! [[String: Any]]; duplicate["items"] = items + items
        XCTAssertThrowsError(try catalogue(duplicate))
        var wrong = raw(), row = (wrong["items"] as! [[String: Any]])[0]
        row["origin"] = "https://user:pass@portal.example.invalid"; wrong["items"] = [row]
        XCTAssertThrowsError(try catalogue(wrong))
    }
    func testNoConnectLoginOrUnknownPayloadIsAccepted() throws {
        for operation in ["connect", "login", "execute", "send"] {
            XCTAssertThrowsError(try AppTaskAction(actionID: "portal1", service: "portal", operation: operation,
                account: account, target: ["portal_id": "studio", "session_id": "ps-123-1"], payload: [:]))
        }
        XCTAssertThrowsError(try AppTaskAction(actionID: "portal1", service: "portal", operation: "status",
            account: account, target: ["portal_id": "studio", "session_id": "ps-123-1"], payload: ["login": .bool(true)]))
    }
    func testCanonicalStatusBodyRetainsExactOriginalRetryAndUsesFreshProof() throws {
        var form = TaskPortalForm(); try form.accept(catalogue(raw())); try form.select(account)
        let request = try AppTaskActionRequest(actions: [form.action()])
        let body = try AppTaskBody(scope: "action", objective: form.objective, targetRepo: "", requestID: "portal-test-001", actionRequest: request)
        let binding = TaskStartRetryBinding(body: body, coreID: "core", deviceID: "phone")
        XCTAssertEqual(binding.restore(scope: "action", objective: form.objective, targetRepo: "", coreID: "core", deviceID: "phone", actionRequest: request)?.requestDigest, body.requestDigest)
        XCTAssertNil(binding.restore(scope: "action", objective: form.objective, targetRepo: "", coreID: "other", deviceID: "phone", actionRequest: request))
        XCTAssertEqual(try JSONDecoder().decode(AppTaskBody.self, from: JSONEncoder().encode(body)), body)
    }
}
