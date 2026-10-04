import Foundation

public enum TaskDocumentError: Error, Equatable, LocalizedError {
    case unsupportedFormat, invalidSize, invalidText, invalidRTF, invalidRequest, unreadableFile
    public var errorDescription: String? {
        switch self {
        case .unsupportedFormat: return "Bitte wähle eine RTF-, TXT-, DOCX- oder ODT-Datei."
        case .invalidSize: return "Die Datei ist leer oder zu groß. TXT und RTF: höchstens 64 KiB; DOCX und ODT: höchstens 1 MiB."
        case .invalidText: return "Bitte wähle eine UTF-8-Textdatei mit lesbarem Text und ohne unerlaubte Steuerzeichen."
        case .invalidRTF: return "Die Datei hat keinen gültigen RTF-Anfang."
        case .invalidRequest: return "Die Dokumentangaben sind ungültig. Bitte wähle die Datei erneut."
        case .unreadableFile: return "Die Datei konnte nicht gelesen werden. Bitte lade sie in Dateien herunter und wähle sie erneut."
        }
    }
}

/// Exact input bytes only. The Core validates Office ZIP/XML contents and owns
/// conversion, grants and results; the phone never extracts or rewrites them.
public struct AppTaskDocumentRequest: Codable, Equatable, Sendable {
    public let operation, format, content_b64: String
    public static let formats = ["rtf", "txt", "docx", "odt"]
    enum CodingKeys: String, CodingKey { case operation, format, content_b64 }

    public static func byteLimit(format: String) throws -> Int {
        switch format {
        case "rtf", "txt": return 65_536
        case "docx", "odt": return 1_048_576
        default: throw TaskDocumentError.unsupportedFormat
        }
    }

    public init(format: String, content: Data) throws {
        let limit = try Self.byteLimit(format: format)
        guard (1...limit).contains(content.count) else { throw TaskDocumentError.invalidSize }
        if format == "rtf" {
            let prefix = Array(content.prefix(7))
            guard prefix.count == 7, Array(prefix.prefix(6)) == Array("{\\rtf1".utf8),
                  [UInt8(92), 32, 9, 13, 10].contains(prefix[6]) else { throw TaskDocumentError.invalidRTF }
        } else if format == "txt" {
            guard let decoded = String(data: content, encoding: .utf8) else { throw TaskDocumentError.invalidText }
            // A UTF-8 BOM is ignored for validation, but retained in the payload.
            let text = decoded.hasPrefix("\u{feff}") ? String(decoded.dropFirst()) : decoded
            guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
                  !text.unicodeScalars.contains(where: { $0.value < 32 && ![9, 10, 13].contains($0.value) }) else {
                throw TaskDocumentError.invalidText
            }
        }
        self.operation = "extract_text"; self.format = format
        content_b64 = content.base64EncodedString()
    }

    public init(from decoder: Decoder) throws {
        try requireTaskKeys(decoder, required: ["operation", "format", "content_b64"])
        let fields = try decoder.container(keyedBy: CodingKeys.self)
        let operation = try fields.decode(String.self, forKey: .operation)
        let format = try fields.decode(String.self, forKey: .format)
        let raw = try fields.decode(String.self, forKey: .content_b64)
        let limit = try Self.byteLimit(format: format)
        guard operation == "extract_text", raw.utf8.count <= 4 * ((limit + 2) / 3),
              let content = Data(base64Encoded: raw), content.base64EncodedString() == raw else {
            throw TaskDocumentError.invalidRequest
        }
        try self.init(format: format, content: content)
    }

    public var content: Data { Data(base64Encoded: content_b64)! }
    public var byteCount: Int { content.count }
    var json: TaskCanonicalValue {
        .object(["operation": .string(operation), "format": .string(format), "content_b64": .string(content_b64)])
    }
}

/// A transient selection. Neither the filename nor a device path is sent to the
/// Core or stored in retry metadata. Call within the file picker's security scope.
public struct TaskDocumentSelection: Equatable, Sendable {
    public let filename: String
    public let request: AppTaskDocumentRequest

    public static func read(_ url: URL) throws -> Self {
        guard url.isFileURL else { throw TaskDocumentError.unreadableFile }
        let format = url.pathExtension.lowercased()
        let limit = try AppTaskDocumentRequest.byteLimit(format: format)
        let values = try url.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey])
        guard values.isRegularFile == true, values.isSymbolicLink != true else { throw TaskDocumentError.unreadableFile }
        let file = try FileHandle(forReadingFrom: url)
        defer { try? file.close() }
        // Read one byte beyond the ceiling to reject oversize files, including
        // those growing after selection. Never load an unbounded Data(contentsOf:).
        var bytes = Data()
        while bytes.count <= limit {
            let part = try file.read(upToCount: min(65_536, limit + 1 - bytes.count)) ?? Data()
            if part.isEmpty { break }
            bytes.append(part)
        }
        let request = try AppTaskDocumentRequest(format: format, content: bytes)
        return Self(filename: url.lastPathComponent, request: request)
    }
}
