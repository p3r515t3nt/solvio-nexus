"""Bestehende lesende Faehigkeiten als Werkzeuge eines Auftrags (Kurskorrektur 25.09.2026, Stufe S1).

Der allgemeine Auftragsarbeiter erreichte bis hierher genau zwei Core-Werkzeuge
(`portal_list`, `result_files_list`), obwohl Mail und Kalender laengst im Core
lesen. Dieses Modul baut keine neue Fachlogik: es verpackt die VORHANDENEN
Handler so, wie der Router einen Auftragsaufruf verlangt — geschlossene
Bindung, strenge Argumente, gepruefter Kostenvertrag.

Chat und Sprache rufen weiter `__call__`, also genau den bisherigen Handler;
fuer sie aendert sich nichts. Nur `execute` (der Auftragsweg) bereitet das
Ergebnis fuer den Modellkontext auf:

* **Zugangsdaten verlassen den Core nicht.** Mails tragen Anmeldelinks,
  Einmalcodes und Tokens. Schluesselformen und Zugangsdaten-Zeilen werden vor
  der Rueckgabe geschwaerzt; was danach den Zaun der Werkzeugantwort noch nicht
  besteht, wird zurueckgehalten statt den ganzen Aufruf zu verwerfen.
* **Die Antwort passt in die Grenze.** Lange Texte werden gekuerzt und als
  gekuerzt markiert, nie still abgeschnitten.

Mailinhalt bleibt INFORMATION, nie AUTORITAET: diese Werkzeuge lesen nur, und
jede Wirkung nach aussen laeuft weiter ueber ihre eigene Freigabe.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from types import MethodType
from typing import Any

from solvio.capabilities.contract import CapabilityDeclined

#: Der eine Kostenvertrag dieser Werkzeuge (ein Beleg-Bezeichner, wie
#: `solvio:portal-list-local:v1`). Gmail- und Kalender-Lesezugriffe der
#: Google-APIs werden nicht abgerechnet; sie verbrauchen nur Kontingent.
CONTRACT = "solvio:task-read-google-workspace:v1"

_ID = re.compile(r"[A-Za-z0-9_\-]{1,128}")

#: Je Faehigkeit die erlaubten Felder: (Typ, Untergrenze, Obergrenze). Fuer
#: Texte ist die Grenze die Laenge, fuer Zahlen der Wert. Nichts anderes kommt durch.
ARGUMENT_RULES: dict[str, tuple[dict[str, tuple[str, int, int]], frozenset[str]]] = {
    "gmail_list_recent": ({"only_unread": ("boolean", 0, 0), "limit": ("integer", 1, 25)}, frozenset()),
    "gmail_search": ({"query": ("string", 1, 300), "limit": ("integer", 1, 25)}, frozenset({"query"})),
    "gmail_read_message": ({"message_id": ("id", 1, 128)}, frozenset({"message_id"})),
    "gmail_read_thread": ({"thread_id": ("id", 1, 128)}, frozenset({"thread_id"})),
    "calendar_list_events": ({"when": ("string", 1, 40), "days": ("integer", 1, 31)}, frozenset()),
    "calendar_get_event": ({"title": ("string", 1, 200), "when": ("string", 1, 40)}, frozenset({"title"})),
    "calendar_search_events": ({"query": ("string", 1, 200), "days": ("integer", 1, 366)},
                               frozenset({"query"})),
    "calendar_find_availability": ({"when": ("string", 1, 40), "duration_minutes": ("integer", 5, 600),
                                    "earliest": ("string", 1, 10), "latest": ("string", 1, 10)},
                                   frozenset()),
}

#: Obergrenze der serialisierten Antwort — unter der Grenze der Werkzeugantwort
#: (native_tools.MAX_RESPONSE_CHARS = 32000), damit Umschlag und Status Platz haben.
MAX_MATERIAL_CHARS = 28000
_TEXT_CAPS = (6000, 3000, 1500, 600)
_TRUNCATED = " [gekuerzt]"
WITHHELD = "[zurueckgehalten: Zugangsdaten]"


def validate_arguments(name: str, arguments: Any) -> dict[str, Any]:
    """Eine strenge Kopie der Argumente — oder eine Absage mit Grund."""
    if name not in ARGUMENT_RULES:
        raise CapabilityDeclined("unknown_task_read", "Dieses Werkzeug gibt es fuer Auftraege nicht.")
    fields, required = ARGUMENT_RULES[name]
    if type(arguments) is not dict or not set(arguments) <= set(fields) or not required <= set(arguments):
        raise CapabilityDeclined("invalid_arguments", "Die Angaben fuer dieses Werkzeug passen nicht.")
    clean: dict[str, Any] = {}
    for key, value in arguments.items():
        kind, low, high = fields[key]
        if kind == "boolean":
            ok = type(value) is bool
        elif kind == "integer":
            ok = type(value) is int and low <= value <= high
        elif kind == "id":
            ok = type(value) is str and _ID.fullmatch(value) is not None
        else:
            ok = type(value) is str and low <= len(value.strip()) and len(value) <= high
        if not ok:
            raise CapabilityDeclined("invalid_arguments", "Die Angaben fuer dieses Werkzeug passen nicht.")
        clean[key] = value
    return clean


def input_schema(name: str) -> dict[str, Any]:
    """Das Schema, das der Arbeiter sieht — dieselbe Regel wie `validate_arguments`."""
    fields, required = ARGUMENT_RULES[name]
    properties: dict[str, Any] = {}
    for key, (kind, low, high) in fields.items():
        if kind == "boolean":
            properties[key] = {"type": "boolean"}
        elif kind == "integer":
            properties[key] = {"type": "integer", "minimum": low, "maximum": high}
        elif kind == "id":
            properties[key] = {"type": "string", "pattern": "^" + _ID.pattern + "$"}
        else:
            properties[key] = {"type": "string", "minLength": low, "maxLength": high}
    schema: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = sorted(required)
    return schema


#: Mails tragen Einmalcodes, Anmelde- und Ruecksetzlinks und Passwortsaetze. Sie
#: werden VOR der Rueckgabe geschwaerzt (Review S1-3) — nach bestem Erkennen, keine
#: Garantie; die Werkzeugbeschreibung sagt das so.
_URL = re.compile(r"https?://[^\s<>\"'\])]+", re.IGNORECASE)
# Tokens in Kurz-, Magic- und Ruecksetzlinks: ab acht Zeichen mit mindestens drei
# Wechseln zwischen Buchstabe und Ziffer, oder ab 32 Zeichen mit einer Ziffer. Lesbare
# Slugs wie `hotel-alster-2026` bleiben (Review S1R2-4, S1R3-3).
_SEGMENT_CHARS = re.compile(r"[A-Za-z0-9_\-]{8,}")
_CODE = re.compile(
    r"(\b(?:code|pin|tan|otp|passcode|kenncode|einmal-?passwort|einmal-?kennwort"
    r"|(?:einmal|best(?:ae|\u00e4)tigung|verifizierung|sicherheit|anmelde|anmeldung|zugang|freischalt"
    r"|login|pr(?:ue|\u00fc)f)s?-?code|verification\s+code|security\s+code|login\s+code)\b"
    r"[^\n\d]{0,40}?)(\d[\d \-]{2,14}\d|(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{4,12})\b", re.IGNORECASE)
_SECRET = re.compile(
    r"(\b(?:passwort|kennwort|password|passwd|pwd|geheimzahl)\b"
    r"(?:[ \t]+(?:lautet|lauten|ist|is)\b[ \t]*:?|[ \t]*[:=])[ \t]*)(\S{3,})", re.IGNORECASE)
#: Eine Zeile mit einem echten Code- oder Anmeldebegriff: alleinstehende Zahlen mit vier
#: bis acht Ziffern, auch in Gruppen („482 927") oder mit Praefix („G-482920"), gleich ob vor
#: oder nach dem Begriff (Review S1R2-4). NICHT: „Bestaetigung" oder „Anmeldung" allein,
#: und keine Belegnummern hinter Nr./Rechnung/Buchung/Bestellung (Review S1R3-3).
_CODE_CONTEXT = re.compile(
    r"code|passcode|\bpin\b|\btan\b|\botp\b|passwort|kennwort|password"
    r"|\b(?:anmelden|anzumelden|einloggen|einzuloggen|log(?:ge)?\s?in|sign\s?in)\b", re.IGNORECASE)
_CODE_NUMBER = re.compile(r"(?<![\w.,/-])(?:[A-Z]-)?(?:\d{3}[ -]\d{3}|\d{4,8})(?!\w|[.,/-]\d)")
_DOCUMENT_BEFORE = re.compile(
    r"(?:nr\.?|nummer|rechnung\w*|buchung\w*|bestellung\w*|auftrag\w*|kunden\w*|vertrag\w*|plz|order"
    r"|invoice|booking)[\s:#.-]*$", re.IGNORECASE)
#: Ein Etikett am Zeilenende — der Wert steht in der NAECHSTEN Zeile, oft nach einer
#: Leerzeile (Review S1R2-1). Code-Saetze mit Doppelpunkt zaehlen mit („Dein Code lautet:",
#: „Geben Sie den folgenden Code ein:", Review S1R3-2).
_LABEL_AT_END = re.compile(
    r"(?:passwort|kennwort|password|passwd|pwd|\bpin|\btan|\botp|token|api-?key|schl(?:ue|\u00fc)ssel"
    r"|geheimzahl|zugangsdaten|passcode|code)\s*[:=]?\s*$", re.IGNORECASE)
_CODE_PHRASE_AT_END = re.compile(
    r"(?:code|passcode|\bpin\b|\btan\b|\botp\b|passwort|kennwort|password)\b[^\n]{0,60}"
    r"(?::|\b(?:lautet|lauten|ist|is))\s*$", re.IGNORECASE)
#: Was dort als Wert steht: ein Wort mit Ziffer oder Zeichen, oder Ziffern in Gruppen.
#: Eine Zeile, die mit ihrem Trenner beginnt („: blaue Katze tanzt"), ist nach einem
#: Etikett immer ein Wert — auch eine Passphrase aus mehreren Woertern.
_VALUE_LINE = re.compile(
    r"\s*(?:[:=]\s*\S.{0,120}|(?=\S*[\d!@#$%^&*+=?])\S{3,64}|\d[\d \-]{2,14}\d)\s*")
_LINE_BREAKS = re.compile("[\u2028\u2029\u0085\r]")
#: Abstand der eigenen Lesefrist zur Frist des Routers (Review S1R2-2).
READ_DEADLINE_MARGIN = 8.0
#: Die Aufbereitung endet so lange vor der Routerfrist (S1R5-2).
MATERIAL_DEADLINE_MARGIN = 3.0
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_LINK_REMOVED = "?[entfernt]"
_TOKEN_REMOVED = "[\u2026]"


def _token_like(part: str) -> bool:
    if not _SEGMENT_CHARS.fullmatch(part) or not re.search(r"\d", part) or not re.search(r"[A-Za-z]", part):
        return False
    changes = sum(1 for a, b in zip(part, part[1:]) if a.isdigit() != b.isdigit() and a.isalnum() and b.isalnum())
    return changes >= 3 or len(part) >= 32


def _numbers_in_code_line(line: str, mask: str) -> str:
    def replace(match):
        return match.group(0) if _DOCUMENT_BEFORE.search(line[max(0, match.start() - 24):match.start()]) else mask
    return _CODE_NUMBER.sub(replace, line)


#: Wie viele vorhergehende nichtleere Zeilen ein Etikett wirkt: bis zu vier Zwischenzeilen
#: („Ihre PIN\\nfuer die Karte:\\n4711", Review S1R4-1; drei Zwischenzeilen, S1R5-1b).
#: Weiter entfernte Werte sind die dokumentierte Grenze (DEBT-0328): die erste Fassung
#: verwarf dort die ganze Mail; hier bleibt der Rest lesbar, und die Sitzung hat kein Web.
LABEL_WINDOW = 5
#: Kontiguierliche Fenster, die der Zaun mit ansieht (die Einzelpruefung reicht weiter).
CONTIGUOUS_WINDOW = 3
#: Ein fester Probewert: eine Zeile ist ETIKETT, wenn der Zaun sie allein durchlaesst, mit
#: diesem Wert dahinter aber nicht — die Begriffe des Zauns, keine eigene Liste (S1R5-1a).
_LABEL_PROBE = "Ab1!cdef"


def _window_refused(previous: list[str], line: str) -> bool:
    """Der Antwortzaun der ersten Fassung sah den ganzen Text als eine Zeile. Hier sieht
    er die Zeile mit bis zu LABEL_WINDOW vorhergehenden (Originaltext, kodierter Umbruch):
    verweigert er erst MIT dieser Zeile, ist sie ein Wert."""
    if not _passes(line):
        return False
    recent = previous[-LABEL_WINDOW:]
    for size in range(1, min(CONTIGUOUS_WINDOW, len(recent)) + 1):
        window = "\\n".join(recent[-size:])
        if _passes(window) and not _passes(window + "\\n" + line):
            return True
    # A window that is refused on its own („Passwort\\nfuer das Kundenkonto") cannot
    # attribute the refusal to this line; each earlier line alone still can.
    return any(_passes(earlier) and not _passes(earlier + "\\n" + line) for earlier in recent)


def _is_label(line: str) -> bool:
    return bool(_LABEL_AT_END.search(line) or _CODE_PHRASE_AT_END.search(line)
                or (_passes(line) and not _passes(line + "\\n" + _LABEL_PROBE)))


def _pair_refused(first: str, second: str) -> bool:
    """Der Antwortzaun, wie er die erste Fassung (`1842683`) sah: ein Etikett, und nach
    einem kodierten Umbruch sein Wert. Was er damals als Ganzes verweigerte, wird jetzt
    als EINE Wertzeile geschwaerzt (Review S1R3-1: „Einmalpasswort:\\n482922",
    Passphrasen) — kein Rueckschritt, aber auch kein gescheiterter Aufruf."""
    return not _passes(first + "\\n" + second) and _passes(first) and _passes(second)


def _link(match: re.Match) -> str:
    """Ein Link bleibt als Ziel erkennbar; Abfrage, Anker, Anmeldedaten im Host und
    tokenartige Pfadteile nicht (Magic- und Ruecksetzlinks)."""
    url = match.group(0)
    head, query, _ = url.partition("?")
    head = head.split("#", 1)[0]
    scheme, _, rest = head.partition("://")
    host, slash, tail = rest.partition("/")
    host = host.rsplit("@", 1)[-1]
    segments = [_TOKEN_REMOVED if _token_like(part) else part for part in tail.split("/")]
    cleaned = scheme + "://" + host + ("/" + "/".join(segments) if slash else "")
    return cleaned + (_LINK_REMOVED if query else "")


def _scrub(text: str) -> str:
    """Links, Schluesselformen, Codes, Passwortsaetze und Zugangsdaten-Zeilen schwaerzen."""
    from solvio.memory.intent import _KEY_SHAPES
    from solvio.secret_vault import firewall
    from solvio.specialists.launcher import MASK, redact
    cleaned = _URL.sub(_link, _LINE_BREAKS.sub("\n", text))
    cleaned = redact(cleaned)
    for shape in _KEY_SHAPES:
        cleaned = shape.sub(MASK, cleaned)
    cleaned = _CODE.sub(lambda m: m.group(1) + MASK, cleaned)
    cleaned = _SECRET.sub(lambda m: m.group(1) + MASK, cleaned)
    lines = cleaned.split("\n")
    # Up to LABEL_WINDOW preceding non-empty lines in their ORIGINAL text: a blacked-out
    # line stays a possible label for what follows („Passwort: …", „Kennwort", Wert).
    previous: list[str] = []
    labels: list[bool] = []
    for index, line in enumerate(lines):
        if _CODE_CONTEXT.search(line):
            line = lines[index] = _numbers_in_code_line(line, MASK)
        if not line.strip():
            continue
        if _window_refused(previous, line) or (any(labels) and _VALUE_LINE.fullmatch(line)):
            lines[index] = firewall.TRANSCRIPT_MARKER
        elif firewall.looks_like_credential_line(line, prose=False, core_material=True):
            lines[index] = firewall.TRANSCRIPT_MARKER
        previous = (previous + [line])[-LABEL_WINDOW:]
        labels = (labels + [_is_label(line)])[-LABEL_WINDOW:]
    return "\n".join(lines)


def _passes(text: str) -> bool:
    from solvio.secret_vault import firewall
    from solvio.specialists.launcher import redact
    try:
        firewall.refuse_if_credential_material(text, where="task_read.material")
    except firewall.CredentialRefused:
        return False
    return redact(text) == text


def leaf_ok(text: str, key: str = "") -> bool:
    """Derselbe Zaun wie fuer die Werkzeugantwort, in den Formen, in denen er sie
    sieht: roh und JSON-kodiert, allein und hinter seinem Schluessel (Review S1-2)."""
    forms = [text, json.dumps(text, ensure_ascii=False)]
    if key:
        forms += [f"{key}: {text}", json.dumps({key: text}, ensure_ascii=False)]
    return all(_passes(form) for form in forms)


# Rueckwaertsname fuer die Argumentpruefung der Bruecke.
_passes_fence = leaf_ok


#: Geschwaerzt wird nur, was ueberhaupt gezeigt werden kann: die groesste Textgrenze plus
#: Kontext davor. Einmal je Text und Aufruf — die Kuerzungsstufen nehmen nur noch weg
#: (gemessen 26.09.2026: 60 lange Mails brauchten sonst 12 s).
_SCRUB_WINDOW = max(_TEXT_CAPS) + 2000


class _MaterialTimeout(Exception):
    pass


def _text(node: str, cap: int, key: str, memo: dict) -> Any:
    """Ein Text: geschwaerzt, ohne Steuerzeichen, gekuerzt — und mehrzeilig als LISTE
    von Zeilen. In der Antwort waere ein Umbruch sonst `\\n`, und der Zaun laese
    „Cookies\\nMehr erfahren" als Zugangsdatum (gemessen 25.09.2026)."""
    scrubbed = memo.get(("scrub", node))
    if scrubbed is None:
        if memo.get("deadline") is not None and time.monotonic() > memo["deadline"]:
            raise _MaterialTimeout()
        scrubbed = memo[("scrub", node)] = _CONTROL.sub(" ", _scrub(node[:_SCRUB_WINDOW]).replace("\t", " "))
    text = scrubbed
    if len(text) > cap:
        text = text[:cap] + _TRUNCATED

    def ok(part, part_key):
        found = memo.get(("ok", part, part_key))
        if found is None:
            found = memo[("ok", part, part_key)] = leaf_ok(part, part_key)
        return found

    if "\n" not in text:
        return text if ok(text, key) else WITHHELD
    return [line if ok(line, "") else WITHHELD for line in text.split("\n")]


