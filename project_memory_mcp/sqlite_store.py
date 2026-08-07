"""SQLite-backed memory store.

The database is the source of truth: there are no memory files. Every row is
scoped to a project, because one server is expected to hold several.

Phase 1 keeps ranking in Python, loading a project's memories to score them,
exactly as the file backend does. Phase 2 replaces that with FTS5 and a
bounded local graph walk, at which point nothing loads the whole project.
See docs/server-architecture.md.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid as uuid_module
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import usage
from . import validation
from .validation import ID_RE, LABEL_RE, LabelExpression

SCHEMA_VERSION = 7

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

-- `queries` counts relevance-mode recalls. It is the unit the audit measures
-- exposure in: a project nobody queried for six weeks has served no chances to
-- be seen, so its memories are not "unused" - they were never asked.
CREATE TABLE IF NOT EXISTS projects (
    id      TEXT PRIMARY KEY,
    name    TEXT NOT NULL,
    created TEXT NOT NULL,
    queries INTEGER NOT NULL DEFAULT 0,
    -- Where this project's code lives, when the store is on the same machine.
    -- Null on a server: it holds memories about repositories it cannot see, so
    -- anything needing the working tree is a local job only.
    root_path TEXT
);

-- `uuid` is the identity every other table points at; `slug` is the readable
-- name the tools and the agent speak. Two people working offline will each coin
-- `shader-compile-stall` for different lessons, so identity cannot be the name.
CREATE TABLE IF NOT EXISTS memories (
    project_id        TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    uuid              TEXT NOT NULL,
    slug              TEXT NOT NULL,
    status            TEXT NOT NULL,
    description       TEXT NOT NULL,
    created           TEXT,
    last_validated    TEXT,
    created_from_task TEXT,
    area              TEXT,
    body              TEXT NOT NULL,
    -- Retention tier. A memory is reviewed once it has had enough exposure
    -- *and* enough real time in its current tier; surviving moves it up, where
    -- the next review is further away. Both clocks matter: queries say whether
    -- it had a fair chance, days say whether the problem outlived the week.
    tier              INTEGER NOT NULL DEFAULT 1,
    tier_since        TEXT,
    tier_since_query  INTEGER NOT NULL DEFAULT 0,
    -- Archive is orthogonal to status. Status answers "is this true"; archive
    -- answers "is this in the working set". A memory can be perfectly correct
    -- and archived for being quiet, or wrong and still live as a warning.
    -- Archived memories leave the ranked pool entirely - they never compete for
    -- a top-K slot - but stay readable by id, visible in the UI, and reversible.
    archived_at       TEXT,
    -- Set when this memory was folded into another. The row stays: a merge is
    -- a judgment call, and keeping the loser archived with a pointer makes a
    -- wrong one visible and reversible instead of silently destructive.
    merged_into       TEXT,
    -- Who wrote this last. The name is one server's word for a client; the
    -- fingerprint is the same everywhere, because it is derived from a key only
    -- that client can use. Both are kept: one is readable, one is verifiable.
    author_client     TEXT,
    author_name       TEXT,
    author_key        TEXT,
    -- Who a memory is *for*, decided when it is written. This is a separate
    -- question from whether it has proven itself: "the user prefers early
    -- returns" gets recalled constantly and is never useful to anyone else, so
    -- no amount of usage should promote it into shared space. Private by
    -- default, because leaking is worse than under-sharing.
    visibility        TEXT NOT NULL DEFAULT 'private',
    -- NULL for memories this machine owns; the remote's name for cached copies.
    -- Cached rows are read-only here: never audited, never promoted, evictable.
    origin_remote     TEXT,
    PRIMARY KEY (project_id, uuid)
);
CREATE INDEX IF NOT EXISTS memories_tier ON memories(project_id, tier);
CREATE INDEX IF NOT EXISTS memories_archived ON memories(project_id, archived_at);
-- Unique for now: one writer, so a duplicate slug is a mistake rather than a
-- merge. Relaxing this is a sync concern, and dropping an index is cheap.
CREATE UNIQUE INDEX IF NOT EXISTS memories_slug ON memories(project_id, slug)
    WHERE origin_remote IS NULL;
CREATE INDEX IF NOT EXISTS memories_origin ON memories(project_id, origin_remote);
CREATE INDEX IF NOT EXISTS memories_visibility ON memories(project_id, visibility);
CREATE INDEX IF NOT EXISTS memories_status  ON memories(project_id, status);
CREATE INDEX IF NOT EXISTS memories_created ON memories(project_id, created, slug);

CREATE TABLE IF NOT EXISTS labels (
    project_id TEXT NOT NULL, memory_id TEXT NOT NULL, label TEXT NOT NULL,
    PRIMARY KEY (project_id, memory_id, label)
);
CREATE INDEX IF NOT EXISTS labels_by_label ON labels(project_id, label);

CREATE TABLE IF NOT EXISTS files (
    project_id TEXT NOT NULL, memory_id TEXT NOT NULL, path TEXT NOT NULL,
    PRIMARY KEY (project_id, memory_id, path)
);
CREATE INDEX IF NOT EXISTS files_by_path ON files(project_id, path);

CREATE TABLE IF NOT EXISTS label_registry (
    project_id TEXT NOT NULL, label TEXT NOT NULL, description TEXT NOT NULL,
    PRIMARY KEY (project_id, label)
);

CREATE TABLE IF NOT EXISTS edges (
    project_id TEXT NOT NULL, src TEXT NOT NULL, dst TEXT NOT NULL,
    kind TEXT NOT NULL, reason TEXT,
    PRIMARY KEY (project_id, src, dst, kind)
);
CREATE INDEX IF NOT EXISTS edges_dst ON edges(project_id, dst);

-- One row per (memory, replica). Counters are grow-only and each replica owns
-- its own row exclusively, so merging is a SUM and pushing is an idempotent
-- overwrite rather than a stream of increments.
--
-- `surfaced` counts every appearance; `surfaced_direct` counts only the ones
-- the memory earned by matching the query. Without the split, a weak memory
-- linked to a popular one inherits its neighbour's traffic and never ages out.
--
-- `spread_bits` is a 64-day rolling bitmap, one bit per day, newest in bit 0.
-- Distinct days is a popcount, merging across replicas is a bitwise OR, and it
-- costs 8 bytes - where storing the days themselves would not stay bounded.
CREATE TABLE IF NOT EXISTS usage (
    project_id TEXT NOT NULL, memory_id TEXT NOT NULL, replica_id TEXT NOT NULL,
    surfaced INTEGER NOT NULL DEFAULT 0,
    surfaced_direct INTEGER NOT NULL DEFAULT 0,
    applied INTEGER NOT NULL DEFAULT 0,
    last_surfaced TEXT, last_applied TEXT,
    spread_bits INTEGER NOT NULL DEFAULT 0,
    spread_epoch INTEGER,
    PRIMARY KEY (project_id, memory_id, replica_id)
);

CREATE TABLE IF NOT EXISTS revisions (
    project_id TEXT NOT NULL, memory_id TEXT NOT NULL,
    revised_at TEXT NOT NULL, body TEXT NOT NULL,
    author_client TEXT, author_name TEXT
);
CREATE INDEX IF NOT EXISTS revisions_memory ON revisions(project_id, memory_id, revised_at);

-- Separate columns rather than one blob so bm25() can weight fields, matching
-- what the Python ranker did by repeating tokens. Identifiers are split on
-- case boundaries at write time, which is where the old tokenizer's work moves.
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    project_id UNINDEXED, memory_id UNINDEXED,
    id_text, description, triggers, tags, labels, facts, pattern, pitfalls, scope_text,
    tokenize='unicode61'
);

-- Every sweep writes what it examined and what it decided, so the auditor can
-- itself be audited. Findings are kept for runs that changed nothing too -
-- that is the whole point of watching it before letting it act.
CREATE TABLE IF NOT EXISTS audit_runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    started    TEXT NOT NULL,
    finished   TEXT,
    applied    INTEGER NOT NULL DEFAULT 0,
    queries    INTEGER NOT NULL DEFAULT 0,
    examined   INTEGER NOT NULL DEFAULT 0,
    due        INTEGER NOT NULL DEFAULT 0,
    promoted   INTEGER NOT NULL DEFAULT 0,
    archived   INTEGER NOT NULL DEFAULT 0,
    deleted    INTEGER NOT NULL DEFAULT 0,
    capped     INTEGER NOT NULL DEFAULT 0,
    policy     TEXT
);
CREATE INDEX IF NOT EXISTS audit_runs_project ON audit_runs(project_id, started);

CREATE TABLE IF NOT EXISTS audit_findings (
    run_id     INTEGER NOT NULL REFERENCES audit_runs(id) ON DELETE CASCADE,
    project_id TEXT NOT NULL,
    memory_id  TEXT NOT NULL,
    slug       TEXT NOT NULL,
    tier       INTEGER NOT NULL,
    verdict    TEXT NOT NULL,
    reason     TEXT NOT NULL,
    evidence   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_findings_run ON audit_findings(run_id);

-- Server-wide, not per project: a client is an actor, and `project_scope` says
-- which projects it may touch. Nothing in this table is confidential - a public
-- key is publishable and a token is stored only as a hash - which matters
-- because this server ships scheduled snapshots and JSON exports.
CREATE TABLE IF NOT EXISTS clients (
    client_id     TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    public_key    TEXT,
    fingerprint   TEXT,
    token_hash    TEXT,
    role          TEXT NOT NULL DEFAULT 'contributor',
    project_scope TEXT,
    created       TEXT NOT NULL,
    last_seen     TEXT,
    revoked_at    TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS clients_fingerprint ON clients(fingerprint)
    WHERE fingerprint IS NOT NULL;

-- Single-use, short-lived, and the only thing that travels over an untrusted
-- channel during enrollment. A code leaked after use is worthless; the real
-- credential never crosses the wire in either direction.
-- Servers this machine federates with. Local configuration, not shared: two
-- clients of the same server need not know about each other's other remotes.
-- Zero rows is the normal case - local-only is the default.
CREATE TABLE IF NOT EXISTS remotes (
    name        TEXT PRIMARY KEY,
    url         TEXT NOT NULL,
    description TEXT,
    token       TEXT,
    enabled     INTEGER NOT NULL DEFAULT 1,
    added       TEXT NOT NULL,
    last_ok     TEXT,
    last_error  TEXT
);

-- Promotions that could not be delivered. Work waits here rather than failing
-- the agent's call: a remote being asleep is not the agent's problem.
CREATE TABLE IF NOT EXISTS outbox (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    memory_id  TEXT NOT NULL,
    remote     TEXT NOT NULL,
    queued_at  TEXT NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(project_id, remote);

CREATE TABLE IF NOT EXISTS enrollment_codes (
    code       TEXT PRIMARY KEY,
    name       TEXT,
    role       TEXT NOT NULL DEFAULT 'contributor',
    project_scope TEXT,
    expires_at TEXT NOT NULL,
    created    TEXT NOT NULL,
    used_at    TEXT,
    used_by    TEXT
);
"""

