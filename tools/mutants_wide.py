"""A second mutation set, covering the modules the first one did not reach.

The first pass took the properties I had just written and asked whether anything
guarded them. This one goes after the parts that have been sitting there since
earlier phases and have never been challenged: authentication, signatures,
ranking, the usage counters, rank fusion, messaging limits, and the store's own
cleanup.

Same contract. Each entry breaks one property. Anything reported as SURVIVED is
a property nothing actually guards - which is a statement about the tests, not
about whether the code happens to work today.

    python tools/mutants_wide.py
"""
import atexit
import pathlib
import signal
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

#: A mutant that hangs is worse than one that fails: it tells you nothing and it
#: blocks whatever is driving the run. Anything past this is reported as HUNG.
RUN_TIMEOUT_SECONDS = 300

#: Written beside a file while it is mutated. If this process is killed - which
#: is exactly what happened once - the next run finds the backup and puts the
#: original back, rather than leaving a sabotaged source in the working tree for
#: somebody to commit.
BACKUP_SUFFIX = ".mutation-backup"


def restore_orphans() -> None:
    for backup in sorted((ROOT / "project_memory_mcp").glob("*" + BACKUP_SUFFIX)):
        target = backup.with_suffix("")
        target.write_text(backup.read_text(encoding="utf-8"), encoding="utf-8")
        backup.unlink()
        print(f"  restored {target.name} left mutated by an earlier run", flush=True)


class AmbiguousMutation(Exception):
    """The pattern matches more than once, so which line is broken is a guess."""


class Mutation:
    """Applies one edit, and puts the file back however this process ends."""

    def __init__(self, path: pathlib.Path, old: str, new: str) -> None:
        self.path, self.good = path, path.read_text(encoding="utf-8")
        self.backup = path.with_suffix(path.suffix + BACKUP_SUFFIX)
        # Exactly once, or refuse. `replace(old, new, 1)` takes the *first*
        # match, so an ambiguous pattern quietly sabotages a different function
        # than the one the mutant is named after - and then reports on it. That
        # happened: a two-line pattern meant for `cmd_serve` matched `cmd_audit`
        # three definitions earlier, survived, and was recorded as a gap in the
        # wrong place. A mutant that tests something other than its own name is
        # worse than no mutant.
        found = self.good.count(old)
        if found != 1:
            raise AmbiguousMutation(
                f"{path.name}: pattern matches {found} times, expected exactly 1")
        self.mutated = self.good.replace(old, new, 1)

    def __enter__(self) -> "Mutation":
        self.backup.write_text(self.good, encoding="utf-8")
        self.path.write_text(self.mutated, encoding="utf-8")
        atexit.register(self.__exit__, None, None, None)
        return self

    def __exit__(self, *_exc) -> bool:
        if self.backup.exists():
            self.path.write_text(self.good, encoding="utf-8")
            self.backup.unlink()
        return False


