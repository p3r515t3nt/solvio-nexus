import Foundation
import XCTest
import UIKit
@testable import SolvioApprovals

/// Synthetic wire and permission callbacks only. Never start recording or connect.
@MainActor
final class ChatVoiceHandoffTests: XCTestCase {
    private let chat = "c-0123456789abcdef"
    private let pairing = PairingPayload(v: 1, type: "fixture", core_instance_id: "core-fixture",
        endpoint: "https://127.0.0.1:1", tls_fingerprint: String(repeating: "0", count: 64),
        mac_pubkey_fingerprint: "fixture", mac_pubkey_x963_b64: "", enrollment_token: "", expires_at: 0)

    // Exercise the real begin/end, parser and MainActor callbacks together.
    // Only microphone activation and opening the network are replaced.
    private final class Background {
        var started = 0
        var ended: [UIBackgroundTaskIdentifier] = []
        var expired: (@Sendable () -> Void)?
        func begin(_ expiration: @escaping @Sendable () -> Void) -> UIBackgroundTaskIdentifier {
            started += 1; expired = expiration
            return UIBackgroundTaskIdentifier(rawValue: started)
        }
    }

    private func connected(background: Background = Background()) async throws -> (VoiceSession, VoiceTransport) {
        var wire: VoiceTransport?
        let voice = VoiceSession(requestPermission: { true },
            connectTransport: { wire = $0 }, startAudio: {},
            beginBackgroundTask: { background.begin($0) }, endBackgroundTask: { background.ended.append($0) })
        await voice.begin(pairing: pairing, deviceID: "fixture", transportCred: "fixture", conversationID: chat)
        let transport = try XCTUnwrap(wire)
        transport.handle(.string("{\"type\":\"session_ready\",\"handoff_protocol\":1,\"session_id\":\"s-fixture\",\"conversation_id\":\"\(chat)\",\"voice_mode\":\"full_duplex\"}"))
        for _ in 0..<100 where voice.handoff.state != .ready { await Task.yield() }
        XCTAssertEqual(voice.handoff.state, .ready)
        return (voice, transport)
    }

    func testActualEndWaitsForFinalReceiptAcrossRepeatedLifecycleStops() async throws {
        for reason in ["user", "background", "closed"] {
            let background = Background()
            let (voice, transport) = try await connected(background: background)
            defer { voice.end() }
            var finished = false
            voice.end(reason: reason)
            XCTAssertEqual(background.started, 1, "iOS must allow final persistence while microphone is already off")
            XCTAssertTrue(background.ended.isEmpty)
            XCTAssertFalse(voice.micActive); XCTAssertFalse(voice.isActive)
            XCTAssertEqual(voice.handoff.state, .ending)
            XCTAssertEqual(background.started, 1, "repeated stop cannot acquire another background task")
            let finishing = Task { try await voice.finishForText(conversationID: chat); finished = true }
            voice.end(reason: "closed")
            transport.handle(.string("{\"type\":\"session_end\",\"reason\":\"endpoint\"}"))
            try await Task.sleep(nanoseconds: 100_000_000)
            XCTAssertFalse(finished, "stopping audio is not confirmation")
            XCTAssertEqual(voice.handoff.state, .ending)
            transport.handle(.string("{\"type\":\"conversation_flushed\",\"session_id\":\"s-fixture\",\"conversation_id\":\"\(chat)\",\"status\":\"complete\",\"provider_closed\":true}"))
            try await finishing.value
            XCTAssertTrue(finished); XCTAssertEqual(voice.handoff.state, .complete)
            XCTAssertEqual(background.ended, [UIBackgroundTaskIdentifier(rawValue: 1)])
        }
    }

    func testActualCoreInitiatedEndWaitsForFinalReceipt() async throws {
        let background = Background()
        let (voice, transport) = try await connected(background: background)
        defer { voice.end() }
        transport.handle(.string("{\"type\":\"session_end\",\"reason\":\"timeout\"}"))
        for _ in 0..<100 where voice.handoff.state != .ending { await Task.yield() }
        XCTAssertEqual(voice.handoff.state, .ending)
        XCTAssertFalse(voice.micActive); XCTAssertFalse(voice.isActive)
        XCTAssertEqual(background.started, 1)
        transport.handle(.string("{\"type\":\"conversation_flushed\",\"session_id\":\"s-fixture\",\"conversation_id\":\"\(chat)\",\"status\":\"complete\",\"provider_closed\":true}"))
        try await voice.finishForText(conversationID: chat)
        XCTAssertEqual(voice.handoff.state, .complete)
        XCTAssertEqual(background.ended.count, 1)
        background.expired?()
        await Task.yield()
        XCTAssertEqual(voice.handoff.state, .complete, "late expiration cannot revoke a confirmed transcript")
        XCTAssertEqual(background.ended.count, 1)
    }

