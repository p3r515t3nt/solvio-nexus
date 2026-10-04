import SwiftUI
import SolvioApprovalsKit

/// Shared entry points; every destination keeps using its existing Core workflow.
struct AssistantMenu: View {
    @ObservedObject var model: AppModel
    let open: (HomeRoute) -> Void

    var body: some View {
        Menu {
            Button { open(.connections) } label: { Label("Verbindungen", systemImage: "point.3.connected.trianglepath.dotted") }
            Button { open(.knowledge) } label: { Label("Gedächtnis & Wissen", systemImage: "book.closed") }
            Button { open(.approvals) } label: {
                Label(model.pending.isEmpty ? "Freigaben" : "Freigaben (\(model.pending.count))", systemImage: "checkmark.shield")
            }
            Button { open(.inbox) } label: {
                Label(model.unread > 0 ? "Hinweise (\(model.unread))" : "Hinweise", systemImage: "bell")
            }
            Button { open(.activity) } label: { Label("Aktivitäten", systemImage: "clock.arrow.circlepath") }
            Section {
                Button { open(.system) } label: { Label("Einstellungen & System", systemImage: "gearshape") }
                Button { open(.tresor) } label: { Label("Tresor", systemImage: "lock") }
            }
        } label: {
            Image(systemName: "line.3.horizontal").font(.system(size: 20)).frame(width: 44, height: 44)
                .overlay(alignment: .topTrailing) {
                    if !model.pending.isEmpty || model.unread > 0 {
                        Circle().fill(Theme.gold).frame(width: 7, height: 7).padding(7).accessibilityHidden(true)
                    }
                }
        }
        .accessibilityLabel("Mehr").accessibilityIdentifier("home.more")
    }
}

struct AssistantOverviewView: View {
    @ObservedObject var model: AppModel
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.Space.section) {
                Text(Date.now.formatted(date: .complete, time: .omitted))
                    .font(.subheadline).foregroundStyle(Theme.ink2)
                if !model.reachable { OfflineCard(since: model.snapshotAt) }
                if !model.pending.isEmpty {
                    NavigationLink(value: HomeRoute.approvals) {
                        Label("\(model.pending.count) Freigaben warten auf dich", systemImage: "checkmark.shield")
                            .frame(maxWidth: .infinity, alignment: .leading).padding(16).card()
                    }
                }
                if model.inbox.isEmpty {
                    EmptyState(icon: "sun.max", title: "Hier beginnt dein Überblick.",
                               text: "Dein Tagesüberblick, Ergebnisse und wichtige Hinweise erscheinen hier, sobald SOLVIO sie bereitstellt.")
                }
                ForEach(model.inbox.sorted { ($0.zeit ?? 0) > ($1.zeit ?? 0) }.prefix(8)) { item in
                    NavigationLink { NoticeDetailView(model: model, item: item) } label: { NoticeCard(item: item) }
                        .buttonStyle(.plain)
                }
                NavigationLink(value: HomeRoute.inbox) { Label("Alle Hinweise", systemImage: "bell") }
                NavigationLink { PlannedView(model: model) } label: {
                    Label("Regelmäßige Aufgaben", systemImage: "calendar.badge.clock")
                }.frame(minHeight: 44)
            }.padding(Theme.Space.margin)
        }.background(Theme.bg).navigationTitle("Überblick")
        .task { await model.refreshControl() }
        .refreshable { await model.refreshControl() }
    }
}

struct AssistantIdea: Identifiable {
    let id: String
    let title: String
    let icon: String
    let prompt: String
    static let examples: [AssistantIdea] = [
        .init(id: "day", title: "Meinen Tag vorbereiten", icon: "sun.max",
              prompt: "Gib mir einen Überblick über meinen heutigen Tag: wichtige Mails, Termine und offene Aufgaben."),
        .init(id: "research", title: "Etwas vergleichen lassen", icon: "magnifyingglass",
              prompt: "Ich möchte etwas vergleichen. Kläre mit mir kurz, was ich suche und welche Anforderungen mir wichtig sind. Recherchiere danach passende Angebote mit Quellen. Kaufe nichts."),
        .init(id: "mail", title: "Eine Mail beantworten", icon: "envelope",
              prompt: "Hilf mir, eine Mail zu beantworten. Kläre zuerst, welche Mail gemeint ist, und bereite die Antwort vor. Vor dem Versand möchte ich Empfänger und Inhalt prüfen."),
        .init(id: "followup", title: "An eine Antwort erinnern", icon: "bell.badge",
              prompt: "Erinnere mich, wenn auf eine bestimmte Mail keine Antwort kommt. Kläre mit mir, welche Mail und bis wann ich eine Antwort erwarte."),
        .init(id: "document", title: "Ein Dokument ausarbeiten", icon: "doc.text",
              prompt: "Hilf mir, ein Dokument auszuarbeiten. Kläre zuerst mit mir Thema, Ziel und gewünschtes Format.")
    ]
}