MUTANTS = [
    # ------------------------------------------------------------------- auth
    # The token comparison moved into clients.client_for_token when the UI login
    # started resolving a token to *which* client holds it rather than to yes or
    # no. Mutating the tail to hand back the unscoped shared client is the same
    # property in its new home: any string signs in, as anyone.
    ("the UI accepts any token", "clients.py",
     "    if shared_token and hmac.compare_digest(presented, shared_token):\n"
     "        return SHARED_TOKEN_CLIENT\n    return None",
     "    return SHARED_TOKEN_CLIENT"),

    ("expired sessions stay valid", "http_server.py",
     "            if expiry < time.time():",
     "            if False:"),

    ("session cookies lose HttpOnly and SameSite", "http_server.py",
     "\"; Path=/; HttpOnly; SameSite=Strict\"",
     "\"; Path=/\""),

    # "admin-only routes accept any client" was here. It survived because
    # _require_admin was called by nothing - dead code, not an untested guard -
    # and it has been deleted. See the note where it used to live.

    # One predicate now, asked by both transports. It used to be spelled inside
    # _require_project, which only the /mcp path ever called - so this mutant
    # passed while the UI was wide open. Pointing it at the shared predicate is
    # what makes it cover both.
    ("project scoping is not enforced", "http_server.py",
     "        return self.client is None or self.client.may_access(project)",
     "        return True"),

    ("a browser session forgets which client opened it", "http_server.py",
     "            self._issued[sid] = (time.time() + SESSION_TTL_SECONDS, client)",
     "            self._issued[sid] = (time.time() + SESSION_TTL_SECONDS, "
     "clients.SHARED_TOKEN_CLIENT)"),

    ("the UI login accepts a revoked client's token", "clients.py",
     "            return None if row[\"revoked_at\"] else _row_to_client(row)",
     "            return _row_to_client(row)"),

    ("a revoked client can still authenticate", "clients.py",
     "        if row[\"revoked_at\"]:\n            raise StoreError(\"This client has been revoked.\")",
     "        if False:\n            raise StoreError(\"This client has been revoked.\")"),

    # -------------------------------------------------------------- identity
    ("signatures are accepted at any age", "identity.py",
     "    if drift > MAX_CLOCK_SKEW_SECONDS:",
     "    if False:"),

    ("the signature stops covering the body", "identity.py",
     "hashlib.sha256(body).hexdigest()",
     "\"\""),

    ("the signature stops covering the request path", "identity.py",
     "        method.upper(), path, timestamp, hashlib.sha256(body).hexdigest(),",
     "        method.upper(), \"\", timestamp, hashlib.sha256(body).hexdigest(),"),

    # ------------------------------------------------------------------ usage
    ("replica spread bitmaps are replaced rather than merged", "usage.py",
     "            merged |= ((bits & SPREAD_MASK) << shift) & SPREAD_MASK",
     "            merged = ((bits & SPREAD_MASK) << shift) & SPREAD_MASK"),

    ("counters are no longer keyed per replica", "usage.py",
     "                (store.project, memory_id, store.replica_id,",
     "                (store.project, memory_id, \"shared\","),

    ("a direct match is recorded as an indirect one", "sqlite_store.py",
     "            usage.record(self, surfaced, direct=direct)",
     "            usage.record(self, surfaced, direct=())"),

    # ------------------------------------------------------------- federation
    ("rank fusion ignores position", "federation.py",
     "            scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_K + rank + 1)",
     "            scores[key] = scores.get(key, 0.0) + 1.0"),

    ("the same memory from two servers no longer fuses", "federation.py",
     "            key = entry.get(\"uuid\") or f\"{source}:{entry['id']}\"",
     "            key = f\"{source}:{entry['id']}\""),

    ("a remote you consulted no longer ranks first", "federation.py",
     "    ranked.sort(key=lambda r: (not r[\"consulted_during_task\"], "
     "-r[\"description_match\"], r[\"name\"]))",
     "    ranked.sort(key=lambda r: r[\"name\"])"),

    # "cached results lose the source they came from" was here. It targeted an
    # INSERT into `memories` that step 6 replaced when borrowed copies moved to
    # a table of their own, where `origin_remote` is NOT NULL and cannot be
    # lost. The property it guarded is now "a borrowed result stops saying where
    # it came from". Removed rather than left to SKIP: a mutant that no longer
    # matches anything reports nothing while still looking like coverage.

    # -------------------------------------------------------------- prefilter
    # Narrowing on the label index is only sound when the expression cannot be
    # satisfied by a memory carrying none of the labels it mentions. Get that
    # wrong and matches vanish silently, which is far worse than the scan it
    # replaces. No mutant here for deleting the prefilter outright: that costs
    # speed and nothing else, and speed is not what these assert.
    ("the prefilter trusts every label a query mentions", "sqlite_store.py",
     "        narrowing = expression.narrowing_labels",
     "        narrowing = expression.used_labels"),

    ("a negated query narrows anyway", "validation.py",
     "            if not parser.saw_negation:\n"
     "                self._narrowing = set(self.used_labels)",
     "            self._narrowing = set(self.used_labels)"),

    ("a dict query of only exclusions narrows on them", "validation.py",
     "            self._narrowing = set(all_labels) or set(any_labels) or None",
     "            self._narrowing = set(all_labels) or set(any_labels) or set(not_labels) or None"),

    # --------------------------------------------------------------- reporting
    ("a failing backup goes back to being silent", "backup.py",
     "            print(f\"project-memory-mcp: BACKUP FAILED to {self.destination}: {exc}\",\n"
     "                  file=sys.stderr, flush=True)",
     "            pass"),

    # The counterweight: shouting on every run trains its reader to skip it.
    ("every backup announces itself, failed or not", "backup.py",
     "            snapshot_database(self.database, self.destination, self.keep)\n"
     "            self.last_error = None",
     "            snapshot_database(self.database, self.destination, self.keep)\n"
     "            self.last_error = None\n"
     "            print(\"project-memory-mcp: BACKUP FAILED (not really)\", file=sys.stderr)"),

    # ------------------------------------------------------------ remote state
    ("disabling a remote throws its queued work away", "federation.py",
     "        connection.execute(\"UPDATE remotes SET enabled=? WHERE name=?\", "
     "(1 if enabled else 0, name))",
     "        connection.execute(\"UPDATE remotes SET enabled=? WHERE name=?\", "
     "(1 if enabled else 0, name))\n"
     "        connection.execute(\"DELETE FROM outbox WHERE remote=?\", (name,))"),

    ("disabling a remote does nothing at all", "federation.py",
     "        connection.execute(\"UPDATE remotes SET enabled=? WHERE name=?\", "
     "(1 if enabled else 0, name))",
     "        pass"),

    ("removing a remote strands its queue again", "federation.py",
     "        connection.execute(\"DELETE FROM outbox WHERE remote=?\", (name,))\n"
     "        connection.execute(\"DELETE FROM remotes WHERE name=?\", (name,))",
     "        connection.execute(\"DELETE FROM remotes WHERE name=?\", (name,))"),

    # The counterweight: dropping the whole outbox loses unrelated work.
    ("removing one remote empties every remote's queue", "federation.py",
     "        connection.execute(\"DELETE FROM outbox WHERE remote=?\", (name,))\n"
     "        connection.execute(\"DELETE FROM remotes WHERE name=?\", (name,))",
     "        connection.execute(\"DELETE FROM outbox\")\n"
     "        connection.execute(\"DELETE FROM remotes WHERE name=?\", (name,))"),

    # ---------------------------------------------------------------- outbound
    # Inbound signature verification was complete and tested for a long time
    # while the outbound half was a parameter nobody passed. These guard the
    # wiring, not the crypto.
    ("delivery stops signing and only ever sends a token", "computer.py",
     "    return deliver_outbox(store, private_key=identity.load_if_present(key_path),\n"
     "                          db_lock=guard)",
     "    return deliver_outbox(store, db_lock=guard)"),

    ("federated recall stops signing", "server.py",
     "                        private_key=identity.load_if_present(self.key_path), **options)",
     "                        **options)"),

    ("a configured token stops taking precedence over a key", "federation.py",
     "        if self.remote.token:\n"
     "            headers[\"Authorization\"] = \"Bearer \" + self.remote.token\n"
     "        elif self.private_key is not None:",
     "        if False:\n"
     "            headers[\"Authorization\"] = \"Bearer \" + self.remote.token\n"
     "        elif self.private_key is not None:"),

    ("loading a key mints one when none exists", "identity.py",
     "    if not path.is_file():\n        return None\n    try:\n        return load_private_key(path)",
     "    if not path.is_file():\n        generate_private_key(path)\n    try:\n"
     "        return load_private_key(path)"),

    # ------------------------------------------------------------------- cache
    # Borrowed copies live in their own table so that "this machine does not own
    # them" is structural rather than a filter every query has to remember.
    ("cached copies go back into the memories table", "sqlite_store.py",
     "                \"INSERT INTO cached_memories(project_id, uuid, slug, status, description, \"",
     "                \"INSERT INTO memories(project_id, uuid, slug, status, description, \""),

    ("a cached copy is never indexed, so nothing can find it", "sqlite_store.py",
     "            for row in rows:\n                self._index_text(json.loads(row[5]), row[1])",
     "            pass"),

    ("recall stops reaching the cache", "sqlite_store.py",
     "            f\"  AND ((m.uuid IS NOT NULL AND m.archived_at IS NULL) OR c.uuid IS NOT NULL) \"",
     "            f\"  AND m.uuid IS NOT NULL AND m.archived_at IS NULL \""),

    ("a borrowed result stops saying where it came from", "sqlite_store.py",
     "            if origin:\n                # Said on every result",
     "            if False:\n                # Said on every result"),

    ("surfacing a borrowed copy ages it like one of ours", "sqlite_store.py",
     "            if not origin:\n                surfaced.append(memory_id)",
     "            if True:\n                surfaced.append(memory_id)"),

    ("get_memory prefers a borrowed copy over our own", "sqlite_store.py",
     "        row = self.connection.execute(\n"
     "            \"SELECT body FROM memories WHERE project_id=? AND slug=?\", (self.project, memory_id)\n"
     "        ).fetchone()\n        if row is None:",
     "        row = None\n        if row is None:"),

    ("the cache grows without bound", "sqlite_store.py",
     "            doomed = sorted({row[\"uuid\"] for row in stale} | {row[\"uuid\"] for row in surplus})",
     "            doomed = []"),

    # The counterweight: evicting everything would satisfy the bound tests and
    # make the cache useless.
    ("eviction takes the whole cache every time", "sqlite_store.py",
     "            doomed = sorted({row[\"uuid\"] for row in stale} | {row[\"uuid\"] for row in surplus})",
     "            doomed = sorted(row[\"uuid\"] for row in self.connection.execute("
     "\"SELECT uuid FROM cached_memories WHERE project_id=?\", (self.project,)))"),

    ("an evicted copy stays in the search index", "sqlite_store.py",
     "                self.connection.execute(\n"
     "                    f\"DELETE FROM memories_fts WHERE project_id=? AND memory_id IN ({marks})\",\n"
     "                    (self.project, *chunk))",
     "                pass"),

    ("the v8 migration leaves borrowed rows where they were", "sqlite_store.py",
     "    connection.execute(\"DELETE FROM memories WHERE origin_remote IS NOT NULL\")",
     "    pass"),

    # ----------------------------------------------------------------- projects
    # The cleanup deletes a project during an upgrade, so the condition that
    # decides *which* one is the whole safety argument. It has to be narrow
    # enough that it cannot match anything anyone created on purpose.
    ("the upgrade eats any project called bootstrap", "sqlite_store.py",
     "    if any(held.values()):\n        return None  # somebody's actual project",
     "    if False:\n        return None  # somebody's actual project"),

    ("the phantom is left where it was", "sqlite_store.py",
     "    remove_project(connection, _ARTIFACT_PROJECT)\n    return _ARTIFACT_PROJECT",
     "    return None"),

    ("removing a project takes every project's rows", "sqlite_store.py",
     "                connection.execute(f\"DELETE FROM {table} WHERE {column}=?\", (project,))",
     "                connection.execute(f\"DELETE FROM {table}\")"),

    ("removing a project leaves its memories behind", "sqlite_store.py",
     "\"label_registry\", \"labels\", \"memories\", \"memories_fts\", \"outbox\", \"revisions\",",
     "\"label_registry\", \"labels\", \"memories_fts\", \"outbox\", \"revisions\","),

    # The distinctive comment is part of the pattern on purpose: without it,
    # `store = SqliteMemoryStore(args.database, args.project, create=False)`
    # appears four times in this file and the first match is `cmd_audit`.
    ("serve creates whatever project it is pointed at", "cli.py",
     "        # was creating here, so `serve --project my-projct` silently made\n"
     "        # `my-projct` and served nothing, forever.\n"
     "        store = SqliteMemoryStore(args.database, args.project, create=False)",
     "        # was creating here, so `serve --project my-projct` silently made\n"
     "        # `my-projct` and served nothing, forever.\n"
     "        store = SqliteMemoryStore(args.database, args.project)"),

    # ------------------------------------------------------------------- setup
    # `ensure_schema` exists so the server-wide tables can be created without
    # inventing a project. If it stops creating them, a server on an empty
    # database answers 500 where it means 401; if something goes back to opening
    # a store for the side effect, a phantom project appears in every listing.
    ("preparing a database no longer creates its tables", "sqlite_store.py",
     "        cleaned = _prepare(connection)\n        _ensure_replica_id(connection)",
     "        cleaned = None"),

    ("enrolling a client invents a project again", "cli.py",
     "    ensure_schema(database)\n    connection = sqlite3.connect(database)",
     "    SqliteMemoryStore(database, \"bootstrap\", create=True).close()\n"
     "    connection = sqlite3.connect(database)"),

    # ------------------------------------------------------------------ outbox
    ("a deleted memory leaves its promotion queued", "sqlite_store.py",
     "\"memories_fts\", \"outbox\"):",
     "\"memories_fts\"):"),

    ("an orphaned queue entry is retried instead of dropped", "federation.py",
     "        if row[\"slug\"] is None or row[\"archived_at\"] is not None:",
     "        if False:"),

    # The counterweight to the two above. Dropping everything undeliverable
    # would satisfy them and destroy the reason the outbox exists.
    ("a promotion to a sleeping server is thrown away", "federation.py",
     "        if row[\"slug\"] is None or row[\"archived_at\"] is not None:",
     "        if True:"),

    ("a memory merged away is still published", "federation.py",
     "        if row[\"slug\"] is None or row[\"archived_at\"] is not None:",
     "        if row[\"slug\"] is None:"),

    ("a failing job goes back to being silent", "computer.py",
     "            print(f\"project-memory-mcp: job {job.kind}/{job.project} failed: {error}\",\n"
     "                  file=sys.stderr, flush=True)",
     "            pass"),

    ("promotion mints a new identity on the remote", "federation.py",
     "                \"create_memory\", {\"memory\": body, \"visibility\": \"public\",\n"
     "                                  \"uuid\": row[\"memory_id\"]})",
     "                \"create_memory\", {\"memory\": body, \"visibility\": \"public\"})"),

    # --------------------------------------------------------------- messages
    ("one sender can bury a recipient", "messages.py",
     "    if waiting >= MAX_UNREAD_PER_SENDER:",
     "    if False:"),

    ("message bodies have no length limit", "messages.py",
     "    if len(body) > MAX_BODY_CHARS:",
     "    if False:"),

    ("a revoked client can still be messaged", "messages.py",
     "    if row[\"revoked_at\"]:",
     "    if False:"),

    # ------------------------------------------------------------ maintenance
    ("one missing file is enough to call a memory adrift", "maintenance.py",
     "        if len(missing) != len(files):",
     "        if False:"),

    ("anchors are marked stale without being asked", "maintenance.py",
     "        if mark_stale and row[\"status\"] == \"active\":",
     "        if row[\"status\"] == \"active\":"),

    ("cached copies are checked against the local tree", "maintenance.py",
     "        \"AND archived_at IS NULL AND origin_remote IS NULL\", (store.project,)).fetchall()",
     "        \"AND archived_at IS NULL\", (store.project,)).fetchall()"),

    # ------------------------------------------------------------------ store
    # These two hit the re-index path, not delete_memory: _write and
    # _index_text clear before re-inserting, so an edit that did not clear would
    # leave the old wording searchable and the old files attached.
    ("editing a memory leaves its old text searchable", "sqlite_store.py",
     "            \"DELETE FROM memories_fts WHERE project_id=? AND memory_id=?\", "
     "(self.project, memory_uuid)",
     "            \"SELECT 1 WHERE ?=? AND 1=0\", (self.project, memory_uuid)"),

    ("editing a memory leaves its old file rows behind", "sqlite_store.py",
     "        self.connection.execute(\"DELETE FROM files WHERE project_id=? AND memory_id=?\",",
     "        self.connection.execute(\"SELECT 1 WHERE ?=? AND 1=0\","),

    ("status no longer weighs on ranking", "sqlite_store.py",
     "_STATUS_FACTORS = {\"active\": 1.0, \"stale\": 0.7, \"superseded\": 0.4, \"wrong\": 0.2}",
     "_STATUS_FACTORS = {\"active\": 1.0, \"stale\": 1.0, \"superseded\": 1.0, \"wrong\": 1.0}"),

    ("the graph walk is unbounded", "sqlite_store.py",
     "WALK_MAX_NODES = 250",
     "WALK_MAX_NODES = 10**9"),

    ("every candidate becomes a derived neighbour", "sqlite_store.py",
     "DERIVED_THRESHOLD = 0.34",
     "DERIVED_THRESHOLD = 0.0"),

    ("a memory keeps unlimited derived neighbours", "sqlite_store.py",
     "DERIVED_MAX_NEIGHBOURS = 10",
     "DERIVED_MAX_NEIGHBOURS = 10**9"),
]


