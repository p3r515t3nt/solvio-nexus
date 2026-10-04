import Foundation
import CryptoKit

/// A projection of the existing Core ledger, never a second task store.
public struct AgentRun: Decodable, Identifiable, Sendable {
    public let id: String
    public let aufgabe: String
    public let auftrag: String
    public let zustand: String
    public let zustand_code: String
    public let offen: Bool
    public let angelegt: Double?
    public let ergebnis: String?
    public let grund: String?
    public let grund_code: String?
    public let vorbereitet: Bool?
    public let befunde: [String]?
    public let quellen: [String]?
    public let dateien: [ResultFile]?
    public let datei_hinweis: String?
    public let wartet_auf: Waiting?
    public let anbietergrenze: ProviderBoundary?
    public let kosten: Costs?
    public let verlauf: [Event]?
    public let action_intent: ActionIntentSnapshot?
    public let task_revision: TaskRevisionSnapshot?
    public let task_history: [TaskHistoryEntry]?
    public let followup: TaskFollowupAvailability?

    public struct Waiting: Decodable, Sendable {
        public let handlung: String?
        public let grund: String?
        public let danach: String?
    }
    public struct ProviderBoundary: Decodable, Sendable {
        public let grund: String?
        public let fortsetzbar: Bool?
        public let boundary_ref: String?
        public let wechseloptionen: [ProviderOption]?
    }
    public struct ProviderOption: Decodable, Sendable, Identifiable {
        public let provider: String
        public let label: String
        public let hinweis: String
        public let werkzeuge: [String]
        public var id: String { provider }
    }
    public struct Costs: Decodable, Sendable {
        public let task_id: String?
        public let currency: String?
        public let configured: Bool?
        public let ask_threshold_cents: Int?
        public let approved_ai_cap_cents: Int?
        public let purchase_cap_cents: Int?
        public let ai_tool: Totals?
        public let counts: Counts?
        public struct Totals: Decodable, Sendable {
            public let spent_cents: Int?
            public let reserved_cents: Int?
        }
        public struct Counts: Decodable, Sendable { public let unknown: Int? }
    }
    public struct Event: Decodable, Sendable {
        public let zeit: Double?
        public let text: String?
        public let art: String?
    }

    public var canResume: Bool {
        zustand_code == "WAITING_USER" && action_intent?.question == nil
            && (anbietergrenze == nil || anbietergrenze?.fortsetzbar == true)
    }
    /// Preserve the Core's incomplete-result wording without changing terminal state.
    public var failureNotice: String? {
        guard zustand_code == "FAILED" else { return nil }
        let title = grund_code == "goal_unverified" ? zustand : "Auftrag fehlgeschlagen"
        return title + ". " + (grund ?? "Das vollständige Ergebnis wurde nicht bestätigt.")
    }
    public var providerOptions: [ProviderOption] {
        guard canResume, let ref = anbietergrenze?.boundary_ref,
              ref.range(of: "^[a-f0-9]{64}$", options: .regularExpression) != nil else { return [] }
        return (anbietergrenze?.wechseloptionen ?? []).filter { ["codex", "claude-code"].contains($0.provider) }
    }
    public static func validRunID(_ id: String) -> Bool {
        id.range(of: "^ar-[0-9a-f]{16}$", options: .regularExpression) != nil
    }
}

public enum ResultFileError: Error, Equatable, LocalizedError {
    case invalidDescriptor, responseChanged, contentChanged, tooLarge
    public var errorDescription: String? {
        switch self {
        case .invalidDescriptor: return "Die Datei ist nicht eindeutig an diesen Auftrag gebunden."
        case .responseChanged: return "Der Core hat die angefragte Datei nicht unverändert bestätigt."
        case .contentChanged: return "Die Datei stimmt nicht mit dem geprüften Ergebnis überein."
        case .tooLarge: return "Diese Datei ist für den Download in der App zu groß."
        }
    }
}

public struct ResultFile: Decodable, Identifiable, Equatable, Sendable {
    public let id: String
    public let name: String
    public let mime_type: String
    public let size: Int
    public let sha256: String
    public let download_url: String
    public let preview_url: String?
    public let preview_kind: String
    public static let maximumBytes = 128 * 1024 * 1024

    /// URLs are exact Core routes. A model URL, path, query, or redirect is not a file capability.
    public func downloadPath(runID: String) throws -> String {
        guard AgentRun.validRunID(runID),
              id.range(of: "^aa-[0-9a-f]{16}$", options: .regularExpression) != nil,
              !name.isEmpty, name.utf8.count <= 255,
              !name.contains("/"), !name.contains("\\"), name != ".", name != "..",
              !name.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) }),
              size >= 0,
              sha256.range(of: "^[0-9a-f]{64}$", options: .regularExpression) != nil,
              mime_type.range(of: "^[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+$", options: .regularExpression) != nil,
              ["image", "pdf", "audio", "video", "text", "none"].contains(preview_kind) else {
            throw ResultFileError.invalidDescriptor
        }
        guard size <= Self.maximumBytes else { throw ResultFileError.tooLarge }
        let base = "/v1/agent/runs/\(runID)/artifacts/\(id)"
        guard download_url == base + "/download",
              preview_url == nil || preview_url == base + "/preview" else {
            throw ResultFileError.invalidDescriptor
        }
        return String(download_url.dropFirst())
    }

    public func verify(data: Data, mimeType: String?, runID: String) throws {
        _ = try downloadPath(runID: runID)
        guard mimeType?.lowercased() == mime_type else { throw ResultFileError.responseChanged }
        guard data.count == size,
              SHA256.hash(data: data).map({ String(format: "%02x", $0) }).joined() == sha256 else {
            throw ResultFileError.contentChanged
        }
    }

    /// Quick Look is only enabled for explicitly supported passive local formats.
    /// Unknown formats still have the authenticated download/share path.
    public var canPreview: Bool {
        switch preview_kind {
        case "image": return ["image/png", "image/jpeg", "image/gif", "image/webp", "image/heic"].contains(mime_type)
        case "pdf": return mime_type == "application/pdf"
        case "text": return ["text/plain", "text/csv", "text/markdown", "application/json"].contains(mime_type)
        case "audio": return ["audio/mpeg", "audio/mp4", "audio/wav", "audio/x-wav", "audio/aac"].contains(mime_type)
        case "video": return ["video/mp4", "video/quicktime"].contains(mime_type)
        default: return false
        }
    }
}
