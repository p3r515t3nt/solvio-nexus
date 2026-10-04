// Ein Textfeld, ein Anhang, Senden. Jede Nachricht traegt einen frischen
// App-Attest-Beweis ueber genau ihren Body; die Wiederholung nach einem
// Netzfehler benutzt dieselbe `client_message_id` (der Core antwortet dann mit
// derselben Zustellung). Im Schluesselbund liegen nur Kennungen und Digests —
// nie der Text, nie ein Anhang.
import Foundation
import SwiftUI
import UniformTypeIdentifiers
import SolvioApprovalsKit

/// Analog zu `TaskRetryCache`: ein Fach je Core und Geraet, gespeichert VOR dem
/// ersten `await`, geleert erst, wenn der Verlauf die Zustellung zeigt.
enum ConversationRetryCache {
    static func tag(coreID: String, deviceID: String) -> String {
        "de.solvio.approvals.messageRetry." + ApprovalCrypto.sha256Hex(Data((coreID + "\0" + deviceID).utf8))
    }
    static func load(_ client: ApprovalClient) -> ConversationMessageRetryBinding? {
        guard let data = Keychain.load(tag: tag(coreID: client.pairedCoreInstanceID, deviceID: client.deviceIdentifier)),
              let binding = try? JSONDecoder().decode(ConversationMessageRetryBinding.self, from: data),
              binding.coreID == client.pairedCoreInstanceID, binding.deviceID == client.deviceIdentifier else { return nil }
        return binding
    }
    static func save(_ binding: ConversationMessageRetryBinding, client: ApprovalClient) throws {
        let encoder = JSONEncoder(); encoder.outputFormatting = [.sortedKeys]
        let data = try encoder.encode(binding), key = tag(coreID: client.pairedCoreInstanceID, deviceID: client.deviceIdentifier)
        if Keychain.load(tag: key) != data { Keychain.save(data, tag: key) }
        guard Keychain.load(tag: key) == data else {
            throw NSError(domain: "ConversationRetryCache", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "Die Wiederholung konnte nicht abgesichert werden. Es wurde keine Nachricht gesendet."])
        }
    }
    static func clear(_ client: ApprovalClient) {
        Keychain.delete(tag: tag(coreID: client.pairedCoreInstanceID, deviceID: client.deviceIdentifier))
    }
}

@MainActor
final class MessageComposerModel: ObservableObject {
    @Published var text = "" { didSet { if oldValue != text { edited() } } }
    @Published private(set) var sending = false
    @Published private(set) var message = ""
    @Published private(set) var needsRetry = false
    @Published private(set) var lastAccepted: AppConversationMessageAccepted?
    /// Eine Marke ohne Text aus einem frueheren Prozess: erst klaeren, dann Neues.
    @Published private(set) var unconfirmed: ConversationMessageRetryBinding?
    /// Die vorhandene Datei-/Dokumentauswahl — eine eigene Instanz, damit ein
    /// Auftrag aus der Auftragsliste den Anhang hier nie blockiert.
    let attachments = TaskStartModel()
    private let attempt = ConversationMessageAttempt()
    /// Der zuletzt in DIESEM Prozess gesendete Body: eine eigene, noch offene
    /// Marke ist kein „frueherer Prozess" und bekommt keinen Neustart-Hinweis.
    private var draft: AppConversationMessageBody?
    private var revision = UUID()

    var trimmedText: String { text.trimmingCharacters(in: .whitespacesAndNewlines) }
    var valid: Bool {
        guard !attachments.documentLoading, attachments.documentError.isEmpty else { return false }
        return AppConversationMessageBody.validText(trimmedText)
    }
    var characterCount: Int { trimmedText.unicodeScalars.count }

    /// Preparing an idea is local only. Never replace a draft, file or uncertain send.
    func stageSuggestion(_ prompt: String) -> Bool {
        guard !sending, trimmedText.isEmpty, !needsRetry, unconfirmed == nil, draft == nil,
              attachments.document == nil, attachments.files == nil,
              !attachments.documentLoading, attachments.documentError.isEmpty else { return false }
        text = prompt
        return true
    }

    private func edited() {
        revision = UUID()
        if lastAccepted != nil { lastAccepted = nil }
        if needsRetry { needsRetry = false; message = "" }
    }

    func restoreRetryHint(load: () -> ConversationMessageRetryBinding?) {
        guard draft == nil, let retry = load(), retry.deliveryID == nil else { return }
        unconfirmed = retry
        message = "Eine frühere Nachricht ist noch ungeklärt. Gib denselben Text erneut ein, um sie zu prüfen"
            + (retry.attachmentKind != nil ? " — mit demselben Anhang" : "") + ". Es wird nichts automatisch gesendet."
    }

