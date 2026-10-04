// Ein Chat, eine Eingabe. Der Core bleibt Besitzer von Verlauf und Auftraegen.
import SwiftUI
import SolvioApprovalsKit

struct HomeView: View {
    @ObservedObject var model: AppModel
    let open: (HomeRoute) -> Void
    @Environment(\.dynamicTypeSize) private var typeSize
    @Environment(\.scenePhase) private var scenePhase
    @StateObject private var chats: ConversationModel
    @StateObject private var composer: MessageComposerModel
    @StateObject private var voice = VoiceSession()
    @StateObject private var voiceFocus: VoiceFocus
    @State private var showChats = false
    @State private var reconciled = ""
    @State private var preparingVoice = false
    @State private var voiceAttempt = UUID()
    @State private var voiceNotice = ""
    #if DEBUG
    // Synthetische Hosting-Bilder, ohne Sitzung, Berechtigung oder Mikrofon.
    var previewPresence: ChatPresencePresentation? = nil
    #endif

    init(model: AppModel, open: @escaping (HomeRoute) -> Void,
         chats: ConversationModel? = nil, voiceFocus: VoiceFocus? = nil,
         composer: MessageComposerModel? = nil) {
        self.model = model; self.open = open
        _chats = StateObject(wrappedValue: chats ?? ConversationModel())
        _voiceFocus = StateObject(wrappedValue: voiceFocus ?? VoiceFocus())
        _composer = StateObject(wrappedValue: composer ?? MessageComposerModel())
    }

    private var clientBinding: String {
        (model.client?.pairedCoreInstanceID ?? "") + ":" + (model.client?.deviceIdentifier ?? "")
    }
    private var voiceBusy: Bool {
        #if DEBUG
        if let previewPresence { return previewPresence.voiceActive }
        #endif
        return preparingVoice || voice.isActive || voice.handoff.state == .ending
    }
    private var presence: ChatPresencePresentation {
        #if DEBUG
        if let previewPresence { return previewPresence }
        #endif
        if preparingVoice { return .init(state: .idle, title: "Chat wird vorbereitet …", voiceActive: true) }
        if voiceBusy {
            return .voice(state: voice.state, status: voice.chatStatus, audioLevel: voice.audioLevel,
                          microphoneActive: voice.micActive, ending: voice.handoff.state == .ending)
        }
        if voice.handoff.hasUncertainty(in: chats.selectedID ?? "") {
            return .init(state: .idle, title: "Sprachabschluss nicht bestätigt", shortTitle: "Abschluss unklar")
        }
        if currentVoiceFailure { return .init(state: .offline, title: voice.chatStatus, shortTitle: "Verbindung fehlt") }
        return .text(detail: chats.detail, sending: composer.sending, reachable: model.reachable,
                     detailUnavailable: !chats.detailError.isEmpty || (chats.selectedID != nil && chats.detail == nil))
    }
    private var currentVoiceFailure: Bool {
        guard let id = chats.selectedID else { return false }
        return voice.handoff.hasUncertainty(in: id) || (voice.conversationID == id && voice.hasVisibleFailure)
    }
    private var focused: Bool { voiceFocus.isPresented || previewVoiceActive }
    private var selectionLocked: Bool { composer.sending || chats.creating || voiceBusy || focused }
    private var focusPresence: ChatPresencePresentation {
        if voiceFocus.phase == .finishing { return .init(state: .idle, title: "Gespräch wird abgeschlossen …") }
        if voiceFocus.phase == .failed { return .init(state: .offline, title: "Mikrofon aus") }
        return presence
    }

