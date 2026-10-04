import XCTest
@testable import SolvioApprovals

@MainActor
final class ContactRefreshTests: XCTestCase {
    private func model(_ clock: @escaping () -> Date = Date.init) -> ContactRefreshModel {
        let defaults = UserDefaults(suiteName: "contact-refresh-test-" + UUID().uuidString)!
        return ContactRefreshModel(defaults: defaults, now: clock)
    }
    private var snapshot: AddressBookSnapshot {
        AddressBookSnapshot(contacts: [AddressBookContact(name: "Alex", emails: ["alex@example.test"])], accounts: [])
    }
    private var ok: ContactMutationResult { ContactMutationResult(ok: true, imported: 1) }

    func testNoUnrequestedReadOrPermissionPrompt() async {
        let m = model()
        await m.refresh(allowed: { false }, read: { XCTFail("must not read"); return self.snapshot },
                        upload: { _ in XCTFail("must not upload"); return self.ok })
        XCTAssertNil(m.lastSuccess)
    }
    func testManualSuccessEnablesRefreshAndHourlyThrottle() async {
        var time = Date(timeIntervalSince1970: 10000)
        let m = model({ time }); var uploads = 0
        let upload: (String) async throws -> ContactMutationResult = { raw in
            uploads += 1; XCTAssertTrue(raw.contains("alex@example.test")); return self.ok
        }
        await m.refresh(manual: true, allowed: { true }, read: { self.snapshot }, upload: upload)
        XCTAssertTrue(m.enabled)
        time.addTimeInterval(120)
        await m.refresh(allowed: { true }, read: { XCTFail("fresh"); return self.snapshot }, upload: upload)
        XCTAssertEqual(uploads, 1)
        time.addTimeInterval(3600)
        await m.refresh(allowed: { true }, read: { self.snapshot }, upload: upload)
        XCTAssertEqual(uploads, 2)
    }
    func testChangedContactsRefreshBeforeHourAndFailureDoesNotClaimFreshness() async {
        var time = Date(timeIntervalSince1970: 10000)
        let m = model({ time })
        await m.refresh(manual: true, allowed: { true }, read: { self.snapshot }, upload: { _ in self.ok })
        let saved = m.lastSuccess; time.addTimeInterval(120); m.changed()
        await m.refresh(allowed: { true }, read: { self.snapshot }, upload: { _ in ContactMutationResult(ok: false) })
        XCTAssertEqual(m.lastSuccess, saved)
        time.addTimeInterval(120)
        await m.refresh(allowed: { true }, read: { self.snapshot }, upload: { _ in self.ok })
        XCTAssertEqual(m.lastSuccess, time)
    }
    func testRevocationClearsSourceWithoutReadingContacts() async {
        let m = model(); m.enabled = true
        await m.refresh(allowed: { false }, read: { XCTFail("revoked"); return self.snapshot }, upload: { raw in
            XCTAssertEqual(raw, "[]"); return self.ok
        })
        XCTAssertNil(m.lastSuccess)
    }
    func testFailedRemovalRetriesAndNeverReimports() async {
        var time = Date(timeIntervalSince1970: 10000)
        let m = model({ time }); m.enabled = true
        await m.refresh(remove: true, allowed: { true }, read: { XCTFail(); return self.snapshot },
                        upload: { raw in XCTAssertEqual(raw, "[]"); throw URLError(.notConnectedToInternet) })
        XCTAssertFalse(m.enabled); time.addTimeInterval(120)
        var cleared = false
        await m.refresh(allowed: { true }, read: { XCTFail(); return self.snapshot }, upload: { raw in
            XCTAssertEqual(raw, "[]"); cleared = true; return self.ok
        })
        XCTAssertTrue(cleared)
        time.addTimeInterval(3600)
        await m.refresh(allowed: { true }, read: { XCTFail(); return self.snapshot }, upload: { _ in XCTFail(); return self.ok })
    }
    func testUnpairWhileReadingPreventsUploadAndRestoringOptIn() async {
        let m = model()
        await m.refresh(manual: true, allowed: { true }, read: { m.reset(); return self.snapshot },
                        upload: { _ in XCTFail("unpaired"); return self.ok })
        XCTAssertFalse(m.enabled); XCTAssertNil(m.lastSuccess)
    }
    func testOverlappingAutomaticRequestDoesNotUploadTwice() async {
        let m = model(); m.enabled = true; var count = 0
        await m.refresh(allowed: { true }, read: {
            await m.refresh(allowed: { true }, read: { XCTFail(); return self.snapshot }, upload: { _ in XCTFail(); return self.ok })
            return self.snapshot
        }, upload: { _ in count += 1; return self.ok })
        XCTAssertEqual(count, 1)
    }
    func testRevocationBypassesThrottleAndDisabledRefresh() async {
        let m = model()
        await m.refresh(manual: true, allowed: { true }, read: { self.snapshot }, upload: { _ in self.ok })
        m.enabled = false
        var removed = false
        await m.refresh(allowed: { false }, revoked: { true }, read: { XCTFail(); return self.snapshot },
                        upload: { raw in removed = raw == "[]"; return self.ok })
        XCTAssertTrue(removed); XCTAssertNil(m.lastSuccess)
    }
    func testManualPermissionErrorRemovesImmediately() async {
        let m = model(); var removed = false
        await m.refresh(manual: true, allowed: { false }, read: { throw ContactReadError.permission },
                        upload: { raw in removed = raw == "[]"; return self.ok })
        XCTAssertTrue(removed)
    }

}
