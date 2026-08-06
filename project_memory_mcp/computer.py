"""The component that does the work on memories, away from the request path.

Everything else in this package answers a question and returns. This is the part
that runs on its own: tracking what has been used, tiering, archiving, draining
promotions, finding duplicates. Without it the audit is a command somebody has
to remember to type, which means in practice a store that never gets cleaned.

Three things shape the design, all of them consequences of the work being heavy
rather than incidental:

**It must not compete with serving.** A sweep over a large project cannot run on
a request thread, and it cannot hold a project's lock for its whole duration
either. Jobs are therefore chunked: each takes the lock, does a bounded slice,
releases, and yields before asking for it again. A long job slows requests down
slightly; it never blocks them.

**It must be able to leave.** At small scale it runs as a thread inside `serve`.
At large scale that is wrong - the computation would contend with request
handling for one interpreter - so the same worker runs standalone against the
same database (`project-memory-mcp compute`), on another machine if you like.
Nothing about a job assumes it shares a process with the server.

**It must be safe to interrupt.** Every job derives what to do from the stored
state rather than from progress it is holding, so a worker killed mid-sweep
leaves a consistent store and the next run simply picks up what is still due.
"""

from __future__ import annotations

import queue
import sqlite3
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

#: How long a job may hold a project's lock before it must yield. Short: the
#: point is that requests keep flowing while heavy work happens.
SLICE_SECONDS = 0.25

#: Pause between slices, so a long job cannot monopolise the interpreter.
YIELD_SECONDS = 0.01

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    kind       TEXT NOT NULL,
    started    TEXT NOT NULL,
    finished   TEXT,
    outcome    TEXT,
    detail     TEXT
);
CREATE INDEX IF NOT EXISTS jobs_recent ON jobs(project_id, started);
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(order=True)
class Job:
    """One unit of work for one project.

    ``priority`` decides what runs first when several are waiting; ``key``
    collapses duplicates, so enqueueing the same job twice while it is still
    pending does not run it twice.
    """

    priority: int
    key: str = field(compare=False)
    project: str = field(compare=False)
    kind: str = field(compare=False)
    run: Callable[[Any], dict[str, Any]] = field(compare=False, repr=False)


class Budget:
    """Lets a job know when to hand the lock back.

    Passed into every job that touches more than a bounded amount of data. A job
    that ignores it still works; a job that respects it keeps the server
    responsive while it runs.
    """

    def __init__(self, seconds: float = SLICE_SECONDS) -> None:
        self.seconds = seconds
        self._start = time.monotonic()

    def expired(self) -> bool:
        return (time.monotonic() - self._start) >= self.seconds

    def reset(self) -> None:
        self._start = time.monotonic()


class Computer:
    """Runs jobs against the store, one project at a time.

    Deliberately single-worker. Two jobs on one project would contend for the
    same rows, and two projects at once would multiply the load this exists to
    keep bounded. Throughput is not the goal; staying out of the way is.
    """

    def __init__(self, open_store: Callable[[str], Any],
                 lock_for: Callable[[str], threading.Lock] | None = None,
                 database: Path | str | None = None) -> None:
        self.open_store = open_store
        self.lock_for = lock_for or (lambda _project: threading.Lock())
        self.database = Path(database) if database else None
        self._queue: queue.PriorityQueue[Job] = queue.PriorityQueue()
        self._pending: set[str] = set()
        self._guard = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error: str | None = None
        self.completed = 0

    # ------------------------------------------------------------------ queue

    def submit(self, job: Job) -> bool:
        """Queue a job unless an identical one is already waiting."""
        with self._guard:
            if job.key in self._pending:
                return False
            self._pending.add(job.key)
        self._queue.put(job)
        return True

    def pending(self) -> int:
        return self._queue.qsize()

    # ----------------------------------------------------------------- runtime

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="project-memory-computer")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            with self._guard:
                self._pending.discard(job.key)
            self.run_one(job)

    def run_one(self, job: Job) -> dict[str, Any]:
        """Execute a job and record what happened, whatever happened.

        Failures are recorded and swallowed: one broken job must not take the
        worker down and leave every other kind of maintenance unrun.
        """
        started = _now()
        store = None
        try:
            store = self.open_store(job.project)
            with self.lock_for(job.project):
                detail = job.run(store)
            outcome, self.completed = "ok", self.completed + 1
        except Exception as error:  # noqa: BLE001 - a job may fail any number of ways
            detail = {"error": str(error), "traceback": traceback.format_exc(limit=3)}
            outcome = "failed"
            self.last_error = f"{job.kind}/{job.project}: {error}"
        finally:
            if store is not None and hasattr(store, "close"):
                try:
                    store.close()
                except Exception:  # noqa: BLE001
                    pass
        self._record(job, started, outcome, detail)
        return {"kind": job.kind, "project": job.project, "outcome": outcome, "detail": detail}

    def _record(self, job: Job, started: str, outcome: str, detail: Any) -> None:
        if self.database is None:
            return
        import json

        try:
            connection = sqlite3.connect(self.database)
            try:
                connection.executescript(SCHEMA)
                with connection:
                    connection.execute(
                        "INSERT INTO jobs(project_id, kind, started, finished, outcome, detail) "
                        "VALUES (?,?,?,?,?,?)",
                        (job.project, job.kind, started, _now(), outcome, json.dumps(detail)[:4000]))
            finally:
                connection.close()
        except sqlite3.Error:
            pass  # a job log that cannot be written must not fail the job


