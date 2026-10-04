import SwiftUI
import QuickLook
import UniformTypeIdentifiers
import SolvioApprovalsKit

struct AgentTasksView: View {
    @ObservedObject var app: AppModel
    let openChat: (() -> Void)?
    @StateObject private var model: AgentResultsModel
    @Environment(\.scenePhase) private var scenePhase

    init(app: AppModel, model: AgentResultsModel? = nil, openChat: (() -> Void)? = nil) {
        self.app = app
        self.openChat = openChat
        _model = StateObject(wrappedValue: model ?? AgentResultsModel())
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.Space.section) {
                if let openChat {
                    Button(action: openChat) {
                        Label("Im Chat beauftragen", systemImage: "bubble.left")
                            .frame(maxWidth: .infinity).padding(16)
                            .foregroundStyle(Theme.onBlue).background(Theme.blue, in: RoundedRectangle(cornerRadius: 14))
                    }
                }
                DisclosureGroup("Weitere Möglichkeiten") {
                    NavigationLink { TaskStartView(app: app, model: app.taskStart) } label: {
                        Label("Auftragsformular öffnen", systemImage: "square.and.pencil").frame(minHeight: 44)
                    }
                }
                NavigationLink { PlannedView(model: app) } label: {
                    Label("Regelmäßige Aufgaben", systemImage: "calendar.badge.clock")
                        .foregroundStyle(Theme.ink2).padding(.vertical, 12)
                }
                if !model.error.isEmpty { resultWarning(model.error) }
                if model.loading && model.runs.isEmpty { ProgressView("Aufträge werden gelesen …") }
                else if model.runs.isEmpty && model.error.isEmpty {
                    EmptyState(icon: "tray", title: "Noch keine Aufträge.", text: "Erteile SOLVIO einen Auftrag. Hier findest du später den Stand und das Ergebnis.")
                }
                ForEach(model.runs) { run in
                    NavigationLink { AgentResultView(app: app, runID: run.id) } label: {
                        VStack(alignment: .leading, spacing: 9) {
                            Text(verbatim: run.auftrag).font(.body.weight(.medium)).foregroundStyle(Theme.ink).multilineTextAlignment(.leading)
                            HStack {
                                Label(run.zustand, systemImage: run.offen ? "circle.dotted" : "circle.fill")
                                    .foregroundStyle(runTone(run))
                                Spacer()
                                Text(relativeTime(run.angelegt)).foregroundStyle(Theme.ink3)
                            }.font(.caption)
                        }.padding(Theme.Space.card).frame(maxWidth: .infinity, alignment: .leading).card()
                    }.buttonStyle(PressScale())
                }
                Text("Alle offenen Aufgaben und die letzten abgeschlossenen Aufgaben. Ältere Ergebnisse findest du in der Bibliothek.")
                    .font(.caption).foregroundStyle(Theme.ink3)
            }.padding(Theme.Space.margin)
        }
        .background(Theme.bg).navigationTitle("Aufgaben")
        .refreshable { await refresh() }
        .task(id: scenePhase) {
            guard scenePhase == .active else { return }
            repeat {
                await refresh()
                do { try await Task.sleep(nanoseconds: 5_000_000_000) } catch { return }
            } while !Task.isCancelled
        }
        .onDisappear { model.invalidate() }
    }

    private func refresh() async {
        guard let client = app.client else { return }
        await model.refreshList { try await client.agentRuns() }
    }
}

struct AgentResultView: View {
    @ObservedObject var app: AppModel
    let initialRunID: String
    @State private var selectedRunID: String?
    private var runID: String { selectedRunID ?? initialRunID }
    let embedded: Bool
    let onNavigationLockChanged: (Bool) -> Void
    @StateObject private var model: AgentResultsModel
    @Environment(\.scenePhase) private var scenePhase
    @State private var action: String?
    @State private var providerChoice: AgentRun.ProviderOption?
    @State private var providerBoundaryRef = ""
    @State private var loadedSelection: Selection?
    @State private var questionProtection = AgentQuestionNavigationProtection()
    @State private var followupProtected = false

