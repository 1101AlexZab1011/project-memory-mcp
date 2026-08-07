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
from contextlib import contextmanager
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


def set_remote_enabled(connection: sqlite3.Connection, name: str, enabled: bool) -> dict[str, Any]:
    """Stop or resume federating with a remote without forgetting it.

    `enabled` was read in six places and written by nothing but the schema
    default, so the only way to stop talking to a server was to remove it -
    which discards its url, token and description. Those are different
    intentions: "down for a week" should not cost you the credential you need to
    come back.

    A disabled remote keeps its queue. That is what disabling means: recall
    stops asking it and delivery stops trying, and both resume where they left
    off. Contrast `remove_remote`, which drops the queue because there is no
    longer anywhere for it to go.
    """
    if not connection.execute("SELECT 1 FROM remotes WHERE name=?", (name,)).fetchone():
        raise StoreError(f"Unknown remote: {name}")
    with connection:
        connection.execute("UPDATE remotes SET enabled=? WHERE name=?", (1 if enabled else 0, name))
    return {"remote": name, "enabled": bool(enabled)}


def remove_remote(connection: sqlite3.Connection, name: str) -> dict[str, Any]:
    """Forget a remote, and anything queued for it.

    The queue has to go with it. `deliver_outbox` builds its lookup from enabled
    remotes, so a promotion queued to a server that no longer exists was skipped
    on every run - never delivered, never dropped, never reported. Same family
    as the orphan a deleted memory used to leave, different cause: there is
    nowhere to send it, and no url or token left to reach anywhere with.

    Use `set_remote_enabled(..., False)` to stop federating and keep the queue.
    """
    with connection:
        cancelled = connection.execute(
            "SELECT COUNT(*) AS n FROM outbox WHERE remote=?", (name,)).fetchone()["n"]
        connection.execute("DELETE FROM outbox WHERE remote=?", (name,))
        connection.execute("DELETE FROM remotes WHERE name=?", (name,))
    result: dict[str, Any] = {"removed": name}
    if cancelled:
        # Said out loud: removing a remote is not obviously a decision about
        # unpublished work, and whoever did it may not have known there was any.
        result["cancelled_promotions"] = cancelled
    return result


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
        # A token configured for *this* remote wins, and the CLI is why: `remote
        # --token` is documented as "if this machine has no enrolled key there",
        # so setting one is the operator saying which credential works on that
        # server. Signing anyway would break every existing token setup the
        # moment this machine enrolled a key with some unrelated server - the
        # remote would reject a fingerprint it has never seen.
        #
        # Where no token is configured, sign. That is the `join` path, and it is
        # the better posture: nothing reusable crosses the wire, which matters
        # because this deployment has no TLS.
        if self.remote.token:
            headers["Authorization"] = "Bearer " + self.remote.token
        elif self.private_key is not None:
            headers.update(identity.sign_request(self.private_key, "POST", path, body))
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


#: A memory must have survived at least this tier before it can be published.
#: Otherwise "earn your way into the shared store" is decorative: anything
#: written thirty seconds ago could be pushed to everyone. Overridable for the
#: rare-but-critical lesson that will never accrue usage on its own.
MIN_PROMOTION_TIER = 2


def promotion_targets(store: Any, memory_id: str,
                      used: list[str] | None = None) -> dict[str, Any]:
    """Where this memory could go, ranked, with the reasoning shown.

    Only public memories are offered a destination. A private memory is private
    because of who it is for, not because it has not proven itself, so no amount
    of usage should ever route it outward.
    """
    from . import secret_scan

    if store.publication_state(memory_id)["visibility"] != "public":
        return {"id": memory_id, "visibility": "private", "targets": [],
                "why": "private memories are never promoted; change visibility first"}
    body = store.get_memory(memory_id)
    result: dict[str, Any] = {
        "id": memory_id, "visibility": "public",
        "targets": choose_remote(list_remotes(store.connection), body, used)}
    # Reported here as well as enforced at promotion, so the hold is something
    # an agent can see coming and fix rather than run into.
    findings = secret_scan.scan(body)
    if findings:
        result["blocked"] = "looks like it contains a secret"
        result["secret_findings"] = [f.describe() for f in findings]
    return result


