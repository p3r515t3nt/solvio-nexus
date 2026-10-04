"""Literal owner fields for the two initial natural action kinds.

The existing native planner extracts spans. It cannot supply an account,
recipient, date, duration or effect not present in the authenticated text.
Calendar arithmetic reuses the Core's timezone/date helpers.
"""
from __future__ import annotations
from datetime import datetime, time, timezone, timedelta
import re
from solvio.capabilities.calendar import USER_TIMEZONE, resolve_day, plus, CalendarDeclined

KINDS = {'calendar.create', 'gmail.compose_draft', 'clarify'}
FIELDS = {'title', 'when', 'time', 'duration', 'end_time', 'location', 'to'}
QUESTIONS = {
 'instruction': ('Welchen einzelnen Auftrag soll ich erledigen: einen Termin eintragen oder einen Mailentwurf verfassen?', 'text', 'Bitte beschreibe den gewünschten Auftrag.'),
 'title': ('Wie soll der Termin heißen?', 'text', 'Titel des Termins'),
 'when': ('An welchem Tag soll der Termin stattfinden?', 'text', 'morgen oder 2026-09-15'),
 'time': ('Um wie viel Uhr beginnt der Termin?', 'text', '15:30'),
 'duration': ('Wie viele Minuten dauert der Termin?', 'number', '60'),
 'to': ('An welche E-Mail-Adresse soll der Entwurf gerichtet sein?', 'email', 'name@example.org'),
 'account': ('Welches verbundene Konto soll ich für diesen Auftrag verwenden?', 'select', ''),
}


def _literal(text, span):
    if span is None:
        return ''
    if (type(span) is not list or len(span)!=2 or any(type(n) is not int for n in span)
            or not 0<=span[0]<span[1]<=len(text)):
        raise ValueError('intent_span_invalid')
    result=text[span[0]:span[1]]
    if result!=result.strip() or not result:
        raise ValueError('intent_span_invalid')
    return result


def clear_kind(text, kind):
    """Require an owner command; quotations and compound effects need clarity.

    This is a narrow check on the command, not a ban on negation anywhere in
    the mail's requested content. 'Nicht senden' narrows a draft normally.
    """
    s=text.casefold().strip()
    if re.search(r'(?m)^\s*(?:>|von:|from:|weitergeleitet)',s):
        return False
    if re.search(r'["„“»«][^"„“»«]*@[^"„“»«]*["„“»«]',s):
        return False
    # Quoted titles remain available as literal payload, but quoted commands
    # cannot establish the main intent (e.g. an explanation of a sentence).
    command=re.sub(r'["„»][^"“«]*["“«]','',s)
    imperative=re.match(r'^(?:(?:bitte|kannst du(?: bitte)?|könntest du(?: bitte)?)\s+)?'
        r'(?:trage|trag|erstelle|lege|plane|notiere|verfasse|schreibe|entwirf)\b',command)
    infinitive=re.fullmatch(r'(?:bitte\s+)?(?:einen?\s+)?(?:termin|kalendereintrag|mailentwurf|e-mail-entwurf)\b.+'
        r'\b(?:eintragen|anlegen|erstellen|verfassen|schreiben)[.!?]?',command)
    if not imperative and not infinitive:
        return False
    if re.search(r'\b(?:kein(?:e[nmrs]?)?|nicht)\s+(?:(?:einen?|den|diesen)\s+)?'
                 r'(?:termin|kalender(?:eintrag)?|mail(?:entwurf)?|e-mail(?:-entwurf)?|entwurf|anlegen|eintragen|verfassen|erstellen)\b',command):
        return False
    if re.search(r'\b(?:und|sowie|außerdem|zusätzlich|danach)\s+(?:(?:noch|bitte|auch|einen?|den|die|das)\s+)*'
                 r'(?:trage|trag|lege|erstelle|schreibe|verfasse|plane|sende|versende|verschicke|lösche|kaufe|buche|bezahle|'
                 r'termin|kalender(?:eintrag)?|mail(?:entwurf)?|e-mail(?:-entwurf)?)\b',command):
        return False
    calendar=bool(re.search(r'\b(?:termin|kalender(?:eintrag)?)\b',command))
    mail=bool(re.search(r'\b(?:mail(?:entwurf)?|e-mail(?:-entwurf)?|entwurf)\b',command))
    if kind=='calendar.create':
        if re.search(r'\bnicht\s+(?:ein|an|anlegen|eintragen|erstellen)\b',command):
            return False
        return calendar and not mail and bool(re.search(r'\b(?:trage|trag|eintragen|anlegen|erstelle|erstellen|lege|plane|planen|notiere)\b',command))
    if kind=='gmail.compose_draft':
        return mail and bool(re.search(r'\b(?:verfasse|verfassen|schreibe|schreiben|erstelle|erstellen|entwirf|entwerfen|lege|anlegen)\b',command))
    return False


