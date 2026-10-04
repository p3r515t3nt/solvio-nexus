// Zusicherungen fuer das Nexus-Bewegungsmodell.
//
// Das Modell ist der 1:1-Port der tick()-Referenz aus dem Design-Handoff:
// sechs Regler, Zielwerte je Modus, weicher Lerp. Diese Tests halten fest,
// dass die Portierung die REGELN traegt — nicht einzelne Pixel.
import XCTest
@testable import SolvioApprovals

final class PresenceModelTests: XCTestCase {

    // Jeder Sitzungszustand hat genau eine Presence-Entsprechung. Ein neuer
    // VoiceState-Fall bricht hier den BUILD, nicht erst den Test.
    func testVoiceStateMappingIstVollstaendigUndKorrekt() {
        let erwartet: [(VoiceState, PresenceState)] = [
            (.ready, .idle), (.listening, .listening), (.thinking, .thinking),
            (.speaking, .speaking), (.deepWork, .deepWork),
            (.reconnecting, .reconnecting), (.offline("x"), .offline),
            (.ended, .ended),
        ]
        for (voice, presence) in erwartet {
            XCTAssertEqual(PresenceState(voice: voice), presence)
        }
    }

    func testOfflineIgnoriertDenGrundtext() {
        XCTAssertEqual(PresenceState(voice: .offline("a")), .offline)
        XCTAssertEqual(PresenceState(voice: .offline("b")), .offline)
    }

    // Die Zielwerte der Referenz, woertlich: zuhoeren in=1/wave=0.55,
    // denken swirl=1, sprechen out=1/wave=1/pulse=1, deepwork orbit=1.
    func testZielwerteEntsprechenDerReferenz() {
        let hoeren = PresenceParameters.target(for: .listening)
        XCTAssertEqual(hoeren.rippleIn, 1); XCTAssertEqual(hoeren.wave, 0.55)
        let denken = PresenceParameters.target(for: .thinking)
        XCTAssertEqual(denken.swirl, 1)
        let sprechen = PresenceParameters.target(for: .speaking)
        XCTAssertEqual(sprechen.rippleOut, 1)
        XCTAssertEqual(sprechen.wave, 1)
        XCTAssertEqual(sprechen.pulse, 1)
        let tief = PresenceParameters.target(for: .deepWork)
        XCTAssertEqual(tief.orbit, 1)
        let ruhe = PresenceParameters.target(for: .idle)
        XCTAssertEqual(ruhe, PresenceParameters())
    }

    // Verbindungswahrheit: offline dimmt deutlich unter idle, reconnecting
    // verliert Zusammenhalt — schlafend bzw. aufgeloest, nie einfach weg.
    func testVerbindungszustaendeDimmenStattVerschwinden() {
        let offline = PresenceParameters.target(for: .offline)
        let ruhe = PresenceParameters.target(for: .idle)
        XCTAssertLessThan(offline.brightness, ruhe.brightness)
        XCTAssertGreaterThan(offline.brightness, 0, "offline darf nie schwarz sein")
        let verbindet = PresenceParameters.target(for: .reconnecting)
        XCTAssertLessThan(verbindet.coherence, 0.5)
    }

    // REFOCUS: Sprechen -> Zuhoeren snappt (>90 % des Weges in 0,1 s), ein
    // gewoehnlicher Wechsel gleitet (<90 % in derselben Zeit). Der
    // Lautsprecher ist bei der Unterbrechung schon still — das Bild muss mit.
    func testUnterbrechungSnapptGewoehnlichesGleitet() {
        var schnell = PresenceMotionModel(state: .speaking)
        for _ in 0..<10 { schnell.step(dt: 0.02) }
        schnell.transition(to: .listening)
        for _ in 0..<5 { schnell.step(dt: 0.02) }
        let schnellFortschritt = schnell.parameters.rippleIn
        XCTAssertGreaterThan(schnellFortschritt, 0.9,
                             "die Unterbrechung kam nicht sofort an")

        var langsam = PresenceMotionModel(state: .idle)
        langsam.transition(to: .listening)
        for _ in 0..<5 { langsam.step(dt: 0.02) }
        XCTAssertLessThan(langsam.parameters.rippleIn, 0.9,
                          "ein gewoehnlicher Wechsel darf nicht springen")
    }

    // Ein wiederholter Wechsel in denselben Zustand ist ein No-Op — auch mit
    // abrupt:true darf er keinen Snap nachruesten.
    func testDoppelterWechselIstWirkungslos() {
        var modell = PresenceMotionModel(state: .listening)
        for _ in 0..<10 { modell.step(dt: 0.02) }
        let vorher = modell.parameters
        modell.transition(to: .listening, abrupt: true)
        modell.step(dt: 0.001)
        XCTAssertEqual(modell.parameters.rippleIn, vorher.rippleIn, accuracy: 0.01)
    }

    // Die Erwachen-Phasen: Fenster wie in der Referenz, Brille snappt in der
    // Flash-Phase, Maskottchen uebernimmt bei der Zuendung.
    func testErwachenPhasenFolgenDerReferenz() {
        let vorher = AwakenPhases(T: 0)
        XCTAssertEqual(vorher.settled, 0)
        XCTAssertEqual(vorher.mascotOpacity, 0)
        let mitte = AwakenPhases(T: 1.6)
        XCTAssertGreaterThan(mitte.flash, 0); XCTAssertLessThan(mitte.flash, 1)
        XCTAssertEqual(mitte.mascotOpacity, 0)
        let fertig = AwakenPhases(T: 3.0)
        XCTAssertEqual(fertig.settled, 1)
        XCTAssertEqual(fertig.mascotOpacity, 1)
        XCTAssertEqual(fertig.glassesOpacity, 1)
        XCTAssertEqual(AwakenPhases.done.settled, 1)
    }
}