# bm25() column weights, in the column order above (the two UNINDEXED columns
# take a weight too). Mirrors ranking.FIELD_WEIGHTS.
FTS_WEIGHTS = (0.0, 0.0, 3.0, 3.0, 2.0, 2.0, 1.0, 1.0, 1.0, 1.0, 1.0)

# Bounds for the local graph walk. The walk's mass concentrates within a few
# hops of the seeds, so expanding further costs time and changes nothing.
WALK_DEPTH = 2
WALK_MAX_NODES = 250
WALK_FRONTIER = 60
WALK_SEEDS = 5
DERIVED_MAX_NEIGHBOURS = 10
DERIVED_CANDIDATE_LIMIT = 200
DERIVED_THRESHOLD = 0.34

# Lifecycle confidence, mirroring ranking.STATUS_FACTORS.
_STATUS_FACTORS = {"active": 1.0, "stale": 0.7, "superseded": 0.4, "wrong": 0.2}


# Re-exported: callers import StoreError from the store they use, and the label
# grammar raises it too, so both must be the same class.
StoreError = validation.StoreError


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ensure_replica_id(connection: sqlite3.Connection) -> str:
    """This installation's identity, generated once and never coordinated.

    Counters are keyed by it so that two machines' counts add rather than
    overwrite each other.
    """
    row = connection.execute("SELECT value FROM meta WHERE key='replica_id'").fetchone()
    if row is not None:
        return row[0]
    replica = str(uuid_module.uuid4())
    connection.execute("INSERT INTO meta(key, value) VALUES ('replica_id', ?)", (replica,))
    return replica


def _upgrade(connection: sqlite3.Connection) -> None:
    """Bring an older database up to the current schema, in place.

    Runs before the schema script, because ``CREATE TABLE IF NOT EXISTS`` would
    silently leave a v1 table as it found it.
    """
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "memories" not in tables:
        return  # fresh database; the schema script creates the current version
    columns = {r[1] for r in connection.execute("PRAGMA table_info(memories)")}
    if "uuid" not in columns:
        _migrate_v1_to_v2(connection)
        columns = {r[1] for r in connection.execute("PRAGMA table_info(memories)")}
    if "tier" not in columns:
        _migrate_v2_to_v3(connection)
        columns = {r[1] for r in connection.execute("PRAGMA table_info(memories)")}
    if "archived_at" not in columns:
        _migrate_v3_to_v4(connection)
        columns = {r[1] for r in connection.execute("PRAGMA table_info(memories)")}
    if "author_client" not in columns:
        _migrate_v4_to_v5(connection)
        columns = {r[1] for r in connection.execute("PRAGMA table_info(memories)")}
    if "visibility" not in columns:
        _migrate_v5_to_v6(connection)
    if "root_path" not in {r[1] for r in connection.execute("PRAGMA table_info(projects)")}:
        _migrate_v6_to_v7(connection)


def _migrate_v6_to_v7(connection: sqlite3.Connection) -> None:
    """Record where a project's code lives, for the checks that need to see it."""
    connection.execute("ALTER TABLE projects ADD COLUMN root_path TEXT")
    connection.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
                       (str(SCHEMA_VERSION),))
    connection.commit()


def _migrate_v5_to_v6(connection: sqlite3.Connection) -> None:
    """Add the visibility axis and remote origin.

    Existing memories become public: they were written into a store that was
    already shared, so calling them private now would misdescribe what happened
    and would silently withdraw them from people already relying on them.
    """
    connection.execute(
        "ALTER TABLE memories ADD COLUMN visibility TEXT NOT NULL DEFAULT 'private'")
    connection.execute("ALTER TABLE memories ADD COLUMN origin_remote TEXT")
    connection.execute("UPDATE memories SET visibility='public'")
    connection.execute("DROP INDEX IF EXISTS memories_slug")
    connection.executescript("""
        CREATE UNIQUE INDEX IF NOT EXISTS memories_slug ON memories(project_id, slug)
            WHERE origin_remote IS NULL;
        CREATE INDEX IF NOT EXISTS memories_origin ON memories(project_id, origin_remote);
        CREATE INDEX IF NOT EXISTS memories_visibility ON memories(project_id, visibility);
    """)
    connection.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
                       (str(SCHEMA_VERSION),))
    connection.commit()


def _migrate_v4_to_v5(connection: sqlite3.Connection) -> None:
    """Add authorship. Additive; existing rows have no recorded author.

    They are left null rather than credited to whoever runs the migration -
    attribution is a record of what happened, and inventing it would make the
    history less trustworthy than admitting the gap.
    """
    for column in ("author_client", "author_name", "author_key"):
        connection.execute(f"ALTER TABLE memories ADD COLUMN {column} TEXT")
    for column in ("author_client", "author_name"):
        connection.execute(f"ALTER TABLE revisions ADD COLUMN {column} TEXT")
    connection.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
                       (str(SCHEMA_VERSION),))
    connection.commit()


def _migrate_v3_to_v4(connection: sqlite3.Connection) -> None:
    """Add the archive state. Purely additive; nothing starts archived."""
    connection.execute("ALTER TABLE memories ADD COLUMN archived_at TEXT")
    connection.execute("ALTER TABLE memories ADD COLUMN merged_into TEXT")
    if "audit_runs" in {r[0] for r in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}:
        if "deleted" not in {r[1] for r in connection.execute("PRAGMA table_info(audit_runs)")}:
            connection.execute("ALTER TABLE audit_runs ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0")
    connection.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
                       (str(SCHEMA_VERSION),))
    connection.commit()


def _migrate_v2_to_v3(connection: sqlite3.Connection) -> None:
    """Add retention tiers and the per-project query counter.

    Purely additive. Existing memories start in tier 1 with their tier clock
    beginning now: nothing was measuring exposure before this, and dating their
    tier entry back to creation would make them instantly due for review on
    evidence that was never collected.
    """
    connection.execute("ALTER TABLE memories ADD COLUMN tier INTEGER NOT NULL DEFAULT 1")
    connection.execute("ALTER TABLE memories ADD COLUMN tier_since TEXT")
    connection.execute("ALTER TABLE memories ADD COLUMN tier_since_query INTEGER NOT NULL DEFAULT 0")
    if "queries" not in {r[1] for r in connection.execute("PRAGMA table_info(projects)")}:
        connection.execute("ALTER TABLE projects ADD COLUMN queries INTEGER NOT NULL DEFAULT 0")
    connection.execute("UPDATE memories SET tier_since=? WHERE tier_since IS NULL", (_now(),))
    connection.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
                       (str(SCHEMA_VERSION),))
    connection.commit()