    func discardUnconfirmed(clear: () -> Void) {
        guard !sending else { return }
        clear(); unconfirmed = nil; message = ""
    }

    func selectFiles(_ urls: [URL]) async { await attachments.selectFiles(urls) }
    func clearAttachment() { attachments.clearDocument() }

    /// Eine im Verlauf gefundene Zustellung bestaetigt auch den lokalen Composer.
    /// Der Cache kann waehrend des Lesens wechseln; laufende Sends behalten ihre Marke.
    func refreshDetail(chats: ConversationModel, coreID: String, deviceID: String,
                       loadRetry: () -> ConversationMessageRetryBinding?, clearRetry: () -> Void,
                       load: (String) async throws -> ConversationDetail) async {
        let pending = loadRetry(), loadedRevision = revision
        await chats.refreshDetail(load: load, pending: pending, clearPending: {
            guard let pending, pending.coreID == coreID, pending.deviceID == deviceID,
                  !self.sending, loadRetry() == pending else { return }
            let ownsDraft = self.draft.map {
                $0.conversation_id == pending.conversationID && $0.client_message_id == pending.clientMessageID
                    && $0.requestDigest == pending.bodyDigest
            } ?? false
            let ownsHint = self.unconfirmed == pending
            clearRetry()
            guard ownsDraft || ownsHint else { return }
            if ownsDraft { self.draft = nil }
            if ownsHint { self.unconfirmed = nil }
            self.needsRetry = false; self.message = ""
            // Eine spaete Bestaetigung darf keinen inzwischen geaenderten Entwurf leeren.
            guard self.revision == loadedRevision, !self.attachments.documentLoading,
                  pending.restore(conversationID: pending.conversationID, text: self.trimmedText,
                                  attachments: self.attachments.composerAttachment,
                                  coreID: coreID, deviceID: deviceID) != nil else { return }
            self.text = ""
            self.attachments.clearDocument()
        })
    }

    /// Sendet in genau diesen Chat. `saveRetry` laeuft VOR dem ersten `await`.
    @discardableResult
    func send(conversationID: String, coreID: String, deviceID: String, keyID: String,
              loadRetry: () -> ConversationMessageRetryBinding?,
              saveRetry: (ConversationMessageRetryBinding) throws -> Void,
              clearRetry: () -> Void,
              challenge: (AppConversationMessageBody) async throws -> AppConversationMessageChallenge,
              sign: (Data) async throws -> Data,
              submit: (AppConversationMessageBody, AppTaskProof) async throws -> AppConversationMessageAccepted,
              now: () -> Double = { Date().timeIntervalSince1970 },
              beforeSend: () async throws -> Void = {}) async -> AppConversationMessageAccepted? {
        guard !sending, valid, ConversationSummary.validID(conversationID) else { return nil }
        sending = true; message = ""
        let submittedRevision = revision
        defer { sending = false }
        do { try await beforeSend() }
        catch { message = error.localizedDescription; return nil }
        guard revision == submittedRevision, !Task.isCancelled else { return nil }
        let text = trimmedText, attachment = attachments.composerAttachment
        let body: AppConversationMessageBody
        do {
            if let pending = unconfirmed ?? loadRetry().flatMap({ $0.deliveryID == nil ? $0 : nil }) {
                // Ungeklaert heisst ungeklaert: nur dieselbe Nachricht darf hinaus.
                guard let restored = pending.restore(conversationID: conversationID, text: text, attachments: attachment,
                                                     coreID: coreID, deviceID: deviceID) else {
                    unconfirmed = pending
                    message = "Die vorherige Nachricht ist ungeklärt. Zum Prüfen müssen Chat, Text und Anhang gleich bleiben — oder verwirf den Hinweis ausdrücklich."
                    return nil
                }
                body = restored
            } else {
                // Die Wiederholung nach einem Netzfehler laeuft ueber die Marke
                // (oben): dieselbe Nachricht bekommt dieselbe Kennung zurueck.
                body = try AppConversationMessageBody(conversationID: conversationID, clientMessageID: UUID().uuidString.lowercased(),
                                                      text: text, attachments: attachment)
            }
            draft = body
            try saveRetry(ConversationMessageRetryBinding(body: body, coreID: coreID, deviceID: deviceID))
        } catch {
            message = error.localizedDescription
            return nil
        }
        do {
            let accepted = try await attempt.send(body: body, coreID: coreID, deviceID: deviceID, appAttestKeyID: keyID,
                                                  challenge: challenge, sign: sign, submit: submit, now: now)
            // Angenommen: die Marke traegt jetzt die Zustellkennung; der Verlauf raeumt sie ab.
            try? saveRetry(ConversationMessageRetryBinding(body: body, coreID: coreID, deviceID: deviceID, deliveryID: accepted.delivery_id))
            guard revision == submittedRevision else { return accepted }
            draft = nil; unconfirmed = nil
            self.text = ""                      // loest edited() aus — danach der Endzustand
            attachments.clearDocument()
            needsRetry = false; message = ""
            lastAccepted = accepted
            return accepted
        } catch {
            guard revision == submittedRevision else { return nil }
            needsRetry = true
            if case ClientError.http(409) = error {
                message = "Der Core kennt diese Nachrichtenkennung mit anderem Inhalt. Bitte den Hinweis verwerfen und neu schreiben."
                unconfirmed = ConversationMessageRetryBinding(body: body, coreID: coreID, deviceID: deviceID)
            } else if case ClientError.http(404) = error {
                message = "Dieser Chat ist nicht mehr vorhanden. Die Nachricht wurde nicht zugestellt."
                clearRetry(); draft = nil
            } else {
                message = "Keine Zustellung bestätigt. Erneut versuchen verwendet dieselbe Nachricht. \(error.localizedDescription)"
            }
            return nil
        }
    }
}

