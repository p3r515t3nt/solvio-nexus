import Foundation
import SwiftUI
import SolvioApprovalsKit

extension ApprovalClient {
    func followupChallenge(_ followup: AppTaskFollowupBody) async throws -> AppTaskFollowupChallenge {
        struct Body: Encodable { let followup: AppTaskFollowupBody }
        var request = controlRequest("v1/agent/runs/\(followup.run_id)/followup/challenge", "POST")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(Body(followup: followup))
        let (data, response) = try await controlSession.data(for: request, delegate: ResultRedirectGuard())
        guard let http = response as? HTTPURLResponse, response.url == request.url else { throw ClientError.decode }
        guard http.statusCode == 200 else { throw ClientError.http(http.statusCode) }
        return try JSONDecoder().decode(AppTaskFollowupChallenge.self, from: data)
    }
    func submitFollowup(_ followup: AppTaskFollowupBody, proof: AppTaskProof) async throws -> AppTaskFollowupAccepted {
        struct Body: Encodable { let followup: AppTaskFollowupBody; let proof: AppTaskProof }
        var request = controlRequest("v1/agent/runs/\(followup.run_id)/followup", "POST")
        request.setValue(nil, forHTTPHeaderField: "X-Transport-Cred")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(Body(followup: followup, proof: proof))
        let (data, response) = try await controlSession.data(for: request, delegate: ResultRedirectGuard())
        guard let http = response as? HTTPURLResponse, response.url == request.url else { throw ClientError.decode }
        guard http.statusCode == 202 else { throw ClientError.http(http.statusCode) }
        return try JSONDecoder().decode(AppTaskFollowupAccepted.self, from: data)
    }
}

@MainActor
enum TaskFollowupRetryCache {
    static func tag(client: ApprovalClient, runID: String) -> String {
        "de.solvio.approvals.taskFollowupRetry." + ApprovalCrypto.sha256Hex(
            Data((client.pairedCoreInstanceID + "\0" + client.deviceIdentifier + "\0" + runID).utf8))
    }
    static func load(client: ApprovalClient, runID: String) -> TaskFollowupRetryBinding? {
        guard let data = Keychain.load(tag: tag(client: client, runID: runID)),
              let retry = try? JSONDecoder().decode(TaskFollowupRetryBinding.self, from: data),
              retry.coreID == client.pairedCoreInstanceID, retry.deviceID == client.deviceIdentifier,
              retry.runID == runID else { return nil }
        return retry
    }
    static func save(_ body: AppTaskFollowupBody, client: ApprovalClient) throws {
        let key = tag(client: client, runID: body.run_id)
        let retry = TaskFollowupRetryBinding(body: body, coreID: client.pairedCoreInstanceID, deviceID: client.deviceIdentifier)
        if let prior = load(client: client, runID: body.run_id), prior != retry { throw ClientError.badServer }
        let encoder = JSONEncoder(); encoder.outputFormatting = [.sortedKeys]
        let data = try encoder.encode(retry)
        if Keychain.load(tag: key) != data { Keychain.save(data, tag: key) }
        guard Keychain.load(tag: key) == data else { throw ClientError.decode }
    }
    static func clear(client: ApprovalClient, expected body: AppTaskFollowupBody) {
        guard let retry = load(client: client, runID: body.run_id),
              retry.requestID == body.client_request_id, retry.bodyDigest == body.requestDigest else { return }
        Keychain.delete(tag: tag(client: client, runID: body.run_id))
    }
}

@MainActor
final class TaskFollowupModel: ObservableObject {
    @Published var text = ""
    @Published private(set) var selectedIDs: [String] = []
    @Published private(set) var sending = false
    @Published private(set) var uncertain = false
    @Published private(set) var accepted: AppTaskFollowupAccepted?
    @Published private(set) var message = ""
    private var binding = ""
    private var generation = UUID()
    private var pending: AppTaskFollowupBody?
    private var retryBinding: TaskFollowupRetryBinding?
    private var coreID = "", deviceID = ""
    private let attempt = AppTaskFollowupAttempt()

