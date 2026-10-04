"""Ortskenntnis fuer genau ein Portal: SOLVIO Studio.

Hier stehen die Annahmen ueber *diese* Seite, damit sie nirgends sonst stehen.
Wenn Studio morgen umgebaut wird, aendert sich diese Datei — und keine andere.

Was auf der Seite steht, hat eine erkennbare Ordnung: erst die Navigation, dann
das Kontomenue, dann der Arbeitsbereich. Diese Ordnung wird hier nicht ueber
Positionen gelesen, sondern ueber **Ankertexte**. Eine feste Zeilennummer ist
nach dem naechsten Deploy falsch und meldet das nicht; ein fehlender Ankertext
faellt sofort auf, weil das Feld dann leer bleibt.

Zwei Dinge, die diese Datei ausdruecklich NICHT tut:

* Sie erfindet nichts. Jede Kennzahl haengt an einem woertlichen Muster der
  Seite. Trifft es nicht, fehlt die Kennzahl. Es gibt keinen Vorgabewert.
* Sie gibt keine Inhalte weiter. Auf der Seite stehen fertige Textentwuerfe fuer
  mehrere Plattformen. Ein Statusbericht braucht davon, dass es sie gibt und wie
  viele — nicht ihren Wortlaut. Der bleibt auf der Seite.
"""
from __future__ import annotations

import re
from typing import Any

from solvio.portal.readout import lines_of, register

PORTAL_ID = "solvio-studio"

#: Das Ende des Kontomenues. Sehr stabil: eine angemeldete Seite hat immer einen
#: Weg hinaus, und er heisst seit jeher so. Davor steht die Huelle, danach der
#: Arbeitsbereich — die einzige Trennung, die diese Reduktion wirklich braucht.
LOGOUT = "Abmelden"

#: Gruppenueberschriften der Navigation. Grossbuchstaben allein reichen nicht als
#: Regel, weil auch Abschnitte im Arbeitsbereich so gesetzt sind.
NAV_GROUPS = ("WORKFLOW", "KONTO")

#: Die Kontonummer: die einzige rein numerische Zeile in der Huelle.
ACCOUNT_NUMBER = re.compile(r"^\d{4,12}$")

#: Initialen im Kontomenue, z.B. „GB".
INITIALS = re.compile(r"^[A-ZÄÖÜ]{1,3}$")

NEXT_STEP = "Nächster Schritt:"

#: Jede Kennzahl: ein woertliches Muster der Seite und die Beschriftung, unter
#: der sie berichtet wird. Die Beschriftungen sind aus den Worten der Seite
#: gebildet, nicht ausgedacht — wer sie liest, findet sie dort wieder.
METRICS: tuple[tuple[str, str], ...] = (
    ("Verwendete Themen bisher",      r"Verwendete Themen\s*\((\d+)\)"),
    ("Frühere Wochenpläne",           r"Frühere Wochenpläne\s*\((\d+)\)"),
    ("Plattformen ausgewählt",        r"^(\d+) ausgewählt$"),
    ("Plattformen im Tarif",          r"bis zu (\d+) Plattformen"),
    ("Performance-Sets, geplanter Verbrauch", r"Verwendet (\d+) Performance-Sets"),
    ("Posts im aktuellen Wochenplan", r"^(\d+) Posts?$"),
    ("Wochenthema, Zeichen genutzt",  r"^(\d+)/(\d+)$"),
)

#: Hinweise, die kein Fehler sind, aber in einen Statusbericht gehoeren.
NOTICES: tuple[tuple[str, str], ...] = (
    ("Cookie-Banner steht offen",
     r"Wir verwenden technisch notwendige Cookies"),
    ("Aktueller Wochenplan ist ein Entwurf, nicht freigegeben",
     r"^Entwurf$"),
)

#: Was noch als Beschriftung durchgeht. Eine Ueberschrift ist kurz; ein Absatz
#: ist keine Ueberschrift, auch wenn er an ihrer Stelle steht. Ohne diese Grenze
#: haette ein Auszug 5000 Zeichen Fliesstext als „offene Seite" gemeldet — der
#: Wert waere nicht erfunden gewesen, aber genauso falsch.
MAX_LABEL = 120

PLAN_HEADING = "AKTUELLER WOCHENPLAN"
PLAN_DATE = re.compile(r"^\d{2}\.\d{2}\.\d{4},\s*\d{2}:\d{2}$")

#: Plattformnamen tauchen auf dieser Seite ZWEIMAL auf: oben als Auswahlkasten
#: („wo soll veroeffentlicht werden") und weiter unten als Ueberschrift ueber
#: einem fertigen Entwurf. Wer nur den Namen sucht, zaehlt Angebote als Arbeit.
#: Ein Entwurf erkennt man an der Zeile darunter — „831 Zeichen".
PLATFORMS = ("Instagram", "Facebook", "LinkedIn", "X / Twitter", "TikTok",
             "YouTube", "Pinterest", "Threads")
DRAFT_LENGTH = re.compile(r"^\d+ Zeichen$")


def _short(line: str) -> str:
    """Die Zeile, wenn sie als Beschriftung taugt — sonst nichts.

    Ausdruecklich kein Abschneiden: ein gekuerzter Absatz sieht aus wie eine
    Ueberschrift und ist keine. Lieber ein fehlendes Feld als ein falsches.
    """
    return line if 0 < len(line) <= MAX_LABEL else ""


