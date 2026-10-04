// Die Galerie — NUR fuer die visuelle Abnahme, nie fuer den Nutzer.
//
// Prozedurale Gestaltung laesst sich nicht am Code beurteilen, nur am Bild.
// Diese Ansicht zeigt jeden Presence-Zustand einzeln, damit die visuelle
// Schleife (rendern, ansehen, verwerfen, nachschaerfen) echte Screenshots
// hat — auch fuer Zustaende, die am lebenden System selten sind
// (reconnecting, offline).
//
// Erreichbar ausschliesslich im DEBUG-Build ueber das Startargument
// `--presence-gallery`. Im Release existiert sie nicht.
// `import SwiftUI` steht AUSSERHALB von `#if DEBUG`, und das ist kein
// Schoenheitsfehler: `PresenceBackdrop` am Ende dieser Datei liegt hinter dem
// `#endif` und wird von `VoiceView` produktiv benutzt. Lag der Import innen,
// fehlte er im Release-Build — die App liess sich seit Presence V2 in Release
// gar nicht mehr uebersetzen, und niemand hat es bemerkt, weil die visuelle
// Abnahme damals mit einem Debug-Build lief.
import SwiftUI

#if DEBUG

struct PresenceGallery: View {
    @State private var index = PresenceGallery.requestedIndex
    @State private var level = 0.0
    @State private var pulse = ProcessInfo.processInfo.arguments.contains("--presence-level")

    static let states: [(String, PresenceState)] = [
        ("arriving", .arriving), ("idle", .idle), ("listening", .listening),
        ("thinking", .thinking), ("speaking", .speaking), ("deepwork", .deepWork),
        ("reconnecting", .reconnecting), ("offline", .offline),
    ]

    var body: some View {
        ZStack {
            PresenceBackdrop()
            VStack(spacing: 12) {
                Spacer()
                Text(Self.states[index].0)
                    .font(.caption.monospaced())
                    .foregroundStyle(.white.opacity(0.5))
                SolvioPresenceView(state: Self.states[index].1,
                                   audioLevel: simulatedLevel)
                HStack {
                    Button("◀") { index = (index + Self.states.count - 1) % Self.states.count }
                    Spacer()
                    Button(pulse ? "Pegel an" : "Pegel aus") { pulse.toggle() }
                    Spacer()
                    Button("▶") { index = (index + 1) % Self.states.count }
                }
                .font(.title3)
                .tint(.white.opacity(0.7))
                .padding(.horizontal, 40)
            }
        }
        .preferredColorScheme(.dark)
        .statusBarHidden()
    }

    /// Ein kuenstlicher Pegel NUR fuer die Galerie: am lebenden System kommt
    /// er aus Mikrofon bzw. Wiedergabe.
    private var simulatedLevel: Double {
        guard pulse else { return 0 }
        let t = Date.timeIntervalSinceReferenceDate
        return max(0, sin(t * 5.1) * 0.5 + sin(t * 13.7) * 0.3 + 0.2)
    }

    static var requested: Bool {
        ProcessInfo.processInfo.arguments.contains("--presence-gallery")
    }

    /// `--presence-state <name>` springt direkt zu einem Zustand — damit die
    /// visuelle Schleife jeden Zustand deterministisch fotografieren kann.
    static var requestedIndex: Int {
        let args = ProcessInfo.processInfo.arguments
        guard let at = args.firstIndex(of: "--presence-state"),
              args.indices.contains(at + 1),
              let found = states.firstIndex(where: { $0.0 == args[at + 1] })
        else { return 0 }
        return found
    }
}
#endif

/// Der tiefe SOLVIO-Raum hinter der Presence — bewusst in Hell wie Dunkel
/// derselbe: das Gespraech hat einen eigenen Ort.
struct PresenceBackdrop: View {
    var body: some View {
        LinearGradient(colors: [Color(hex: "#02060B"), Color(hex: "#07111D")],
                       startPoint: .top, endPoint: .bottom)
            .ignoresSafeArea()
    }
}
