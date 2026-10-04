import XCTest
import SwiftUI
import SolvioApprovalsKit
@testable import SolvioApprovals

@MainActor
final class AssistantSectionsTests: XCTestCase {
    func testSuggestionNeverReplacesDraftAndDoesNotSend() {
        let composer = MessageComposerModel()
        XCTAssertTrue(composer.stageSuggestion("Prüfe meinen Tag"))
        XCTAssertFalse(composer.stageSuggestion("Andere Idee"))
        XCTAssertEqual(composer.text, "Prüfe meinen Tag")
        XCTAssertFalse(composer.sending)
        XCTAssertNil(composer.lastAccepted)
    }
    func testLibrarySearchFindsFileAndKeepsIncompleteResultStatus() throws {
        let raw = """
        {"id":"ar-0123456789abcdef","aufgabe":"at-0123456789abcdef","auftrag":"Vergleiche Angebote",
        "zustand":"Unvollständig","zustand_code":"FAILED","offen":false,"ergebnis":"Eine Quelle fehlt.",
        "dateien":[{"id":"aa-0123456789abcdef","name":"Vergleich.pdf","mime_type":"application/pdf","size":1,
        "sha256":"abc","download_url":"/invalid","preview_kind":"none"}]}
        """
        let run = try JSONDecoder().decode(AgentRun.self, from: Data(raw.utf8))
        XCTAssertEqual(AssistantLibraryView.entries([run], search: "VERGLEICH.PDF", filesOnly: true).first?.zustand_code, "FAILED")
        XCTAssertEqual(AssistantLibraryView.entries([run], search: "Quelle", filesOnly: false).count, 1)
        XCTAssertTrue(AssistantLibraryView.entries([run], search: "unbekannt", filesOnly: false).isEmpty)
    }

    private func ideaRun(_ number: Int, task: Int? = nil, state: String = "SUCCEEDED", open: Bool = false,
                         created: Double? = 10000000, revision: Int = 1, result: String = "Ergebnis") throws -> AgentRun {
        let raw: [String: Any] = ["id": String(format: "ar-%016x", number), "aufgabe": String(format: "at-%016x", task ?? number),
            "auftrag": "Synthetischer Auftrag", "zustand": state, "zustand_code": state, "offen": open,
            "angelegt": created as Any? ?? NSNull(), "ergebnis": result,
            "task_revision": ["revision": revision, "digest": String(repeating: "a", count: 64), "text": "Auftrag", "parent_run_id": revision == 1 ? "" : "ar-0000000000000001"]]
        return try JSONDecoder().decode(AgentRun.self, from: JSONSerialization.data(withJSONObject: raw))
    }

    func testPersonalIdeasUseLatestRevisionRankQuestionsAndBindExactSource() throws {
        let old = try ideaRun(1, state: "WAITING_USER", open: true)
        let newer = try ideaRun(2, task: 1, state: "RUNNING", open: true, revision: 2)
        let question = try ideaRun(3, state: "WAITING_USER", open: true, created: nil)
        let approval = try ideaRun(4, state: "WAITING_APPROVAL", open: true)
        let blocked = try ideaRun(5, state: "FAILED")
        let result = try ideaRun(6)
        let ideas = PersonalAssistantIdea.select([old, result, newer, blocked, approval, question], now: 10000001)
        XCTAssertEqual(ideas.map(\.id), [question.id, approval.id, blocked.id, result.id])
        XCTAssertEqual(ideas.map(\.action), ["Frage ansehen", "Auftrag ansehen", "Hindernis ansehen", "Ergebnis ansehen"])
        XCTAssertEqual(ideas.last?.run.ergebnis, "Ergebnis")
    }

    func testPersonalIdeasExcludeCancelledOldUnknownAndEmptyResultsAndLimitFour() throws {
        let omitted = try [ideaRun(1, state: "CANCELLED"), ideaRun(2, state: "KILLED"),
            ideaRun(3, state: "FAILED", created: 1), ideaRun(4, created: nil), ideaRun(5, result: "  "),
            ideaRun(6, created: 10000002), ideaRun(7, state: "WAITING_USER", open: false)]
        XCTAssertTrue(PersonalAssistantIdea.select(omitted, now: 10000001).isEmpty)
        let recent = try (10...16).map { try ideaRun($0, created: Double(10000000 - $0)) }
        XCTAssertEqual(PersonalAssistantIdea.select(recent.reversed(), now: 10000001).map(\.id), Array(recent.prefix(4)).map(\.id))
    }

    func testIdeasRefreshInvalidationDropsLatePrivateResponse() async throws {
        let model = AgentResultsModel(), run = try ideaRun(1, state: "WAITING_USER", open: true)
        await model.refreshList { model.invalidate(); return [run] }
        XCTAssertTrue(PersonalAssistantIdea.select(model.runs).isEmpty)
        XCTAssertFalse(model.loading)
    }

