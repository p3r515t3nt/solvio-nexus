import Foundation
import SwiftUI
import SolvioApprovalsKit

extension ApprovalClient {
    func costApprovalChallenge(_ value: AppTaskCostApprovalBody) async throws -> AppTaskCostApprovalChallenge {
        struct Body: Encodable { let cost_approval: AppTaskCostApprovalBody }
        var request = controlRequest("v1/agent/tasks/\(value.task_id)/cost-approval/challenge", "POST")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(Body(cost_approval: value))
        let (data, response) = try await controlSession.data(for: request, delegate: ResultRedirectGuard())
        guard let http = response as? HTTPURLResponse, response.url == request.url else { throw ClientError.decode }
        guard http.statusCode == 200 else { throw ClientError.http(http.statusCode) }
        return try JSONDecoder().decode(AppTaskCostApprovalChallenge.self, from: data)
    }
    func submitCostApproval(_ value: AppTaskCostApprovalBody, proof: AppTaskProof) async throws -> AppTaskCostApprovalAccepted {
        struct Body: Encodable { let cost_approval: AppTaskCostApprovalBody; let proof: AppTaskProof }
        var request = controlRequest("v1/agent/tasks/\(value.task_id)/cost-approval", "POST")
        request.setValue(nil, forHTTPHeaderField: "X-Transport-Cred")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(Body(cost_approval: value, proof: proof))
        let (data, response) = try await controlSession.data(for: request, delegate: ResultRedirectGuard())
        guard let http = response as? HTTPURLResponse, response.url == request.url else { throw ClientError.decode }
        guard http.statusCode == 200 else { throw ClientError.http(http.statusCode) }
        return try JSONDecoder().decode(AppTaskCostApprovalAccepted.self, from: data)
    }
}

@MainActor enum TaskCostApprovalRetryCache {
    static func tag(client: ApprovalClient, taskID: String) -> String {
        "de.solvio.approvals.taskCostRetry." + ApprovalCrypto.sha256Hex(
            Data((client.pairedCoreInstanceID + "\0" + client.deviceIdentifier + "\0" + taskID).utf8))
    }
    static func load(client: ApprovalClient, taskID: String) throws -> TaskCostApprovalRetryBinding? {
        guard let data = Keychain.load(tag: tag(client: client, taskID: taskID)) else { return nil }
        let retry = try JSONDecoder().decode(TaskCostApprovalRetryBinding.self, from: data)
        guard retry.restore(taskID: taskID, coreID: client.pairedCoreInstanceID, deviceID: client.deviceIdentifier) != nil else {
            throw ClientError.badServer
        }
        return retry
    }
    static func save(_ body: AppTaskCostApprovalBody, client: ApprovalClient) throws {
        let key = tag(client: client, taskID: body.task_id)
        let retry = TaskCostApprovalRetryBinding(body: body, coreID: client.pairedCoreInstanceID, deviceID: client.deviceIdentifier)
        if let prior = try load(client: client, taskID: body.task_id), prior != retry { throw ClientError.badServer }
        let encoder = JSONEncoder(); encoder.outputFormatting = [.sortedKeys]
        let data = try encoder.encode(retry)
        if Keychain.load(tag: key) != data { Keychain.save(data, tag: key) }
        guard Keychain.load(tag: key) == data else { throw ClientError.decode }
    }
    static func clear(client: ApprovalClient, expected body: AppTaskCostApprovalBody) {
        guard let retry = try? load(client: client, taskID: body.task_id), retry.body == body else { return }
        Keychain.delete(tag: tag(client: client, taskID: body.task_id))
    }
}

@MainActor final class TaskCostApprovalModel: ObservableObject {
    @Published var amount = ""
    @Published private(set) var sending = false
    @Published private(set) var uncertain = false
    @Published private(set) var retryUnavailable = false
    @Published private(set) var accepted: AppTaskCostApprovalAccepted?
    @Published private(set) var message = ""
    private var taskID = "", coreID = "", deviceID = ""
    private var generation = UUID()
    private var pending: AppTaskCostApprovalBody?
    private let attempt = AppTaskCostApprovalAttempt()
    var amountLocked: Bool { sending || uncertain || retryUnavailable || accepted != nil }

