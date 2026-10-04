import Foundation
import XCTest
import SolvioApprovalsKit
@testable import SolvioApprovals

/// Senden, Wiederholen mit derselben Kennung, `alreadySending`, Marke vor dem
/// ersten `await`. Netz und Signierer sind Attrappen; nichts beruehrt Keychain
/// oder App Attest.
@MainActor
final class MessageComposerTests: XCTestCase {
    private let chat = "c-0123456789abcdef"
    private let accepted = AppConversationMessageAccepted(deliveryID: "cd-0123456789abcdef", status: "accepted", messageID: "m-0123456789abcdef")

    private func challenge(_ body: AppConversationMessageBody, nonce: String = String(repeating: "b", count: 64)) throws -> AppConversationMessageChallenge {
        let raw: [String: Any] = ["protocol_version": 1, "type": "app_conversation_message_binding", "core_instance_id": "core",
            "principal_id": "owner", "device_id": "phone", "nonce": nonce, "request_digest": body.requestDigest,
            "enrollment_id": "enrollment", "app_attest_key_id": "key", "approval_key_sha256": String(repeating: "c", count: 64)]
        return .init(nonce: nonce, requestDigest: body.requestDigest, expiresAt: Date().timeIntervalSince1970 + 30,
                     bindingB64: try JSONSerialization.data(withJSONObject: raw, options: [.sortedKeys]).base64EncodedString())
    }

    /// Ein kleiner Schluesselbund-Ersatz mit Reihenfolgeprotokoll.
    private final class Store {
        var binding: ConversationMessageRetryBinding?
        var log: [String] = []
        func load() -> ConversationMessageRetryBinding? { binding }
        func save(_ value: ConversationMessageRetryBinding) { binding = value; log.append("save:" + (value.deliveryID ?? "-")) }
        func clear() { binding = nil; log.append("clear") }
    }

    private func send(_ model: MessageComposerModel, store: Store, chat: String? = nil,
                      challenge: ((AppConversationMessageBody) async throws -> AppConversationMessageChallenge)? = nil,
                      sign: ((Data) async throws -> Data)? = nil,
                      submit: @escaping (AppConversationMessageBody, AppTaskProof) async throws -> AppConversationMessageAccepted) async -> AppConversationMessageAccepted? {
        await model.send(conversationID: chat ?? self.chat, coreID: "core", deviceID: "phone", keyID: "key",
            loadRetry: { store.load() }, saveRetry: { store.save($0) }, clearRetry: { store.clear() },
            challenge: challenge ?? { body in store.log.append("challenge"); return try self.challenge(body) },
            sign: sign ?? { _ in Data([1]) }, submit: submit)
    }

    private func confirmedDetail(_ pending: ConversationMessageRetryBinding) throws -> ConversationDetail {
        let raw: [String: Any] = [
            "conversation": ["conversation_id": pending.conversationID, "title": "Chat", "kind": "text"],
            "messages": [["message_id": accepted.message_id, "sequence": 1, "role": "user", "text": "Gesendet",
                          "delivery": ["delivery_id": accepted.delivery_id, "status": "completed",
                                       "client_message_id": pending.clientMessageID]]],
            "auftraege": [], "deliveries_open": 0]
        return try JSONDecoder().decode(ConversationDetail.self, from: JSONSerialization.data(withJSONObject: raw))
    }

    private func poll(_ model: MessageComposerModel, chats: ConversationModel, store: Store,
                      load: (String) async throws -> ConversationDetail) async {
        await model.refreshDetail(chats: chats, coreID: "core", deviceID: "phone",
            loadRetry: { store.load() }, clearRetry: { store.clear() }, load: load)
    }

    func testVoiceHandoffFinishesBeforeFirstMessageEffectAndFailurePreservesDraft() async throws {
        let model = MessageComposerModel(), store = Store()
        model.text = "Bitte ergänze den gesprochenen Auftrag."
        var confirmed = false, submits = 0, preparations = 0
        func perform() async -> AppConversationMessageAccepted? {
            await model.send(conversationID: chat, coreID: "core", deviceID: "phone", keyID: "key",
                loadRetry: { store.load() }, saveRetry: { store.save($0) }, clearRetry: { store.clear() },
                challenge: { body in
                    XCTAssertTrue(confirmed); return try self.challenge(body)
                }, sign: { _ in Data([1]) }, submit: { _, _ in
                    submits += 1; return self.accepted
                }, beforeSend: {
                    preparations += 1
                    XCTAssertTrue(model.sending)
                    XCTAssertTrue(store.log.isEmpty, "no retry marker or request before voice history is confirmed")
                    guard confirmed else { throw VoiceHandoffError() }
                })
        }
        let first = await perform(); XCTAssertNil(first)
        XCTAssertNil(store.binding); XCTAssertEqual(submits, 0)
        XCTAssertEqual(model.text, "Bitte ergänze den gesprochenen Auftrag.")
        XCTAssertFalse(model.needsRetry, "failed handoff is not an already-dispatched message")
        XCTAssertFalse(model.message.isEmpty)
        let second = await perform(); XCTAssertNil(second, "a second click must still require confirmation")
        confirmed = true
        let third = await perform(); XCTAssertNotNil(third)
        XCTAssertEqual(submits, 1); XCTAssertEqual(preparations, 3)
        XCTAssertTrue(model.text.isEmpty)
    }