/// Deterministic next steps from the existing task projection, with no new store.
struct PersonalAssistantIdea: Identifiable {
    enum Kind: Int { case question, approval, blocked, result }
    let run: AgentRun
    let kind: Kind
    var id: String { run.id }
    var title: String {
        switch kind {
        case .question: return "Eine offene Frage klären"
        case .approval: return "Eine ausstehende Entscheidung prüfen"
        case .blocked: return "Ein Hindernis klären"
        case .result: return "Ein Ergebnis weiterverwenden"
        }
    }
    var reason: String {
        switch kind {
        case .question: return "Hier wartet SOLVIO auf deine Antwort."
        case .approval: return "Sieh dir an, wofür dein OK benötigt wird."
        case .blocked: return "Prüfe, was den Abschluss verhindert hat."
        case .result: return "Ergebnis öffnen, nutzen oder ergänzen lassen."
        }
    }
    var action: String {
        switch kind {
        case .question: return "Frage ansehen"
        case .approval: return "Auftrag ansehen"
        case .blocked: return "Hindernis ansehen"
        case .result: return "Ergebnis ansehen"
        }
    }
    @MainActor static func select(_ runs: [AgentRun], now: Double = Date.now.timeIntervalSince1970) -> [Self] {
        Array(AgentResultsModel.latestTasks(runs).compactMap { run -> Self? in
            guard AgentRun.validRunID(run.id) else { return nil }
            if run.offen && run.zustand_code == "WAITING_USER" { return .init(run: run, kind: .question) }
            if run.offen && run.zustand_code == "WAITING_APPROVAL" { return .init(run: run, kind: .approval) }
            guard !run.offen, let created = run.angelegt, created > now - 30 * 86400, created <= now else { return nil }
            if run.zustand_code == "FAILED" { return .init(run: run, kind: .blocked) }
            if run.zustand_code == "SUCCEEDED" && (!(run.ergebnis ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !(run.dateien ?? []).isEmpty) {
                return .init(run: run, kind: .result)
            }
            return nil
        }.sorted {
            if $0.kind != $1.kind { return $0.kind.rawValue < $1.kind.rawValue }
            if ($0.run.angelegt ?? 0) != ($1.run.angelegt ?? 0) { return ($0.run.angelegt ?? 0) > ($1.run.angelegt ?? 0) }
            return $0.id < $1.id
        }.prefix(4))
    }
}

struct AssistantIdeasView: View {
    @ObservedObject var app: AppModel
    @StateObject private var results: AgentResultsModel
    @Environment(\.scenePhase) private var scenePhase
    let notice: String
    let prepare: (String) -> Void
    init(app: AppModel, model: AgentResultsModel? = nil, notice: String, prepare: @escaping (String) -> Void) {
        self.app = app
        _results = StateObject(wrappedValue: model ?? AgentResultsModel())
        self.notice = notice
        self.prepare = prepare
    }
    private var suggestions: [PersonalAssistantIdea] {
        results.error.isEmpty && app.reachable ? PersonalAssistantIdea.select(results.runs) : []
    }
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.Space.section) {
                Text("Für dich").font(.title2.weight(.semibold))
                Text("Nächste Schritte aus deinen Aufgaben. Du entscheidest, was SOLVIO weiterbearbeitet.")
                    .font(.subheadline).foregroundStyle(Theme.ink2)
                if !app.reachable || !results.error.isEmpty {
                    Text("Deine Anregungen erscheinen, sobald der aktuelle Auftragsstand wieder erreichbar ist.").foregroundStyle(Theme.warn)
                } else if results.loading && results.runs.isEmpty {
                    ProgressView("Anregungen werden gelesen …")
                } else if suggestions.isEmpty {
                    Text("Gerade gibt es keinen passenden nächsten Schritt aus deinen Aufgaben. Starte unten mit einer neuen Idee.")
                        .font(.subheadline).foregroundStyle(Theme.ink2)
                }
                ForEach(suggestions) { idea in
                    NavigationLink { AgentResultView(app: app, runID: idea.id) } label: {
                        VStack(alignment: .leading, spacing: 10) {
                            Text(idea.title).font(.headline).foregroundStyle(Theme.ink)
                            Text(verbatim: idea.run.auftrag).font(.subheadline.weight(.medium)).foregroundStyle(Theme.ink).lineLimit(3)
                            Text(idea.reason).font(.subheadline).foregroundStyle(Theme.ink2)
                            Label(idea.action, systemImage: "arrow.up.right").font(.subheadline)
                        }.frame(maxWidth: .infinity, alignment: .leading).padding(Theme.Space.card).card()
                    }.buttonStyle(.plain).accessibilityIdentifier("ideas.personal." + idea.id)
                }
                Text("Etwas Neues beginnen").font(.title2.weight(.semibold)).padding(.top, 8)
                Text("Anregungen für neue Themen. Im Chat anpassen und senden.")
                    .font(.subheadline).foregroundStyle(Theme.ink2)
                if !notice.isEmpty { Text(notice).foregroundStyle(Theme.warn).accessibilityIdentifier("ideas.notice") }
                ForEach(AssistantIdea.examples) { idea in
                    Button { prepare(idea.prompt) } label: {
                        HStack(spacing: 14) {
                            Image(systemName: idea.icon).font(.title2).frame(width: 32)
                            VStack(alignment: .leading, spacing: 6) {
                                Text(idea.title).font(.headline).foregroundStyle(Theme.ink)
                                Text("Im Chat vorbereiten").font(.caption).foregroundStyle(Theme.ink2)
                            }
                            Spacer(); Image(systemName: "arrow.up.right")
                        }.padding(Theme.Space.card).frame(maxWidth: .infinity, alignment: .leading).card()
                    }.buttonStyle(.plain).accessibilityIdentifier("ideas." + idea.id)
                }
            }.padding(Theme.Space.margin)
        }.background(Theme.bg).navigationTitle("Ideen")
        .refreshable { await refresh() }
        .task(id: "\(scenePhase):\(app.taskStartKeyID ?? "")") {
            results.invalidate()
            guard scenePhase == .active else { return }
            repeat {
                await refresh()
                do { try await Task.sleep(nanoseconds: 10_000_000_000) } catch { return }
            } while !Task.isCancelled
        }
        .onDisappear { results.invalidate() }
    }
    private func refresh() async {
        guard let client = app.client else { results.invalidate(); return }
        await results.refreshList { try await client.agentRuns() }
    }
}

