"""Several servers may hold the same project. They are sources, not replicas.

The distinction is the whole design. Replicas must converge, which costs version
vectors, tombstone propagation and conflict resolution. Sources are *meant* to
differ - the way two sites covering one topic overlap without anyone merging
their databases - so none of that machinery is needed. The client never merges
data. It merges ranked lists, which are bounded by K per source and thrown away
after the query.

Three properties this rests on:

**Local always answers.** Remotes are queried concurrently with a per-remote
deadline; one that is slow or asleep is dropped from that query and the response
says which sources replied. An incomplete answer that arrives beats a complete
one that does not.

**Merging is by rank, not score.** BM25's IDF is computed over each server's own
corpus, so a 1.3 from one and a 1.3 from another do not mean the same thing.
Reciprocal rank fusion depends only on position, needs no calibration, and
degrades gracefully when a source drops out.

**Dedup across sources is free.** A memory promoted to two servers carries one
uuid on both, so it fuses into a single result scoring from both lists. Two
memories written independently about the same thing have different uuids and
both appear, which is correct.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from . import identity
from .validation import StoreError

#: Reciprocal rank fusion's damping constant. 60 is the value from the original
#: paper and the one every implementation uses; it keeps the top few results
#: from dominating while still respecting order.
RRF_K = 60

#: A remote gets this long to answer before the query proceeds without it.
#: Short on purpose: recall is on the agent's critical path, and a stale-but-
#: instant answer is worth more than a complete one that arrives too late.
REMOTE_TIMEOUT_SECONDS = 4.0

MAX_PARALLEL_REMOTES = 8


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Remote:
    name: str
    url: str
    description: str | None
    token: str | None
    enabled: bool

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "url": self.url,
                "description": self.description or "", "enabled": self.enabled}


def _row_to_remote(row: sqlite3.Row) -> Remote:
    return Remote(name=row["name"], url=row["url"], description=row["description"],
                  token=row["token"], enabled=bool(row["enabled"]))


def add_remote(connection: sqlite3.Connection, name: str, url: str,
               description: str | None = None, token: str | None = None) -> dict[str, Any]:
    """Register a server this machine will federate with."""
    if not name or not name.replace("-", "").replace("_", "").isalnum():
        raise StoreError("A remote name should be a simple identifier.")
    if not url.startswith(("http://", "https://")):
        raise StoreError("A remote url must start with http:// or https://")
    with connection:
        connection.execute(
            "INSERT INTO remotes(name, url, description, token, added) VALUES (?,?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET url=excluded.url, description=excluded.description, "
            "token=excluded.token",
            (name, url.rstrip("/"), description, token, _now()))
    return {"added": name, "url": url}


def remove_remote(connection: sqlite3.Connection, name: str) -> dict[str, Any]:
    with connection:
        connection.execute("DELETE FROM remotes WHERE name=?", (name,))
    return {"removed": name}


def list_remotes(connection: sqlite3.Connection, enabled_only: bool = False) -> list[Remote]:
    sql = "SELECT * FROM remotes"
    if enabled_only:
        sql += " WHERE enabled=1"
    return [_row_to_remote(row) for row in connection.execute(sql + " ORDER BY name")]


class RemoteClient:
    """One remote, spoken to over the MCP tools it already exposes.

    There is no second protocol: a remote is just a project-memory server, and
    the tools are the query API. Requests are signed when this machine has a
    key, so nothing reusable travels even without TLS.
    """

    def __init__(self, remote: Remote, project: str, private_key: Any = None) -> None:
        self.remote = remote
        self.project = project
        self.private_key = private_key

    def _endpoint(self) -> str:
        return f"/mcp?project={self.project}"

    def call(self, tool: str, arguments: dict[str, Any],
             timeout: float = REMOTE_TIMEOUT_SECONDS) -> dict[str, Any]:
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": tool, "arguments": arguments}}).encode()
        path = self._endpoint()
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if self.private_key is not None:
            headers.update(identity.sign_request(self.private_key, "POST", path, body))
        elif self.remote.token:
            headers["Authorization"] = "Bearer " + self.remote.token
        request = urllib.request.Request(self.remote.url + path, data=body, headers=headers)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode())
        result = payload.get("result") or {}
        if result.get("isError"):
            raise StoreError(result["content"][0]["text"])
        if "error" in payload:
            raise StoreError(str(payload["error"].get("message") or payload["error"]))
        return json.loads(result["content"][0]["text"])


def fan_out(clients: list[RemoteClient], tool: str, arguments: dict[str, Any],
            timeout: float = REMOTE_TIMEOUT_SECONDS) -> tuple[dict[str, Any], dict[str, str]]:
    """Ask every remote at once. Returns what answered, and why the rest did not.

    Concurrent rather than sequential, so latency is the slowest responder
    instead of the sum. Failures are collected rather than raised: one
    unreachable source must not take down a query the others can answer.
    """
    answers: dict[str, Any] = {}
    failures: dict[str, str] = {}
    if not clients:
        return answers, failures

    def ask(client: RemoteClient) -> tuple[str, Any, str | None]:
        try:
            return client.remote.name, client.call(tool, arguments, timeout=timeout), None
        except (urllib.error.URLError, StoreError, OSError, ValueError, KeyError) as error:
            return client.remote.name, None, str(error)

    with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_REMOTES, len(clients))) as pool:
        for name, payload, error in pool.map(ask, clients):
            if error is None:
                answers[name] = payload
            else:
                failures[name] = error
    return answers, failures


def fuse(ranked_lists: dict[str, list[dict[str, Any]]], limit: int) -> list[dict[str, Any]]:
    """Reciprocal rank fusion over per-source result lists.

    Scores from different servers are not comparable - each is computed against
    its own corpus - so only position is used. A memory appearing in several
    lists accumulates from each, which is what makes agreement across sources
    count for something without needing them to agree on a scale.
    """
    scores: dict[str, float] = {}
    best: dict[str, dict[str, Any]] = {}
    sources: dict[str, list[str]] = {}
    for source, results in ranked_lists.items():
        for rank, entry in enumerate(results):
            key = entry.get("uuid") or f"{source}:{entry['id']}"
            scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_K + rank + 1)
            sources.setdefault(key, []).append(source)
            # Keep the copy from whichever source ranked it highest, so the
            # inlined body comes from the server that matched it best.
            if key not in best or rank < best[key]["_rank"]:
                best[key] = {**entry, "_rank": rank, "_source": source}

    fused = []
    for key, score in sorted(scores.items(), key=lambda kv: (-kv[1], best[kv[0]]["id"])):
        entry = dict(best[key])
        entry.pop("_rank", None)
        entry["sources"] = sorted(set(sources[key]))
        entry["fused_score"] = round(score, 6)
        fused.append(entry)
    return fused[:limit]


def choose_remote(remotes: list[Remote], memory: dict[str, Any],
                  used: list[str] | None = None) -> list[dict[str, Any]]:
    """Rank remotes as destinations for one memory, best first.

    Two signals, in this order:

    1. **Which remotes were actually consulted while solving the task.** If the
       answer came from the team server and the personal one was never touched,
       the lesson belongs where the conversation happened. This is evidence.
    2. **How well the memory matches a remote's description.** A guess, and only
       the tiebreak.

    The agent decides. This returns the reasoning rather than acting on it,
    because matching a lesson to a one-line description is exactly the kind of
    judgment a score is bad at and a reader is good at.
    """
    from .ranking import tokenize

    used = used or []
    text = " ".join([memory.get("description") or ""]
                    + list(memory.get("triggers") or [])
                    + list(memory.get("labels") or []))
    words = set(tokenize(text))
    ranked = []
    for remote in remotes:
        if not remote.enabled:
            continue
        described = set(tokenize(remote.description or ""))
        overlap = len(words & described) / len(described) if described else 0.0
        consulted = remote.name in used
        ranked.append({
            "name": remote.name, "url": remote.url,
            "description": remote.description or "",
            "consulted_during_task": consulted,
            "description_match": round(overlap, 3),
            "why": ("you queried this remote while solving the task"
                    if consulted else
                    f"description overlap {overlap:.0%}" if overlap else
                    "no signal either way"),
        })
    ranked.sort(key=lambda r: (not r["consulted_during_task"], -r["description_match"], r["name"]))
    return ranked
