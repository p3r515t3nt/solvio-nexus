"""C6: bounded retrieval from the real canonical conversation store, no models."""
from contextlib import contextmanager
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from solvio.conversation import store as C


class World:
    def __init__(self, path):
        self.now = 1000.0
        self.serial = 0
        self.store = C.ConversationStore(path, now_fn=lambda: self.now).open()
        self.current = self.chat(title="Aktueller Chat")

    def tick(self):
        self.now += 1
        return self.now

    def chat(self, text="", *, owner="alice", title="", role="user"):
        self.tick()
        self.serial += 1
        row, _ = self.store.create_conversation(owner_principal=owner, title=title,
                                               client_request_id=f"new-{self.serial}")
        cid = row["conversation_id"]
        if text:
            self.message(cid, text, role=role)
        return cid

    def message(self, cid, text, *, role="user"):
        self.tick()
        return self.store.add_message(cid, role, text)

    def link(self, cid, task="at-one", run="ar-one"):
        self.tick()
        self.store.add_task_link(cid, task, run, source="task:synthetic")

    def search(self, text, **kw):
        return self.store.search_context("alice", text, current_conversation_id=self.current,
                                         before_created_at=self.tick(), **kw)

    def recent(self, **kw):
        return self.store.recent_task_links("alice", current_conversation_id=self.current,
                                            before_created_at=self.tick(), **kw)


@contextmanager
def world():
    with tempfile.TemporaryDirectory(prefix="solvio-chat-search-") as folder:
        with patch.dict(os.environ, {"SOLVIO_STATE_DIR": folder}):
            w = World(str(Path(folder) / "conversations.sqlite3"))
            try:
                yield w
            finally:
                w.store.close()


def t_only_active_explicit_owned_chats_and_their_links_are_retrieved():
    with world() as w:
        own = w.chat("Islandreise Kosten", title="Reiseplanung", role="assistant")
        w.link(own)
        foreign = w.chat("Islandreise privat", owner="bob")
        w.link(foreign, "at-bob", "ar-bob")
        inactive = w.chat("Islandreise archiviert")
        w.link(inactive, "at-inactive", "ar-inactive")
        w.store.conn.execute("UPDATE conversations SET status='archived' WHERE conversation_id=?", (inactive,))
        legacy, _ = w.store.begin_session("legacy", principal="alice")
        w.message(legacy, "Islandreise sprachlich")
        w.link(legacy, "at-legacy", "ar-legacy")
        w.message(w.current, "Islandreise in diesem Chat")
        w.link(w.current, "at-current", "ar-current")
        for result, kind in ((w.search("Islandreise"), "topic"), (w.recent(), "recent")):
            require_equal([hit["conversation_id"] for hit in result["hits"]], [own])
            hit = result["hits"][0]
            require_equal(set(hit), {"conversation_id", "title", "message_id", "sequence", "role", "excerpt", "task_links", "match_kind"})
            require_equal(hit["task_links"], [{"task_id": "at-one", "run_id": "ar-one"}])
            require_equal((hit["role"], hit["sequence"], hit["match_kind"]), ("assistant", 1, kind))
            require(not result["incomplete"])


def t_current_chat_is_checked_before_search_and_never_crosses_owners():
    with world() as w:
        other = w.chat("Private Notiz", owner="bob")
        for current, owner in ((other, "alice"), (w.current, ""), ("missing", "alice")):
            for function, args in ((w.store.search_context, (owner, "")), (w.store.recent_task_links, (owner,))):
                try:
                    function(*args, current_conversation_id=current, before_created_at=w.tick())
                except C.ConversationStoreError as exc:
                    require_equal(str(exc), "unknown_conversation")
                else:
                    raise AssertionError("unowned current chat admitted")
        w.store.delete_conversation(w.current)
        try:
            w.search("Notiz")
        except C.ConversationStoreError as exc:
            require_equal(str(exc), "unknown_conversation")
        else:
            raise AssertionError("deleted current chat admitted")


def t_old_topic_beyond_latest_hundred_chats_matches_a_whole_new_request():
    with world() as w:
        old = w.chat("Islandreise Kosten", title="Urlaubsplanung")
        for i in range(130):
            w.chat(f"Andere Angelegenheit Nummer {i}")
        require(old not in [r["conversation_id"] for r in w.store.list_conversations("alice", limit=100)])
        result = w.search("Ergänze den Bericht über die Islandreise", limit=1)
        require_equal([hit["conversation_id"] for hit in result["hits"]], [old])
        require(not result["incomplete"], "one matched topic is complete despite many unrelated chats")


