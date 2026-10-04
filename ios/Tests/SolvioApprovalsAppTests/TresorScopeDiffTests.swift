// Rechteänderung — die Rechnung, die einen Verlust sichtbar macht.
//
// Der Core ERSETZT die Rechteliste, er ergänzt sie nicht. Zwei Rechte
// hinzuzufügen heißt also, alle anderen mitzuschicken. Diese Rechnung ist der
// einzige Schutz davor, dass jemand beim Hinzufügen zehn Rechte verliert —
// deshalb wird sie hier geprüft und nicht nur im Bildschirm geglaubt.
import XCTest
@testable import SolvioApprovals

final class TresorScopeDiffTests: XCTestCase {

    /// Die zehn, die der Google-Zugang heute führt.
    private let bestand: Set<String> = [
        "calendar_create_event", "calendar_delete_event", "calendar_list_events",
        "calendar_update_event", "gmail_create_draft", "gmail_list_recent",
        "gmail_read_message", "gmail_read_thread", "gmail_search",
        "gmail_send_draft"]

    func testEineErgaenzungVerliertNichts() {
        let diff = TresorScopeDiff(
            bisher: bestand,
            gewaehlt: bestand.union(["document_find", "document_ask"]))

        XCTAssertEqual(diff.kommtHinzu, ["document_ask", "document_find"])
        XCTAssertEqual(diff.entfaellt, [], "eine Ergänzung nahm Rechte mit")
        XCTAssertEqual(diff.bleibt.count, 10)
        XCTAssertEqual(diff.vollstaendig.count, 12,
                       "zum Mac geht nicht die vollständige Liste")
    }

    /// Der Fehler, für den die Rechnung überhaupt existiert: wer nur die zwei
    /// neuen Namen wählt, verliert zehn — und muss es sehen.
    func testEineErsetzungZeigtDenVerlust() {
        let diff = TresorScopeDiff(bisher: bestand,
                                   gewaehlt: ["document_find", "document_ask"])

        XCTAssertEqual(diff.entfaellt.count, 10, "der Verlust blieb unsichtbar")
        XCTAssertTrue(diff.entfaellt.contains("gmail_send_draft"))
        XCTAssertEqual(diff.bleibt, [])
        XCTAssertFalse(diff.unveraendert)
    }

    func testKeineAenderungHeisstKeineAenderung() {
        let diff = TresorScopeDiff(bisher: bestand, gewaehlt: bestand)
        XCTAssertTrue(diff.unveraendert)
        XCTAssertEqual(diff.kommtHinzu, [])
        XCTAssertEqual(diff.entfaellt, [])
        XCTAssertEqual(diff.vollstaendig.count, 10)
    }

    /// Entfernen bleibt möglich — es soll nur nicht aus Versehen passieren.
    func testEinzelnesEntfernenIstSichtbarUndMoeglich() {
        let diff = TresorScopeDiff(
            bisher: bestand,
            gewaehlt: bestand.subtracting(["gmail_send_draft"]))

        XCTAssertEqual(diff.entfaellt, ["gmail_send_draft"])
        XCTAssertEqual(diff.kommtHinzu, [])
        XCTAssertEqual(diff.vollstaendig.count, 9)
    }

    /// Die Liste geht sortiert hinaus. Zwei gleiche Auswahlen sollen nicht
    /// zwei verschiedene Anfragen ergeben — sonst wäre der Digest nicht
    /// wiederholbar und eine Freigabe nie zweimal dieselbe.
    func testDieVollstaendigeListeIstStabilSortiert() {
        let a = TresorScopeDiff(bisher: [], gewaehlt: ["b", "a", "c"]).vollstaendig
        let b = TresorScopeDiff(bisher: [], gewaehlt: ["c", "b", "a"]).vollstaendig
        XCTAssertEqual(a, b)
        XCTAssertEqual(a, ["a", "b", "c"])
    }
}
