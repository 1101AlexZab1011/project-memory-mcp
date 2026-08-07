"""Federation: several servers holding the same project, meant to differ.

What these protect: local always answers, results merge by rank rather than by
incomparable scores, a private memory never leaves, and one unreachable remote
never takes down a query the others can serve.
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from project_memory_mcp import clients, federation, identity
from project_memory_mcp.federation import Remote, choose_remote, fuse
from project_memory_mcp.http_server import _Handler, _Sessions, _StoreRegistry
from project_memory_mcp.server import McpServer
from project_memory_mcp.sqlite_store import SqliteMemoryStore
from project_memory_mcp.validation import StoreError

TOKEN = "fed-token"
CACHE = "Session cache invalidation races the auth refresh under load."
SHADER = "Shader compilation stalls on a cold start on the build farm."
PACKAGING = "Packaging fails when the editor is still open on the project."


def memory(memory_id, description, labels=("area:x",)):
    return {
        "schema_version": 1, "id": memory_id, "status": "active", "description": description,
        "tags": [], "labels": list(labels),
        "scope": {"project": "p", "area": "a", "files": [], "applies_to": []},
        "triggers": ["trigger for " + memory_id], "remembered_facts": [description],
        "solution_pattern": [], "pitfalls": [],
        "evidence": {"created_from_task": "t", "last_validated": "2026-01-01"},
        "relationships": {"related": [], "supersedes": [], "superseded_by": []},
    }


class FusionTests(unittest.TestCase):
    """Rank fusion, because scores from different corpora are not comparable."""

    def test_a_memory_in_several_lists_outranks_one_in_a_single_list(self):
        fused = fuse({
            "a": [{"uuid": "u1", "id": "shared"}, {"uuid": "u2", "id": "only-a"}],
            "b": [{"uuid": "u3", "id": "only-b"}, {"uuid": "u1", "id": "shared"}],
        }, limit=5)
        self.assertEqual("shared", fused[0]["id"])
        self.assertEqual(["a", "b"], fused[0]["sources"])

    def test_the_same_memory_on_two_servers_fuses_into_one_result(self):
        # Same uuid means one memory promoted to both, not two memories.
        fused = fuse({"a": [{"uuid": "same", "id": "m"}], "b": [{"uuid": "same", "id": "m"}]}, 5)
        self.assertEqual(1, len(fused))

    def test_different_memories_about_the_same_thing_both_appear(self):
        fused = fuse({"a": [{"uuid": "u1", "id": "m"}], "b": [{"uuid": "u2", "id": "m"}]}, 5)
        self.assertEqual(2, len(fused))

    def test_a_huge_score_buys_nothing_beyond_its_position(self):
        # Scores are computed per corpus and are not comparable; only rank is.
        loud = fuse({"loud": [{"uuid": "u1", "id": "m", "score": 9999.0}]}, 5)
        quiet = fuse({"quiet": [{"uuid": "u1", "id": "m", "score": 0.0001}]}, 5)
        self.assertEqual(loud[0]["fused_score"], quiet[0]["fused_score"])

    def test_position_within_a_list_decides_the_order(self):
        # Every other fusion test compares "in two lists" against "in one", and
        # passes even if rank is discarded entirely. This is the one that needs
        # the 1/(k+rank) actually to be there.
        fused = fuse({"a": [{"uuid": "first", "id": "first"},
                            {"uuid": "second", "id": "second"},
                            {"uuid": "third", "id": "third"}]}, limit=5)
        self.assertEqual(["first", "second", "third"], [m["id"] for m in fused])
        self.assertGreater(fused[0]["fused_score"], fused[1]["fused_score"])
        self.assertGreater(fused[1]["fused_score"], fused[2]["fused_score"])

    def test_agreement_helps_but_does_not_override_a_large_rank_gap(self):
        # Two sources agreeing on a mid-list result *should* beat one source's
        # top hit - that is what fusion is for, and my first version of this
        # test had it backwards. What rank still has to buy is that the
        # agreement stops paying once the gap is wide enough: with k=60, two
        # hits at rank 200 score 2/261 against one at 1/61.
        def scores(depth):
            # Padding is per source, or the padding itself appears in two lists
            # and outscores everything the test is actually about.
            pad = lambda tag: [{"uuid": f"{tag}-{i}", "id": f"{tag}-{i}"} for i in range(depth)]
            fused = fuse({
                "a": [{"uuid": "sharp", "id": "sharp"}],
                "b": pad("b") + [{"uuid": "shared", "id": "shared"}],
                "c": pad("c") + [{"uuid": "shared", "id": "shared"}],
            }, limit=10_000)
            return {m["id"]: m["fused_score"] for m in fused}

        deep = scores(200)
        self.assertGreater(deep["sharp"], deep["shared"])

        # And the shallow case, where agreement is meant to win.
        shallow = scores(5)
        self.assertGreater(shallow["shared"], shallow["sharp"])

    def test_one_source_dropping_out_still_returns_the_rest(self):
        self.assertEqual(1, len(fuse({"a": [{"uuid": "u1", "id": "m"}], "b": []}, 5)))


class RoutingTests(unittest.TestCase):
    """Where a promotion goes: evidence first, description matching second."""

    def setUp(self):
        self.remotes = [
            Remote("team", "http://team", "Shared engine and build knowledge for the whole team",
                   None, True),
            Remote("personal", "http://personal", "My own scratch notes about anything", None, True),
        ]

    def test_a_remote_consulted_during_the_task_wins(self):
        entry = memory("m", "Scratch note about anything at all")
        ranked = choose_remote(self.remotes, entry, used=["team"])
        self.assertEqual("team", ranked[0]["name"])
        self.assertIn("queried this remote", ranked[0]["why"])

    def test_description_matching_is_the_tiebreak(self):
        entry = memory("m", "The build fails on the shared engine when packaging")
        ranked = choose_remote(self.remotes, entry, used=[])
        self.assertEqual("team", ranked[0]["name"])
        self.assertGreater(ranked[0]["description_match"], 0)

    def test_a_disabled_remote_is_not_offered(self):
        self.remotes[0] = Remote("team", "http://team", "x", None, False)
        self.assertEqual(["personal"], [r["name"] for r in choose_remote(self.remotes, memory("m", "any text"))])


class TierCase(unittest.TestCase):
    def earn(self, slug, tier=2):
        """Put a memory where the audit would have, had it proven itself."""
        self.store.connection.execute(
            "UPDATE memories SET tier=? WHERE project_id='demo' AND slug=?", (tier, slug))
        self.store.connection.commit()


class LocalStoreTests(TierCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SqliteMemoryStore(Path(self.tmp.name) / "memory.db", "demo")
        self.addCleanup(self.store.close)
        self.store.add_label("area:x", "x")

    def test_local_only_is_a_complete_setup(self):
        self.store.create_memory(memory("cache-race", CACHE), visibility="public")
        self.assertEqual([], federation.list_remotes(self.store.connection))
        found = federation.recall_across(self.store, "cache invalidation")
        self.assertEqual(["cache-race"], [m["id"] for m in found["memories"]])

    def test_memories_are_private_unless_asked_otherwise(self):
        # Over-sharing is the harder mistake to undo, so it is not the default.
        self.store.create_memory(memory("user-habit", "The user prefers early returns always."))
        row = self.store.connection.execute(
            "SELECT visibility FROM memories WHERE slug='user-habit'").fetchone()
        self.assertEqual("private", row["visibility"])

    def test_a_private_memory_has_no_promotion_targets(self):
        federation.add_remote(self.store.connection, "team", "http://team", "everything")
        self.store.create_memory(memory("user-habit", "The user prefers early returns always."))
        targets = federation.promotion_targets(self.store, "user-habit")
        self.assertEqual([], targets["targets"])
        self.assertIn("never promoted", targets["why"])

    def test_promoting_a_private_memory_is_refused(self):
        federation.add_remote(self.store.connection, "team", "http://team", "everything")
        self.store.create_memory(memory("user-habit", "The user prefers early returns always."))
        with self.assertRaises(StoreError) as caught:
            federation.promote(self.store, "user-habit", "team")
        self.assertIn("private", str(caught.exception))

    def test_visibility_can_be_changed_deliberately(self):
        self.store.create_memory(memory("cache-race", CACHE))
        self.store.set_visibility("cache-race", "public")
        federation.add_remote(self.store.connection, "team", "http://team", "everything")
        self.assertTrue(federation.promotion_targets(self.store, "cache-race")["targets"])

    def test_a_memory_that_has_not_earned_a_tier_is_not_published(self):
        # Otherwise "earn your way into the shared store" is decorative: a
        # memory written thirty seconds ago could be pushed to everyone.
        federation.add_remote(self.store.connection, "team", "http://team", "everything")
        self.store.create_memory(memory("brand-new", CACHE), visibility="public")
        with self.assertRaises(StoreError) as caught:
            federation.promote(self.store, "brand-new", "team")
        self.assertIn("has not earned publication", str(caught.exception))

    def test_a_rare_but_critical_lesson_can_be_published_deliberately(self):
        # Knowledge that will never accrue usage still needs a way out.
        federation.add_remote(self.store.connection, "team", "http://127.0.0.1:9/", "down")
        self.store.create_memory(memory("rare-lesson", CACHE), visibility="public")
        result = federation.promote(self.store, "rare-lesson", "team", force=True)
        self.assertIn("queued", result)

    def test_an_unreachable_remote_queues_the_promotion_instead_of_failing(self):
        federation.add_remote(self.store.connection, "team", "http://127.0.0.1:9/", "nothing here")
        self.store.create_memory(memory("cache-race", CACHE), visibility="public")
        self.earn("cache-race")
        result = federation.promote(self.store, "cache-race", "team")
        self.assertIn("queued", result)
        queued = self.store.connection.execute("SELECT COUNT(*) AS n FROM outbox").fetchone()["n"]
        self.assertEqual(1, queued)

    def test_recall_still_answers_when_a_remote_is_down(self):
        federation.add_remote(self.store.connection, "dead", "http://127.0.0.1:9/", "unreachable")
        self.store.create_memory(memory("cache-race", CACHE), visibility="public")
        found = federation.recall_across(self.store, "cache invalidation")
        self.assertEqual(["cache-race"], [m["id"] for m in found["memories"]])
        self.assertIn("dead", found["sources_unreachable"])
        self.assertEqual(["local"], found["sources_answered"])


class TwoServerTests(TierCase):
    """A real second server, queried over HTTP."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

        remote_db = Path(self.tmp.name) / "remote.db"
        seed = SqliteMemoryStore(remote_db, "demo")
        seed.add_label("area:x", "x")
        seed.create_memory(memory("packaging-editor-open", PACKAGING))
        seed.create_memory(memory("shader-stall", SHADER))
        seed.close()

        registry = _StoreRegistry(remote_db)
        self.addCleanup(registry.close)
        handler = type("H", (_Handler,), {"registry": registry, "token": TOKEN,
                                          "sessions": _Sessions(), "ui_enabled": False})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.addCleanup(self.httpd.server_close)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.shutdown)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]

        self.store = SqliteMemoryStore(Path(self.tmp.name) / "local.db", "demo")
        self.addCleanup(self.store.close)
        self.store.add_label("area:x", "x")
        federation.add_remote(self.store.connection, "team", self.url,
                              "Shared build and packaging knowledge", token=TOKEN)

    def test_recall_reaches_both_and_says_which_answered(self):
        self.store.create_memory(memory("cache-race", CACHE), visibility="public")
        found = federation.recall_across(self.store, "packaging fails editor open", limit=5)
        self.assertIn("packaging-editor-open", [m["id"] for m in found["memories"]])
        self.assertEqual(["local", "team"], found["sources_answered"])
        self.assertEqual({}, found["sources_unreachable"])

    def test_a_result_says_which_source_it_came_from(self):
        found = federation.recall_across(self.store, "packaging fails editor open", limit=5)
        entry = [m for m in found["memories"] if m["id"] == "packaging-editor-open"][0]
        self.assertEqual(["team"], entry["sources"])

    def test_what_came_back_is_cached_and_marked_as_borrowed(self):
        federation.recall_across(self.store, "packaging fails editor open", limit=5)
        row = self.store.connection.execute(
            "SELECT origin_remote FROM cached_memories WHERE slug='packaging-editor-open'"
        ).fetchone()
        self.assertEqual("team", row["origin_remote"])
        self.assertIsNone(
            self.store.connection.execute(
                "SELECT 1 FROM memories WHERE slug='packaging-editor-open'").fetchone(),
            "a borrowed copy was written into the table of memories this machine owns")

    def test_a_cached_memory_is_findable_offline_and_says_where_it_came_from(self):
        # The entire point of caching. This used to fail: cached rows bypassed
        # _index_text, so they were absent from the one query that would have
        # used them - the cache was write-only for its stated purpose.
        federation.recall_across(self.store, "packaging fails editor open", limit=5)

        # No remotes consulted; a purely local recall, as if the server were now
        # unreachable.
        found = self.store.recall("packaging editor open", limit=5, record=False)
        borrowed = [m for m in found["memories"] if m["id"] == "packaging-editor-open"]
        self.assertEqual(1, len(borrowed), "a cached memory was not findable by local recall")
        self.assertEqual("team", borrowed[0]["origin"],
                         "a borrowed result did not say which server it came from")

    def test_a_cached_memory_does_not_age_this_machine_s_counters(self):
        # Counters feed the audit and the audit governs retention, which is not
        # a decision to make about a memory this machine does not own.
        federation.recall_across(self.store, "packaging fails editor open", limit=5)
        self.store.recall("packaging editor open", limit=5)
        cached_uuid = self.store.connection.execute(
            "SELECT uuid FROM cached_memories WHERE slug='packaging-editor-open'").fetchone()["uuid"]
        self.assertIsNone(
            self.store.connection.execute(
                "SELECT 1 FROM usage WHERE memory_id=?", (cached_uuid,)).fetchone(),
            "surfacing a borrowed copy recorded usage against it")

    def test_a_cached_copy_is_not_this_machine_s_to_publish(self):
        federation.recall_across(self.store, "packaging fails editor open", limit=5)
        with self.assertRaises(StoreError) as caught:
            federation.promote(self.store, "packaging-editor-open", "team")
        self.assertIn("cached copy", str(caught.exception))

    def test_a_cached_slug_does_not_collide_with_a_local_one(self):
        # The unique-slug rule covers memories this machine owns, not borrowed
        # copies - otherwise caching would fail the moment two servers happened
        # to name a lesson the same way.
        #
        # This test used to assert the *arrangement* - two rows in `memories`,
        # one with origin_remote set - and passed while the property was broken
        # in the other order. See the test below.
        self.store.create_memory(memory("packaging-editor-open", "A different local lesson entirely."))
        federation.recall_across(self.store, "packaging fails editor open", limit=5)

        self.assertIn("A different local lesson", self.store.get_memory(
            "packaging-editor-open")["description"], "a borrowed copy answered to a local name")
        self.assertEqual(1, self.store.connection.execute(
            "SELECT COUNT(*) AS n FROM cached_memories WHERE slug='packaging-editor-open'"
        ).fetchone()["n"], "the borrowed copy was dropped instead of kept alongside")

    def test_caching_a_name_first_does_not_reserve_it(self):
        # The order the old test did not try, and the one that was broken:
        # consult a remote, then write your own lesson about what you learned.
        # `create_memory` checked every row with that slug, so it answered
        # "Memory already exists" for a name that was free.
        federation.recall_across(self.store, "packaging fails editor open", limit=5)

        self.store.create_memory(
            memory("packaging-editor-open", "What we concluded ourselves about packaging."))

        self.assertIn("What we concluded ourselves", self.store.get_memory(
            "packaging-editor-open")["description"])
        # And it is a real, editable, promotable memory of this machine's.
        self.assertFalse(self.store.publication_state("packaging-editor-open")["borrowed"])

    def test_promotion_publishes_to_the_named_remote(self):
        self.store.create_memory(memory("cache-race", CACHE), visibility="public")
        self.earn("cache-race")
        result = federation.promote(self.store, "cache-race", "team")
        # Queued, not published: promote never speaks to the network. The
        # Computer's outbox job is what delivers.
        self.assertEqual("cache-race", result["queued"])
        self.assertNotIn("promoted", result)
        federation.deliver_outbox(self.store)
        found = federation.recall_across(self.store, "cache invalidation races auth", limit=5)
        entry = [m for m in found["memories"] if m["id"] == "cache-race"][0]
        self.assertEqual(["local", "team"], entry["sources"])

    def test_promotion_does_not_touch_the_network(self):
        # The point of the whole change. A remote that black-holes the
        # connection would previously have held this project's lock for the full
        # connect timeout, stalling every other request including reads.
        federation.add_remote(self.store.connection, "blackhole", "http://192.0.2.1:9/", "down")
        self.store.create_memory(memory("cache-race", CACHE), visibility="public")
        self.earn("cache-race")
        started = time.monotonic()
        result = federation.promote(self.store, "cache-race", "blackhole")
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual("cache-race", result["queued"])

    def test_queueing_the_same_memory_twice_does_not_send_it_twice(self):
        self.store.create_memory(memory("cache-race", CACHE), visibility="public")
        self.earn("cache-race")
        federation.promote(self.store, "cache-race", "team")
        federation.promote(self.store, "cache-race", "team")
        self.assertEqual(1, self.store.connection.execute(
            "SELECT COUNT(*) AS n FROM outbox").fetchone()["n"])
        self.assertEqual(["cache-race"], federation.deliver_outbox(self.store)["sent"])

    def test_outbox_status_reports_what_is_waiting_and_why(self):
        federation.add_remote(self.store.connection, "flaky", "http://127.0.0.1:9/", "down")
        self.store.create_memory(memory("cache-race", CACHE), visibility="public")
        self.earn("cache-race")
        federation.promote(self.store, "cache-race", "flaky")

        queued = federation.outbox_status(self.store)
        self.assertEqual(1, queued["count"])
        self.assertEqual("cache-race", queued["queued"][0]["id"])
        self.assertEqual(0, queued["queued"][0]["attempts"])
        self.assertIsNone(queued["queued"][0]["last_error"])

        federation.deliver_outbox(self.store)
        after = federation.outbox_status(self.store)
        self.assertEqual(1, after["queued"][0]["attempts"])
        self.assertTrue(after["queued"][0]["last_error"])

    def test_a_delivered_promotion_records_that_the_remote_is_up(self):
        self.store.create_memory(memory("cache-race", CACHE), visibility="public")
        self.earn("cache-race")
        federation.promote(self.store, "cache-race", "team")
        federation.deliver_outbox(self.store)
        row = self.store.connection.execute(
            "SELECT last_ok, last_error FROM remotes WHERE name='team'").fetchone()
        self.assertTrue(row["last_ok"])
        self.assertIsNone(row["last_error"])

    def test_a_queued_promotion_is_sent_when_the_remote_returns(self):
        federation.add_remote(self.store.connection, "flaky", "http://127.0.0.1:9/", "down")
        self.store.create_memory(memory("cache-race", CACHE), visibility="public")
        self.earn("cache-race")
        federation.promote(self.store, "cache-race", "flaky")
        federation.add_remote(self.store.connection, "flaky", self.url, "back up", token=TOKEN)
        result = federation.deliver_outbox(self.store)
        self.assertEqual(["cache-race"], result["sent"])
        self.assertEqual(0, self.store.connection.execute(
            "SELECT COUNT(*) AS n FROM outbox").fetchone()["n"])


