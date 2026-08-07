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

    def test_removing_a_link_removes_the_mirror_of_it(self):
        # The case the reverse lookup exists for. A write only visits the
        # memories it links to plus the ones that link back at it; if the second
        # half were missed, dropping a link would leave the mirror behind
        # forever and the graph would keep an edge nobody asked for.
        self.store.create_memory(memory("a", FIRST, ["area:x"]))
        self.store.create_memory(memory("b", SECOND, ["area:x"], related=["a"]))
        self.assertEqual(["b"], [e["id"] for e in
                                 self.store.get_memory("a")["relationships"]["related"]])

        self.store.update_memory("b", {"relationships": {
            "related": [], "supersedes": [], "superseded_by": []}})
        self.assertEqual([], self.store.get_memory("a")["relationships"]["related"],
                         "the mirrored link outlived the link it mirrored")

    def test_a_write_visits_only_the_memories_its_links_touch(self):
        # This used to load and parse every memory in the project on every
        # write. Counting bodies read is how that stays fixed: the cost of a
        # write must depend on its links, not on the size of the store.
        for i in range(30):
            self.store.create_memory(memory(f"bystander-{i}", f"{FIRST} number {i}", ["area:x"]))
        self.store.create_memory(memory("target", SECOND, ["area:x"]))

        seen: list[str] = []
        self.store.connection.set_trace_callback(seen.append)
        try:
            self.store.create_memory(
                memory("linker", "Links to exactly one thing.", ["area:x"], related=["target"]))
        finally:
            self.store.connection.set_trace_callback(None)

        scans = [sql for sql in seen
                 if "FROM memories" in sql and "body" in sql and "slug<>" in sql]
        self.assertEqual(
            [], scans,
            "a write is scanning every other memory in the project to mirror its links")

    def test_update_keeps_the_previous_body_as_a_revision(self):
        self.store.create_memory(memory("a", FIRST, ["area:x"]))
        self.store.update_memory("a", {"status": "stale"})
        self.assertEqual("stale", self.store.get_memory("a")["status"])
        row = self.store.connection.execute(
            "SELECT COUNT(*) AS n FROM revisions WHERE project_id='demo' AND memory_id=?",
            (self.store._uuid_for("a"),),
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


class StoreCase(unittest.TestCase):
    """Fixture only. Kept separate from SqliteStoreTests so that subclassing it
    for a new group does not re-run every test in that class as well."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "memory.db"
        self.store = SqliteMemoryStore(self.db, project="demo")
        self.addCleanup(self.store.close)
        for label in ("area:x", "area:z", "kind:bug"):
            self.store.add_label(label, "description for " + label)


class ReindexOnWriteTests(StoreCase):
    """An edit must replace a memory's derived rows, not add to them.

    Nothing covered this. The failure is quiet in the worst way: the memory
    looks correct when read back, while its old text stays searchable and its
    old files stay attached, so recall keeps matching a version that no longer
    exists.
    """

    def test_editing_the_text_stops_the_old_wording_matching(self):
        body = memory("a", "Shader compilation stalls on a cold start.", ["area:x"])
        self.store.create_memory(body)
        self.assertEqual(["a"], [m["id"] for m in self.store.recall(
            "shader compilation", limit=5, record=False)["memories"]])

        # Every field carrying the old wording, or the index is right to keep
        # matching and the test is measuring nothing.
        self.store.update_memory("a", {
            "description": "Audio mixing clips on the title screen.",
            "remembered_facts": ["Audio mixing clips on the title screen."],
            "triggers": ["audio mixing"]})
        found = [m["id"] for m in self.store.recall(
            "shader compilation", limit=5, record=False)["memories"]]
        self.assertEqual([], found, "the replaced wording is still in the search index")
        self.assertEqual(["a"], [m["id"] for m in self.store.recall(
            "audio mixing clips", limit=5, record=False)["memories"]])

    def test_editing_the_scope_detaches_the_files_it_dropped(self):
        body = memory("a", FIRST, ["area:x"])
        body["scope"]["files"] = ["Source/Old.cpp"]
        self.store.create_memory(body)
        self.store.update_memory("a", {"scope": {"project": "p", "area": "a",
                                                 "files": ["Source/New.cpp"], "applies_to": []}})
        rows = [r["path"] for r in self.store.connection.execute(
            "SELECT path FROM files WHERE project_id='demo'")]
        self.assertEqual(["Source/New.cpp"], rows,
                         "the dropped file is still attached to the memory")


class RankingShapeTests(StoreCase):
    """Properties of the ranker that are decisions rather than tuning."""

    def test_a_memory_marked_wrong_ranks_below_an_equal_one(self):
        # Status is a multiplier for a reason: a memory somebody marked wrong
        # must not sit above an active one that matches just as well.
        for slug, status in (("good-note", "active"), ("bad-note", "wrong")):
            self.store.create_memory(memory(slug, "Cache invalidation races the auth refresh.",
                                            ["area:x"]))
            if status != "active":
                self.store.update_memory(slug, {"status": status})
        found = self.store.recall("cache invalidation races", limit=5,
                                  status_filter="all", record=False)["memories"]
        order = [m["id"] for m in found]
        self.assertEqual(["good-note", "bad-note"], order,
                         "status is not weighing on the ranking at all")
        self.assertLess([m for m in found if m["id"] == "bad-note"][0]["score"],
                        [m for m in found if m["id"] == "good-note"][0]["score"])

    def test_the_graph_walk_stays_bounded_by_default(self):
        """The walk must stop expanding, using the bound recall actually runs under.

        Two earlier versions of this were wrong in different ways. The first
        passed `max_nodes` explicitly, which proves the parameter works and says
        nothing about the default. The second built a real 500-memory store, and
        under an unbounded walk it did not fail - it ran for over two minutes,
        which is useless as a signal and hangs anything driving it.

        The walk reads only the edges table, so the graph is written straight
        into it. That is instant, and it lets the shape be chosen precisely: one
        seed, a first level wide enough to exhaust the bound, and a second level
        that only an unbounded walk would ever reach.
        """
        from project_memory_mcp.sqlite_store import WALK_MAX_NODES

        wide = WALK_MAX_NODES + 50
        edges = [("demo", "seed", f"L1-{i}", "related") for i in range(wide)]
        edges += [("demo", f"L1-{i}", f"L2-{i}-{j}", "related")
                  for i in range(wide) for j in range(10)]
        with self.store.connection:
            self.store.connection.executemany(
                "INSERT OR REPLACE INTO edges(project_id, src, dst, kind) VALUES (?,?,?,?)", edges)

        adjacency = self.store.neighbourhood(["seed"])
        self.assertLess(
            len(adjacency), wide * 2,
            "the walk expanded past its bound into the second level - ranking cost is no "
            "longer independent of how large and connected the store is")

    def test_unrelated_memories_are_not_linked_by_similarity(self):
        # The derived-edge threshold. At zero every candidate becomes a
        # neighbour and the graph stops carrying information.
        # They have to be *candidates* first, or the threshold is never
        # consulted and the test passes at any value. Sharing one label out of
        # three scores 0.23 - a real candidate, below the 0.34 cutoff.
        self.store.create_memory(memory("broad-note", "Cache invalidation races auth.",
                                        ["area:x", "area:z", "kind:bug"]))
        self.store.create_memory(memory("narrow-note", "Shader stalls on cold start.", ["area:x"]))
        edges = self.store.connection.execute(
            "SELECT COUNT(*) AS n FROM edges WHERE project_id='demo' AND kind='derived'"
        ).fetchone()["n"]
        self.assertEqual(0, edges, "two unrelated memories were linked as similar")

    def test_a_memory_keeps_only_its_strongest_neighbours(self):
        # Without the cap one popular label makes every memory a hub, and the
        # walk degenerates into "everything is related to everything".
        for i in range(25):
            body = memory(f"sib-{i:02d}", f"{FIRST} variation {i}", ["area:x"])
            body["scope"]["files"] = ["Source/Shared.cpp"]
            self.store.create_memory(body)
        worst = self.store.connection.execute(
            "SELECT COUNT(*) AS n FROM edges WHERE project_id='demo' AND kind='derived' "
            "GROUP BY src ORDER BY n DESC LIMIT 1").fetchone()["n"]
        self.assertLessEqual(worst, 10, "a memory kept more derived neighbours than the cap")
