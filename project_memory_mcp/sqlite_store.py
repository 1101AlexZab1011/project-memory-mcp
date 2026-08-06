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
import uuid as uuid_module
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import validation
from .validation import ID_RE, LABEL_RE, LabelExpression

SCHEMA_VERSION = 3

# 64-day window for the spread bitmap: long enough to judge the early tiers,
# and it fits one SQLite integer.
SPREAD_WINDOW_DAYS = 64
SPREAD_MASK = (1 << SPREAD_WINDOW_DAYS) - 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

-- `queries` counts relevance-mode recalls. It is the unit the audit measures
-- exposure in: a project nobody queried for six weeks has served no chances to
-- be seen, so its memories are not "unused" - they were never asked.
CREATE TABLE IF NOT EXISTS projects (
    id      TEXT PRIMARY KEY,
    name    TEXT NOT NULL,
    created TEXT NOT NULL,
    queries INTEGER NOT NULL DEFAULT 0
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
    PRIMARY KEY (project_id, uuid)
);
CREATE INDEX IF NOT EXISTS memories_tier ON memories(project_id, tier);
-- Unique for now: one writer, so a duplicate slug is a mistake rather than a
-- merge. Relaxing this is a sync concern, and dropping an index is cheap.
CREATE UNIQUE INDEX IF NOT EXISTS memories_slug ON memories(project_id, slug);
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
    revised_at TEXT NOT NULL, body TEXT NOT NULL
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


def _today() -> int:
    """Days since the epoch, UTC. The unit of the spread bitmap."""
    return datetime.now(timezone.utc).toordinal()


def _as_signed(bits: int) -> int:
    """SQLite integers are signed 64-bit; bit 63 set would overflow on write."""
    bits &= SPREAD_MASK
    return bits - (1 << SPREAD_WINDOW_DAYS) if bits >> (SPREAD_WINDOW_DAYS - 1) else bits


def _touch_spread(bits: int, epoch: int | None, today: int) -> tuple[int, int]:
    """Set today's bit in a 64-day rolling window, newest day in bit 0.

    Spread - how many distinct days a memory was recalled on - says something
    that a rate does not: thirty hits in one afternoon is a burst, while one hit
    a month for a year is a memory that keeps proving itself.
    """
    bits &= SPREAD_MASK
    if epoch is None:
        return _as_signed(1), today
    delta = today - epoch
    if delta == 0:
        return _as_signed(bits | 1), epoch
    if delta < 0:
        # A replica's clock ran behind, or rows merged out of order. Record the
        # day in its own slot rather than rolling the window backwards.
        back = -delta
        if back < SPREAD_WINDOW_DAYS:
            bits |= 1 << back
        return _as_signed(bits), epoch
    if delta >= SPREAD_WINDOW_DAYS:
        return _as_signed(1), today
    return _as_signed(((bits << delta) & SPREAD_MASK) | 1), today