/// This is a projection of existing run results, not another file or memory store.
struct AssistantLibraryView: View {
    @ObservedObject var app: AppModel
    @ObservedObject var results: AgentLibraryModel
    @State private var search = ""
    @State private var filesOnly = false

    static func entries(_ runs: [AgentRun], search: String, filesOnly: Bool) -> [AgentRun] {
        let query = search.trimmingCharacters(in: .whitespacesAndNewlines)
        return runs.filter { run in
            let files = run.dateien ?? []
            let hasResult = !(run.ergebnis ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            guard !files.isEmpty || (!filesOnly && hasResult) else { return false }
            return query.isEmpty || ([run.auftrag, run.ergebnis ?? ""] + files.map(\.name))
                .contains { $0.localizedCaseInsensitiveContains(query) }
        }
    }

    private var entries: [AgentRun] { Self.entries(results.runs, search: "", filesOnly: filesOnly) }
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.Space.section) {
                Picker("Inhalt", selection: $filesOnly) {
                    Text("Ergebnisse").tag(false); Text("Dateien").tag(true)
                }.pickerStyle(.segmented)
                Text("Ergebnisse und Dateien deiner Aufgaben. Die Suche berücksichtigt auch ältere gespeicherte Ergebnisse.")
                    .font(.caption).foregroundStyle(Theme.ink2)
                if !app.reachable { OfflineCard(since: app.snapshotAt) }
                if !results.error.isEmpty { Text(results.error).foregroundStyle(Theme.warn) }
                if results.loading { ProgressView("Ergebnisse werden gelesen …") }
                if entries.isEmpty && !results.loading && results.error.isEmpty {
                    EmptyState(icon: "doc.on.doc", title: search.isEmpty ? "Noch keine Ergebnisse." : "Kein Treffer.",
                               text: "Dokumente, Dateien und Rechercheergebnisse deiner Aufträge findest du hier.")
                }
                ForEach(entries) { run in
                    VStack(alignment: .leading, spacing: 12) {
                        NavigationLink { AgentResultView(app: app, runID: run.id) } label: {
                            VStack(alignment: .leading, spacing: 5) {
                                Text(verbatim: run.auftrag).font(.headline).foregroundStyle(Theme.ink)
                                Text(run.zustand).font(.caption).foregroundStyle(Theme.ink2)
                                if let result = run.ergebnis, !result.isEmpty {
                                    Text(verbatim: result).font(.subheadline).foregroundStyle(Theme.ink2).lineLimit(3)
                                }
                            }.frame(maxWidth: .infinity, alignment: .leading)
                        }.buttonStyle(.plain)
                        ForEach(run.dateien ?? []) { file in
                            ResultFileView(app: app, runID: run.id, file: file)
                        }
                    }.padding(Theme.Space.card).card()
                }
                if results.next != nil {
                    Button("Ältere Ergebnisse laden") { Task { await refresh(older: true) } }
                        .disabled(results.loading).accessibilityIdentifier("library.more")
                }
                if !results.error.isEmpty {
                    Button("Erneut versuchen") { Task { await refresh(older: results.next != nil) } }
                        .disabled(results.loading)
                }
            }.padding(Theme.Space.margin)
        }.background(Theme.bg).navigationTitle("Bibliothek")
        .searchable(text: $search, prompt: "Ergebnis oder Datei suchen")
        .task(id: "\(app.taskStartKeyID ?? ""):\(search)") {
            results.select(query: search, identity: app.taskStartKeyID)
            guard !results.loaded else { return }
            if !search.isEmpty {
                do { try await Task.sleep(nanoseconds: 350_000_000) } catch { return }
            }
            guard !Task.isCancelled else { return }
            await refresh()
        }
        .refreshable { await refresh() }
        .onDisappear { results.cancelLoading() }
    }
    private func refresh(older: Bool = false) async {
        guard let client = app.client else { return }
        await results.load(older: older) { cursor in try await client.agentRunPage(before: cursor, query: results.query) }
    }
}