    func testActualUnknownReceiptPreservesWarningAndTurnsOffMicrophone() async throws {
        let background = Background()
        let (voice, transport) = try await connected(background: background)
        defer { voice.end() }
        voice.end()
        transport.handle(.string("{\"type\":\"conversation_flushed\",\"session_id\":\"s-fixture\",\"conversation_id\":\"\(chat)\",\"status\":\"unknown\",\"provider_closed\":false}"))
        do { try await voice.finishForText(conversationID: chat); XCTFail("unknown receipt must not confirm history") }
        catch { XCTAssertTrue(error is VoiceHandoffError) }
        XCTAssertEqual(voice.handoff.state, .unknown)
        XCTAssertFalse(voice.micActive); XCTAssertFalse(voice.isActive)
        XCTAssertEqual(background.ended.count, 1)
    }

    func testBackgroundExpirationOrLostConnectionCannotClaimSavedHistory() async throws {
        for expires in [true, false] {
            let background = Background()
            let (voice, transport) = try await connected(background: background)
            defer { voice.end() }
            voice.end(reason: "background")
            XCTAssertEqual(background.started, 1)
            if expires { background.expired?() }
            else { voice.handle(.closed(reason: .transport)) }
            for _ in 0..<100 where voice.handoff.state != .unknown { await Task.yield() }
            XCTAssertEqual(voice.handoff.state, .unknown)
            XCTAssertEqual(background.ended, [UIBackgroundTaskIdentifier(rawValue: 1)])
            transport.handle(.string("{\"type\":\"conversation_flushed\",\"session_id\":\"s-fixture\",\"conversation_id\":\"\(chat)\",\"status\":\"complete\",\"provider_closed\":true}"))
            do { try await voice.finishForText(conversationID: chat); XCTFail("lost connection cannot confirm history") }
            catch { XCTAssertTrue(error is VoiceHandoffError) }
            XCTAssertFalse(voice.micActive); XCTAssertFalse(voice.isActive)
            voice.end(reason: "closed")
            XCTAssertEqual(background.ended.count, 1)
        }
    }

    func testNavigationReleasingViewStillOwnsOnlyTheBoundedClose() async throws {
        let background = Background()
        var (voice, wire): (VoiceSession?, VoiceTransport?) = try await connected(background: background)
        let transport = try XCTUnwrap(wire); wire = nil
        weak var remaining = voice
        voice?.end(reason: "closed")
        voice = nil
        XCTAssertNotNil(remaining, "the final receipt must outlive the disappearing view")
        transport.handle(.string("{\"type\":\"conversation_flushed\",\"session_id\":\"s-fixture\",\"conversation_id\":\"\(chat)\",\"status\":\"complete\",\"provider_closed\":true}"))
        for _ in 0..<100 where remaining != nil { await Task.yield() }
        XCTAssertNil(remaining, "completion must release both timer and background allowance")
        XCTAssertEqual(background.ended.count, 1)
    }

    func testOnlyMatchingCompleteFlushUnlocksText() {
        var gate = VoiceHandoff()
        XCTAssertFalse(gate.blocksText)
        gate.start(conversationID: chat)
        gate.bind(sessionID: "session-1", conversationID: chat, version: 1)
        gate.ending()
        XCTAssertEqual(gate.state, .ending)
        XCTAssertFalse(gate.accept(sessionID: "other", conversationID: chat, status: "complete", providerClosed: true))
        XCTAssertFalse(gate.accept(sessionID: "session-1", conversationID: "c-ffffffffffffffff", status: "complete", providerClosed: true))
        XCTAssertTrue(gate.blocksText)
        XCTAssertTrue(gate.accept(sessionID: "session-1", conversationID: chat, status: "complete", providerClosed: true))
        XCTAssertFalse(gate.blocksText)
        gate.fail()
        XCTAssertFalse(gate.blocksText, "socket closing after confirmation cannot revoke the completed history")
    }