    private struct Selection: Equatable {
        let runID: String
        let client: ObjectIdentifier?
    }
    private struct PollIdentity: Equatable {
        let selection: Selection
        let phase: ScenePhase
    }
    private var selection: Selection {
        Selection(runID: runID, client: app.client.map { ObjectIdentifier($0) })
    }
    private var isCurrentSelection: Bool {
        loadedSelection == nil || loadedSelection == selection
    }
    private var currentQuestionBinding: String? {
        guard isCurrentSelection, let run = model.detail, run.id == runID,
              run.offen, let question = run.action_intent?.question else { return nil }
        return run.id + ":" + question.bindingID
    }

    init(app: AppModel, runID: String, model: AgentResultsModel? = nil, embedded: Bool = false,
         onNavigationLockChanged: @escaping (Bool) -> Void = { _ in }) {
        self.app = app; self.initialRunID = runID; self.embedded = embedded
        self.onNavigationLockChanged = onNavigationLockChanged
        _model = StateObject(wrappedValue: model ?? AgentResultsModel())
    }

    var body: some View {
        presentation
            .task(id: PollIdentity(selection: selection, phase: scenePhase)) {
                guard scenePhase == .active else { return }
                if !isCurrentSelection || (model.detail != nil && model.detail?.id != runID) {
                    model.invalidate(); action = nil; questionProtection.synchronize(nil)
                    followupProtected = false
                }
                loadedSelection = selection
                repeat {
                    guard !Task.isCancelled else { return }
                    await refresh()
                    do { try await Task.sleep(nanoseconds: 5_000_000_000) } catch { return }
                } while !Task.isCancelled
            }
            .onAppear {
                questionProtection.synchronize(currentQuestionBinding)
                reportNavigationProtection()
            }
            .onChange(of: model.working) { _ in reportNavigationProtection() }
            .onChange(of: initialRunID) { _ in
                selectedRunID = nil; model.invalidate(); loadedSelection = nil
                followupProtected = false; questionProtection.synchronize(nil)
                reportNavigationProtection()
            }
            .onChange(of: currentQuestionBinding) { binding in
                questionProtection.synchronize(binding)
                reportNavigationProtection()
            }
            .onDisappear {
                model.invalidate(); action = nil
                followupProtected = false
                questionProtection.synchronize(nil)
                onNavigationLockChanged(false)
            }
            .confirmationDialog(action == "switch" ? "Zu \(providerChoice?.label ?? "") wechseln?" : action == "cancel" ? "Diesen Auftrag abbrechen?" : "Diesen Auftrag fortsetzen?",
                isPresented: Binding(get: { action != nil }, set: { if !$0 { action = nil } }), titleVisibility: .visible) {
                if let selected = action {
                    Button(selected == "switch" ? "Anbieter wechseln und fortsetzen" : selected == "cancel" ? "Auftrag abbrechen" : "Fortsetzen", role: selected == "cancel" ? .destructive : nil) {
                        Task { await perform(selected) }
                    }
                }
                Button("Zurück", role: .cancel) { action = nil }
            } message: {
                if action == "switch", let choice = providerChoice {
                    Text(choice.hinweis + "\nVerfügbare Werkzeuge: " + choice.werkzeuge.joined(separator: ", "))
                }
            }
    }

    @ViewBuilder private var presentation: some View {
        if embedded {
            conversation
        } else {
            ScrollView { conversation.padding(Theme.Space.margin) }
                .background(Theme.bg).navigationTitle("Ergebnis").navigationBarTitleDisplayMode(.inline)
                .refreshable { await refresh() }
        }
    }

