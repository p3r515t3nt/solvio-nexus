"""Text task profiles reuse the authenticated task start and its recovery.

HTTPS and Core contracts are real; the classifier provider is the existing
processing fake. The original research path and carried attachments stay intact.
"""
import asyncio
import base64
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
import test_conversation_processing as T
from solvio.capabilities.agent import AgentCapabilities, SPECS
from solvio.cognition import continuity as C, policy as PO, prompt as P
from solvio.cognition.types import ModelTier


@asynccontextmanager
async def _world():
    async with T.world() as w:
        # The historical endpoint fixture only registers research and build.
        w.router.register(SPECS["agent_task_task"], AgentCapabilities(w.orch).task)
        yield w


def _document():
    return {"operation": "extract_text", "format": "rtf",
            "content_b64": base64.b64encode(b"{\\rtf1\\ansi Umsatz: 12 Prozent.}").decode()}


def _artifact_count(w, run_id):
    with w.ledger._open() as db:
        return db.execute("SELECT COUNT(*) FROM agent_artifacts WHERE run_id=?", (run_id,)).fetchone()[0]


async def t_general_work_uses_the_existing_task_capability_and_authenticated_grant():
    async with _world() as w:
        cid = await w.chat()
        objective = "Erstelle mir eine lokale Packliste als Textdatei."
        w.cli.assessments.append(T.assessment("auftrag_recherche", objective, auftragsprofil="allgemein"))
        row = await w.ask(cid, objective)
        require_equal(row["status"], "completed", row["error_code"])
        task = w.ledger.get_task(row["task_id"])
        require_equal((task.scope, task.objective, task.conversation_ref), ("task", objective, cid))
        require_equal(json.loads(row["dispatch"])["scope"], "task")
        grant = w.orch.task_authority.for_run(row["run_id"])
        require_equal((grant.receipt_method, grant.authorizer), ("dashboard_session", "local-owner"))
        require(w.orch.task_starts.ready(row["run_id"]))
        require_equal(w.orch.costs.view(row["task_id"])["ask_threshold_cents"], 1000)
        require_equal(len(w.cli.of("assess")), 1, "profile selection added a model round")
        require_equal(len(w.cli.of("answer")), 0)
        require_equal(w.tasks(), 1)


async def t_a_mail_question_starts_a_private_task_with_mail_and_a_general_one_gets_none():
    """Kurskorrektur S1 / ADR-0040: only the `persoenlich` profile carries the
    private-data marker from the chat into the start; only then are the mail
    reads granted. A general order in the same chat reads no mail."""
    import test_native_mail_calendar_tools as MT
    from solvio.capabilities import gmail as G
    async with _world() as w:
        G.register(w.router, G.GmailCapabilities(MT.RecordingGmail([])))
        cid = await w.chat()
        question = "Was ist diese Woche in meinen Mails wichtig?"
        w.cli.assessments.append(T.assessment("auftrag_recherche", question, auftragsprofil="persoenlich"))
        row = await w.ask(cid, question)
        require_equal(row["status"], "completed", row["error_code"])
        require_equal(json.loads(row["dispatch"]).get("private_data"), True)
        task = w.ledger.get_task(row["task_id"])
        require_equal((task.scope, task.conversation_ref), ("task", cid))
        mail = {"gmail_list_recent", "gmail_search", "gmail_read_message", "gmail_read_thread"}
        granted = {c.name for c in w.orch.task_authority.for_run(row["run_id"]).capabilities}
        require(mail <= granted, granted)
        other = "Erstelle mir eine lokale Packliste als Textdatei."
        w.cli.assessments.append(T.assessment("auftrag_recherche", other, auftragsprofil="allgemein"))
        general = await w.ask(await w.chat(), other, client_message_id="msg-0002")
        require_equal(general["status"], "completed", general["error_code"])
        require(json.loads(general["dispatch"]).get("private_data") is None)
        granted = {c.name for c in w.orch.task_authority.for_run(general["run_id"]).capabilities}
        require(not granted & mail, granted)


