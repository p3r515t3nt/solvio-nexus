import Foundation
import XCTest
@testable import SolvioApprovalsKit

final class TaskFileTests: XCTestCase {
    private let sample = Data("a,b\n1,2\n".utf8)
    private func body(_ request: AppTaskFileRequest) throws -> AppTaskBody {
        try AppTaskBody(scope: "research", objective: "Analysiere die angehängte Tabelle.",
            targetRepo: "", requestID: "file-request-001", fileRequest: request)
    }
    func testActualCoreDigestMatchesAndAllFilesSurviveRoundtrip() throws {
        let request = try AppTaskFileRequest(files: [AppTaskFileInput(name: "Daten.csv", content: sample)])
        let task = try body(request)
        XCTAssertEqual(task.requestDigest, "cc6741a9f66b3d66663b1f4ffca9eb2ec6c741692ca6e1500f0d7e44edc1f8b6")
        let decoded = try JSONDecoder().decode(AppTaskBody.self, from: JSONEncoder().encode(task))
        XCTAssertEqual(task, decoded)
        XCTAssertNil(decoded.document_request)
        XCTAssertEqual(decoded.file_request?.files[0].content, sample)
    }
    func testChangedNamesBytesOrderAndRemovedFilesInvalidateRetry() throws {
        let a = try AppTaskFileInput(name: "Juli.csv", content: sample)
        let b = try AppTaskFileInput(name: "August.csv", content: sample)
        let original = try body(AppTaskFileRequest(files: [a, b]))
        let retry = TaskStartRetryBinding(body: original, coreID: "core", deviceID: "phone")
        for files in [[b, a], [a], [a, try AppTaskFileInput(name: "August.csv", content: Data("a,b\n8,9\n".utf8))]] {
            let request = try AppTaskFileRequest(files: files)
            XCTAssertNotEqual(try body(request).requestDigest, original.requestDigest)
            XCTAssertNil(retry.restore(scope: original.scope, objective: original.objective, targetRepo: "",
                coreID: "core", deviceID: "phone", fileRequest: request))
        }
        XCTAssertNotNil(retry.restore(scope: original.scope, objective: original.objective, targetRepo: "",
            coreID: "core", deviceID: "phone", fileRequest: original.file_request))
        let saved = String(decoding: try JSONEncoder().encode(retry), as: UTF8.self)
        XCTAssertFalse(saved.contains("Juli.csv")); XCTAssertFalse(saved.contains("YSxi"))
        XCTAssertEqual(retry.attachmentKind, "files")
        XCTAssertEqual(try JSONDecoder().decode(TaskStartRetryBinding.self,
            from: JSONEncoder().encode(retry)).attachmentKind, "files")
    }
    func testExactClosedWireRejectsPathsNullAndAlternateEncoding() throws {
        for raw in [
            #"{"operation":"process_files","files":[{"name":"a.csv","content_b64":"eA==","path":"/tmp/a"}]}"#,
            #"{"operation":"process_files","files":[{"name":"a.csv","content_b64":"eB=="}]}"#,
            #"{"operation":"process_files","files":[]}"#,
            #"{"operation":"other","files":[{"name":"a.csv","content_b64":"eA=="}]}"#
        ] { XCTAssertThrowsError(try JSONDecoder().decode(AppTaskFileRequest.self, from: Data(raw.utf8))) }
        let task = try body(AppTaskFileRequest(files: [AppTaskFileInput(name: "Daten.csv", content: sample)]))
        var wire = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(task)) as? [String: Any])
        wire["file_request"] = NSNull()
        XCTAssertThrowsError(try JSONDecoder().decode(AppTaskBody.self, from: JSONSerialization.data(withJSONObject: wire)))
        XCTAssertThrowsError(try AppTaskBody(scope: "build", objective: task.objective, targetRepo: "/tmp/repo",
            requestID: task.client_request_id, fileRequest: task.file_request))
        XCTAssertThrowsError(try AppTaskBody(scope: "research", objective: task.objective, targetRepo: "",
            requestID: task.client_request_id, documentRequest: AppTaskDocumentRequest(format: "txt", content: Data("Hallo".utf8)),
            fileRequest: task.file_request))
    }
    func testFileNamesCountAndTotalBytesAreBounded() throws {
        for name in ["../x.csv", "/a.csv", "x\\a.csv", "a:csv", "a\0.csv", " x.csv", "x.csv."] {
            XCTAssertThrowsError(try AppTaskFileInput(name: name, content: sample))
        }
        let input = try AppTaskFileInput(name: "a.csv", content: sample)
        XCTAssertThrowsError(try AppTaskFileRequest(files: [input, input]))
        XCTAssertThrowsError(try AppTaskFileRequest(files: (0..<5).map { try AppTaskFileInput(name: "\($0).csv", content: sample) }))
        let exact = try AppTaskFileInput(name: "exact.csv", content: Data(repeating: 1, count: AppTaskFileInput.byteLimit))
        XCTAssertEqual(try AppTaskFileRequest(files: [exact]).byteCount, AppTaskFileInput.byteLimit)
        XCTAssertThrowsError(try AppTaskFileRequest(files: [exact, input]))
    }
    func testLocalSelectionPreservesActualBytesAndRefusesLinks() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let first = root.appendingPathComponent("one.csv"), second = root.appendingPathComponent("two.xlsx")
        try sample.write(to: first); try Data("synthetic second input".utf8).write(to: second)
        let selection = try TaskFileSelection.read([first, second])
        XCTAssertEqual(selection.request.files.map(\.name), ["one.csv", "two.xlsx"])
        XCTAssertEqual(selection.request.files[0].content, sample)
        let link = root.appendingPathComponent("link.csv")
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: first)
        XCTAssertThrowsError(try TaskFileSelection.read([link]))
        XCTAssertThrowsError(try TaskFileSelection.read([first, root.appendingPathComponent("not-a-table.txt")]))
    }

    func testDecomposedPickerNameIsCanonicalButUnchangedWireSpellingIsRefused() throws {
        let name = "Cafe\u{301}.csv"
        XCTAssertThrowsError(try AppTaskFileInput(name: name, content: sample))
        let raw = try JSONSerialization.data(withJSONObject: ["name": name, "content_b64": sample.base64EncodedString()])
        XCTAssertThrowsError(try JSONDecoder().decode(AppTaskFileInput.self, from: raw))
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let url = root.appendingPathComponent(name)
        try sample.write(to: url)
        let selection = try TaskFileSelection.read([url])
        XCTAssertEqual(Array(selection.request.files[0].name.utf8), Array("Café.csv".utf8))
        XCTAssertEqual(selection.request.files[0].content, sample)
    }
}