def _merge_spread(bits_a: int, epoch_a: int | None,
                  bits_b: int, epoch_b: int | None) -> tuple[int, int | None]:
    """Combine two replicas' windows by aligning them and OR-ing.

    Two machines recalling a memory on the same day is one day of spread, which
    is exactly what OR gives and what summing would not.
    """
    if epoch_a is None:
        return bits_b & SPREAD_MASK, epoch_b
    if epoch_b is None:
        return bits_a & SPREAD_MASK, epoch_a
    newest = max(epoch_a, epoch_b)
    merged = 0
    for bits, epoch in ((bits_a, epoch_a), (bits_b, epoch_b)):
        shift = newest - epoch
        if shift < SPREAD_WINDOW_DAYS:
            merged |= ((bits & SPREAD_MASK) << shift) & SPREAD_MASK
    return merged, newest


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
            for position, memory in enumerate(memories):
                entry = _light_record(memory)
                entry["created"] = (memory.get("evidence") or {}).get("created")
                if position < full_count:
                    entry["memory"] = memory
                results.append(entry)
            # Browsing the timeline is retrieval by position, not by match, so
            # it never counts as direct: a memory should not earn its tier by
            # being adjacent in time to something someone was reading.
            if record:
                shown = self._uuids_for_slugs([r["id"] for r in results])
                self._record_usage(shown.values(), direct=())
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
                "SELECT src AS uuid, COUNT(*) AS degree FROM edges WHERE project_id=? "
                "GROUP BY src ORDER BY degree DESC LIMIT ?", (self.project, limit * 4),
            ).fetchall()
            combined = {row["uuid"]: float(row["degree"]) for row in rows}

        # Tie-break on the slug, not the uuid: which memories survive the limit
        # must not depend on a random identifier.
        slugs = self._slugs_for(combined)
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
            self._record_usage(surfaced, direct=direct)
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
        rows = self.connection.execute(
            f"SELECT memory_id, -bm25(memories_fts, {weights}) AS score FROM memories_fts "
            f"WHERE memories_fts MATCH ? AND project_id = ? ORDER BY score DESC LIMIT ?",
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
        where = "project_id=?"
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
            "SELECT body FROM memories WHERE project_id=? "
            "ORDER BY created DESC, slug DESC LIMIT ? OFFSET ?",
            (self.project, limit, offset),
        ).fetchall()
        memories = [_light_record(json.loads(row["body"])) for row in rows]
        return {"order": "recent", "offset": offset, "count": len(memories), "memories": memories}

    # --------------------------------------------------------------- mutations

    def create_memory(self, memory: dict[str, Any], related_label_query: Any = None) -> dict[str, Any]:
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
            self._write(memory, str(uuid_module.uuid4()))
            self._synchronize_relationships(memory_id)
        return {"created": memory_id, "related_candidates": []}

    def update_memory(self, memory_id: str, patch: dict[str, Any], related_label_query: Any = None) -> dict[str, Any]:
        if "id" in patch and patch["id"] != memory_id:
            raise StoreError("update_memory cannot change a memory id.")
        memory_uuid = self._uuid_for(memory_id)
        current = self._body_by_uuid(memory_uuid)
        merged = _deep_merge(current, patch)
        self._require_valid(merged)
        with self.connection:
            self.connection.execute(
                "INSERT INTO revisions(project_id, memory_id, revised_at, body) VALUES (?,?,?,?)",
                (self.project, memory_uuid, _now(), json.dumps(current)),
            )
            self._write(merged, memory_uuid)
            self._synchronize_relationships(memory_id)
        return {"updated": memory_id, "related_candidates": []}

    def delete_memory(self, memory_id: str, confirm_exact_id: str) -> dict[str, Any]:
        if confirm_exact_id != memory_id:
            raise StoreError("confirm_exact_id must exactly match id.")
        memory_uuid = self._uuid_for(memory_id)
        body = self._body_by_uuid(memory_uuid)
        touched: list[str] = []
        with self.connection:
            self.connection.execute(
                "INSERT INTO revisions(project_id, memory_id, revised_at, body) VALUES (?,?,?,?)",
                (self.project, memory_uuid, _now(), json.dumps(body)),
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
                self._write(other, other_uuid)
                touched.append(other["id"])
            for table in ("memories", "labels", "files", "usage", "memories_fts"):
                self.connection.execute(
                    f"DELETE FROM {table} WHERE project_id=? AND "
                    f"{'uuid' if table == 'memories' else 'memory_id'}=?",
                    (self.project, memory_uuid),
                )
            self.connection.execute(
                "DELETE FROM edges WHERE project_id=? AND (src=? OR dst=?)",
                (self.project, memory_uuid, memory_uuid),
            )
        return {"deleted": memory_id, "cleaned_references_in": sorted(set(touched))}

    # ------------------------------------------------------------------- usage

    def load_usage(self) -> dict[str, Any]:
        """Counters per memory, summed across every replica that reported one.

        Grow-only counters from independent replicas add; the spread bitmaps
        OR, because two machines using a memory on the same day is one day of
        spread, not two.
        """
        rows = self.connection.execute(
            "SELECT m.slug AS slug, u.surfaced AS surfaced, u.surfaced_direct AS surfaced_direct, "
            "u.applied AS applied, u.last_surfaced AS last_surfaced, u.last_applied AS last_applied, "
            "u.spread_bits AS spread_bits, u.spread_epoch AS spread_epoch "
            "FROM usage u JOIN memories m ON m.project_id=u.project_id AND m.uuid=u.memory_id "
            "WHERE u.project_id=?", (self.project,)
        ).fetchall()
        merged: dict[str, dict[str, Any]] = {}
        for row in rows:
            entry = merged.setdefault(row["slug"], {
                "surfaced": 0, "surfaced_direct": 0, "applied": 0,
                "last_surfaced": None, "last_applied": None, "_bits": 0, "_epoch": None,
            })
            entry["surfaced"] += row["surfaced"]
            entry["surfaced_direct"] += row["surfaced_direct"]
            entry["applied"] += row["applied"]
            for field in ("last_surfaced", "last_applied"):
                if row[field] and (entry[field] is None or row[field] > entry[field]):
                    entry[field] = row[field]
            entry["_bits"], entry["_epoch"] = _merge_spread(
                entry["_bits"], entry["_epoch"], row["spread_bits"], row["spread_epoch"])
        for entry in merged.values():
            entry["spread_days"] = bin(entry.pop("_bits") & SPREAD_MASK).count("1")
            entry.pop("_epoch")
        return {"schema_version": SCHEMA_VERSION, "memories": merged}

    def record_use(self, memory_ids: list[str]) -> dict[str, Any]:
        if not isinstance(memory_ids, list) or not all(isinstance(i, str) for i in memory_ids):
            raise StoreError("memory_ids must be an array of strings.")
        uuids = []
        for memory_id in memory_ids:
            uuids.append(self._uuid_for(memory_id))  # raises on unknown id
        self._record_usage((), applied=uuids)
        return {"recorded": sorted(set(memory_ids)), "field": "applied"}

    def _record_usage(self, surfaced: Iterable[str], direct: Iterable[str] = (),
                      applied: Iterable[str] = ()) -> None:
        """Bump this replica's counters for the memories a call touched.

        Every id here is a uuid. Rows are keyed by replica as well as memory, so
        this only ever writes counters this machine owns - which is what lets a
        push be an idempotent overwrite instead of a stream of increments.
        """
        surfaced, direct, applied = set(surfaced), set(direct), set(applied)
        touched = sorted(surfaced | direct | applied)
        if not touched:
            return
        stamp, today = _now(), _today()
        with self.connection:
            for memory_id in touched:
                row = self.connection.execute(
                    "SELECT spread_bits, spread_epoch FROM usage "
                    "WHERE project_id=? AND memory_id=? AND replica_id=?",
                    (self.project, memory_id, self.replica_id),
                ).fetchone()
                bits, epoch = _touch_spread(
                    row["spread_bits"] if row else 0, row["spread_epoch"] if row else None, today)
                self.connection.execute(
                    "INSERT INTO usage(project_id, memory_id, replica_id, surfaced, surfaced_direct, "
                    "applied, last_surfaced, last_applied, spread_bits, spread_epoch) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(project_id, memory_id, replica_id) DO UPDATE SET "
                    "surfaced=surfaced+excluded.surfaced, "
                    "surfaced_direct=surfaced_direct+excluded.surfaced_direct, "
                    "applied=applied+excluded.applied, "
                    "last_surfaced=COALESCE(excluded.last_surfaced, last_surfaced), "
                    "last_applied=COALESCE(excluded.last_applied, last_applied), "
                    "spread_bits=excluded.spread_bits, spread_epoch=excluded.spread_epoch",
                    (self.project, memory_id, self.replica_id,
                     1 if memory_id in surfaced else 0,
                     1 if memory_id in direct else 0,
                     1 if memory_id in applied else 0,
                     stamp if memory_id in surfaced else None,
                     stamp if memory_id in applied else None,
                     bits, epoch),
                )

    # -------------------------------------------------------------- validation

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

    def _write(self, memory: dict[str, Any], memory_uuid: str) -> None:
        slug = memory["id"]
        evidence = memory.get("evidence") or {}
        scope = memory.get("scope") or {}
        self.connection.execute(
            "INSERT INTO memories(project_id, uuid, slug, status, description, created, last_validated, "
            "created_from_task, area, body, tier_since, tier_since_query) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,(SELECT queries FROM projects WHERE id=?)) "
            "ON CONFLICT(project_id, uuid) DO UPDATE SET slug=excluded.slug, status=excluded.status, "
            "description=excluded.description, created=excluded.created, "
            "last_validated=excluded.last_validated, created_from_task=excluded.created_from_task, "
            "area=excluded.area, body=excluded.body",
            (self.project, memory_uuid, slug, memory["status"], memory["description"],
             evidence.get("created"), evidence.get("last_validated"),
             evidence.get("created_from_task"), scope.get("area"), json.dumps(memory),
             _now(), self.project),
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

    def _synchronize_relationships(self, memory_id: str) -> None:
        """Mirror this memory's links onto their targets, as the file backend does."""
        current = self.get_memory(memory_id)
        related_map = {e["id"]: e["reason"] for e in current["relationships"]["related"]}
        for target_id in related_map:
            if not self.connection.execute(
                "SELECT 1 FROM memories WHERE project_id=? AND slug=?", (self.project, target_id)
            ).fetchone():
                raise StoreError(f"relationships.related references unknown memory: {target_id}")
        for row in self.connection.execute(
            "SELECT uuid, body FROM memories WHERE project_id=? AND slug<>?", (self.project, memory_id)
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
                self._write(other, row["uuid"])


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


def migrate_from_files(database: Path | str, project: str, store_dir: Path | str) -> dict[str, Any]:
    """Import a file-backed .project-memory store into the database.

    The source files are left untouched, so the move is reversible by
    discarding the database. Memories are written first and validated after:
    the file store already carries mirrored relationships, and validating each
    one as it lands would fail on links to memories not imported yet.
    """
    store_dir = Path(store_dir)
    active = store_dir / "active"
    if not active.is_dir():
        raise StoreError(f"No active/ directory under {store_dir}")

    store = SqliteMemoryStore(database, project)
    registry_path = store_dir / "labels.json"
    labels = 0
    if registry_path.is_file():
        registry = json.loads(registry_path.read_text(encoding="utf-8-sig"))
        for label, data in (registry.get("labels") or {}).items():
            try:
                store.add_label(label, (data or {}).get("description") or label)
                labels += 1
            except StoreError:
                pass  # already registered; migration is re-runnable

    imported, skipped = 0, []
    with store.connection:
        for path in sorted(active.glob("*.json")):
            memory = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(memory, dict) or "id" not in memory:
                skipped.append(path.name)
                continue
            evidence = memory.setdefault("evidence", {})
            if isinstance(evidence, dict) and not evidence.get("created"):
                # Best available creation time for a store written before the
                # field existed; date-only, but it preserves relative order.
                evidence["created"] = evidence.get("last_validated") or ""
            store._write(memory)
            imported += 1
    return {
        "project": project,
        "database": str(store.path),
        "labels": labels,
        "imported": imported,
        "skipped": skipped,
        "errors": store.validate_store(),
    }
