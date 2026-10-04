// Der Sprachraum.
//
// Ein eigener Ort: in Hell wie Dunkel derselbe tiefe Nachtblau-Raum. Kein
// Equalizer, kein Neon, kein HUD — ein ruhiger Lichtkreis, der atmet, wenn
// SOLVIO zuhoert, schimmert, wenn es nachdenkt, und weiche Wellen zieht,
// wenn es spricht.
//
// Zwei Dinge sind nicht verhandelbar:
// * Der Mikrofonzustand steht IMMER in Worten auf dem Bildschirm.
// * Der Weg hinaus ist IMMER sichtbar (Beenden unten, Minimieren oben).
//
// Fachbegriffe (Provider, Session, Tools, Hermes) kommen hier nicht vor.
// Innen sind es viele Spezialisten — nach aussen ist es EIN SOLVIO.
import SwiftUI

struct VoiceView: View {
    @ObservedObject var model: DesignModel
    @Environment(\.dismiss) private var dismiss
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var micMuted = false

    private var state: VoiceState { model.voiceState }

    var body: some View {
        ZStack {
            LinearGradient(colors: [Theme.voiceTop, Theme.voiceBottom],
                           startPoint: .top, endPoint: .bottom)
                .ignoresSafeArea()

            VStack(spacing: 0) {
                topBar
                Spacer()
                VoiceOrb(state: state, reduceMotion: reduceMotion)
                    .frame(width: 220, height: 220)
                statusBlock
                    .padding(.top, 36)
                Spacer()
                bottomControls
            }
            .padding(.horizontal, Theme.Space.margin)
        }
        .preferredColorScheme(.dark)   // der Raum ist immer dunkel
        .onChange(of: state) { Haptics.state() }
    }

    // MARK: oben — Identitaet + Minimieren

    private var topBar: some View {
        HStack {
            HStack(spacing: 8) {
                Mark(size: 26)
                Wordmark(size: .subheadline, color: .white.opacity(0.9))
            }
            Spacer()
            Button {
                Haptics.tap(); dismiss()
            } label: {
                Image(systemName: "chevron.down")
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(.white.opacity(0.7))
                    .frame(width: 40, height: 40)
                    .background(.white.opacity(0.08), in: Circle())
            }
            .accessibilityLabel("Gespräch minimieren")
        }
        .padding(.top, 12)
    }

    // MARK: Mitte — der eine Satz zum Zustand

