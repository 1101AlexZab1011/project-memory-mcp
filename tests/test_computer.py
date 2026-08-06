"""The component that does the work, and the fact that it does it unprompted.

Before this existed the audit was a command somebody had to remember to type,
which in practice means a store that never gets cleaned. These check that the
work happens on its own, that it stays out of the way of serving, and that one
broken job does not stop the rest.
"""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from project_memory_mcp.audit import AuditPolicy, TierGate
from project_memory_mcp.computer import Budget, Computer, Job, Scheduler, make_job
from project_memory_mcp.sqlite_store import SqliteMemoryStore

CACHE = "Session cache invalidation races the auth refresh under load."


def memory(memory_id, description):
    return {
        "schema_version": 1, "id": memory_id, "status": "active", "description": description,
        "tags": [], "labels": ["area:x"],
        "scope": {"project": "p", "area": "a", "files": [], "applies_to": []},
        "triggers": ["trigger for " + memory_id], "remembered_facts": [description],
        "solution_pattern": [], "pitfalls": [],
        "evidence": {"created_from_task": "t", "last_validated": "2026-01-01"},
        "relationships": {"related": [], "supersedes": [], "superseded_by": []},
    }


class ComputerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "memory.db"
        store = SqliteMemoryStore(self.db, "demo")
        store.add_label("area:x", "x")
        store.close()

    def open_store(self, project):
        return SqliteMemoryStore(self.db, project, create=False)

    def computer(self):
        return Computer(open_store=self.open_store, database=self.db)


class QueueTests(ComputerCase):
    def test_a_job_runs_and_is_recorded(self):
        computer = self.computer()
        result = computer.run_one(make_job("dedup", "demo"))
        self.assertEqual("ok", result["outcome"])
        connection = sqlite3.connect(self.db)
        self.addCleanup(connection.close)
        self.assertEqual(("dedup", "ok"),
                         connection.execute("SELECT kind, outcome FROM jobs").fetchone())

    def test_the_same_job_queued_twice_only_waits_once(self):
        # A busy project would otherwise queue a hundred identical sweeps.
        computer = self.computer()
        self.assertTrue(computer.submit(make_job("audit", "demo")))
        self.assertFalse(computer.submit(make_job("audit", "demo")))
        self.assertEqual(1, computer.pending())

    def test_delivery_is_ordered_before_tidying(self):
        # Somebody is waiting on the far end of a promotion; nobody is waiting
        # on a sweep.
        computer = self.computer()
        computer.submit(make_job("dedup", "demo"))
        computer.submit(make_job("audit", "demo"))
        computer.submit(make_job("outbox", "demo"))
        order = [computer._queue.get().kind for _ in range(3)]
        self.assertEqual(["outbox", "audit", "dedup"], order)

    def test_a_failing_job_is_recorded_and_does_not_stop_the_worker(self):
        computer = self.computer()
        boom = Job(priority=1, key="boom:demo", project="demo", kind="boom",
                   run=lambda store: (_ for _ in ()).throw(RuntimeError("job exploded")))
        result = computer.run_one(boom)
        self.assertEqual("failed", result["outcome"])
        self.assertIn("job exploded", computer.last_error)
        # The next job still runs.
        self.assertEqual("ok", computer.run_one(make_job("dedup", "demo"))["outcome"])

    def test_an_unwritable_job_log_does_not_fail_the_job(self):
        computer = Computer(open_store=self.open_store, database=Path("/nonexistent/dir/x.db"))
        self.assertEqual("ok", computer.run_one(make_job("dedup", "demo"))["outcome"])

    def test_an_unknown_job_kind_is_refused_at_submission(self):
        with self.assertRaises(ValueError):
            make_job("teleport", "demo")


class BudgetTests(unittest.TestCase):
    """Heavy work has to hand the lock back, or serving stops while it runs."""

    def test_a_budget_expires(self):
        budget = Budget(seconds=0.01)
        self.assertFalse(budget.expired())
        time.sleep(0.02)
        self.assertTrue(budget.expired())

    def test_resetting_starts_the_slice_again(self):
        budget = Budget(seconds=0.05)
        time.sleep(0.03)
        budget.reset()
        self.assertFalse(budget.expired())


class WorkTests(ComputerCase):
    def test_the_audit_runs_without_anyone_typing_a_command(self):
        # This is the whole reason the component exists.
        store = self.open_store("demo")
        store.create_memory(memory("quiet-note", CACHE), visibility="public")
        # Both clocks have to pass: exposure and real time. Wind the tier clock
        # back rather than lowering the gate, so this exercises the shipped
        # defaults instead of a policy invented for the test.
        old = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
        store.connection.execute("UPDATE memories SET tier_since_query=0, tier_since=?", (old,))
        store.connection.commit()
        for _ in range(60):
            store.recall("nothing matches this at all", limit=1, full_count=0)
        store.close()

        computer = self.computer()
        computer.run_one(make_job("audit", "demo"))

        store = self.open_store("demo")
        self.addCleanup(store.close)
        archived = store.connection.execute(
            "SELECT archived_at FROM memories WHERE slug='quiet-note'").fetchone()["archived_at"]
        self.assertIsNotNone(archived)

    def test_the_worker_picks_jobs_up_on_its_own(self):
        computer = self.computer()
        computer.start()
        self.addCleanup(computer.stop)
        computer.submit(make_job("dedup", "demo"))
        deadline = time.monotonic() + 5
        while computer.completed == 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(1, computer.completed)

    def test_a_job_never_runs_two_at_once_for_one_project(self):
        # Two sweeps on one project would contend for the same rows.
        lock = threading.Lock()
        concurrent, peak = [0], [0]

        def watched(project):
            concurrent[0] += 1
            peak[0] = max(peak[0], concurrent[0])
            time.sleep(0.02)
            concurrent[0] -= 1
            return self.open_store(project)

        computer = Computer(open_store=watched, lock_for=lambda _p: lock, database=self.db)
        computer.start()
        self.addCleanup(computer.stop)
        for kind in ("audit", "outbox", "dedup"):
            computer.submit(make_job(kind, "demo"))
        deadline = time.monotonic() + 10
        while computer.completed < 3 and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(3, computer.completed)
        self.assertEqual(1, peak[0])


