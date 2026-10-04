"""Zwoelf Zusicherungen fuer die kanalunabhaengige Kommunikationsschicht."""
from __future__ import annotations

import asyncio

import ast
import inspect
import os
import sys
import tempfile
import textwrap

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))

from _guard import enforce_assertions, require, require_equal  # noqa: E402
enforce_assertions()

from solvio.capabilities.communication import (  # noqa: E402
    DELIVERY_TRUTHS, CommunicationCapabilities, SPECS)
from solvio.capabilities.contract import (  # noqa: E402
    CapabilityDeclined, CapabilityRefused, ExecutorUnavailable)
from solvio.communication.bindings import BindingStore, normalize_alias  # noqa: E402
from solvio.tools.communication_capability_tools import _SCHEMAS  # noqa: E402


class _Gmail:
    def __init__(self) -> None:
        self.created = []
        self.sent = []
        self.messages = [{"from": "Max <max@example.test>"}]
        self.fail = False

    async def search(self, arguments):
        return {"messages": list(self.messages), "content_trust": "untrusted_email"}

    async def create_draft(self, arguments):
        self.created.append(dict(arguments))
        return {"draft_id": "d-1", "to": arguments["to"],
                "subject": arguments["subject"], "body": arguments["body"]}

    async def send_composed(self, *, to, subject, body):
        # Wie GmailCapabilities.send_composed: der von DIESER Freigabe gebundene Inhalt,
        # Byte fuer Byte (ADR-0041).
        self.created.append({"to": to, "subject": subject, "body": body})
        self.sent.append({"to": to, "subject": subject, "body": body})
        if self.fail:
            raise ExecutorUnavailable("transport")
        return {"message_id": "m-1", "sent": True}


def _stack():
    directory = tempfile.TemporaryDirectory()
    gmail = _Gmail()
    cap = CommunicationCapabilities(gmail, BindingStore(os.path.join(directory.name, "c.db")))
    cap._test_directory = directory
    return gmail, cap


def _confirm(cap, alias="mein Sohn", value="sohn@example.test", channel="gmail"):
    return cap.store.confirm(alias, "Mein Sohn", [{"channel": channel, "value": value}],
                             "user_confirmed")


async def t_a_relation_alias_cannot_be_invented_by_the_model():
    gmail, cap = _stack()
    result = await cap.resolve_recipient({"alias": "mein Sohn"})
    require(not result["confirmed"], str(result))
    require(cap.store.get("mein Sohn") is None, "ein Suchtreffer wurde zur Bindung")
    require_equal(gmail.sent, [], "die Aufloesung hat gesendet")


async def t_a_confirmed_alias_resolves_deterministically():
    gmail, cap = _stack()
    expected = _confirm(cap)
    gmail.messages = [{"from": "Angreifer <eve@example.test>"}]
    result = await cap.resolve_recipient({"alias": "  MEIN   SOHN "})
    require_equal(result, {"confirmed": True, "binding": expected})


async def t_a_changed_recipient_invalidates_the_send():
    gmail, cap = _stack(); _confirm(cap)
    try:
        await cap.send({"alias": "mein Sohn", "channel": "gmail",
                        "recipient_handle": "eve@example.test", "content": "Komm heim."})
    except CapabilityRefused as exc:
        require_equal(exc.reason, "recipient_not_bound")
    else:
        raise AssertionError("veraenderter Empfaenger akzeptiert")
    require(not gmail.created and not gmail.sent, "trotz falschem Empfaenger gesendet")


async def t_a_changed_content_is_not_silently_sent():
    gmail, cap = _stack(); _confirm(cap)
    result = await cap.send({"alias": "mein Sohn", "channel": "gmail",
                             "recipient_handle": "sohn@example.test",
                             "content": "Komm bitte nach Hause."})
    require_equal(gmail.created[0]["body"], result["requested_content"])
    require_equal(gmail.sent[0]["body"], result["requested_content"])


async def t_an_unbound_handle_is_named_as_such_even_on_an_unbound_channel():
    """Der Grund muss zum Problem passen — sonst ist eine Pruefung Zierde.

    Gefunden durch eine Mutation: die erste Pruefung („kennt die Bindung diesen
    Kontaktweg ueberhaupt?") liess sich entfernen, ohne dass etwas rot wurde.
    Die dritte faengt denselben Fall — aber nur, solange der KANAL stimmt.
    Stimmt er auch nicht, meldet ohne die erste Pruefung der Kanal das Problem,
    und der Empfaenger, um den es geht, kommt gar nicht vor.

    Dieselbe Lehre wie bei `assert_safe_ref`: ein Riegel, den niemand je
    erreicht, sieht aus wie Sicherheit und ist keine.
    """
    gmail, cap = _stack(); _confirm(cap)
    try:
        await cap.send({"alias": "mein Sohn", "channel": "sms",
                        "recipient_handle": "eve@example.test",
                        "content": "Komm."})
    except CapabilityRefused as exc:
        require_equal(exc.reason, "recipient_not_bound",
                      "der falsche Empfaenger wurde als Kanalproblem gemeldet")
    else:
        raise AssertionError("unbekannter Kontaktweg akzeptiert")
    require(not gmail.created and not gmail.sent, "trotzdem gesendet")


