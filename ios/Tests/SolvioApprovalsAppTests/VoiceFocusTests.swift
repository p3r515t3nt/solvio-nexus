import SwiftUI
import UIKit
import XCTest
@testable import SolvioApprovals

@MainActor
final class VoiceFocusTests: XCTestCase {
    private let id = "c-0123456789abcdef"

    private func detail(_ text: String, conversationID: String? = nil, historyCount: Int = 0) throws -> ConversationDetail {
        var messages: [[String: Any]] = (0..<historyCount).map { index in
            ["message_id": String(format: "m-%016x", index + 1), "sequence": index + 1,
             "role": "assistant", "text": "Früherer Beitrag \(index + 1). " + String(repeating: "Gespeicherter Verlauf. ", count: 8)]
        }
        messages.append(["message_id": String(format: "m-%016x", historyCount + 1),
                         "sequence": historyCount + 1, "role": "assistant", "text": text])
        let raw: [String: Any] = [
            "conversation": ["conversation_id": conversationID ?? id, "title": "Gespräch mit SOLVIO", "kind": "text"],
            "messages": messages,
            "auftraege": [], "deliveries_open": 0]
        return try JSONDecoder().decode(ConversationDetail.self, from: JSONSerialization.data(withJSONObject: raw))
    }
    private func bound(_ focus: VoiceFocus, _ chats: ConversationModel) {
        chats.select(id, remember: false)
        XCTAssertTrue(focus.bind(id, attempt: focus.present()))
    }

    func testWaitsForFlushAndANewReadAfterAnOlderPollBeforeReturningToSameChat() async throws {
        let focus = VoiceFocus(), chats = ConversationModel(), composer = MessageComposerModel()
        composer.text = "Mein ungesendeter Entwurf"
        let old = try detail("Alter Verlauf"), final = try detail("Gesprochene Antwort")
        bound(focus, chats)
        let poll = Gate(), flush = Gate(), fresh = Gate()
        var oldStarted = false, waitingForFlush = false, freshStarted = false
        let oldRead = Task { await chats.refreshDetail { _ in oldStarted = true; await poll.wait(); return old } }
        while !oldStarted { await Task.yield() }
        let ending = Task {
            await focus.finish(chats: chats, stillBound: { true },
                waitForClose: { waitingForFlush = true; await flush.wait() },
                load: { _ in freshStarted = true; await fresh.wait(); return final })
        }
        while !waitingForFlush { await Task.yield() }
        XCTAssertEqual(focus.phase, .finishing); XCTAssertFalse(freshStarted)
        await flush.open()
        try await Task.sleep(nanoseconds: 70_000_000)
        XCTAssertFalse(freshStarted, "old in-flight poll must finish before a new read starts")
        XCTAssertTrue(focus.isPresented)
        await poll.open(); await oldRead.value
        while !freshStarted { await Task.yield() }
        XCTAssertEqual(chats.detail?.messages.first?.text, "Alter Verlauf")
        XCTAssertTrue(focus.isPresented, "old history cannot dismiss the focus")
        await fresh.open(); await ending.value
        XCTAssertFalse(focus.isPresented)
        XCTAssertEqual(chats.selectedID, id)
        XCTAssertEqual(chats.detail?.messages.first?.text, "Gesprochene Antwort")
        XCTAssertEqual(composer.text, "Mein ungesendeter Entwurf")
    }

    func testUnknownKeepsWarningAndExplicitReturnDoesNotUnlockHandoff() async throws {
        let focus = VoiceFocus(), chats = ConversationModel()
        bound(focus, chats)
        var handoff = VoiceHandoff(); handoff.start(conversationID: id); handoff.fail()
        var read = false
        await focus.finish(chats: chats, stillBound: { true }, waitForClose: { throw VoiceHandoffError() },
            load: { _ in read = true; return try self.detail("unused") })
        XCTAssertEqual(focus.phase, .failed); XCTAssertTrue(focus.notice.contains("nicht bestätigt"))
        XCTAssertFalse(read)
        focus.dismiss() // Explicit return to the stored chat is NOT acceptance of its completeness.
        XCTAssertEqual(handoff.state, .unknown); XCTAssertTrue(handoff.blocksText(in: id))
    }

