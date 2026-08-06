"""File-based project memory store.

Memories are individual JSON files under ``.project-memory/active/``, described by a
canonical label registry (``labels.json``). The store is plain JSON on disk so it
diffs, merges, and reviews cleanly in git alongside the project it belongs to.

There is no generated index: memory files are the only source of truth. They are
parsed on demand and cached in memory until one of them changes.
"""

from __future__ import annotations

import copy
import json
import os
import re
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .ranking import RankingContext, rank_memories

STORE_DIR_NAME = ".project-memory"

VALID_STATUSES = {"active", "stale", "superseded", "wrong"}
ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
LABEL_RE = re.compile(r"^[a-z]+:[a-z0-9]+(-[a-z0-9]+)*$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

MEMORY_FIELDS = (
    "schema_version",
    "id",
    "status",
    "description",
    "tags",
    "labels",
    "scope",
    "triggers",
    "remembered_facts",
    "solution_pattern",
    "pitfalls",
    "evidence",
    "relationships",
)
SCOPE_REQUIRED_FIELDS = ("project", "area", "files")
SCOPE_FIELDS = ("project", "area", "files", "applies_to")
EVIDENCE_FIELDS = ("created_from_task", "last_validated")
RELATIONSHIP_FIELDS = ("related", "supersedes", "superseded_by")

# Buffer usage counters this long before writing them out (see flush_usage).
USAGE_FLUSH_INTERVAL_SECONDS = 5.0


class StoreError(ValueError):
    """Raised for any invalid input, invalid store state, or failed operation."""


def find_store_root(start: Path | str | None = None) -> Path | None:
    """Walk upward from ``start`` (default: cwd) to the first directory containing
    a ``.project-memory`` store. Returns None when no store is found."""
    base = (Path(start) if start is not None else Path.cwd()).resolve()
    for candidate in (base, *base.parents):
        if (candidate / STORE_DIR_NAME).is_dir():
            return candidate
    return None


class LabelExpression:
    """Compiled label query: either a dict with all/any/not arrays, or a string
    expression using AND, OR, NOT, and parentheses over registered labels."""

    def __init__(self, query: Any, known_labels: set[str]) -> None:
        self.query = query
        self.known_labels = known_labels
        self.used_labels: set[str] = set()
        self._predicate = self._compile(query)

    def matches(self, labels: list[str]) -> bool:
        return self._predicate(set(labels))

    def _compile(self, query: Any) -> Callable[[set[str]], bool]:
        if query in (None, "", {}, []):
            return lambda _labels: True
        if isinstance(query, dict):
            all_labels = self._normalize_label_list(query.get("all") or query.get("and") or [])
            any_labels = self._normalize_label_list(query.get("any") or query.get("or") or [])
            not_labels = self._normalize_label_list(query.get("not") or [])
            return lambda labels: (
                all(label in labels for label in all_labels)
                and (not any_labels or any(label in labels for label in any_labels))
                and all(label not in labels for label in not_labels)
            )
        if isinstance(query, str):
            tokens = self._tokenize(query)
            if not tokens:
                return lambda _labels: True
            parser = _LabelParser(tokens, self._record_label)
            expr = parser.parse_expression()
            parser.expect_end()
            return expr
        raise StoreError("label_query must be an object, string, null, or omitted.")

    def _normalize_label_list(self, value: Any) -> list[str]:
        if isinstance(value, str):
            items = [value]
        elif isinstance(value, list):
            items = value
        else:
            raise StoreError("Label query groups must be strings or arrays of strings.")
        labels: list[str] = []
        for item in items:
            if not isinstance(item, str):
                raise StoreError("Labels must be strings.")
            labels.append(self._record_label(item))
        return labels

    def _record_label(self, label: str) -> str:
        normalized = label.strip().lower()
        if not LABEL_RE.match(normalized):
            raise StoreError(f"Invalid label format: {label}")
        if normalized not in self.known_labels:
            raise StoreError(f"Unknown label: {normalized}")
        self.used_labels.add(normalized)
        return normalized

    @staticmethod
    def _tokenize(query: str) -> list[str]:
        token_re = re.compile(r"\s*(AND|OR|NOT|\(|\)|[a-z]+:[a-z0-9]+(?:-[a-z0-9]+)*)\s*", re.IGNORECASE)
        tokens: list[str] = []
        pos = 0
        while pos < len(query):
            match = token_re.match(query, pos)
            if not match:
                raise StoreError(f"Invalid label query near: {query[pos:]}")
            token = match.group(1)
            tokens.append(token.upper() if token.upper() in {"AND", "OR", "NOT"} else token.lower())
            pos = match.end()
        return tokens


class _LabelParser:
    def __init__(self, tokens: list[str], record_label: Callable[[str], str]) -> None:
        self.tokens = tokens
        self.record_label = record_label
        self.pos = 0

    def parse_expression(self) -> Callable[[set[str]], bool]:
        return self.parse_or()

    def parse_or(self) -> Callable[[set[str]], bool]:
        left = self.parse_and()
        while self._peek() == "OR":
            self.pos += 1
            right = self.parse_and()
            left = (lambda left=left, right=right: lambda labels: left(labels) or right(labels))()
        return left

    def parse_and(self) -> Callable[[set[str]], bool]:
        left = self.parse_unary()
        while self._peek() == "AND":
            self.pos += 1
            right = self.parse_unary()
            left = (lambda left=left, right=right: lambda labels: left(labels) and right(labels))()
        return left

    def parse_unary(self) -> Callable[[set[str]], bool]:
        if self._peek() == "NOT":
            self.pos += 1
            inner = self.parse_unary()
            return lambda labels: not inner(labels)
        return self.parse_primary()

    def parse_primary(self) -> Callable[[set[str]], bool]:
        token = self._peek()
        if token is None:
            raise StoreError("Unexpected end of label query.")
        if token == "(":
            self.pos += 1
            expr = self.parse_expression()
            if self._peek() != ")":
                raise StoreError("Missing ')' in label query.")
            self.pos += 1
            return expr
        if token in {"AND", "OR", "NOT", ")"}:
            raise StoreError(f"Unexpected token in label query: {token}")
        self.pos += 1
        label = self.record_label(token)
        return lambda labels: label in labels

    def expect_end(self) -> None:
        if self._peek() is not None:
            raise StoreError(f"Unexpected token in label query: {self._peek()}")

    def _peek(self) -> str | None:
        if self.pos >= len(self.tokens):
            return None
        return self.tokens[self.pos]


class MemoryStore:
    def __init__(self, root: Path | str | None = None) -> None:
        if root is None:
            root = find_store_root() or Path.cwd()
        self.root = Path(root).resolve()
        self.memory_root = self.root / STORE_DIR_NAME
        self.active_root = self.memory_root / "active"
        self.labels_path = self.memory_root / "labels.json"
        self.usage_path = self.memory_root / "usage.json"
        self._cache_signature: tuple[tuple[str, int, int], ...] | None = None
        self._cache_records: dict[str, dict[str, Any]] | None = None
        self._cache_contexts: dict[bool, RankingContext] = {}
        self._usage_pending: dict[str, dict[str, Any]] = {}
        self._usage_last_flush = 0.0

    # ------------------------------------------------------------------ reads

    def list_labels(self) -> dict[str, Any]:
        registry = self._read_json(self.labels_path)
        grouped: dict[str, dict[str, Any]] = {}
        for label, data in sorted(registry["labels"].items()):
            prefix = label.split(":", 1)[0]
            grouped.setdefault(prefix, {})[label] = data
        return {
            "schema_version": registry["schema_version"],
            "description": registry.get("description", ""),
            "labels": registry["labels"],
            "groups": grouped,
        }

    def search_memories(
        self,
        label_query: Any = None,
        status_filter: list[str] | str | None = None,
        text_query: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        known = set(self.list_labels()["labels"].keys())
        expression = LabelExpression(label_query, known)
        statuses = self._normalize_status_filter(status_filter)
        needle = text_query.lower().strip() if text_query else ""
        matches: list[dict[str, Any]] = []
        for memory_id, record in sorted(self.load_memories().items()):
            memory = record["memory"]
            if statuses is not None and memory.get("status") not in statuses:
                continue
            labels = memory.get("labels", [])
            if not expression.matches(labels):
                continue
            entry = self._light_record(memory_id, record["path"], memory)
            if needle and needle not in self._entry_text(entry):
                continue
            matches.append(entry)
            if limit is not None and len(matches) >= limit:
                break
        return {"count": len(matches), "label_query_labels": sorted(expression.used_labels), "memories": matches}

    def get_memory(self, memory_id: str) -> dict[str, Any]:
        path = self._find_memory_path(memory_id)
        return self._read_json(path)

    def get_memory_neighborhood(
        self,
        memory_id: str,
        depth: int = 1,
        max_nodes: int = 25,
    ) -> dict[str, Any]:
        if depth < 0:
            raise StoreError("depth must be >= 0.")
        if max_nodes < 1:
            raise StoreError("max_nodes must be >= 1.")
        records = self._load_all_memories()
        if memory_id not in records:
            raise StoreError(f"Unknown memory id: {memory_id}")
        nodes: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, Any]] = []
        seen_edges: set[tuple[str, str, str]] = set()
        queue: deque[tuple[str, int]] = deque([(memory_id, 0)])
        while queue and len(nodes) < max_nodes:
            current_id, current_depth = queue.popleft()
            if current_id in nodes:
                continue
            current = records[current_id]["memory"]
            nodes[current_id] = self._light_record(current_id, records[current_id]["path"], current)
            if current_depth >= depth:
                continue
            for edge in self._outgoing_edges(current):
                target_id = edge["to"]
                if target_id not in records:
                    continue
                edge_key = (edge["type"], edge["from"], edge["to"])
                if edge_key not in seen_edges:
                    seen_edges.add(edge_key)
                    edges.append(edge)
                if target_id not in nodes and len(nodes) + len(queue) < max_nodes:
                    queue.append((target_id, current_depth + 1))
        return {"root": memory_id, "depth": depth, "nodes": list(nodes.values()), "edges": edges}

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
        """Ranked retrieval in one call.

        Scores every memory by BM25 text relevance, personalized-PageRank
        proximity to the query's best matches, and label overlap, then returns
        the best ``limit`` of them - with the top ``full_count`` inlined in
        full so the caller does not need follow-up ``get_memory`` calls.

        ``related_to`` anchors the walk at one memory instead, ranking the rest
        of the store by how strongly it relates to that one - authored links
        first, then memories reachable through the graph.

        With no query, no labels and no anchor the restart distribution is
        uniform, which makes this ordinary PageRank: the most structurally
        central memories, i.e. a reasonable "orient me in this store" answer.
        """
        if limit < 1:
            raise StoreError("limit must be >= 1.")
        if full_count < 0:
            raise StoreError("full_count must be >= 0.")
        if related_to is not None:
            self._find_memory_path(related_to)  # raises on unknown id
        known = set(self.list_labels()["labels"].keys())
        expression = LabelExpression(label_query, known)
        statuses = self._normalize_status_filter(status_filter)
        records = self.load_memories()
        if not records:
            return {"query": query, "considered": 0, "count": 0, "memories": []}

        context = self.ranking_context(include_derived=include_derived)
        memories = context.memories
        # Rank across the whole graph and filter afterwards: a memory that is
        # excluded from the results can still be the link between two that are
        # not, so removing it before the walk would distort proximity.
        ranked = rank_memories(
            memories,
            query=query,
            query_labels=expression.used_labels,
            related_to=related_to,
            include_derived=include_derived,
            context=context,
        )

        results: list[dict[str, Any]] = []
        for entry in ranked:
            memory_id = entry["id"]
            memory = memories[memory_id]
            if statuses is not None and memory.get("status") not in statuses:
                continue
            if not expression.matches(memory.get("labels") or []):
                continue
            if entry["score"] <= 0.0:
                continue
            result = self._light_record(memory_id, records[memory_id]["path"], memory)
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

        self._record_usage([entry["id"] for entry in results], "surfaced")

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

    # ------------------------------------------------------------------ usage

    def load_usage(self) -> dict[str, Any]:
        """Usage counters: what is on disk, plus anything not yet flushed.

        Usage lives outside the memory files on purpose: recording a read must
        never dirty a memory's JSON, or every recall would show up in git and
        memory diffs would stop being reviewable.
        """
        data = self._read_usage_file()
        if not self._usage_pending:
            return data
        entries = data.setdefault("memories", {})
        for memory_id, pending in self._usage_pending.items():
            entry = entries.setdefault(memory_id, {})
            for field, value in pending.items():
                if field.startswith("last_"):
                    entry[field] = value
                else:
                    entry[field] = int(entry.get(field, 0)) + value
        return data

    def flush_usage(self, force: bool = False) -> bool:
        """Write buffered counters to disk. Returns whether anything was written.

        Recalls arrive in bursts, and each write changes the file's bytes, so
        writing per call cost ~25 ms against ~4 ms of actual ranking work.
        Buffering collapses a burst into one write; the exposure is losing the
        last few counts if the process is killed, which is acceptable for
        telemetry that is already disposable.
        """
        if not self._usage_pending:
            return False
        now = time.monotonic()
        if not force and (now - self._usage_last_flush) < USAGE_FLUSH_INTERVAL_SECONDS:
            return False
        merged = self.load_usage()
        try:
            self._write_json(self.usage_path, merged)
        except OSError:
            return False  # a read-only checkout should still be able to recall
        self._usage_pending.clear()
        self._usage_last_flush = now
        return True

    def _read_usage_file(self) -> dict[str, Any]:
        if not self.usage_path.is_file():
            return {"schema_version": 1, "memories": {}}
        try:
            data = json.loads(self.usage_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            # Usage is telemetry, not truth. A corrupt file costs history, not
            # correctness, so start over rather than failing the caller's read.
            return {"schema_version": 1, "memories": {}}
        if not isinstance(data, dict) or not isinstance(data.get("memories"), dict):
            return {"schema_version": 1, "memories": {}}
        return data

    def record_use(self, memory_ids: list[str]) -> dict[str, Any]:
        """Mark memories as having actually informed the work in hand.

        ``surfaced`` is recorded automatically by recall; ``applied`` cannot be,
        because only the caller knows whether a returned memory changed what it
        did. The gap between the two counts is the useful signal: a memory
        surfaced often and applied never is polluting retrieval.
        """
        if not isinstance(memory_ids, list) or not all(isinstance(i, str) for i in memory_ids):
            raise StoreError("memory_ids must be an array of strings.")
        known = set(self.load_memories())
        unknown = [i for i in memory_ids if i not in known]
        if unknown:
            raise StoreError(f"Unknown memory ids: {', '.join(sorted(unknown))}")
        self._record_usage(memory_ids, "applied")
        return {"recorded": sorted(set(memory_ids)), "field": "applied"}

    def _record_usage(self, memory_ids: list[str], field: str) -> None:
        if not memory_ids:
            return
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for memory_id in set(memory_ids):
            pending = self._usage_pending.setdefault(memory_id, {})
            pending[field] = int(pending.get(field, 0)) + 1
            pending[f"last_{field}"] = stamp
        self.flush_usage()

    # -------------------------------------------------------------- mutations

    def create_memory(self, memory: dict[str, Any], related_label_query: Any = None) -> dict[str, Any]:
        def mutate() -> dict[str, Any]:
            self._require_valid_memory(memory)
            memory_id = memory["id"]
            if self._memory_path_or_none(memory_id) is not None:
                raise StoreError(f"Memory already exists: {memory_id}")
            path = self._memory_path(memory_id)
            self._write_json(path, memory)
            self._synchronize_relationships(memory_id)
            candidates = self._related_candidates(related_label_query, exclude_id=memory_id)
            return {"created": str(path.relative_to(self.root)), "related_candidates": candidates}

        return self._transaction(mutate)

    def update_memory(self, memory_id: str, patch: dict[str, Any], related_label_query: Any = None) -> dict[str, Any]:
        if "id" in patch and patch["id"] != memory_id:
            raise StoreError("update_memory cannot change a memory id.")

        def mutate() -> dict[str, Any]:
            path = self._find_memory_path(memory_id)
            memory = self._read_json(path)
            merged = self._deep_merge(memory, patch)
            self._require_valid_memory(merged)
            self._write_json(path, merged)
            self._synchronize_relationships(memory_id)
            candidates = self._related_candidates(related_label_query, exclude_id=memory_id)
            return {"updated": str(path.relative_to(self.root)), "related_candidates": candidates}

        return self._transaction(mutate)

    def add_label(self, label: str, description: str) -> dict[str, Any]:
        normalized = label.strip().lower()
        if not LABEL_RE.match(normalized):
            raise StoreError("Label must use prefix:kebab-case format.")
        if not description or not description.strip():
            raise StoreError("Label description is required.")

        def mutate() -> dict[str, Any]:
            registry = self._read_json(self.labels_path)
            labels = registry.setdefault("labels", {})
            if normalized in labels:
                raise StoreError(f"Label already exists: {normalized}")
            labels[normalized] = {"description": description.strip()}
            registry["labels"] = dict(sorted(labels.items()))
            self._write_json(self.labels_path, registry)
            return {"added": normalized}

        return self._transaction(mutate)

    def delete_memory(self, memory_id: str, confirm_exact_id: str) -> dict[str, Any]:
        if confirm_exact_id != memory_id:
            raise StoreError("confirm_exact_id must exactly match id.")

        def mutate() -> dict[str, Any]:
            path = self._find_memory_path(memory_id)
            path.unlink()
            touched: list[str] = []
            for record in self._load_all_memories().values():
                memory = record["memory"]
                changed = False
                related = [entry for entry in memory["relationships"]["related"] if entry.get("id") != memory_id]
                if len(related) != len(memory["relationships"]["related"]):
                    memory["relationships"]["related"] = related
                    changed = True
                for field in ("supersedes", "superseded_by"):
                    values = [value for value in memory["relationships"][field] if value != memory_id]
                    if len(values) != len(memory["relationships"][field]):
                        memory["relationships"][field] = values
                        changed = True
                if changed:
                    self._write_json(record["path"], memory)
                    touched.append(memory["id"])
            return {"deleted": memory_id, "cleaned_references_in": sorted(touched)}

        return self._transaction(mutate)

    # ------------------------------------------------------- index/validation

    def validate_store(self) -> list[str]:
        """Validate the whole store. Returns a list of human-readable problems
        (empty when the store is valid). Never raises for content problems."""
        errors: list[str] = []
        for directory in (self.memory_root, self.active_root):
            if not directory.is_dir():
                errors.append(f"Missing directory: {directory}")
        if not self.memory_root.is_dir():
            return errors

        known_labels = self._validate_label_registry(errors)
        records = self._validate_memory_files(errors, known_labels)
        self._validate_relationship_graph(errors, records)
        return errors

    def validate_memory(self, memory: Any, known_labels: set[str] | None, where: str) -> list[str]:
        """Validate a single memory document against the schema. ``known_labels``
        of None skips registry membership checks."""
        errors: list[str] = []
        if not isinstance(memory, dict):
            return [f"{where}: memory must be a JSON object."]

        for field in MEMORY_FIELDS:
            if field not in memory:
                errors.append(f"{where}: missing required field '{field}'.")
        for field in memory:
            if field not in MEMORY_FIELDS:
                errors.append(f"{where}: unknown field '{field}'.")
        if errors:
            return errors

        if memory["schema_version"] != 1:
            errors.append(f"{where}: schema_version must be 1.")
        if not isinstance(memory["id"], str) or not ID_RE.match(memory["id"]):
            errors.append(f"{where}: id must be lowercase kebab-case.")
        if memory["status"] not in VALID_STATUSES:
            errors.append(f"{where}: status '{memory['status']}' is not one of {sorted(VALID_STATUSES)}.")
        if not isinstance(memory["description"], str) or len(memory["description"]) < 20:
            errors.append(f"{where}: description must be a string of at least 20 characters.")

        # Tags are free-text search keys, deliberately not slugs. Code
        # identifiers make excellent tags - `bReplicates` is exactly what
        # someone searching for that behavior types, and the tokenizer splits
        # it on case boundaries while keeping it whole, so it matches
        # "replicates" and the exact identifier. Slug discipline is enforced
        # where it is load-bearing instead: `id` becomes a filename, and
        # `labels` are keys into the registry.
        self._check_string_array(errors, where, memory, "tags", unique=True)
        self._check_string_array(errors, where, memory, "labels", unique=True, min_items=1, item_re=LABEL_RE)
        self._check_string_array(errors, where, memory, "triggers", unique=True, min_items=1)
        self._check_string_array(errors, where, memory, "remembered_facts", min_items=1)
        self._check_string_array(errors, where, memory, "solution_pattern")
        self._check_string_array(errors, where, memory, "pitfalls")

        if known_labels is not None and isinstance(memory["labels"], list):
            for label in memory["labels"]:
                if isinstance(label, str) and LABEL_RE.match(label) and label not in known_labels:
                    errors.append(f"{where}: label '{label}' is not declared in labels.json.")

        scope = memory["scope"]
        if not isinstance(scope, dict):
            errors.append(f"{where}: scope must be an object.")
        else:
            for field in SCOPE_REQUIRED_FIELDS:
                if field not in scope:
                    errors.append(f"{where}: scope is missing '{field}'.")
            for field in scope:
                if field not in SCOPE_FIELDS:
                    errors.append(f"{where}: scope has unknown field '{field}'.")
            for field in ("project", "area"):
                if field in scope and not isinstance(scope[field], str):
                    errors.append(f"{where}: scope.{field} must be a string.")
            for field in ("files", "applies_to"):
                if field in scope:
                    self._check_string_array(errors, where, scope, field, unique=True, prefix="scope.")

        evidence = memory["evidence"]
        if not isinstance(evidence, dict):
            errors.append(f"{where}: evidence must be an object.")
        else:
            for field in EVIDENCE_FIELDS:
                if field not in evidence:
                    errors.append(f"{where}: evidence is missing '{field}'.")
            for field in evidence:
                if field not in EVIDENCE_FIELDS:
                    errors.append(f"{where}: evidence has unknown field '{field}'.")
            if "created_from_task" in evidence and not isinstance(evidence["created_from_task"], str):
                errors.append(f"{where}: evidence.created_from_task must be a string.")
            if "last_validated" in evidence:
                value = evidence["last_validated"]
                if not isinstance(value, str) or not DATE_RE.match(value):
                    errors.append(f"{where}: evidence.last_validated must be YYYY-MM-DD.")

        relationships = memory["relationships"]
        if not isinstance(relationships, dict):
            errors.append(f"{where}: relationships must be an object.")
        else:
            for field in RELATIONSHIP_FIELDS:
                if field not in relationships:
                    errors.append(f"{where}: relationships is missing '{field}'.")
            for field in relationships:
                if field not in RELATIONSHIP_FIELDS:
                    errors.append(f"{where}: relationships has unknown field '{field}'.")
            related = relationships.get("related")
            if related is not None:
                if not isinstance(related, list):
                    errors.append(f"{where}: relationships.related must be an array.")
                else:
                    for entry in related:
                        if not isinstance(entry, dict) or set(entry) != {"id", "reason"}:
                            errors.append(
                                f"{where}: relationships.related entries must be objects "
                                "with exactly 'id' and 'reason'."
                            )
                            continue
                        if not isinstance(entry["id"], str) or not ID_RE.match(entry["id"]):
                            errors.append(f"{where}: relationships.related id must be lowercase kebab-case.")
                        if not isinstance(entry["reason"], str) or len(entry["reason"]) < 10:
                            errors.append(
                                f"{where}: relationships.related reason must be a string "
                                "of at least 10 characters."
                            )
            for field in ("supersedes", "superseded_by"):
                if field in relationships:
                    self._check_string_array(
                        errors, where, relationships, field, unique=True, item_re=ID_RE, prefix="relationships."
                    )
        return errors

    # -------------------------------------------------------- internal: store

    def _transaction(self, mutate: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        snapshot = self._snapshot()
        try:
            result = mutate()
            errors = self.validate_store()
            if errors:
                raise StoreError("Store validation failed after mutation:\n" + "\n".join(errors))
            return result
        except Exception:
            self._restore(snapshot)
            raise

    def _snapshot(self) -> dict[Path, bytes | None]:
        paths = [self.labels_path]
        if self.active_root.exists():
            paths.extend(self.active_root.glob("*.json"))
        return {path: path.read_bytes() if path.exists() else None for path in paths}

    def _restore(self, snapshot: dict[Path, bytes | None]) -> None:
        current_paths = {self.labels_path}
        if self.active_root.exists():
            current_paths.update(self.active_root.glob("*.json"))
        for path in current_paths:
            if path not in snapshot and path.exists():
                path.unlink()
        for path, data in snapshot.items():
            if data is None:
                if path.exists():
                    path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)

    def _require_valid_memory(self, memory: dict[str, Any]) -> None:
        known = set(self.list_labels()["labels"].keys())
        where = memory.get("id", "memory") if isinstance(memory, dict) else "memory"
        errors = self.validate_memory(memory, known, str(where))
        if errors:
            raise StoreError("Invalid memory:\n" + "\n".join(errors))

    def _synchronize_relationships(self, memory_id: str) -> None:
        records = self._load_all_memories()
        current = records[memory_id]["memory"]
        related_map = {entry["id"]: entry["reason"] for entry in current["relationships"]["related"]}
        for target_id in related_map:
            if target_id not in records:
                raise StoreError(f"relationships.related references unknown memory: {target_id}")
        for other_id, record in records.items():
            if other_id == memory_id:
                continue
            other = record["memory"]
            before = json.dumps(other["relationships"], sort_keys=True)
            reverse = [entry for entry in other["relationships"]["related"] if entry.get("id") != memory_id]
            if other_id in related_map:
                reverse.append({"id": memory_id, "reason": related_map[other_id]})
            other["relationships"]["related"] = reverse
            self._mirror_array(current, other, "supersedes", "superseded_by")
            self._mirror_array(current, other, "superseded_by", "supersedes")
            after = json.dumps(other["relationships"], sort_keys=True)
            if before != after:
                self._write_json(record["path"], other)
        self._write_json(records[memory_id]["path"], current)

    def _mirror_array(self, current: dict[str, Any], other: dict[str, Any], source: str, target: str) -> None:
        memory_id = current["id"]
        other_id = other["id"]
        values = [value for value in other["relationships"][target] if value != memory_id]
        if other_id in current["relationships"][source]:
            values.append(memory_id)
        other["relationships"][target] = values

    def _related_candidates(self, related_label_query: Any, exclude_id: str) -> list[dict[str, Any]]:
        if related_label_query in (None, "", {}, []):
            return []
        result = self.search_memories(related_label_query, status_filter=["active", "stale"])
        return [entry for entry in result["memories"] if entry["id"] != exclude_id]

    def _load_all_memories(self) -> dict[str, dict[str, Any]]:
        records: dict[str, dict[str, Any]] = {}
        if self.active_root.exists():
            for path in sorted(self.active_root.glob("*.json")):
                memory = self._read_json(path)
                records[memory["id"]] = {"path": path, "memory": memory}
        return records

    def _store_signature(self) -> tuple[tuple[str, int, int], ...]:
        """Cheap fingerprint of the memory files, used to invalidate the cache.

        Uses ``os.scandir`` rather than ``Path.glob`` plus ``stat``: a
        ``DirEntry`` carries metadata from the directory read itself, so size
        and mtime arrive with the listing instead of costing two extra syscalls
        per file. Measured 28x faster on a 3000-memory store (103 ms -> 3.7 ms),
        which is what keeps cache validation cheap enough to run on every read.
        """
        if not self.active_root.exists():
            return ()
        entries: list[tuple[str, int, int]] = []
        with os.scandir(self.active_root) as iterator:
            for entry in iterator:
                if not entry.name.endswith(".json") or not entry.is_file():
                    continue
                info = entry.stat()
                entries.append((entry.name, info.st_mtime_ns, info.st_size))
        entries.sort()
        return tuple(entries)

    def load_memories(self) -> dict[str, dict[str, Any]]:
        """Parsed memories, cached until any memory file changes.

        Ranking touches every memory on every call, so re-parsing the whole
        store per request is the dominant cost in a long-lived server process.
        The cache key is the name, size and mtime of each memory file, so an
        edit made outside this process still invalidates it.
        """
        signature = self._store_signature()
        if self._cache_records is not None and self._cache_signature == signature:
            return self._cache_records
        records = self._load_all_memories()
        self._cache_signature = signature
        self._cache_records = records
        self._cache_contexts.clear()
        return records

    def ranking_context(self, include_derived: bool = True) -> RankingContext:
        """Prebuilt BM25 index and adjacency for the current store revision.

        Building these costs ~10x what answering a query does, so they are
        cached alongside the parsed memories and dropped together with them
        whenever a file changes.
        """
        records = self.load_memories()
        cached = self._cache_contexts.get(include_derived)
        if cached is not None:
            return cached
        context = RankingContext(
            {memory_id: record["memory"] for memory_id, record in records.items()},
            include_derived=include_derived,
        )
        self._cache_contexts[include_derived] = context
        return context

    def _outgoing_edges(self, memory: dict[str, Any]) -> list[dict[str, Any]]:
        edges: list[dict[str, Any]] = []
        for related in memory["relationships"].get("related", []):
            edges.append({"type": "related", "from": memory["id"], "to": related["id"], "reason": related["reason"]})
        for target in memory["relationships"].get("supersedes", []):
            edges.append({"type": "supersedes", "from": memory["id"], "to": target})
        for target in memory["relationships"].get("superseded_by", []):
            edges.append({"type": "superseded_by", "from": memory["id"], "to": target})
        return edges

    def _light_record(self, memory_id: str, path: Path, memory: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": memory_id,
            "status": memory.get("status"),
            "description": memory.get("description"),
            "labels": memory.get("labels", []),
            "tags": memory.get("tags", []),
            "triggers": memory.get("triggers", []),
            "file": path.relative_to(self.memory_root).as_posix(),
        }

    def _entry_text(self, entry: dict[str, Any]) -> str:
        chunks = [
            entry.get("id", ""),
            entry.get("description", ""),
            " ".join(entry.get("tags", [])),
            " ".join(entry.get("labels", [])),
            " ".join(entry.get("triggers", [])),
        ]
        return "\n".join(chunks).lower()

    def _normalize_status_filter(self, status_filter: list[str] | str | None) -> set[str] | None:
        if status_filter is None:
            return {"active", "stale"}
        if status_filter == "all":
            return None
        statuses = [status_filter] if isinstance(status_filter, str) else status_filter
        normalized = set(statuses)
        unknown = normalized - VALID_STATUSES
        if unknown:
            raise StoreError(f"Unknown statuses: {', '.join(sorted(unknown))}")
        return normalized

    def _find_memory_path(self, memory_id: str) -> Path:
        path = self._memory_path_or_none(memory_id)
        if path is None:
            raise StoreError(f"Unknown memory id: {memory_id}")
        return path

    def _memory_path_or_none(self, memory_id: str) -> Path | None:
        if not ID_RE.match(memory_id):
            raise StoreError("Memory id must be lowercase kebab-case.")
        path = self._memory_path(memory_id)
        return path if path.exists() else None

    def _memory_path(self, memory_id: str) -> Path:
        return self.active_root / f"{memory_id}.json"

    def _read_json(self, path: Path) -> dict[str, Any]:
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError as exc:
            hint = ""
            if path == self.labels_path:
                hint = " If the store is not initialized yet, run 'project-memory-mcp init'."
            raise StoreError(f"Missing file: {path}.{hint}") from exc
        except json.JSONDecodeError as exc:
            raise StoreError(f"Invalid JSON in {path}: {exc}") from exc

    def _write_json(self, path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(value, indent=2, ensure_ascii=True) + "\n"
        path.write_text(text, encoding="utf-8")

    def _deep_merge(self, base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
        merged = copy.deepcopy(base)
        for key, value in patch.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = self._deep_merge(merged[key], value)
            else:
                merged[key] = value
        return merged

    # --------------------------------------------------- internal: validation

    def _validate_label_registry(self, errors: list[str]) -> set[str] | None:
        if not self.labels_path.is_file():
            errors.append(f"Missing label registry: {self.labels_path}")
            return None
        try:
            registry = self._read_json(self.labels_path)
        except StoreError as exc:
            errors.append(str(exc))
            return None
        if registry.get("schema_version") != 1:
            errors.append(f"{self.labels_path}: schema_version must be 1.")
        labels = registry.get("labels")
        if not isinstance(labels, dict):
            errors.append(f"{self.labels_path}: missing labels object.")
            return None
        for label, data in labels.items():
            if not LABEL_RE.match(label):
                errors.append(f"{self.labels_path}: label '{label}' must be prefix:kebab-case.")
            description = data.get("description") if isinstance(data, dict) else None
            if not isinstance(description, str) or not description.strip():
                errors.append(f"{self.labels_path}: label '{label}' must have a description.")
        return set(labels.keys())

    def _validate_memory_files(
        self, errors: list[str], known_labels: set[str] | None
    ) -> dict[str, dict[str, Any]]:
        records: dict[str, dict[str, Any]] = {}
        if not self.active_root.is_dir():
            return records
        for path in sorted(self.active_root.glob("*.json")):
            where = path.relative_to(self.root).as_posix()
            try:
                memory = json.loads(path.read_text(encoding="utf-8-sig"))
            except json.JSONDecodeError as exc:
                errors.append(f"{where}: not valid JSON: {exc}")
                continue
            errors.extend(self.validate_memory(memory, known_labels, where))
            if not isinstance(memory, dict):
                continue
            memory_id = memory.get("id")
            if isinstance(memory_id, str):
                if memory_id != path.stem:
                    errors.append(f"{where}: id '{memory_id}' must match filename '{path.stem}'.")
                if memory_id in records:
                    errors.append(f"{where}: duplicate memory id '{memory_id}'.")
                else:
                    records[memory_id] = {"path": path, "memory": memory, "where": where}
        return records

    def _validate_relationship_graph(self, errors: list[str], records: dict[str, dict[str, Any]]) -> None:
        for memory_id, record in records.items():
            where = record["where"]
            relationships = record["memory"].get("relationships")
            if not isinstance(relationships, dict):
                continue
            for entry in relationships.get("related") or []:
                if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
                    continue
                target_id = entry["id"]
                if target_id == memory_id:
                    errors.append(f"{where}: relationships.related must not reference itself.")
                    continue
                if target_id not in records:
                    errors.append(f"{where}: relationships.related references unknown memory '{target_id}'.")
                    continue
                target_related = records[target_id]["memory"].get("relationships", {}).get("related") or []
                reverse = next(
                    (item for item in target_related if isinstance(item, dict) and item.get("id") == memory_id),
                    None,
                )
                if reverse is None:
                    errors.append(f"{where}: relationships.related '{target_id}' is not bidirectional.")
                elif reverse.get("reason") != entry.get("reason"):
                    errors.append(
                        f"{where}: relationships.related '{target_id}' reason must match "
                        "the reverse relationship reason."
                    )
            for source_field, mirror_field in (("supersedes", "superseded_by"), ("superseded_by", "supersedes")):
                for target_id in relationships.get(source_field) or []:
                    if not isinstance(target_id, str):
                        continue
                    if target_id not in records:
                        errors.append(
                            f"{where}: relationships.{source_field} references unknown memory '{target_id}'."
                        )
                        continue
                    mirror = records[target_id]["memory"].get("relationships", {}).get(mirror_field) or []
                    if memory_id not in mirror:
                        errors.append(
                            f"{where}: relationships.{source_field} '{target_id}' is not mirrored "
                            f"by {mirror_field}."
                        )

    def _check_string_array(
        self,
        errors: list[str],
        where: str,
        container: dict[str, Any],
        field: str,
        unique: bool = False,
        min_items: int = 0,
        item_re: re.Pattern[str] | None = None,
        prefix: str = "",
    ) -> None:
        value = container.get(field)
        name = f"{prefix}{field}"
        if not isinstance(value, list):
            errors.append(f"{where}: field '{name}' must be an array.")
            return
        if len(value) < min_items:
            errors.append(f"{where}: field '{name}' must have at least {min_items} item(s).")
        for item in value:
            if not isinstance(item, str):
                errors.append(f"{where}: field '{name}' must contain only strings.")
                return
            if item_re is not None and not item_re.match(item):
                errors.append(f"{where}: field '{name}' value '{item}' has an invalid format.")
        if unique and len(set(value)) != len(value):
            errors.append(f"{where}: field '{name}' contains duplicate values.")
