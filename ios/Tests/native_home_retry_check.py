#!/usr/bin/env python3
"""Real native task model + Kit retry flow; only device/network/Keychain are local fakes."""
from __future__ import annotations
import argparse, hashlib, json, subprocess, tempfile
from pathlib import Path

SUPPORT = r'''
import Foundation
import SwiftUI
import SolvioApprovalsKit

enum ClientError: Error { case http(Int), decode, badServer }
final class FakeKeychainState: @unchecked Sendable {
    let lock = NSLock()
    var values: [String: Data] = [:]
}
enum Keychain {
    static let state = FakeKeychainState()
    static func reset() { state.lock.lock(); defer { state.lock.unlock() }; state.values = [:] }
    static func load(tag: String) -> Data? { state.lock.lock(); defer { state.lock.unlock() }; return state.values[tag] }
    static func save(_ data: Data, tag: String) { state.lock.lock(); defer { state.lock.unlock() }; state.values[tag] = data }
    static func delete(tag: String) { state.lock.lock(); defer { state.lock.unlock() }; state.values.removeValue(forKey: tag) }
}
@MainActor enum AppAttestManager {
    static var signatures: [Data] = []
    static func assert(keyId: String, clientDataHash: Data) async throws -> Data {
        signatures.append(clientDataHash); return Data([1])
    }
}
let keyID = Data(repeating: 1, count: 32).base64EncodedString()
let homeAccount = ActionServiceAccount(service: "ha", account: "ha-" + String(repeating: "a", count: 32), resource: "configured_home")
let otherAccount = ActionServiceAccount(service: "ha", account: "ha-" + String(repeating: "b", count: 32), resource: "configured_home")
func resources(account: ActionServiceAccount = homeAccount, name: String = "Lampe", state: String = "off") throws -> TaskHomeResources {
    let data = try JSONSerialization.data(withJSONObject: ["service": "ha", "account": account.account, "truncated": false,
        "items": ["fixture", "second"].map { ["target": ["entity_id": "light." + $0], "name": name + ($0 == "second" ? " 2" : ""),
        "area": "Zimmer", "domain": "light", "state": state, "operations": ["set_state", "set_brightness"]] as [String: Any] }])
    return try TaskHomeResources.decode(data, account: account)
}
@MainActor final class ApprovalClient {
    let pairedCoreInstanceID = "core", deviceIdentifier = "phone"
    var challenges = 0, reads = 0
    var rows = [homeAccount]
    var catalogueName = "Lampe", catalogueState = "off"
    var submitted: [AppTaskBody] = []
    var submit: ((AppTaskBody) async throws -> AppTaskAccepted)?
    func taskActionServices() async throws -> [ActionServiceAccount] { rows }
    func taskHomeResources(account: ActionServiceAccount) async throws -> TaskHomeResources {
        reads += 1; return try resources(account: account, name: catalogueName, state: catalogueState)
    }
    func taskPortalSessions() async throws -> TaskPortalSessions { throw ClientError.decode }
    func taskStartChallenge(_ body: AppTaskBody) async throws -> AppTaskChallenge {
        challenges += 1
        let nonce = String(format: "%064x", challenges)
        let fields: [String: Any] = ["protocol_version": 1, "type": "app_task_start_binding",
            "core_instance_id": pairedCoreInstanceID, "principal_id": "owner", "device_id": deviceIdentifier,
            "nonce": nonce, "request_digest": body.requestDigest, "enrollment_id": "enrollment",
            "app_attest_key_id": keyID, "approval_key_sha256": String(repeating: "a", count: 64)]
        let raw = try JSONSerialization.data(withJSONObject: fields, options: [.sortedKeys, .withoutEscapingSlashes])
        return AppTaskChallenge(nonce: nonce, requestDigest: body.requestDigest,
            expiresAt: Date().timeIntervalSince1970 + 30, bindingB64: raw.base64EncodedString())
    }
    func submitTask(_ body: AppTaskBody, proof: AppTaskProof) async throws -> AppTaskAccepted {
        submitted.append(body)
        if let submit { return try await submit(body) }
        return AppTaskAccepted(taskID: "task-local", runID: "ar-0123456789abcdef", state: "accepted")
    }
}
struct CheckFailure: Error { let message: String }
func check(_ value: @autoclosure () -> Bool, _ message: String) throws { if !value() { throw CheckFailure(message: message) } }
@MainActor func ready(_ c: ApprovalClient) async throws -> TaskStartModel {
    let m = TaskStartModel(); m.scope = "action"; m.exactAction = true; m.actionForm.kind = .ha
    await m.loadActionServices(client: c); await m.loadHomeResources(client: c)
    m.selectHomeDevice("light.fixture"); m.actionForm.home.desired = .on
    try check(m.valid, "fixture not valid"); return m
}
'''
CHECKS = r'''
@main struct NativeChecks {
    @MainActor static func main() async throws {
        var results: [[String: Any]] = []
        func run(_ name: String, _ test: () async throws -> Void) async {
            Keychain.reset(); AppAttestManager.signatures = []
            do { try await test(); results.append(["case": name, "passed": true]) }
            catch { results.append(["case": name, "passed": false, "error": String(describing: error)]) }
        }
        await run("already_unknown_unchanged_refresh_reuses_exact_body") {
            let c = ApprovalClient(), m = try await ready(c)
            c.submit = { _ in throw URLError(.networkConnectionLost) }
            await m.send(client: c, keyID: keyID)
            try check(m.hasUnconfirmedRetry, "unknown not recorded")
            let first = c.submitted[0]
            await m.loadHomeResources(client: c); c.submit = nil
            await m.send(client: c, keyID: keyID)
            try check(c.submitted == [first, first] && m.accepted != nil, "unchanged retry changed identity")
            try check(AppAttestManager.signatures.count == 2 && AppAttestManager.signatures[0] != AppAttestManager.signatures[1], "fresh nonce missing")
        }
        await run("refresh_while_submit_pending_retains_uncertainty_and_request") {
            let c = ApprovalClient(), m = try await ready(c)
            var finish: CheckedContinuation<AppTaskAccepted, Error>?
            c.submit = { _ in try await withCheckedThrowingContinuation { finish = $0 } }
            let pending = Task { await m.send(client: c, keyID: keyID) }
            while finish == nil { await Task.yield() }
            let first = c.submitted[0], reads = c.reads
            await m.loadHomeResources(client: c)
            finish?.resume(throwing: URLError(.networkConnectionLost)); await pending.value
            let unknownAfter = m.hasUnconfirmedRetry
            await m.loadHomeResources(client: c); c.submit = nil
            await m.send(client: c, keyID: keyID)
            try check(c.reads == reads + 1, "refresh during send should not fetch")
            try check(c.submitted == [first, first], "same action acquired new request ID after pending refresh")
            try check(unknownAfter && m.accepted != nil, "pending read erased unknown result")
        }
        await run("metadata_refresh_keeps_unknown_retry_and_requested_name") {
            let c = ApprovalClient(), m = try await ready(c)
            c.submit = { _ in throw URLError(.networkConnectionLost) }
            await m.send(client: c, keyID: keyID); let first = c.submitted[0]
            c.catalogueName = "Neue Anzeige"; c.catalogueState = "on"
            await m.loadHomeResources(client: c)
            try check(m.needsRetry && m.hasUnconfirmedRetry, "read metadata hid retry state")
            c.submit = nil; await m.send(client: c, keyID: keyID)
            try check(c.submitted == [first, first], "display name/state changed original task body")
        }
        for changed in ["device", "operation", "account"] {
            await run("explicit_\(changed)_change_cannot_resubmit_unknown_old_action") {
                let c = ApprovalClient(), m = try await ready(c)
                c.submit = { _ in throw URLError(.networkConnectionLost) }
                await m.send(client: c, keyID: keyID); let first = c.submitted[0]
                if changed == "device" { m.selectHomeDevice("light.second"); m.actionForm.home.desired = .on }
                if changed == "operation" { m.actionForm.home.desired = .off }
                if changed == "account" {
                    c.rows = [homeAccount, otherAccount]; await m.loadActionServices(client: c)
                    m.selectActionAccount(otherAccount); await m.loadHomeResources(client: c)
                    m.selectHomeDevice("light.fixture"); m.actionForm.home.desired = .on
                }
                try check(m.valid, "changed input invalid")
                c.submit = nil; await m.send(client: c, keyID: keyID)
                try check(c.submitted == [first] && m.accepted == nil && m.hasUnconfirmedRetry, "changed uncertain action sent")
                try check(TaskRetryCache.load(c)?.requestID == first.client_request_id, "old durable retry replaced")
            }
        }
        await run("account_catalogue_refresh_does_not_hide_unknown_retry") {
            let c = ApprovalClient(), m = try await ready(c)
            c.submit = { _ in throw URLError(.networkConnectionLost) }
            await m.send(client: c, keyID: keyID); let first = c.submitted[0]
            c.rows = [homeAccount, otherAccount]; await m.loadActionServices(client: c)
            try check(m.needsRetry && m.hasUnconfirmedRetry, "unrelated offered account hid retry")
            c.submit = nil; await m.send(client: c, keyID: keyID)
            try check(c.submitted == [first, first], "unchanged chosen account got new request")
        }
        await run("failed_refresh_keeps_retry_but_prevents_stale_dispatch") {
            let c = ApprovalClient(), m = try await ready(c)
            c.submit = { _ in throw URLError(.networkConnectionLost) }
            await m.send(client: c, keyID: keyID); let first = c.submitted[0]
            await m.loadHomeResources(coreID: "core", deviceID: "phone", fetch: { _ in throw URLError(.timedOut) })
            try check(m.needsRetry && m.hasUnconfirmedRetry && !m.valid, "failed read lost retry or kept actionable stale devices")
            c.submit = nil; await m.send(client: c, keyID: keyID)
            try check(c.submitted == [first], "stale catalogue dispatched")
            await m.loadHomeResources(client: c); await m.send(client: c, keyID: keyID)
            try check(c.submitted == [first, first] && m.accepted != nil, "fresh catalogue did not restore exact retry")
        }
        await run("explicit_new_task_after_unknown_gets_distinct_bound_request") {
            let c = ApprovalClient(), m = try await ready(c)
            c.submit = { _ in throw URLError(.networkConnectionLost) }
            await m.send(client: c, keyID: keyID); let first = c.submitted[0]
            m.reset(); m.scope = "action"; m.exactAction = true; m.actionForm.kind = .ha
            await m.loadActionServices(client: c); await m.loadHomeResources(client: c)
            m.selectHomeDevice("light.second"); m.actionForm.home.desired = .off
            c.submit = nil; await m.send(client: c, keyID: keyID)
            try check(c.submitted.count == 2 && m.accepted != nil, "explicit new task could not be submitted")
            let next = c.submitted[1]
            try check(next.client_request_id != first.client_request_id && next.requestDigest != first.requestDigest, "explicit new task reused old binding")
            try check(next.action_request?.actions[0].target == ["entity_id": "light.second"] && next.action_request?.actions[0].payload == ["state": .string("off")], "new task targets differ from user input")
        }
        await run("accepted_task_survives_late_view_refresh_without_resubmission") {
            let c = ApprovalClient(), m = try await ready(c)
            await m.send(client: c, keyID: keyID); let accepted = m.accepted, reads = c.reads
            await m.loadHomeResources(client: c); await m.loadActionServices(client: c)
            await m.send(client: c, keyID: keyID)
            try check(m.accepted == accepted && accepted != nil && c.reads == reads && c.submitted.count == 1, "late view refresh altered accepted state")
        }
        let failed = results.filter { $0["passed"] as? Bool == false }.count
        print(String(decoding: try JSONSerialization.data(withJSONObject: ["passed": results.count - failed,
            "failed": failed, "cases": results, "network_calls": 0, "device_calls": 0,
            "keychain": "temporary_dictionary", "signer": "synthetic"], options: [.sortedKeys]), as: UTF8.self))
    }
}
'''
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--kit-build', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    source = root / 'App/TaskStart.swift'
    raw = source.read_bytes(); text = raw.decode()
    model = text[text.index('private enum TaskRetryCache {'):text.index('struct TaskStartView: View {')]
    build = args.kit_build / 'arm64-apple-macosx/debug'
    objects = sorted((build/'SolvioApprovalsKit.build').glob('*.swift.o'))
    if not objects: raise SystemExit('Missing existing Kit objects; run swift test first.')
    with tempfile.TemporaryDirectory(prefix='solvio-native-home-retry-') as temporary:
        harness = Path(temporary)/'Check.swift'; binary = Path(temporary)/'check'
        harness.write_text(SUPPORT+'\n'+model+'\n'+CHECKS)
        subprocess.run(['xcrun','swiftc','-swift-version','6','-parse-as-library','-I',str(build/'Modules'),str(harness),*map(str,objects),'-o',str(binary)],check=True)
        result = subprocess.run([str(binary)],check=True,capture_output=True,text=True,timeout=30)
        if source.read_bytes() != raw: raise SystemExit('Model changed during probe.')
        proof = json.loads(result.stdout)
        proof.update(source=str(source),source_sha256=hashlib.sha256(raw).hexdigest(),
                     method='verbatim_native_model_and_cache_with_host_substitutes')
        print(json.dumps(proof,ensure_ascii=False,indent=2))
        raise SystemExit(1 if proof['failed'] else 0)
if __name__ == '__main__': main()
