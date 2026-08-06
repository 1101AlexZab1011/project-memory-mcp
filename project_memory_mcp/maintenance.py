"""Checks that read something other than the database.

A short module on purpose. It holds the work that needs the *world* rather than
the store: whether the files a memory names still exist, and which pairs of
memories look like the same lesson written twice.

Both are the Computer's jobs, and both are judgments about content rather than
operations on rows - which is why neither belongs on a class whose subject is
SQLite.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: Fields whose text carries the lesson, and therefore the fields worth
#: comparing when asking whether two memories say the same thing.
COMPARED_FIELDS = ("triggers", "remembered_facts", "solution_pattern", "pitfalls")


def _bag(memory: dict[str, Any]) -> set[str]:
    from .ranking import tokenize

    parts = [memory.get("description") or ""]
    for field in COMPARED_FIELDS:
        parts.extend(str(v) for v in (memory.get(field) or []))
    return set(tokenize(" ".join(parts)))


def text_similarity(left: dict[str, Any], right: dict[str, Any]) -> float:
    """Token overlap across the fields that carry the lesson.

    Deliberately crude. Its job is to nominate pairs worth an agent's attention,
    not to decide anything, so a cheap symmetric measure over the text a human
    would actually compare is the right amount of machinery.
    """
    a, b = _bag(left), _bag(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def duplicate_candidates(store: Any, limit: int = 25,
                         threshold: float = 0.6) -> dict[str, Any]:
    """Pairs that look like the same lesson written twice.

    Similarity nominates; it never decides. A score is good at finding pairs
    worth reading and bad at telling whether two statements mean the same thing,
    so this returns both memories in full for an agent to judge.

    Candidates come from the derived edges already materialised on write - the
    memories that share labels or files - so this costs one pass over a capped
    edge set rather than comparing every pair.
    """
    rows = store.connection.execute(
        "SELECT DISTINCT ma.slug AS a, mb.slug AS b FROM edges e "
        "JOIN memories ma ON ma.project_id=e.project_id AND ma.uuid=e.src "
        "  AND ma.archived_at IS NULL "
        "JOIN memories mb ON mb.project_id=e.project_id AND mb.uuid=e.dst "
        "  AND mb.archived_at IS NULL "
        "WHERE e.project_id=? AND e.kind='derived' AND e.src < e.dst", (store.project,)
    ).fetchall()

    pairs = []
    for row in rows:
        left, right = store.get_memory(row["a"]), store.get_memory(row["b"])
        score = text_similarity(left, right)
        if score >= threshold:
            pairs.append({"score": round(score, 3), "memories": [left, right]})
    pairs.sort(key=lambda p: -p["score"])
    return {"count": len(pairs), "threshold": threshold, "candidates": pairs[:limit]}


def check_anchors(store: Any, mark_stale: bool = False) -> dict[str, Any]:
    """Find memories whose files no longer exist.

    The one correctness check that is a fact rather than a judgment: either the
    paths are there or they are not. It can only run where the code is, which is
    why it belongs to a local installation and never to a server - a server holds
    memories about repositories it cannot see, and there this reports having
    nothing to check rather than declaring everything adrift.

    Marking stale rather than wrong: a file that vanished may have moved, and
    stale says "check this against current code", which is exactly the
    instruction the evidence supports. One surviving anchor is enough - a memory
    spanning four files has not gone stale because one of them moved.
    """
    root = store.root_path()
    if not root:
        return {"checked": 0, "skipped": "no root_path recorded for this project"}
    base = Path(root)
    if not base.is_dir():
        return {"checked": 0, "skipped": f"root_path {root} is not a directory here"}

    rows = store.connection.execute(
        "SELECT uuid, slug, body, status FROM memories WHERE project_id=? "
        "AND archived_at IS NULL AND origin_remote IS NULL", (store.project,)).fetchall()
    checked = 0
    adrift: list[dict[str, Any]] = []
    for row in rows:
        body = json.loads(row["body"])
        files = [f for f in (body.get("scope") or {}).get("files") or [] if isinstance(f, str)]
        if not files:
            continue  # nothing anchored it in the first place
        checked += 1
        missing = [f for f in files if not (base / f).exists()]
        if len(missing) != len(files):
            continue  # at least one anchor still stands
        adrift.append({"id": row["slug"], "files": missing, "status": row["status"]})
        if mark_stale and row["status"] == "active":
            store.update_memory(row["slug"], {"status": "stale"})
    return {"checked": checked, "adrift": adrift, "marked_stale": bool(mark_stale)}
