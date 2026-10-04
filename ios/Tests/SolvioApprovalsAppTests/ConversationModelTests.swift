import Foundation
import XCTest
import SolvioApprovalsKit
@testable import SolvioApprovals

/// Liste, Detail, Zustellungen, Reconcile nach Neustart und die Epoche der
/// Auswahl — alles mit synthetischen Daten, ohne Netz, Core oder Keychain.
@MainActor
final class ConversationModelTests: XCTestCase {
    private let chatA = "c-aaaaaaaaaaaaaaaa", chatB = "c-bbbbbbbbbbbbbbbb"

    private func summary(_ id: String, title: String = "Hotel in Hamburg", tasks: Int = 0, deliveries: Int = 0) -> [String: Any] {
        ["conversation_id": id, "title": title, "kind": "text", "last_activity_at": 1_760_000_000.0,
         "message_count": 2, "open_task_count": tasks, "open_delivery_count": deliveries]
    }
    private func detailJSON(_ id: String, deliveryStatus: String = "completed", errorCode: String = "",
                            runID: String? = nil, clientMessageID: String? = nil, deliveryID: String = "cd-0123456789abcdef",
                            extraRun: [String: Any]? = nil, deliveriesOpen: Int = 0, sourceChat: Any? = nil) throws -> Data {
        var delivery: [String: Any] = ["delivery_id": deliveryID, "status": deliveryStatus, "error_code": errorCode,
                                       "task_id": runID == nil ? "" : "at-0123456789abcdef", "run_id": runID ?? "", "revision": 1]
        if let clientMessageID { delivery["client_message_id"] = clientMessageID }
        if let sourceChat { delivery["source_chat"] = sourceChat }
        var runs: [[String: Any]] = []
        if let runID {
            runs.append(["id": runID, "aufgabe": "at-0123456789abcdef", "auftrag": "Besorg mir ein Hotel.", "zustand": "Läuft",
                         "zustand_code": "RUNNING", "offen": true])
        }
        if let extraRun { runs.append(extraRun) }
        let raw: [String: Any] = ["conversation": summary(id), "deliveries_open": deliveriesOpen, "auftraege": runs,
            "messages": [["message_id": "m-0000000000000001", "sequence": 1, "role": "user", "text": "Besorg mir ein Hotel.",
                          "created_at": 1_760_000_000.0, "delivery": delivery],
                         ["message_id": "m-0000000000000002", "sequence": 2, "role": "assistant", "text": "Ich habe das als Auftrag aufgenommen.",
                          "created_at": 1_760_000_001.0]]]
        return try JSONSerialization.data(withJSONObject: raw)
    }
    private func detail(_ id: String, deliveryStatus: String = "completed", errorCode: String = "", runID: String? = nil,
                        clientMessageID: String? = nil, deliveryID: String = "cd-0123456789abcdef",
                        extraRun: [String: Any]? = nil, deliveriesOpen: Int = 0, sourceChat: Any? = nil) throws -> ConversationDetail {
        try JSONDecoder().decode(ConversationDetail.self, from: detailJSON(id, deliveryStatus: deliveryStatus, errorCode: errorCode,
            runID: runID, clientMessageID: clientMessageID, deliveryID: deliveryID, extraRun: extraRun, deliveriesOpen: deliveriesOpen, sourceChat: sourceChat))
    }
    private func list(_ rows: [[String: Any]]) throws -> [ConversationSummary] {
        try JSONDecoder().decode([ConversationSummary].self, from: JSONSerialization.data(withJSONObject: rows))
    }
    private func binding(_ chat: String, delivery: String? = nil) throws -> ConversationMessageRetryBinding {
        let body = try AppConversationMessageBody(conversationID: chat, clientMessageID: "msg-20260918-0001", text: "Besorg mir ein Hotel.")
        return ConversationMessageRetryBinding(body: body, coreID: "core", deviceID: "phone", deliveryID: delivery)
    }

    override func setUp() { UserDefaults.standard.removeObject(forKey: ConversationModel.selectionKey) }

