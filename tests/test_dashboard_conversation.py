"""Real dashboard controllers (chat, tasks, action answers) through isolated Node DOM/HTTP/storage ports, no browser."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()


def t_actual_chat_and_task_controllers_keep_identity_results_and_controls():
    node = os.environ.get('SOLVIO_TEST_NODE') or shutil.which('node')
    require(node, 'Existing Node runtime required; no install or skipped assertion')
    result = subprocess.run([node, str(Path(__file__).with_name('dashboard_conversation_check.cjs'))],
                            capture_output=True, text=True, timeout=60)
    # Diagnose statt Datenwust: der Runner kappt die Meldung (Review Runde 8, A8-H1 —
    # ein einzelner roter Lauf unter Fremdlast liess sich danach nicht mehr zuordnen).
    report = json.loads(result.stdout)
    failed = [row for row in report['results'] if row.get('passed') is not True]
    require(not failed, 'failed checks: ' + json.dumps(failed, ensure_ascii=False)[:1500])
    require_equal(result.returncode, 0, 'node exit ' + str(result.returncode) + ': ' + (result.stderr or result.stdout)[-1200:])
    require_equal(report['actualSources'], ['app.js', 'chat.js', 'canonical.js', 'action-intent.js', 'browser-voice.js', 'index.html'])
    require_equal(report['browser'], False)
    require_equal(report['network'], False)
    require_equal(len(report['results']), 68)
    require_equal(len({row['name'] for row in report['results']}), 68)
    for name in ('sending_records_pending_before_fetch', 'reload_reconciles_stored_deliveries', 'blocked_delivery_shows_the_error_line',
                 'task_card_follows_its_triggering_message', 'late_detail_of_a_previous_chat', 'voice_starts_only_by_its_button',
                 'local_storage_holds_identifiers_and_digests_only', 'new_chat_is_idempotent',
                 'a_definitive_503_names_the_reason_and_is_not_uncertain',
                 'source_chat_arrives_without_switching', 'source_chat_title_is_literal',
                 'absent_invalid_and_self_source_chat_metadata'):
        require(any(row['name'].startswith(name) for row in report['results']), name)
    require(all(row['passed'] is True for row in report['results']), 'a check passed only partially')
    # The packaged meter is exercised on generated sample blocks, never a device.
    processor = subprocess.run([node, str(Path(__file__).parent / 'browser' / 'voice_worklet_check.cjs')],
                               capture_output=True, text=True, timeout=20)
    require_equal(processor.returncode, 0, (processor.stderr or processor.stdout)[-1200:])


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
