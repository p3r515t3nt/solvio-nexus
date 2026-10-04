// SOLVIO NEXUS — die Ansicht.
//
// Fuellt ihren Container ganz, wie das Canvas der Referenz den Screen fuellt:
// `cx = w/2`, `cy = h*0.42`, `R = min(w,h)*0.235*breath`. Zwei Zeituhren wie
// dort — `t` fuer den Dauerlauf, `T` fuer das Erwachen ab `seqStart`.
//
// Ueber dem Canvas liegen die zwei Logo-Bilder aus dem Handoff: das volle
// Maskottchen und die Brille — GLEICHE viewBox, GLEICHE Breite, dadurch
// pixelgenau ueberlagert. Die Brille snappt gegen Ende der Flash-Phase ein,
// das Maskottchen uebernimmt bei der Zuendung (Vollgesicht-Modus, wie vom
// Nutzer entschieden). Beide atmen mit dem 3,4-s-Keyframe der Referenz.
//
// Reduzierte Bewegung: Sequenz auf Endzustand, Dauerlauf eingefroren — die
// Zustandswechsel bleiben als weiche Reglerfahrten sichtbar.
import SwiftUI

/// Bild-zu-Bild-Zustand. Eine Klasse mit stabiler Identitaet, damit die
/// Canvas-Klausur sie fortschreiben kann, ohne je Bild eine View-Invalidierung
/// anzustossen — die Zeit liefert ohnehin die TimelineView.
@MainActor
final class PresenceEngine {
    var model = PresenceMotionModel()
    var lastTick: Double?
    /// Die T-Uhr des Erwachens. Gesetzt beim ersten Bild im Zustand
    /// `arriving`; nil heisst: laengst wach.
    var seqStart: Double?
    var started = false
}

struct SolvioPresenceView: View {
    var state: PresenceState
    /// Fluechtiger Pegel 0…1 — Mikrofon beim Zuhoeren, Wiedergabe beim
    /// Sprechen. Wird gezeichnet und vergessen; nichts sammelt ihn.
    var audioLevel: Double = 0
    /// Start-Modus: in Ruhe loesen sich goldene Einladungsringe vom Kern —
    /// die Gestalt sagt sichtbar, dass sie beruehrt werden will.
    var invite: Bool = false

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var engine = PresenceEngine()

    /// Ruhe kostet keine Bildrate: idle/offline/ended laufen mit 30 Bildern
    /// voellig fluessig — die volle Rate gehoert den lebendigen Zustaenden.
    private var frameInterval: Double? {
        if reduceMotion { return 1.0 / 10 }
        switch state {
        case .idle, .offline, .ended: return 1.0 / 30
        default: return nil
        }
    }

    var body: some View {
        GeometryReader { geo in
            TimelineView(.animation(minimumInterval: frameInterval)) { timeline in
                let now = timeline.date.timeIntervalSinceReferenceDate
                let awaken = phases(now: now)
                ZStack {
                    Canvas { ctx, size in
                        let dt = min(0.1, max(0.001, now - (engine.lastTick ?? now)))
                        engine.lastTick = now
                        // `arriving` ist die Sequenz, kein Reglerzustand: die
                        // Regler laufen waehrenddessen auf idle zu.
                        engine.model.transition(to: state == .arriving ? .idle : state)
                        engine.model.step(dt: dt)
                        PresenceRenderer.draw(
                            in: ctx, size: size,
                            parameters: engine.model.parameters,
                            awaken: awaken,
                            audio: reduceMotion ? 0 : audioLevel,
                            t: reduceMotion ? 0 : now,
                            state: state,
                            invite: invite)
                    }
                    overlay(in: geo.size, awaken: awaken)
                }
            }
        }
        .accessibilityHidden(true)
    }

    /// Die Phasen der T-Uhr — bei reduzierter Bewegung sofort der Endzustand.
    private func phases(now: Double) -> AwakenPhases {
        if reduceMotion { return .done }
        if state == .arriving {
            if engine.seqStart == nil { engine.seqStart = now }
        } else if !engine.started {
            // Ohne Erwachen (z. B. Sprachraum direkt): laengst wach.
            engine.seqStart = now - 10
        }
        engine.started = true
        guard let start = engine.seqStart else { return .done }
        return AwakenPhases(T: now - start)
    }

    /// Maskottchen + Brille — pixelgenau ueberlagert wie in der Referenz
    /// (gleiche viewBox, gleiche Breite), mittig auf der 42-%-Achse.
    @ViewBuilder
    private func overlay(in size: CGSize, awaken: AwakenPhases) -> some View {
        // Bildbreite relativ zum ungeatmeten Basisradius, wie im Prototyp
        // (118 px bei R≈92 -> Faktor ~1.283).
        let base = min(size.width, size.height) * 0.235
        let width = base * 1.283
        let center = CGPoint(x: size.width / 2, y: size.height * 0.42)
        let gOp = awaken.glassesOpacity
        let mOp = awaken.mascotOpacity
        let dim = engine.model.parameters.brightness
        ZStack {
            Image("SolvioMascot")
                .resizable().scaledToFit()
                .frame(width: width)
                .opacity(mOp * dim)
            Image("SolvioGlasses")
                .resizable().scaledToFit()
                .frame(width: width)
                .opacity(max(0, gOp - mOp) * dim)
        }
        .modifier(BreatheScale(active: !reduceMotion))
        .position(center)
        .allowsHitTesting(false)
    }
}

/// Der `breathe`-Keyframe der Referenz: scale 1 -> 1.04 -> 1, 3,4 s.
private struct BreatheScale: ViewModifier {
    let active: Bool
    @State private var up = false
    func body(content: Content) -> some View {
        content
            .scaleEffect(active && up ? 1.04 : 1.0)
            .animation(active ? .easeInOut(duration: 1.7).repeatForever(autoreverses: true)
                              : nil, value: up)
            .onAppear { up = true }
    }
}

// MARK: - Ankunft (Einblendung der umgebenden Inhalte)

/// Der Inhalt unter dem Orb blendet weich ein — er ist vom ersten Bild an
/// bedienbar; nichts wartet auf die Sequenz.
struct PresenceArrival: ViewModifier {
    @State private var arrived = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    func body(content: Content) -> some View {
        content
            .opacity(arrived ? 1 : 0)
            .animation(.easeOut(duration: reduceMotion ? 0.2 : 0.6), value: arrived)
            .onAppear { arrived = true }
    }
}

extension View {
    func presenceArrival() -> some View { modifier(PresenceArrival()) }
}