    func testPersonalIdeaLayoutsInSmallAndAccessibleViews() async throws {
        for (name, size, type) in [("small", CGSize(width: 320, height: 568), DynamicTypeSize.large),
                                   ("regular", CGSize(width: 390, height: 844), DynamicTypeSize.large),
                                   ("accessible", CGSize(width: 390, height: 844), DynamicTypeSize.accessibility3)] {
            let model = AgentResultsModel(), app = AppModel.visualPreview()
            let view = AssistantIdeasView(app: app, model: model, notice: "", prepare: { _ in XCTFail("No action during rendering") })
            let controller = UIHostingController(rootView: NavigationStack { view }.environment(\.dynamicTypeSize, type))
            let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.first as? UIWindowScene)
            let window = UIWindow(windowScene: scene)
            window.frame = .init(origin: .zero, size: size); window.windowLevel = .alert + 1
            window.rootViewController = controller; window.makeKeyAndVisible(); controller.view.frame = window.bounds
            try await Task.sleep(nanoseconds: 300_000_000)
            await model.refreshList { [try self.ideaRun(1, state: "WAITING_USER", open: true)] }
            try await Task.sleep(nanoseconds: 400_000_000); controller.view.layoutIfNeeded()
            XCTAssertEqual(PersonalAssistantIdea.select(model.runs).count, 1)
            let image = UIGraphicsImageRenderer(bounds: window.bounds).image { context in window.layer.render(in: context.cgContext) }
            let attachment = XCTAttachment(image: image); attachment.name = "personal-ideas-" + name
            attachment.lifetime = .keepAlways; add(attachment); window.isHidden = true
        }
    }

    private func page(_ ids: [String], next: String? = nil) throws -> AgentRunPage {
        let rows = ids.map { ["id": $0, "aufgabe": "at-0123456789abcdef", "auftrag": "Älteres Ergebnis", "zustand": "Fertig", "zustand_code": "SUCCEEDED", "offen": false, "ergebnis": "Ergebnis"] as [String: Any] }
        return try JSONDecoder().decode(AgentRunPage.self, from: JSONSerialization.data(withJSONObject: ["laeufe": rows, "next_before": next as Any? ?? NSNull()]))
    }

    func testLibraryKeepsOlderRevisionsAndRetryKeepsEarlierPage() async throws {
        let model = AgentLibraryModel(), a = "ar-1111111111111111", b = "ar-2222222222222222"
        await model.load { cursor in XCTAssertNil(cursor); return try self.page([b], next: b) }
        await model.load(older: true) { cursor in XCTAssertEqual(cursor, b); throw ClientError.http(503) }
        XCTAssertEqual(model.runs.map(\.id), [b]); XCTAssertEqual(model.next, b); XCTAssertFalse(model.error.isEmpty)
        await model.load(older: true) { cursor in XCTAssertEqual(cursor, b); return try self.page([a]) }
        XCTAssertEqual(model.runs.map(\.id), [b, a]); XCTAssertNil(model.next); XCTAssertEqual(model.error, "")
    }

    func testLibraryDiscardsReplyAfterInvalidationAndRejectsLoopingCursor() async throws {
        let model = AgentLibraryModel(), a = "ar-1111111111111111"
        await model.load { _ in model.invalidate(); return try self.page([a], next: a) }
        XCTAssertTrue(model.runs.isEmpty); XCTAssertNil(model.next); XCTAssertFalse(model.loading)
        await model.load { _ in try self.page([a], next: a) }
        await model.load(older: true) { _ in try self.page([a], next: a) }
        XCTAssertFalse(model.error.isEmpty); XCTAssertEqual(model.runs.count, 1)
    }
    func testLibraryNavigationKeepsPagesButAccountAndSearchChangesClearThem() async throws {
        let model = AgentLibraryModel(), a = "ar-1111111111111111", b = "ar-2222222222222222"
        model.select(query: "", identity: "owner-a")
        await model.load { _ in try self.page([b], next: b) }
        await model.load(older: true) { _ in try self.page([a]) }
        model.cancelLoading()
        model.select(query: "", identity: "owner-a")
        XCTAssertEqual(model.runs.map(\.id), [b, a]); XCTAssertTrue(model.loaded)
        model.select(query: "älter", identity: "owner-a")
        XCTAssertTrue(model.runs.isEmpty); XCTAssertFalse(model.loaded)
        await model.load { _ in try self.page([a]) }
        model.select(query: "älter", identity: "owner-b")
        XCTAssertTrue(model.runs.isEmpty); XCTAssertFalse(model.loaded)
    }

    func testLibraryIgnoresSearchReplyAfterNavigationCancellation() async throws {
        let model = AgentLibraryModel(), a = "ar-1111111111111111"
        await model.load { _ in
            model.cancelLoading()
            return try self.page([a])
        }
        XCTAssertTrue(model.runs.isEmpty); XCTAssertFalse(model.loaded); XCTAssertFalse(model.loading)
        await model.load { _ in try self.page([a]) }
        XCTAssertTrue(model.loaded)
    }

    func testSearchContinuesAcrossNonmatchingHistoryPages() async throws {
        let model = AgentLibraryModel(), a = "ar-1111111111111111", b = "ar-2222222222222222"
        model.select(query: "alt", identity: "owner")
        var calls = 0
        await model.load { cursor in
            calls += 1
            if cursor == nil { return try self.page([], next: b) }
            XCTAssertEqual(cursor, b)
            return try self.page([a])
        }
        XCTAssertEqual(calls, 2); XCTAssertEqual(model.runs.map(\.id), [a]); XCTAssertTrue(model.loaded)
    }

}
