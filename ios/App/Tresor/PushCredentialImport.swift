import CryptoKit
import Darwin
import Foundation

/// One local setup handoff, outside chats, backups, logs and the app bundle.
/// The existing attested vault mutation still requires genuine owner approval.
enum PushCredentialImport {
    static let reference = "secret://apple-push/nexus"
    static let maximumBytes = 16_384
    static var preparedURL: URL {
        FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("solvio-apple-push.json")
    }
    enum Failure: Error { case unavailable, invalid }

    static func isPrepared(at url: URL = preparedURL) -> Bool {
        var info = stat()
        return lstat(url.path, &info) == 0 && (info.st_mode & S_IFMT) == S_IFREG
            && info.st_size > 0 && info.st_size <= maximumBytes
    }

    static var draft: TresorDraft {
        var draft = TresorDraft()
        draft.service = "apple-push"
        draft.account = "nexus"
        draft.displayName = "Apple-Mitteilungen für SOLVIO Nexus"
        draft.kind = "api_key"
        draft.target = "https://api.sandbox.push.apple.com"
        draft.capability = "push_notify"
        draft.executor = "http"
        draft.allowBackground = true
        return draft
    }

    static func validate(_ data: Data) throws -> String {
        guard !data.isEmpty, data.count <= maximumBytes,
              let value = try? JSONSerialization.jsonObject(with: data) as? [String: String],
              Set(value.keys) == ["bundle_id", "environment", "team_id", "key_id", "private_key"],
              value["bundle_id"] == "de.solvio.approvals",
              value["environment"] == "development",
              value["team_id"] == "WQ8CG7R53R",
              let keyID = value["key_id"],
              keyID.utf8.count == 10,
              keyID.utf8.allSatisfy({ (65...90).contains($0) || (48...57).contains($0) }),
              let pem = value["private_key"],
              (try? P256.Signing.PrivateKey(pemRepresentation: pem)) != nil
        else { throw Failure.invalid }
        let payload = ["team_id": value["team_id"]!, "key_id": keyID, "private_key": pem]
        return String(decoding: try JSONSerialization.data(withJSONObject: payload, options: .sortedKeys), as: UTF8.self)
    }

    /// Delete the temporary handoff before starting any network operation.
    /// A second tap, denied approval or restart cannot reuse it automatically.
    static func take(at url: URL = preparedURL) throws -> String {
        let fd = open(url.path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK)
        guard fd >= 0 else { throw Failure.unavailable }
        let handle = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
        defer { try? handle.close() }
        var opened = stat()
        guard fstat(fd, &opened) == 0, (opened.st_mode & S_IFMT) == S_IFREG,
              opened.st_size > 0, opened.st_size <= maximumBytes,
              let data = try handle.read(upToCount: maximumBytes + 1)
        else { throw Failure.invalid }
        let payload = try validate(data)
        var current = stat()
        guard lstat(url.path, &current) == 0,
              current.st_dev == opened.st_dev, current.st_ino == opened.st_ino,
              unlink(url.path) == 0 else { throw Failure.unavailable }
        return payload
    }
}
