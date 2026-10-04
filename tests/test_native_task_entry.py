"""Public task scope through real Core lifecycle, costs, tools and result stores.

Criteria/assessment use the existing local CLI protocol fixture. Only the
native provider turn is simulated; no real model, account or production state.
"""
from contextlib import asynccontextmanager
import asyncio
import json
import hashlib
import os
import shlex
from pathlib import Path
import sys
import types
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'), str(Path(__file__).resolve().parent)]
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
import test_agent_task_entry as E
import test_agent_task_followup_entry as F
import test_agent_portal_cost_dispatch as PORTAL
from test_agent_cost_runtime import _cli
from solvio.agent_runtime import checkpoint as CP, cost_dispatch as D, costs as C, planner as PL
from solvio.agent_runtime import native_result_files as NRF
import test_native_core_tools as NCT
from _native_worker_seams import claude_module_double
from solvio.agent_runtime import native_tasks as N, specialists as SP, store as S, task_revisions as TR
from solvio.agent_runtime import provider_switch as PS, result_files as RF, checkpoint as CP
from solvio.capabilities.agent import SPECS, AgentCapabilities
from solvio.specialists import native_task as NT, hermes_native as H, providers as P, launcher as L
from solvio.specialists.result import SpecialistResult
from solvio.specialists.subscription import SubscriptionTransport

OBJECTIVE = 'Nenne die lokal konfigurierten Portale und liefere den Befund als Textdatei.'
CRITERIA = {'anforderungen': {'auskunft': [{'id': 'a1', 'text': 'Nenne die lokal konfigurierten Portale.'}],
    'handlungen': [{'id': 'h1', 'text': 'Liefere den gelesenen Portalbestand als Textdatei.', 'effect': 'file'}],
    'unklar': [], 'belege': {'mindestens': 0}}}
SOURCE = 'https://example.org/synthetic-local-evidence'
VERDICT = {'beantwortet': [{'id': 'a1', 'belege': [SOURCE]}],
    'offen': [], 'fehlend': [], 'unsicher': [], 'weiterarbeit_noetig': False}
FREE = D.CostQuote(0, C.CostEvidence('free_local', 'test:public-native-task'))


#: The proven non-start as the broker book shows it (N8_C4_API.md §2.4):
#: exclusively refusals with HTTP 429 and zero output tokens.
NONSTART_BOOK = {'rows': 2, 'requests': 2, 'input_tokens': 84, 'output_tokens': 0, 'tokens': 84,
                 'outcomes': {'upstream_error': 2}, 'status_codes': {'429': 2}}
#: A book that says the provider answered once — never a non-start.
ANSWERED_BOOK = {'rows': 2, 'requests': 2, 'input_tokens': 84, 'output_tokens': 5, 'tokens': 89,
                 'outcomes': {'forwarded': 1, 'upstream_error': 1}, 'status_codes': {'200': 1, '429': 1}}
CLAUDE_SESSION = '11111111-2222-4333-8444-555555555555'
HELPER_SOURCE = '''import csv
import json
import sys


def total(path, column):
    with open(path, newline='') as stream:
        return sum(float(row[column]) for row in csv.DictReader(stream))


if __name__ == '__main__':
    print(json.dumps({'sum': total(sys.argv[1], sys.argv[2])}))
'''


class _BrokerDouble:
    """The running broker as the Claude path sees it: a port and a task book."""
    def __init__(self, book):
        self.port = 48791
        self.ledger = self
        self.queries = []

    def usage_for_task(self, task_ref, *, since):
        self.queries.append((task_ref, since))
        return dict(self.book)


def _claude_module():
    """Synthetic double of implementer A's `claude_native_task` (§2.1/§2.2 surface)."""
    module = types.ModuleType('solvio.specialists.claude_native_task')

    class ClaudeWorkerConfig:
        def __init__(self, *, jail, broker_port, mcp_mode, session_mode):
            self.jail, self.broker_port, self.mcp_mode, self.session_mode = jail, broker_port, mcp_mode, session_mode

    module.ClaudeWorkerConfig = ClaudeWorkerConfig
    module.claude_task_policy = lambda config: hashlib.sha256(json.dumps(
        {'protocol': 'solvio-claude-task-v1-test', 'mcp_mode': config.mcp_mode,
         'session_mode': config.session_mode}, sort_keys=True).encode()).hexdigest()
    module.session_mode = lambda: 'handover'
    module.mcp_mode = lambda: 'none'
    module.worker_canary_verdict = lambda: ''
    return module


