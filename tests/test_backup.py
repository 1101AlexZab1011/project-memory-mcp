"""Backup: byte-exact snapshots, portable exports, and round-trip restore."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from project_memory_mcp.backup import (
    BackupScheduler,
    export_json,
    import_json,
    prune_snapshots,
    snapshot_database,
)
from project_memory_mcp.sqlite_store import SqliteMemoryStore


def memory(memory_id, description, labels):
    return {
        "schema_version": 1, "id": memory_id, "status": "active", "description": description,
        "tags": [], "labels": labels,
        "scope": {"project": "p", "area": "a", "files": [], "applies_to": []},
        "triggers": ["trigger for " + memory_id], "remembered_facts": [description],
        "solution_pattern": [], "pitfalls": [],
        "evidence": {"created_from_task": "t", "last_validated": "2026-01-01"},
        "relationships": {"related": [], "supersedes": [], "superseded_by": []},
    }


CACHE = "Session cache invalidation races the auth refresh."
SHADER = "Shader compilation stalls on a cold start."


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "memory.db"
        store = SqliteMemoryStore(self.db, "demo")
        store.add_label("area:x", "x")
        store.create_memory(memory("cache-race", CACHE, ["area:x"]))
        store.create_memory(memory("shader-stall", SHADER, ["area:x"]))
        store.recall(query="cache invalidation", limit=1, full_count=0)
        store.record_use(["cache-race"])
        store.close()

    def test_snapshot_is_a_usable_database(self):
        target = snapshot_database(self.db, self.root / "backups")
        self.assertTrue(target.is_file())
        restored = SqliteMemoryStore(target, "demo", create=False)
        self.addCleanup(restored.close)
        self.assertEqual(2, restored.count())
        self.assertEqual(CACHE, restored.get_memory("cache-race")["description"])

    def test_snapshot_preserves_usage_counters(self):
        # The distinguishing property against a JSON export: snapshots restore
        # everything, including telemetry and revision history.
        target = snapshot_database(self.db, self.root / "backups")
        restored = SqliteMemoryStore(target, "demo", create=False)
        self.addCleanup(restored.close)
        self.assertEqual(1, restored.load_usage()["memories"]["cache-race"]["applied"])

    def test_snapshots_rotate(self):
        out = self.root / "backups"
        for index in range(5):
            path = snapshot_database(self.db, out, keep=3)
            # Timestamps have second resolution; make the names distinct.
            path.rename(out / f"memory-2026010{index}T000000Z.db")
        prune_snapshots(out, keep=3)
        remaining = sorted(p.name for p in out.glob("memory-*.db"))
        self.assertEqual(3, len(remaining))
        self.assertEqual("memory-20260104T000000Z.db", remaining[-1])  # newest kept

    def test_snapshot_of_a_missing_database_fails_clearly(self):
        with self.assertRaises(FileNotFoundError):
            snapshot_database(self.root / "nope.db", self.root / "backups")

    def test_export_is_readable_json(self):
        out = self.root / "export.json"
        result = export_json(self.db, out)
        self.assertEqual({"demo": 2}, result["projects"])
        payload = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual("project-memory-export", payload["format"])
        ids = [m["id"] for m in payload["projects"]["demo"]["memories"]]
        self.assertEqual(["cache-race", "shader-stall"], ids)
        self.assertIn("area:x", payload["projects"]["demo"]["labels"])

    def test_export_can_select_one_project(self):
        other = SqliteMemoryStore(self.db, "other")
        other.add_label("area:x", "x")
        other.create_memory(memory("only-here", "A memory in the other project entirely.", ["area:x"]))
        other.close()
        result = export_json(self.db, self.root / "one.json", project="other")
        self.assertEqual({"other": 1}, result["projects"])

    def test_export_rejects_an_unknown_project(self):
        with self.assertRaises(ValueError):
            export_json(self.db, self.root / "x.json", project="nope")

    def test_export_restores_into_an_empty_database(self):
        export_json(self.db, self.root / "export.json")
        fresh = self.root / "fresh.db"
        result = import_json(fresh, self.root / "export.json")
        self.assertEqual({"demo": 2}, result["projects"])
        restored = SqliteMemoryStore(fresh, "demo", create=False)
        self.addCleanup(restored.close)
        self.assertEqual(2, restored.count())
        self.assertEqual(CACHE, restored.get_memory("cache-race")["description"])
        self.assertEqual([], restored.validate_store())
        self.assertEqual(1, restored.load_usage()["memories"]["cache-race"]["applied"])

    def test_restored_store_is_searchable(self):
        # The FTS index is rebuilt on write, so a restore must be queryable
        # rather than merely present.
        export_json(self.db, self.root / "export.json")
        fresh = self.root / "fresh.db"
        import_json(fresh, self.root / "export.json")
        restored = SqliteMemoryStore(fresh, "demo", create=False)
        self.addCleanup(restored.close)
        hits = restored.recall(query="cache invalidation", limit=1, full_count=0)["memories"]
        self.assertEqual(["cache-race"], [m["id"] for m in hits])

    def test_import_rejects_a_foreign_file(self):
        stray = self.root / "stray.json"
        stray.write_text(json.dumps({"format": "something-else"}), encoding="utf-8")
        with self.assertRaises(ValueError):
            import_json(self.root / "x.db", stray)

    def test_scheduler_enforces_a_minimum_interval(self):
        scheduler = BackupScheduler(self.db, self.root / "b", interval_seconds=1, keep=2)
        self.assertEqual(60, scheduler.interval)

    def test_scheduler_stops_cleanly_without_snapshotting(self):
        # It waits before its first snapshot, so a short-lived server does not
        # duplicate whatever the previous run already wrote.
        out = self.root / "b"
        scheduler = BackupScheduler(self.db, out, interval_seconds=3600, keep=2)
        scheduler.start()
        scheduler.stop()
        self.assertFalse(out.exists())