    func testRoomHistoryStaysReadableButCannotStartPrivateVoiceInIt() async throws {
        let model = ConversationModel(); model.select(chatA)
        var row = summary(chatA); row["read_only"] = true
        let decoded = try list([row])[0]
        XCTAssertTrue(decoded.isReadOnly)
        let room = ConversationDetail(conversation: decoded, messages: [], auftraege: [], deliveriesOpen: 0)
        let result = await model.prepareVoiceConversation(create: { _ in
            XCTFail("a private replacement must not be silently created")
            throw URLError(.badURL)
        }, load: { _ in room })
        XCTAssertNil(result); XCTAssertEqual(model.selectedID, chatA)
        XCTAssertEqual(model.detail?.conversation.isReadOnly, true)
        XCTAssertTrue(model.detailError.contains("Raumgespräch"))
    }

    func testOlderCoreChatsRemainWritableWithoutOptionalRoomFlag() throws {
        XCTAssertFalse(try list([summary(chatA)])[0].isReadOnly)
    }

    func testVoiceCreatesCanonicalChatThenLoadsItBeforeStartAndKeepsExistingSelection() async throws {
        let model = ConversationModel()
        var events: [String] = []
        let new = await model.prepareVoiceConversation(create: { _ in
            events.append("create")
            return ConversationCreated(conversation_id: self.chatA, title: "", kind: "text", created_at: 1)
        }, load: { id in
            XCTAssertEqual(model.selectedID, self.chatA)
            events.append("load:" + id)
            return try self.detail(id)
        })
        XCTAssertEqual(new, chatA); XCTAssertEqual(events, ["create", "load:" + chatA])
        let existing = await model.prepareVoiceConversation(create: { _ in
            XCTFail("selected chat must not create another conversation")
            throw URLError(.badURL)
        }, load: { id in try self.detail(id) })
        XCTAssertEqual(existing, chatA)
    }

    func testVoiceRefusesDeletedMismatchedAndChangedSelection() async throws {
        let model = ConversationModel(); model.select(chatA)
        func noCreate(_ id: String) async throws -> ConversationCreated { throw URLError(.badURL) }
        let gone = await model.prepareVoiceConversation(create: noCreate, load: { _ in throw ClientError.http(404) })
        XCTAssertNil(gone); XCTAssertEqual(model.selectedID, chatA)
        let mismatch = await model.prepareVoiceConversation(create: noCreate, load: { _ in try self.detail(self.chatB) })
        XCTAssertNil(mismatch)
        let changed = await model.prepareVoiceConversation(create: noCreate, load: { _ in
            model.select(self.chatB); return try self.detail(self.chatA)
        })
        XCTAssertNil(changed); XCTAssertEqual(model.selectedID, chatB)
    }

    func testSourceChatArrivalKeepsCurrentSelectionUntilExplicitNavigation() async throws {
        let model = ConversationModel()
        // The source may be older than the current page of the chat list.
        await model.refreshList { try self.list([self.summary(self.chatB)]) }
        model.select(chatB)
        await model.refreshDetail { id in try self.detail(id, deliveryStatus: "running") }
        XCTAssertNil(model.detail?.messages.first?.delivery?.source_chat)
        await model.refreshDetail { id in
            try self.detail(id, sourceChat: ["conversation_id": self.chatA, "title": "Hotel in Hamburg"])
        }
        XCTAssertEqual(model.selectedID, chatB)
        XCTAssertEqual(model.detail?.conversation.conversation_id, chatB)
        XCTAssertEqual(UserDefaults.standard.string(forKey: ConversationModel.selectionKey), chatB)
        let source = try XCTUnwrap(model.detail?.messages.first?.delivery?.sourceChat(from: chatB))
        XCTAssertEqual(source.conversation_id, chatA)
        XCTAssertEqual(source.displayTitle, "Hotel in Hamburg")
        // The source button uses the same selection as the existing chat list.
        model.select(source.conversation_id)
        XCTAssertNil(model.detail)
        await model.refreshDetail { id in
            XCTAssertEqual(id, self.chatA)
            return try self.detail(id)
        }
        XCTAssertEqual(model.selectedID, chatA)
        XCTAssertEqual(model.detail?.conversation.conversation_id, chatA)
        XCTAssertEqual(UserDefaults.standard.string(forKey: ConversationModel.selectionKey), chatA)
    }

