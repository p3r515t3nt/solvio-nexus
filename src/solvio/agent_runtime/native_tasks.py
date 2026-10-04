"""One full Owner order delegated to a native task worker (Codex or Claude Code).

The static outer step is only a lifecycle checkpoint. The native worker owns
its planning and execution; Core retains authority, physical costs, durable
turns and actual result publication. No retry or additional agent loop lives
here, and no automatic provider failover: a proven non-start opens the
existing owner boundary with a switch offer, everything else parks without one.

N8/C4: both workers share ONE workspace root (`agent_runtime_task_workspace_root`,
outside `~/.solvio` and `~/.solvio-nexus`, both sealed for the Claude jail), so
an owner switch keeps the workspace literally. The Claude jail lives beside the
workspace under `agent_runtime_claude_jail_root`, never inside it.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import time

from solvio.agent_runtime import cost_dispatch as D, native_sessions as N
from solvio.agent_runtime import planner as PL, requirements as RQ, specialists as SP
from solvio.agent_runtime import provider_switch as PS
from solvio.agent_runtime import store as S, task_revisions as TR
from solvio.logging_setup import get_logger
from solvio.secret_vault.firewall import redact_lines_if_credential, refuse_if_key_shaped

log = get_logger('agent_runtime')

#: Broker ledger outcomes that prove a refusal BEFORE any model answer.
NONSTART_OUTCOMES = frozenset({'denied', 'upstream_error'})
NONSTART_STATUS = 429
#: Handover block bounds (§2.5): Core records only, never a foreign transcript.
HANDOVER_SUMMARY_CHARS = 1500
HANDOVER_MAX_FILES = 12
#: Bound of removed helper directories recorded per observation (§3.4).
HELPERS_REMOVED_MAX = 64
#: Only categorical criteria errors are retained, never the rejected model text.
REQUIREMENTS_REPAIR_REASONS = frozenset({'native_requirements_invalid', 'not_json'})
REQUIREMENTS_REPAIR_REF = 'native_requirements_repair:'


def delegation(task, provider='codex'):
    if task.scope != S.SCOPE_TASK or task.target_repo:
        raise PL.PlanInvalid('native_worker_scope_required')
    if provider not in SP.WORKER_PROFILES:
        raise PL.PlanInvalid('native_worker_provider_invalid')
    return PL.Plan(goal=task.objective, steps=(PL.PlannedStep(
        kind='specialist', profile=SP.WORKER_PROFILES[provider], instruction=task.objective),))


def check_plan(task, plan):
    """Equal modulo the worker profile: goal and instruction fixed, profile one of the two workers."""
    if plan is None or plan.goal != task.objective or len(plan.steps) != 1:
        raise PL.PlanInvalid('native_delegation_changed')
    profile = plan.steps[0].profile
    if profile not in SP.WORKER_PROFILES.values():
        raise PL.PlanInvalid('native_delegation_changed')
    if plan.steps != delegation(task, SP.profile(profile).provider).steps:
        raise PL.PlanInvalid('native_delegation_changed')


def requirements(call, objective):
    raw = PL.call_payload(call)
    if type(raw) is not dict or set(raw) != {'anforderungen'}:
        raise PL.PlanInvalid('native_requirements_invalid')
    block = raw['anforderungen']
    if type(block) is not dict or set(block) != {RQ.ASK, RQ.ACTION, RQ.UNCLEAR, 'belege'}:
        raise PL.PlanInvalid('native_requirements_invalid')
    for kind in (RQ.ASK, RQ.ACTION, RQ.UNCLEAR):
        if type(block[kind]) is not list:
            raise PL.PlanInvalid('native_requirements_invalid')
        for entry in block[kind]:
            expected = {'id', 'text', 'effect'} if kind == RQ.ACTION else {'id', 'text'}
            if (type(entry) is not dict or set(entry) != expected
                    or type(entry['id']) is not str or not 1 <= len(entry['id']) <= RQ.MAX_ID
                    or type(entry['text']) is not str or not 1 <= len(entry['text']) <= RQ.MAX_TEXT
                    or entry['id'] != entry['id'].strip() or entry['text'] != entry['text'].strip()):
                raise PL.PlanInvalid('native_requirements_invalid')
    if block['belege'] != {'mindestens': 0}:
        raise PL.PlanInvalid('native_requirements_invalid')
    try:
        return RQ.validate(block, objective=objective)
    except ValueError as exc:
        raise PL.PlanInvalid('native_requirements_invalid') from exc


# -- Start choice (§2.6): read once, persisted on the run ---------------------

def _persist_worker_choice(ledger, run, provider):
    """Write `choices["worker"]` once; a later setting change never moves an open order."""
    choices = PS.selections(run.provider_selection)
    if 'worker' in choices:
        return choices['worker']['provider']
    choices['worker'] = {'provider': provider, 'boundary_ref': PS.reference(run)}
    encoded = json.dumps(choices, sort_keys=True)
    with ledger._open() as db:
        db.execute('BEGIN IMMEDIATE')
        current = db.execute('SELECT provider_selection FROM agent_runs WHERE run_id=?',
                             (run.run_id,)).fetchone()
        if current is None or (current['provider_selection'] or '') != (run.provider_selection or ''):
            raise PL.PlanInvalid('native_delegation_not_retained')
        db.execute('UPDATE agent_runs SET provider_selection=?,updated_at=? WHERE run_id=?',
                   (encoded, time.time(), run.run_id))
    return provider


def start_worker(ledger, run):
    """The durable worker of this run: an existing choice, the parent run's
    choice (a follow-up revision stays with its worker), else the setting —
    read exactly once here."""
    chosen = PS.selected(run, 'worker')
    if chosen:
        return chosen
    if run.parent_run_id:
        parent = ledger.get_run(run.parent_run_id)
        inherited = PS.selected(parent, 'worker') if parent else ''
        if inherited:
            return _persist_worker_choice(ledger, run, inherited)
    from solvio.config import load_settings
    setting = str(getattr(load_settings(), 'agent_runtime_task_worker', '') or '')
    return _persist_worker_choice(ledger, run, setting if setting in SP.WORKER_PROFILES else 'codex')


async def prepare_plan(orch, run, context):
    task = TR.task_view(orch.ledger, run.run_id)
    plan = delegation(task, start_worker(orch.ledger, run))
    if any(step.kind == 'specialist' for step in orch.ledger.steps_for_run(run.run_id)):
        raise PL.PlanInvalid('native_delegation_already_started')
    bound = RQ.load(task.requirements, objective=task.objective)
    if bound is None:
        # The first rejected answer is known work, while a quota non-start is
        # not. Retain its repair reason in the existing run journal so that an
        # explicit resume (also after restart) uses the second place, not a new
        # first call. The pre-await counter still blocks any uncertain replay.
        hint = next((event.ref[len(REQUIREMENTS_REPAIR_REF):]
            for event in orch.ledger.events_for_run(run.run_id, limit=S.MAX_EVENTS_PER_RUN)
            if event.kind == 'budget_event' and event.ref.startswith(REQUIREMENTS_REPAIR_REF)
            and event.ref[len(REQUIREMENTS_REPAIR_REF):] in REQUIREMENTS_REPAIR_REASONS), '')
        if task.requirements or run.planner_calls != (1 if hint else 0):
            raise PL.PlanInvalid('native_requirements_not_retained')
        planner = orch._planner_for_run(run.run_id)
        if planner is None or not callable(getattr(planner, 'extract_requirements', None)):
            raise PL.PlanInvalid('native_requirements_unavailable')
        orch._ensure_provider_route(run.run_id, 'plan', planner)
        # ONE repair with the Core's reason, as `Planner.plan` grants it: the
        # criteria block is free-form model JSON, and a single slip (a stray key,
        # an unstripped text) ended the order FAILED plan_invalid before any
        # worker turn (measured 19.09.2026, attempt aa, run 2). Each attempt is
        # an intent persisted before its await; nothing here has an effect.
        for attempt in range(2 if run.planner_calls else 1, 3):
            context.ledger.check_planner()
            orch.ledger.set_run_fields(run.run_id, planner_calls=context.ledger.planner_calls + 1)
            with orch._cost_scope(run, 'plan'):
                call = await planner.extract_requirements(objective=task.objective, run_id=run.run_id,
                                                          **({'hint': hint} if hint else {}))
            context.ledger.note_planner_call()
            orch._record_provider_route(run.run_id, call, 'plan')
            if not call.ok:
                if call.dispatch_started is False:
                    context.ledger.planner_calls -= 1
                    orch.ledger.set_run_fields(run.run_id, planner_calls=context.ledger.planner_calls)
                from solvio.agent_runtime.orchestrator import _ProviderPause
                raise _ProviderPause(call, 'plan')
            if not await orch._call_still_allowed(run.run_id, context):
                return
            try:
                bound = requirements(call, task.objective)
                break
            except PL.PlanInvalid as exc:
                log.info('agent_runtime.requirements_rejected', run_id=run.run_id, attempt=attempt,
                         reason=exc.reason)
                if attempt == 2:
                    raise
                hint = exc.reason
                if hint not in REQUIREMENTS_REPAIR_REASONS:
                    raise
                orch.ledger.record_event(run.run_id, 'budget_event',
                    'Die Anforderungserfassung wird einmal mit dem Prüfgrund korrigiert.',
                    ref=REQUIREMENTS_REPAIR_REF + hint)
        payload = json.dumps(bound, ensure_ascii=False, sort_keys=True)
        if TR.revision_for_run(orch.ledger, run.run_id)['revision'] > 1:
            written = TR.bind_requirements(orch.ledger, run.run_id, payload)
        else:
            written = orch.ledger.bind_requirements(task.task_id, payload)
        if not written or TR.task_view(orch.ledger, run.run_id).requirements != payload:
            raise PL.PlanInvalid('native_requirements_not_retained')
        orch.ledger.set_run_fields(run.run_id, planner_calls=context.ledger.planner_calls,
                                  tokens_planner=call.tokens)
    context.plan, context.cursor, context.plan_state = plan, 0, 'fresh'
    if not orch._checkpoint(run.run_id, context):
        raise PL.PlanInvalid('native_delegation_not_retained')
    current = orch.ledger.get_run(run.run_id)
    if current.state in {S.PLANNING, S.INTERRUPTED}:
        orch.ledger.transition(run.run_id, S.RUNNING)
    orch._clear_provider_resume(run.run_id, 'plan')
    orch.ledger.record_event(run.run_id, 'state_changed',
        'Der vollständige Auftrag wird einmal an den nativen Agenten übergeben.')


# -- Workspace and jail roots (E1) --------------------------------------------

def _socket_root(ledger):
    from solvio.agent_runtime.native_tool_bridge import socket_root
    return socket_root(ledger.path)


def _private_root(value, name):
    """Create the configured root privately (0o700 on every created level)
    and return it canonical; never a relative, symlinked or shared path."""
    text = str(value or '').strip()
    if not text:
        raise ValueError(name + '_unset')
    root = Path(os.path.expanduser(text))
    if not root.is_absolute() or root == Path('/'):
        raise ValueError(name + '_invalid')
    # An isolated world (own SOLVIO_STATE_DIR: test worlds, the isolated live
    # proof) never lands in the owner's home roots. Loud, not silent. Isolated
    # means DIFFERENT from the installation's default — a variable that merely
    # spells out `~/.solvio` is production, not a test world (review round 7,
    # A7-H2: the mere presence of the variable failed every native order).
    configured = os.environ.get('SOLVIO_STATE_DIR', '').strip()
    isolated = bool(configured) and Path(os.path.expanduser(configured)).resolve() != Path(os.path.expanduser('~/.solvio')).resolve()
    if isolated and root.is_relative_to(Path(os.path.expanduser('~/.solvio-tasks'))):
        raise ValueError(name + '_not_isolated')
    missing = []
    probe = root
    while not probe.exists():
        missing.append(probe)
        probe = probe.parent
    for folder in reversed(missing):
        # Two Core paths may create the same root at once; the privacy check
        # below is what matters, not who created it.
        folder.mkdir(mode=0o700, exist_ok=True)
    return N._workspace(str(root.resolve()))


def workspace_root():
    from solvio.config import load_settings
    return _private_root(load_settings().agent_runtime_task_workspace_root, 'native_workspace_root')


def jail_root():
    from solvio.config import load_settings
    return _private_root(load_settings().agent_runtime_claude_jail_root, 'native_jail_root')


def _workspace(ledger, task_id):
    """`<root>/<task_id>` — the same private folder for both providers."""
    root = Path(workspace_root())
    folder = root / task_id
    folder.mkdir(mode=0o700, exist_ok=True)
    return N._workspace(str(folder))


def _jail(task_id, workspace):
    """`<jail root>/<task_id>` beside — never inside — the workspace."""
    root = Path(jail_root())
    folder = root / task_id
    folder.mkdir(mode=0o700, exist_ok=True)
    jail = N._workspace(str(folder))
    if Path(jail).is_relative_to(Path(workspace)) or Path(workspace).is_relative_to(Path(jail)):
        raise ValueError('native_jail_location')
    (Path(jail) / 'sock').mkdir(mode=0o700, exist_ok=True)
    return jail


def _redact_observation_blocks(receipts):
    """Copies of the worker's receipts with credential-bearing lines replaced
    (block redacted=true); the caller's tuple stays untouched."""
    out = []
    for receipt in receipts:
        item = dict(receipt) if type(receipt) is dict else receipt
        if type(item) is dict:
            for key in ('command', 'output', 'query'):
                block = item.get(key)
                if type(block) is dict and type(block.get('text')) is str and block['text']:
                    text, changed = redact_lines_if_credential(block['text'], where='native_task_observation.' + key)
                    if changed:
                        item[key] = dict(block, text=text, redacted=True)
        out.append(item)
    return out


