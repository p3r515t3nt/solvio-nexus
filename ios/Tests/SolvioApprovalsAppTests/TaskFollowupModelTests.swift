import Foundation
import XCTest
import SwiftUI
import SolvioApprovalsKit
@testable import SolvioApprovals

@MainActor
final class TaskFollowupModelTests: XCTestCase {
    private let parent = "ar-1111111111111111", task = "at-1111111111111111", artifact = "aa-1111111111111111"
    private func fixtureRun(id: String = "ar-1111111111111111", revision: Int = 1, eligible: Bool = true,
                     otherTask: Bool = false, size: Int = 100, filesVisible: Bool = true) throws -> AgentRun {
        let file: [String: Any] = ["id": artifact, "name": "Analyse.xlsx", "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "size": size, "sha256": String(repeating: "b", count: 64), "preview_kind": "none",
            "download_url": "/v1/agent/runs/\(id)/artifacts/\(artifact)/download"]
        let raw: [String: Any] = ["id": id, "aufgabe": otherTask ? "at-2222222222222222" : task,
            "auftrag": "Analysiere meine Umsätze.", "zustand": "Fertig", "zustand_code": "SUCCEEDED", "offen": false,
            "ergebnis": "Die Auswertung ist fertig.", "dateien": filesVisible ? [file] : [],
            "angelegt": Double(revision), "task_revision": ["revision": revision, "digest": String(repeating: "a", count: 64),
                "text": revision == 1 ? "Analysiere meine Umsätze." : "Ergänze den März.", "parent_run_id": revision == 1 ? "" : parent],
            "followup": ["eligible": eligible, "reason": eligible ? "" : "newer_revision_exists"]]
        return try JSONDecoder().decode(AgentRun.self, from: JSONSerialization.data(withJSONObject: raw))
    }
    private func challenge(_ body: AppTaskFollowupBody, nonce: String = String(repeating: "b", count: 64)) throws -> AppTaskFollowupChallenge {
        let raw: [String: Any] = ["protocol_version": 1, "type": "app_task_followup_binding", "core_instance_id": "core",
            "principal_id": "owner", "device_id": "phone", "nonce": nonce, "request_digest": body.requestDigest,
            "enrollment_id": "enrollment", "app_attest_key_id": "key", "approval_key_sha256": String(repeating: "c", count: 64)]
        return .init(nonce: nonce, requestDigest: body.requestDigest, expiresAt: Date().timeIntervalSince1970 + 30,
            bindingB64: try JSONSerialization.data(withJSONObject: raw, options: [.sortedKeys]).base64EncodedString())
    }
    private func accepted(_ body: AppTaskFollowupBody) -> AppTaskFollowupAccepted {
        .init(taskID: task, runID: "ar-2222222222222222", parentRunID: body.run_id, revision: body.expected_revision + 1,
            digest: String(repeating: "d", count: 64), admission: "ready")
    }
    private func configured(_ run: AgentRun, retry: TaskFollowupRetryBinding? = nil) -> TaskFollowupModel {
        let model = TaskFollowupModel(); model.configure(run: run, coreID: "core", deviceID: "phone", retry: retry)
        model.text = "Ergänze den März."; return model
    }
    func testOneInputIsVisibleDefaultAndPollDoesNotEraseEditingButRunChangeDoes() throws {
        let run = try fixtureRun(), model = configured(run)
        XCTAssertEqual(model.selectedIDs, [artifact]); XCTAssertTrue(model.valid(run: run))
        model.configure(run: run, coreID: "core", deviceID: "phone", retry: nil)
        XCTAssertEqual(model.text, "Ergänze den März.")
        model.toggle(artifact, run: run); XCTAssertTrue(model.selectedIDs.isEmpty)
        model.configure(run: try self.fixtureRun(id: "ar-2222222222222222", revision: 2), coreID: "core", deviceID: "phone", retry: nil)
        XCTAssertTrue(model.text.isEmpty); XCTAssertNil(model.accepted); XCTAssertFalse(model.uncertain)
    }
    func testLostReplySurvivesFreshModelAndEditedTextCannotReplaceIt() async throws {
        let run = try fixtureRun(), first = configured(run)
        var retry: TaskFollowupRetryBinding?, sent: [AppTaskFollowupBody] = [], proofs: [AppTaskProof] = []
        await first.send(run: run, coreID: "core", deviceID: "phone", keyID: "key",
            loadRetry: { retry }, saveRetry: { retry = .init(body: $0, coreID: "core", deviceID: "phone") }, clearRetry: { _ in retry = nil },
            challenge: { try self.challenge($0) }, sign: { _ in Data([1]) }, submit: { b, p in
                sent.append(b); proofs.append(p); throw URLError(.networkConnectionLost)
            }, isCurrent: { true })
        XCTAssertTrue(first.uncertain); XCTAssertNotNil(retry)
        let fresh = configured(run, retry: retry); fresh.text = "Ein völlig anderer Auftrag."
        await fresh.send(run: run, coreID: "core", deviceID: "phone", keyID: "key", loadRetry: { retry }, saveRetry: { _ in XCTFail() }, clearRetry: { _ in XCTFail() },
            challenge: { b in XCTFail(); return try self.challenge(b) }, sign: { _ in XCTFail(); return Data([1]) },
            submit: { b, _ in XCTFail(); return self.accepted(b) }, isCurrent: { true })
        XCTAssertTrue(fresh.uncertain); XCTAssertEqual(sent.count, 1)
        fresh.toggle(artifact, run: run); XCTAssertEqual(fresh.selectedIDs, [artifact])
        fresh.text = "Ergänze den März."
        await fresh.send(run: try self.fixtureRun(eligible: false), coreID: "core", deviceID: "phone", keyID: "key",
            loadRetry: { retry }, saveRetry: { b in XCTAssertEqual(b, sent[0]) }, clearRetry: { _ in retry = nil },
            challenge: { try self.challenge($0, nonce: String(repeating: "c", count: 64)) }, sign: { _ in Data([2]) },
            submit: { b, p in sent.append(b); proofs.append(p); return self.accepted(b) }, isCurrent: { true })
        XCTAssertEqual(sent.count, 2); XCTAssertEqual(sent[0], sent[1]); XCTAssertNotEqual(proofs[0].nonce, proofs[1].nonce)
        XCTAssertNil(retry); XCTAssertNotNil(fresh.accepted); XCTAssertFalse(fresh.uncertain)
    }
    func testFirstDefinitiveRejectionClearsButEarlierUnknownResponseRemainsFrozen() async throws {
        let run = try fixtureRun()
        for earlierUnknown in [false, true] {
            let body = try AppTaskFollowupBody(runID: parent, text: "Ergänze den März.", revision: 1,
                digest: String(repeating: "a", count: 64), inputIDs: [artifact], requestID: "original-followup")
            var retry: TaskFollowupRetryBinding? = earlierUnknown ? .init(body: body, coreID: "core", deviceID: "phone") : nil
            let model = configured(run, retry: retry)
            await model.send(run: run, coreID: "core", deviceID: "phone", keyID: "key",
                loadRetry: { retry }, saveRetry: { retry = .init(body: $0, coreID: "core", deviceID: "phone") }, clearRetry: { _ in retry = nil },
                challenge: { _ in throw ClientError.http(409) }, sign: { _ in XCTFail(); return Data([1]) },
                submit: { b, _ in XCTFail(); return self.accepted(b) }, isCurrent: { true })
            XCTAssertEqual(model.uncertain, earlierUnknown); XCTAssertEqual(retry != nil, earlierUnknown)
        }
    }
    func testClientChangeWhileChallengeIsPendingPreventsSigningAndKeepsRecoveryBinding() async throws {
        let run = try fixtureRun(), model = configured(run)
        var resume: CheckedContinuation<AppTaskFollowupChallenge, Never>?, issuedBody: AppTaskFollowupBody?
        var current = true, retry: TaskFollowupRetryBinding?, signed = 0, submitted = 0
        let pending = Task { await model.send(run: run, coreID: "core", deviceID: "phone", keyID: "key",
            loadRetry: { retry }, saveRetry: { retry = .init(body: $0, coreID: "core", deviceID: "phone") }, clearRetry: { _ in retry = nil },
            challenge: { b in issuedBody = b; return await withCheckedContinuation { resume = $0 } },
            sign: { _ in signed += 1; return Data([1]) }, submit: { b, _ in submitted += 1; return self.accepted(b) }, isCurrent: { current }) }
        while resume == nil { await Task.yield() }
        current = false; model.invalidate()
        resume?.resume(returning: try challenge(XCTUnwrap(issuedBody))); await pending.value
        XCTAssertEqual(signed, 0); XCTAssertEqual(submitted, 0); XCTAssertNotNil(retry)
        XCTAssertNil(model.accepted); XCTAssertFalse(model.sending); XCTAssertFalse(model.uncertain)
    }
    func testLateKnownAcceptanceClearsOnlyItsMetadataAndNeverPaintsAnotherSelection() async throws {
        let run = try fixtureRun(), model = configured(run)
        var resume: CheckedContinuation<AppTaskFollowupAccepted, Never>?, submitted: AppTaskFollowupBody?
        var retry: TaskFollowupRetryBinding?, current = true, cleared: [String] = []
        let pending = Task { await model.send(run: run, coreID: "core", deviceID: "phone", keyID: "key",
            loadRetry: { retry }, saveRetry: { retry = .init(body: $0, coreID: "core", deviceID: "phone") },
            clearRetry: { b in cleared.append(b.requestDigest); if retry?.bodyDigest == b.requestDigest { retry = nil } },
            challenge: { try self.challenge($0) }, sign: { _ in Data([1]) },
            submit: { b, _ in submitted = b; return await withCheckedContinuation { resume = $0 } }, isCurrent: { current }) }
        while resume == nil { await Task.yield() }
        current = false; model.invalidate(); resume?.resume(returning: accepted(try XCTUnwrap(submitted))); await pending.value
        XCTAssertEqual(cleared, [submitted!.requestDigest]); XCTAssertNil(retry); XCTAssertNil(model.accepted); XCTAssertTrue(model.text.isEmpty)
    }
    func testMalformedAcceptanceRetainsRetryAndUnavailableOrOversizedInputCannotSubmit() async throws {
        let run = try fixtureRun(), model = configured(run)
        var retry: TaskFollowupRetryBinding?
        await model.send(run: run, coreID: "core", deviceID: "phone", keyID: "key",
            loadRetry: { retry }, saveRetry: { retry = .init(body: $0, coreID: "core", deviceID: "phone") }, clearRetry: { _ in XCTFail() },
            challenge: { try self.challenge($0) }, sign: { _ in Data([1]) }, submit: { b, _ in
                .init(taskID: "at-2222222222222222", runID: "ar-2222222222222222", parentRunID: b.run_id,
                    revision: 2, digest: String(repeating: "d", count: 64), admission: "ready")
            }, isCurrent: { true })
        XCTAssertTrue(model.uncertain); XCTAssertNil(model.accepted); XCTAssertNotNil(retry)
        let noFile = try self.fixtureRun(size: AppTaskFileInput.byteLimit + 1)
        let restarted = configured(noFile, retry: retry)
        XCTAssertTrue(restarted.valid(run: noFile), "An exact previous request does not re-admit current file metadata")
        restarted.text = "Eine andere Ergänzung."
        XCTAssertFalse(restarted.valid(run: noFile))
        let unavailable = try self.fixtureRun(eligible: false), fresh = configured(unavailable)
        await fresh.send(run: unavailable, coreID: "core", deviceID: "phone", keyID: "key", loadRetry: { nil }, saveRetry: { _ in XCTFail() }, clearRetry: { _ in XCTFail() },
            challenge: { b in XCTFail(); return try self.challenge(b) }, sign: { _ in XCTFail(); return Data([1]) },
            submit: { b, _ in XCTFail(); return self.accepted(b) }, isCurrent: { true })
        XCTAssertFalse(fresh.uncertain)
    }
    func testExactPersistedRetryCanResolveAfterParentFileDisappearsIncludingReopenedView() async throws {
        let original = try fixtureRun(), missing = try fixtureRun(eligible: false, filesVisible: false)
        for reopen in [false, true] {
            let first = configured(original)
            var retry: TaskFollowupRetryBinding?, sent: [AppTaskFollowupBody] = [], proofs: [AppTaskProof] = []
            await first.send(run: original, coreID: "core", deviceID: "phone", keyID: "key",
                loadRetry: { retry }, saveRetry: { retry = .init(body: $0, coreID: "core", deviceID: "phone") }, clearRetry: { _ in retry = nil },
                challenge: { try self.challenge($0) }, sign: { _ in Data([1]) }, submit: { b, p in
                    sent.append(b); proofs.append(p); throw URLError(.networkConnectionLost)
                }, isCurrent: { true })
            XCTAssertTrue(first.uncertain); XCTAssertNotNil(retry)
            let next = reopen ? configured(missing, retry: retry) : first
            if !reopen { next.configure(run: missing, coreID: "core", deviceID: "phone", retry: retry) }
            XCTAssertEqual(next.selectedIDs, [artifact]); XCTAssertTrue(next.valid(run: missing))
            XCTAssertFalse(String(decoding: try JSONEncoder().encode(XCTUnwrap(retry)), as: UTF8.self).contains(first.text))
            await next.send(run: missing, coreID: "core", deviceID: "phone", keyID: "key",
                loadRetry: { retry }, saveRetry: { b in XCTAssertEqual(b, sent[0]) }, clearRetry: { _ in retry = nil },
                challenge: { try self.challenge($0, nonce: String(repeating: "c", count: 64)) }, sign: { _ in Data([2]) },
                submit: { b, p in sent.append(b); proofs.append(p); return self.accepted(b) }, isCurrent: { true })
            XCTAssertEqual(sent.count, 2); XCTAssertEqual(sent[0], sent[1]); XCTAssertNotEqual(proofs[0].nonce, proofs[1].nonce)
            XCTAssertNil(retry); XCTAssertNotNil(next.accepted); XCTAssertFalse(next.uncertain)
        }
    }
    func testMissingParentFileNeverAuthorizesChangedOrForeignRetry() async throws {
        let original = try fixtureRun(), missing = try fixtureRun(filesVisible: false)
        let body = try AppTaskFollowupBody(runID: parent, text: "Ergänze den März.", revision: 1,
            digest: String(repeating: "a", count: 64), inputIDs: [artifact], requestID: "original-followup")
        for otherCore in [false, true] {
            let retry = TaskFollowupRetryBinding(body: body, coreID: otherCore ? "other" : "core", deviceID: "phone")
            let model = configured(missing, retry: retry)
            if !otherCore { model.text = "Ein anderer Inhalt." }
            XCTAssertFalse(model.valid(run: missing))
            await model.send(run: missing, coreID: "core", deviceID: "phone", keyID: "key",
                loadRetry: { retry }, saveRetry: { _ in XCTFail() }, clearRetry: { _ in XCTFail() },
                challenge: { b in XCTFail(); return try self.challenge(b) }, sign: { _ in XCTFail(); return Data([1]) },
                submit: { b, _ in XCTFail(); return self.accepted(b) }, isCurrent: { true })
            XCTAssertNil(model.accepted)
        }
        let fresh = configured(original)
        XCTAssertFalse(fresh.valid(run: missing))
        await fresh.send(run: missing, coreID: "core", deviceID: "phone", keyID: "key", loadRetry: { nil },
            saveRetry: { _ in XCTFail() }, clearRetry: { _ in XCTFail() }, challenge: { b in XCTFail(); return try self.challenge(b) },
            sign: { _ in XCTFail(); return Data([1]) }, submit: { b, _ in XCTFail(); return self.accepted(b) }, isCurrent: { true })
        XCTAssertFalse(fresh.uncertain)
    }
    func testTaskListKeepsNewestRevisionOnceAndOtherTasksRemainSeparate() throws {
        let original = try fixtureRun(), second = try fixtureRun(id: "ar-2222222222222222", revision: 2),
            other = try fixtureRun(id: "ar-3333333333333333", otherTask: true)
        XCTAssertEqual(AgentResultsModel.latestTasks([original, other, second]).map(\.id), [second.id, other.id])
        XCTAssertEqual(AgentResultsModel.latestTasks([second, original]).map(\.id), [second.id])
        XCTAssertEqual(original.auftrag, second.auftrag)
    }
    func testFollowupFieldRendersAtNarrowAndLargestTextWithoutAConnection() async throws {
        let app = AppModel.visualPreview(), run = try fixtureRun()
        guard app.client == nil else { throw XCTSkip("Requires unpaired synthetic UI") }
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.first as? UIWindowScene)
        for (width, size) in [(390.0, DynamicTypeSize.large), (320.0, DynamicTypeSize.accessibility5)] {
            let controller = UIHostingController(rootView: ScrollView {
                TaskFollowupView(app: app, run: run, available: false, onAccepted: { _ in XCTFail() }).padding()
            }.environment(\.dynamicTypeSize, size).preferredColorScheme(.dark))
            let window = UIWindow(windowScene: scene); window.frame = CGRect(x: 0, y: 0, width: width, height: 844)
            window.rootViewController = controller; window.makeKeyAndVisible()
            try await Task.sleep(nanoseconds: 100_000_000)
            controller.view.frame = window.bounds; controller.view.layoutIfNeeded()
            let image = UIGraphicsImageRenderer(size: window.bounds.size).image { _ in controller.view.drawHierarchy(in: window.bounds, afterScreenUpdates: true) }
            let attachment = XCTAttachment(image: image); attachment.name = "followup-\(Int(width))-\(size)"; attachment.lifetime = .keepAlways; add(attachment)
            XCTAssertNil(app.client); window.isHidden = true; window.rootViewController = nil
        }
    }
}