async def t_an_unknown_recipient_sends_nothing():
    gmail, cap = _stack()
    try:
        await cap.send({"alias": "mein Sohn", "channel": "gmail",
                        "recipient_handle": "x@example.test", "content": "Komm."})
    except CapabilityDeclined as exc:
        require_equal(exc.reason, "recipient_unknown")
    else:
        raise AssertionError("unbekannter Empfaenger akzeptiert")
    require(not gmail.created and not gmail.sent, "unbekannter Empfaenger wurde angeschrieben")


def t_the_existing_gmail_authority_is_unchanged():
    from solvio.capabilities import gmail
    source = inspect.getsource(gmail.register)
    require('"gmail_create_draft"' in source and '"gmail_send_draft"' in source,
            "bestehende Gmail-Faehigkeiten fehlen")


async def t_a_transport_failure_never_claims_sent_or_delivered():
    gmail, cap = _stack(); _confirm(cap); gmail.fail = True
    result = await cap.send({"alias": "mein Sohn", "channel": "gmail",
                             "recipient_handle": "sohn@example.test", "content": "Komm."})
    require_equal(result["delivery_truth"], "FAILED")
    require(result["delivery_truth"] in DELIVERY_TRUTHS, "offene Ergebnismenge")
    require(result["delivery_truth"] not in {"SENT_TO_PROVIDER", "DELIVERED"})


def t_untrusted_content_cannot_authorise_a_message():
    from solvio.capabilities.contract import authority_refusal
    from solvio.contracts.trust import TrustContext, TrustLevel
    spec = SPECS["communication_send"]
    refusal = authority_refusal(
        spec, TrustContext(TrustLevel.UNTRUSTED_EMAIL, user_authorized=True),
        spec.base_risk)
    require_equal(refusal, "untrusted_origin")


def t_the_natural_language_path_has_no_magic_keyword():
    source = inspect.getsource(__import__(
        "solvio.tools.communication_capability_tools", fromlist=["*"]))
    tree = ast.parse(source)
    magic = [node for node in ast.walk(tree) if isinstance(node, ast.Compare)
             and any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops)
             and any(isinstance(item, ast.Constant) and isinstance(item.value, str)
                     and item.value.lower() in {"sag", "schreib", "melde"}
                     for item in ast.walk(node))]
    require(not magic, "Werkzeugauswahl enthaelt einen Schluesselwortabgleich")
    require("sag, schreib oder melde" in _SCHEMAS["communication_resolve_recipient"]["description"])


async def t_only_available_bound_channels_are_offered():
    gmail, cap = _stack(); _confirm(cap, channel="sms")
    resolved = await cap.resolve_recipient({"alias": "mein Sohn"})
    require_equal(resolved["binding"]["handles"], [
        {"channel": "sms", "value": "sohn@example.test"}])
    try:
        await cap.send({"alias": "mein Sohn", "channel": "gmail",
                        "recipient_handle": "sohn@example.test", "content": "Komm."})
    except CapabilityRefused as exc:
        require_equal(exc.reason, "channel_unavailable")
    else:
        raise AssertionError("nicht gebundener Kanal akzeptiert")


async def t_no_secret_reaches_the_model_context():
    gmail, cap = _stack(); gmail.messages = [{"from": "Max <max@example.test>",
                                               "snippet": "token=SECRET"}]
    result = await cap.resolve_recipient({"alias": "Max"})
    require("SECRET" not in repr(result), "Mailtext gelangte in Kandidaten")


async def t_attachments_are_refused_not_silently_dropped():
    gmail, cap = _stack(); _confirm(cap)
    try:
        await cap.send({"alias": "mein Sohn", "channel": "gmail",
                        "recipient_handle": "sohn@example.test", "content": "Komm.",
                        "attachments": [{"name": "x.pdf"}]})
    except CapabilityRefused as exc:
        require_equal(exc.reason, "attachments_not_supported")
    else:
        raise AssertionError("Anhang still verworfen")
    require(not gmail.created and not gmail.sent, "Anhang ging in den Versandweg")


