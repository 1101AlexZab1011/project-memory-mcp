import tempfile
import unittest
from pathlib import Path

from project_memory_mcp.cli import main
from project_memory_mcp.store import MemoryStore

from test_store import make_memory


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_init_scaffolds_a_valid_store(self):
        exit_code = main(["init", "--root", str(self.root)])

        self.assertEqual(0, exit_code)
        store_dir = self.root / ".project-memory"
        for name in ("labels.json", "memory.schema.json", "README.md", "INDEX.json"):
            self.assertTrue((store_dir / name).is_file(), name)
        self.assertTrue((store_dir / "active").is_dir())
        self.assertEqual([], MemoryStore(self.root).validate_store())

    def test_init_keeps_existing_files_without_force(self):
        main(["init", "--root", str(self.root)])
        labels_path = self.root / ".project-memory" / "labels.json"
        custom = labels_path.read_text(encoding="utf-8").replace("A recurring failure mode", "custom description")
        labels_path.write_text(custom, encoding="utf-8")

        main(["init", "--root", str(self.root)])

        self.assertIn("custom description", labels_path.read_text(encoding="utf-8"))

    def test_starter_labels_support_a_full_memory_lifecycle(self):
        main(["init", "--root", str(self.root)])
        store = MemoryStore(self.root)

        memory = make_memory("first-lesson", ["kind:bug", "context:runtime"])
        store.create_memory(memory)

        result = store.search_memories(label_query="kind:bug")
        self.assertEqual(["first-lesson"], [entry["id"] for entry in result["memories"]])
        self.assertEqual(0, main(["validate", "--root", str(self.root)]))

    def test_validate_fix_index_regenerates(self):
        main(["init", "--root", str(self.root)])
        store = MemoryStore(self.root)
        store.create_memory(make_memory("first-lesson", ["kind:bug"]))
        store.index_path.write_text('{"schema_version": 1, "memories": []}', encoding="utf-8")

        self.assertEqual(1, main(["validate", "--root", str(self.root)]))
        self.assertEqual(0, main(["validate", "--root", str(self.root), "--fix-index"]))
        self.assertEqual(0, main(["validate", "--root", str(self.root)]))

    def test_validate_fix_index_refuses_to_mask_a_broken_store(self):
        main(["init", "--root", str(self.root)])
        bad = make_memory("bad-memory", ["kind:bug"])
        bad["description"] = "short"
        MemoryStore(self.root)._write_json(
            self.root / ".project-memory" / "active" / "bad-memory.json", bad
        )

        self.assertEqual(1, main(["validate", "--root", str(self.root), "--fix-index"]))

    def test_install_skills_copies_all_skills(self):
        exit_code = main(["install-skills", "--root", str(self.root), "--claude", "--codex"])

        self.assertEqual(0, exit_code)
        for base in (".claude", ".agents"):
            for skill in ("project-memory-recall", "project-memory-remember", "project-memory-forget"):
                self.assertTrue((self.root / base / "skills" / skill / "SKILL.md").is_file(), f"{base}/{skill}")


if __name__ == "__main__":
    unittest.main()
