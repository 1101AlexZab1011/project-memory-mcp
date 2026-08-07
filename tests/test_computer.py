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
from project_memory_mcp import maintenance
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
                   run=lambda store, guard: (_ for _ in ()).throw(RuntimeError("job exploded")))
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

        result = maintenance.check_anchors(store)

        self.assertEqual(["gone"], [entry["id"] for entry in result["adrift"]])
        self.assertEqual(2, result["checked"])

    def test_one_surviving_anchor_is_enough(self):
        # A memory spanning several files has not gone stale because one moved.
        store = self.open_store("demo")
        self.addCleanup(store.close)
        store.set_root_path(str(self.root))
        store.create_memory(self.anchored("partial", ["Source/Cache.cpp", "Source/Gone.cpp"]))
        self.assertEqual([], maintenance.check_anchors(store)["adrift"])

    def test_memories_with_no_files_are_not_judged(self):
        store = self.open_store("demo")
        self.addCleanup(store.close)
        store.set_root_path(str(self.root))
        store.create_memory(memory("unanchored", CACHE))
        self.assertEqual(0, maintenance.check_anchors(store)["checked"])

    def test_marking_is_opt_in_and_uses_stale_not_wrong(self):
        # The files may have moved rather than gone; stale says "check this",
        # which is exactly what the evidence supports.
        store = self.open_store("demo")
        self.addCleanup(store.close)
        store.set_root_path(str(self.root))
        store.create_memory(self.anchored("gone", ["Source/Deleted.cpp"]))

        maintenance.check_anchors(store)
        self.assertEqual("active", store.get_memory("gone")["status"])
        maintenance.check_anchors(store, mark_stale=True)
        self.assertEqual("stale", store.get_memory("gone")["status"])

    def test_a_store_that_cannot_see_the_code_says_so(self):
        # A server holds memories about repositories it cannot see. It reports
        # having nothing to check rather than declaring everything adrift.
        store = self.open_store("demo")
        self.addCleanup(store.close)
        store.create_memory(self.anchored("gone", ["Source/Deleted.cpp"]))
        result = maintenance.check_anchors(store)
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


