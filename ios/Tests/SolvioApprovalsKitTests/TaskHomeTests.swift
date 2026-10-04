import Foundation
import XCTest
@testable import SolvioApprovalsKit

final class TaskHomeTests: XCTestCase {
    private let account = ActionServiceAccount(service: "ha", account: "ha-" + String(repeating: "a", count: 32), resource: "configured_home")
    private func wire(_ account: String? = nil) -> [String: Any] {
        ["service": "ha", "account": account ?? self.account.account, "truncated": false, "items": [
            ["target": ["entity_id": "light.living_room"], "name": "Leselampe 💡", "area": "Wohnzimmer",
             "domain": "light", "state": "off", "operations": ["set_state", "set_brightness"]],
            ["target": ["entity_id": "switch.desk"], "name": "Schreibtisch", "area": "Büro",
             "domain": "switch", "state": "on", "operations": ["set_state"]]]]
    }
    private func catalogue(_ value: [String: Any]? = nil, account: ActionServiceAccount? = nil) throws -> TaskHomeResources {
        try TaskHomeResources.decode(JSONSerialization.data(withJSONObject: value ?? wire()), account: account ?? self.account)
    }
    private func form() throws -> TaskActionForm {
        var form = TaskActionForm(); form.kind = .ha; form.reconcileAccounts([account])
        try form.home.accept(catalogue(), account: account)
        return form
    }

    func testCatalogueRequiresExplicitDeviceAndDesiredOperation() throws {
        var form = try form()
        XCTAssertEqual(form.selectedAccount?.displayLabel, "Home Assistant")
        XCTAssertNil(form.home.selectedEntityID); XCTAssertNil(form.home.desired)
        XCTAssertThrowsError(try form.request(accounts: [account]))
        try form.home.select("light.living_room")
        XCTAssertNil(form.home.desired)
        XCTAssertThrowsError(try form.request(accounts: [account]))
        form.home.desired = .on
        let action = try form.request(accounts: [account]).actions[0]
        XCTAssertEqual(action.target, ["entity_id": "light.living_room"])
        XCTAssertEqual(action.payload, ["state": .string("on")])
        XCTAssertEqual(form.home.selectedDevice?.stateLabel, "Aus")
        XCTAssertEqual(form.home.selectedDevice?.displayLabel, "Leselampe 💡 · Wohnzimmer")
        try form.home.select("switch.desk")
        XCTAssertNil(form.home.desired)
    }

    func testBrightnessIsAnExactIntegerAndNeverOfferedForSwitches() throws {
        var form = try form(); try form.home.select("light.living_room"); form.home.desired = .brightness
        for number in [0, 1, 50, 100] {
            form.home.brightnessPercent = number
            let action = try form.request(accounts: [account]).actions[0]
            XCTAssertEqual(action.operation, "set_brightness")
            XCTAssertEqual(action.payload, ["brightness_pct": .number(number)])
        }
        for number in [-1, 101] {
            form.home.brightnessPercent = number; XCTAssertThrowsError(try form.request(accounts: [account]))
        }
        try form.home.select("switch.desk"); form.home.desired = .brightness; form.home.brightnessPercent = 50
        XCTAssertThrowsError(try form.request(accounts: [account]))
        form.home.desired = .off
        XCTAssertEqual(try form.request(accounts: [account]).actions[0].payload, ["state": .string("off")])
    }

    func testMalformedForeignUnsafeAndDuplicateCatalogueRowsAreRejected() throws {
        for mutation in ["account", "service", "duplicate", "domain", "entity", "extra", "operation", "boolean", "count"] {
            var raw = wire(), items = try XCTUnwrap(raw["items"] as? [[String: Any]])
            switch mutation {
            case "account": raw["account"] = "ha-" + String(repeating: "b", count: 32)
            case "service": raw["service"] = "calendar"
            case "duplicate": items.append(items[0])
            case "domain": items[0]["domain"] = "switch"
            case "entity": items[0]["target"] = ["entity_id": "lock.front_door"]
            case "extra": items[0]["url"] = "https://untrusted.invalid"
            case "operation": items[0]["operations"] = ["set_state", "unlock"]
            case "boolean": raw["truncated"] = "false"
            default: items = Array(repeating: items[0], count: 101)
            }
            raw["items"] = items
            XCTAssertThrowsError(try catalogue(raw), mutation)
        }
    }

    func testCatalogueInvalidationCannotUseLastKnownDeviceOrInventedTarget() throws {
        var form = try form(); try form.home.select("light.living_room"); form.home.desired = .on
        let original = try form.request(accounts: [account])
        XCTAssertThrowsError(try form.home.select("light.invented"))
        form.home.invalidate()
        XCTAssertNil(form.home.catalogue); XCTAssertThrowsError(try form.request(accounts: [account]))
        try form.home.accept(catalogue(), account: account)
        XCTAssertEqual(try form.request(accounts: [account]), original)
        var empty = wire(); empty["items"] = []
        try form.home.accept(catalogue(empty), account: account)
        XCTAssertNil(form.home.selectedDevice); XCTAssertThrowsError(try form.request(accounts: [account]))
    }