async def t_only_confirm_binding_creates_a_binding():
    gmail, cap = _stack()
    args = {
        "alias": "  Meine   Tochter ", "display_name": "Meine Tochter",
        "handles": [{"channel": "GMAIL", "value": "tochter@example.test"}],
        "source": "user_confirmed",
    }
    # Seit Contact Binding Authority Hardening V1 schreibt der Handler nur
    # innerhalb eines freigegebenen Vorgangs — und nur, wenn der Text aus
    # Wunsch und jetzigem Stand zu dem Digest passt, den das Geraet signiert
    # hat. Derselbe Weg, den der Router geht.
    from solvio.capabilities import execution_identity as EI
    from solvio.capabilities import policy as AP
    from solvio.capabilities.approval_gateway import approval_digest
    from solvio.secret_vault import context as SC
    digest = approval_digest(SPECS["communication_confirm_binding"],
                             cap.describe_confirm_binding(args),
                             AP.origin_label(SC.current().origin))
    with EI.bound(EI.ExecutionIdentity(execution_id="exec-t", idempotency_key="idem-t",
                                       approval_id="appr-t", action_digest=digest,
                                       capability="communication_confirm_binding")):
        result = await cap.confirm_binding(args)
    require(result["confirmed"], str(result))
    require_equal(cap.store.get("MEINE TOCHTER"), result["binding"])

    capability_source = inspect.getsource(
        __import__("solvio.capabilities.communication", fromlist=["*"]))
    require_equal(capability_source.count("self.store.confirm("), 1,
                  "mehr als ein Capability-Schreibpfad erzeugt Bindungen")
    store_tree = ast.parse(inspect.getsource(
        __import__("solvio.communication.bindings", fromlist=["*"])))
    binding_class = next(node for node in store_tree.body
                         if isinstance(node, ast.ClassDef) and node.name == "BindingStore")
    writers = []
    for method in (node for node in binding_class.body
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))):
        text = ast.get_source_segment(inspect.getsource(
            __import__("solvio.communication.bindings", fromlist=["*"])), method) or ""
        if "INSERT " in text or "UPDATE " in text or "DELETE " in text:
            writers.append(method.name)
    require_equal(writers, ["confirm"], "BindingStore hat einen zweiten Schreibpfad")


def t_communication_send_has_exactly_one_gmail_send_seam():
    source = inspect.getsource(CommunicationCapabilities.send)
    tree = ast.parse(textwrap.dedent(source))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    gmail_calls = [node.func.attr for node in calls
                   if isinstance(node.func, ast.Attribute)
                   and isinstance(node.func.value, ast.Attribute)
                   and isinstance(node.func.value.value, ast.Name)
                   and node.func.value.value.id == "self"
                   and node.func.value.attr == "gmail"]
    require_equal(gmail_calls, ["send_composed"])
    require(".provider" not in source, "communication_send greift direkt auf Provider zu")


def t_delivery_truth_is_closed_and_never_claims_delivered():
    require_equal(DELIVERY_TRUTHS,
                  frozenset({"QUEUED", "SENT_TO_PROVIDER", "DELIVERED",
                             "FAILED", "UNKNOWN"}))
    source = inspect.getsource(CommunicationCapabilities.send)
    tree = ast.parse(textwrap.dedent(source))
    produced = {node.value for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
                and node.value in DELIVERY_TRUTHS}
    require_equal(produced, {"SENT_TO_PROVIDER", "FAILED", "UNKNOWN"})
    require("DELIVERED" not in produced, "Gmail behauptet eine Zustellung")


def t_communication_registry_is_gated_by_gmail_provider():
    from solvio.tools import registry
    source = inspect.getsource(registry)
    gmail_gate = source.index("if gmail_provider is not None:")
    communication_register = source.index("register_communication_capabilities(")
    gmail_else = source.index("else:", gmail_gate)
    require(gmail_gate < communication_register < gmail_else,
            "Communication ist nicht innerhalb des Gmail-Provider-Gates registriert")


# ---------------------------------------------------------------------------
# Die Naht zur echten Gmail-Bindung
#
# Alle Faelle oben stellen Gmail als Attrappe — richtig fuer die Fragen, die
# sie stellen. Diese eine Frage kann eine Attrappe aber NICHT beantworten:
# haelt die exakte Bindung von `gmail_send_draft` auch dann, wenn sie ueber
# `communication_send` benutzt wird? Dafuer muss die echte Faehigkeit im Spiel
# sein, mit einer Attrappe eine Ebene tiefer — beim Postfach.
# ---------------------------------------------------------------------------