def _retain_observation(sessions, session_id, run_id, step_id, outcome, cancel, helpers=None):
    from solvio.agent_runtime import artifact_creation as A, native_result_files as RF
    from solvio.agent_runtime.native_observations import capture_core_receipts
    def current():
        return RF._context(sessions, session_id, run_id, step_id, outcome.cost_invocation_id,
            outcome.native_thread_id, outcome.native_turn_id, (), cancel)
    binding = current()
    if (type(outcome.native_tool_receipts) is not tuple or len(outcome.native_tool_receipts) > 100
            or any(type(item) is not dict for item in outcome.native_tool_receipts)):
        raise ValueError('native_task_receipt_invalid')
    body = {**binding, 'kind': 'native_task_observation',
        'receipts': list(outcome.native_tool_receipts),
        'core_tools': capture_core_receipts(sessions.ledger, binding),
        'attestation': 'Native tool observations under this completed turn and settled cost; '
                       'not proof of external action success.'}
    if helpers is not None:
        # §3.4 Nachlauf: the Core's own seeding record and post-turn digests,
        # never a model statement about helpers.
        body['helpers'] = {'seeded': list(helpers['seeded']), 'removed': list(helpers['removed'])}
    # Observations are a Verlauf, not an Aussage. Two fences, in this order:
    # (1) a key-SHAPED value (sk-…, JWT, PEM header, bearer) anywhere in the RAW
    # receipts refuses the whole record — before any redaction, because a line
    # pass removes exactly the signature the refusal needs while a PEM body or a
    # value split across receipts stays (review round 10, B10-1; ADR-0029:
    # refuse, never sanitize); (2) a line that merely STATES a credential
    # ("DB_PASSWORD=…", "Passwort:" + next line) becomes the marker, the block
    # says redacted=true, the record stays. The former whole-JSON statement
    # heuristic refused on the WORD "keys" in the Python docs a worker had read —
    # the finished job vanished as native_result_not_retained (19.09.2026).
    # On the RAW field texts as well as on the JSON: `Bearer\n<token>` is a key shape
    # in raw text (a newline is whitespace) but not in its JSON form `\\n`, and the
    # read side checks raw text (review round 11, H11-2 — same fence on both sides).
    raw_texts = '\n'.join(block['text'] for receipt in body['receipts'] if type(receipt) is dict
                          for key in ('command', 'output', 'query') for block in (receipt.get(key),)
                          if type(block) is dict and type(block.get('text')) is str)
    refuse_if_key_shaped(raw_texts, where='native_task_observation')
    refuse_if_key_shaped(A._json(body).decode(), where='native_task_observation')
    body['receipts'] = _redact_observation_blocks(body['receipts'])
    raw = A._json(body)
    A._record(sessions.ledger, run_id, step_id, 'native_task_observation',
              'native-turn-' + step_id + '.json', raw)
    if current() != binding:
        raise ValueError('native_task_observation_binding_changed')


