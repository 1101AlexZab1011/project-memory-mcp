"""Zero-touch setup: a repo directory to a working local store, no network.

The rule this file exists to protect: setup must never damage a project it is
run inside. It adds; it does not replace.
"""

from __future__ import annotations

import json
import tempfile
import tomllib
import unittest
from pathlib import Path

from project_memory_mcp.cli import _slugify, main
from project_memory_mcp.sqlite_store import SqliteMemoryStore

EXISTING_MCP = {
    "mcpServers": {
        "unreal-editor": {"type": "http", "url": "http://127.0.0.1:8000/mcp"},
    }
}


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "My Game Project"
        self.root.mkdir()
        self.db = Path(self.tmp.name) / "memory.db"

    def run_setup(self, *extra):
        return main(["setup", "--root", str(self.root), "--database", str(self.db), *extra])

    def test_one_command_produces_a_working_store(self):
        self.assertEqual(0, self.run_setup())
        store = SqliteMemoryStore(self.db, "my-game-project", create=False)
        self.addCleanup(store.close)
        self.assertTrue(store.list_labels()["labels"])
        self.assertEqual([], store.validate_store())

    def test_the_project_id_comes_from_the_directory_name(self):
        self.run_setup()
        store = SqliteMemoryStore(self.db, "my-game-project", create=False)
        self.addCleanup(store.close)
        self.assertEqual(["my-game-project"], SqliteMemoryStore.list_projects(store.connection))

    def test_skills_are_installed(self):
        self.run_setup()
        for skill in ("project-memory-recall", "project-memory-remember", "project-memory-forget"):
            self.assertTrue((self.root / ".claude" / "skills" / skill / "SKILL.md").is_file(), skill)

    def test_client_config_points_at_the_local_database(self):
        self.run_setup()
        entry = json.loads((self.root / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["project-memory"]
        self.assertEqual("stdio", entry["type"])
        self.assertIn(str(self.db), entry["args"])
        self.assertIn("my-game-project", entry["args"])

    def test_an_existing_mcp_server_is_not_clobbered(self):
        # Projects routinely configure several MCP servers. Replacing the file
        # to add one would silently delete the others.
        (self.root / ".mcp.json").write_text(json.dumps(EXISTING_MCP), encoding="utf-8")
        self.run_setup()
        servers = json.loads((self.root / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
        self.assertIn("unreal-editor", servers)
        self.assertIn("project-memory", servers)

    def test_a_malformed_mcp_json_is_left_alone_rather_than_overwritten(self):
        (self.root / ".mcp.json").write_text("{ not json", encoding="utf-8")
        self.assertEqual(0, self.run_setup())
        self.assertEqual("{ not json", (self.root / ".mcp.json").read_text(encoding="utf-8"))

    def test_codex_config_is_valid_toml_with_windows_paths(self):
        # A backslash path inside a TOML basic string is a run of invalid
        # escapes, and the whole file fails to parse.
        self.run_setup("--codex")
        text = (self.root / ".codex" / "config.toml").read_text(encoding="utf-8")
        parsed = tomllib.loads(text)
        entry = parsed["mcp_servers"]["project-memory"]
        self.assertEqual("project-memory-mcp", entry["command"])
        self.assertIn(str(self.db), entry["args"])

    def test_an_existing_codex_config_is_appended_to(self):
        config = self.root / ".codex" / "config.toml"
        config.parent.mkdir()
        config.write_text('[mcp_servers.unreal-mcp]\nurl = "http://127.0.0.1:8000/mcp"\n', encoding="utf-8")
        self.run_setup("--codex")
        parsed = tomllib.loads(config.read_text(encoding="utf-8"))
        self.assertIn("unreal-mcp", parsed["mcp_servers"])
        self.assertIn("project-memory", parsed["mcp_servers"])

    def test_running_setup_twice_changes_nothing(self):
        self.run_setup("--codex")
        first = (self.root / ".mcp.json").read_text(), (self.root / ".codex" / "config.toml").read_text()
        self.assertEqual(0, self.run_setup("--codex"))
        self.assertEqual(first, ((self.root / ".mcp.json").read_text(),
                                 (self.root / ".codex" / "config.toml").read_text()))

    def test_setup_needs_no_network_and_no_token(self):
        # The whole point: local-only is the default, not a fallback.
        self.run_setup()
        entry = json.loads((self.root / ".mcp.json").read_text())["mcpServers"]["project-memory"]
        self.assertNotIn("url", entry)
        self.assertNotIn("headers", entry)


class SlugTests(unittest.TestCase):
    def test_directory_names_become_valid_project_ids(self):
        from project_memory_mcp.validation import ID_RE

        for name, expected in (
            ("My Game Project", "my-game-project"),
            ("tales-of-arvendale", "tales-of-arvendale"),
            ("Some_Repo.v2", "some-repo-v2"),
            ("  spaced  ", "spaced"),
            ("!!!", "project"),
        ):
            with self.subTest(name=name):
                self.assertEqual(expected, _slugify(name))
                self.assertRegex(_slugify(name), ID_RE)


if __name__ == "__main__":
    unittest.main()