def _echter_stapel():
    """Der echte Gmail-Client ueber einer Gmail-REST-Nachbildung (`mail_send_harness`):
    Entwuerfe als MIME, „aktualisieren und senden" wie bei Gmail."""
    from mail_send_harness import GmailApi
    from solvio.capabilities.gmail import GmailCapabilities
    postfach = GmailApi()
    store = BindingStore(os.path.join(tempfile.mkdtemp(), "c.sqlite3"))
    cap = CommunicationCapabilities(GmailCapabilities(postfach), store)
    store.confirm("mein Sohn", "Adam",
                  [{"channel": "gmail", "value": "sohn@example.test"}], "test")
    return postfach, cap


async def t_a_draft_changed_after_approval_is_never_sent():
    """Zwischen Anlegen und Versenden aendert etwas anderes den Entwurf — ein zweiter
    Client, eine Synchronisierung. Seit ADR-0041 geht dann trotzdem nur der
    freigegebene Wortlaut hinaus: gesendet werden die Bytes aus der Freigabe
    („aktualisieren und senden"), nicht, was bei Gmail liegt."""
    from mail_send_harness import mime_with_attachment, sent_message
    postfach, cap = _echter_stapel()
    original = "Komm bitte nach Hause."

    def tamper(method, path):
        if method == "POST" and path == "/drafts/send":
            for draft_id in list(postfach.drafts):
                postfach.replace_draft(draft_id, mime_with_attachment(
                    sender="x@example.test", to="angreifer@example.test",
                    subject="(ohne Betreff)", body="Ueberweise 5000 Euro.", files=[]))

    postfach.before_response = tamper
    result = await cap.send({"alias": "mein Sohn", "channel": "gmail",
                             "recipient_handle": "sohn@example.test",
                             "content": original})
    require_equal(len(postfach.sent), 1)
    mail = sent_message(postfach.sent[0])
    require_equal(mail["To"], "sohn@example.test", "der veraenderte Empfaenger ging hinaus")
    require_equal(mail.get_content().strip(), original, "der veraenderte Text ging hinaus")
    require_equal(result["delivery_truth"], "SENT_TO_PROVIDER", str(result))
    require_equal(result["requested_content"], original,
                  "das Ergebnis nennt nicht den freigegebenen Wortlaut")


async def t_the_real_gmail_seam_sends_exactly_what_was_requested():
    """Die Gegenprobe. Ohne sie waere die Zusicherung oben auch mit einer
    Schicht zufrieden, die NIE etwas sendet."""
    postfach, cap = _echter_stapel()
    result = await cap.send({"alias": "mein Sohn", "channel": "gmail",
                             "recipient_handle": "sohn@example.test",
                             "content": "Komm bitte nach Hause."})
    require_equal(result["delivery_truth"], "SENT_TO_PROVIDER",
                  f"der gute Fall kommt nicht durch: {result}")
    require_equal(len(postfach.sent), 1, "genau ein Versand erwartet")
    from mail_send_harness import sent_message
    versandt = sent_message(postfach.sent[0])
    require_equal(versandt["To"], "sohn@example.test", "falscher Empfaenger")
    require_equal(versandt.get_content().strip(), "Komm bitte nach Hause.", "falscher Wortlaut")


def t_the_delivery_truth_guard_survives_optimised_python():
    """Eine Wache, die unter `python -O` verschwindet, ist keine.

    Sie stand hier zuerst als `assert`. Die Ergebnismenge ist aber genau die
    Stelle, an der ein erfundener Wert eine Aussage ueber Zustellung waere,
    die niemand geprueft hat.
    """
    quelle = inspect.getsource(CommunicationCapabilities.send)
    require("assert " not in quelle,
            "die Ergebnismenge haengt an einem assert")
    require("DELIVERY_TRUTHS" in quelle,
            "die Ergebnismenge wird gar nicht mehr geprueft")
    require_equal(DELIVERY_TRUTHS, frozenset(
        {"QUEUED", "SENT_TO_PROVIDER", "DELIVERED", "FAILED", "UNKNOWN"}),
        "die Ergebnismenge hat sich veraendert")


# ---------------------------------------------------------------------------
# C12 — die Zielphrase auf dem echten Autoritaetspfad
# ---------------------------------------------------------------------------

ZIELPHRASE = "Sag meinem Sohn, er soll nach Hause kommen."


def _autoritaetsstapel():
    from solvio.capabilities.invocation import CapabilityInvocationGate, voice_trust
    from solvio.capabilities.router import CapabilityRouter
    from solvio.capabilities.communication import register

    router = CapabilityRouter()
    gate = CapabilityInvocationGate()
    store = BindingStore(os.path.join(tempfile.mkdtemp(), "c12.sqlite3"))
    gmail = _Gmail()
    register(router, CommunicationCapabilities(gmail, store))
    return gmail, router, gate, voice_trust


