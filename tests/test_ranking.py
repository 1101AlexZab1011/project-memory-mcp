"""Tests for BM25 text scoring, the weighted memory graph, and ranked recall."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from project_memory_mcp.ranking import (
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
        self.store.regenerate_index()

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