    func testFailedOrWrongConversationReadCannotClaimSuccessfulReturn() async throws {
        for wrongID in [false, true] {
            let focus = VoiceFocus(), chats = ConversationModel()
            bound(focus, chats)
            await chats.refreshDetail { _ in try self.detail("Alte Antwort") }
            await focus.finish(chats: chats, stillBound: { true }, waitForClose: {}, load: { _ in
                if !wrongID { throw ClientError.http(503) }
                return try self.detail("Falscher Chat", conversationID: "c-ffffffffffffffff")
            })
            XCTAssertEqual(focus.phase, .failed)
            XCTAssertEqual(chats.detail?.messages.first?.text, "Alte Antwort")
            XCTAssertTrue(focus.canRetryHistory)
            await focus.finish(chats: chats, stillBound: { true }, waitForClose: {},
                               load: { _ in try self.detail("Frischer Verlauf") })
            XCTAssertFalse(focus.isPresented)
            XCTAssertEqual(chats.detail?.messages.first?.text, "Frischer Verlauf")
        }
    }

    func testCancelledPreparationAndLateCompletionCannotReopenOrReplaceANewSelection() async throws {
        let focus = VoiceFocus(), chats = ConversationModel()
        let preparing = focus.present(); focus.dismiss()
        XCTAssertFalse(focus.bind(id, attempt: preparing)); XCTAssertFalse(focus.isPresented)
        bound(focus, chats)
        let read = Gate(); var reading = false
        let ending = Task { await focus.finish(chats: chats, stillBound: { chats.selectedID == self.id },
            waitForClose: {}, load: { _ in reading = true; await read.wait(); return try self.detail("Zu spät") }) }
        while !reading { await Task.yield() }
        focus.dismiss(); chats.select("c-ffffffffffffffff", remember: false)
        await read.open(); await ending.value
        XCTAssertFalse(focus.isPresented)
        XCTAssertEqual(chats.selectedID, "c-ffffffffffffffff"); XCTAssertNil(chats.detail)
    }

    func testConfirmedCloseWhileLockedDefersReadUntilForeground() async throws {
        let focus = VoiceFocus(), chats = ConversationModel(), composer = MessageComposerModel()
        composer.text = "Bleibt erhalten"; bound(focus, chats)
        focus.setForeground(false)
        var reads = 0, receipts = 0
        await focus.finish(chats: chats, stillBound: { true }, waitForClose: { receipts += 1 },
            load: { _ in reads += 1; return try self.detail("unused") })
        XCTAssertEqual(receipts, 1); XCTAssertEqual(reads, 0)
        XCTAssertEqual(focus.phase, .waitingForForeground); XCTAssertTrue(focus.notice.isEmpty)
        focus.setForeground(true)
        await focus.finish(chats: chats, stillBound: { true }, waitForClose: {},
            load: { _ in reads += 1; return try self.detail("Gespeicherte Sprache") })
        XCTAssertEqual(reads, 1); XCTAssertFalse(focus.isPresented)
        XCTAssertEqual(chats.detail?.messages.last?.text, "Gespeicherte Sprache")
        XCTAssertEqual(composer.text, "Bleibt erhalten")
    }

    func testLockDuringReadDefersFailureAndFreshForegroundReadRecovers() async throws {
        let focus = VoiceFocus(), chats = ConversationModel(); bound(focus, chats)
        let gate = Gate(); var started = false
        let finishing = Task { await focus.finish(chats: chats, stillBound: { true }, waitForClose: {},
            load: { _ in started = true; await gate.wait(); throw URLError(.networkConnectionLost) }) }
        while !started { await Task.yield() }
        focus.setForeground(false); await gate.open(); await finishing.value
        XCTAssertEqual(focus.phase, .waitingForForeground); XCTAssertTrue(focus.notice.isEmpty)
        focus.setForeground(true)
        await focus.finish(chats: chats, stillBound: { true }, waitForClose: {},
            load: { _ in try self.detail("Nach Entsperren") })
        XCTAssertFalse(focus.isPresented)
        XCTAssertEqual(chats.detail?.messages.last?.text, "Nach Entsperren")
    }

