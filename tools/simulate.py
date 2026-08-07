"""A whole federation, living for three simulated years.

The unit tests check properties in isolation and the mutation harness checks
that they would notice being broken. Neither walks the system end to end, and
almost every real defect this project has had was found by walking it: the
outbox orphan, the slug collision between a borrowed copy and a local memory,
the backup that failed in silence. All of those were green in the suite.

So this stands up real HTTP servers, real Ed25519 clients, real stores and the
real Computer, and then *lives* in them - writing, recalling, promoting,
federating, arguing, ageing - for long enough that the retention lifecycle
actually fires. Nothing here is mocked. The only thing that is not real is time.

    python tools/simulate.py            # narrative + invariant report
    python tools/simulate.py --quiet    # just the failures and the summary

Exits non-zero if any invariant breaks.

**How time works.** Every gate in the audit is two-clocked: exposure (queries
served) and real days elapsed. Exposure is raised by actually running recalls.
Days are simulated by winding every stored timestamp *backwards*, which from the
store's point of view is indistinguishable from the world having moved forwards.
That keeps production code free of an injected clock, and it is what the
existing audit tests already do, at three days rather than three years.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
import threading
import traceback
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from project_memory_mcp import clients, federation, identity, maintenance, messages  # noqa: E402
from project_memory_mcp.computer import Computer, make_job  # noqa: E402
from project_memory_mcp.http_server import (_Handler, _Sessions,  # noqa: E402
                                            _StoreRegistry)
from project_memory_mcp.server import McpServer  # noqa: E402
from project_memory_mcp.sqlite_store import SqliteMemoryStore  # noqa: E402
from project_memory_mcp.validation import StoreError  # noqa: E402

PROJECT = "atlas"

#: Columns holding an ISO timestamp, per table. Winding these back is what
#: "a year passed" means here.
TIME_COLUMNS = {
    "memories": ("created", "last_validated", "tier_since", "archived_at"),
    "cached_memories": ("created", "cached_at"),
    "usage": ("last_surfaced", "last_applied"),
    "revisions": ("revised_at",),
    "audit_runs": ("started", "finished"),
    "outbox": ("queued_at",),
    "clients": ("created", "last_seen", "revoked_at"),
    "enrollment_codes": ("expires_at", "created", "used_at"),
    "remotes": ("added", "last_ok"),
    "projects": ("created",),
    "jobs": ("started", "finished"),
}


# --------------------------------------------------------------------- content

AREAS = ("render", "netcode", "build", "audio", "physics", "tooling")
SUBJECTS = (
    ("shader cache", "the shader cache is rebuilt from scratch whenever the driver version changes"),
    ("replication window", "replicated actors outside the relevancy window stop ticking entirely"),
    ("incremental link", "the incremental linker silently falls back to a full link past 2GB of objects"),
    ("voice bank", "streamed voice banks hold their file handles until the level fully unloads"),
    ("collision sweep", "a zero-extent sweep reports a hit against the actor that issued it"),
    ("asset cook", "cooking on a case-sensitive volume misses assets referenced with the wrong case"),
    ("frame pacing", "frame pacing drifts when the render thread outruns the game thread by two frames"),
    ("packet loss", "the reliability layer resends the whole channel rather than the missing packet"),
    ("unity build", "a unity build hides a missing include until somebody edits one file in isolation"),
    ("mip streaming", "mip streaming stalls on the first frame after a large camera cut"),
    ("reverb zone", "overlapping reverb zones multiply rather than blend"),
    ("ragdoll blend", "the ragdoll blend-out uses the wrong bone space on mirrored rigs"),
    ("editor pie", "play-in-editor keeps a stale copy of any asset saved while it is running"),
    ("nav mesh", "nav mesh tiles regenerate out of order after a streaming level loads"),
    ("gpu capture", "a GPU capture changes the timing enough to hide the bug being captured"),
    ("save game", "save games written during a level transition record the previous level's actors"),
)


def memory_document(slug: str, area: str, subject: str, detail: str,
                    labels: list[str], related: list[str] | None = None,
                    supersedes: list[str] | None = None,
                    facts: list[str] | None = None) -> dict:
    """A schema-valid memory. Content is invented; shape is the real thing."""
    return {
        "schema_version": 1,
        "id": slug,
        "status": "active",
        "description": f"In {area}, {detail}.",
        "tags": [subject.replace(" ", "-")],
        "labels": labels,
        "scope": {"project": PROJECT, "area": area,
                  "files": [f"Source/{area.title()}/{subject.title().replace(' ', '')}.cpp"],
                  "applies_to": []},
        "triggers": [f"{subject} behaves oddly", f"anything in {area} that smells like {subject}"],
        "remembered_facts": facts or [detail.capitalize() + "."],
        "solution_pattern": [f"Check {subject} before assuming the caller is wrong."],
        "pitfalls": [f"Reproducing this under a debugger changes the {subject} timing."],
        "evidence": {"created_from_task": f"task-{slug}", "last_validated": "2026-01-01"},
        "relationships": {
            "related": [{"id": r, "reason": "They are two halves of one failure mode."}
                        for r in (related or [])],
            "supersedes": supersedes or [],
            "superseded_by": [],
        },
    }


# ----------------------------------------------------------------------- world

class Report:
    """Every invariant checked, and whether it held."""

    def __init__(self, quiet: bool) -> None:
        self.quiet = quiet
        self.passed: list[str] = []
        self.failed: list[tuple[str, str]] = []
        self.act = ""

    def scene(self, title: str) -> None:
        self.act = title
        if not self.quiet:
            print(f"\n\033[1m{title}\033[0m" if sys.stdout.isatty() else f"\n== {title}")

    def note(self, text: str) -> None:
        if not self.quiet:
            print(f"   . {text}")

    def check(self, claim: str, got, want) -> bool:
        if got == want:
            self.passed.append(f"{self.act}: {claim}")
            if not self.quiet:
                print("   ok   " + claim)
            return True
        self.failed.append((f"{self.act}: {claim}", f"got {got!r}, wanted {want!r}"))
        print("   FAIL " + claim)
        print("        got    %r" % (got,))
        print("        wanted %r" % (want,))
        return False

    def truthy(self, claim: str, got) -> bool:
        return self.check(claim, bool(got), True)

    def summary(self, expected: int) -> int:
        total = len(self.passed) + len(self.failed)
        print("\n%d/%d invariants held" % (len(self.passed), total))
        for claim, detail in self.failed:
            print("  BROKEN  %s\n          %s" % (claim, detail))
        if total != expected:
            # A check that does not run is not a check that passed. Several of
            # these sit behind an `if`, and a regression that empties the
            # condition deletes them silently while the ratio still reads N/N.
            # Removing the audit's per-run cap did exactly that: 98/98, green,
            # three checks gone.
            print("  BROKEN  the run made %d checks, expected %d" % (total, expected))
            print("          A check that stopped being reached is a regression,")
            print("          not a smaller test.")
            return 1
        return 1 if self.failed else 0


class Server:
    """A real project-memory server on a real socket."""

    def __init__(self, name: str, root: Path, token: str) -> None:
        self.name = name
        self.token = token
        self.path = root / f"{name}.db"
        seed = SqliteMemoryStore(self.path, PROJECT)
        for area in AREAS:
            seed.add_label(f"area:{area}", f"the {area} subsystem")
        for kind in ("kind:bug", "kind:procedure", "kind:gotcha"):
            seed.add_label(kind, kind)
        seed.close()

        self.registry = _StoreRegistry(self.path)
        handler = type(f"H{name}", (_Handler,), {
            "registry": self.registry, "token": token,
            "sessions": _Sessions(), "ui_enabled": True})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.down = False

    def store(self) -> SqliteMemoryStore:
        return SqliteMemoryStore(self.path, PROJECT, create=False)

    def enroll(self, name: str, public_key: str | None = None,
               projects: list[str] | None = None) -> dict:
        code = clients.create_code(self.registry.control(), name=name, projects=projects)["code"]
        return clients.redeem_code(self.registry.control(), code, name, public_key)

    def go_offline(self) -> None:
        self.httpd.shutdown()
        self.down = True

    def stop(self) -> None:
        if not self.down:
            self.httpd.shutdown()
        self.httpd.server_close()
        self.registry.close()


class Machine:
    """One person's laptop: a local store, a key, and some remotes."""

    def __init__(self, name: str, root: Path) -> None:
        self.name = name
        self.path = root / f"{name}.db"
        self.key_path = root / f"{name}.pem"
        self.key = identity.load_or_create(self.key_path)
        store = SqliteMemoryStore(self.path, PROJECT)
        for area in AREAS:
            store.add_label(f"area:{area}", f"the {area} subsystem")
        for kind in ("kind:bug", "kind:procedure", "kind:gotcha"):
            store.add_label(kind, kind)
        store.close()

    def store(self) -> SqliteMemoryStore:
        return SqliteMemoryStore(self.path, PROJECT, create=False)

    def tools(self, store: SqliteMemoryStore) -> McpServer:
        """The real MCP dispatcher, as an agent would reach it."""
        return McpServer(store, key_path=self.key_path)

    def call(self, store: SqliteMemoryStore, tool: str, **arguments):
        result = self.tools(store)._call_tool(tool, arguments)
        text = result["content"][0]["text"]
        if result.get("isError"):
            raise StoreError(text)
        return json.loads(text)

    def add_remote(self, store, name: str, url: str, description: str,
                   token: str | None = None) -> None:
        federation.add_remote(store.connection, name, url, description, token=token)

    def sweep(self, kinds=("outbox", "audit", "evict", "reindex", "dedup"), **kwargs) -> dict:
        """Run the Computer's jobs for real, one at a time, deterministically."""
        computer = Computer(open_store=lambda _p: self.store(), database=self.path)
        done = {}
        for kind in kinds:
            extra = dict(kwargs.get(kind, {}))
            if kind == "outbox":
                extra.setdefault("key_path", self.key_path)
            done[kind] = computer.run_one(make_job(kind, PROJECT, **extra))
        return done


