// Dauerhafte Chats (C3), wie die App sie sieht.
//
// Der Core ist der einzige Gespraechsspeicher. Hier liegt nur die fluechtige
// Projektion: Liste, gewaehlter Chat, sein Verlauf und seine Zustellungen —
// alles aus `/v1/conversations`, nichts erfunden, nichts dauerhaft ausser der
// Kennung des zuletzt gewaehlten Chats. Ein spaetes Ergebnis kann nie eine
// andere Auswahl uebermalen: jede Auswahl traegt ihre eigene Epoche.
import Foundation
import Combine
import SolvioApprovalsKit

struct ConversationSummary: Decodable, Identifiable, Equatable, Sendable {
    let conversation_id: String
    let title: String
    let kind: String
    let last_activity_at: Double?
    let message_count: Int?
    let open_task_count: Int?
    let open_delivery_count: Int?
    var read_only: Bool? = nil
    var isReadOnly: Bool { read_only == true }
    var id: String { conversation_id }
    var displayTitle: String { title.isEmpty ? "Neuer Chat" : title }
    var openWork: Int { max(0, open_task_count ?? 0) + max(0, open_delivery_count ?? 0) }
    static func validID(_ id: String) -> Bool {
        id.range(of: "^c-[0-9a-f]{16}$", options: .regularExpression) != nil
    }
}

struct ConversationCreated: Decodable, Equatable, Sendable {
    let conversation_id: String
    let title: String?
    let kind: String?
    let created_at: Double?
}

/// Ein Herkunftsverweis ist eine Core-Chatkennung mit reinem Anzeigetext, keine URL.
struct ConversationSourceChat: Decodable, Equatable, Sendable {
    let conversation_id: String
    let title: String
    var displayTitle: String {
        let value = String(title.trimmingCharacters(in: .whitespacesAndNewlines).prefix(80))
        return value.isEmpty ? "Neuer Chat" : value
    }
    enum CodingKeys: String, CodingKey { case conversation_id, title }
    init(from decoder: Decoder) throws {
        let fields = try decoder.container(keyedBy: CodingKeys.self)
        conversation_id = try fields.decode(String.self, forKey: .conversation_id)
        title = try fields.decode(String.self, forKey: .title)
        guard conversation_id.utf8.count == 18, ConversationSummary.validID(conversation_id) else {
            throw DecodingError.dataCorruptedError(forKey: .conversation_id, in: fields, debugDescription: "Invalid source chat identifier")
        }
    }
}

struct ConversationDelivery: Decodable, Equatable, Sendable {
    let delivery_id: String
    let status: String
    let error_code: String?
    let task_id: String?
    let run_id: String?
    let revision: Int?
    /// Nicht Teil des Vertragsminimums; wird genutzt, wenn der Core sie liefert.
    let client_message_id: String?
    let source_chat: ConversationSourceChat?
    enum CodingKeys: String, CodingKey { case delivery_id, status, error_code, task_id, run_id, revision, client_message_id, source_chat }
    init(from decoder: Decoder) throws {
        let fields = try decoder.container(keyedBy: CodingKeys.self)
        delivery_id = try fields.decode(String.self, forKey: .delivery_id)
        status = try fields.decode(String.self, forKey: .status)
        error_code = try fields.decodeIfPresent(String.self, forKey: .error_code)
        task_id = try fields.decodeIfPresent(String.self, forKey: .task_id)
        run_id = try fields.decodeIfPresent(String.self, forKey: .run_id)
        revision = try fields.decodeIfPresent(Int.self, forKey: .revision)
        client_message_id = try fields.decodeIfPresent(String.self, forKey: .client_message_id)
        // Ein fehlender oder unlesbarer optionaler Verweis verdeckt nie den Verlauf.
        source_chat = try? fields.decode(ConversationSourceChat.self, forKey: .source_chat)
    }
    var isOpen: Bool { status == "accepted" || status == "running" }
    var isBlocked: Bool { status == "blocked" }
    var linkedRunID: String? { run_id.flatMap { AgentRun.validRunID($0) ? $0 : nil } }
    func sourceChat(from currentID: String) -> ConversationSourceChat? {
        source_chat.flatMap { $0.conversation_id != currentID ? $0 : nil }
    }
    static func validID(_ id: String) -> Bool {
        id.range(of: "^cd-[0-9a-f]{16}$", options: .regularExpression) != nil
    }
}

