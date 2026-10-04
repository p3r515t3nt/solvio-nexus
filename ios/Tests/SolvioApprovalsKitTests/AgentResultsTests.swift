import Foundation
import CryptoKit
import XCTest
@testable import SolvioApprovalsKit

final class AgentResultsTests: XCTestCase {
    func testProviderChoiceRequiresWaitingStateAndExactBoundaryAndKnownRoute() throws {
        var raw: [String: Any] = ["id": "ar-0123456789abcdef", "aufgabe": "at-0123456789abcdef",
            "auftrag": "Prüfe die Quellen", "zustand": "wartet auf dich", "zustand_code": "WAITING_USER", "offen": true,
            "anbietergrenze": ["fortsetzbar": true, "boundary_ref": String(repeating: "a", count: 64),
                "wechseloptionen": [["provider": "claude-code", "label": "Claude Code",
                    "hinweis": "Derselbe Auftrag", "werkzeuge": ["WebSearch", "WebFetch"]],
                    ["provider": "external-api", "label": "Fremd", "hinweis": "", "werkzeuge": []]]]]
        func decode() throws -> AgentRun {
            try JSONDecoder().decode(AgentRun.self, from: JSONSerialization.data(withJSONObject: raw))
        }
        XCTAssertEqual(try decode().providerOptions.map(\.provider), ["claude-code"])
        XCTAssertEqual(try decode().providerOptions.first?.werkzeuge, ["WebSearch", "WebFetch"])
        raw["zustand_code"] = "RUNNING"
        XCTAssertTrue(try decode().providerOptions.isEmpty)
        raw["zustand_code"] = "WAITING_USER"
        raw["anbietergrenze"] = ["fortsetzbar": true, "boundary_ref": "stale"]
        XCTAssertTrue(try decode().providerOptions.isEmpty)
        raw["anbietergrenze"] = ["fortsetzbar": false]
        XCTAssertFalse(try decode().canResume)
    }

    private let runID = "ar-0123456789abcdef"
    private let fileID = "aa-fedcba9876543210"
    private let bytes = Data("Grüße vom Hund 🐕\n".utf8)

    private func raw() -> [String: Any] {
        ["id": fileID, "name": "Ergebnis.txt", "mime_type": "text/plain", "size": bytes.count,
         "sha256": SHA256.hash(data: bytes).map { String(format: "%02x", $0) }.joined(),
         "download_url": "/v1/agent/runs/\(runID)/artifacts/\(fileID)/download",
         "preview_url": "/v1/agent/runs/\(runID)/artifacts/\(fileID)/preview", "preview_kind": "text"]
    }
    private func file(_ mutation: (inout [String: Any]) -> Void = { _ in }) throws -> ResultFile {
        var value = raw(); mutation(&value)
        return try JSONDecoder().decode(ResultFile.self, from: JSONSerialization.data(withJSONObject: value))
    }

    func testActualUTF8BytesHashAndBoundDownloadPass() throws {
        let file = try file()
        XCTAssertEqual(try file.downloadPath(runID: runID), "v1/agent/runs/\(runID)/artifacts/\(fileID)/download")
        XCTAssertTrue(file.canPreview)
        try file.verify(data: bytes, mimeType: "text/plain", runID: runID)
    }

    func testModelURLsPathsQueriesAndForeignRunNeverBecomeDownloadAuthority() throws {
        let exact = try file().download_url
        for url in ["https://example.org/picture.png", "//example.org/file", "file:///tmp/a", exact + "?token=a",
                    exact.replacingOccurrences(of: runID, with: "ar-aaaaaaaaaaaaaaaa"),
                    exact.replacingOccurrences(of: fileID, with: "aa-aaaaaaaaaaaaaaaa"),
                    exact.replacingOccurrences(of: "/artifacts/", with: "/artifacts/../"), exact + "#x"] {
            XCTAssertThrowsError(try file { $0["download_url"] = url }.downloadPath(runID: runID), url)
        }
        XCTAssertThrowsError(try file { $0["preview_url"] = "https://example.org/file" }.downloadPath(runID: runID))
        XCTAssertThrowsError(try file().downloadPath(runID: "../another"))
    }

    func testChangedSizeMimeOrSingleByteCannotBeSharedAsVerified() throws {
        let file = try file()
        XCTAssertThrowsError(try file.verify(data: bytes + Data([0]), mimeType: "text/plain", runID: runID))
        XCTAssertThrowsError(try file.verify(data: Data(repeating: 0, count: bytes.count), mimeType: "text/plain", runID: runID))
        XCTAssertThrowsError(try file.verify(data: bytes, mimeType: "text/html", runID: runID))
        XCTAssertThrowsError(try file.verify(data: bytes, mimeType: nil, runID: runID))
    }

    func testDescriptorBoundsAndFilenameTraversalFailBeforeDownload() throws {
        for name in ["", "../secret", "folder/file", "folder\\file", ".", "..", "a\u{0}b", String(repeating: "x", count: 256)] {
            XCTAssertThrowsError(try file { $0["name"] = name }.downloadPath(runID: runID))
        }
        for size in [-1, ResultFile.maximumBytes + 1] {
            XCTAssertThrowsError(try file { $0["size"] = size }.downloadPath(runID: runID))
        }
        XCTAssertThrowsError(try file { $0["sha256"] = "claim" }.downloadPath(runID: runID))
        XCTAssertThrowsError(try file { $0["mime_type"] = "text/plain\r\nheader: x" }.downloadPath(runID: runID))
        XCTAssertThrowsError(try file { $0["preview_kind"] = "execute" }.downloadPath(runID: runID))
    }