    func configure(taskID: String, coreID: String, deviceID: String, retry: TaskCostApprovalRetryBinding?) {
        guard self.taskID != taskID || self.coreID != coreID || self.deviceID != deviceID else { return }
        invalidate(); self.taskID = taskID; self.coreID = coreID; self.deviceID = deviceID
        if let retry {
            guard let body = retry.restore(taskID: taskID, coreID: coreID, deviceID: deviceID) else { blockRetry(); return }
            pending = body; amount = TaskCostAmount.text(body.max_total_cents); uncertain = true
            message = "Eine frühere Bestätigung ist ungeklärt. Erneut versuchen übermittelt denselben Gesamtbetrag."
        }
    }
    func blockRetry() {
        retryUnavailable = true; uncertain = true
        message = "Die gespeicherte Kostenanfrage lässt sich nicht eindeutig zuordnen. Es wird keine neue Freigabe gesendet."
    }
    func invalidate() {
        generation = UUID(); taskID = ""; coreID = ""; deviceID = ""; pending = nil
        amount = ""; sending = false; uncertain = false; retryUnavailable = false; accepted = nil; message = ""
    }
    func valid(run: AgentRun) -> Bool {
        guard !retryUnavailable, accepted == nil, run.aufgabe == taskID,
              let cents = TaskCostAmount.cents(amount) else { return false }
        if uncertain { return pending?.max_total_cents == cents }
        guard let minimum = Self.minimumNewCap(run) else { return false }
        return cents >= minimum
    }
    static func scopedCosts(_ run: AgentRun) -> AgentRun.Costs? {
        guard let costs = run.kosten, costs.configured == true,
              costs.task_id == run.aufgabe, costs.currency == "EUR" else { return nil }
        return costs
    }
    static func minimumNewCap(_ run: AgentRun) -> Int? {
        guard let costs = scopedCosts(run),
              let spent = costs.ai_tool?.spent_cents,
              let reserved = costs.ai_tool?.reserved_cents,
              (0...AppTaskCostApprovalBody.maxCents).contains(spent),
              (0...AppTaskCostApprovalBody.maxCents).contains(reserved) else { return nil }
        let (total, overflow) = spent.addingReportingOverflow(reserved)
        let existing = costs.approved_ai_cap_cents ?? 0
        guard !overflow, total <= AppTaskCostApprovalBody.maxCents,
              (0...AppTaskCostApprovalBody.maxCents).contains(existing) else { return nil }
        return max(total, existing)
    }
    func send(run: AgentRun, coreID: String, deviceID: String, keyID: String,
        loadRetry: () throws -> TaskCostApprovalRetryBinding?, saveRetry: (AppTaskCostApprovalBody) throws -> Void,
        clearRetry: (AppTaskCostApprovalBody) -> Void,
        challenge: (AppTaskCostApprovalBody) async throws -> AppTaskCostApprovalChallenge,
        sign: (Data) async throws -> Data,
        submit: (AppTaskCostApprovalBody, AppTaskProof) async throws -> AppTaskCostApprovalAccepted,
        isCurrent: () -> Bool) async {
        guard !sending, valid(run: run), isCurrent(), self.coreID == coreID, self.deviceID == deviceID,
              run.anbietergrenze?.grund == "cost_approval_required" || uncertain else { return }
        let current = generation
        sending = true; message = ""
        defer { if generation == current { sending = false } }
        var prepared: AppTaskCostApprovalBody?
        var submitted = false
        var wasUncertain = uncertain
        do {
            let prior = try loadRetry()
            wasUncertain = wasUncertain || prior != nil
            let request = try AppTaskCostApprovalBody(taskID: taskID,
                maxTotalCents: TaskCostAmount.cents(amount) ?? -1,
                requestID: prior?.body.client_request_id ?? pending?.client_request_id ?? UUID().uuidString.lowercased())
            if let prior, prior.restore(taskID: taskID, coreID: coreID, deviceID: deviceID) != request {
                blockRetry(); return
            }
            if wasUncertain && pending == nil && prior == nil { blockRetry(); return }
            if wasUncertain, let pending, pending != request { blockRetry(); return }
            prepared = request; pending = request
            try saveRetry(request)  // Durable exact retry binding before any challenge/sign/submit.
            uncertain = true
            let response = try await attempt.send(body: request, coreID: coreID, deviceID: deviceID,
                appAttestKeyID: keyID, challenge: challenge, sign: sign,
                submit: { body, proof in submitted = true; return try await submit(body, proof) },
                isCurrent: { generation == current && isCurrent() })
            clearRetry(request)
            guard generation == current, isCurrent(), !Task.isCancelled else { return }
            accepted = response; uncertain = false; pending = nil
            message = "Gesamtrahmen bestätigt. Der Auftrag startet nicht automatisch. Wähle anschließend ausdrücklich „Fortsetzen“."
        } catch {
            guard generation == current, isCurrent(), !Task.isCancelled else { return }
            let definiteRejection: Bool
            if case ClientError.http(let status) = error { definiteRejection = [400, 403, 404, 409].contains(status) }
            else { definiteRejection = false }
            if !wasUncertain && (!submitted || definiteRejection), let prepared {
                clearRetry(prepared); pending = nil; uncertain = false
                message = "Der Kostenrahmen wurde nicht bestätigt. Lies den aktuellen Auftragsstand und versuche es erneut."
            } else if prepared == nil {
                blockRetry()
            } else {
                uncertain = true
                message = "Noch keine Kostenfreigabe bestätigt. Erneut versuchen verwendet genau diesen Gesamtbetrag."
            }
        }
    }
}