    private var statusBlock: some View {
        VStack(spacing: 10) {
            Text(headline)
                .font(.display(.title2, weight: .semibold))
                .foregroundStyle(.white)
                .multilineTextAlignment(.center)
            if let sub = subline {
                Text(sub)
                    .font(.subheadline)
                    .foregroundStyle(.white.opacity(0.65))
                    .multilineTextAlignment(.center)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if state == .offline {
                Button { Haptics.tap() } label: {
                    Text("Erneut versuchen")
                        .font(.body.weight(.semibold))
                        .foregroundStyle(Theme.voiceTop)
                        .padding(.horizontal, 22).padding(.vertical, 11)
                        .background(.white, in: Capsule())
                }
                .padding(.top, 8)
            }
            if state == .deepwork {
                Button { Haptics.tap(); dismiss() } label: {
                    Text("Du kannst die App schließen")
                        .font(.footnote.weight(.medium))
                        .foregroundStyle(.white.opacity(0.85))
                        .padding(.horizontal, 18).padding(.vertical, 9)
                        .background(.white.opacity(0.12), in: Capsule())
                }
                .padding(.top, 8)
            }
        }
        .frame(maxWidth: 320)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(a11yLabel)
    }

    private var headline: String {
        switch state {
        case .ready:        return "Einen Moment …"
        case .listening:    return "Hört zu …"
        case .thinking:     return "Denkt nach …"
        case .speaking:     return "SOLVIO spricht"
        case .interrupted:  return "Hört zu …"
        case .deepwork:     return "Ich schaue das gründlich nach."
        case .reconnecting: return "Verbindung wird wiederhergestellt …"
        case .offline:      return "SOLVIO ist gerade nicht erreichbar."
        case .ended:        return "Bis später."
        }
    }

    private var subline: String? {
        switch state {
        case .ready:        return "SOLVIO ist gleich für dich da."
        case .listening:    return "Sprich einfach — SOLVIO hört dir zu."
        case .speaking:     return "„Morgen hast du drei Termine. Der erste ist um zehn — die Besprechung wurde verschoben.“"
        case .interrupted:  return nil
        case .deepwork:     return "Das dauert ein paar Minuten. Du bekommst einen Hinweis, sobald es fertig ist."
        case .reconnecting: return "Das Gespräch geht gleich weiter."
        case .offline:      return "Prüfe, ob dein iPhone im Heimnetz ist."
        default:            return nil
        }
    }

    private var a11yLabel: String {
        let mic = micActive ? "Mikrofon aktiv." : "Mikrofon aus."
        return "\(headline) \(subline ?? "") \(mic)"
    }

    // MARK: unten — Mikrofonwahrheit + Ausgang

    private var micActive: Bool {
        if micMuted { return false }
        switch state {
        case .listening, .interrupted, .ready: return true
        default: return false
        }
    }

    private var bottomControls: some View {
        VStack(spacing: 22) {
            // Die Mikrofonwahrheit: immer in Worten, nie nur als Farbe.
            HStack(spacing: 8) {
                Image(systemName: micActive ? "mic.fill" : "mic.slash.fill")
                    .font(.footnote.weight(.semibold))
                Text(micActive ? "Mikrofon aktiv" : "Mikrofon aus")
                    .font(.footnote.weight(.medium))
            }
            .foregroundStyle(micActive ? Theme.voiceTop : .white.opacity(0.75))
            .padding(.horizontal, 14).padding(.vertical, 8)
            .background(micActive ? AnyShapeStyle(Color(hex: "#7FB5EA"))
                                  : AnyShapeStyle(.white.opacity(0.12)),
                        in: Capsule())

            HStack(spacing: 44) {
                controlButton(symbol: micMuted ? "mic.slash.fill" : "mic.fill",
                              label: micMuted ? "Mikro an" : "Stumm",
                              style: .quiet) {
                    micMuted.toggle()
                }
                controlButton(symbol: "xmark", label: "Beenden", style: .end) {
                    dismiss()
                }
                controlButton(symbol: "keyboard", label: "Tippen", style: .quiet) { }
            }
        }
        .padding(.bottom, 26)
    }

    private enum ControlStyle { case quiet, end }

    private func controlButton(symbol: String, label: String,
                               style: ControlStyle,
                               action: @escaping () -> Void) -> some View {
        VStack(spacing: 8) {
            Button { Haptics.tap(); action() } label: {
                Image(systemName: symbol)
                    .font(.system(size: style == .end ? 24 : 19, weight: .semibold))
                    .foregroundStyle(style == .end ? Color.white : .white.opacity(0.85))
                    .frame(width: style == .end ? 68 : 54,
                           height: style == .end ? 68 : 54)
                    .background(style == .end ? AnyShapeStyle(Theme.bad)
                                              : AnyShapeStyle(.white.opacity(0.10)),
                                in: Circle())
            }
            .buttonStyle(PressScale())
            Text(label)
                .font(.caption2)
                .foregroundStyle(.white.opacity(0.6))
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(label)
    }
}

// MARK: - Der Lichtkreis

struct VoiceOrb: View {
    let state: VoiceState
    let reduceMotion: Bool

    @State private var breathe = false
    @State private var spin = false
    @State private var rippleTick = false

    var body: some View {
        ZStack {
            // Wellen beim Sprechen: weiche konzentrische Ringe, goldwarm.
            if state == .speaking && !reduceMotion {
                ForEach(0..<3, id: \.self) { index in
                    Circle()
                        .stroke(Theme.gold.opacity(0.5), lineWidth: 1.5)
                        .scaleEffect(rippleTick ? 1.65 : 0.85)
                        .opacity(rippleTick ? 0 : 0.7)
                        .animation(Motion.ripple.delay(Double(index) * 0.6),
                                   value: rippleTick)
                }
            }

            // Atmender Ring beim Zuhoeren.
            if state == .listening || state == .interrupted || state == .ready {
                Circle()
                    .stroke(Color(hex: "#7FB5EA").opacity(0.45), lineWidth: 2)
                    .scaleEffect(breatheScale)
                    .animation(reduceMotion ? nil : Motion.breathe, value: breathe)
            }

            // Denk-Schimmer: ein umlaufender heller Bogen.
            if state == .thinking && !reduceMotion {
                Circle()
                    .trim(from: 0, to: 0.28)
                    .stroke(LinearGradient(colors: [Color(hex: "#7FB5EA"), .clear],
                                           startPoint: .leading, endPoint: .trailing),
                            style: StrokeStyle(lineWidth: 3, lineCap: .round))
                    .rotationEffect(.degrees(spin ? 360 : 0))
                    .animation(Motion.orbit, value: spin)
                    .padding(6)
            }

            // Der Kern.
            Circle()
                .fill(
                    RadialGradient(colors: coreColors,
                                   center: .center, startRadius: 8, endRadius: 110)
                )
                .padding(28)
                .scaleEffect(state == .listening && !reduceMotion ? (breathe ? 1.05 : 1.0) : 1.0)
                .animation(reduceMotion ? nil : Motion.breathe, value: breathe)
                .opacity(dimmed ? 0.45 : 1)

            // Die Marke wohnt im Kern — SOLVIO ist eine Person, kein Messgeraet.
            Mark(size: 64)
                .opacity(dimmed ? 0.35 : 0.92)
                .saturation(dimmed ? 0 : 1)
        }
        .onAppear {
            breathe = true; spin = true; rippleTick = true
        }
        .accessibilityHidden(true)
    }

    private var breatheScale: CGFloat {
        reduceMotion ? 1.0 : (breathe ? 1.10 : 0.98)
    }

    private var dimmed: Bool {
        state == .reconnecting || state == .offline || state == .ended
    }

    private var coreColors: [Color] {
        switch state {
        case .speaking:
            return [Color(hex: "#FFE9C4").opacity(0.9), Color(hex: "#2E86DE"), Color(hex: "#12365C")]
        case .offline, .reconnecting, .ended:
            return [Color(hex: "#8B97A8").opacity(0.5), Color(hex: "#3A4557"), Color(hex: "#1A2434")]
        default:
            return [Color(hex: "#BFDCF7"), Color(hex: "#2E86DE"), Color(hex: "#12365C")]
        }
    }
}
