#!/usr/bin/env python3
"""Exercise the actual native answer model/cache with temporary host substitutes.

No app build, signing, Keychain, networking, device or provider access. Swift
compiles the verbatim cache/model slice from App/ActionQuestion.swift against
the existing SwiftPM Kit build; only client, attestation and Keychain are fake.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile


SUPPORT = r'''
import Foundation
import SwiftUI
import SolvioApprovalsKit

enum ClientError: Error { case http(Int), decode, badServer }
@MainActor enum Keychain {
    static var values: [String: Data] = [:]
    static func load(tag: String) -> Data? { values[tag] }
    static func save(_ data: Data, tag: String) { values[tag] = data }
    static func delete(tag: String) { values.removeValue(forKey: tag) }
}
@MainActor enum AppAttestManager {
    static var signatures: [Data] = []
    static func assert(keyId: String, clientDataHash: Data) async throws -> Data {
        signatures.append(clientDataHash); return Data([1])
    }
}
@MainActor final class ApprovalClient {
    let pairedCoreInstanceID = "core", deviceIdentifier = "phone"
    var challengeStatus: Int?
    var challenges = 0
    var submitted: [AppTaskActionAnswerBody] = []
    var submit: ((AppTaskActionAnswerBody) async throws -> AppTaskActionAnswerAccepted)?
    func actionAnswerChallenge(_ body: AppTaskActionAnswerBody) async throws -> AppTaskActionAnswerChallenge {
        challenges += 1
        if let challengeStatus { throw ClientError.http(challengeStatus) }
        let nonce = String(format: "%064x", challenges)
        let fields: [String: Any] = ["protocol_version": 1, "type": "app_action_answer_binding",
            "core_instance_id": pairedCoreInstanceID, "principal_id": "owner", "device_id": deviceIdentifier,
            "nonce": nonce, "request_digest": body.requestDigest, "enrollment_id": "enrollment",
            "app_attest_key_id": keyID, "approval_key_sha256": String(repeating: "a", count: 64)]
        let raw = try JSONSerialization.data(withJSONObject: fields, options: [.sortedKeys, .withoutEscapingSlashes])
        return AppTaskActionAnswerChallenge(nonce: nonce, requestDigest: body.requestDigest,
            expiresAt: Date().timeIntervalSince1970 + 30, bindingB64: raw.base64EncodedString())
    }
    func submitActionAnswer(_ body: AppTaskActionAnswerBody, proof: AppTaskActionAnswerProof) async throws -> AppTaskActionAnswerAccepted {
        submitted.append(body)
        if let submit { return try await submit(body) }
        return try accepted()
    }
}
let runID = "ar-0123456789abcdef"
let keyID = Data(repeating: 1, count: 32).base64EncodedString()
func accepted() throws -> AppTaskActionAnswerAccepted {
    try AppTaskActionAnswerAccepted(actionIntent: ActionIntentSnapshot(status: .interpreting))
}
func question(_ revision: Int = 1) throws -> ActionIntentQuestion {
    try ActionIntentQuestion(id: "aiq-" + String(repeating: "b", count: 32), revision: revision,
        digest: String(repeating: revision == 1 ? "c" : "d", count: 64), field: .duration,
        prompt: "Wie lange dauert der Termin?", inputType: .number)
}
struct CheckFailure: Error { let message: String }
func check(_ condition: @autoclosure () -> Bool, _ message: String) throws {
    if !condition() { throw CheckFailure(message: message) }
}
'''

CHECKS = r'''
@main struct NativeChecks {
    @MainActor static func reset() { Keychain.values = [:]; AppAttestManager.signatures = [] }
    @MainActor static func main() async throws {
        var names: [String] = []
        reset()
        do {
            let c = ApprovalClient(), m = ActionQuestionModel(), q = try question()
            m.answer = "60"
            await m.send(client: c, keyID: keyID, runID: runID, question: q)
            try check(m.accepted && !m.uncertain && c.submitted.count == 1, "success not confirmed")
            try check(ActionAnswerRetryCache.load(client: c, runID: runID, question: q) == nil, "success cache retained")
            await m.send(client: c, keyID: keyID, runID: runID, question: q)
            try check(c.submitted.count == 1, "accepted model resubmitted")
            names.append("confirmed_success_clears_only_its_retry")
        }
        reset()
        do {
            let c = ApprovalClient(), m = ActionQuestionModel(), q = try question()
            c.submit = { _ in throw URLError(.networkConnectionLost) }; m.answer = "60"
            await m.send(client: c, keyID: keyID, runID: runID, question: q)
            try check(m.uncertain && !m.accepted, "lost response not uncertain")
            let first = c.submitted[0]
            m.answer = "90"
            await m.send(client: c, keyID: keyID, runID: runID, question: q)
            try check(c.challenges == 1 && c.submitted.count == 1, "changed uncertain answer dispatched")
            m.answer = "60"; c.submit = nil
            await m.send(client: c, keyID: keyID, runID: runID, question: q)
            try check(m.accepted && c.submitted == [first, first], "retry body changed")
            try check(AppAttestManager.signatures.count == 2 && AppAttestManager.signatures[0] != AppAttestManager.signatures[1], "nonce reused")
            names.append("uncertain_answer_retained_and_exact_retry_fresh_nonce")
        }
        for stage in ["challenge", "submit"] {
            reset()
            let c = ApprovalClient(), original = ActionQuestionModel(), q = try question()
            c.submit = { _ in throw URLError(.networkConnectionLost) }; original.answer = "60"
            await original.send(client: c, keyID: keyID, runID: runID, question: q)
            let old = ActionAnswerRetryCache.load(client: c, runID: runID, question: q)
            // Fresh view proves persisted uncertainty, not merely an in-memory flag.
            let restarted = ActionQuestionModel(); restarted.answer = "60"
            if stage == "challenge" { c.challengeStatus = 403 }
            else { c.submit = { _ in throw ClientError.http(403) } }
            await restarted.send(client: c, keyID: keyID, runID: runID, question: q)
            try check(restarted.uncertain && !restarted.accepted, "\(stage) retry denial erased prior uncertainty")
            try check(ActionAnswerRetryCache.load(client: c, runID: runID, question: q) == old, "\(stage) retry denial erased metadata")
            names.append("unknown_prior_submit_survives_\(stage)_403_after_view_restart")
        }
        reset()
        do {
            let c = ApprovalClient(), q1 = try question(), q2 = try question(2)
            let old = try AppTaskActionAnswerBody(runID: runID, question: q1, answer: "60", requestID: "answer-old-001")
            let newer = try AppTaskActionAnswerBody(runID: runID, question: q2, answer: "90", requestID: "answer-new-001")
            try ActionAnswerRetryCache.save(newer, client: c, question: q2)
            let stored = ActionAnswerRetryCache.load(client: c, runID: runID, question: q2)
            _ = ActionAnswerRetryCache.load(client: c, runID: runID, question: q1)
            try check(ActionAnswerRetryCache.load(client: c, runID: runID, question: q2) == stored, "old load erased newer cache")
            do { try ActionAnswerRetryCache.save(old, client: c, question: q1) } catch {}
            try check(ActionAnswerRetryCache.load(client: c, runID: runID, question: q2) == stored, "old save overwrote newer cache")
            names.append("old_question_load_and_save_preserve_newer_revision")
        }
        reset()
        do {
            let c = ApprovalClient(), old = ActionQuestionModel(), newer = ActionQuestionModel()
            let q1 = try question(), q2 = try question(2)
            var resume: CheckedContinuation<AppTaskActionAnswerAccepted, Error>?
            c.submit = { _ in try await withCheckedThrowingContinuation { resume = $0 } }
            old.answer = "60"
            let pending = Task { await old.send(client: c, keyID: keyID, runID: runID, question: q1) }
            while resume == nil { await Task.yield() }
            c.submit = { _ in throw URLError(.networkConnectionLost) }; newer.answer = "90"
            await newer.send(client: c, keyID: keyID, runID: runID, question: q2)
            let retry = ActionAnswerRetryCache.load(client: c, runID: runID, question: q2)
            try check(retry != nil && newer.uncertain, "new answer was not stored")
            resume?.resume(returning: try accepted()); await pending.value
            try check(old.accepted, "old success missing")
            try check(ActionAnswerRetryCache.load(client: c, runID: runID, question: q2) == retry, "delayed old success erased new retry")
            names.append("delayed_previous_success_cannot_clear_new_question_retry")
        }
        let result: [String: Any] = ["passed": names.count, "failed": 0, "cases": names,
            "network_calls": 0, "device_calls": 0, "signer": "synthetic", "keychain": "temporary_dictionary"]
        print(String(decoding: try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys]), as: UTF8.self))
    }
}
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--kit-build', type=Path, default=Path('/tmp/solvio-natural-actions-ios-kit-build'))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    source = root / 'App/ActionQuestion.swift'
    raw = source.read_bytes()
    text = raw.decode()
    model = text[text.index('@MainActor\nenum ActionAnswerRetryCache {'):text.index('struct ActionQuestionView: View {')]
    build = args.kit_build / 'arm64-apple-macosx/debug'
    objects = sorted((build / 'SolvioApprovalsKit.build').glob('*.swift.o'))
    if not objects:
        raise SystemExit('Run swift test with --scratch-path first; no Kit objects found.')
    with tempfile.TemporaryDirectory(prefix='solvio-native-action-question-') as temporary:
        harness = Path(temporary) / 'Check.swift'
        binary = Path(temporary) / 'check'
        harness.write_text(SUPPORT + '\n' + model + '\n' + CHECKS)
        subprocess.run(['xcrun', 'swiftc', '-swift-version', '6', '-parse-as-library',
                        '-I', str(build / 'Modules'), str(harness), *map(str, objects), '-o', str(binary)], check=True)
        result = subprocess.run([str(binary)], check=True, capture_output=True, text=True)
        if source.read_bytes() != raw:
            raise SystemExit('Model source changed during the probe; result is not final.')
        proof = json.loads(result.stdout)
        proof.update(source=str(source), source_sha256=hashlib.sha256(raw).hexdigest(),
                     method='verbatim_native_model_and_cache_with_host_substitutes')
        print(json.dumps(proof, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
