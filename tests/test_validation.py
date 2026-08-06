import json
import unittest

from test_store import StoreTestCase, make_memory


class ValidateStoreTests(StoreTestCase):
    def test_clean_store_has_no_errors(self):
        self.seed_memories()

        self.assertEqual([], self.store.validate_store())

    def test_missing_required_field_is_reported(self):
        memory = make_memory("alpha-bug", ["area:alpha", "kind:bug"])
        del memory["triggers"]
        self.write_memory(memory)

        errors = self.store.validate_store()

        self.assertTrue(any("missing required field 'triggers'" in error for error in errors))

    def test_unknown_top_level_field_is_reported(self):
        memory = make_memory("alpha-bug", ["area:alpha", "kind:bug"])
        memory["extra_field"] = "unexpected"
        self.write_memory(memory)

        errors = self.store.validate_store()

        self.assertTrue(any("unknown field 'extra_field'" in error for error in errors))

    def test_unregistered_label_is_reported(self):
        memory = make_memory("alpha-bug", ["area:alpha", "kind:bug"])
        self.write_memory(memory)
        # Bypass mutation APIs to simulate a hand-edit introducing a rogue label.
        path = self.root / ".project-memory" / "active" / "alpha-bug.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["labels"] = ["area:alpha", "area:unregistered"]
        path.write_text(json.dumps(raw), encoding="utf-8")

        errors = self.store.validate_store()

        self.assertTrue(any("not declared in labels.json" in error for error in errors))

    def test_id_must_match_filename(self):
        memory = make_memory("alpha-bug", ["area:alpha", "kind:bug"])
        path = self.root / ".project-memory" / "active" / "wrong-name.json"
        self.store._write_json(path, memory)

        errors = self.store.validate_store()

        self.assertTrue(any("must match filename" in error for error in errors))

    def test_non_bidirectional_related_is_reported(self):
        self.write_memory(
            make_memory(
                "alpha-bug",
                ["area:alpha", "kind:bug"],
                [{"id": "beta-workflow", "reason": "shared test relationship"}],
            )
        )
        self.write_memory(make_memory("beta-workflow", ["area:beta", "kind:workflow"]))

        errors = self.store.validate_store()

        self.assertTrue(any("is not bidirectional" in error for error in errors))

    def test_unmirrored_supersedes_is_reported(self):
        old = make_memory("old-lesson", ["area:alpha", "kind:bug"])
        new = make_memory("new-lesson", ["area:alpha", "kind:bug"])
        new["relationships"]["supersedes"] = ["old-lesson"]
        self.write_memory(old)
        self.write_memory(new)

        errors = self.store.validate_store()

        self.assertTrue(any("is not mirrored by superseded_by" in error for error in errors))

    def test_a_hand_written_memory_file_needs_no_catalogue_step(self):
        self.seed_memories()
        self.write_memory(make_memory("uncatalogued", ["area:alpha", "kind:bug"]))

        self.assertEqual([], self.store.validate_store())
        self.assertIn("uncatalogued", self.store.load_memories())

    def test_missing_status_and_description_do_not_crash_validation(self):
        memory = make_memory("alpha-bug", ["area:alpha", "kind:bug"])
        del memory["status"]
        del memory["description"]
        self.write_memory(memory)

        errors = self.store.validate_store()

        self.assertTrue(any("missing required field 'status'" in error for error in errors))
        self.assertTrue(any("missing required field 'description'" in error for error in errors))

    def test_invalid_status_and_date_are_reported(self):
        memory = make_memory("alpha-bug", ["area:alpha", "kind:bug"])
        memory["status"] = "archived"
        memory["evidence"]["last_validated"] = "July 2026"
        self.write_memory(memory)

        errors = self.store.validate_store()

        self.assertTrue(any("status 'archived'" in error for error in errors))
        self.assertTrue(any("last_validated must be YYYY-MM-DD" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
