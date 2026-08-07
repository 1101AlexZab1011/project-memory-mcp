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


class EveryScriptTests(unittest.TestCase):
    """A script the tokenizer cannot see is a language the store cannot hold.

    `_TOKEN_RE` was `[A-Za-z0-9]+`, so Cyrillic and CJK text produced no tokens
    at all: a memory written in Russian or Japanese was indexed as empty and
    could never be recalled by any word it contained. Recall simply returned
    nothing - no error, no warning, just a store that had quietly forgotten.

    Accented Latin failed differently and worse: `café naïve` became
    `caf`, `na`, `ve`, indexing fragments of words as if they were words.

    Nothing in this suite had ever typed a non-ASCII character. Found by the
    federation simulation, which writes one memory in Russian.
    """

    def test_cyrillic_is_tokenized(self):
        self.assertEqual(["шейдерный", "кэш"], tokenize("Шейдерный кэш"))

    def test_japanese_is_tokenized_whole_and_as_bigrams(self):
        # Japanese puts no spaces between words, so the "word" a regex finds is
        # a whole clause - which only an identical clause could ever match.
        # Bigrams are the usual answer without a real segmenter, and they are
        # symmetric: the index and the query are built by this same function.
        tokens = tokenize("シェーダー")
        self.assertEqual("シェーダー", tokens[0])
        self.assertEqual(["シェ", "ェー", "ーダ", "ダー"], tokens[1:])

    def test_a_japanese_query_shares_bigrams_with_the_sentence_holding_it(self):
        # The property that makes recall work at all: a phrase and the sentence
        # containing it must have terms in common.
        phrase = set(tokenize("シェーダーキャッシュ"))
        sentence = set(tokenize("シェーダーキャッシュはドライバ更新で再構築されます"))
        self.assertGreaterEqual(len(phrase & sentence), 8)

    def test_bigrams_are_only_for_scripts_without_spaces(self):
        # Cyrillic separates its words already; bigramming it would triple the
        # index for nothing and blur which word matched.
        self.assertEqual(["шейдерный", "кэш"], tokenize("Шейдерный кэш"))

    def test_greek_is_tokenized(self):
        self.assertEqual(["σκιάδιο"], tokenize("Σκιάδιο"))

    def test_an_accented_word_stays_one_word(self):
        # Not `caf`, `na`, `ve`. Case-splitting is an ASCII identifier
        # heuristic; applied here it indexes fragments of a word as words.
        self.assertEqual(["café", "naïve"], tokenize("café naïve"))

    def test_scripts_mix_within_one_string(self):
        self.assertEqual(["the", "шейдер", "cache"], tokenize("the шейдер cache"))

    def test_emoji_are_not_words(self):
        self.assertEqual([], tokenize("🔥💥"))

    def test_ascii_identifiers_are_unchanged(self):
        # The counterweight. Widening the character class must not cost the
        # case-boundary splitting that identifier search depends on.
        self.assertEqual(
            ["bforcesynchronousinstancebuild", "force", "synchronous", "instance", "build"],
            tokenize("bForceSynchronousInstanceBuild"))
        self.assertEqual(["httpserver", "http", "server"], tokenize("HTTPServer"))
        self.assertEqual(["snake", "case", "name"], tokenize("snake_case_name"))
        self.assertEqual(["utf8", "mode2"], tokenize("utf8 mode2"))


class NonEnglishRecallTests(unittest.TestCase):
    """End to end: write it in Russian, find it in Russian."""

    def setUp(self):
        import tempfile
        from pathlib import Path

        from project_memory_mcp.sqlite_store import SqliteMemoryStore

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SqliteMemoryStore(Path(self.tmp.name) / "memory.db", "demo")
        self.addCleanup(self.store.close)
        self.store.add_label("area:x", "x")

    def memory(self, slug, description, facts):
        return {
            "schema_version": 1, "id": slug, "status": "active",
            "description": description, "tags": [], "labels": ["area:x"],
            "scope": {"project": "p", "area": "a", "files": [], "applies_to": []},
            "triggers": ["триггер"], "remembered_facts": facts,
            "solution_pattern": [], "pitfalls": [],
            "evidence": {"created_from_task": "t", "last_validated": "2026-01-01"},
            "relationships": {"related": [], "supersedes": [], "superseded_by": []},
        }

    def test_a_russian_memory_is_recallable_in_russian(self):
        self.store.create_memory(self.memory(
            "shader-note", "Шейдерный кэш пересобирается при смене драйвера",
            ["Кэш шейдеров полностью пересобирается."]))
        found = self.store.recall("шейдерный кэш", record=False)
        self.assertEqual(["shader-note"], [m["id"] for m in found["memories"]])

    def test_a_japanese_memory_is_recallable_in_japanese(self):
        self.store.create_memory(self.memory(
            "cache-note", "シェーダーキャッシュはドライバ更新で再構築されます",
            ["キャッシュの問題"]))
        found = self.store.recall("シェーダーキャッシュ", record=False)
        self.assertEqual(["cache-note"], [m["id"] for m in found["memories"]])

    def test_the_text_survives_storage_exactly(self):
        text = "Кэш шейдеров 🔥"
        self.store.create_memory(self.memory(
            "round-trip", "Описание достаточной длины для валидатора здесь", [text]))
        self.assertEqual(text, self.store.get_memory("round-trip")["remembered_facts"][0])


if __name__ == "__main__":
    unittest.main()
