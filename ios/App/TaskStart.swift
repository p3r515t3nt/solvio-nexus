// Written app tasks reuse the enrolled App Attest key and pinned gateway client.
// Authentication of this explicit task needs no additional Face-ID decision.
import Foundation
import SwiftUI
import UniformTypeIdentifiers
import SolvioApprovalsKit

extension ApprovalClient {
    func taskPortalSessions() async throws -> TaskPortalSessions {
        var request = controlRequest("v1/agent/action-portal-sessions")
        guard let url = request.url, var components = URLComponents(url: url, resolvingAgainstBaseURL: false) else { throw ClientError.decode }
        components.queryItems = [URLQueryItem(name: "limit", value: "50")]
        request.url = components.url
        let (data, response) = try await controlSession.data(for: request, delegate: ResultRedirectGuard())
        guard let http = response as? HTTPURLResponse, http.statusCode == 200,
              response.url == request.url else { throw ClientError.decode }
        return try TaskPortalSessions.decode(data)
    }

    func taskHomeResources(account: ActionServiceAccount) async throws -> TaskHomeResources {
        guard account.service == "ha", TaskHomeResources.validAccount(account.account) else { throw TaskActionError.invalidFields }
        var request = controlRequest("v1/agent/action-resources")
        guard let url = request.url, var components = URLComponents(url: url, resolvingAgainstBaseURL: false) else { throw ClientError.decode }
        components.queryItems = [URLQueryItem(name: "service", value: "ha"),
            URLQueryItem(name: "account", value: account.account), URLQueryItem(name: "limit", value: "100")]
        request.url = components.url
        let (data, response) = try await controlSession.data(for: request, delegate: ResultRedirectGuard())
        guard let http = response as? HTTPURLResponse, http.statusCode == 200,
              response.url == request.url else { throw ClientError.decode }
        return try TaskHomeResources.decode(data, account: account)
    }

    func taskActionServices() async throws -> [ActionServiceAccount] {
        struct Envelope: Decodable { let services: [ActionServiceAccount] }
        let request = controlRequest("v1/agent/action-services")
        let (data, response) = try await controlSession.data(for: request, delegate: ResultRedirectGuard())
        guard let http = response as? HTTPURLResponse, http.statusCode == 200,
              response.url == request.url else { throw ClientError.decode }
        return try JSONDecoder().decode(Envelope.self, from: data).services
    }

    func taskStartChallenge(_ task: AppTaskBody) async throws -> AppTaskChallenge {
        struct Body: Encodable { let task: AppTaskBody }
        var request = controlRequest("v1/agent/tasks/challenge", "POST")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(Body(task: task))
        let (data, response) = try await controlSession.data(for: request)
        guard (response as? HTTPURLResponse)?.statusCode == 200 else {
            throw ClientError.http((response as? HTTPURLResponse)?.statusCode ?? -1)
        }
        return try JSONDecoder().decode(AppTaskChallenge.self, from: data)
    }

    func submitTask(_ task: AppTaskBody, proof: AppTaskProof) async throws -> AppTaskAccepted {
        struct Body: Encodable { let task: AppTaskBody; let proof: AppTaskProof }
        var request = controlRequest("v1/agent/tasks", "POST")
        // The fresh proof is the start credential. The static transport secret is
        // needed only to fetch the challenge, not to authorize this POST.
        request.setValue(nil, forHTTPHeaderField: "X-Transport-Cred")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(Body(task: task, proof: proof))
        let (data, response) = try await controlSession.data(for: request)
        let status = (response as? HTTPURLResponse)?.statusCode ?? -1
        guard [201, 202].contains(status) else {
            throw ClientError.http((response as? HTTPURLResponse)?.statusCode ?? -1)
        }
        return try AppTaskAccepted.response(data: data, status: status)
    }
}

