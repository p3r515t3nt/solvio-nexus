"""Resolve a spoken/typed reply reminder through existing Gmail and scheduler tools."""
from datetime import datetime, timedelta
import re
import time
from zoneinfo import ZoneInfo

from solvio.capabilities.envelope import CapabilityOutcome
from solvio.tools.base import ToolResult

SCHEMA = {
    'description': 'Erinnert an eine ausstehende Antwort auf eine bereits gesendete Gmail-Mail. '
        'Verwende die gerade besprochene Mail aus dem Gespräch für query; keine Mailkennung erfinden. '
        'Bei unklarer Mail oder fehlender Frist nachfragen. wann enthält den gewünschten einmaligen '
        'Zeitpunkt, z.B. Freitag um 17 Uhr, morgen 09:00 oder in zwei Stunden. '
        'Der Core prüft die eindeutige gesendete Mail und fragt einmal nach Face ID. '
        'Es wird nichts versendet; kein Einrichtungsmenü nötig.',
    'parameters': {'type': 'object', 'properties': {
        'query': {'type': 'string', 'minLength': 2, 'maxLength': 200,
                  'description': 'Konkrete Gmail-Suche aus der vom Nutzer gemeinten Mail, z.B. to:person@example.test subject:Angebot.'},
        'wann': {'type': 'string', 'minLength': 1, 'maxLength': 120,
                 'description': 'Vom Nutzer gewünschte einmalige Frist. Keine Uhrzeit oder Frist erfinden.'}},
        'required': ['query', 'wann'], 'additionalProperties': False}}


def deadline(text, *, now=None):
    """Freeze one occurrence before approval; a weekday never becomes recurring."""
    from solvio.capabilities.proactive import parse_when
    from solvio.proactive.schedule import Kind
    now = time.time() if now is None else now
    low = text.strip().lower()
    zone = ZoneInfo('Europe/Berlin')
    try:
        plan, _ = parse_when(low, now=now)
    except ValueError:
        return None
    if plan is not None and plan.kind is Kind.ONE_SHOT:
        value = datetime.fromtimestamp(plan.at_epoch, zone)
    else:
        match = re.fullmatch(r'(?:am |bis |bis zum )?(heute|morgen|übermorgen|uebermorgen|montag|dienstag|mittwoch|donnerstag|freitag|samstag|sonntag)\s+(?:um\s+)?(\d{1,2})(?::(\d{2}))?\s*(?:uhr)?', low)
        if not match:
            return None
        day, hour, minute = match.groups()
        local = datetime.fromtimestamp(now, zone)
        days = ['montag', 'dienstag', 'mittwoch', 'donnerstag', 'freitag', 'samstag', 'sonntag']
        offset = (days.index(day) - local.weekday()) % 7 if day in days else {'heute': 0, 'morgen': 1, 'übermorgen': 2, 'uebermorgen': 2}[day]
        try:
            value = (local + timedelta(days=offset)).replace(hour=int(hour), minute=int(minute or 0), second=0, microsecond=0)
        except ValueError:
            return None
    # The existing parser validates DST gaps and future dates. A relative
    # deadline is fixed now, never recomputed after Face ID.
    absolute = value.strftime('%Y-%m-%d %H:%M')
    checked, _ = parse_when(absolute, now=now)
    return absolute if checked is not None else None


async def prepare(tool, context, args):
    from solvio.tools.gmail_capability_tools import _quoted, _speak
    if set(args) != {'query', 'wann'} or any(not isinstance(v, str) for v in args.values()):
        return ToolResult(False, error='invalid_followup', human_message='Welche gesendete Mail soll ich bis wann auf eine Antwort prüfen?')
    query = args['query'].strip()
    when = deadline(args['wann'])
    if not 2 <= len(query) <= 200:
        return ToolResult(False, error='followup_needs_details', human_message='Welche gesendete Mail meinst du – an wen ging sie oder wie lautet ihr Betreff?')
    if when is None:
        return ToolResult(False, error='followup_needs_details', human_message='An welchem Tag und zu welcher Uhrzeit soll ich nach einer Antwort sehen?')
    if tool.ledger is None:
        return ToolResult(False, error='continuation_unavailable', human_message='Die Erinnerung konnte noch nicht sicher vorgemerkt werden. Es ist keine Prüfung eingerichtet.')
    found = await tool._call(context, 'gmail_search', {'query': 'in:sent -in:drafts (' + query + ')', 'limit': 2})
    if not found.succeeded:
        return _speak(found)
    rows = (found.data or {}).get('messages')
    if not isinstance(rows, list) or not rows:
        return ToolResult(False, error='no_matching_sent_mail', human_message='Ich finde dazu keine gesendete Mail. An wen ging sie oder wie lautet ihr Betreff?')
    if len(rows) != 1 or (found.data or {}).get('complete') is not True:
        return ToolResult(False, error='ambiguous_sent_mail', human_message='Dazu finde ich mehrere gesendete Mails. Welche meinst du – zum Beispiel mit welchem Betreff oder von welchem Tag?')
    original = rows[0]
    if (not isinstance(original, dict) or original.get('sent') is not True or original.get('draft') is not False
            or any(not isinstance(original.get(k), str) or not re.fullmatch('[A-Za-z0-9_-]{1,128}', original[k]) for k in ('id', 'thread_id'))):
        return ToolResult(False, error='sent_mail_unconfirmed', human_message='Die gesendete Originalmail konnte ich noch nicht eindeutig bestätigen. Es ist keine Erinnerung eingerichtet.')
    schedule = {'titel': 'Antwort prüfen: ' + _quoted(original.get('subject'), 70),
                'wann': when, 'aktion': 'mail_antwort_pruefen',
                'argumente': {'thread_id': original['thread_id'], 'message_id': original['id']}}
    result = await tool._call(context, 'background_create', schedule)
    from solvio.tools.agent_capability_tools import remember_pending_start
    remember_pending_start(tool.ledger, capability='background_create', result=result, arguments=schedule, context=context)
    if result.outcome is CapabilityOutcome.APPROVAL_REQUIRED:
        request_id = (result.data or {}).get('request_id')
        retained = tool.ledger.get_pending_start(request_id)
        if (not retained or retained['arguments'] != schedule or retained['principal'] != context.principal):
            await tool.router._abandon(request_id, 'continuation_unavailable')
            return ToolResult(False, error='continuation_unavailable', human_message='Die Erinnerung konnte nicht sicher vorgemerkt werden. Es ist keine Prüfung eingerichtet.')
        return ToolResult(False, error='approval_required', data=result.data,
            human_message=f'Ich prüfe am {when} (deutsche Zeit), ob auf die Mail „{_quoted(original.get("subject"), 70)}“ im selben Verlauf eine Antwort eingegangen ist. Bitte bestätige diese Erinnerung einmal mit Face ID unter Freigaben. Noch ist sie nicht eingerichtet; es wird keine Mail versendet.')
    return _speak(result)