struct MessageComposerView: View {
    @ObservedObject var app: AppModel
    @ObservedObject var chats: ConversationModel
    @ObservedObject var model: MessageComposerModel
    @ObservedObject private var attachments: TaskStartModel
    @Environment(\.dynamicTypeSize) private var typeSize
    @State private var showPicker = false
    @FocusState private var focused: Bool
    var voiceBusy: Bool
    var showsMicrophone: Bool
    let onMicrophone: () -> Void
    let beforeSend: () async throws -> Void

    init(app: AppModel, chats: ConversationModel, model: MessageComposerModel,
         voiceBusy: Bool = false, showsMicrophone: Bool = true, onMicrophone: @escaping () -> Void = {},
         beforeSend: @escaping () async throws -> Void = {}) {
        self.app = app; self.chats = chats; self.model = model; attachments = model.attachments
        self.voiceBusy = voiceBusy; self.showsMicrophone = showsMicrophone; self.onMicrophone = onMicrophone; self.beforeSend = beforeSend
    }

    private var canSend: Bool {
        model.valid && chats.selected?.isReadOnly != true && !model.sending && !chats.creating && app.reachable && app.client != nil
            && app.taskStartKeyID != nil && app.attested && AppAttestManager.isSupported
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if !model.message.isEmpty {
                Text(verbatim: model.message).font(.footnote).foregroundStyle(Theme.warn)
                    .fixedSize(horizontal: false, vertical: true).accessibilityIdentifier("chat.message")
                if model.unconfirmed != nil && !model.sending {
                    Button("Hinweis verwerfen") {
                        guard let client = app.client else { return }
                        model.discardUnconfirmed { ConversationRetryCache.clear(client) }
                    }.font(.footnote).frame(minHeight: 44)
                }
            }
            if !chats.createError.isEmpty {
                Text(verbatim: chats.createError).font(.footnote).foregroundStyle(Theme.warn)
            }
            attachmentLine
            if typeSize.isAccessibilitySize {
                VStack(spacing: 4) {
                    textField
                    HStack { attachmentButton; Spacer(); if showsMicrophone { microphoneButton }; sendButton }
                }
            } else {
                HStack(alignment: .bottom, spacing: 4) {
                    attachmentButton; textField; if showsMicrophone { microphoneButton }; sendButton
                }
            }
            if model.characterCount > AppConversationMessageBody.maxTextScalars {
                Text("höchstens \(AppConversationMessageBody.maxTextScalars) Zeichen · \(model.characterCount)")
                    .font(.caption).foregroundStyle(Theme.warn)
            }
            if !AppAttestManager.isSupported {
                Text("Nachrichten sind auf diesem Gerät nicht freigeschaltet.")
                    .font(.footnote).foregroundStyle(Theme.ink2)
            }
        }
        .fileImporter(isPresented: $showPicker,
            allowedContentTypes: (AppTaskDocumentRequest.formats + TaskFileSelection.formats).compactMap { UTType(filenameExtension: $0) },
            allowsMultipleSelection: true) { result in
                switch result {
                case let .success(urls): Task { await model.selectFiles(urls) }
                case let .failure(error): attachments.documentPickerFailed(error)
                }
            }
        .onAppear { if let client = app.client { model.restoreRetryHint { ConversationRetryCache.load(client) } } }
    }

    private var textField: some View {
        TextField("Schreib SOLVIO …", text: $model.text, axis: .vertical)
            .lineLimit(1...5).focused($focused)
            .padding(.horizontal, 14).padding(.vertical, 10)
            .background(Theme.surface, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
            .foregroundStyle(Theme.ink)
            .accessibilityLabel("Deine Nachricht").accessibilityIdentifier("chat.text")
            .disabled(model.sending)
    }
    private var attachmentButton: some View {
        Button { showPicker = true } label: {
            Image(systemName: "paperclip").font(.system(size: 20)).frame(width: 44, height: 44)
        }
        .accessibilityLabel("Anhang hinzufügen").accessibilityIdentifier("chat.attach")
        .disabled(model.sending || attachments.documentLoading)
        .foregroundStyle(Theme.ink2)
    }
    private var microphoneButton: some View {
        Button(action: onMicrophone) {
            Image(systemName: "mic").font(.system(size: 20)).frame(width: 44, height: 44)
        }
        .disabled(voiceBusy || model.sending || chats.creating || !app.reachable || app.client == nil)
        .accessibilityLabel("In diesem Chat sprechen, Mikrofon einschalten")
        .accessibilityIdentifier("home.speak").foregroundStyle(Theme.ink2)
    }

    @ViewBuilder private var attachmentLine: some View {
        if let document = attachments.document {
            HStack {
                Label(document.filename, systemImage: "paperclip").font(.caption).lineLimit(2)
                Spacer(minLength: 8)
                Button("Entfernen", role: .destructive) { model.clearAttachment() }.font(.caption).disabled(model.sending)
            }.foregroundStyle(Theme.ink2)
        }
        if let files = attachments.files {
            HStack {
                Label(files.request.files.map(\.name).joined(separator: " · "), systemImage: "paperclip").font(.caption).lineLimit(3)
                Spacer(minLength: 8)
                Button("Entfernen", role: .destructive) { model.clearAttachment() }.font(.caption).disabled(model.sending)
            }.foregroundStyle(Theme.ink2)
        }
        if attachments.documentLoading { ProgressView("Datei wird lokal gelesen …").font(.caption).foregroundStyle(Theme.ink2) }
        if !attachments.documentError.isEmpty {
            Text(verbatim: attachments.documentError).font(.footnote).foregroundStyle(Theme.warn)
        }
    }

    private var sendButton: some View {
        Button {
            Haptics.tap(); focused = false
            Task { await send() }
        } label: {
            Group {
                if model.sending { ProgressView().tint(Theme.onBlue) }
                else { Image(systemName: model.needsRetry ? "arrow.clockwise" : "arrow.up").font(.system(size: 18, weight: .semibold)) }
            }
            .frame(width: 44, height: 44)
            .foregroundStyle(canSend ? Theme.onBlue : Theme.ink3).background(canSend ? Theme.blue : Theme.surface, in: Circle())
        }
        .buttonStyle(.plain).disabled(!canSend)
        .accessibilityLabel(voiceBusy ? "Sprache beenden und Nachricht senden" : (model.needsRetry ? "Dieselbe Nachricht erneut senden" : "Senden"))
        .accessibilityIdentifier("chat.send")
    }

    private func send() async {
        guard let client = app.client, let keyID = app.taskStartKeyID else { return }
        var conversationID = chats.selectedID
        if conversationID == nil {
            conversationID = await chats.createConversation { try await client.createConversation(clientRequestID: $0) }
        }
        guard let conversationID else { return }
        let accepted = await model.send(conversationID: conversationID, coreID: client.pairedCoreInstanceID,
            deviceID: client.deviceIdentifier, keyID: keyID,
            loadRetry: { ConversationRetryCache.load(client) },
            saveRetry: { try ConversationRetryCache.save($0, client: client) },
            clearRetry: { ConversationRetryCache.clear(client) },
            challenge: { try await client.messageChallenge(conversationID, body: $0) },
            sign: { try await AppAttestManager.assert(keyId: keyID, clientDataHash: $0) },
            submit: { try await client.submitMessage(conversationID, body: $0, proof: $1) },
            beforeSend: beforeSend)
        if accepted != nil {
            await model.refreshDetail(chats: chats, coreID: client.pairedCoreInstanceID,
                deviceID: client.deviceIdentifier, loadRetry: { ConversationRetryCache.load(client) },
                clearRetry: { ConversationRetryCache.clear(client) }, load: { try await client.conversation($0) })
        }
    }
}