/// Four transport references in the existing device-only Keychain; never the task text.
private enum TaskRetryCache {
    private static func tag(_ client: ApprovalClient) -> String {
        let scope = client.pairedCoreInstanceID + "\0" + client.deviceIdentifier
        return "de.solvio.approvals.taskRetry." + ApprovalCrypto.sha256Hex(Data(scope.utf8))
    }
    static func load(_ client: ApprovalClient) -> TaskStartRetryBinding? {
        guard let data = Keychain.load(tag: tag(client)),
              let binding = try? JSONDecoder().decode(TaskStartRetryBinding.self, from: data),
              binding.coreID == client.pairedCoreInstanceID, binding.deviceID == client.deviceIdentifier else { return nil }
        return binding
    }
    static func save(_ body: AppTaskBody, client: ApprovalClient) throws {
        let binding = TaskStartRetryBinding(body: body, coreID: client.pairedCoreInstanceID,
                                            deviceID: client.deviceIdentifier)
        let encoder = JSONEncoder(); encoder.outputFormatting = [.sortedKeys]
        let data = try encoder.encode(binding), key = tag(client)
        // Do not delete/rewrite an already durable retry binding before a retry.
        if Keychain.load(tag: key) != data { Keychain.save(data, tag: key) }
        guard Keychain.load(tag: key) == data else {
            throw NSError(domain: "TaskRetryCache", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "Die Wiederholung konnte nicht abgesichert werden. Es wurde kein Auftrag gesendet."])
        }
    }
    static func clear(_ client: ApprovalClient) { Keychain.delete(tag: tag(client)) }
}

@MainActor
final class TaskStartModel: ObservableObject {
    @Published var scope = "research" { didSet { if oldValue != scope { clearDocument(); invalidateHomeResources(); invalidatePortalSessions(); edited() } } }
    @Published var objective = "" { didSet { if oldValue != objective { edited() } } }
    @Published var exactAction = false { didSet { if oldValue != exactAction { invalidateHomeResources(); invalidatePortalSessions(); edited() } } }
    @Published var repository = "" { didSet { if oldValue != repository { edited() } } }
    @Published var actionForm = TaskActionForm() {
        didSet {
            if oldValue.kind != actionForm.kind || oldValue.selectedAccount != actionForm.selectedAccount {
                homeGeneration = UUID(); homeLoading = false; homeMessage = ""
                actionForm.home.invalidate()
                portalGeneration = UUID(); portalLoading = false; portalMessage = ""
                actionForm.portal.invalidate()
            }
            if !oldValue.hasSameInput(as: actionForm) && scope == "action" && exactAction { edited() }
        }
    }
    @Published private(set) var document: TaskDocumentSelection?
    @Published private(set) var files: TaskFileSelection?
    @Published private(set) var documentLoading = false
    @Published private(set) var documentError = ""
    private var documentGeneration = UUID()
    private var uncertainDocument = false
    @Published private(set) var actionServices: [ActionServiceAccount] = []
    @Published private(set) var servicesLoading = false
    @Published private(set) var servicesMessage = ""
    @Published private(set) var homeLoading = false
    @Published private(set) var homeMessage = ""
    private var homeGeneration = UUID()
    @Published private(set) var portalLoading = false
    @Published private(set) var portalMessage = ""
    private var portalGeneration = UUID()
    @Published private(set) var sending = false
    @Published private(set) var accepted: AppTaskAccepted?
    @Published private(set) var message = ""
    @Published private(set) var needsRetry = false
    private var draft = TaskStartDraft()
    private let attempt = TaskStartAttempt()
    private var revision = UUID()
    private var unconfirmedEarlier = false
    private var preparedInThisProcess = false
    private var forceNewRequest = false
    var hasUnconfirmedRetry: Bool { unconfirmedEarlier }