struct ConversationMessage: Decodable, Identifiable, Equatable, Sendable {
    let message_id: String
    let sequence: Int
    let role: String
    let text: String
    let created_at: Double?
    let delivery: ConversationDelivery?
    var id: String { message_id }
    var isUser: Bool { role == "user" }
}

struct ConversationDeliveryStatus: Decodable, Equatable, Sendable {
    let status: String
    let message_id: String?
    let assistant_message_id: String?
    let task_id: String?
    let run_id: String?
    let revision: Int?
    let error_code: String?
}

/// `auftraege` sind Auftragskarten des Cores. Eine einzelne unlesbare Karte
/// darf den ganzen Verlauf nicht unlesbar machen — sie faellt einzeln weg.
struct ConversationDetail: Decodable, Sendable {
    let conversation: ConversationSummary
    let messages: [ConversationMessage]
    let auftraege: [AgentRun]
    let deliveries_open: Int
    enum CodingKeys: String, CodingKey { case conversation, messages, auftraege, deliveries_open }
    private struct Lossy: Decodable { let value: AgentRun?; init(from decoder: Decoder) throws { value = try? AgentRun(from: decoder) } }
    init(from decoder: Decoder) throws {
        let fields = try decoder.container(keyedBy: CodingKeys.self)
        conversation = try fields.decode(ConversationSummary.self, forKey: .conversation)
        messages = try fields.decode([ConversationMessage].self, forKey: .messages)
        auftraege = (try fields.decodeIfPresent([Lossy].self, forKey: .auftraege) ?? []).compactMap(\.value)
            .filter { AgentRun.validRunID($0.id) }
        deliveries_open = try fields.decodeIfPresent(Int.self, forKey: .deliveries_open) ?? 0
    }
    init(conversation: ConversationSummary, messages: [ConversationMessage], auftraege: [AgentRun], deliveriesOpen: Int) {
        self.conversation = conversation; self.messages = messages; self.auftraege = auftraege; deliveries_open = deliveriesOpen
    }
    var hasOpenWork: Bool { deliveries_open > 0 || auftraege.contains { $0.offen } || messages.contains { $0.delivery?.isOpen == true } }
    /// Enthaelt der Verlauf die Zustellung dieser Wiederholungsmarke?
    func contains(_ pending: ConversationMessageRetryBinding) -> Bool {
        messages.contains { message in
            guard let delivery = message.delivery else { return false }
            if let id = pending.deliveryID, delivery.delivery_id == id { return true }
            return delivery.client_message_id == pending.clientMessageID
        }
    }
}

/// Deutsche Saetze je Zustellfehler. Keiner behauptet Erfolg.
enum ConversationErrorText {
    static func text(_ code: String) -> String {
        switch code {
        case "cost_recovery_required": return "Eine Kostenbuchung dieser Nachricht ist ungeklärt. Sie wurde nicht beantwortet; die nächste Nachricht ist davon nicht betroffen."
        case "room_conversation_read_only": return "Dieses Raumgespräch ist hier zum Nachlesen. Starte einen neuen Chat für deine private Fortsetzung."
        case "quota": return "Das Kontingent ist erschöpft. Bitte später erneut senden."
        case "provider_unavailable": return "Der Anbieter ist gerade nicht erreichbar. Diese Nachricht wurde nicht beantwortet."
        case "provider_output_invalid": return "Die Antwort war nicht verwertbar. Bitte die Nachricht erneut senden."
        case "assessment_unavailable": return "SOLVIO konnte diese Nachricht nicht einordnen. Bitte erneut senden."
        case "processing_timeout": return "Die Verarbeitung hat zu lange gedauert. Diese Nachricht wurde nicht beantwortet."
        case "source_revoked": return "Deine Anmeldung ist inzwischen abgelaufen — bitte das iPhone neu koppeln und die Nachricht erneut senden."
        case "followup_not_available": return "Dieser Auftrag lässt sich so nicht fortsetzen."
        case "objective_too_long": return "Für einen Auftrag bitte kürzer fassen: höchstens 2000 Zeichen."
        case "core_restarted_unresolved": return "Der Core wurde neu gestartet; das Ergebnis dieser Nachricht ist nicht bekannt."
        default:
            if code.hasPrefix("task_start_refused:") {
                return "Der Auftrag wurde nicht gestartet (\(code.dropFirst("task_start_refused:".count)))."
            }
            return code.isEmpty ? "Diese Nachricht wurde nicht verarbeitet." : "Diese Nachricht wurde nicht verarbeitet (\(code))."
        }
    }
}