def _shell_and_body(lines: list[str]) -> tuple[list[str], list[str]]:
    """Huelle (Navigation + Konto) und Arbeitsbereich, getrennt am Abmelden."""
    try:
        cut = lines.index(LOGOUT)
    except ValueError:
        # Kein Abmelden-Knopf: entweder nicht angemeldet oder umgebaut. Beides
        # heisst hier dasselbe — keine Huelle behaupten, die man nicht sieht.
        return [], lines
    return lines[:cut], lines[cut + 1:]


def _account_start(shell: list[str]) -> int:
    """Wo das Kontomenue beginnt — der Anker ist die Kontonummer.

    Ohne diese Grenze laeuft die letzte Navigationsgruppe einfach weiter und
    nimmt Name und Kontonummer als „Menuepunkte" mit. Das sah im ersten Lauf
    plausibel aus und war schlicht falsch.
    """
    for index, line in enumerate(shell):
        if ACCOUNT_NUMBER.match(line) and index >= 2:
            start = index - 2
            if start >= 1 and INITIALS.match(shell[start - 1]):
                start -= 1
            return start
    return len(shell)


def _sections(shell: list[str]) -> dict[str, list[str]]:
    """Die Navigationsgruppen mit ihren Eintraegen, in Seitenreihenfolge."""
    sections: dict[str, list[str]] = {}
    current = ""
    for line in shell:
        if line in NAV_GROUPS:
            current = line
            sections[current] = []
        elif current and len(sections[current]) < 20:
            sections[current].append(line)
    return {name: items for name, items in sections.items() if items}


def _account(shell: list[str]) -> dict[str, str]:
    """Name, Arbeitsbereich, Kontonummer — verankert an der Nummer selbst.

    Rueckwaerts gelesen, weil die Nummer der einzige eindeutige Anker ist: ein
    Name kann alles sein, eine rein numerische Zeile in der Huelle nicht.
    """
    for index, line in enumerate(shell):
        if not ACCOUNT_NUMBER.match(line) or index < 2:
            continue
        account: dict[str, str] = {"kundennummer": line,
                                   "arbeitsbereich": shell[index - 1],
                                   "name": shell[index - 2]}
        if index >= 3 and INITIALS.match(shell[index - 3]):
            account["initialen"] = shell[index - 3]
        return account
    return {}


def _metrics(lines: list[str]) -> list[dict[str, str]]:
    """Beschriftete Zahlen. Kein Treffer, kein Eintrag — nie ein Vorgabewert."""
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for label, pattern in METRICS:
        expression = re.compile(pattern)
        for line in lines:
            match = expression.search(line)
            if match is None:
                continue
            value = match.group(1)
            if match.lastindex and match.lastindex >= 2:
                value = f"{match.group(1)} von {match.group(2)}"
            if label not in seen:
                found.append({"label": label, "wert": value})
                seen.add(label)
            break
    return found


def _notices(lines: list[str]) -> list[str]:
    text = "\n".join(lines)
    out: list[str] = []
    for note, pattern in NOTICES:
        if re.search(pattern, text, re.MULTILINE):
            out.append(note)
    return out


def _next_steps(lines: list[str]) -> list[str]:
    return [line for line in lines if line.startswith(NEXT_STEP)][:5]


def _plan(body: list[str]) -> dict[str, Any]:
    """Der aktuelle Wochenplan — Thema, Zustand, Umfang, Stand.

    Ausdruecklich ohne die Beitragstexte. Sie stehen direkt darunter auf der
    Seite, sie sind lang, und fuer die Frage „wie steht es" tragen sie nichts
    bei, was die Anzahl nicht schon sagt.
    """
    try:
        start = body.index(PLAN_HEADING) + 1
    except ValueError:
        return {}
    window = body[start:start + 6]
    plan: dict[str, Any] = {}
    if window and _short(window[0]):
        plan["thema"] = window[0]
    for line in window[1:]:
        if line in ("Entwurf", "Freigegeben", "Veröffentlicht"):
            plan.setdefault("status", line)
        elif re.match(r"^\d+ Posts?$", line):
            plan.setdefault("posts", line.split(" ", 1)[0])
        elif PLAN_DATE.match(line):
            plan.setdefault("stand", line)
    drafts = [name for index, name in enumerate(body[:-1])
              if name in PLATFORMS and DRAFT_LENGTH.match(body[index + 1])]
    if drafts:
        plan["plattformen_mit_entwurf"] = drafts
    return plan


def reduce(reply: dict[str, Any]) -> dict[str, Any]:
    """Aus einer gelesenen Studio-Seite ein kompakter, stabiler Zustand."""
    lines = lines_of(str(reply.get("text", "")))
    shell, body = _shell_and_body(lines)
    readout: dict[str, Any] = {"portal": PORTAL_ID}

    account = _account(shell)
    if account:
        readout["konto"] = account
    sections = _sections(shell[:_account_start(shell)])
    if sections:
        readout["bereiche"] = sections
    if body and _short(body[0]):
        # Die erste Zeile nach dem Abmelden ist die Ueberschrift des
        # Arbeitsbereichs — auf dieser Seite „Wochenplaner".
        readout["seite"] = body[0]
    metrics = _metrics(lines)
    if metrics:
        readout["kennzahlen"] = metrics
    steps = _next_steps(lines)
    if steps:
        readout["naechste_schritte"] = steps
    notices = _notices(lines)
    if notices:
        readout["hinweise"] = notices
    plan = _plan(body)
    if plan:
        readout["wochenplan"] = plan
    return readout


register(PORTAL_ID, reduce)
