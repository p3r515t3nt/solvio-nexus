import XCTest
import SwiftUI
import SolvioApprovalsKit
@testable import SolvioApprovals

@MainActor
final class AgentResultsModelTests: XCTestCase {
    private let id = "ar-0123456789abcdef"
    private func fixtureRun(_ id: String = "ar-0123456789abcdef", state: String = "RUNNING") throws -> AgentRun {
        let raw: [String: Any] = ["id": id, "aufgabe": "at-0123456789abcdef", "auftrag": "Erstelle ein Ergebnis.",
            "zustand": "In Arbeit", "zustand_code": state, "offen": state == "RUNNING"]
        return try JSONDecoder().decode(AgentRun.self, from: JSONSerialization.data(withJSONObject: raw))
    }
    func testLateReplyAfterLeavingViewDoesNotRepopulateResult() async throws {
        let model = AgentResultsModel(), value = try fixtureRun()
        var resume: CheckedContinuation<AgentRun, Never>?
        let task = Task { await model.refreshDetail(runID: id) { await withCheckedContinuation { resume = $0 } } }
        while resume == nil { await Task.yield() }
        model.invalidate(); resume?.resume(returning: value); await task.value
        XCTAssertNil(model.detail); XCTAssertFalse(model.loading)
    }
    func testWrongRunReplyIsNotDisplayedAndReadFailureIsExplicit() async throws {
        let model = AgentResultsModel()
        await model.refreshDetail(runID: id) { try self.fixtureRun("ar-aaaaaaaaaaaaaaaa") }
        XCTAssertNil(model.detail); XCTAssertFalse(model.error.isEmpty)
        await model.refreshDetail(runID: id) { try self.fixtureRun() }
        XCTAssertEqual(model.detail?.id, id); XCTAssertTrue(model.error.isEmpty)
        await model.refreshDetail(runID: id) { throw URLError(.notConnectedToInternet) }
        XCTAssertEqual(model.detail?.id, id); XCTAssertFalse(model.error.isEmpty)
    }
    func testRepeatedCancelWhileInFlightCallsExactlyOneBoundAction() async throws {
        let model = AgentResultsModel(); await model.refreshDetail(runID: id) { try self.fixtureRun() }
        var resume: CheckedContinuation<Void, Never>?, calls = 0
        let first = Task { await model.act(runID: id, action: "cancel") {
            calls += 1; await withCheckedContinuation { resume = $0 }
        } }
        while resume == nil { await Task.yield() }
        await model.act(runID: id, action: "cancel") { calls += 1 }
        await model.act(runID: "ar-aaaaaaaaaaaaaaaa", action: "cancel") { calls += 1 }
        resume?.resume(); await first.value
        XCTAssertEqual(calls, 1); XCTAssertFalse(model.working)
        XCTAssertTrue(model.actionMessage.contains("Abbruch bestätigt"))
    }
    func testTerminalResultCannotCancelOrResumeAndFailedActionIsNotSuccess() async throws {
        let model = AgentResultsModel(); var calls = 0
        await model.refreshDetail(runID: id) { try self.fixtureRun(state: "SUCCEEDED") }
        await model.act(runID: id, action: "cancel") { calls += 1 }
        await model.act(runID: id, action: "resume") { calls += 1 }
        XCTAssertEqual(calls, 0)
        await model.refreshDetail(runID: id) { try self.fixtureRun() }
        await model.act(runID: id, action: "cancel") { throw URLError(.timedOut) }
        XCTAssertFalse(model.actionMessage.contains("Abbruch bestätigt"))
        XCTAssertTrue(model.actionMessage.contains("nicht bestätigt"))
    }