def t_a_talkative_new_chat_never_pushes_an_older_topic_out_of_reach():
    """DEBT-0309 (Review Runde 22, Verifikation N-3): das Zeilenbudget band bisher GLOBAL.

    Ein einziger gespraechiger Chat verbrauchte es, und ein sachlich passender aelterer
    Chat verschwand still aus der Suche. Gemessen mit kurzen Nachrichten — genau dem
    produktionsueblichen Fall, in dem das Zeilen- lange vor dem Zeichenbudget bindet.
    """
    with world() as w:
        old = w.chat("Islandreise Kosten", title="Urlaubsplanung")
        chatty = w.chat("Ganz anderes Thema", title="Vielredner")
        # Der Vielredner traegt einen Auftrag: nur dann kann seine Kuerzung einen Kandidaten
        # verbergen und muss als unvollstaendig gemeldet werden (Review Runde 24, R24-1).
        w.link(chatty, "at-chatty", "ar-chatty")
        for i in range(C.CONTEXT_SCAN_ROWS + 200):
            w.message(chatty, f"Zeile {i} ohne Bezug")
        result = w.search("Was kostet die Islandreise?", limit=3)
        require_equal([hit["conversation_id"] for hit in result["hits"]], [old],
                      "the older topic must stay reachable behind a talkative chat")
        require(result["incomplete"], "the truncated chat with a task must be named as incomplete")
        # Die Kappe je Gespraech behaelt die JUENGSTEN Zeilen dieses Chats.
        w.message(chatty, "Islandreise doch noch erwaehnt")
        again = w.search("Islandreise", limit=3)
        require(chatty in [hit["conversation_id"] for hit in again["hits"]],
                "the newest line of the talkative chat is still searched")


def t_a_deep_chat_that_matches_is_not_reported_as_incomplete():
    """Review Runde 23, K23B-2: die Kappe je Gespraech meldete JEDE Kuerzung als
    unvollstaendig — und `related_incomplete` erzwingt im Chatweg eine Rueckfrage. Damit
    verfiel die chatuebergreifende Fortsetzung, sobald IRGENDEIN Quellchat mehr als
    CONTEXT_SCAN_ROWS_PER_CHAT Nachrichten trug: ein stiller Verlust wurde gegen einen
    lauten getauscht. Ein gekuerztes Gespraech, das in seinen juengsten Zeilen ohnehin
    passt, ist vertreten — nur ein gekuerztes OHNE Treffer kann etwas verbergen."""
    with world() as w:
        deep = w.chat("Ganz frueher Anfang", title="Tiefer Chat")
        for i in range(C.CONTEXT_SCAN_ROWS_PER_CHAT + 50):
            w.message(deep, f"Zwischenzeile {i}")
        w.message(deep, "Islandreise Kosten geklaert")
        hit = w.search("Was kostet die Islandreise?", limit=3)
        require_equal([h["conversation_id"] for h in hit["hits"]], [deep],
                      "the deep chat matches in its newest lines")
        require(not hit["incomplete"],
                "a trimmed chat that matched is represented — reporting it incomplete forces a needless question")
        # Ein gekuerztes Gespraech OHNE Treffer und OHNE Auftrag kann keinen Kandidaten
        # verbergen — Kandidaten entstehen nur aus verknuepften Auftraegen (Review Runde 24,
        # R24-1: sonst loeste jeder lange Plauder- oder Sprachchat eine Rueckfrage aus).
        quiet = w.chat("Anderes Thema", title="Stiller Tiefer")
        for i in range(C.CONTEXT_SCAN_ROWS_PER_CHAT + 50):
            w.message(quiet, f"Nichts zur Sache {i}")
        idle = w.search("Was kostet die Islandreise?", limit=3)
        require(not idle["incomplete"], "a long chat without any task cannot hide a candidate")
        # Traegt derselbe gekuerzte Chat einen Auftrag, kann dort etwas fehlen.
        w.link(quiet, "at-quiet", "ar-quiet")
        miss = w.search("Was kostet die Islandreise?", limit=3)
        require(miss["incomplete"], "a trimmed chat with a task and without a hit may hide a candidate")


