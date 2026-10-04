import XCTest
@testable import SolvioApprovals

@MainActor
private final class PushFake: PushClient {
    var calls: [String] = []
    var fail = false
    var onRegister: (() async -> Void)?
    func pushChallenge() async throws -> PushSetupResponse { PushSetupResponse(ok: true, configured: true) }
    func pushRegister(token: String, environment: String) async throws -> PushSetupResponse {
        calls.append(token)
        if let onRegister { await onRegister() }
        if fail { throw URLError(.notConnectedToInternet) }
        return PushSetupResponse(ok: true, configured: true)
    }
}

@MainActor
final class PushTests: XCTestCase {
    func testPushTapBeforeViewExistsSurvivesInactiveScene() {
        let route = PushNavigation()
        route.receive(route: "inbox")
        XCTAssertFalse(route.consume(isActive: false))
        XCTAssertTrue(route.pendingInbox)
        XCTAssertTrue(route.consume(isActive: true))
        XCTAssertFalse(route.pendingInbox)
    }
    func testConsumedPushDoesNotReopenOnNextForeground() {
        let route = PushNavigation()
        route.receive(route: "inbox")
        XCTAssertTrue(route.consume(isActive: true))
        XCTAssertFalse(route.consume(isActive: true))
        route.receive(route: "inbox")
        XCTAssertTrue(route.consume(isActive: true))
    }
    func testPushCannotNavigateToOtherDestinations() {
        let route = PushNavigation()
        for destination in [nil, "", "approve", "https://example.invalid"] as [String?] {
            route.receive(route: destination)
            XCTAssertFalse(route.consume(isActive: true))
        }
    }
    private func defaults() -> UserDefaults { UserDefaults(suiteName: "push-test-" + UUID().uuidString)! }
    func testFailedDisablePersistsAndRetriesWhileDisabledAfterRestart() async {
        let d = defaults(); d.set(true, forKey: "push.registrationPossible")
        var now = Date(); let c = PushFake(); c.fail = true
        let m = PushModel(defaults: d, now: { now }, registerOS: {}, unregisterOS: {})
        let first = await m.disable(client: c)
        XCTAssertFalse(first); XCTAssertFalse(m.enabled)
        XCTAssertTrue(d.bool(forKey: "push.pendingRemoval"))
        let restarted = PushModel(defaults: d, now: { now }, registerOS: {}, unregisterOS: {})
        c.fail = false; now.addTimeInterval(61)
        await restarted.refresh(client: c, denied: { XCTFail("must not request OS status"); return false })
        XCTAssertEqual(c.calls, ["", ""])
        XCTAssertFalse(d.bool(forKey: "push.pendingRemoval"))
        XCTAssertFalse(d.bool(forKey: "push.registrationPossible"))
    }
    func testDisableDuringPermissionCannotReenable() async {
        let c = PushFake(); var registers = 0
        let m = PushModel(defaults: defaults(), registerOS: { registers += 1 }, unregisterOS: {})
        await m.enable(client: c, permission: { _ = await m.disable(client: c); return true })
        XCTAssertFalse(m.enabled); XCTAssertEqual(registers, 0)
    }
    func testOSRegistrationIsThrottledAndNoPrivateDataStored() async {
        let c = PushFake(); var registers = 0; var now = Date()
        let m = PushModel(defaults: defaults(), now: { now }, registerOS: { registers += 1 }, unregisterOS: {})
        await m.enable(client: c, permission: { true })
        await m.refresh(client: c, denied: { false }); XCTAssertEqual(registers, 1)
        now.addTimeInterval(61)
        await m.refresh(client: c, denied: { false }); XCTAssertEqual(registers, 2)
        XCTAssertTrue(c.calls.isEmpty)
    }
    func testDisableDuringRegistrationKeepsCleanupUntilRegisterFinishes() async {
        let d = defaults(); let c = PushFake(); var now = Date()
        let m = PushModel(defaults: d, now: { now }, registerOS: {}, unregisterOS: {})
        await m.enable(client: c, permission: { true })
        let sent = expectation(description: "register finished")
        c.onRegister = {
            let disabled = await m.disable(client: c)
            XCTAssertFalse(disabled)
            c.onRegister = nil; sent.fulfill()
        }
        m.received(Data(repeating: 0xab, count: 32))
        await fulfillment(of: [sent], timeout: 2)
        await Task.yield()
        XCTAssertFalse(m.enabled); XCTAssertTrue(d.bool(forKey: "push.pendingRemoval"))
        now.addTimeInterval(61)
        await m.refresh(client: c, denied: { false })
        XCTAssertEqual(c.calls.count, 2); XCTAssertEqual(c.calls.last, "")
        XCTAssertFalse(d.bool(forKey: "push.pendingRemoval"))
    }
    func testUnpairWithoutAnyRegistrationWorksOffline() async {
        let c = PushFake(); c.fail = true
        let m = PushModel(defaults: defaults(), registerOS: {}, unregisterOS: {})
        let safe = await m.disable(client: c)
        XCTAssertTrue(safe); XCTAssertTrue(c.calls.isEmpty)
    }
}
