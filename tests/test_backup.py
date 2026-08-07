"""Backup: byte-exact snapshots, portable exports, and round-trip restore."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from project_memory_mcp import backup
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


class BackupFailureIsLoudTests(unittest.TestCase):
    """A backup nobody knows is broken is worse than no backup.

    `BackupScheduler.last_error` was written on every failure and read by
    nothing, so a snapshot failing every hour looked exactly like one succeeding
    every hour. This class exists because losing the store is the failure that
    matters most - and the same risk, plus the belief that it is covered, is a
    worse position than the risk alone.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "memory.db"
        store = SqliteMemoryStore(self.db, "demo")
        store.add_label("area:x", "x")
        store.create_memory(memory("cache-race", CACHE, ["area:x"]))
        store.close()

    def scheduler(self, database=None, destination=None):
        from project_memory_mcp.backup import BackupScheduler

        return BackupScheduler(database or self.db,
                               destination or Path(self.tmp.name) / "snapshots",
                               interval_seconds=3600, keep=3)

    def run_once(self, scheduler):
        import contextlib
        import io

        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            error = scheduler.snapshot_once()
        return error, captured.getvalue()

    def test_a_failing_snapshot_says_so_on_stderr(self):
        error, logged = self.run_once(self.scheduler(database=Path(self.tmp.name) / "gone.db"))
        self.assertIsNotNone(error, "a snapshot of a missing database reported success")
        self.assertIn("BACKUP FAILED", logged)

    def test_a_working_snapshot_stays_quiet_and_writes_a_file(self):
        # The counterweight: printing on every run would make the message above
        # meaningless, and a scheduler that only ever failed would pass it.
        scheduler = self.scheduler()
        error, logged = self.run_once(scheduler)
        self.assertIsNone(error)
        self.assertEqual("", logged)
        self.assertEqual(1, len(list((Path(self.tmp.name) / "snapshots").glob("*.db"))))

    def test_a_failure_does_not_stop_the_next_attempt(self):
        scheduler = self.scheduler(database=Path(self.tmp.name) / "gone.db")
        self.run_once(scheduler)
        scheduler.database = self.db  # whatever was wrong is fixed
        self.assertIsNone(self.run_once(scheduler)[0])
        self.assertIsNone(scheduler.last_error, "a stale error survived a successful run")


class ExportCarriesStandingTests(unittest.TestCase):
    """An export has to carry where a memory stands, not only what it says.

    Tier, archive state and visibility live in columns rather than in the memory
    body, so exporting the bodies alone dropped them. A restore therefore
    un-archived everything the audit had retired, reset every tier to 1, and
    turned every public memory private - undoing months of curation, and one
    judgment about audience that no statistic can make again.

    Found by the federation simulation, three simulated years in, when memories
    the audit had archived came back alive through a backup round trip. Every
    unit test here passed throughout.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "memory.db"
        store = SqliteMemoryStore(self.db, "demo")
        store.add_label("area:x", "x")
        store.create_memory(memory("proven-note", "A lesson that earned its way to tier three",
                                   ["area:x"]), visibility="public")
        store.create_memory(memory("retired-note", "A lesson the audit archived for being quiet",
                                   ["area:x"]))
        store.archive_memory("retired-note")
        store.connection.execute(
            "UPDATE memories SET tier=3, tier_since='2024-01-01T00:00:00Z', tier_since_query=400 "
            "WHERE slug='proven-note'")
        store.connection.commit()
        store.close()

    def round_trip(self):
        export = self.root / "export.json"
        backup.export_json(self.db, export)
        backup.import_json(self.root / "restored.db", export)
        restored = SqliteMemoryStore(self.root / "restored.db", "demo", create=False)
        self.addCleanup(restored.close)
        return restored

    def standing(self, store, slug):
        row = store.connection.execute(
            "SELECT tier, tier_since_query, archived_at, visibility FROM memories WHERE slug=?",
            (slug,)).fetchone()
        return (row["tier"], row["tier_since_query"],
                row["archived_at"] is not None, row["visibility"])

    def test_a_tier_survives_the_round_trip(self):
        self.assertEqual((3, 400, False, "public"), self.standing(self.round_trip(), "proven-note"))

    def test_an_archived_memory_stays_archived(self):
        restored = self.round_trip()
        self.assertEqual((1, 0, True, "private"), self.standing(restored, "retired-note"))
        found = restored.recall(query="", limit=20, record=False)
        self.assertEqual([], [m for m in found["memories"] if m["id"] == "retired-note"],
                         "a restored archive was back in the ranked pool")

    def test_a_public_memory_does_not_become_private(self):
        # Visibility is a judgment about audience, made by a person. Resetting
        # it silently means the next promotion is refused for a reason nobody
        # chose.
        restored = self.round_trip()
        self.assertEqual("public", self.standing(restored, "proven-note")[3])

    def test_the_content_still_arrives_too(self):
        # The counterweight: carrying standing must not cost the memories.
        restored = self.round_trip()
        self.assertEqual(["proven-note", "retired-note"],
                         sorted(r[0] for r in restored.connection.execute(
                             "SELECT slug FROM memories ORDER BY slug")))
        self.assertEqual([], restored.validate_store())
        self.assertIn("tier three", restored.get_memory("proven-note")["description"])

    def test_an_older_export_without_standing_still_restores(self):
        # Format 1 and 2 carry no `state`. Those memories land at tier 1, active
        # and private - the old behaviour, and the right fallback when standing
        # is unrecorded rather than known to be default.
        export = self.root / "export.json"
        backup.export_json(self.db, export)
        payload = json.loads(export.read_text(encoding="utf-8"))
        payload["format_version"] = 2
        for data in payload["projects"].values():
            data.pop("state", None)
        export.write_text(json.dumps(payload), encoding="utf-8")

        backup.import_json(self.root / "old.db", export)
        restored = SqliteMemoryStore(self.root / "old.db", "demo", create=False)
        self.addCleanup(restored.close)
        self.assertEqual((1, 0, False, "private"), self.standing(restored, "proven-note"))
        self.assertEqual(2, restored.count())


if __name__ == "__main__":
    unittest.main()