def t_recent_links_are_recent_in_time_not_merely_the_newest_ones():
    """DEBT-0308 (Review Runde 22, Verifikation N-2b): der Rueckfallweg kannte keine Obergrenze.

    Eine jahrealte Verknuepfung trug eine Zwei-Zeichen-Fortsetzung, solange sie nur die
    neueste war. „Juengste Auftragsverknuepfungen" ist eine Aussage ueber Zeit. Ein
    Thementreffer darf dagegen beliebig alt sein; er traegt Evidenz.
    """
    with world() as w:
        stale = w.chat("Islandreise Kosten", title="Uralt")
        w.link(stale, "at-stale", "ar-stale")
        w.store.conn.execute("UPDATE conversation_task_links SET linked_at = ? WHERE task_id = ?",
                             (w.now - C.RECENT_LINK_MAX_AGE_SECONDS - 60.0, "at-stale"))
        require_equal([hit["conversation_id"] for hit in w.recent()["hits"]], [],
                      "a link older than the horizon is not a recent link")
        # Der Horizont ist an seinen WERT gebunden, nicht nur an seine Existenz: ein Verweis
        # KNAPP innerhalb bleibt ein juengster Verweis (Review Runde 23, H-2 — die Fixtureuhr
        # stand auf 1000, der "uralte" Zeitstempel wurde negativ, und die Mutation
        # `>= 0.0` — also gar kein Horizont — ueberlebte die Probe).
        inside = w.chat("Ein anderes Thema knapp im Horizont", title="Knapp drin")
        w.link(inside, "at-inside", "ar-inside")
        w.store.conn.execute("UPDATE conversation_task_links SET linked_at = ? WHERE task_id = ?",
                             (w.now - C.RECENT_LINK_MAX_AGE_SECONDS + 3600.0, "at-inside"))
        require_equal([hit["conversation_id"] for hit in w.recent()["hits"]], [inside],
                      "a link one hour inside the horizon is still a recent link")
        w.store.conn.execute("UPDATE conversation_task_links SET linked_at = ? WHERE task_id = ?",
                             (w.now - C.RECENT_LINK_MAX_AGE_SECONDS - 3600.0, "at-inside"))
        require_equal([hit["conversation_id"] for hit in w.recent()["hits"]], [],
                      "one hour outside the horizon and the same link is gone")
        require_equal([hit["conversation_id"] for hit in w.search("Islandreise")["hits"]], [stale],
                      "the same chat stays findable by topic — evidence, not recency")
        fresh = w.chat("Etwas Neues", title="Frisch")
        w.link(fresh, "at-fresh", "ar-fresh")
        require_equal([hit["conversation_id"] for hit in w.recent()["hits"]], [fresh])


def t_unicode_casefold_and_search_punctuation_are_literal():
    with world() as w:
        unicode_chat = w.chat("Straße und Cafe\u0301 am Fluss")
        punctuation = w.chat("Wörtliche Zeichen %_[*] und ein einzelnes '")
        w.chat("Normale Zeichen ABC ohne Suchzeichen")
        for query in ("STRASSE", "CAFÉ"):
            require_equal([h["conversation_id"] for h in w.search(query)["hits"]], [unicode_chat])
        for query in ("%", "_", "*", "[", "'", "%_[*]"):
            require_equal([h["conversation_id"] for h in w.search(query)["hits"]], [punctuation], query)
        require_equal(w.search("nichtvorhanden%_")["hits"], [])


def t_phrase_and_title_rank_before_recent_partial_word_hits_and_result_cap():
    with world() as w:
        exact = w.chat("Beschlossene Daten zur Reise", title="Islandreise Kosten")
        for i in range(8):
            w.chat(f"Kosten für eine andere Reise Nummer {i}")
        result = w.search("Islandreise Kosten", limit=1)
        require_equal([h["conversation_id"] for h in result["hits"]], [exact])
        require(result["incomplete"], "the other lexical matches were capped")