async def _ruf(router, gate, voice_trust, name, args, *, origin):
    gate.begin_turn(session_id="s", turn_id="t", principal="pi", origin=origin,
                    trust=voice_trust(True), user_text=ZIELPHRASE)
    context = gate.context()
    return await router.execute(name, args, trust=context.trust,
                                provenance=gate.provenance_for(args),
                                principal=context.principal,
                                origin=context.origin,
                                commanded=context.commanded)


async def t_the_target_phrase_meets_a_named_authority_boundary():
    """Aus dem Raum heraus endet der Versand an der Freigabe — mit GRUND.

    Gemessen waehrend dieses Milestones: ohne Eintrag in der Aktionsklassen-
    Tabelle faellt die Faehigkeit auf `unclassified`. Das ist fail-closed und
    verlangt ebenfalls Face ID — nennt aber den falschen Grund. Der Nutzer
    erfuehre „nicht klassifiziert" statt „das ist folgenreich", und die Zeile,
    die es haette sagen sollen, fehlte still.

    Deshalb prueft diese Zusicherung den GRUND und nicht die Entscheidung.
    """
    from solvio.capabilities import policy as AP
    from solvio.capabilities.envelope import CapabilityOutcome

    gmail, router, gate, vt = _autoritaetsstapel()
    ergebnis = await _ruf(router, gate, vt, "communication_send",
                          {"alias": "mein Sohn", "channel": "gmail",
                           "recipient_handle": "x@example.test",
                           "content": "Komm bitte nach Hause."},
                          origin=AP.OriginClass.ROOM_VOICE)
    require_equal(ergebnis.outcome, CapabilityOutcome.REJECTED_BY_POLICY,
                  f"der Versand lief ohne Freigabe: {ergebnis.outcome} {ergebnis.reason}")
    require_equal(AP.base_class("communication_send", read_only=False),
                  AP.ActionClass.CRITICAL,
                  "der Versand ist nicht als folgenreich klassifiziert")
    # Die Bindung ist seit Contact Binding Authority Hardening V1 (DEBT-0208)
    # eine Stufe strenger als der Versand: VERY_CRITICAL, geboren.
    require_equal(AP.base_class("communication_confirm_binding", read_only=False),
                  AP.ActionClass.VERY_CRITICAL,
                  "die Bindung ist nicht als Identitaetsaenderung klassifiziert")
    require(not gmail.created and not gmail.sent,
            "vor der Freigabe wurde schon etwas angelegt oder gesendet")


async def t_the_target_phrase_reaches_the_contact_boundary_not_a_technical_error():
    """Und mit Freigabe endet sie an der fehlenden Bindung — nirgends vorher.

    Das ist der eigentliche Anspruch des Milestones: „Sag meinem Sohn …" soll
    an genau EINER Stelle stehen bleiben, naemlich daran, dass niemand
    bestaetigt hat, wer das ist. Nicht an einer fehlenden Registrierung, nicht
    an einem fehlenden Anbieter, nicht an einem Schema.
    """
    from solvio.capabilities import policy as AP
    from solvio.capabilities.envelope import CapabilityOutcome

    gmail, router, gate, vt = _autoritaetsstapel()

    # Seit ADR-0041 fragt jeder Versand Face ID — auch vom Telefon. Mit einem
    # Freigabeweg, der jede Anfrage mitschreibt: an einer fehlenden Bindung
    # darf der Mensch gar nicht erst gefragt werden.
    class _Mobile:
        def __init__(self):
            self.requests = []

        async def request(self, spec, shown, **kw):
            self.requests.append((spec.name, dict(shown)))
            return "ap-1"

    mobile = _Mobile()
    router._mobile = mobile

    # Lesen kommt durch — die Aufloesung ist die erste Haelfte der Phrase.
    aufl = await _ruf(router, gate, vt, "communication_resolve_recipient",
                      {"alias": "mein Sohn"},
                      origin=AP.OriginClass.ROOM_VOICE)
    require_equal(aufl.outcome, CapabilityOutcome.SUCCESS, str(aufl))
    require_equal(aufl.data["confirmed"], False,
                  "eine unbestaetigte Beziehung galt als bestaetigt")

    # Und der Versand endet an der Bindung, nicht an der Technik.
    ergebnis = await _ruf(router, gate, vt, "communication_send",
                          {"alias": "mein Sohn", "channel": "gmail",
                           "recipient_handle": "x@example.test",
                           "content": "Komm bitte nach Hause."},
                          origin=AP.OriginClass.TRUSTED_INTERACTIVE_APP)
    require("recipient_unknown" in f"{ergebnis.reason} {ergebnis.detail}",
            f"nicht die Kontaktgrenze: {ergebnis.outcome} "
            f"{ergebnis.reason!r} {ergebnis.detail!r}")
    require(ergebnis.outcome is not CapabilityOutcome.REJECTED_BY_POLICY,
            "die Freigabe hielt noch, statt an der Bindung zu enden")
    require(not gmail.created and not gmail.sent,
            "ohne bestaetigte Bindung wurde etwas angelegt oder gesendet")
    require_equal(mobile.requests, [],
                  "der Mensch wurde nach einem Versand gefragt, der danach ohnehin endet")



