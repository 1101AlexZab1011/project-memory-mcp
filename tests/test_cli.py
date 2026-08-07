import tempfile
import unittest
from pathlib import Path

from project_memory_mcp.cli import main
from project_memory_mcp.sqlite_store import SqliteMemoryStore

from test_validation import make_memory


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.db = self.root / "memory.db"

    def open_store(self, project="demo"):
        store = SqliteMemoryStore(self.db, project, create=False)
        self.addCleanup(store.close)
        return store

    def test_init_creates_the_project_and_seeds_labels(self):
        exit_code = main(["init", "--database", str(self.db), "--project", "demo"])

        self.assertEqual(0, exit_code)
        store = self.open_store()
        self.assertEqual([], store.validate_store())
        self.assertTrue(store.list_labels()["labels"])

    def test_init_makes_the_project_visible_before_any_memory_exists(self):
        # Without this the project does not exist until its first write, so the
        # server cannot serve it and it is missing from the UI's project list.
        main(["init", "--database", str(self.db), "--project", "demo"])

        store = self.open_store()
        self.assertEqual(["demo"], SqliteMemoryStore.list_projects(store.connection))
        self.assertEqual(0, store.count())

    def test_init_keeps_existing_labels_without_force(self):
        main(["init", "--database", str(self.db), "--project", "demo"])
        store = self.open_store()
        before = store.list_labels()["labels"]["kind:bug"]["description"]

        main(["init", "--database", str(self.db), "--project", "demo"])

        self.assertEqual(before, self.open_store().list_labels()["labels"]["kind:bug"]["description"])

    def test_projects_in_one_database_are_initialized_independently(self):
        main(["init", "--database", str(self.db), "--project", "demo"])
        main(["init", "--database", str(self.db), "--project", "other"])

        store = self.open_store()
        self.assertEqual(["demo", "other"], SqliteMemoryStore.list_projects(store.connection))

    def test_seeded_labels_support_a_full_memory_lifecycle(self):
        main(["init", "--database", str(self.db), "--project", "demo"])
        store = self.open_store()
        store.create_memory(make_memory("first-lesson", ["kind:bug", "context:runtime"]))

        result = store.search_memories(label_query="kind:bug")

        self.assertEqual(["first-lesson"], [entry["id"] for entry in result["memories"]])
        self.assertEqual(0, main(["validate", "--database", str(self.db), "--project", "demo"]))

    def test_validate_reports_an_unknown_project_instead_of_creating_one(self):
        main(["init", "--database", str(self.db), "--project", "demo"])

        self.assertEqual(1, main(["validate", "--database", str(self.db), "--project", "nope"]))
        store = self.open_store()
        self.assertEqual(["demo"], SqliteMemoryStore.list_projects(store.connection))

    def test_install_skills_copies_all_skills(self):
        exit_code = main(["install-skills", "--root", str(self.root), "--claude", "--codex"])

        self.assertEqual(0, exit_code)
        for base in (".claude", ".agents"):
            for skill in ("project-memory-recall", "project-memory-remember", "project-memory-forget"):
                self.assertTrue((self.root / base / "skills" / skill / "SKILL.md").is_file(), f"{base}/{skill}")


if __name__ == "__main__":
    unittest.main()


class DestructiveConfirmationTests(CliTests):
    """`audit --apply` must not act without `--yes`.

    The one command in this tool that destroys work on its own. Everything else
    the CLI does is additive or reversible by re-running it; this archives
    memories, and with --delete-superseded it deletes them. Nothing tested the
    confirmation, so removing it would have gone unnoticed - and the failure is
    silent, because somebody previewing a report would simply have applied it.
    """

    def seed_something_due(self):
        main(["init", "--database", str(self.db), "--project", "demo"])
        store = self.open_store()
        label = sorted(store.list_labels()["labels"])[0]
        store.create_memory(make_memory("quiet-note", [label]))
        # Give it exposure so a gate can be reached, without waiting a month.
        for _ in range(6):
            store.recall("nothing that matches this at all", limit=1, full_count=0)
        return store

    def archived(self, store):
        return store.connection.execute(
            "SELECT archived_at FROM memories WHERE slug='quiet-note'").fetchone()["archived_at"]

    def test_apply_without_yes_changes_nothing_and_fails(self):
        store = self.seed_something_due()
        exit_code = main(["audit", "--database", str(self.db), "--project", "demo",
                          "--gate", "1:5:0", "--apply"])
        self.assertEqual(1, exit_code, "--apply without --yes reported success")
        self.assertIsNone(self.archived(store),
                          "--apply acted on the store without being confirmed")

    def test_apply_with_yes_does_act(self):
        # The negative above is only worth anything if applying works at all.
        store = self.seed_something_due()
        exit_code = main(["audit", "--database", str(self.db), "--project", "demo",
                          "--gate", "1:5:0", "--apply", "--yes"])
        self.assertEqual(0, exit_code)
        self.assertIsNotNone(self.archived(store), "--apply --yes did nothing")