def t_long_unique_chat_is_complete_and_matching_messages_do_not_crowd_out_other_chats():
    with world() as w:
        old = w.chat("Islandreise frühere Planung")
        for _ in range(5):
            w.message(old, "Islandreise weiterer besprochener Punkt")
        latest = w.message(old, "Islandreise " + "ausführliche Notizen " * 80)
        w.link(old)
        single = w.search("Islandreise", limit=1, max_chars=31)
        require_equal(len(single["hits"]), 1)
        require_equal(single["hits"][0]["message_id"], latest, "the best tied hit is the latest message")
        require(len(single["hits"][0]["excerpt"]) <= 31)
        require(not single["incomplete"], "an intentional excerpt window does not hide another candidate")
        require(not w.recent()["incomplete"], "a long recent excerpt also leaves the candidate set complete")

        other = w.chat("Islandreise zweite Planung")
        # Move five matching messages of the first chat ahead of the second.
        for _ in range(5):
            w.message(old, "Islandreise neuester Diskussionspunkt")
        both = w.search("Islandreise", limit=2)
        require_equal([h["conversation_id"] for h in both["hits"]], [old, other])
        require(not both["incomplete"], "deduplication must precede the chat-candidate cap")
        capped = w.search("Islandreise", limit=1)
        require_equal([h["conversation_id"] for h in capped["hits"]], [old])
        require(capped["incomplete"], "an additional omitted chat is genuinely incomplete")


def t_deletion_and_loss_of_source_ownership_remove_excerpts_and_links_immediately():
    with world() as w:
        deleted = w.chat("Islandreise gelöscht")
        changed = w.chat("Islandreise übertragen")
        w.link(deleted, "at-deleted", "ar-deleted")
        w.link(changed, "at-changed", "ar-changed")
        require_equal(len(w.search("Islandreise")["hits"]), 2)
        w.store.delete_conversation(deleted)
        w.store.conn.execute("UPDATE conversations SET owner_principal='bob' WHERE conversation_id=?", (changed,))
        require_equal(w.search("Islandreise"), {"hits": [], "incomplete": False})
        require_equal(w.recent(), {"hits": [], "incomplete": False})


def t_cutoff_excludes_equal_and_future_messages_and_links_but_keeps_earlier_card():
    with world() as w:
        old = w.chat("Islandreise früher")
        w.link(old, "at-old", "ar-old")
        card_only = w.chat(title="Auftragskarte ohne Nachricht")
        w.link(card_only, "at-card", "ar-card")
        cutoff = w.tick()
        # Equal timestamps are excluded, not just strictly future timestamps.
        w.store.add_message(old, "assistant", "Gleichzeitige Zukunftsnotiz")
        w.store.add_task_link(old, "at-equal", "ar-equal", source="task:equal")
        w.message(old, "Spätere Zukunftsnotiz")
        w.link(old, "at-future", "ar-future")
        current = dict(current_conversation_id=w.current, before_created_at=cutoff)
        require_equal(w.store.search_context("alice", "Zukunftsnotiz", **current)["hits"], [])
        result = w.store.search_context("alice", "Islandreise", **current)
        require_equal(result["hits"][0]["task_links"], [{"task_id": "at-old", "run_id": "ar-old"}])
        recent = w.store.recent_task_links("alice", **current)["hits"]
        require_equal([h["task_links"][0]["task_id"] for h in recent], ["at-card", "at-old"])
        require_equal((recent[0]["message_id"], recent[0]["sequence"], recent[0]["role"]), ("", 0, ""))
        require_equal(recent[1]["excerpt"], "Islandreise früher")


def t_title_only_task_card_is_a_topic_hit_without_recent_fallback_or_future_messages():
    with world() as w:
        source = w.chat(title="Islandreise Kosten")
        w.link(source)
        cutoff = w.tick()
        w.message(source, "Eine spätere Nachricht darf nicht mitgelesen werden.")
        result = w.store.search_context("alice", "Wie ist der Stand der Islandreise?",
            current_conversation_id=w.current, before_created_at=cutoff)
        require_equal(len(result["hits"]), 1)
        hit = result["hits"][0]
        require_equal(hit["conversation_id"], source)
        require_equal((hit["message_id"], hit["sequence"], hit["role"], hit["excerpt"]), ("", 0, "", ""))
        require_equal(hit["task_links"], [{"task_id": "at-one", "run_id": "ar-one"}])
        require_equal(hit["match_kind"], "topic")
        require(not result["incomplete"])