    func testLostAcceptanceThenPollingConfirmationDoesNotResubmitTheDraft() async throws {
        let model = MessageComposerModel(), store = Store(), chats = ConversationModel()
        chats.select(chat, remember: false)
        model.text = "Besorg mir ein Hotel."
        var submissions = 0
        _ = await send(model, store: store, submit: { _, _ in
            submissions += 1; throw URLError(.networkConnectionLost)
        })
        let pending = try XCTUnwrap(store.binding)
        XCTAssertTrue(model.needsRetry)
        await poll(model, chats: chats, store: store) { _ in try self.confirmedDetail(pending) }
        XCTAssertNil(store.binding)
        XCTAssertEqual(model.text, "")
        XCTAssertFalse(model.needsRetry)
        XCTAssertTrue(model.message.isEmpty)
        let retry = await send(model, store: store, submit: { _, _ in
            submissions += 1; return self.accepted
        })
        XCTAssertNil(retry)
        XCTAssertEqual(submissions, 1, "the polled acceptance must not become a new message ID")
    }

    func testPollingConfirmationPreservesTextEditedDuringTheRead() async throws {
        let model = MessageComposerModel(), store = Store(), chats = ConversationModel()
        chats.select(chat, remember: false)
        model.text = "Erste Nachricht."
        _ = await send(model, store: store, submit: { _, _ in throw URLError(.timedOut) })
        let pending = try XCTUnwrap(store.binding)
        await poll(model, chats: chats, store: store) { _ in
            model.text = "Zweite Nachricht."
            return try self.confirmedDetail(pending)
        }
        XCTAssertNil(store.binding, "the earlier message was confirmed")
        XCTAssertEqual(model.text, "Zweite Nachricht.")
        XCTAssertFalse(model.needsRetry)
        var newID: String?
        _ = await send(model, store: store, submit: { body, _ in newID = body.client_message_id; return self.accepted })
        XCTAssertNotNil(newID)
        XCTAssertNotEqual(newID, pending.clientMessageID, "only the deliberately edited text becomes a new message")
    }

    func testPollingConfirmationCannotClearAnotherChatOrAReplacementRetryBinding() async throws {
        let model = MessageComposerModel(), store = Store(), chats = ConversationModel()
        chats.select(chat, remember: false)
        model.text = "Erste Nachricht."
        _ = await send(model, store: store, submit: { _, _ in throw URLError(.timedOut) })
        let pending = try XCTUnwrap(store.binding)
        await poll(model, chats: chats, store: store) { _ in
            chats.select("c-fedcba9876543210", remember: false)
            return try self.confirmedDetail(pending)
        }
        XCTAssertEqual(store.binding, pending)
        XCTAssertEqual(model.text, "Erste Nachricht.")
        XCTAssertTrue(model.needsRetry)
        chats.select(chat, remember: false)
        let replacement = ConversationMessageRetryBinding(body: try AppConversationMessageBody(
            conversationID: chat, clientMessageID: "msg-replacement-0001", text: "Andere Nachricht."),
            coreID: "core", deviceID: "phone")
        await poll(model, chats: chats, store: store) { _ in
            store.save(replacement)
            return try self.confirmedDetail(pending)
        }
        XCTAssertEqual(store.binding, replacement)
        XCTAssertEqual(model.text, "Erste Nachricht.")
        XCTAssertTrue(model.needsRetry)
    }

