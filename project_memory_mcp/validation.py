"""Memory document validation and the label query grammar.

Pure functions over a memory dict: no store, no filesystem, no database. This
is also where ``StoreError`` lives, so that every layer raises and catches one
exception class rather than one per backend.
"""

from __future__ import annotations

import re
from typing import Any, Callable


class StoreError(ValueError):
    """Raised for any invalid input, invalid store state, or failed operation."""

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