class LockDisciplineTests(ComputerCase):
    """A job holds the project lock for its database work and nothing else.

    This was wrong in a way that hid: run_one took the lock around the whole
    job, so the slicing meant nothing - a sliced job slept between slices while
    still holding it, which is worse than not sleeping at all, and the outbox
    job waited on other people's servers with it held. Chunking a job cannot fix
    that; only handing the lock to the job can.
    """

    def seed(self, n, label_pool=("area:x",)):
        store = SqliteMemoryStore(self.db, "demo", create=False)
        for i in range(n):
            body = memory(f"mem-{i:03d}", f"{CACHE} variation {i}")
            body["scope"]["files"] = ["Source/Cache.cpp"]
            store.create_memory(body)
        store.close()

    def contention(self, kind, **kwargs):
        """Run a job and measure the longest an outsider waited for the lock.

        Counting successful grabs is not enough - a watcher running before and
        after the job grabs it freely either way, which made an earlier version
        of this pass against the very bug it was written for. The question is
        how long a request would have been stalled, so that is what is measured.
        """
        from project_memory_mcp.computer import make_job

        lock = threading.Lock()
        waits: list[float] = []
        stop = threading.Event()

        def outsider():
            while not stop.is_set():
                started = time.perf_counter()
                if lock.acquire(timeout=30):
                    waits.append(time.perf_counter() - started)
                    lock.release()
                time.sleep(0.002)

        computer = Computer(open_store=self.open_store, lock_for=lambda _p: lock)
        watcher = threading.Thread(target=outsider, daemon=True)
        watcher.start()
        try:
            began = time.perf_counter()
            result = computer.run_one(make_job(kind, "demo", **kwargs))
            elapsed = time.perf_counter() - began
        finally:
            stop.set()
            watcher.join(timeout=10)
        return result, max(waits, default=0.0), elapsed

    def test_a_sliced_dedup_lets_requests_through_while_it_runs(self):
        self.seed(60)
        result, longest_wait, elapsed = self.contention("dedup", chunk=5)
        self.assertEqual("ok", result["outcome"])
        self.assertLess(
            longest_wait, elapsed / 2,
            f"a request waited {longest_wait:.2f}s of a {elapsed:.2f}s dedup - the job is "
            "holding the project lock across its slices instead of between them")

    def test_dedup_examines_every_pair_across_slices(self):
        # Slicing must not lose pairs: a stable ordering and a moving offset are
        # what make one pass over the edge set add up.
        self.seed(30)
        store = self.open_store("demo")
        try:
            # A limit high enough that neither side truncates, or this
            # compares a capped list against an uncapped one.
            whole = maintenance.duplicate_candidates(store, limit=10_000)
            sliced, offset, seen = [], 0, 0
            while True:
                page = maintenance.duplicate_candidates(
                    store, limit=10_000, offset=offset, scan=4)
                sliced.extend(page["candidates"])
                seen += page["examined"]
                offset = page["next_offset"]
                if not page["remaining"]:
                    break
        finally:
            store.close()
        self.assertEqual(whole["examined"], seen)
        self.assertEqual(
            sorted(tuple(sorted(m["id"] for m in p["memories"])) for p in whole["candidates"]),
            sorted(tuple(sorted(m["id"] for m in p["memories"])) for p in sliced))

    def test_the_outbox_does_not_hold_the_lock_while_waiting_on_a_remote(self):
        # The step-1 bug, in the worker rather than the request path. A queue of
        # promotions to a dead server is a queue of connect timeouts.
        from project_memory_mcp import federation

        self.seed(1)
        store = self.open_store("demo")
        try:
            federation.add_remote(store.connection, "blackhole", "http://192.0.2.1:9/", "down")
            store.set_visibility("mem-000", "public")
            federation.promote(store, "mem-000", "blackhole", force=True)
        finally:
            store.close()

        result, longest_wait, elapsed = self.contention("outbox")
        self.assertEqual("ok", result["outcome"])
        # Nearly all of `elapsed` is the connect timeout. None of it should be
        # time anybody else spent waiting for this project.
        self.assertLess(
            longest_wait, elapsed / 2,
            f"a request waited {longest_wait:.2f}s of a {elapsed:.2f}s outbox drain - the "
            "lock is held across the network call")


