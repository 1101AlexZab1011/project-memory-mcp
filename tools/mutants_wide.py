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


class Mutation:
    """Applies one edit, and puts the file back however this process ends."""

    def __init__(self, path: pathlib.Path, old: str, new: str) -> None:
        self.path, self.good = path, path.read_text(encoding="utf-8")
        self.backup = path.with_suffix(path.suffix + BACKUP_SUFFIX)
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
    ("the UI accepts any token", "http_server.py",
     "            accepted = bool(self.token) and hmac.compare_digest(presented, self.token)",
     "            accepted = True"),

    ("expired sessions stay valid", "http_server.py",
     "            if expiry < time.time():",
     "            if False:"),

    ("session cookies lose HttpOnly and SameSite", "http_server.py",
     "\"; Path=/; HttpOnly; SameSite=Strict\"",
     "\"; Path=/\""),

    # "admin-only routes accept any client" was here. It survived because
    # _require_admin was called by nothing - dead code, not an untested guard -
    # and it has been deleted. See the note where it used to live.

    ("project scoping is not enforced", "http_server.py",
     "        if self.client is None or self.client.may_access(project):\n            return True",
     "        if True:\n            return True"),

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

    ("cached results lose the source they came from", "sqlite_store.py",
     "                rows.append((self.project, uuid, body[\"id\"], body.get(\"status\", \"active\"),\n"
     "                             body.get(\"description\", \"\"), stamp, json.dumps(body), name))",
     "                rows.append((self.project, uuid, body[\"id\"], body.get(\"status\", \"active\"),\n"
     "                             body.get(\"description\", \"\"), stamp, json.dumps(body), None))"),

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

    survived, killed, hung, skipped = [], [], [], []
    for name, filename, old, new in MUTANTS:
        path = ROOT / "project_memory_mcp" / filename
        if old not in path.read_text(encoding="utf-8"):
            skipped.append(name)
            print(f"  SKIP     {name} (pattern not in {filename})", flush=True)
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
    print(f"{len(killed)} caught, {len(survived)} survived, "
          f"{len(hung)} hung, {len(skipped)} skipped")
    for name in survived:
        print(f"  unguarded: {name}")
    for name in hung:
        print(f"  hung (a test that hangs cannot tell you anything): {name}")
    for name in skipped:
        print(f"  not tested: {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