async def t_local_creation_can_be_general_while_repository_work_requires_a_project():
    async with _world() as w:
        local = await w.chat("chat-local")
        text = "Erstelle eine kleine eigenstaendige HTML-Seite als lokale Datei."
        w.cli.assessments.append(T.assessment("auftrag_bau", text, auftragsprofil="allgemein"))
        row = await w.ask(local, text)
        require_equal(row["status"], "completed", row["error_code"])
        require(row["task_id"], "local creation was stopped at repository selection")
        require_equal(w.ledger.get_task(row["task_id"]).scope, "task")
        project = await w.chat("chat-project")
        text = "Aendere die Anmeldung in meinem vorhandenen Webprojekt."
        # Even a research route must not override the explicit project profile.
        w.cli.assessments.append(T.assessment("auftrag_recherche", text, auftragsprofil="projekt"))
        question = await w.ask(project, text)
        require_equal((question["status"], question["task_id"]), ("completed", ""))
        require_equal(w.chat_store.messages(project)[-1]["text"], T.PR.BUILD_NEEDS_PROJECT_TEXT)
        require_equal(w.tasks(), 1)


async def t_attested_iphone_message_uses_the_same_general_task_start_without_an_extra_approval():
    async with _world() as w:
        device, app, headers = await w.device()
        response = await w.create("chat-iphone-task", client=app, headers=headers)
        require_equal(response.status, 201, await response.text())
        cid = (await response.json())["conversation_id"]
        text = "Erstelle meine Packliste als lokale Textdatei."
        message = {"conversation_id": cid, "client_message_id": "msg-iphone-task", "text": text}
        payload = await w.app_proof(device, app, headers, message)
        w.cli.assessments.append(T.assessment("auftrag_bau", text, auftragsprofil="allgemein"))
        response = await app.post(f"{T.CE.PREFIX}/{cid}/messages", json=payload, headers=headers)
        require_equal(response.status, 202, await response.text())
        row = await w.settled(cid, (await response.json())["delivery_id"])
        require_equal(row["status"], "completed", row["error_code"])
        task = w.ledger.get_task(row["task_id"])
        require_equal((task.scope, task.conversation_ref, task.created_principal), ("task", cid, "local-owner"))
        grant = w.orch.task_authority.for_run(row["run_id"])
        require_equal(grant.receipt_method, "app_session")
        require_equal(grant.authorizer, "local-owner")
        require_equal(await w.store.list_pending(), [])
        require_equal(w.tasks(), 1)
        require_equal(len(w.cli.calls), 1)


async def t_absent_and_specialized_profiles_keep_the_original_research_and_build_contract():
    async with _world() as w:
        for number, extra in enumerate(({}, {"auftragsprofil": "spezialisiert"}), 1):
            cid = await w.chat(f"chat-research-{number}")
            text = "Recherchiere Hotels in Hamburg mit belastbaren Quellen."
            w.cli.assessments.append(T.assessment("auftrag_recherche", text, **extra))
            row = await w.ask(cid, text)
            require_equal(row["status"], "completed", row["error_code"])
            require_equal(w.ledger.get_task(row["task_id"]).scope, "research")
            require_equal(json.loads(row["dispatch"])["scope"], "research")
            project = await w.chat(f"chat-build-{number}")
            text = "Repariere den Login in meinem vorhandenen Projekt."
            w.cli.assessments.append(T.assessment("auftrag_bau", text, **extra))
            asked = await w.ask(project, text)
            require_equal((asked["status"], asked["task_id"]), ("completed", ""))
            require_equal(w.chat_store.messages(project)[-1]["text"], T.PR.BUILD_NEEDS_PROJECT_TEXT)
        require_equal(w.tasks(), 2)


async def t_own_and_inherited_attachments_keep_research_even_for_a_general_profile():
    async with _world() as w:
        direct = await w.chat("chat-document")
        text = "Erstelle einen Bericht aus dem angehaengten Dokument."
        w.cli.assessments.append(T.assessment("auftrag_bau", text, auftragsprofil="allgemein"))
        row = await w.ask(direct, text, attachments=_document())
        require_equal(row["status"], "completed", row["error_code"])
        require(row["task_id"], "general local creation with an attachment was not accepted")
        require_equal(w.ledger.get_task(row["task_id"]).scope, "research")
        require_equal(_artifact_count(w, row["run_id"]), 1)
        inherited = await w.chat("chat-inherited")
        w.cli.assessments.append(T.assessment("klaerung", text, klaerungsfrage="Welche Werte sollen hinein?"))
        asked = await w.ask(inherited, text, attachments=_document())
        require_equal(json.loads(asked["dispatch"])["action_class"], T.PR.ACTION_CLARIFY)
        w.cli.assessments.append(T.assessment("auftrag_recherche", "Die Umsatzzahlen bitte.",
                                             auftragsprofil="allgemein"))
        row = await w.ask(inherited, "Die Umsatzzahlen bitte.", client_message_id="msg-reply")
        require_equal(row["status"], "completed", row["error_code"])
        require_equal(w.ledger.get_task(row["task_id"]).scope, "research")
        dispatch = json.loads(row["dispatch"])
        require_equal((dispatch["scope"], dispatch["attachment_turn"]), ("research", asked["delivery_id"]))
        require_equal(_artifact_count(w, row["run_id"]), 1)
        require_equal(w.tasks(), 2)


