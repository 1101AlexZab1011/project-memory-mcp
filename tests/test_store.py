import tempfile
import unittest
from pathlib import Path

from project_memory_mcp.store import MemoryStore, StoreError


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


LABELS_JSON = """{
  "schema_version": 1,
  "description": "test labels",
  "labels": {
    "area:alpha": {"description": "Alpha area."},
    "area:beta": {"description": "Beta area."},
    "context:runtime": {"description": "Runtime."},
    "kind:bug": {"description": "Bug."},
    "kind:workflow": {"description": "Workflow."}
  }
}
"""


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        memory_root = self.root / ".project-memory"
        (memory_root / "active").mkdir(parents=True)
        (memory_root / "labels.json").write_text(LABELS_JSON, encoding="utf-8")
        self.store = MemoryStore(self.root)

    def tearDown(self):
        self.tempdir.cleanup()

    def write_memory(self, memory):
        path = self.root / ".project-memory" / "active" / f"{memory['id']}.json"
        self.store._write_json(path, memory)

    def seed_memories(self):
        self.write_memory(make_memory("alpha-bug", ["area:alpha", "context:runtime", "kind:bug"]))
        self.write_memory(make_memory("beta-workflow", ["area:beta", "kind:workflow"]))
        self.write_memory(make_memory("alpha-workflow", ["area:alpha", "kind:workflow"]))
        self.store.regenerate_index()


class SearchTests(StoreTestCase):
    def test_label_query_supports_and_or_not(self):
        self.seed_memories()

        result = self.store.search_memories(
            label_query="area:alpha AND (kind:bug OR kind:workflow) AND NOT area:beta",
            status_filter="all",
        )

        self.assertEqual(["alpha-bug", "alpha-workflow"], [entry["id"] for entry in result["memories"]])

    def test_unknown_label_is_rejected(self):
        self.seed_memories()

        with self.assertRaises(StoreError):
            self.store.search_memories(label_query={"all": ["area:missing"]})

    def test_text_query_filters_within_label_cluster(self):
        self.seed_memories()

        result = self.store.search_memories(label_query={"all": ["area:alpha"]}, text_query="alpha-bug")

        self.assertEqual(["alpha-bug"], [entry["id"] for entry in result["memories"]])


class MutationTests(StoreTestCase):
    def test_create_regenerates_index_with_labels(self):
        memory = make_memory("new-memory", ["area:alpha", "kind:bug"])

        self.store.create_memory(memory)
        index = self.store._read_json(self.root / ".project-memory" / "INDEX.json")

        self.assertEqual(["area:alpha", "kind:bug"], index["memories"][0]["labels"])

    def test_create_rejects_invalid_memory_and_rolls_back(self):
        self.seed_memories()
        bad = make_memory("bad-memory", ["area:alpha"])
        bad["description"] = "too short"

        with self.assertRaises(StoreError):
            self.store.create_memory(bad)

        self.assertFalse((self.root / ".project-memory" / "active" / "bad-memory.json").exists())
        self.assertEqual([], self.store.validate_store())

    def test_create_synchronizes_bidirectional_relationships(self):
        self.seed_memories()
        memory = make_memory(
            "gamma-bug",
            ["area:alpha", "kind:bug"],
            [{"id": "alpha-bug", "reason": "same subsystem failure mode"}],
        )

        self.store.create_memory(memory)
        alpha = self.store.get_memory("alpha-bug")

        self.assertEqual(
            [{"id": "gamma-bug", "reason": "same subsystem failure mode"}],
            alpha["relationships"]["related"],
        )
        self.assertEqual([], self.store.validate_store())

    def test_update_cannot_change_id(self):
        self.seed_memories()

        with self.assertRaises(StoreError):
            self.store.update_memory("alpha-bug", {"id": "other-id"})

    def test_update_patch_changes_status(self):
        self.seed_memories()

        self.store.update_memory("alpha-bug", {"status": "stale"})

        self.assertEqual("stale", self.store.get_memory("alpha-bug")["status"])
        self.assertEqual([], self.store.validate_store())

    def test_add_label_and_reject_duplicate(self):
        self.seed_memories()

        self.store.add_label("signal:flaky-test", "Recurring flaky test symptom.")
        labels = self.store.list_labels()["labels"]

        self.assertIn("signal:flaky-test", labels)
        with self.assertRaises(StoreError):
            self.store.add_label("signal:flaky-test", "Duplicate.")

    def test_delete_requires_confirmation_and_cleans_relationships(self):
        self.write_memory(
            make_memory(
                "alpha-bug",
                ["area:alpha", "kind:bug"],
                [{"id": "beta-workflow", "reason": "shared test relationship"}],
            )
        )
        self.write_memory(
            make_memory(
                "beta-workflow",
                ["area:beta", "kind:workflow"],
                [{"id": "alpha-bug", "reason": "shared test relationship"}],
            )
        )
        self.store.regenerate_index()

        with self.assertRaises(StoreError):
            self.store.delete_memory("beta-workflow", "wrong-id")

        self.store.delete_memory("beta-workflow", "beta-workflow")
        alpha = self.store.get_memory("alpha-bug")

        self.assertFalse((self.root / ".project-memory" / "active" / "beta-workflow.json").exists())
        self.assertEqual([], alpha["relationships"]["related"])
        self.assertEqual([], self.store.validate_store())


class NeighborhoodTests(StoreTestCase):
    def test_neighborhood_depth_is_bounded(self):
        self.write_memory(
            make_memory(
                "alpha-bug",
                ["area:alpha", "kind:bug"],
                [{"id": "beta-workflow", "reason": "shared test relationship"}],
            )
        )
        self.write_memory(
            make_memory(
                "beta-workflow",
                ["area:beta", "kind:workflow"],
                [
                    {"id": "alpha-bug", "reason": "shared test relationship"},
                    {"id": "alpha-workflow", "reason": "second hop relationship"},
                ],
            )
        )
        self.write_memory(
            make_memory(
                "alpha-workflow",
                ["area:alpha", "kind:workflow"],
                [{"id": "beta-workflow", "reason": "second hop relationship"}],
            )
        )
        self.store.regenerate_index()

        depth_one = self.store.get_memory_neighborhood("alpha-bug", depth=1)
        depth_two = self.store.get_memory_neighborhood("alpha-bug", depth=2)

        self.assertEqual({"alpha-bug", "beta-workflow"}, {node["id"] for node in depth_one["nodes"]})
        self.assertEqual({"alpha-bug", "beta-workflow", "alpha-workflow"}, {node["id"] for node in depth_two["nodes"]})


if __name__ == "__main__":
    unittest.main()