    private var text: String { scope == "action" && exactAction ? actionForm.objective : objective.trimmingCharacters(in: .whitespacesAndNewlines) }
    private var repo: String { scope == "build" ? repository.trimmingCharacters(in: .whitespacesAndNewlines) : "" }
    private func actionRequest() throws -> AppTaskActionRequest? {
        scope == "action" && exactAction ? try actionForm.request(accounts: actionServices) : nil
    }
    private var actionIntent: AppTaskActionIntent? {
        scope == "action" && !exactAction ? AppTaskActionIntent() : nil
    }
    var actionAccountChoices: [ActionServiceAccount] {
        var seen = Set<ActionServiceAccount>()
        return actionServices.filter { $0.service == actionForm.kind.rawValue && seen.insert($0).inserted }
    }
    var selectedActionAccount: ActionServiceAccount? {
        guard let row = actionForm.selectedAccount, actionAccountChoices.contains(row) else { return nil }
        return row
    }
    func selectActionAccount(_ row: ActionServiceAccount?) {
        guard !sending, accepted == nil, let row else { return }
        do { try actionForm.selectAccount(row, accounts: actionServices) }
        catch { servicesMessage = error.localizedDescription }
    }
    var actionAccountMessage: String {
        do {
            let account = try actionForm.account(accounts: actionServices)
            return actionForm.kind == .calendar ? "\(account.displayLabel): \(account.resource)" : account.displayLabel
        } catch { return error.localizedDescription }
    }
    private var documentRequest: AppTaskDocumentRequest? { scope == "research" ? document?.request : nil }
    private var fileRequest: AppTaskFileRequest? { scope == "research" ? files?.request : nil }
    var valid: Bool {
        guard !documentLoading, documentError.isEmpty else { return false }
        guard !servicesLoading || scope != "action" || !exactAction || actionForm.kind == .portal else { return false }
        guard !homeLoading || scope != "action" || !exactAction || actionForm.kind != .ha else { return false }
        guard !portalLoading || scope != "action" || !exactAction || actionForm.kind != .portal else { return false }
        do {
            _ = try AppTaskBody(scope: scope, objective: text, targetRepo: repo,
                               requestID: "validation-only", actionRequest: actionRequest(), documentRequest: documentRequest,
                               fileRequest: fileRequest, actionIntent: actionIntent)
            return true
        } catch { return false }
    }

    func clearDocument() {
        documentGeneration = UUID()
        document = nil; files = nil; documentLoading = false; documentError = ""
        edited()
    }

    func selectDocument(_ url: URL) async {
        guard scope == "research", !sending, accepted == nil else { return }
        clearDocument()
        let generation = documentGeneration
        documentLoading = true
        defer { if generation == documentGeneration { documentLoading = false } }
        do {
            let selection = try await Task.detached(priority: .userInitiated) {
                let access = url.startAccessingSecurityScopedResource()
                defer { if access { url.stopAccessingSecurityScopedResource() } }
                return try TaskDocumentSelection.read(url)
            }.value
            guard generation == documentGeneration, scope == "research", !Task.isCancelled else { return }
            document = selection
            documentLoading = false
        } catch {
            guard generation == documentGeneration, scope == "research", !Task.isCancelled else { return }
            documentLoading = false
            documentError = (error as? TaskDocumentError)?.localizedDescription ?? TaskDocumentError.unreadableFile.localizedDescription
        }
    }

    func selectFiles(_ urls: [URL]) async {
        guard scope == "research", !sending, accepted == nil, !urls.isEmpty else { return }
        if urls.count == 1 && AppTaskDocumentRequest.formats.contains(urls[0].pathExtension.lowercased()) {
            await selectDocument(urls[0]); return
        }
        clearDocument()
        let generation = documentGeneration
        documentLoading = true
        defer { if generation == documentGeneration { documentLoading = false } }
        do {
            let selection = try await Task.detached(priority: .userInitiated) {
                let scopes = urls.map { ($0, $0.startAccessingSecurityScopedResource()) }
                defer { for (url, access) in scopes where access { url.stopAccessingSecurityScopedResource() } }
                return try TaskFileSelection.read(urls)
            }.value
            guard generation == documentGeneration, scope == "research", !Task.isCancelled else { return }
            files = selection; documentLoading = false
        } catch {
            guard generation == documentGeneration, scope == "research", !Task.isCancelled else { return }
            documentLoading = false
            documentError = (error as? TaskFileError)?.localizedDescription ?? TaskFileError.unreadableFile.localizedDescription
        }
    }