    private var conversation: some View {
        VStack(alignment: .leading, spacing: Theme.Space.section) {
            if isCurrentSelection && !model.error.isEmpty { resultWarning(model.error) }
            if let run = model.detail, run.id == runID, isCurrentSelection {
                VStack(alignment: .leading, spacing: 8) {
                    Text("Dein Auftrag").font(.caption.weight(.semibold)).foregroundStyle(Theme.ink2)
                    Text(verbatim: run.auftrag).font(.body).foregroundStyle(Theme.ink).textSelection(.enabled)
                    if let revision = run.task_revision, revision.revision > 1 {
                        Text("Deine Folgeanweisung").font(.caption.weight(.semibold)).foregroundStyle(Theme.ink2)
                        Text(verbatim: revision.text).font(.body).foregroundStyle(Theme.ink).textSelection(.enabled)
                    }
                }
                .padding(Theme.Space.card).frame(maxWidth: .infinity, alignment: .leading)
                .background(Theme.tintBlue, in: RoundedRectangle(cornerRadius: Theme.Radius.card))

                VStack(alignment: .leading, spacing: 12) {
                    Text("SOLVIO").font(.display(.headline)).foregroundStyle(Theme.ink)
                    Label(run.zustand, systemImage: run.offen ? "circle.dotted" : "circle.fill")
                        .font(.subheadline.weight(.medium)).foregroundStyle(runTone(run))
                    if let result = run.ergebnis, !result.isEmpty {
                        Text(verbatim: result).font(.body).foregroundStyle(Theme.ink).textSelection(.enabled)
                    } else {
                        Text(verbatim: run.grund.flatMap { $0.isEmpty ? nil : $0 } ?? "Noch kein bestätigtes Ergebnis.")
                            .foregroundStyle(Theme.ink2)
                    }
                }.frame(maxWidth: .infinity, alignment: .leading)

                if let notice = run.failureNotice {
                    resultWarning(notice)
                }
                if let waiting = run.wartet_auf {
                    resultWarning([waiting.handlung, waiting.grund, waiting.danach].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: "\n"))
                } else if run.anbietergrenze != nil {
                    resultWarning("Eine Anbietergrenze braucht deine Entscheidung. Danach ausdrücklich fortsetzen; kein automatischer Anbieterwechsel.")
                }
                if (run.kosten?.counts?.unknown ?? 0) > 0 {
                    resultWarning("Eine Kostenbuchung ist ungewiss. Ihre Reserve bleibt bestehen.")
                }
                if run.vorbereitet == true {
                    resultWarning("Änderung vorbereitet. Eine produktive Übernahme ist damit nicht bestätigt.")
                }
                if !model.actionMessage.isEmpty { resultWarning(model.actionMessage) }
                if run.offen, let question = run.action_intent?.question {
                    let questionBinding = run.id + ":" + question.bindingID
                    let callbackSelection = selection
                    ActionQuestionView(app: app, runID: runID, question: question,
                        available: model.error.isEmpty && !model.working, refresh: refresh,
                        refreshAccounts: { await perform("resume") },
                        onProtectionChange: { protects in
                            guard loadedSelection == nil || loadedSelection == callbackSelection else { return }
                            questionProtection.report(protects, for: questionBinding, current: currentQuestionBinding)
                            reportNavigationProtection()
                        })
                        .id(runID + question.bindingID + (app.client?.pairedCoreInstanceID ?? "")
                            + (app.client?.deviceIdentifier ?? ""))
                }
                TaskCostApprovalView(app: app, run: run,
                    available: model.error.isEmpty && !model.working && !model.loading, refresh: refresh)
                    .id(run.aufgabe + (app.client?.pairedCoreInstanceID ?? "") + (app.client?.deviceIdentifier ?? ""))
                if run.offen {
                    ViewThatFits(in: .horizontal) {
                        HStack { controls(run) }
                        VStack(alignment: .leading, spacing: 12) { controls(run) }
                    }.disabled(model.working || model.loading || !model.error.isEmpty || app.client == nil)
                }
                if let note = run.datei_hinweis, !note.isEmpty { resultWarning(note) }
                if let files = run.dateien, !files.isEmpty {
                    VStack(alignment: .leading, spacing: 12) {
                        Text("Dateien").font(.display(.headline))
                        if run.zustand_code != "SUCCEEDED" {
                            Text("Vorliegende Dateien. Der gesamte Auftrag ist noch nicht als erfolgreich bestätigt.")
                                .font(.caption).foregroundStyle(Theme.ink2)
                        }
                        ForEach(files) { file in
                            ResultFileView(app: app, runID: runID, file: file)
                                .id(runID + file.id + file.sha256)
                        }
                    }
                }
                if run.task_revision != nil && !run.offen {
                    let callbackSelection = selection
                    TaskFollowupView(app: app, run: run,
                        available: model.error.isEmpty && !model.loading && !model.working,
                        onAccepted: { accepted in
                            guard selection == callbackSelection, accepted.task_id == run.aufgabe,
                                  accepted.parent_run_id == run.id else { return }
                            selectResult(accepted.run_id)
                        }, onProtectionChange: { protects in
                            guard selection == callbackSelection else { return }
                            followupProtected = protects; reportNavigationProtection()
                        }).id(run.id + (run.task_revision?.bindingID ?? ""))
                }
                if let history = run.task_history, history.count > 1 {
                    DisclosureGroup("Bisherige Nachrichten und Ergebnisse") {
                        VStack(alignment: .leading, spacing: 16) {
                            ForEach(history.filter { AgentRun.validRunID($0.run_id) }) { entry in
                                VStack(alignment: .leading, spacing: 8) {
                                    Text(verbatim: entry.text).font(.callout)
                                    if !entry.result_summary.isEmpty { Text(verbatim: entry.result_summary).font(.footnote).foregroundStyle(Theme.ink2) }
                                    if entry.run_id != runID {
                                        Button("Dieses Ergebnis ansehen") { selectResult(entry.run_id) }
                                            .disabled(model.working || followupProtected || questionProtection.isProtected(for: currentQuestionBinding))
                                    }
                                }
                            }
                        }.padding(.top, 12)
                    }.foregroundStyle(Theme.ink2)
                }
                details(run).id(runID)
            } else if model.error.isEmpty || !isCurrentSelection {
                ProgressView("Auftrag wird gelesen …")
            }
        }
    }

    @ViewBuilder private func controls(_ run: AgentRun) -> some View {
        if run.canResume { Button("Fortsetzen") { action = "resume" }.buttonStyle(.borderedProminent) }
        ForEach(run.providerOptions) { choice in
            Button("Mit \(choice.label) fortsetzen") {
                providerChoice = choice
                providerBoundaryRef = run.anbietergrenze?.boundary_ref ?? ""
                action = "switch"
            }.buttonStyle(.bordered)
        }
        Button("Auftrag abbrechen", role: .destructive) { action = "cancel" }.buttonStyle(.bordered)
    }

    private func details(_ run: AgentRun) -> some View {
        DisclosureGroup("Details zum Auftrag") {
            VStack(alignment: .leading, spacing: Theme.Space.section) {
                if let facts = run.befunde, !facts.isEmpty {
                    VStack(alignment: .leading, spacing: 12) {
                        Text("Befunde").font(.headline).foregroundStyle(Theme.ink)
                        ForEach(Array(facts.enumerated()), id: \.offset) { _, fact in Text(verbatim: fact).textSelection(.enabled) }
                    }.font(.callout)
                }
                if let sources = run.quellen, !sources.isEmpty {
                    VStack(alignment: .leading, spacing: 12) {
                        Text("Quellen").font(.headline).foregroundStyle(Theme.ink)
                        ForEach(Array(sources.enumerated()), id: \.offset) { _, source in Text(verbatim: source).textSelection(.enabled) }
                    }.font(.footnote)
                }
                VStack(alignment: .leading, spacing: 10) {
                    Text("Zusatzkosten").font(.headline).foregroundStyle(Theme.ink)
                    if run.kosten?.configured == true {
                        LabeledContent("Gebucht", value: cents(run.kosten?.ai_tool?.spent_cents))
                        LabeledContent("Reserviert", value: cents(run.kosten?.ai_tool?.reserved_cents))
                        LabeledContent("Fragegrenze", value: cents(run.kosten?.ask_threshold_cents))
                        LabeledContent("Kaufbudget", value: cents(run.kosten?.purchase_cap_cents))
                    } else { Text("Noch kein bestätigter Kostenstand.") }
                }.font(.footnote)
                if let events = run.verlauf, !events.isEmpty {
                    VStack(alignment: .leading, spacing: 12) {
                        Text("Verlauf").font(.headline).foregroundStyle(Theme.ink)
                        ForEach(Array(events.enumerated()), id: \.offset) { _, event in
                            VStack(alignment: .leading, spacing: 4) {
                                Text(clockTime(event.zeit)).font(.caption2).foregroundStyle(Theme.ink3)
                                Text(verbatim: event.text ?? "Stand aktualisiert").font(.footnote)
                            }
                        }
                    }
                }
            }.foregroundStyle(Theme.ink2).padding(.top, 16)
        }.foregroundStyle(Theme.ink2)
    }

    private func reportNavigationProtection() {
        onNavigationLockChanged(model.working || followupProtected || questionProtection.isProtected(for: currentQuestionBinding))
    }

    private func selectResult(_ id: String) {
        guard AgentRun.validRunID(id) else { return }
        selectedRunID = id; model.invalidate(); loadedSelection = nil; action = nil
        followupProtected = false; questionProtection.synchronize(nil)
        reportNavigationProtection()
    }

    private func refresh() async {
        guard !Task.isCancelled, let client = app.client else { return }
        await model.refreshDetail(runID: runID) {
            let run = try await client.agentRun(runID)
            guard app.client === client else { throw CancellationError() }
            return run
        }
    }
    private func perform(_ action: String) async {
        guard isCurrentSelection, model.detail?.id == runID, model.error.isEmpty,
              let client = app.client else { return }
        if action == "switch", let choice = providerChoice {
            let ref = providerBoundaryRef
            await model.act(runID: runID, action: "resume") {
                try await client.switchAgentRun(runID, provider: choice.provider, boundaryRef: ref)
            }
        } else {
            await model.act(runID: runID, action: action) { try await client.controlAgentRun(runID, action: action) }
        }
        await refresh()
    }
}