def _material(node: Any, cap: int, key: str = "", memo: dict | None = None) -> Any:
    memo = {} if memo is None else memo
    if isinstance(node, str):
        return _text(node, cap, key, memo)
    if isinstance(node, list):
        return [_material(item, cap, "", memo) for item in node]
    if isinstance(node, dict):
        return {str(k): _material(v, cap, str(k), memo) for k, v in node.items()}
    if node is None or isinstance(node, (bool, int, float)):
        return node
    return _text(str(node), cap, key, memo)


def fit(data: Any, cap: int) -> Any:
    """Dieselbe Aufbereitung mit einer festen Textgrenze (die Bruecke kuerzt weiter,
    bis die endgueltige Antwort passt)."""
    return _material(data, cap)


def _minimum_size(node: Any) -> int:
    """A lower bound of the material at the smallest text limit (texts only)."""
    if isinstance(node, str):
        return min(len(node), min(_TEXT_CAPS))
    if isinstance(node, list):
        return sum(_minimum_size(item) for item in node)
    if isinstance(node, dict):
        return sum(_minimum_size(value) for value in node.values())
    return 0


_TOO_LARGE = {"withheld": "result_too_large",
              "hint": "Das Ergebnis ist zu gross; bitte enger suchen oder einzeln lesen."}