    func documentPickerFailed(_ error: Error) {
        guard !sending, accepted == nil else { return }
        clearDocument()
        documentError = error.localizedDescription
    }

    func loadActionServices(client: ApprovalClient) async {
        guard !servicesLoading, !sending, accepted == nil else { return }
        servicesLoading = true; servicesMessage = ""
        let core = client.pairedCoreInstanceID, device = client.deviceIdentifier
        defer { servicesLoading = false }
        do {
            let rows = try await client.taskActionServices()
            guard !Task.isCancelled else { return }
            guard core == client.pairedCoreInstanceID, device == client.deviceIdentifier else { return }
            actionServices = rows
            actionForm.reconcileAccounts(rows)
        } catch {
            guard !Task.isCancelled else { return }
            actionServices = []
            invalidateHomeResources()
            servicesMessage = "Die Konten konnten nicht vom Core geladen werden. Bitte erneut laden."
        }
    }

    func invalidateHomeResources() {
        homeGeneration = UUID(); homeLoading = false; homeMessage = ""
        actionForm.home.invalidate()
    }

    func invalidatePortalSessions() {
        portalGeneration = UUID(); portalLoading = false; portalMessage = ""
        actionForm.portal.invalidate()
    }

    func selectPortalSession(_ account: String?) {
        guard !sending, accepted == nil else { return }
        do { try actionForm.portal.select(account) }
        catch { portalMessage = error.localizedDescription }
    }

    func loadPortalSessions(client: ApprovalClient) async {
        let core = client.pairedCoreInstanceID, device = client.deviceIdentifier
        await loadPortalSessions(coreID: core, deviceID: device,
            fetch: { try await client.taskPortalSessions() },
            stillConnected: { core == client.pairedCoreInstanceID && device == client.deviceIdentifier })
    }

    func loadPortalSessions(coreID: String, deviceID: String,
        fetch: () async throws -> TaskPortalSessions, stillConnected: () -> Bool = { true }) async {
        // A read never invalidates the captured body of an in-flight submission.
        guard !sending, accepted == nil else { return }
        guard scope == "action", exactAction, actionForm.kind == .portal,
              !coreID.isEmpty, !deviceID.isEmpty else { invalidatePortalSessions(); return }
        invalidatePortalSessions()
        let generation = portalGeneration
        portalLoading = true
        defer { if generation == portalGeneration { portalLoading = false } }
        do {
            let rows = try await fetch()
            guard generation == portalGeneration, !Task.isCancelled, stillConnected(),
                  scope == "action", exactAction, actionForm.kind == .portal else { return }
            try actionForm.portal.accept(rows)
            portalMessage = rows.items.isEmpty ? "Es ist keine eigene Portal-Sitzung verfügbar. Eine Verbindung ist damit nicht bestätigt." : ""
        } catch {
            guard generation == portalGeneration, !Task.isCancelled else { return }
            actionForm.portal.invalidate()
            portalMessage = "Die Portal-Sitzungen konnten nicht aktuell vom Core geladen werden. Bitte erneut laden."
        }
    }

    func selectHomeDevice(_ entityID: String?) {
        guard !sending, accepted == nil else { return }
        do { try actionForm.home.select(entityID) }
        catch { homeMessage = error.localizedDescription }
    }

    func loadHomeResources(client: ApprovalClient) async {
        let core = client.pairedCoreInstanceID, device = client.deviceIdentifier
        await loadHomeResources(coreID: core, deviceID: device,
            fetch: { try await client.taskHomeResources(account: $0) },
            stillConnected: { core == client.pairedCoreInstanceID && device == client.deviceIdentifier })
    }