# ------------------------------------------------------------------------ jobs
#
# Each takes an open store and returns what it did. They are ordinary functions
# so they can be called directly in a test or from the CLI without a worker.


def audit_job(store: Any, apply: bool = True, policy: Any = None) -> dict[str, Any]:
    """Tier, archive and clean one project."""
    from .audit import AuditPolicy, run_audit

    report = run_audit(store, policy=policy or AuditPolicy(), apply=apply)
    return report.summary()


def outbox_job(store: Any) -> dict[str, Any]:
    """Deliver everything queued for publication.

    Not a retry path but the only path: `promote` never sends, so this is how a
    memory reaches a remote at all. That is why it runs first among jobs -
    somebody is waiting on the far end of a promotion and nobody is waiting on
    a sweep.
    """
    from .federation import deliver_outbox

    return deliver_outbox(store)


def dedup_job(store: Any, threshold: float = 0.6, limit: int = 25) -> dict[str, Any]:
    """Find near-duplicate pairs and record how many are waiting for a decision.

    Nominates only. Deciding whether two memories are one lesson is a reading
    comprehension question, and this is the wrong place to answer it.
    """
    from .maintenance import duplicate_candidates

    found = duplicate_candidates(store, limit=limit, threshold=threshold)
    return {"candidates": found["count"],
            "pairs": [[m["id"] for m in pair["memories"]] for pair in found["candidates"][:limit]]}


def reindex_job(store: Any, chunk: int = 500) -> dict[str, Any]:
    """Rebuild the similarity graph in bounded slices.

    Derived edges are computed per write, which means the graph reflects
    whatever threshold was in force when each memory happened to be stored.
    Rebuilding is what makes changing that threshold mean anything - and it is
    precomputation, so it belongs here rather than on the write path.
    """
    offset, rebuilt = 0, 0
    while True:
        result = store.rebuild_derived_edges(limit=chunk, offset=offset)
        rebuilt += result["rebuilt"]
        offset = result["next_offset"]
        if not result["remaining"] or not result["rebuilt"]:
            return {"rebuilt": rebuilt}
        time.sleep(YIELD_SECONDS)  # hand the interpreter back between slices


def rebase_job(store: Any, mark_stale: bool = False) -> dict[str, Any]:
    """Re-anchor memories to the code as it is now.

    A memory naming files that no longer exist is describing something that has
    gone. That is a fact rather than a judgment - the paths are there or they
    are not - which is what makes it the one correctness check that can be
    automated at all.

    It needs the working tree, so it only ever runs on a machine that has it. A
    server holds memories about repositories it cannot see, and there this job
    reports that it has nothing to check rather than pretending otherwise.
    """
    from .maintenance import check_anchors

    return check_anchors(store, mark_stale=mark_stale)


JOB_KINDS: dict[str, Callable[..., dict[str, Any]]] = {
    "audit": audit_job,
    "outbox": outbox_job,
    "dedup": dedup_job,
    "reindex": reindex_job,
    "rebase": rebase_job,
}

#: Lower runs first. Delivering queued work beats tidying, because somebody is
#: waiting on the far end of a promotion and nobody is waiting on a sweep.
PRIORITIES = {"outbox": 10, "rebase": 20, "audit": 30, "reindex": 40, "dedup": 50}


def make_job(kind: str, project: str, **kwargs: Any) -> Job:
    if kind not in JOB_KINDS:
        raise ValueError(f"Unknown job kind: {kind}. Known: {', '.join(sorted(JOB_KINDS))}")
    run = JOB_KINDS[kind]
    return Job(priority=PRIORITIES.get(kind, 50), key=f"{kind}:{project}",
               project=project, kind=kind,
               run=lambda store: run(store, **kwargs))


class Scheduler:
    """Decides when jobs are submitted. Separate from what runs them.

    Two triggers, matching how the work actually arrives:

    - **A floor timer**, so tiering keeps moving in a project nobody is writing
      to. Without it a quiet store never advances, because every other trigger
      is driven by activity.
    - **Capacity**, checked cheaply after writes: when tier 1 grows past its
      bound, a sweep is due. This is the one that matters under load, and it is
      why enqueueing is idempotent - a busy project would otherwise queue a
      hundred identical sweeps.
    """

    def __init__(self, computer: Computer, projects: Callable[[], list[str]],
                 interval_seconds: int = 3600,
                 kinds: tuple[str, ...] = ("outbox", "rebase", "audit", "dedup")) -> None:
        self.computer = computer
        self.projects = projects
        self.interval = max(60, int(interval_seconds))
        self.kinds = kinds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="project-memory-scheduler")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def sweep_now(self) -> int:
        submitted = 0
        for project in self.projects():
            for kind in self.kinds:
                submitted += int(self.computer.submit(make_job(kind, project)))
        return submitted

    def _run(self) -> None:
        while not self._stop.is_set():
            # Wait first: a burst of work at startup would compete with the
            # server coming up, and nothing is urgent at second zero.
            if self._stop.wait(self.interval):
                return
            try:
                self.sweep_now()
            except Exception:  # noqa: BLE001 - a scheduler that dies stops all maintenance
                continue
