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

from project_memory_mcp import federation
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
            "SELECT origin_remote, visibility FROM memories WHERE slug='packaging-editor-open'"
        ).fetchone()
        self.assertEqual("team", row["origin_remote"])

    def test_a_cached_copy_is_not_this_machine_s_to_publish(self):
        federation.recall_across(self.store, "packaging fails editor open", limit=5)
        with self.assertRaises(StoreError) as caught:
            federation.promote(self.store, "packaging-editor-open", "team")
        self.assertIn("cached copy", str(caught.exception))

    def test_a_cached_slug_does_not_collide_with_a_local_one(self):
        # The unique-slug rule covers memories this machine owns, not borrowed
        # copies - otherwise caching would fail the moment two servers happened
        # to name a lesson the same way.
        self.store.create_memory(memory("packaging-editor-open", "A different local lesson entirely."))
        federation.recall_across(self.store, "packaging fails editor open", limit=5)
        rows = self.store.connection.execute(
            "SELECT origin_remote FROM memories WHERE slug='packaging-editor-open'").fetchall()
        self.assertEqual({None, "team"}, {row["origin_remote"] for row in rows})

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