def _migrate_v1_to_v2(connection: sqlite3.Connection) -> None:
    """Give every memory a uuid and repoint the tables that referenced its slug.

    The slug stays exactly as it was, so retrieval, relationships, and the FTS
    index are unchanged - what moves is which value the other tables join on.
    """
    rows = connection.execute("SELECT project_id, id FROM memories").fetchall()
    mapping = {(r[0], r[1]): str(uuid_module.uuid4()) for r in rows}

    connection.executescript("""
        ALTER TABLE memories RENAME TO memories_v1;
        DROP INDEX IF EXISTS memories_status;
        DROP INDEX IF EXISTS memories_created;
        CREATE TABLE memories (
            project_id        TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            uuid              TEXT NOT NULL,
            slug              TEXT NOT NULL,
            status            TEXT NOT NULL,
            description       TEXT NOT NULL,
            created           TEXT,
            last_validated    TEXT,
            created_from_task TEXT,
            area              TEXT,
            body              TEXT NOT NULL,
            PRIMARY KEY (project_id, uuid)
        );
        ALTER TABLE usage RENAME TO usage_v1;
        CREATE TABLE usage (
            project_id TEXT NOT NULL, memory_id TEXT NOT NULL, replica_id TEXT NOT NULL,
            surfaced INTEGER NOT NULL DEFAULT 0,
            surfaced_direct INTEGER NOT NULL DEFAULT 0,
            applied INTEGER NOT NULL DEFAULT 0,
            last_surfaced TEXT, last_applied TEXT,
            spread_bits INTEGER NOT NULL DEFAULT 0,
            spread_epoch INTEGER,
            PRIMARY KEY (project_id, memory_id, replica_id)
        );
    """)

    connection.executemany(
        "INSERT INTO memories(project_id, uuid, slug, status, description, created, "
        "last_validated, created_from_task, area, body) "
        "SELECT project_id, ?, id, status, description, created, last_validated, "
        "created_from_task, area, body FROM memories_v1 WHERE project_id=? AND id=?",
        [(new, project, old) for (project, old), new in mapping.items()],
    )

    for table, column in (("labels", "memory_id"), ("files", "memory_id"),
                          ("revisions", "memory_id"), ("memories_fts", "memory_id"),
                          ("edges", "src"), ("edges", "dst")):
        connection.executemany(
            f"UPDATE {table} SET {column}=? WHERE project_id=? AND {column}=?",
            [(new, project, old) for (project, old), new in mapping.items()],
        )

    # Historical counts belong to this replica - it is the only one that has
    # ever written to this database. surfaced_direct starts at 0 because the
    # split did not exist when those surfacings were recorded, and inventing a
    # value would put made-up evidence in front of the audit.
    replica = _ensure_replica_id(connection)
    connection.executemany(
        "INSERT INTO usage(project_id, memory_id, replica_id, surfaced, applied, "
        "last_surfaced, last_applied) SELECT project_id, ?, ?, surfaced, applied, "
        "last_surfaced, last_applied FROM usage_v1 WHERE project_id=? AND memory_id=?",
        [(new, replica, project, old) for (project, old), new in mapping.items()],
    )

    connection.executescript("DROP TABLE memories_v1; DROP TABLE usage_v1;")
    connection.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
                       (str(SCHEMA_VERSION),))
    connection.commit()