enum ConversationReconcile: Equatable { case resolved, unresolved, unavailable }

@MainActor
final class ConversationModel: ObservableObject {
    @Published private(set) var conversations: [ConversationSummary] = []
    @Published private(set) var selectedID: String?
    @Published private(set) var detail: ConversationDetail?
    @Published private(set) var listError = ""
    @Published private(set) var detailError = ""
    @Published private(set) var loadingDetail = false
    @Published private(set) var creating = false
    @Published private(set) var createError = ""
    /// Hinweis nach Neustart: eine Nachricht ist moeglicherweise nicht angekommen.
    @Published private(set) var pendingHint = ""

    /// Die Epoche der Auswahl. Ein Ergebnis aus einer alten Epoche wird verworfen.
    private var epoch = UUID()
    private var listGeneration = UUID()
    /// Bleibt ueber Wiederholungen stehen: dieselbe Kennung ergibt denselben Chat.
    private var createRequestID: String?

    static let selectionKey = "solvio.chat.selected.v1"
    static let workingPoll: UInt64 = 3_000_000_000
    static let idlePoll: UInt64 = 5_000_000_000

    var workingCount: Int { conversations.reduce(0) { $0 + $1.openWork } }
    var hasOpenWork: Bool { detail?.hasOpenWork == true }
    var pollNanoseconds: UInt64 { hasOpenWork ? Self.workingPoll : Self.idlePoll }
    var selected: ConversationSummary? { conversations.first { $0.conversation_id == selectedID } ?? detail?.conversation }

    func select(_ id: String?, remember: Bool = true) {
        // Nur nil waehlt ab; eine ungueltige Kennung ist keine Auswahl und kein Abwaehlen.
        if let id, !ConversationSummary.validID(id) { return }
        let next = id
        guard next != selectedID else { return }
        epoch = UUID()
        selectedID = next
        detail = nil; detailError = ""; loadingDetail = false
        if remember { UserDefaults.standard.set(next, forKey: Self.selectionKey) }
    }

    /// Nur Kennungen werden gemerkt; die Kennung zaehlt nur, wenn die Liste sie kennt.
    func restoreSelection() {
        guard selectedID == nil, let saved = UserDefaults.standard.string(forKey: Self.selectionKey),
              conversations.contains(where: { $0.conversation_id == saved }) else { return }
        select(saved, remember: false)
    }

    func invalidate() {
        epoch = UUID(); listGeneration = UUID()
        conversations = []; selectedID = nil; detail = nil
        listError = ""; detailError = ""; loadingDetail = false; createError = ""; pendingHint = ""
        createRequestID = nil; creating = false
    }

    func refreshList(load: () async throws -> [ConversationSummary]) async {
        let current = listGeneration
        do {
            let rows = try await load()
            guard listGeneration == current, !Task.isCancelled else { return }
            conversations = rows; listError = ""
            if let selectedID, !rows.contains(where: { $0.conversation_id == selectedID }), detail == nil, !detailError.isEmpty {
                // Ein Chat, den weder Liste noch Detail kennen, ist kein gewaehlter Chat mehr.
                select(nil)
            }
        } catch {
            guard listGeneration == current, !Task.isCancelled else { return }
            listError = "Die Chatliste konnte nicht gelesen werden. Der Core kann weiterarbeiten."
        }
    }

    /// Liest den gewaehlten Chat. `pending`/`clearPending` raeumen eine
    /// Wiederholungsmarke ab, sobald der Verlauf ihre Zustellung zeigt.
    func refreshDetail(load: (String) async throws -> ConversationDetail,
                       pending: ConversationMessageRetryBinding? = nil, clearPending: () -> Void = {}) async {
        guard let id = selectedID, !loadingDetail else { return }
        let current = epoch
        loadingDetail = true
        defer { if epoch == current { loadingDetail = false } }
        do {
            let value = try await load(id)
            guard epoch == current, selectedID == id, !Task.isCancelled else { return }
            guard value.conversation.conversation_id == id else { throw ClientError.decode }
            detail = value; detailError = ""
            if let pending, pending.conversationID == id, value.contains(pending) { clearPending(); pendingHint = "" }
        } catch {
            guard epoch == current, selectedID == id, !Task.isCancelled else { return }
            if case ClientError.http(404) = error {
                detailError = "Dieser Chat ist nicht mehr vorhanden."
            } else {
                detailError = "Der Verlauf konnte nicht gelesen werden. Angezeigt wird gegebenenfalls der letzte gelesene Stand."
            }
        }
    }

