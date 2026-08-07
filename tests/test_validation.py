"""Memory document validation, and the store-wide check that finds drift.

Writes validate the memory they touch, so an invalid document can only reach
the database through a migration, a restore, or an edit made outside the tools.
These tests reproduce that by rewriting a stored body directly.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from project_memory_mcp import validation
from project_memory_mcp.sqlite_store import SqliteMemoryStore


def make_memory(memory_id, labels, related=None):
    return {
        "schema_version": 1,
        "id": memory_id,
        "status": "active",
        "description": f"Reusable project memory for {memory_id}.",
        "tags": [memory_id],
        "labels": labels,
        "scope": {"project": "example-project", "area": "tests", "files": [], "applies_to": []},
        "triggers": [f"trigger {memory_id}"],
        "remembered_facts": [f"fact {memory_id}"],
        "solution_pattern": [],
        "pitfalls": [],
        "evidence": {"created_from_task": "unit test", "last_validated": "2026-07-07"},
        "relationships": {"related": related or [], "supersedes": [], "superseded_by": []},
    }


class StoreTestCase(unittest.TestCase):
    LABELS = {
        "area:alpha": "Alpha area.", "area:beta": "Beta area.",
        "context:runtime": "Runtime.", "kind:bug": "Bug.", "kind:workflow": "Workflow.",
    }

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.store = SqliteMemoryStore(Path(self.tempdir.name) / "memory.db", "demo")
        self.addCleanup(self.store.close)
        for label, description in self.LABELS.items():
            self.store.add_label(label, description)

    def corrupt(self, memory):
        """Replace a stored body without going through validation."""
        self.store.connection.execute(
            "UPDATE memories SET body=? WHERE project_id=? AND slug=?",
            (json.dumps(memory), self.store.project, memory["id"]),
        )
        self.store.connection.commit()

    def seed_memories(self):
        self.store.create_memory(make_memory("alpha-bug", ["area:alpha", "context:runtime", "kind:bug"]))
        self.store.create_memory(make_memory("beta-workflow", ["area:beta", "kind:workflow"]))
        self.store.create_memory(make_memory("alpha-workflow", ["area:alpha", "kind:workflow"]))


class ValidateStoreTests(StoreTestCase):
    def test_clean_store_has_no_errors(self):
        self.seed_memories()

        self.assertEqual([], self.store.validate_store())

    def test_missing_required_field_is_reported(self):
        memory = make_memory("alpha-bug", ["area:alpha", "kind:bug"])
        self.store.create_memory(memory)
        del memory["triggers"]
        self.corrupt(memory)

        errors = self.store.validate_store()

        self.assertTrue(any("missing required field 'triggers'" in error for error in errors))

    def test_unknown_top_level_field_is_reported(self):
        memory = make_memory("alpha-bug", ["area:alpha", "kind:bug"])
        self.store.create_memory(memory)
        memory["extra_field"] = "unexpected"
        self.corrupt(memory)

        errors = self.store.validate_store()

        self.assertTrue(any("unknown field 'extra_field'" in error for error in errors))

    def test_unregistered_label_is_reported(self):
        memory = make_memory("alpha-bug", ["area:alpha", "kind:bug"])
        self.store.create_memory(memory)
        memory["labels"] = ["area:alpha", "area:unregistered"]
        self.corrupt(memory)

        errors = self.store.validate_store()

        self.assertTrue(any("area:unregistered" in error for error in errors))

    def test_non_bidirectional_related_is_reported(self):
        self.store.create_memory(make_memory("beta-workflow", ["area:beta", "kind:workflow"]))
        memory = make_memory("alpha-bug", ["area:alpha", "kind:bug"])
        self.store.create_memory(memory)
        memory["relationships"]["related"] = [
            {"id": "beta-workflow", "reason": "shared test relationship"}
        ]
        self.corrupt(memory)

        errors = self.store.validate_store()

        self.assertTrue(any("not mirrored back" in error for error in errors))

    def test_related_to_a_memory_that_does_not_exist_is_reported(self):
        memory = make_memory("alpha-bug", ["area:alpha", "kind:bug"])
        self.store.create_memory(memory)
        memory["relationships"]["related"] = [{"id": "ghost", "reason": "gone"}]
        self.corrupt(memory)

        errors = self.store.validate_store()

        self.assertTrue(any("unknown memory: ghost" in error for error in errors))

    def test_missing_status_and_description_do_not_crash_validation(self):
        memory = make_memory("alpha-bug", ["area:alpha", "kind:bug"])
        self.store.create_memory(memory)
        del memory["status"]
        del memory["description"]
        self.corrupt(memory)

        errors = self.store.validate_store()

        self.assertTrue(any("missing required field 'status'" in error for error in errors))
        self.assertTrue(any("missing required field 'description'" in error for error in errors))

    def test_invalid_status_and_date_are_reported(self):
        memory = make_memory("alpha-bug", ["area:alpha", "kind:bug"])
        self.store.create_memory(memory)
        memory["status"] = "archived"
        memory["evidence"]["last_validated"] = "July 2026"
        self.corrupt(memory)

        errors = self.store.validate_store()

        self.assertTrue(any("status 'archived'" in error for error in errors))
        self.assertTrue(any("last_validated must be YYYY-MM-DD" in error for error in errors))


class DocumentRuleTests(unittest.TestCase):
    """Tags accept code identifiers; ids and labels stay strict slugs."""

    def check(self, memory):
        return validation.validate_memory(memory, None, "test")

    def test_code_identifier_tags_are_accepted(self):
        entry = make_memory("a", ["area:x"])
        entry["tags"] = ["bReplicates", "C4459", "from_pydata", "localUVDensities", "kebab-case"]
        self.assertEqual([], self.check(entry))

    def test_duplicate_tags_are_rejected(self):
        entry = make_memory("a", ["area:x"])
        entry["tags"] = ["dupe", "dupe"]
        self.assertTrue(self.check(entry))

    def test_non_string_tags_are_rejected(self):
        entry = make_memory("a", ["area:x"])
        entry["tags"] = [42]
        self.assertTrue(self.check(entry))

    def test_ids_and_labels_remain_strict(self):
        self.assertTrue(self.check(make_memory("NotASlug", ["area:x"])))
        self.assertTrue(self.check(make_memory("a", ["NotALabel"])))


class LabelPrefilterTests(unittest.TestCase):
    """The index prefilter must never change which memories match.

    `search_memories` used to read and JSON-parse every memory in the project to
    apply a label filter in Python. It now narrows on the label index first -
    but only when the expression allows it, and getting that wrong loses real
    matches silently, which is far worse than the scan it replaces.

    So this is differential: every query is answered twice, once through the
    store and once by brute force over every memory, and the two must agree.
    The `NOT` cases are the ones that matter. A memory satisfying `NOT area:x`
    may carry none of the labels the query mentions, so narrowing to the
    mentioned set would drop it - which is why `used_labels` is not a sound
    prefilter and `narrowing_labels` exists.
    """

    LABELS = ("area:x", "area:y", "area:z", "kind:bug")

    QUERIES = (
        None, "", "area:x", "area:y", "kind:bug",
        "area:x AND area:y", "area:x OR area:y", "area:x AND kind:bug",
        "NOT area:x", "NOT kind:bug", "area:x AND NOT kind:bug",
        "NOT area:x AND NOT area:y", "(area:x OR area:y) AND NOT kind:bug",
        "(area:x OR area:y) AND kind:bug", "NOT (area:x OR area:y)",
        {"all": ["area:x"]}, {"any": ["area:x", "area:y"]},
        {"not": ["kind:bug"]}, {"all": ["area:x"], "not": ["kind:bug"]},
        {"all": ["area:x", "area:y"]}, {"any": ["area:z"], "not": ["area:x"]},
    )

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SqliteMemoryStore(Path(self.tmp.name) / "memory.db", "demo")
        self.addCleanup(self.store.close)
        for label in self.LABELS:
            self.store.add_label(label, "description for " + label)
        # Every non-empty combination of the four labels, so each query above
        # has memories on both sides of it.
        import itertools

        self.bodies = []
        for size in range(1, len(self.LABELS) + 1):
            for combo in itertools.combinations(self.LABELS, size):
                slug = "note-" + "-".join(l.split(":")[1] for l in combo)
                body = make_memory(slug, list(combo))
                self.store.create_memory(body)
                self.bodies.append(body)

    def brute_force(self, query):
        expression = validation.LabelExpression(query, set(self.LABELS))
        return sorted(b["id"] for b in self.bodies if expression.matches(b["labels"]))

    def test_the_prefilter_agrees_with_a_full_scan_on_every_query(self):
        for query in self.QUERIES:
            with self.subTest(query=query):
                found = sorted(m["id"] for m in
                               self.store.search_memories(label_query=query)["memories"])
                self.assertEqual(self.brute_force(query), found)

    def test_a_negated_query_narrows_nothing(self):
        # Stated on its own so the reason survives: this is the case that makes
        # the mentioned-label set unsound, and a prefilter applied here would
        # drop every memory that carries no mentioned label at all.
        expression = validation.LabelExpression("NOT area:x", set(self.LABELS))
        self.assertIsNone(expression.narrowing_labels)
        self.assertEqual({"area:x"}, expression.used_labels)

    def test_a_positive_query_narrows_to_what_it_mentions(self):
        expression = validation.LabelExpression("area:x OR area:y", set(self.LABELS))
        self.assertEqual({"area:x", "area:y"}, expression.narrowing_labels)

    def test_a_dict_query_narrows_on_all_before_any(self):
        # `all` is the stronger claim - every match carries all of them - so it
        # is the better set to fetch on when both are present.
        expression = validation.LabelExpression(
            {"all": ["area:x"], "any": ["area:y", "area:z"]}, set(self.LABELS))
        self.assertEqual({"area:x"}, expression.narrowing_labels)


if __name__ == "__main__":
    unittest.main()
