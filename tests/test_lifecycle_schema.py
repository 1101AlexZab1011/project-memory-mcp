"""Schema v2: uuid identity, per-replica counters, and the spread bitmap.

These are the parts the audit will read, so what they mean matters more than
that they store something. Each test states the meaning it is protecting.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from project_memory_mcp.sqlite_store import (
    SPREAD_WINDOW_DAYS,
    SqliteMemoryStore,
    _merge_spread,
    _touch_spread,
)

CACHE = "Session cache invalidation races the auth refresh."
SHADER = "Shader compilation stalls on a cold start on the build farm."


def memory(memory_id, description, labels, related=None):
    return {
        "schema_version": 1, "id": memory_id, "status": "active", "description": description,
        "tags": [], "labels": labels,
        "scope": {"project": "p", "area": "a", "files": [], "applies_to": []},
        "triggers": ["trigger for " + memory_id], "remembered_facts": [description],
        "solution_pattern": [], "pitfalls": [],
        "evidence": {"created_from_task": "t", "last_validated": "2026-01-01"},
        "relationships": {
            "related": [{"id": o, "reason": "They share a subsystem."} for o in (related or [])],
            "supersedes": [], "superseded_by": [],
        },
    }


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "memory.db"
        self.store = SqliteMemoryStore(self.db, "demo")
        self.addCleanup(self.store.close)
        for label in ("area:x", "area:z"):
            self.store.add_label(label, "description for " + label)


class IdentityTests(StoreCase):
    def test_the_slug_stays_the_identifier_callers_see(self):
        self.store.create_memory(memory("cache-race", CACHE, ["area:x"]))
        self.assertEqual("cache-race", self.store.get_memory("cache-race")["id"])
        self.assertEqual("cache-race", self.store.recall("cache invalidation")["memories"][0]["id"])

    def test_every_memory_gets_a_uuid_that_survives_an_update(self):
        self.store.create_memory(memory("cache-race", CACHE, ["area:x"]))
        before = self.store._uuid_for("cache-race")
        self.store.update_memory("cache-race", {"status": "stale"})
        self.assertEqual(before, self.store._uuid_for("cache-race"))
        self.assertEqual(36, len(before))

    def test_other_tables_join_on_the_uuid_not_the_slug(self):
        # This is the whole point: once two nurseries have each coined
        # `shader-compile-stall`, a slug can no longer identify a row.
        self.store.create_memory(memory("cache-race", CACHE, ["area:x"]))
        memory_uuid = self.store._uuid_for("cache-race")
        for table in ("labels", "memories_fts"):
            stored = self.store.connection.execute(
                f"SELECT memory_id FROM {table} WHERE project_id='demo'").fetchone()
            self.assertEqual(memory_uuid, stored["memory_id"], table)

    def test_relationships_are_edged_by_uuid_and_still_read_back_as_slugs(self):
        self.store.create_memory(memory("a", CACHE, ["area:x"]))
        self.store.create_memory(memory("b", SHADER, ["area:x"], related=["a"]))
        a, b = self.store._uuid_for("a"), self.store._uuid_for("b")
        edges = self.store.connection.execute(
            "SELECT src, dst FROM edges WHERE project_id='demo' AND kind='related'").fetchall()
        # Links are mirrored, so both directions exist - and both are uuids.
        self.assertEqual({(a, b), (b, a)}, {(row["src"], row["dst"]) for row in edges})
        neighbourhood = self.store.get_memory_neighborhood("b")
        self.assertEqual([("b", "a")], [(e["from"], e["to"]) for e in neighbourhood["edges"]])

    def test_a_duplicate_slug_is_still_rejected_while_there_is_one_writer(self):
        self.store.create_memory(memory("cache-race", CACHE, ["area:x"]))
        with self.assertRaises(Exception):
            self.store.create_memory(memory("cache-race", SHADER, ["area:x"]))

    def test_delete_leaves_no_orphan_rows_behind(self):
        # files and the FTS row used to survive a delete, so a deleted memory
        # kept matching queries and kept generating derived-edge candidates.
        entry = memory("cache-race", CACHE, ["area:x"])
        entry["scope"]["files"] = ["Source/Cache.cpp"]
        self.store.create_memory(entry)
        self.store.delete_memory("cache-race", "cache-race")
        for table in ("memories", "labels", "files", "usage", "memories_fts"):
            left = self.store.connection.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE project_id='demo'").fetchone()["n"]
            self.assertEqual(0, left, table)


class SurfacingSplitTests(StoreCase):
    def test_a_text_match_counts_as_direct(self):
        self.store.create_memory(memory("cache-race", CACHE, ["area:x"]))
        self.store.recall("cache invalidation")
        entry = self.store.load_usage()["memories"]["cache-race"]
        self.assertEqual(1, entry["surfaced"])
        self.assertEqual(1, entry["surfaced_direct"])

    def test_a_neighbour_pulled_in_by_the_graph_does_not_count_as_direct(self):
        # Otherwise a weak memory attached to a popular one inherits its
        # neighbour's traffic and never ages out of the store.
        self.store.create_memory(memory("cache-race", CACHE, ["area:x"]))
        self.store.create_memory(
            memory("unrelated-note", "Totally different subject matter entirely.", ["area:z"],
                   related=["cache-race"]))
        self.store.recall("cache invalidation")
        entry = self.store.load_usage()["memories"]["unrelated-note"]
        self.assertEqual(1, entry["surfaced"])
        self.assertEqual(0, entry["surfaced_direct"])

    def test_browsing_the_timeline_never_counts_as_direct(self):
        self.store.create_memory(memory("cache-race", CACHE, ["area:x"]))
        self.store.recall(order="recent", limit=5)
        entry = self.store.load_usage()["memories"]["cache-race"]
        self.assertEqual(1, entry["surfaced"])
        self.assertEqual(0, entry["surfaced_direct"])


class ReplicaCounterTests(StoreCase):
    def test_counters_from_two_replicas_add_up(self):
        self.store.create_memory(memory("cache-race", CACHE, ["area:x"]))
        self.store.recall("cache invalidation")
        self.store.connection.execute(
            "INSERT INTO usage(project_id, memory_id, replica_id, surfaced, surfaced_direct, applied) "
            "VALUES ('demo', ?, 'other-machine', 4, 3, 2)", (self.store._uuid_for("cache-race"),))
        self.store.connection.commit()
        entry = self.store.load_usage()["memories"]["cache-race"]
        self.assertEqual(5, entry["surfaced"])
        self.assertEqual(4, entry["surfaced_direct"])
        self.assertEqual(2, entry["applied"])

    def test_a_replica_only_ever_writes_its_own_row(self):
        self.store.create_memory(memory("cache-race", CACHE, ["area:x"]))
        memory_uuid = self.store._uuid_for("cache-race")
        self.store.connection.execute(
            "INSERT INTO usage(project_id, memory_id, replica_id, surfaced) "
            "VALUES ('demo', ?, 'other-machine', 7)", (memory_uuid,))
        self.store.connection.commit()
        self.store.recall("cache invalidation")
        other = self.store.connection.execute(
            "SELECT surfaced FROM usage WHERE replica_id='other-machine'").fetchone()
        self.assertEqual(7, other["surfaced"])

    def test_the_replica_id_is_stable_across_reopening(self):
        first = self.store.replica_id
        reopened = SqliteMemoryStore(self.db, "demo", create=False)
        self.addCleanup(reopened.close)
        self.assertEqual(first, reopened.replica_id)


class SpreadBitmapTests(unittest.TestCase):
    """Spread is distinct days, not a rate: bursts must not outrank durability."""

    def test_two_recalls_on_one_day_are_one_day_of_spread(self):
        bits, epoch = _touch_spread(0, None, 1000)
        bits, epoch = _touch_spread(bits, epoch, 1000)
        self.assertEqual(1, bin(bits & ((1 << 64) - 1)).count("1"))

    def test_recalls_on_different_days_accumulate(self):
        bits, epoch = _touch_spread(0, None, 1000)
        for day in (1001, 1005, 1030):
            bits, epoch = _touch_spread(bits, epoch, day)
        self.assertEqual(4, bin(bits & ((1 << 64) - 1)).count("1"))
        self.assertEqual(1030, epoch)

    def test_a_gap_longer_than_the_window_starts_over(self):
        bits, epoch = _touch_spread(0, None, 1000)
        bits, epoch = _touch_spread(bits, epoch, 1000 + SPREAD_WINDOW_DAYS + 5)
        self.assertEqual(1, bin(bits & ((1 << 64) - 1)).count("1"))

    def test_days_fall_out_of_the_window_as_it_rolls(self):
        bits, epoch = _touch_spread(0, None, 1000)
        bits, epoch = _touch_spread(bits, epoch, 1001)
        bits, epoch = _touch_spread(bits, epoch, 1000 + SPREAD_WINDOW_DAYS)
        # The first day is now older than the window; the second and third stay.
        self.assertEqual(2, bin(bits & ((1 << 64) - 1)).count("1"))

    def test_a_clock_that_ran_backwards_does_not_roll_the_window(self):
        bits, epoch = _touch_spread(0, None, 1000)
        bits, epoch = _touch_spread(bits, epoch, 998)
        self.assertEqual(1000, epoch)
        self.assertEqual(2, bin(bits & ((1 << 64) - 1)).count("1"))

    def test_bit_63_stays_writable_as_a_signed_integer(self):
        # SQLite integers are signed; a naive bitmap overflows on write.
        bits, epoch = _touch_spread(0, None, 1000)
        bits, epoch = _touch_spread(bits, epoch, 1000 + SPREAD_WINDOW_DAYS - 1)
        self.assertLess(bits, 1 << 63)
        with sqlite3.connect(":memory:") as connection:
            connection.execute("CREATE TABLE t (b INTEGER)")
            connection.execute("INSERT INTO t VALUES (?)", (bits,))
            self.assertEqual(bits, connection.execute("SELECT b FROM t").fetchone()[0])

    def test_merging_two_replicas_ors_rather_than_sums(self):
        # The same day on two machines is one day of spread, not two.
        a, _ = _touch_spread(0, None, 1000)
        b, _ = _touch_spread(0, None, 1000)
        merged, epoch = _merge_spread(a, 1000, b, 1000)
        self.assertEqual(1, bin(merged).count("1"))
        self.assertEqual(1000, epoch)

    def test_merging_aligns_windows_that_end_on_different_days(self):
        a, epoch_a = _touch_spread(0, None, 1000)
        b, epoch_b = _touch_spread(0, None, 1003)
        merged, epoch = _merge_spread(a, epoch_a, b, epoch_b)
        self.assertEqual(1003, epoch)
        self.assertEqual(2, bin(merged).count("1"))
        self.assertTrue(merged & 1)          # the newer day, in bit 0
        self.assertTrue(merged & (1 << 3))   # the older day, three days back


class MigrationTests(unittest.TestCase):
    """A v1 database opens as v2 with retrieval and references intact."""

    V1_SCHEMA = """
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL, created TEXT NOT NULL);
    CREATE TABLE memories (
        project_id TEXT NOT NULL, id TEXT NOT NULL, status TEXT NOT NULL,
        description TEXT NOT NULL, created TEXT, last_validated TEXT,
        created_from_task TEXT, area TEXT, body TEXT NOT NULL,
        PRIMARY KEY (project_id, id));
    CREATE TABLE labels (project_id TEXT, memory_id TEXT, label TEXT,
        PRIMARY KEY (project_id, memory_id, label));
    CREATE TABLE files (project_id TEXT, memory_id TEXT, path TEXT,
        PRIMARY KEY (project_id, memory_id, path));
    CREATE TABLE label_registry (project_id TEXT, label TEXT, description TEXT,
        PRIMARY KEY (project_id, label));
    CREATE TABLE edges (project_id TEXT, src TEXT, dst TEXT, kind TEXT, reason TEXT,
        PRIMARY KEY (project_id, src, dst, kind));
    CREATE TABLE usage (project_id TEXT, memory_id TEXT, surfaced INTEGER DEFAULT 0,
        applied INTEGER DEFAULT 0, last_surfaced TEXT, last_applied TEXT,
        PRIMARY KEY (project_id, memory_id));
    CREATE TABLE revisions (project_id TEXT, memory_id TEXT, revised_at TEXT, body TEXT);
    CREATE VIRTUAL TABLE memories_fts USING fts5(
        project_id UNINDEXED, memory_id UNINDEXED,
        id_text, description, triggers, tags, labels, facts, pattern, pitfalls, scope_text,
        tokenize='unicode61');
    """

    def setUp(self):
        import json

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "memory.db"
        connection = sqlite3.connect(self.db)
        connection.executescript(self.V1_SCHEMA)
        connection.execute("INSERT INTO meta VALUES ('schema_version', '1')")
        connection.execute("INSERT INTO projects VALUES ('demo', 'demo', '2026-01-01T00:00:00Z')")
        connection.executemany("INSERT INTO label_registry VALUES ('demo', ?, ?)",
                               [("area:x", "x"), ("area:z", "z")])
        for slug, description, labels, related in (
            ("cache-race", CACHE, ["area:x"], ["shader-stall"]),
            ("shader-stall", SHADER, ["area:z"], ["cache-race"]),
        ):
            body = memory(slug, description, labels, related)
            connection.execute(
                "INSERT INTO memories VALUES ('demo',?,?,?,?,?,?,?,?)",
                (slug, "active", description, "2026-01-0%dT00:00:00Z" % (1 if slug == "cache-race" else 2),
                 "2026-01-01", "t", "a", json.dumps(body)))
            connection.executemany("INSERT INTO labels VALUES ('demo',?,?)",
                                   [(slug, label) for label in labels])
            connection.execute("INSERT INTO files VALUES ('demo',?,?)", (slug, "Source/%s.cpp" % slug))
            connection.execute(
                "INSERT INTO memories_fts(project_id, memory_id, id_text, description, triggers, "
                "tags, labels, facts, pattern, pitfalls, scope_text) "
                "VALUES ('demo',?,?,?,?,'',?,?,'','','')",
                (slug, slug.replace("-", " "), description, "trigger for " + slug,
                 " ".join(labels), description))
        connection.executemany("INSERT INTO edges VALUES ('demo',?,?,'related','shared')",
                               [("cache-race", "shader-stall"), ("shader-stall", "cache-race")])
        connection.execute("INSERT INTO usage VALUES ('demo','cache-race',9,4,'2026-01-05T00:00:00Z',NULL)")
        connection.commit()
        connection.close()

    def open(self):
        store = SqliteMemoryStore(self.db, "demo", create=False)
        self.addCleanup(store.close)
        return store

    def test_opening_a_v1_database_migrates_it(self):
        store = self.open()
        version = store.connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        self.assertEqual("2", version[0])
        self.assertEqual(2, store.count())

    def test_retrieval_is_unchanged_after_migration(self):
        store = self.open()
        self.assertEqual("cache-race", store.recall("cache invalidation")["memories"][0]["id"])
        self.assertEqual(["shader-stall", "cache-race"],
                         [m["id"] for m in store.recall(order="recent", limit=5)["memories"]])

    def test_references_survive_as_uuid_edges(self):
        store = self.open()
        cache_uuid = store._uuid_for("cache-race")
        shader_uuid = store._uuid_for("shader-stall")
        edges = store.connection.execute(
            "SELECT src, dst FROM edges WHERE project_id='demo' AND kind='related'").fetchall()
        self.assertEqual({(cache_uuid, shader_uuid), (shader_uuid, cache_uuid)},
                         {(row["src"], row["dst"]) for row in edges})
        self.assertEqual(["cache-race"],
                         [e["to"] for e in store.get_memory_neighborhood("shader-stall")["edges"]])

    def test_existing_counters_are_kept_and_attributed_to_this_replica(self):
        store = self.open()
        entry = store.load_usage()["memories"]["cache-race"]
        self.assertEqual(9, entry["surfaced"])
        self.assertEqual(4, entry["applied"])
        # The split did not exist when those were recorded, so it starts empty
        # rather than inventing evidence for the audit to act on.
        self.assertEqual(0, entry["surfaced_direct"])
        owner = store.connection.execute("SELECT DISTINCT replica_id FROM usage").fetchone()
        self.assertEqual(store.replica_id, owner[0])

    def test_migration_is_not_repeated_on_the_next_open(self):
        first = self.open()
        uuids = {row[0] for row in first.connection.execute("SELECT uuid FROM memories")}
        again = SqliteMemoryStore(self.db, "demo", create=False)
        self.addCleanup(again.close)
        self.assertEqual(uuids, {row[0] for row in again.connection.execute("SELECT uuid FROM memories")})
        self.assertEqual(2, again.count())

    def test_the_v1_tables_are_gone(self):
        store = self.open()
        tables = {r[0] for r in store.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("memories_v1", tables)
        self.assertNotIn("usage_v1", tables)


if __name__ == "__main__":
    unittest.main()
