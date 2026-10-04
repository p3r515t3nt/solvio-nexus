import XCTest
@testable import SolvioApprovals

/// Native parsing/state/audio-control only. Never connect, request permission,
/// start AVAudioEngine, load pairing, or play/record microphone samples.
@MainActor
final class VoiceDuplexTests: XCTestCase {
    private let pairing = PairingPayload(v: 1, type: "fixture", core_instance_id: "core-fixture",
        endpoint: "https://127.0.0.1:1", tls_fingerprint: String(repeating: "0", count: 64),
        mac_pubkey_fingerprint: "fixture", mac_pubkey_x963_b64: "", enrollment_token: "", expires_at: 0)

    private func ready(_ raw: Any?) throws -> VoiceWireEvent {
        var received: [VoiceWireEvent] = []
        let transport = VoiceTransport(pairing: pairing, deviceID: "fixture", transportCred: "fixture",
            events: { received.append($0) })
        defer { transport.close() }
        var body: [String: Any] = ["type": "session_ready"]
        if let raw { body["voice_mode"] = raw }
        let data = try JSONSerialization.data(withJSONObject: body)
        transport.handle(.string(try XCTUnwrap(String(data: data, encoding: .utf8))))
        return try XCTUnwrap(received.first)
    }

    func testOnlyExactReadyCapabilitySelectsDuplex() throws {
        let cases: [(Any?, VoiceMode)] = [
            ("full_duplex", .fullDuplex), (nil, .turnBased), (true, .turnBased),
            ("FULL_DUPLEX", .turnBased), ("gpt-live-1", .turnBased), ("unknown", .turnBased)]
        for (raw, expected) in cases {
            guard case let .sessionReady(mode) = try ready(raw) else { return XCTFail("wrong event") }
            XCTAssertEqual(mode, expected)
        }
    }

    func testNativeReadyToAudioDisablesOnlyAutomaticInterruption() async throws {
        let audio = VoiceAudio(), session = VoiceSession(audio: VoiceAudio())
        // Separate instances must not share negotiated mode.
        session.handle(try ready("full_duplex"))
        XCTAssertEqual(audio.voiceMode, .turnBased)
        session.end()

        let active = VoiceSession(audio: audio)
        defer { active.end() }
        active.handle(try ready("full_duplex"))
        XCTAssertEqual(audio.voiceMode, .fullDuplex)
        let forbidden = expectation(description: "continuous overlap must not silence output")
        forbidden.isInverted = true
        audio.onLocalBargeIn = { _ in forbidden.fulfill() }
        audio.interruptAutomatically(heardMilliseconds: 900)
        await fulfillment(of: [forbidden], timeout: 0.1)

        active.handle(try ready(nil))
        XCTAssertEqual(audio.voiceMode, .turnBased)
        let legacy = expectation(description: "legacy RMS interruption remains functional")
        audio.onLocalBargeIn = { heard in XCTAssertEqual(heard, 900); legacy.fulfill() }
        audio.interruptAutomatically(heardMilliseconds: 900)
        await fulfillment(of: [legacy], timeout: 1)
    }

    func testMuteFlushEndAndFailureDoNotRetainOrUpgradeMode() throws {
        let audio = VoiceAudio()
        let active = VoiceSession(audio: audio)
        defer { active.end() }
        active.handle(try ready("full_duplex"))
        active.muted = true
        XCTAssertTrue(audio.captureMuted)
        XCTAssertEqual(audio.voiceMode, .fullDuplex)
        active.handle(.flush)
        XCTAssertEqual(audio.voiceMode, .fullDuplex, "explicit flush remains separate from negotiation")
        active.muted = false
        XCTAssertFalse(audio.captureMuted)
        active.handle(.sessionEnd("core"))
        XCTAssertEqual(audio.voiceMode, .turnBased)
        XCTAssertEqual(active.state, .ended)
        active.handle(try ready("full_duplex"))
        active.handle(.closed(reason: .transport))
        XCTAssertEqual(audio.voiceMode, .turnBased)
        XCTAssertFalse(active.micActive)
        active.handle(try ready(nil))
        XCTAssertEqual(audio.voiceMode, .turnBased)
    }

    func testStoppingBeforeAudioStartedStillClearsNegotiatedMode() {
        let audio = VoiceAudio()
        audio.voiceMode = .fullDuplex
        audio.stop()
        XCTAssertEqual(audio.voiceMode, .turnBased)
    }

    func testClosedTransportCannotDeliverLateReadyCapability() {
        var count = 0
        let transport = VoiceTransport(pairing: pairing, deviceID: "fixture", transportCred: "fixture",
            events: { _ in count += 1 })
        transport.close()
        transport.handle(.string("{\"type\":\"session_ready\",\"voice_mode\":\"full_duplex\"}"))
        XCTAssertEqual(count, 0)
    }
}