private func cents(_ value: Int?) -> String {
    guard let value else { return "Nicht belegt" }
    return (Double(value) / 100).formatted(.currency(code: "EUR"))
}
private func runTone(_ run: AgentRun) -> Color {
    switch run.zustand_code {
    case "SUCCEEDED": return Theme.good
    case "FAILED", "KILLED": return Theme.bad
    case "WAITING_USER", "WAITING_APPROVAL": return Theme.warn
    default: return Theme.ink2
    }
}
private func resultWarning(_ text: String) -> some View {
    Text(verbatim: text).font(.footnote).foregroundStyle(Theme.warn)
        .padding(14).frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.tintGold, in: RoundedRectangle(cornerRadius: 12))
}

private struct LocalResult: Identifiable {
    let id = UUID()
    let url: URL
}

struct ResultFileView: View {
    @ObservedObject var app: AppModel
    @Environment(\.dynamicTypeSize) private var typeSize
    let runID: String
    let file: ResultFile
    @State private var busy = false
    @State private var message = ""
    @State private var preview: LocalResult?
    @State private var sharing: LocalResult?
    @State private var download: Task<Void, Never>?
    @State private var directory: URL?
    @State private var generation = UUID()
    @State private var thumbnail: UIImage?

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if let thumbnail {
                Image(uiImage: thumbnail).resizable().scaledToFit()
                    .frame(maxHeight: 280).clipShape(RoundedRectangle(cornerRadius: 12))
                    .accessibilityLabel("Vorschau: " + file.name)
            }
            Label(file.name, systemImage: file.preview_kind == "image" ? "photo" : "doc")
                .font(.body.weight(.medium)).textSelection(.enabled)
            Text(ByteCountFormatter.string(fromByteCount: Int64(file.size), countStyle: .file))
                .font(.caption).foregroundStyle(Theme.ink2)
            Group {
                if typeSize.isAccessibilitySize {
                    VStack(alignment: .leading, spacing: 12) { fileActions }
                } else {
                    HStack { fileActions }
                }
            }.disabled(busy || app.client == nil)
            if !message.isEmpty { Text(verbatim: message).font(.caption).foregroundStyle(Theme.warn) }
        }.padding(16).frame(maxWidth: .infinity, alignment: .leading).card()
        .task(id: file.sha256) {
            if file.preview_kind == "image", file.canPreview, file.size <= 8 * 1024 * 1024 {
                fetch(showPreview: false, thumbnailOnly: true)
            }
        }
        .sheet(item: $preview) { item in ResultPreviewSheet(url: item.url) }
        .sheet(item: $sharing) { item in LocalShare(url: item.url) }
        .onDisappear { generation = UUID(); download?.cancel(); download = nil; busy = false; cleanup() }
    }

    @ViewBuilder private var fileActions: some View {
        if file.canPreview {
            Button { fetch(showPreview: true) } label: {
                Text("Vorschau").fixedSize(horizontal: false, vertical: true).frame(minHeight: 44)
            }.buttonStyle(.bordered)
        }
        Button { fetch(showPreview: false) } label: {
            Text("Teilen / Sichern").fixedSize(horizontal: false, vertical: true).frame(minHeight: 44)
        }.buttonStyle(.bordered)
        if busy { ProgressView() }
    }

    private func fetch(showPreview: Bool, thumbnailOnly: Bool = false) {
        guard !busy, let client = app.client else { return }
        if !thumbnailOnly, let directory {
            let item = LocalResult(url: directory.appendingPathComponent(file.name))
            if showPreview { preview = item } else { sharing = item }
            return
        }
        busy = true; message = ""; let current = generation
        download = Task { @MainActor in
            defer { if generation == current { busy = false; download = nil } }
            do {
                let data = try await client.resultFile(file, runID: runID)
                guard generation == current, !Task.isCancelled, app.client === client else { return }
                cleanup()
                let folder = FileManager.default.temporaryDirectory.appendingPathComponent("solvio-result-" + UUID().uuidString, isDirectory: true)
                try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: false,
                    attributes: [.protectionKey: FileProtectionType.complete])
                directory = folder
                let url = folder.appendingPathComponent(file.name, isDirectory: false)
                try data.write(to: url, options: [.atomic, .completeFileProtection])
                if thumbnailOnly { thumbnail = UIImage(data: data) }
                else if showPreview { preview = LocalResult(url: url) }
                else { sharing = LocalResult(url: url) }
            } catch {
                if generation == current, !Task.isCancelled {
                    cleanup()
                    message = (error as? ResultFileError)?.errorDescription ?? "Die Datei konnte nicht bestätigt geladen werden. Es wurde kein neuer Auftrag gestartet."
                }
            }
        }
    }
    private func cleanup() {
        if let directory { try? FileManager.default.removeItem(at: directory) }
        directory = nil; thumbnail = nil
    }
}