def t_the_binding_the_owner_confirms_is_readable():
    """Wer gemeint ist, muss lesbar dastehen — nicht als JSON.

    Auf dem iPhone stand `communication_confirm_binding ausfuehren` und darunter
    `handles: [{"channel": "phone", "value": "+44..."}]`. Genau hier entscheidet
    der Mensch aber, WEN ein Alias kuenftig meint — und davon haengt ab, wessen
    Telefon bei einem Anruf klingelt. Dieselbe Luecke wie DEBT-0206, eine
    Faehigkeit weiter.

    Seit DEBT-0208 steht auch der BISHERIGE Stand im Text. Die Rufnummern hier
    stammen aus dem von Ofcom fuer Fiktion reservierten Bereich.
    """
    from solvio.capabilities.approval_gateway import render_action
    from solvio.capabilities.communication import SPECS as C_SPECS

    _, cap = _stack()
    args = {"alias": "ich", "display_name": "Mara Muster",
            "handles": [{"channel": "phone", "value": "+447700900111"}],
            "source": "owner_confirmed_locally"}
    text = render_action(C_SPECS["communication_confirm_binding"],
                         cap.describe_confirm_binding(args), "Lokal am Rechner")
    require("Mara Muster" in text, "der Name fehlt im Freigabetext")
    require("+447700900111" in text, "die Rufnummer fehlt im Freigabetext")
    require("Telefon" in text, "der Kanal steht nicht in Worten da")
    require("ich" in text, "der Alias fehlt im Freigabetext")
    require("noch nicht gemerkt" in text, "eine erste Bindung sagt nicht, dass es keine gab")
    for roh in ("communication_confirm_binding", "handles", "display_name",
                "channel", "source", "kontakt_bisher", "kontakt_neu"):
        require(roh not in text, f"roher Argumentname im Freigabetext: {roh}")

    # Und beim Umhaengen stehen ALT und NEU da — beide unveraendert.
    cap.store.confirm("ich", "Mara Muster",
                      [{"channel": "phone", "value": "+447700900111"}], "test")
    neu = dict(args, handles=[{"channel": "phone", "value": "+447700900222"}])
    text = render_action(C_SPECS["communication_confirm_binding"],
                         cap.describe_confirm_binding(neu), "Lokal am Rechner")
    require("Bisher erreichbar über: \"Telefon: +447700900111\"" in text, text)
    require("Künftig erreichbar über: \"Telefon: +447700900222\"" in text, text)
    require("Stand:" in text, "die Fassung der bestehenden Bindung fehlt im Text")


def t_an_incomplete_binding_never_reaches_the_owner():
    """Eine Bindung, die niemand erfuellen kann, wird gar nicht erst gestellt."""
    _, cap = _stack()
    for luecke in ({"alias": "", "display_name": "Mara Muster",
                    "handles": [{"channel": "phone", "value": "+447700900111"}]},
                   {"alias": "ich", "display_name": "",
                    "handles": [{"channel": "phone", "value": "+447700900111"}]},
                   {"alias": "ich", "display_name": "Mara Muster", "handles": []},
                   {"alias": "ich", "display_name": "Mara Muster",
                    "handles": [{"channel": "phone", "value": "  "}]}):
        try:
            cap.describe_confirm_binding(luecke)
            require(False, f"unvollstaendige Bindung beschrieben: {luecke}")
        except CapabilityDeclined:
            pass


def t_the_describer_is_wired_into_the_router():
    """Ein gebauter und nie angemeldeter Beschreiber ist keiner."""
    import asyncio as _a

    from solvio.capabilities.communication import (
        CommunicationCapabilities, SPECS as C_SPECS, register as c_register)
    from solvio.capabilities.router import CapabilityRouter

    router = CapabilityRouter()
    c_register(router, CommunicationCapabilities(
        gmail=None, store=BindingStore(os.path.join(tempfile.mkdtemp(), "w.sqlite3"))))
    gezeigt = _a.run(router._describe(
        C_SPECS["communication_confirm_binding"],
        {"alias": "ich", "display_name": "Mara Muster",
         "handles": [{"channel": "phone", "value": "+447700900111"}]}))
    require("+447700900111" in str(gezeigt),
            "der Router beschreibt die Bindung ohne die Rufnummer")
    require("handles" not in gezeigt,
            "der Router reicht die Rohargumente durch")