    static func inputFiles(_ run: AgentRun) -> [ResultFile] {
        (run.dateien ?? []).filter { file in
            let ext = (file.name as NSString).pathExtension.lowercased()
            return ["csv", "xlsx"].contains(ext) && file.size > 0 && file.size <= AppTaskFileInput.byteLimit
                && (try? file.downloadPath(runID: run.id)) != nil
        }
    }
    func configure(run: AgentRun, coreID: String, deviceID: String, retry: TaskFollowupRetryBinding?) {
        let next = coreID + ":" + deviceID + ":" + run.id + ":" + (run.task_revision?.bindingID ?? "")
        guard next != binding else { return }
        generation = UUID(); binding = next; text = ""; pending = nil; accepted = nil; sending = false
        self.coreID = coreID; self.deviceID = deviceID; retryBinding = retry
        uncertain = retry != nil
        let offered = Self.inputFiles(run)
        selectedIDs = retry?.inputIDs ?? (offered.count == 1 ? [offered[0].id] : [])
        message = uncertain ? "Eine frühere Folgeanweisung ist noch ungeklärt. Gib denselben Text erneut ein. Die Dateiauswahl bleibt gebunden." : ""
    }
    func invalidate() { generation = UUID(); binding = ""; sending = false; accepted = nil; pending = nil; text = ""; selectedIDs = []; uncertain = false; message = ""; retryBinding = nil; coreID = ""; deviceID = "" }
    func toggle(_ id: String, run: AgentRun) {
        guard !sending, !uncertain, accepted == nil, Self.inputFiles(run).contains(where: { $0.id == id }) else { return }
        if selectedIDs.contains(id) { selectedIDs.removeAll { $0 == id } }
        else if selectedIDs.count < 4 { selectedIDs.append(id) }
        pending = nil
    }
    private func body(run: AgentRun, requestID: String, replay: TaskFollowupRetryBinding?) throws -> AppTaskFollowupBody {
        guard let revision = run.task_revision else { throw TaskStartError.invalidBody }
        let request = try AppTaskFollowupBody(runID: run.id, text: text.trimmingCharacters(in: .whitespacesAndNewlines),
            revision: revision.revision, digest: revision.digest, inputIDs: selectedIDs, requestID: requestID)
        // Only an exact, already saved request may replay without the old
        // file catalogue. The Core can return its durable acceptance even if
        // the original file is now missing. No changed/new request gets this.
        if let replay, replay.restore(runID: run.id, text: request.text, revision: revision.revision,
            digest: revision.digest, inputIDs: selectedIDs, coreID: coreID, deviceID: deviceID) == request {
            return request
        }
        let files = Self.inputFiles(run)
        guard selectedIDs.allSatisfy({ id in files.contains { $0.id == id } }),
              files.filter({ selectedIDs.contains($0.id) }).reduce(0, { $0 + $1.size }) <= AppTaskFileInput.byteLimit else {
            throw TaskFileError.invalidSize
        }
        return request
    }
    func valid(run: AgentRun) -> Bool {
        (try? body(run: run, requestID: retryBinding?.requestID ?? pending?.client_request_id ?? "validation-only",
            replay: retryBinding)) != nil
    }

    func send(run: AgentRun, coreID: String, deviceID: String, keyID: String,
        loadRetry: () -> TaskFollowupRetryBinding?, saveRetry: (AppTaskFollowupBody) throws -> Void,
        clearRetry: (AppTaskFollowupBody) -> Void,
        challenge: (AppTaskFollowupBody) async throws -> AppTaskFollowupChallenge,
        sign: (Data) async throws -> Data,
        submit: (AppTaskFollowupBody, AppTaskProof) async throws -> AppTaskFollowupAccepted,
        isCurrent: () -> Bool) async {
        guard !sending, accepted == nil, isCurrent(), self.coreID == coreID, self.deviceID == deviceID,
              run.followup?.eligible == true || uncertain else { return }
        let prior = loadRetry()
        let requestID = prior?.requestID ?? pending?.client_request_id ?? retryBinding?.requestID ?? UUID().uuidString.lowercased()
        guard (try? body(run: run, requestID: requestID, replay: prior ?? retryBinding)) != nil else { return }
        let current = generation
        sending = true; message = ""
        defer { if generation == current { sending = false } }
        let wasUncertain = uncertain || prior != nil
        var prepared: AppTaskFollowupBody?
        do {
            let request = try body(run: run, requestID: requestID, replay: prior ?? retryBinding)
            if let prior, prior.restore(runID: run.id, text: request.text, revision: request.expected_revision,
                digest: request.expected_digest, inputIDs: request.input_artifact_ids, coreID: coreID, deviceID: deviceID) == nil {
                uncertain = true; message = "Die frühere Annahme ist ungeklärt. Text und Dateiauswahl müssen gleich bleiben."; return
            }
            if wasUncertain, let pending, pending != request {
                uncertain = true; message = "Die frühere Annahme ist ungeklärt. Verwende denselben Text erneut."; return
            }
            if wasUncertain && prior == nil && pending == nil {
                message = "Die Wiederholung ist nicht mehr eindeutig gebunden. Lies den Auftragsverlauf erneut."; return
            }
            prepared = request; pending = request
            try saveRetry(request)
            retryBinding = .init(body: request, coreID: coreID, deviceID: deviceID)
            let response = try await attempt.send(body: request, taskID: run.aufgabe, coreID: coreID,
                deviceID: deviceID, appAttestKeyID: keyID, challenge: challenge, sign: sign, submit: submit,
                isCurrent: { generation == current && isCurrent() })
            clearRetry(request)
            guard generation == current, isCurrent(), !Task.isCancelled else { return }
            accepted = response; uncertain = false; retryBinding = nil
            message = response.annahme == "preparing" ? "Folgeanweisung erfasst. SOLVIO bereitet den nächsten Schritt vor." : "Folgeanweisung angenommen. SOLVIO arbeitet am selben Auftrag weiter."
        } catch ClientError.http(let status) where !wasUncertain && [400, 403, 404, 409].contains(status) {
            if let prepared { clearRetry(prepared) }
            guard generation == current, isCurrent(), !Task.isCancelled else { return }
            pending = nil; uncertain = false; retryBinding = nil
            message = "Die Folgeanweisung wurde nicht übernommen. Lies den aktuellen Stand erneut."
        } catch {
            guard generation == current, isCurrent(), !Task.isCancelled else { return }
            uncertain = true
            message = "Noch keine Annahme bestätigt. Erneut versuchen verwendet genau diese Folgeanweisung."
        }
    }
}