def promote(store: Any, memory_id: str, remote: str,
            force: bool = False, allow_secrets: bool = False) -> dict[str, Any]:
    """Queue one memory for publication to one named remote.

    Never to all of them: that is how one lesson ends up duplicated across
    servers with independently diverging edits.

    **This does not touch the network.** Everything checkable locally is checked
    here, and then the work goes in the outbox for the Computer to deliver.
    Publishing inline would put a remote's availability on the request path: one
    promotion to an unreachable server would hold this project's lock for the
    whole connect timeout, stalling every other request including reads.

    So the answer is always "queued", never "published". An agent gets an
    immediate, honest answer about whether the memory is *allowed* out, and finds
    out separately whether it got there - see ``outbox_status``.

    ``force`` overrides the tier requirement; ``allow_secrets`` overrides the
    credential scan. They are separate arguments because they are separate
    judgments - "this matters more than its usage shows" and "that string is not
    a secret" are not the same claim, and one should never imply the other.
    """
    from . import secret_scan

    state = store.publication_state(memory_id)
    if state["visibility"] != "public":
        raise StoreError(
            f"'{memory_id}' is private. Set its visibility to public before promoting it.")
    if state["borrowed"]:
        raise StoreError(
            f"'{memory_id}' is a cached copy from another server, not yours to publish.")
    if state["tier"] < MIN_PROMOTION_TIER and not force:
        raise StoreError(
            f"'{memory_id}' is still tier {state['tier']} and has not earned publication "
            f"(needs tier {MIN_PROMOTION_TIER}). Pass force=true only for a lesson that "
            f"matters more than its usage will ever show.")

    known = list_remotes(store.connection)
    if remote in {r.name for r in known if not r.enabled}:
        # Distinguished from unknown, which it is not. This branch could not be
        # reached until remotes could be disabled at all, and it answered
        # "Unknown remote 'team'. Known: team" - a message that contradicts
        # itself in the same breath.
        raise StoreError(
            f"Remote '{remote}' is disabled. Re-enable it with "
            f"`project-memory-mcp remote --enable {remote}`, or promote somewhere else.")
    if remote not in {r.name for r in known if r.enabled}:
        names = ", ".join(r.name for r in known) or "(none configured)"
        raise StoreError(f"Unknown remote '{remote}'. Known: {names}")

    body = store.get_memory(memory_id)
    # Last gate before the memory leaves this machine. A secret in a local
    # memory sits on the host that already has it; this is the crossing. Checked
    # here as well as at delivery so the refusal reaches whoever can act on it,
    # rather than only a job log nobody reads.
    if not allow_secrets:
        findings = secret_scan.scan(body)
        if findings:
            raise StoreError(secret_scan.explain(memory_id, findings))

    memory_uuid = state["uuid"]
    with store.connection:
        # Queueing the same memory for the same remote twice would deliver it
        # twice. Re-queueing is a legitimate thing to ask for - after an edit, or
        # after a failure - so it refreshes the existing row instead of refusing,
        # and clears the stale error with it.
        existing = store.connection.execute(
            "SELECT id FROM outbox WHERE project_id=? AND memory_id=? AND remote=?",
            (store.project, memory_uuid, remote)).fetchone()
        if existing:
            store.connection.execute(
                "UPDATE outbox SET queued_at=?, last_error=NULL WHERE id=?",
                (_now(), existing["id"]))
        else:
            store.connection.execute(
                "INSERT INTO outbox(project_id, memory_id, remote, queued_at) VALUES (?,?,?,?)",
                (store.project, memory_uuid, remote, _now()))
    waiting = store.connection.execute(
        "SELECT COUNT(*) n FROM outbox WHERE project_id=?", (store.project,)).fetchone()["n"]
    return {"queued": memory_id, "remote": remote, "waiting": waiting,
            "note": "delivery happens in the background; check outbox_status"}


def outbox_status(store: Any, limit: int = 50) -> dict[str, Any]:
    """What is waiting to be published, and why it has not gone yet.

    The receipt for a queued promotion. Without it "queued" is a promise with
    nothing to check it against.
    """
    rows = store.connection.execute(
        "SELECT o.memory_id, o.remote, o.queued_at, o.attempts, o.last_error, m.slug "
        "FROM outbox o LEFT JOIN memories m "
        "  ON m.project_id=o.project_id AND m.uuid=o.memory_id "
        "WHERE o.project_id=? ORDER BY o.id LIMIT ?", (store.project, limit)).fetchall()
    return {"count": len(rows), "queued": [
        {"id": row["slug"] or row["memory_id"], "remote": row["remote"],
         "queued_at": row["queued_at"], "attempts": row["attempts"],
         "last_error": row["last_error"]} for row in rows]}


