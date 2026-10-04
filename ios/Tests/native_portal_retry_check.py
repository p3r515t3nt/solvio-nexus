#!/usr/bin/env python3
"""Unmodified native model/cache and actual Kit; temporary transport/keychain only."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from native_home_retry_check import SUPPORT as HOME_SUPPORT

SUPPORT = HOME_SUPPORT.replace(
    'func taskPortalSessions() async throws -> TaskPortalSessions { throw ClientError.decode }',
    '''func taskPortalSessions() async throws -> TaskPortalSessions {
        reads += 1; return try portalRows(label: catalogueName)
    }''')
SUPPORT += r'''
let portalAccount = "portal-" + String(repeating: "a", count: 32)
func portalRows(label: String = "Studio", authenticated: Bool = true, empty: Bool = false) throws -> TaskPortalSessions {
    let rows: [[String: Any]] = empty ? [] : [1, 2].map { i in
        ["account": "portal-" + String(repeating: i == 1 ? "a" : "b", count: 32),
         "target": ["portal_id": "studio", "session_id": "ps-123-\(i)"], "label": label,
         "origin": "https://portal.example.invalid", "authenticated": authenticated, "expires_in_s": 300]
    }
    return try TaskPortalSessions.decode(JSONSerialization.data(withJSONObject:
        ["service": "portal", "items": rows, "observed_at": Date().timeIntervalSince1970, "truncated": false]))
}
@MainActor func portalReady(_ c: ApprovalClient) async throws -> TaskStartModel {
    let m = TaskStartModel(); m.scope = "action"; m.exactAction = true; m.actionForm.kind = .portal
    await m.loadPortalSessions(client: c)
    try check(!m.valid && m.actionForm.portal.selectedAccount == nil, "automatic selection")
    m.selectPortalSession(portalAccount); try check(m.valid, "selection not valid")
    return m
}
'''
CHECKS = r'''
@main struct PortalChecks {
    @MainActor static func main() async throws {
        var results: [[String: Any]] = []
        func run(_ name: String, _ test: () async throws -> Void) async {
            Keychain.reset(); AppAttestManager.signatures = []
            do { try await test(); results.append(["case": name, "passed": true]) }
            catch { results.append(["case": name, "passed": false, "error": String(describing: error)]) }
        }
        await run("read_and_select_never_submit") {
            let c = ApprovalClient(), m = try await portalReady(c)
            try check(c.reads == 1 && c.submitted.isEmpty && c.challenges == 0, "read performed action")
            await m.send(client: c, keyID: keyID)
            let body = c.submitted[0], action = body.action_request!.actions[0]
            try check(action.operation == "status" && action.payload.isEmpty && action.target == ["portal_id": "studio", "session_id": "ps-123-1"], "wrong action")
            try check(body.scope == "action" && c.challenges == 1 && m.accepted != nil, "normal original task flow missing")
        }
        await run("unknown_refresh_preserves_body_name_and_fresh_attest") {
            let c = ApprovalClient(), m = try await portalReady(c)
            c.submit = { _ in throw URLError(.networkConnectionLost) }; await m.send(client: c, keyID: keyID)
            let first = c.submitted[0]
            c.catalogueName = "Renamed"; await m.loadPortalSessions(client: c)
            try check(m.hasUnconfirmedRetry && m.needsRetry, "read erased unknown")
            c.submit = nil; await m.send(client: c, keyID: keyID)
            try check(c.submitted == [first, first] && m.accepted != nil, "read replaced original request")
            try check(AppAttestManager.signatures.count == 2 && AppAttestManager.signatures[0] != AppAttestManager.signatures[1], "fresh proof missing")
        }
        await run("pending_submit_cannot_be_invalidated_by_refresh") {
            let c = ApprovalClient(), m = try await portalReady(c)
            var completion: CheckedContinuation<AppTaskAccepted, Error>?
            c.submit = { _ in try await withCheckedThrowingContinuation { completion = $0 } }
            let task = Task { await m.send(client: c, keyID: keyID) }
            while completion == nil { await Task.yield() }
            let first = c.submitted[0], reads = c.reads
            await m.loadPortalSessions(client: c)
            try check(c.reads == reads, "read started during submission")
            completion?.resume(throwing: URLError(.networkConnectionLost)); await task.value
            try check(m.hasUnconfirmedRetry && m.needsRetry, "uncertain submission hidden")
            c.submit = nil; await m.loadPortalSessions(client: c); await m.send(client: c, keyID: keyID)
            try check(c.submitted == [first, first], "pending refresh duplicated logical task")
        }
        await run("failed_or_unauthenticated_read_holds_unknown_retry") {
            let c = ApprovalClient(), m = try await portalReady(c)
            c.submit = { _ in throw URLError(.networkConnectionLost) }; await m.send(client: c, keyID: keyID)
            let first = c.submitted[0]
            await m.loadPortalSessions(coreID: "core", deviceID: "phone", fetch: { throw URLError(.timedOut) })
            try check(!m.valid && m.hasUnconfirmedRetry && m.needsRetry, "stale read actionable")
            await m.loadPortalSessions(coreID: "core", deviceID: "phone", fetch: { try portalRows(authenticated: false) })
            c.submit = nil; await m.send(client: c, keyID: keyID)
            try check(c.submitted == [first] && !m.valid, "unconfirmed session dispatched")
            await m.loadPortalSessions(client: c); await m.send(client: c, keyID: keyID)
            try check(c.submitted == [first, first], "original retry lost")
        }
        await run("different_session_requires_explicit_new_task_after_unknown") {
            let c = ApprovalClient(), m = try await portalReady(c)
            c.submit = { _ in throw URLError(.networkConnectionLost) }; await m.send(client: c, keyID: keyID)
            let first = c.submitted[0]
            m.selectPortalSession("portal-" + String(repeating: "b", count: 32))
            c.submit = nil; await m.send(client: c, keyID: keyID)
            try check(c.submitted == [first] && m.hasUnconfirmedRetry, "changed unknown task dispatched")
            m.reset(); m.scope = "action"; m.exactAction = true; m.actionForm.kind = .portal
            await m.loadPortalSessions(client: c); m.selectPortalSession("portal-" + String(repeating: "b", count: 32))
            await m.send(client: c, keyID: keyID)
            try check(c.submitted.count == 2 && c.submitted[1].client_request_id != first.client_request_id, "explicit new task unavailable")
        }
        await run("late_scope_pairing_and_parallel_replies_discarded") {
            let c = ApprovalClient(), m = try await portalReady(c)
            var reply: CheckedContinuation<TaskPortalSessions, Never>?
            let task = Task { await m.loadPortalSessions(coreID: "core", deviceID: "phone", fetch: { await withCheckedContinuation { reply = $0 } }) }
            while reply == nil { await Task.yield() }
            m.scope = "research"; reply?.resume(returning: try portalRows()); await task.value
            try check(m.actionForm.portal.catalogue == nil && !m.portalLoading, "late old scope populated")
            m.scope = "action"; m.exactAction = true; m.actionForm.kind = .portal
            await m.loadPortalSessions(coreID: "core", deviceID: "phone", fetch: { try portalRows() }, stillConnected: { false })
            try check(m.actionForm.portal.catalogue == nil, "old pairing populated")
            reply = nil
            let first = Task { await m.loadPortalSessions(coreID: "core", deviceID: "phone", fetch: { await withCheckedContinuation { reply = $0 } }) }
            while reply == nil { await Task.yield() }
            await m.loadPortalSessions(coreID: "core", deviceID: "phone", fetch: { throw URLError(.timedOut) })
            reply?.resume(returning: try portalRows()); await first.value
            try check(m.actionForm.portal.catalogue == nil && !m.portalMessage.isEmpty, "older read won")
        }
        await run("empty_catalogue_is_honest_and_accepted_request_stays_closed") {
            let c = ApprovalClient(), m = try await portalReady(c)
            await m.loadPortalSessions(coreID: "core", deviceID: "phone", fetch: { try portalRows(empty: true) })
            try check(!m.valid && m.portalMessage.contains("nicht bestätigt"), "empty catalogue claims connection")
            await m.loadPortalSessions(client: c); await m.send(client: c, keyID: keyID)
            let accepted = m.accepted, reads = c.reads
            await m.loadPortalSessions(client: c); await m.send(client: c, keyID: keyID)
            try check(m.accepted == accepted && c.reads == reads && c.submitted.count == 1, "accepted request reopened")
        }
        let failed = results.filter { $0["passed"] as? Bool == false }.count
        print(String(decoding: try JSONSerialization.data(withJSONObject: ["passed": results.count - failed, "failed": failed,
            "cases": results, "network_calls": 0, "device_calls": 0, "keychain": "temporary_dictionary", "signer": "synthetic"], options: [.sortedKeys]), as: UTF8.self))
    }
}
'''

def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--kit-build', type=Path, required=True); args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]; source = root/'App/TaskStart.swift'
    raw = source.read_bytes(); content = raw.decode()
    model = content[content.index('private enum TaskRetryCache {'):content.index('struct TaskStartView: View {')]
    build = args.kit_build/'arm64-apple-macosx/debug'; objects = sorted((build/'SolvioApprovalsKit.build').glob('*.swift.o'))
    if not objects: raise SystemExit('Missing existing Kit build')
    with tempfile.TemporaryDirectory(prefix='solvio-native-portal-retry-') as temp:
        harness = Path(temp)/'Check.swift'; binary = Path(temp)/'check'
        harness.write_text(SUPPORT+'\n'+model+'\n'+CHECKS)
        subprocess.run(['xcrun','swiftc','-swift-version','6','-parse-as-library','-I',str(build/'Modules'),str(harness),*map(str,objects),'-o',str(binary)], check=True)
        result = subprocess.run([str(binary)], check=True,capture_output=True,text=True,timeout=30)
        if source.read_bytes() != raw: raise SystemExit('Source changed during test')
        proof = json.loads(result.stdout); proof.update(source=str(source),source_sha256=hashlib.sha256(raw).hexdigest())
        print(json.dumps(proof,ensure_ascii=False,indent=2)); raise SystemExit(bool(proof['failed']))
if __name__ == '__main__': main()