    // The closure seam permits an isolated response-race test; the production
    // caller above always uses the enrolled client's pinned read endpoint.
    func loadHomeResources(coreID: String, deviceID: String,
        fetch: (ActionServiceAccount) async throws -> TaskHomeResources,
        stillConnected: () -> Bool = { true }) async {
        // Reopening the view can run its read task during submission. Do not
        // invalidate that captured request or suppress its uncertain result.
        guard !sending, accepted == nil else { return }
        guard scope == "action", exactAction, actionForm.kind == .ha,
              let account = selectedActionAccount,
              !coreID.isEmpty, !deviceID.isEmpty else { invalidateHomeResources(); return }
        invalidateHomeResources()
        let generation = homeGeneration
        homeLoading = true
        defer { if generation == homeGeneration { homeLoading = false } }
        do {
            let rows = try await fetch(account)
            guard generation == homeGeneration, !Task.isCancelled, stillConnected(),
                  scope == "action", exactAction, actionForm.kind == .ha,
                  selectedActionAccount == account else { return }
            try actionForm.home.accept(rows, account: account)
            homeMessage = rows.items.isEmpty ? "Es sind keine schaltbaren Geräte freigegeben." : ""
        } catch {
            guard generation == homeGeneration, !Task.isCancelled else { return }
            actionForm.home.invalidate()
            homeMessage = "Die Geräte konnten nicht aktuell vom Core geladen werden. Bitte erneut laden."
        }
    }

    private func edited() {
        revision = UUID()
        if preparedInThisProcess { forceNewRequest = true }
        draft.invalidate()
        accepted = nil
        if unconfirmedEarlier {
            message = "Der vorherige Auftrag wurde nicht bestätigt. Mit geänderten Angaben erteilst du einen neuen Auftrag."
        } else { message = "" }
        needsRetry = false
    }

    func reset() {
        revision = UUID()
        objective = ""; repository = ""; scope = "research"; exactAction = false; actionForm = TaskActionForm()
        draft.invalidate(); accepted = nil; message = ""; needsRetry = false
        unconfirmedEarlier = false
        uncertainDocument = false
        clearDocument()
        forceNewRequest = true
    }

    func restoreRetryHint(client: ApprovalClient) {
        guard !preparedInThisProcess, !forceNewRequest, accepted == nil,
              let retry = TaskRetryCache.load(client) else { return }
        unconfirmedEarlier = true
        uncertainDocument = retry.attachmentKind != nil
        message = "Eine frühere Annahme ist noch ungeklärt. Gib denselben Auftrag erneut ein, um sie zu prüfen."
            + (uncertainDocument ? " Wähle dazu dieselben Anhänge erneut aus." : "")
            + " Es wird nichts automatisch gesendet."
    }

    func send(client: ApprovalClient, keyID: String) async {
        guard !sending, accepted == nil, valid else { return }
        sending = true
        message = ""
        let submittedRevision = revision
        defer { sending = false }
        do {
            let action = try actionRequest()
            let retry = TaskRetryCache.load(client)
            // Unknown admission never becomes a new request through missing
            // input, a scope change, or old metadata without an attachment tag.
            // Only the explicit new-task action clears unconfirmedEarlier.
            let boundRetry = unconfirmedEarlier || scope == "action" || documentRequest != nil || fileRequest != nil || uncertainDocument
            if boundRetry && unconfirmedEarlier {
                guard retry?.restore(scope: scope, objective: text, targetRepo: repo,
                    coreID: client.pairedCoreInstanceID, deviceID: client.deviceIdentifier,
                    actionRequest: action, documentRequest: documentRequest, fileRequest: fileRequest, actionIntent: actionIntent) != nil else {
                    needsRetry = false
                    message = "Die vorherige Annahme ist ungeklärt. Zum Prüfen müssen alle Angaben gleich bleiben. Sieh in der Auftragsliste nach oder beginne ausdrücklich einen neuen Auftrag."
                    return
                }
            }
            // Capture BEFORE the first await. Every network retry retains this exact
            // body and request ID, but the flow always requests a new nonce/assertion.
            let task = try draft.prepare(scope: scope, objective: text, targetRepo: repo,
                retry: (forceNewRequest && !(boundRetry && unconfirmedEarlier)) ? nil : retry,
                coreID: client.pairedCoreInstanceID, deviceID: client.deviceIdentifier, actionRequest: action,
                documentRequest: documentRequest, fileRequest: fileRequest, actionIntent: actionIntent)
            preparedInThisProcess = true
            uncertainDocument = task.document_request != nil || task.file_request != nil
            try TaskRetryCache.save(task, client: client)
            let result = try await attempt.send(body: task, coreID: client.pairedCoreInstanceID,
                deviceID: client.deviceIdentifier, appAttestKeyID: keyID,
                challenge: { try await client.taskStartChallenge($0) },
                sign: { try await AppAttestManager.assert(keyId: keyID, clientDataHash: $0) },
                submit: { try await client.submitTask($0, proof: $1) })
            TaskRetryCache.clear(client)
            guard revision == submittedRevision else { return } // old pairing/draft cannot repaint a new one
            accepted = result
            needsRetry = false
            unconfirmedEarlier = false
            uncertainDocument = false
            message = result.isPreparing
                ? "Der Core hat deinen Auftrag erfasst und bereitet die Annahme vor. Das Ergebnis steht noch aus."
                : "Der Core hat deinen Auftrag angenommen. Das Ergebnis steht noch aus."
        } catch {
            guard revision == submittedRevision else { return }
            needsRetry = true
            unconfirmedEarlier = true
            message = "Keine Annahme bestätigt. Erneut versuchen verwendet denselben Auftrag. \(error.localizedDescription)"
        }
    }
}