/// One sheet at the existing task card. Closing it never denies a ledger entry.
struct TaskCostApprovalView: View {
    @ObservedObject var app: AppModel
    let run: AgentRun
    let available: Bool
    let refresh: () async -> Void
    @StateObject private var model = TaskCostApprovalModel()
    @State private var showing = false
    @Environment(\.scenePhase) private var scenePhase
    private var scope: String { run.aufgabe + (app.client.map { String(describing: ObjectIdentifier($0)) } ?? "") }
    private var allowed: Bool { available && scenePhase == .active && app.client != nil && app.taskStartKeyID != nil && app.attested && AppAttestManager.isSupported }
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if run.anbietergrenze?.grund == "cost_approval_required" || model.uncertain {
                Button(model.uncertain ? "Kostenfreigabe prüfen" : "Kostenrahmen freigeben") { showing = true }
                    .buttonStyle(.borderedProminent).disabled(!available || app.client == nil)
                    .accessibilityIdentifier("task.cost.open")
            }
            if model.accepted != nil { Text(verbatim: model.message).font(.footnote).foregroundStyle(Theme.ink2) }
        }
        .task(id: scope) {
            guard let client = app.client else { model.invalidate(); return }
            do {
                model.configure(taskID: run.aufgabe, coreID: client.pairedCoreInstanceID, deviceID: client.deviceIdentifier,
                    retry: try TaskCostApprovalRetryCache.load(client: client, taskID: run.aufgabe))
            } catch { model.blockRetry() }
        }
        .sheet(isPresented: $showing) { sheet }
        .onDisappear { model.invalidate() }
    }
    private var sheet: some View {
        TaskCostApprovalSheet(run: run, model: model, allowed: allowed,
            onConfirm: { Task { await send() } }, onClose: { showing = false })
    }
    private func send() async {
        guard allowed, let client = app.client, let keyID = app.taskStartKeyID else { return }
        let captured = scope
        await model.send(run: run, coreID: client.pairedCoreInstanceID, deviceID: client.deviceIdentifier, keyID: keyID,
            loadRetry: { try TaskCostApprovalRetryCache.load(client: client, taskID: run.aufgabe) },
            saveRetry: { try TaskCostApprovalRetryCache.save($0, client: client) },
            clearRetry: { TaskCostApprovalRetryCache.clear(client: client, expected: $0) },
            challenge: { try await client.costApprovalChallenge($0) },
            sign: { try await AppAttestManager.assert(keyId: keyID, clientDataHash: $0) },
            submit: { try await client.submitCostApproval($0, proof: $1) },
            isCurrent: { app.client === client && scope == captured && showing && scenePhase == .active })
        if app.client === client, scope == captured { await refresh() }
    }
}