    func testMissingMarkerUnknownAndTimeoutNeverUnlockText() {
        for (status, closed) in [("unknown", true), ("complete", false), ("other", true)] {
            var gate = VoiceHandoff(); gate.start(conversationID: chat)
            gate.bind(sessionID: "session-1", conversationID: chat, version: 1); gate.ending()
            XCTAssertTrue(gate.accept(sessionID: "session-1", conversationID: chat, status: status, providerClosed: closed))
            XCTAssertEqual(gate.state, .unknown); XCTAssertTrue(gate.blocksText)
            XCTAssertFalse(gate.accept(sessionID: "session-1", conversationID: chat, status: "complete", providerClosed: true))
        }
        var old = VoiceHandoff(); old.start(conversationID: chat); old.ending()
        XCTAssertEqual(old.state, .unknown); XCTAssertTrue(old.blocksText)
        var timeout = VoiceHandoff(); timeout.start(conversationID: chat)
        timeout.bind(sessionID: "session-1", conversationID: chat, version: 1); timeout.ending(); timeout.fail()
        XCTAssertEqual(timeout.state, .unknown); XCTAssertTrue(timeout.blocksText)
    }

    func testExplicitIncompleteRecoveryIsBoundToChatAndNeverClaimsComplete() {
        var gate = VoiceHandoff(); gate.start(conversationID: chat); gate.fail()
        XCTAssertFalse(gate.blocksText(in: "c-ffffffffffffffff"), "another chat has its own history")
        XCTAssertTrue(gate.blocksText(in: chat))
        XCTAssertFalse(gate.acceptStoredHistory(conversationID: "c-ffffffffffffffff"))
        XCTAssertEqual(gate.state, .unknown)
        XCTAssertTrue(gate.acceptStoredHistory(conversationID: chat))
        XCTAssertEqual(gate.state, .acceptedIncomplete)
        XCTAssertFalse(gate.blocksText(in: chat))
        XCTAssertFalse(gate.accept(sessionID: "old", conversationID: chat, status: "complete", providerClosed: true))
        XCTAssertEqual(gate.state, .acceptedIncomplete)
    }

    func testReconnectAndOtherChatsCannotEraseAnUnconfirmedEarlierVoiceSession() {
        let other = "c-ffffffffffffffff"
        var gate = VoiceHandoff(); gate.start(conversationID: chat)
        gate.bind(sessionID: "old", conversationID: chat, version: 1)
        gate.fail() // actual transport loss
        gate.start(conversationID: chat)
        gate.bind(sessionID: "reconnected", conversationID: chat, version: 1); gate.ending()
        XCTAssertTrue(gate.accept(sessionID: "reconnected", conversationID: chat, status: "complete", providerClosed: true))
        XCTAssertEqual(gate.state, .unknown, "new provider closure says nothing about the lost old tail")
        gate.start(conversationID: other)
        gate.bind(sessionID: "other-session", conversationID: other, version: 1); gate.ending()
        XCTAssertTrue(gate.accept(sessionID: "other-session", conversationID: other, status: "complete", providerClosed: true))
        XCTAssertFalse(gate.blocksText(in: other))
        XCTAssertTrue(gate.blocksText(in: chat), "returning to the first chat must retain the warning")
        XCTAssertTrue(gate.acceptStoredHistory(conversationID: chat))
        XCTAssertFalse(gate.blocksText(in: chat))
        XCTAssertEqual(gate.state, .complete, "the unrelated chat's valid receipt remains intact")
    }

    func testWireKeepsExactProtocolTypesAndConversationBinding() throws {
        var events: [VoiceWireEvent] = []
        let transport = VoiceTransport(pairing: pairing, deviceID: "fixture", transportCred: "fixture", events: { events.append($0) })
        defer { transport.close() }
        transport.handle(.string("{\"type\":\"session_ready\",\"handoff_protocol\":1,\"session_id\":\"session-1\",\"conversation_id\":\"\(chat)\"}"))
        guard case let .handoffReady(id, conversation, version) = try XCTUnwrap(events.last) else { return XCTFail("missing ready binding") }
        XCTAssertEqual(id, "session-1"); XCTAssertEqual(conversation, chat); XCTAssertEqual(version, 1)
        for raw in ["true", "\"1\""] {
            transport.handle(.string("{\"type\":\"session_ready\",\"handoff_protocol\":\(raw),\"session_id\":\"session-1\",\"conversation_id\":\"\(chat)\"}"))
            guard case let .handoffReady(_, _, version) = try XCTUnwrap(events.last) else { return XCTFail() }
            XCTAssertEqual(version, 0)
        }
        let before = events.count
        transport.handle(.string("{\"type\":\"conversation_flushed\",\"session_id\":\"session-1\",\"conversation_id\":\"\(chat)\",\"status\":\"complete\",\"provider_closed\":1}"))
        XCTAssertEqual(events.count, before, "numeric 1 is not a provider-close boolean")
        transport.handle(.string("{\"type\":\"conversation_flushed\",\"session_id\":\"session-1\",\"conversation_id\":\"\(chat)\",\"status\":\"complete\",\"provider_closed\":true}"))
        guard case let .conversationFlushed(id, conversation, status, closed) = try XCTUnwrap(events.last) else { return XCTFail() }
        XCTAssertEqual(id, "session-1"); XCTAssertEqual(conversation, chat); XCTAssertEqual(status, "complete"); XCTAssertTrue(closed)
    }