    func testOptionalSourceChatNeverMakesOldOrMalformedHistoryUnreadable() throws {
        let invalid: [Any?] = [nil, NSNull(), "https://example.invalid", [] as [String], [:] as [String: String],
            ["conversation_id": "https://example.invalid", "title": "URL"],
            ["conversation_id": chatA + "\n", "title": "Ungültig"],
            ["conversation_id": [chatA], "title": "Ungültig"] as [String: Any],
            ["conversation_id": chatA, "title": 17] as [String: Any]]
        for source in invalid {
            let value = try detail(chatB, sourceChat: source)
            XCTAssertEqual(value.messages.count, 2)
            XCTAssertEqual(value.messages.first?.text, "Besorg mir ein Hotel.")
            XCTAssertNil(value.messages.first?.delivery?.source_chat)
        }
        let same = try detail(chatB, sourceChat: ["conversation_id": chatB, "title": "Derselbe Chat"])
        XCTAssertNil(same.messages.first?.delivery?.sourceChat(from: chatB))
    }

    func testSourceChatTitleRemainsLiteralAndBounded() throws {
        let title = "<img src=x onerror=alert(1)> [Hotel](https://example.invalid)"
        let value = try detail(chatB, sourceChat: ["conversation_id": chatA, "title": title, "url": "https://example.invalid"])
        let source = try XCTUnwrap(value.messages.first?.delivery?.sourceChat(from: chatB))
        XCTAssertEqual(source.displayTitle, title)
        let empty = try detail(chatB, sourceChat: ["conversation_id": chatA, "title": "  "])
        XCTAssertEqual(empty.messages.first?.delivery?.source_chat?.displayTitle, "Neuer Chat")
        let long = try detail(chatB, sourceChat: ["conversation_id": chatA, "title": String(repeating: "ä", count: 120)])
        XCTAssertEqual(long.messages.first?.delivery?.source_chat?.displayTitle.count, 80)
    }