    func testPollingDuringRetryKeepsTheBindingUntilTheSendSettles() async throws {
        let model = MessageComposerModel(), store = Store(), chats = ConversationModel()
        chats.select(chat, remember: false)
        model.text = "Besorg mir ein Hotel."
        var ids: [String] = []
        _ = await send(model, store: store, submit: { body, _ in
            ids.append(body.client_message_id); throw URLError(.timedOut)
        })
        let pending = try XCTUnwrap(store.binding)
        _ = await send(model, store: store, submit: { body, _ in
            ids.append(body.client_message_id)
            await self.poll(model, chats: chats, store: store) { _ in try self.confirmedDetail(pending) }
            XCTAssertTrue(model.sending)
            XCTAssertEqual(store.binding, pending)
            XCTAssertEqual(model.text, "Besorg mir ein Hotel.")
            throw URLError(.networkConnectionLost)
        })
        XCTAssertEqual(ids, [pending.clientMessageID, pending.clientMessageID])
        XCTAssertTrue(model.needsRetry)
        await poll(model, chats: chats, store: store) { _ in try self.confirmedDetail(pending) }
        XCTAssertNil(store.binding)
        XCTAssertEqual(model.text, "")
        XCTAssertFalse(model.needsRetry)
    }

    func testMarkIsSavedBeforeTheFirstAwaitAndTheRetryReusesTheSameMessageID() async throws {
        let model = MessageComposerModel(), store = Store()
        model.text = "  Besorg mir ein Hotel in Hamburg.  "
        XCTAssertTrue(model.valid)
        var sent: [AppConversationMessageBody] = [], proofs: [AppTaskProof] = []
        let first = await send(model, store: store, submit: { body, proof in
            sent.append(body); proofs.append(proof); throw URLError(.networkConnectionLost)
        })
        XCTAssertNil(first); XCTAssertTrue(model.needsRetry); XCTAssertFalse(model.sending)
        XCTAssertEqual(store.log.first, "save:-", "the mark exists before the challenge is requested")
        XCTAssertEqual(store.log, ["save:-", "challenge"])
        XCTAssertEqual(sent.count, 1); XCTAssertEqual(sent[0].text, "Besorg mir ein Hotel in Hamburg.")
        XCTAssertEqual(store.binding?.clientMessageID, sent[0].client_message_id)
        XCTAssertNil(store.binding?.deliveryID)
        XCTAssertFalse(String(decoding: try JSONEncoder().encode(store.binding), as: UTF8.self).contains("Hamburg"))
        XCTAssertEqual(model.text, "  Besorg mir ein Hotel in Hamburg.  ", "the text stays in the field, never in storage")
        // Retry: same body, same client_message_id, a fresh nonce.
        let second = await send(model, store: store,
            challenge: { body in store.log.append("challenge"); return try self.challenge(body, nonce: String(repeating: "d", count: 64)) },
            submit: { body, proof in sent.append(body); proofs.append(proof); return self.accepted })
        XCTAssertEqual(second, accepted)
        XCTAssertEqual(sent.count, 2); XCTAssertEqual(sent[0], sent[1])
        XCTAssertNotEqual(proofs[0].nonce, proofs[1].nonce)
        XCTAssertEqual(store.binding?.deliveryID, accepted.delivery_id, "after 202 the mark carries the delivery id")
        XCTAssertEqual(model.text, ""); XCTAssertFalse(model.needsRetry); XCTAssertEqual(model.lastAccepted, accepted)
        // The next message is a new one.
        model.text = "Und ein Tisch für zwei."
        let third = await send(model, store: store, submit: { body, _ in sent.append(body); return self.accepted })
        XCTAssertEqual(third, accepted)
        XCTAssertNotEqual(sent[2].client_message_id, sent[1].client_message_id)
    }

    func testEditingAfterAFailureDoesNotSendADifferentMessageUntilTheEarlierOneIsDiscarded() async throws {
        let model = MessageComposerModel(), store = Store()
        model.text = "Erste Nachricht."
        var sent: [AppConversationMessageBody] = []
        _ = await send(model, store: store, submit: { body, _ in sent.append(body); throw URLError(.timedOut) })
        model.text = "Zweite, andere Nachricht."
        XCTAssertFalse(model.needsRetry, "editing ends the retry offer")
        let blocked = await send(model, store: store, submit: { body, _ in sent.append(body); return self.accepted })
        XCTAssertNil(blocked)
        XCTAssertEqual(sent.count, 1, "the unresolved first message blocks a different one")
        XCTAssertNotNil(model.unconfirmed); XCTAssertTrue(model.message.contains("ungeklärt"))
        model.discardUnconfirmed { store.clear() }
        XCTAssertNil(store.binding)
        let afterDiscard = await send(model, store: store, submit: { body, _ in sent.append(body); return self.accepted })
        XCTAssertEqual(afterDiscard, accepted)
        XCTAssertEqual(sent.count, 2)
        XCTAssertNotEqual(sent[0].client_message_id, sent[1].client_message_id)
        XCTAssertEqual(sent[1].text, "Zweite, andere Nachricht.")
    }

