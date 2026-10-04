import Foundation
import Combine
import SolvioApprovalsKit

/// A redirect cannot carry the enrolled device's headers to another resource.
final class ResultRedirectGuard: NSObject, URLSessionTaskDelegate, Sendable {
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest) async -> URLRequest? { nil }
}

extension ApprovalClient {
    func agentRuns() async throws -> [AgentRun] {
        struct Envelope: Decodable { let laeufe: [AgentRun] }
        var request = controlRequest("v1/agent/runs")
        guard let url = request.url, var parts = URLComponents(url: url, resolvingAgainstBaseURL: false) else { throw ClientError.decode }
        parts.queryItems = [URLQueryItem(name: "view", value: "tasks")]
        request.url = parts.url
        let (data, response) = try await controlSession.data(for: request, delegate: ResultRedirectGuard())
        try resultResponse(response, request: request)
        let runs = try JSONDecoder().decode(Envelope.self, from: data).laeufe
        guard runs.allSatisfy({ AgentRun.validRunID($0.id) }) else { throw ClientError.decode }
        return runs
    }

    func agentRunPage(before: String? = nil, query: String = "") async throws -> AgentRunPage {
        guard before == nil || AgentRun.validRunID(before!) else { throw ClientError.decode }
        var request = controlRequest("v1/agent/runs")
        guard let url = request.url, var parts = URLComponents(url: url, resolvingAgainstBaseURL: false) else { throw ClientError.decode }
        var items: [URLQueryItem] = []
        if let before { items.append(URLQueryItem(name: "before", value: before)) }
        if !query.isEmpty { items.append(URLQueryItem(name: "q", value: query)) }
        parts.queryItems = items.isEmpty ? nil : items
        guard let pagedURL = parts.url else { throw ClientError.decode }
        request.url = pagedURL
        let (data, response) = try await controlSession.data(for: request, delegate: ResultRedirectGuard())
        try resultResponse(response, request: request)
        let page = try JSONDecoder().decode(AgentRunPage.self, from: data)
        try page.validate(previous: before, searching: !query.isEmpty)
        return page
    }

    func agentRun(_ runID: String) async throws -> AgentRun {
        guard AgentRun.validRunID(runID) else { throw ClientError.decode }
        let request = controlRequest("v1/agent/runs/\(runID)")
        let (data, response) = try await controlSession.data(for: request, delegate: ResultRedirectGuard())
        try resultResponse(response, request: request)
        let run = try JSONDecoder().decode(AgentRun.self, from: data)
        guard run.id == runID else { throw ClientError.decode }
        return run
    }

    func controlAgentRun(_ runID: String, action: String) async throws {
        guard AgentRun.validRunID(runID), ["cancel", "resume"].contains(action) else { throw ClientError.decode }
        let request = controlRequest("v1/agent/runs/\(runID)/\(action)", "POST")
        let (_, response) = try await controlSession.data(for: request, delegate: ResultRedirectGuard())
        try resultResponse(response, request: request)
    }

    func switchAgentRun(_ runID: String, provider: String, boundaryRef: String) async throws {
        guard AgentRun.validRunID(runID), ["codex", "claude-code"].contains(provider),
              boundaryRef.range(of: "^[a-f0-9]{64}$", options: .regularExpression) != nil else { throw ClientError.decode }
        var request = controlRequest("v1/agent/runs/\(runID)/resume", "POST")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: ["provider": provider, "boundary_ref": boundaryRef])
        let (_, response) = try await controlSession.data(for: request, delegate: ResultRedirectGuard())
        try resultResponse(response, request: request)
    }

    func resultFile(_ file: ResultFile, runID: String) async throws -> Data {
        let request = controlRequest(try file.downloadPath(runID: runID))
        let (bytes, response) = try await controlSession.bytes(for: request, delegate: ResultRedirectGuard())
        try resultResponse(response, request: request)
        guard response.mimeType?.lowercased() == file.mime_type,
              response.expectedContentLength < 0 || response.expectedContentLength == Int64(file.size) else {
            throw ResultFileError.responseChanged
        }
        var data = Data(); data.reserveCapacity(file.size)
        for try await byte in bytes {
            try Task.checkCancellation()
            guard data.count < file.size else { throw ResultFileError.contentChanged }
            data.append(byte)
        }
        try file.verify(data: data, mimeType: response.mimeType, runID: runID)
        return data
    }

    private func resultResponse(_ response: URLResponse, request: URLRequest) throws {
        guard let http = response as? HTTPURLResponse, http.statusCode == 200,
              response.url == request.url else {
            throw ClientError.http((response as? HTTPURLResponse)?.statusCode ?? -1)
        }
    }
}

/// UI protection belongs to one currently displayed Core question. A failed
/// read retains that question; only a different confirmed question (or none)
/// releases its protection. Late callbacks cannot affect another question.
struct AgentQuestionNavigationProtection {
    private var bindingID: String?
    private var protected = false

    mutating func synchronize(_ currentBindingID: String?) {
        guard bindingID != currentBindingID else { return }
        bindingID = currentBindingID
        protected = false
    }

    mutating func report(_ value: Bool, for reportedBindingID: String, current currentBindingID: String?) {
        guard reportedBindingID == currentBindingID else { return }
        synchronize(currentBindingID)
        protected = value
    }

    func isProtected(for currentBindingID: String?) -> Bool {
        currentBindingID != nil && bindingID == currentBindingID && protected
    }
}

