import XCTest
@testable import SolvioApprovals

@MainActor
final class GoogleSignInClientTests: XCTestCase {
    func testCodeIsConsumedOnceAndOrdinaryDescriptionsAreRedacted() throws {
        let code = try GoogleAuthorizationCode(code: "synthetic-one-time-code", accountID: "synthetic-subject",
                                               accountEmail: "fixture@example.invalid", scopes: GoogleSignInClient.scopes)
        XCTAssertFalse(String(describing: code).contains("synthetic"))
        XCTAssertFalse(String(reflecting: code).contains("fixture@"))
        XCTAssertEqual(try code.takeCode(), "synthetic-one-time-code")
        XCTAssertThrowsError(try code.takeCode())
    }

    func testDiscardAndIncompleteConsentCannotBeUsed() throws {
        let code = try GoogleAuthorizationCode(code: "synthetic-code", accountID: "subject",
                                               accountEmail: "fixture@example.invalid", scopes: GoogleSignInClient.scopes)
        code.discard()
        XCTAssertThrowsError(try code.takeCode())
        XCTAssertThrowsError(try GoogleAuthorizationCode(code: "synthetic-code", accountID: "subject",
                                accountEmail: "fixture@example.invalid", scopes: Array(GoogleSignInClient.scopes.dropLast())))
        XCTAssertThrowsError(try GoogleAuthorizationCode(code: "synthetic\ncode", accountID: "subject",
                                accountEmail: "fixture@example.invalid", scopes: GoogleSignInClient.scopes))
    }
}
