"""SQLite backend: same contract as the file backend, and no memory files."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from project_memory_mcp.sqlite_store import SqliteMemoryStore, StoreError


def memory(memory_id, description, labels, related=None, status="active"):
    return {
        "schema_version": 1,
        "id": memory_id,
        "status": status,
        "description": description,
        "tags": [],
        "labels": labels,
        "scope": {"project": "p", "area": "a", "files": [], "applies_to": []},
        "triggers": ["trigger for " + memory_id],
        "remembered_facts": [description],
        "solution_pattern": [],
        "pitfalls": [],
        "evidence": {"created_from_task": "t", "last_validated": "2026-01-01"},
        "relationships": {
            "related": [{"id": o, "reason": "They share a subsystem."} for o in (related or [])],
            "supersedes": [],
            "superseded_by": [],
        },
    }


CACHE = "Session cache invalidation races the auth refresh."
SHADER = "Shader compilation stalls on a cold start."
FIRST = "First memory about the caching layer."
SECOND = "Second memory about the caching layer."


class SqliteStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "memory.db"
        self.store = SqliteMemoryStore(self.db, project="demo")
        self.addCleanup(self.store.close)
        for label in ("area:x", "area:z", "kind:bug"):
            self.store.add_label(label, "description for " + label)

    def test_a_bad_label_query_raises_the_store_error_callers_catch(self):
        # The label grammar used to live in the file backend and raise that
        # module's StoreError, which the HTTP layer does not catch - so an
        # unknown label in a query returned 500 instead of a client error.
        self.store.create_memory(memory("cache-race", CACHE, ["area:x"]))
        for bad in ("area:unregistered", "area:x AND", "NotALabel"):
            with self.subTest(query=bad), self.assertRaises(StoreError):
                self.store.recall("anything", label_query=bad)

    def test_create_and_read_back(self):
        self.store.create_memory(memory("cache-race", CACHE, ["area:x"]))
        got = self.store.get_memory("cache-race")
        self.assertEqual("cache-race", got["id"])
        self.assertEqual(1, self.store.count())
        self.assertRegex(got["evidence"]["created"], r"^\d{4}-\d{2}-\d{2}T")

    def test_no_memory_files_are_written(self):
        self.store.create_memory(memory("cache-race", CACHE, ["area:x"]))
        written = [p.name for p in Path(self.tmp.name).iterdir()]
        self.assertNotIn("active", written)
        self.assertTrue(any(name.startswith("memory.db") for name in written))

    def test_duplicate_id_is_rejected(self):
        self.store.create_memory(memory("cache-race", CACHE, ["area:x"]))
        with self.assertRaises(StoreError):
            self.store.create_memory(memory("cache-race", SHADER, ["area:x"]))

    def test_invalid_memory_leaves_nothing_behind(self):
        with self.assertRaises(StoreError):
            self.store.create_memory(memory("bad-one", "too short", ["area:x"]))
        self.assertEqual(0, self.store.count())

    def test_unknown_label_is_rejected(self):
        with self.assertRaises(StoreError):
            self.store.create_memory(memory("m", "A memory using an unregistered label.", ["area:nope"]))

    def test_relationships_are_mirrored_onto_the_target(self):
        self.store.create_memory(memory("a", FIRST, ["area:x"]))
        self.store.create_memory(memory("b", SECOND, ["area:x"], related=["a"]))
        back = self.store.get_memory("a")["relationships"]["related"]
        self.assertEqual(["b"], [e["id"] for e in back])
        self.assertEqual("They share a subsystem.", back[0]["reason"])

    def test_update_keeps_the_previous_body_as_a_revision(self):
        self.store.create_memory(memory("a", FIRST, ["area:x"]))
        self.store.update_memory("a", {"status": "stale"})
        self.assertEqual("stale", self.store.get_memory("a")["status"])
        row = self.store.connection.execute(
            "SELECT COUNT(*) AS n FROM revisions WHERE project_id='demo' AND memory_id='a'"
        ).fetchone()
        self.assertEqual(1, row["n"])

    def test_delete_cleans_references_to_it(self):
        self.store.create_memory(memory("a", FIRST, ["area:x"]))
        self.store.create_memory(memory("b", SECOND, ["area:x"], related=["a"]))
        result = self.store.delete_memory("a", "a")
        self.assertEqual(["b"], result["cleaned_references_in"])
        self.assertEqual([], self.store.get_memory("b")["relationships"]["related"])
        with self.assertRaises(StoreError):
            self.store.get_memory("a")

    def test_delete_requires_exact_confirmation(self):
        self.store.create_memory(memory("a", FIRST, ["area:x"]))
        with self.assertRaises(StoreError):
            self.store.delete_memory("a", "not-a")

    def test_recall_ranks_and_inlines_the_top_result(self):
        self.store.create_memory(memory("cache-race", CACHE, ["area:x"]))
        self.store.create_memory(memory("shader-stall", SHADER, ["area:z"]))
        result = self.store.recall(query="cache invalidation", limit=2, full_count=1)
        self.assertEqual("cache-race", result["memories"][0]["id"])
        self.assertIn("memory", result["memories"][0])
        self.assertIn("why", result["memories"][0])

    def test_search_filters_by_label_and_status(self):
        self.store.create_memory(memory("a", FIRST, ["area:x"]))
        self.store.create_memory(memory("b", SHADER, ["area:z"]))
        self.assertEqual(["a"], [m["id"] for m in self.store.search_memories(label_query="area:x")["memories"]])
        self.store.update_memory("a", {"status": "wrong"})
        self.assertEqual([], self.store.search_memories(label_query="area:x")["memories"])
        self.assertEqual(
            ["a"],
            [m["id"] for m in self.store.search_memories(label_query="area:x", status_filter="all")["memories"]],
        )

    def test_recent_is_newest_first_and_pages(self):
        for i in range(3):
            self.store.create_memory(memory("m-" + str(i), "Memory number %d about a failure mode." % i, ["area:x"]))
        self.assertEqual("m-2", self.store.recent(limit=1)["memories"][0]["id"])
        self.assertEqual("m-1", self.store.recent(limit=1, offset=1)["memories"][0]["id"])

    def test_usage_counters_are_upserted(self):
        self.store.create_memory(memory("cache-race", CACHE, ["area:x"]))
        for _ in range(3):
            self.store.recall(query="cache invalidation", limit=1, full_count=0)
        self.store.record_use(["cache-race"])
        entry = self.store.load_usage()["memories"]["cache-race"]
        self.assertEqual(3, entry["surfaced"])
        self.assertEqual(1, entry["applied"])

    def test_record_use_rejects_unknown_id(self):
        with self.assertRaises(StoreError):
            self.store.record_use(["nope"])

    def test_projects_sharing_one_database_stay_isolated(self):
        self.store.create_memory(memory("shared-id", "A memory belonging to the demo project.", ["area:x"]))
        other = SqliteMemoryStore(self.db, project="other")
        self.addCleanup(other.close)
        self.assertEqual(0, other.count())
        other.add_label("area:x", "x")
        other.create_memory(memory("shared-id", "A different memory under the same id.", ["area:x"]))
        self.assertEqual(1, other.count())
        self.assertEqual(1, self.store.count())
        self.assertNotEqual(
            self.store.get_memory("shared-id")["description"],
            other.get_memory("shared-id")["description"],
        )

    def test_validate_store_is_clean_for_a_healthy_project(self):
        self.store.create_memory(memory("a", FIRST, ["area:x"]))
        self.store.create_memory(memory("b", SECOND, ["area:x"], related=["a"]))
        self.assertEqual([], self.store.validate_store())

    def test_project_id_must_be_a_slug(self):
        with self.assertRaises(StoreError):
            SqliteMemoryStore(self.db, project="Not A Slug")

    def test_neighborhood_walks_authored_links_only(self):
        self.store.create_memory(memory("a", FIRST, ["area:x"]))
        self.store.create_memory(memory("b", SECOND, ["area:x"], related=["a"]))
        result = self.store.get_memory_neighborhood("b", depth=1)
        self.assertEqual("b", result["root"])
        self.assertIn("a", [n["id"] for n in result["nodes"]])
        self.assertTrue(all(e["type"] != "derived" for e in result["edges"]))

    def test_neighborhood_rejects_unknown_id(self):
        with self.assertRaises(StoreError):
            self.store.get_memory_neighborhood("nope")

    def test_recall_recent_order_and_anchors(self):
        for i in range(3):
            self.store.create_memory(memory("m-" + str(i), "Memory number %d about a failure." % i, ["area:x"]))
        newest = self.store.recall(order="recent", limit=3, full_count=0)
        self.assertEqual("m-2", newest["memories"][0]["id"])
        before = self.store.recall(order="recent", before="m-2", limit=2, full_count=0)
        self.assertEqual(["m-1", "m-0"], [m["id"] for m in before["memories"]])
        after = self.store.recall(order="recent", after="m-0", limit=2, full_count=0)
        self.assertEqual(["m-1", "m-2"], [m["id"] for m in after["memories"]])
        self.assertNotIn("m-2", [m["id"] for m in before["memories"][:0]])

    def test_recall_rejects_bad_order_and_anchor_combinations(self):
        with self.assertRaises(StoreError):
            self.store.recall(order="sideways")
        with self.assertRaises(StoreError):
            self.store.recall(before="m-1")
        with self.assertRaises(StoreError):
            self.store.recall(order="recent", before="a", after="b")
        with self.assertRaises(StoreError):
            self.store.recall(offset=-1)