class IntermittentUptimeTests(ComputerCase):
    """Maintenance has to survive a machine that is not always on.

    The timer used to start at a full interval on every process start, which
    meant a server up for less than an interval at a time never swept at all -
    not late, never. Uptime is not the clock; the job log is.
    """

    def scheduler(self, computer, interval=3600):
        from project_memory_mcp.computer import Scheduler

        return Scheduler(computer, lambda: ["demo"], interval_seconds=interval)

    def test_a_short_lived_process_still_sweeps(self):
        from project_memory_mcp.computer import STARTUP_GRACE_SECONDS

        computer = Computer(open_store=self.open_store, database=self.db)
        # Nothing has ever run, so a sweep is overdue rather than an hour away.
        self.assertLessEqual(self.scheduler(computer).first_delay(), STARTUP_GRACE_SECONDS)

    def test_a_recent_sweep_is_not_repeated_immediately(self):
        computer = Computer(open_store=self.open_store, database=self.db)
        computer.run_one(make_job("rebase", "demo"))
        delay = self.scheduler(computer, interval=3600).first_delay()
        self.assertGreater(delay, 3000, "a sweep that just ran is being repeated at startup")

    def test_a_sweep_overdue_since_the_last_run_happens_promptly(self):
        from project_memory_mcp.computer import STARTUP_GRACE_SECONDS

        computer = Computer(open_store=self.open_store, database=self.db)
        computer.run_one(make_job("rebase", "demo"))
        # Backdate the log: two hours of downtime against a one-hour interval.
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        connection = sqlite3.connect(self.db)
        try:
            with connection:
                connection.execute("UPDATE jobs SET started=?", (old,))
        finally:
            connection.close()
        self.assertLessEqual(self.scheduler(computer, interval=3600).first_delay(),
                             STARTUP_GRACE_SECONDS)

    def test_a_restart_loop_cannot_become_a_sweep_loop(self):
        # Overdue must still mean "soon", never "now": a server crash-looping
        # would otherwise spend all its uptime sweeping.
        from project_memory_mcp.computer import STARTUP_GRACE_SECONDS

        computer = Computer(open_store=self.open_store, database=self.db)
        self.assertGreaterEqual(self.scheduler(computer, interval=3600).first_delay(),
                                min(3600, STARTUP_GRACE_SECONDS))

    def test_a_clock_that_moved_backwards_makes_a_sweep_look_due(self):
        computer = Computer(open_store=self.open_store, database=self.db)
        computer.run_one(make_job("rebase", "demo"))
        future = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        connection = sqlite3.connect(self.db)
        try:
            with connection:
                connection.execute("UPDATE jobs SET started=?", (future,))
        finally:
            connection.close()
        # Clamped to zero elapsed rather than negative, so the wait stays sane.
        self.assertLessEqual(computer.seconds_since_last_run(), 0.0)
        self.assertLessEqual(self.scheduler(computer, interval=3600).first_delay(), 3600)

    def test_the_running_scheduler_sweeps_promptly_when_overdue(self):
        # first_delay() being right is not enough - _run has to use it. An
        # earlier version of these tests checked the calculation alone and
        # passed against the very bug they were written for.
        from project_memory_mcp import computer as computer_module

        computer = Computer(open_store=self.open_store, database=self.db)
        scheduler = self.scheduler(computer, interval=3600)
        original = computer_module.STARTUP_GRACE_SECONDS
        computer_module.STARTUP_GRACE_SECONDS = 0.2
        try:
            scheduler.start()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and computer.pending() == 0:
                time.sleep(0.05)
        finally:
            scheduler.stop()
            computer_module.STARTUP_GRACE_SECONDS = original
        self.assertGreater(
            computer.pending(), 0,
            "the scheduler waited a full interval before its first sweep, so a process that "
            "lives for less than an interval never sweeps at all")


class BorrowedAnchorTests(ComputerCase):
    """A cached copy describes another machine's repository."""

    def test_a_borrowed_memory_is_not_checked_against_this_working_tree(self):
        # Its file paths belong to whoever wrote it. Checking them here finds
        # nothing, every time, and would mark the whole cache adrift - or with
        # mark_stale, quietly rewrite another server's memories.
        root = Path(self.tmp.name) / "repo"
        root.mkdir()
        store = self.open_store("demo")
        try:
            store.set_root_path(str(root))
            store.cache_remote_results({"team": {"memories": [{
                "uuid": "borrowed-uuid",
                "memory": {
                    "schema_version": 1, "id": "their-lesson", "status": "active",
                    "description": "A lesson from a repository this machine cannot see.",
                    "tags": [], "labels": [],
                    "scope": {"project": "p", "area": "a",
                              "files": ["Source/TheirFile.cpp"], "applies_to": []},
                    "triggers": ["theirs"], "remembered_facts": ["a fact"],
                    "solution_pattern": [], "pitfalls": [],
                    "evidence": {"created_from_task": "t", "last_validated": "2026-01-01"},
                    "relationships": {"related": [], "supersedes": [], "superseded_by": []},
                }}]}})
            result = maintenance.check_anchors(store, mark_stale=True)
            self.assertEqual(0, result["checked"],
                             "a cached copy was checked against the local working tree")
            self.assertEqual([], result["adrift"])
            row = store.connection.execute(
                "SELECT status FROM memories WHERE slug='their-lesson'").fetchone()
            self.assertEqual("active", row["status"],
                             "another server's memory was marked stale from here")
        finally:
            store.close()