struct ResultPreviewSheet: View {
    let url: URL
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(spacing: 0) {
            ViewThatFits(in: .horizontal) {
                HStack {
                    Text("Vorschau").font(.headline)
                    Spacer(minLength: 16)
                    closeButton
                }
                VStack(alignment: .leading, spacing: 8) {
                    Text("Vorschau").font(.headline)
                    closeButton.frame(maxWidth: .infinity, alignment: .trailing)
                }
            }
            .padding(.horizontal, 20).padding(.vertical, 8)
            Divider()
            LocalPreview(url: url).frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .background(.background)
        .presentationDragIndicator(.visible)
    }

    private var closeButton: some View {
        Button { dismiss() } label: {
            Text("Schließen")
                .font(.body.weight(.semibold))
                .lineLimit(1).minimumScaleFactor(0.75)
                .frame(minHeight: 44)
        }
            .buttonStyle(.bordered)
            .accessibilityIdentifier("resultPreview.close")
    }
}

private struct LocalPreview: UIViewControllerRepresentable {
    let url: URL
    func makeCoordinator() -> Coordinator { Coordinator(url: url) }
    func makeUIViewController(context: Context) -> QLPreviewController {
        let controller = QLPreviewController(); controller.dataSource = context.coordinator
        return controller
    }
    func updateUIViewController(_ controller: QLPreviewController, context: Context) {}
    final class Coordinator: NSObject, QLPreviewControllerDataSource {
        let url: URL
        init(url: URL) { self.url = url }
        func numberOfPreviewItems(in controller: QLPreviewController) -> Int { 1 }
        func previewController(_ controller: QLPreviewController, previewItemAt index: Int) -> any QLPreviewItem { url as NSURL }
    }
}
private struct LocalShare: UIViewControllerRepresentable {
    let url: URL
    func makeUIViewController(context: Context) -> UIActivityViewController {
        UIActivityViewController(activityItems: [url], applicationActivities: nil)
    }
    func updateUIViewController(_ controller: UIActivityViewController, context: Context) {}
}