    /// „Neuer Chat" — idempotent ueber eine Anfragekennung, die bis zum Erfolg
    /// stehen bleibt: ein Doppeltipp oder Netzfehler erzeugt keinen zweiten Chat.
    @discardableResult
    func createConversation(create: (String) async throws -> ConversationCreated) async -> String? {
        guard !creating else { return nil }
        let generation = listGeneration
        creating = true; createError = ""
        defer { if listGeneration == generation { creating = false } }
        let requestID = createRequestID ?? "chat-" + UUID().uuidString.lowercased()
        createRequestID = requestID
        do {
            let created = try await create(requestID)
            guard listGeneration == generation, !Task.isCancelled else { return nil }
            createRequestID = nil
            if !conversations.contains(where: { $0.conversation_id == created.conversation_id }) {
                conversations.insert(ConversationSummary(conversation_id: created.conversation_id, title: created.title ?? "",
                    kind: created.kind ?? "text", last_activity_at: created.created_at, message_count: 0,
                    open_task_count: 0, open_delivery_count: 0), at: 0)
            }
            select(created.conversation_id)
            return created.conversation_id
        } catch {
            guard listGeneration == generation, !Task.isCancelled else { return nil }
            createError = "Der Chat konnte nicht angelegt werden. Erneut versuchen verwendet dieselbe Anfrage."
            return nil
        }
    }

    /// Sprache startet nur in einem vom Core bestaetigten, weiterhin ausgewaehlten Chat.
    /// Keine eigene Chatablage und kein stiller Ersatz fuer einen geloeschten Chat.
    func prepareVoiceConversation(create: (String) async throws -> ConversationCreated,
                                  load: (String) async throws -> ConversationDetail) async -> String? {
        let id: String
        if let selectedID { id = selectedID }
        else {
            guard let created = await createConversation(create: create) else { return nil }
            id = created
        }
        let current = epoch
        do {
            let value = try await load(id)
            guard !Task.isCancelled, epoch == current, selectedID == id,
                  value.conversation.conversation_id == id else { return nil }
            detail = value; detailError = ""
            guard !value.conversation.isReadOnly else {
                detailError = "Das Raumgespräch ist hier zum Nachlesen. Starte einen neuen Chat, um privat fortzufahren."
                return nil
            }
            return id
        } catch {
            if epoch == current { detailError = "Der Chat konnte nicht für Sprache bestätigt werden." }
            return nil
        }
    }

    /// Nach einem Neustart: ist die gemerkte Nachricht im Verlauf angekommen?
    /// Nie ein blindes Neusenden — der Text ist nicht gespeichert.
    func reconcile(pending: ConversationMessageRetryBinding,
                   load: (String) async throws -> ConversationDetail) async -> ConversationReconcile {
        guard ConversationSummary.validID(pending.conversationID) else { pendingHint = ""; return .resolved }
        do {
            let value = try await load(pending.conversationID)
            guard !Task.isCancelled else { return .unavailable }
            if value.contains(pending) { pendingHint = ""; return .resolved }
            pendingHint = "Eine Nachricht ist möglicherweise nicht angekommen. Sieh im Verlauf nach oder gib denselben Text erneut ein — es wird nichts automatisch gesendet."
            return .unresolved
        } catch {
            guard !Task.isCancelled else { return .unavailable }
            if case ClientError.http(404) = error {
                // Der Chat existiert nicht mehr; die Marke fuehrt zu nichts.
                pendingHint = ""; return .resolved
            }
            pendingHint = "Eine Nachricht ist möglicherweise nicht angekommen; der Verlauf ließ sich noch nicht lesen. Es wird nichts automatisch gesendet."
            return .unavailable
        }
    }

    func clearPendingHint() { pendingHint = "" }
}