def run_suite():
    """True if the suite noticed. None if the mutant made it hang."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:randomly"],
            cwd=ROOT, capture_output=True, text=True, timeout=RUN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return None
    return result.returncode != 0


def main() -> int:
    # SIGTERM does not run atexit handlers on its own, and being killed is
    # exactly the case that left a sabotaged file in the working tree once.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    restore_orphans()

    survived, killed, hung, skipped, ambiguous = [], [], [], [], []
    # Checked up front, all of them, before any run: a stale or ambiguous
    # pattern is a mutant that is not testing what its name says, and finding
    # that out forty minutes into a run - or not at all, because SKIP scrolls
    # past - is how a harness comes to overstate its own coverage.
    for name, filename, old, _new in MUTANTS:
        matches = (ROOT / "project_memory_mcp" / filename).read_text(encoding="utf-8").count(old)
        if matches == 0:
            skipped.append(name)
            print(f"  STALE    {name} (pattern gone from {filename})", flush=True)
        elif matches > 1:
            ambiguous.append(name)
            print(f"  AMBIGUOUS {name} ({matches} matches in {filename}; it would break "
                  f"the first, which may be a different function entirely)", flush=True)
    if skipped or ambiguous:
        print("")

    for name, filename, old, new in MUTANTS:
        path = ROOT / "project_memory_mcp" / filename
        if name in skipped or name in ambiguous:
            continue
        with Mutation(path, old, new):
            detected = run_suite()
        if detected is None:
            hung.append(name)
            label = "HUNG    "
        elif detected:
            killed.append(name)
            label = "caught  "
        else:
            survived.append(name)
            label = "SURVIVED"
        print(f"  {label} {name}"
              f"{'' if detected else '   <-- nothing guards this' if detected is False else ''}",
              flush=True)

    print("")
    print(f"{len(killed)} caught, {len(survived)} survived, {len(hung)} hung, "
          f"{len(skipped)} stale, {len(ambiguous)} ambiguous")
    for name in survived:
        print(f"  unguarded: {name}")
    for name in hung:
        print(f"  hung (a test that hangs cannot tell you anything): {name}")
    for name in skipped:
        print(f"  stale, so it tested nothing: {name}")
    for name in ambiguous:
        print(f"  ambiguous, so it tested the wrong thing: {name}")
    # Non-zero when the harness itself is wrong, separately from what it found.
    # A stale mutant is not a passing mutant.
    return 1 if (skipped or ambiguous) else 0


if __name__ == "__main__":
    raise SystemExit(main())