struct TaskStartView: View {
    @ObservedObject var app: AppModel
    @ObservedObject var model: TaskStartModel
    var embedded = false
    @Environment(\.dynamicTypeSize) private var typeSize
    @State private var showDocumentPicker = false
    @State private var documentExpanded = false
    @State private var optionsExpanded = false
    @State private var confirmNewTask = false
    @State private var resultProtectsNavigation = false

    private var scopeLabel: String {
        model.scope == "action" ? "Im Alltag erledigen" : model.scope == "build" ? "Codearbeit" : "Allgemeiner Auftrag"
    }
    private var scopeMenu: some View {
        Picker("Aufgabe", selection: $model.scope) {
            Text("Allgemeiner Auftrag").tag("research")
            Text("Codearbeit").tag("build")
            Text("Im Alltag erledigen").tag("action")
        }
    }
    private var inputChoices: some View {
        Group {
            Text("In eigenen Worten").tag(false)
            Text("Details eingeben").tag(true)
        }
    }

    var body: some View {
        Group {
            if embedded { content }
            else {
                ScrollView { content.padding(Theme.Space.margin) }
                    .background(Theme.bg)
                    .navigationTitle("Neuer Auftrag")
                    .navigationBarTitleDisplayMode(.inline)
            }
        }
        .fileImporter(isPresented: $showDocumentPicker,
            allowedContentTypes: (AppTaskDocumentRequest.formats + TaskFileSelection.formats).compactMap { UTType(filenameExtension: $0) },
            allowsMultipleSelection: true) { result in
                switch result {
                case let .success(urls):
                    Task { await model.selectFiles(urls) }
                case let .failure(error): model.documentPickerFailed(error)
                }
            }
        .onAppear { if let client = app.client { model.restoreRetryHint(client: client) } }
        .task(id: model.scope + (model.exactAction ? ":exact:" : ":natural:") + model.actionForm.kind.rawValue) {
            if model.scope == "action", model.exactAction, model.actionForm.kind != .portal,
               let client = app.client { await model.loadActionServices(client: client) }
        }
        .confirmationDialog("Die Annahme des vorherigen Auftrags ist noch ungeklärt.",
            isPresented: $confirmNewTask, titleVisibility: .visible) {
                Button("Trotzdem einen neuen Auftrag beginnen") {
                    guard !model.sending else { return }
                    model.reset()
                }
                Button("Beim bisherigen Auftrag bleiben", role: .cancel) { }
            } message: {
                Text("Ein neuer Auftrag klärt oder beendet den vorherigen nicht. Du findest vorhandene Aufträge unter Aufträge.")
            }
    }

