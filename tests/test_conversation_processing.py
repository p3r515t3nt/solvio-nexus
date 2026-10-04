"""N8/C3 §3: der Prozessor — Routing, Antwort, Auftrag, Folgeanweisung, Neustart.

Echtes HTTPS (Browsersitzung, synthetisches iPhone), echte Buecher (Gespraeche,
Agenten, Kosten, Entscheidungen), echte Kostentore (Aktivitaet, Reservierung,
Claim) — nur der Anbieter ist eine Attrappe: ein Starter, der den Codex-Umschlag
zurueckgibt und dabei mitzaehlt, welcher Chat wie oft und wie gleichzeitig ruft.
Kein Modell, kein Netz, kein Produktionszustand.
"""
import asyncio
from contextlib import asynccontextmanager
import json
import os
import shutil
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()

from test_agent_task_entry import BODY
from test_conversation_endpoint import ChatWorld
from solvio.agent_runtime import cost_dispatch as D, cost_subjects as A, costs as C
from solvio.agent_runtime import requirements as RQ, result_files as F, store as S, task_revisions as TR
from solvio.cognition.ledger import CognitionLedger
from solvio.cognition.router import CognitiveRouter
from solvio.conversation import answer as AN, endpoint as CE, processing as PR
from solvio.specialists import launcher as L, providers as P, subscription as U

FREE = D.CostQuote(0, C.CostEvidence("free_local", "test:actual-local-cli"))
AUTH = P.ProviderStatus("codex", True, auth="chatgpt", billing_mode="subscription")


def assessment(weg, ziel, **extra):
    return {"weg": weg, "ziel": ziel, "zuversicht": 0.92, "schwierigkeit": "niedrig", **extra}


def codex_lines(reply):
    return "\n".join([json.dumps({"type": "thread.started", "thread_id": "local"}),
                      json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": reply}}),
                      json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}})])


class FakeCLI:
    """Der Starter hinter dem Abo-Transport — mit Buchfuehrung je Chat und Zustellung."""

    def __init__(self):
        self.assessments = []
        self.answers = []
        self.calls = []
        self.concurrent = {}
        self.peak = {}
        self.delay = 0.0
        self.hang = None
        self.override = None

    async def run(self, invocation, prompt):
        scope = D.current_scope()
        binding = scope.binding
        kind = "answer" if AN.ANSWER_MARKER in prompt else "assess"
        cid = binding.conversation_id
        self.concurrent[cid] = self.concurrent.get(cid, 0) + 1
        self.peak[cid] = max(self.peak.get(cid, 0), self.concurrent[cid])
        record = {"kind": kind, "conversation_id": cid, "delivery_id": binding.operation_key,
                  "prompt": prompt, "at": time.monotonic(), "ordinal": None}
        self.calls.append(record)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.hang is not None:
                await self.hang.wait()
            if self.override is not None:
                outcome = self.override(kind, record)
                if outcome is not None:
                    return outcome
            if kind == "assess":
                reply = json.dumps(self.assessments.pop(0) if self.assessments
                                   else assessment("kein_auftrag", ""))
            else:
                reply = self.answers.pop(0) if self.answers else "Gern — hier meine Antwort."
            return L.Outcome(True, text=codex_lines(reply), exit_code=0, process_started=True)
        finally:
            self.concurrent[cid] -= 1

    def of(self, kind, delivery_id=None):
        return [c for c in self.calls if c["kind"] == kind and (delivery_id is None or c["delivery_id"] == delivery_id)]