    func testAccountRotationRequiresExplicitSelectionAndDoesNotCarryDeviceAction() throws {
        var form = try form(); try form.home.select("light.living_room"); form.home.desired = .on
        let other = ActionServiceAccount(service: "ha", account: "ha-" + String(repeating: "b", count: 32), resource: "configured_home")
        form.reconcileAccounts([other])
        XCTAssertEqual(form.selectedAccount, account); XCTAssertThrowsError(try form.request(accounts: [other]))
        try form.selectAccount(other, accounts: [other])
        XCTAssertThrowsError(try form.request(accounts: [other]))
        try form.home.accept(catalogue(wire(other.account), account: other), account: other)
        XCTAssertNil(form.home.selectedDevice); XCTAssertNil(form.home.desired)
        XCTAssertThrowsError(try form.request(accounts: [other]))
    }

    func testSignedCanonicalBytesAndRetryBindEntityOperationAndInteger() throws {
        var form = try form(); try form.home.select("light.living_room")
        form.home.desired = .brightness; form.home.brightnessPercent = 37
        let request = try form.request(accounts: [account])
        let body = try AppTaskBody(scope: "action", objective: form.objective, targetRepo: "", requestID: "ha-request-001", actionRequest: request)
        XCTAssertTrue(String(decoding: body.canonicalBytes(), as: UTF8.self).contains("\"brightness_pct\":37"))
        XCTAssertEqual(try JSONDecoder().decode(AppTaskBody.self, from: JSONEncoder().encode(body)), body)
        let retry = TaskStartRetryBinding(body: body, coreID: "core", deviceID: "phone")
        XCTAssertEqual(retry.restore(scope: "action", objective: form.objective, targetRepo: "", coreID: "core", deviceID: "phone", actionRequest: request), body)
        for mutation in ["brightness", "operation", "device"] {
            var changed = form
            if mutation == "brightness" { changed.home.brightnessPercent = 38 }
            if mutation == "operation" { changed.home.desired = .on }
            if mutation == "device" { try changed.home.select("switch.desk"); changed.home.desired = .off }
            XCTAssertNil(retry.restore(scope: "action", objective: body.objective, targetRepo: "", coreID: "core", deviceID: "phone", actionRequest: try changed.request(accounts: [account])))
        }
        // Shared Python Core canonicalizer vector, checked in the public Core
        // companion candidate without native service execution.
        XCTAssertEqual(body.requestDigest, "adee8eb0886bbc21bb7e7752d3c0069944cc385b9af860056d4d0fca2cf1415c")
    }

    func testHAActionDecoderRejectsBooleanFractionAndHiddenEffectFields() throws {
        for rawValue in [true, 1.5, "50", -1, 101] as [Any] {
            let raw: [String: Any] = ["action_id": "home1", "service": "ha", "operation": "set_brightness",
                "account": account.account, "target": ["entity_id": "light.living_room"], "payload": ["brightness_pct": rawValue]]
            XCTAssertThrowsError(try JSONDecoder().decode(AppTaskAction.self, from: JSONSerialization.data(withJSONObject: raw)))
        }
        XCTAssertThrowsError(try AppTaskAction(actionID: "home1", service: "ha", operation: "set_state", account: account.account,
            target: ["entity_id": "light.living_room", "all": "true"], payload: ["state": .string("on")]))
    }

    func testReadMetadataPreservesSelectedTaskBytesButNeverStalePermission() throws {
        var form = try form(); try form.home.select("light.living_room"); form.home.desired = .on
        let original = form
        let body = try AppTaskBody(scope: "action", objective: form.objective, targetRepo: "", requestID: "home-retry-001", actionRequest: form.request(accounts: [account]))
        form.home.invalidate()
        XCTAssertTrue(form.hasSameInput(as: original))
        XCTAssertThrowsError(try form.request(accounts: [account]))
        var raw = wire(), items = try XCTUnwrap(raw["items"] as? [[String: Any]])
        items[0]["name"] = "Neuer Anzeigename"; items[0]["state"] = "on"; raw["items"] = items
        try form.home.accept(catalogue(raw), account: account)
        XCTAssertEqual(form.home.selectedDevice?.name, "Neuer Anzeigename")
        XCTAssertEqual(form.home.selectedDevice?.state, "on")
        XCTAssertTrue(form.hasSameInput(as: original))
        let retry = try AppTaskBody(scope: "action", objective: form.objective, targetRepo: "", requestID: body.client_request_id, actionRequest: form.request(accounts: [account]))
        XCTAssertEqual(retry.canonicalBytes(), body.canonicalBytes())
        raw["items"] = []
        try form.home.accept(catalogue(raw), account: account)
        XCTAssertTrue(form.hasSameInput(as: original))
        XCTAssertThrowsError(try form.request(accounts: [account]))
    }

    func testInputComparisonDistinguishesExplicitChoiceFromUnrelatedAccountRead() throws {
        var form = try form(); try form.home.select("light.living_room"); form.home.desired = .on
        let original = form
        form.reconcileAccounts([account, ActionServiceAccount(service: "gmail", account: "gmail-fixture", resource: "me")])
        XCTAssertTrue(form.hasSameInput(as: original))
        form.home.desired = .off
        XCTAssertFalse(form.hasSameInput(as: original))
        form = original; try form.home.select("switch.desk"); form.home.desired = .on
        XCTAssertFalse(form.hasSameInput(as: original))
        form = original; form.home.brightnessPercent = 25
        XCTAssertFalse(form.hasSameInput(as: original))
    }
}
