import XCTest
@testable import SolvioApprovals

final class ConnectionsTests: XCTestCase {
    @MainActor func testUnavailableOrUnmeasuredHealthNeverClaimsConnected() {
        let healthy = ComponentHealth(komponente: "gmail", name: "Mail", zustand: "healthy", grund: "", geprueft_um: 100, zuletzt_gesund: 100, braucht_dich: false, reparierbar: false)
        XCTAssertEqual(ConnectionsView.status(healthy, unavailable: true), "Stand nicht bestätigt")
        XCTAssertEqual(ConnectionsView.status(nil, unavailable: false), "Noch nicht geprüft")
        XCTAssertEqual(ConnectionsView.status(healthy, unavailable: false), "Zuletzt verfügbar")
        let unmeasured = ComponentHealth(komponente: "gmail", name: "Mail", zustand: "healthy", grund: "", geprueft_um: nil, zuletzt_gesund: nil, braucht_dich: false, reparierbar: false)
        XCTAssertEqual(ConnectionsView.status(unmeasured, unavailable: false), "Noch nicht geprüft")
    }
}
