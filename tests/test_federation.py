"""Federation: several servers holding the same project, meant to differ.

What these protect: local always answers, results merge by rank rather than by
incomparable scores, a private memory never leaves, and one unreachable remote
never takes down a query the others can serve.
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from project_memory_mcp import federation
from project_memory_mcp.federation import Remote, choose_remote, fuse
from project_memory_mcp.http_server import _Handler, _Sessions, _StoreRegistry
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
        self.assertEqual([], self.store.remotes())
        found = self.store.federated_recall("cache invalidation")
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
        targets = self.store.promotion_targets("user-habit")
        self.assertEqual([], targets["targets"])
        self.assertIn("never promoted", targets["why"])

    def test_promoting_a_private_memory_is_refused(self):
        federation.add_remote(self.store.connection, "team", "http://team", "everything")
        self.store.create_memory(memory("user-habit", "The user prefers early returns always."))
        with self.assertRaises(StoreError) as caught:
            self.store.promote("user-habit", "team")
        self.assertIn("private", str(caught.exception))

    def test_visibility_can_be_changed_deliberately(self):
        self.store.create_memory(memory("cache-race", CACHE))
        self.store.set_visibility("cache-race", "public")
        federation.add_remote(self.store.connection, "team", "http://team", "everything")
        self.assertTrue(self.store.promotion_targets("cache-race")["targets"])

    def test_a_memory_that_has_not_earned_a_tier_is_not_published(self):
        # Otherwise "earn your way into the shared store" is decorative: a
        # memory written thirty seconds ago could be pushed to everyone.
        federation.add_remote(self.store.connection, "team", "http://team", "everything")
        self.store.create_memory(memory("brand-new", CACHE), visibility="public")
        with self.assertRaises(StoreError) as caught:
            self.store.promote("brand-new", "team")
        self.assertIn("has not earned publication", str(caught.exception))

    def test_a_rare_but_critical_lesson_can_be_published_deliberately(self):
        # Knowledge that will never accrue usage still needs a way out.
        federation.add_remote(self.store.connection, "team", "http://127.0.0.1:9/", "down")
        self.store.create_memory(memory("rare-lesson", CACHE), visibility="public")
        result = self.store.promote("rare-lesson", "team", force=True)
        self.assertIn("queued", result)

    def test_an_unreachable_remote_queues_the_promotion_instead_of_failing(self):
        federation.add_remote(self.store.connection, "team", "http://127.0.0.1:9/", "nothing here")
        self.store.create_memory(memory("cache-race", CACHE), visibility="public")
        self.earn("cache-race")
        result = self.store.promote("cache-race", "team")
        self.assertIn("queued", result)
        queued = self.store.connection.execute("SELECT COUNT(*) AS n FROM outbox").fetchone()["n"]
        self.assertEqual(1, queued)

    def test_recall_still_answers_when_a_remote_is_down(self):
        federation.add_remote(self.store.connection, "dead", "http://127.0.0.1:9/", "unreachable")
        self.store.create_memory(memory("cache-race", CACHE), visibility="public")
        found = self.store.federated_recall("cache invalidation")
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
        found = self.store.federated_recall("packaging fails editor open", limit=5)
        self.assertIn("packaging-editor-open", [m["id"] for m in found["memories"]])
        self.assertEqual(["local", "team"], found["sources_answered"])
        self.assertEqual({}, found["sources_unreachable"])

    def test_a_result_says_which_source_it_came_from(self):
        found = self.store.federated_recall("packaging fails editor open", limit=5)
        entry = [m for m in found["memories"] if m["id"] == "packaging-editor-open"][0]
        self.assertEqual(["team"], entry["sources"])

    def test_what_came_back_is_cached_and_marked_as_borrowed(self):
        self.store.federated_recall("packaging fails editor open", limit=5)
        row = self.store.connection.execute(
            "SELECT origin_remote, visibility FROM memories WHERE slug='packaging-editor-open'"
        ).fetchone()
        self.assertEqual("team", row["origin_remote"])

    def test_a_cached_copy_is_not_this_machine_s_to_publish(self):
        self.store.federated_recall("packaging fails editor open", limit=5)
        with self.assertRaises(StoreError) as caught:
            self.store.promote("packaging-editor-open", "team")
        self.assertIn("cached copy", str(caught.exception))

    def test_a_cached_slug_does_not_collide_with_a_local_one(self):
        # The unique-slug rule covers memories this machine owns, not borrowed
        # copies - otherwise caching would fail the moment two servers happened
        # to name a lesson the same way.
        self.store.create_memory(memory("packaging-editor-open", "A different local lesson entirely."))
        self.store.federated_recall("packaging fails editor open", limit=5)
        rows = self.store.connection.execute(
            "SELECT origin_remote FROM memories WHERE slug='packaging-editor-open'").fetchall()
        self.assertEqual({None, "team"}, {row["origin_remote"] for row in rows})

    def test_promotion_publishes_to_the_named_remote(self):
        self.store.create_memory(memory("cache-race", CACHE), visibility="public")
        self.earn("cache-race")
        result = self.store.promote("cache-race", "team")
        self.assertEqual("cache-race", result["promoted"])
        found = self.store.federated_recall("cache invalidation races auth", limit=5)
        entry = [m for m in found["memories"] if m["id"] == "cache-race"][0]
        self.assertEqual(["local", "team"], entry["sources"])

    def test_a_queued_promotion_is_sent_when_the_remote_returns(self):
        federation.add_remote(self.store.connection, "flaky", "http://127.0.0.1:9/", "down")
        self.store.create_memory(memory("cache-race", CACHE), visibility="public")
        self.earn("cache-race")
        self.store.promote("cache-race", "flaky")
        federation.add_remote(self.store.connection, "flaky", self.url, "back up", token=TOKEN)
        result = self.store.drain_outbox()
        self.assertEqual(["cache-race"], result["sent"])
        self.assertEqual(0, self.store.connection.execute(
            "SELECT COUNT(*) AS n FROM outbox").fetchone()["n"])


if __name__ == "__main__":
    unittest.main()