# -- Helpers (§3.4/§3.3): seeding before the turn, readback after, publication after SUCCEEDED

def _seed_helpers(ledger, task, workspace):
    """Seed the owner's validated helpers into the workspace (0o400 copies +
    HELPERS.json), removing directories that are not in the current version
    list. Returns the Core-held seeding record for the post-turn readback."""
    from solvio.agent_runtime import extension_versions as EV, helper_seeding as HS
    versions = EV.helper_versions(ledger).helpers_for(task.created_principal) if task.created_principal else ()
    return HS.seed_helpers(workspace, versions)


def _helper_record(workspace, seeding):
    """§3.4 Nachlauf: recompute the seeded digests after the turn."""
    from solvio.agent_runtime import helper_seeding as HS
    return {'seeded': HS.verify_seeded(workspace, seeding['seeded']),
            'removed': list(seeding['removed'])[:HELPERS_REMOVED_MAX]}


async def after_success(orch, run_id):
    """§3.3: publish the run's helper candidates once the run is SUCCEEDED.

    Publication needs the SUCCEEDED origin and the Core readback receipt again
    (`ExtensionVersions.publish_helper`); refusals are recorded as events, never
    raised into the finished run. A run that did not end SUCCEEDED publishes
    nothing.
    """
    from solvio.agent_runtime import extension_versions as EV
    run = orch.ledger.get_run(run_id)
    if run is None or run.state != S.SUCCEEDED:
        return []
    task = orch.ledger.get_task(run.task_id)
    if task is None or task.scope != S.SCOPE_TASK:
        return []
    try:
        return await asyncio.to_thread(EV.helper_versions(orch.ledger).publish_helpers, run_id)
    except (ValueError, OSError):
        return []