/// The same presentation can be rendered offline; authorization stays at the caller.
struct TaskCostApprovalSheet: View {
    let run: AgentRun
    @ObservedObject var model: TaskCostApprovalModel
    let allowed: Bool
    let onConfirm: () -> Void
    let onClose: () -> Void
    @FocusState private var amountFocused: Bool
    private var costs: AgentRun.Costs? { TaskCostApprovalModel.scopedCosts(run) }
    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    Text(verbatim: run.auftrag).font(.headline).textSelection(.enabled)
                    Text("Die Gesamtobergrenze umfasst alle bisherigen und künftigen zusätzlichen KI- und Werkzeugkosten dieses Auftrags. Sie ist kein zusätzliches Guthaben und kein Kaufbudget.")
                    costLine("Bereits verbraucht", costs?.ai_tool?.spent_cents)
                    costLine("Noch reserviert", costs?.ai_tool?.reserved_cents)
                    costLine("Bisher freigegeben", costs?.approved_ai_cap_cents,
                        empty: costs == nil ? "Unbekannt" : "Noch kein freigegebener Gesamtdeckel")
                    if !model.uncertain, TaskCostApprovalModel.minimumNewCap(run) == nil {
                        Text("Die bisherigen Kosten sind nicht vollständig bestätigt. Lies den aktuellen Stand erneut, bevor du einen neuen Rahmen freigibst.").foregroundStyle(Theme.warn)
                    }
                    if (costs?.counts?.unknown ?? 0) > 0 {
                        Text("Eine Kostenbuchung ist ungewiss. Ihre Reserve bleibt bestehen.").foregroundStyle(Theme.warn)
                    }
                    Text("Neuer Gesamtrahmen in Euro").font(.subheadline.weight(.semibold))
                    TextField("Zum Beispiel 20,00", text: $model.amount)
                        .keyboardType(.decimalPad).textFieldStyle(.roundedBorder).disabled(model.amountLocked)
                        .focused($amountFocused)
                        .accessibilityIdentifier("task.cost.amount")
                    if let cents = TaskCostAmount.cents(model.amount) {
                        Text("Gesamtobergrenze: \(TaskCostAmount.text(cents)) €").font(.headline)
                    }
                    if !model.message.isEmpty { Text(verbatim: model.message).font(.footnote) }
                    Button(model.sending ? "Wird bestätigt …" : model.uncertain ? "Denselben Betrag erneut bestätigen" : "Diesen Gesamtrahmen freigeben") {
                        onConfirm()
                    }.buttonStyle(.borderedProminent)
                        .disabled(!allowed || model.sending || !model.valid(run: run))
                        .accessibilityIdentifier("task.cost.confirm")
                    Text("Eine Bestätigung startet den Auftrag nicht automatisch. Ohne Freigabe bleibt er unverändert warten.")
                        .font(.footnote).foregroundStyle(Theme.ink2)
                    Button(model.accepted != nil ? "Schließen" : model.uncertain ? "Später prüfen" : "Nicht freigeben", role: .cancel) { onClose() }
                        .disabled(model.sending).accessibilityIdentifier("task.cost.close")
                }.padding(Theme.Space.margin)
            }.background(Theme.bg).navigationTitle("Kostenrahmen").navigationBarTitleDisplayMode(.inline)
                .toolbar { ToolbarItemGroup(placement: .keyboard) {
                    Spacer(); Button("Fertig") { amountFocused = false }
                } }
        }.interactiveDismissDisabled(model.sending)
    }
    private func costLine(_ label: String, _ cents: Int?, empty: String = "Unbekannt") -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label).font(.caption).foregroundStyle(Theme.ink2)
            Text(cents.map { TaskCostAmount.text($0) + " €" } ?? empty)
        }
    }
}
