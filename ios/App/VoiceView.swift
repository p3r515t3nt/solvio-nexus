// Der Sprachraum.
//
// Ein eigener Ort: in Hell wie Dunkel derselbe tiefe Nachtblau-Raum. In der
// Mitte wohnt die Presence — SOLVIOs sichtbare Aufmerksamkeit, die sich mit
// dem Gespraech verwandelt. Der Text weicht an den unteren Rand, damit die
// Gestalt den Raum traegt.
//
// Zwei Dinge sind nicht verhandelbar:
// * Der Mikrofonzustand steht IMMER in Worten auf dem Bildschirm.
// * Der Weg hinaus ist IMMER sichtbar (Beenden unten, Minimieren oben).
//
// Jeder Zustand kommt aus einem echten Ereignis der Sitzung — kein Zeitgeber
// spielt hier etwas vor. Die Abbildung auf die Presence geht ausschliesslich
// ueber PresenceState(voice:); eine zweite Zuordnung hier waere eine zweite
// Wahrheit. Fachbegriffe des Cores kommen nicht vor: innen sind es viele
// Spezialisten, nach aussen ist es EIN SOLVIO.
import SwiftUI

struct VoiceView: View {
    @ObservedObject var model: AppModel
    /// C3: der Chat, den diese Sitzung binden soll (nil = ungebunden wie bisher).
    var conversationID: String? = nil
    @StateObject private var voice = VoiceSession()
    @Environment(\.dismiss) private var dismiss
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        ZStack {
            PresenceBackdrop()
            // Der Orb fuellt den ganzen Raum — cx = w/2, cy = 0.42h wie in
            // der Nexus-Referenz. Der Statustext unten bleibt darueber lesbar.
            SolvioPresenceView(state: PresenceState(voice: voice.state),
                               audioLevel: voice.audioLevel)
                .ignoresSafeArea()

            VStack(spacing: 0) {
                topBar
                Spacer()
Color.clear
                Spacer()
                statusBlock
                    .padding(.bottom, 28)
                bottomControls
            }
            .padding(.horizontal, Theme.Space.margin)
        }
        .preferredColorScheme(.dark)
        .task { await start() }
        .onChange(of: voice.state) { _ in Haptics.state() }
        .onChange(of: voice.dismissNow) { done in
            // Kein Abschiedssatz, keine Haptik, kein Zwischenbild.
            if done { dismiss() }
        }
        .onChange(of: scenePhase) { phase in
            // Kein verdecktes Weiterhoeren: geht die App in den Hintergrund,
            // endet das Gespraech und das Mikrofon wird freigegeben. Es gibt
            // dafuer ausdruecklich KEINE Hintergrund-Berechtigung.
            if phase != .active { voice.end(reason: "background"); dismiss() }
        }
        .onDisappear { voice.end(reason: "closed") }
    }

    private func start() async {
        guard let pairing = model.pairing, let enrollment = model.enrollmentForVoice else {
            return
        }
        await voice.begin(pairing: pairing, deviceID: enrollment.deviceID,
                          transportCred: enrollment.transportCred, conversationID: conversationID)
    }

    // MARK: oben

    private var topBar: some View {
        HStack {
            HStack(spacing: 8) {
                Mark(size: 26)
                Wordmark(size: .subheadline, color: .white.opacity(0.9))
            }
            Spacer()
            Button {
                Haptics.tap(); voice.end(reason: "user"); dismiss()
            } label: {
                Image(systemName: "chevron.down")
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(.white.opacity(0.7))
                    .frame(width: 44, height: 44)
                    .background(.white.opacity(0.08), in: Circle())
            }
            .accessibilityLabel("Gespräch beenden und schließen")
        }
        .padding(.top, 12)
    }

    // MARK: unten — der Text

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
            if case .offline = voice.state {
                Button {
                    Haptics.tap()
                    Task { await voice.retry() }
                } label: {
                    Text("Erneut versuchen")
                        .font(.body.weight(.semibold))
                        .foregroundStyle(Theme.voiceTop)
                        .padding(.horizontal, 22).padding(.vertical, 12)
                        .background(.white, in: Capsule())
                }
                .padding(.top, 8)
            }
            if voice.state == .deepWork {
                Text("Du kannst die App schließen")
                    .font(.footnote.weight(.medium))
                    .foregroundStyle(.white.opacity(0.85))
                    .padding(.horizontal, 18).padding(.vertical, 9)
                    .background(.white.opacity(0.12), in: Capsule())
                    .padding(.top, 8)
            }
        }
        .frame(maxWidth: 320)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(a11yLabel)
    }

    private var headline: String {
        switch voice.state {
        case .ready:        return "Einen Moment …"
        case .listening:    return "Hört zu …"
        case .thinking:     return "Denkt nach …"
        case .speaking:     return "SOLVIO spricht"
        case .deepWork:     return "Ich schaue das gründlich nach."
        case .reconnecting: return "Verbindung wird wiederhergestellt …"
        case .offline:      return "SOLVIO ist gerade nicht erreichbar."
        case .ended:        return "Bis später."
        }
    }

    private var subline: String? {
        switch voice.state {
        case .ready:        return "SOLVIO ist gleich für dich da."
        case .listening:    return "Sprich einfach — SOLVIO hört dir zu."
        case .thinking:     return nil
        case .speaking:     return nil
        case .deepWork:
            return "Das dauert ein paar Minuten. Du bekommst einen Hinweis, sobald es fertig ist."
        case .reconnecting: return "Das Gespräch geht gleich weiter."
        case let .offline(why): return why
        case .ended:        return nil
        }
    }

    private var a11yLabel: String {
        let mic = voice.micActive ? "Mikrofon aktiv." : "Mikrofon aus."
        return "\(headline) \(subline ?? "") \(mic)"
    }

    // MARK: unten — die Bedienung

    private var bottomControls: some View {
        VStack(spacing: 14) {
            // Die Mikrofonwahrheit: immer in Worten, nie nur als Farbe.
            HStack(spacing: 8) {
                Image(systemName: voice.micActive ? "mic.fill" : "mic.slash.fill")
                    .font(.footnote.weight(.semibold))
                Text(voice.micActive ? "Mikrofon aktiv" : "Mikrofon aus")
                    .font(.footnote.weight(.medium))
            }
            // Leiser als zuvor: die Pille sass mitten auf der Animation und
            // deckte sie zu. Die Wahrheit bleibt in Worten — sie muss nur
            // nicht leuchten wie ein Knopf.
            .foregroundStyle(voice.micActive ? Color(hex: "#9CC6EF") : .white.opacity(0.6))
            .padding(.horizontal, 11).padding(.vertical, 5)
            .background(.white.opacity(voice.micActive ? 0.10 : 0.06), in: Capsule())
            .overlay(Capsule().strokeBorder(.white.opacity(0.10), lineWidth: 1))

            HStack(spacing: 44) {
                control(symbol: voice.muted ? "mic.slash.fill" : "mic.fill",
                        label: voice.muted ? "Mikro an" : "Stumm", end: false) {
                    voice.muted.toggle()
                }
                control(symbol: "xmark", label: "Beenden", end: true) {
                    voice.end(reason: "user"); dismiss()
                }
                // Der dritte Platz des Entwurfs („Tippen") bleibt frei, solange
                // es keine Texteingabe gibt — ein Knopf, der nichts tut, ist
                // ein gebrochenes Versprechen.
                Color.clear.frame(width: 54, height: 54)
                    .accessibilityHidden(true)
            }
        }
        .padding(.bottom, 26)
    }

    private func control(symbol: String, label: String, end: Bool,
                         action: @escaping () -> Void) -> some View {
        VStack(spacing: 8) {
            Button { Haptics.tap(); action() } label: {
                Image(systemName: symbol)
                    .font(.system(size: end ? 24 : 19, weight: .semibold))
                    .foregroundStyle(end ? Color.white : .white.opacity(0.85))
                    .frame(width: end ? 68 : 54, height: end ? 68 : 54)
                    .background(end ? AnyShapeStyle(Theme.bad)
                                    : AnyShapeStyle(.white.opacity(0.10)),
                                in: Circle())
            }
            .buttonStyle(PressScale())
            Text(label).font(.caption2).foregroundStyle(.white.opacity(0.6))
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(label)
    }
}