# -- Reopened step after a proven non-start (§2.4/§2.6) -----------------------

def _continue_after_nonstart(sessions, scope, step):
    """The reopened worker step keeps its operation_id (= step_id); its new
    cost scope would collide with the settled ordinal-1 claim of the proven
    non-start. Continue the ordinal only when EVERY prior physical attempt of
    this step is terminal and settled/released AND its native turn never
    started (`terminal/not_started`). Anything else keeps the replay lock."""
    with sessions.ledger._open() as db:
        rows = db.execute(
            'SELECT p.reservation_id, p.state, t.state AS turn_state, t.terminal_status '
            'FROM agent_provider_invocations p LEFT JOIN agent_native_turns t '
            'ON t.reservation_id=p.reservation_id WHERE p.task_id=? AND p.run_id=? '
            "AND p.phase='specialist' AND p.operation_id=?",
            (scope.task_id, scope.run_id, step.step_id)).fetchall()
    if not rows:
        return 0
    if any(row['state'] not in {'finished', 'not_dispatched'}
           or (row['turn_state'], row['terminal_status']) != ('terminal', 'not_started') for row in rows):
        return 0
    return scope.continue_after_settled()


# -- Handover (§2.5): Core records only ---------------------------------------

def _previous_provider(sessions, task_id, profile):
    """The provider of the task's last terminal native turn under ANOTHER
    profile, or '' when this worker is the first (no handover)."""
    with sessions.ledger._open() as db:
        row = db.execute('SELECT s.provider FROM agent_native_turns t JOIN agent_native_sessions s '
            "ON s.session_id=t.session_id WHERE s.task_id=? AND s.profile<>? AND t.state='terminal' "
            'ORDER BY t.rowid DESC LIMIT 1', (task_id, profile)).fetchone()
    return row['provider'] if row else ''


