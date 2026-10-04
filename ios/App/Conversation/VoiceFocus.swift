import SwiftUI

/// Flüchtiger Ansichtswechsel. VoiceSession und der kanonische Chat bleiben
/// Besitzer von Sprache und Verlauf; dieser Zustand bestätigt keinen Abschluss.
@MainActor
final class VoiceFocus: ObservableObject {
    enum Phase: Equatable { case hidden, preparing, active, finishing, waitingForForeground, failed }
    @Published private(set) var phase: Phase = .hidden
    @Published private(set) var notice = ""
    private(set) var canRetryHistory = false
    private(set) var conversationID: String?
    private var generation = UUID()
    private var foreground = true
    func setForeground(_ active: Bool) { foreground = active }
    var isPresented: Bool { phase != .hidden }

    @discardableResult func present() -> UUID {
        generation = UUID(); conversationID = nil; notice = ""; canRetryHistory = false; phase = .preparing
        return generation
    }
    func bind(_ id: String, attempt: UUID) -> Bool {
        guard generation == attempt, phase == .preparing, ConversationSummary.validID(id) else { return false }
        conversationID = id; phase = .active; return true
    }
    func startFailed(_ message: String) { canRetryHistory = false; notice = message; phase = .failed }
    func dismiss() { generation = UUID(); phase = .hidden; conversationID = nil; notice = ""; canRetryHistory = false }

    /// Erst der passende Flush, dann ein NEU begonnener Detailabruf. Ein alter
    /// Poll darf nicht als Nachweis des gerade abgeschlossenen Sprachverlaufs gelten.
    func finish(chats: ConversationModel, stillBound: () -> Bool,
                waitForClose: () async throws -> Void,
                retryDelay: (UInt64) async throws -> Void = { try await Task.sleep(nanoseconds: $0) },
                load: (String) async throws -> ConversationDetail) async {
        guard phase == .active || phase == .failed || phase == .waitingForForeground,
              let id = conversationID else { return }
        let attempt = generation
        phase = .finishing; notice = ""
        do {
            try await waitForClose()
            guard generation == attempt else { return }
            canRetryHistory = true
            guard foreground else { phase = .waitingForForeground; return }
            let deadline = Date().addingTimeInterval(15)
            while chats.loadingDetail {
                guard foreground else { phase = .waitingForForeground; return }
                guard Date() < deadline, stillBound(), chats.selectedID == id else { throw ClientError.decode }
                try await Task.sleep(nanoseconds: 50_000_000)
                guard generation == attempt else { return }
            }
            guard stillBound(), chats.selectedID == id, !Task.isCancelled else { throw ClientError.decode }
            // A confirmed close is not a successful history read. After unlock,
            // allow the pinned connection a bounded chance to recover; never
            // repeat a voice session or a message to repair a read.
            for attemptNumber in 0..<3 {
                guard foreground else { phase = .waitingForForeground; return }
                var freshReadSucceeded = false
                var readError: Error?
                await chats.refreshDetail { requestedID in
                    do {
                        guard requestedID == id, stillBound() else { throw ClientError.decode }
                        let value = try await load(requestedID)
                        guard value.conversation.conversation_id == id else { throw ClientError.decode }
                        freshReadSucceeded = true
                        return value
                    } catch { readError = error; throw error }
                }
                guard generation == attempt else { return }
                guard foreground else { phase = .waitingForForeground; return }
                guard stillBound(), chats.selectedID == id, !Task.isCancelled else { throw ClientError.decode }
                if freshReadSucceeded && chats.detailError.isEmpty && chats.detail?.conversation.conversation_id == id { break }
                guard attemptNumber < 2, let error = readError, Self.transientReadFailure(error) else {
                    throw readError ?? ClientError.decode
                }
                try await retryDelay(UInt64(attemptNumber + 1) * 1_000_000_000)
                guard generation == attempt else { return }
            }
            dismiss()
        } catch {
            guard generation == attempt else { return }
            if canRetryHistory && !foreground { phase = .waitingForForeground; return }
            notice = error is VoiceHandoffError
                ? "Der Sprachabschluss ist nicht bestätigt. Der letzte Sprachabschnitt kann im Chat fehlen."
                : "Der Chatverlauf konnte nicht frisch geladen werden. Dein Entwurf bleibt erhalten."
            phase = .failed
        }
    }

    private static func transientReadFailure(_ error: Error) -> Bool {
        if let network = error as? URLError {
            return [.timedOut, .networkConnectionLost, .notConnectedToInternet,
                    .cannotConnectToHost, .cannotFindHost, .dnsLookupFailed].contains(network.code)
        }
        if case ClientError.http(let status) = error { return [502, 503, 504].contains(status) }
        return false
    }

}

/// Große, vorhandene SOLVIO-Gestalt; keine eigene Sitzung und kein Autostart.
struct VoiceFocusView<Controls: View>: View {
    let presentation: ChatPresencePresentation
    var openWork = 0
    var workIsCurrent = true
    let controls: Controls
    @Environment(\.dynamicTypeSize) private var typeSize

    init(presentation: ChatPresencePresentation, openWork: Int, workIsCurrent: Bool,
         @ViewBuilder controls: () -> Controls) {
        self.presentation = presentation; self.openWork = openWork
        self.workIsCurrent = workIsCurrent; self.controls = controls()
    }

    var body: some View {
        GeometryReader { geometry in
            ScrollView {
                VStack(spacing: 12) {
                    Spacer(minLength: 0)
                    let stage = min(420, max(0, geometry.size.width - 40), max(180, geometry.size.height * 0.63))
                    SolvioPresenceView(state: presentation.state, audioLevel: presentation.audioLevel)
                        .frame(width: 320, height: 320)
                        .scaleEffect(stage / 320)
                        .frame(width: stage, height: stage)
                        .frame(height: stage * 0.84, alignment: .top)
                        .clipped().allowsHitTesting(false)
                    Text(typeSize.isAccessibilitySize ? presentation.shortTitle ?? presentation.title : presentation.title)
                        .font(typeSize.isAccessibilitySize ? .headline : .title2.weight(.semibold))
                        .multilineTextAlignment(.center).fixedSize(horizontal: false, vertical: true)
                        .accessibilityIdentifier("chat.voice.focus.status").accessibilityLabel(presentation.title)
                    if openWork > 0 {
                        Text((workIsCurrent ? "Offene Vorgänge: " : "Zuletzt offen: ") + String(openWork))
                            .font(.caption).foregroundStyle(.white.opacity(0.7))
                            .accessibilityIdentifier("chat.background-work")
                    }
                    Spacer(minLength: 0)
                }
                .padding(.horizontal, 20).frame(maxWidth: .infinity)
                .frame(minHeight: geometry.size.height)
            }
        }
        .safeAreaInset(edge: .bottom, spacing: 0) {
            controls.padding(.horizontal, 20).padding(.vertical, 16)
                .frame(maxWidth: .infinity).background(Theme.voiceTop)
        }
        .foregroundStyle(.white).background(Theme.voiceTop)
        .accessibilityIdentifier("chat.voice.focus")
    }
}
