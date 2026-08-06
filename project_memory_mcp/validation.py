"""Memory document validation, shared by every storage backend.

Pure functions over a memory dict: no store, no filesystem, no database. The
file backend and the SQLite backend validate identically, so a memory that is
valid in one is valid in the other.
"""

from __future__ import annotations

import re
from typing import Any

VALID_STATUSES = {"active", "stale", "superseded", "wrong"}
ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
LABEL_RE = re.compile(r"^[a-z]+:[a-z0-9]+(-[a-z0-9]+)*$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

MEMORY_FIELDS = (
    "schema_version", "id", "status", "description", "tags", "labels", "scope",
    "triggers", "remembered_facts", "solution_pattern", "pitfalls", "evidence",
    "relationships",
)
SCOPE_REQUIRED_FIELDS = ("project", "area", "files")
SCOPE_FIELDS = ("project", "area", "files", "applies_to")
EVIDENCE_REQUIRED_FIELDS = ("created_from_task", "last_validated")
EVIDENCE_FIELDS = ("created_from_task", "last_validated", "created")
RELATIONSHIP_FIELDS = ("related", "supersedes", "superseded_by")


def validate_memory(memory: Any, known_labels: set[str] | None, where: str) -> list[str]:
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
    check_string_array(errors, where, memory, "tags", unique=True)
    check_string_array(errors, where, memory, "labels", unique=True, min_items=1, item_re=LABEL_RE)
    check_string_array(errors, where, memory, "triggers", unique=True, min_items=1)
    check_string_array(errors, where, memory, "remembered_facts", min_items=1)
    check_string_array(errors, where, memory, "solution_pattern")
    check_string_array(errors, where, memory, "pitfalls")

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
                check_string_array(errors, where, scope, field, unique=True, prefix="scope.")

    evidence = memory["evidence"]
    if not isinstance(evidence, dict):
        errors.append(f"{where}: evidence must be an object.")
    else:
        for field in EVIDENCE_REQUIRED_FIELDS:
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
        if "created" in evidence and not isinstance(evidence["created"], str):
            errors.append(f"{where}: evidence.created must be an ISO-8601 string.")

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
                check_string_array(
                    errors, where, relationships, field, unique=True, item_re=ID_RE, prefix="relationships."
                )
    return errors


def check_string_array(
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