    func testBackgroundEndWhilePermissionIsPendingCannotOpenAudioAfterApproval() async throws {
        let permission = Gate()
        var requested = false
        let voice = VoiceSession(requestPermission: {
            requested = true; await permission.wait(); return true
        })
        let start = Task { await voice.begin(pairing: pairing, deviceID: "fixture", transportCred: "fixture", conversationID: chat) }
        while !requested { await Task.yield() }
        XCTAssertTrue(voice.isActive); XCTAssertFalse(voice.micActive)
        voice.end(reason: "background")
        await permission.open(); await start.value
        XCTAssertEqual(voice.state, .ended); XCTAssertFalse(voice.isActive); XCTAssertFalse(voice.micActive)
        XCTAssertEqual(voice.handoff.state, .idle, "never opened a transport or audio engine")
        try await voice.finishForText()
    }

    private func ready(_ voice: VoiceSession, _ wire: VoiceTransport, sessionID: String) async {
        wire.handle(.string("{\"type\":\"session_ready\",\"handoff_protocol\":1,\"session_id\":\"\(sessionID)\",\"conversation_id\":\"\(chat)\",\"voice_mode\":\"full_duplex\"}"))
        for _ in 0..<100 where voice.handoff.state != .ready { await Task.yield() }
        XCTAssertEqual(voice.handoff.state, .ready)
    }

    private func finish(_ voice: VoiceSession, _ wire: VoiceTransport, sessionID: String) async throws {
        voice.end(reason: "user")
        wire.handle(.string("{\"type\":\"conversation_flushed\",\"session_id\":\"\(sessionID)\",\"conversation_id\":\"\(chat)\",\"status\":\"complete\",\"provider_closed\":true}"))
        try await voice.finishForText(conversationID: chat)
        XCTAssertEqual(voice.handoff.state, .complete)
    }

    func testNewExplicitConversationUnmutesOnlyAfterFreshPermissionIsAccepted() async throws {
        let audio = VoiceAudio(), permission = Gate(), background = Background()
        let secondPermission = expectation(description: "new conversation asks permission")
        var requested = 0, starts = 0
        var wires: [VoiceTransport] = []
        let voice = VoiceSession(audio: audio, requestPermission: {
            requested += 1
            if requested == 2 { secondPermission.fulfill(); await permission.wait() }
            return true
        }, connectTransport: { wires.append($0) }, startAudio: { starts += 1 },
            beginBackgroundTask: { background.begin($0) }, endBackgroundTask: { background.ended.append($0) })
        defer { voice.end() }
        await voice.begin(pairing: pairing, deviceID: "fixture", transportCred: "fixture", conversationID: chat)
        let first = try XCTUnwrap(wires.first)
        await ready(voice, first, sessionID: "s-first")
        voice.muted = true
        XCTAssertTrue(audio.captureMuted); XCTAssertFalse(voice.micActive)
        XCTAssertEqual(voice.chatStatus, "Stummgeschaltet")
        try await finish(voice, first, sessionID: "s-first")
        XCTAssertTrue(voice.muted, "ending must not turn a muted microphone on")

        let start = Task { await voice.begin(pairing: pairing, deviceID: "fixture", transportCred: "fixture", conversationID: chat) }
        await fulfillment(of: [secondPermission], timeout: 1)
        XCTAssertTrue(voice.muted); XCTAssertTrue(audio.captureMuted)
        XCTAssertFalse(voice.micActive); XCTAssertEqual(starts, 1); XCTAssertEqual(wires.count, 1)
        await permission.open(); await start.value
        XCTAssertFalse(voice.muted, "a newly requested conversation must be ready to hear its first words")
        XCTAssertFalse(audio.captureMuted); XCTAssertTrue(voice.micActive)
        XCTAssertEqual(starts, 2); XCTAssertEqual(wires.count, 2)
        let second = try XCTUnwrap(wires.last)
        await ready(voice, second, sessionID: "s-second")
        XCTAssertEqual(voice.chatStatus, "SOLVIO hört zu")
        try await finish(voice, second, sessionID: "s-second")
    }