class OutboxSurvivesItsMemoryTests(TierCase):
    """A queue entry whose memory is gone must not take the queue with it.

    `delete_memory` cleaned six tables and left `outbox` alone. The row that
    survived could not be delivered or skipped - its LEFT JOIN gave a null slug,
    `get_memory(None)` raised, and because it sorted first by id it took every
    promotion behind it down too, on every run, for good. The Computer swallowed
    the exception, so federation stopped working and every surface said fine.

    A dead server retries, which is right. A deleted memory is not a delivery
    that failed; it is one that no longer means anything.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SqliteMemoryStore(Path(self.tmp.name) / "memory.db", "demo")
        self.addCleanup(self.store.close)
        self.store.add_label("area:x", "x")
        # Black-holed, so nothing here depends on a reachable server: every
        # assertion is about what the queue does before it reaches the network.
        federation.add_remote(self.store.connection, "team", "http://127.0.0.1:9/", "team")

    def queue(self, slug, description):
        self.store.create_memory(memory(slug, description), visibility="public")
        self.earn(slug)
        federation.promote(self.store, slug, "team")

    def outbox_count(self):
        return self.store.connection.execute(
            "SELECT COUNT(*) AS n FROM outbox WHERE project_id='demo'").fetchone()["n"]

    def test_deleting_a_queued_memory_cancels_its_promotion(self):
        self.queue("cache-race", CACHE)
        result = self.store.delete_memory("cache-race", "cache-race")
        self.assertEqual(1, result["cancelled_promotions"],
                         "the caller was not told a queued promotion was dropped")
        self.assertEqual(0, self.outbox_count(), "a deleted memory left a row in the outbox")

    def test_delivery_survives_an_orphan_left_by_an_older_version(self):
        # The state a database written before the fix is already in. The orphan
        # is inserted first so it sorts ahead - which is what made one of these
        # able to block everything behind it.
        with self.store.connection:
            self.store.connection.execute(
                "INSERT INTO outbox(project_id, memory_id, remote, queued_at) "
                "VALUES ('demo','11111111-dead-dead-dead-111111111111','team','2026-01-01T00:00:00Z')")
        self.queue("cache-race", CACHE)

        result = federation.deliver_outbox(self.store)

        self.assertEqual(["11111111-dead-dead-dead-111111111111"],
                         result["dropped_memory_gone"])
        # The live promotion behind it is untouched and still due a retry. This
        # is the assertion that matters: not that the orphan went, but that it
        # stopped being in front of everything else.
        remaining = self.store.connection.execute(
            "SELECT memory_id FROM outbox WHERE project_id='demo'").fetchall()
        self.assertEqual([self.store._uuid_for("cache-race")],
                         [r["memory_id"] for r in remaining])

    def test_a_memory_merged_away_is_not_published_afterwards(self):
        # merge archives the loser rather than deleting it, so its slug survives
        # and the queued promotion stays technically deliverable. Publishing a
        # lesson that was just folded into another is not what anyone asked for.
        self.store.create_memory(memory("keep-note", SHADER), visibility="public")
        self.queue("fold-note", CACHE)
        self.store.merge_memories("keep-note", "fold-note", "They say one thing twice.")

        result = federation.deliver_outbox(self.store)

        self.assertEqual(["fold-note"], result["dropped_memory_gone"])
        self.assertEqual(0, self.outbox_count())

    def test_a_dead_remote_still_gets_retried(self):
        # The counterweight. Dropping everything undeliverable would satisfy the
        # tests above and destroy the outbox's whole reason for existing.
        self.queue("cache-race", CACHE)
        result = federation.deliver_outbox(self.store)
        self.assertEqual([], result["sent"])
        self.assertNotIn("dropped_memory_gone", result)
        self.assertEqual(1, self.outbox_count(), "a promotion to a sleeping server was discarded")


class ToolSurfaceTests(TierCase):
    """recall over the real tool dispatch, local and federated."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SqliteMemoryStore(Path(self.tmp.name) / "local.db", "demo")
        self.addCleanup(self.store.close)
        self.store.add_label("area:x", "x")
        self.store.create_memory(memory("cache-race", CACHE), visibility="public")

    def call(self, arguments, db_lock=None):
        response = McpServer(self.store, db_lock=db_lock).handle({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "recall", "arguments": arguments}})
        return json.loads(response["result"]["content"][0]["text"])

    def test_recall_is_local_unless_asked_otherwise(self):
        # The default must not put the network on the hottest path in the system.
        federation.add_remote(self.store.connection, "team", "http://127.0.0.1:9/", "down")
        found = self.call({"query": "cache invalidation", "limit": 3})
        self.assertEqual(["cache-race"], [m["id"] for m in found["memories"]])
        self.assertNotIn("sources_answered", found)

    def test_include_remotes_reports_a_source_that_did_not_answer(self):
        federation.add_remote(self.store.connection, "team", "http://127.0.0.1:9/", "down")
        found = self.call({"query": "cache invalidation", "limit": 3, "include_remotes": True})
        # A partial answer, not a failure: local still replied.
        self.assertEqual(["cache-race"], [m["id"] for m in found["memories"]])
        self.assertEqual(["local"], found["sources_answered"])
        self.assertIn("team", found["sources_unreachable"])

    def test_include_remotes_with_none_configured_is_just_a_local_recall(self):
        found = self.call({"query": "cache invalidation", "limit": 3, "include_remotes": True})
        self.assertEqual(["cache-race"], [m["id"] for m in found["memories"]])

    def test_the_lock_is_free_while_a_remote_is_being_waited_on(self):
        # The point of step 2 and 3 together. A remote that never answers must
        # not stall every other request for this project.
        federation.add_remote(self.store.connection, "slow", "http://192.0.2.1:9/", "black hole")
        lock = threading.Lock()
        seen = []

        def watcher():
            # Give the fan-out time to be in flight, then check the lock is free.
            time.sleep(0.3)
            seen.append(lock.acquire(timeout=2.0))
            if seen[-1]:
                lock.release()

        thread = threading.Thread(target=watcher)
        thread.start()
        self.call({"query": "cache invalidation", "limit": 3, "include_remotes": True},
                  db_lock=lock)
        thread.join(timeout=10)
        self.assertEqual([True], seen,
                         "the project lock was held while waiting on a remote")