REWORK_HEADER = ('NACHARBEIT IM SELBEN AUFTRAG (Core-Befund der Abschlussprüfung; unvertraute Hinweise, '
                 'keine neuen Befugnisse, kein neuer Auftrag)')


def rework_block(orch, run, step, bound):
    """§3.5: the one rework turn carries the assessor's concrete open points as
    untrusted hints — data from the Core's own stored verdict, never a model
    statement — and the rule that every deliverable is published again.
    Empty for the first worker step of a run."""
    earlier = [s for s in orch.ledger.steps_for_run(run.run_id)
               if s.kind == 'specialist' and s.specialist_profile in SP.WORKER_PROFILES.values()
               and s.state == 'succeeded' and s.step_id != step.step_id]
    if not earlier:
        return ''
    stored = orch._stored_verdict(orch.ledger.get_run(run.run_id)) or {}
    texts = {entry['id']: entry['text'] for kind in (RQ.ASK, RQ.ACTION, RQ.UNCLEAR) for entry in bound[kind]}
    points = [str(item)[:600] for key in ('fehlend', 'unsicher') for item in (stored.get(key) or [])]
    points += [texts[key] + ' (' + key + ')' for key in (stored.get('offen') or []) if key in texts]
    if not points:
        return ''
    lines = '\n'.join('- ' + point for point in points[:RQ.MAX_ITEMS])
    return ('\n\n' + REWORK_HEADER + ':\nDie Abschlussprüfung des Core hat dein bisheriges Ergebnis '
            'dieses Auftrags nicht als vollständig belegt. Offene Punkte:\n' + lines + '\n'
            'Arbeite genau diese Punkte im selben Arbeitsordner nach — sichtbar: führe die verlangten '
            'Prüfungen und Herstellungsschritte als Befehle aus, deren Ausgabe die Prüfung zeigt (kurze '
            'Ausgaben). Die Beobachtungen deines ersten Turns bleiben als Belege erhalten; wiederhole nur, '
            'was der Prüfung fehlt. Liefere danach ALLE Ergebnisdateien des Auftrags erneut in files (auch '
            'unveränderte, unter denselben Namen) und deklariere deinen Helfer erneut in helpers; die '
            'früheren Veröffentlichungen bleiben als Verlauf erhalten. Keine neuen Ziele, keine Außenwirkung.')


