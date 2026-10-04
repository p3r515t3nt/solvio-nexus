import Foundation
import XCTest
import SolvioApprovalsKit
@testable import SolvioApprovals

@MainActor final class TaskCostApprovalModelTests: XCTestCase {
    private let task = "at-1111111111111111"
    private func fixtureRun(cap: Int = 1000, waiting: Bool = true, taskID: String = "at-1111111111111111") throws -> AgentRun {
        let raw: [String:Any] = ["id":"ar-1111111111111111", "aufgabe":taskID, "auftrag":"Erstelle die lokale Tabelle.",
            "zustand":"Wartet", "zustand_code":"WAITING_USER", "offen":true,
            "anbietergrenze":["grund": waiting ? "cost_approval_required" : "", "fortsetzbar":true],
            "kosten":["configured":true,"task_id":taskID,"currency":"EUR","approved_ai_cap_cents":cap,"ask_threshold_cents":1000,
                      "ai_tool":["spent_cents":125,"reserved_cents":75],"counts":["unknown":0]]]
        return try JSONDecoder().decode(AgentRun.self, from: JSONSerialization.data(withJSONObject: raw))
    }
    private func model(_ run: AgentRun, retry: TaskCostApprovalRetryBinding? = nil) -> TaskCostApprovalModel {
        let model = TaskCostApprovalModel()
        model.configure(taskID: run.aufgabe, coreID: "core", deviceID: "phone", retry: retry)
        if retry == nil { model.amount = "20,00" }
        return model
    }
    private func challenge(_ body: AppTaskCostApprovalBody, nonce: String = String(repeating: "b", count: 64)) throws -> AppTaskCostApprovalChallenge {
        let raw: [String:Any] = ["protocol_version":1,"type":"app_task_cost_approval_binding","core_instance_id":"core",
            "principal_id":"owner","device_id":"phone","nonce":nonce,"request_digest":body.requestDigest,
            "enrollment_id":"enrollment","app_attest_key_id":"key","approval_key_sha256":String(repeating:"c",count:64)]
        return .init(nonce:nonce,requestDigest:body.requestDigest,expiresAt:Date().timeIntervalSince1970+30,
            bindingB64:try JSONSerialization.data(withJSONObject:raw,options:[.sortedKeys]).base64EncodedString())
    }
    private func accepted(_ body: AppTaskCostApprovalBody, changedRequest: Bool = false) throws -> AppTaskCostApprovalAccepted {
        let raw: [String:Any] = ["task_id":body.task_id,"client_request_id":changedRequest ? "another-request" : body.client_request_id,
            "max_total_cents":body.max_total_cents,"costs":["configured":true,"task_id":body.task_id,"currency":"EUR","approved_ai_cap_cents":5000]]
        return try JSONDecoder().decode(AppTaskCostApprovalAccepted.self,from:JSONSerialization.data(withJSONObject:raw))
    }
    @MainActor private final class Cache {
        var value: TaskCostApprovalRetryBinding?
        var writes = 0, clears = 0
        func save(_ body: AppTaskCostApprovalBody) throws {
            let next = TaskCostApprovalRetryBinding(body:body,coreID:"core",deviceID:"phone")
            if let value, value != next { throw ClientError.badServer }
            value = next; writes += 1
        }
        func clear(_ body: AppTaskCostApprovalBody) {
            if value?.body == body { value = nil; clears += 1 }
        }
    }
    private func send(_ model: TaskCostApprovalModel, _ run: AgentRun, cache: Cache,
        challenge: ((AppTaskCostApprovalBody) async throws -> AppTaskCostApprovalChallenge)? = nil,
        submit: @escaping (AppTaskCostApprovalBody, AppTaskProof) async throws -> AppTaskCostApprovalAccepted,
        current: @escaping () -> Bool = { true }) async {
        await model.send(run:run,coreID:"core",deviceID:"phone",keyID:"key",loadRetry:{cache.value},
            saveRetry:{try cache.save($0)},clearRetry:{cache.clear($0)},
            challenge:{ body in
                XCTAssertEqual(cache.value?.body,body) // Durable binding precedes even challenge issuance.
                if let challenge { return try await challenge(body) }
                return try self.challenge(body)
            },sign:{_ in Data([1])},submit:submit,isCurrent:current)
    }
    func testNewCapRequiresConfirmedSpentAndReservedWithoutOverflowButExactRetryStillResolves() throws {
        let original = try fixtureRun(cap: 0)
        let model = model(original)
        model.amount = "1,99"; XCTAssertFalse(model.valid(run: original))
        model.amount = "2,00"; XCTAssertTrue(model.valid(run: original))
        let base: [String:Any] = ["id":"ar-1111111111111111", "aufgabe":task, "auftrag":"Tabelle erstellen",
            "zustand":"Wartet", "zustand_code":"WAITING_USER", "offen":true]
        for totals in [["spent_cents":125], ["reserved_cents":75],
                       ["spent_cents":-1,"reserved_cents":75],
                       ["spent_cents":Int.max,"reserved_cents":75],
                       ["spent_cents":1_000_000_000,"reserved_cents":1]] {
            var raw = base
            raw["kosten"] = ["configured":true,"task_id":task,"currency":"EUR","ai_tool":totals]
            let changed = try JSONDecoder().decode(AgentRun.self,from:JSONSerialization.data(withJSONObject:raw))
            XCTAssertFalse(model.valid(run: changed))
        }
        let realCosts: [String:Any] = ["configured":true,"task_id":task,"currency":"EUR",
            "ai_tool":["spent_cents":125,"reserved_cents":75]]
        for change in [("task_id", "at-2222222222222222" as String?), ("currency", "USD"),
                       ("task_id", nil), ("currency", nil)] {
            var costs = realCosts
            if let value = change.1 { costs[change.0] = value } else { costs.removeValue(forKey: change.0) }
            var raw = base; raw["kosten"] = costs
            let changed = try JSONDecoder().decode(AgentRun.self,from:JSONSerialization.data(withJSONObject:raw))
            XCTAssertFalse(model.valid(run:changed))
            XCTAssertNil(TaskCostApprovalModel.scopedCosts(changed))
        }
        let b = try AppTaskCostApprovalBody(taskID:task,maxTotalCents:100,requestID:"original-cost-id")
        model.invalidate()
        model.configure(taskID:task,coreID:"core",deviceID:"phone",
            retry:TaskCostApprovalRetryBinding(body:b,coreID:"core",deviceID:"phone"))
        // This repeats an existing uncertain request; it cannot be changed into a new cap.
        XCTAssertTrue(model.valid(run: original))
    }
    func testExplicitConfirmationOnceAndNoActionWhenUserClosesBeforeSend() async throws {
        let run = try fixtureRun(), m = model(run), cache = Cache()
        XCTAssertNil(cache.value); XCTAssertNil(m.accepted)
        m.invalidate() // Closing before confirmation creates no server decision.
        XCTAssertNil(cache.value); XCTAssertEqual(cache.writes,0)
        m.configure(taskID:task,coreID:"core",deviceID:"phone",retry:nil);m.amount="20,00"
        var sends=0
        let submit: (AppTaskCostApprovalBody,AppTaskProof) async throws -> AppTaskCostApprovalAccepted = { body,_ in
            sends += 1; return try self.accepted(body)
        }
        await send(m,run,cache:cache,submit:submit)
        XCTAssertEqual(sends,1);XCTAssertNotNil(m.accepted);XCTAssertNil(cache.value)
        XCTAssertTrue(m.message.contains("nicht automatisch"))
        await send(m,run,cache:cache,submit:submit)
        XCTAssertEqual(sends,1)
    }
    func testUnknownSurvivesCloseReopenAndHigherCapWithoutPretendingSuccess() async throws {
        let run = try fixtureRun(), m = model(run), cache = Cache()
        var sent:[AppTaskCostApprovalBody]=[];var nonces:[String]=[]
        await send(m,run,cache:cache,submit:{ body,proof in
            sent.append(body);nonces.append(proof.nonce);throw URLError(.timedOut)
        })
        XCTAssertTrue(m.uncertain);XCTAssertTrue(m.amountLocked);XCTAssertNil(m.accepted)
        let saved = try XCTUnwrap(cache.value)
        m.invalidate() // "Später prüfen"/leaving never clears the exact retry.
        XCTAssertEqual(cache.value,saved)
        let refreshed = try self.fixtureRun(cap:5000,waiting:false), reopened = model(refreshed,retry:saved)
        XCTAssertEqual(reopened.amount,"20,00");XCTAssertTrue(reopened.uncertain);XCTAssertNil(reopened.accepted)
        await send(reopened,refreshed,cache:cache,challenge:{ body in try self.challenge(body,nonce:String(repeating:"d",count:64)) },submit:{ body,proof in
            sent.append(body);nonces.append(proof.nonce);return try self.accepted(body)
        })
        XCTAssertEqual(sent.count,2);XCTAssertEqual(sent[0],sent[1]);XCTAssertNotEqual(nonces[0],nonces[1])
        XCTAssertNotNil(reopened.accepted);XCTAssertNil(cache.value)
    }
    func testUnknownAmountCannotChangeAnd409CannotEraseEarlierUncertainty() async throws {
        let run = try fixtureRun(), m=model(run), cache=Cache()
        await send(m,run,cache:cache,submit:{_,_ in throw URLError(.networkConnectionLost)})
        let saved=try XCTUnwrap(cache.value)
        m.amount="30,00"
        await send(m,run,cache:cache,submit:{body,_ in XCTFail("Changed amount sent");return try self.accepted(body)})
        XCTAssertEqual(cache.value,saved);XCTAssertTrue(m.uncertain)
        m.amount="20,00"
        await send(m,run,cache:cache,submit:{_,_ in throw ClientError.http(409)})
        XCTAssertEqual(cache.value,saved);XCTAssertTrue(m.uncertain);XCTAssertNil(m.accepted)
    }
    func testFirstDefiniteRejectionAllowsCorrectionButChangedReceiptIsUnknown() async throws {
        let run=try fixtureRun(),m=model(run),cache=Cache()
        await send(m,run,cache:cache,submit:{_,_ in throw ClientError.http(409)})
        XCTAssertNil(cache.value);XCTAssertFalse(m.uncertain);XCTAssertFalse(m.amountLocked)
        await send(m,run,cache:cache,submit:{body,_ in try self.accepted(body,changedRequest:true)})
        XCTAssertNotNil(cache.value);XCTAssertTrue(m.uncertain);XCTAssertNil(m.accepted)
    }
    func testConcurrentTapDoesNotSendTwiceAndLateResponseCannotRepaintOtherTask() async throws {
        let run=try fixtureRun(),m=model(run),cache=Cache()
        var submits=0
        await send(m,run,cache:cache,challenge:{body in
            await self.send(m,run,cache:cache,submit:{b,_ in XCTFail("Concurrent submit");return try self.accepted(b)})
            return try self.challenge(body)
        },submit:{body,_ in
            submits += 1
            m.configure(taskID:"at-2222222222222222",coreID:"core",deviceID:"phone",retry:nil)
            return try self.accepted(body)
        })
        XCTAssertEqual(submits,1);XCTAssertNil(m.accepted);XCTAssertEqual(m.amount,"")
        XCTAssertNil(cache.value) // A fully bound late receipt may retire only its own marker.
    }
    func testIdentityMismatchAndUnreadableRetryDoNotIssueChallenges() async throws {
        let run=try fixtureRun(),m=model(run),cache=Cache()
        let b=try AppTaskCostApprovalBody(taskID:task,maxTotalCents:2000,requestID:"original-cost-id")
        let foreign=TaskCostApprovalRetryBinding(body:b,coreID:"different",deviceID:"phone")
        m.invalidate();m.configure(taskID:task,coreID:"core",deviceID:"phone",retry:foreign)
        XCTAssertTrue(m.retryUnavailable)
        await send(m,run,cache:cache,submit:{body,_ in XCTFail("Foreign retry submitted");return try self.accepted(body)})
        XCTAssertEqual(cache.writes,0)
        m.invalidate();m.configure(taskID:task,coreID:"core",deviceID:"phone",retry:nil);m.amount="20,00"
        await m.send(run:run,coreID:"core",deviceID:"phone",keyID:"key",loadRetry:{throw ClientError.decode},
            saveRetry:{_ in XCTFail()},clearRetry:{_ in XCTFail()},challenge:{body in XCTFail();return try self.challenge(body)},
            sign:{_ in XCTFail();return Data([1])},submit:{body,_ in XCTFail();return try self.accepted(body)},isCurrent:{true})
        XCTAssertTrue(m.retryUnavailable);XCTAssertNil(m.accepted)
    }
}