class RemoteLifecycleTests(TierCase):
    """Disabling and removing are different intentions, and now different acts.

    `enabled` was read in six places and written by nothing but the schema
    default, so the only way to stop talking to a server was `--remove`, which
    discards its url, token and description. "Down for a week" should not cost
    the credential you need to come back.

    And removal used to leave its queue behind: `deliver_outbox` builds its
    lookup from enabled remotes, so promotions to a server that no longer exists
    were skipped on every run - never delivered, never dropped, never reported.
    The same shape as the orphan a deleted memory left, deferred here from step
    3 because it belongs with the remote lifecycle.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SqliteMemoryStore(Path(self.tmp.name) / "memory.db", "demo")
        self.addCleanup(self.store.close)
        self.store.add_label("area:x", "x")
        federation.add_remote(self.store.connection, "team", "http://127.0.0.1:9/",
                              "Shared knowledge", token="team-token")
        self.store.create_memory(memory("cache-race", CACHE), visibility="public")
        self.earn("cache-race")
        federation.promote(self.store, "cache-race", "team")

    def queued(self):
        return self.store.connection.execute(
            "SELECT COUNT(*) AS n FROM outbox").fetchone()["n"]

    def test_a_disabled_remote_keeps_its_queue(self):
        federation.set_remote_enabled(self.store.connection, "team", False)
        federation.deliver_outbox(self.store)
        self.assertEqual(1, self.queued(), "disabling a remote discarded its queued work")

    def test_a_disabled_remote_is_not_queried_or_delivered_to(self):
        federation.set_remote_enabled(self.store.connection, "team", False)
        self.assertEqual([], [r.name for r in federation.list_remotes(
            self.store.connection, enabled_only=True)])
        self.assertEqual([], federation.deliver_outbox(self.store)["sent"])

    def test_re_enabling_needs_no_credentials_re_entered(self):
        federation.set_remote_enabled(self.store.connection, "team", False)
        federation.set_remote_enabled(self.store.connection, "team", True)
        remote = federation.list_remotes(self.store.connection)[0]
        self.assertEqual("http://127.0.0.1:9", remote.url)
        self.assertEqual("team-token", remote.token)
        self.assertEqual("Shared knowledge", remote.description)
        self.assertEqual(1, self.queued(), "the queue did not survive the round trip")

    def test_promoting_to_a_disabled_remote_says_it_is_disabled(self):
        # This branch was unreachable until remotes could be disabled, and it
        # answered "Unknown remote 'team'. Known: team" - a message that
        # contradicts itself in the same breath.
        federation.set_remote_enabled(self.store.connection, "team", False)
        with self.assertRaises(StoreError) as caught:
            federation.promote(self.store, "cache-race", "team")
        self.assertIn("is disabled", str(caught.exception))

    def test_removing_a_remote_drops_what_was_queued_for_it(self):
        result = federation.remove_remote(self.store.connection, "team")
        self.assertEqual(1, result["cancelled_promotions"],
                         "the caller was not told queued work went with the remote")
        self.assertEqual(0, self.queued(),
                         "a promotion was left queued to a server that no longer exists")

    def test_removing_a_remote_leaves_other_remotes_queues_alone(self):
        # The counterweight. Dropping the whole outbox would satisfy the test
        # above and lose unrelated work.
        federation.add_remote(self.store.connection, "personal", "http://127.0.0.1:9/", "mine")
        federation.promote(self.store, "cache-race", "personal")
        self.assertEqual(2, self.queued())

        federation.remove_remote(self.store.connection, "team")

        rows = self.store.connection.execute("SELECT remote FROM outbox").fetchall()
        self.assertEqual(["personal"], [r["remote"] for r in rows])

    def test_enabling_an_unknown_remote_is_an_error_not_a_no_op(self):
        with self.assertRaises(StoreError):
            federation.set_remote_enabled(self.store.connection, "ghost", True)


class TokenFreeFederationTests(TierCase):
    """Federating with a key and no bearer token at all.

    `private_key` was a parameter on RemoteClient, deliver_outbox and
    recall_across, and **nothing ever passed it**. The only place a key was
    loaded was `join`, which wrote it to disk, printed "the private key never
    left this machine... which is how identity works across servers", and was
    the last thing to read it. Inbound verification was complete and tested; the
    other half of the handshake was unreachable.

    So this asserts the property that was missing rather than that a header is
    present: a promotion delivered to a server where the only credential is an
    enrolled key.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)

        remote_db = root / "remote.db"
        seed = SqliteMemoryStore(remote_db, "demo")
        seed.add_label("area:x", "x")
        seed.close()

        self.registry = _StoreRegistry(remote_db)
        self.addCleanup(self.registry.close)
        # The server has a shared token, and this machine will never be told it.
        handler = type("H", (_Handler,), {"registry": self.registry, "token": "not-shared-with-us",
                                          "sessions": _Sessions(), "ui_enabled": False})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.addCleanup(self.httpd.server_close)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.shutdown)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]

        self.key_path = root / "client_key.pem"
        self.key = identity.load_or_create(self.key_path)

        self.store = SqliteMemoryStore(root / "local.db", "demo")
        self.addCleanup(self.store.close)
        self.store.add_label("area:x", "x")
        # No token on the remote. Today that is an unusable configuration.
        federation.add_remote(self.store.connection, "team", self.url, "Shared knowledge")

    def enroll_our_key(self):
        code = clients.create_code(self.registry.control(), name="laptop")["code"]
        public = identity.encode_public(identity.public_bytes(self.key))
        return clients.redeem_code(self.registry.control(), code, "laptop", public)

    def queue_a_promotion(self):
        self.store.create_memory(memory("cache-race", CACHE), visibility="public")
        self.earn("cache-race")
        federation.promote(self.store, "cache-race", "team")

    def test_a_promotion_is_delivered_with_a_key_and_no_token(self):
        self.enroll_our_key()
        self.queue_a_promotion()

        result = federation.deliver_outbox(self.store, private_key=self.key)

        self.assertEqual(["cache-race"], result["sent"])
        landed = SqliteMemoryStore(self.registry.database, "demo", create=False)
        self.addCleanup(landed.close)
        self.assertEqual(CACHE, landed.get_memory("cache-race")["description"])

    def test_the_delivery_is_attributed_to_the_key_that_signed_it(self):
        # The point of signing over a shared secret: the far side knows who,
        # not merely that somebody knew the password.
        enrolled = self.enroll_our_key()
        self.queue_a_promotion()
        federation.deliver_outbox(self.store, private_key=self.key)

        landed = SqliteMemoryStore(self.registry.database, "demo", create=False)
        self.addCleanup(landed.close)
        row = landed.connection.execute(
            "SELECT author_client, author_key FROM memories WHERE slug='cache-race'").fetchone()
        self.assertEqual(enrolled["client_id"], row["author_client"])
        self.assertEqual(enrolled["fingerprint"], row["author_key"])

    def test_without_the_key_the_same_delivery_is_refused(self):
        # The counterweight. A server that accepted this unsigned would make the
        # test above pass for the wrong reason.
        self.enroll_our_key()
        self.queue_a_promotion()

        result = federation.deliver_outbox(self.store, private_key=None)

        self.assertEqual([], result["sent"])
        self.assertEqual(1, self.store.connection.execute(
            "SELECT COUNT(*) AS n FROM outbox").fetchone()["n"])

    def test_an_unenrolled_key_is_refused(self):
        # Holding *a* key is not authorisation; the server has to know it.
        self.queue_a_promotion()
        self.assertEqual([], federation.deliver_outbox(self.store, private_key=self.key)["sent"])

    def test_a_configured_token_still_wins_over_a_key(self):
        # `remote --token` is documented as "if this machine has no enrolled key
        # there", so setting one says which credential works on that server.
        # Signing anyway would break every existing token setup the moment this
        # machine enrolled a key with some unrelated server.
        federation.add_remote(self.store.connection, "team", self.url, "Shared",
                              token="not-shared-with-us")
        self.queue_a_promotion()  # key deliberately never enrolled

        self.assertEqual(["cache-race"],
                         federation.deliver_outbox(self.store, private_key=self.key)["sent"])

    def test_the_outbox_job_signs_with_this_machine_s_key(self):
        # Through the job, not through deliver_outbox. Asserting the fix one
        # level below the wiring is how the original defect survived: the
        # `private_key` argument worked perfectly and nothing passed it. A
        # mutant reverting the job's call survived until this test existed.
        import threading as _threading

        from project_memory_mcp.computer import make_job

        self.enroll_our_key()
        self.queue_a_promotion()

        job = make_job("outbox", "demo", key_path=self.key_path)
        result = job.run(self.store, _threading.Lock())

        self.assertEqual(["cache-race"], result["sent"])

    def test_federated_recall_signs_with_this_machine_s_key(self):
        # And the same for the read path, over the real tool dispatch.
        self.enroll_our_key()
        remote_store = SqliteMemoryStore(self.registry.database, "demo", create=False)
        self.addCleanup(remote_store.close)
        remote_store.create_memory(memory("their-note", PACKAGING), visibility="public")

        server = McpServer(self.store, key_path=self.key_path)
        answer = json.loads(server._call_tool(
            "recall", {"query": "packaging editor open", "include_remotes": True}
        )["content"][0]["text"])

        self.assertEqual({}, answer["sources_unreachable"],
                         "the remote refused a request this machine should have signed")
        self.assertIn("team", answer["sources_answered"])

    def test_without_a_key_that_same_recall_gets_only_a_local_answer(self):
        # The counterweight: a remote that answered unauthenticated would make
        # the test above pass without any signing at all.
        self.enroll_our_key()
        server = McpServer(self.store, key_path=Path(self.tmp.name) / "no-key.pem")
        answer = json.loads(server._call_tool(
            "recall", {"query": "packaging editor open", "include_remotes": True}
        )["content"][0]["text"])

        self.assertIn("team", answer["sources_unreachable"])
        self.assertEqual(["local"], answer["sources_answered"])

    def test_a_machine_that_never_enrolled_has_no_key_and_that_is_fine(self):
        self.assertIsNone(identity.load_if_present(Path(self.tmp.name) / "nothing-here.pem"))

    def test_loading_a_key_never_creates_one(self):
        # A background sweep that minted a keypair would be enrolling a client
        # nobody asked for. `join` creates; nothing else does.
        missing = Path(self.tmp.name) / "should-not-appear.pem"
        identity.load_if_present(missing)
        self.assertFalse(missing.exists())