    func testUnknownFormatsRemainDownloadableButHTMLIsNeverAnInlinePreview() throws {
        let archive = try file { $0["mime_type"] = "application/zip"; $0["preview_kind"] = "none"; $0["preview_url"] = NSNull() }
        XCTAssertNoThrow(try archive.downloadPath(runID: runID)); XCTAssertFalse(archive.canPreview)
        let html = try file { $0["mime_type"] = "text/html"; $0["preview_kind"] = "text" }
        XCTAssertFalse(html.canPreview)
        let image = try file { $0["mime_type"] = "image/png"; $0["preview_kind"] = "image" }
        XCTAssertTrue(image.canPreview)
    }

    func testFailedTaskStillHasItsVerifiedFileWithoutBecomingSuccessful() throws {
        let raw: [String: Any] = ["id": runID, "aufgabe": "at-0123456789abcdef", "auftrag": "Extrahiere den Text.",
            "zustand": "Fehlgeschlagen", "zustand_code": "FAILED", "offen": false,
            "ergebnis": "", "grund": "Ziel nicht vollständig bestätigt", "dateien": [self.raw()],
            "datei_hinweis": "Ein weiteres Ergebnis ist nicht verfügbar."]
        let run = try JSONDecoder().decode(AgentRun.self, from: JSONSerialization.data(withJSONObject: raw))
        XCTAssertEqual(run.zustand_code, "FAILED"); XCTAssertFalse(run.canResume)
        XCTAssertEqual(run.datei_hinweis, "Ein weiteres Ergebnis ist nicht verfügbar.")
        try XCTUnwrap(run.dateien?.first).verify(data: bytes, mimeType: "text/plain", runID: run.id)
    }

    func testOlderCoreWithoutFilesStillDecodesAndResumeRequiresActualBoundary() throws {
        var raw: [String: Any] = ["id": runID, "aufgabe": "at-0123456789abcdef", "auftrag": "Vergleiche Optionen.",
            "zustand": "Wartet", "zustand_code": "WAITING_USER", "offen": true]
        func decode() throws -> AgentRun { try JSONDecoder().decode(AgentRun.self, from: JSONSerialization.data(withJSONObject: raw)) }
        XCTAssertNil(try decode().dateien); XCTAssertTrue(try decode().canResume)
        raw["anbietergrenze"] = ["fortsetzbar": false]
        XCTAssertFalse(try decode().canResume)
    }

    func testUnverifiedGoalUsesCorePresentationAndKeepsTerminalResultAndFiles() throws {
        var raw: [String: Any] = ["id": runID, "aufgabe": "at-0123456789abcdef",
            "auftrag": "Vergleiche Optionen.", "zustand_code": "FAILED", "offen": false,
            "grund_code": "goal_unverified", "grund": "Eine Anforderung bleibt offen.",
            "ergebnis": "Die gefundenen Optionen stehen bereit.", "dateien": [self.raw()]]
        for title in ["Recherche unvollständig", "Abschluss nicht bestätigt"] {
            raw["zustand"] = title
            let run = try JSONDecoder().decode(AgentRun.self, from: JSONSerialization.data(withJSONObject: raw))
            XCTAssertTrue(try XCTUnwrap(run.failureNotice).hasPrefix(run.zustand + ". "))
            XCTAssertTrue(try XCTUnwrap(run.failureNotice).contains(try XCTUnwrap(run.grund)))
            XCTAssertEqual(run.zustand_code, "FAILED"); XCTAssertFalse(run.canResume)
            XCTAssertTrue(run.providerOptions.isEmpty)
            XCTAssertEqual(run.ergebnis, raw["ergebnis"] as? String)
            try XCTUnwrap(run.dateien?.first).verify(data: bytes, mimeType: "text/plain", runID: run.id)
        }
    }

    func testUnverifiedPresentationRequiresExactReasonAndFailedStateWithLegacyFallback() throws {
        var raw: [String: Any] = ["id": runID, "aufgabe": "at-0123456789abcdef",
            "auftrag": "Vergleiche Optionen.", "zustand": "Recherche unvollständig",
            "zustand_code": "FAILED", "offen": false]
        func decode() throws -> AgentRun {
            try JSONDecoder().decode(AgentRun.self, from: JSONSerialization.data(withJSONObject: raw))
        }
        let legacy = try decode()
        XCTAssertNil(legacy.grund_code)
        XCTAssertNotNil(legacy.failureNotice)
        for reason in ["goal_unverified_extra", "executor_failed", ""] {
            raw["grund_code"] = reason
            XCTAssertEqual(try decode().failureNotice, legacy.failureNotice)
        }
        raw["grund_code"] = "goal_unverified"
        XCTAssertNotEqual(try decode().failureNotice, legacy.failureNotice)
        for state in ["SUCCEEDED", "RUNNING", "WAITING_USER", "KILLED"] {
            raw["zustand_code"] = state
            XCTAssertNil(try decode().failureNotice)
        }
    }
}