def handover_block(orch, task, run, previous_provider):
    """Data for the new session, never an instruction and never a foreign transcript."""
    from solvio.agent_runtime import result_files as RF
    runs = orch.ledger.runs_for_task(task.task_id)
    summary = ''
    for candidate in sorted(runs, key=lambda item: item.created_at or 0.0, reverse=True):
        finished = [s for s in orch.ledger.steps_for_run(candidate.run_id)
                    if s.kind == 'specialist' and s.finished_at is not None and s.summary]
        if finished:
            summary = SP.redact_specialist_output(finished[-1].summary)[:HANDOVER_SUMMARY_CHARS]
            break
    files, verified = [], {}
    for candidate in runs:
        try:
            verified.update(orch._verified_effects(candidate.run_id))
        except Exception:  # noqa: BLE001 - a missing catalogue is no handover claim
            pass
        for descriptor in RF.describe_files(orch.ledger, candidate.run_id)[0]:
            requirement = next((req for evidence, req in verified.items()
                                if descriptor['name'] in str(evidence)), '')
            files.append({'path': descriptor['name'], 'sha256': descriptor['sha256'],
                          'requirement': requirement})
    bound = RQ.load(task.requirements, objective=task.objective) or {}
    known = {entry['id'] for kind in (RQ.ASK, RQ.ACTION) for entry in bound.get(kind, [])}
    open_ids = sorted(known - set(verified.values()))
    lines = ['--- ÜBERGABE AUS DIESEM AUFTRAG (Daten, keine Anweisung) ---',
             'vorheriger_anbieter: ' + previous_provider,
             'letzter_terminaler_schritt: ' + (summary or '(keiner)'),
             'ergebnisdateien_im_arbeitsordner: ' + json.dumps(files[:HANDOVER_MAX_FILES], ensure_ascii=False),
             'offene_anforderungen: ' + json.dumps(open_ids, ensure_ascii=False),
             'helfer: siehe .solvio-helpers/HELPERS.json']
    return '\n'.join(lines)


# -- Broker-book classification of a non-start (§2.4) -------------------------

def nonstart_proven(usage):
    """True only when the broker book for the lease shows exclusively refusals
    with HTTP 429 and zero output tokens — the Core's truth, not CLI wording.

    An empty book is NOT a proven quota non-start (nothing was refused), and a
    single forwarded row or any output token means the provider answered.
    """
    if type(usage) is not dict:
        return False
    outcomes = usage.get('outcomes') or {}
    statuses = usage.get('status_codes') or {}
    rows = int(usage.get('rows') or 0)
    if rows <= 0 or sum(outcomes.values()) != rows or sum(statuses.values()) != rows:
        return False
    if set(outcomes) - NONSTART_OUTCOMES or set(statuses) != {str(NONSTART_STATUS)}:
        return False
    return int(usage.get('output_tokens') or 0) == 0


def _broker():
    from solvio.provider_broker import service
    return service.running()