class BorrowedStaysBorrowedTests(unittest.TestCase):
    """Cached copies are not this machine's memories, structurally.

    They used to sit in `memories` behind a nullable `origin_remote`, which
    every query had to remember and almost none did. What that cost, all of it
    found by walking the paths rather than by any failing test:

    - `create_memory` refused a slug that was free locally, because its
      existence check saw borrowed rows too. Consult a remote, lose the name.
    - `validate_store` reported dangling links for cached memories pointing at
      things this machine never cached.
    - the audit swept them, against a schema comment promising it never would.
    - a backup round-trip dropped `origin_remote`, quietly turning somebody
      else's lesson into one this machine owned - and could publish onward.

    A separate table makes each of those impossible rather than filtered.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SqliteMemoryStore(Path(self.tmp.name) / "memory.db", "demo")
        self.addCleanup(self.store.close)
        self.store.add_label("area:x", "x")
        self.store.create_memory(memory("local-note", "Our own lesson about shader stalls here"))

    def cache(self, slug, description, related=None, uuid="99999999-9999-9999-9999-999999999999"):
        body = memory(slug, description)
        if related:
            body["relationships"]["related"] = [{"id": r, "reason": "They share a subsystem."}
                                                for r in related]
        self.store.cache_remote_results({"upstream": {"memories": [{"uuid": uuid, "memory": body}]}})
        return body

    def test_a_borrowed_name_does_not_reserve_a_local_one(self):
        self.cache("their-note", "Their lesson about the very same subsystem")
        self.store.create_memory(memory("their-note", "What we concluded ourselves about it"))
        self.assertIn("What we concluded ourselves",
                      self.store.get_memory("their-note")["description"])

    def test_our_own_memory_wins_a_name_it_shares_with_a_copy(self):
        self.cache("local-note", "Their unrelated lesson that happens to share a name")
        self.assertIn("Our own lesson", self.store.get_memory("local-note")["description"])
        self.assertFalse(self.store.publication_state("local-note")["borrowed"])

    def test_a_borrowed_copy_can_still_be_read_by_name(self):
        # It has to be: recall shows them, and a reader needs the whole thing.
        self.cache("their-note", "Their lesson, which we should be able to read in full")
        self.assertIn("read in full", self.store.get_memory("their-note")["description"])

    def test_a_borrowed_link_to_something_uncached_is_not_our_error(self):
        self.cache("their-note", "Their lesson linking to one we never cached",
                   related=["never-cached-here"])
        self.assertEqual([], self.store.validate_store())

    def test_the_audit_does_not_examine_borrowed_copies(self):
        from project_memory_mcp import audit

        self.cache("their-note", "Their lesson, which this machine must not retire")
        _queries, rows = audit._collect(self.store)
        self.assertEqual(["local-note"], sorted(r["slug"] for r in rows))

    def test_a_backup_round_trip_cannot_claim_a_borrowed_copy(self):
        from project_memory_mcp import backup

        self.cache("their-note", "Their lesson, which a restore must not adopt")
        out = Path(self.tmp.name) / "export.json"
        backup.export_json(self.store.path, out)
        backup.import_json(Path(self.tmp.name) / "restored.db", out)

        restored = SqliteMemoryStore(Path(self.tmp.name) / "restored.db", "demo", create=False)
        self.addCleanup(restored.close)
        self.assertEqual(
            ["local-note"],
            sorted(r["slug"] for r in restored.connection.execute("SELECT slug FROM memories")),
            "a restore adopted another server's memory as this machine's own")

    def test_the_cache_is_bounded_by_age_and_by_count(self):
        for i in range(6):
            self.cache(f"their-note-{i}", f"Their lesson number {i} about shader stalls",
                       uuid=f"0000{i:04d}-0000-0000-0000-000000000000")
        with self.store.connection:
            self.store.connection.execute(
                "UPDATE cached_memories SET cached_at='2020-01-01T00:00:00Z' "
                "WHERE slug IN ('their-note-0','their-note-1')")

        self.assertEqual(2, self.store.evict_cache(older_than_days=30,
                                                   keep_most_recent=500)["evicted"])
        self.assertEqual(3, self.store.evict_cache(older_than_days=30,
                                                   keep_most_recent=1)["evicted"])
        self.assertEqual(1, self.store.connection.execute(
            "SELECT COUNT(*) AS n FROM cached_memories").fetchone()["n"])

    def test_an_evicted_copy_stops_being_searchable(self):
        self.cache("their-note", "Their lesson about shader stalls on the build farm")
        self.assertTrue(any(m["id"] == "their-note" for m in
                            self.store.recall("build farm", record=False)["memories"]))

        self.store.evict_cache(older_than_days=30, keep_most_recent=0)

        self.assertFalse(any(m["id"] == "their-note" for m in
                             self.store.recall("build farm", record=False)["memories"]))

    def test_eviction_takes_the_search_index_row_with_the_body(self):
        # Asserted directly on the index, because the behavioural check above
        # cannot see this: recall reads each candidate's body and skips the ones
        # it cannot find, so a dangling FTS row produces no visible wrong answer.
        # It is still a leak - the index would grow forever, eviction being the
        # only thing that ever removes from it - and it wastes candidate slots,
        # which are capped, so enough of them push real memories out of range.
        #
        # Found by mutation: deleting the FTS cleanup left the test above green.
        self.cache("their-note", "Their lesson about shader stalls on the build farm")
        cached_uuid = self.store.connection.execute(
            "SELECT uuid FROM cached_memories WHERE slug='their-note'").fetchone()["uuid"]
        self.assertIsNotNone(self.store.connection.execute(
            "SELECT 1 FROM memories_fts WHERE memory_id=?", (cached_uuid,)).fetchone())

        self.store.evict_cache(older_than_days=30, keep_most_recent=0)

        self.assertIsNone(
            self.store.connection.execute(
                "SELECT 1 FROM memories_fts WHERE memory_id=?", (cached_uuid,)).fetchone(),
            "eviction left the search index pointing at a body it had deleted")
        self.assertIsNotNone(
            self.store.connection.execute(
                "SELECT 1 FROM memories_fts WHERE memory_id=?",
                (self.store._uuid_for("local-note"),)).fetchone(),
            "eviction removed our own memory from the index")

    def test_our_own_memories_survive_an_eviction(self):
        # The counterweight. Evicting everything would satisfy the two above.
        self.cache("their-note", "Their lesson about shader stalls on the build farm")
        self.store.evict_cache(older_than_days=0, keep_most_recent=0)
        self.assertIn("Our own lesson", self.store.get_memory("local-note")["description"])
        self.assertEqual(1, self.store.count())


class LayeringGuardTests(unittest.TestCase):
    def test_the_transport_only_unlocks_the_call_that_needs_it(self):
        # If this ever returns True for a write, that write runs unserialised
        # against SQLite, which is the bug this routing could introduce.
        from project_memory_mcp.http_server import _talks_to_other_machines

        def call(name, arguments=None):
            return {"method": "tools/call",
                    "params": {"name": name, "arguments": arguments or {}}}

        self.assertTrue(_talks_to_other_machines(
            call("recall", {"include_remotes": True})))
        self.assertFalse(_talks_to_other_machines(call("recall", {})))
        self.assertFalse(_talks_to_other_machines(call("create_memory", {"memory": {}})))
        self.assertFalse(_talks_to_other_machines(
            call("promote_memory", {"include_remotes": True})))
        self.assertFalse(_talks_to_other_machines({"method": "tools/list"}))


if __name__ == "__main__":
    unittest.main()