def deliver_outbox(store: Any, private_key: Any = None, limit: int = 50,
                   db_lock: Any = None) -> dict[str, Any]:
    """Send everything queued for publication. Safe to call as often as you like.

    The other half of the outbox. `promote` decides whether a memory may leave;
    this decides whether it can get there, which is the part that needs a
    network and therefore the part that must not run on a request thread.

    ``db_lock`` is taken around each item's database work and released across
    the send. Holding it for the whole drain would reproduce, in the background
    worker, exactly the bug the outbox was built to fix: fifty queued items to a
    dead server is fifty connect timeouts, and with the lock held that is
    minutes in which no request for this project can be served.

    Bookkeeping happens against the store's connection rather than through store
    methods, because delivery is this module's job and giving the store an API
    for each step would put the outbox back inside it.
    """
    from . import secret_scan

    with _held(db_lock):
        rows = store.connection.execute(
            "SELECT o.id, o.memory_id, o.remote, m.slug, m.archived_at FROM outbox o "
            "LEFT JOIN memories m ON m.project_id=o.project_id AND m.uuid=o.memory_id "
            "WHERE o.project_id=? ORDER BY o.id LIMIT ?", (store.project, limit)).fetchall()
        by_name = {r.name: r for r in list_remotes(store.connection, enabled_only=True)}
    sent: list[str] = []
    held: list[str] = []
    dropped: list[str] = []
    for row in rows:
        # A queue entry whose memory is gone or withdrawn is not a delivery that
        # failed - it is a delivery that no longer means anything, and retrying
        # it forever is the wrong answer twice over. `delete_memory` now removes
        # these on the way out, so this handles the rows already sitting in
        # databases written before it did, and any future path that removes a
        # memory without going through it.
        #
        # Archived counts as gone. `merge_memories` archives the memory it
        # folded away rather than deleting it, so its slug survives and the
        # queued promotion stays technically deliverable - publishing a lesson
        # that was just merged into another is not what anybody asked for.
        if row["slug"] is None or row["archived_at"] is not None:
            dropped.append(row["slug"] or row["memory_id"])
            with _held(db_lock), store.connection:
                store.connection.execute("DELETE FROM outbox WHERE id=?", (row["id"],))
            continue
        remote = by_name.get(row["remote"])
        if remote is None:
            continue
        # Scanned again on the way out, not only on the way in: the body is
        # re-read here, so an edit made while the item sat in the queue would
        # otherwise ship unexamined.
        with _held(db_lock):
            body = store.get_memory(row["slug"])
            findings = secret_scan.scan(body)
            if findings:
                held.append(body["id"])
                with store.connection:
                    store.connection.execute(
                        "UPDATE outbox SET last_error=? WHERE id=?",
                        ("held: " + "; ".join(f.describe() for f in findings), row["id"]))
        if findings:
            continue
        try:
            # No lock here. This is the part that waits on somebody else.
            RemoteClient(remote, store.project, private_key).call(
                "create_memory", {"memory": body, "visibility": "public",
                                  "uuid": row["memory_id"]})
        except Exception as error:  # noqa: BLE001 - any failure means "try again later"
            with _held(db_lock), store.connection:
                store.connection.execute(
                    "UPDATE outbox SET attempts=attempts+1, last_error=? WHERE id=?",
                    (str(error), row["id"]))
            continue
        sent.append(body["id"])
        with _held(db_lock), store.connection:
            store.connection.execute("DELETE FROM outbox WHERE id=?", (row["id"],))
            # Recorded here because this is the only place a delivery succeeds,
            # so it is the only place that learns the remote is up.
            store.connection.execute(
                "UPDATE remotes SET last_ok=?, last_error=NULL WHERE name=?",
                (_now(), row["remote"]))
    result: dict[str, Any] = {"sent": sent,
                              "still_queued": len(rows) - len(sent) - len(dropped)}
    if held:
        # Named separately from a delivery failure: retrying will not help,
        # somebody has to edit the memory.
        result["held_for_secrets"] = held
    if dropped:
        # Named separately again, and for the opposite reason: nothing is wrong
        # and nothing needs doing. It goes in the job log so that a promotion
        # which quietly stopped existing leaves a trace of why.
        result["dropped_memory_gone"] = dropped
    return result


@contextmanager
def _held(lock: Any):
    """Hold a lock if there is one. There is not, outside the HTTP server."""
    if lock is None:
        yield
        return
    with lock:
        yield


def recall_across(store: Any, query: str = "", limit: int = 8, full_count: int = 3,
                  private_key: Any = None, db_lock: Any = None, **kwargs: Any) -> dict[str, Any]:
    """Recall on this machine and every enabled remote, fused by rank.

    This lives here rather than on the store because it opens sockets, and the
    store must not. A store method that blocks on a remote is a store method
    that can hold the project lock for a connect timeout - the same defect the
    outbox fixed for promotion.

    The order matters and is the point of the function. Local is queried first
    and always answers, so a machine with unreachable remotes still gets its own
    memories.

    ``db_lock`` is the caller's per-project lock, and this takes it for the two
    database phases only - never across the fan-out. That is the whole reason
    this function exists rather than a store method: the lock protects SQLite,
    the network is not SQLite, and holding one for the other is what turns a
    slow remote into a stalled server.
    """
    with _held(db_lock):
        local = store.recall(query=query, limit=limit, full_count=full_count, **kwargs)
        remotes = list_remotes(store.connection, enabled_only=True)
    if not remotes:
        return local

    clients = [RemoteClient(remote, store.project, private_key) for remote in remotes]
    answers, failures = fan_out(clients, "recall",
                                {"query": query, "limit": limit, "full_count": 0})
    lists = {"local": local["memories"]}
    for name, payload in answers.items():
        for entry in payload.get("memories") or []:
            entry["remote"] = name
        lists[name] = payload.get("memories") or []
    fused = fuse(lists, limit)
    with _held(db_lock):
        store.cache_remote_results(answers)

    return {
        "query": query,
        "count": len(fused),
        "memories": fused,
        "sources_answered": sorted(lists),
        # Reported rather than hidden: an agent should know it is looking at a
        # partial view of what the network holds.
        "sources_unreachable": failures,
        "considered": local.get("considered", 0) + sum(len(v) for v in lists.values()),
    }


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