    func testOnlyTransientReadsRetryAndRetriesAreBounded() async throws {
        for (failure, expected) in [(URLError(.networkConnectionLost) as Error, 3), (ClientError.http(401), 1),
                                    (ClientError.http(404), 1), (ClientError.decode, 1)] {
            let focus = VoiceFocus(), chats = ConversationModel(); bound(focus, chats)
            var reads = 0, delays: [UInt64] = []
            await focus.finish(chats: chats, stillBound: { true }, waitForClose: {},
                retryDelay: { delays.append($0) }, load: { _ in reads += 1; throw failure })
            XCTAssertEqual(reads, expected); XCTAssertEqual(delays.count, expected - 1)
            XCTAssertEqual(focus.phase, .failed); XCTAssertTrue(focus.canRetryHistory)
        }
        let focus = VoiceFocus(), chats = ConversationModel(); bound(focus, chats)
        var reads = 0, closes = 0
        await focus.finish(chats: chats, stillBound: { true }, waitForClose: { closes += 1 }, retryDelay: { _ in },
            load: { _ in reads += 1; if reads == 1 { throw URLError(.notConnectedToInternet) }; return try self.detail("Recovered") })
        XCTAssertFalse(focus.isPresented); XCTAssertEqual(reads, 2); XCTAssertEqual(closes, 1)
    }

    func testForegroundNeverConvertsMissingReceiptOrChangedBindingToSuccess() async throws {
        let focus = VoiceFocus(), chats = ConversationModel(); bound(focus, chats)
        focus.setForeground(false)
        await focus.finish(chats: chats, stillBound: { true }, waitForClose: { throw VoiceHandoffError() },
            load: { _ in XCTFail("No read without receipt"); return try self.detail("unused") })
        focus.setForeground(true)
        XCTAssertEqual(focus.phase, .failed); XCTAssertFalse(focus.canRetryHistory)
        XCTAssertTrue(focus.notice.contains("nicht bestätigt"))
        bound(focus, chats); focus.setForeground(false)
        await focus.finish(chats: chats, stillBound: { true }, waitForClose: {}, load: { _ in try self.detail("unused") })
        chats.select("c-ffffffffffffffff", remember: false); focus.setForeground(true)
        await focus.finish(chats: chats, stillBound: { false }, waitForClose: {},
            load: { _ in XCTFail("Old binding must not read"); return try self.detail("unused") })
        XCTAssertNil(chats.detail); XCTAssertEqual(focus.phase, .failed)
    }

    func testStartFailureDoesNotClaimAnIncompleteConversationOrStartAgain() {
        let focus = VoiceFocus()
        focus.present(); focus.startFailed("Mikrofon nicht freigegeben.")
        XCTAssertEqual(focus.phase, .failed); XCTAssertNil(focus.conversationID)
        focus.dismiss(); XCTAssertFalse(focus.isPresented)
    }