    func testLeavingDuringActionDiscardsLateMessageAndClearsBusyState() async throws {
        let model = AgentResultsModel(); await model.refreshDetail(runID: id) { try self.fixtureRun() }
        var resume: CheckedContinuation<Void, Never>?
        let pending = Task { await model.act(runID: id, action: "cancel") {
            await withCheckedContinuation { resume = $0 }
        } }
        while resume == nil { await Task.yield() }
        XCTAssertTrue(model.working)
        model.invalidate(); resume?.resume(); await pending.value
        XCTAssertFalse(model.working); XCTAssertTrue(model.actionMessage.isEmpty)
        XCTAssertNil(model.detail)
    }

    func testNoticeOpensOnlyAnExplicitValidCoreRunAndKeepsOldNoticesReadable() throws {
        var raw: [String: Any] = ["id": "notice-fixture", "zusammenfassung": "Eine Datei steht bereit.",
            "dringlichkeit": "normal", "aufgabe": "", "gelesen": false, "quelle": "SOLVIO"]
        func decode() throws -> InboxItem {
            try JSONDecoder().decode(InboxItem.self, from: JSONSerialization.data(withJSONObject: raw))
        }
        XCTAssertNil(try decode().agentRunID)
        raw["lauf"] = id
        XCTAssertEqual(try decode().agentRunID, id)
        for invalid in ["https://example.invalid/", "../" + id, id + "?action=start", "approval-123"] {
            raw["lauf"] = invalid; XCTAssertNil(try decode().agentRunID)
        }
    }

    func testIsolatedResultScreensRenderAtNarrowWidthsWithoutAConnection() async throws {
        let app = AppModel.visualPreview()
        guard app.client == nil else { throw XCTSkip("Visual fixture requires an unpaired simulator") }
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.first as? UIWindowScene)
        let animations = UIView.areAnimationsEnabled
        UIView.setAnimationsEnabled(false)
        defer { UIView.setAnimationsEnabled(animations) }
        for (width, state) in [(390.0, "SUCCEEDED"), (320.0, "FAILED")] {
            let model = AgentResultsModel(), fileID = "aa-0123456789abcdef"
            let raw: [String: Any] = ["id": id, "aufgabe": "at-0123456789abcdef",
                "auftrag": "Eine Datei für meinen Ausflug erstellen.", "zustand_code": state,
                "zustand": state == "SUCCEEDED" ? "Fertig" : "Fehlgeschlagen", "offen": false,
                "ergebnis": "Dein Ausflugsplan steht als Datei bereit.",
                "grund": "Ein zusätzlicher Prüfschritt konnte nicht abgeschlossen werden.",
                "dateien": [["id": fileID, "name": "Ausflugsplan.txt", "mime_type": "text/plain",
                    "size": 120, "sha256": String(repeating: "a", count: 64), "preview_kind": "text",
                    "download_url": "/v1/agent/runs/\(id)/artifacts/\(fileID)/download",
                    "preview_url": "/v1/agent/runs/\(id)/artifacts/\(fileID)/preview"]]]
            let run = try JSONDecoder().decode(AgentRun.self, from: JSONSerialization.data(withJSONObject: raw))
            await model.refreshDetail(runID: id) { run }
            let controller = UIHostingController(rootView: NavigationStack {
                AgentResultView(app: app, runID: id, model: model)
            }.preferredColorScheme(.dark))
            let window = UIWindow(windowScene: scene)
            window.frame = CGRect(x: 0, y: 0, width: width, height: 844)
            window.rootViewController = controller; window.makeKeyAndVisible()
            try await Task.sleep(nanoseconds: 100_000_000)
            controller.view.frame = window.bounds; controller.view.layoutIfNeeded()
            let image = UIGraphicsImageRenderer(size: window.bounds.size).image { _ in
                controller.view.drawHierarchy(in: window.bounds, afterScreenUpdates: true)
            }
            let attachment = XCTAttachment(image: image)
            attachment.name = "result-\(state)-\(Int(width))"; attachment.lifetime = .keepAlways
            add(attachment)
            XCTAssertEqual(model.detail?.id, id); XCTAssertNil(app.client)
            window.isHidden = true; window.rootViewController = nil
        }
    }
}
