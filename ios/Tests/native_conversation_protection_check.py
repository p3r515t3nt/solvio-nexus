#!/usr/bin/env python3
"""Compile the actual question-navigation state against isolated Swift cases.

No UI, network, Core, Keychain, App Attest, simulator or package installation.
This verifies the state transitions; it does not claim SwiftUI lifecycle proof.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile


CHECKS = r'''
import Foundation

struct CheckFailure: Error { let message: String }
func check(_ value: @autoclosure () -> Bool, _ message: String) throws {
    if !value() { throw CheckFailure(message: message) }
}
@main struct ConversationProtectionChecks {
    static func main() throws {
        let first = "run-a:question-1:revision-1:digest-a"
        let second = "run-a:question-2:revision-2:digest-b"
        var results: [[String: Any]] = []
        func run(_ name: String, _ test: () throws -> Void) {
            do { try test(); results.append(["case": name, "passed": true]) }
            catch { results.append(["case": name, "passed": false, "error": String(describing: error)]) }
        }
        run("current_question_can_report_before_parent_appears") {
            var state = AgentQuestionNavigationProtection()
            try check(!state.isProtected(for: nil), "empty view blocks navigation")
            state.report(true, for: first, current: first)
            state.synchronize(first)
            try check(state.isProtected(for: first), "parent appearance erased pending answer")
        }
        run("unchanged_snapshot_and_read_failure_preserve_uncertain_answer") {
            var state = AgentQuestionNavigationProtection()
            state.report(true, for: first, current: first)
            // A failed read leaves the last Core question intact, as the
            // existing AgentResultsModel explicitly does.
            for _ in 0..<3 { state.synchronize(first) }
            try check(state.isProtected(for: first), "unchanged question erased uncertainty")
            state.report(false, for: first, current: first)
            try check(!state.isProtected(for: first), "confirmed answer cannot release protection")
        }
        run("confirmed_question_removal_releases_without_an_old_child_callback") {
            var state = AgentQuestionNavigationProtection()
            state.report(true, for: first, current: first)
            try check(!state.isProtected(for: nil), "removed question remains protected before synchronization")
            state.synchronize(nil)
            state.report(true, for: first, current: nil)
            try check(!state.isProtected(for: nil), "late removed child restored protection")
            state.synchronize(first)
            try check(!state.isProtected(for: first), "cleared protection was retained")
        }
        run("confirmed_new_question_does_not_inherit_old_uncertainty") {
            var state = AgentQuestionNavigationProtection()
            state.report(true, for: first, current: first)
            try check(!state.isProtected(for: second), "new question inherited old lock before synchronization")
            state.synchronize(second)
            try check(!state.isProtected(for: second), "new question inherited old lock")
            state.report(true, for: first, current: second)
            try check(!state.isProtected(for: second), "late old pending callback locked new question")
        }
        run("late_old_success_cannot_unlock_the_new_pending_question") {
            var state = AgentQuestionNavigationProtection()
            state.report(true, for: first, current: first)
            state.synchronize(second)
            state.report(true, for: second, current: second)
            state.report(false, for: first, current: second)
            try check(state.isProtected(for: second), "old success unlocked another pending answer")
        }
        run("another_run_and_unoffered_question_cannot_change_current_protection") {
            var state = AgentQuestionNavigationProtection()
            state.report(true, for: first, current: first)
            for foreign in ["run-b:question-1:revision-1:digest-a", second] {
                state.report(false, for: foreign, current: first)
                try check(state.isProtected(for: first), "foreign callback unlocked current answer")
            }
        }
        let failed = results.filter { $0["passed"] as? Bool == false }.count
        print(String(decoding: try JSONSerialization.data(withJSONObject:
            ["passed": results.count - failed, "failed": failed, "cases": results],
            options: [.sortedKeys]), as: UTF8.self))
    }
}
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mutations', action='store_true')
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1] / 'App/AgentResults.swift'
    raw = source.read_bytes()
    text = raw.decode()
    helper = text[text.index('struct AgentQuestionNavigationProtection {'):
                  text.index('/// No durable task copy:')]
    variants = {'current': helper}
    if args.mutations:
        replacements = {
            'old_callback_not_bound': ('guard reportedBindingID == currentBindingID else { return }', ''),
            'old_uncertainty_kept_on_question_change': ('bindingID = currentBindingID\n        protected = false', 'bindingID = currentBindingID'),
        }
        for name, (before, after) in replacements.items():
            if helper.count(before) != 1:
                raise SystemExit('Mutation seam changed: ' + name)
            variants[name] = helper.replace(before, after)
    results = {}
    with tempfile.TemporaryDirectory(prefix='solvio-conversation-protection-') as temporary:
        folder = Path(temporary)
        for name, body in variants.items():
            swift = folder / (name + '.swift')
            executable = folder / name
            swift.write_text(body + '\n' + CHECKS)
            subprocess.run(['xcrun', 'swiftc', '-swift-version', '6', '-parse-as-library',
                            str(swift), '-o', str(executable)], check=True,
                           capture_output=True, text=True, timeout=60)
            result = subprocess.run([str(executable)], check=True, capture_output=True,
                                    text=True, timeout=10)
            results[name] = json.loads(result.stdout)
    if source.read_bytes() != raw:
        raise SystemExit('Product source changed during check.')
    proof = {'source': str(source), 'source_sha256': hashlib.sha256(raw).hexdigest(),
             'method': 'verbatim_native_question_protection_with_host_cases',
             'ui_executed': False, 'network_calls': 0, 'device_calls': 0,
             **results['current']}
    if args.mutations:
        proof['mutations'] = {name: result for name, result in results.items() if name != 'current'}
        proof['mutations_killed'] = sum(result['failed'] > 0 for name, result in results.items() if name != 'current')
    print(json.dumps(proof, ensure_ascii=False, indent=2))
    raise SystemExit(1 if proof['failed'] or any(result['failed'] == 0 for name, result in results.items() if name != 'current') else 0)


if __name__ == '__main__':
    main()
