import XCTest
@testable import SolvioApprovals

final class ChatPresenceTests: XCTestCase {
    func testOnlyConfirmedCurrentTextWorkAnimates() throws {
        XCTAssertEqual(text(try detail()).state, .idle)
        XCTAssertEqual(text(try detail(delivery: "running")).state, .thinking)
        XCTAssertEqual(text(try detail(delivery: "accepted")).state, .thinking)
        for state in ["PLANNING", "RUNNING", "WAITING_SPECIALIST", "VERIFYING"] {
            XCTAssertEqual(text(try detail(run: state)).state, .deepWork, state)
        }
        for state in ["WAITING_USER", "WAITING_APPROVAL", "WAITING_CAPABILITY", "CREATED"] {
            let waiting = text(try detail(run: state))
            XCTAssertEqual(waiting.state, .idle, state)
            XCTAssertEqual(waiting.title, "Ein Auftrag wartet")
        }
        for state in ["SUCCEEDED", "FAILED", "CANCELLED"] {
            XCTAssertEqual(text(try detail(run: state, open: false)).title, "SOLVIO ist bereit")
        }
        XCTAssertEqual(text(try detail(delivery: "blocked")).state, .idle)
    }

    func testOfflineOrUnconfirmedHistoryNeverClaimsCurrentWork() throws {
        let working = try detail(run: "RUNNING")
        XCTAssertEqual(ChatPresencePresentation.text(detail: working, sending: false, reachable: false,
                                                    detailUnavailable: false).state, .offline)
        let stale = ChatPresencePresentation.text(detail: working, sending: false, reachable: true, detailUnavailable: true)
        XCTAssertEqual(stale.state, .idle)
        XCTAssertEqual(stale.title, "Chatstand nicht bestätigt")
        let sending = ChatPresencePresentation.text(detail: nil, sending: true, reachable: true, detailUnavailable: false)
        XCTAssertEqual(sending.state, .idle, "Transport is not model thinking")
        XCTAssertEqual(sending.title, "Nachricht wird übermittelt …")
    }

    func testVoiceUsesExistingMappingAndOnlyCurrentAudio() {
        for (state, expected) in [(VoiceState.listening, PresenceState.listening), (.thinking, .thinking),
                                 (.speaking, .speaking), (.deepWork, .deepWork), (.reconnecting, .reconnecting),
                                 (.offline("x"), .offline), (.ended, .ended)] {
            let shown = ChatPresencePresentation.voice(state: state, status: "actual", audioLevel: 0.6, microphoneActive: true)
            XCTAssertEqual(shown.state, expected)
            XCTAssertEqual(shown.title, "actual")
            XCTAssertEqual(shown.audioLevel, state == .listening || state == .speaking ? 0.6 : 0)
        }
        let muted = ChatPresencePresentation.voice(state: .listening, status: "Stummgeschaltet", audioLevel: 0.9, microphoneActive: false)
        XCTAssertEqual(muted.state, .idle); XCTAssertEqual(muted.audioLevel, 0)
        let output = ChatPresencePresentation.voice(state: .speaking, status: "SOLVIO spricht", audioLevel: 0.6, microphoneActive: false)
        XCTAssertEqual(output.audioLevel, 0.6, "Muted microphone does not mute actual playback")
        let ending = ChatPresencePresentation.voice(state: .speaking, status: "Speichern", audioLevel: 0.6, microphoneActive: true, ending: true)
        XCTAssertEqual(ending.state, .idle); XCTAssertEqual(ending.audioLevel, 0); XCTAssertFalse(ending.microphoneActive)
        XCTAssertEqual(ChatPresencePresentation.voice(state: .speaking, status: "", audioLevel: .nan, microphoneActive: false).audioLevel, 0)
    }

    func testVoiceStageYieldsSpaceToKeyboardAndLargeType() {
        XCTAssertEqual(ChatPresenceView.stageSize(voiceActive: false, availableHeight: 800, accessible: false), 88)
        XCTAssertEqual(ChatPresenceView.stageSize(voiceActive: true, availableHeight: 800, accessible: false), 156)
        XCTAssertEqual(ChatPresenceView.stageSize(voiceActive: true, availableHeight: 400, accessible: false), 88)
        XCTAssertEqual(ChatPresenceView.stageSize(voiceActive: true, availableHeight: 800, accessible: true), 88)
    }

    private func text(_ detail: ConversationDetail) -> ChatPresencePresentation {
        .text(detail: detail, sending: false, reachable: true, detailUnavailable: false)
    }
    private func detail(delivery: String = "completed", run: String? = nil, open: Bool = true) throws -> ConversationDetail {
        var runs: [[String: Any]] = []
        if let run { runs = [["id": "ar-0000000000000001", "aufgabe": "Test", "auftrag": "Test",
                              "zustand": run, "zustand_code": run, "offen": open]] }
        let raw: [String: Any] = [
            "conversation": ["conversation_id": "c-0000000000000001", "title": "Test", "kind": "text"],
            "messages": [["message_id": "m-0000000000000001", "sequence": 1, "role": "user", "text": "Test",
                          "delivery": ["delivery_id": "cd-0000000000000001", "status": delivery]]],
            "auftraege": runs, "deliveries_open": 0]
        return try JSONDecoder().decode(ConversationDetail.self, from: JSONSerialization.data(withJSONObject: raw))
    }
}
