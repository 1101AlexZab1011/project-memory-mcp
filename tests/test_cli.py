import tempfile
import unittest
from pathlib import Path

from project_memory_mcp.cli import main
from project_memory_mcp.sqlite_store import SqliteMemoryStore

from test_validation import make_memory


class CliCase(unittest.TestCase):
    """Fixture only, no tests of its own.

    The classes below used to inherit `CliTests` to reuse this setUp, which also
    inherited its seven tests - so they ran three times over and the file
    reported 26 tests when it had 12. Nothing was wrong with any of them; the
    count just meant less than it looked like, which is the failure this whole
    pass keeps running into.
    """

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.db = self.root / "memory.db"

    def open_store(self, project="demo"):
        store = SqliteMemoryStore(self.db, project, create=False)
        self.addCleanup(store.close)
        return store


class CliTests(CliCase):
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


class RemovedCommandTests(CliCase):
    """`migrate` is gone, and nothing points at it any more.

    It imported the pre-0.4.0 `.project-memory` layout and had raised TypeError
    on every call since schema v2 required a uuid in `_write`. Untested, and
    recommended by the server's own startup banner. The format is one the README
    calls dead and the recall skill tells agents not to open, so keeping a
    broken importer for it was a claim rather than a migration path.
    """

    def test_migrate_is_not_a_command(self):
        with self.assertRaises(SystemExit) as raised:
            main(["migrate", "--from", str(self.root), "--project", "demo",
                  "--database", str(self.db)])
        self.assertEqual(2, raised.exception.code, "argparse still accepts `migrate`")

    def test_nothing_in_the_package_still_offers_to_import_a_file_store(self):
        # Both halves went, not just the subcommand: `migrate_from_files` was a
        # public name on the store and would have stayed importable and broken.
        import project_memory_mcp.sqlite_store as store_module

        self.assertFalse(hasattr(store_module, "migrate_from_files"))

    def test_the_startup_banner_names_a_command_that_exists(self):
        # The banner told anyone with an empty database to run `migrate`, which
        # could not work. Whatever it names has to be a real subcommand - that
        # is the property, not the particular word.
        import re
        from pathlib import Path as _Path

        from project_memory_mcp import http_server

        source = _Path(http_server.__file__).read_text(encoding="utf-8")
        banner = [line for line in source.splitlines() if "(none - create one with" in line]
        self.assertEqual(1, len(banner), "the empty-database banner moved or was duplicated")
        named = re.search(r"`(\w+)`", banner[0]).group(1)

        with self.assertRaises(SystemExit) as raised:
            main([named, "--help"])
        self.assertEqual(0, raised.exception.code,
                         f"the banner sends people to `{named}`, which argparse does not accept")


class DestructiveConfirmationTests(CliCase):
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


if __name__ == "__main__":
    unittest.main()
