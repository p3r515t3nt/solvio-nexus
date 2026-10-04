"""C6 integration through HTTPS, with real stores and the existing fake CLI.

Only this module's tests are collected: the processing fixtures stay behind T.
No provider, task worker or production state is used.
"""
import asyncio
import json
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
import test_conversation_processing as T


async def _completed(w, request_id, objective):
    cid = await w.chat(request_id)
    w.cli.assessments.append(T.assessment("auftrag_recherche", objective, auftragsbezug="neu"))
    started = await w.ask(cid, objective)
    require_equal((started["status"], started["revision"]), ("completed", 1), started["error_code"])
    require(started["task_id"].startswith("at-"))
    await T._finish_task(w, started["task_id"], started["run_id"])
    return cid, started


async def _detail(w, cid):
    response = await w.client.get(T.CE.PREFIX + "/" + cid)
    require_equal(response.status, 200, await response.text())
    return await response.json()


async def _delivery(w, cid, delivery_id):
    response = await w.client.get(f"{T.CE.PREFIX}/{cid}/deliveries/{delivery_id}")
    require_equal(response.status, 200, await response.text())
    return await response.json()


async def _ambiguous(w, request_id="chat-choice"):
    first_cid, first = await _completed(w, "chat-bikes", "Vergleiche rote Fahrradtaschen im Preis.")
    second_cid, second = await _completed(w, "chat-camping", "Vergleiche blaue Campingkocher nach Gewicht.")
    cid = await w.chat(request_id)
    text = "Optimiere es weiter."
    # A guessed pointer must not override the core's multiple-recent guard.
    w.cli.assessments.append(T.assessment("auftrag_recherche", text,
        auftragsbezug="fortsetzen", fortsetzung_von=first["task_id"]))
    asked = await w.ask(cid, text)
    require_equal((asked["status"], asked["task_id"], asked["revision"]), ("completed", "", 0))
    dispatch = json.loads(asked["dispatch"])
    require_equal(dispatch["action_class"], T.PR.ACTION_CLARIFY)
    refs = dispatch.get("related_candidates", [])
    require_equal({ref["task_id"] for ref in refs}, {first["task_id"], second["task_id"]})
    require_equal([ref["match_kind"] for ref in refs], ["recent", "recent"])
    require_equal(refs[0]["task_id"], second["task_id"], "initial order is newest first")
    return cid, asked, refs, {first["task_id"]: (first_cid, first), second["task_id"]: (second_cid, second)}


async def t_https_new_chat_continues_the_same_task_and_exposes_source_without_touching_old_messages():
    objective = "Vergleiche drei Hotels in Hamburg mit Quellen."
    async with T.world() as w:
        source, started = await _completed(w, "chat-hamburg", objective)
        old_detail = await _detail(w, source)
        old_links = w.chat_store.task_links(source)
        cid = await w.chat("chat-followup")
        text = "Ergaenze zu Hamburg bitte auch Bremen."
        w.cli.assessments.append(T.assessment("auftrag_recherche", text,
            auftragsbezug="fortsetzen", fortsetzung_von=started["task_id"]))
        changed = await w.ask(cid, text)
        require_equal((changed["status"], changed["task_id"], changed["revision"]),
                      ("completed", started["task_id"], 2), changed["error_code"])
        require(changed["run_id"] != started["run_id"])
        require_equal(w.tasks(), 1, "cross-chat follow-up created another task")
        require_equal(len(w.ledger.runs_for_task(started["task_id"])), 2)
        require_equal(T.TR.revision_for_run(w.ledger, changed["run_id"])["text"], text)
        require_equal(w.ledger.get_task(started["task_id"]).objective, objective)
        require_equal(w.chat_store.task_links(source), old_links)
        after = await _detail(w, source)
        require_equal(after["messages"], old_detail["messages"])
        require_equal(after["conversation"], old_detail["conversation"])
        require_equal(len(w.cli.of("answer")), 0)
        require_equal(len(w.cli.of("assess", changed["delivery_id"])), 1)
        expected_source = {"conversation_id": source, "title": old_detail["conversation"]["title"]}
        detail = await _detail(w, cid)
        require_equal(detail["messages"][0]["delivery"].get("source_chat"), expected_source)
        public = await _delivery(w, cid, changed["delivery_id"])
        require_equal(public.get("source_chat"), expected_source)
        require_equal((public["task_id"], public["run_id"], public["revision"]),
                      (started["task_id"], changed["run_id"], 2))
        require_equal([card["id"] for card in detail["auftraege"]], [changed["run_id"]])
        require(expected_source["title"] in detail["messages"][-1]["text"])