def warp(path: Path, days: int) -> None:
    """Wind every stored timestamp back, so the world appears to have moved on."""
    connection = sqlite3.connect(path)
    try:
        present = {r[0] for r in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for table, columns in TIME_COLUMNS.items():
            if table not in present:
                continue
            existing = {r[1] for r in connection.execute(f"PRAGMA table_info({table})")}
            for column in columns:
                if column not in existing:
                    continue
                connection.execute(
                    f"UPDATE {table} SET {column} = "
                    f"strftime('%Y-%m-%dT%H:%M:%SZ', {column}, '-{days} days') "
                    f"WHERE {column} IS NOT NULL")
        if "usage" in present:
            # An ordinal day number rather than a string, so it shifts by
            # subtraction. Left alone it would make every spread bitmap look
            # like it came from the future.
            connection.execute(
                "UPDATE usage SET spread_epoch = spread_epoch - ? WHERE spread_epoch IS NOT NULL",
                (days,))
        connection.commit()
    finally:
        connection.close()


def raise_exposure(store: SqliteMemoryStore, queries: int) -> None:
    """Serve queries that match nothing, to age the project without feeding it.

    The audit's gate asks whether there were enough *opportunities* for a memory
    to be seen. A query matching nothing is a real opportunity and surfaces
    nobody, which is exactly how to separate exposure from usage.
    """
    for _ in range(queries):
        store.recall("qqzzx nothing whatsoever matches this string", limit=1, full_count=0)


# ------------------------------------------------------------------------ acts

def act_day_zero(report: Report, world: dict) -> None:
    """Three machines, two servers, and the first memories written."""
    report.scene("Day 0 - three machines start keeping notes")
    subjects = list(SUBJECTS)

    for name, machine in world["machines"].items():
        store = machine.store()
        try:
            chosen = {"laptop": subjects[:6], "desktop": subjects[6:11]}.get(name, subjects[11:])
            written = []
            for index, (subject, detail) in enumerate(chosen):
                area = AREAS[index % len(AREAS)]
                slug = f"{name}-{subject.replace(' ', '-')}"
                document = memory_document(
                    slug, area, subject, detail,
                    labels=[f"area:{area}", "kind:bug" if index % 2 else "kind:gotcha"])
                machine.call(store, "create_memory", memory=document,
                             visibility="public" if index % 3 else "private")
                written.append(slug)
            if name == "laptop":
                # A keystone: never queried, never applied, but pointed at by
                # others. The audit keeps it on incoming links alone, and that
                # is the branch a usage-only retention policy gets wrong -
                # "load-bearing even if rarely matched on its own". Breaking
                # that branch changed nothing here until this existed, because
                # every other memory survived on an earlier one.
                machine.call(store, "create_memory", memory=memory_document(
                    "laptop-keystone", "tooling", "build graph",
                    "the build graph is what every other tooling note ends up "
                    "depending on", labels=["area:tooling", "kind:gotcha"]))
                for index in (0, 1):
                    machine.call(store, "create_memory", memory=memory_document(
                        "laptop-depends-%d" % index, "tooling", "build graph",
                        "a tooling note that only makes sense beside the build "
                        "graph note, number %d" % index,
                        labels=["area:tooling", "kind:gotcha"],
                        related=["laptop-keystone"]))
                written.extend(["laptop-keystone", "laptop-depends-0", "laptop-depends-1"])
            machine.written = written
            report.note(f"{name}: wrote {len(written)} memories")
            report.check(f"{name} validates clean after writing",
                         store.validate_store(), [])
        finally:
            store.close()

    laptop = world["machines"]["laptop"]
    store = laptop.store()
    try:
        first = store.get_memory(laptop.written[0])
        found = laptop.call(store, "recall", query=first["description"][:60], limit=1)
        report.check("a fresh store answers its first recall",
                     found["count"] > 0, True)
        report.check("nothing is borrowed yet",
                     [m for m in found["memories"] if m.get("origin")], [])
    finally:
        store.close()

    solo = world["machines"]["solo"]
    store = solo.store()
    try:
        report.check("local-only is a complete setup (no remotes configured)",
                     federation.list_remotes(store.connection), [])
        answer = federation.recall_across(store, "editor keeps a stale copy", limit=5)
        report.check("recall_across on a local-only store just answers locally",
                     answer["count"] > 0, True)
    finally:
        store.close()


def act_enrollment(report: Report, world: dict) -> None:
    """Keys enrolled, remotes configured, scopes assigned."""
    report.scene("Day 0 - machines enrol with the servers")
    hub, lab = world["servers"]["hub"], world["servers"]["lab"]

    for name in ("laptop", "desktop"):
        machine = world["machines"][name]
        public = identity.encode_public(identity.public_bytes(machine.key))
        enrolled = hub.enroll(name, public)
        machine.hub_client_id = enrolled["client_id"]
        machine.fingerprint = enrolled["fingerprint"]
        report.check(f"{name} enrolled on hub without receiving a secret",
                     "token" in enrolled, False)

    laptop = world["machines"]["laptop"]
    public = identity.encode_public(identity.public_bytes(laptop.key))
    laptop_on_lab = lab.enroll("laptop", public)
    report.check("the same key enrolls on a second server as the same fingerprint",
                 laptop_on_lab["fingerprint"], laptop.fingerprint)

    # A client scoped to a project that is not this one, to prove scoping bites.
    scoped = hub.enroll("contractor", None, projects=["other-project"])
    world["scoped_token"] = scoped["token"]
    report.truthy("a keyless client still gets a bearer token", scoped["token"])

    for name, remotes in (("laptop", ("hub", "lab")), ("desktop", ("hub",))):
        machine = world["machines"][name]
        store = machine.store()
        try:
            for remote in remotes:
                server = world["servers"][remote]
                # No token: this machine signs. That path was unreachable until
                # the key was wired through.
                machine.add_remote(store, remote, server.url,
                                   "shared engine knowledge" if remote == "hub"
                                   else "experimental branch findings")
            report.check(f"{name} has {len(remotes)} remote(s) configured",
                         len(federation.list_remotes(store.connection)), len(remotes))
        finally:
            store.close()


def act_scoping(report: Report, world: dict) -> None:
    """A scoped credential must be scoped on both transports."""
    report.scene("Day 0 - a contractor's credential is checked on both doors")
    import urllib.error
    import urllib.request

    hub = world["servers"]["hub"]
    token = world["scoped_token"]

    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "recall", "arguments": {"query": "shader"}}}).encode()
    request = urllib.request.Request(
        f"{hub.url}/mcp?project={PROJECT}", data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
    try:
        urllib.request.urlopen(request, timeout=10)
        report.check("MCP refuses an out-of-scope project", "served it", "403")
    except urllib.error.HTTPError as error:
        report.check("MCP refuses an out-of-scope project", error.code, 403)

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    opener = urllib.request.build_opener(NoRedirect)
    login = urllib.request.Request(
        f"{hub.url}/api/login", data=("token=" + token).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        response = opener.open(login)
        cookie = response.headers.get("Set-Cookie")
    except urllib.error.HTTPError as error:
        cookie = error.headers.get("Set-Cookie")
    report.truthy("the same token signs in to the browser UI", cookie)

    sid = cookie.split(";")[0]
    for path, claim in ((f"/api/memories?project={PROJECT}", "the UI refuses the same project"),
                        ("/api/projects", "the UI project list is filtered")):
        request = urllib.request.Request(hub.url + path, headers={"Cookie": sid})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.loads(response.read())
                if path == "/api/projects":
                    report.check(claim, payload["projects"], [])
                else:
                    report.check(claim, "served it", "403")
        except urllib.error.HTTPError as error:
            report.check(claim, error.code, 403)


def act_first_month(report: Report, world: dict) -> None:
    """A month of use: some memories earn their keep, others are never needed."""
    report.scene("Days 1-30 - a month of actual work")
    laptop = world["machines"]["laptop"]
    store = laptop.store()
    try:
        earners = laptop.written[:3]
        for _ in range(12):
            for slug in earners:
                document = store.get_memory(slug)
                found = laptop.call(store, "recall",
                                    query=document["description"][:60], limit=1)
                if any(m["id"] == slug for m in found["memories"]):
                    laptop.call(store, "record_memory_use", memory_ids=[slug])
        raise_exposure(store, 60)

        counters = store.load_usage()["memories"]
        report.truthy("the memories that were used have applied counts",
                      all(counters.get(s, {}).get("applied", 0) > 0 for s in earners))
        report.check("the ones never needed have none",
                     [s for s in laptop.written[3:] if counters.get(s, {}).get("applied", 0)], [])
        report.truthy("the project's query counter passed the tier-1 gate",
                      store.connection.execute(
                          "SELECT queries FROM projects WHERE id=?", (PROJECT,)).fetchone()[0] >= 50)
    finally:
        store.close()

    # Spread has to be distinct *days*, not a burst, so walk the clock forward a
    # day at a time and use them again. This is the signal a counter cannot give.
    for _day in range(4):
        warp(laptop.path, 1)
        store = laptop.store()
        try:
            for slug in laptop.written[:3]:
                document = store.get_memory(slug)
                laptop.call(store, "recall", query=document["description"][:60], limit=1)
        finally:
            store.close()
    store = laptop.store()
    try:
        spread = store.load_usage()["memories"][laptop.written[0]]["spread_days"]
        report.truthy("spread records distinct days, not one burst (%d days)" % spread,
                      spread >= 3)
    finally:
        store.close()

    warp(laptop.path, 30)
    report.note("...thirty days pass...")


def act_first_audit(report: Report, world: dict) -> None:
    """The nursery sweep: what earned a tier, and what quietly leaves."""
    report.scene("Day 30 - the first audit runs unprompted")
    laptop = world["machines"]["laptop"]
    before = laptop.store()
    try:
        tiers_before = dict(before.connection.execute(
            "SELECT slug, tier FROM memories WHERE project_id=?", (PROJECT,)))
    finally:
        before.close()
    report.check("everything starts at tier 1", set(tiers_before.values()), {1})

    from project_memory_mcp.audit import AuditPolicy, run_audit
    probe = laptop.store()
    try:
        preview = run_audit(probe, policy=AuditPolicy(), apply=False, record=False)
        report.note("audit sees %d due of %d examined" % (preview.due, preview.examined))
        reasons = {f.slug: f.reason for f in preview.findings}
        report.truthy("a memory kept alive by matching queries says so",
                      any("matched a query directly" in reasons.get(s, "")
                          for s in laptop.written[:3]))
        report.truthy("the per-run cap holds back what one sweep may touch",
                      [f for f in preview.findings if "per-run cap" in f.reason])
        # Never queried and never applied. If it survives, it survives on the
        # one signal that is not usage at all.
        report.check("a memory nothing queries but others depend on is kept",
                     reasons.get("laptop-keystone", ""), "2 other memories link to it")
        for finding in preview.findings:
            report.note("  %-28s %-8s %s" % (finding.slug, finding.verdict, finding.reason))
    finally:
        probe.close()

    done = laptop.sweep()
    report.check("every job the Computer runs succeeds",
                 sorted(k for k, v in done.items() if v["outcome"] != "ok"), [])

    after = laptop.store()
    try:
        rows = dict(after.connection.execute(
            "SELECT slug, tier FROM memories WHERE project_id=?", (PROJECT,)))
        archived = [r[0] for r in after.connection.execute(
            "SELECT slug FROM memories WHERE project_id=? AND archived_at IS NOT NULL", (PROJECT,))]
        promoted = [s for s, t in rows.items() if t > 1]
        report.note("promoted %s" % sorted(promoted))
        report.note("archived %s" % sorted(archived))
        report.truthy("memories that were used were promoted, not archived",
                      all(s in promoted and s not in archived for s in laptop.written[:3]))
        report.truthy("something unused was archived", archived)
        report.check("nothing was deleted - archiving is reversible",
                     after.connection.execute(
                         "SELECT COUNT(*) FROM memories WHERE project_id=?",
                         (PROJECT,)).fetchone()[0], len(laptop.written))

        if archived:
            report.check("an archived memory is still readable by id",
                         after.get_memory(archived[0])["id"], archived[0])
            found = after.recall(query="", limit=50, record=False)
            report.check("but it no longer competes for a result slot",
                         [m for m in found["memories"] if m["id"] == archived[0]], [])
            after.archive_memory(archived[0], archived=False)
            report.check("and it can be restored",
                         after.connection.execute(
                             "SELECT archived_at FROM memories WHERE slug=?",
                             (archived[0],)).fetchone()[0], None)
    finally:
        after.close()


def act_promotion(report: Report, world: dict) -> None:
    """Publishing outward: the tier gate, the secret scan, and a signed delivery."""
    report.scene("Day 30 - laptop publishes what it has proven")
    laptop = world["machines"]["laptop"]
    hub = world["servers"]["hub"]
    store = laptop.store()
    try:
        proven = laptop.written[0]
        store.set_visibility(proven, "public")

        targets = laptop.call(store, "promotion_targets", id=proven, consulted=["hub"])
        report.check("a remote actually consulted ranks first",
                     targets["targets"][0]["name"], "hub")

        unproven = memory_document(
            "laptop-brand-new-note", "build", "fresh finding",
            "something learned thirty seconds ago and not yet proven anywhere",
            labels=["area:build", "kind:bug"])
        laptop.call(store, "create_memory", memory=unproven, visibility="public")
        try:
            laptop.call(store, "promote_memory", id="laptop-brand-new-note", remote="hub")
            report.check("an unproven memory is refused publication", "allowed", "refused")
        except StoreError as error:
            report.check("an unproven memory is refused publication",
                         "has not earned publication" in str(error), True)

        leaky = memory_document(
            "laptop-deploy-key", "build", "deploy key",
            "the deploy pipeline authenticates with a token we keep in the vault",
            labels=["area:build", "kind:procedure"],
            facts=["The CI token is " + "AKIA" + "J7HGF2LQPWXZ4RVN" + ", kept in the vault."])
        laptop.call(store, "create_memory", memory=leaky, visibility="public")
        store.connection.execute("UPDATE memories SET tier=2 WHERE slug='laptop-deploy-key'")
        store.connection.commit()
        try:
            laptop.call(store, "promote_memory", id="laptop-deploy-key", remote="hub")
            report.check("a memory that looks like it holds a secret is held back",
                         "published", "refused")
        except StoreError as error:
            report.check("a memory that looks like it holds a secret is held back",
                         "secret" in str(error).lower(), True)

        queued = laptop.call(store, "promote_memory", id=proven, remote="hub")
        report.check("promotion answers queued, never published", "queued" in queued, True)
        world["published"] = proven
    finally:
        store.close()

    done = laptop.sweep(kinds=("outbox",))
    report.check("the Computer delivers it, signed with laptop's key",
                 done["outbox"]["detail"]["sent"], [world["published"]])

    landed = hub.store()
    local = world["machines"]["laptop"].store()
    try:
        report.check("it arrived on the server",
                     landed.get_memory(world["published"])["id"], world["published"])
        row = landed.connection.execute(
            "SELECT author_key, visibility FROM memories WHERE slug=?",
            (world["published"],)).fetchone()
        report.check("attributed to the key that signed it",
                     row["author_key"], world["machines"]["laptop"].fingerprint)
        report.check("and public on the server", row["visibility"], "public")
        report.check("one lesson keeps one identity on both machines",
                     landed.connection.execute(
                         "SELECT uuid FROM memories WHERE slug=?",
                         (world["published"],)).fetchone()[0],
                     local._uuid_for(world["published"]))
    finally:
        landed.close()
        local.close()


def act_federated_read(report: Report, world: dict) -> None:
    """Reading across servers, and still reading when one is gone."""
    report.scene("Day 31 - desktop asks the network what it does not know")
    desktop = world["machines"]["desktop"]
    published = world["published"]
    store = desktop.store()
    try:
        wanted = world["machines"]["laptop"].store()
        try:
            query = wanted.get_memory(published)["description"][:60]
        finally:
            wanted.close()

        local_only = desktop.call(store, "recall", query=query, limit=5)
        report.check("desktop has never heard of it locally",
                     [m for m in local_only["memories"] if m["id"] == published], [])

        across = desktop.call(store, "recall", query=query, limit=5, include_remotes=True)
        borrowed = [m for m in across["memories"] if m["id"] == published]
        report.check("federated recall finds it on the hub", len(borrowed), 1)
        report.check("and says which server it came from",
                     borrowed[0].get("sources"), ["hub"])
        report.check("hub answered", "hub" in across["sources_answered"], True)
        report.check("and nothing was unreachable", across["sources_unreachable"], {})

        cached = store.connection.execute(
            "SELECT origin_remote FROM cached_memories WHERE slug=?", (published,)).fetchone()
        report.check("the answer was cached, tagged with its origin",
                     cached["origin_remote"] if cached else None, "hub")
        report.check("and it is not in the table of memories desktop owns",
                     store.connection.execute(
                         "SELECT 1 FROM memories WHERE slug=?", (published,)).fetchone(), None)
        world["cached_query"] = query
    finally:
        store.close()

    report.note("...the hub goes offline...")
    world["servers"]["hub"].go_offline()
    store = desktop.store()
    try:
        offline = desktop.call(store, "recall", query=world["cached_query"], limit=5)
        found = [m for m in offline["memories"] if m["id"] == published]
        report.check("a purely local recall still finds the cached copy", len(found), 1)
        report.check("labelled as borrowed, so nobody mistakes it for their own",
                     found[0].get("origin"), "hub")

        across = desktop.call(store, "recall", query=world["cached_query"],
                              limit=5, include_remotes=True)
        report.check("asking the network anyway reports the hub as unreachable",
                     "hub" in across["sources_unreachable"], True)
        report.check("and still answers", across["count"] > 0, True)

        report.check("a borrowed copy is not desktop's to publish onward",
                     store.publication_state(published)["borrowed"], True)
        try:
            federation.promote(store, published, "hub")
            report.check("and promoting it is refused", "allowed", "refused")
        except StoreError as error:
            report.check("and promoting it is refused", "cached copy" in str(error), True)
    finally:
        store.close()


def act_messaging(report: Report, world: dict) -> None:
    """The question a memory's own text cannot answer: why is this true?"""
    report.scene("Day 32 - desktop asks laptop why a memory is true")
    hub = world["servers"]["hub"]
    served = hub.store()
    try:
        served.actor = {"client_id": world["machines"]["desktop"].hub_client_id,
                        "name": "desktop"}
        sent = messages.send_from(served, "laptop",
                                 "Why does the shader cache rebuild rather than invalidate? "
                                 "The memory says it does, not why.",
                                 world["published"], None)
        report.truthy("the message is accepted for a known client", sent)
    finally:
        served.actor = None
        served.close()

    served = hub.store()
    try:
        served.actor = {"client_id": world["machines"]["laptop"].hub_client_id, "name": "laptop"}
        waiting = messages.unread_for(served)
        report.check("laptop has one unread message", waiting, 1)

        inbox = messages.inbox_for(served, unread_only=True, mark_read=True)
        body = inbox["messages"][0]
        report.check("the body arrives labelled as untrusted input",
                     "untrusted_body" in body, True)
        report.check("reading it clears the unread count", messages.unread_for(served), 0)
    finally:
        served.actor = None
        served.close()

    served = hub.store()
    try:
        served.actor = {"client_id": world["machines"]["laptop"].hub_client_id, "name": "laptop"}
        messages.send_from(served, "desktop", "Because the driver bumps the cache key.", None, None)
        served.actor = None
        served.actor = {"client_id": world["machines"]["desktop"].hub_client_id, "name": "desktop"}
        report.check("the reply reaches the other client", messages.unread_for(served), 1)
    finally:
        served.actor = None
        served.close()

    served = hub.store()
    try:
        served.actor = {"client_id": world["machines"]["laptop"].hub_client_id,
                        "name": "laptop"}
        try:
            messages.send_from(served, "nobody-here", "hello?", None, None)
            report.check("messaging an unknown client is refused", "accepted", "refused")
        except StoreError:
            report.check("messaging an unknown client is refused", True, True)
    finally:
        served.actor = None
        served.close()


def act_the_long_haul(report: Report, world: dict) -> None:
    """Two more years, and the tiers that only time can reach."""
    report.scene("Days 31-395 - a year of steady use")
    laptop = world["machines"]["laptop"]
    survivors = laptop.written[:3]

    store = laptop.store()
    try:
        for _ in range(40):
            for slug in survivors:
                document = store.get_memory(slug)
                laptop.call(store, "recall", query=document["description"][:60], limit=1)
                laptop.call(store, "record_memory_use", memory_ids=[slug])
        raise_exposure(store, 520)
        queries = store.connection.execute(
            "SELECT queries FROM projects WHERE id=?", (PROJECT,)).fetchone()[0]
        report.note("the project has now served %d queries" % queries)
    finally:
        store.close()

    warp(laptop.path, 365)
    report.note("...a year passes...")
    laptop.sweep(kinds=("audit",))

    store = laptop.store()
    try:
        tiers = dict(store.connection.execute(
            "SELECT slug, tier FROM memories WHERE project_id=?", (PROJECT,)))
        report.note("tiers now %s" % sorted((t, s) for s, t in tiers.items() if t > 1))
        report.truthy("a memory used across a year reaches tier 3",
                      any(tiers.get(s, 0) >= 3 for s in survivors))
    finally:
        store.close()

    report.scene("Days 395-1100 - the second and third years")
    store = laptop.store()
    try:
        for _ in range(40):
            for slug in survivors:
                document = store.get_memory(slug)
                laptop.call(store, "recall", query=document["description"][:60], limit=1)
                laptop.call(store, "record_memory_use", memory_ids=[slug])
        raise_exposure(store, 700)
    finally:
        store.close()
    warp(laptop.path, 700)
    report.note("...two more years pass...")
    done = laptop.sweep()
    report.check("the Computer still runs cleanly three years in",
                 sorted(k for k, v in done.items() if v["outcome"] != "ok"), [])

    store = laptop.store()
    try:
        report.check("the store is still valid after three years of lifecycle",
                     store.validate_store(), [])
        report.truthy("and a proven memory is still retrievable",
                      store.recall(survivors[0].replace("-", " "), record=False)["count"] > 0)
        run = store.connection.execute(
            "SELECT COUNT(*) FROM audit_runs WHERE project_id=?", (PROJECT,)).fetchone()[0]
        report.truthy("every sweep left a record that can itself be audited (%d runs)" % run,
                      run >= 3)
    finally:
        store.close()


def act_supersession(report: Report, world: dict) -> None:
    """A newer finding replaces an older one, and only then may it be deleted."""
    report.scene("Day 1100 - a finding is superseded, and the old one removed")
    laptop = world["machines"]["laptop"]
    store = laptop.store()
    try:
        old = laptop.written[4]
        try:
            store.get_memory(old)
        except StoreError:
            old = laptop.written[0]
        replacement = memory_document(
            "laptop-superseding-note", "render", "shader cache",
            "the shader cache now keys on driver build, so the rebuild no longer happens",
            labels=["area:render", "kind:bug"], supersedes=[old])
        laptop.call(store, "create_memory", memory=replacement, visibility="public")

        mirrored = store.get_memory(old)["relationships"]["superseded_by"]
        report.check("the link is mirrored onto the memory it replaces",
                     mirrored, ["laptop-superseding-note"])
        report.check("the store still validates with the pair in place",
                     store.validate_store(), [])
        before = store.count()
    finally:
        store.close()

    from project_memory_mcp.audit import AuditPolicy, DEFAULT_GATES, run_audit
    store = laptop.store()
    try:
        policy = AuditPolicy(gates=DEFAULT_GATES, delete_superseded=False)
        report_only = run_audit(store, policy=policy, apply=True, record=False)
        report.check("without opting in, nothing is deleted", report_only.deleted, 0)
        report.check("and the count is unchanged", store.count(), before)

        store.connection.execute(
            "UPDATE memories SET tier_since=?, tier_since_query=0 WHERE slug=?",
            ("2020-01-01T00:00:00Z", old))
        store.connection.commit()
        opted_in = run_audit(store, policy=AuditPolicy(delete_superseded=True),
                             apply=True, record=False)
        report.note("delete verdicts: %d" % opted_in.deleted)
        if opted_in.deleted:
            report.check("the superseded memory is gone",
                         store.connection.execute(
                             "SELECT 1 FROM memories WHERE slug=?", (old,)).fetchone(), None)
            report.truthy("but its body was written to revisions on the way out",
                          store.connection.execute(
                              "SELECT COUNT(*) FROM revisions WHERE project_id=?",
                              (PROJECT,)).fetchone()[0] > 0)
            report.check("its successor is still standing",
                         store.get_memory("laptop-superseding-note")["id"],
                         "laptop-superseding-note")
        report.check("the store validates after a deletion", store.validate_store(), [])
    finally:
        store.close()


def act_dedup_and_merge(report: Report, world: dict) -> None:
    """Two people writing the same lesson twice, and folding them together."""
    report.scene("Day 1100 - the same lesson, written twice")
    desktop = world["machines"]["desktop"]
    store = desktop.store()
    try:
        shared = ("the reliability layer resends the whole channel rather than "
                  "the missing packet when loss exceeds five percent")
        for slug in ("desktop-packet-resend", "desktop-packet-resend-again"):
            document = memory_document(slug, "netcode", "packet loss", shared,
                                       labels=["area:netcode", "kind:bug"])
            desktop.call(store, "create_memory", memory=document)

        pairs = maintenance.duplicate_candidates(store, limit=10, threshold=0.5)
        found = [sorted(m["id"] for m in pair["memories"]) for pair in pairs["candidates"]]
        report.check("the near-duplicate pair is nominated",
                     ["desktop-packet-resend", "desktop-packet-resend-again"] in found, True)
        report.check("nomination does not decide anything on its own",
                     store.count() >= 2, True)

        for _ in range(3):
            store.recall("packet resend reliability", limit=3)
        before = store.load_usage()["memories"]
        total = (before.get("desktop-packet-resend", {}).get("surfaced", 0)
                 + before.get("desktop-packet-resend-again", {}).get("surfaced", 0))

        desktop.call(store, "merge_memories", keep_id="desktop-packet-resend",
                     merge_id="desktop-packet-resend-again",
                     reason="Both describe one resend bug, written a week apart.")
        after = store.load_usage()["memories"]["desktop-packet-resend"]
        report.check("the counters add, because both were evidence about one fact",
                     after["surfaced"], total)
        report.check("the folded memory is archived, not destroyed",
                     store.get_memory("desktop-packet-resend-again")["id"],
                     "desktop-packet-resend-again")
        report.truthy("with a pointer back to what it became",
                      store.connection.execute(
                          "SELECT merged_into FROM memories WHERE slug=?",
                          ("desktop-packet-resend-again",)).fetchone()[0])
        report.check("the store validates after a merge", store.validate_store(), [])
    finally:
        store.close()


def act_revocation(report: Report, world: dict) -> None:
    """A lost laptop is one row, not a rotation nobody performs."""
    report.scene("Day 1100 - desktop's credential is revoked")
    hub = world["servers"]["hub"]
    desktop = world["machines"]["desktop"]
    control = hub.registry.control()

    listed = clients.list_clients(control)
    report.check("both machines are listed as enrolled",
                 sum(1 for row in listed if row["name"] in ("laptop", "desktop")), 2)

    clients.revoke(control, desktop.hub_client_id)
    report.truthy("the row stays, because attribution is history",
                  any(row["client_id"] == desktop.hub_client_id for row in clients.list_clients(control)))

    class Headers:
        def __init__(self, mapping):
            self.mapping = mapping

        def get(self, key, default=None):
            return self.mapping.get(key, default)

    try:
        clients.authenticate(control, Headers({"X-PM-Key": desktop.fingerprint}),
                             "POST", "/mcp", b"", hub.token)
        report.check("a revoked key can no longer authenticate", "accepted", "refused")
    except StoreError as error:
        report.check("a revoked key can no longer authenticate", "revoked" in str(error), True)

    served = hub.store()
    try:
        try:
            messages.send_from(served, "desktop", "still there?", None, None)
            report.check("and cannot be messaged either", "accepted", "refused")
        except StoreError:
            report.check("and cannot be messaged either", True, True)
    finally:
        served.close()

    report.check("laptop is unaffected",
                 clients.authenticate(control, Headers({"Authorization": "Bearer " + hub.token}),
                                      "POST", "/mcp", b"", hub.token).name,
                 "shared token")


def act_cache_expiry(report: Report, world: dict) -> None:
    """A borrowed copy is only good for so long."""
    report.scene("Day 1100 - the cache is three years stale")
    desktop = world["machines"]["desktop"]
    warp(desktop.path, 1100)
    store = desktop.store()
    try:
        cached = store.connection.execute(
            "SELECT COUNT(*) FROM cached_memories WHERE project_id=?", (PROJECT,)).fetchone()[0]
        report.truthy("there are borrowed copies to expire", cached)
        mine = store.count()
    finally:
        store.close()

    done = desktop.sweep(kinds=("evict",))
    report.truthy("the eviction job removed the stale copies",
                  done["evict"]["detail"]["evicted"] > 0)

    store = desktop.store()
    try:
        report.check("none are left",
                     store.connection.execute(
                         "SELECT COUNT(*) FROM cached_memories WHERE project_id=?",
                         (PROJECT,)).fetchone()[0], 0)
        report.check("desktop's own memories are untouched", store.count(), mine)
        report.check("and the index went with the bodies",
                     store.recall(world["cached_query"], record=False)["memories"] and
                     [m for m in store.recall(world["cached_query"], record=False)["memories"]
                      if m.get("origin")], [])
        report.check("the store still validates", store.validate_store(), [])
    finally:
        store.close()


def act_backup_and_projects(report: Report, world: dict) -> None:
    """The last resorts: a restorable snapshot, and removing a project."""
    report.scene("Day 1100 - backup, restore, and tidying up")
    from project_memory_mcp import backup

    laptop = world["machines"]["laptop"]
    root = world["root"]
    store = laptop.store()
    try:
        before_count = store.count()
        before_ids = sorted(m["id"] for m in store.search_memories()["memories"])
    finally:
        store.close()

    export = root / "export.json"
    backup.export_json(laptop.path, export)
    restored_path = root / "restored.db"
    backup.import_json(restored_path, export)
    restored = SqliteMemoryStore(restored_path, PROJECT, create=False)
    try:
        report.check("a JSON round trip preserves the memories", restored.count(), before_count)
        report.check("and their ids",
                     sorted(m["id"] for m in restored.search_memories()["memories"]), before_ids)
        report.check("and the restored store validates", restored.validate_store(), [])
        report.truthy("and is searchable",
                      restored.recall(before_ids[0].replace("-", " "), record=False)["count"] > 0)
    finally:
        restored.close()

    snapshot_dir = root / "snapshots"
    scheduler = backup.BackupScheduler(laptop.path, snapshot_dir, 3600, 3)
    report.check("a snapshot succeeds quietly", scheduler.snapshot_once(), None)
    report.check("and wrote a file", len(list(snapshot_dir.glob("*.db"))), 1)
    broken = backup.BackupScheduler(root / "no-such.db", snapshot_dir, 3600, 3)
    report.truthy("a failing snapshot reports rather than hides", broken.snapshot_once())

    from project_memory_mcp.sqlite_store import project_contents, remove_project
    connection = sqlite3.connect(restored_path)
    connection.row_factory = sqlite3.Row
    try:
        held = project_contents(connection, PROJECT)
        report.truthy("the project reports what it holds", held["memories"] > 0)
        remove_project(connection, PROJECT)
        report.check("removing it leaves no projects",
                     SqliteMemoryStore.list_projects(connection), [])
        for table in ("memories", "labels", "usage", "edges", "memories_fts"):
            report.check("no %s rows survive the removal" % table,
                         connection.execute(
                             "SELECT COUNT(*) FROM %s WHERE project_id=?" % table,
                             (PROJECT,)).fetchone()[0], 0)
    finally:
        connection.close()


# ------------------------------------------------------------------------ main

#: Every check the run should make. Asserted, so that a check which stops being
#: reached fails rather than quietly shrinking the total.
EXPECTED_INVARIANTS = 104

ACTS = (
    act_day_zero,
    act_enrollment,
    act_scoping,
    act_first_month,
    act_first_audit,
    act_promotion,
    act_federated_read,
    act_messaging,
    act_the_long_haul,
    act_supersession,
    act_dedup_and_merge,
    act_revocation,
    act_cache_expiry,
    act_backup_and_projects,
)


def build_world(root: Path) -> dict:
    servers = {"hub": Server("hub", root, "hub-token"),
               "lab": Server("lab", root, "lab-token")}
    machines = {name: Machine(name, root) for name in ("laptop", "desktop", "solo")}
    return {"root": root, "servers": servers, "machines": machines}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--quiet", action="store_true",
                        help="Only failures and the summary.")
    parser.add_argument("--keep", action="store_true",
                        help="Leave the simulated world on disk for inspection.")
    args = parser.parse_args()

    report = Report(args.quiet)
    root = Path(tempfile.mkdtemp(prefix="pm-simulation-"))
    world = build_world(root)
    print("simulating a federation in %s" % root)
    try:
        for act in ACTS:
            try:
                act(report, world)
            except Exception as error:  # an act that explodes is a finding too
                report.failed.append((f"{report.act}: {act.__name__} raised",
                                      f"{type(error).__name__}: {error}"))
                print("   ! %s raised %s: %s" % (act.__name__, type(error).__name__, error))
                traceback.print_exc(limit=4)
    finally:
        for server in world["servers"].values():
            try:
                server.stop()
            except Exception:
                pass
        if not args.keep:
            shutil.rmtree(root, ignore_errors=True)
        else:
            print("world kept at %s" % root)
    return report.summary(EXPECTED_INVARIANTS)


if __name__ == "__main__":
    raise SystemExit(main())