@asynccontextmanager
async def world(*, criteria=CRITERIA, mode='ok', objective=OBJECTIVE, local_check=False, unrelated_check=False,
                composite=False, worker='codex', book=None, rework=None, criteria_sequence=None,
                assessment_sequence=None, finding='Der lokale Portalbestand wurde gelesen.',
                recommended_path='Der Befund liegt als Datei bereit.'):
    """`rework` (N8/C4 §3.5): how the double behaves in the ONE rework turn the Core
    may grant — 'fix' performs the required local check only then, 'stale_file'
    writes a wrong file first and the right one on rework, 'fail' brings no result."""
    async with E.world() as w:
        folder = Path(w.ledger.path).resolve().parent
        # E1: both roots live in the isolated world, never under the owner's
        # `~/.solvio-tasks`. The jail root must stay short: its endpoint is
        # `<jail root>/<task_id>/sock/<8>/core.sock` under the AF_UNIX limit
        # (104 bytes), so it sits directly on the short private temp base
        # (production: 84 bytes under ~/.solvio-tasks/claude-jails). Task
        # leaves are unique; the world removes only its own leaves.
        from _private_temp import short_private_base
        jail_root = short_private_base() / 'j'
        roots = {'AGENT_RUNTIME_TASK_WORKSPACE_ROOT': str(folder / 'task-workspaces'),
                 'AGENT_RUNTIME_CLAUDE_JAIL_ROOT': str(jail_root),
                 'AGENT_RUNTIME_TASK_WORKER': worker}
        os.environ.update(roots)
        w.roots = roots
        caps = AgentCapabilities(w.orch)
        w.router.register(SPECS['agent_task_task'], caps.task)
        PORTAL.P.register(w.router, PORTAL.P.PortalCapabilities(PORTAL.NoPortalClient(),
            PORTAL.PortalVault(str(folder / 'absent-vault'))))
        executable = _cli(folder, w.ledger.path, plans=list(criteria_sequence or [criteria]),
                          assessments=list(assessment_sequence or [VERDICT]))
        source = executable.read_text()
        source = source.replace("'pid':os.getpid()", "'pid':os.getpid(), 'request':request, "
                                "'system':(messages[0]['content'] if '--json' in sys.argv else '')")
        marker = "    text = reply if isinstance(reply, str) else json.dumps(reply)"
        source = source.replace(marker, '''    if kind == 'assessment':
        snapshot = json.loads(request['ergebnis'])
        effects = request.get('gepruefte_handlungsbelege', {}).get('eintraege', [])
        matched = next((snapshot[item['feld']][item['index']] for item in effects if item['id'] == 'h1'), None)
        # A deliberately overconfident assessor also assigns a generic file
        # receipt when the Core provided no verified effect (external case).
        matched = matched or next((text for text in snapshot['befunde'] if text.startswith('Core-Dateibeleg:')), '')
        reply = dict(reply, beantwortet=reply['beantwortet'] + [{'id':'h1', 'belege':[matched]}])
        if effects and any(item.get('effect') == 'file' for item in request['gebundene_anforderungen']['handlungen']) and not any('portale' in text and 'Native result revision' in text for text in snapshot['befunde']):
            reply = dict(reply, beantwortet=[], offen=['a1','h1'], fehlend=['Actual file readback missing'])
''' + marker)
        if local_check:
            source = source.replace(marker, """    if kind == 'assessment':
        observed = '\n'.join(snapshot['befunde'])
        checked = 'PORTAL_READBACK_OK' in observed and 'assert isinstance(data' in observed
        # This fixture represents the independent semantic judgement. A real
        # exit-zero command remains insufficient when it did another job.
        information = next((text for text in snapshot['befunde'] if 'portal_list' in text and 'portale' in text), '')
        checked = checked and bool(information)
        reply = dict(reply, beantwortet=[{'id':'a1','belege':[information]}, {'id':'h1','belege':[matched]}] if checked else [],
            offen=[] if checked else ['a1','h1'],
            fehlend=[] if checked else ['The observed command did not perform the required local check'])
""".replace("'\n'", "'\\n'") + marker)
        if composite:
            source = source.replace(marker, """    if kind == 'assessment':
        web = next((text for text in snapshot['befunde'] if 'webSearch' in text and 'openPage' in text and 'https://docs.python.org/3/library/csv.html' in text), '')
        reply['beantwortet'].append({'id':'a2', 'belege':[web]})
        for key in ('f1', 'f2'):
            matched_file = next((snapshot[item['feld']][item['index']] for item in effects if item['id'] == key), '')
            reply['beantwortet'].append({'id':key, 'belege':[matched_file]})
""" + marker)
        executable.write_text(source)
        hermes = folder / 'hermes/agent/transports'
        hermes.mkdir(parents=True)
        (hermes / 'codex_app_server_session.py').write_text('# Never imported by this provider fixture.\n')
        home = folder / 'native-home'
        home.mkdir(mode=0o700)
        (home / 'config.toml').write_text(H.native_config_text('local-test'))
        config = H.NativeResearchConfig(sys.executable, str(folder / 'hermes'), '/usr/bin/true', str(home), 'local-test')
        w.orch.planner = PL.Planner(subscription_transport=SubscriptionTransport('codex', timeout=5))
        w.orch.cost_quote_adapter = lambda *_: FREE
        w.calls = []
        w.refusals = []
        w.mode = mode
        w.objective = objective
        w.observed_receipts = []
        w.declared_helpers = ()
        w.threads = {}

        def thread_for(task_id, first):
            """One native thread id per task (UNIQUE per provider in the ledger)."""
            if task_id not in w.threads:
                w.threads[task_id] = first if not w.threads else first[:-1] + str(len(w.threads) + 1)
            return w.threads[task_id]

        async def native(request, *, config, continuation, bridge, on_event=None):
            session = continuation.sessions.session(continuation.session_id)
            w.calls.append((request, session))
            reworking = N.REWORK_HEADER in request.context
            unrelated_now = unrelated_check if rework != 'fix' else not reworking
            if reworking and rework == 'quota_then_fix' and getattr(w, 'rework_parks_left', 1) > 0:
                # The ONE rework turn meets a proven quota non-start first (H11-6);
                # `rework_parks_left` lets a test park it more than once (W12-1).
                w.rework_parks_left = getattr(w, 'rework_parks_left', 1) - 1
                w.rework_parked = True
                return SP.SpecialistRun(result=SpecialistResult(role='worker', provider='codex',
                    question=request.objective, ok=False, reason='quota'), quota=True,
                    provider='codex', billing_mode='subscription', dispatch_started=False)
            if rework == 'quota_then_fix':
                unrelated_now = not reworking
            if w.mode == 'quota_before':
                return SP.SpecialistRun(result=SpecialistResult(role='worker', provider='codex',
                    question=request.objective, ok=False, reason='quota'), quota=True,
                    provider='codex', billing_mode='subscription', dispatch_started=False)
            require_equal(w.ledger.get_run(request.run_id).state, S.RUNNING)
            bound = TR.task_view(w.ledger, request.run_id).requirements
            require(json.loads(bound)['auskunft'][0]['id'] in request.context)
            require('KANONISCHER ANFORDERUNGSVERTRAG' in request.context)
            turn_id = 'turn-' + str(len(w.calls))
            thread = thread_for(session.task_id, 'thread-one')
            revision = TR.revision_for_run(w.ledger, request.run_id)['revision']
            async def perform(*_):
                claim = continuation.sessions.active_claim(request.run_id)
                turn, fresh = continuation.sessions.request_turn(session_id=session.session_id,
                    run_id=request.run_id, revision=revision, invocation_id=claim['invocation_id'])
                require(fresh)
                continuation.sessions.bind_thread(turn.invocation_id, thread)
                continuation.sessions.started(turn.invocation_id, native_thread_id=thread, native_turn_id=turn_id)
                if on_event is not None:
                    on_event({'schema': 1, 'type': 'event', 'event': 'started', 'seq': 1,
                        'runtime': 'hermes-codex-app-server', 'model': 'local-test',
                        'thread_id': thread, 'turn_id': turn_id})
                if w.mode == 'lost':
                    continuation.sessions.unknown(turn.invocation_id)
                    return L.Outcome(False, reason='provider_failed', process_started=True, exit_code=None)
                response = await bridge.adapter.call({'threadId': thread, 'turnId': turn_id,
                    'callId': 'portal-read', 'tool': 'portal_list', 'arguments': {}})
                require(response['success'], response)
                stale = rework == 'stale_file' and not reworking
                (Path(session.workspace) / 'answer.txt').write_text(('Vorlaeufig ' if stale else 'Native result revision ') + str(revision)
                    + '\n' + json.dumps(json.loads(response['contentItems'][0]['text'])['data'], ensure_ascii=False))
                w.observed_receipts = [{'kind': 'dynamicToolCall', 'item_id': 'portal-read',
                    'status': 'completed', 'tool': 'portal_list', 'success': True}]
                if w.router.spec('result_files_list') is not None:
                    # With the product tool registered the worker also lists its
                    # own result descriptors through the REAL bridge (§4).
                    listed = await bridge.adapter.call({'threadId': thread, 'turnId': turn_id,
                        'callId': 'files-read', 'tool': 'result_files_list', 'arguments': {}})
                    require(listed['success'], listed)
                    w.observed_receipts.append({'kind': 'dynamicToolCall', 'item_id': 'files-read',
                        'status': 'completed', 'tool': 'result_files_list', 'success': True})
                if w.router.spec('gmail_search') is not None:
                    # Stufe S1: with the mail reads registered the worker searches
                    # and reads one mail through the REAL bridge.
                    for call_id, tool, arguments in (
                            ('mail-search', 'gmail_search', {'query': 'rechnung', 'limit': 3}),
                            ('mail-read', 'gmail_read_message', {'message_id': 'm1'})):
                        read = await bridge.adapter.call({'threadId': thread, 'turnId': turn_id,
                            'callId': call_id, 'tool': tool, 'arguments': arguments})
                        require(read['success'], read)
                        w.observed_receipts.append({'kind': 'dynamicToolCall', 'item_id': call_id,
                            'status': 'completed', 'tool': tool, 'success': True})
                if 'owner_task_overview' in {tool['name'] for tool in bridge.tools}:
                    report = await bridge.adapter.call({'threadId':thread,'turnId':turn_id,
                        'callId':'owner-overview','tool':'owner_task_overview','arguments':{}})
                    require(report['success'],report)
                    w.owner_overview = json.loads(report['contentItems'][0]['text'])['data']
                    w.observed_receipts.append({'kind':'dynamicToolCall','item_id':'owner-overview',
                        'status':'completed','tool':'owner_task_overview','success':True})
                    if w.router.spec('calendar_list_events') is not None:
                        calendar = await bridge.adapter.call({'threadId':thread,'turnId':turn_id,
                            'callId':'daily-calendar','tool':'calendar_list_events','arguments':{'when':'heute'}})
                        require(calendar['success'],calendar)
                        w.daily_calendar = json.loads(calendar['contentItems'][0]['text'])['data']
                        w.observed_receipts.append({'kind':'dynamicToolCall','item_id':'daily-calendar',
                            'status':'completed','tool':'calendar_list_events','success':True})
                helpers = ()
                if w.mode == 'helper':
                    # §3.2: the worker writes a helper, reads it back with a real
                    # shasum command (Core readback evidence), declares it.
                    tools = Path(session.workspace) / 'tools'
                    tools.mkdir(exist_ok=True)
                    (tools / 'csv_sum.py').write_text(HELPER_SOURCE)
                    argv = ['/usr/bin/shasum', '-a', '256', 'tools/csv_sum.py']
                    proc = await asyncio.create_subprocess_exec(*argv, cwd=session.workspace,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
                    stdout, _ = await proc.communicate()
                    require_equal(proc.returncode, 0)
                    command, output = shlex.join(argv), stdout.decode()
                    def hblock(text):
                        return {'text': text, 'original_chars': len(text), 'complete': True, 'redacted': False}
                    w.observed_receipts.append({'kind': 'commandExecution', 'item_id': 'helper-readback',
                        'status': 'completed', 'exit_code': 0,
                        'command_sha256': hashlib.sha256(command.encode()).hexdigest(),
                        'command': hblock(command), 'output': hblock(output)})
                    helpers = ({'path': 'tools/csv_sum.py', 'name': 'csv_sum',
                                'purpose': 'Summiert eine CSV-Spalte (Betrag)'},)
                    # A test may declare OTHER paths (a model's typo, a binary
                    # file): the declaration is words, the bytes are Core work.
                    for extra_path, extra_bytes in (getattr(w, 'helper_files', None) or {}).items():
                        target = Path(session.workspace) / extra_path
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(extra_bytes)
                    helpers = tuple(getattr(w, 'helper_declarations', None) or helpers)
                w.declared_helpers = helpers
                if w.mode == 'reuse':
                    # §3.5: task two finds the seeded copy and uses it by path.
                    seeded = sorted((Path(session.workspace) / '.solvio-helpers').glob('extension-v1-*/csv_sum.py'))
                    rel = str(seeded[0].relative_to(session.workspace)) if seeded else 'MISSING'
                    argv = [sys.executable, rel, 'data.csv', 'betrag']
                    (Path(session.workspace) / 'data.csv').write_text('betrag\n1.5\n1.5\n')
                    proc = await asyncio.create_subprocess_exec(*argv, cwd=session.workspace,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
                    stdout, _ = await proc.communicate()
                    command, output = shlex.join(argv), stdout.decode()
                    w.observed_receipts.append({'kind': 'commandExecution', 'item_id': 'helper-reuse',
                        'status': 'completed' if proc.returncode == 0 else 'failed', 'exit_code': proc.returncode,
                        'command_sha256': hashlib.sha256(command.encode()).hexdigest(),
                        'command': {'text': command, 'original_chars': len(command), 'complete': True, 'redacted': False},
                        'output': {'text': output, 'original_chars': len(output), 'complete': True, 'redacted': False}})
                if local_check:
                    code = ("print('UNRELATED_SUCCESS')" if unrelated_now else
                        "import json; from pathlib import Path; "
                        "data=json.loads(Path('answer.txt').read_text().split('\\n',1)[1]); "
                        "assert isinstance(data['portale'], list); print('PORTAL_READBACK_OK')")
                    argv = [sys.executable, '-c', code]
                    proc = await asyncio.create_subprocess_exec(*argv, cwd=session.workspace,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
                    stdout, _ = await proc.communicate()
                    require_equal(proc.returncode, 0)
                    command, output = shlex.join(argv), stdout.decode()
                    def block(text):
                        return {'text': text, 'original_chars': len(text), 'complete': True, 'redacted': False}
                    w.observed_receipts.append({'kind': 'commandExecution', 'item_id': 'local-readback',
                        'status': 'completed', 'exit_code': proc.returncode,
                        'command_sha256': hashlib.sha256(command.encode()).hexdigest(),
                        'command': block(command), 'output': block(output)})
                if composite:
                    query = 'official Python csv documentation'
                    if on_event is not None:
                        on_event({'schema': 1, 'type': 'event', 'event': 'web_search', 'seq': 2,
                            'thread_id': thread, 'turn_id': turn_id,
                            'status': 'completed', 'item_id': 'web-csv', 'query': query})
                    w.observed_receipts.append({'kind': 'webSearch', 'item_id': 'web-csv',
                        'status': 'completed', 'query': block(query), 'action': 'openPage',
                        'urls': ['https://docs.python.org/3/library/csv.html'], 'urls_complete': True})
                    programs = [
                        "from pathlib import Path; Path('report.md').write_text('# Portal report\\n' + Path('answer.txt').read_text()); print('REPORT_WRITTEN')",
                        "from pathlib import Path; import hashlib; a=Path('answer.txt').read_bytes(); b=Path('report.md').read_bytes(); assert a in b; print('BUNDLE_READBACK_OK',hashlib.sha256(a).hexdigest(),hashlib.sha256(b).hexdigest())"]
                    for index, program in enumerate(programs):
                        args = [sys.executable, '-c', program]
                        proc = await asyncio.create_subprocess_exec(*args, cwd=session.workspace,
                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
                        stdout, _ = await proc.communicate()
                        require_equal(proc.returncode, 0)
                        command = shlex.join(args)
                        w.observed_receipts.append({'kind':'commandExecution', 'item_id':'bundle-' + str(index),
                            'status':'completed', 'exit_code':0,
                            'command_sha256':hashlib.sha256(command.encode()).hexdigest(),
                            'command':block(command), 'output':block(stdout.decode())})
                continuation.sessions.terminal(turn.invocation_id, native_thread_id=thread,
                    native_turn_id=turn_id, status='completed')
                return L.Outcome(True, exit_code=0, process_started=True)
            result = await D.dispatch('codex', L.Invocation('/synthetic/native', (), timeout=1), request.objective, perform)
            if not result.dispatch_started:
                # Refused by the cost gate before any claim — the real worker
                # reports exactly that reason (`native_task.run_task` failure()).
                w.refusals.append(result.outcome.reason)
                return SP.SpecialistRun(result=SpecialistResult(role='worker', provider='codex',
                    question=request.objective, ok=False, reason=result.outcome.reason),
                    provider='codex', billing_mode='subscription', dispatch_started=False,
                    quota=result.outcome.reason == 'quota', cost_status=result.cost_status,
                    cost_invocation_id=result.invocation_id, cost_reservation_id=result.reservation_id)
            ok = w.mode in {'ok', 'helper', 'reuse'} and not (rework == 'fail' and reworking)
            return SP.SpecialistRun(result=SpecialistResult(role='worker', provider='codex',
                question=request.objective, ok=ok, reason='cost_recovery_required' if w.mode == 'lost' else
                    'quota' if w.mode == 'quota_after' else 'native_result_failed' if not ok else '',
                findings=[finding], evidence=[SOURCE],
                recommended_path=recommended_path),
                provider='codex', billing_mode='subscription', dispatch_started=True, quota=w.mode == 'quota_after',
                cost_status=result.cost_status, cost_invocation_id=result.invocation_id,
                cost_reservation_id=result.reservation_id,
                native_thread_id=thread, native_turn_id=turn_id,
                native_files=('answer.txt','report.md') if composite else ('answer.txt',) if ok and not local_check else (),
                native_file_requirements=(('answer.txt','f1'),('report.md','f2')) if composite else (('answer.txt', 'h1'),) if ok and not local_check else (),
                native_tool_receipts=tuple(w.observed_receipts), native_helpers=getattr(w, 'declared_helpers', ()),
                native_helper_rejections=tuple(getattr(w, 'helper_rejections', ()) or ()))

        w.claude_calls = []
        w.broker = _BrokerDouble(NONSTART_BOOK if book is None else book)
        w.broker.book = NONSTART_BOOK if book is None else book

        async def claude(request, *, config, continuation, bridge, on_event=None):
            """The Claude worker double (A's run_task surface): quota outcomes,
            the lease gate before the starter, and the ok path with Core tools
            through the REAL adapter and the real result-file seam."""
            session = continuation.sessions.session(continuation.session_id)
            w.claude_calls.append((request, session, config))
            require_equal(session.provider, 'claude-code')
            require_equal(session.profile, SP.CLAUDE_TASK_PROFILE)
            require(str(Path(config.jail)).startswith(roots['AGENT_RUNTIME_CLAUDE_JAIL_ROOT']))
            require(not Path(config.jail).is_relative_to(Path(session.workspace)))
            revision = TR.revision_for_run(w.ledger, request.run_id)['revision']
            async def perform(*_):
                claim = continuation.sessions.active_claim(request.run_id)
                turn, fresh = continuation.sessions.request_turn(session_id=session.session_id,
                    run_id=request.run_id, revision=revision, invocation_id=claim['invocation_id'])
                require(fresh)
                if w.mode == 'quota_before':
                    # No `assistant` event: the CLI ended at the broker's 429.
                    continuation.sessions.not_started(turn.invocation_id)
                    return L.Outcome(False, reason='quota', exit_code=1, process_started=True)
                if w.mode == 'lease_refused':
                    # The broker refused the lease (principal caps): no process.
                    continuation.sessions.not_started(turn.invocation_id)
                    return L.Outcome(False, reason='broker_lease_refused', process_started=False, exit_code=None)
                continuation.sessions.bind_thread(turn.invocation_id, CLAUDE_SESSION)
                turn_id = CLAUDE_SESSION + '/' + turn.invocation_id
                continuation.sessions.started(turn.invocation_id, native_thread_id=CLAUDE_SESSION, native_turn_id=turn_id)
                if w.mode == 'ok':
                    w.claude_turn = turn_id
                    tools = [('portal-read', 'portal_list')]
                    if w.router.spec('result_files_list') is not None:
                        tools.append(('files-read', 'result_files_list'))
                    for call_id, tool in tools:
                        response = await bridge.adapter.call({'threadId': CLAUDE_SESSION, 'turnId': turn_id,
                            'callId': call_id, 'tool': tool, 'arguments': {}})
                        require(response['success'], response)
                        w.observed_receipts.append({'kind': 'dynamicToolCall', 'item_id': call_id,
                            'status': 'completed', 'tool': tool, 'success': True})
                        if tool == 'portal_list':
                            (Path(session.workspace) / 'answer.txt').write_text('Native result revision ' + str(revision)
                                + '\n' + json.dumps(json.loads(response['contentItems'][0]['text'])['data'], ensure_ascii=False))
                    continuation.sessions.terminal(turn.invocation_id, native_thread_id=CLAUDE_SESSION,
                        native_turn_id=turn_id, status='completed')
                    return L.Outcome(True, exit_code=0, process_started=True)
                continuation.sessions.terminal(turn.invocation_id, native_thread_id=CLAUDE_SESSION,
                    native_turn_id=turn_id, status='failed')
                return L.Outcome(False, reason='quota', exit_code=1, process_started=True)
            w.observed_receipts = []
            result = await D.dispatch('claude-code', L.Invocation('/usr/bin/sandbox-exec', (), timeout=1),
                                      request.objective, perform)
            if not result.dispatch_started:
                w.refusals.append(result.outcome.reason)
                # Mirrors claude_native_task.run_task: a gate before the starter
                # (GATE_REASONS) is the owner boundary `provider_unavailable`.
                reason = result.outcome.reason
                note = ''
                if reason in {'broker_lease_refused', 'native_broker_unavailable'}:
                    reason, note = 'provider_unavailable', result.outcome.reason
                return SP.SpecialistRun(result=SpecialistResult(role='worker', provider='claude-code',
                    question=request.objective, ok=False, reason=reason), stderr_note=note,
                    provider='claude-code', billing_mode='subscription', dispatch_started=False,
                    quota=reason == 'quota', cost_status=result.cost_status,
                    cost_invocation_id=result.invocation_id, cost_reservation_id=result.reservation_id)
            if w.mode == 'ok':
                return SP.SpecialistRun(result=SpecialistResult(role='worker', provider='claude-code',
                    question=request.objective, ok=True,
                    findings=['Der lokale Portalbestand wurde gelesen.'], evidence=[SOURCE],
                    recommended_path='Der Befund liegt als Datei bereit.'),
                    provider='claude-code', billing_mode='subscription', dispatch_started=True,
                    cost_status=result.cost_status, cost_invocation_id=result.invocation_id,
                    cost_reservation_id=result.reservation_id,
                    native_thread_id=CLAUDE_SESSION, native_turn_id=w.claude_turn,
                    native_files=('answer.txt',), native_file_requirements=(('answer.txt', 'h1'),),
                    native_tool_receipts=tuple(w.observed_receipts))
            return SP.SpecialistRun(result=SpecialistResult(role='worker', provider='claude-code',
                question=request.objective, ok=False, reason='quota'),
                provider='claude-code', billing_mode='subscription', quota=True,
                dispatch_started=w.mode != 'quota_before',
                cost_status=result.cost_status, cost_invocation_id=result.invocation_id,
                cost_reservation_id=result.reservation_id)

        module = _claude_module()
        module.run_task = claude

        from solvio.agent_runtime import native_tools as NTOOLS, native_tool_bridge as NBRIDGE
        RealTools, RealBridge = NTOOLS.NativeCoreTools, NBRIDGE.NativeToolBridge

        class _ToolsDouble:
            """Recorder around the REAL NativeCoreTools (implementer C widened
            its provider set): notes which session the Core tools bind."""
            def __new__(cls, ledger, sessions, session_id, run_id, router, *, cancel_token=None):
                w.tool_bindings.append(sessions.session(session_id))
                return RealTools(ledger, sessions, session_id, run_id, router, cancel_token=cancel_token)

        class _BridgeDouble:
            """Recorder around the REAL NativeToolBridge: notes the socket root
            (the Claude jail's `sock/` leaf, the Codex socket root beside the ledger)."""
            def __new__(cls, adapter, *, socket_root):
                w.bridge_roots.append(socket_root)
                return RealBridge(adapter, socket_root=socket_root)

        w.tool_bindings, w.bridge_roots = [], []
        claude_patches = []
        if worker == 'claude-code':
            claude_patches = [claude_module_double(module),
                              patch.object(N, '_broker', return_value=w.broker),
                              patch.object(NTOOLS, 'NativeCoreTools', _ToolsDouble),
                              patch.object(NBRIDGE, 'NativeToolBridge', _BridgeDouble),
                              patch.object(PS, '_WORKER_CANARY', '')]
        from contextlib import ExitStack
        with ExitStack() as stack:
            stack.enter_context(patch.object(P, 'resolve', return_value=str(executable)))
            stack.enter_context(patch.object(SP, 'native_research_config', return_value=config))
            stack.enter_context(patch.object(NT, 'run_task', side_effect=native))
            sockets = stack.enter_context(NCT.socket_root())
            stack.enter_context(patch.object(N, '_socket_root', return_value=sockets))
            for item in claude_patches:
                stack.enter_context(item)
            w.folder = folder
            try:
                yield w
            finally:
                for key in roots:
                    os.environ.pop(key, None)
                import shutil
                for task in w.ledger.recent_tasks() if hasattr(w.ledger, 'recent_tasks') else []:
                    shutil.rmtree(jail_root / task.task_id, ignore_errors=True)
                for _, session, _ in w.claude_calls:
                    shutil.rmtree(jail_root / session.task_id, ignore_errors=True)


async def start(w, **changes):
    response = await w.start(dict(E.BODY, scope='task', objective=w.objective, **changes))
    require_equal(response.status, 201, await response.text())
    return await response.json()


async def start_private_chat_task(w, request_id='chat-private-001'):
    """The chat's own start for a `persoenlich` order (processing._start_task):
    authenticated app chat, bound conversation, private-data marker."""
    from solvio.agent_runtime.task_start_service import AuthorizedTaskStart
    from solvio.agent_runtime.task_authority import VerifiedTaskReceipt
    from solvio.capabilities.contract import ArgumentSource
    from solvio.capabilities.envelope import CapabilityOutcome
    from solvio.capabilities.policy import OriginClass
    from solvio.contracts.trust import TrustContext, TrustLevel
    arguments = {'objective': w.objective}
    start = AuthorizedTaskStart.bind(receipt=VerifiedTaskReceipt('app_session', 'app:chat:' + request_id, 'local-owner'),
        request_id=request_id, capability='agent_task_task', arguments=arguments,
        conversation_ref='c-0123456789abcdef', private_data=True)
    result = await w.orch.router.execute('agent_task_task', arguments,
        trust=TrustContext(TrustLevel.USER_DIRECT, user_authorized=True),
        provenance={key: ArgumentSource.USER_DIRECT for key in arguments},
        principal='local-owner', origin=OriginClass.TRUSTED_INTERACTIVE_APP, task_start=start)
    require_equal(result.outcome, CapabilityOutcome.SUCCESS, result.reason)
    return result.data


async def drive(w, run_id):
    for _ in range(12):
        await w.orch.tick()
        run = w.ledger.get_run(run_id)
        if run.terminal or run.state == S.WAITING_USER:
            return run
    raise AssertionError('task did not reach its boundary')


async def t_public_task_delegates_once_with_real_costs_tools_files_and_two_followups():
    async with world() as w:
        accepted = await start(w)
        grant = w.orch.task_authority.for_run(accepted['run_id'])
        require_equal({c.name for c in grant.capabilities}, {'portal_list', 'artifact_create'})
        histories, files = [], []
        for revision in (1, 2, 3):
            run_id = accepted['run_id']
            run = await drive(w, run_id)
            require_equal(run.state, S.SUCCEEDED, run.failure_category + ': ' + run.result_summary)
            require_equal(run.planner_calls, 1)
            require_equal(run.specialist_count, 1)
            require_equal(run.plan_revision, 0)
            steps = [s for s in w.ledger.steps_for_run(run_id) if s.kind in {'capability', 'specialist'}]
            require_equal([(s.seq, s.kind) for s in steps], [(1, 'specialist'), (2, 'capability')])
            require_equal(CP.decode(run.plan_checkpoint)['cursor'], 1)
            descriptor = RF.describe_files(w.ledger, run_id)[0][0]
            files.append((run_id, descriptor['id'], RF.read_result(w.ledger, run_id, descriptor['id'])[1]))
            histories.append(run)
            observation = next(a for a in w.ledger.artifacts_for_run(run_id) if a.kind == 'native_task_observation')
            receipt = json.loads(Path(observation.path).read_text())
            require_equal(receipt['revision'], revision)
            require_equal(receipt['cost']['operation_id'], steps[0].step_id)
            require_equal(receipt['cost']['settlement_state'], 'settled')
            # A follow-up run's assessor gets the Core's own fact about the earlier
            # revision: its files re-read unchanged and still downloadable (attempt s).
            predecessor = NRF.predecessor_evidence(w.ledger, run_id)
            if revision == 1:
                require_equal(predecessor, ())
            else:
                parent_id, _, parent_content = files[-2]
                require_equal([d.artifact_id for d in predecessor], [parent_id])
                require('Core-Verlaufsbeleg' in predecessor[0].evidence and parent_id in predecessor[0].evidence
                        and hashlib.sha256(parent_content).hexdigest() in predecessor[0].evidence
                        and '/v1/agent/runs/' + parent_id + '/artifacts/' in predecessor[0].evidence, predecessor[0].evidence)
                snapshot = [a for a in w.ledger.artifacts_for_run(run_id) if a.kind == 'evaluation_snapshot'][-1]
                require(predecessor[0].evidence in Path(snapshot.path).read_text(), 'the assessor did not get the predecessor fact')
            if revision < 3:
                require(TR.eligibility(w.ledger, run_id)['eligible'], TR.eligibility(w.ledger, run_id))
                response = await F.post(w, F.request(w, run_id,
                    client_request_id='native-followup-' + str(revision), text='Ergänze den bisherigen Befund genauer.'))
                require_equal(response.status, 202, await response.text())
                accepted = await response.json()
        require_equal(len({s.session_id for _, s in w.calls}), 1)
        require_equal(len({s.workspace for _, s in w.calls}), 1)
        require_equal([s.native_thread_id for _, s in w.calls], ['', 'thread-one', 'thread-one'])
        for run in histories:
            require_equal(w.ledger.get_run(run.run_id), run)
        for run_id, artifact, content in files:
            require_equal(RF.read_result(w.ledger, run_id, artifact)[1], content)
        text_calls = [json.loads(line) for line in (w.folder / 'calls.jsonl').read_text().splitlines()]
        require_equal([c['kind'] for c in text_calls], ['plan', 'assessment'] * 3)
        for call in text_calls[::2]:
            require_equal(call['request']['ziel'], 'anforderungen_erfassen')
        require_equal(w.orch.costs.view(accepted['task_id'])['counts'], {'settled': 12})


async def t_model_step_plan_is_rejected_without_native_delegation_after_the_one_repair():
    """A model that plans steps instead of naming criteria gets the ONE repair call
    with the Core's reason (as `Planner.plan` grants it); a second such answer ends
    the run FAILED plan_invalid — never a native delegation, never a third call."""
    async with world(criteria={**CRITERIA, 'schritte': [{'art': 'specialist'}]}) as w:
        accepted = await start(w)
        run = await drive(w, accepted['run_id'])
        require_equal(run.state, S.FAILED)
        require_equal(run.failure_category, 'plan_invalid')
        require_equal(w.calls, [])
        require_equal(run.planner_calls, 2)
        text_calls = [json.loads(line) for line in (w.folder / 'calls.jsonl').read_text().splitlines()]
        require_equal([c['kind'] for c in text_calls], ['plan', 'plan'])


async def t_a_failed_assessment_call_takes_the_next_place_under_the_cap_and_the_order_proceeds():
    """Measured 20.09.2026 (attempt ab, run 1): the assessment CLI exited non-zero
    after three seconds and a finished order ended FAILED goal_unverified. A failed
    CALL is not a verdict: it takes the next place under the same per-run cap (as a
    format repair does) — one more call, never a third."""
    async with world(assessment_sequence=['__EXIT_3__', VERDICT]) as w:
        accepted = await start(w)
        run = await drive(w, accepted['run_id'])
        require_equal(run.state, S.SUCCEEDED, run.failure_category)
        require_equal(run.assessment_calls, 2)
        text_calls = [json.loads(line) for line in (w.folder / 'calls.jsonl').read_text().splitlines()]
        require_equal([c['kind'] for c in text_calls], ['plan', 'assessment', 'assessment'])
    async with world(assessment_sequence=['__EXIT_3__']) as w:
        accepted = await start(w)
        run = await drive(w, accepted['run_id'])
        require_equal((run.state, run.failure_category, run.assessment_calls), (S.FAILED, 'goal_unverified', 2),
                      'two failed calls end the run under the cap, never a third call')


async def t_an_invalid_criteria_block_gets_one_repair_call_with_the_reason_and_the_order_proceeds():
    """Measured 19.09.2026 (attempt aa, run 2): one free-form slip of the criteria
    model ended the whole order FAILED plan_invalid before any worker turn. Now the
    second call carries the Core's reason (a category, never material) and the order
    goes on; the intent of each call is persisted before its await."""
    slip = {'anforderungen': {**CRITERIA['anforderungen'], 'auskunft': [
        {**CRITERIA['anforderungen']['auskunft'][0], 'effect': 'file'}]}}   # effect in auskunft
    async with world(criteria_sequence=[slip, CRITERIA]) as w:
        accepted = await start(w)
        run = await drive(w, accepted['run_id'])
        require_equal(run.state, S.SUCCEEDED, run.failure_category)
        require_equal(run.planner_calls, 2)
        require_equal(len(w.calls), 1, 'the worker ran once after the repaired criteria')
        text_calls = [json.loads(line) for line in (w.folder / 'calls.jsonl').read_text().splitlines()]
        require_equal([c['kind'] for c in text_calls], ['plan', 'plan', 'assessment'])
        require('native_requirements_invalid' not in text_calls[0]['system'], 'the first call carried a hint')
        require('ungültig (native_requirements_invalid)' in text_calls[1]['system'], text_calls[1]['system'][-200:])
        require_equal(text_calls[1]['request'], text_calls[0]['request'], 'the order text must not change with the repair')


async def t_criteria_repair_survives_repeated_proven_nonstarts_and_a_context_restart():
    slip = {**CRITERIA, 'schritte': []}
    async with world(criteria_sequence=[slip, CRITERIA]) as w:
        accepted = await start(w)
        actual = w.orch.planner.extract_requirements
        calls, pauses = [], [2]
        async def paused(**kwargs):
            calls.append((kwargs.get('hint', ''), w.ledger.get_run(accepted['run_id']).planner_calls))
            if len(calls) > 1 and pauses[0]:
                pauses[0] -= 1
                return PL.PlannerCall(False, reason='quota', provider='codex',
                    billing_mode='subscription', auth='subscription', dispatch_started=False)
            return await actual(**kwargs)
        with patch.object(w.orch.planner, 'extract_requirements', paused):
            for _ in range(2):
                parked = await drive(w, accepted['run_id'])
                require_equal((parked.state, parked.planner_calls, len(w.calls)), (S.WAITING_USER, 1, 0),
                              parked.failure_category + ': ' + parked.result_summary)
                require(await w.orch.resume(parked.run_id))
                w.orch._contexts.pop(parked.run_id, None)
            run = await drive(w, accepted['run_id'])
        require_equal((run.state, run.planner_calls, len(w.calls)), (S.SUCCEEDED, 2, 1), run.result_summary)
        require_equal(calls, [('', 1)] + [('native_requirements_invalid', 2)] * 3,
                      'only the same second attempt may resume with its original repair reason')
        text_calls = [json.loads(line) for line in (w.folder / 'calls.jsonl').read_text().splitlines()]
        require_equal([c['kind'] for c in text_calls], ['plan', 'plan', 'assessment'])


async def t_uncertain_criteria_repair_is_never_replayed_after_owner_resume():
    async with world(criteria_sequence=[{**CRITERIA, 'schritte': []}, CRITERIA]) as w:
        accepted = await start(w)
        actual, calls = w.orch.planner.extract_requirements, []
        async def uncertain(**kwargs):
            calls.append(kwargs.get('hint', ''))
            if len(calls) == 2:
                return PL.PlannerCall(False, reason='quota', provider='codex',
                    billing_mode='subscription', auth='subscription', dispatch_started=True)
            return await actual(**kwargs)
        with patch.object(w.orch.planner, 'extract_requirements', uncertain):
            parked = await drive(w, accepted['run_id'])
            require_equal((parked.state, parked.planner_calls), (S.WAITING_USER, 2))
            require(await w.orch.resume(parked.run_id))
            w.orch._contexts.pop(parked.run_id, None)
            run = await drive(w, parked.run_id)
        require_equal((run.state, run.failure_category, len(calls), len(w.calls)),
                      (S.FAILED, 'plan_invalid', 2, 0))


async def t_lost_native_turn_parks_without_second_send_or_completed_files():
    async with world(mode='lost') as w:
        accepted = await start(w)
        run = await drive(w, accepted['run_id'])
        require_equal(run.state, S.WAITING_USER)
        for _ in range(3):
            await w.orch.tick()
        require_equal(len(w.calls), 1)
        require_equal(RF.describe_files(w.ledger, run.run_id)[0], [])
        require(not TR.eligibility(w.ledger, run.run_id)['eligible'])


async def t_worker_profile_cannot_escape_through_ordinary_specialist_or_research_scope():
    request = SP.SpecialistRequest(SP.TASK_PROFILE, OBJECTIVE, '', run_id='unbound')
    outcome = await SP.run_specialist(request)
    require(not outcome.result.ok)
    require_equal(outcome.dispatch_started, False)
    try:
        PL.validate({'schritte': [{'art': 'specialist', 'profil': SP.TASK_PROFILE, 'auftrag': OBJECTIVE}]},
            scope='research', allowed_profiles={SP.TASK_PROFILE}, known_capabilities=set(), goal=OBJECTIVE)
    except PL.PlanInvalid as exc:
        require_equal(exc.reason, 'native_worker_scope_required')
    else:
        raise AssertionError('worker accepted in research scope')


async def t_proven_nonstart_keeps_worker_budget_and_resumes_same_outer_step():
    async with world(mode='quota_before') as w:
        accepted = await start(w)
        waiting = await drive(w, accepted['run_id'])
        require_equal(waiting.state, S.WAITING_USER)
        require_equal(waiting.specialist_count, 0)
        step = next(s for s in w.ledger.steps_for_run(waiting.run_id) if s.kind == 'specialist')
        require_equal(step.state, 'waiting')
        require(await w.orch.resume(waiting.run_id))
        w.orch._contexts.pop(waiting.run_id, None)
        w.mode = 'ok'
        run = await drive(w, waiting.run_id)
        require_equal(run.state, S.SUCCEEDED, run.result_summary)
        require_equal(run.specialist_count, 1)
        require_equal([s.step_id for s in w.ledger.steps_for_run(run.run_id) if s.kind == 'specialist'], [step.step_id])


async def t_quota_after_possible_writes_does_not_open_readonly_retry():
    async with world(mode='quota_after') as w:
        accepted = await start(w)
        waiting = await drive(w, accepted['run_id'])
        require_equal(waiting.state, S.WAITING_USER)
        require(not json.loads(waiting.boundary)['provider_wait']['resume_allowed'])
        require(not await w.orch.resume(waiting.run_id))
        for _ in range(2):
            await w.orch.tick()
        require_equal(len(w.calls), 1)


async def t_public_task_rejects_unauthenticated_body_repo_and_contract_injection():
    async with world() as w:
        body = dict(E.BODY, scope='task', objective=OBJECTIVE)
        unauthenticated = await w.client.post('/v1/agent/tasks', json={'task': body}, headers={})
        require_equal(unauthenticated.status, 401)
        require_equal((await w.start(dict(body, target_repo='/tmp/unbound'))).status, 400)
        require_equal((await w.start(dict(body, capabilities=['send_mail']))).status, 400)
        require_equal(w.ledger.recent_runs(), [])


async def t_an_observation_token_for_a_file_criterion_is_material_but_never_the_file_proof():
    """DEBT-0289 H5 pattern (attempts p/q/v): a file criterion that bundles checks now
    gets an observation token with ITS id in the assessor material — so an execution can
    be attributed to it — while the effect catalogue keeps requiring the FILE token for a
    file effect: an observation token alone verifies no file criterion."""
    from solvio.agent_runtime import native_observations as NO
    async with world(mode='ok') as w:
        accepted = await start(w)
        run = await drive(w, accepted['run_id'])
        require_equal(run.state, S.SUCCEEDED, run.result_summary)
        tokens = NO.completion_evidence(w.ledger, run.run_id)
        require('h1' in {d.requirement for d in tokens}, 'no observation token for the file criterion h1')
        catalogue = w.orch._verified_effects(run.run_id)
        obs_tokens = [d.evidence for d in tokens if d.requirement == 'h1']
        require(all(catalogue.get(token) == 'h1' for token in obs_tokens), 'the observation token for h1 is not in the catalogue')
        file_tokens = w.orch._file_evidence_tokens(run.run_id)
        require(file_tokens and all(catalogue.get(token) == 'h1' for token in file_tokens), 'the file token itself is missing from the catalogue')
        # The contract: a file effect cited with the observation token ALONE is not verified;
        # with the file token it is.
        from solvio.agent_runtime import completion as CO, requirements as RQ
        stored = w.orch._stored_verdict(w.ledger.get_run(run.run_id))
        snapshot = RQ.read_snapshot(w.ledger, run.run_id, stored['snapshot'])
        bound = RQ.load(TR.task_view(w.ledger, run.run_id).requirements, objective=TR.task_view(w.ledger, run.run_id).objective)
        def verdict(belege):
            judgement = dict(stored, beantwortet=[e for e in stored['beantwortet'] if e['id'] != 'h1'] + [{'id': 'h1', 'belege': belege}])
            return CO.information(bound=bound, judgement=judgement, snapshot=snapshot, snapshot_digest=stored['snapshot'],
                                  requirements_digest=RQ.digest_of(bound), task_id=run.task_id, run_id=run.run_id,
                                  verified_effects=catalogue, file_evidence=file_tokens)
        require_equal((verdict(obs_tokens[:1]).satisfied, verdict(obs_tokens[:1]).reason), (False, 'action_not_verified'))
        require(verdict([next(iter(file_tokens))]).satisfied, verdict([next(iter(file_tokens))]).reason)
        require(verdict([next(iter(file_tokens)), obs_tokens[0]]).satisfied)
        snapshot = [a for a in w.ledger.artifacts_for_run(run.run_id) if a.kind == 'evaluation_snapshot'][-1]
        require(all(token in Path(snapshot.path).read_text() for token in obs_tokens), 'the token is not assessor material')


async def t_file_receipt_cannot_prove_booking_despite_native_and_assessor_claim():
    criteria = {'anforderungen': {**CRITERIA['anforderungen'], 'handlungen': [
        {'id': 'h1', 'text': 'Buche das Hotel tatsächlich.', 'effect': 'external'}]}}
    async with world(criteria=criteria, objective='Nenne die lokal konfigurierten Portale und buche das Hotel tatsächlich.') as w:
        accepted = await start(w)
        run = await drive(w, accepted['run_id'])
        require_equal(run.state, S.FAILED)
        require_equal(run.failure_category, 'goal_unverified')
        require_equal(w.orch._verified_effects(run.run_id), {})
        require_equal(len(w.calls), 1)
        require_equal(len(RF.describe_files(w.ledger, run.run_id)[0]), 1)
        require(TR.eligibility(w.ledger, run.run_id)['eligible'], TR.eligibility(w.ledger, run.run_id))
        with w.ledger._open() as db:
            invocation = db.execute('SELECT invocation_id FROM agent_native_turns WHERE run_id=?', (run.run_id,)).fetchone()[0]
            db.execute("UPDATE agent_native_turns SET terminal_status='failed' WHERE invocation_id=?", (invocation,))
        require(not TR.eligibility(w.ledger, run.run_id)['eligible'])
        with w.ledger._open() as db:
            db.execute("UPDATE agent_native_turns SET terminal_status='completed' WHERE invocation_id=?", (invocation,))
        response = await F.post(w, F.request(w, run.run_id, client_request_id='explicit-native-clarification',
            text='Nur einen Dateibericht erstellen. Keine Buchung vornehmen.'))
        require_equal(response.status, 202, await response.text())
        followup = await response.json()
        require_equal(followup['task_id'], accepted['task_id'])
        require_equal(followup['revision'], 2)
        require(followup['run_id'] != run.run_id)
        require_equal(w.ledger.get_run(run.run_id), run)
        require_equal(len(w.calls), 1, 'admission started a second provider invocation')


async def t_local_execution_is_observed_and_semantically_checked_without_publishing_a_file():
    criteria = {'anforderungen': {**CRITERIA['anforderungen'], 'handlungen': [
        {'id': 'h1', 'text': 'Lies den lokalen Portalbefund zurück und prüfe dessen Portalliste.',
         'effect': 'local_execution'}]}}
    for unrelated in (False, True):
        async with world(criteria=criteria, local_check=True, unrelated_check=unrelated,
                objective='Nenne die lokalen Portale und prüfe den lokal gespeicherten Portalbefund durch Rücklesen.') as w:
            accepted = await start(w)
            run = await drive(w, accepted['run_id'])
            require_equal(run.state, S.FAILED if unrelated else S.SUCCEEDED)
            # §3.5: a concrete open point earns exactly ONE rework turn — the double
            # repeats its unrelated check, the second verdict is final, no third turn.
            require_equal(len(w.calls), 2 if unrelated else 1)
            require_equal(RF.describe_files(w.ledger, run.run_id)[0], [])
            effects = w.orch._verified_effects(run.run_id)
            require(effects and set(effects.values()) == {'h1'})
            snapshot = next(a for a in w.ledger.artifacts_for_run(run.run_id) if a.kind == 'evaluation_snapshot')
            material = Path(snapshot.path).read_text()
            require('portal_list' in material and 'portale' in material)
            require(('UNRELATED_SUCCESS' if unrelated else 'PORTAL_READBACK_OK') in material)
            if unrelated:
                require_equal(run.failure_category, 'goal_unverified')


async def t_a_worker_task_gets_one_rework_in_its_own_session_and_the_second_verdict_decides():
    """N8/C4 §3.5 (Owner-Praezisierung 19.09.2026): the assessor finds a concrete,
    locally fixable gap; the worker gets ONE more turn in the SAME native session
    with the open points as untrusted hints; the verification runs again and the
    run ends on that verdict. No new task, no revision, no owner receipt."""
    criteria = {'anforderungen': {**CRITERIA['anforderungen'], 'handlungen': [
        {'id': 'h1', 'text': 'Lies den lokalen Portalbefund zurück und prüfe dessen Portalliste.',
         'effect': 'local_execution'}]}}
    async with world(criteria=criteria, local_check=True, rework='fix',
            objective='Nenne die lokalen Portale und prüfe den lokal gespeicherten Portalbefund durch Rücklesen.') as w:
        accepted = await start(w)
        run = await drive(w, accepted['run_id'])
        require_equal((run.state, run.assessment_calls), (S.SUCCEEDED, 2), run.result_summary)
        require_equal(len(w.calls), 2)
        first, second = w.calls
        require(first[1].session_id == second[1].session_id, 'the rework left the native session')
        require(N.REWORK_HEADER not in first[0].context and N.REWORK_HEADER in second[0].context)
        require('The observed command did not perform the required local check' in second[0].context,
                'the rework turn does not carry the assessor\'s open point')
        require_equal(second[0].objective, w.objective, 'the rework changed the bound objective')
        steps = w.ledger.steps_for_run(run.run_id)
        workers = [s for s in steps if s.kind == 'specialist']
        require_equal([(s.state, s.specialist_profile) for s in workers], [('succeeded', SP.WORKER_PROFILES['codex'])] * 2)
        require_equal([s.outcome_reason for s in steps if s.kind == 'verify'], ['task_reworked', ''])
        with w.ledger._open() as db:
            turns = [dict(r) for r in db.execute('SELECT t.*, s.native_thread_id FROM agent_native_turns t '
                                                 'JOIN agent_native_sessions s ON s.session_id=t.session_id ORDER BY t.rowid')]
            sessions = db.execute('SELECT COUNT(*) FROM agent_native_sessions').fetchone()[0]
        require_equal((sessions, [t['revision'] for t in turns], {t['native_thread_id'] for t in turns},
                       len({t['native_turn_id'] for t in turns})), (1, [1, 1], {'thread-one'}, 2))
        require(all(t['state'] == 'terminal' and t['terminal_status'] == 'completed' for t in turns))
        effects = w.orch._verified_effects(run.run_id)
        require(effects and set(effects.values()) == {'h1'})
        snapshots = [a for a in w.ledger.artifacts_for_run(run.run_id) if a.kind == 'evaluation_snapshot']
        require_equal(len(snapshots), 2, 'one evaluation snapshot per verdict')
        require('UNRELATED_SUCCESS' in Path(snapshots[0].path).read_text()
                and 'PORTAL_READBACK_OK' in Path(snapshots[-1].path).read_text())
        require_equal(len(w.ledger.runs_for_task(accepted['task_id'])), 1, 'the rework created a revision')
        costs = w.orch.costs.view(accepted['task_id'])
        require_equal((costs['ai_tool']['spent_cents'], costs['ai_tool']['reserved_cents'], costs['counts']),
                      (0, 0, {'settled': 7}), 'the rework left the task cost account (plan, 2 workers, 2 tool calls, 2 assessments)')
        require(TR.eligibility(w.ledger, run.run_id)['eligible'], 'the reworked run is not continuable')


async def t_a_rework_republishes_the_deliverables_and_supersedes_the_first_publication():
    """The rework turn publishes every deliverable again; the run's result, the
    assessor's evidence and the file list are the LATEST publication. The first
    stays in the ledger as history, never as a second copy of the result."""
    async with world(rework='stale_file') as w:
        accepted = await start(w)
        run = await drive(w, accepted['run_id'])
        require_equal((run.state, len(w.calls)), (S.SUCCEEDED, 2), run.result_summary)
        files, note = RF.describe_files(w.ledger, run.run_id)
        require_equal(([f['name'] for f in files], note), (['answer.txt'], ''))
        _, content = RF.read_result(w.ledger, run.run_id, files[0]['id'])
        require(content.startswith(b'Native result revision 1'), content[:40])
        stored = [a for a in w.ledger.artifacts_for_run(run.run_id) if a.kind == 'result_file']
        require_equal(len(stored), 2, 'the first publication vanished from the ledger')
        superseded = RF.superseded_artifact_ids(w.ledger, run.run_id)
        require_equal({a.artifact_id for a in stored} - superseded, {files[0]['id']})
        require_equal([d.artifact_id for d in RF.completion_evidence(w.ledger, run.run_id)], [files[0]['id']])
        require_equal([d.artifact_id for d in NRF.completion_evidence(w.ledger, run.run_id)], [files[0]['id']])
        from solvio.agent_runtime import native_observations as NO
        # Observations are CUMULATIVE (attempt s, 19.09.2026: the first turn's web read,
        # portal call and helper checks must stay evidence); each turn's projection
        # keeps within its share of the material cap.
        observations = NO.completion_evidence(w.ledger, run.run_id)
        workers = sorted((s for s in w.ledger.steps_for_run(run.run_id) if s.kind == 'specialist'), key=lambda s: s.seq)
        origins = [json.loads(d.finding.split('\n', 1)[1])['origin']['step_id'] for d in observations]
        require_equal(origins, [s.step_id for s in workers], 'the assessor does not see both turns\' observations')
        require(all(len(d.finding) <= NO.MAX_MATERIAL_CHARS // 2 for d in observations), 'a turn exceeds its share of the cap')
        require_equal(len({d.evidence for d in observations}), 2)
        require_equal([row['name'] for row in RF.describe_task_files(w.ledger, accepted['task_id'])], ['answer.txt'])
        # The public view carries the one file, and a follow-up still starts from this run.
        view = await (await w.client.get('/v1/agent/runs/' + run.run_id, headers=w.headers)).json()
        require_equal([f['name'] for f in view['dateien']], ['answer.txt'])
        require(TR.eligibility(w.ledger, run.run_id)['eligible'], TR.eligibility(w.ledger, run.run_id))


async def t_negative_completed_rework_allows_an_owner_followup_but_unconfirmed_turns_do_not():
    criteria = {'anforderungen': {**CRITERIA['anforderungen'], 'handlungen': [
        {'id': 'h1', 'text': 'Prüfe den gelesenen lokalen Portalbefund.', 'effect': 'local_execution'}]}}
    async with world(criteria=criteria, local_check=True, unrelated_check=True,
            objective='Nenne die lokalen Portale und prüfe den lokal gespeicherten Portalbefund durch Rücklesen.') as w:
        accepted = await start(w)
        run = await drive(w, accepted['run_id'])
        require_equal((run.state, run.failure_category, len(w.calls)), (S.FAILED, 'goal_unverified', 2))
        require(TR.eligibility(w.ledger, run.run_id)['eligible'], TR.eligibility(w.ledger, run.run_id))
        with w.ledger._open() as db:
            first = db.execute('SELECT invocation_id FROM agent_native_turns WHERE run_id=? ORDER BY rowid LIMIT 1',
                               (run.run_id,)).fetchone()[0]
            db.execute("UPDATE agent_native_turns SET state='unknown' WHERE invocation_id=?", (first,))
        require_equal(TR.eligibility(w.ledger, run.run_id),
                      {'eligible': False, 'reason': 'followup_native_task_unconfirmed'})
        with w.ledger._open() as db:
            db.execute("UPDATE agent_native_turns SET state='terminal' WHERE invocation_id=?", (first,))
            db.execute("UPDATE agent_steps SET outcome_reason='task_rework_started' WHERE run_id=? AND outcome_reason='task_reworked'",
                       (run.run_id,))
        require_equal(TR.eligibility(w.ledger, run.run_id),
                      {'eligible': False, 'reason': 'followup_native_task_unconfirmed'})
        with w.ledger._open() as db:
            db.execute("UPDATE agent_steps SET outcome_reason='task_reworked' WHERE run_id=? AND outcome_reason='task_rework_started'",
                       (run.run_id,))
        response = await F.post(w, F.request(w, run.run_id, client_request_id='negative-rework-followup',
            text='Prüfe denselben lokalen Portalbefund erneut sorgfältig.'))
        require_equal(response.status, 202, await response.text())
        followup = await response.json()
        require_equal((followup['task_id'], followup['revision']), (accepted['task_id'], 2))
        require_equal(w.ledger.get_run(run.run_id), run, 'the completed predecessor changed')
        require_equal(len(w.calls), 2, 'admission itself dispatched a provider')


async def t_released_native_nonstart_does_not_block_followup_after_negative_completed_rework():
    # The lease refusal goes through the real dispatcher and NativeSessions:
    # not_dispatched/released is a proved non-start, not an unsettled claim.
    async with world(worker='claude-code', mode='lease_refused', local_check=True) as w:
        accepted = await start(w)
        parked = await drive(w, accepted['run_id'])
        require_equal(parked.state, S.WAITING_USER, parked.result_summary)
        with w.ledger._open() as db:
            first = dict(db.execute('SELECT i.invocation_id,i.state,c.state AS cost_state,t.terminal_status '
                'FROM agent_provider_invocations i JOIN agent_cost_reservations c USING(reservation_id) '
                'JOIN agent_native_turns t USING(invocation_id) WHERE i.run_id=?', (parked.run_id,)).fetchone())
        require_equal((first['state'], first['cost_state'], first['terminal_status']),
                      ('not_dispatched', 'released', 'not_started'))
        w.mode = 'ok'
        require(await w.orch.resume(parked.run_id))
        w.orch._contexts.pop(parked.run_id, None)
        run = await drive(w, parked.run_id)
        require_equal((run.state, run.failure_category, len(w.claude_calls)),
                      (S.FAILED, 'goal_unverified', 3), run.result_summary)
        require_equal([s.state for s in w.ledger.steps_for_run(run.run_id) if s.kind == 'specialist'],
                      ['succeeded', 'succeeded'])
        require(TR.eligibility(w.ledger, run.run_id)['eligible'], TR.eligibility(w.ledger, run.run_id))
        with w.ledger._open() as db:
            db.execute("UPDATE agent_native_turns SET state='unknown' WHERE invocation_id=?", (first['invocation_id'],))
        require_equal(TR.eligibility(w.ledger, run.run_id),
                      {'eligible': False, 'reason': 'followup_native_task_unconfirmed'})
        with w.ledger._open() as db:
            db.execute("UPDATE agent_native_turns SET state='terminal' WHERE invocation_id=?", (first['invocation_id'],))
        response = await F.post(w, F.request(w, run.run_id, client_request_id='released-rework-followup',
            text='Prüfe denselben lokalen Portalbefund erneut sorgfältig.'))
        require_equal(response.status, 202, await response.text())
        require_equal((await response.json())['revision'], 2)


async def t_a_failed_rework_keeps_the_first_result_and_is_never_repeated():
    criteria = {'anforderungen': {**CRITERIA['anforderungen'], 'handlungen': [
        {'id': 'h1', 'text': 'Lies den lokalen Portalbefund zurück und prüfe dessen Portalliste.',
         'effect': 'local_execution'}]}}
    async with world(criteria=criteria, local_check=True, unrelated_check=True, rework='fail',
            objective='Nenne die lokalen Portale und prüfe den lokal gespeicherten Portalbefund durch Rücklesen.') as w:
        accepted = await start(w)
        run = await drive(w, accepted['run_id'])
        require_equal((run.state, run.failure_category, len(w.calls)), (S.FAILED, 'goal_unverified', 2))
        require('Nacharbeit hat kein bestätigtes Ergebnis' in run.result_summary, run.result_summary)
        steps = w.ledger.steps_for_run(run.run_id)
        require_equal([s.state for s in steps if s.kind == 'specialist'], ['succeeded', 'failed'])
        require_equal(RF.superseded_artifact_ids(w.ledger, run.run_id), frozenset(), 'a failed rework superseded the first result')
        require_equal(run.assessment_calls, 1, 'a failed rework was assessed')


async def t_a_committed_rework_survives_a_restart_and_is_dispatched_exactly_once():
    """The intent (state RUNNING + marked verify step) is durable before any
    dispatch; a process loss right after it leads to ONE rework on the next
    tick — never two, never none."""
    criteria = {'anforderungen': {**CRITERIA['anforderungen'], 'handlungen': [
        {'id': 'h1', 'text': 'Lies den lokalen Portalbefund zurück und prüfe dessen Portalliste.',
         'effect': 'local_execution'}]}}
    async with world(criteria=criteria, local_check=True, rework='fix',
            objective='Nenne die lokalen Portale und prüfe den lokal gespeicherten Portalbefund durch Rücklesen.') as w:
        accepted = await start(w)
        crashed = []
        actual = w.orch._dispatch_task_rework
        async def crashing(run, context, verify_step):
            crashed.append(verify_step.step_id)
            raise asyncio.CancelledError()
        with patch.object(w.orch, '_dispatch_task_rework', crashing):
            for _ in range(6):
                try:
                    await w.orch.tick()
                except asyncio.CancelledError:
                    break
                if crashed:
                    break
        require_equal(len(crashed), 1, 'the rework was not committed')
        pending = w.orch._task_rework_step(accepted['run_id'], pending=True)
        require(pending is not None and pending.outcome_reason == 'task_rework_pending')
        require_equal(w.ledger.get_run(accepted['run_id']).state, S.RUNNING)
        require_equal(len(w.calls), 1)
        # Restart: the context is rebuilt from the ledger, the marker dispatches the rework once.
        w.orch._contexts.pop(accepted['run_id'], None)
        run = await drive(w, accepted['run_id'])
        require_equal((run.state, len(w.calls)), (S.SUCCEEDED, 2), run.result_summary)
        require_equal([s.outcome_reason for s in w.ledger.steps_for_run(run.run_id) if s.kind == 'verify'], ['task_reworked', ''])


async def t_a_rework_never_sits_behind_an_effect_or_a_foreign_grant():
    """Review round 11, H11-4: the two guards of _try_task_rework — no step with an
    approval/execution/commit, and a grant of local tools only — as behaviour, not
    as a code comment. Either violated: no rework, the run ends on the first verdict."""
    criteria = {'anforderungen': {**CRITERIA['anforderungen'], 'handlungen': [
        {'id': 'h1', 'text': 'Lies den lokalen Portalbefund zurück und prüfe dessen Portalliste.',
         'effect': 'local_execution'}]}}
    objective = 'Nenne die lokalen Portale und prüfe den lokal gespeicherten Portalbefund durch Rücklesen.'
    from solvio.agent_runtime.task_authority import CapabilityGrant
    for label, tamper in (('execution on a step', 'execution'), ('foreign capability in the grant', 'grant')):
        async with world(criteria=criteria, local_check=True, rework='fix', objective=objective) as w:
            accepted = await start(w)
            actual = w.orch._try_task_rework
            async def spied(run, context, verdict, step, _actual=actual, _tamper=tamper):
                if _tamper == 'execution':
                    worker = next(s for s in w.ledger.steps_for_run(run.run_id) if s.kind == 'specialist')
                    with w.ledger._open() as db:
                        db.execute("UPDATE agent_steps SET execution_id='ex-probe' WHERE step_id=?", (worker.step_id,))
                else:
                    from dataclasses import replace as _replace
                    authority, real_for_run = w.orch.task_authority, w.orch.task_authority.for_run
                    def widened(run_id, _real=real_for_run):
                        grant = _real(run_id)
                        return _replace(grant, capabilities=(*grant.capabilities, CapabilityGrant('note_write', 1))) if grant else grant
                    with patch.object(authority, 'for_run', widened):
                        return await _actual(run, context, verdict, step)
                return await _actual(run, context, verdict, step)
            with patch.object(w.orch, '_try_task_rework', spied):
                run = await drive(w, accepted['run_id'])
            require_equal((run.state, run.failure_category, len(w.calls)), (S.FAILED, 'goal_unverified', 1), label)
            require(w.orch._task_rework_step(run.run_id) is None, label + ': a rework was committed')


async def t_a_started_marker_without_a_step_dispatches_once_after_a_restart():
    """Review round 11, H11-5: a process loss between the marker `task_rework_started`
    and the worker step's creation must lead to ONE rework on the next tick, never to
    plan_invalid over a delivered and assessed first result."""
    criteria = {'anforderungen': {**CRITERIA['anforderungen'], 'handlungen': [
        {'id': 'h1', 'text': 'Lies den lokalen Portalbefund zurück und prüfe dessen Portalliste.',
         'effect': 'local_execution'}]}}
    async with world(criteria=criteria, local_check=True, rework='fix',
            objective='Nenne die lokalen Portale und prüfe den lokal gespeicherten Portalbefund durch Rücklesen.') as w:
        accepted = await start(w)
        actual = w.orch._run_specialist_step
        crashed = []
        async def crashing(run, context, planned, seq, attempt=1):
            if not crashed and w.orch._task_rework_step(run.run_id) is not None:
                crashed.append(seq)
                raise asyncio.CancelledError()   # after the marker moved to `started`, before create_step
            return await actual(run, context, planned, seq, attempt)
        with patch.object(w.orch, '_run_specialist_step', crashing):
            for _ in range(8):
                try:
                    await w.orch.tick()
                except asyncio.CancelledError:
                    break
                if crashed:
                    break
        require_equal(len(crashed), 1)
        marker = w.orch._task_rework_step(accepted['run_id'])
        require(marker is not None and marker.outcome_reason == 'task_rework_started', marker)
        require(w.orch._task_rework_specialist(accepted['run_id'], marker) is None, 'a worker step exists')
        w.orch._contexts.pop(accepted['run_id'], None)
        run = await drive(w, accepted['run_id'])
        require_equal((run.state, len(w.calls)), (S.SUCCEEDED, 2), run.result_summary)


async def t_a_rework_parked_at_a_provider_boundary_is_reopened_as_the_same_step():
    """Review round 11, H11-6: the ONE rework turn meets a proven non-start (quota); the
    run parks WAITING_USER; after the owner's resume the same rework step is reopened —
    no second rework step, no verification over the first result."""
    criteria = {'anforderungen': {**CRITERIA['anforderungen'], 'handlungen': [
        {'id': 'h1', 'text': 'Lies den lokalen Portalbefund zurück und prüfe dessen Portalliste.',
         'effect': 'local_execution'}]}}
    async with world(criteria=criteria, local_check=True, rework='quota_then_fix',
            objective='Nenne die lokalen Portale und prüfe den lokal gespeicherten Portalbefund durch Rücklesen.') as w:
        accepted = await start(w)
        waiting = await drive(w, accepted['run_id'])
        require_equal(waiting.state, S.WAITING_USER, waiting.result_summary)
        steps = [s for s in w.ledger.steps_for_run(waiting.run_id) if s.kind == 'specialist']
        require_equal([s.state for s in steps], ['succeeded', 'waiting'])
        marker = w.orch._task_rework_step(waiting.run_id)
        require_equal(marker.outcome_reason, 'task_rework_started')
        require(await w.orch.resume(waiting.run_id))
        w.orch._contexts.pop(waiting.run_id, None)
        run = await drive(w, waiting.run_id)
        require_equal((run.state, run.assessment_calls), (S.SUCCEEDED, 2), run.result_summary)
        after = [s for s in w.ledger.steps_for_run(run.run_id) if s.kind == 'specialist']
        require_equal([s.step_id for s in after], [s.step_id for s in steps], 'the parked rework step was not reopened as itself')
        require_equal([s.state for s in after], ['succeeded', 'succeeded'])
        require_equal(w.orch._task_rework_step(run.run_id).outcome_reason, 'task_reworked')


async def t_a_worker_finding_about_keys_kills_neither_the_rework_nor_the_park_nor_the_result():
    """Review round 13, F13-1/K13-1 (the attempt-z class at the remaining write sites):
    the worker's finding names the KEYS of a dictionary and its recommendation the
    column keys; the Core's own colon ("Empfehlung des Spezialisten: …") plus the term
    made the memory heuristic refuse (a) the rework checkpoint — no rework, FAILED
    capability_failed —, (b) the provider boundary at a quota non-start — FAILED instead
    of WAITING_USER — and (c) the result summary at the SUCCEEDED transition. All three
    now go through the material fences: a key shape refuses, a statement line becomes a
    marker, the record and the run survive."""
    criteria = {'anforderungen': {**CRITERIA['anforderungen'], 'handlungen': [
        {'id': 'h1', 'text': 'Lies den lokalen Portalbefund zurück und prüfe dessen Portalliste.',
         'effect': 'local_execution'}]}}
    finding = 'Die CSV nutzt die Kopfzeile als Schlüssel; keys: element, zweck, quelle.'
    path = 'Die Spaltenschlüssel der CSV prüfen und die Datei herunterladen.'
    objective = 'Nenne die lokalen Portale und prüfe den lokal gespeicherten Portalbefund durch Rücklesen.'
    # (a) rework: the checkpoint written with the rework intent carries the finding
    async with world(criteria=criteria, local_check=True, rework='fix', objective=objective,
                     finding=finding, recommended_path=path) as w:
        accepted = await start(w)
        run = await drive(w, accepted['run_id'])
        require_equal((run.state, run.assessment_calls), (S.SUCCEEDED, 2), (run.failure_category, run.result_summary))
        require_equal(len(w.calls), 2, 'the rework did not happen')
        require('Spaltenschlüssel' in run.result_summary, run.result_summary)
        checkpoint = CP.decode(w.ledger.get_run(run.run_id).plan_checkpoint)
        require(checkpoint is not None and any('Kopfzeile' in f for f in checkpoint['befunde']), 'the finding vanished from the checkpoint')
    # (b) the ONE rework meets a quota non-start while the finding sits in the checkpoint
    async with world(criteria=criteria, local_check=True, rework='quota_then_fix', objective=objective,
                     finding=finding, recommended_path=path) as w:
        accepted = await start(w)
        waiting = await drive(w, accepted['run_id'])
        require_equal(waiting.state, S.WAITING_USER, (waiting.failure_category, waiting.result_summary))
        require(await w.orch.resume(waiting.run_id))
        w.orch._contexts.pop(waiting.run_id, None)
        run = await drive(w, waiting.run_id)
        require_equal(run.state, S.SUCCEEDED, run.result_summary)
    # a key SHAPE in the same finding never reaches the book: the specialist boundary redacts
    # it (`<entfernt>`) before any write, and the store's fences would refuse what slipped past
    async with world(criteria=criteria, local_check=True, rework='fix', objective=objective,
                     finding='API key: sk-' + 'A1b2C3d4E5f6G7h8I9j0K1l2', recommended_path=path) as w:
        accepted = await start(w)
        run = await drive(w, accepted['run_id'])
        book = json.dumps([e.summary for e in w.ledger.events_for_run(run.run_id)]) + (run.result_summary or '') + (run.plan_checkpoint or '')
        require('sk-A1b2' not in book and 'A1b2C3d4' not in book, 'a key shape reached the book')


async def t_proven_non_starts_never_count_as_attempts_of_the_loop_guard():
    """Review round 12, W12-1: a proven provider non-start (no turn, 0 ct) is not a
    'gleichartiger Versuch'. Two parks at the quota boundary and two owner resumes
    must reach the provider a third time — for the rework turn AND for the first
    step — instead of ending as loop_detected behind a delivered first result."""
    criteria = {'anforderungen': {**CRITERIA['anforderungen'], 'handlungen': [
        {'id': 'h1', 'text': 'Lies den lokalen Portalbefund zurück und prüfe dessen Portalliste.',
         'effect': 'local_execution'}]}}
    async with world(criteria=criteria, local_check=True, rework='quota_then_fix',
            objective='Nenne die lokalen Portale und prüfe den lokal gespeicherten Portalbefund durch Rücklesen.') as w:
        w.rework_parks_left = 2
        accepted = await start(w)
        for park in (1, 2):
            waiting = await drive(w, accepted['run_id'])
            require_equal(waiting.state, S.WAITING_USER, f'park {park}: ' + waiting.result_summary)
            require(await w.orch.resume(waiting.run_id))
            w.orch._contexts.pop(waiting.run_id, None)
        run = await drive(w, accepted['run_id'])
        require_equal((run.state, run.failure_category), (S.SUCCEEDED, ''), run.result_summary)
        steps = [s for s in w.ledger.steps_for_run(run.run_id) if s.kind == 'specialist']
        require_equal([s.state for s in steps], ['succeeded', 'succeeded'], 'the parked rework step was not reopened as itself')
        require_equal(len(w.calls), 4)   # first turn, two non-starts, the real rework turn
    # The first step: park, resume, park, resume, then the provider is available.
    async with world(mode='quota_before') as w:
        accepted = await start(w)
        for park in (1, 2):
            waiting = await drive(w, accepted['run_id'])
            require_equal(waiting.state, S.WAITING_USER, f'park {park}')
            require(await w.orch.resume(waiting.run_id))
            w.orch._contexts.pop(waiting.run_id, None)
        w.mode = 'ok'
        run = await drive(w, accepted['run_id'])
        require_equal(run.state, S.SUCCEEDED, run.result_summary)
        require_equal([s.step_id for s in w.ledger.steps_for_run(run.run_id) if s.kind == 'specialist'],
                      [next(s.step_id for s in w.ledger.steps_for_run(run.run_id) if s.kind == 'specialist')])


async def t_local_execution_receipt_cannot_become_a_file_or_external_effect():
    for effect, instruction in (('external', 'Buche das Hotel tatsächlich.'),
                                ('file', 'Liefere eine herunterladbare Datei.')):
        criteria = {'anforderungen': {**CRITERIA['anforderungen'], 'handlungen': [
            {'id': 'h1', 'text': instruction, 'effect': effect}]}}
        async with world(criteria=criteria, local_check=True,
                objective='Nenne die lokalen Portale. ' + instruction) as w:
            accepted = await start(w)
            run = await drive(w, accepted['run_id'])
            require_equal(run.state, S.FAILED)
            require_equal(run.failure_category, 'goal_unverified')
            # An observation token may name a file criterion in the catalogue (material for
            # bundled checks); the FILE proof itself is absent, so the file effect is not
            # verified — completion.information demands a token from _file_evidence_tokens.
            require_equal(w.orch._file_evidence_tokens(run.run_id), frozenset())
            require(all(not k.startswith('Core-Dateibeleg') for k in w.orch._verified_effects(run.run_id)))
            require_equal(len(w.calls), 1)  # a missing file is no 'concrete local gap' the worker could rework (reason action_not_verified)


async def t_complete_native_bundle_observations_fit_the_unchanged_assessment_budget():
    criteria = {'anforderungen': {**CRITERIA['anforderungen'],
        'auskunft': CRITERIA['anforderungen']['auskunft'] + [
            {'id':'a2', 'text':'Dokumentiere den beobachteten nativen Seitenaufruf der offiziellen Python-csv-Dokumentation.'}],
        'handlungen': [
        {'id':'h1', 'text':'Prüfe den lokalen Portalbefund durch Rücklesen.', 'effect':'local_execution'},
        {'id':'f1', 'text':'Liefere den Portalbefund als Datei.', 'effect':'file'},
        {'id':'f2', 'text':'Liefere den zugehörigen Bericht als Datei.', 'effect':'file'}]}}
    async with world(criteria=criteria, local_check=True, composite=True,
            objective='Dokumentiere den nativen Seitenaufruf der offiziellen Python csv-Dokumentation und lies die lokalen Portale. '
                'Prüfe den gespeicherten Portalbefund durch Rücklesen und liefere ihn mit einem Bericht als zwei Dateien.') as w:
        accepted = await start(w)
        run = await drive(w, accepted['run_id'])
        require_equal(run.state, S.SUCCEEDED)
        require_equal(len(RF.describe_files(w.ledger, run.run_id)[0]), 2)
        require_equal(len(w.calls), 1)
        snapshot = next(a for a in w.ledger.artifacts_for_run(run.run_id) if a.kind == 'evaluation_snapshot')
        body = Path(snapshot.path).read_text()
        require(all(token in body for token in ('portal_list','webSearch','openPage',
            'https://docs.python.org/3/library/csv.html','PORTAL_READBACK_OK','BUNDLE_READBACK_OK')))
        task = TR.task_view(w.ledger, run.run_id)
        size = PL.assessment_input_size(objective=task.objective, bound=json.loads(task.requirements),
            snapshot_body=body, verified_effects=w.orch._verified_effects(run.run_id))
        from solvio.agent_runtime import requirements as RQ
        require(size <= RQ.MAX_EVALUATION_CHARS)
        print('native_bundle_assessment_chars=' + str(size) + ';snapshot_chars=' + str(len(body)))


# ---------------------------------------------------------------------------
# N8/C4 — Claude Code as the second worker of the same order path: start
# choice, proven non-start with a switch offer, started turn without one, and
# no automatic failover in either case.
# ---------------------------------------------------------------------------

def _wait(run):
    return json.loads(run.boundary)['provider_wait']


def _specialist_step(w, run_id):
    return next(s for s in w.ledger.steps_for_run(run_id) if s.kind == 'specialist')


async def t_start_choice_is_read_once_and_persisted_on_the_run():
    async with world(mode='quota_before', worker='claude-code') as w:
        accepted = await start(w)
        waiting = await drive(w, accepted['run_id'])
        require_equal(waiting.state, S.WAITING_USER)
        require_equal(PS.selected(waiting, 'worker'), 'claude-code')
        choice = PS.selections(waiting.provider_selection)['worker']
        require(len(choice['boundary_ref']) == 64)
        require_equal(CP.decode(waiting.plan_checkpoint)['schritte'][0]['profil'], SP.CLAUDE_TASK_PROFILE)
        require_equal(_specialist_step(w, waiting.run_id).specialist_profile, SP.CLAUDE_TASK_PROFILE)
        require_equal(len(w.claude_calls), 1)
        require_equal(w.calls, [], 'the Codex worker was started although Claude was chosen')
        # A later setting change does not move the open order: the same
        # provider resume still runs the Claude worker.
        os.environ['AGENT_RUNTIME_TASK_WORKER'] = 'codex'
        require(await w.orch.resume(waiting.run_id))
        w.orch._contexts.pop(waiting.run_id, None)
        again = await drive(w, waiting.run_id)
        require_equal(again.state, S.WAITING_USER)
        require_equal(PS.selected(again, 'worker'), 'claude-code')
        require_equal(len(w.claude_calls), 2, 'the resumed attempt went to Claude, not to the new default')
        require_equal(w.claude_calls[1][1].profile, SP.CLAUDE_TASK_PROFILE)
        require_equal(w.calls, [])
        require(not Path(os.path.expanduser('~/.solvio-tasks')).exists()
                or not any(str(w.folder) in str(x) for x in Path(os.path.expanduser('~/.solvio-tasks')).rglob('*')),
                'the isolated world escaped into the owner home')
        session = w.claude_calls[0][1]
        require(session.workspace.startswith(w.roots['AGENT_RUNTIME_TASK_WORKSPACE_ROOT']))
        require_equal(w.bridge_roots[0], str(Path(w.claude_calls[0][2].jail) / 'sock'))
        require_equal({s.provider for s in w.tool_bindings}, {'claude-code'})


async def t_claude_proven_nonstart_parks_resumable_offers_codex_only_and_switches_on_the_same_workspace():
    """Proven non-start → resumable boundary → Codex offered → switch keeps the
    workspace, hands over Core records and the reopened step actually RUNS.

    The Claude non-start (N8_C4_API.md §2.4) leaves a settled physical claim
    on the outer step (ordinal 1). The reopened step keeps its operation_id,
    so its new cost scope continues BEHIND that settled, never-started claim
    (`native_tasks._continue_after_nonstart`) instead of colliding with it.
    """
    async with world(mode='quota_before', worker='claude-code') as w:
        accepted = await start(w)
        waiting = await drive(w, accepted['run_id'])
        require_equal(waiting.state, S.WAITING_USER)
        wait = _wait(waiting)
        require_equal((wait['provider'], wait['reason'], wait['resume_allowed'], wait['dispatch_started']),
                      ('claude-code', 'quota', True, False))
        require_equal(waiting.specialist_count, 0, 'a proven non-start must not consume the worker budget')
        step = _specialist_step(w, waiting.run_id)
        require_equal((step.state, step.outcome_reason, step.specialist_profile),
                      ('waiting', 'provider_wait', SP.CLAUDE_TASK_PROFILE))
        require_equal(len(w.claude_calls), 1)
        require_equal(w.calls, [], 'no automatic failover to Codex')
        records = [r for r in D.invocations(w.ledger, accepted['task_id']) if r['phase'] == 'specialist']
        require_equal([(r['provider'], r['state']) for r in records], [('claude-code', 'finished')],
                      'exactly one physical claim in the step, no second one')
        require_equal(w.broker.queries[0][0], 'task:' + accepted['task_id'])
        offers = PS.offers(w.ledger, waiting)
        require_equal([o['provider'] for o in offers], ['codex'])
        require('Lokale Datei- und Codearbeit im Auftragsordner' in offers[0]['werkzeuge'])
        require('Bearbeitung des Auftrags' in offers[0]['hinweis'])
        # Owner switch to Codex: same task, same workspace, one new session,
        # handover block from Core records in the new context.
        before_workspace = w.claude_calls[0][1].workspace
        require(await w.orch.resume(waiting.run_id, provider='codex', boundary_ref=PS.reference(waiting),
                                    principal='local-owner'))
        switched = w.ledger.get_run(waiting.run_id)
        require_equal(PS.selected(switched, 'worker'), 'codex')
        require_equal(_specialist_step(w, waiting.run_id).specialist_profile, SP.TASK_PROFILE)
        require_equal(CP.decode(switched.plan_checkpoint)['schritte'][0]['profil'], SP.TASK_PROFILE)
        w.mode = 'ok'   # the Codex double would complete the order
        run = await drive(w, waiting.run_id)
        require_equal(len(w.calls), 1)
        request, session = w.calls[0]
        require_equal(session.workspace, before_workspace, 'the switch must keep the workspace')
        require_equal(session.provider, 'codex')
        require('ÜBERGABE AUS DIESEM AUFTRAG' in request.context)
        require('vorheriger_anbieter: claude-code' in request.context)
        require('offene_anforderungen: ["a1", "h1"]' in request.context)
        require('KANONISCHER ANFORDERUNGSVERTRAG' in request.context)
        with w.ledger._open() as db:
            sessions = db.execute('SELECT provider, workspace FROM agent_native_sessions WHERE task_id=? ORDER BY provider',
                                  (accepted['task_id'],)).fetchall()
        require_equal([(r['provider'], r['workspace']) for r in sessions],
                      [('claude-code', before_workspace), ('codex', before_workspace)])
        require_equal([s.step_id for s in w.ledger.steps_for_run(run.run_id) if s.kind == 'specialist'],
                      [step.step_id], 'the same outer step was reused')
        require_equal(w.refusals, [], 'the reopened step must not collide with the settled non-start claim')
        require_equal(run.state, S.SUCCEEDED, run.failure_category + ': ' + run.result_summary)
        require_equal(run.specialist_count, 1)
        records = [r for r in D.invocations(w.ledger, accepted['task_id']) if r['phase'] == 'specialist']
        require_equal([(r['provider'], r['state'], r['ordinal']) for r in records],
                      [('claude-code', 'finished', 1), ('codex', 'finished', 2)],
                      'one settled non-start claim, then the Codex turn behind it')
        observation = next(a for a in w.ledger.artifacts_for_run(run.run_id) if a.kind == 'native_task_observation')
        require_equal(json.loads(Path(observation.path).read_text())['cost']['invocation_id'], records[1]['invocation_id'])


async def t_claude_nonstart_claim_without_broker_proof_parks_without_switch():
    for book in (ANSWERED_BOOK, dict(NONSTART_BOOK, rows=0, requests=0, outcomes={}, status_codes={})):
        async with world(mode='quota_before', worker='claude-code', book=book) as w:
            accepted = await start(w)
            waiting = await drive(w, accepted['run_id'])
            require_equal(waiting.state, S.WAITING_USER)
            wait = _wait(waiting)
            require_equal((wait['resume_allowed'], wait['dispatch_started']), (False, True),
                          'a non-start the broker book does not prove is a started turn')
            require_equal(waiting.specialist_count, 1)
            require_equal(PS.offers(w.ledger, waiting), [])
            require(not await w.orch.resume(waiting.run_id))
            for _ in range(2):
                await w.orch.tick()
            require_equal((len(w.claude_calls), w.calls), (1, []))


async def t_claude_quota_after_started_turn_parks_without_switch_or_second_attempt():
    async with world(mode='quota_after', worker='claude-code') as w:
        accepted = await start(w)
        waiting = await drive(w, accepted['run_id'])
        require_equal(waiting.state, S.WAITING_USER)
        wait = _wait(waiting)
        require_equal((wait['provider'], wait['resume_allowed'], wait['dispatch_started']), ('claude-code', False, True))
        require_equal(waiting.specialist_count, 1)
        require_equal(PS.offers(w.ledger, waiting), [], 'a started worker turn offers no switch (E7)')
        require(not await w.orch.resume(waiting.run_id))
        require(not await w.orch.resume(waiting.run_id, provider='codex', boundary_ref=PS.reference(waiting),
                                        principal='local-owner'))
        for _ in range(2):
            await w.orch.tick()
        require_equal(len(w.claude_calls), 1)
        require_equal(w.calls, [], 'no automatic failover after a started Claude turn')
        specialist = [r for r in D.invocations(w.ledger, accepted['task_id']) if r['phase'] == 'specialist']
        require_equal([(r['provider'], r['state']) for r in specialist], [('claude-code', 'finished')])


async def t_claude_proven_nonstart_same_provider_resume_reaches_the_worker_again():
    """Same-provider resume reopens the same outer step and reaches the Claude
    worker a second time without consuming the budget: a second proven
    non-start parks resumable again (ordinal 2 behind the settled first
    claim, same session, no replay lock). In a second world the resumed
    attempt completes the order on the same step, workspace and session — the
    C4 ok path in the product way (execute → observation → file publication
    → assessment). (The existing loop guard refuses a THIRD identical attempt
    for Codex and Claude alike; that limit is not this test's subject.)"""
    async with world(mode='quota_before', worker='claude-code') as w:
        accepted = await start(w)
        waiting = await drive(w, accepted['run_id'])
        require_equal(waiting.state, S.WAITING_USER)
        require(await w.orch.resume(waiting.run_id))
        w.orch._contexts.pop(waiting.run_id, None)
        again = await drive(w, waiting.run_id)
        require_equal(again.state, S.WAITING_USER)
        require_equal(len(w.claude_calls), 2)
        require_equal(again.specialist_count, 0)
        require_equal([s.step_id for s in w.ledger.steps_for_run(again.run_id) if s.kind == 'specialist'],
                      [_specialist_step(w, again.run_id).step_id])
        require_equal(w.calls, [], 'no failover to Codex')
        require_equal(w.refusals, [], 'the reopened step must reach the worker, not the replay lock')
        require_equal((_wait(again)['reason'], _wait(again)['resume_allowed']), ('quota', True))
        records = [r for r in D.invocations(w.ledger, accepted['task_id']) if r['phase'] == 'specialist']
        require_equal([(r['provider'], r['state'], r['ordinal']) for r in records],
                      [('claude-code', 'finished', 1), ('claude-code', 'finished', 2)])
        require_equal([(t['state'], t['terminal_status']) for t in _turns(w)],
                      [('terminal', 'not_started'), ('terminal', 'not_started')])
        require_equal(len({s.session_id for _, s, _ in w.claude_calls}), 1)
    async with world(mode='quota_before', worker='claude-code') as w:
        accepted = await start(w)
        waiting = await drive(w, accepted['run_id'])
        require_equal(waiting.state, S.WAITING_USER)
        step = _specialist_step(w, waiting.run_id)
        w.mode = 'ok'
        require(await w.orch.resume(waiting.run_id))
        w.orch._contexts.pop(waiting.run_id, None)
        done = await drive(w, waiting.run_id)
        require_equal(done.state, S.SUCCEEDED, done.failure_category + ': ' + done.result_summary)
        require_equal((len(w.claude_calls), done.specialist_count, w.refusals), (2, 1, []))
        require_equal(len({s.session_id for _, s, _ in w.claude_calls}), 1)
        require_equal(_specialist_step(w, done.run_id).step_id, step.step_id)
        records = [r for r in D.invocations(w.ledger, accepted['task_id']) if r['phase'] == 'specialist']
        require_equal([(r['provider'], r['state'], r['ordinal']) for r in records],
                      [('claude-code', 'finished', 1), ('claude-code', 'finished', 2)])
        observation = next(a for a in w.ledger.artifacts_for_run(done.run_id) if a.kind == 'native_task_observation')
        require_equal(json.loads(Path(observation.path).read_text())['cost']['invocation_id'], records[1]['invocation_id'])


def _turns(w):
    with w.ledger._open() as db:
        return [dict(r) for r in db.execute('SELECT * FROM agent_native_turns ORDER BY rowid').fetchall()]


async def t_claude_worker_completes_an_order_with_both_core_tools_and_no_test_seam():
    """The C4 ok path end to end: Claude session, real NativeCoreTools with
    portal_list AND result_files_list, observation with two dynamicToolCall
    receipts and two settled Core receipts, native file publication,
    assessment, SUCCEEDED — against the product's own claim lookup, with no
    test seam around cost_dispatch."""
    async with world(mode='ok', worker='claude-code') as w:
        # N8/C4 §4 on this router (the product registration in tools/registry
        # is an open main-agent item, as is the follow-up grant descriptor).
        RF.register(w.router, w.ledger)
        accepted = await start(w)
        grant = w.orch.task_authority.for_run(accepted['run_id'])
        require_equal({c.name for c in grant.capabilities}, {'portal_list', 'result_files_list', 'artifact_create'})
        run = await drive(w, accepted['run_id'])
        require_equal(run.state, S.SUCCEEDED, run.failure_category + ': ' + run.result_summary)
        require_equal((len(w.claude_calls), w.calls, run.specialist_count), (1, [], 1))
        step = _specialist_step(w, run.run_id)
        require_equal((step.state, step.specialist_profile), ('succeeded', SP.CLAUDE_TASK_PROFILE))
        tool_steps = [s for s in w.ledger.steps_for_run(run.run_id) if s.kind == 'capability']
        require_equal(sorted((s.capability, s.state) for s in tool_steps),
                      [('portal_list', 'succeeded'), ('result_files_list', 'succeeded')])
        observation = next(a for a in w.ledger.artifacts_for_run(run.run_id) if a.kind == 'native_task_observation')
        proof = json.loads(Path(observation.path).read_text())
        require_equal((proof['profile'], proof['cost']['provider'], proof['cost']['settlement_state']),
                      (SP.CLAUDE_TASK_PROFILE, 'claude-code', 'settled'))
        require_equal(sorted(r['tool'] for r in proof['receipts']), ['portal_list', 'result_files_list'])
        require_equal(sorted(c['tool'] for c in proof['core_tools']), ['portal_list', 'result_files_list'])
        require_equal(proof['helpers'], {'seeded': [], 'removed': []})
        # The observation is accepted as candidate evidence for BOTH tools.
        from solvio.agent_runtime import native_observations as O
        items = O.completion_evidence(w.ledger, run.run_id)
        require_equal(len(items), 1)
        material = json.loads(items[0].finding.split('\n', 1)[1])
        require_equal(sorted(c['tool'] for c in material['core_results']), ['portal_list', 'result_files_list'])
        require_equal(RF.describe_files(w.ledger, run.run_id)[0][0]['name'], 'answer.txt')
        require_equal({s.provider for s in w.tool_bindings}, {'claude-code'})
        # A READ_ONLY Core tool call is no external effect: the order stays
        # open for an Owner follow-up (eligibility, admission with the widened
        # grant, and the follow-up run itself on the same session/workspace).
        require(TR.eligibility(w.ledger, run.run_id)['eligible'], TR.eligibility(w.ledger, run.run_id))
        response = await F.post(w, F.request(w, run.run_id, client_request_id='claude-followup-1',
                                            text='Ergänze den bisherigen Befund genauer.'))
        require_equal(response.status, 202, await response.text())
        followup = await response.json()
        require_equal((followup['task_id'], followup['revision']), (accepted['task_id'], 2))
        second = await drive(w, followup['run_id'])
        require_equal(second.state, S.SUCCEEDED, second.failure_category + ': ' + second.result_summary)
        require_equal(len({s.session_id for _, s, _ in w.claude_calls}), 1, 'the follow-up left the Claude session')
        require_equal(len({s.workspace for _, s, _ in w.claude_calls}), 1)
        require_equal(sorted((s.capability, s.state) for s in w.ledger.steps_for_run(second.run_id) if s.kind == 'capability'),
                      [('portal_list', 'succeeded'), ('result_files_list', 'succeeded')])


async def t_codex_worker_with_result_files_list_registered_keeps_the_follow_up_path():
    """The same for the Codex worker once the product registers result_files_list
    (N8/C4 §4): grant with three entries, a succeeded capability step for the
    tool, eligibility and admission — no pin on `portal_list` anywhere in the
    follow-up path (`_READ_CAPABILITIES`, `source_descriptor`)."""
    async with world(mode='ok') as w:
        RF.register(w.router, w.ledger)
        accepted = await start(w)
        grant = w.orch.task_authority.for_run(accepted['run_id'])
        require_equal({c.name for c in grant.capabilities}, {'portal_list', 'result_files_list', 'artifact_create'})
        run = await drive(w, accepted['run_id'])
        require_equal(run.state, S.SUCCEEDED, run.failure_category + ': ' + run.result_summary)
        require_equal(sorted((s.capability, s.state) for s in w.ledger.steps_for_run(run.run_id) if s.kind == 'capability'),
                      [('portal_list', 'succeeded'), ('result_files_list', 'succeeded')])
        require(TR.eligibility(w.ledger, run.run_id)['eligible'], TR.eligibility(w.ledger, run.run_id))
        response = await F.post(w, F.request(w, run.run_id, client_request_id='codex-followup-1',
                                            text='Ergänze den bisherigen Befund genauer.'))
        require_equal(response.status, 202, await response.text())
        followup = await response.json()
        second = await drive(w, followup['run_id'])
        require_equal(second.state, S.SUCCEEDED, second.failure_category + ': ' + second.result_summary)
        require_equal(len({s.session_id for _, s in w.calls}), 1)
        # Counter-probe: a capability step OUTSIDE the read-only set still closes the path.
        with w.ledger._open() as db:
            db.execute("UPDATE agent_steps SET capability='portal_open' WHERE run_id=? AND capability='result_files_list'",
                       (second.run_id,))
        require_equal(TR.eligibility(w.ledger, second.run_id),
                      {'eligible': False, 'reason': 'followup_external_effect_not_supported'})


async def t_codex_worker_reads_mail_through_the_real_bridge_and_the_order_succeeds():
    """Stufe S1 (Kurskorrektur 25.09.2026) end to end: with the ORIGINAL
    GmailCapabilities registered (fake provider), the grant carries the mail
    reads, the worker searches and reads one mail through the real bridge,
    the observation binds both calls WITH their arguments and keeps a
    projection instead of the mail, and the order stays open for a follow-up."""
    import test_native_mail_calendar_tools as MT
    from solvio.capabilities import gmail as G
    from solvio.agent_runtime import native_observations as O
    async with world(mode='ok') as w:
        provider = MT.RecordingGmail([MT.mail('m1', 'Rechnung September', 'Betrag 120 Euro. ' * 400)])
        G.register(w.router, G.GmailCapabilities(provider))
        # Without the chat's private-data marker an order gets no mail at all (ADR-0040).
        plain = await start(w)
        require(not {c.name for c in w.orch.task_authority.for_run(plain['run_id']).capabilities}
                & {'gmail_search', 'gmail_read_message'}, 'an ordinary order reads no mail')
        await w.orch.cancel(plain['run_id']) if hasattr(w.orch, 'cancel') else None
        accepted = await start_private_chat_task(w)
        grant = w.orch.task_authority.for_run(accepted['run_id'])
        require({'gmail_search', 'gmail_read_message', 'gmail_list_recent', 'gmail_read_thread'}
                <= {c.name for c in grant.capabilities}, grant.capabilities)
        require(not {c.name for c in grant.capabilities} & {'gmail_create_draft', 'gmail_send_draft'})
        run = await drive(w, accepted['run_id'])
        require_equal(run.state, S.SUCCEEDED, run.failure_category + ': ' + run.result_summary)
        require_equal(provider.calls, [('search', 'rechnung', 3, ''), ('message', 'm1')])
        require_equal(sorted((s.capability, s.state) for s in w.ledger.steps_for_run(run.run_id) if s.kind == 'capability'),
                      [('gmail_read_message', 'succeeded'), ('gmail_search', 'succeeded'), ('portal_list', 'succeeded')])
        observation = next(a for a in w.ledger.artifacts_for_run(run.run_id) if a.kind == 'native_task_observation')
        proof = json.loads(Path(observation.path).read_text())
        mail = {c['tool']: c for c in proof['core_tools'] if c['tool'].startswith('gmail_')}
        require_equal(sorted(mail), ['gmail_read_message', 'gmail_search'])
        for entry in mail.values():
            require_equal(entry['response']['projection'], 'read_tool_material')
            require('contentItems' not in entry['response'], 'the mail itself is not in the evidence')
            require(len(entry['response'].get('preview', '')) <= O.READ_PREVIEWS[0])
            require_equal(entry['cost']['provider'], 'google.gmail')
        # The mail says it 400 times; the evidence keeps two short previews at most.
        require(json.dumps(proof).count('Betrag 120 Euro') < 100, 'the full mail reached the evidence')
        items = O.completion_evidence(w.ledger, run.run_id)
        require_equal(len(items), 1)
        material = json.loads(items[0].finding.split('\n', 1)[1])
        require({'gmail_search', 'gmail_read_message'} <= {c['tool'] for c in material['core_results']})
        require(TR.eligibility(w.ledger, run.run_id)['eligible'], TR.eligibility(w.ledger, run.run_id))
        # A follow-up inherits the private-data marker of its order (ADR-0040).
        response = await F.post(w, F.request(w, run.run_id, client_request_id='mail-followup-1',
                                            text='Ergänze bitte die Absender.'))
        require_equal(response.status, 202, await response.text())
        followup = await response.json()
        inherited = {c.name for c in w.orch.task_authority.for_run(followup['run_id']).capabilities}
        require({'gmail_search', 'gmail_read_message'} <= inherited, inherited)


async def t_a_follow_up_of_a_mail_order_is_refused_while_its_mail_tools_are_unavailable():
    """Review S1R2-3 / ADR-0040: without its mail tools the follow-up would continue
    the same native thread WITH web search. It is refused instead."""
    import test_native_mail_calendar_tools as MT
    from solvio.capabilities import gmail as G
    async with world(mode='ok') as w:
        G.register(w.router, G.GmailCapabilities(MT.RecordingGmail([MT.mail('m1', 'Rechnung', 'x')])))
        accepted = await start_private_chat_task(w, request_id='chat-private-002')
        run = await drive(w, accepted['run_id'])
        require_equal(run.state, S.SUCCEEDED, run.failure_category + ': ' + run.result_summary)
        for name in G.GmailTaskRead.METHODS:       # Google is gone after a restart
            w.router._specs.pop(name, None)
            w.router._handlers.pop(name, None)
        response = await F.post(w, F.request(w, run.run_id, client_request_id='mail-followup-2',
                                            text='Ergänze bitte die Absender.'))
        require(response.status != 202, 'a follow-up without its mail tools was admitted')
        require_equal(len(w.ledger.runs_for_task(accepted['task_id'])), 1, 'no second run was created')


async def t_claude_worker_after_negative_assessment_gets_the_explicit_owner_revision_like_codex():
    """`_completed_native_task` (failed_research, goal_unverified): the worker
    profile names the provider; session, turn and physical claim must carry
    THAT provider. A Claude worker run ends eligible exactly like a Codex one,
    and a provider that does not match the profile closes the path."""
    criteria = {'anforderungen': {**CRITERIA['anforderungen'], 'handlungen': [
        {'id': 'h1', 'text': 'Buche das Hotel tatsächlich.', 'effect': 'external'}]}}
    async with world(criteria=criteria, worker='claude-code',
                     objective='Nenne die lokal konfigurierten Portale und buche das Hotel tatsächlich.') as w:
        accepted = await start(w)
        run = await drive(w, accepted['run_id'])
        require_equal((run.state, run.failure_category), (S.FAILED, 'goal_unverified'))
        require_equal(len(w.claude_calls), 1)
        require_equal(_specialist_step(w, run.run_id).specialist_profile, SP.CLAUDE_TASK_PROFILE)
        require(TR.eligibility(w.ledger, run.run_id)['eligible'], TR.eligibility(w.ledger, run.run_id))
        with w.ledger._open() as db:
            session_id = db.execute('SELECT session_id FROM agent_native_sessions WHERE task_id=?',
                                    (accepted['task_id'],)).fetchone()[0]
            # Counter-probe: the durable session names another provider than the
            # step's profile → unconfirmed, not "any native worker will do".
            db.execute("UPDATE agent_native_sessions SET provider='codex' WHERE session_id=?", (session_id,))
        require_equal(TR.eligibility(w.ledger, run.run_id), {'eligible': False, 'reason': 'followup_native_task_unconfirmed'})
        with w.ledger._open() as db:
            db.execute("UPDATE agent_native_sessions SET provider='claude-code' WHERE session_id=?", (session_id,))
        response = await F.post(w, F.request(w, run.run_id, client_request_id='explicit-claude-clarification',
            text='Nur einen Dateibericht erstellen. Keine Buchung vornehmen.'))
        require_equal(response.status, 202, await response.text())
        followup = await response.json()
        require_equal((followup['task_id'], followup['revision']), (accepted['task_id'], 2))
        require_equal(w.ledger.get_run(run.run_id), run)
        require_equal(len(w.claude_calls), 1, 'admission started a second provider invocation')


async def t_an_unreadable_helper_declaration_never_sinks_a_delivered_order():
    """§3.2: a helper is manufacturing method, not a deliverable. A declaration
    the Core cannot read as a candidate (typo in the path; a file that is not
    UTF-8) is rejected with its reason in the event log; the published result
    file stays visible and the run ends SUCCEEDED. A valid declaration beside
    an invalid one still becomes a candidate."""
    from solvio.agent_runtime import extension_versions as EV
    cases = (
        ('tools/nicht_da.py', 'helper_file_unreadable'),
        ('tools/blob.py', 'helper_bytes_invalid'),
    )
    for path, reason in cases:
        async with world(mode='helper') as w:
            w.helper_declarations = [{'path': path, 'name': 'csv_sum', 'purpose': 'Summiert eine CSV-Spalte (Betrag)'},
                                     {'path': 'tools/csv_sum.py', 'name': 'csv_sum_ok', 'purpose': 'Summiert eine CSV-Spalte'}]
            if path == 'tools/blob.py':
                w.helper_files = {path: b'\xff\xfe\x00print(1)\n'}
            accepted = await start(w)
            run = await drive(w, accepted['run_id'])
            require_equal(run.state, S.SUCCEEDED, path + ': ' + run.failure_category + ': ' + run.result_summary)
            require_equal(_specialist_step(w, run.run_id).outcome_reason, 'ok')
            require_equal([f['name'] for f in RF.describe_files(w.ledger, run.run_id)[0]], ['answer.txt'])
            candidates = [a for a in w.ledger.artifacts_for_run(run.run_id) if a.kind == EV.HELPER_CANDIDATE_KIND]
            require_equal([json.loads(Path(a.path).read_text())['path'] for a in candidates], ['tools/csv_sum.py'],
                          'the valid declaration beside the invalid one must still become a candidate')
            events = [e for e in w.ledger.events_for_run(run.run_id) if 'nicht als Kandidat' in e.summary]
            require_equal([(e.kind, path in e.summary, reason in e.summary) for e in events],
                          [('helper_published', True, True)], [e.summary for e in events])
            observation = next(a for a in w.ledger.artifacts_for_run(run.run_id) if a.kind == 'native_task_observation')
            require_equal(json.loads(Path(observation.path).read_text())['helpers'], {'seeded': [], 'removed': []})
    # A declaration the WORKER could not hand over (measured 19.09.2026, third real
    # Durchstich: name "CSV- und Berichtsgenerator") arrives as a rejection beside
    # the accepted one — an owner-visible event, the result and the run intact.
    async with world(mode='helper') as w:
        w.helper_rejections = ((1, 'helper_declaration_invalid_name'),)
        accepted = await start(w)
        run = await drive(w, accepted['run_id'])
        require_equal(run.state, S.SUCCEEDED, run.failure_category + ': ' + run.result_summary)
        require_equal([f['name'] for f in RF.describe_files(w.ledger, run.run_id)[0]], ['answer.txt'])
        candidates = [a for a in w.ledger.artifacts_for_run(run.run_id) if a.kind == EV.HELPER_CANDIDATE_KIND]
        require_equal([json.loads(Path(a.path).read_text())['path'] for a in candidates], ['tools/csv_sum.py'])
        events = [e.summary for e in w.ledger.events_for_run(run.run_id) if 'nicht als Kandidat' in e.summary]
        require_equal(events, ['Helfer nicht als Kandidat übernommen (helpers[1]): helper_declaration_invalid_name.'])
    # Even a context-level refusal of the candidate step (binding changed under
    # the read) is not a lost result: the file was published under its own
    # verified binding before; the refusal becomes an event, the run SUCCEEDED.
    from unittest.mock import AsyncMock
    async with world(mode='helper') as w:
        with patch.object(NRF, 'publish_helper_candidates', AsyncMock(side_effect=ValueError('native_result_binding_changed'))):
            accepted = await start(w)
            run = await drive(w, accepted['run_id'])
        require_equal(run.state, S.SUCCEEDED, run.failure_category + ': ' + run.result_summary)
        require_equal([f['name'] for f in RF.describe_files(w.ledger, run.run_id)[0]], ['answer.txt'])
        require_equal([a for a in w.ledger.artifacts_for_run(run.run_id) if a.kind == EV.HELPER_CANDIDATE_KIND], [])
        events = [e.summary for e in w.ledger.events_for_run(run.run_id) if 'nicht als Kandidat' in e.summary]
        require_equal(events, ['Helfer nicht als Kandidat übernommen (*): native_result_binding_changed.'])


async def t_claude_lease_gate_before_the_starter_is_a_resumable_owner_boundary():
    """A refused broker lease (principal caps) or a missing broker ran no
    process and spent no token: the order parks at the owner boundary
    (`provider_unavailable`, resumable, budget untouched) instead of failing,
    and the resume reaches the worker again behind the released claim."""
    async with world(mode='lease_refused', worker='claude-code') as w:
        accepted = await start(w)
        waiting = await drive(w, accepted['run_id'])
        require_equal(waiting.state, S.WAITING_USER, waiting.failure_category)
        wait = _wait(waiting)
        require_equal((wait['provider'], wait['reason'], wait['resume_allowed'], wait['dispatch_started']),
                      ('claude-code', 'provider_unavailable', True, False))
        require_equal(waiting.specialist_count, 0)
        require_equal(w.refusals, ['broker_lease_refused'])
        step = _specialist_step(w, waiting.run_id)
        require_equal((step.state, step.outcome_reason), ('waiting', 'provider_wait'))
        records = [r for r in D.invocations(w.ledger, accepted['task_id']) if r['phase'] == 'specialist']
        require_equal([(r['provider'], r['state']) for r in records], [('claude-code', 'not_dispatched')])
        require_equal([(t['state'], t['terminal_status']) for t in _turns(w)], [('terminal', 'not_started')])
        require_equal(PS.offers(w.ledger, waiting), [], 'a switch offer belongs to the proven quota non-start only')
        w.mode = 'ok'
        require(await w.orch.resume(waiting.run_id))
        w.orch._contexts.pop(waiting.run_id, None)
        done = await drive(w, waiting.run_id)
        require_equal(done.state, S.SUCCEEDED, done.failure_category + ': ' + done.result_summary)
        require_equal(w.refusals, ['broker_lease_refused'], 'the reopened step met the replay lock')
        require_equal((len(w.claude_calls), done.specialist_count), (2, 1))


async def t_a_published_helper_is_seeded_into_the_next_task_and_its_reuse_is_observed():
    """§3.2–§3.5 in the product path: task one declares a helper with a real
    readback receipt → candidate after the turn → published only after
    SUCCEEDED (`helper_published` event) → task two (new task_id) finds the
    0o400 copy under .solvio-helpers, the Core context line, uses it, and the
    observation records `helpers.seeded[0].unchanged == True`."""
    import stat
    from solvio.agent_runtime import extension_versions as EV, helper_seeding as HS
    async with world(mode='helper') as w:
        accepted = await start(w)
        first = await drive(w, accepted['run_id'])
        require_equal(first.state, S.SUCCEEDED, first.failure_category + ': ' + first.result_summary)
        candidates = [a for a in w.ledger.artifacts_for_run(first.run_id) if a.kind == EV.HELPER_CANDIDATE_KIND]
        require_equal(len(candidates), 1, 'the declared helper became a candidate after the turn')
        # The assessor sees the Core's own verdict on the candidate (static check: stdlib
        # only, compile probe) — never the source text (measured 19.09.2026: "nur
        # Standardbibliothek" was unjudgeable without it, the done job goal_unverified).
        deliveries = NRF.helper_candidate_evidence(w.ledger, first.run_id)
        require_equal([d.artifact_id for d in deliveries], [candidates[0].artifact_id])
        require('Core-Helferkandidat helper-candidate:' + candidates[0].sha256 + ':tools/csv_sum.py' in deliveries[0].evidence
                and 'statische Pruefung am Quelltext bestanden: ' + NRF.STATIC_CHECK_MEANING in deliveries[0].evidence
                and 'KEIN Beweis' in deliveries[0].evidence  # review round 10, W10-3: never more than the check proves
                and 'Kompilierprobe bestanden' in deliveries[0].evidence,
                deliveries[0].evidence)
        projection = json.loads(deliveries[0].finding[deliveries[0].finding.index('{'):])
        require_equal((projection['path'], projection['name'], projection['static_check']['ok'], projection['compile_probe']['ok']),
                      ('tools/csv_sum.py', 'csv_sum', True, True))
        require('import csv' not in deliveries[0].finding and 'DictReader' not in deliveries[0].finding, 'source text leaked into the assessor view')
        snapshot = next(a for a in w.ledger.artifacts_for_run(first.run_id) if a.kind == 'evaluation_snapshot')
        require('Core-Helferkandidat helper-candidate:' + candidates[0].sha256 in Path(snapshot.path).read_text(),
                'the candidate verdict did not reach the assessment snapshot')
        versions = EV.helper_versions(w.ledger).helpers_for('local-owner')
        require_equal([(v.name, v.origin_run_id, v.origin_artifact_id) for v in versions],
                      [('csv_sum', first.run_id, candidates[0].artifact_id)])
        version_id = versions[0].version_id
        events = [e for e in w.ledger.events_for_run(first.run_id) if e.kind == 'helper_published']
        require_equal([(e.ref, version_id in e.summary) for e in events], [(version_id, True)])
        proof = json.loads(Path(next(a for a in w.ledger.artifacts_for_run(first.run_id)
                                     if a.kind == 'native_task_observation').path).read_text())
        require_equal(proof['helpers'], {'seeded': [], 'removed': []}, 'task one had nothing to reuse')
        # Task two: a NEW task of the same owner.
        w.mode = 'reuse'
        w.objective = 'Summiere die Beträge der neuen CSV mit dem vorhandenen Helfer und liefere den Befund als Textdatei.'
        second = await start(w, client_request_id='request-002')
        require(second['task_id'] != accepted['task_id'])
        run = await drive(w, second['run_id'])
        require_equal(run.state, S.SUCCEEDED, run.failure_category + ': ' + run.result_summary)
        request, session = w.calls[-1]
        copy = Path(session.workspace) / HS.HELPERS_DIR / version_id / 'csv_sum.py'
        require(copy.is_file(), 'the helper was not seeded into the new workspace')
        require_equal(stat.S_IMODE(copy.stat().st_mode), 0o400)
        require_equal(copy.read_bytes(), HELPER_SOURCE.encode())
        catalog = json.loads((Path(session.workspace) / HS.HELPERS_DIR / HS.CATALOG).read_text())
        require_equal([entry['version_id'] for entry in catalog], [version_id])
        require(HS.CONTEXT_LINE in request.context, 'the Core context line is missing')
        require('KANONISCHER ANFORDERUNGSVERTRAG' in request.context)
        proof = json.loads(Path(next(a for a in w.ledger.artifacts_for_run(run.run_id)
                                     if a.kind == 'native_task_observation').path).read_text())
        require_equal(proof['helpers'], {'seeded': [{'version_id': version_id, 'unchanged': True}], 'removed': []})
        reuse = next(r for r in proof['receipts'] if r['item_id'] == 'helper-reuse')
        require_equal((reuse['status'], reuse['exit_code']), ('completed', 0))
        require(HS.HELPERS_DIR + '/' + version_id + '/csv_sum.py' in reuse['command']['text'])
        require('"sum": 3.0' in reuse['output']['text'])
        require_equal([a for a in w.ledger.artifacts_for_run(run.run_id) if a.kind == EV.HELPER_CANDIDATE_KIND], [],
                      'a seeded helper is never re-declared as a candidate')
        require_equal(w.orch.costs.view(second['task_id'])['counts'], {'settled': 4})


async def t_the_reopened_step_continues_only_behind_settled_never_started_attempts():
    """The replay lock stays the rule: a new cost scope for the same operation
    starts at ordinal 1 and collides with its predecessor. The ONE exception is
    the reopened worker step whose prior attempts are all terminal, settled or
    released, and never started (`terminal/not_started`)."""
    from solvio.agent_runtime import native_sessions as NS
    # (a) proven non-start → continue behind ordinal 1; the same scope again
    #     starts at 2 (deterministic invocation ids, no double skip).
    async with world(mode='quota_before', worker='claude-code') as w:
        accepted = await start(w)
        waiting = await drive(w, accepted['run_id'])
        step = _specialist_step(w, waiting.run_id)
        sessions = NS.NativeSessions(w.ledger, authority=w.orch.task_authority)
        scope = D.TaskCostScope(w.ledger, task_id=accepted['task_id'], run_id=waiting.run_id,
                                phase='specialist', operation_id=step.step_id)
        first_id = D.TaskCostScope(w.ledger, task_id=accepted['task_id'], run_id=waiting.run_id,
                                   phase='specialist', operation_id=step.step_id).next_invocation()[1]
        require_equal([r['invocation_id'] for r in D.invocations(w.ledger, accepted['task_id']) if r['phase'] == 'specialist'],
                      [first_id], 'the settled non-start claim sits at ordinal 1')
        require_equal(N._continue_after_nonstart(sessions, scope, step), 1)
        ordinal, invocation_id = scope.next_invocation()
        require_equal(ordinal, 2)
        require(invocation_id != first_id)
        # A foreign operation's rows never move this scope.
        other = D.TaskCostScope(w.ledger, task_id=accepted['task_id'], run_id=waiting.run_id,
                                phase='specialist', operation_id='some-other-step')
        require_equal(other.continue_after_settled(), 0)
        require_equal(other.next_invocation()[0], 1)
    # (b) a started turn (quota after the first assistant event: terminal/failed,
    #     settled) keeps the lock even though the cost side is settled.
    async with world(mode='quota_after', worker='claude-code') as w:
        accepted = await start(w)
        waiting = await drive(w, accepted['run_id'])
        step = _specialist_step(w, waiting.run_id)
        sessions = NS.NativeSessions(w.ledger, authority=w.orch.task_authority)
        scope = D.TaskCostScope(w.ledger, task_id=accepted['task_id'], run_id=waiting.run_id,
                                phase='specialist', operation_id=step.step_id)
        require_equal(N._continue_after_nonstart(sessions, scope, step), 0, 'a started turn is never skipped')
        require_equal(scope.next_invocation()[0], 1)
        require_equal(scope.continue_after_settled(), 1, 'the cost layer alone would continue: the turn gate is what holds')
    # (c) an uncertain attempt (unknown reservation) keeps the lock at the cost layer.
    async with world(mode='lost') as w:
        accepted = await start(w)
        waiting = await drive(w, accepted['run_id'])
        step = _specialist_step(w, waiting.run_id)
        records = [r for r in D.invocations(w.ledger, accepted['task_id']) if r['phase'] == 'specialist']
        require_equal([r['state'] for r in records], ['unknown'])
        scope = D.TaskCostScope(w.ledger, task_id=accepted['task_id'], run_id=waiting.run_id,
                                phase='specialist', operation_id=step.step_id)
        require_equal(scope.continue_after_settled(), 0)
        require_equal(scope.next_invocation()[1], records[0]['invocation_id'], 'the lock: same id, refused later')
        sessions = NS.NativeSessions(w.ledger, authority=w.orch.task_authority)
        scope = D.TaskCostScope(w.ledger, task_id=accepted['task_id'], run_id=waiting.run_id,
                                phase='specialist', operation_id=step.step_id)
        require_equal(N._continue_after_nonstart(sessions, scope, step), 0)


def t_an_isolated_world_never_creates_the_owner_home_roots():
    """With its own SOLVIO_STATE_DIR a world is isolated; pointing the roots at
    the owner's `~/.solvio-tasks` is refused BEFORE any directory is made."""
    home_root = Path(os.path.expanduser('~/.solvio-tasks'))
    before = {str(p) for p in home_root.rglob('*')} if home_root.exists() else None
    with patch.dict(os.environ, {'SOLVIO_STATE_DIR': '/private/tmp/solvio-isolated-probe'}):
        for setting, name in (('~/.solvio-tasks/workspaces', 'native_workspace_root'),
                              ('~/.solvio-tasks/claude-jails', 'native_jail_root'),
                              (str(home_root / 'elsewhere'), 'native_workspace_root')):
            try:
                N._private_root(setting, name)
            except ValueError as exc:
                require_equal(str(exc), name + '_not_isolated')
            else:
                raise AssertionError('the isolated world reached the owner home root: ' + setting)
        for setting, name in (('', 'native_workspace_root'), ('relative/path', 'native_workspace_root'), ('/', 'native_jail_root')):
            try:
                N._private_root(setting, name)
            except ValueError as exc:
                require(str(exc).startswith(name + '_'), str(exc))
            else:
                raise AssertionError('accepted: ' + repr(setting))
    # A variable that merely spells out the installation's default is NOT an isolated
    # world: the default roots stay allowed (review round 7, A7-H2 — before, the bare
    # presence of SOLVIO_STATE_DIR=~/.solvio failed every native order). The probe
    # stops at the first mkdir, so nothing is created in the owner's home.
    class ReachedMkdir(Exception):
        pass
    probe_root = str(home_root / ('probe-' + os.urandom(4).hex()))
    with patch.dict(os.environ, {'SOLVIO_STATE_DIR': os.path.expanduser('~/.solvio')}), \
            patch.object(N.Path, 'mkdir', side_effect=ReachedMkdir()):
        try:
            N._private_root(probe_root, 'native_workspace_root')
        except ReachedMkdir:
            pass                              # the isolation guard let the default world through
        except ValueError as exc:
            raise AssertionError('the default state dir counted as isolation: ' + str(exc))
    after = {str(p) for p in home_root.rglob('*')} if home_root.exists() else None
    require_equal(after, before, 'the probe changed the owner home root')


async def t_codex_nonstart_offers_claude_only_when_every_worker_gate_holds():
    from solvio.agent_runtime import native_costs as NC
    async with world(mode='quota_before') as w:
        accepted = await start(w)
        waiting = await drive(w, accepted['run_id'])
        require_equal(waiting.state, S.WAITING_USER)
        require_equal(_wait(waiting)['provider'], 'codex')
        require_equal(PS.offers(w.ledger, waiting), [], 'Claude offered without broker, grant and pot proof')
        module = _claude_module()
        with claude_module_double(module), \
                patch.object(PS, '_WORKER_CANARY', ''), \
                patch.object(PS, '_measure', side_effect=lambda function, **kwargs: function()), \
                patch.object(NC, 'claude_worker_preconditions', return_value=(True, '', {})):
            # The bound criteria name the portal catalogue (a Core read path):
            # without the MCP bridge (M-C4 red) the toolless worker is not offered.
            # The probe's async lifecycle/cache has its own provider-switch tests;
            # this admission test supplies the two measured gate verdicts directly.
            require_equal(PS.offers(w.ledger, waiting), [], 'Core tool need without bridge')
            module.mcp_mode = lambda: 'bridge'
            offers = PS.offers(w.ledger, waiting)
            require_equal([o['provider'] for o in offers], ['claude-code'])
            require_equal(offers[0]['werkzeuge'], ['Lokale Datei- und Codearbeit im Auftragsordner', 'Core-Lesewege'])
            with patch.object(PS, '_WORKER_CANARY', 'worker_canary_failed:leak'):
                require_equal(PS.offers(w.ledger, waiting), [], 'a red worker canary must block the offer')
            with patch.object(NC, 'claude_worker_preconditions', return_value=(False, 'pot_proof_missing', {})):
                require_equal(PS.offers(w.ledger, waiting), [])
            with patch.object(PS.isolation if hasattr(PS, 'isolation') else __import__('solvio.agent_runtime.isolation', fromlist=['available']), 'available', return_value=False):
                require_equal(PS.offers(w.ledger, waiting), [], 'no sandbox, no Claude worker')


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