def task_material(data: Any, deadline: float | None = None) -> Any:
    """Das Ergebnis eines lesenden Handlers, aufbereitet fuer den Modellkontext. Mit
    `deadline` (monotone Zeit) endet die Aufbereitung rechtzeitig vor der Routerfrist
    und haelt das Ergebnis zurueck, statt den Aufruf `unknown` werden zu lassen (S1R5-2)."""
    if _minimum_size(data) > MAX_MATERIAL_CHARS:
        # Even at the smallest text limit this cannot fit: say so at once instead of
        # scrubbing everything first (Review S1R6-2: 100 long mails blocked 8 s).
        return dict(_TOO_LARGE)
    memo: dict = {"deadline": deadline}
    try:
        for cap in _TEXT_CAPS:
            material = _material(data, cap, "", memo)
            if len(json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))) <= MAX_MATERIAL_CHARS:
                return material
    except _MaterialTimeout:
        return dict(_TOO_LARGE)
    return dict(_TOO_LARGE)


def _google_evidence_current() -> bool:
    """Die EINE gepruefte Preisbeobachtung fuer Google (task_action), mit Ablauf
    (Review S1-6) — kein zweiter Vertrag daneben."""
    from solvio.capabilities import task_action as TA
    return TA.GOOGLE_POLICY_CHECKED <= TA._today() <= TA.GOOGLE_POLICY_EXPIRES