async def t_saved_selection_keeps_its_order_after_another_chat_adds_a_newer_task():
    async with T.world() as w:
        cid, asked, refs, sources = await _ambiguous(w)
        saved = json.loads(asked["dispatch"])["related_candidates"]
        question = w.chat_store.messages(cid)[-1]["text"]
        require("1." in question and "2." in question)
        _, newer = await _completed(w, "chat-newer", "Recherchiere Sternbilder der Suedhalbkugel.")
        selected = refs[0]["task_id"]
        w.cli.assessments.append(T.assessment("auftrag_recherche", "Den ersten bitte.",
            auftragsbezug="fortsetzen", fortsetzung_von=selected))
        changed = await w.ask(cid, "Den ersten bitte.", client_message_id="msg-choice")
        require_equal((changed["status"], changed["task_id"], changed["revision"]),
                      ("completed", selected, 2), changed["error_code"])
        require_equal(w.tasks(), 3)
        require_equal(len(w.ledger.runs_for_task(newer["task_id"])), 1)
        require_equal(json.loads(w.chat_store.delivery(cid, asked["delivery_id"])["dispatch"])["related_candidates"], saved)
        prompt = w.cli.of("assess", changed["delivery_id"])[0]["prompt"]
        require(prompt.index(refs[0]["task_id"]) < prompt.index(refs[1]["task_id"]))
        require(newer["task_id"] not in prompt, "new candidate changed a pending choice")
        require_equal(len(w.cli.of("answer")), 0)
        source_id, _ = sources[selected]
        require_equal((await _delivery(w, cid, changed["delivery_id"]))["source_chat"]["conversation_id"], source_id)
        followup = T.TR.revision_for_run(w.ledger, changed["run_id"])["text"]
        require("Optimiere es weiter." in followup and "Den ersten bitte." in followup)
        require("Campingkocher" not in followup and "Fahrradtaschen" not in followup,
                "historical objective was copied into the new instruction")


async def t_explicit_new_topic_ignores_a_model_continuation_pointer():
    async with T.world() as w:
        source, prior = await _completed(w, "chat-original", "Vergleiche Hotels in Hamburg mit Quellen.")
        cid = await w.chat("chat-independent")
        text = "Neues Thema: Recherchiere die Hamburger Hafenbruecken."
        w.cli.assessments.append(T.assessment("auftrag_recherche", text,
            auftragsbezug="neu", fortsetzung_von=prior["task_id"]))
        created = await w.ask(cid, text)
        require_equal((created["status"], created["revision"]), ("completed", 1))
        require(created["task_id"] != prior["task_id"])
        require_equal(w.tasks(), 2)
        require_equal(len(w.ledger.runs_for_task(prior["task_id"])), 1)
        require_equal(w.ledger.get_task(created["task_id"]).objective, text)
        require_equal(w.ledger.get_task(created["task_id"]).conversation_ref, cid)
        require("source_chat" not in await _delivery(w, cid, created["delivery_id"]))
        require_equal(len(w.cli.of("answer")), 0)