/// No durable task copy: refreshing reads the Core. A late reply from an old
/// view cannot repaint a different selection or trigger another effect.
@MainActor
final class AgentResultsModel: ObservableObject {
    @Published private(set) var runs: [AgentRun] = []
    @Published private(set) var detail: AgentRun?
    @Published private(set) var error = ""
    @Published private(set) var loading = false
    @Published private(set) var working = false
    @Published private(set) var actionMessage = ""
    private var generation = UUID()
    private var actionPending = false

    static func latestTasks(_ value: [AgentRun]) -> [AgentRun] {
        var order: [String] = [], selected: [String: AgentRun] = [:]
        for run in value {
            let key = run.aufgabe.isEmpty ? run.id : run.aufgabe
            if let prior = selected[key] {
                let oldRevision = prior.task_revision?.revision ?? 1
                let newRevision = run.task_revision?.revision ?? 1
                if newRevision > oldRevision || (newRevision == oldRevision && (run.angelegt ?? 0) > (prior.angelegt ?? 0)) {
                    selected[key] = run
                }
            } else { order.append(key); selected[key] = run }
        }
        return order.compactMap { selected[$0] }
    }

    func invalidate() { generation = UUID(); detail = nil; runs = []; error = ""; actionMessage = ""; loading = false }

    func refreshList(load: () async throws -> [AgentRun]) async {
        guard !loading else { return }
        let current = generation; loading = true
        defer { if generation == current { loading = false } }
        do {
            let value = try await load()
            guard generation == current, !Task.isCancelled else { return }
            runs = Self.latestTasks(value); error = ""
        } catch {
            if generation == current, !Task.isCancelled { self.error = "Der aktuelle Auftragsstand konnte nicht gelesen werden. Der Core kann weiterarbeiten." }
        }
    }

    func refreshDetail(runID: String, load: () async throws -> AgentRun) async {
        guard !loading else { return }
        let current = generation; loading = true
        defer { if generation == current { loading = false } }
        do {
            let value = try await load()
            guard value.id == runID else { throw ClientError.decode }
            guard generation == current, !Task.isCancelled else { return }
            detail = value; error = ""
        } catch {
            if generation == current, !Task.isCancelled { self.error = "Der aktuelle Stand konnte nicht gelesen werden. Angezeigt wird gegebenenfalls der letzte gelesene Stand." }
        }
    }

    func act(runID: String, action: String, perform: () async throws -> Void) async {
        guard !actionPending, detail?.id == runID,
              (action == "cancel" && detail?.offen == true || action == "resume" && detail?.canResume == true) else { return }
        actionPending = true; working = true; let current = generation
        defer { actionPending = false; working = false }
        do {
            try await perform()
            guard current == generation, !Task.isCancelled else { return }
            actionMessage = action == "cancel" ? "Abbruch bestätigt. Bereits erfolgte Wirkungen bleiben bestehen." : "Fortsetzung angefragt. Der aktuelle Stand wird neu gelesen."
        } catch {
            if current == generation, !Task.isCancelled { actionMessage = "Die Änderung wurde nicht bestätigt. Prüfe den aktuellen Stand, bevor du erneut entscheidest." }
        }
    }
}


struct AgentRunPage: Decodable {
    let laeufe: [AgentRun]
    let next_before: String?

    func validate(previous: String?, searching: Bool = false) throws {
        guard laeufe.allSatisfy({ AgentRun.validRunID($0.id) }),
              Set(laeufe.map(\.id)).count == laeufe.count,
              next_before == nil || (AgentRun.validRunID(next_before!) && next_before != previous && (searching || next_before == laeufe.last?.id))
        else { throw ClientError.decode }
    }
}

/// Ephemeral projection of all loaded run pages; older revisions keep their files.
@MainActor
final class AgentLibraryModel: ObservableObject {
    @Published private(set) var runs: [AgentRun] = []
    @Published private(set) var next: String?
    @Published private(set) var loading = false
    @Published private(set) var error = ""
    @Published private(set) var loaded = false
    private(set) var query = ""
    private var identity: String?
    private var generation = UUID()

    func invalidate() { generation = UUID(); runs = []; next = nil; loading = false; error = ""; loaded = false }

    func select(query: String, identity: String?) {
        let normalized = query.trimmingCharacters(in: .whitespacesAndNewlines)
        if self.query != normalized || self.identity != identity {
            invalidate(); self.query = normalized; self.identity = identity
        }
    }

    // Navigation cancels an in-flight read, but retains loaded pages and search.
    func cancelLoading() { generation = UUID(); loading = false }

    func load(older: Bool = false, fetch: (String?) async throws -> AgentRunPage) async {
        guard !loading, !older || next != nil else { return }
        let current = generation, cursor = older ? next : nil
        loading = true; error = ""
        defer { if generation == current { loading = false } }
        do {
            var nextCursor = cursor
            var visited = Set<String>()
            if let cursor { visited.insert(cursor) }
            var found: [AgentRun] = []
            repeat {
                try Task.checkCancellation()
                let page = try await fetch(nextCursor)
                try page.validate(previous: nextCursor, searching: !query.isEmpty)
                guard current == generation, !Task.isCancelled else { return }
                if let next = page.next_before, !visited.insert(next).inserted { throw ClientError.decode }
                found += page.laeufe; nextCursor = page.next_before
            } while !query.isEmpty && nextCursor != nil && found.count < 25
            var combined = older ? runs : []
            for run in found {
                if let index = combined.firstIndex(where: { $0.id == run.id }) { combined[index] = run }
                else { combined.append(run) }
            }
            runs = combined; next = nextCursor; loaded = true
        } catch {
            guard current == generation, !Task.isCancelled else { return }
            self.error = "Ergebnisse konnten nicht nachgeladen werden. Bereits geladene Ergebnisse bleiben sichtbar. Bitte erneut versuchen."
        }
    }
}
