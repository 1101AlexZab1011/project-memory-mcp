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
import sys
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

#: Shortest wait before a sweep that is already overdue at startup. Long enough
#: that the server has finished coming up, short enough that a machine which is
#: only on for a few minutes at a time still gets its maintenance.
STARTUP_GRACE_SECONDS = 15

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
    run: Callable[[Any, Any], dict[str, Any]] = field(compare=False, repr=False)


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

        Swallowed, but not silent. A job that fails every hour forever used to
        leave nothing but a row in a table nobody queries - which is how a
        single orphaned outbox entry could stop this machine publishing anything
        at all, indefinitely, while every surface reported health. Failures go
        to stderr, because that is the one channel this server has that somebody
        is already reading.
        """
        started = _now()
        store = None
        try:
            store = self.open_store(job.project)
            # The lock is handed to the job, not held around it. Holding it for
            # the whole run is what the chunking was supposed to avoid, and it
            # silently defeated it: a sliced job slept between slices while
            # still holding the lock, which is worse than not sleeping, and the
            # outbox job waited on other people's servers with it held. A job
            # takes this for its database work and drops it for everything else.
            detail = job.run(store, self.lock_for(job.project))
            outcome, self.completed = "ok", self.completed + 1
        except Exception as error:  # noqa: BLE001 - a job may fail any number of ways
            detail = {"error": str(error), "traceback": traceback.format_exc(limit=3)}
            outcome = "failed"
            self.last_error = f"{job.kind}/{job.project}: {error}"
            # Every occurrence, not the first only: a job still failing on the
            # hundredth sweep is still broken, and a message that stops arriving
            # reads as a problem that went away.
            print(f"project-memory-mcp: job {job.kind}/{job.project} failed: {error}",
                  file=sys.stderr, flush=True)
        finally:
            if store is not None and hasattr(store, "close"):
                try:
                    store.close()
                except Exception:  # noqa: BLE001
                    pass
        self._record(job, started, outcome, detail)
        return {"kind": job.kind, "project": job.project, "outcome": outcome, "detail": detail}

    def seconds_since_last_run(self) -> float | None:
        """How long since any job ran, read from the log rather than from uptime.

        This is what lets the schedule survive a machine that is not always on.
        Process uptime says nothing about when maintenance last happened; the
        job log does, and it is already being written.

        None means "no record" - a fresh store, or a worker with no database to
        log to - which the scheduler treats as overdue rather than as recent.
        """
        if self.database is None:
            return None
        try:
            connection = sqlite3.connect(self.database)
            try:
                connection.executescript(SCHEMA)
                row = connection.execute("SELECT MAX(started) AS last FROM jobs").fetchone()
            finally:
                connection.close()
        except sqlite3.Error:
            return None
        if not row or not row[0]:
            return None
        try:
            then = datetime.strptime(row[0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
        # Clamped: a clock that moved backwards should make the next sweep look
        # overdue rather than schedule one in the past.
        return max(0.0, (datetime.now(timezone.utc) - then).total_seconds())

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
# Each takes an open store and a `guard` - the project lock - and returns what it
# did. A job holds the guard only while it touches the database and drops it for
# anything else: waiting on a remote, or simply pausing between slices. Holding
# it for the whole run is the mistake this signature exists to prevent.
#
# They are ordinary functions, so a test or the CLI can call one directly by
# passing any lock (or `threading.Lock()` if there is nothing to serialise).


def audit_job(store: Any, guard: Any, apply: bool = True, policy: Any = None) -> dict[str, Any]:
    """Tier, archive and clean one project.

    Held for the whole sweep, deliberately: a verdict is computed from counters
    that a concurrent write could change underneath it, and half an audit is
    worse than a slow one. This is the job to slice first if a project ever
    grows large enough for the hold to be felt.
    """
    from .audit import AuditPolicy, run_audit

    with guard:
        report = run_audit(store, policy=policy or AuditPolicy(), apply=apply)
    return report.summary()


def outbox_job(store: Any, guard: Any, key_path: Any = None) -> dict[str, Any]:
    """Deliver everything queued for publication.

    Not a retry path but the only path: `promote` never sends, so this is how a
    memory reaches a remote at all. That is why it runs first among jobs -
    somebody is waiting on the far end of a promotion and nobody is waiting on
    a sweep.

    `deliver_outbox` takes the guard per item and releases it across each send,
    so a queue of promotions to a dead server costs its connect timeouts without
    blocking a single read.

    The signing key is loaded here rather than inside `deliver_outbox`, because
    "which key is this machine's" is a fact about the installation and delivery
    should stay a function of its arguments. None is the ordinary case - a store
    that has never enrolled anywhere - and means the remote's bearer token is
    the credential.

    ``key_path`` names it, defaulting to this machine's. It is a parameter and
    not a constant read inside because a test has to be able to say where the
    key is: the whole defect this fixes was a `private_key` argument that every
    caller left at None, and asserting the fix at `deliver_outbox` rather than
    here would reproduce exactly that gap one level up. It did, in the first
    version of this change - a mutant that reverted this line survived.
    """
    from . import identity
    from .federation import deliver_outbox

    return deliver_outbox(store, private_key=identity.load_if_present(key_path),
                          db_lock=guard)


def dedup_job(store: Any, guard: Any, threshold: float = 0.6, limit: int = 25,
              chunk: int = 200) -> dict[str, Any]:
    """Find near-duplicate pairs and record how many are waiting for a decision.

    Nominates only. Deciding whether two memories are one lesson is a reading
    comprehension question, and this is the wrong place to answer it.

    Sliced because it is the most expensive job by a wide margin: every
    candidate pair means two body reads and two tokenisations, and the pair
    count grows with the edge count. Measured at 46ms for 200 memories and
    1.6s for 800 - which as one unbroken hold would stall every request for
    that project for the whole of it.
    """
    from .maintenance import duplicate_candidates

    best: list[dict[str, Any]] = []
    offset, examined = 0, 0
    while True:
        with guard:
            found = duplicate_candidates(store, limit=limit, threshold=threshold,
                                         offset=offset, scan=chunk)
        examined += found["examined"]
        # Trimmed every slice rather than accumulated: only the strongest
        # candidates are ever reported, and holding every body in memory to
        # throw most of them away at the end would be its own problem.
        best = sorted(best + found["candidates"], key=lambda p: -p["score"])[:limit]
        offset = found["next_offset"]
        if not found["remaining"]:
            break
        time.sleep(YIELD_SECONDS)  # lock released above; this is a real yield
    return {"candidates": len(best), "examined": examined,
            "pairs": [[m["id"] for m in pair["memories"]] for pair in best]}


def reindex_job(store: Any, guard: Any, chunk: int = 500) -> dict[str, Any]:
    """Rebuild the similarity graph in bounded slices.

    Derived edges are computed per write, which means the graph reflects
    whatever threshold was in force when each memory happened to be stored.
    Rebuilding is what makes changing that threshold mean anything - and it is
    precomputation, so it belongs here rather than on the write path.

    It also repairs drift from updates, which is narrower than it first looks.
    A write deletes every derived edge touching the memory, in both directions,
    then re-inserts only the ones pointing outward from it. The *pairs* usually
    survive, since traversal reads edges both ways - but not when the top-K is
    asymmetric: if B counts A among its ten best neighbours and A does not count
    B, updating A drops `B->A` and nothing puts it back. Only this does.

    Measured at 0.22s for 200 memories, 0.89s for 500, 2.29s for 1000 - linear,
    about 2.3ms each, in 500-row slices with the lock handed back between them.
    Hourly on a 1000-memory store is a 0.06% duty cycle. Somewhere north of tens
    of thousands that stops being free, and the answer then is a longer interval
    for this job rather than the whole sweep.
    """
    offset, rebuilt = 0, 0
    while True:
        with guard:
            result = store.rebuild_derived_edges(limit=chunk, offset=offset)
        rebuilt += result["rebuilt"]
        offset = result["next_offset"]
        if not result["remaining"] or not result["rebuilt"]:
            return {"rebuilt": rebuilt}
        time.sleep(YIELD_SECONDS)  # lock released above; this is a real yield


def rebase_job(store: Any, guard: Any, mark_stale: bool = False) -> dict[str, Any]:
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

    with guard:
        return check_anchors(store, mark_stale=mark_stale)


def evict_job(store: Any, guard: Any, older_than_days: int | None = None,
              keep_most_recent: int | None = None) -> dict[str, Any]:
    """Drop borrowed copies that have gone stale, or that there are too many of.

    A cache with no eviction is not a cache, it is a leak: every federated
    recall added rows and nothing ever removed one.

    Cheap and bounded - two indexed deletes - so it is not sliced. If a store
    ever accumulates enough cached rows for this to be felt, the eviction bound
    is already doing the wrong thing.
    """
    from .sqlite_store import CACHE_MAX_ROWS, CACHE_TTL_DAYS

    with guard:
        return store.evict_cache(
            older_than_days=CACHE_TTL_DAYS if older_than_days is None else older_than_days,
            keep_most_recent=CACHE_MAX_ROWS if keep_most_recent is None else keep_most_recent)


JOB_KINDS: dict[str, Callable[..., dict[str, Any]]] = {
    "audit": audit_job,
    "outbox": outbox_job,
    "dedup": dedup_job,
    "reindex": reindex_job,
    "rebase": rebase_job,
    "evict": evict_job,
}

#: Lower runs first. Delivering queued work beats tidying, because somebody is
#: waiting on the far end of a promotion and nobody is waiting on a sweep.
PRIORITIES = {"outbox": 10, "rebase": 20, "audit": 30, "evict": 35, "reindex": 40, "dedup": 50}


def make_job(kind: str, project: str, **kwargs: Any) -> Job:
    if kind not in JOB_KINDS:
        raise ValueError(f"Unknown job kind: {kind}. Known: {', '.join(sorted(JOB_KINDS))}")
    run = JOB_KINDS[kind]
    return Job(priority=PRIORITIES.get(kind, 50), key=f"{kind}:{project}",
               project=project, kind=kind,
               run=lambda store, guard: run(store, guard, **kwargs))


class Scheduler:
    """Decides when jobs are submitted. Separate from what runs them.

    One trigger: a floor timer, so tiering keeps moving in a project nobody is
    writing to. A quiet store has to advance too, and every activity-driven
    trigger fails exactly there.

    A second, capacity-based trigger was planned - "tier 1 is full, sweep now" -
    and deliberately dropped. It cannot do anything the timer does not: the
    audit's gates still decide what is eligible, so a capacity-fired sweep
    arrives to find nothing due. Making it do something would mean reviewing
    memories before they have had the exposure to be judged on, which is the one
    thing the audit is built not to do.

    Enqueueing is idempotent anyway. That is worth keeping for its own sake - a
    slow sweep must never let a backlog of identical ones accumulate behind it.
    """

    def __init__(self, computer: Computer, projects: Callable[[], list[str]],
                 interval_seconds: int = 3600,
                 kinds: tuple[str, ...] = ("outbox", "rebase", "audit", "evict", "reindex", "dedup")) -> None:
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

    def first_delay(self) -> float:
        """How long to wait before the first sweep of this run.

        The timer used to start at a full interval every time the process
        started, which quietly meant that a machine up for less than an interval
        at a time never swept at all. Not late - never: no audit, no outbox
        delivery, nothing, on any laptop that sleeps or any server restarted
        often. Uptime is not the clock; the job log is.
        """
        since = self.computer.seconds_since_last_run()
        if since is None:
            # Nothing on record. Treat as overdue - on a fresh store the sweep
            # finds nothing and costs nothing.
            return float(min(self.interval, STARTUP_GRACE_SECONDS))
        # Never sooner than the grace period, so a restart loop cannot turn into
        # a sweep loop, and never later than one interval.
        return float(max(STARTUP_GRACE_SECONDS, min(self.interval, self.interval - since)))

    def _run(self) -> None:
        # Short at startup only if a sweep is actually due, so a burst of work
        # never competes with the server coming up for no reason.
        delay = self.first_delay()
        while not self._stop.is_set():
            if self._stop.wait(delay):
                return
            delay = float(self.interval)
            try:
                self.sweep_now()
            except Exception:  # noqa: BLE001 - a scheduler that dies stops all maintenance
                continue