async def t_short_mach_weiter_continues_the_only_earlier_task():
    async with T.world() as w:
        _, started = await _completed(w, "chat-short-source", "Vergleiche Hotels in Hamburg mit Quellen.")
        cid = await w.chat("chat-short-target")
        text = "Mach weiter"
        require_equal(len(text), 11)
        w.cli.assessments.append(T.assessment("auftrag_recherche", text,
            auftragsbezug="fortsetzen", fortsetzung_von=started["task_id"]))
        row = await w.ask(cid, text)
        require_equal((row["status"], row["task_id"], row["revision"]),
                      ("completed", started["task_id"], 2), row["error_code"])
        require_equal(T.TR.revision_for_run(w.ledger, row["run_id"])["text"], text)
        require_equal(w.tasks(), 1)
        require_equal(len(w.cli.of("answer")), 0, "short continuation was misrouted as a question")


async def t_two_topic_candidates_and_a_two_character_answer_ask_instead_of_choosing():
    """DEBT-0308 (Review Runde 22, Verifikation N-2b): die Mehrdeutigkeitsregel der Policy
    verlangt, dass ALLE Kandidaten aus dem Rueckfallweg stammen (`match_kind == "recent"`).
    Bei zwei THEMENTREFFERN griff sie nicht — ein „Ja" band eine kostenpflichtige
    Fortsetzung an einen der beiden, und allein die Einschaetzung entschied welchen.
    Jetzt fragt SOLVIO; die Auswahl bleibt beim Menschen."""
    async with T.world() as w:
        _, first = await _completed(w, "chat-hotels-hh", "Vergleiche Hotels in Hamburg nach Preis.")
        _, second = await _completed(w, "chat-hotels-hb", "Vergleiche Hotels in Bremen nach Preis.")
        cid = await w.chat("chat-hotels-short")
        text = "Hotels ja"
        from solvio.capabilities.agent import MIN_OBJECTIVE
        require(len(text) < MIN_OBJECTIVE, "der Satz muss unter der Mindestlaenge liegen: " + text)
        w.cli.assessments.append(T.assessment("auftrag_recherche", text,
            auftragsbezug="fortsetzen", fortsetzung_von=first["task_id"]))
        asked = await w.ask(cid, text)
        require_equal((asked["status"], asked["task_id"], asked["revision"]), ("completed", "", 0),
                      asked["error_code"])
        dispatch = json.loads(asked["dispatch"])
        require_equal(dispatch["action_class"], T.PR.ACTION_AMBIGUOUS)
        refs = dispatch.get("related_candidates", [])
        require_equal({ref["task_id"] for ref in refs}, {first["task_id"], second["task_id"]})
        require_equal(sorted({ref["match_kind"] for ref in refs}), ["topic"],
                      "this is the topic path — the recent guard would not have fired")
        require_equal(w.tasks(), 2, "no third task and no revision was created by a two-word answer")
        # Steht die Auswahlfrage offen, IST die naechste kurze Antwort die Auswahl.
        w.cli.assessments.append(T.assessment("auftrag_recherche", "Hamburg",
            auftragsbezug="fortsetzen", fortsetzung_von=first["task_id"]))
        chosen = await w.ask(cid, "Hamburg", client_message_id="msg-0002")
        require_equal((chosen["status"], chosen["task_id"], chosen["revision"]),
                      ("completed", first["task_id"], 2), chosen["error_code"])


