import Foundation
import SwiftUI
import SolvioApprovalsKit

extension ApprovalClient {
    func actionAnswerChallenge(_ answer: AppTaskActionAnswerBody) async throws -> AppTaskActionAnswerChallenge {
        guard AgentRun.validRunID(answer.run_id) else { throw ClientError.decode }
        struct Body: Encodable { let answer: AppTaskActionAnswerBody }
        var request = controlRequest("v1/agent/runs/\(answer.run_id)/action-answer/challenge", "POST")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(Body(answer: answer))
        let (data, response) = try await controlSession.data(for: request, delegate: ResultRedirectGuard())
        guard let http = response as? HTTPURLResponse, response.url == request.url else { throw ClientError.decode }
        guard http.statusCode == 200 else { throw ClientError.http(http.statusCode) }
        return try JSONDecoder().decode(AppTaskActionAnswerChallenge.self, from: data)
    }

    func submitActionAnswer(_ answer: AppTaskActionAnswerBody,
                            proof: AppTaskActionAnswerProof) async throws -> AppTaskActionAnswerAccepted {
        guard AgentRun.validRunID(answer.run_id) else { throw ClientError.decode }
        struct Body: Encodable { let answer: AppTaskActionAnswerBody; let proof: AppTaskActionAnswerProof }
        var request = controlRequest("v1/agent/runs/\(answer.run_id)/action-answer", "POST")
        request.setValue(nil, forHTTPHeaderField: "X-Transport-Cred")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(Body(answer: answer, proof: proof))
        let (data, response) = try await controlSession.data(for: request, delegate: ResultRedirectGuard())
        guard let http = response as? HTTPURLResponse, response.url == request.url else { throw ClientError.decode }
        guard http.statusCode == 200 else { throw ClientError.http(http.statusCode) }
        return try JSONDecoder().decode(AppTaskActionAnswerAccepted.self, from: data)
    }
}

@MainActor
enum ActionAnswerRetryCache {
    struct Stored: Codable {
        let question: String
        let revision: Int
        let retry: TaskActionAnswerRetryBinding
    }
    static func tag(client: ApprovalClient, runID: String) -> String {
        let scope = client.pairedCoreInstanceID + "\0" + client.deviceIdentifier + "\0" + runID
        return "de.solvio.approvals.actionAnswerRetry." + ApprovalCrypto.sha256Hex(Data(scope.utf8))
    }
    static func load(client: ApprovalClient, runID: String, question: ActionIntentQuestion) -> TaskActionAnswerRetryBinding? {
        let key = tag(client: client, runID: runID)
        guard let data = Keychain.load(tag: key), let saved = try? JSONDecoder().decode(Stored.self, from: data) else { return nil }
        guard saved.question == question.bindingID else { return nil }
        return saved.retry
    }
    static func save(_ body: AppTaskActionAnswerBody, client: ApprovalClient, question: ActionIntentQuestion) throws {
        let saved = Stored(question: question.bindingID, revision: question.revision,
            retry: TaskActionAnswerRetryBinding(body: body, coreID: client.pairedCoreInstanceID, deviceID: client.deviceIdentifier))
        let encoder = JSONEncoder(); encoder.outputFormatting = [.sortedKeys]
        let data = try encoder.encode(saved), key = tag(client: client, runID: body.run_id)
        if let current = Keychain.load(tag: key), let previous = try? JSONDecoder().decode(Stored.self, from: current) {
            // A delayed old view must never replace a newer question's retry.
            guard (previous.question == saved.question && previous.retry == saved.retry)
                || previous.revision < saved.revision else { throw ClientError.badServer }
        }
        if Keychain.load(tag: key) != data { Keychain.save(data, tag: key) }
        guard Keychain.load(tag: key) == data else { throw ClientError.decode }
    }
    static func clear(client: ApprovalClient, expected body: AppTaskActionAnswerBody) {
        let key = tag(client: client, runID: body.run_id)
        guard let data = Keychain.load(tag: key), let saved = try? JSONDecoder().decode(Stored.self, from: data),
              saved.retry.requestID == body.client_request_id, saved.retry.bodyDigest == body.requestDigest else { return }
        Keychain.delete(tag: key)
    }
}

@MainActor
final class ActionQuestionModel: ObservableObject {
    @Published var answer = ""
    @Published private(set) var sending = false
    @Published private(set) var accepted = false
    @Published private(set) var uncertain = false
    @Published private(set) var message = ""
    private var draft = TaskActionAnswerDraft()
    private let attempt = AppTaskActionAnswerAttempt()

    func valid(runID: String, question: ActionIntentQuestion) -> Bool {
        (try? AppTaskActionAnswerBody(runID: runID, question: question,
            answer: answer.trimmingCharacters(in: .whitespacesAndNewlines), requestID: "validation-only")) != nil
    }

