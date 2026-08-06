"""Tests for BM25 text scoring, the weighted memory graph, and ranked recall."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from project_memory_mcp.ranking import (
    DERIVED_MAX_NEIGHBOURS,
    build_adjacency,
    personalized_pagerank,
    rank_memories,
    tokenize,
)
from project_memory_mcp.store import MemoryStore, StoreError


def memory(
    memory_id: str,
    description: str,
    labels: list[str],
    related: list[str] | None = None,
    status: str = "active",
    tags: list[str] | None = None,
    files: list[str] | None = None,
) -> dict:
    return {
        "schema_version": 1,
        "id": memory_id,
        "status": status,
        "description": description,
        "tags": tags or [],
        "labels": labels,
        "scope": {"project": "p", "area": "a", "files": files or [], "applies_to": []},
        "triggers": [f"trigger for {memory_id}"],
        "remembered_facts": [description],
        "solution_pattern": [],
        "pitfalls": [],
        "evidence": {"created_from_task": "t", "last_validated": "2026-01-01"},
        "relationships": {
            "related": [{"id": other, "reason": "They share a subsystem."} for other in (related or [])],
            "supersedes": [],
            "superseded_by": [],
        },
    }


class TokenizeTests(unittest.TestCase):
    def test_splits_camel_case_but_keeps_the_whole_identifier(self):
        tokens = tokenize("bForceSynchronousBuild")
        self.assertIn("bforcesynchronousbuild", tokens)
        self.assertIn("synchronous", tokens)
        self.assertIn("build", tokens)

    def test_handles_acronyms_and_punctuation(self):
        tokens = tokenize("HISMComponent.set_value(42)")
        self.assertIn("hism", tokens)
        self.assertIn("component", tokens)
        self.assertIn("42", tokens)

    def test_single_characters_are_not_emitted_as_parts(self):
        # 'b' from bReplicates is noise; the whole token still carries it.
        self.assertNotIn("b", tokenize("bReplicates"))
        self.assertIn("replicates", tokenize("bReplicates"))


class AdjacencyTests(unittest.TestCase):
    def test_curated_links_are_symmetric_and_full_weight(self):
        memories = {
            "a": memory("a", "first memory about caching", ["area:x"], related=["b"]),
            "b": memory("b", "second memory about caching", ["area:y"]),
        }
        adjacency = build_adjacency(memories, include_derived=False)
        self.assertEqual(adjacency["a"]["b"], 1.0)
        self.assertEqual(adjacency["b"]["a"], 1.0)

    def test_derived_edges_are_weaker_than_curated_ones(self):
        shared = ["area:x", "kind:bug", "context:runtime"]
        memories = {
            "a": memory("a", "first memory about caching", shared),
            "b": memory("b", "second memory about caching", shared),
        }
        derived = build_adjacency(memories, include_derived=True)
        self.assertIn("b", derived["a"])
        self.assertLess(derived["a"]["b"], 1.0)
        self.assertEqual(build_adjacency(memories, include_derived=False)["a"], {})

    def test_total_derived_edges_stay_linear_in_store_size(self):
        # Without a cap every memory sharing labels links to every other, so
        # edges - and every PageRank iteration - grow as N^2. The cap bounds
        # the total; individual degree can still exceed it, because connect()
        # is symmetric and a memory chosen by many others keeps those edges.
        # This fixture is the worst case for that: identical labels means every
        # similarity ties, so all 40 pick the same first ten.
        shared = ["area:x", "kind:bug"]
        memories = {f"m-{i:03d}": memory(f"m-{i:03d}", f"A memory number {i} about caching.", shared)
                    for i in range(40)}
        adjacency = build_adjacency(memories, include_derived=True)

        edges = sum(len(v) for v in adjacency.values()) // 2
        self.assertLessEqual(edges, len(memories) * DERIVED_MAX_NEIGHBOURS)
        self.assertLess(edges, len(memories) * (len(memories) - 1) // 2)  # far below complete
        self.assertGreater(edges, 0)

    def test_a_label_on_most_of_the_store_generates_no_candidates(self):
        # A label that broad carries no relatedness signal, like a stopword.
        # Below the floor it still counts, so small stores are unaffected.
        big = {f"m-{i:04d}": memory(f"m-{i:04d}", f"Memory {i} with an ubiquitous label.", ["area:x"])
               for i in range(300)}
        self.assertEqual(0, sum(len(v) for v in build_adjacency(big, include_derived=True).values()))

        small = {f"m-{i:03d}": memory(f"m-{i:03d}", f"Memory {i} with a shared label.", ["area:x"])
                 for i in range(20)}
        self.assertGreater(sum(len(v) for v in build_adjacency(small, include_derived=True).values()), 0)

    def test_curated_links_survive_the_derived_cap(self):
        shared = ["area:x", "kind:bug"]
        memories = {f"m-{i:03d}": memory(f"m-{i:03d}", f"A memory number {i} about caching.", shared)
                    for i in range(40)}
        memories["m-000"]["relationships"]["related"] = [
            {"id": "m-039", "reason": "They describe the same failure."}
        ]
        adjacency = build_adjacency(memories, include_derived=True)

        self.assertEqual(1.0, adjacency["m-000"]["m-039"])

    def test_dangling_reference_is_ignored(self):
        memories = {"a": memory("a", "a memory referencing a ghost", ["area:x"], related=["gone"])}
        self.assertEqual(build_adjacency(memories, include_derived=False)["a"], {})


class PageRankTests(unittest.TestCase):
    def test_mass_is_conserved(self):
        adjacency = {"a": {"b": 1.0}, "b": {"a": 1.0, "c": 1.0}, "c": {"b": 1.0}}
        ranks = personalized_pagerank(adjacency)
        self.assertAlmostEqual(sum(ranks.values()), 1.0, places=6)

    def test_seeding_favours_the_seed_neighbourhood(self):
        # Two triangles joined by a single bridge; seeding in one must not
        # rank the far side above the near side.
        adjacency = {
            "a1": {"a2": 1.0, "a3": 1.0},
            "a2": {"a1": 1.0, "a3": 1.0},
            "a3": {"a1": 1.0, "a2": 1.0, "b1": 1.0},
            "b1": {"b2": 1.0, "b3": 1.0, "a3": 1.0},
            "b2": {"b1": 1.0, "b3": 1.0},
            "b3": {"b1": 1.0, "b2": 1.0},
        }
        ranks = personalized_pagerank(adjacency, {"a1": 1.0})
        self.assertGreater(ranks["a2"], ranks["b2"])
        self.assertGreater(ranks["a3"], ranks["b1"])

    def test_isolated_node_does_not_leak_mass(self):
        adjacency = {"a": {"b": 1.0}, "b": {"a": 1.0}, "lonely": {}}
        ranks = personalized_pagerank(adjacency)
        self.assertAlmostEqual(sum(ranks.values()), 1.0, places=6)
        self.assertGreater(ranks["lonely"], 0.0)

    def test_uniform_seeding_matches_global_pagerank(self):
        adjacency = {"a": {"b": 1.0}, "b": {"a": 1.0, "c": 1.0}, "c": {"b": 1.0}}
        uniform = {node: 1.0 for node in adjacency}
        self.assertEqual(
            [round(v, 9) for v in personalized_pagerank(adjacency).values()],
            [round(v, 9) for v in personalized_pagerank(adjacency, uniform).values()],
        )


class RankMemoriesTests(unittest.TestCase):
    def setUp(self):
        self.memories = {
            "cache-race": memory("cache-race", "Session cache invalidation races the auth refresh.", ["area:x"]),
            "token-skew": memory("token-skew", "Token refresh breaks under clock skew.", ["area:x"], related=["cache-race"]),
            "unrelated": memory("unrelated", "Shader compilation stalls on cold start.", ["area:z"]),
        }

    def test_text_match_outranks_unrelated_memory(self):
        ranked = rank_memories(self.memories, query="cache invalidation race")
        self.assertEqual(ranked[0]["id"], "cache-race")
        self.assertGreater(ranked[0]["text_score"], 0.0)

    def test_wrong_status_is_demoted_but_still_returned(self):
        query = "cache invalidation race"
        before = {entry["id"]: entry for entry in rank_memories(self.memories, query=query)}
        self.memories["cache-race"]["status"] = "wrong"
        after = {entry["id"]: entry for entry in rank_memories(self.memories, query=query)}
        # Kept deliberately as a warning, so it must still come back...
        self.assertIn("cache-race", after)
        # ...but scored far below what the same text match earned while active.
        self.assertEqual(after["cache-race"]["status_factor"], 0.2)
        self.assertLess(after["cache-race"]["score"], before["cache-race"]["score"])

    def test_related_to_anchors_the_walk_and_drops_the_anchor(self):
        ranked = rank_memories(self.memories, related_to="cache-race")
        ids = [entry["id"] for entry in ranked]
        self.assertNotIn("cache-race", ids)
        self.assertEqual(ids[0], "token-skew")

    def test_empty_query_falls_back_to_central_memories(self):
        ranked = rank_memories(self.memories)
        self.assertEqual(len(ranked), 3)
        self.assertTrue(all(entry["graph_score"] > 0 for entry in ranked))


class TagFormatTests(unittest.TestCase):
    """Tags accept code identifiers; ids and labels stay strict slugs."""

    def setUp(self):
        self.store = MemoryStore(Path(tempfile.mkdtemp()))

    def test_code_identifier_tags_are_accepted(self):
        entry = memory("a", "A memory about replicated actors.", ["area:x"])
        entry["tags"] = ["bReplicates", "C4459", "from_pydata", "localUVDensities", "kebab-case"]
        self.assertEqual(self.store.validate_memory(entry, None, "test"), [])

    def test_duplicate_tags_are_still_rejected(self):
        entry = memory("a", "A memory about replicated actors.", ["area:x"])
        entry["tags"] = ["dupe", "dupe"]
        self.assertTrue(self.store.validate_memory(entry, None, "test"))

    def test_non_string_tags_are_still_rejected(self):
        entry = memory("a", "A memory about replicated actors.", ["area:x"])
        entry["tags"] = [42]
        self.assertTrue(self.store.validate_memory(entry, None, "test"))

    def test_ids_and_labels_remain_strict(self):
        bad_id = memory("NotASlug", "A memory with an invalid id value.", ["area:x"])
        self.assertTrue(self.store.validate_memory(bad_id, None, "test"))
        bad_label = memory("a", "A memory with an invalid label value.", ["NotALabel"])
        self.assertTrue(self.store.validate_memory(bad_label, None, "test"))


class RecallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        store_dir = root / ".project-memory" / "active"
        store_dir.mkdir(parents=True)
        (root / ".project-memory" / "labels.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "description": "labels",
                    "labels": {"area:x": {"description": "x"}, "area:z": {"description": "z"}},
                }
            ),
            encoding="utf-8",
        )
        for entry in (
            memory("cache-race", "Session cache invalidation races the auth refresh.", ["area:x"], related=["token-skew"]),
            memory("token-skew", "Token refresh breaks under clock skew.", ["area:x"], related=["cache-race"]),
            memory("shader-stall", "Shader compilation stalls on cold start.", ["area:z"]),
        ):
            (store_dir / f"{entry['id']}.json").write_text(json.dumps(entry), encoding="utf-8")
        self.store = MemoryStore(root)

    def test_recall_ranks_and_inlines_full_memories(self):
        result = self.store.recall(query="cache invalidation", limit=2, full_count=1)
        self.assertEqual(result["memories"][0]["id"], "cache-race")
        self.assertIn("memory", result["memories"][0])
        self.assertNotIn("memory", result["memories"][1])
        self.assertEqual(result["considered"], 3)

    def test_label_filter_restricts_results_without_breaking_the_graph(self):
        result = self.store.recall(query="cache", label_query="area:z")
        self.assertEqual([entry["id"] for entry in result["memories"]], ["shader-stall"])

    def test_related_to_excludes_the_anchor(self):
        result = self.store.recall(related_to="cache-race")
        self.assertNotIn("cache-race", [entry["id"] for entry in result["memories"]])
        self.assertEqual(result["related_to"], "cache-race")

    def test_related_to_rejects_unknown_id(self):
        with self.assertRaises(StoreError):
            self.store.recall(related_to="does-not-exist")

    def test_invalid_limits_are_rejected(self):
        with self.assertRaises(StoreError):
            self.store.recall(limit=0)
        with self.assertRaises(StoreError):
            self.store.recall(full_count=-1)

    def test_cache_is_invalidated_when_a_memory_is_added_or_removed(self):
        # The cache key is built from a scandir listing, so appearing and
        # disappearing files must invalidate it as reliably as edits do.
        self.assertNotIn("late-arrival", self.store.load_memories())
        added = self.store.active_root / "late-arrival.json"
        added.write_text(
            json.dumps(memory("late-arrival", "A memory written outside this process.", ["area:x"])),
            encoding="utf-8",
        )
        self.assertIn("late-arrival", self.store.load_memories())

        added.unlink()
        self.assertNotIn("late-arrival", self.store.load_memories())

    def test_cache_is_invalidated_when_a_memory_changes(self):
        first = self.store.load_memories()
        self.assertIs(self.store.load_memories(), first)
        path = self.store.active_root / "shader-stall.json"
        changed = json.loads(path.read_text(encoding="utf-8"))
        changed["description"] = "Shader compilation stalls badly on a cold start."
        path.write_text(json.dumps(changed), encoding="utf-8")
        reloaded = self.store.load_memories()
        self.assertEqual(
            reloaded["shader-stall"]["memory"]["description"],
            "Shader compilation stalls badly on a cold start.",
        )


if __name__ == "__main__":
    unittest.main()


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        store_dir = root / ".project-memory" / "active"
        store_dir.mkdir(parents=True)
        (root / ".project-memory" / "labels.json").write_text(
            json.dumps({"schema_version": 1, "description": "l",
                        "labels": {"area:x": {"description": "x"}}}),
            encoding="utf-8",
        )
        for entry in (
            memory("cache-race", "Session cache invalidation races the auth refresh.", ["area:x"]),
            memory("shader-stall", "Shader compilation stalls on cold start.", ["area:x"]),
        ):
            (store_dir / f"{entry['id']}.json").write_text(json.dumps(entry), encoding="utf-8")
        self.store = MemoryStore(root)

    def test_recall_records_what_it_surfaced(self):
        self.store.recall(query="cache invalidation", limit=1, full_count=0)
        entries = self.store.load_usage()["memories"]

        self.assertEqual(1, entries["cache-race"]["surfaced"])
        self.assertIn("last_surfaced", entries["cache-race"])
        self.assertNotIn("shader-stall", entries)

    def test_surfaced_accumulates_across_calls(self):
        for _ in range(3):
            self.store.recall(query="cache invalidation", limit=1, full_count=0)

        self.assertEqual(3, self.store.load_usage()["memories"]["cache-race"]["surfaced"])

    def test_applied_is_separate_from_surfaced(self):
        self.store.recall(query="cache invalidation", limit=2, full_count=0)
        self.store.record_use(["cache-race"])
        entries = self.store.load_usage()["memories"]

        self.assertEqual(1, entries["cache-race"]["applied"])
        # Surfaced but never applied: exactly the signal audit will look for.
        self.assertEqual(0, entries["shader-stall"].get("applied", 0))
        self.assertGreater(entries["shader-stall"]["surfaced"], 0)

    def test_record_use_rejects_unknown_ids(self):
        with self.assertRaises(StoreError):
            self.store.record_use(["no-such-memory"])

    def test_usage_lives_outside_the_memory_files(self):
        before = (self.store.active_root / "cache-race.json").read_bytes()
        self.store.recall(query="cache invalidation")
        self.store.record_use(["cache-race"])

        self.assertEqual(before, (self.store.active_root / "cache-race.json").read_bytes())
        self.assertTrue(self.store.usage_path.is_file())
        self.assertEqual([], self.store.validate_store())

    def test_corrupt_usage_file_does_not_break_recall(self):
        self.store.usage_path.write_text("{ not json", encoding="utf-8")

        result = self.store.recall(query="cache invalidation", limit=1, full_count=0)

        self.assertEqual(1, result["count"])
        self.assertEqual(1, self.store.load_usage()["memories"]["cache-race"]["surfaced"])

    def test_counts_are_visible_before_they_are_flushed(self):
        # Buffering must not make load_usage lie: pending counts merge on read.
        self.store._usage_last_flush = 1e9   # far future -> nothing will flush
        self.store.recall(query="cache invalidation", limit=1, full_count=0)

        self.assertFalse(self.store.usage_path.is_file())
        self.assertEqual(1, self.store.load_usage()["memories"]["cache-race"]["surfaced"])

    def test_a_burst_of_recalls_writes_once(self):
        self.store.recall(query="cache invalidation", limit=1, full_count=0)  # first flushes
        stamp = self.store.usage_path.stat().st_mtime_ns
        for _ in range(10):
            self.store.recall(query="cache invalidation", limit=1, full_count=0)

        self.assertEqual(stamp, self.store.usage_path.stat().st_mtime_ns)
        # ...and none of those counts are lost.
        self.assertEqual(11, self.store.load_usage()["memories"]["cache-race"]["surfaced"])

    def test_forced_flush_persists_the_buffer(self):
        self.store._usage_last_flush = 1e9
        self.store.recall(query="cache invalidation", limit=1, full_count=0)

        self.assertTrue(self.store.flush_usage(force=True))
        reloaded = MemoryStore(self.store.root).load_usage()
        self.assertEqual(1, reloaded["memories"]["cache-race"]["surfaced"])
        self.assertFalse(self.store.flush_usage(force=True))  # nothing left pending