async def t_a_pending_selection_answered_in_two_letters_still_chooses_and_never_loops():
    """Review Runde 23, K23B-3: die Bedingung `not related_selection_pending` sah aus wie
    Tiefenstaffelung — der Kandidatentest rettete sich naemlich ueber die Rueckfragekette,
    die mit „Hotels ja Hamburg" ueber die Mindestlaenge kam. Bei DURCHWEG sehr kurzen
    Antworten traegt allein diese Bedingung: ohne sie fragt SOLVIO endlos weiter, weil die
    Kette nie zwoelf Zeichen erreicht. Der Eintrag im Schuldenregister behauptete das
    Gegenteil; hier steht die Messung."""
    async with T.world() as w:
        _, first = await _completed(w, "chat-kurz-hamburg", "Vergleiche Hotels in Hamburg nach Preis.")
        _, second = await _completed(w, "chat-kurz-bremen", "Vergleiche Hotels in Bremen nach Preis.")
        cid = await w.chat("chat-kurz-auswahl")
        w.cli.assessments.append(T.assessment("auftrag_recherche", "ja",
            auftragsbezug="fortsetzen", fortsetzung_von=first["task_id"]))
        asked = await w.ask(cid, "ja")
        dispatch = json.loads(asked["dispatch"])
        require(dispatch["action_class"] in (T.PR.ACTION_AMBIGUOUS, T.PR.ACTION_CLARIFY),
                "zwei Kandidaten und zwei Zeichen ergeben eine Frage, keine Fortsetzung: "
                + dispatch["action_class"])
        require_equal((asked["task_id"], asked["revision"]), ("", 0))
        require_equal({ref["task_id"] for ref in dispatch.get("related_candidates", [])},
                      {first["task_id"], second["task_id"]}, "die Auswahl steht mit beiden Kandidaten offen")
        # Die Auswahl steht offen: zwei Zeichen genuegen jetzt, und zwar genau EINMAL.
        w.cli.assessments.append(T.assessment("auftrag_recherche", "HH",
            auftragsbezug="fortsetzen", fortsetzung_von=first["task_id"]))
        chosen = await w.ask(cid, "HH", client_message_id="msg-0002")
        require_equal((chosen["status"], chosen["task_id"], chosen["revision"]),
                      ("completed", first["task_id"], 2), chosen["error_code"])
        require_equal(json.loads(chosen["dispatch"])["action_class"], T.PR.ACTION_FOLLOWUP,
                      "die Antwort auf eine offene Auswahl ist die Auswahl, keine neue Frage")
        require_equal(w.tasks(), 2, "aus der Auswahl entsteht kein dritter Auftrag")


async def t_a_task_already_linked_here_survives_deletion_of_its_original_chat():
    async with T.world() as w:
        source, started = await _completed(w, "chat-once-source", "Vergleiche Hotels in Hamburg mit Quellen.")
        cid = await w.chat("chat-now-local")
        first_text = "Ergaenze zu Hamburg bitte Bremen."
        w.cli.assessments.append(T.assessment("auftrag_recherche", first_text,
            auftragsbezug="fortsetzen", fortsetzung_von=started["task_id"]))
        second = await w.ask(cid, first_text)
        require_equal((second["status"], second["task_id"], second["revision"]),
                      ("completed", started["task_id"], 2), second["error_code"])
        await T._finish_task(w, started["task_id"], second["run_id"], bind=False)
        deleted = await w.client.delete(T.CE.PREFIX + "/" + source, headers=w.headers)
        require_equal(deleted.status, 204, await deleted.text())
        next_text = "Ergaenze zum Vergleich bitte noch Luebeck."
        w.cli.assessments.append(T.assessment("auftrag_recherche", next_text,
            auftragsbezug="fortsetzen", fortsetzung_von=started["task_id"]))
        third = await w.ask(cid, next_text, client_message_id="msg-local-followup")
        require_equal((third["status"], third["task_id"], third["revision"]),
                      ("completed", started["task_id"], 3), third["error_code"])
        require_equal(w.tasks(), 1)
        require_equal(len(w.ledger.runs_for_task(started["task_id"])), 3)
        require_equal([link["revision"] for link in w.chat_store.task_links(cid)], [2, 3])
        require_equal(T.TR.revision_for_run(w.ledger, third["run_id"])["text"], next_text)
        require("source_chat" not in json.loads(third["dispatch"]))
        require("source_chat" not in await _delivery(w, cid, third["delivery_id"]))
        require("source_chat" not in await _delivery(w, cid, second["delivery_id"]),
                "a deleted source chat was still exposed")
        require_equal(len(w.cli.of("answer")), 0)