    func testDeniedFreshPermissionKeepsMuteAndCannotOpenMicrophoneOrTransport() async {
        let audio = VoiceAudio()
        var connections = 0, starts = 0
        let voice = VoiceSession(audio: audio, requestPermission: { false },
            connectTransport: { _ in connections += 1 }, startAudio: { starts += 1 })
        defer { voice.end() }
        voice.muted = true
        await voice.begin(pairing: pairing, deviceID: "fixture", transportCred: "fixture", conversationID: chat)
        XCTAssertTrue(voice.muted); XCTAssertTrue(audio.captureMuted)
        XCTAssertFalse(voice.micActive); XCTAssertFalse(voice.isActive)
        XCTAssertEqual(connections, 0); XCTAssertEqual(starts, 0)
        guard case .offline = voice.state else { return XCTFail("permission denial must remain visible") }
    }

    func testCancelledFreshPermissionKeepsMuteAndCannotOpenMicrophoneOrTransport() async {
        for endDuringPermission in [true, false] {
            let audio = VoiceAudio(), permission = Gate()
            let asked = expectation(description: "pending microphone permission")
            var connections = 0, starts = 0
            let voice = VoiceSession(audio: audio, requestPermission: {
                asked.fulfill(); await permission.wait(); return true
            }, connectTransport: { _ in connections += 1 }, startAudio: { starts += 1 })
            voice.muted = true
            let start = Task { await voice.begin(pairing: pairing, deviceID: "fixture", transportCred: "fixture", conversationID: chat) }
            await fulfillment(of: [asked], timeout: 1)
            XCTAssertTrue(voice.muted); XCTAssertFalse(voice.micActive)
            if endDuringPermission { voice.end(reason: "background") }
            else { start.cancel() }
            await permission.open(); await start.value
            XCTAssertTrue(voice.muted); XCTAssertTrue(audio.captureMuted)
            XCTAssertFalse(voice.micActive); XCTAssertEqual(connections, 0); XCTAssertEqual(starts, 0)
            voice.end(reason: "background")
            XCTAssertFalse(voice.isActive)
        }
    }

    func testSameConversationReadyAndAutomaticReconnectKeepMuteAndBackgroundEndDoesNotResumeAudio() async throws {
        let audio = VoiceAudio(), background = Background()
        let reconnected = expectation(description: "existing bounded reconnect opens its replacement transport")
        var wires: [VoiceTransport] = [], muteAtAudioStart: [Bool] = []
        let voice = VoiceSession(audio: audio, requestPermission: { true },
            connectTransport: { wires.append($0); if wires.count == 2 { reconnected.fulfill() } },
            startAudio: { muteAtAudioStart.append(audio.captureMuted) },
            beginBackgroundTask: { background.begin($0) }, endBackgroundTask: { background.ended.append($0) })
        defer { voice.end(); wires.forEach { $0.close() } }
        await voice.begin(pairing: pairing, deviceID: "fixture", transportCred: "fixture", conversationID: chat)
        await ready(voice, try XCTUnwrap(wires.first), sessionID: "s-before-reconnect")
        voice.muted = true
        voice.handle(.sessionReady(mode: .fullDuplex))
        XCTAssertTrue(voice.muted); XCTAssertTrue(audio.captureMuted); XCTAssertFalse(voice.micActive)
        XCTAssertEqual(voice.chatStatus, "Stummgeschaltet")
        voice.handle(.closed(reason: .transport))
        await fulfillment(of: [reconnected], timeout: 3)
        XCTAssertEqual(muteAtAudioStart, [false, true], "automatic reconnect cannot unmute the same conversation")
        let second = try XCTUnwrap(wires.last)
        await ready(voice, second, sessionID: "s-after-reconnect")
        XCTAssertTrue(voice.muted); XCTAssertTrue(audio.captureMuted); XCTAssertFalse(voice.micActive)
        voice.end(reason: "background")
        second.handle(.string("{\"type\":\"conversation_flushed\",\"session_id\":\"s-after-reconnect\",\"conversation_id\":\"\(chat)\",\"status\":\"unknown\",\"provider_closed\":true}"))
        for _ in 0..<100 where voice.handoff.state != .unknown { await Task.yield() }
        XCTAssertEqual(voice.handoff.state, .unknown)
        voice.handle(.sessionReady(mode: .fullDuplex))
        XCTAssertFalse(voice.isActive); XCTAssertFalse(voice.micActive)
        XCTAssertTrue(voice.muted); XCTAssertTrue(audio.captureMuted)
        XCTAssertEqual(muteAtAudioStart.count, 2, "a late ready event after background exit cannot resume the microphone")
    }
}
