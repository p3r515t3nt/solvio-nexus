// SOLVIO NEXUS — Zustand und Bewegung, portiert aus der Design-Referenz.
//
// Massgeblich ist die tick()-Methode des Handoffs (design_handoff_solvio_nexus,
// „SOLVIO Nexus Prototyp.dc.html"): dieselben Parameter, dieselben Zielwerte,
// derselbe weiche Lerp. Der Orb ist EIN Wesen mit sechs Reglern — jeder
// Zustand ist nur ein Zielpunkt darin, der Wechsel ist Interpolation, nie ein
// Schnitt.
//
// Was die Referenz nicht kennt, kennt die App trotzdem: reconnecting und
// offline sind ECHTE Zustaende der Sitzung und brauchen ein ehrliches Bild.
// Dafuer gibt es zwei Zusatzregler (coherence, brightness), die die
// Nexus-Ebenen dimmen und aufloesen, ohne den Referenz-Algorithmus zu
// veraendern.
import SwiftUI

// MARK: - Zustand

enum PresenceState: Equatable {
    /// Erwachen-Sequenz (Logo-Morph). Laeuft ueber die T-Uhr, nicht ueber Regler.
    case arriving
    case idle
    case listening
    case thinking
    case speaking
    case deepWork
    case reconnecting
    case offline
    case ended

    init(voice: VoiceState) {
        switch voice {
        case .ready:        self = .idle
        case .listening:    self = .listening
        case .thinking:     self = .thinking
        case .speaking:     self = .speaking
        case .deepWork:     self = .deepWork
        case .reconnecting: self = .reconnecting
        case .offline:      self = .offline
        case .ended:        self = .ended
        }
    }
}

// MARK: - Die Regler (params der Referenz)

/// `{in, out, wave, swirl, orbit, pulse}` aus tick() — plus die zwei
/// App-Regler fuer Verbindungswahrheit. `in`/`out` heissen hier
/// rippleIn/rippleOut, weil `in` in Swift ein Schluesselwort ist.
struct PresenceParameters: Equatable {
    var rippleIn: Double = 0
    var rippleOut: Double = 0
    var wave: Double = 0
    var swirl: Double = 0
    var orbit: Double = 0
    var pulse: Double = 0
    /// App-Erweiterung: Strukturzusammenhalt. reconnecting loest auf.
    var coherence: Double = 1
    /// App-Erweiterung: Gesamthelligkeit. offline dimmt, loescht nie.
    var brightness: Double = 1
}

extension PresenceParameters {
    /// Die Zielwerte je Modus — woertlich aus tick():
    ///   zuhoeren: in=1, wave=0.55 · denken: swirl=1
    ///   sprechen: out=1, wave=1, pulse=1 · deepwork: orbit=1 · home: 0
    static func target(for state: PresenceState) -> PresenceParameters {
        var p = PresenceParameters()
        switch state {
        case .listening:    p.rippleIn = 1; p.wave = 0.55
        case .thinking:     p.swirl = 1
        case .speaking:     p.rippleOut = 1; p.wave = 1; p.pulse = 1
        case .deepWork:     p.orbit = 1
        case .reconnecting: p.coherence = 0.35; p.brightness = 0.75
        case .offline:      p.coherence = 0.8; p.brightness = 0.38
        case .ended:        p.brightness = 0.6
        case .arriving, .idle: break
        }
        return p
    }
}

// MARK: - Erwachen (die T-Uhr)

/// Die Phasen der Erwachen-Sequenz — Zeitfenster und Easing woertlich aus der
/// Referenz: `ph(a,b) = clamp((T-a)/(b-a),0,1)`, smoothstep.
struct AwakenPhases {
    let swoosh, headp, flash, ignite, settled: Double

    init(T: Double) {
        func ph(_ a: Double, _ b: Double) -> Double {
            min(1, max(0, (T - a) / (b - a)))
        }
        swoosh = ph(0.1, 0.85)
        headp = ph(0.5, 1.5)
        flash = ph(1.35, 1.75)
        ignite = ph(2.0, 2.5)
        settled = ph(2.1, 2.75)
    }

    /// Abgeschlossen — alles sichtbar, keine Morph-Linien mehr.
    static let done = AwakenPhases(T: 10)

    /// Brille snappt gegen Ende der Flash-Phase ein.
    var glassesOpacity: Double { ease(min(1, max(0, (flash - 0.45) / 0.4))) }
    /// Volles Maskottchen uebernimmt bei der Zuendung.
    var mascotOpacity: Double { ease(ignite) }
}

func ease(_ x: Double) -> Double {
    x <= 0 ? 0 : x >= 1 ? 1 : x * x * (3 - 2 * x)
}

// MARK: - Bewegung

/// Der Lerp der Referenz: `p += (ziel - p) * 0.05` je Frame bei 60 fps —
/// hier bildratenunabhaengig als `k = 1 - 0.95^(dt*60)`.
///
/// REFOCUS bleibt die eine App-Ausnahme: die Unterbrechung (Sprechen ->
/// Zuhoeren) snappt, weil der Lautsprecher in diesem Moment schon still IST —
/// ein Bild, das weiterspricht, waere eine kleine Luege mit grosser Wirkung.
struct PresenceMotionModel {
    private(set) var parameters = PresenceParameters()
    private(set) var state: PresenceState = .idle
    private var snap: Double = 0

    init(state: PresenceState = .idle) {
        self.state = state
        if state != .arriving { parameters = .target(for: state) }
    }

    mutating func transition(to newState: PresenceState, abrupt: Bool = false) {
        guard newState != state else { return }
        let wasSpeaking = (state == .speaking)
        state = newState
        snap = (abrupt || (wasSpeaking && newState == .listening)) ? 1.0 : 0.0
    }

    mutating func step(dt: Double) {
        let target = PresenceParameters.target(for: state)
        let k = snap > 0.5 ? min(1.0, dt / 0.05)
                           : 1 - pow(0.95, dt * 60)
        parameters.rippleIn   += (target.rippleIn   - parameters.rippleIn)   * k
        parameters.rippleOut  += (target.rippleOut  - parameters.rippleOut)  * k
        parameters.wave       += (target.wave       - parameters.wave)       * k
        parameters.swirl      += (target.swirl      - parameters.swirl)      * k
        parameters.orbit      += (target.orbit      - parameters.orbit)      * k
        parameters.pulse      += (target.pulse      - parameters.pulse)      * k
        parameters.coherence  += (target.coherence  - parameters.coherence)  * k
        parameters.brightness += (target.brightness - parameters.brightness) * k
        if snap > 0 { snap = max(0, snap - dt * 6) }
    }
}