class TaskRead:
    """Ein vorhandener lesender Handler mit dem Vertrag eines Auftragsaufrufs.

    Unterklassen nennen ROUTE, OWNER (die Klasse der Handler), METHODS (Name ->
    Originalfunktion) und SPECS. Der Router prueft Typ und Methodenbindung; ein
    kopierter Name oder eine ueberschriebene Methode bekommt keinen Kostenvertrag.
    """

    ROUTE: tuple[str, str] = ("", "")
    OWNER: type = type(None)
    METHODS: dict[str, Any] = {}
    SPECS: dict[str, Any] = {}

    def __init__(self, name: str, handler: Any) -> None:
        self.name = name
        self.handler = handler

    def _binding(self) -> dict[str, Any]:
        cls = type(self)
        handler = self.handler
        spec = cls.SPECS.get(self.name)
        if (spec is None or spec.name != self.name or spec.version != 1 or not spec.is_read_only()
                or self.name not in ARGUMENT_RULES or type(handler) is not MethodType
                or type(handler.__self__) is not cls.OWNER
                or handler.__func__ is not cls.METHODS.get(self.name)):
            raise ValueError("task_read_binding_invalid")
        return {"contract": CONTRACT, "capability": self.name,
                "service": cls.ROUTE[0], "operation": cls.ROUTE[1]}

    def resources(self, spec: Any, arguments: dict, task_step: Any = None) -> dict[str, Any]:
        if spec is not type(self).SPECS.get(self.name):
            raise ValueError("task_read_binding_invalid")
        # Ueber die Klasse, nie ueber die Instanz: ein Instanzattribut `_binding`
        # darf die Pruefung nicht ersetzen.
        resources = TaskRead._binding(self)
        validate_arguments(self.name, arguments)
        return resources

    def quote(self, service: str, invocation: Any):
        from solvio.agent_runtime import cost_dispatch as CD
        from solvio.agent_runtime.costs import CostEvidence
        cls = type(self)
        from solvio.capabilities import task_action as TA
        if (service != cls.ROUTE[0] or invocation.capability != self.name or invocation.version != 1
                or invocation.operation != cls.ROUTE[1]
                or invocation.resources_digest != CD._service_value_digest("resources", TaskRead._binding(self))):
            raise ValueError("task_read_binding_invalid")
        if not _google_evidence_current():
            return CD.CostQuote()
        return CD.CostQuote(0, CostEvidence("included_no_extra_charge", TA.GOOGLE_POLICY_REFERENCE),
                            validate_before_dispatch=_google_evidence_current)

    async def execute(self, arguments: dict, task_step: Any) -> Any:
        """Eine Absage des Handlers ist ein ABGESCHLOSSENER Aufruf ohne Erfolg, kein
        ungewisser (Review S1-1): lesen hat keine Wirkung, die nachzupruefen waere.
        Sonst sperrte „Mail nicht gefunden" die ganze Sitzung und den Auftrag."""
        from solvio.agent_runtime.cost_dispatch import ServiceOutcome
        from solvio.capabilities.contract import AmbiguousExecution, CapabilityRefused, ExecutorUnavailable
        receipt = "core:task-read:" + self.name
        if not _google_evidence_current():
            return ServiceOutcome("not_dispatched", reason="read_cost_evidence_expired", receipt_ref=receipt)
        spec = type(self).SPECS.get(self.name)
        started = time.monotonic()
        limit = float(getattr(spec, "timeout", 30.0) or 30.0)
        # The router's deadline would turn a slow read into `unknown` and block the
        # session (Review S1R2-2). A read has no effect: end it here, completed.
        deadline = max(0.1, limit - READ_DEADLINE_MARGIN)
        try:
            data = await asyncio.wait_for(self.handler(validate_arguments(self.name, arguments)), deadline)
        except (CapabilityDeclined, CapabilityRefused) as exc:
            reason = exc.reason if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", str(exc.reason or "")) else "read_declined"
            return ServiceOutcome("completed", reason=reason, receipt_ref=receipt + ":declined",
                                  data={"reason": reason, "hinweis": str(exc.human_message or "")[:200]})
        except (ExecutorUnavailable, AmbiguousExecution, TimeoutError):
            return ServiceOutcome("completed", reason="read_service_unavailable", receipt_ref=receipt + ":unavailable",
                                  data={"reason": "read_service_unavailable",
                                        "hinweis": "Postfach oder Kalender sind gerade nicht erreichbar."})
        # Off the event loop: scrubbing a long thread must not stall voice, chat and the
        # other tools of this process (Review S1R6-2).
        material = await asyncio.to_thread(task_material, data, started + limit - MATERIAL_DEADLINE_MARGIN)
        return ServiceOutcome("completed", ok=True, receipt_ref=receipt + ":terminal", data=material)

    async def __call__(self, arguments: dict) -> Any:
        return await self.handler(arguments)


TASK_READ_SERVICE_METHODS = {name: getattr(TaskRead, name) for name in ("resources", "quote", "execute")}

#: Werkzeuge mit privaten Daten des Owners. Eine Sitzung, die eines davon hat,
#: bekommt keinen Kanal nach aussen (ADR-0040, Review S1-4).
PRIVATE_DATA_TOOLS = frozenset(ARGUMENT_RULES) | {'owner_task_overview'}