async def t_deleted_foreign_and_invented_candidates_never_create_a_revision():
    async with T.world() as w:
        deleted_source, deleted = await _completed(w, "chat-deleted", "Pruefe Hotels in Rostock mit Quellen.")
        removed = await w.client.delete(T.CE.PREFIX + "/" + deleted_source, headers=w.headers)
        require_equal(removed.status, 204, await removed.text())
        own_source = await w.chat("chat-invalid-link")
        w.chat_store.add_message(own_source, "user", "Dieser Archivverweis ist keine Autoritaet.")
        foreign = w.ledger.create_task(objective="Ein anderer Besitzer vergleicht Kaelteanlagen.",
            scope="research", created_origin="trusted_dashboard", created_principal="another-owner")
        foreign_run = w.ledger.create_run(task_id=foreign.task_id)
        await T._finish_task(w, foreign.task_id, foreign_run.run_id)
        w.chat_store.add_task_link(own_source, foreign.task_id, foreign_run.run_id, source="test-invalid")
        invented = "at-000000000000cafe"
        w.chat_store.add_task_link(own_source, invented, "ar-000000000000cafe", source="test-invalid")
        before = w.tasks()
        for number, candidate in enumerate((deleted["task_id"], foreign.task_id, invented), 1):
            cid = await w.chat(f"chat-rejected-{number}")
            text = "Ueberarbeite diesen Auftrag bitte."
            w.cli.assessments.append(T.assessment("auftrag_recherche", text,
                auftragsbezug="fortsetzen", fortsetzung_von=candidate))
            row = await w.ask(cid, text)
            require_equal((row["status"], row["task_id"], row["revision"]), ("completed", "", 0), row["error_code"])
            require_equal(json.loads(row["dispatch"])["action_class"], T.PR.ACTION_CLARIFY)
            require_equal(w.chat_store.task_links(cid), [])
            require("source_chat" not in await _delivery(w, cid, row["delivery_id"]))
        require_equal(w.tasks(), before)
        require_equal(len(w.ledger.runs_for_task(deleted["task_id"])), 1)
        require_equal(len(w.ledger.runs_for_task(foreign.task_id)), 1)
        require_equal(len(w.cli.of("answer")), 0)


async def t_local_task_followup_does_not_depend_on_a_source_deleted_after_prepare():
    async with T.world() as w:
        source, started = await _completed(w, "chat-local-race-source", "Vergleiche Hotels in Hamburg mit Quellen.")
        cid = await w.chat("chat-local-race-target")
        text = "Ergaenze zu Hamburg bitte Bremen."
        w.cli.assessments.append(T.assessment("auftrag_recherche", text,
            auftragsbezug="fortsetzen", fortsetzung_von=started["task_id"]))
        second = await w.ask(cid, text)
        require_equal((second["status"], second["revision"]), ("completed", 2))
        await T._finish_task(w, started["task_id"], second["run_id"], bind=False)
        third_text = "Ergaenze fuer Hamburg bitte die Hotelkategorien."
        w.cli.assessments.append(T.assessment("auftrag_recherche", third_text,
            auftragsbezug="fortsetzen", fortsetzung_von=started["task_id"]))
        actual = T.TR.prepare
        removed = []

        def prepare_then_delete(*args, **kwargs):
            prepared = actual(*args, **kwargs)
            removed.append(w.chat_store.delete_conversation(source))
            return prepared

        with patch.object(T.TR, "prepare", prepare_then_delete):
            third = await w.ask(cid, third_text, client_message_id="msg-local-race")
        require_equal(len(removed), 1, "test did not delete the source after prepare")
        require_equal((third["status"], third["task_id"], third["revision"]),
                      ("completed", started["task_id"], 3), third["error_code"])
        require("source_chat" not in json.loads(third["dispatch"]),
                "local task admission still depended on its earlier chat")
        require_equal(w.tasks(), 1)
        require_equal(len(w.ledger.runs_for_task(started["task_id"])), 3)
        require_equal(len(w.cli.of("answer")), 0)