class SqliteMemoryStore:
    def __init__(self, database: Path | str, project: str, create: bool = True,
                 check_same_thread: bool = True) -> None:
        if not ID_RE.match(project):
            raise StoreError("Project id must be lowercase kebab-case.")
        self.project = project
        self.path = Path(database)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, check_same_thread=check_same_thread)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        _upgrade(self.connection)
        self.connection.executescript(SCHEMA)
        self.connection.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self.replica_id = _ensure_replica_id(self.connection)
        #: Who the current caller is, set by the transport for the duration of
        #: one request. None means a local write with no authenticated client.
        #:
        #: Thread-local, because one store object serves every request thread and
        #: this is the only per-request state on it. It used to be a plain
        #: attribute, correct only because the project lock happened to span the
        #: whole of handle(). The moment one call was allowed to run outside that
        #: lock, a request could read the identity another thread had left here -
        #: and be told the unread-message count belonging to a different client.
        #: Identity must not depend on a lock taken for something else.
        self._actor = threading.local()
        known = self.connection.execute(
            "SELECT 1 FROM projects WHERE id=?", (project,)
        ).fetchone()
        if known is None:
            if not create:
                # A served project must already exist. Auto-creating one turns a
                # typo in a client's URL into an empty store that looks like a
                # working store, which is worse than an error.
                existing = ", ".join(self.list_projects(self.connection)) or "(none)"
                # Close before raising: an unknown project is a normal thing for
                # a server to be asked for, and leaking a connection per bad
                # request would eventually exhaust handles.
                self.connection.close()
                raise StoreError(f"Unknown project: {project}. Existing projects: {existing}")
            self.connection.execute(
                "INSERT INTO projects(id, name, created) VALUES (?, ?, ?)", (project, project, _now())
            )
        self.connection.commit()

    @staticmethod
    def list_projects(connection: sqlite3.Connection) -> list[str]:
        try:
            return [row[0] for row in connection.execute("SELECT id FROM projects ORDER BY id")]
        except sqlite3.Error:
            return []

    @property
    def actor(self) -> dict[str, Any] | None:
        return getattr(self._actor, "value", None)

    @actor.setter
    def actor(self, value: dict[str, Any] | None) -> None:
        self._actor.value = value

    def close(self) -> None:
        self.connection.close()

    # ------------------------------------------------------------------ labels

    def list_labels(self) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT label, description FROM label_registry WHERE project_id=? ORDER BY label",
            (self.project,),
        ).fetchall()
        labels = {row["label"]: {"description": row["description"]} for row in rows}
        grouped: dict[str, dict[str, Any]] = {}
        for label, data in labels.items():
            grouped.setdefault(label.split(":", 1)[0], {})[label] = data
        return {"schema_version": SCHEMA_VERSION, "description": "", "labels": labels, "groups": grouped}

    def add_label(self, label: str, description: str) -> dict[str, Any]:
        normalized = label.strip().lower()
        if not LABEL_RE.match(normalized):
            raise StoreError("Label must use prefix:kebab-case format.")
        if not description or not description.strip():
            raise StoreError("Label description is required.")
        existing = self.connection.execute(
            "SELECT 1 FROM label_registry WHERE project_id=? AND label=?", (self.project, normalized)
        ).fetchone()
        if existing:
            raise StoreError(f"Label already exists: {normalized}")
        self.connection.execute(
            "INSERT INTO label_registry(project_id, label, description) VALUES (?,?,?)",
            (self.project, normalized, description.strip()),
        )
        self.connection.commit()
        return {"added": normalized}

    # ------------------------------------------------------------------- reads

    # The tools, the UI and the memory documents all speak slugs; every table
    # other than `memories` joins on uuid. These three translate at that seam,
    # and everything below the seam works in uuid.

    def _uuid_for(self, slug: str) -> str:
        row = self.connection.execute(
            "SELECT uuid FROM memories WHERE project_id=? AND slug=?", (self.project, slug)
        ).fetchone()
        if row is None:
            raise StoreError(f"Unknown memory id: {slug}")
        return row["uuid"]

    def _body_by_uuid(self, memory_uuid: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT body FROM memories WHERE project_id=? AND uuid=?", (self.project, memory_uuid)
        ).fetchone()
        if row is None:
            raise StoreError(f"Unknown memory uuid: {memory_uuid}")
        return json.loads(row["body"])

    def _slugs_for(self, uuids: Iterable[str]) -> dict[str, str]:
        ids = sorted(set(uuids))
        if not ids:
            return {}
        rows = self.connection.execute(
            f"SELECT uuid, slug FROM memories WHERE project_id=? "
            f"AND uuid IN ({','.join('?' * len(ids))})", (self.project, *ids),
        ).fetchall()
        return {row["uuid"]: row["slug"] for row in rows}

    def _live_slugs(self, uuids: Iterable[str]) -> dict[str, str]:
        """Slugs for the ones still in the working set.

        The graph walk can reach an archived memory through a live neighbour;
        leaving it out here is what keeps it from taking a result slot.
        """
        ids = sorted(set(uuids))
        if not ids:
            return {}
        rows = self.connection.execute(
            f"SELECT uuid, slug FROM memories WHERE project_id=? AND archived_at IS NULL "
            f"AND uuid IN ({','.join('?' * len(ids))})", (self.project, *ids),
        ).fetchall()
        return {row["uuid"]: row["slug"] for row in rows}

    def get_memory(self, memory_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT body FROM memories WHERE project_id=? AND slug=?", (self.project, memory_id)
        ).fetchone()
        if row is None:
            raise StoreError(f"Unknown memory id: {memory_id}")
        return json.loads(row["body"])

    def count(self) -> int:
        return self.connection.execute(
            "SELECT COUNT(*) AS n FROM memories WHERE project_id=?", (self.project,)
        ).fetchone()["n"]

    def load_memories(self) -> dict[str, dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT slug, body FROM memories WHERE project_id=? ORDER BY slug", (self.project,)
        ).fetchall()
        return {row["slug"]: json.loads(row["body"]) for row in rows}

    def search_memories(
        self,
        label_query: Any = None,
        status_filter: list[str] | str | None = None,
        text_query: str | None = None,
        limit: int | None = None,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        """Filter by label expression, status and substring.

        Label filtering happens in SQL; the label expression grammar is still
        evaluated in Python so that AND/OR/NOT keep working unchanged.
        """

        known = set(self.list_labels()["labels"])
        expression = LabelExpression(label_query, known)
        statuses = _normalize_status_filter(status_filter)
        needle = text_query.lower().strip() if text_query else ""

        sql = "SELECT slug, body FROM memories WHERE project_id=?"
        params: list[Any] = [self.project]
        if not include_archived:
            sql += " AND archived_at IS NULL"
        if statuses is not None:
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            params.extend(sorted(statuses))
        sql += " ORDER BY slug"

        matches: list[dict[str, Any]] = []
        for row in self.connection.execute(sql, params):
            memory = json.loads(row["body"])
            if not expression.matches(memory.get("labels") or []):
                continue
            entry = _light_record(memory)
            if needle and needle not in _entry_text(entry):
                continue
            matches.append(entry)
            if limit is not None and len(matches) >= limit:
                break
        return {"count": len(matches), "label_query_labels": sorted(expression.used_labels), "memories": matches}

    def recall(
        self,
        query: str = "",
        label_query: Any = None,
        related_to: str | None = None,
        before: str | None = None,
        after: str | None = None,
        status_filter: list[str] | str | None = None,
        limit: int = 8,
        offset: int = 0,
        full_count: int = 3,
        include_derived: bool = True,
        order: str = "relevance",
        record: bool = True,
    ) -> dict[str, Any]:
        """Ranked retrieval that never loads the project.

        Text scoring is FTS5; the graph term is a walk bounded to the region
        around the seeds. Only the memories that actually score are read in
        full, so cost tracks the size of the answer rather than the store.
        """
        from .ranking import personalized_pagerank

        if limit < 1:
            raise StoreError("limit must be >= 1.")
        if offset < 0:
            raise StoreError("offset must be >= 0.")
        if full_count < 0:
            raise StoreError("full_count must be >= 0.")
        if order not in ("relevance", "recent"):
            raise StoreError("order must be 'relevance' or 'recent'.")
        if before and after:
            raise StoreError("Pass before or after, not both.")
        if (before or after) and order != "recent":
            raise StoreError("before/after anchors require order='recent'.")
        if related_to is not None:
            self.get_memory(related_to)
        known = set(self.list_labels()["labels"])
        expression = LabelExpression(label_query, known)
        statuses = _normalize_status_filter(status_filter)

        if order == "recent":
            statuses_for_walk = _normalize_status_filter(status_filter)
            bodies = self.timeline_window(before or after, forward=bool(after),
                                          limit=limit, offset=offset, statuses=statuses_for_walk)
            memories = [m for m in bodies if expression.matches(m.get("labels") or [])]
            results = []
            slugs_to_uuid = self._uuids_for_slugs([m["id"] for m in memories])
            for position, memory in enumerate(memories):
                entry = _light_record(memory)
                entry["uuid"] = slugs_to_uuid.get(memory["id"])
                entry["created"] = (memory.get("evidence") or {}).get("created")
                if position < full_count:
                    entry["memory"] = memory
                results.append(entry)
            # Browsing the timeline is retrieval by position, not by match, so
            # it never counts as direct: a memory should not earn its tier by
            # being adjacent in time to something someone was reading.
            if record:
                shown = self._uuids_for_slugs([r["id"] for r in results])
                usage.record(self, shown.values(), direct=())
            payload = {"order": "recent", "offset": offset,
                       "label_query_labels": sorted(expression.used_labels),
                       "count": len(results), "memories": results}
            if before:
                payload["before"] = before
            if after:
                payload["after"] = after
            return payload

        # One query = one chance for every memory in the project to be matched.
        # Only relevance mode counts: the timeline path above retrieves by
        # position, so counting it would age memories on exposure they never
        # actually got. `record=False` covers the management UI, where a human
        # inspecting the store must not change the metrics they are inspecting.
        if record:
            with self.connection:
                self.connection.execute(
                    "UPDATE projects SET queries=queries+1 WHERE id=?", (self.project,))

        related_uuid = self._uuid_for(related_to) if related_to else None
        text_scores = self.text_candidates(query) if query else {}
        seeds = dict(sorted(text_scores.items(), key=lambda i: -i[1])[:WALK_SEEDS])
        if related_uuid:
            seeds[related_uuid] = max(1.0, sum(seeds.values()))

        graph_scores: dict[str, float] = {}
        if seeds:
            adjacency = self.neighbourhood(seeds, depth=WALK_DEPTH if include_derived else 1)
            if adjacency:
                raw = personalized_pagerank(adjacency, {k: v for k, v in seeds.items() if k in adjacency} or None)
                top = max(raw.values()) or 1.0
                graph_scores = {k: v / top for k, v in raw.items()}

        graph_weight = 1.0 if related_to else 0.3
        combined: dict[str, float] = {}
        for memory_id in set(text_scores) | set(graph_scores):
            if memory_id == related_uuid:
                continue
            combined[memory_id] = text_scores.get(memory_id, 0.0) + graph_weight * graph_scores.get(memory_id, 0.0)

        if not combined and not query and not related_to:
            # No query at all: fall back to the most connected memories, which
            # is the "orient me in this store" answer.
            rows = self.connection.execute(
                "SELECT e.src AS uuid, COUNT(*) AS degree FROM edges e "
                "JOIN memories m ON m.project_id=e.project_id AND m.uuid=e.src "
                "WHERE e.project_id=? AND m.archived_at IS NULL "
                "GROUP BY e.src ORDER BY degree DESC LIMIT ?", (self.project, limit * 4),
            ).fetchall()
            combined = {row["uuid"]: float(row["degree"]) for row in rows}

        # Tie-break on the slug, not the uuid: which memories survive the limit
        # must not depend on a random identifier.
        slugs = self._live_slugs(combined)
        combined = {k: v for k, v in combined.items() if k in slugs}
        results: list[dict[str, Any]] = []
        surfaced: list[str] = []
        direct: list[str] = []
        for memory_id, score in sorted(combined.items(), key=lambda i: (-i[1], slugs.get(i[0], i[0]))):
            if score <= 0.0:
                continue
            try:
                memory = self._body_by_uuid(memory_id)
            except StoreError:
                continue
            if statuses is not None and memory.get("status") not in statuses:
                continue
            if not expression.matches(memory.get("labels") or []):
                continue
            factor = _STATUS_FACTORS.get(memory.get("status", "active"), 1.0)
            result = _light_record(memory)
            result["uuid"] = memory_id
            result["score"] = round(score * factor, 6)
            result["why"] = {
                "text": round(text_scores.get(memory_id, 0.0), 6),
                "graph": round(graph_scores.get(memory_id, 0.0), 6),
                "status_factor": factor,
            }
            if len(results) < full_count:
                result["memory"] = memory
            results.append(result)
            surfaced.append(memory_id)
            # Direct means the memory matched the query itself. Everything else
            # arrived through a neighbour, and riding a popular neighbour's
            # traffic is not evidence that this memory is worth keeping.
            if text_scores.get(memory_id, 0.0) > 0.0:
                direct.append(memory_id)
            if len(results) >= limit:
                break
        results.sort(key=lambda r: (-r["score"], r["id"]))
        if record:
            usage.record(self, surfaced, direct=direct)
        payload: dict[str, Any] = {
            "query": query,
            "label_query_labels": sorted(expression.used_labels),
            "considered": len(combined),
            "count": len(results),
            "memories": results,
        }
        if related_to:
            payload["related_to"] = related_to
        return payload

    def text_candidates(self, query: str, limit: int = 50) -> dict[str, float]:
        """Best text matches by FTS5 bm25, normalized to 0..1, best first.

        bm25() returns negative numbers where more negative is better, so the
        sign is flipped before normalizing.
        """
        match = _fts_query(query)
        if not match:
            return {}
        weights = ",".join(str(w) for w in FTS_WEIGHTS)
        # Archived memories are excluded here rather than filtered out of the
        # results, so they never consume a candidate slot that a live memory
        # could have used.
        rows = self.connection.execute(
            f"SELECT f.memory_id AS memory_id, -bm25(memories_fts, {weights}) AS score "
            f"FROM memories_fts f JOIN memories m "
            f"  ON m.project_id=f.project_id AND m.uuid=f.memory_id "
            f"WHERE memories_fts MATCH ? AND f.project_id = ? AND m.archived_at IS NULL "
            f"ORDER BY score DESC LIMIT ?",
            (match, self.project, limit),
        ).fetchall()
        if not rows:
            return {}
        best = max(row["score"] for row in rows) or 1.0
        return {row["memory_id"]: max(0.0, row["score"] / best) for row in rows}

    def neighbourhood(
        self, seeds: Iterable[str], depth: int = WALK_DEPTH, max_nodes: int = WALK_MAX_NODES
    ) -> dict[str, dict[str, float]]:
        """Weighted adjacency for the region of the graph around ``seeds``.

        Expands breadth-first through the edges table and stops at ``depth`` or
        ``max_nodes``. This is what keeps ranking independent of store size: a
        walk over the whole graph would have to load every edge, while the mass
        that actually reaches a result lives a few hops from the seeds.
        """
        frontier = list(dict.fromkeys(seeds))[:WALK_FRONTIER]
        seen: set[str] = set(frontier)
        adjacency: dict[str, dict[str, float]] = {node: {} for node in frontier}
        for _ in range(max(0, depth)):
            if not frontier or len(seen) >= max_nodes:
                break
            placeholders = ",".join("?" * len(frontier))
            # LIMIT matters: a derived-edge hub can have thousands of incoming
            # edges, and pulling them all is how a "bounded" walk stops being
            # bounded. Cap what any one level may contribute.
            rows = self.connection.execute(
                f"SELECT src, dst, kind FROM edges WHERE project_id=? "
                f"AND (src IN ({placeholders}) OR dst IN ({placeholders})) LIMIT ?",
                (self.project, *frontier, *frontier, max_nodes * 4),
            ).fetchall()
            nxt: list[str] = []
            for row in rows:
                weight = 1.0 if row["kind"] != "derived" else 0.5
                for a, b in ((row["src"], row["dst"]), (row["dst"], row["src"])):
                    adjacency.setdefault(a, {})[b] = max(adjacency.get(a, {}).get(b, 0.0), weight)
                    adjacency.setdefault(b, {})
                    if b not in seen and len(seen) < max_nodes:
                        seen.add(b)
                        nxt.append(b)
            frontier = nxt[:WALK_FRONTIER]
        # Drop dangling targets that were never expanded, so every node in the
        # walk has its edges represented rather than a truncated view.
        return {node: {n: w for n, w in edges.items() if n in adjacency} for node, edges in adjacency.items()}

    def get_memory_neighborhood(self, memory_id: str, depth: int = 1, max_nodes: int = 25) -> dict[str, Any]:
        """Bounded relationship graph around one memory.

        Walks the edges table breadth-first. Derived edges are excluded: this
        answers "what did somebody link to this", and computed similarity would
        drown the authored links it exists to show.
        """
        if depth < 0:
            raise StoreError("depth must be >= 0.")
        if max_nodes < 1:
            raise StoreError("max_nodes must be >= 1.")
        root_uuid = self._uuid_for(memory_id)  # raises on unknown id

        nodes: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, Any]] = []
        seen_edges: set[tuple[str, str, str]] = set()
        frontier = [root_uuid]
        for level in range(depth + 1):
            if not frontier or len(nodes) >= max_nodes:
                break
            for node in frontier:
                if node not in nodes and len(nodes) < max_nodes:
                    nodes[node] = _light_record(self._body_by_uuid(node))
            if level == depth:
                break
            placeholders = ",".join("?" * len(frontier))
            rows = self.connection.execute(
                f"SELECT src, dst, kind, reason FROM edges WHERE project_id=? AND kind<>'derived' "
                f"AND src IN ({placeholders})",
                (self.project, *frontier),
            ).fetchall()
            nxt: list[str] = []
            for row in rows:
                key = (row["kind"], row["src"], row["dst"])
                if key not in seen_edges:
                    seen_edges.add(key)
                    edges.append({"type": row["kind"], "from": row["src"], "to": row["dst"],
                                  "reason": row["reason"]})
                if row["dst"] not in nodes and row["dst"] not in nxt and len(nodes) + len(nxt) < max_nodes:
                    nxt.append(row["dst"])
            frontier = nxt
        # The graph is walked in uuid space; callers only ever see slugs.
        slugs = self._slugs_for(list(nodes) + [e[end] for e in edges for end in ("from", "to")])
        rendered = []
        for edge in edges:
            entry = {"type": edge["type"], "from": slugs.get(edge["from"], edge["from"]),
                     "to": slugs.get(edge["to"], edge["to"])}
            if edge["reason"]:
                entry["reason"] = edge["reason"]
            rendered.append(entry)
        return {"root": memory_id, "depth": depth, "nodes": list(nodes.values()), "edges": rendered}

    def timeline_window(
        self,
        anchor: str | None,
        forward: bool,
        limit: int,
        offset: int,
        statuses: set[str] | None,
    ) -> list[dict[str, Any]]:
        """Memories adjacent to ``anchor`` in creation order, nearest first.

        Indexed range scan rather than a sort: the anchor's timestamp bounds the
        query, so cost tracks the window rather than the store.
        """
        params: list[Any] = [self.project]
        where = "project_id=? AND archived_at IS NULL"
        if anchor is not None:
            row = self.connection.execute(
                "SELECT created, slug FROM memories WHERE project_id=? AND slug=?", (self.project, anchor)
            ).fetchone()
            if row is None:
                raise StoreError(f"Unknown memory id: {anchor}")
            comparison = ">" if forward else "<"
            where += f" AND (created, slug) {comparison} (?, ?)"
            params += [row["created"], row["slug"]]
        if statuses is not None:
            where += f" AND status IN ({','.join('?' * len(statuses))})"
            params += sorted(statuses)
        direction = "ASC" if forward else "DESC"
        params += [limit, offset]
        rows = self.connection.execute(
            f"SELECT body FROM memories WHERE {where} "
            f"ORDER BY created {direction}, slug {direction} LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        return [json.loads(row["body"]) for row in rows]

    def recent(self, limit: int = 8, offset: int = 0) -> dict[str, Any]:
        """Newest first, straight out of an index - no ranking involved."""
        rows = self.connection.execute(
            # Order by the indexed column directly: COALESCE(created, ...) is
            # not sargable and turned this into a full scan and sort. Writes and
            # migration both populate `created`, so the fallback is not needed.
            "SELECT body FROM memories WHERE project_id=? AND archived_at IS NULL "
            "ORDER BY created DESC, slug DESC LIMIT ? OFFSET ?",
            (self.project, limit, offset),
        ).fetchall()
        memories = [_light_record(json.loads(row["body"])) for row in rows]
        return {"order": "recent", "offset": offset, "count": len(memories), "memories": memories}

    # --------------------------------------------------------------- mutations

    def create_memory(self, memory: dict[str, Any], related_label_query: Any = None,
                      visibility: str | None = None, uuid: str | None = None) -> dict[str, Any]:
        evidence = memory.setdefault("evidence", {})
        if isinstance(evidence, dict) and not evidence.get("created"):
            evidence["created"] = _now()
        self._require_valid(memory)
        memory_id = memory["id"]
        if self.connection.execute(
            "SELECT 1 FROM memories WHERE project_id=? AND slug=?", (self.project, memory_id)
        ).fetchone():
            raise StoreError(f"Memory already exists: {memory_id}")
        with self.connection:
            # A promotion carries the memory's uuid so the same lesson keeps one
            # identity on every server that holds it. That is what makes results
            # from different sources fuse instead of duplicating.
            self._write(memory, uuid or str(uuid_module.uuid4()), visibility=visibility)
            self._synchronize_relationships(memory_id)
        return {"created": memory_id, "visibility": visibility or "private",
                "related_candidates": []}

    def update_memory(self, memory_id: str, patch: dict[str, Any], related_label_query: Any = None) -> dict[str, Any]:
        if "id" in patch and patch["id"] != memory_id:
            raise StoreError("update_memory cannot change a memory id.")
        memory_uuid = self._uuid_for(memory_id)
        current = self._body_by_uuid(memory_uuid)
        merged = _deep_merge(current, patch)
        self._require_valid(merged)
        with self.connection:
            self.connection.execute(
                "INSERT INTO revisions(project_id, memory_id, revised_at, body, author_client, author_name) "
                "VALUES (?,?,?,?,?,?)",
                (self.project, memory_uuid, _now(), json.dumps(current),
                 (self.actor or {}).get("client_id"), (self.actor or {}).get("name")),
            )
            self._write(merged, memory_uuid, visibility=self._visibility_of(memory_uuid))
            self._synchronize_relationships(memory_id)
        return {"updated": memory_id, "related_candidates": []}

    def set_root_path(self, root: str | None) -> dict[str, Any]:
        """Tell this project where its code is, so anchors can be checked."""
        with self.connection:
            self.connection.execute("UPDATE projects SET root_path=? WHERE id=?",
                                    (str(root) if root else None, self.project))
        return {"project": self.project, "root_path": root}

    def root_path(self) -> str | None:
        row = self.connection.execute(
            "SELECT root_path FROM projects WHERE id=?", (self.project,)).fetchone()
        return row["root_path"] if row else None

    def rebuild_derived_edges(self, limit: int = 500, offset: int = 0) -> dict[str, Any]:
        """Recompute similarity neighbours for a slice of the project.

        On a write only the new memory's edges are computed, which is right for
        a write but leaves the graph reflecting whichever threshold happened to
        be in force when each memory was stored. This rebuilds them, so changing
        the threshold means something.

        Sliced rather than wholesale: the caller runs it in bounded chunks and
        hands the lock back between them, so a large project does not stop being
        served while its graph is recomputed.
        """
        rows = self.connection.execute(
            "SELECT uuid, body FROM memories WHERE project_id=? AND archived_at IS NULL "
            "AND origin_remote IS NULL LIMIT ? OFFSET ?",
            (self.project, limit, offset)).fetchall()
        for row in rows:
            with self.connection:
                self._materialize_derived_edges(json.loads(row["body"]), row["uuid"])
        remaining = self.connection.execute(
            "SELECT COUNT(*) AS n FROM memories WHERE project_id=? AND archived_at IS NULL "
            "AND origin_remote IS NULL", (self.project,)).fetchone()["n"] - (offset + len(rows))
        return {"rebuilt": len(rows), "next_offset": offset + len(rows),
                "remaining": max(0, remaining)}

    def _visibility_of(self, memory_uuid: str) -> str:
        row = self.connection.execute(
            "SELECT visibility FROM memories WHERE project_id=? AND uuid=?",
            (self.project, memory_uuid)).fetchone()
        return row["visibility"] if row else "private"

    def set_visibility(self, memory_id: str, visibility: str) -> dict[str, Any]:
        """Change who a memory is for. Cannot un-publish what a remote already has."""
        if visibility not in ("private", "public"):
            raise StoreError("visibility must be 'private' or 'public'.")
        memory_uuid = self._uuid_for(memory_id)
        with self.connection:
            self.connection.execute(
                "UPDATE memories SET visibility=? WHERE project_id=? AND uuid=? "
                "AND origin_remote IS NULL", (visibility, self.project, memory_uuid))
        return {"id": memory_id, "visibility": visibility}

    def archive_memory(self, memory_id: str, archived: bool = True) -> dict[str, Any]:
        """Take a memory out of the working set, or put it back.

        Reversible by design: this is what the audit does instead of deleting,
        so that acting on thin evidence costs a recoverable mistake rather than
        a permanent one. The memory keeps its content, its links and its
        counters - it simply stops competing for a place in results.
        """
        memory_uuid = self._uuid_for(memory_id)
        with self.connection:
            self.connection.execute(
                "UPDATE memories SET archived_at=? WHERE project_id=? AND uuid=?",
                (_now() if archived else None, self.project, memory_uuid),
            )
        return {"archived" if archived else "restored": memory_id}

    def archived(self, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """Newest archives first. The recovery path once something leaves the pool."""
        rows = self.connection.execute(
            "SELECT body, archived_at FROM memories WHERE project_id=? AND archived_at IS NOT NULL "
            "ORDER BY archived_at DESC, slug DESC LIMIT ? OFFSET ?",
            (self.project, limit, offset),
        ).fetchall()
        memories = []
        for row in rows:
            entry = _light_record(json.loads(row["body"]))
            entry["archived_at"] = row["archived_at"]
            memories.append(entry)
        return {"count": len(memories), "offset": offset, "memories": memories}

    # ------------------------------------------------------------------ messages

    # ---------------------------------------------------------------- federation

    def publication_state(self, memory_id: str) -> dict[str, Any]:
        """The facts publication depends on, in one read.

        Federation decides whether a memory may leave; those decisions are about
        stored state, so the store answers them. One method rather than four
        accessors because they are always wanted together, and because a caller
        that has to make four calls will eventually make three.
        """
        memory_uuid = self._uuid_for(memory_id)
        row = self.connection.execute(
            "SELECT tier, visibility, origin_remote FROM memories "
            "WHERE project_id=? AND uuid=?", (self.project, memory_uuid)).fetchone()
        if row is None:
            raise StoreError(f"Unknown memory: {memory_id}")
        return {"uuid": memory_uuid, "tier": row["tier"], "visibility": row["visibility"],
                # A cached copy belongs to the server it came from. Publishing it
                # onward would make this machine a source for something it only
                # borrowed.
                "borrowed": row["origin_remote"] is not None}

    def cache_remote_results(self, answers: dict[str, Any]) -> None:
        """Keep what came back, tagged with where it came from.

        The cache is the working set: whatever has recently been needed stays
        reachable when a remote is not. Cached rows are never audited and never
        promoted - this machine does not own them.
        """
        rows = []
        stamp = _now()
        for name, payload in answers.items():
            for entry in payload.get("memories") or []:
                body = entry.get("memory") or entry
                uuid = entry.get("uuid")
                if not uuid or not body.get("id"):
                    continue
                rows.append((self.project, uuid, body["id"], body.get("status", "active"),
                             body.get("description", ""), stamp, json.dumps(body), name))
        if not rows:
            return
        with self.connection:
            self.connection.executemany(
                "INSERT INTO memories(project_id, uuid, slug, status, description, created, "
                "body, origin_remote, visibility) VALUES (?,?,?,?,?,?,?,?,'public') "
                "ON CONFLICT(project_id, uuid) DO UPDATE SET status=excluded.status, "
                "description=excluded.description, body=excluded.body, "
                "origin_remote=excluded.origin_remote",
                rows)

    def merge_memories(self, keep_id: str, merge_id: str, reason: str) -> dict[str, Any]:
        """Fold one memory into another, keeping everything both knew.

        Counters add rather than being picked between: both were evidence about
        the same underlying fact, so the combined count is the more accurate
        number. The merged memory is archived with a pointer rather than
        deleted, because a merge is a judgment and judgments are sometimes wrong.
        """
        if keep_id == merge_id:
            raise StoreError("A memory cannot be merged into itself.")
        if not reason or not reason.strip():
            raise StoreError("A merge needs a reason: it is a judgment, not a computation.")
        keep_uuid, merge_uuid = self._uuid_for(keep_id), self._uuid_for(merge_id)
        keep, merged = self._body_by_uuid(keep_uuid), self._body_by_uuid(merge_uuid)

        for field in ("tags", "labels", "triggers", "remembered_facts", "solution_pattern", "pitfalls"):
            seen = list(keep.get(field) or [])
            seen += [v for v in (merged.get(field) or []) if v not in seen]
            keep[field] = seen
        scope = keep.setdefault("scope", {})
        for field in ("files", "applies_to"):
            seen = list(scope.get(field) or [])
            seen += [v for v in ((merged.get("scope") or {}).get(field) or []) if v not in seen]
            scope[field] = seen
        relationships = keep.setdefault("relationships", {})
        related = list(relationships.get("related") or [])
        known = {e.get("id") for e in related} | {keep_id, merge_id}
        related += [e for e in (merged.get("relationships") or {}).get("related") or []
                    if isinstance(e, dict) and e.get("id") not in known]
        relationships["related"] = related

        with self.connection:
            self.connection.execute(
                "INSERT INTO revisions(project_id, memory_id, revised_at, body) VALUES (?,?,?,?)",
                (self.project, merge_uuid, _now(), json.dumps(merged)))
            # Both counters were evidence about one fact, so they add.
            self.connection.execute(
                "INSERT INTO usage(project_id, memory_id, replica_id, surfaced, surfaced_direct, "
                "applied, last_surfaced, last_applied, spread_bits, spread_epoch) "
                "SELECT ?, ?, replica_id, surfaced, surfaced_direct, applied, last_surfaced, "
                "last_applied, spread_bits, spread_epoch FROM usage "
                "WHERE project_id=? AND memory_id=? "
                "ON CONFLICT(project_id, memory_id, replica_id) DO UPDATE SET "
                "surfaced=surfaced+excluded.surfaced, "
                "surfaced_direct=surfaced_direct+excluded.surfaced_direct, "
                "applied=applied+excluded.applied",
                (self.project, keep_uuid, self.project, merge_uuid))
            self.connection.execute(
                "UPDATE memories SET archived_at=?, merged_into=? WHERE project_id=? AND uuid=?",
                (_now(), keep_uuid, self.project, merge_uuid))
            self._write(keep, keep_uuid, visibility=self._visibility_of(keep_uuid))

        self._synchronize_relationships(keep_id)
        return {"kept": keep_id, "merged": merge_id, "reason": reason}

    def delete_memory(self, memory_id: str, confirm_exact_id: str) -> dict[str, Any]:
        if confirm_exact_id != memory_id:
            raise StoreError("confirm_exact_id must exactly match id.")
        memory_uuid = self._uuid_for(memory_id)
        body = self._body_by_uuid(memory_uuid)
        touched: list[str] = []
        with self.connection:
            self.connection.execute(
                "INSERT INTO revisions(project_id, memory_id, revised_at, body, author_client, author_name) "
                "VALUES (?,?,?,?,?,?)",
                (self.project, memory_uuid, _now(), json.dumps(body),
                 (self.actor or {}).get("client_id"), (self.actor or {}).get("name")),
            )
            referrers = self.connection.execute(
                "SELECT DISTINCT src FROM edges WHERE project_id=? AND dst=?", (self.project, memory_uuid)
            ).fetchall()
            for row in referrers:
                other_uuid = row["src"]
                other = self._body_by_uuid(other_uuid)
                rel = other["relationships"]
                rel["related"] = [e for e in rel["related"] if e.get("id") != memory_id]
                for field in ("supersedes", "superseded_by"):
                    rel[field] = [v for v in rel[field] if v != memory_id]
                self._write(other, other_uuid, visibility=self._visibility_of(other_uuid))
                touched.append(other["id"])
            # `outbox` is in this list because a queued promotion outlives the
            # memory otherwise, and the row that is left cannot be delivered or
            # skipped: its LEFT JOIN yields a null slug, delivery raises on it,
            # and because it sorts first it takes every promotion behind it down
            # with it, on every run, silently. You cannot publish what you just
            # deleted, so the queue entry goes with the memory.
            cancelled = self.connection.execute(
                "SELECT COUNT(*) AS n FROM outbox WHERE project_id=? AND memory_id=?",
                (self.project, memory_uuid)).fetchone()["n"]
            for table in ("memories", "labels", "files", "usage", "memories_fts", "outbox"):
                self.connection.execute(
                    f"DELETE FROM {table} WHERE project_id=? AND "
                    f"{'uuid' if table == 'memories' else 'memory_id'}=?",
                    (self.project, memory_uuid),
                )
            self.connection.execute(
                "DELETE FROM edges WHERE project_id=? AND (src=? OR dst=?)",
                (self.project, memory_uuid, memory_uuid),
            )
        result = {"deleted": memory_id, "cleaned_references_in": sorted(set(touched))}
        if cancelled:
            # Reported rather than done quietly: whoever deleted this may not
            # have known it was waiting to be published somewhere.
            result["cancelled_promotions"] = cancelled
        return result

    # ------------------------------------------------------------------- usage

    # ------------------------------------------------------------------- usage
    #
    # The arithmetic lives in usage.py - grow-only per-replica counters and the
    # spread bitmap are their own idea with their own merge rules. What stays
    # here is the boundary these two cross: callers speak slugs, the counters are
    # keyed by uuid, and translating between them is the store's job.

    def load_usage(self) -> dict[str, Any]:
        return {"schema_version": SCHEMA_VERSION, **usage.load(self)}

    def record_use(self, memory_ids: list[str]) -> dict[str, Any]:
        if not isinstance(memory_ids, list) or not all(isinstance(i, str) for i in memory_ids):
            raise StoreError("memory_ids must be an array of strings.")
        uuids = [self._uuid_for(memory_id) for memory_id in memory_ids]  # raises on unknown id
        usage.record(self, (), applied=uuids)
        return {"recorded": sorted(set(memory_ids)), "field": "applied"}

    def validate_memory(self, memory: Any, known_labels: set[str] | None, where: str) -> list[str]:
        return validation.validate_memory(memory, known_labels, where)

    def validate_store(self) -> list[str]:
        """Validate every memory and the relationship graph.

        Unlike the file backend this is only needed on demand: writes validate
        the single memory they touch, not the whole project.
        """
        errors: list[str] = []
        known = set(self.list_labels()["labels"])
        memories = self.load_memories()
        for memory_id, memory in memories.items():
            errors.extend(validation.validate_memory(memory, known, memory_id))
        for memory_id, memory in memories.items():
            for entry in (memory.get("relationships") or {}).get("related") or []:
                target = entry.get("id") if isinstance(entry, dict) else None
                if target not in memories:
                    errors.append(f"{memory_id}: relationships.related references unknown memory: {target}")
                    continue
                back = [e.get("id") for e in memories[target]["relationships"]["related"]]
                if memory_id not in back:
                    errors.append(f"{memory_id}: related '{target}' is not mirrored back.")
        return errors

    def _require_valid(self, memory: dict[str, Any]) -> None:
        known = set(self.list_labels()["labels"])
        where = memory.get("id", "memory") if isinstance(memory, dict) else "memory"
        errors = validation.validate_memory(memory, known, str(where))
        if errors:
            raise StoreError("Invalid memory:\n" + "\n".join(errors))

    # ---------------------------------------------------------------- internal

    def _write(self, memory: dict[str, Any], memory_uuid: str,
               visibility: str | None = None, origin_remote: str | None = None) -> None:
        slug = memory["id"]
        if visibility is None:
            visibility = memory.get("visibility") or "private"
        if visibility not in ("private", "public"):
            raise StoreError("visibility must be 'private' or 'public'.")
        evidence = memory.get("evidence") or {}
        scope = memory.get("scope") or {}
        self.connection.execute(
            "INSERT INTO memories(project_id, uuid, slug, status, description, created, last_validated, "
            "created_from_task, area, body, tier_since, tier_since_query, "
            "author_client, author_name, author_key, visibility, origin_remote) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,(SELECT queries FROM projects WHERE id=?),?,?,?,?,?) "
            "ON CONFLICT(project_id, uuid) DO UPDATE SET slug=excluded.slug, status=excluded.status, "
            "description=excluded.description, created=excluded.created, "
            "last_validated=excluded.last_validated, created_from_task=excluded.created_from_task, "
            "area=excluded.area, body=excluded.body, author_client=excluded.author_client, "
            "author_name=excluded.author_name, author_key=excluded.author_key, "
            "visibility=excluded.visibility, origin_remote=excluded.origin_remote",
            (self.project, memory_uuid, slug, memory["status"], memory["description"],
             evidence.get("created"), evidence.get("last_validated"),
             evidence.get("created_from_task"), scope.get("area"), json.dumps(memory),
             _now(), self.project,
             (self.actor or {}).get("client_id"), (self.actor or {}).get("name"),
             (self.actor or {}).get("fingerprint"), visibility, origin_remote),
        )
        self.connection.execute("DELETE FROM labels WHERE project_id=? AND memory_id=?",
                                (self.project, memory_uuid))
        self.connection.executemany(
            "INSERT OR IGNORE INTO labels(project_id, memory_id, label) VALUES (?,?,?)",
            [(self.project, memory_uuid, label) for label in memory.get("labels") or []],
        )
        self.connection.execute("DELETE FROM files WHERE project_id=? AND memory_id=?",
                                (self.project, memory_uuid))
        self.connection.executemany(
            "INSERT OR IGNORE INTO files(project_id, memory_id, path) VALUES (?,?,?)",
            [(self.project, memory_uuid, path) for path in (memory.get("scope") or {}).get("files") or []],
        )
        self.connection.execute("DELETE FROM edges WHERE project_id=? AND kind<>'derived' AND src=?",
                                (self.project, memory_uuid))
        # Bodies reference their targets by slug; edges join on uuid. A target
        # that does not exist yet is simply not edged - validate_store reads the
        # bodies, so a dangling reference is still reported there.
        relationships = memory.get("relationships") or {}
        targets = [e["id"] for e in relationships.get("related") or [] if isinstance(e, dict)]
        targets += [t for kind in ("supersedes", "superseded_by") for t in relationships.get(kind) or []]
        resolved = self._uuids_for_slugs(targets)
        rows = [(self.project, memory_uuid, resolved[e["id"]], "related", e.get("reason"))
                for e in relationships.get("related") or []
                if isinstance(e, dict) and e["id"] in resolved]
        rows += [(self.project, memory_uuid, resolved[t], kind, None)
                 for kind in ("supersedes", "superseded_by")
                 for t in relationships.get(kind) or [] if t in resolved]
        self.connection.executemany(
            "INSERT OR REPLACE INTO edges(project_id, src, dst, kind, reason) VALUES (?,?,?,?,?)", rows)
        self._index_text(memory, memory_uuid)
        self._materialize_derived_edges(memory, memory_uuid)

    def _uuids_for_slugs(self, slugs: Iterable[str]) -> dict[str, str]:
        wanted = sorted({s for s in slugs if isinstance(s, str)})
        if not wanted:
            return {}
        rows = self.connection.execute(
            f"SELECT uuid, slug FROM memories WHERE project_id=? "
            f"AND slug IN ({','.join('?' * len(wanted))})", (self.project, *wanted),
        ).fetchall()
        return {row["slug"]: row["uuid"] for row in rows}

    def _index_text(self, memory: dict[str, Any], memory_uuid: str) -> None:
        """Refresh this memory's FTS row, identifiers already case-split."""
        memory_id = memory["id"]
        self.connection.execute(
            "DELETE FROM memories_fts WHERE project_id=? AND memory_id=?", (self.project, memory_uuid)
        )
        scope = memory.get("scope") or {}
        self.connection.execute(
            "INSERT INTO memories_fts(project_id, memory_id, id_text, description, triggers, tags, "
            "labels, facts, pattern, pitfalls, scope_text) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                self.project, memory_uuid,
                _expand(memory_id),
                _expand(memory.get("description") or ""),
                _expand(" ".join(memory.get("triggers") or [])),
                _expand(" ".join(memory.get("tags") or [])),
                " ".join(memory.get("labels") or []),
                _expand(" ".join(memory.get("remembered_facts") or [])),
                _expand(" ".join(memory.get("solution_pattern") or [])),
                _expand(" ".join(memory.get("pitfalls") or [])),
                _expand(" ".join([scope.get("area") or ""] + list(scope.get("files") or [])
                                 + list(scope.get("applies_to") or []))),
            ),
        )

    def _materialize_derived_edges(self, memory: dict[str, Any], memory_uuid: str) -> None:
        """Store this memory's strongest label/file neighbours as derived edges.

        The file backend recomputed these across every pair on each rebuild,
        which was O(N^2). Here three indexed queries find only the memories that
        share a label or a file, and just the strongest few are kept.

        Deliberately does not read candidate bodies: overlap is computed from
        the labels and files tables, so a write costs a handful of queries
        rather than one row read per candidate.
        """
        memory_id = memory_uuid
        self.connection.execute(
            "DELETE FROM edges WHERE project_id=? AND kind='derived' AND (src=? OR dst=?)",
            (self.project, memory_id, memory_id),
        )
        labels = set(memory.get("labels") or [])
        files = set((memory.get("scope") or {}).get("files") or [])
        if not labels and not files:
            return

        candidates: set[str] = set()
        if labels:
            rows = self.connection.execute(
                f"SELECT memory_id, COUNT(*) AS shared FROM labels WHERE project_id=? AND memory_id<>? "
                f"AND label IN ({','.join('?' * len(labels))}) GROUP BY memory_id "
                f"ORDER BY shared DESC LIMIT ?",
                (self.project, memory_id, *sorted(labels), DERIVED_CANDIDATE_LIMIT),
            ).fetchall()
            candidates.update(row["memory_id"] for row in rows)
        if files:
            rows = self.connection.execute(
                f"SELECT DISTINCT memory_id FROM files WHERE project_id=? AND memory_id<>? "
                f"AND path IN ({','.join('?' * len(files))}) LIMIT ?",
                (self.project, memory_id, *sorted(files), DERIVED_CANDIDATE_LIMIT),
            ).fetchall()
            candidates.update(row["memory_id"] for row in rows)
        if not candidates:
            return

        placeholders = ",".join("?" * len(candidates))
        ordered = sorted(candidates)
        other_labels: dict[str, set[str]] = {c: set() for c in ordered}
        for row in self.connection.execute(
            f"SELECT memory_id, label FROM labels WHERE project_id=? AND memory_id IN ({placeholders})",
            (self.project, *ordered),
        ):
            other_labels[row["memory_id"]].add(row["label"])
        other_files: dict[str, set[str]] = {c: set() for c in ordered}
        for row in self.connection.execute(
            f"SELECT memory_id, path FROM files WHERE project_id=? AND memory_id IN ({placeholders})",
            (self.project, *ordered),
        ):
            other_files[row["memory_id"]].add(row["path"])

        scored = []
        for other in ordered:
            similarity = 0.7 * _jaccard(labels, other_labels[other]) + 0.3 * _jaccard(files, other_files[other])
            if similarity >= DERIVED_THRESHOLD:
                scored.append((similarity, other))
        scored.sort(key=lambda item: (-item[0], item[1]))
        self.connection.executemany(
            "INSERT OR REPLACE INTO edges(project_id, src, dst, kind, reason) VALUES (?,?,?,'derived',NULL)",
            [(self.project, memory_id, other) for _, other in scored[:DERIVED_MAX_NEIGHBOURS]],
        )

    #: Edge kinds that mirror onto the other memory. Everything else - notably
    #: 'derived' - is computed from content and is nobody's reverse link.
    _MIRRORED_KINDS = ("related", "supersedes", "superseded_by")

    def _synchronize_relationships(self, memory_id: str) -> None:
        """Mirror this memory's links onto their targets, as the file backend does.

        Only two kinds of memory can possibly change: the ones this memory now
        points at, and the ones that currently point back at it - the latter
        because a link that has just been removed leaves a stale mirror that has
        to be cleaned up. Both are small sets, and the second is an indexed
        lookup because the edges table already records exactly that.

        This used to load and JSON-parse *every other memory in the project* on
        every write, to adjust a handful of links. That was 71% of the cost of a
        write and the reason write time grew with store size - which is to say,
        the reason building a store was quadratic.
        """
        current = self.get_memory(memory_id)
        memory_uuid = self._uuid_for(memory_id)
        related_map = {e["id"]: e["reason"] for e in current["relationships"]["related"]}
        for target_id in related_map:
            if not self.connection.execute(
                "SELECT 1 FROM memories WHERE project_id=? AND slug=?", (self.project, target_id)
            ).fetchone():
                raise StoreError(f"relationships.related references unknown memory: {target_id}")

        # Where this memory's links now point.
        targets = set(related_map)
        for field in ("supersedes", "superseded_by"):
            targets.update(current["relationships"].get(field) or [])
        affected = set(self._uuids_for_slugs(targets).values())
        # And whoever still points back at it, so a removed link is un-mirrored.
        # The memory's own edges were rewritten just before this, so these rows
        # are the old state - which is exactly what needs correcting.
        placeholders = ",".join("?" * len(self._MIRRORED_KINDS))
        affected.update(row["src"] for row in self.connection.execute(
            f"SELECT src FROM edges WHERE project_id=? AND dst=? AND kind IN ({placeholders})",
            (self.project, memory_uuid, *self._MIRRORED_KINDS)))
        affected.discard(memory_uuid)
        if not affected:
            return

        ordered = sorted(affected)
        for row in self.connection.execute(
            f"SELECT uuid, body FROM memories WHERE project_id=? "
            f"AND uuid IN ({','.join('?' * len(ordered))})", (self.project, *ordered)
        ).fetchall():
            other = json.loads(row["body"])
            before = json.dumps(other["relationships"], sort_keys=True)
            rel = other["relationships"]
            rel["related"] = [e for e in rel["related"] if e.get("id") != memory_id]
            if other["id"] in related_map:
                rel["related"].append({"id": memory_id, "reason": related_map[other["id"]]})
            for source, target in (("supersedes", "superseded_by"), ("superseded_by", "supersedes")):
                values = [v for v in rel[target] if v != memory_id]
                if other["id"] in current["relationships"][source]:
                    values.append(memory_id)
                rel[target] = values
            if json.dumps(rel, sort_keys=True) != before:
                self._write(other, row["uuid"], visibility=self._visibility_of(row["uuid"]))


def _expand(text: str) -> str:
    """Text plus case-split forms of any identifiers in it.

    `bReplicates` is indexed as itself and as `replicates`, so both an exact
    identifier search and a plain-English one find it. The file backend did
    this on every index rebuild; here it happens once, on write.
    """
    from .ranking import tokenize

    return " ".join(dict.fromkeys(tokenize(text))) if text else ""


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def _fts_query(text: str) -> str:
    """A safe FTS5 MATCH expression: quoted terms, OR-joined."""
    from .ranking import tokenize

    terms = [t for t in dict.fromkeys(tokenize(text)) if len(t) > 1]
    return " OR ".join('"' + t.replace('"', '') + '"' for t in terms)


def _normalize_status_filter(status_filter: list[str] | str | None) -> set[str] | None:
    if status_filter is None:
        return {"active", "stale"}
    if status_filter == "all":
        return None
    statuses = [status_filter] if isinstance(status_filter, str) else status_filter
    unknown = set(statuses) - validation.VALID_STATUSES
    if unknown:
        raise StoreError(f"Unknown statuses: {', '.join(sorted(unknown))}")
    return set(statuses)


def _light_record(memory: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": memory.get("id"),
        "status": memory.get("status"),
        "description": memory.get("description"),
        "labels": memory.get("labels", []),
        "tags": memory.get("tags", []),
        "triggers": memory.get("triggers", []),
    }


def _entry_text(entry: dict[str, Any]) -> str:
    return "\n".join([
        entry.get("id", ""), entry.get("description", ""),
        " ".join(entry.get("tags", [])), " ".join(entry.get("labels", [])),
        " ".join(entry.get("triggers", [])),
    ]).lower()


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = json.loads(json.dumps(base))
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


# `migrate_from_files` used to live here, importing the pre-0.4.0
# `.project-memory` file layout. It called `store._write(memory)` and had done
# since before schema v2 gave `_write` a required `memory_uuid`, so every
# invocation raised TypeError - for many releases, with no test covering it and
# the server's own startup banner recommending it.
#
# Deleted rather than repaired. The format is documented as dead in two other
# places, and an importer nobody has been able to run cannot be something anyone
# depends on. If a file store ever does surface, write the importer against it
# as a fixture; that beats keeping an untested one against a guess.