    var body: some View {
        ZStack {
        if focused {
            VoiceFocusView(presentation: focusPresence, openWork: chats.workingCount,
                           workIsCurrent: chats.listError.isEmpty && model.reachable) { focusControls }
        } else {
        GeometryReader { geometry in
        VStack(spacing: 0) {
            header
            Divider().overlay(Theme.line)
            ChatPresenceView(presentation: presence, availableHeight: geometry.size.height,
                             openWork: chats.workingCount, workIsCurrent: chats.listError.isEmpty && model.reachable,
                             canOpenChats: !selectionLocked,
                             onOpenChats: { if !selectionLocked { open(.activity) } })
            ScrollViewReader { scroll in
                ScrollView {
                    VStack(alignment: .leading, spacing: 18) {
                        if !model.reachable { OfflineCard(since: model.snapshotAt) }
                        if let notice = model.inbox.first(where: { !$0.gelesen }) {
                            NavigationLink { NoticeDetailView(model: model, item: notice) } label: {
                                NoticeCard(item: notice)
                            }.buttonStyle(.plain).accessibilityIdentifier("home.latest-notice")
                        }
                        if let selected = chats.selectedID {
                            ConversationView(app: model, model: chats, conversationID: selected,
                                             selectionLocked: selectionLocked)
                        } else {
                            Text("Was möchtest du tun?").font(.title2.weight(.semibold))
                                .foregroundStyle(Theme.ink).padding(.top, 16)
                        }
                        Color.clear.frame(height: 1).id("chat.bottom")
                    }
                    .frame(maxWidth: 600, alignment: .leading)
                    .padding(.horizontal, Theme.Space.margin).padding(.vertical, 18)
                    .frame(maxWidth: .infinity)
                }
                .scrollDismissesKeyboard(.interactively)
                .task {
                    // This ScrollView is recreated after the confirmed voice read.
                    // Reveal the new final messages once; subsequent scrolling stays free.
                    await Task.yield()
                    scroll.scrollTo("chat.bottom", anchor: .bottom)
                }
                .onChange(of: chats.selectedID) { _ in
                    voiceNotice = ""
                    Task { await refreshChats(list: false) }
                }
                .onChange(of: chats.detail?.messages.last?.message_id) { _ in
                    withAnimation(.easeOut(duration: 0.2)) { scroll.scrollTo("chat.bottom", anchor: .bottom) }
                }
                .refreshable { await model.refreshAll(); await refreshChats(list: true) }
            }
        }
        }
        .safeAreaInset(edge: .bottom, spacing: 0) {
            VStack(spacing: 8) {
                if voiceBusy || !voiceNotice.isEmpty || currentVoiceFailure {
                    voiceControls
                }
                if chats.selected?.isReadOnly == true {
                    Text("Gespräch am Raspberry Pi · zum Nachlesen. Raumgespräche werden wie bisher nach 90 Tagen ohne Aktivität entfernt.")
                        .font(.footnote).foregroundStyle(Theme.ink2)
                    Button("Neuer privater Chat", action: createChat)
                        .frame(maxWidth: .infinity, minHeight: 44)
                        .disabled(selectionLocked || !model.reachable || model.client == nil)
                        .accessibilityIdentifier("chat.room.private")
                } else {
                Button(action: startVoice) {
                    Label("Mit SOLVIO sprechen", systemImage: "mic.fill")
                        .font(.subheadline.weight(.semibold)).frame(maxWidth: .infinity, minHeight: 44)
                }
                .accessibilityIdentifier("home.speak")
                .disabled(selectionLocked || !model.reachable || model.client == nil || model.enrollmentForVoice == nil
                          || voice.handoff.hasUncertainty(in: chats.selectedID ?? ""))
                if !currentVoiceFailure && voiceNotice.isEmpty {
                    Text("Mikrofon aus").font(.caption2).foregroundStyle(Theme.ink2)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                MessageComposerView(app: model, chats: chats, model: composer,
                                    voiceBusy: voiceBusy || voice.handoff.hasUncertainty(in: chats.selectedID ?? ""),
                                    showsMicrophone: false, onMicrophone: startVoice,
                                    beforeSend: prepareTextSend)
                }
            }
            .padding(.horizontal, 16).padding(.vertical, 10)
            .frame(maxWidth: .infinity).background(Theme.bg)
        }
        }
        }
        .background((focused ? Theme.voiceTop : Theme.bg).ignoresSafeArea())
        .toolbar(focused ? .hidden : .visible, for: .tabBar)
        .toolbar(.hidden, for: .navigationBar)
        .task(id: "\(scenePhase):\(clientBinding)") {
            voiceFocus.setForeground(scenePhase == .active)
            guard scenePhase == .active else { return }
            finishVoiceFocus()
            await reconcilePending()
            var lastList: Double = 0
            repeat {
                let now = Date().timeIntervalSince1970
                let refreshList = now - lastList >= 12
                if refreshList { lastList = now }
                await refreshChats(list: refreshList)
                do { try await Task.sleep(nanoseconds: chats.pollNanoseconds) } catch { return }
            } while !Task.isCancelled
        }
        .onDisappear { stopVoice(reason: "closed") }
        .onChange(of: scenePhase) { phase in
            voiceFocus.setForeground(phase == .active)
            if phase == .background { stopVoice(reason: "background") }
            if phase == .active { finishVoiceFocus() }
        }
        .onChange(of: clientBinding) { _ in
            stopVoice(reason: "binding_changed"); voiceFocus.dismiss(); chats.invalidate(); reconciled = ""
        }
        .onChange(of: voice.handoff.state) { state in
            if state == .complete || state == .unknown { finishVoiceFocus() }
        }
        .onChange(of: voice.isActive) { active in
            if !active && !preparingVoice { finishVoiceFocus() }
        }
        .sheet(isPresented: $showChats) {
            ConversationListView(model: chats, onSelect: { id in
                guard !selectionLocked else { return }
                chats.select(id)
            }, onCreate: createChat)
            .presentationDetents([.medium, .large])
        }
    }

    private var header: some View {
        HStack(spacing: 4) {
            moreMenu
            Button { Haptics.tap(); showChats = true } label: {
                Image(systemName: "bubble.left.and.bubble.right").font(.system(size: 20)).frame(width: 44, height: 44)
            }
            .accessibilityLabel("Chats").accessibilityIdentifier("home.chats")
            .disabled(selectionLocked)
            Text(verbatim: chats.selected?.displayTitle ?? "SOLVIO Nexus")
                .font(.headline).lineLimit(typeSize.isAccessibilitySize ? 2 : 1)
                .frame(maxWidth: .infinity, alignment: .leading)
                .accessibilityAddTraits(.isHeader).accessibilityIdentifier("home.conversation")
            Button(action: createChat) {
                Image(systemName: "square.and.pencil").font(.system(size: 20)).frame(width: 44, height: 44)
            }
            .accessibilityLabel("Neuer Chat").accessibilityIdentifier("home.new-chat")
            .disabled(selectionLocked || model.client == nil || !model.reachable)
        }
        .foregroundStyle(Theme.ink).padding(.horizontal, 8).padding(.vertical, 4)
    }

    private var moreMenu: some View {
        AssistantMenu(model: model, open: open)
            .disabled(voiceBusy || composer.sending)
    }

    private var voiceControls: some View {
        VStack(alignment: .leading, spacing: 8) {
            if typeSize.isAccessibilitySize {
                microphoneStatus
                HStack { muteControl; Spacer(minLength: 4); endControl }
            } else {
                HStack(spacing: 10) {
                    microphoneStatus; Spacer(minLength: 4); muteControl; endControl
                }
            }
            if voice.handoffUnsupported {
                Text("Dieser Core bestätigt den Wechsel zu Text noch nicht. Dein Entwurf bleibt erhalten.")
                    .font(.caption).foregroundStyle(Theme.warn)
            }
            if !voiceNotice.isEmpty {
                Text(verbatim: voiceNotice).font(.caption).foregroundStyle(Theme.warn)
            }
            if voice.handoff.hasUncertainty(in: chats.selectedID ?? "") {
                Text("Der letzte Sprachverlauf ist möglicherweise unvollständig.")
                    .font(.caption).foregroundStyle(Theme.warn)
                Button("Mit vorhandenem Verlauf weiterschreiben") { recoverVoiceHistory() }
                    .font(.footnote).frame(minHeight: 44).disabled(composer.sending)
                    .accessibilityIdentifier("chat.voice.recover")
            }
        }.foregroundStyle(Theme.ink).accessibilityIdentifier("chat.voice.status")
    }

    private var focusControls: some View {
        VStack(spacing: 12) {
            microphoneStatus
            if voiceFocus.phase == .failed {
                Text(voiceFocus.notice).font(.subheadline).foregroundStyle(Theme.warn)
                    .multilineTextAlignment(.center).fixedSize(horizontal: false, vertical: true)
                if voiceFocus.canRetryHistory {
                    Button("Verlauf erneut laden") { finishVoiceFocus() }
                        .frame(minHeight: 44).accessibilityIdentifier("chat.voice.reload")
                }
                Button("Zum gespeicherten Chat") {
                    voiceNotice = voiceFocus.notice; voiceFocus.dismiss()
                    Task { await refreshChats(list: false) }
                }
                .frame(minHeight: 44).accessibilityIdentifier("chat.voice.return")
            } else if voiceFocus.phase == .waitingForForeground {
                Text("Mikrofon aus. Der Verlauf wird beim Zurückkehren geladen.")
                    .font(.footnote).multilineTextAlignment(.center)
            } else if voiceFocus.phase == .finishing {
                Text("Mikrofon aus. Der Verlauf wird bestätigt und geladen.")
                    .font(.footnote).multilineTextAlignment(.center)
            } else {
                if voice.handoffUnsupported {
                    Text("Dieser Core kann den Sprachabschluss nicht bestätigen.")
                        .font(.footnote).foregroundStyle(Theme.warn)
                }
                HStack(spacing: 24) {
                    muteControl
                    Button("Beenden") { stopVoice(reason: "user") }
                        .font(.headline).fixedSize().frame(minWidth: 100, minHeight: 52)
                        .background(.white.opacity(0.12), in: Capsule())
                        .accessibilityIdentifier("chat.voice.end").disabled(previewVoiceActive)
                        .accessibilityHint("Beendet die Sprache und lädt danach den Verlauf in diesem Chat")
                }
                if !typeSize.isAccessibilitySize {
                    Text("Verlauf danach im Chat.")
                        .font(.caption).foregroundStyle(.white.opacity(0.7)).multilineTextAlignment(.center)
                }
            }
        }.foregroundStyle(.white).accessibilityIdentifier("chat.voice.status")
    }

    private var microphoneStatus: some View {
        Label(presence.microphoneActive ? "Mikrofon aktiv" : "Mikrofon aus",
              systemImage: presence.microphoneActive ? "mic.fill" : "mic.slash")
            .font(.footnote.weight(.semibold)).fixedSize(horizontal: false, vertical: true)
    }
    @ViewBuilder private var muteControl: some View {
        if voice.isActive || previewVoiceActive {
            let muted = previewVoiceActive ? !presence.microphoneActive : voice.muted
            Button { voice.muted.toggle() } label: {
                Image(systemName: muted ? "mic" : "mic.slash").frame(width: 44, height: 44)
            }.accessibilityLabel(muted ? "Mikrofon einschalten" : "Mikrofon stummschalten")
                .accessibilityIdentifier("chat.voice.mute").disabled(previewVoiceActive)
        }
    }
    @ViewBuilder private var endControl: some View {
        if voiceBusy {
            Button("Beenden") { stopVoice(reason: "user") }
                .font(.footnote.weight(.semibold)).fixedSize().frame(minHeight: 44)
                .accessibilityIdentifier("chat.voice.end").disabled(previewVoiceActive)
        }
    }

    private var previewVoiceActive: Bool {
        #if DEBUG
        return previewPresence?.voiceActive == true
        #else
        return false
        #endif
    }

    private func createChat() {
        guard !selectionLocked, let client = model.client else { return }
        Haptics.tap()
        Task {
            if await chats.createConversation(create: { try await client.createConversation(clientRequestID: $0) }) != nil {
                showChats = false
            }
        }
    }

    private func startVoice() {
        guard !selectionLocked, chats.selected?.isReadOnly != true, model.reachable, let client = model.client,
              let pairing = model.pairing, let enrollment = model.enrollmentForVoice else { return }
        guard !voice.handoff.hasUncertainty(in: chats.selectedID ?? "") else { return }
        Haptics.tap(); preparingVoice = true; voiceNotice = ""
        // Removing the composer dismisses its keyboard, not its model or draft.
        let attempt = voiceFocus.present(); voiceAttempt = attempt
        Task {
            let id = await chats.prepareVoiceConversation(create: { try await client.createConversation(clientRequestID: $0) },
                                                          load: { try await client.conversation($0) })
            guard voiceAttempt == attempt, scenePhase == .active else { return }
            preparingVoice = false
            guard let id else {
                voiceFocus.startFailed("Der Chat konnte nicht für Sprache geöffnet werden. Es wurde keine Aufnahme gestartet.")
                return
            }
            guard voiceFocus.bind(id, attempt: attempt) else { return }
            await voice.begin(pairing: pairing, deviceID: enrollment.deviceID,
                              transportCred: enrollment.transportCred, conversationID: id)
            if !voice.isActive { finishVoiceFocus() }
        }
    }

    private func prepareTextSend() async throws {
        guard !preparingVoice, let client = model.client else { throw VoiceHandoffError() }
        guard let id = chats.selectedID else { throw VoiceHandoffError() }
        let requiresRefresh = voice.handoff.conversationID == id && voice.handoff.state != .idle
        try await voice.finishForText(conversationID: id)
        guard requiresRefresh else { return }
        // Same canonical detail/retry path as normal polling; no inferred transcript.
        await composer.refreshDetail(chats: chats, coreID: client.pairedCoreInstanceID,
            deviceID: client.deviceIdentifier, loadRetry: { ConversationRetryCache.load(client) },
            clearRetry: { ConversationRetryCache.clear(client) }, load: { try await client.conversation($0) })
        guard chats.detailError.isEmpty, chats.detail?.conversation.conversation_id == chats.selectedID else {
            throw VoiceHandoffError()
        }
    }

    private func recoverVoiceHistory() {
        guard let client = model.client, let id = chats.selectedID else { return }
        Task {
            do {
                try await voice.useStoredHistory(conversationID: id, load: {
                    let detail = try await client.conversation(id)
                    guard chats.selectedID == id else { throw VoiceHandoffError() }
                    return detail
                })
                voiceNotice = "Du schreibst mit dem gespeicherten Verlauf weiter. Der letzte Sprachabschnitt kann fehlen."
                await refreshChats(list: false)
            } catch { voiceNotice = "Der vorhandene Verlauf konnte nicht bestätigt werden. Dein Entwurf bleibt erhalten." }
        }
    }

    private func stopVoice(reason: String) {
        let wasPreparing = preparingVoice
        voiceAttempt = UUID(); preparingVoice = false
        voice.end(reason: reason)
        if wasPreparing { voiceFocus.dismiss() }
        else { finishVoiceFocus() }
    }

    private func finishVoiceFocus() {
        guard voiceFocus.isPresented, !preparingVoice, !voice.isActive else { return }
        if voice.hasVisibleFailure && !voice.handoff.hasUncertainty(in: voiceFocus.conversationID ?? "") {
            voiceFocus.startFailed(voice.chatStatus); return
        }
        guard let client = model.client, let id = voiceFocus.conversationID else { return }
        let binding = clientBinding
        Task {
            await voiceFocus.finish(chats: chats,
                stillBound: { clientBinding == binding && chats.selectedID == id },
                waitForClose: { try await voice.finishForText(conversationID: id) },
                load: { try await client.conversation($0) })
        }
    }

    private func refreshChats(list: Bool) async {
        guard let client = model.client else { return }
        if list || chats.conversations.isEmpty {
            await chats.refreshList { try await client.conversations() }
            chats.restoreSelection()
        }
        // No pre-flush polling read may stand in for the final focused read.
        guard !focused, chats.selectedID != nil else { return }
        await composer.refreshDetail(chats: chats, coreID: client.pairedCoreInstanceID,
            deviceID: client.deviceIdentifier, loadRetry: { ConversationRetryCache.load(client) },
            clearRetry: { ConversationRetryCache.clear(client) }, load: { try await client.conversation($0) })
    }
    private func reconcilePending() async {
        guard let client = model.client, reconciled != clientBinding else { return }
        reconciled = clientBinding
        guard let pending = ConversationRetryCache.load(client) else { return }
        switch await chats.reconcile(pending: pending, load: { try await client.conversation($0) }) {
        case .resolved: ConversationRetryCache.clear(client)
        case .unresolved, .unavailable: break
        }
    }
}

// MARK: - Bausteine

enum HomeRoute: Hashable { case tasks, connections, knowledge, approvals, inbox, system, activity, tresor, zahlungen }

/// Ein alter Stand darf nie wie der aktuelle aussehen.
struct OfflineCard: View {
    let since: Double?

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: "wifi.slash").foregroundStyle(Theme.warn)
            VStack(alignment: .leading, spacing: 2) {
                Text("SOLVIO ist gerade nicht erreichbar.")
                    .font(.subheadline.weight(.medium)).foregroundStyle(Theme.ink)
                if let since {
                    Text("Angezeigt wird der Stand von \(relativeTime(since)).")
                        .font(.caption).foregroundStyle(Theme.ink2)
                }
            }
            Spacer()
        }
        .padding(Theme.Space.card)
        .background(Theme.tintGold,
                    in: RoundedRectangle(cornerRadius: Theme.Radius.card, style: .continuous))
    }
}

struct EmptyState: View {
    let icon: String
    let title: String
    let text: String

    var body: some View {
        VStack(spacing: 12) {
            Image(systemName: icon)
                .font(.system(size: 40, weight: .light))
                .foregroundStyle(Theme.ink3)
            Text(title).font(.display(.headline)).foregroundStyle(Theme.ink)
            Text(text).font(.subheadline).foregroundStyle(Theme.ink2)
                .multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 64)
        .padding(.horizontal, 24)
    }
}

/// Sanfte Druckreaktion — Tastgefuehl ohne Effekthascherei.
struct PressScale: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(configuration.isPressed ? 0.98 : 1)
            .animation(.easeOut(duration: 0.12), value: configuration.isPressed)
    }
}
