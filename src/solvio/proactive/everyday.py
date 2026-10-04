"""Bound daily overview and a one-time reply check on the existing scheduler.

Uses existing read capabilities and inbox. No new model, scheduler or send path.
"""
from __future__ import annotations
import hashlib
import re
from solvio.capabilities.contract import CapabilityDeclined
from solvio.capabilities.task_read import task_material
from solvio.proactive.schedule import Kind

ACTIONS = ('tagesueberblick', 'mail_antwort_pruefen')


def validate(action, arguments, plan):
    if arguments.get('bedingung') or arguments.get('thema') or arguments.get('melden') not in (None, '', 'immer'):
        raise CapabilityDeclined('unsupported_condition', 'Dieser Ablauf hat einen festen Umfang und meldet sein Ergebnis ohne weitere Filter.')
    args = arguments.get('argumente') or {}
    if not isinstance(args, dict):
        raise CapabilityDeclined('invalid_arguments', 'Die Angaben zur Aufgabe sind unvollständig.')
    if action == 'tagesueberblick':
        if args or plan.kind not in (Kind.DAILY, Kind.WEEKLY):
            raise CapabilityDeclined('daily_schedule_required', 'Nenne eine tägliche oder wöchentliche Uhrzeit für den Überblick.')
    elif action == 'mail_antwort_pruefen':
        if (set(args) != {'thread_id', 'message_id'} or plan.kind != Kind.ONE_SHOT
            or any(not isinstance(v, str) or not re.fullmatch('[A-Za-z0-9_-]{1,128}', v) for v in args.values())):
            raise CapabilityDeclined('mail_reference_required', 'Wähle die gesendete Mail und einen einmaligen Prüfzeitpunkt aus.')
    return {'kind': action, 'arguments': dict(args), 'notify': 'immer'}


async def execute(runner, task, action, run_id):
    from solvio.proactive.runner import RunOutcome, background_trust, ALWAYS
    from solvio.proactive import store as S
    from solvio.capabilities.contract import ArgumentSource
    from solvio.capabilities.policy import OriginClass
    async def still_authorized():
        if runner.store is None or not await runner.store.is_running_version(task):
            raise PermissionError('schedule_changed_or_removed')

    async def read(name, args):
        await still_authorized()
        result = await runner.dispatcher.capabilities.execute(name, args,
            trust=background_trust(task.created_at),
            provenance={key: ArgumentSource.TRUSTED_CONTEXT for key in args},
            principal=task.owner, origin=OriginClass.BACKGROUND_AUTOMATION,
            automation_id=task.task_id)
        if not result.succeeded or not isinstance(result.data, dict):
            raise CapabilityDeclined('read_unavailable', 'Die benötigte Abfrage ist nicht bestätigt; bitte Verbindung und Berechtigung prüfen.')
        return result.data
    try:
        await still_authorized()
        if action['kind'] == 'mail_antwort_pruefen':
            args = action['arguments']
            data = await read('gmail_read_thread', {'thread_id': args['thread_id']})
            messages = data.get('messages')
            if not isinstance(messages, list):
                raise ValueError('incomplete_thread')
            original = next((m for m in messages if m.get('id') == args['message_id']), None)
            if (not original or original.get('sent') is not True
                    or not isinstance(original.get('received_at_ms'), int) or original['received_at_ms'] <= 0):
                raise ValueError('original_not_verified')
            if any(not isinstance(m.get('received_at_ms'), int) or m['received_at_ms'] <= 0
                   or not isinstance(m.get('sent'), bool) or not isinstance(m.get('draft'), bool) for m in messages):
                raise ValueError('thread_metadata_incomplete')
            replies = [m for m in messages if m['received_at_ms'] > original['received_at_ms']
                       and not m['sent'] and not m['draft']]
            if replies:
                return RunOutcome(S.NO_CHANGE, outcome='reply_observed',
                    detail='Spätere eingegangene Nachricht im beobachteten Verlauf gefunden; kein Nachfasshinweis und kein Versand.')
            else:
                summary = 'Bis zur Prüfung ist im beobachteten Mailverlauf keine spätere eingegangene Nachricht vorhanden. Eine Antwort in einem anderen Verlauf ist damit nicht ausgeschlossen. Es wurde nichts versendet.'
            result = {'zusammenfassung': summary, 'gepruefter_verlauf': args['thread_id']}
        else:
            lines = []; incomplete = False
            for name, args, key, label in (
                ('calendar_list_events', {'when': 'heute'}, 'events', 'Termine heute'),
                ('gmail_list_recent', {'only_unread': True, 'limit': 10}, 'messages', 'Neueste ungelesene Mails (höchstens 10)')):
                try:
                    data = task_material(await read(name, args))
                    rows = data.get(key)
                    if not isinstance(rows, list): raise ValueError('incomplete_list')
                    lines.append(f'{label}: {len(rows)}.')
                    for row in rows[:5]:
                        if isinstance(row, dict):
                            title = row.get('subject') or row.get('title') or row.get('summary')
                            when = row.get('start') or row.get('date') or ''
                            if title: lines.append(str(when)[:40] + ' — ' + str(title)[:160])
                except PermissionError:
                    raise
                except Exception:
                    incomplete = True; lines.append(label + ': Abfrage nicht bestätigt.')
            await still_authorized()
            runtime = getattr(runner.dispatcher, 'agent_runtime', None)
            ledger = getattr(runtime, 'ledger', None)
            if ledger is not None:
                from solvio.agent_runtime.task_overview import overview_for_owner
                overview = overview_for_owner(ledger, task.owner)
                lines.append(f"Offene SOLVIO-Aufträge: {overview['offen_gesamt']}.")
                for row in overview['auftraege'][:5]:
                    lines.append(str(row['bedeutung']) + ': ' + str(row['anliegen'])[:160])
            else:
                incomplete = True; lines.append('Offene SOLVIO-Aufträge: Abfrage nicht bestätigt.')
            result = {'zusammenfassung': 'Dein Tagesüberblick' + (' (unvollständig).' if incomplete else '.'),
                      'ueberblick': lines,
                      'grenze': 'Momentaufnahme; Mailauswahl ist begrenzt. Einzelne Mailfreigaben sind nicht mitgezählt.'}
        await still_authorized()
        # Deliberately unique per occurrence: same quiet day still gets its daily report.
        judged = runner._judge(task, {**action, 'notify': ALWAYS}, run_id,
            task_material(result), source=action['kind'])
        if judged.item is not None:
            judged.item['fingerprint'] = hashlib.sha256(run_id.encode()).hexdigest()
            judged.item['content_trust'] = 'untrusted_email'
            if action['kind'] == 'tagesueberblick':
                judged.item['findings'] = [str(line)[:300] for line in task_material(lines)][:20]
        return judged
    except PermissionError:
        return RunOutcome(S.SKIPPED, outcome='schedule_changed_or_removed', detail='Aufgabe geändert, pausiert oder entfernt; keine weiteren Abfragen.')
    except Exception:
        detail = 'Mail-/Auftragsprüfung nicht bestätigt; keine Aussage über ausgebliebene Antworten.'
        notice = runner._judge(task, {**action, 'notify': ALWAYS}, run_id,
            {'zusammenfassung': detail + ' SOLVIO versucht die reine Leseprüfung begrenzt erneut; den Stand findest du unter regelmäßigen Aufgaben.'},
            source=action['kind'])
        return RunOutcome(S.FAILED, outcome='read_unconfirmed', detail=detail, item=notice.item)