class AnchorTests(ComputerCase):
    """Re-anchoring: the one correctness check that is a fact, not a judgment."""

    def setUp(self):
        super().setUp()
        self.root = Path(self.tmp.name) / "repo"
        (self.root / "Source").mkdir(parents=True)
        (self.root / "Source" / "Cache.cpp").write_text("// still here", encoding="utf-8")

    def anchored(self, slug, files):
        entry = memory(slug, CACHE)
        entry["scope"]["files"] = files
        return entry

    def test_a_memory_whose_files_are_gone_is_reported(self):
        store = self.open_store("demo")
        self.addCleanup(store.close)
        store.set_root_path(str(self.root))
        store.create_memory(self.anchored("gone", ["Source/Deleted.cpp"]))
        store.create_memory(self.anchored("here", ["Source/Cache.cpp"]))

        result = store.check_anchors()

        self.assertEqual(["gone"], [entry["id"] for entry in result["adrift"]])
        self.assertEqual(2, result["checked"])

    def test_one_surviving_anchor_is_enough(self):
        # A memory spanning several files has not gone stale because one moved.
        store = self.open_store("demo")
        self.addCleanup(store.close)
        store.set_root_path(str(self.root))
        store.create_memory(self.anchored("partial", ["Source/Cache.cpp", "Source/Gone.cpp"]))
        self.assertEqual([], store.check_anchors()["adrift"])

    def test_memories_with_no_files_are_not_judged(self):
        store = self.open_store("demo")
        self.addCleanup(store.close)
        store.set_root_path(str(self.root))
        store.create_memory(memory("unanchored", CACHE))
        self.assertEqual(0, store.check_anchors()["checked"])

    def test_marking_is_opt_in_and_uses_stale_not_wrong(self):
        # The files may have moved rather than gone; stale says "check this",
        # which is exactly what the evidence supports.
        store = self.open_store("demo")
        self.addCleanup(store.close)
        store.set_root_path(str(self.root))
        store.create_memory(self.anchored("gone", ["Source/Deleted.cpp"]))

        store.check_anchors()
        self.assertEqual("active", store.get_memory("gone")["status"])
        store.check_anchors(mark_stale=True)
        self.assertEqual("stale", store.get_memory("gone")["status"])

    def test_a_store_that_cannot_see_the_code_says_so(self):
        # A server holds memories about repositories it cannot see. It reports
        # having nothing to check rather than declaring everything adrift.
        store = self.open_store("demo")
        self.addCleanup(store.close)
        store.create_memory(self.anchored("gone", ["Source/Deleted.cpp"]))
        result = store.check_anchors()
        self.assertEqual(0, result["checked"])
        self.assertIn("no root_path", result["skipped"])

    def test_the_job_runs_it(self):
        store = self.open_store("demo")
        store.set_root_path(str(self.root))
        store.create_memory(self.anchored("gone", ["Source/Deleted.cpp"]))
        store.close()
        result = self.computer().run_one(make_job("rebase", "demo"))
        self.assertEqual("ok", result["outcome"])
        self.assertEqual(["gone"], [e["id"] for e in result["detail"]["adrift"]])


class ReindexTests(ComputerCase):
    def test_the_similarity_graph_is_rebuilt_in_slices(self):
        store = self.open_store("demo")
        for i in range(5):
            store.create_memory(memory(f"note-{i}", f"{CACHE} variation {i}"))
        store.close()
        result = self.computer().run_one(make_job("reindex", "demo", chunk=2))
        self.assertEqual("ok", result["outcome"])
        self.assertEqual(5, result["detail"]["rebuilt"])

    def test_reindexing_an_empty_project_terminates(self):
        self.assertEqual({"rebuilt": 0},
                         self.computer().run_one(make_job("reindex", "demo"))["detail"])


class SchedulerTests(ComputerCase):
    def test_a_sweep_covers_every_project_and_every_kind(self):
        SqliteMemoryStore(self.db, "second").close()
        computer = self.computer()
        scheduler = Scheduler(computer, lambda: ["demo", "second"])
        self.assertEqual(2 * len(scheduler.kinds), scheduler.sweep_now())

    def test_a_quiet_project_still_gets_swept(self):
        # Every other trigger is driven by activity, so without a floor timer a
        # store nobody writes to would never advance a tier.
        computer = self.computer()
        scheduler = Scheduler(computer, lambda: ["demo"], interval_seconds=60)
        self.assertGreater(scheduler.sweep_now(), 0)

    def test_the_interval_has_a_floor(self):
        scheduler = Scheduler(self.computer(), lambda: [], interval_seconds=1)
        self.assertGreaterEqual(scheduler.interval, 60)


if __name__ == "__main__":
    unittest.main()