def _bindeskript():
    """Laedt scripts/bind_owner_phone.py als Modul — es ist kein Paket."""
    import importlib.util
    pfad = os.path.join(os.path.dirname(__file__), "..", "scripts",
                        "bind_owner_phone.py")
    spec = importlib.util.spec_from_file_location("_bop", pfad)
    modul = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modul)
    return modul


def t_a_pending_approval_is_never_treated_as_a_rejection():
    """Die teuerste Verwechslung dieses Projekts, jetzt festgenagelt.

    Der eingefrorene Freigabepfad meldet „noch nicht freigegeben" und
    „abgelehnt" mit demselben Umschlag. Die erste Fassung von
    `bind_owner_phone.py` schloss daraus auf den Zustand, rief unmittelbar nach
    dem Stellen der Freigabe erneut auf und las `rejected_by_policy` als
    endgueltig — waehrend die Anfrage unberuehrt auf dem iPhone lag. Gemessen:
    `ap-6a3ba906…`, Zustand PENDING, 447 Sekunden Restzeit, null
    Ausfuehrungsversuche, Kontaktspeicher unveraendert.

    Derselbe Fehler steckte zwei Stunden zuvor im Beobachter des
    Tresor-Rescopes. Ein Ergebnisumschlag ist keine Auskunft ueber den Zustand
    einer Freigabe.
    """
    bop = _bindeskript()

    # Der Zustand wandert erst nach drei Blicken von PENDING nach APPROVED —
    # solange MUSS das Skript warten, obwohl der Umschlag „abgelehnt" sagt.
    zustaende = ["PENDING", "PENDING", "PENDING", "APPROVED"]
    gesehen = []

    async def _zustand(_id):
        gesehen.append(zustaende[min(len(gesehen), len(zustaende) - 1)])
        return gesehen[-1]

    versuche = []

    class _Client:
        def available(self):
            return True

        async def run(self, capability, arguments=None, *, approval_request_id=""):
            versuche.append(approval_request_id)
            if not approval_request_id:
                return {"ok": False, "outcome": "approval_required",
                        "data": {"request_id": "ap-probe"}}
            # GENAU der Umschlag, der die erste Fassung zum Abbruch brachte.
            if gesehen[-1] != "APPROVED":
                return {"ok": False, "outcome": "rejected_by_policy",
                        "reason": "not_approved",
                        "message": "Das ist noch nicht freigegeben."}
            return {"ok": True, "outcome": "success", "data": {}}

    bop._zustand = _zustand
    bop.ControlClient = lambda: _Client()
    bop.ABSTAND_SECS = 0

    ergebnis = asyncio.run(bop._binden("ich", "+49155000001111"))
    require(ergebnis is True,
            "ein PENDING wurde als Ablehnung gelesen — der Abbruch ist zurueck")
    require(len(gesehen) >= 4,
            f"das Skript hat nicht gewartet, sondern nach {len(gesehen)} Blicken "
            f"aufgegeben")
    require(all(a == "ap-probe" for a in versuche[1:]),
            "es wurde eine ZWEITE Freigabe erzeugt, statt die erste fortzusetzen")


def t_a_real_rejection_still_stops_immediately():
    """Warten darf nicht heissen: alles aussitzen."""
    bop = _bindeskript()

    async def _zustand(_id):
        return "DENIED"

    class _Client:
        def available(self):
            return True

        async def run(self, capability, arguments=None, *, approval_request_id=""):
            if not approval_request_id:
                return {"ok": False, "outcome": "approval_required",
                        "data": {"request_id": "ap-probe"}}
            require(False, "nach einer echten Ablehnung wurde trotzdem ausgefuehrt")

    bop._zustand = _zustand
    bop.ControlClient = lambda: _Client()
    bop.ABSTAND_SECS = 0
    require(asyncio.run(bop._binden("ich", "+49155000001111")) is False,
            "eine echte Ablehnung wurde nicht als solche erkannt")


def t_an_expired_approval_is_reported_as_expired_not_denied():
    """Abgelaufen ist etwas anderes als abgelehnt — und heilt durch Neustarten."""
    bop = _bindeskript()

    async def _zustand(_id):
        return "EXPIRED"

    class _Client:
        def available(self):
            return True

        async def run(self, capability, arguments=None, *, approval_request_id=""):
            if not approval_request_id:
                return {"ok": False, "outcome": "approval_required",
                        "data": {"request_id": "ap-probe"}}
            require(False, "eine abgelaufene Freigabe wurde ausgefuehrt")

    bop._zustand = _zustand
    bop.ControlClient = lambda: _Client()
    bop.ABSTAND_SECS = 0
    require(asyncio.run(bop._binden("ich", "+49155000001111")) is False,
            "eine abgelaufene Freigabe wurde als Erfolg gewertet")