async def t_followup_keeps_the_task_scope_despite_a_different_profile():
    async with _world() as w:
        cid = await w.chat()
        text = "Vergleiche Hotels in Hamburg mit Quellen."
        w.cli.assessments.append(T.assessment("auftrag_recherche", text))
        first = await w.ask(cid, text)
        await T._finish_task(w, first["task_id"], first["run_id"])
        text = "Ergaenze bitte auch Angebote aus Bremen."
        w.cli.assessments.append(T.assessment("auftrag_recherche", text,
            fortsetzung_von=first["task_id"], auftragsprofil="allgemein"))
        row = await w.ask(cid, text, client_message_id="msg-followup")
        require_equal((row["status"], row["task_id"], row["revision"]), ("completed", first["task_id"], 2))
        require_equal(w.ledger.get_task(row["task_id"]).scope, "research")
        require_equal(w.tasks(), 1)


async def t_recorded_general_dispatch_recovers_before_or_after_admission_without_reclassification():
    for point in ("before_admit", "after_admit"):
        async with _world() as w:
            cid = await w.chat()
            objective = "Erstelle eine lokale Packliste als Textdatei."
            w.cli.assessments.append(T.assessment("auftrag_recherche", objective, auftragsprofil="allgemein"))
            crashed = asyncio.Event()
            loop = asyncio.get_running_loop()

            async def fail_execute(*args, **kwargs):
                crashed.set()
                raise asyncio.CancelledError()

            def fail_complete(*args, **kwargs):
                loop.call_soon_threadsafe(crashed.set)
                raise asyncio.CancelledError()

            target, name, function = ((w.router, "execute", fail_execute) if point == "before_admit"
                                      else (w.chat_store, "complete_delivery", fail_complete))
            with patch.object(target, name, function):
                delivery_id = await w.post(cid, objective)
                await asyncio.wait_for(crashed.wait(), 10)
                await w.processor.wait_idle(5)
            dispatch = json.loads(w.chat_store.delivery(cid, delivery_id)["dispatch"])
            require_equal(dispatch["scope"], "task", "scope must be durable before task admission")
            require_equal(w.tasks(), 0 if point == "before_admit" else 1)
            restarted = w.second_processor()
            try:
                require_equal(await restarted.recover(), 1)
                row = await w.settled(cid, delivery_id)
                await restarted.wait_idle(5)
                require_equal((row["status"], row["revision"]), ("completed", 1), row["error_code"])
                require_equal(w.ledger.get_task(row["task_id"]).scope, "task")
                require_equal(w.tasks(), 1)
                require_equal(len(w.ledger.recent_runs()), 1)
                require_equal(len(w.cli.of("assess")), 1)
                require_equal(len(w.cli.of("answer")), 0)
            finally:
                restarted.stop()


def t_profile_is_optional_closed_and_does_not_change_the_voice_schema():
    raw = T.assessment("auftrag_recherche", "Erstelle eine lokale Liste.")
    original = PO.validate(json.dumps(raw), view=C.ContinuityView(),
                           scope_text=raw["ziel"], tier=ModelTier.MINI)
    require_equal(original.task_profile, "")
    for profile in ("allgemein", "spezialisiert", "projekt", "persoenlich"):
        selected = PO.validate(json.dumps(dict(raw, auftragsprofil=profile)), view=C.ContinuityView(),
                                scope_text=raw["ziel"], tier=ModelTier.MINI)
        require_equal(selected.task_profile, profile)
    for invalid in ("shell", None, [], 7):
        try:
            PO.validate(json.dumps(dict(raw, auftragsprofil=invalid)), view=C.ContinuityView(),
                        scope_text=raw["ziel"], tier=ModelTier.MINI)
        except PO.AssessmentInvalid as exc:
            require_equal(exc.reason, "unknown_task_profile")
        else:
            raise AssertionError("unrecognized task profile was accepted")
    voice = P.build_request(model="fake", user_text=raw["ziel"], register="Leer.")
    require("auftragsprofil" not in json.dumps(voice, ensure_ascii=False))
    require_equal(voice["input"][0]["content"], P.INSTRUCTION)


