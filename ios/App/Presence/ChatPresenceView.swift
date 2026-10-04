import SwiftUI

/// Nur eine Anzeigeprojektion. Verlauf, Aufträge und Audio bleiben bei ihren
/// vorhandenen Besitzern; insbesondere ist ein offener Auftrag nicht immer Arbeit.
struct ChatPresencePresentation {
    let state: PresenceState
    let title: String
    var audioLevel: Double = 0
    var voiceActive = false
    var microphoneActive = false
    var shortTitle: String? = nil

    static func voice(state: VoiceState, status: String, audioLevel: Double,
                      microphoneActive: Bool, ending: Bool = false) -> Self {
        let listeningMuted = state == .listening && !microphoneActive
        let levelIsCurrent = state == .speaking || (state == .listening && microphoneActive)
        let short: String
        switch state {
        case .ready: short = "Verbindet …"
        case .listening: short = microphoneActive ? "Hört zu" : "Stumm"
        case .thinking: short = "Denkt nach"
        case .speaking: short = "Spricht"
        case .deepWork: short = "Arbeitet"
        case .reconnecting: short = "Verbindet …"
        case .offline: short = "Verbindung fehlt"
        case .ended: short = "Beendet"
        }
        return Self(state: ending || listeningMuted ? .idle : PresenceState(voice: state),
                    title: status,
                    audioLevel: !ending && levelIsCurrent && audioLevel.isFinite ? min(1, max(0, audioLevel)) : 0,
                    voiceActive: true, microphoneActive: !ending && microphoneActive,
                    shortTitle: ending ? "Speichert …" : short)
    }

    static func text(detail: ConversationDetail?, sending: Bool, reachable: Bool,
                     detailUnavailable: Bool) -> Self {
        guard reachable else { return Self(state: .offline, title: "Verbindung unterbrochen", shortTitle: "Offline") }
        if sending { return Self(state: .idle, title: "Nachricht wird übermittelt …", shortTitle: "Sendet …") }
        guard !detailUnavailable else { return Self(state: .idle, title: "Chatstand nicht bestätigt", shortTitle: "Stand unbestätigt") }
        if detail?.deliveries_open ?? 0 > 0 || detail?.messages.contains(where: { $0.delivery?.isOpen == true }) == true {
            return Self(state: .thinking, title: "SOLVIO bearbeitet deine Nachricht", shortTitle: "Bearbeitet Nachricht")
        }
        let runs = detail?.auftraege ?? []
        if runs.contains(where: { $0.offen && ["PLANNING", "RUNNING", "WAITING_SPECIALIST", "VERIFYING"].contains($0.zustand_code) }) {
            return Self(state: .deepWork, title: "SOLVIO arbeitet in diesem Chat", shortTitle: "Arbeitet")
        }
        if runs.contains(where: { $0.offen }) {
            return Self(state: .idle, title: "Ein Auftrag wartet", shortTitle: "Auftrag wartet")
        }
        return Self(state: .idle, title: "SOLVIO ist bereit", shortTitle: "Bereit")
    }
}

/// Die vorhandene Nexus-Gestalt, eingebettet statt als eigener Sprachraum.
/// Wenig Höhe (auch durch Tastatur) und große Schrift geben dem Verlauf Platz.
struct ChatPresenceView: View {
    let presentation: ChatPresencePresentation
    let availableHeight: CGFloat
    var openWork = 0
    var workIsCurrent = true
    var canOpenChats = true
    var onOpenChats: () -> Void = {}
    @Environment(\.dynamicTypeSize) private var typeSize

    static func stageSize(voiceActive: Bool, availableHeight: CGFloat, accessible: Bool) -> CGFloat {
        voiceActive && availableHeight >= 520 && !accessible ? 156 : 88
    }

    var body: some View {
        let stage = typeSize.isAccessibilitySize || availableHeight < 420 ? 54.0 : 80.0
        Button(action: onOpenChats) {
            VStack(spacing: 1) {
                SolvioPresenceView(state: presentation.state, audioLevel: presentation.audioLevel)
                    .frame(width: 320, height: 320)
                    .scaleEffect(stage / 320)
                    .frame(width: stage, height: stage)
                    .frame(height: stage * 0.82, alignment: .top).clipped()
                    .allowsHitTesting(false).accessibilityHidden(true)
                Text("SOLVIO").font(.display(.headline, weight: .bold)).kerning(1.5)
                Text(presentation.shortTitle ?? presentation.title)
                    .font(.caption).foregroundStyle(Theme.ink2)
                    .multilineTextAlignment(.center).fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier("chat.presence.status")
                if openWork > 0 {
                    Text((workIsCurrent ? "Offene Vorgänge: " : "Zuletzt offen: ") + String(openWork))
                        .font(.caption2).foregroundStyle(Theme.ink2)
                        .accessibilityIdentifier("chat.background-work")
                }
            }
            .padding(.horizontal, 20).padding(.bottom, 8)
            .frame(maxWidth: .infinity).contentShape(Rectangle())
        }
        .buttonStyle(.plain).foregroundStyle(Theme.ink)
        .disabled(!canOpenChats)
        .accessibilityLabel("SOLVIO, " + presentation.title)
        .accessibilityHint("Zeigt die Aktivitäten")
        .accessibilityIdentifier("chat.presence")
    }
}