def t_the_script_never_shows_the_number():
    """Die Nummer erscheint nirgends — auch nicht im Fehlerfall."""
    import inspect

    bop = _bindeskript()
    import ast

    quelle = inspect.getsource(bop)
    # Keine Ausgabe, die den eingelesenen WERT einsetzt. Gemessen wird am
    # Syntaxbaum, nicht an Wortvorkommen: eine Zeile, die das Wort „Nummer" in
    # deutscher Prosa enthaelt, ist keine Ausgabe der Nummer.
    heikel = {"nummer", "erste", "zweite"}
    for knoten in ast.walk(ast.parse(quelle)):
        if not (isinstance(knoten, ast.Call)
                and isinstance(knoten.func, ast.Name)
                and knoten.func.id == "print"):
            continue
        for teil in ast.walk(knoten):
            if isinstance(teil, ast.Name) and teil.id in heikel:
                require(False, f"das Skript gibt die Nummer aus (Zeile {knoten.lineno})")
            if isinstance(teil, ast.FormattedValue):
                for innen in ast.walk(teil):
                    if isinstance(innen, ast.Name) and innen.id in heikel:
                        require(False,
                                f"die Nummer steht in einer Ausgabe "
                                f"(Zeile {knoten.lineno})")
    require("getpass.getpass" in quelle, "die Nummer wird nicht verdeckt gelesen")
    require("read_only=True" in quelle,
            "der Freigabespeicher wird nicht nur lesend geoeffnet")



def t_no_approval_is_raised_before_the_phone_is_listening():
    """Ohne offene App verbrennt die Frist — also erst fragen, dann stellen.

    Es gibt keinen Push (DEBT-0029): die Zustellung ist Vordergrund-Polling.
    Die erste Fassung stellte die Freigabe sofort und meldete „liegt auf deinem
    iPhone" — waehrend die App zu war. Gemessen an den Zugriffen des Cores hat
    sie waehrend beider Freigabefenster KEIN einziges Mal gepollt; der erste
    Abruf danach kam 43 Sekunden nach Ablauf.

    Geprueft wird die Reihenfolge: der Wartepunkt MUSS vor dem ersten
    `ControlClient().run` liegen.
    """
    bop = _bindeskript()
    reihenfolge = []

    bop._nummer_einlesen = lambda: (reihenfolge.append("nummer"), "+49155000001111")[1]
    bop._app_muss_offen_sein = lambda: reihenfolge.append("wartepunkt")

    class _Client:
        def available(self):
            return True

        async def run(self, capability, arguments=None, *, approval_request_id=""):
            reihenfolge.append("freigabe_gestellt")
            return {"ok": True, "outcome": "success", "data": {}}

    bop.ControlClient = lambda: _Client()
    asyncio.run(bop.run(["ich"], ""))

    require("wartepunkt" in reihenfolge,
            "das Skript stellt eine Freigabe, ohne zu pruefen, ob jemand zuhoert")
    require(reihenfolge.index("wartepunkt") < reihenfolge.index("freigabe_gestellt"),
            f"der Wartepunkt kommt zu spaet: {reihenfolge}")


def t_the_expiry_message_names_the_real_cause():
    """Eine abgelaufene Freigabe muss sagen, WARUM — sonst rennt man hinein."""
    import inspect

    bop = _bindeskript()
    quelle = inspect.getsource(bop._warte_und_fuehre_aus)
    i = quelle.index("ABGELAUFEN")
    umfeld = quelle[i:i + 600]
    require("Vordergrund" in umfeld,
            "die Ablaufmeldung nennt die Ursache nicht")
    require("DEBT-0029" in umfeld,
            "die Ablaufmeldung verweist nicht auf die bekannte Schuld")


def t_the_script_never_claims_delivery_it_cannot_guarantee():
    """„Liegt auf deinem iPhone" war eine Zusage, die das Skript nicht halten kann."""
    import inspect

    import ast

    bop = _bindeskript()
    quelle = inspect.getsource(bop)
    # Nur AUSGABEN pruefen, nicht die ganze Quelle: der Docstring zitiert die
    # alte, falsche Meldung ausdruecklich, um sie zu erklaeren. Ein Test, der
    # das verbietet, verbietet die Begruendung.
    for knoten in ast.walk(ast.parse(quelle)):
        if not (isinstance(knoten, ast.Call)
                and isinstance(knoten.func, ast.Name)
                and knoten.func.id == "print"):
            continue
        for teil in ast.walk(knoten):
            if isinstance(teil, ast.Constant) and isinstance(teil.value, str):
                require("liegt auf deinem iPhone" not in teil.value,
                        f"das Skript behauptet eine Zustellung, die es nicht "
                        f"kennt (Zeile {knoten.lineno})")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
