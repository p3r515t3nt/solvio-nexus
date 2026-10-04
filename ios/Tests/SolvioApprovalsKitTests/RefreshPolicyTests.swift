// Die Kadenz der Nachfrage — geprueft ohne Geraet, ohne Netz, ohne Zeitgeber.
//
// Der Anlass ist konkret: eine Freigabe lag bereit, die App stand offen, und sie
// fragte nie nach. Was daraus folgte, ist eine Schleife — und eine Schleife, die
// niemand prueft, wird entweder zu schnell (Batterie) oder zu langsam (der Mensch
// wartet) oder haemmert gegen ein totes Netz.
import XCTest
@testable import SolvioApprovalsKit

final class RefreshPolicyTests: XCTestCase {

    func testNormalCadenceIsShortEnoughToFeelImmediate() {
        let p = RefreshPolicy()
        XCTAssertEqual(p.interval, 3, accuracy: 0.001)
        XCTAssertLessThanOrEqual(p.interval, 5, "eine Freigabe soll gleich erscheinen")
        XCTAssertGreaterThanOrEqual(p.interval, 2, "aber kein Dauerfeuer")
        XCTAssertFalse(p.isBackingOff)
    }

    func testFailuresBackOffAndAreCapped() {
        var p = RefreshPolicy()
        p.failed(); XCTAssertEqual(p.interval, 6, accuracy: 0.001)
        p.failed(); XCTAssertEqual(p.interval, 12, accuracy: 0.001)
        p.failed(); XCTAssertEqual(p.interval, 24, accuracy: 0.001)
        p.failed(); XCTAssertEqual(p.interval, 30, accuracy: 0.001, "gedeckelt")
        for _ in 0..<20 { p.failed() }
        XCTAssertEqual(p.interval, 30, accuracy: 0.001,
                       "auch nach langer Stoerung nicht mehr als die Obergrenze")
        XCTAssertTrue(p.isBackingOff)
    }

    func testRecoveryIsImmediateNotGradual() {
        var p = RefreshPolicy()
        for _ in 0..<6 { p.failed() }
        XCTAssertEqual(p.interval, 30, accuracy: 0.001)
        p.succeeded()
        XCTAssertEqual(p.interval, 3, accuracy: 0.001,
                       "wer wieder erreichbar ist, ist erreichbar — kein Aufwaermen")
        XCTAssertFalse(p.isBackingOff)
    }

    func testTheIntervalNeverReachesZeroOrGoesBackwards() {
        var p = RefreshPolicy()
        var previous = p.interval
        for _ in 0..<10 {
            p.failed()
            XCTAssertGreaterThanOrEqual(p.interval, previous,
                                        "ein Fehlschlag verkuerzt den Abstand nie")
            XCTAssertGreaterThan(p.interval, 0)
            previous = p.interval
        }
    }

    func testFailureCountNeverGoesNegative() {
        let p = RefreshPolicy(failures: -5)
        XCTAssertEqual(p.failures, 0)
        XCTAssertEqual(p.interval, p.base, accuracy: 0.001)
    }

    func testACustomPolicyKeepsItsOwnBounds() {
        var p = RefreshPolicy(base: 1, ceiling: 4, factor: 3)
        XCTAssertEqual(p.interval, 1, accuracy: 0.001)
        p.failed(); XCTAssertEqual(p.interval, 3, accuracy: 0.001)
        p.failed(); XCTAssertEqual(p.interval, 4, accuracy: 0.001, "eigene Obergrenze")
    }
}