async def t_https_general_task_runs_the_existing_native_lifecycle_and_preserves_scope_on_followup():
    import test_native_task_entry as NT

    actual_text_invocation = T.U.text_invocation
    async with T.world() as w:
        @asynccontextmanager
        async def existing_world():
            yield w

        # Reuse the established local planner/native-provider doubles on the
        # real HTTPS world. No direct task-start endpoint substitutes the chat.
        with patch.object(NT.E, "world", existing_world):
            async with NT.world(mode="ok", worker="codex") as native:
                require(native is w)
                cid = await w.chat()
                w.cli.assessments.append(T.assessment("auftrag_recherche", NT.OBJECTIVE,
                    auftragsprofil="allgemein"))
                row = await w.ask(cid, NT.OBJECTIVE)
                require_equal(row["status"], "completed", row["error_code"])
                task_id = row["task_id"]
                require_equal(w.ledger.get_task(task_id).scope, "task")
                first_claims = w.invocations(row["activity_id"])
                require_equal([(claim["phase"], claim["state"]) for claim in first_claims],
                              [("text_chat", "finished")])
                require_equal(w.activity(row["activity_id"])["state"], "completed")
                files = []
                for revision in (1, 2):
                    with patch.object(T.U, "text_invocation", actual_text_invocation):
                        run = await NT.drive(w, row["run_id"])
                    require_equal(run.state, NT.S.SUCCEEDED, run.failure_category + ": " + run.result_summary)
                    require_equal((run.planner_calls, run.assessment_calls, run.specialist_count), (1, 1, 1))
                    steps = w.ledger.steps_for_run(run.run_id)
                    workers = [step for step in steps if step.kind == "specialist"]
                    require_equal([step.state for step in workers], ["succeeded"])
                    require(any(step.kind == "capability" and step.capability == "portal_list"
                                and step.state == "succeeded" for step in steps))
                    observation = next(artifact for artifact in w.ledger.artifacts_for_run(run.run_id)
                                       if artifact.kind == "native_task_observation")
                    receipt = json.loads(Path(observation.path).read_text())
                    require_equal((receipt["revision"], receipt["cost"]["operation_id"],
                                   receipt["cost"]["settlement_state"]),
                                  (revision, workers[0].step_id, "settled"))
                    descriptors, _ = NT.RF.describe_files(w.ledger, run.run_id)
                    require_equal([item["name"] for item in descriptors], ["answer.txt"])
                    _, content = NT.RF.read_result(w.ledger, run.run_id, descriptors[0]["id"])
                    require(f"Native result revision {revision}".encode() in content)
                    require(b"portale" in content)
                    files.append((run.run_id, descriptors[0]["id"], content))
                    with w.ledger._open() as db:
                        turns = [dict(item) for item in db.execute(
                            "SELECT t.state, t.terminal_status, i.state AS claim_state "
                            "FROM agent_native_turns t JOIN agent_provider_invocations i USING(invocation_id) "
                            "WHERE t.run_id=?", (run.run_id,))]
                    require_equal(turns, [{"state": "terminal", "terminal_status": "completed", "claim_state": "finished"}])
                    public = await (await w.client.get(
                        f"{T.CE.PREFIX}/{cid}/deliveries/{row['delivery_id']}")).json()
                    require_equal([item["name"] for item in public["files"]], ["answer.txt"])
                    if revision == 1:
                        followup = "Ergaenze den bisherigen Portalbefund genauer."
                        w.cli.assessments.append(T.assessment("auftrag_recherche", followup,
                            fortsetzung_von=task_id, auftragsprofil="spezialisiert"))
                        row = await w.ask(cid, followup, client_message_id="msg-native-followup")
                        require_equal((row["status"], row["task_id"], row["revision"]), ("completed", task_id, 2))
                        require_equal(w.ledger.get_task(task_id).scope, "task")
                require_equal(w.tasks(), 1)
                require_equal(len(w.calls), 2)
                require_equal(len({session.session_id for _, session in w.calls}), 1,
                              "follow-up abandoned the existing native session")
                require_equal(len(w.cli.of("assess")), 2)
                require_equal(len(w.cli.of("answer")), 0)
                require_equal(w.invocations(first_claims[0]["activity_id"]), first_claims)
                model_calls = [json.loads(line) for line in (w.folder / "calls.jsonl").read_text().splitlines()]
                require_equal([call["kind"] for call in model_calls], ["plan", "assessment"] * 2)
                for run_id, artifact_id, original in files:
                    require_equal(NT.RF.read_result(w.ledger, run_id, artifact_id)[1], original)


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