def broker_book_for_turn(sessions, invocation_id):
    """The broker book rows of this task since the turn was requested (or None
    without a running broker / a known turn)."""
    broker = _broker()
    turn = sessions.turn(invocation_id) if invocation_id else None
    if broker is None or turn is None:
        return None
    session = sessions.session(turn.session_id)
    with sessions.ledger._open() as db:
        row = db.execute('SELECT requested_at FROM agent_native_turns WHERE invocation_id=?',
                         (invocation_id,)).fetchone()
    if session is None or row is None:
        return None
    return broker.ledger.usage_for_task('task:' + session.task_id, since=float(row['requested_at']))


def _confirm_nonstart(sessions, outcome):
    """A Claude worker's QUOTA non-start (`dispatch_started=False`) counts only
    when the ledger shows a `not_started` turn AND the broker book proves the
    refusal; otherwise the outcome is treated as a started turn (no switch
    offer). Refusals by gates before any claim (cost gate, missing broker)
    keep their own `dispatch_started`, as for Codex.
    """
    quota = bool(outcome.quota) or str(getattr(outcome.result, 'reason', '') or '') == 'quota'
    if outcome.dispatch_started is not False or not quota:
        return outcome
    turn = sessions.turn(outcome.cost_invocation_id) if outcome.cost_invocation_id else None
    proven = (turn is not None and turn.state == 'terminal' and turn.terminal_status == 'not_started'
              and nonstart_proven(broker_book_for_turn(sessions, outcome.cost_invocation_id)))
    if proven:
        return outcome
    return replace(outcome, dispatch_started=True)


async def _session_mode(module):
    """`resume` only when A's module measured M-C3 green; default handover.

    The measurement is a physical probe with its own event loop (cached per
    process by the module), so it runs on a thread, never on this loop.
    """
    try:
        mode = str(await asyncio.to_thread(module.session_mode))
    except (AttributeError, TypeError, ValueError, RuntimeError):
        return 'handover'
    return mode if mode in {'resume', 'handover'} else 'handover'


async def _mcp_mode():
    return await asyncio.to_thread(PS.claude_mcp_mode)


