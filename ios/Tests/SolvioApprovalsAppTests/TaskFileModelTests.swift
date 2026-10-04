import Foundation
import XCTest
import SolvioApprovalsKit
@testable import SolvioApprovals

@MainActor
final class TaskFileModelTests: XCTestCase {
    private func folder() throws -> URL {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }
    func testMultipleTableSelectionAndExplicitRemoval() async throws {
        let root = try folder(); defer { try? FileManager.default.removeItem(at: root) }
        let urls = [root.appendingPathComponent("Juli.csv"), root.appendingPathComponent("August.csv")]
        for url in urls { try Data("Monat,Umsatz\nAugust,145\n".utf8).write(to: url) }
        let model = TaskStartModel(); model.objective = "Analysiere die beiden Tabellen."
        await model.selectFiles(urls)
        XCTAssertEqual(model.files?.request.files.map(\.name), ["Juli.csv", "August.csv"])
        XCTAssertNil(model.document); XCTAssertTrue(model.valid)
        XCTAssertFalse(model.sending); XCTAssertNil(model.accepted)
        model.clearDocument()
        XCTAssertNil(model.files); XCTAssertNil(model.document)
    }
    func testMixedSelectionCannotKeepPartialPreviousAttachment() async throws {
        let root = try folder(); defer { try? FileManager.default.removeItem(at: root) }
        let csv = root.appendingPathComponent("Tabelle.csv"), txt = root.appendingPathComponent("Text.txt")
        try Data("a,b\n1,2\n".utf8).write(to: csv); try Data("Vorhandener Text".utf8).write(to: txt)
        let model = TaskStartModel(); model.objective = "Analysiere die ausgewählten Dateien."
        await model.selectFiles([csv]); XCTAssertNotNil(model.files)
        await model.selectFiles([csv, txt])
        XCTAssertNil(model.files); XCTAssertNil(model.document)
        XCTAssertFalse(model.valid); XCTAssertFalse(model.documentError.isEmpty)
        await model.selectFiles([txt])
        XCTAssertNotNil(model.document); XCTAssertNil(model.files); XCTAssertTrue(model.valid)
    }
    func testChangedScopeDuringTableReadCannotAttachToOtherMode() async throws {
        let root = try folder(); defer { try? FileManager.default.removeItem(at: root) }
        let csv = root.appendingPathComponent("Tabelle.csv")
        try Data(repeating: 65, count: 1024 * 1024).write(to: csv)
        let model = TaskStartModel(); model.objective = "Analysiere diese Tabelle."
        let selection = Task { await model.selectFiles([csv]) }
        for _ in 0..<1000 {
            if model.documentLoading { break }
            await Task.yield()
        }
        XCTAssertTrue(model.documentLoading, "The selection must enter its actual asynchronous read")
        model.scope = "build"
        await selection.value
        XCTAssertNil(model.files); XCTAssertNil(model.document)
        XCTAssertFalse(model.documentLoading)
    }

    func testCancelledTableReadReleasesOnlyItsLoadingState() async throws {
        let root = try folder(); defer { try? FileManager.default.removeItem(at: root) }
        let csv = root.appendingPathComponent("Tabelle.csv")
        try Data(repeating: 65, count: AppTaskFileInput.byteLimit).write(to: csv)
        let model = TaskStartModel(); model.objective = "Analysiere diese Tabelle."
        let selection = Task { await model.selectFiles([csv]) }
        for _ in 0..<1000 {
            if model.documentLoading { break }
            await Task.yield()
        }
        XCTAssertTrue(model.documentLoading)
        selection.cancel(); await selection.value
        XCTAssertNil(model.files); XCTAssertNil(model.document)
        XCTAssertFalse(model.documentLoading)
    }
}