class ProcWorld(ChatWorld):
    async def open(self, folder):
        await super().open(folder)
        self.folder = folder
        # Der echte Prozessor — mit echtem Router, echtem Speicher, Abo-Transport-Attrappe.
        self.processor = self.real_processor
        self.processor._stopping = False
        self.app[CE.PROCESSOR_KEY] = self.processor
        self.bag = SimpleNamespace(conversations=self.chat_store, agent_runtime=self.orch,
                                   capabilities=self.router)
        self.cognition = CognitiveRouter(self.bag, mode="active",
                                         ledger=CognitionLedger(os.path.join(folder, "cognition.sqlite3")))
        self.app[CE.ROUTER_PROVIDER_KEY] = lambda: self.cognition
        self.cli = FakeCLI()
        self.transport = U.SubscriptionTransport(runner=self.cli.run)
        self.app[CE.TRANSPORT_PROVIDER_KEY] = lambda: self.transport
        self.orch.cost_quote_adapter = lambda *_: FREE
        return self

    async def settled(self, cid, delivery_id, timeout=15.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            row = self.chat_store.delivery(cid, delivery_id)
            if row is not None and row["status"] in ("completed", "blocked"):
                return row
            await asyncio.sleep(0.02)
        raise AssertionError(f"delivery {delivery_id} did not settle: {self.chat_store.delivery(cid, delivery_id)}")

    async def post(self, cid, text, *, client_message_id="msg-0001", **extra):
        res = await self.send(cid, text, client_message_id=client_message_id, **extra)
        require_equal(res.status, 202, await res.text())
        return (await res.json())["delivery_id"]

    async def ask(self, cid, text, *, client_message_id="msg-0001", **extra):
        delivery_id = await self.post(cid, text, client_message_id=client_message_id, **extra)
        return await self.settled(cid, delivery_id)

    def invocations(self, activity_id):
        with self.ledger._open() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM agent_provider_invocations WHERE activity_id=? ORDER BY ordinal", (activity_id,))]

    def activity(self, activity_id):
        with self.ledger._open() as db:
            row = db.execute("SELECT * FROM agent_cost_activities WHERE activity_id=?", (activity_id,)).fetchone()
        return dict(row) if row else None

    def tasks(self):
        with self.ledger._open() as db:
            return db.execute("SELECT COUNT(*) FROM agent_tasks").fetchone()[0]

    def second_processor(self, **kwargs):
        """Ein Neustart: neue Generation, dieselbe Laufzeit."""
        return PR.ConversationProcessor(self.processor._resolve, **kwargs)

    async def auth_session_id(self):
        from solvio.security.mobile_approval import browser_sessions as B
        cookie = self.client.session.cookie_jar.filter_cookies(self.client.make_url("/"))[B.COOKIE_NAME].value
        return (await self.sessions.authenticate(cookie)).session_id

    def gate_current(self):
        """`current()` anhalten koennen — damit eine zweite Nachricht VOR der Verarbeitung ankommt."""
        gate = asyncio.Event()
        original = self.processor.current

        async def gated(row, runtime):
            await gate.wait()
            return await original(row, runtime)
        self.processor.current = gated
        return gate


@asynccontextmanager
async def world():
    with tempfile.TemporaryDirectory(prefix="solvio-chat-processing-") as folder:
        with patch.dict(os.environ, {"SOLVIO_STATE_DIR": folder}):
            w = await ProcWorld().open(folder)

            def invocation(provider, *, workdir, **_):
                require_equal(provider, "codex")
                return L.Invocation(sys.executable, ("-c", "pass"), cwd=workdir, timeout=65)
            with patch.object(U, "text_invocation", invocation), \
                    patch.object(P, "codex_status", AsyncMock(return_value=AUTH)):
                try:
                    yield w
                finally:
                    await w.processor.wait_idle(20)
                    await w.close()


def _prompt(call):
    return call["prompt"]


# ---------------------------------------------------------------- Fall 1

async def t_01_a_question_creates_no_task_and_its_answer_is_persisted_only_after_a_finished_claim():
    async with world() as w:
        cid = await w.chat()
        w.cli.assessments.append(assessment("kein_auftrag", "Wie spaet ist es in Hamburg?"))
        w.cli.answers.append("In Hamburg ist es jetzt kurz nach drei.")
        row = await w.ask(cid, "Wie spaet ist es in Hamburg?")
        require_equal(row["status"], "completed", row["error_code"])
        require_equal(w.tasks(), 0, "a question became a task")
        messages = w.chat_store.messages(cid)
        require_equal([m["role"] for m in messages], ["user", "assistant"])
        require_equal(messages[1]["text"], "In Hamburg ist es jetzt kurz nach drei.")
        require_equal(messages[1]["source_turn_id"], row["delivery_id"])
        require_equal(row["assistant_message_id"], messages[1]["message_id"])
        require_equal([c["kind"] for c in w.cli.calls], ["assess", "answer"])
        # Zwei physische Claims derselben Aktivitaet, Zweck text_chat, beide beendet.
        claims = w.invocations(row["activity_id"])
        require_equal([(c["phase"], c["ordinal"], c["state"]) for c in claims],
                      [("text_chat", 1, "finished"), ("text_chat", 2, "finished")])
        require_equal(w.activity(row["activity_id"])["state"], "completed")
        # Das Subjekt gehoert der ZUSTELLUNG — und ist nicht der Chat.
        with w.ledger._open() as db:
            kind = db.execute("SELECT source_kind, conversation_id FROM agent_cost_subjects WHERE subject_id=?",
                              (w.activity(row["activity_id"])["subject_id"],)).fetchone()
        require_equal((kind[0], kind[1]), ("conversation_message", row["delivery_id"]))
        require_equal(w.activity(row["activity_id"])["source_kind"], "dashboard")
        # Der Antwortprompt traegt die Frage, den Rahmen — und keine Werkzeuge.
        prompt = _prompt(w.cli.of("answer")[0])
        require("Wie spaet ist es in Hamburg?" in prompt)
        require(AN.ANSWER_MARKER in prompt)
        # Das Entscheidungsbuch kennt den Turn als handed_back.
        decisions = w.cognition.ledger.recent(cid)
        require_equal([(d["turn_ref"], d["outcome"], d["route_final"]) for d in decisions],
                      [(row["delivery_id"], "handed_back", "kein_auftrag")])
        # Die Sicht ueber HTTP: Nachricht, Antwort, Zustellstand.
        detail = await (await w.client.get(CE.PREFIX + "/" + cid)).json()
        require_equal([m["role"] for m in detail["messages"]], ["user", "assistant"])
        require_equal(detail["messages"][0]["delivery"]["status"], "completed")
        require_equal(detail["deliveries_open"], 0)


async def t_01b_a_claim_that_the_ledger_does_not_confirm_never_yields_an_answer():
    """Antwortpersistenz erst nach geprueftem Claim: ein Transport, der `ok` sagt, aber keinen
    beendeten Claim dieser Aktivitaet hinterlaesst, schreibt keinen Assistententext."""
    async with world() as w:
        cid = await w.chat()
        w.cli.assessments.append(assessment("kein_auftrag", "Wie geht es dir?"))
        actual = w.transport

        async def forged(payload):
            result = await actual(payload)
            if AN.ANSWER_MARKER in payload["input"][0]["content"]:
                result = dict(result, cost_reservation_id="rs-does-not-exist")
            return result
        w.app[CE.TRANSPORT_PROVIDER_KEY] = lambda: forged
        row = await w.ask(cid, "Wie geht es dir?")
        require_equal((row["status"], row["error_code"]), ("blocked", "cost_recovery_required"))
        require_equal([m["role"] for m in w.chat_store.messages(cid)], ["user"])
        require_equal(w.activity(row["activity_id"])["state"], "cancelled")


async def t_01c_the_model_call_is_never_cut_by_the_processors_own_deadline():
    """Kein `wait_for` um den Abo-Transport: eine Antwort, die laenger dauert als die
    Frist der claimfreien Phasen, kommt trotzdem an — und wird nicht `unknown`."""
    async with world() as w:
        w.processor._prep_timeout = 0.2
        cid = await w.chat()
        w.cli.assessments.append(assessment("kein_auftrag", "Erzaehl mir etwas."))
        w.cli.delay = 0.45
        row = await w.ask(cid, "Erzaehl mir etwas.")
        require_equal((row["status"], row["error_code"]), ("completed", ""))
        require_equal([c["state"] for c in w.invocations(row["activity_id"])], ["finished", "finished"])


# ---------------------------------------------------------------- Fall 2

async def t_02_two_chats_on_two_devices_share_neither_clarification_nor_task_nor_grant():
    async with world() as w:
        ctx_a, app_a, headers_a = await w.device("local-owner", "device-a")
        ctx_b, app_b, headers_b = await w.device("second-owner", "device-b")
        chat_a = (await (await w.create("chat-a-0001", client=app_a, headers=headers_a)).json())["conversation_id"]
        chat_b = (await (await w.create("chat-b-0001", client=app_b, headers=headers_b)).json())["conversation_id"]

        async def app_send(ctx, client, headers, cid, text, cm, counter):
            message = {"conversation_id": cid, "client_message_id": cm, "text": text}
            payload = await w.app_proof(ctx, client, headers, message, counter=counter)
            res = await client.post(f"{CE.PREFIX}/{cid}/messages", json=payload, headers=headers)
            require_equal(res.status, 202, await res.text())
            return await w.settled(cid, (await res.json())["delivery_id"])
        # A: SOLVIO fragt zurueck.
        w.cli.assessments.append(assessment("klaerung", "Hotels vergleichen", klaerungsfrage="Fuer welche Stadt?"))
        row = await app_send(ctx_a, app_a, headers_a, chat_a, "Vergleiche mir bitte drei Hotels.", "message-a-1", 1)
        require_equal((row["status"], row["source_kind"]), ("completed", "app"))
        require_equal(w.chat_store.messages(chat_a)[-1]["text"], "Fuer welche Stadt?")
        require_equal(len(w.cli.of("answer")), 0, "a clarification costs no answer call")
        # B: die Einschaetzung von B sieht weder A's Rueckfrage noch A's Text.
        w.cli.assessments.append(assessment("kein_auftrag", "Wie ist das Wetter?"))
        row_b = await app_send(ctx_b, app_b, headers_b, chat_b, "Wie ist das Wetter?", "message-b-1", 1)
        require_equal(row_b["status"], "completed")
        prompt_b = _prompt(w.cli.of("assess", row_b["delivery_id"])[0])
        require("Fuer welche Stadt?" not in prompt_b)
        require("drei Hotels" not in prompt_b)
        # A antwortet auf die Rueckfrage: die Einschaetzung von A ist an A's Frage gebunden.
        objective = "Vergleiche mir bitte drei Hotels in Hamburg."
        w.cli.assessments.append(assessment("auftrag_recherche", objective))
        row_a2 = await app_send(ctx_a, app_a, headers_a, chat_a, "In Hamburg.", "message-a-2", 2)
        require_equal((row_a2["status"], row_a2["error_code"]), ("completed", ""), row_a2["error_code"])
        require(row_a2["task_id"].startswith("at-"))
        prompt_a2 = _prompt(w.cli.of("assess", row_a2["delivery_id"])[0])
        require("Fuer welche Stadt?" in prompt_a2 or "drei Hotels" in prompt_a2)
        task = w.ledger.get_task(row_a2["task_id"])
        require_equal((task.created_principal, task.created_origin, task.conversation_ref),
                      ("local-owner", "trusted_interactive_app", chat_a))
        # §3.5: der Auftrag sind A's persistierte Worte (Satz + Antwort), nie das Modellziel.
        require_equal(task.objective, "Vergleiche mir bitte drei Hotels. In Hamburg.")
        grant = w.orch.task_authority.for_run(row_a2["run_id"])
        require_equal((grant.receipt_method, grant.authorizer), ("app_session", "local-owner"))
        require(grant.receipt_reference.startswith("app:chat:"))
        require_equal([l["task_id"] for l in w.chat_store.task_links(chat_a)], [task.task_id])
        require_equal(w.chat_store.task_links(chat_b), [])
        # B's Register kennt A's Auftrag nicht — auch nach dem Start.
        w.cli.assessments.append(assessment("kein_auftrag", "Und sonst?"))
        row_b2 = await app_send(ctx_b, app_b, headers_b, chat_b, "Und sonst?", "message-b-2", 2)
        require(task.task_id not in _prompt(w.cli.of("assess", row_b2["delivery_id"])[0]))
        require(task.task_id not in _prompt(w.cli.of("answer", row_b2["delivery_id"])[0]))
        detail_b = await (await app_b.get(CE.PREFIX + "/" + chat_b, headers=headers_b)).json()
        require_equal(detail_b["auftraege"], [])
        require_equal((await app_b.get(CE.PREFIX + "/" + chat_a, headers=headers_b)).status, 404)
        require_equal(w.tasks(), 1)


# ---------------------------------------------------------------- Fall 3

async def t_03_duplicate_delivery_and_a_processor_restart_yield_one_message_one_activity_one_claim_set():
    async with world() as w:
        cid = await w.chat()
        w.cli.assessments.append(assessment("kein_auftrag", "Hallo SOLVIO"))
        first = await w.post(cid, "Hallo SOLVIO")
        second = await w.post(cid, "Hallo SOLVIO")
        require_equal(first, second)
        row = await w.settled(cid, first)
        require_equal(row["status"], "completed")
        before = (w.chat_store.messages(cid), w.chat_store.deliveries(cid), w.invocations(row["activity_id"]))
        # Neustart: neue Generation, dieselben Buecher — nichts wird noch einmal getan.
        restarted = w.second_processor()
        require_equal(await restarted.recover(), 0)
        await restarted.wait_idle(5)
        require_equal((w.chat_store.messages(cid), w.chat_store.deliveries(cid), w.invocations(row["activity_id"])), before)
        with w.ledger._open() as db:
            require_equal(db.execute("SELECT COUNT(*) FROM agent_cost_activities WHERE operation_key=?", (first,)).fetchone()[0], 1)
        # Ein Neustart, waehrend eine Zustellung noch `accepted` ist: der neue Prozessor
        # nimmt sie auf — genau einmal, auch wenn sie waehrenddessen erneut gesendet wird.
        w.processor._stopping = True
        w.cli.assessments.append(assessment("kein_auftrag", "Bist du noch da?"))
        third = await w.post(cid, "Bist du noch da?", client_message_id="msg-0002")
        require_equal(w.chat_store.delivery(cid, third)["status"], "accepted")
        replay = await w.post(cid, "Bist du noch da?", client_message_id="msg-0002")
        require_equal(replay, third)
        require_equal(await restarted.recover(), 1)
        row3 = await w.settled(cid, third)
        require_equal((row3["status"], row3["worker_generation"]), ("completed", restarted.worker_generation))
        require_equal([m["role"] for m in w.chat_store.messages(cid)], ["user", "assistant", "user", "assistant"])
        with w.ledger._open() as db:
            require_equal(db.execute("SELECT COUNT(*) FROM agent_cost_activities WHERE operation_key=?", (third,)).fetchone()[0], 1)
        require_equal(len(w.invocations(row3["activity_id"])), 2)
        require_equal(len(w.cli.of("assess", third)), 1)
        w.processor._stopping = False


# ---------------------------------------------------------------- Fall 4 + 15

async def t_04_a_lost_receipt_blocks_only_this_delivery_and_the_next_message_is_answered():
    async with world() as w:
        cid = await w.chat()
        w.cli.override = lambda kind, record: L.Outcome(False, reason="", exit_code=None, process_started=True)
        row = await w.ask(cid, "Wie wird das Wetter morgen?")
        require_equal((row["status"], row["error_code"]), ("blocked", "cost_recovery_required"))
        require_equal(len(w.cli.calls), 1, "a lost receipt must never trigger a second physical run")
        claims = w.invocations(row["activity_id"])
        require_equal([(c["ordinal"], c["state"]) for c in claims], [(1, "unknown")])
        activity = w.activity(row["activity_id"])
        require_equal(activity["state"], "cancelled")
        require_equal(activity["held_reason"], "", "cost_recovery_required is never a hold")
        require_equal([m["role"] for m in w.chat_store.messages(cid)], ["user"])
        require_equal(w.tasks(), 0)
        # Das Subjekt der ZUSTELLUNG ist gesperrt — nicht der Chat: die naechste Nachricht laeuft normal.
        w.cli.override = None
        w.cli.assessments.append(assessment("kein_auftrag", "Und uebermorgen?"))
        row2 = await w.ask(cid, "Und uebermorgen?", client_message_id="msg-0002")
        require_equal((row2["status"], row2["error_code"]), ("completed", ""))
        require_equal([c["state"] for c in w.invocations(row2["activity_id"])], ["finished", "finished"])
        require(row2["activity_id"] != row["activity_id"])
        # Fall 15: nach `blocked` laeuft das Downgrade OHNE `--core-stopped` durch — die
        # `unknown`-Invocation und die geschlossene Aktivitaet blockieren nicht.
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
        import cost_schema_downgrade_v4_to_v3 as DG
        with w.ledger._open() as db:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        copied = os.path.join(w.folder, "agent-copy.sqlite3")
        shutil.copyfile(w.ledger.path, copied)
        report = DG.downgrade(copied, core_stopped=False, dry_run=False)
        require_equal(report.get("version_after"), 3, str(report))
        require_equal(report.get("parked_invocations"), 3, str(report))
        require_equal(report.get("parked_activities"), 2, str(report))


async def t_04b_an_unexpected_failure_after_the_admit_still_closes_the_activity():
    """Der Catch-all des Prozessors kennt die Aktivitaet nur ueber den Store: `row` stammt
    vom Claim-Zeitpunkt, `_admit` legt die Aktivitaet erst danach an. Eine unerwartete
    Ausnahme hinter dem Admit (hier: das Entscheidungsbuch) endet `blocked/processing_failed`
    UND schliesst die Aktivitaet — sonst verweigert das Downgrade bis zu 900 s."""
    async with world() as w:
        cid = await w.chat()
        w.cli.assessments.append(assessment("kein_auftrag", "Wie spaet ist es?"))
        w.cli.answers.append("nie erreicht")

        def broken(*_args, **_kwargs):
            raise RuntimeError("synthetic ledger failure")
        with patch.object(w.cognition, "record_text_decision", broken):
            row = await w.ask(cid, "Wie spaet ist es?")
        require_equal((row["status"], row["error_code"]), ("blocked", PR.ERROR_FAILED))
        require(row["activity_id"], "the admitted activity must be persisted on the delivery")
        activity = w.activity(row["activity_id"])
        require_equal(activity["state"], "cancelled", "a terminal delivery must not leave an open activity")
        require_equal([c["state"] for c in w.invocations(row["activity_id"])], ["finished"])
        require_equal([m["role"] for m in w.chat_store.messages(cid)], ["user"])
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
        import cost_schema_downgrade_v4_to_v3 as DG
        with w.ledger._open() as db:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        copied = os.path.join(w.folder, "agent-copy-04b.sqlite3")
        shutil.copyfile(w.ledger.path, copied)
        report = DG.downgrade(copied, core_stopped=False, dry_run=True)
        require_equal(report.get("refused"), None, str(report))


async def t_04c_an_orphan_whose_activity_expired_without_any_claim_is_named_a_restart_not_a_cost_doubt():
    """Absturz nach dem Admit, vor dem ersten Claim; Neustart erst nach der Lebensdauer der
    Aktivitaet. Kein Modellaufruf, keine Invocation-Zeile — der ehrliche Code ist
    `core_restarted_unresolved`, nicht „Der Ausgang einer Kostenbuchung ist ungewiss"."""
    async with world() as w:
        cid = await w.chat()
        crashed = asyncio.Event()

        async def crashing_classify(*_args, **_kwargs):
            crashed.set()
            raise asyncio.CancelledError()
        with patch.object(PR, "ACTIVITY_LIFETIME_SECONDS", 1), \
                patch.object(w.cognition, "classify_text", crashing_classify):
            delivery_id = await w.post(cid, "Wie spaet ist es?")
            await asyncio.wait_for(crashed.wait(), 10)
            await w.processor.wait_idle(5)
        row = w.chat_store.delivery(cid, delivery_id)
        require_equal((row["status"], row["dispatch"]), ("running", ""))
        require(row["activity_id"], "the admitted activity is persisted before the crash")
        require_equal(w.invocations(row["activity_id"]), [], "no claim happened before the crash")
        await asyncio.sleep(1.3)                      # die Aktivitaet laeuft ab
        restarted = w.second_processor()
        require_equal(await restarted.recover(), 1)
        row = await w.settled(cid, delivery_id)
        require_equal((row["status"], row["error_code"]), ("blocked", PR.ERROR_RESTARTED))
        require_equal(len(w.cli.calls), 0, "no physical run may follow an expired orphan")
        require_equal(w.invocations(row["activity_id"]), [])
        require_equal(w.activity(row["activity_id"])["state"], "cancelled")


async def t_04d_the_task_objective_is_the_users_persisted_text_never_the_assessors_paraphrase():
    """§3.5: der Auftragstext geht unveraendert hinaus. Der Einschaetzer darf sein Ziel
    umformulieren (die Ueberlappungsregel laesst 30 % fremde Worte durch) — an den
    Auftragsstart gebunden und im Buch sichtbar ist trotzdem der Satz des Nutzers.
    Nach einer Rueckfrage sind es der Satz, auf den zurueckgefragt wurde, plus die
    Antwort; SOLVIOs Frage gehoert nie dazu."""
    async with world() as w:
        # Ohne Rueckfrage: eine Nachdichtung, die die Ueberlappungsregel PASSIERT (7 von 9
        # Worten sind die des Nutzers, 0.78 >= 0.7) und zwei eigene Worte anhaengt —
        # genau die darf der Auftrag nicht tragen.
        cid = await w.chat()
        spoken = "Vergleiche mir bitte drei Hotels in Hamburg mit Preisen."
        paraphrase = "Vergleiche drei Hotels in Hamburg mit Preisen und Buchungslink"
        from solvio.cognition import policy as POL
        require(POL.overlap(paraphrase, spoken) >= 0.7, "the probe must survive the overlap rule")
        w.cli.assessments.append(assessment("auftrag_recherche", paraphrase))
        row = await w.ask(cid, spoken)
        require_equal((row["status"], row["error_code"]), ("completed", ""), row["error_code"])
        task = w.ledger.get_task(row["task_id"])
        require_equal(task.objective, spoken)
        require_equal(json.loads(row["dispatch"])["objective"], spoken)
        require(paraphrase not in json.dumps(w.orch.task_authority.for_run(row["run_id"]).__dict__, default=str),
                "the assessor's words must not be bound to the start")
        # Mit Rueckfrage: Satz + Antwort, beides Nutzertext, die Frage nicht.
        cid2 = (await (await w.create("chat-0002")).json())["conversation_id"]
        w.cli.assessments.append(assessment("klaerung", "Hotel suchen", klaerungsfrage="Fuer welche Stadt?"))
        asked = await w.ask(cid2, "Such mir ein gutes Hotel.")
        require_equal(w.chat_store.messages(cid2)[-1]["text"], "Fuer welche Stadt?")
        w.cli.assessments.append(assessment("auftrag_recherche", "Ein gutes Hotel in Kiel suchen."))
        row2 = await w.ask(cid2, "In Kiel.", client_message_id="msg-0002")
        require_equal((row2["status"], row2["error_code"]), ("completed", ""), row2["error_code"])
        task2 = w.ledger.get_task(row2["task_id"])
        require_equal(task2.objective, "Such mir ein gutes Hotel. In Kiel.")
        require("Fuer welche Stadt?" not in task2.objective)
        # Eine zweite Antwort nach erledigter Rueckfrage bindet nichts Altes mehr an.
        w.cli.assessments.append(assessment("auftrag_recherche", "Parkplaetze in Kiel pruefen"))
        row3 = await w.ask(cid2, "Pruefe bitte auch die Parkplaetze dort.", client_message_id="msg-0003")
        require_equal(row3["status"], "completed", row3["error_code"])
        require_equal(w.ledger.get_task(row3["task_id"]).objective, "Pruefe bitte auch die Parkplaetze dort.")
        # Zwei Rueckfragen hintereinander: die GANZE Kette, in Reihenfolge (Review Runde 2, F-5).
        cid3 = (await (await w.create("chat-0003")).json())["conversation_id"]
        w.cli.assessments.append(assessment("klaerung", "Hotel", klaerungsfrage="Fuer welche Stadt?"))
        await w.ask(cid3, "Such mir ein gutes Hotel.")
        w.cli.assessments.append(assessment("klaerung", "Hotel Kiel", klaerungsfrage="Fuer welchen Tag?"))
        await w.ask(cid3, "In Kiel.", client_message_id="msg-0002")
        w.cli.assessments.append(assessment("auftrag_recherche", "Hotel in Kiel fuer morgen suchen"))
        chained = await w.ask(cid3, "Morgen bitte.", client_message_id="msg-0003")
        require_equal((chained["status"], chained["error_code"]), ("completed", ""), chained["error_code"])
        require_equal(w.ledger.get_task(chained["task_id"]).objective, "Such mir ein gutes Hotel. In Kiel. Morgen bitte.")
        # Eine abgewiesene Ueberlaenge wird durch die Kurzfassung ERSETZT, nie verkettet (F-1).
        cid4 = (await (await w.create("chat-0004")).json())["conversation_id"]
        long = ("Bitte recherchiere ausfuehrlich " * 80).strip()
        w.cli.assessments.append(assessment("auftrag_recherche", long))
        rejected = await w.ask(cid4, long)
        require_equal(json.loads(rejected["dispatch"])["action_class"], "objective_too_long")
        short = "Vergleiche mir bitte drei Hotels in Hamburg mit Preisen."
        w.cli.assessments.append(assessment("auftrag_recherche", short))
        replaced = await w.ask(cid4, short, client_message_id="msg-0002")
        require_equal((replaced["status"], json.loads(replaced["dispatch"])["action_class"]), ("completed", "auftrag"),
                      replaced["error_code"])
        require_equal(w.ledger.get_task(replaced["task_id"]).objective, short)
        w.cli.assessments.append(assessment("auftrag_recherche", short))
        third = await w.ask(cid4, short, client_message_id="msg-0003")
        require_equal(json.loads(third["dispatch"])["objective"], short, "the third message chained the second")


async def t_04e_a_clarification_asked_by_voice_in_this_chat_is_part_of_the_chain_and_a_missing_link_ends_the_delivery():
    """Review Runde 3, F3-1: eine an den Chat gebundene Sprachsitzung bucht ihre Rueckfrage mit dem
    Sprach-Turn als `turn_ref` — keine Zustellung. Die Antwort per Text traegt die gesprochenen
    Worte mit (aus dem Verlauf, `source_turn_id`). Fehlt ein Glied wirklich, endet die Zustellung
    terminal (`processing_failed`), statt fuer immer `running` zu bleiben."""
    from solvio.cognition.types import EscalationEvent
    async with world() as w:
        cid = await w.chat()
        w.cognition.record_text_decision(conversation_ref=cid, turn_ref="turn-voice-0001",
            origin="trusted_interactive_app", user_text="Such mir ein Hotel.",
            route_final="klaerung", outcome="clarification", event=EscalationEvent.NONE)
        w.chat_store.add_message(cid, "user", "Such mir ein Hotel.", source_session_id="s-voice",
                                 source_turn_id="turn-voice-0001")
        w.chat_store.add_message(cid, "assistant", "Fuer welche Stadt?", source_session_id="s-voice",
                                 source_turn_id="turn-voice-0001")
        w.cli.assessments.append(assessment("auftrag_recherche", "Ein Hotel in Kiel suchen."))
        row = await w.ask(cid, "In Kiel bitte, drei Vorschlaege.")
        require_equal((row["status"], row["error_code"]), ("completed", ""), row["error_code"])
        require_equal(w.ledger.get_task(row["task_id"]).objective, "Such mir ein Hotel. In Kiel bitte, drei Vorschlaege.")
        # Ein Glied ohne Zustellung UND ohne Verlaufszeile (Barge-in-Fehlablage): SOLVIO fragt
        # ehrlich neu, die offene Rueckfrage ist zurueckgesetzt, kein halber Auftrag, und die
        # naechste Auftragsnachricht laeuft wieder (Review Runde 4, R4-F-1).
        cid2 = (await (await w.create("chat-0002")).json())["conversation_id"]
        w.cognition.record_text_decision(conversation_ref=cid2, turn_ref="turn-voice-gone",
            origin="trusted_interactive_app", user_text="", route_final="klaerung",
            outcome="clarification", event=EscalationEvent.NONE)
        w.cli.assessments.append(assessment("auftrag_recherche", "Ein Hotel in Kiel suchen."))
        lost = await w.ask(cid2, "In Kiel bitte, drei Vorschlaege.")
        require_equal((lost["status"], lost["error_code"], lost["task_id"]), ("completed", "", ""))
        require_equal(json.loads(lost["dispatch"])["action_class"], PR.ACTION_CHAIN_LOST)
        require_equal(w.chat_store.messages(cid2)[-1]["text"], PR.CHAIN_LOST_TEXT)
        require_equal(w.chat_store.open_delivery_count(cid2), 0)
        require_equal(w.tasks(), 1, "the lost link created a task")
        w.cli.assessments.append(assessment("auftrag_recherche", "Ein Hotel in Kiel suchen."))
        again = await w.ask(cid2, "Such mir ein Hotel in Kiel, drei Vorschlaege.", client_message_id="msg-0002")
        require_equal((again["status"], again["error_code"]), ("completed", ""), again["error_code"])
        require_equal(w.ledger.get_task(again["task_id"]).objective, "Such mir ein Hotel in Kiel, drei Vorschlaege.")
        # Ein vorhandenes Sprach-Glied, aelter als das 40-Zeilen-Fenster, wird trotzdem gefunden (R4-F-1).
        cid3 = (await (await w.create("chat-0003")).json())["conversation_id"]
        w.cognition.record_text_decision(conversation_ref=cid3, turn_ref="s-v-t1",
            origin="trusted_interactive_app", user_text="Such mir ein Hotel.", route_final="klaerung",
            outcome="clarification", event=EscalationEvent.NONE)
        w.chat_store.add_message(cid3, "user", "Such mir ein Hotel.", source_session_id="s-voice", source_turn_id="s-v-t1")
        w.chat_store.add_message(cid3, "assistant", "Fuer welche Stadt?", source_session_id="s-voice", source_turn_id="s-v-t1")
        for i in range(25):
            w.chat_store.add_message(cid3, "user", f"Smalltalk {i}", source_session_id="s-voice", source_turn_id=f"s-v-t{i + 2}")
            w.chat_store.add_message(cid3, "assistant", f"Antwort {i}", source_session_id="s-voice", source_turn_id=f"s-v-t{i + 2}")
        w.cli.assessments.append(assessment("auftrag_recherche", "Ein Hotel in Kiel suchen."))
        deep = await w.ask(cid3, "In Kiel bitte, drei Vorschlaege.")
        require_equal((deep["status"], deep["error_code"]), ("completed", ""), deep["error_code"])
        require_equal(w.ledger.get_task(deep["task_id"]).objective, "Such mir ein Hotel. In Kiel bitte, drei Vorschlaege.")
        # Text-Rueckfrage → Sprach-Antwort mit erneuter Rueckfrage → Text-Antwort: die GANZE Kette (R4-F-2).
        cid4 = (await (await w.create("chat-0004")).json())["conversation_id"]
        w.cli.assessments.append(assessment("klaerung", "Hotel", klaerungsfrage="Fuer welche Stadt?"))
        await w.ask(cid4, "Such mir ein gutes Hotel.")
        w.cognition.record_text_decision(conversation_ref=cid4, turn_ref="s-v-t9",
            origin="trusted_interactive_app", user_text="In Kiel.", route_final="klaerung",
            outcome="clarification", event=EscalationEvent.NONE)
        w.chat_store.add_message(cid4, "user", "In Kiel.", source_session_id="s-voice", source_turn_id="s-v-t9")
        w.chat_store.add_message(cid4, "assistant", "Fuer welchen Tag?", source_session_id="s-voice", source_turn_id="s-v-t9")
        w.cli.assessments.append(assessment("auftrag_recherche", "Hotel Kiel Freitag drei Vorschlaege"))
        mixed = await w.ask(cid4, "Am Freitag, drei Vorschlaege.", client_message_id="msg-0002")
        require_equal((mixed["status"], mixed["error_code"]), ("completed", ""), mixed["error_code"])
        require_equal(w.ledger.get_task(mixed["task_id"]).objective,
                      "Such mir ein gutes Hotel. In Kiel. Am Freitag, drei Vorschlaege.")
        # Nur eine RUECKFRAGE ist ein aelteres Glied: eine beantwortete Frage dazwischen kettet nicht
        # (Review Runde 5, R5-H-3 M9) — und die Kette liest die Sequenz, nie spaetere Zeilen (M7).
        cid5 = (await (await w.create("chat-0005")).json())["conversation_id"]
        w.cli.assessments.append(assessment("kein_auftrag", "Wie spaet ist es?"))
        w.cli.answers.append("Kurz vor zwoelf.")
        await w.ask(cid5, "Wie spaet ist es?")
        w.cognition.record_text_decision(conversation_ref=cid5, turn_ref="s-v-t5",
            origin="trusted_interactive_app", user_text="In Kiel.", route_final="klaerung",
            outcome="clarification", event=EscalationEvent.NONE)
        w.chat_store.add_message(cid5, "user", "In Kiel.", source_session_id="s-voice", source_turn_id="s-v-t5")
        w.chat_store.add_message(cid5, "assistant", "Fuer welchen Tag?", source_session_id="s-voice", source_turn_id="s-v-t5")
        w.cli.assessments.append(assessment("auftrag_recherche", "Kiel Freitag drei Vorschlaege"))
        lone = await w.ask(cid5, "Am Freitag, drei Vorschlaege.", client_message_id="msg-0002")
        require_equal((lone["status"], lone["error_code"]), ("completed", ""), lone["error_code"])
        require_equal(w.ledger.get_task(lone["task_id"]).objective, "In Kiel. Am Freitag, drei Vorschlaege.",
                      "an answered question was chained into the order")
        # Eine Zeile unter derselben Sprach-Turn-Kennung, die NACH der Zustellung ankommt (Barge-in
        # waehrend der Verarbeitung), gehoert nicht zur Kette — der Prozessor bindet an die Sequenz
        # der lesenden Zustellung, nicht nur der Store (M7, Review Runde 6 R6-H-1).
        w.cognition.record_text_decision(conversation_ref=cid5, turn_ref="s-v-t8",
            origin="trusted_interactive_app", user_text="In Luebeck.", route_final="klaerung",
            outcome="clarification", event=EscalationEvent.NONE)
        w.chat_store.add_message(cid5, "user", "In Luebeck.", source_session_id="s-voice", source_turn_id="s-v-t8")
        w.chat_store.add_message(cid5, "assistant", "Fuer welchen Tag?", source_session_id="s-voice", source_turn_id="s-v-t8")
        gate = w.gate_current()
        w.cli.assessments.append(assessment("auftrag_recherche", "Luebeck Samstag zwei Vorschlaege"))
        late_id = await w.post(cid5, "Am Samstag, zwei Vorschlaege.", client_message_id="msg-0003")
        w.chat_store.add_message(cid5, "user", "Nachtrag spaeter", source_session_id="s-voice", source_turn_id="s-v-t8")
        gate.set()
        late = await w.settled(cid5, late_id)
        require_equal((late["status"], late["error_code"]), ("completed", ""), late["error_code"])
        require_equal(w.ledger.get_task(late["task_id"]).objective, "In Luebeck. Am Samstag, zwei Vorschlaege.",
                      "a line persisted after the delivery was chained into the order")
        # Drei Glieder ueber zwei Sprach-Turns und eine Text-Rueckfrage (M10: das Buchfenster traegt die Kette).
        cid6 = (await (await w.create("chat-0006")).json())["conversation_id"]
        w.cli.assessments.append(assessment("klaerung", "Hotel", klaerungsfrage="Fuer welche Stadt?"))
        await w.ask(cid6, "Such mir ein gutes Hotel.")
        for turn, text_, question in (("s-v-t61", "In Kiel.", "Fuer welchen Tag?"), ("s-v-t62", "Am Freitag.", "Wie viele Vorschlaege?")):
            w.cognition.record_text_decision(conversation_ref=cid6, turn_ref=turn, origin="trusted_interactive_app",
                user_text=text_, route_final="klaerung", outcome="clarification", event=EscalationEvent.NONE)
            w.chat_store.add_message(cid6, "user", text_, source_session_id="s-voice", source_turn_id=turn)
            w.chat_store.add_message(cid6, "assistant", question, source_session_id="s-voice", source_turn_id=turn)
        w.cli.assessments.append(assessment("auftrag_recherche", "Hotel Kiel Freitag drei"))
        three = await w.ask(cid6, "Drei bitte.", client_message_id="msg-0002")
        require_equal((three["status"], three["error_code"]), ("completed", ""), three["error_code"])
        require_equal(w.ledger.get_task(three["task_id"]).objective, "Such mir ein gutes Hotel. In Kiel. Am Freitag. Drei bitte.")
        # Ein Sprach-Glied, das nur den Tresor-Platzhalter traegt, ist kein Auftragskopf (R5-H-1).
        from solvio.secret_vault.firewall import TRANSCRIPT_MARKER
        cid7 = (await (await w.create("chat-0007")).json())["conversation_id"]
        w.cognition.record_text_decision(conversation_ref=cid7, turn_ref="s-v-t7", origin="trusted_interactive_app",
            user_text="", route_final="klaerung", outcome="clarification", event=EscalationEvent.NONE)
        w.chat_store.add_message(cid7, "user", "Mein Amazon-Passwort ist Hund1234", source_session_id="s-voice", source_turn_id="s-v-t7")
        require_equal(w.chat_store.messages(cid7)[-1]["text"], TRANSCRIPT_MARKER)
        w.cli.assessments.append(assessment("auftrag_recherche", "Ein Hotel in Kiel suchen."))
        fenced = await w.ask(cid7, "In Kiel bitte, drei Vorschlaege.")
        require_equal((fenced["status"], fenced["task_id"]), ("completed", ""))
        require_equal(json.loads(fenced["dispatch"])["action_class"], PR.ACTION_CHAIN_LOST)


async def t_04f_a_credential_is_fenced_before_the_model_and_neither_routed_nor_titled():
    """Review Runde 4, B-H2: der Tresor-Zaun ersetzt Zugangsdaten durch den Platzhalter. Der
    Platzhalter wird weder geroutet (kein Modellaufruf) noch beantwortet noch Chattitel —
    eine feste Auskunft, Zustellung `completed`, kein Auftrag."""
    from solvio.secret_vault.firewall import TRANSCRIPT_MARKER
    async with world() as w:
        cid = await w.chat()
        row = await w.ask(cid, "Mein Amazon-Passwort ist Hund1234")
        require_equal((row["status"], row["error_code"], row["task_id"]), ("completed", "", ""))
        messages = w.chat_store.messages(cid)
        require_equal([m["role"] for m in messages], ["user", "assistant"])
        require_equal(messages[0]["text"], TRANSCRIPT_MARKER)
        require_equal(messages[1]["text"], PR.CREDENTIAL_TEXT)
        require_equal(len(w.cli.calls), 0, "the placeholder reached a model")
        require_equal(w.chat_store.conversation(cid)["title"], "", "the placeholder became the title")
        require("Hund1234" not in json.dumps(w.chat_store.conversation(cid)) + json.dumps(messages))
        # Die naechste normale Nachricht traegt den Titel und laeuft wie gewohnt.
        w.cli.assessments.append(assessment("kein_auftrag", "Wie spaet ist es?"))
        w.cli.answers.append("Kurz vor zwoelf.")
        again = await w.ask(cid, "Wie spaet ist es?", client_message_id="msg-0002")
        require_equal(again["status"], "completed", again["error_code"])
        require_equal(w.chat_store.conversation(cid)["title"], "Wie spaet ist es?")


async def t_04g_the_typed_message_fence_knows_the_forms_the_worker_fence_knows():
    """Review round 13, B13-1: the typed-message fence was only the statement heuristic;
    the forms the rounds 9-12 taught the worker fence — a quoted value with spaces, URL
    userinfo, `-u user:pw` — reached history, routing prompt and task objective in
    cleartext. Now the same line fence runs on what the owner types: the value line
    becomes the marker, the other lines stay, and a single-line credential message is
    still the fixed answer without any model call."""
    from solvio.secret_vault.firewall import TRANSCRIPT_MARKER
    async with world() as w:
        cid = await w.chat()
        row = await w.ask(cid, 'Trag bitte PASSWORD="mein geheimes pass" in die .env ein')
        require_equal((row["status"], row["task_id"]), ("completed", ""))
        messages = w.chat_store.messages(cid)
        require_equal(messages[0]["text"], TRANSCRIPT_MARKER)
        require_equal(messages[1]["text"], PR.CREDENTIAL_TEXT)
        require_equal(len(w.cli.calls), 0, "the quoted credential reached a model")
        w.cli.assessments.append(assessment("kein_auftrag", "Bitte pruefen."))
        w.cli.answers.append("Geprueft.")
        multi = await w.ask(cid, "Bitte die Verbindung pruefen:\npostgres://app:pw12345@db.example.net/prod\nund dann Bescheid geben.",
                            client_message_id="msg-0002")
        require_equal(multi["status"], "completed", multi["error_code"])
        stored = w.chat_store.messages(cid)[2]["text"]
        require("pw12345" not in stored and TRANSCRIPT_MARKER in stored and "Bescheid geben" in stored, stored)
        require("pw12345" not in json.dumps(w.cli.calls), "the userinfo reached the routing prompt")
        require("pw12345" not in json.dumps(w.chat_store.conversation(cid)), "the userinfo became the title")
        # Review round 15, B15-1/R15-1: the shared history write path keeps the ASR-calibrated
        # heuristic of the memory (voice transcripts arrive here too) — spoken and typed
        # statements without a token-like value stay fenced as in production …
        for spoken in ("mein passwort ist sonnenblume", "die TAN ist 482913", "der api key ist k9912abc",
                       # round 17, K17-1: a bare bearer value without a digit, as production fences it
                       "Bearer abcdefghijklmnop", "Nutze Bearer AbCdEfGhIjKlMnOpQrSt fuer die API",
                       # round 20, K20-3: a typed SecretRef is fenced as in production (DEBT-0295: the price)
                       "secret://amazon/password", "Nimm secret://amazon/password fuer die Anmeldung",
                       # the six sentences the candidate of round 14 stored in clear text (R15-1, measured)
                       "Mein Passwort lautet: Sommer2024x", "Mein Passwort ist sonnenblume, merk dir das",
                       "Das Passwort fuer Amazon ist uebrigens Hund1234", "Bitte aendere das Passwort auf Winter2025!",
                       "Der API Key ist abcdefghijklmnopqrstuvwxyz", "Meine Geheimzahl ist 4711"):
            w.chat_store.add_message(cid, "user", spoken, source_session_id="s-voice", source_turn_id="s-v-" + spoken[:8])
            require_equal(w.chat_store.messages(cid)[-1]["text"], TRANSCRIPT_MARKER, spoken)
        # … which means a typed knowledge QUESTION with a credential term and "ist" still gets the
        # fixed credential answer without a model call (DEBT-0295, a decision for the architect,
        # not a weakening): measured, not hidden.
        asked = await w.ask(cid, "Was ist der Unterschied zwischen Basic Auth und Bearer Tokens?", client_message_id="msg-0003")
        require_equal((asked["status"], w.chat_store.messages(cid)[-1]["text"]), ("completed", PR.CREDENTIAL_TEXT))
        # A statement WITH a value stays fenced, also as a typed title (B14-H1).
        stated = await w.ask(cid, "Mein Passwort lautet Winterzeit99", client_message_id="msg-0004")
        require_equal((stated["status"], w.chat_store.messages(cid)[-2]["text"]), ("completed", TRANSCRIPT_MARKER))
        w.chat_store.update_title(cid, "Zugang postgres://app:pw12345@db/x")
        require("pw12345" not in json.dumps(w.chat_store.conversation(cid)), "the userinfo became the title")


# ---------------------------------------------------------------- Fall 5

async def _crash(exc_type=asyncio.CancelledError):
    raise exc_type()


async def t_05_link_repair_after_a_crash_creates_no_second_task_and_never_reclassifies():
    objective = "Vergleiche drei Hotels in Hamburg mit Quellen."
    # (a) Absturz NACH record_dispatch, VOR router.execute.
    async with world() as w:
        cid = await w.chat()
        w.cli.assessments.append(assessment("auftrag_recherche", objective))
        actual_execute = w.router.execute
        crashed = asyncio.Event()

        async def crashing_execute(*args, **kwargs):
            crashed.set()
            raise asyncio.CancelledError()
        with patch.object(w.router, "execute", crashing_execute):
            delivery_id = await w.post(cid, objective)
            await asyncio.wait_for(crashed.wait(), 10)
            await w.processor.wait_idle(5)
        row = w.chat_store.delivery(cid, delivery_id)
        require_equal((row["status"], row["task_id"]), ("running", ""))
        require_equal(json.loads(row["dispatch"])["action_class"], "auftrag")
        require_equal(w.tasks(), 0)
        restarted = w.second_processor()
        require_equal(await restarted.recover(), 1)
        row = await w.settled(cid, delivery_id)
        require_equal((row["status"], row["revision"]), ("completed", 1), row["error_code"])
        require(row["task_id"].startswith("at-"))
        require_equal(w.tasks(), 1)
        require_equal(len(w.cli.of("assess")), 1, "recover() classified the orphan again")
        require_equal(len(w.cli.calls), 1, "a second physical provider run happened")
        require_equal([(l["task_id"], l["source"]) for l in w.chat_store.task_links(cid)],
                      [(row["task_id"], delivery_id)])
        task = w.ledger.get_task(row["task_id"])
        require_equal((task.objective, task.scope, task.conversation_ref), (objective, "research", cid))
        grant = w.orch.task_authority.for_run(row["run_id"])
        require_equal(grant.receipt_reference, "browser:" + (await w.auth_session_id()) + ":" + delivery_id)
        require_equal(w.chat_store.messages(cid)[-1]["text"], PR.TASK_ACCEPTED_TEXT)
        decision = w.cognition.ledger.recent(cid)[0]
        require_equal((decision["outcome"], decision["produced_ref"]), ("dispatched", row["task_id"]))
    # (b) Absturz NACH router.execute, VOR complete_delivery: derselbe Auftrag wird gefunden.
    async with world() as w:
        cid = await w.chat()
        w.cli.assessments.append(assessment("auftrag_recherche", objective))
        actual_complete = w.chat_store.complete_delivery
        crashed = asyncio.Event()

        def crashing_complete(*args, **kwargs):
            crashed.set()
            raise asyncio.CancelledError()
        with patch.object(w.chat_store, "complete_delivery", crashing_complete):
            delivery_id = await w.post(cid, objective)
            await asyncio.wait_for(crashed.wait(), 10)
            await w.processor.wait_idle(5)
        require_equal(w.tasks(), 1, "the task was committed before the crash")
        committed = w.ledger.recent_runs()[0]
        row = w.chat_store.delivery(cid, delivery_id)
        require_equal((row["status"], row["task_id"]), ("running", ""))
        require_equal(w.chat_store.task_links(cid), [])
        restarted = w.second_processor()
        require_equal(await restarted.recover(), 1)
        row = await w.settled(cid, delivery_id)
        require_equal((row["status"], row["task_id"], row["run_id"]), ("completed", committed.task_id, committed.run_id),
                      row["error_code"])
        require_equal(w.tasks(), 1, "the replay created a second task")
        require_equal(len(w.ledger.recent_runs()), 1)
        require_equal(len(w.cli.calls), 1)
        require_equal([l["task_id"] for l in w.chat_store.task_links(cid)], [committed.task_id])
    # (c) Absturz VOR record_dispatch mit beendetem Routing-Claim: nicht mehr aufloesbar.
    async with world() as w:
        cid = await w.chat()
        w.cli.assessments.append(assessment("auftrag_recherche", objective))
        crashed = asyncio.Event()

        def crashing_record(*args, **kwargs):
            crashed.set()
            raise asyncio.CancelledError()
        with patch.object(w.chat_store, "record_dispatch", crashing_record):
            delivery_id = await w.post(cid, objective)
            await asyncio.wait_for(crashed.wait(), 10)
            await w.processor.wait_idle(5)
        row = w.chat_store.delivery(cid, delivery_id)
        require_equal((row["status"], row["dispatch"]), ("running", ""))
        require([c["state"] for c in w.invocations(row["activity_id"])] == ["finished"])
        restarted = w.second_processor()
        require_equal(await restarted.recover(), 1)
        row = await w.settled(cid, delivery_id)
        require_equal((row["status"], row["error_code"]), ("blocked", "core_restarted_unresolved"))
        require_equal(len(w.cli.calls), 1, "an orphan with a finished routing claim was classified again")
        require_equal(w.tasks(), 0)
        require_equal(w.activity(row["activity_id"])["state"], "cancelled")


# ---------------------------------------------------------------- Fall 6

async def t_06_the_detail_is_byte_identical_across_reads_and_another_chats_result_does_not_touch_it():
    async with world() as w:
        chat_a = await w.chat("chat-a-0001")
        chat_b = await w.chat("chat-b-0001")
        w.cli.assessments.append(assessment("kein_auftrag", "Hallo A"))
        await w.ask(chat_a, "Hallo A")
        first = await (await w.client.get(CE.PREFIX + "/" + chat_a)).read()
        second = await (await w.client.get(CE.PREFIX + "/" + chat_a)).read()
        require_equal(first, second)
        w.cli.assessments.append(assessment("kein_auftrag", "Hallo B"))
        await w.ask(chat_b, "Hallo B")
        third = await (await w.client.get(CE.PREFIX + "/" + chat_a)).read()
        require_equal(first, third, "another chat's late result changed this chat's view")
        data = json.loads(third)
        require_equal(data["conversation"]["conversation_id"], chat_a)
        require("Hallo B" not in third.decode())


# ---------------------------------------------------------------- Fall 7

async def t_07_a_status_question_sees_only_this_chats_tasks():
    async with world() as w:
        chat_a = await w.chat("chat-a-0001")
        chat_b = await w.chat("chat-b-0001")
        task_a = await (await w.start(dict(BODY, client_request_id="request-a", conversation_ref=chat_a))).json()
        task_b = await (await w.start(dict(BODY, client_request_id="request-b", conversation_ref=chat_b,
                                           objective="Finde drei Ferienwohnungen in Kiel mit Quellen."))).json()
        w.cli.assessments.append(assessment("kein_auftrag", "Wie weit bist du?"))
        w.cli.answers.append("Dein Hotelvergleich ist angelegt und wartet auf den Start.")
        row = await w.ask(chat_a, "Wie weit bist du?")
        require_equal(row["status"], "completed")
        assess = _prompt(w.cli.of("assess", row["delivery_id"])[0])
        answer = _prompt(w.cli.of("answer", row["delivery_id"])[0])
        require(task_a["task_id"] in assess and task_b["task_id"] not in assess, "the register leaked")
        require(task_a["task_id"] in answer, "the answer context lacks this chat's task")
        require(task_b["task_id"] not in answer and "Ferienwohnungen" not in answer, "the answer context leaked")
        require("untrusted_executor" in answer)
        require_equal(w.tasks(), 2, "a status question started a task")
        detail = await (await w.client.get(CE.PREFIX + "/" + chat_a)).json()
        require_equal([c["aufgabe"] for c in detail["auftraege"]], [task_a["task_id"]])


# ---------------------------------------------------------------- Fall 8

async def _finish_task(w, task_id, run_id, *, bind=True):
    task = w.ledger.get_task(task_id)
    if bind:
        requirements = RQ.validate({"auskunft": [{"id": "r1", "text": "Vergleiche die Werte."}]}, objective=task.objective)
        require(w.ledger.bind_requirements(task_id, json.dumps(requirements)))
    else:
        # Eine Revision traegt ihre Anforderungen je Lauf, nicht je Auftrag.
        requirements = RQ.validate({"auskunft": [{"id": "r1", "text": "Vergleiche die Werte."}]},
                                   objective=TR.effective_objective(w.ledger, run_id))
        require(TR.bind_requirements(w.ledger, run_id, json.dumps(requirements)))
    w.ledger.transition(run_id, S.PLANNING)
    w.ledger.transition(run_id, S.RUNNING)
    step = w.ledger.create_step(run_id=run_id, seq=1, kind="specialist", specialist_profile="researcher/hermes")
    w.ledger.update_step(step.step_id, state="running", started=True)
    if bind:
        F.publish_file(w.ledger, run_id, step.step_id, b"Monat,Umsatz\nJuli,12\n", "Vergleich.csv", "text/csv", requirement="r1")
    w.ledger.update_step(step.step_id, state="succeeded", finished=True)
    w.ledger.transition(run_id, S.SUCCEEDED, result_summary="Der Vergleich ist fertig.")
    w.ledger.set_task_state(task_id, S.TASK_COMPLETED)


async def t_08_a_change_becomes_revision_two_of_the_same_task_and_a_running_task_gets_a_question():
    objective = "Vergleiche drei Hotels in Hamburg mit Quellen."
    async with world() as w:
        cid = await w.chat()
        w.cli.assessments.append(assessment("auftrag_recherche", objective))
        started = await w.ask(cid, objective)
        require_equal(started["status"], "completed", started["error_code"])
        task_id, run_id = started["task_id"], started["run_id"]
        # Laeuft noch: eine Rueckfrage statt eines zweiten Auftrags.
        w.cli.assessments.append(assessment("auftrag_recherche", "Und bitte auch Bremen.", fortsetzung_von=task_id))
        asked = await w.ask(cid, "Und bitte auch Bremen.", client_message_id="msg-0002")
        require_equal((asked["status"], asked["task_id"]), ("completed", ""))
        require_equal(w.chat_store.messages(cid)[-1]["text"], PR.RUNNING_TEXT)
        require_equal(len(w.ledger.recent_runs()), 1)
        # Fertig: die Folgeanweisung wird Revision 2 desselben Auftrags.
        await _finish_task(w, task_id, run_id)
        w.cli.assessments.append(assessment("auftrag_recherche", "Ergaenze bitte Maerz.", fortsetzung_von=task_id))
        changed = await w.ask(cid, "Ergaenze bitte Maerz.", client_message_id="msg-0003")
        require_equal((changed["status"], changed["task_id"], changed["revision"]), ("completed", task_id, 2),
                      changed["error_code"])
        require(changed["run_id"] != run_id)
        require_equal(w.tasks(), 1)
        require_equal(TR.revision_for_run(w.ledger, changed["run_id"])["text"], "Ergaenze bitte Maerz.")
        require_equal([(l["run_id"], l["revision"]) for l in w.chat_store.task_links(cid)],
                      [(run_id, 1), (changed["run_id"], 2)])
        grant = w.orch.task_authority.for_run(changed["run_id"])
        require_equal(grant.receipt_method, "dashboard_session")
        require_equal(w.orch.costs.view(task_id)["ask_threshold_cents"], 1000)
        require_equal(w.chat_store.messages(cid)[-1]["text"], PR.FOLLOWUP_ACCEPTED_TEXT)
        require_equal(len(w.cli.of("answer")), 0, "a followup never calls the answer model")
        # Explizites Ziel im Body: kein Routing, Folgeanweisung an genau dieses Ziel.
        await _finish_task(w, task_id, changed["run_id"], bind=False)
        explicit = await w.ask(cid, "Und bitte April dazu.", client_message_id="msg-0004",
                               target={"task_id": task_id, "run_id": changed["run_id"], "revision": 2})
        require_equal((explicit["status"], explicit["task_id"], explicit["revision"]), ("completed", task_id, 3),
                      explicit["error_code"])
        require_equal(len(w.cli.of("assess")), 3, "an explicit target was routed anyway")
        require_equal(TR.revision_for_run(w.ledger, explicit["run_id"])["text"], "Und bitte April dazu.")
        # Ueber 2000 Zeichen an ein explizites Ziel: Rueckfrage wie im gerouteten Weg, keine
        # stille Kuerzung, keine Revision (Review Runde 2, F-2: 3074 gesendet, 2000 gebunden).
        await _finish_task(w, task_id, explicit["run_id"], bind=False)
        too_long = ("Bitte ergaenze ausserdem folgende Punkte " * 75).strip()
        require(2000 < len(too_long) <= 4000)
        overlong = await w.ask(cid, too_long, client_message_id="msg-0004b",
                               target={"task_id": task_id, "run_id": explicit["run_id"], "revision": 3})
        require_equal((overlong["status"], overlong["task_id"], overlong["revision"]), ("completed", "", 0),
                      overlong["error_code"])
        require_equal(json.loads(overlong["dispatch"])["action_class"], "objective_too_long")
        require_equal(w.chat_store.messages(cid)[-1]["text"], PR.TOO_LONG_TEXT)
        require_equal(len(w.ledger.runs_for_task(task_id)), 3, "an over-long explicit followup created a revision")
        # Ein fremdes oder veraltetes Ziel: `followup_not_available`, nichts angelegt.
        foreign = w.ledger.create_task(objective="Fremd", scope="research", created_origin="trusted_dashboard",
                                       created_principal="someone-else")
        foreign_run = w.ledger.create_run(task_id=foreign.task_id)
        blocked = await w.ask(cid, "Mach das noch einmal.", client_message_id="msg-0005",
                              target={"task_id": foreign.task_id, "run_id": foreign_run.run_id, "revision": 1})
        require_equal((blocked["status"], blocked["error_code"]), ("blocked", "followup_not_available"))
        stale = await w.ask(cid, "Und Mai.", client_message_id="msg-0006",
                            target={"task_id": task_id, "run_id": changed["run_id"], "revision": 1})
        require_equal((stale["status"], stale["error_code"]), ("blocked", "followup_not_available"))
        require_equal(len(w.cli.of("assess")), 3)
        require_equal(len(w.ledger.runs_for_task(task_id)), 3)
        detail = await (await w.client.get(CE.PREFIX + "/" + cid)).json()
        require_equal(sorted(c["id"] for c in detail["auftraege"]),
                      sorted([run_id, changed["run_id"], explicit["run_id"]]))


async def t_08b_a_followup_admitted_before_a_crash_is_found_again_and_bound_to_the_chat():
    """Codex-Review 19.09.2026, Befund d: nach `admit_followup` und VOR dem Endzustand der
    Zustellung stuerzt der Core ab. Der Elternlauf ist dann nicht mehr fortsetzbar (der
    Folgelauf ist aktiv) — die Wiederaufnahme muss den schon angenommenen Folgelauf per
    Replay wiederfinden und an den Chat binden, nicht „laeuft noch" ohne Verweis melden."""
    objective = "Vergleiche drei Hotels in Hamburg mit Quellen."
    async with world() as w:
        cid = await w.chat()
        w.cli.assessments.append(assessment("auftrag_recherche", objective))
        started = await w.ask(cid, objective)
        require_equal(started["status"], "completed", started["error_code"])
        task_id, run_id = started["task_id"], started["run_id"]
        await _finish_task(w, task_id, run_id)
        w.cli.assessments.append(assessment("auftrag_recherche", "Ergaenze bitte Maerz.", fortsetzung_von=task_id))
        crashed = asyncio.Event()

        def crashing_complete(*args, **kwargs):
            crashed.set()
            raise asyncio.CancelledError()
        with patch.object(w.chat_store, "complete_delivery", crashing_complete):
            delivery_id = await w.post(cid, "Ergaenze bitte Maerz.", client_message_id="msg-0003")
            await asyncio.wait_for(crashed.wait(), 10)
            await w.processor.wait_idle(5)
        runs = w.ledger.runs_for_task(task_id)
        require_equal(len(runs), 2, "the follow-up run was admitted before the crash")
        followup_run = next(r for r in runs if r.run_id != run_id)
        row = w.chat_store.delivery(cid, delivery_id)
        require_equal((row["status"], row["task_id"]), ("running", ""))
        require_equal([l["revision"] for l in w.chat_store.task_links(cid)], [1])
        require_equal(TR.eligibility(w.ledger, run_id)["eligible"], False,
                      "the parent is not continuable while the admitted follow-up is active")
        restarted = w.second_processor()
        require_equal(await restarted.recover(), 1)
        row = await w.settled(cid, delivery_id)
        require_equal((row["status"], row["task_id"], row["run_id"], row["revision"]),
                      ("completed", task_id, followup_run.run_id, 2), row["error_code"])
        require_equal(len(w.ledger.runs_for_task(task_id)), 2, "the replay created a second follow-up run")
        require_equal([(l["run_id"], l["revision"]) for l in w.chat_store.task_links(cid)],
                      [(run_id, 1), (followup_run.run_id, 2)])
        require_equal(w.chat_store.messages(cid)[-1]["text"], PR.FOLLOWUP_ACCEPTED_TEXT)


async def t_08c_a_store_error_while_waking_after_a_restart_is_logged_and_never_kills_recovery():
    """Review round 10, K10-4: the recovery-side store reads raised raw sqlite3 errors while
    recover() caught only ConversationStoreError — a store that became unusable while
    _recover_when_ready waited for the runtime killed that task silently, and orphaned
    deliveries were never woken. The reads now raise ConversationStoreError, recover()
    reports 0 with a log line, and _recover_when_ready never dies unlogged."""
    import sqlite3
    from solvio.conversation.store import ConversationStore, ConversationStoreError
    async with world() as w:
        cid = await w.chat()
        w.cli.assessments.append(assessment("frage", "Wie spaet ist es?"))
        started = await w.ask(cid, "Wie spaet ist es?")
        require_equal(started["status"], "completed", started["error_code"])
        store = w.chat_store
        actual = store._conn
        closed = sqlite3.connect(":memory:")
        closed.close()
        store._conn = closed  # der Store ist unbrauchbar: jede Abfrage wirft rohes sqlite3
        try:
            for name, args in (("conversations_with_open_deliveries", ()), ("open_delivery_count", (cid,)),
                               ("deliveries", (cid,)), ("messages", (cid,)),
                               # review round 12, C12-H2: the processing-path readers too
                               ("message", (cid, "m")), ("turn_user_text", (cid, "t")), ("conversation", (cid,)),
                               ("sessions_of", (cid,)), ("delivery", (cid, "d")), ("task_links", (cid,))):
                try:
                    getattr(store, name)(*args)
                except ConversationStoreError:
                    pass
                else:
                    raise AssertionError(name + " raised nothing on a closed store")
            restarted = w.second_processor()
            require_equal(await restarted.recover(), 0, "an unreadable store is reported as nothing to wake")
            # Die wartende Wiederaufnahme stirbt nicht: sie endet, geloggt, ohne Ausnahme.
            broken = w.second_processor()
            with patch.object(broken, "recover", AsyncMock(side_effect=RuntimeError("boom"))):
                await asyncio.wait_for(broken._recover_when_ready(poll=0.01, limit=1), 5)
        finally:
            store._conn = actual
        require_equal(await w.second_processor().recover(), 0)


async def t_08d_a_store_error_while_reading_the_claimed_message_leaves_the_delivery_for_recovery():
    """Review round 12, C12-H2 (the rest of the owner's step "robust handling of SQLite
    errors when resuming stored messages"): a raw sqlite3 error from `store.message`
    after the claim fell into the processor's catch-all, which blocked the delivery
    TERMINALLY as processing_failed — through a store that had just failed. Now the
    reader raises ConversationStoreError, the processor logs store_failed and leaves
    the delivery `running`; a restart recovers it and it completes."""
    import sqlite3
    from solvio.conversation.store import ConversationStoreError
    async with world() as w:
        cid = await w.chat()
        store = w.chat_store
        actual = store._conn
        closed = sqlite3.connect(":memory:")
        closed.close()
        original_message = store.message
        failures = []

        def failing_message(conversation_id, message_id):
            store._conn = closed          # the store dies exactly at the first read after the claim
            try:
                return original_message(conversation_id, message_id)
            except ConversationStoreError as exc:
                failures.append(str(exc)); raise
            finally:
                store._conn = actual
        store.message = failing_message
        lost_before = w.processor.stats["lost"]
        w.cli.assessments.append(assessment("frage", "Wie spaet ist es?"))
        w.cli.answers.append("Es ist spaet.")
        d1 = await w.post(cid, "Wie spaet ist es?")
        for _ in range(300):
            if failures:
                break
            await asyncio.sleep(0.01)
        require(failures and "message failed" in failures[0], failures)
        store.message = original_message
        await asyncio.sleep(0.05)
        row = store.delivery(cid, d1)
        require_equal((row["status"], row["error_code"]), ("running", ""), "the delivery must wait for recovery, not end")
        require_equal(w.processor.stats["lost"], lost_before)
        require_equal(await w.second_processor().recover(), 1, "a restart wakes the waiting delivery")
        settled = await w.settled(cid, d1)
        require_equal(settled["status"], "completed", settled["error_code"])
        require_equal(w.chat_store.messages(cid)[-1]["text"], "Es ist spaet.")


async def t_08e_a_store_that_dies_while_blocking_a_failed_delivery_never_kills_the_chats_worker():
    """Review round 11, C11-H2: a non-store failure during processing goes to `_block`;
    when the store is unusable exactly then, `block_delivery` raised ConversationStoreError
    out of the except branch of `_process` — the chat's worker task died with an
    unretrieved exception and no log line naming the delivery. Now the failure is logged
    (store_failed, stage=block), the worker ends cleanly, the delivery stays `running`
    and a restart recovers it."""
    import sqlite3
    async with world() as w:
        cid = await w.chat()
        store = w.chat_store
        actual = store._conn
        closed = sqlite3.connect(":memory:")
        closed.close()
        original_message = store.message
        seen = []

        def dying_message(conversation_id, message_id):
            seen.append(message_id)
            store._conn = closed              # the store dies …
            raise RuntimeError("boom")        # … while a non-store failure hits the catch-all
        store.message = dying_message
        gate = w.gate_current()               # hold the worker so its task can be observed
        d1 = await w.post(cid, "Wie spaet ist es?")
        worker = w.processor._workers.get(cid)
        require(worker is not None and not worker.done(), "no worker waits for the delivery")
        gate.set()
        for _ in range(500):
            if worker.done():
                break
            await asyncio.sleep(0.01)
        require(seen, "the message read never happened")
        require(worker.done(), "the worker did not end")
        require_equal(worker.exception(), None, "the chat's worker died on the store error")
        store.message = original_message
        store._conn = actual
        row = store.delivery(cid, d1)
        require_equal((row["status"], row["error_code"]), ("running", ""), row)
        w.cli.assessments.append(assessment("frage", "Wie spaet ist es?"))
        w.cli.answers.append("Es ist spaet.")
        require_equal(await w.second_processor().recover(), 1)
        settled = await w.settled(cid, d1)
        require_equal(settled["status"], "completed", settled["error_code"])


# ---------------------------------------------------------------- Fall 9

async def t_08f_an_attachment_outside_a_new_order_is_named_never_silently_dropped():
    """Review Runde 18, B18-1: ein Anhang wirkte nur beim Auftragsstart; eine Folgeanweisung
    (geroutet oder mit explizitem Bezug) und eine Frage meldeten Erfolg, waehrend der Anhang
    liegen blieb. Jetzt sagt SOLVIO es (`anhang_unbenutzt`, Outcome `handed_back`, keine
    Kette — Runde 19 F19-1/F19-2) — ohne Revision, ohne Auftrag, ohne Modellaufruf. Und eine
    RUECKFRAGE an einen neuen Auftrag mit Anhang bleibt eine Rueckfrage, der Anhang reist mit
    in den Auftrag (Runde 20, B20-1/B20-2/B20-3)."""
    import base64
    document = {"operation": "extract_text", "format": "rtf",
                "content_b64": base64.b64encode(b"{\\rtf1\\ansi Sommerbericht: Umsatz 12 Prozent.}").decode()}
    def artifacts(w, run_id):
        with w.ledger._open() as db:
            return db.execute("SELECT COUNT(*) FROM agent_artifacts WHERE run_id=?", (run_id,)).fetchone()[0]
    def revisions(w, task_id):
        with w.ledger._open() as db:
            return db.execute("SELECT COUNT(*) FROM agent_task_revisions WHERE task_id=?", (task_id,)).fetchone()[0]
    async with world() as w:
        cid = await w.chat()
        objective = "Recherchiere bitte Hotels in Hamburg fuer morgen und fasse zusammen."
        w.cli.assessments.append(assessment("auftrag_recherche", objective))
        started = await w.ask(cid, objective)
        task_id, run_id = started["task_id"], started["run_id"]
        await _finish_task(w, task_id, run_id)
        # (1) geroutet als Folgeanweisung, mit Anhang: benannt, keine Revision, Outcome handed_back
        w.cli.assessments.append(assessment("auftrag_recherche", "Ergaenze die Werte aus dem Dokument.", fortsetzung_von=task_id))
        row = await w.ask(cid, "Ergaenze die Werte aus dem Dokument.", client_message_id="msg-0002", attachments=document)
        require_equal((row["status"], row["task_id"], int(row["revision"] or 0)), ("completed", "", 0), row["dispatch"][:200])
        require_equal(json.loads(row["dispatch"])["action_class"], PR.ACTION_ATTACHMENT_UNUSED)
        require_equal(w.chat_store.messages(cid)[-1]["text"], PR.ATTACHMENT_NEEDS_TASK_TEXT)
        require_equal(revisions(w, task_id), 0, "a revision was created for the attachment message")
        require_equal(w.cognition.ledger.recent(cid)[0]["outcome"], "handed_back", "the refusal opened a clarification")
        # (2) expliziter Auftragsbezug, mit Anhang
        row = await w.ask(cid, "Und bitte April dazu.", client_message_id="msg-0003", attachments=document,
                          target={"task_id": task_id, "run_id": run_id, "revision": 1})
        require_equal((row["status"], row["task_id"], json.loads(row["dispatch"])["action_class"]), ("completed", "", PR.ACTION_ATTACHMENT_UNUSED), row)
        require_equal(w.cognition.ledger.recent(cid)[0]["outcome"], "handed_back")
        # (3) eine Frage mit Anhang: kein blindes Modell
        answers_before = len(w.cli.of("answer"))
        w.cli.assessments.append(assessment("kein_auftrag", "Was steht in dem Dokument?"))
        row = await w.ask(cid, "Was steht in dem Dokument?", client_message_id="msg-0004", attachments=document)
        require_equal((row["status"], row["task_id"], json.loads(row["dispatch"])["action_class"]), ("completed", "", PR.ACTION_ATTACHMENT_UNUSED), row)
        require_equal(len(w.cli.of("answer")), answers_before, "the answer model was called blind")
        # (4) ohne Anhang laeuft die Folgeanweisung wie zuvor — und die Benennung hat keine Kette hinterlassen
        w.cli.assessments.append(assessment("auftrag_recherche", "Ergaenze bitte Maerz.", fortsetzung_von=task_id))
        changed = await w.ask(cid, "Ergaenze bitte Maerz.", client_message_id="msg-0005")
        require_equal((changed["status"], changed["task_id"], changed["revision"]), ("completed", task_id, 2), changed)
        require_equal(TR.revision_for_run(w.ledger, changed["run_id"])["text"], "Ergaenze bitte Maerz.", "the refused sentence was chained")
        await _finish_task(w, task_id, changed["run_id"], bind=False)
        # (5) eine RUECKFRAGE des Einschaetzers an einen neuen Auftrag mit Anhang bleibt eine Rueckfrage …
        w.cli.assessments.append(assessment("klaerung", "Dokument auswerten", klaerungsfrage="Welche Werte sollen es sein?"))
        row = await w.ask(cid, "Werte das Dokument aus.", client_message_id="msg-0006", attachments=document)
        require_equal((json.loads(row["dispatch"])["action_class"], w.chat_store.messages(cid)[-1]["text"]),
                      ("rueckfrage", "Welche Werte sollen es sein?"), row["dispatch"][:200])
        require_equal(w.cognition.ledger.recent(cid)[0]["outcome"], "clarification")
        # … und die Antwort ohne Anhang bindet den Auftrag MIT dem Anhang aus der Kette
        w.cli.assessments.append(assessment("auftrag_recherche", "Werte das Dokument aus. Die Umsatzzahlen."))
        answered = await w.ask(cid, "Die Umsatzzahlen.", client_message_id="msg-0007")
        require_equal(answered["status"], "completed", answered)
        require(answered["task_id"] and answered["task_id"] != task_id, answered)
        require_equal(w.ledger.get_task(answered["task_id"]).objective, "Werte das Dokument aus. Die Umsatzzahlen.")
        require_equal(json.loads(answered["dispatch"]).get("attachment_turn"), row["delivery_id"], "the chain lost the attachment turn")
        require_equal(artifacts(w, answered["run_id"]), 1, "the attachment of the chain was not bound to the new order")
        await _finish_task(w, answered["task_id"], answered["run_id"])
        # (6) ueberlanger neuer Auftrag mit Anhang: Ueberlaenge wird ersetzt, der Anhang nicht (B20-2)
        long_text = ("Recherchiere bitte sehr ausfuehrlich " * 80).strip()
        require(2000 < len(long_text) <= 4000)
        w.cli.assessments.append(assessment("auftrag_recherche", long_text))
        row = await w.ask(cid, long_text, client_message_id="msg-0008", attachments=document)
        require_equal((row["task_id"], json.loads(row["dispatch"])["action_class"], w.chat_store.messages(cid)[-1]["text"]),
                      ("", "objective_too_long", PR.TOO_LONG_TEXT), row["dispatch"][:200])
        short = "Recherchiere bitte Fluege nach Lissabon im Oktober."
        w.cli.assessments.append(assessment("auftrag_recherche", short))
        shortened = await w.ask(cid, short, client_message_id="msg-0009")
        require_equal((shortened["status"], w.ledger.get_task(shortened["task_id"]).objective), ("completed", short), shortened)
        require_equal(artifacts(w, shortened["run_id"]), 1, "the attachment of the too-long message was lost")
        await _finish_task(w, shortened["task_id"], shortened["run_id"])
        # (7) zwei laufende Auftraege: die Mehrdeutigkeitsfrage wird gestellt, der Anhang reist mit (B20-1)
        for name in ("a", "b"):
            res = await w.start(dict(BODY, client_request_id=f"request-{name}", conversation_ref=cid))
            require_equal(res.status, 201)
        w.cli.assessments.append(assessment("auftrag_recherche", "Vergleiche die Zahlen im Dokument."))
        row = await w.ask(cid, "Vergleiche die Zahlen im Dokument.", client_message_id="msg-0010", attachments=document)
        require_equal(json.loads(row["dispatch"])["action_class"], "mehrdeutig", row["dispatch"][:200])
        require("Meinst du" in w.chat_store.messages(cid)[-1]["text"])
        w.cli.assessments.append(assessment("auftrag_recherche", "Vergleiche die Zahlen im Dokument. Etwas Neues."))
        fresh = await w.ask(cid, "Etwas Neues.", client_message_id="msg-0011")
        require_equal(fresh["status"], "completed", fresh)
        require(fresh["task_id"] and fresh["task_id"] not in (task_id, answered["task_id"], shortened["task_id"]), fresh)
        require_equal(artifacts(w, fresh["run_id"]), 1, "the attachment was lost across the ambiguity question")
        # (8b, Runde 21 F21-1) … auch mit dem Anhang der OFFENEN KETTE: Klaerung an einen Auftrag mit
        # Anhang, dann scheitert das Routing der Antwort — benannt, kein blindes Modell, kein Verlust
        w.cli.assessments.append(assessment("klaerung", "Dokument auswerten", klaerungsfrage="Welche Werte sollen es sein?"))
        row = await w.ask(cid, "Werte das Dokument aus.", client_message_id="msg-0013", attachments=document)
        require_equal(json.loads(row["dispatch"])["action_class"], "rueckfrage", row["dispatch"][:200])
        carrier = row["delivery_id"]
        answers_before = len(w.cli.of("answer"))
        w.cli.override = lambda kind, record: L.Outcome(True, text=codex_lines("nicht json"), exit_code=0, process_started=True) if kind == "assess" else None
        row = await w.ask(cid, "Die Umsatzzahlen.", client_message_id="msg-0014")
        w.cli.override = None
        require_equal((json.loads(row["dispatch"])["action_class"], json.loads(row["dispatch"]).get("attachment_turn"), w.chat_store.messages(cid)[-1]["text"]),
                      (PR.ACTION_ATTACHMENT_UNUSED, carrier, PR.ATTACHMENT_NEEDS_TASK_TEXT), row["dispatch"][:200])
        require_equal(len(w.cli.of("answer")), answers_before, "the answer model was called blind on a chain attachment")
        # (9, Runde 21 F21-1b) ein expliziter Auftragsbezug waehrend einer offenen Kette mit Anhang wird benannt
        w.cli.assessments.append(assessment("klaerung", "Dokument auswerten", klaerungsfrage="Welche Werte sollen es sein?"))
        row = await w.ask(cid, "Werte das Dokument aus.", client_message_id="msg-0015", attachments=document)
        require_equal(json.loads(row["dispatch"])["action_class"], "rueckfrage", row["dispatch"][:200])
        carrier = row["delivery_id"]
        row = await w.ask(cid, "Und bitte April dazu.", client_message_id="msg-0016",
                          target={"task_id": task_id, "run_id": changed["run_id"], "revision": 2})
        require_equal((json.loads(row["dispatch"])["action_class"], json.loads(row["dispatch"]).get("attachment_turn"), int(row["revision"] or 0)),
                      (PR.ACTION_ATTACHMENT_UNUSED, carrier, 0), row["dispatch"][:200])
        require_equal(revisions(w, task_id), 1, "the explicit follow-up during a chain with an attachment created a revision")
        # (8) der Fehlschlagzweig des Routings antwortet mit Anhang nicht blind (F19-2)
        answers_before = len(w.cli.of("answer"))
        w.cli.override = lambda kind, record: L.Outcome(True, text=codex_lines("nicht json"), exit_code=0, process_started=True) if kind == "assess" else None
        row = await w.ask(cid, "Was steht in dem Dokument?", client_message_id="msg-0012", attachments=document)
        w.cli.override = None
        require_equal((row["status"], json.loads(row["dispatch"])["action_class"], w.chat_store.messages(cid)[-1]["text"]),
                      ("completed", PR.ACTION_ATTACHMENT_UNUSED, PR.ATTACHMENT_NEEDS_TASK_TEXT), row["dispatch"][:200])
        require_equal(len(w.cli.of("answer")), answers_before, "the answer model was called blind after a routing failure")


async def t_09_a_text_over_2000_characters_never_becomes_a_task():
    async with world() as w:
        cid = await w.chat()
        text = ("Bitte recherchiere ausfuehrlich " * 80).strip()
        require(2000 < len(text) <= 4000)
        w.cli.assessments.append(assessment("auftrag_recherche", text))
        row = await w.ask(cid, text)
        require_equal((row["status"], row["task_id"]), ("completed", ""))
        require_equal(w.chat_store.messages(cid)[-1]["text"], PR.TOO_LONG_TEXT)
        require_equal(json.loads(row["dispatch"])["action_class"], "objective_too_long")
        require_equal(w.tasks(), 0)
        require_equal(len(w.cli.of("answer")), 0)


# ---------------------------------------------------------------- Fall 10

async def t_10_the_sequence_bound_binds_the_open_question_to_d1_and_hides_d2():
    async with world() as w:
        cid = await w.chat()
        w.cli.assessments.append(assessment("klaerung", "Hotels", klaerungsfrage="Fuer welche Stadt?"))
        asked = await w.ask(cid, "Such mir ein Hotel.")
        require_equal(w.chat_store.messages(cid)[-1]["text"], "Fuer welche Stadt?")
        gate = w.gate_current()
        w.cli.assessments.append(assessment("kein_auftrag", "Hamburg."))
        w.cli.answers.append("Hamburg, verstanden.")
        d1 = await w.post(cid, "Hamburg.", client_message_id="delivery-1")
        for _ in range(200):
            if w.chat_store.delivery(cid, d1)["status"] == "running":
                break
            await asyncio.sleep(0.01)
        require_equal(w.chat_store.delivery(cid, d1)["status"], "running", "D1 was not claimed before D2 arrived")
        d2 = await w.post(cid, "Und wie ist das Wetter dort?", client_message_id="delivery-2")
        require_equal(w.chat_store.delivery(cid, d2)["message_sequence"], 4)
        gate.set()
        row1 = await w.settled(cid, d1)
        require_equal(row1["status"], "completed", row1["error_code"])
        assess = _prompt(w.cli.of("assess", d1)[0])
        require("Fuer welche Stadt?" in assess or "Such mir ein Hotel." in assess,
                "the open question was not bound to D1")
        require("Wetter" not in assess, "D1's assessment saw D2")
        answer = _prompt(w.cli.of("answer", d1)[0])
        require("Wetter" not in answer, "D1's answer context saw D2")
        require("Such mir ein Hotel." in answer and "Fuer welche Stadt?" in answer)
        # Der Aktivitaets-Digest von D1 ist unveraendert, obwohl D2 laengst im Verlauf steht:
        # dieselbe Anmeldung liefert dieselbe Aktivitaet — kein Replay-Widerspruch.
        digest = PR.content_digest(cid, row1["message_id"], row1["message_sequence"],
                                   w.chat_store.message(cid, row1["message_id"])["text"])
        require_equal(w.activity(row1["activity_id"])["content_digest"], digest)
        source = A._verified_source(principal="local-owner", source_kind="dashboard",
                                    source_ref=row1["source_ref"], conversation_id=cid,
                                    message_id=row1["message_id"])
        again = A.ActivityLedger(w.ledger).admit(source, content_digest=digest, purpose="text_chat",
                                                 operation_key=d1, lifetime_seconds=900)
        require_equal(again.activity_id, row1["activity_id"])
        w.cli.assessments.append(assessment("kein_auftrag", "Wetter"))
        row2 = await w.settled(cid, d2)
        require_equal(row2["status"], "completed", row2["error_code"])
        require_equal([m["role"] for m in w.chat_store.messages(cid)],
                      ["user", "assistant", "user", "user", "assistant", "assistant"])


# ---------------------------------------------------------------- Fall 11

async def t_11_concurrent_acceptance_of_one_client_message_id_is_one_row_one_delivery_one_run():
    async with world() as w:
        cid = await w.chat()
        w.cli.assessments.append(assessment("kein_auftrag", "Hallo"))
        responses = await asyncio.gather(*(w.send(cid, "Hallo, bist du da?", client_message_id="msg-parallel")
                                          for _ in range(4)))
        require_equal([r.status for r in responses], [202] * 4)
        ids = {(await r.json())["delivery_id"] for r in responses}
        require_equal(len(ids), 1)
        row = await w.settled(cid, ids.pop())
        require_equal(row["status"], "completed")
        require_equal(len([m for m in w.chat_store.messages(cid) if m["role"] == "user"]), 1)
        require_equal(len(w.chat_store.deliveries(cid)), 1)
        require_equal(w.processor.stats["processed"], 1, "the delivery was processed more than once")
        require_equal(len(w.cli.of("assess")), 1)


# ---------------------------------------------------------------- Fall 12

async def t_12_double_wake_and_recover_process_once_and_a_foreign_generation_cannot_overwrite_an_end_state():
    async with world() as w:
        cid = await w.chat()
        w.cli.assessments.append(assessment("kein_auftrag", "Hallo"))
        delivery_id = await w.post(cid, "Hallo, ich bin es.")
        w.processor.wake(cid)
        w.processor.wake(cid)
        require_equal(await w.processor.recover(), 1)
        row = await w.settled(cid, delivery_id)
        require_equal(row["status"], "completed")
        require_equal(w.processor.stats["processed"], 1)
        require_equal([c["kind"] for c in w.cli.calls], ["assess", "answer"])
        with w.ledger._open() as db:
            require_equal(db.execute("SELECT COUNT(*) FROM agent_cost_activities WHERE operation_key=?",
                                     (delivery_id,)).fetchone()[0], 1)
        # Ein Worker fremder Generation kann den Endzustand nicht ueberschreiben.
        messages = w.chat_store.messages(cid)
        try:
            w.chat_store.block_delivery(delivery_id, "pw-foreign", error_code="quota")
        except Exception as exc:
            require("delivery_lost" in str(exc), str(exc))
        else:
            raise AssertionError("a foreign generation overwrote a completed delivery")
        try:
            w.chat_store.complete_delivery(delivery_id, "pw-foreign", assistant_text="Eingeschmuggelt.")
        except Exception as exc:
            require("delivery_lost" in str(exc), str(exc))
        else:
            raise AssertionError("a foreign generation appended to a completed delivery")
        require_equal(w.chat_store.messages(cid), messages)
        require(w.chat_store.claim_next_delivery(cid, "pw-foreign") is None)
        require_equal(w.chat_store.delivery(cid, delivery_id)["status"], "completed")
        # Und ein zweiter Prozessor findet nach dem Ende nichts mehr.
        require_equal(await w.second_processor().recover(), 0)


# ---------------------------------------------------------------- Fall 13

async def t_13_liveness_revocation_between_acceptance_and_processing_blocks_without_any_effect():
    objective = "Vergleiche drei Hotels in Hamburg mit Quellen."
    # (i) Browser-Logout zwischen Annahme und Verarbeitung.
    async with world() as w:
        cid = await w.chat()
        gate = w.gate_current()
        w.cli.assessments.append(assessment("auftrag_recherche", objective))
        delivery_id = await w.post(cid, objective)
        await w.sessions.revoke((await w.auth_session_id()), principal="local-owner")
        gate.set()
        row = await w.settled(cid, delivery_id)
        require_equal((row["status"], row["error_code"]), ("blocked", "source_revoked"))
        require_equal(w.cli.calls, [], "a revoked source still reached the model")
        require_equal(w.tasks(), 0)
        require_equal([m["role"] for m in w.chat_store.messages(cid)], ["user"])
    # (ii) Geraete-Widerruf zwischen Annahme und Verarbeitung.
    async with world() as w:
        ctx, app, headers = await w.device()
        cid = (await (await w.create("chat-app-0001", client=app, headers=headers)).json())["conversation_id"]
        gate = w.gate_current()
        message = {"conversation_id": cid, "client_message_id": "message-1", "text": objective}
        payload = await w.app_proof(ctx, app, headers, message)
        res = await app.post(f"{CE.PREFIX}/{cid}/messages", json=payload, headers=headers)
        require_equal(res.status, 202, await res.text())
        delivery_id = (await res.json())["delivery_id"]
        await w.cp.revoke_device(ctx.device_id, reason="lost")
        gate.set()
        row = await w.settled(cid, delivery_id)
        require_equal((row["status"], row["error_code"]), ("blocked", "source_revoked"))
        require_equal(w.cli.calls, [])
        require_equal(w.tasks(), 0)
    # (iii) Replay nach Neustart: der Entscheid steht, die Quelle ist weg — kein Auftrag.
    async with world() as w:
        cid = await w.chat()
        w.cli.assessments.append(assessment("auftrag_recherche", objective))
        crashed = asyncio.Event()

        async def crashing_execute(*args, **kwargs):
            crashed.set()
            raise asyncio.CancelledError()
        with patch.object(w.router, "execute", crashing_execute):
            delivery_id = await w.post(cid, objective)
            await asyncio.wait_for(crashed.wait(), 10)
            await w.processor.wait_idle(5)
        require_equal(json.loads(w.chat_store.delivery(cid, delivery_id)["dispatch"])["action_class"], "auftrag")
        await w.sessions.revoke((await w.auth_session_id()), principal="local-owner")
        restarted = w.second_processor()
        require_equal(await restarted.recover(), 1)
        row = await w.settled(cid, delivery_id)
        require_equal((row["status"], row["error_code"]), ("blocked", "source_revoked"))
        require_equal(w.tasks(), 0)
        require_equal(len(w.cli.calls), 1)
    # (iv) Eine andere Core-Kennung: dito. Die Zustellung traegt die Kennung des Cores, der sie
    # annahm; verarbeitet sie ein Core mit anderer Kennung, ist die Quelle nicht mehr dieselbe.
    # (Frueher aenderte der Test die Zeile per SQL NACH dem Senden — ein Rennen gegen den
    # Claim-Schnappschuss, ~14 % rot; Review Runde 3, C3-1/B-R3-2. Jetzt wechselt der Core.)
    async with world() as w:
        cid = await w.chat()
        gate = w.gate_current()
        w.cli.assessments.append(assessment("auftrag_recherche", objective))
        delivery_id = await w.post(cid, objective)
        require_equal(w.chat_store.delivery(cid, delivery_id)["core_id"], w.cp.core_instance_id)
        with patch.object(w.cp, "core_instance_id", "core-elsewhere"):
            gate.set()
            row = await w.settled(cid, delivery_id)
        require_equal((row["status"], row["error_code"]), ("blocked", "source_revoked"))
        require_equal(w.cli.calls, [])
        require_equal(w.tasks(), 0)
    # (v) Widerruf UNMITTELBAR vor router.execute — nach dem Routing, nach dem Entscheid.
    async with world() as w:
        cid = await w.chat()
        w.cli.assessments.append(assessment("auftrag_recherche", objective))
        actual_execute = w.router.execute

        def revoke_then_execute(*args, **kwargs):
            raise AssertionError("router.execute ran although the source was revoked before it")
        actual_current = w.processor.current
        seen = []

        async def counting_current(row, runtime):
            seen.append(1)
            if len(seen) == 3:
                # der dritte Aufruf ist der unmittelbar vor router.execute (Start, Claim, Execute)
                await w.sessions.revoke((await w.auth_session_id()), principal="local-owner")
            return await actual_current(row, runtime)
        w.processor.current = counting_current
        with patch.object(w.router, "execute", revoke_then_execute):
            row = await w.ask(cid, objective)
        require_equal((row["status"], row["error_code"]), ("blocked", "source_revoked"))
        require_equal(len(w.cli.calls), 1)
        require_equal(w.tasks(), 0)


async def t_13b_a_revocation_at_the_cost_gate_is_named_source_revoked_and_never_cost_recovery():
    """Ergebniswahrheit des Fehlercodes (§3.2, §5.4). `cost_dispatch.dispatch` faengt
    jede Ausnahme des `source_check` und sagt `cost_recovery_required` — fuer den
    Nutzer „Kostenpruefung erforderlich" ohne Handlungsanweisung, obwohl seine
    Anmeldung abgelaufen ist und „neu anmelden, erneut senden" die Antwort waere.
    Das Fenster ist nicht klein: es liegt ueber jedem CLI-Aufruf des Routings (bis
    120 s) und vor dem Antwort-Claim. Echter Browser-Logout, echtes `current()`."""
    async def revoke_before_call(w, ordinal):
        actual = w.processor.current
        seen = []

        async def counting(row, runtime):
            seen.append(1)
            if len(seen) == ordinal:
                await w.sessions.revoke((await w.auth_session_id()), principal="local-owner")
            return await actual(row, runtime)
        w.processor.current = counting
        return seen
    # (i) Logout unmittelbar vor dem Routing-Claim (Ordinal 1): kein Modellaufruf.
    async with world() as w:
        cid = await w.chat()
        w.cli.assessments.append(assessment("kein_auftrag", "Hallo"))
        await revoke_before_call(w, 2)           # 1 = Start, 2 = source_check des Routings
        row = await w.ask(cid, "Hallo, bist du da?")
        require_equal((row["status"], row["error_code"]), ("blocked", "source_revoked"))
        require_equal(w.cli.calls, [], "a revoked source still reached the model")
        require_equal(w.invocations(row["activity_id"]), [], "no claim may exist without a call")
        activity = w.activity(row["activity_id"])
        require_equal((activity["state"], activity["held_reason"]), ("cancelled", "source_revoked"))
        require_equal([m["role"] for m in w.chat_store.messages(cid)], ["user"])
        require_equal(w.tasks(), 0)
    # (ii) Logout waehrend des Routings, vor dem Antwort-Claim (Ordinal 2): ein Aufruf, keine Antwort.
    async with world() as w:
        cid = await w.chat()
        w.cli.assessments.append(assessment("kein_auftrag", "Hallo"))
        await revoke_before_call(w, 3)           # 3 = source_check der Antwort
        row = await w.ask(cid, "Hallo, bist du da?")
        require_equal((row["status"], row["error_code"]), ("blocked", "source_revoked"))
        require_equal([c["kind"] for c in w.cli.calls], ["assess"], "the answer call ran without a live source")
        require_equal([(c["ordinal"], c["state"]) for c in w.invocations(row["activity_id"])], [(1, "finished")])
        activity = w.activity(row["activity_id"])
        require_equal((activity["state"], activity["held_reason"]), ("cancelled", "source_revoked"))
        require_equal([m["role"] for m in w.chat_store.messages(cid)], ["user"])
        require_equal(w.tasks(), 0)
    # (iii) Gegenprobe: eine ECHTE Kostenabsage des Tors heisst weiterhin so — die
    # Quelle war da, das Subjekt traegt einen ungewissen Claim.
    async with world() as w:
        cid = await w.chat()
        w.cli.override = lambda kind, record: L.Outcome(False, reason="", exit_code=None, process_started=True)
        row = await w.ask(cid, "Hallo, bist du da?")
        require_equal((row["status"], row["error_code"]), ("blocked", "cost_recovery_required"))
        require_equal(w.activity(row["activity_id"])["held_reason"], "")


# ---------------------------------------------------------------- Fall 14

async def t_14_a_second_chat_starts_before_the_first_chats_third_message_and_never_two_calls_per_chat():
    async with world() as w:
        require_equal(w.processor._max_parallel, PR.MAX_PARALLEL_CHATS)
        chat_a = await w.chat("chat-a-0001")
        chat_b = await w.chat("chat-b-0001")
        w.cli.delay = 0.12
        for i in range(3):
            w.cli.assessments.append(assessment("kein_auftrag", f"A{i}"))
        ids_a = [await w.post(chat_a, f"Nachricht A{i}", client_message_id=f"message-a-{i}") for i in range(3)]
        w.cli.assessments.append(assessment("kein_auftrag", "B0"))
        id_b = await w.post(chat_b, "Nachricht B0", client_message_id="message-b-0")
        for cid, did in [(chat_a, i) for i in ids_a] + [(chat_b, id_b)]:
            require_equal((await w.settled(cid, did, timeout=30))["status"], "completed")
        first_b = min(c["at"] for c in w.cli.calls if c["delivery_id"] == id_b)
        first_a3 = min(c["at"] for c in w.cli.calls if c["delivery_id"] == ids_a[2])
        require(first_b < first_a3, "the second chat waited behind the first chat's third message")
        require_equal(max(w.cli.peak.values()), 1, "two calls of one chat ran at the same time")
        order = [c["delivery_id"] for c in w.cli.calls if c["conversation_id"] == chat_a]
        require_equal(order, [ids_a[0], ids_a[0], ids_a[1], ids_a[1], ids_a[2], ids_a[2]],
                      "one chat's messages did not run strictly in order")
        require_equal(w.processor.stats["processed"], 4)


# ---------------------------------------------------------------- Fall 16

async def t_16_a_reader_over_https_never_sees_the_message_without_its_delivery():
    async with world() as w:
        cid = await w.chat()
        w.processor._stopping = True                       # nichts verarbeiten, nur annehmen
        actual = w.chat_store._insert_message
        entered, release = asyncio.Event(), None
        loop = asyncio.get_running_loop()
        import threading
        hold = threading.Event()

        def slow_insert(*args, **kwargs):
            result = actual(*args, **kwargs)
            loop.call_soon_threadsafe(entered.set)
            hold.wait(5)                                    # die Transaktion bleibt offen
            return result
        with patch.object(w.chat_store, "_insert_message", slow_insert):
            sending = asyncio.ensure_future(w.send(cid, "Hallo, bist du da?"))
            await asyncio.wait_for(entered.wait(), 5)
            reading = asyncio.ensure_future(w.client.get(CE.PREFIX + "/" + cid))
            await asyncio.sleep(0.1)
            require(not reading.done(), "the reader did not wait for the open transaction")
            hold.set()
            res = await sending
            require_equal(res.status, 202)
            detail = await (await reading).json()
        require_equal(len(detail["messages"]), 1)
        require("delivery" in detail["messages"][0], "the reader saw the message without its delivery")
        require_equal(detail["deliveries_open"], 1)
        w.processor._stopping = False


# ---------------------------------------------------------------- Rueckfragen und Mehrdeutigkeit

# ---------------------------------------------------------------- Fall 17

async def t_17_stop_while_a_wake_is_pending_ends_the_worker_instead_of_respawning_it_forever():
    """Review-Befund (correctness): `_drain` legte sich im `finally` endlos neu an,
    sobald `stop()` bei gesetztem dirty-Ereignis kam — 37230 Neuanlagen in 0,5 s,
    ein Busy-Loop bis zum Prozessende. Produktiv: Herunterfahren, waehrend ein
    Chat verarbeitet wird und ein Wecken bereits eingetroffen ist."""
    spawns = []
    original = PR.ConversationProcessor._drain

    async def counting(self, conversation_id):
        spawns.append(conversation_id)
        await original(self, conversation_id)

    async with world() as w:
        cid = await w.chat()
        w.cli.assessments.append(assessment("kein_auftrag", "Hallo"))
        w.cli.answers.append("Hallo zurueck.")
        gate, processing = asyncio.Event(), asyncio.Event()
        current = w.processor.current

        async def paused_current(row, runtime):
            processing.set()
            await gate.wait()
            return await current(row, runtime)

        w.processor.current = paused_current
        with patch.object(PR.ConversationProcessor, "_drain", counting):
            delivery_id = await w.post(cid, "Hallo, ich bin es.")
            # `running` allein bedeutet nur, dass der Store-Thread geclaimt hat.
            # Dieser Fall stoppt absichtlich ERST nach Beginn der Verarbeitung;
            # der Stopp waehrend des Claim-Await ist separat in t_17d geprueft.
            await asyncio.wait_for(processing.wait(), 5)
            # Ein Wecken trifft ein, waehrend verarbeitet wird — dann kommt der Abbau.
            w.processor._dirty[cid].set()
            w.processor.stop()
            gate.set()
            row = await w.settled(cid, delivery_id)
            require_equal(row["status"], "completed", row["error_code"])
            before = len(spawns)
            await asyncio.sleep(0.3)
            require(len(spawns) - before <= 1,
                    f"the worker respawned {len(spawns) - before} times in 0.3 s after stop()")
            require(await w.processor.wait_idle(2), "a worker is still alive after stop()")
            require(cid not in w.processor._workers, "the stopped worker is still registered")
            require(cid not in w.processor._dirty, "the dirty event survived the stop")
        # Die Zustellung ist im Store abgeschlossen; nichts wartet auf einen Neustart.
        require_equal(await w.second_processor().recover(), 0)
        w.processor._stopping = False


async def t_17b_http_shutdown_waits_for_an_answer_and_leaves_queued_chats_for_restart():
    """DEBT-0306: der echte on_shutdown-Hook muss vor dem Store-Abbau warten."""
    async with world() as w:
        w.processor._shutdown_grace = 0.8
        w.processor._sem = asyncio.Semaphore(1)
        entered, release, exited = asyncio.Event(), asyncio.Event(), asyncio.Event()
        actual = w.cli.run

        async def paused(invocation, prompt):
            answering = AN.ANSWER_MARKER in prompt
            if answering:
                w.cli.hang = release
                entered.set()
            try:
                return await actual(invocation, prompt)
            finally:
                if answering:
                    exited.set()

        w.transport._runner = paused
        cid = await w.chat()
        did = await w.post(cid, "Hallo, gib mir bitte eine Antwort.")
        await asyncio.wait_for(entered.wait(), 5)
        queued_chat = await w.chat("chat-queued")
        queued = await w.post(queued_chat, "Diese Nachricht wartet auf den naechsten Start.")
        closing = asyncio.create_task(w.app.shutdown())
        try:
            await asyncio.sleep(0.05)
            require(not closing.done(), "HTTP shutdown detached an in-flight answer")
            require_equal(w.chat_store.delivery(cid, did)["status"], "running")
            release.set()
            await asyncio.wait_for(closing, 1.5)
            require(exited.is_set(), "the provider had not exited when shutdown returned")
            row = w.chat_store.delivery(cid, did)
            require_equal(row["status"], "completed", row)
            require_equal([claim["state"] for claim in w.invocations(row["activity_id"])],
                          ["finished", "finished"])
            require_equal(w.chat_store.messages(cid)[-1]["text"], "Gern — hier meine Antwort.")
            require_equal(w.chat_store.delivery(queued_chat, queued)["status"], "accepted",
                          "a worker waiting for capacity started a new delivery during shutdown")
            require(not w.processor._workers, "HTTP shutdown returned with owned workers")
        finally:
            release.set()
            await asyncio.gather(closing, return_exceptions=True)


async def t_17c_shutdown_cancels_a_stalled_answer_drains_cleanup_and_never_replays_its_claim():
    """Die Frist begrenzt das Auslaufen; der Abbruch wird vor Store-close abgewickelt."""
    async with world() as w:
        w.processor._shutdown_grace = 0.08
        entered, release = asyncio.Event(), asyncio.Event()
        cancelling, reaped = asyncio.Event(), asyncio.Event()
        actual = w.cli.run
        ticks = []

        async def paused(invocation, prompt):
            if AN.ANSWER_MARKER in prompt:
                w.cli.hang = release
                entered.set()
            try:
                return await actual(invocation, prompt)
            except asyncio.CancelledError:
                cancelling.set()
                # Der Starter hat asynchrones Aufraeumen, bevor er Cancellation
                # ans Kostentor gibt. Erneute Stopp-Signale duerfen es nicht kuerzen.
                await asyncio.sleep(0.08)
                require(w.chat_store.delivery(cid, did) is not None, "store closed before provider cleanup")
                reaped.set()
                raise

        async def heartbeat():
            while True:
                ticks.append(time.monotonic())
                await asyncio.sleep(0.005)

        w.transport._runner = paused
        cid = await w.chat()
        did = await w.post(cid, "Hallo, beantworte bitte meine Frage.")
        await asyncio.wait_for(entered.wait(), 5)
        pulse = asyncio.create_task(heartbeat())
        started = time.monotonic()
        closing = asyncio.create_task(w.app.shutdown())
        try:
            await asyncio.wait_for(cancelling.wait(), 1.5)
            require(time.monotonic() - started >= 0.06, "shutdown skipped its configured grace")
            closing.cancel()
            await asyncio.sleep(0.01)
            closing.cancel()
            require(not closing.done(), "repeated cancellation detached provider cleanup")
            result = await asyncio.wait_for(asyncio.gather(closing, return_exceptions=True), 1.5)
            require(isinstance(result[0], asyncio.CancelledError), result)
            require(time.monotonic() - started < 1.5, "shutdown did not bound the stalled call")
            require(len(ticks) >= 5, "shutdown blocked the event loop")
            require(reaped.is_set(), "shutdown returned before provider cleanup")
            require(not w.processor._workers, "a worker survived shutdown")
            require_equal(w.cli.concurrent[cid], 0)
            row = w.chat_store.delivery(cid, did)
            require_equal(row["status"], "running", "shutdown invented a terminal delivery")
            require_equal([claim["state"] for claim in w.invocations(row["activity_id"])],
                          ["finished", "unknown"])
            require_equal([m["role"] for m in w.chat_store.messages(cid)], ["user"])
            calls = len(w.cli.calls)
            release.set()
            replacement = w.second_processor()
            try:
                require_equal(await replacement.recover(), 1)
                require(await replacement.wait_idle(5), "restart did not resolve the stopped delivery")
                resumed = w.chat_store.delivery(cid, did)
                require_equal((resumed["status"], resumed["error_code"]),
                              ("blocked", PR.ERROR_COST_RECOVERY))
                require_equal(len(w.cli.calls), calls, "restart replayed the cancelled model claim")
            finally:
                await replacement.shutdown()
        finally:
            release.set()
            pulse.cancel()
            await asyncio.gather(closing, pulse, return_exceptions=True)


async def t_17d_shutdown_during_the_store_claim_never_starts_processing_after_the_claim_returns():
    """Ein noch laufender Store-Thread darf nach dem Annahmestopp kein Modell starten."""
    import threading
    async with world() as w:
        w.processor._shutdown_grace = 0.8
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        actual = w.chat_store.claim_next_delivery

        def paused_claim(*args):
            row = actual(*args)
            if row is not None:
                loop.call_soon_threadsafe(entered.set)
                require(release.wait(5), "test did not release the store claim")
            return row

        cid = await w.chat()
        with patch.object(w.chat_store, "claim_next_delivery", paused_claim):
            did = await w.post(cid, "Hallo, beantworte bitte diese Nachricht.")
            await asyncio.wait_for(entered.wait(), 5)
            claimed = w.chat_store.delivery(cid, did)
            require_equal(claimed["status"], "running")
            closing = asyncio.create_task(w.app.shutdown())
            try:
                await asyncio.sleep(0.03)
                require(w.processor._stopping and not closing.done(), "shutdown did not wait for the pending claim")
                release.set()
                await asyncio.wait_for(closing, 1.5)
            finally:
                release.set()
                await asyncio.gather(closing, return_exceptions=True)
        require_equal(w.cli.calls, [], "the claimed delivery started a provider after shutdown began")
        stopped = w.chat_store.delivery(cid, did)
        require_equal((stopped["status"], stopped["dispatch"], stopped["activity_id"]),
                      ("running", "", ""), "shutdown invented a reset or an execution result")
        require_equal(stopped["worker_generation"], claimed["worker_generation"])
        require(not w.processor._workers, "the stopped claimant is still registered")
        # Keine Wirkung hat begonnen: die vorhandene neue Generation darf bei
        # weiterhin gueltiger Originalquelle genau diese Zustellung uebernehmen.
        replacement = w.second_processor()
        try:
            require_equal(await replacement.recover(), 1)
            require(await replacement.wait_idle(5), "restart left the untouched delivery unresolved")
            resumed = w.chat_store.delivery(cid, did)
            require_equal((resumed["status"], resumed["worker_generation"], resumed["message_id"]),
                          ("completed", replacement.worker_generation, claimed["message_id"]))
            require_equal([call["kind"] for call in w.cli.calls], ["assess", "answer"])
            require_equal([message["role"] for message in w.chat_store.messages(cid)], ["user", "assistant"])
            require_equal(await replacement.recover(), 0)
        finally:
            await replacement.shutdown()


async def t_a_build_request_and_two_running_tasks_end_in_a_question_not_a_task():
    async with world() as w:
        # Ein Bauauftrag ohne Projekt: eine Rueckfrage, kein Task (im Chat gibt es kein target_repo).
        build_chat = await w.chat("chat-build-0001")
        w.cli.assessments.append(assessment("auftrag_bau", "Baue mir eine Landingpage fuer den Verein."))
        row = await w.ask(build_chat, "Baue mir eine Landingpage fuer den Verein.")
        require_equal((row["status"], row["task_id"]), ("completed", ""))
        require_equal(w.chat_store.messages(build_chat)[-1]["text"], PR.BUILD_NEEDS_PROJECT_TEXT)
        require_equal(w.tasks(), 0)
        require_equal(len(w.cli.of("answer")), 0)
        cid = await w.chat()
        # Zwei laufende Auftraege im Register, kein Bezug: mehrdeutig.
        for name in ("a", "b"):
            res = await w.start(dict(BODY, client_request_id=f"request-{name}", conversation_ref=cid))
            require_equal(res.status, 201)
        w.cli.assessments.append(assessment("auftrag_recherche", "Und bitte auch mit Preisen."))
        row = await w.ask(cid, "Und bitte auch mit Preisen.")
        require_equal((row["status"], row["task_id"]), ("completed", ""))
        require("Meinst du" in w.chat_store.messages(cid)[-1]["text"])
        require_equal(json.loads(row["dispatch"])["action_class"], "mehrdeutig")
        require_equal(w.tasks(), 2)
        # Die Antwort auf unsere Frage darf ein neuer Auftrag werden.
        w.cli.assessments.append(assessment("auftrag_recherche", "Etwas Neues: finde drei Fahrradladen in Kiel."))
        row = await w.ask(cid, "Etwas Neues: finde drei Fahrradladen in Kiel.", client_message_id="msg-0002")
        require_equal(row["status"], "completed", row["error_code"])
        require(row["task_id"].startswith("at-"))
        require_equal(w.tasks(), 3)
        require_equal(len(w.cli.of("answer")), 0)


async def t_a_failed_routing_is_answered_without_a_task_but_a_quota_blocks():
    async with world() as w:
        cid = await w.chat()
        w.cli.override = lambda kind, record: (L.Outcome(True, text=codex_lines("kein json"), exit_code=0, process_started=True)
                                               if kind == "assess" else None)
        w.cli.answers.append("Ich habe dich verstanden.")
        row = await w.ask(cid, "Vergleiche drei Hotels in Hamburg mit Quellen.")
        require_equal((row["status"], row["error_code"]), ("completed", ""))
        require_equal(w.chat_store.messages(cid)[-1]["text"], "Ich habe dich verstanden.")
        require_equal(w.tasks(), 0)
        require_equal([c["kind"] for c in w.cli.calls], ["assess", "assess", "assess", "answer"])
        require_equal(json.loads(row["dispatch"])["routing_invocations"], 3)
        require_equal([c["ordinal"] for c in w.invocations(row["activity_id"])], [1, 2, 3, 4])
        w.cli.calls.clear()
        w.cli.override = lambda kind, record: L.Outcome(
            True, text=codex_lines_failed("Usage limit reached"), exit_code=0, process_started=True)
        row = await w.ask(cid, "Und was ist mit Bremen?", client_message_id="msg-0002")
        require_equal((row["status"], row["error_code"]), ("blocked", "quota"))
        require_equal(w.activity(row["activity_id"])["state"], "cancelled")
        require(w.activity(row["activity_id"])["held_reason"] != "")
        require_equal(len(w.cli.calls), 1)


def codex_lines_failed(message):
    return "\n".join([json.dumps({"type": "thread.started", "thread_id": "local"}),
                      json.dumps({"type": "turn.failed", "error": {"message": message}})])


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