    func testWireShapesDecodeAndAnUnreadableTaskCardDoesNotHideTheHistory() throws {
        let value = try detail(chatA, deliveryStatus: "blocked", errorCode: "quota", runID: "ar-0123456789abcdef",
                               extraRun: ["id": "ar-broken", "unexpected": true], deliveriesOpen: 0)
        XCTAssertEqual(value.messages.count, 2)
        XCTAssertEqual(value.messages[0].delivery?.status, "blocked")
        XCTAssertTrue(value.messages[0].delivery?.isBlocked == true)
        XCTAssertEqual(value.auftraege.map(\.id), ["ar-0123456789abcdef"], "the unreadable card falls away alone")
        XCTAssertEqual(ConversationErrorText.text("quota"), "Das Kontingent ist erschöpft. Bitte später erneut senden.")
        XCTAssertTrue(ConversationErrorText.text("task_start_refused:policy").contains("policy"))
        for code in ["cost_recovery_required", "quota", "provider_unavailable", "provider_output_invalid", "assessment_unavailable",
                     "processing_timeout", "source_revoked", "followup_not_available", "objective_too_long", "core_restarted_unresolved", "", "x"] {
            XCTAssertFalse(ConversationErrorText.text(code).lowercased().contains("erfolg"), code)
        }
        XCTAssertTrue(value.hasOpenWork, "an open task counts as work")
        XCTAssertFalse(try detail(chatA).hasOpenWork)
        XCTAssertTrue(try detail(chatA, deliveryStatus: "running").hasOpenWork)
        XCTAssertThrowsError(try JSONDecoder().decode(ConversationDetail.self, from: Data(#"{"messages":[]}"#.utf8)))
    }

    func testListDrivesTheGlobalWorkingIndicatorAndPollInterval() async throws {
        let model = ConversationModel()
        await model.refreshList { try self.list([self.summary(self.chatA, tasks: 1, deliveries: 2), self.summary(self.chatB)]) }
        XCTAssertEqual(model.conversations.count, 2)
        XCTAssertEqual(model.workingCount, 3)
        XCTAssertEqual(model.pollNanoseconds, ConversationModel.idlePoll, "no chat selected: idle rhythm")
        model.select(chatA)
        await model.refreshDetail { _ in try self.detail(self.chatA, deliveryStatus: "accepted", deliveriesOpen: 1) }
        XCTAssertEqual(model.pollNanoseconds, ConversationModel.workingPoll)
        await model.refreshDetail { _ in try self.detail(self.chatA) }
        XCTAssertEqual(model.pollNanoseconds, ConversationModel.idlePoll)
        await model.refreshList { throw URLError(.notConnectedToInternet) }
        XCTAssertFalse(model.listError.isEmpty)
        XCTAssertEqual(model.conversations.count, 2, "a failed read keeps the last list")
    }

    func testLateResultOfAnotherChatNeverOverwritesTheSelection() async throws {
        let model = ConversationModel()
        model.select(chatA)
        let gate = Gate()
        let slow = Task { @MainActor in
            await model.refreshDetail { id in
                await gate.wait()
                return try self.detail(id)
            }
        }
        await Task.yield(); await Task.yield()
        model.select(chatB)
        XCTAssertNil(model.detail)
        await model.refreshDetail { id in try self.detail(id, deliveryStatus: "running") }
        XCTAssertEqual(model.detail?.conversation.conversation_id, chatB)
        await gate.open()
        await slow.value
        XCTAssertEqual(model.detail?.conversation.conversation_id, chatB, "chat A's late answer must not repaint chat B")
        XCTAssertEqual(model.detail?.messages.first?.delivery?.status, "running")
        // And a detail whose ID differs from the requested one is refused.
        model.select(chatA)
        await model.refreshDetail { _ in try self.detail(self.chatB) }
        XCTAssertNil(model.detail); XCTAssertFalse(model.detailError.isEmpty)
    }

    func testSelectionIsRememberedAsIdentifierOnlyAndOnlyWhenTheListKnowsIt() async throws {
        let model = ConversationModel()
        model.select(chatA)
        XCTAssertEqual(UserDefaults.standard.string(forKey: ConversationModel.selectionKey), chatA)
        let fresh = ConversationModel()
        await fresh.refreshList { try self.list([self.summary(self.chatB)]) }
        fresh.restoreSelection()
        XCTAssertNil(fresh.selectedID, "an unknown identifier is not restored")
        await fresh.refreshList { try self.list([self.summary(self.chatA), self.summary(self.chatB)]) }
        fresh.restoreSelection()
        XCTAssertEqual(fresh.selectedID, chatA)
        model.select("not-a-chat")
        XCTAssertEqual(model.selectedID, chatA, "an invalid identifier is ignored")
        model.invalidate()
        XCTAssertNil(model.selectedID); XCTAssertTrue(model.conversations.isEmpty)
    }

    func testCreateIsIdempotentAcrossRetriesAndSelectsTheNewChat() async throws {
        let model = ConversationModel()
        var seen: [String] = []
        await model.createConversation { id in seen.append(id); throw URLError(.timedOut) }
        XCTAssertFalse(model.createError.isEmpty); XCTAssertNil(model.selectedID)
        let created = await model.createConversation { id in
            seen.append(id)
            return ConversationCreated(conversation_id: self.chatB, title: "", kind: "text", created_at: 1)
        }
        XCTAssertEqual(created, chatB)
        XCTAssertEqual(seen.count, 2); XCTAssertEqual(seen[0], seen[1], "the retry reuses the same client_request_id")
        XCTAssertEqual(model.selectedID, chatB)
        XCTAssertEqual(model.conversations.first?.conversation_id, chatB)
        let next = await model.createConversation { id in
            seen.append(id)
            return ConversationCreated(conversation_id: self.chatA, title: "", kind: "text", created_at: 2)
        }
        XCTAssertEqual(next, chatA)
        XCTAssertNotEqual(seen[2], seen[1], "after success a new chat is a new request")
    }

    func testOldCreationCannotReturnAfterConnectionInvalidationOrClearNewCreation() async {
        let model = ConversationModel()
        let oldGate = Gate(), newGate = Gate()
        var oldStarted = false, newStarted = false
        var oldRequest = "", newRequest = ""
        let old = Task { @MainActor in
            await model.createConversation { id in
                oldRequest = id; oldStarted = true
                await oldGate.wait()
                return ConversationCreated(conversation_id: self.chatA, title: "Alter Zugang", kind: "text", created_at: 1)
            }
        }
        while !oldStarted { await Task.yield() }
        model.invalidate()
        XCTAssertFalse(model.creating)
        let fresh = Task { @MainActor in
            await model.createConversation { id in
                newRequest = id; newStarted = true
                await newGate.wait()
                return ConversationCreated(conversation_id: self.chatB, title: "Neuer Zugang", kind: "text", created_at: 2)
            }
        }
        for _ in 0..<100 where !newStarted { await Task.yield() }
        XCTAssertTrue(newStarted)
        await oldGate.open()
        let stale = await old.value
        XCTAssertNil(stale)
        XCTAssertTrue(model.conversations.isEmpty)
        XCTAssertNil(model.selectedID)
        XCTAssertTrue(model.creating, "late completion cannot clear the current request's progress")
        await newGate.open()
        let created = await fresh.value
        XCTAssertEqual(created, chatB)
        XCTAssertNotEqual(oldRequest, newRequest)
        XCTAssertEqual(model.conversations.map(\.conversation_id), [chatB])
        XCTAssertEqual(model.selectedID, chatB)
        XCTAssertFalse(model.creating)
    }

    func testOldCreationFailureCannotPolluteNewConnection() async {
        let model = ConversationModel(), gate = Gate()
        var started = false
        let old = Task { @MainActor in
            await model.createConversation { _ in
                started = true; await gate.wait(); throw URLError(.timedOut)
            }
        }
        while !started { await Task.yield() }
        model.invalidate()
        await gate.open(); _ = await old.value
        XCTAssertTrue(model.createError.isEmpty)
        XCTAssertNil(model.selectedID)
        XCTAssertFalse(model.creating)
    }

    func testReconcileAfterRestartResolvesOnlyWhenTheHistoryShowsTheDeliveryAndNeverResends() async throws {
        let model = ConversationModel()
        // The 202 was received: the history carries the delivery id → resolved silently.
        let accepted = try binding(chatA, delivery: "cd-0123456789abcdef")
        let byDelivery = await model.reconcile(pending: accepted) { id in try self.detail(id) }
        XCTAssertEqual(byDelivery, .resolved)
        XCTAssertTrue(model.pendingHint.isEmpty)
        // No 202: the Core reports the client_message_id on the delivery → resolved.
        let unknown = try binding(chatA)
        let byClientID = await model.reconcile(pending: unknown) { id in try self.detail(id, clientMessageID: "msg-20260918-0001") }
        XCTAssertEqual(byClientID, .resolved)
        // No 202 and nothing matching → unresolved with a hint, and no send closure exists at all.
        let unresolved = await model.reconcile(pending: unknown) { id in try self.detail(id, clientMessageID: "msg-other") }
        XCTAssertEqual(unresolved, .unresolved)
        XCTAssertTrue(model.pendingHint.contains("möglicherweise nicht angekommen"))
        XCTAssertTrue(model.pendingHint.contains("nichts automatisch gesendet"))
        let unavailable = await model.reconcile(pending: unknown) { _ in throw URLError(.notConnectedToInternet) }
        XCTAssertEqual(unavailable, .unavailable)
        XCTAssertFalse(model.pendingHint.isEmpty)
        let gone = await model.reconcile(pending: unknown) { _ in throw ClientError.http(404) }
        XCTAssertEqual(gone, .resolved, "a deleted chat resolves the mark")
        XCTAssertTrue(model.pendingHint.isEmpty)
    }

    func testRefreshDetailClearsAPendingMarkOnlyWhenItsDeliveryAppearsInThisChat() async throws {
        let model = ConversationModel()
        model.select(chatA)
        var cleared = 0
        let pending = try binding(chatA, delivery: "cd-0123456789abcdef")
        await model.refreshDetail(load: { id in try self.detail(id, deliveryID: "cd-fedcba9876543210") },
                                  pending: pending, clearPending: { cleared += 1 })
        XCTAssertEqual(cleared, 0, "another delivery does not clear the mark")
        await model.refreshDetail(load: { id in try self.detail(id) }, pending: try binding(chatB, delivery: "cd-0123456789abcdef"),
                                  clearPending: { cleared += 1 })
        XCTAssertEqual(cleared, 0, "a mark for another chat is not cleared here")
        await model.refreshDetail(load: { id in try self.detail(id) }, pending: pending, clearPending: { cleared += 1 })
        XCTAssertEqual(cleared, 1)
    }
}

actor Gate {
    private var opened = false
    private var waiters: [CheckedContinuation<Void, Never>] = []
    func wait() async {
        if opened { return }
        await withCheckedContinuation { waiters.append($0) }
    }
    func open() {
        opened = true
        for waiter in waiters { waiter.resume() }
        waiters.removeAll()
    }
}
