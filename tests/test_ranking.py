"""Tokenization and the seeded graph walk.

BM25 scoring and whole-store ranking moved into SQLite (FTS5 and a bounded
walk) when the file backend was removed; those are covered by test_sqlite_store.
"""

from __future__ import annotations

import unittest

from project_memory_mcp.ranking import personalized_pagerank, tokenize


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


if __name__ == "__main__":
    unittest.main()