    private var content: some View {
        VStack(alignment: .leading, spacing: 20) {
            if let accepted = model.accepted {
                Text(verbatim: model.message).font(.footnote).foregroundStyle(Theme.ink2)
                if AgentRun.validRunID(accepted.run_id) {
                    AgentResultView(app: app, runID: accepted.run_id, embedded: true, onNavigationLockChanged: { locked in
                        guard model.accepted?.run_id == accepted.run_id else { return }
                        resultProtectsNavigation = locked
                    })
                        .id(accepted.run_id + (app.client?.pairedCoreInstanceID ?? "") + (app.client?.deviceIdentifier ?? ""))
                } else {
                    Text("Der Core hat die Annahme bestätigt, aber die Auftragskennung lässt sich nicht öffnen. Prüfe den Auftrag in der Auftragsliste.")
                        .font(.body).foregroundStyle(Theme.warn)
                    NavigationLink("Auftragsliste öffnen") { AgentTasksView(app: app) }
                        .buttonStyle(.bordered)
                }
                Button("Neuer Auftrag") { if !model.sending && !resultProtectsNavigation { model.reset() } }
                    .buttonStyle(.bordered).disabled(model.sending || resultProtectsNavigation)
            } else {
                VStack(alignment: .leading, spacing: 16) {
                    Text(typeSize.isAccessibilitySize ? "Dein Auftrag" : "Was soll ich für dich erledigen?")
                        .font(.headline).foregroundStyle(Theme.ink)
                    inputs.disabled(model.sending || model.accepted != nil)
                    if !model.message.isEmpty {
                        Text(verbatim: model.message).font(.footnote).foregroundStyle(Theme.warn)
                            .accessibilityIdentifier("task.message")
                    }
                    submitButton
                    if model.hasUnconfirmedRetry && !model.sending {
                        Button("Neuen Auftrag beginnen") { confirmNewTask = true }
                            .font(.footnote).frame(minHeight: 44)
                    }
                }
                .padding(Theme.Space.card).card()
                Text("Angenommene Aufträge laufen weiter, wenn du die App schließt.")
                    .font(.caption).foregroundStyle(Theme.ink2)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private var inputs: some View {
        VStack(alignment: .leading, spacing: 16) {
            if model.scope != "action" || !model.exactAction {
                TextField("Schreibe deinen Auftrag …", text: $model.objective, axis: .vertical)
                    .lineLimit(3...10).frame(minHeight: 96, alignment: .topLeading)
                    .accessibilityLabel("Dein Auftrag").accessibilityIdentifier("task.objective")
                let count = model.objective.trimmingCharacters(in: .whitespacesAndNewlines).unicodeScalars.count
                if count > 0 && (count < 12 || count > 2000) {
                    Text("12–2000 Zeichen · \(count)").font(.caption).foregroundStyle(Theme.warn)
                }
                if model.scope == "action" {
                    Text("Beschreibe deinen Kalendertermin oder Mailentwurf. Fehlende Angaben fragt SOLVIO im Auftrag nach.")
                        .font(.footnote).foregroundStyle(Theme.ink2)
                }
            }
            if model.scope == "research" { documentInput }
            DisclosureGroup("Auftragsart & Optionen", isExpanded: $optionsExpanded) {
                VStack(alignment: .leading, spacing: 16) {
                    TaskAccessibleMenu("Aufgabe", value: scopeLabel) { scopeMenu }
                    if model.scope == "action" {
                        TaskAccessibleMenu("Eingabe", value: model.exactAction ? "Details eingeben" : "In eigenen Worten") {
                            Picker("Eingabe", selection: $model.exactAction) { inputChoices }
                        }
                    }
                    if model.scope == "build" {
                        TextField("Repository (optional)", text: $model.repository)
                            .textInputAutocapitalization(.never).autocorrectionDisabled()
                    }
                }.padding(.top, 12)
            }
            .font(.subheadline).accessibilityIdentifier("task.options")
            if model.scope == "action" && model.exactAction {
                VStack(alignment: .leading, spacing: 16) {
                    TaskActionFields(model: model, client: app.client)
                        .environment(\.timeZone, TimeZone(identifier: model.actionForm.timeZoneIdentifier) ?? .gmt)
                }
            }
            Text(scopeLabel).font(.caption).foregroundStyle(Theme.ink2)
        }
    }

    private var documentInput: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let document = model.document {
                Label(document.filename, systemImage: "paperclip")
                    .font(.caption).foregroundStyle(Theme.ink2).lineLimit(2)
            }
            if let files = model.files {
                Label(files.request.files.map(\.name).joined(separator: " · "), systemImage: "paperclip")
                    .font(.caption).foregroundStyle(Theme.ink2).lineLimit(3)
            }
            if !model.documentError.isEmpty {
                Text(verbatim: model.documentError).font(.footnote).foregroundStyle(Theme.warn)
            }
            DisclosureGroup("Dateien anhängen", isExpanded: $documentExpanded) {
                VStack(alignment: .leading, spacing: 12) {
                    Button(model.document == nil && model.files == nil ? "Dateien auswählen" : "Andere Dateien auswählen") { showDocumentPicker = true }
                        .disabled(model.documentLoading).frame(minHeight: 44)
                    if model.documentLoading { ProgressView("Datei wird lokal gelesen …") }
                    if let document = model.document {
                        Text("\(document.request.format.uppercased()) · \(document.request.byteCount.formatted(.number.locale(Locale(identifier: "de_DE")))) Bytes")
                            .font(.caption).foregroundStyle(Theme.ink2)
                    }
                    if model.document != nil || model.files != nil || !model.documentError.isEmpty || model.documentLoading {
                        Button("Anhang entfernen", role: .destructive) { model.clearDocument() }.frame(minHeight: 44)
                    }
                    Text("Bis zu vier CSV-/XLSX-Tabellen, zusammen 8 MiB, für eine Übersicht mit Exceldatei, Diagramm und PDF. Oder ein Textdokument: TXT/RTF bis 64 KiB, DOCX/ODT bis 1 MiB.")
                        .font(.caption).foregroundStyle(Theme.ink2)
                }.padding(.top, 12)
            }.font(.subheadline).accessibilityIdentifier("task.document")
        }
    }

    private var submitButton: some View {
        VStack(alignment: .leading, spacing: 10) {
            Button {
                guard let client = app.client, let keyID = app.taskStartKeyID else { return }
                Task { await model.send(client: client, keyID: keyID) }
            } label: {
                HStack(spacing: 10) {
                    Text(model.sending ? "Wird gesendet …" : model.needsRetry ? "Erneut versuchen" : "Auftrag senden")
                        .fixedSize(horizontal: false, vertical: true)
                    if model.sending { ProgressView() }
                    else { Image(systemName: "arrow.up").accessibilityHidden(true) }
                }
                .font(.body.weight(.semibold)).multilineTextAlignment(.center)
                .frame(maxWidth: .infinity, minHeight: 48).padding(.horizontal, 12).padding(.vertical, 4)
                .foregroundStyle(Theme.onBlue).background(Theme.blue, in: RoundedRectangle(cornerRadius: 14))
            }
            .buttonStyle(.plain).accessibilityIdentifier("task.send")
            .disabled(!model.valid || model.sending || model.accepted != nil || !app.reachable || app.client == nil || app.taskStartKeyID == nil
                      || !app.attested || !AppAttestManager.isSupported)
            if !AppAttestManager.isSupported {
                Text("Dieses Gerät kann keine schriftlichen Aufträge authentifizieren. Verwende ein gekoppeltes iPhone.")
                    .font(.footnote).foregroundStyle(Theme.ink2)
            }
        }
    }
}

// MARK: - Uebergabe an den Chat-Composer (C3)
//
// Dieselbe Datei-/Dokumentauswahl, dieselben Grenzen, dieselbe kanonische Form
// wie im Auftrag. Der Composer besitzt seine eigene Instanz dieses Modells;
// hier wird nur die Auswahl als genau EIN Anhang herausgereicht.
extension TaskStartModel {
    var composerAttachment: AppConversationAttachment? {
        if let files { return .files(files.request) }
        if let document { return .document(document.request) }
        return nil
    }
}