def t_recent_task_limit_counts_distinct_tasks_instead_of_their_revisions():
    with world() as w:
        older = w.chat("Älterer Auftrag")
        w.link(older, "at-older", "ar-older")
        revised = w.chat("Häufig fortgesetzter Auftrag")
        for revision in range(8):
            w.tick()
            w.store.add_task_link(revised, "at-revised", f"ar-revision-{revision}",
                                  revision=revision + 1, source="task:synthetic")
        result = w.recent(limit=2)
        require_equal([h["task_links"][0] for h in result["hits"]], [
            {"task_id": "at-revised", "run_id": "ar-revision-7"},
            {"task_id": "at-older", "run_id": "ar-older"}])
        require(not result["incomplete"], "eight revisions are still just one candidate task")
        require(w.recent(limit=1)["incomplete"], "a genuinely omitted second task is incomplete")
        with patch.object(C, "CONTEXT_SCAN_ROWS", 2):
            partial = w.recent(limit=2)
            require_equal(len(partial["hits"]), 1)
            require(partial["incomplete"], "a bounded raw scan cannot claim all tasks were found")


def t_topic_links_count_distinct_tasks_before_the_link_limit_even_after_twenty_revisions():
    for count in (8, 20):
        with world() as w:
            source = w.chat("Islandreise Bericht")
            w.link(source, "at-older", "ar-older")
            for revision in range(count):
                w.tick()
                w.store.add_task_link(source, "at-revised", f"ar-revision-{revision}",
                                      revision=revision + 1, source="task:synthetic")
            result = w.search("Islandreise")
            require_equal(len(result["hits"]), 1)
            require_equal(result["hits"][0]["task_links"], [
                {"task_id": "at-revised", "run_id": f"ar-revision-{count - 1}"},
                {"task_id": "at-older", "run_id": "ar-older"}])
            require(not result["incomplete"], f"{count} revisions are still only one topic-linked task")
            with patch.object(C, "CONTEXT_SCAN_ROWS", 2):
                partial = w.search("Islandreise")
                require_equal(partial["hits"][0]["task_links"], [
                    {"task_id": "at-revised", "run_id": f"ar-revision-{count - 1}"}])
                require(partial["incomplete"], "the raw-link scan budget remains a real limit")


def t_scan_character_and_result_budgets_report_incomplete_and_preserve_bounds():
    with world() as w:
        old = w.chat("Seltenes Thema")
        for _ in range(4):
            w.chat("Neuere andere Nachricht")
        with patch.object(C, "CONTEXT_SCAN_ROWS", 2):
            result = w.search("Seltenes")
            require_equal(result, {"hits": [], "incomplete": True})
        require_equal(w.search("Seltenes")["hits"][0]["conversation_id"], old)
        with patch.object(C, "CONTEXT_SCAN_CHARS", 5):
            require(w.search("Seltenes")["incomplete"])
        long = w.chat("Islandreise " + "ausführliche Notizen " * 80)
        w.link(long)
        result = w.search("Islandreise", max_chars=31)
        require(sum(len(h["excerpt"]) for h in result["hits"]) <= 31)
        require(not result["incomplete"], "the only matching chat was found; only its excerpt was shortened")
        with patch.object(C, "CONTEXT_MESSAGE_CHARS", 5):
            require(w.search("Notizen")["incomplete"])
        require_equal(w.search("x" * (C.CONTEXT_QUERY_CHARS + 1)), {"hits": [], "incomplete": True})
        require_equal(w.search("Islandreise", limit=0), {"hits": [], "incomplete": True})
        for value in (float("nan"), float("inf"), "tomorrow"):
            try:
                w.store.search_context("alice", "Islandreise", current_conversation_id=w.current, before_created_at=value)
            except C.ConversationStoreError as exc:
                require_equal(str(exc), "invalid_context_query")
            else:
                raise AssertionError("invalid temporal bound admitted")


def t_sql_work_budget_and_link_cap_are_explicit_and_do_not_poison_later_reads():
    with world() as w:
        cid = w.chat("Islandreise Ergebnis")
        for i in range(8):
            w.link(cid, f"at-{i}", f"ar-{i}")
        result = w.search("Islandreise")
        require_equal(len(result["hits"][0]["task_links"]), C.CONTEXT_LINKS_PER_HIT)
        require(result["incomplete"])
        recent = w.recent(limit=2)
        require_equal(len(recent["hits"]), 2)
        require(recent["incomplete"])
        for i in range(100):
            w.chat(f"Unbeteiligter Verlauf {i}")
        with patch.object(C, "CONTEXT_SQL_STEPS", 1):
            require(w.search("Islandreise")["incomplete"])
        require_equal(w.search("Islandreise")["hits"][0]["conversation_id"], cid)
        require(w.store.conversation_owned(w.current, "alice"), "interrupted query left a progress handler behind")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
