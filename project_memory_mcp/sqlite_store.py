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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import validation
from .ranking import RankingContext, rank_memories
from .validation import ID_RE, LABEL_RE

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS projects (
    id      TEXT PRIMARY KEY,
    name    TEXT NOT NULL,
    created TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memories (
    project_id        TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    id                TEXT NOT NULL,
    status            TEXT NOT NULL,
    description       TEXT NOT NULL,
    created           TEXT,
    last_validated    TEXT,
    created_from_task TEXT,
    area              TEXT,
    body              TEXT NOT NULL,
    PRIMARY KEY (project_id, id)
);
CREATE INDEX IF NOT EXISTS memories_status  ON memories(project_id, status);
CREATE INDEX IF NOT EXISTS memories_created ON memories(project_id, created);

CREATE TABLE IF NOT EXISTS labels (
    project_id TEXT NOT NULL, memory_id TEXT NOT NULL, label TEXT NOT NULL,
    PRIMARY KEY (project_id, memory_id, label)
);
CREATE INDEX IF NOT EXISTS labels_by_label ON labels(project_id, label);

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

CREATE TABLE IF NOT EXISTS usage (
    project_id TEXT NOT NULL, memory_id TEXT NOT NULL,
    surfaced INTEGER NOT NULL DEFAULT 0, applied INTEGER NOT NULL DEFAULT 0,
    last_surfaced TEXT, last_applied TEXT,
    PRIMARY KEY (project_id, memory_id)
);

CREATE TABLE IF NOT EXISTS revisions (
    project_id TEXT NOT NULL, memory_id TEXT NOT NULL,
    revised_at TEXT NOT NULL, body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS revisions_memory ON revisions(project_id, memory_id, revised_at);
"""


class StoreError(ValueError):
    """Raised for invalid input, invalid store state, or a failed operation."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class SqliteMemoryStore:
    def __init__(self, database: Path | str, project: str) -> None:
        if not ID_RE.match(project):
            raise StoreError("Project id must be lowercase kebab-case.")
        self.project = project
        self.path = Path(database)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript(SCHEMA)
        self.connection.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self.connection.execute(
            "INSERT OR IGNORE INTO projects(id, name, created) VALUES (?, ?, ?)",
            (project, project, _now()),
        )
        self.connection.commit()

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

    def get_memory(self, memory_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT body FROM memories WHERE project_id=? AND id=?", (self.project, memory_id)
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
            "SELECT id, body FROM memories WHERE project_id=? ORDER BY id", (self.project,)
        ).fetchall()
        return {row["id"]: json.loads(row["body"]) for row in rows}

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
        from .store import LabelExpression  # local import: shared grammar, no cycle at import time

        known = set(self.list_labels()["labels"])
        expression = LabelExpression(label_query, known)
        statuses = _normalize_status_filter(status_filter)
        needle = text_query.lower().strip() if text_query else ""

        sql = "SELECT id, body FROM memories WHERE project_id=?"
        params: list[Any] = [self.project]
        if statuses is not None:
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            params.extend(sorted(statuses))
        sql += " ORDER BY id"

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
        status_filter: list[str] | str | None = None,
        limit: int = 8,
        full_count: int = 3,
        include_derived: bool = True,
    ) -> dict[str, Any]:
        from .store import LabelExpression

        if limit < 1:
            raise StoreError("limit must be >= 1.")
        if full_count < 0:
            raise StoreError("full_count must be >= 0.")
        if related_to is not None:
            self.get_memory(related_to)  # raises on unknown id
        known = set(self.list_labels()["labels"])
        expression = LabelExpression(label_query, known)
        statuses = _normalize_status_filter(status_filter)

        memories = self.load_memories()
        if not memories:
            return {"query": query, "considered": 0, "count": 0, "memories": []}
        ranked = rank_memories(
            memories,
            query=query,
            query_labels=expression.used_labels,
            related_to=related_to,
            include_derived=include_derived,
            context=RankingContext(memories, include_derived=include_derived),
        )

        results: list[dict[str, Any]] = []
        for entry in ranked:
            memory = memories[entry["id"]]
            if statuses is not None and memory.get("status") not in statuses:
                continue
            if not expression.matches(memory.get("labels") or []):
                continue
            if entry["score"] <= 0.0:
                continue
            result = _light_record(memory)
            result["score"] = entry["score"]
            result["why"] = {
                "text": entry["text_score"],
                "graph": entry["graph_score"],
                "label": entry["label_score"],
                "status_factor": entry["status_factor"],
            }
            if len(results) < full_count:
                result["memory"] = memory
            results.append(result)
            if len(results) >= limit:
                break
        self._record_usage([r["id"] for r in results], "surfaced")
        payload: dict[str, Any] = {
            "query": query,
            "label_query_labels": sorted(expression.used_labels),
            "considered": len(memories),
            "count": len(results),
            "memories": results,
        }
        if related_to:
            payload["related_to"] = related_to
        return payload

    def recent(self, limit: int = 8, offset: int = 0) -> dict[str, Any]:
        """Newest first, straight out of an index - no ranking involved."""
        rows = self.connection.execute(
            "SELECT body FROM memories WHERE project_id=? "
            "ORDER BY COALESCE(created, last_validated) DESC, id DESC LIMIT ? OFFSET ?",
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
            "SELECT 1 FROM memories WHERE project_id=? AND id=?", (self.project, memory_id)
        ).fetchone():
            raise StoreError(f"Memory already exists: {memory_id}")
        with self.connection:
            self._write(memory)
            self._synchronize_relationships(memory_id)
        return {"created": memory_id, "related_candidates": []}

    def update_memory(self, memory_id: str, patch: dict[str, Any], related_label_query: Any = None) -> dict[str, Any]:
        if "id" in patch and patch["id"] != memory_id:
            raise StoreError("update_memory cannot change a memory id.")
        current = self.get_memory(memory_id)
        merged = _deep_merge(current, patch)
        self._require_valid(merged)
        with self.connection:
            self.connection.execute(
                "INSERT INTO revisions(project_id, memory_id, revised_at, body) VALUES (?,?,?,?)",
                (self.project, memory_id, _now(), json.dumps(current)),
            )
            self._write(merged)
            self._synchronize_relationships(memory_id)
        return {"updated": memory_id, "related_candidates": []}

    def delete_memory(self, memory_id: str, confirm_exact_id: str) -> dict[str, Any]:
        if confirm_exact_id != memory_id:
            raise StoreError("confirm_exact_id must exactly match id.")
        body = self.get_memory(memory_id)
        touched: list[str] = []
        with self.connection:
            self.connection.execute(
                "INSERT INTO revisions(project_id, memory_id, revised_at, body) VALUES (?,?,?,?)",
                (self.project, memory_id, _now(), json.dumps(body)),
            )
            referrers = self.connection.execute(
                "SELECT DISTINCT src FROM edges WHERE project_id=? AND dst=?", (self.project, memory_id)
            ).fetchall()
            for row in referrers:
                other = self.get_memory(row["src"])
                rel = other["relationships"]
                rel["related"] = [e for e in rel["related"] if e.get("id") != memory_id]
                for field in ("supersedes", "superseded_by"):
                    rel[field] = [v for v in rel[field] if v != memory_id]
                self._write(other)
                touched.append(other["id"])
            for table in ("memories", "labels", "usage"):
                self.connection.execute(
                    f"DELETE FROM {table} WHERE project_id=? AND "
                    f"{'id' if table == 'memories' else 'memory_id'}=?",
                    (self.project, memory_id),
                )
            self.connection.execute(
                "DELETE FROM edges WHERE project_id=? AND (src=? OR dst=?)",
                (self.project, memory_id, memory_id),
            )
        return {"deleted": memory_id, "cleaned_references_in": sorted(set(touched))}

    # ------------------------------------------------------------------- usage

    def load_usage(self) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT memory_id, surfaced, applied, last_surfaced, last_applied "
            "FROM usage WHERE project_id=?", (self.project,)
        ).fetchall()
        return {
            "schema_version": SCHEMA_VERSION,
            "memories": {
                row["memory_id"]: {
                    "surfaced": row["surfaced"], "applied": row["applied"],
                    "last_surfaced": row["last_surfaced"], "last_applied": row["last_applied"],
                }
                for row in rows
            },
        }

    def record_use(self, memory_ids: list[str]) -> dict[str, Any]:
        if not isinstance(memory_ids, list) or not all(isinstance(i, str) for i in memory_ids):
            raise StoreError("memory_ids must be an array of strings.")
        for memory_id in memory_ids:
            self.get_memory(memory_id)  # raises on unknown id
        self._record_usage(memory_ids, "applied")
        return {"recorded": sorted(set(memory_ids)), "field": "applied"}

    def _record_usage(self, memory_ids: Iterable[str], field: str) -> None:
        ids = sorted(set(memory_ids))
        if not ids:
            return
        stamp = _now()
        # An UPSERT per memory, so there is no read-modify-write of a whole
        # counters document the way the file backend needed.
        with self.connection:
            for memory_id in ids:
                self.connection.execute(
                    f"INSERT INTO usage(project_id, memory_id, {field}, last_{field}) VALUES (?,?,1,?) "
                    f"ON CONFLICT(project_id, memory_id) DO UPDATE SET "
                    f"{field}={field}+1, last_{field}=excluded.last_{field}",
                    (self.project, memory_id, stamp),
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

    def _write(self, memory: dict[str, Any]) -> None:
        memory_id = memory["id"]
        evidence = memory.get("evidence") or {}
        scope = memory.get("scope") or {}
        self.connection.execute(
            "INSERT INTO memories(project_id, id, status, description, created, last_validated, "
            "created_from_task, area, body) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(project_id, id) DO UPDATE SET status=excluded.status, "
            "description=excluded.description, created=excluded.created, "
            "last_validated=excluded.last_validated, created_from_task=excluded.created_from_task, "
            "area=excluded.area, body=excluded.body",
            (self.project, memory_id, memory["status"], memory["description"],
             evidence.get("created"), evidence.get("last_validated"),
             evidence.get("created_from_task"), scope.get("area"), json.dumps(memory)),
        )
        self.connection.execute("DELETE FROM labels WHERE project_id=? AND memory_id=?",
                                (self.project, memory_id))
        self.connection.executemany(
            "INSERT OR IGNORE INTO labels(project_id, memory_id, label) VALUES (?,?,?)",
            [(self.project, memory_id, label) for label in memory.get("labels") or []],
        )
        self.connection.execute("DELETE FROM edges WHERE project_id=? AND src=?",
                                (self.project, memory_id))
        relationships = memory.get("relationships") or {}
        rows = [(self.project, memory_id, e["id"], "related", e.get("reason"))
                for e in relationships.get("related") or [] if isinstance(e, dict)]
        rows += [(self.project, memory_id, t, kind, None)
                 for kind in ("supersedes", "superseded_by")
                 for t in relationships.get(kind) or []]
        self.connection.executemany(
            "INSERT OR REPLACE INTO edges(project_id, src, dst, kind, reason) VALUES (?,?,?,?,?)", rows)

    def _synchronize_relationships(self, memory_id: str) -> None:
        """Mirror this memory's links onto their targets, as the file backend does."""
        current = self.get_memory(memory_id)
        related_map = {e["id"]: e["reason"] for e in current["relationships"]["related"]}
        for target_id in related_map:
            if not self.connection.execute(
                "SELECT 1 FROM memories WHERE project_id=? AND id=?", (self.project, target_id)
            ).fetchone():
                raise StoreError(f"relationships.related references unknown memory: {target_id}")
        for row in self.connection.execute(
            "SELECT id FROM memories WHERE project_id=? AND id<>?", (self.project, memory_id)
        ).fetchall():
            other = self.get_memory(row["id"])
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
                self._write(other)


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