def extract(text, proposal):
    if type(proposal) is not dict or set(proposal)!={'kind','fields'} or proposal['kind'] not in KINDS:
        raise ValueError('intent_proposal_invalid')
    fields=proposal['fields']
    if type(fields) is not dict or set(fields)!=FIELDS:
        raise ValueError('intent_proposal_fields_invalid')
    values={key:_literal(text,span) for key,span in fields.items()}
    kind=proposal['kind']
    if kind=='clarify' or not clear_kind(text,kind):
        return {'kind':'clarify','fields':{}}
    allowed={'to'} if kind=='gmail.compose_draft' else FIELDS-{'to'}
    if any(v for k,v in values.items() if k not in allowed):
        raise ValueError('intent_unexpected_field')
    if kind=='gmail.compose_draft' and values.get('to'):
        addresses=re.findall(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~\-]+@[A-Za-z0-9.\-]+",text)
        prefix=text[:fields['to'][0]]
        if len(addresses)!=1 or not re.search(r'\b(?:an|für|fuer)\s+$',prefix,re.I):
            values['to']=''
    return {'kind':kind,'fields':values}


def date_value(value, anchor):
    s=value.casefold().strip()
    if not re.fullmatch(r'(?:heute|morgen|übermorgen|uebermorgen|montag|dienstag|mittwoch|donnerstag|freitag|samstag|sonntag|\d{4}-\d{2}-\d{2})',s):
        raise ValueError('invalid_answer')
    try:
        return resolve_day(when=s if not s[0].isdigit() else '',date_text=s if s[0].isdigit() else '',reference=anchor)
    except CalendarDeclined:
        raise ValueError('invalid_answer') from None


def time_value(value, day):
    # Explicit offsets are accepted for DST folds; otherwise require a unique
    # existent local wall-clock instant, never silently choose one side.
    match=re.fullmatch(r'(\d{1,2})(?::(\d{2}))?(?:\s*Uhr)?([+-]\d{2}:\d{2})?',value,re.I)
    if not match:
        raise ValueError('invalid_answer')
    hour,minute=int(match[1]),int(match[2] or 0)
    if hour>23 or minute>59:
        raise ValueError('invalid_answer')
    local=datetime.combine(day,time(hour,minute),USER_TIMEZONE)
    valid=[]
    for fold in (0,1):
        candidate=local.replace(fold=fold)
        if candidate.astimezone(timezone.utc).astimezone(USER_TIMEZONE).replace(fold=fold)==candidate:
            if not valid or candidate.utcoffset()!=valid[0].utcoffset():
                valid.append(candidate)
    if match[3]:
        explicit=datetime.fromisoformat(day.isoformat()+f'T{hour:02d}:{minute:02d}'+match[3])
        valid=[v for v in valid if v.utcoffset()==explicit.utcoffset()]
    if len(valid)!=1:
        raise ValueError('invalid_answer')
    return valid[0]


def minutes_value(value):
    match=re.fullmatch(r'(\d{1,4})(?:\s*(Minuten?|min|Stunden?|h))?',value,re.I)
    if not match:
        raise ValueError('invalid_answer')
    number=int(match[1])*(60 if (match[2] or '').casefold().startswith(('stund','h')) else 1)
    if not 1<=number<=1440:
        raise ValueError('invalid_answer')
    return number


def email_value(value):
    from solvio.agent_runtime.action_contract import _email
    _email(value)
    return value


def resolve(values, answers, *, anchor):
    """Return missing field or exact native payload, all from owner literals."""
    kind=values['kind']
    if kind=='clarify':
        return 'instruction',None
    fields=values['fields']|{k:v for k,v in answers.items() if k in FIELDS}
    if kind=='gmail.compose_draft':
        try:
            address=email_value(fields.get('to',''))
        except ValueError:
            return 'to',None
        return '',{'to':address}
    for key in ('title','when','time'):
        if not fields.get(key):
            return key,None
    try:
        day=date_value(fields['when'],anchor)
    except ValueError:
        return 'when',None
    try:
        start=time_value(fields['time'],day)
    except ValueError:
        return 'time',None
    if fields.get('duration'):
        try:
            end=plus(start,timedelta(minutes=minutes_value(fields['duration'])))
        except ValueError:
            return 'duration',None
    elif fields.get('end_time'):
        try:
            end=time_value(fields['end_time'],day)
            if end.timestamp()<=start.timestamp():
                raise ValueError('invalid_answer')
        except ValueError:
            return 'duration',None
    else:
        return 'duration',None
    return '',{'summary':fields['title'],'start':start.isoformat(),'end':end.isoformat(),
        'all_day':False,'description':'','location':fields.get('location','')}


def validate_answer(field, value, *, anchor, fields):
    if field=='when': date_value(value,anchor)
    elif field=='time':
        day=date_value(fields.get('when','heute'),anchor)
        time_value(value,day)
    elif field=='duration': minutes_value(value)
    elif field=='to': email_value(value)
    elif field=='title' and len(value)>500: raise ValueError('invalid_answer')
    return value