    func testUnconfirmedMarkFromAnEarlierProcessAllowsOnlyTheSameMessageOrAnExplicitDiscard() async throws {
        let model = MessageComposerModel(), store = Store()
        let earlier = try AppConversationMessageBody(conversationID: chat, clientMessageID: "msg-earlier-0001", text: "Besorg mir ein Hotel.")
        store.binding = ConversationMessageRetryBinding(body: earlier, coreID: "core", deviceID: "phone")
        model.restoreRetryHint { store.load() }
        XCTAssertNotNil(model.unconfirmed); XCTAssertTrue(model.message.contains("ungeklärt"))
        model.text = "Ein ganz anderer Text."
        var sent: [AppConversationMessageBody] = []
        let refused = await send(model, store: store, submit: { body, _ in sent.append(body); return self.accepted })
        XCTAssertNil(refused); XCTAssertTrue(sent.isEmpty, "a different text is not sent while the earlier one is unresolved")
        XCTAssertFalse(store.log.contains("challenge"))
        // Another chat with the same text is a different message, too.
        model.text = "Besorg mir ein Hotel."
        let otherChat = await send(model, store: store, chat: "c-fedcba9876543210", submit: { body, _ in sent.append(body); return self.accepted })
        XCTAssertNil(otherChat)
        XCTAssertTrue(sent.isEmpty)
        // The same message in the same chat goes out with the earlier id.
        let confirmed = await send(model, store: store, submit: { body, _ in sent.append(body); return self.accepted })
        XCTAssertEqual(confirmed, accepted)
        XCTAssertEqual(sent.map(\.client_message_id), ["msg-earlier-0001"])
        XCTAssertNil(model.unconfirmed)
        // Discard is explicit and clears the mark.
        let again = MessageComposerModel(), fresh = Store()
        fresh.binding = ConversationMessageRetryBinding(body: earlier, coreID: "core", deviceID: "phone")
        again.restoreRetryHint { fresh.load() }
        again.discardUnconfirmed { fresh.clear() }
        XCTAssertNil(again.unconfirmed); XCTAssertNil(fresh.binding); XCTAssertTrue(again.message.isEmpty)
        // A mark that already carries a delivery id is not "unconfirmed".
        let done = MessageComposerModel(), doneStore = Store()
        doneStore.binding = ConversationMessageRetryBinding(body: earlier, coreID: "core", deviceID: "phone", deliveryID: "cd-0123456789abcdef")
        done.restoreRetryHint { doneStore.load() }
        XCTAssertNil(done.unconfirmed)
    }

    func testOverlappingSendIsRefusedAndInvalidTextNeverLeaves() async throws {
        let model = MessageComposerModel(), store = Store()
        model.text = ""
        XCTAssertFalse(model.valid)
        let emptySend = await send(model, store: store, submit: { _, _ in XCTFail("empty text sent"); return self.accepted })
        XCTAssertNil(emptySend)
        XCTAssertTrue(store.log.isEmpty)
        model.text = String(repeating: "x", count: 4001)
        XCTAssertFalse(model.valid)
        model.text = "Besorg mir ein Hotel."
        var overlapping: AppConversationMessageAccepted? = accepted
        let result = await send(model, store: store,
            challenge: { body in
                overlapping = await self.send(model, store: store, submit: { _, _ in XCTFail("overlap submitted"); return self.accepted })
                return try self.challenge(body)
            }, submit: { _, _ in self.accepted })
        XCTAssertEqual(result, accepted)
        XCTAssertNil(overlapping, "the second send during the first is refused")
        XCTAssertEqual(store.log.filter { $0 == "challenge" }.count, 0)
        XCTAssertEqual(store.log.filter { $0.hasPrefix("save:") }.count, 2, "one mark before, one with the delivery id after")
    }

    func testConflictAndUnknownChatAreReportedWithoutClaimingDelivery() async throws {
        let model = MessageComposerModel(), store = Store()
        model.text = "Besorg mir ein Hotel."
        let conflict = await send(model, store: store, submit: { _, _ in throw ClientError.http(409) })
        XCTAssertNil(conflict)
        XCTAssertTrue(model.message.contains("anderem Inhalt")); XCTAssertNotNil(model.unconfirmed)
        XCTAssertFalse(model.message.lowercased().contains("zugestellt."))
        let other = MessageComposerModel(), otherStore = Store()
        other.text = "Besorg mir ein Hotel."
        let vanished = await send(other, store: otherStore, submit: { _, _ in throw ClientError.http(404) })
        XCTAssertNil(vanished)
        XCTAssertTrue(other.message.contains("nicht mehr vorhanden"))
        XCTAssertNil(otherStore.binding, "a message to a vanished chat leaves no mark behind")
    }
}
