// Der Kaufbildschirm — liest den signierten Auftrag, erfindet nie etwas dazu.
//
// Diese Tests halten die eine Eigenschaft fest, an der die Karte haengt: sie
// zeigt ausschliesslich, was im signierten Text steht, und sie zeigt gar nichts,
// wenn sie ihn nicht vollstaendig lesen kann. Eine Anzeige, die aus einem
// unverstandenen Text etwas Huebsches macht, ist gefaehrlicher als keine.
//
// Der Beispieltext ist WOERTLICH das, was `approval_gateway.render_action` im
// Core baut: Ueberschrift, dann je Angabe eine Zeile `Beschriftung: <json>`,
// dann die Herkunft.
import XCTest
@testable import SolvioApprovals

final class PaymentApprovalCardTests: XCTestCase {

    private let signierterAuftrag = """
    SOLVIO möchte für dich bezahlen
    Adresse: "https://sandbox.solvio.invalid"
    Artikel: "1× MagSafe Stativ zu 84,99 EUR"
    Belastet wird: "wie oben"
    Gesamtbetrag: "89,98 EUR"
    Gültig bis: "2026-08-27T16:45:06+00:00"
    Händler: "SOLVIO Testladen"
    Hinzu kommen: "Versand 4,99 EUR"
    Lieferung: "keine"
    Lieferziel (Prüfsumme): "keins"
    Prüfsumme: "dc3cfadad128cf5c"
    Stückzahl: "1"
    Vorgang: "pi-90b29d12811b2505"
    Währung: "EUR"
    Zahlungsmittel: "SOLVIO Shopping (•••• 4242)"
    Zwischensumme: "84,99 EUR"
    Angefragt über: iPhone-App
    """

    func testDerBetragKommtWoertlichAusDemSigniertenText() {
        let kauf = ParsedPurchase.parse(signierterAuftrag)
        XCTAssertTrue(kauf.complete)
        XCTAssertEqual(kauf.betrag, "89,98 EUR")
        XCTAssertEqual(kauf.haendler, "SOLVIO Testladen")
        XCTAssertEqual(kauf.adresse, "https://sandbox.solvio.invalid")
        XCTAssertEqual(kauf.zahlungsmittel, "SOLVIO Shopping (•••• 4242)")
        XCTAssertEqual(kauf.zwischensumme, "84,99 EUR")
        XCTAssertEqual(kauf.aufschlaege, "Versand 4,99 EUR")
        XCTAssertEqual(kauf.herkunft, "iPhone-App")
    }

    /// Die Herkunft steht im signierten Text und damit im Digest. Eine Anfrage
    /// aus dem Raum und eine aus der App tragen verschiedene Texte und koennen
    /// einander deshalb nicht einloesen — das soll man auch sehen.
    func testDieHerkunftWirdAngezeigtUndNichtErfunden() {
        let ausDemRaum = signierterAuftrag.replacingOccurrences(
            of: "Angefragt über: iPhone-App", with: "Angefragt über: Raum-Mikrofon")
        XCTAssertEqual(ParsedPurchase.parse(ausDemRaum).herkunft, "Raum-Mikrofon")
    }

    /// FAIL CLOSED: fehlt eine der vier tragenden Angaben, erscheint die Karte
    /// gar nicht und es bleibt beim woertlichen Auftrag.
    func testEinUnvollstaendigerTextErgibtKeineKarte() {
        for fehlt in ["Gesamtbetrag", "Händler", "Adresse", "Zahlungsmittel"] {
            let zerlegt = signierterAuftrag
                .split(separator: "\n", omittingEmptySubsequences: false)
                .filter { !$0.hasPrefix(fehlt + ":") }
                .joined(separator: "\n")
            XCTAssertFalse(ParsedPurchase.parse(zerlegt).complete,
                           "ohne \(fehlt) darf keine Kaufkarte entstehen")
        }
    }

    func testEinVoelligFremderTextErgibtKeineKarte() {
        XCTAssertFalse(ParsedPurchase.parse("").complete)
        XCTAssertFalse(ParsedPurchase.parse("Kalendertermin anlegen\nTitel: \"Zahnarzt\"")
                        .complete)
    }

    /// Der Core kodiert Werte als JSON. Ein Umlaut darf dabei nicht zu `\\u00fc`
    /// werden, und Anfuehrungszeichen im Wert duerfen die Zeile nicht sprengen.
    func testWerteWerdenAlsJsonGelesenUndNichtRoh() {
        let text = """
        SOLVIO möchte für dich bezahlen
        Adresse: "https://sandbox.solvio.invalid"
        Gesamtbetrag: "1.234,50 EUR"
        Händler: "Müller & Söhne"
        Zahlungsmittel: "SOLVIO Shopping"
        """
        let kauf = ParsedPurchase.parse(text)
        XCTAssertTrue(kauf.complete)
        XCTAssertEqual(kauf.haendler, "Müller & Söhne")
        XCTAssertEqual(kauf.betrag, "1.234,50 EUR")
    }

    /// Zwei verschiedene Betraege duerfen nie denselben Bildschirm ergeben.
    func testEinCentUnterschiedIstSichtbar() {
        let teurer = signierterAuftrag.replacingOccurrences(
            of: "Gesamtbetrag: \"89,98 EUR\"", with: "Gesamtbetrag: \"89,99 EUR\"")
        XCTAssertNotEqual(ParsedPurchase.parse(signierterAuftrag).betrag,
                          ParsedPurchase.parse(teurer).betrag)
    }

    /// Deutsche Schreibweise, ohne Locale-Rat: eine Systemeinstellung darf nicht
    /// bestimmen, welcher Betrag auf einer Kaufbestaetigung steht.
    func testBetragsformatIstDeterministisch() {
        XCTAssertEqual(PaymentAmount.text(8998, "EUR"), "89,98 EUR")
        XCTAssertEqual(PaymentAmount.text(1234567, "EUR"), "12.345,67 EUR")
        XCTAssertEqual(PaymentAmount.text(5, "EUR"), "0,05 EUR")
        XCTAssertEqual(PaymentAmount.text(100000000, "EUR"), "1.000.000,00 EUR")
    }
}
