import Foundation

public enum TaskFileError: Error, Equatable, LocalizedError {
    case invalidFiles, invalidName, invalidSize, unreadableFile
    public var errorDescription: String? {
        switch self {
        case .invalidFiles: return "Bitte wähle bis zu vier CSV- oder XLSX-Tabellen. Textdokumente bitte einzeln anhängen."
        case .invalidName: return "Die Dateinamen müssen eindeutig sein und dürfen keine Pfadangaben enthalten."
        case .invalidSize: return "Die Dateien dürfen zusammen höchstens 8 MiB groß sein und keine leere Datei enthalten."
        case .unreadableFile: return "Die Datei konnte nicht gelesen werden. Lade sie in Dateien herunter und wähle sie erneut."
        }
    }
}

public struct AppTaskFileInput: Codable, Equatable, Sendable {
    public let name, content_b64: String
    public static let byteLimit = 8 * 1024 * 1024
    enum CodingKeys: String, CodingKey { case name, content_b64 }

    public init(name: String, content: Data) throws {
        guard !name.isEmpty, Array(name.utf8) == Array(name.precomposedStringWithCanonicalMapping.utf8),
              name == name.trimmingCharacters(in: .whitespacesAndNewlines.union(CharacterSet(charactersIn: "."))),
              name.unicodeScalars.count <= 120, name.utf8.count <= 240,
              name.rangeOfCharacter(from: .controlCharacters.union(CharacterSet(charactersIn: "/\\<>:\"|?*"))) == nil
              else { throw TaskFileError.invalidName }
        guard (1...Self.byteLimit).contains(content.count) else { throw TaskFileError.invalidSize }
        self.name = name; content_b64 = content.base64EncodedString()
    }

    public init(from decoder: Decoder) throws {
        try requireTaskKeys(decoder, required: ["name", "content_b64"])
        let fields = try decoder.container(keyedBy: CodingKeys.self)
        let name = try fields.decode(String.self, forKey: .name)
        let raw = try fields.decode(String.self, forKey: .content_b64)
        guard raw.utf8.count <= 4 * ((Self.byteLimit + 2) / 3),
              let bytes = Data(base64Encoded: raw), bytes.base64EncodedString() == raw
              else { throw TaskFileError.invalidFiles }
        try self.init(name: name, content: bytes)
    }
    public var content: Data { Data(base64Encoded: content_b64)! }
    var json: TaskCanonicalValue { .object(["name": .string(name), "content_b64": .string(content_b64)]) }
}

public struct AppTaskFileRequest: Codable, Equatable, Sendable {
    public let operation: String
    public let files: [AppTaskFileInput]
    enum CodingKeys: String, CodingKey { case operation, files }
    public init(files: [AppTaskFileInput]) throws {
        guard (1...4).contains(files.count) else { throw TaskFileError.invalidFiles }
        let names = files.map { $0.name.folding(options: [.caseInsensitive], locale: Locale(identifier: "en_US_POSIX")) }
        guard Set(names).count == names.count else { throw TaskFileError.invalidName }
        guard files.reduce(0, { $0 + $1.content.count }) <= AppTaskFileInput.byteLimit
              else { throw TaskFileError.invalidSize }
        operation = "process_files"; self.files = files
    }
    public init(from decoder: Decoder) throws {
        try requireTaskKeys(decoder, required: ["operation", "files"])
        let fields = try decoder.container(keyedBy: CodingKeys.self)
        guard try fields.decode(String.self, forKey: .operation) == "process_files"
              else { throw TaskFileError.invalidFiles }
        try self.init(files: fields.decode([AppTaskFileInput].self, forKey: .files))
    }
    var json: TaskCanonicalValue { .object(["operation": .string(operation), "files": .array(files.map(\.json))]) }
    public var byteCount: Int { files.reduce(0, { $0 + $1.content.count }) }
}

/// Only selected bytes enter the signed request. No device path is sent or saved.
public struct TaskFileSelection: Equatable, Sendable {
    public let request: AppTaskFileRequest
    public static let formats = ["csv", "xlsx"]
    /// Caller holds each file picker's security scope for the duration of read.
    public static func read(_ urls: [URL]) throws -> Self {
        guard (1...4).contains(urls.count), urls.allSatisfy({ $0.isFileURL && formats.contains($0.pathExtension.lowercased()) })
              else { throw TaskFileError.invalidFiles }
        var selected: [AppTaskFileInput] = []; var remaining = AppTaskFileInput.byteLimit
        for url in urls {
            let metadata = try url.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey])
            guard metadata.isRegularFile == true, metadata.isSymbolicLink != true else { throw TaskFileError.unreadableFile }
            let file = try FileHandle(forReadingFrom: url)
            defer { try? file.close() }
            var bytes = Data()
            while bytes.count <= remaining {
                let next = try file.read(upToCount: min(65_536, remaining + 1 - bytes.count)) ?? Data()
                if next.isEmpty { break }
                bytes.append(next)
            }
            guard !bytes.isEmpty, bytes.count <= remaining else { throw TaskFileError.invalidSize }
            remaining -= bytes.count
            // Filesystems can expose a decomposed spelling. Canonicalize the
            // display filename before binding; the selected bytes stay exact.
            selected.append(try AppTaskFileInput(name: url.lastPathComponent.precomposedStringWithCanonicalMapping, content: bytes))
        }
        return Self(request: try AppTaskFileRequest(files: selected))
    }
}
