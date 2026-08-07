"""Backups for a database-backed store.

Leaving git cost the store its redundancy: every clone used to be a complete
replica, and now a single unbacked failure loses everything. So backup is part
of the product rather than an operational afterthought.

Two forms, for two different jobs:

- **Snapshot** - a byte-exact copy of the database via SQLite's online backup
  API. Safe against a live server, restores everything including usage counters
  and revision history. This is the disaster-recovery artifact.
- **Export** - portable JSON, one file per database. Survives schema changes,
  reads in a text editor, and can be committed if you want memories in git
  again. This is the archive and migration artifact.

Snapshots restore exactly; exports restore the durable content. Keep both.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SNAPSHOT_PREFIX = "memory-"
SNAPSHOT_SUFFIX = ".db"


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def snapshot_database(database: Path | str, destination: Path | str, keep: int = 7) -> Path:
    """Copy the database with SQLite's online backup API.

    Safe while a server is serving: the API takes a consistent copy without
    blocking writers for the whole operation, which a filesystem copy of a live
    WAL database would not give.
    """
    database = Path(database)
    if not database.is_file():
        raise FileNotFoundError(f"No database at {database}")
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / f"{SNAPSHOT_PREFIX}{_stamp()}{SNAPSHOT_SUFFIX}"

    source = sqlite3.connect(database)
    try:
        copy = sqlite3.connect(target)
        try:
            source.backup(copy)
        finally:
            copy.close()
    finally:
        source.close()
    prune_snapshots(destination, keep)
    return target


def prune_snapshots(destination: Path | str, keep: int) -> list[Path]:
    """Delete all but the newest ``keep`` snapshots. Returns what was removed."""
    if keep < 1:
        return []
    destination = Path(destination)
    snapshots = sorted(
        (p for p in destination.glob(f"{SNAPSHOT_PREFIX}*{SNAPSHOT_SUFFIX}") if p.is_file()),
        key=lambda p: p.name,
        reverse=True,
    )
    removed = []
    for stale in snapshots[keep:]:
        stale.unlink()
        removed.append(stale)
    return removed


def export_json(database: Path | str, destination: Path | str, project: str | None = None) -> dict[str, Any]:
    """Write the durable content of the database as JSON.

    Memories, the label registry and usage counters, per project. Revision
    history is deliberately left to snapshots: it is large, it is history rather
    than content, and an export is meant to stay readable.
    """
    database = Path(database)
    if not database.is_file():
        raise FileNotFoundError(f"No database at {database}")
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        projects = [row["id"] for row in connection.execute("SELECT id FROM projects ORDER BY id")]
        if project is not None:
            if project not in projects:
                raise ValueError(f"Unknown project: {project}. Known: {', '.join(projects) or '(none)'}")
            projects = [project]

        payload: dict[str, Any] = {
            "format": "project-memory-export",
            "format_version": 2,
            "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": str(database),
            "projects": {},
        }
        for name in projects:
            memories = [
                json.loads(row["body"])
                for row in connection.execute(
                    "SELECT body FROM memories WHERE project_id=? ORDER BY slug", (name,)
                )
            ]
            labels = {
                row["label"]: {"description": row["description"]}
                for row in connection.execute(
                    "SELECT label, description FROM label_registry WHERE project_id=? ORDER BY label", (name,)
                )
            }
            # Keyed by slug and by replica: an export has to survive being read
            # into a different database, where the uuids will not match, and it
            # must not collapse two replicas' counters into one.
            usage: dict[str, dict[str, Any]] = {}
            for row in connection.execute(
                "SELECT m.slug AS slug, u.replica_id AS replica_id, u.surfaced AS surfaced, "
                "u.surfaced_direct AS surfaced_direct, u.applied AS applied, "
                "u.last_surfaced AS last_surfaced, u.last_applied AS last_applied, "
                "u.spread_bits AS spread_bits, u.spread_epoch AS spread_epoch "
                "FROM usage u JOIN memories m ON m.project_id=u.project_id AND m.uuid=u.memory_id "
                "WHERE u.project_id=? ORDER BY m.slug, u.replica_id", (name,)
            ):
                usage.setdefault(row["slug"], {})[row["replica_id"]] = {
                    "surfaced": row["surfaced"], "surfaced_direct": row["surfaced_direct"],
                    "applied": row["applied"], "last_surfaced": row["last_surfaced"],
                    "last_applied": row["last_applied"],
                    "spread_bits": row["spread_bits"], "spread_epoch": row["spread_epoch"],
                }
            payload["projects"][name] = {"labels": labels, "memories": memories, "usage": usage}
    finally:
        connection.close()

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    return {
        "written": str(destination),
        "projects": {name: len(data["memories"]) for name, data in payload["projects"].items()},
    }


def _replica_counters(by_replica: Any) -> dict[str, dict[str, Any]]:
    """Normalize either export shape into {replica_id: counters}.

    Format 1 stored one flat set of counters per memory, from before counters
    were attributed to a replica. Those belong to whoever wrote that database,
    which nothing records - so they are parked under a legacy id rather than
    silently credited to the machine doing the restore.
    """
    if not isinstance(by_replica, dict):
        return {}
    if any(isinstance(v, dict) for v in by_replica.values()):
        return {k: v for k, v in by_replica.items() if isinstance(v, dict)}
    return {"legacy": by_replica}


def import_json(database: Path | str, source: Path | str) -> dict[str, Any]:
    """Restore an export into a database, creating projects as needed.

    Existing memories with the same id are overwritten: an export is a
    known-good state, so restoring it should converge on that state rather than
    merge into whatever is there.
    """
    from .sqlite_store import SqliteMemoryStore

    payload = json.loads(Path(source).read_text(encoding="utf-8-sig"))
    if payload.get("format") != "project-memory-export":
        raise ValueError(f"{source} is not a project-memory export")

    restored: dict[str, int] = {}
    for name, data in (payload.get("projects") or {}).items():
        store = SqliteMemoryStore(database, name, create=True)
        try:
            for label, meta in (data.get("labels") or {}).items():
                try:
                    store.add_label(label, (meta or {}).get("description") or label)
                except Exception:
                    pass  # already registered; import is re-runnable
            with store.connection:
                for memory in data.get("memories") or []:
                    existing = store.connection.execute(
                        "SELECT uuid FROM memories WHERE project_id=? AND slug=?", (name, memory["id"])
                    ).fetchone()
                    store._write(memory, existing["uuid"] if existing else str(uuid.uuid4()))
                for slug, by_replica in (data.get("usage") or {}).items():
                    row = store.connection.execute(
                        "SELECT uuid FROM memories WHERE project_id=? AND slug=?", (name, slug)
                    ).fetchone()
                    if row is None:
                        continue  # counters for a memory the export did not carry
                    for replica_id, counters in _replica_counters(by_replica).items():
                        store.connection.execute(
                            "INSERT OR REPLACE INTO usage(project_id, memory_id, replica_id, surfaced, "
                            "surfaced_direct, applied, last_surfaced, last_applied, spread_bits, "
                            "spread_epoch) VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (name, row["uuid"], replica_id, counters.get("surfaced", 0),
                             counters.get("surfaced_direct", 0), counters.get("applied", 0),
                             counters.get("last_surfaced"), counters.get("last_applied"),
                             counters.get("spread_bits", 0), counters.get("spread_epoch")),
                        )
            restored[name] = len(data.get("memories") or [])
        finally:
            store.close()
    return {"database": str(database), "projects": restored}


class BackupScheduler:
    """Periodic snapshots alongside a running server.

    A daemon thread rather than an external cron job, so that turning the server
    on turns backups on. Losing the store because nobody wired up a scheduler is
    exactly the failure this exists to prevent.
    """

    def __init__(self, database: Path | str, destination: Path | str, interval_seconds: int, keep: int) -> None:
        self.database = Path(database)
        self.destination = Path(destination)
        self.interval = max(60, int(interval_seconds))
        self.keep = keep
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error: str | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="project-memory-backup")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def snapshot_once(self) -> str | None:
        """One snapshot. Returns the error if it failed, having reported it.

        Its own method so the failure path can be reached without waiting out an
        interval on a daemon thread. It was inline, and untested, and silent.
        """
        try:
            snapshot_database(self.database, self.destination, self.keep)
            self.last_error = None
        except Exception as exc:  # a failed backup must not take the server down
            self.last_error = str(exc)
            # Nor may it be silent, which it was: `last_error` was written here
            # and read by nothing, so a snapshot failing every hour looked
            # exactly like one succeeding every hour. This class exists because
            # losing the store is the failure that matters most, and a backup
            # nobody knows is broken is worse than no backup - it is the same
            # risk, plus the belief that it is covered.
            print(f"project-memory-mcp: BACKUP FAILED to {self.destination}: {exc}",
                  file=sys.stderr, flush=True)
        return self.last_error

    def _run(self) -> None:
        while not self._stop.is_set():
            # Wait first: a snapshot at startup would duplicate whatever the
            # previous run already wrote.
            if self._stop.wait(self.interval):
                return
            self.snapshot_once()