async def t_deleting_a_source_of_a_saved_choice_requires_a_new_question():
    async with T.world() as w:
        cid, _, refs, sources = await _ambiguous(w)
        removed_source, _ = sources[refs[0]["task_id"]]
        response = await w.client.delete(T.CE.PREFIX + "/" + removed_source, headers=w.headers)
        require_equal(response.status, 204, await response.text())
        # Even a surviving pointer cannot turn the old 'first' into the new first.
        survivor = refs[1]["task_id"]
        w.cli.assessments.append(T.assessment("auftrag_recherche", "Den ersten bitte.",
            auftragsbezug="fortsetzen", fortsetzung_von=survivor))
        row = await w.ask(cid, "Den ersten bitte.", client_message_id="msg-stale-choice")
        require_equal((row["status"], row["task_id"], row["revision"]), ("completed", "", 0))
        require_equal(json.loads(row["dispatch"])["action_class"], T.PR.ACTION_CLARIFY)
        require_equal(w.tasks(), 2)
        require_equal(len(w.ledger.runs_for_task(survivor)), 1)
        require_equal(len(w.cli.of("answer")), 0)


async def t_source_is_rechecked_after_prepare_before_followup_admission():
    async with T.world() as w:
        source, started = await _completed(w, "chat-source-race", "Vergleiche Hotels in Hamburg mit Quellen.")
        cid = await w.chat("chat-target-race")
        text = "Ergaenze zu Hamburg bitte Bremen."
        w.cli.assessments.append(T.assessment("auftrag_recherche", text,
            auftragsbezug="fortsetzen", fortsetzung_von=started["task_id"]))
        actual = T.TR.prepare
        removed = []

        def prepare_then_delete(*args, **kwargs):
            prepared = actual(*args, **kwargs)
            removed.append(w.chat_store.delete_conversation(source))
            return prepared

        with patch.object(T.TR, "prepare", prepare_then_delete):
            row = await w.ask(cid, text)
        require_equal(len(removed), 1, "test never reached the admission boundary")
        require_equal((row["status"], row["error_code"]), ("blocked", "followup_not_available"))
        require_equal(row["task_id"], "")
        require_equal(len(w.ledger.runs_for_task(started["task_id"])), 1)
        require_equal(w.tasks(), 1)
        require_equal(w.chat_store.task_links(cid), [])


async def t_cross_chat_admitted_revision_is_reused_after_a_crash_without_reclassification():
    async with T.world() as w:
        source, started = await _completed(w, "chat-replay-source", "Vergleiche Hotels in Hamburg mit Quellen.")
        cid = await w.chat("chat-replay-target")
        text = "Ergaenze zu Hamburg bitte Bremen."
        w.cli.assessments.append(T.assessment("auftrag_recherche", text,
            auftragsbezug="fortsetzen", fortsetzung_von=started["task_id"]))
        crashed = asyncio.Event()
        loop = asyncio.get_running_loop()

        def fail_complete(*args, **kwargs):
            loop.call_soon_threadsafe(crashed.set)
            raise asyncio.CancelledError()

        with patch.object(w.chat_store, "complete_delivery", fail_complete):
            delivery_id = await w.post(cid, text)
            await asyncio.wait_for(crashed.wait(), 10)
            await w.processor.wait_idle(5)
        runs = w.ledger.runs_for_task(started["task_id"])
        require_equal(len(runs), 2, "the follow-up must already have been admitted")
        admitted = next(run for run in runs if run.run_id != started["run_id"])
        require_equal(w.chat_store.task_links(cid), [])
        require_equal(w.chat_store.delivery(cid, delivery_id)["status"], "running")
        before_calls = list(w.cli.calls)
        restarted = w.second_processor()
        try:
            require_equal(await restarted.recover(), 1)
            row = await w.settled(cid, delivery_id)
            await restarted.wait_idle(5)
            require_equal((row["status"], row["task_id"], row["run_id"], row["revision"]),
                          ("completed", started["task_id"], admitted.run_id, 2), row["error_code"])
            require_equal(w.cli.calls, before_calls, "recovery reran a model or answer")
            require_equal(len(w.ledger.runs_for_task(started["task_id"])), 2)
            require_equal(w.tasks(), 1)
            require_equal([(link["run_id"], link["revision"]) for link in w.chat_store.task_links(cid)],
                          [(admitted.run_id, 2)])
            require_equal((await _delivery(w, cid, delivery_id))["source_chat"]["conversation_id"], source)
        finally:
            restarted.stop()


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