    func testActualFocusLayoutsAndConfirmedEndRevealSameChat() async throws {
        let chats = ConversationModel(); chats.select(id, remember: false)
        await chats.refreshDetail { _ in try self.detail("Dieser gespeicherte Verlauf erscheint nach dem Gespräch.") }
        let scenarios: [(String, VoiceState, Bool, CGSize, DynamicTypeSize)] = [
            ("connecting", .ready, false, .init(width: 390, height: 844), .large),
            ("listening-small", .listening, true, .init(width: 320, height: 568), .large),
            ("thinking", .thinking, true, .init(width: 390, height: 844), .large),
            ("speaking", .speaking, true, .init(width: 390, height: 844), .large),
            ("muted-accessible", .listening, false, .init(width: 320, height: 568), .accessibility3)]
        for (name, state, mic, size, type) in scenarios {
            var home = HomeView(model: AppModel.visualPreview(), open: { _ in }, chats: chats)
            let status: String
            switch state {
            case .ready: status = "Sprache wird verbunden …"
            case .listening: status = mic ? "SOLVIO hört zu" : "Stummgeschaltet"
            case .thinking: status = "SOLVIO denkt nach"
            default: status = "SOLVIO spricht"
            }
            home.previewPresence = .voice(state: state, status: status, audioLevel: 0.7, microphoneActive: mic)
            let window = try await host(home, size: size, type: type)
            XCTAssertTrue(descendants(window).compactMap { $0 as? UITextView }.isEmpty)
            attach(window, name: "voice-focus-\(name)"); window.isHidden = true
        }
        let focus = VoiceFocus(); bound(focus, chats)
        let close = Gate(); var waiting = false
        let ending = Task { await focus.finish(chats: chats, stillBound: { true },
            waitForClose: { waiting = true; await close.wait() }, load: { _ in try self.detail("Das gesprochene Ergebnis.", historyCount: 12) }) }
        while !waiting { await Task.yield() }
        let composer = MessageComposerModel(); composer.text = "Dieser Entwurf bleibt erhalten."
        let home = HomeView(model: AppModel.visualPreview(), open: { _ in }, chats: chats,
                            voiceFocus: focus, composer: composer)
        let window = try await host(home, size: .init(width: 390, height: 844), type: .large)
        XCTAssertTrue(descendants(window).compactMap { $0 as? UITextView }.isEmpty)
        attach(window, name: "voice-focus-ending")
        await close.open(); await ending.value
        try await Task.sleep(nanoseconds: 400_000_000)
        XCTAssertFalse(descendants(window).compactMap { $0 as? UITextView }.isEmpty,
                       "confirmed end plus fresh read must restore the actual text composer")
        XCTAssertEqual(chats.selectedID, id); XCTAssertEqual(chats.detail?.messages.last?.text, "Das gesprochene Ergebnis.")
        XCTAssertEqual(composer.text, "Dieser Entwurf bleibt erhalten.")
        XCTAssertEqual(descendants(window).compactMap { $0 as? UITextView }.first?.text, composer.text)
        let transcript = try XCTUnwrap(descendants(window).compactMap { $0 as? UIScrollView }
            .first { !($0 is UITextView) && $0.contentSize.height > $0.bounds.height * 2 })
        XCTAssertGreaterThan(transcript.contentOffset.y, 0, "long history must reveal its final speech contribution")
        XCTAssertLessThanOrEqual(transcript.contentSize.height - transcript.contentOffset.y - transcript.bounds.height,
                                 transcript.adjustedContentInset.bottom + 10, "the final chat anchor is in view")
        attach(window, name: "voice-focus-returned-long-chat"); window.isHidden = true
        bound(focus, chats)
        await focus.finish(chats: chats, stillBound: { true }, waitForClose: { throw VoiceHandoffError() },
                           load: { _ in try self.detail("unused") })
        let errorWindow = try await host(home, size: .init(width: 320, height: 568), type: .large)
        attach(errorWindow, name: "voice-focus-unknown"); errorWindow.isHidden = true
    }

    private func host(_ home: HomeView, size: CGSize, type: DynamicTypeSize) async throws -> UIWindow {
        let controller = UIHostingController(rootView: NavigationStack { home }.environment(\.dynamicTypeSize, type).environment(\.scenePhase, .active))
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.first as? UIWindowScene)
        let window = UIWindow(windowScene: scene)
        window.frame = .init(origin: .zero, size: size); window.windowLevel = .alert + 1
        window.rootViewController = controller; window.makeKeyAndVisible(); controller.view.frame = window.bounds
        try await Task.sleep(nanoseconds: 500_000_000); controller.view.layoutIfNeeded()
        return window
    }
    private func descendants(_ view: UIView) -> [UIView] { view.subviews.flatMap { [$0] + descendants($0) } }
    private func attach(_ view: UIView, name: String) {
        let image = UIGraphicsImageRenderer(bounds: view.bounds).image { context in view.layer.render(in: context.cgContext) }
        let attachment = XCTAttachment(image: image); attachment.name = name; attachment.lifetime = .keepAlways; add(attachment)
    }
}