async def execute(orch, run, step, request, on_event):
    from solvio.agent_runtime.native_tools import NativeCoreTools
    from solvio.agent_runtime.native_tool_bridge import NativeToolBridge
    from solvio.agent_runtime import native_result_files as RF
    from solvio.specialists import native_task as NT, hermes_native as H
    task = TR.task_view(orch.ledger, run.run_id)
    scope = D.current_scope()
    profile = step.specialist_profile
    if (task.scope != S.SCOPE_TASK or profile not in SP.WORKER_PROFILES.values()
            or request.profile != profile or request.objective != task.objective
            or type(scope) is not D.TaskCostScope or scope.task_id != task.task_id
            or scope.run_id != run.run_id or scope.phase != 'specialist'
            or scope.operation_id != step.step_id or orch.ledger.get_run(run.run_id).state != S.RUNNING):
        raise ValueError('native_task_execution_binding_invalid')
    bound = RQ.load(task.requirements, objective=task.objective)
    if bound is None:
        raise ValueError('native_requirements_unbound')
    provider = SP.profile(profile).provider
    sessions = N.NativeSessions(orch.ledger, authority=orch.task_authority)
    workspace = _workspace(orch.ledger, task.task_id)
    context = orch._contexts[run.run_id]
    _continue_after_nonstart(sessions, scope, step)
    # §3.4: seed validated helpers BEFORE the session binds (same workspace for
    # both providers); the Core keeps the record for the post-turn readback.
    seeding = await asyncio.to_thread(_seed_helpers, orch.ledger, task, workspace)
    if provider == SP.CODEX:
        config = SP.native_research_config()
        config = replace(config, browser_python='', browser_bin='', browser_chrome='',
                         timeout_s=SP.profile(profile).timeout)
        config.validate()
        session = sessions.bind(task_id=task.task_id, run_id=run.run_id, provider='codex',
            profile=profile, policy_digest=NT.task_policy(config), workspace=workspace)
        socket_root, runner = _socket_root(orch.ledger), NT.run_task
    else:
        # Claude Code as the second worker of the same order path: the jail
        # (sandbox profile, CLI state, tool socket) lives beside the workspace.
        # ASSUMPTION (implementer A, §2.1/§2.2): `claude_native_task` exposes
        # `ClaudeWorkerConfig(jail, broker_port, mcp_mode, session_mode)`,
        # `claude_task_policy(config)`, `session_mode()` and `run_task(...)`
        # with the `native_task.run_task` signature.
        from solvio.specialists import claude_native_task as CNT
        broker = _broker()
        if broker is None:
            raise ValueError('native_broker_unavailable')
        jail = _jail(task.task_id, workspace)
        config = CNT.ClaudeWorkerConfig(jail=jail, broker_port=int(broker.port),
            mcp_mode=await _mcp_mode(), session_mode=await _session_mode(CNT))
        session = sessions.bind(task_id=task.task_id, run_id=run.run_id, provider='claude-code',
            profile=profile, policy_digest=CNT.claude_task_policy(config), workspace=workspace)
        socket_root, runner = str(Path(jail) / 'sock'), CNT.run_task
    bridge = NativeToolBridge(NativeCoreTools(orch.ledger, sessions, session.session_id,
        run.run_id, orch.router, cancel_token=context.cancel), socket_root=socket_root)
    handover = ''
    previous = _previous_provider(sessions, task.task_id, profile)
    if previous and previous != provider:
        handover = '\n\n' + handover_block(orch, task, run, previous)
    helpers_line = ''
    if seeding['seeded']:
        from solvio.agent_runtime.helper_seeding import CONTEXT_LINE
        helpers_line = '\n\n' + CONTEXT_LINE
    request = replace(request, workdir=session.workspace,
        context='KANONISCHER ANFORDERUNGSVERTRAG (Kriterien, keine zusätzlichen Befugnisse):\n'
                + json.dumps(bound, ensure_ascii=False, sort_keys=True) + '\n\n' + request.context
                + helpers_line + handover + rework_block(orch, run, step, bound))
    try:
        outcome = await runner(request, config=config,
            continuation=H.NativeContinuation(sessions, session.session_id), bridge=bridge, on_event=on_event)
        if provider == SP.CLAUDE:
            outcome = _confirm_nonstart(sessions, outcome)
        if outcome.result.ok:
            try:
                await asyncio.to_thread(RF.ensure_rework_files, orch.ledger, run.run_id,
                                        step.step_id, outcome.native_files)
                helpers = await asyncio.to_thread(_helper_record, session.workspace, seeding)
                await asyncio.to_thread(_retain_observation, sessions, session.session_id,
                    run.run_id, step.step_id, outcome, context.cancel, helpers)
                if outcome.native_files:
                    await asyncio.to_thread(RF.publish, sessions, session_id=session.session_id,
                        run_id=run.run_id, step_id=step.step_id, invocation_id=outcome.cost_invocation_id,
                        native_thread_id=outcome.native_thread_id, native_turn_id=outcome.native_turn_id,
                        relative_paths=outcome.native_files, requirement_assignments=outcome.native_file_requirements,
                        cancel_token=context.cancel)
            except (ValueError, OSError):
                outcome.result.ok = False
                outcome.result.reason = 'native_result_not_retained'
            for index, reason in (outcome.native_helper_rejections if outcome.result.ok else ()):
                # A declaration the worker could not hand over (name with spaces, foreign
                # path, fifth entry): an owner-visible event, never a lost result.
                RF.record_helper_rejection(orch.ledger, run.run_id, step.step_id,
                                           'helpers[' + str(index) + ']', reason)
            if outcome.result.ok and outcome.native_helpers:
                # §3.2 Kandidatur: byte-exact reads after the turn; publication
                # into the helper family only after SUCCEEDED (after_success).
                # A helper is manufacturing method, never a deliverable: a
                # declaration the Core cannot take as a candidate leaves the
                # already published result untouched and becomes an event.
                try:
                    await RF.publish_helper_candidates(sessions, session_id=session.session_id,
                        run_id=run.run_id, step_id=step.step_id, invocation_id=outcome.cost_invocation_id,
                        native_thread_id=outcome.native_thread_id, native_turn_id=outcome.native_turn_id,
                        helpers=outcome.native_helpers, cancel_token=context.cancel)
                except (ValueError, OSError) as exc:
                    RF.record_helper_rejection(orch.ledger, run.run_id, step.step_id, '*',
                        str(exc) if type(exc) is ValueError else 'helper_candidates_unreadable')
        return outcome
    finally:
        await bridge.close()
