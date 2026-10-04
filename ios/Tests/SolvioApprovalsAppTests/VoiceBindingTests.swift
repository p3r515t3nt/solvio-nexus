import XCTest
import SolvioApprovalsKit
@testable import SolvioApprovals

/// Sitzungs-Assertion mit und ohne `conversation_id`; 4401 `conversation_not_bindable`
/// fuehrt zu keinem Ersatz-Sitzungsaufbau. Kein Netz, kein Mikrofon, kein App Attest.
@MainActor
final class VoiceBindingTests: XCTestCase {
    private let pairing = PairingPayload(v: 1, type: "fixture", core_instance_id: "core-fixture",
        endpoint: "https://127.0.0.1:1", tls_fingerprint: String(repeating: "0", count: 64),
        mac_pubkey_fingerprint: "fixture", mac_pubkey_x963_b64: "", enrollment_token: "", expires_at: 0)
    private let nonce = String(repeating: "ab", count: 32)
    private let chat = "c-0123456789abcdef"

    private final class Capture: @unchecked Sendable {
        var controls: [[String: String]] = []
        var hashes: [Data] = []
    }

    private func transport(conversationID: String?, capture: Capture) -> VoiceTransport {
        VoiceTransport(pairing: pairing, deviceID: "dev-fixture", transportCred: "fixture", conversationID: conversationID,
            signer: { hash in capture.hashes.append(hash); return Data([9, 9, 9]) },
            controlSink: { capture.controls.append($0) }, events: { _ in })
    }

    func testSessionAssertionCarriesTheChatOnlyWhenBoundAndSignsTheMatchingBinding() async throws {
        let unbound = Capture(), bound = Capture(), empty = Capture()
        await transport(conversationID: nil, capture: unbound).answerSessionChallenge(core: "core-fixture", nonce: nonce)
        await transport(conversationID: chat, capture: bound).answerSessionChallenge(core: "core-fixture", nonce: nonce)
        await transport(conversationID: "", capture: empty).answerSessionChallenge(core: "core-fixture", nonce: nonce)
        XCTAssertEqual(unbound.controls.count, 1); XCTAssertEqual(bound.controls.count, 1)
        XCTAssertEqual(unbound.controls[0]["type"], "session_assertion")
        XCTAssertEqual(unbound.controls[0]["session_nonce"], nonce)
        XCTAssertNil(unbound.controls[0]["conversation_id"], "an unbound session sends exactly the old message")
        XCTAssertEqual(Set(unbound.controls[0].keys), ["type", "session_nonce", "assertion"])
        XCTAssertEqual(bound.controls[0]["conversation_id"], chat)
        XCTAssertEqual(Set(bound.controls[0].keys), ["type", "session_nonce", "assertion", "conversation_id"])
        XCTAssertEqual(bound.controls[0]["assertion"], Data([9, 9, 9]).base64EncodedString())
        XCTAssertEqual(empty.controls[0], unbound.controls[0], "an empty id is no binding")
        let legacy = VoiceSessionBinding(coreInstanceID: "core-fixture", deviceID: "dev-fixture", sessionNonce: nonce)
        let withChat = VoiceSessionBinding(coreInstanceID: "core-fixture", deviceID: "dev-fixture", sessionNonce: nonce, conversationID: chat)
        XCTAssertEqual(unbound.hashes, [legacy.clientDataHash()])
        XCTAssertEqual(bound.hashes, [withChat.clientDataHash()])
        XCTAssertEqual(empty.hashes, [legacy.clientDataHash()])
        XCTAssertNotEqual(legacy.clientDataHash(), withChat.clientDataHash())
    }

    func testChallengeFromTheWireAnswersWithTheBoundChat() async throws {
        let capture = Capture()
        let transport = transport(conversationID: chat, capture: capture)
        defer { transport.close() }
        transport.handle(.string("{\"type\":\"session_challenge\",\"core_instance_id\":\"core-fixture\",\"session_nonce\":\"\(nonce)\"}"))
        for _ in 0..<50 where capture.controls.isEmpty { try await Task.sleep(nanoseconds: 10_000_000) }
        XCTAssertEqual(capture.controls.first?["conversation_id"], chat)
        XCTAssertEqual(capture.controls.first?["session_nonce"], nonce)
    }

    func testFailedSignerSendsNothingRatherThanAnUnboundAssertion() async throws {
        let capture = Capture()
        let transport = VoiceTransport(pairing: pairing, deviceID: "dev-fixture", transportCred: "fixture", conversationID: chat,
            signer: { _ in throw URLError(.cancelled) }, controlSink: { capture.controls.append($0) }, events: { _ in })
        await transport.answerSessionChallenge(core: "core-fixture", nonce: nonce)
        XCTAssertTrue(capture.controls.isEmpty)
    }

    func testCloseCodesAreClassifiedByTheCoreNotGuessed() {
        XCTAssertEqual(VoiceCloseReason.classify(httpStatus: nil, closeCode: 4401, closeReason: "conversation_not_bindable"), .conversationNotBindable)
        XCTAssertEqual(VoiceCloseReason.classify(httpStatus: nil, closeCode: 4401, closeReason: "unauthorized"), .unauthorized)
        XCTAssertEqual(VoiceCloseReason.classify(httpStatus: nil, closeCode: 4409, closeReason: "voice_busy"), .busy)
        XCTAssertEqual(VoiceCloseReason.classify(httpStatus: 401, closeCode: nil, closeReason: ""), .unauthorized)
        XCTAssertEqual(VoiceCloseReason.classify(httpStatus: 409, closeCode: nil, closeReason: ""), .busy)
        XCTAssertEqual(VoiceCloseReason.classify(httpStatus: nil, closeCode: nil, closeReason: ""), .transport)
        XCTAssertEqual(VoiceCloseReason.classify(httpStatus: nil, closeCode: 1006, closeReason: ""), .transport)
        XCTAssertFalse(VoiceCloseReason.conversationNotBindable.retriable)
    }

    func testNotBindableEndsTheSessionWithoutAReplacementConnection() throws {
        let audio = VoiceAudio()
        let session = VoiceSession(audio: audio)
        defer { session.end() }
        session.handle(.closed(reason: .conversationNotBindable))
        guard case let .offline(why) = session.state else { return XCTFail("expected offline, got \(session.state)") }
        XCTAssertEqual(why, "Dieser Chat ist für Sprache nicht verfügbar.")
        XCTAssertFalse(session.micActive)
        XCTAssertEqual(audio.voiceMode, .turnBased)
        // A transport failure would schedule a reconnect only while running; not-bindable never does.
        session.handle(.closed(reason: .conversationNotBindable))
        guard case .offline = session.state else { return XCTFail("state must stay offline") }
    }
}