struct TaskFollowupView: View {
    @ObservedObject var app: AppModel
    let run: AgentRun
    let available: Bool
    let onAccepted: (AppTaskFollowupAccepted) -> Void
    var onProtectionChange: (Bool) -> Void = { _ in }
    @StateObject private var model = TaskFollowupModel()
    private var scope: String { run.id + (run.task_revision?.bindingID ?? "") + (app.client.map { String(describing: ObjectIdentifier($0)) } ?? "") }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if run.followup?.eligible == true || model.uncertain {
                Text("Was soll ich ergänzen?").font(.headline)
                TextField("Ergänzung", text: $model.text, axis: .vertical)
                    .textFieldStyle(.roundedBorder).lineLimit(2...6)
                    .disabled(model.sending || model.accepted != nil)
                    .accessibilityIdentifier("task.followup.text")
                let files = TaskFollowupModel.inputFiles(run)
                if !files.isEmpty {
                    DisclosureGroup {
                        ForEach(files) { file in
                            Toggle(isOn: Binding(get: { model.selectedIDs.contains(file.id) },
                                set: { _ in model.toggle(file.id, run: run) })) {
                                Text(verbatim: file.name).font(.callout)
                            }.disabled(model.sending || model.uncertain || model.accepted != nil)
                        }
                    } label: {
                        Text("Dateien verwenden").font(.callout).multilineTextAlignment(.leading)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if !model.selectedIDs.isEmpty {
                        Text(files.filter { model.selectedIDs.contains($0.id) }.map(\.name).joined(separator: " · "))
                            .font(.caption).foregroundStyle(Theme.ink2)
                    }
                }
                if !model.message.isEmpty { Text(verbatim: model.message).font(.footnote).foregroundStyle(Theme.ink2) }
                Button { Task { await send() } } label: {
                    Text(model.sending ? "Wird gesendet …" : model.uncertain ? "Erneut versuchen" : "Senden")
                        .frame(maxWidth: .infinity).fixedSize(horizontal: false, vertical: true)
                }
                    .buttonStyle(.borderedProminent)
                    .disabled(!available || model.sending || model.accepted != nil || !model.valid(run: run)
                        || app.client == nil || app.taskStartKeyID == nil || !app.attested || !AppAttestManager.isSupported)
                    .accessibilityIdentifier("task.followup.send")
                Text("Bleibt im selben Auftrag. Die bisherigen Ergebnisse bleiben erhalten.")
                    .font(.caption).foregroundStyle(Theme.ink2)
            }
        }
        .task(id: scope) {
            guard let client = app.client else { model.invalidate(); onProtectionChange(false); return }
            model.configure(run: run, coreID: client.pairedCoreInstanceID, deviceID: client.deviceIdentifier,
                retry: TaskFollowupRetryCache.load(client: client, runID: run.id))
            reportProtection()
        }
        .onChange(of: model.sending) { _ in reportProtection() }
        .onChange(of: model.uncertain) { _ in reportProtection() }
        .onChange(of: model.accepted) { _ in reportProtection() }
        .onDisappear { model.invalidate(); onProtectionChange(false) }
    }
    private func reportProtection() { onProtectionChange(model.sending || model.uncertain) }
    private func send() async {
        guard let client = app.client, let keyID = app.taskStartKeyID else { return }
        let captured = scope
        await model.send(run: run, coreID: client.pairedCoreInstanceID, deviceID: client.deviceIdentifier, keyID: keyID,
            loadRetry: { TaskFollowupRetryCache.load(client: client, runID: run.id) },
            saveRetry: { try TaskFollowupRetryCache.save($0, client: client) },
            clearRetry: { TaskFollowupRetryCache.clear(client: client, expected: $0) },
            challenge: { try await client.followupChallenge($0) },
            sign: { try await AppAttestManager.assert(keyId: keyID, clientDataHash: $0) },
            submit: { try await client.submitFollowup($0, proof: $1) },
            isCurrent: { app.client === client && scope == captured })
        if let accepted = model.accepted, app.client === client, scope == captured { onAccepted(accepted) }
    }
}
