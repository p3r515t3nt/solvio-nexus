// Wissen — Tests fuer die Uebersetzung in Produktsprache.
//
// Der Core spricht in Stufen (`lifecycle`), der Bildschirm in Saetzen. Diese
// Tests halten fest, dass die Uebersetzung woertlich bleibt: jede bekannte
// Stufe hat ihren einen Satz, und eine unbekannte faellt auf die vorsichtige
// Formulierung zurueck, statt Core-Jargon auf den Bildschirm zu heben.
import XCTest
@testable import SolvioApprovals

final class WissenModelTests: XCTestCase {

    func testHerkunftswortUebersetztJedeBekannteStufe() {
        XCTAssertEqual(herkunftswort("explicit"), "Von dir gemerkt")
        XCTAssertEqual(herkunftswort("confirmed"), "Von dir bestätigt")
        XCTAssertEqual(herkunftswort("learned"), "Von SOLVIO erkannt")
    }

    /// Eine Stufe, die der Core spaeter dazuerfindet, darf nie roh auf dem
    /// Bildschirm landen — der Rueckfall ist ein ganzer Satz, kein Bezeichner.
    func testUnbekannteStufeFaelltVorsichtigZurueck() {
        XCTAssertEqual(herkunftswort("agent_generated"), "Aus einem früheren Gespräch")
        XCTAssertEqual(herkunftswort(""), "Aus einem früheren Gespräch")
    }
}