    func send(client: ApprovalClient, keyID: String, runID: String, question: ActionIntentQuestion) async {
        guard !sending, !accepted, valid(runID: runID, question: question) else { return }
        sending = true; message = ""
        defer { sending = false }
        var priorUncertain = uncertain
        var prepared: AppTaskActionAnswerBody?
        do {
            let text = answer.trimmingCharacters(in: .whitespacesAndNewlines)
            let retry = ActionAnswerRetryCache.load(client: client, runID: runID, question: question)
            priorUncertain = priorUncertain || retry != nil
            if let retry, retry.restore(runID: runID, question: question, answer: text,
                coreID: client.pairedCoreInstanceID, deviceID: client.deviceIdentifier) == nil {
                uncertain = true
                message = "Die vorherige Antwort ist noch ungeklärt. Verwende dieselbe Antwort, um sie zu prüfen."
                return
            }
            let body = try draft.prepare(runID: runID, question: question, answer: text, retry: retry,
                coreID: client.pairedCoreInstanceID, deviceID: client.deviceIdentifier)
            prepared = body
            try ActionAnswerRetryCache.save(body, client: client, question: question)
            _ = try await attempt.send(body: body, coreID: client.pairedCoreInstanceID,
                deviceID: client.deviceIdentifier, appAttestKeyID: keyID,
                challenge: { try await client.actionAnswerChallenge($0) },
                sign: { try await AppAttestManager.assert(keyId: keyID, clientDataHash: $0) },
                submit: { try await client.submitActionAnswer($0, proof: $1) })
            ActionAnswerRetryCache.clear(client: client, expected: body)
            accepted = true; uncertain = false
            message = "Antwort übernommen. SOLVIO arbeitet am selben Auftrag weiter."
        } catch ClientError.http(let status) where !priorUncertain && [400, 403, 404, 409].contains(status) {
            if let prepared { ActionAnswerRetryCache.clear(client: client, expected: prepared) }
            uncertain = false
            message = "Die Antwort wurde nicht übernommen. Lies den aktuellen Stand und prüfe deine Angaben."
        } catch {
            uncertain = true
            message = "Noch keine Antwort bestätigt. Erneut versuchen prüft dieselbe Antwort."
        }
    }
}

struct ActionQuestionView: View {
    @ObservedObject var app: AppModel
    let runID: String
    let question: ActionIntentQuestion
    let available: Bool
    let refresh: () async -> Void
    let refreshAccounts: () async -> Void
    var onProtectionChange: (Bool) -> Void = { _ in }
    @StateObject private var model = ActionQuestionModel()

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Noch eine Angabe").font(.headline)
            Text(verbatim: question.prompt)
            if question.input_type == .select {
                if let options = question.options, !options.isEmpty {
                    Picker("Deine Auswahl", selection: $model.answer) {
                        Text("Bitte wählen").tag("")
                        ForEach(options) { option in Text(verbatim: option.label).tag(option.value) }
                    }.pickerStyle(.menu)
                }
            } else {
                TextField(question.placeholder ?? "Deine Antwort", text: $model.answer, axis: .vertical)
                    .textFieldStyle(.roundedBorder).lineLimit(1...6)
                    .keyboardType(question.input_type == .email ? .emailAddress : question.input_type == .number ? .numberPad : .default)
                    .textInputAutocapitalization(question.input_type == .email ? .never : .sentences)
                    .autocorrectionDisabled(question.input_type == .email)
            }
            if question.field == .account {
                Button("Verbundene Konten neu prüfen") { Task { await refreshAccounts() } }
                    .disabled(!available || model.sending || model.accepted || app.client == nil)
            }
            if !model.message.isEmpty { Text(verbatim: model.message).font(.footnote).foregroundStyle(Theme.ink2) }
            if question.input_type != .select || !(question.options ?? []).isEmpty {
                Button(model.sending ? "Wird gesendet …" : model.uncertain ? "Erneut versuchen" : "Antworten") {
                    guard let client = app.client, let keyID = app.taskStartKeyID else { return }
                    Task { await model.send(client: client, keyID: keyID, runID: runID, question: question); await refresh() }
                }.buttonStyle(.borderedProminent)
                    .disabled(!available || model.sending || model.accepted || !model.valid(runID: runID, question: question)
                        || app.client == nil || app.taskStartKeyID == nil || !app.attested || !AppAttestManager.isSupported)
            }
        }.disabled(model.sending || model.accepted)
            .padding(Theme.Space.card).frame(maxWidth: .infinity, alignment: .leading).card()
            .onAppear { reportProtection() }
            .onChange(of: model.sending) { _ in reportProtection() }
            .onChange(of: model.uncertain) { _ in reportProtection() }
            .onChange(of: model.accepted) { _ in reportProtection() }
    }

    private func reportProtection() {
        let retry = app.client.flatMap { ActionAnswerRetryCache.load(client: $0, runID: runID, question: question) }
        onProtectionChange(model.sending || model.uncertain || (!model.accepted && retry != nil))
    }
}
