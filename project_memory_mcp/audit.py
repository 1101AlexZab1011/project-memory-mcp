"""Retention audit: decide which memories have earned their place.

Auto-remember writes on the model's own judgment, so a store grows without a
path out. This is that path. It is deliberately conservative, for two reasons
that took a while to get right:

**Non-recall is only evidence once there has been exposure.** ``surfaced`` is
decided by the ranker, not by the memory: something that consistently lands
eleventh when recall returns ten is never seen, and judging it on that would
make a ranking error permanent and unfalsifiable. So a memory is only reviewed
after the project has served enough queries for silence to mean something. A
project nobody touched for six weeks ages nothing, because nothing was asked.

**Real time is a separate signal from exposure.** A hundred queries in one
afternoon is one task; a hundred across a month spans many. A memory recalled
again a week later says the problem outlived the session, which no amount of
same-day traffic shows. Both clocks must pass before a verdict.

What survives moves up a tier, where the next review is further away - the
generational-GC bet that something which lasted a year deserves less scrutiny
than something written on Tuesday. What does not survive is *archived*: out of
the ranked pool, still in the database, still visible, still recoverable.

Nothing is ever deleted for being quiet. Deletion requires positive evidence,
and of the cases that qualify - a duplicate, a live successor, a memory that is
wrong and carries no warning, a subsystem that no longer exists - a server can
decide exactly one alone: supersession. The rest need judgment, or a repository
this process cannot see. So deletion is opt-in, separate from archiving, and
writes the body to `revisions` on the way out.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

from .sqlite_store import SPREAD_MASK, SqliteMemoryStore, _merge_spread, _now

VERDICT_PROMOTE = "promote"
VERDICT_ARCHIVE = "archive"
VERDICT_HOLD = "hold"
VERDICT_DELETE = "delete"


@dataclass(frozen=True)
class TierGate:
    """When a memory in this tier becomes reviewable, and where it goes next."""

    tier: int
    min_queries: int
    min_days: int


# Defaults, not constants: a store of 200 memories in a solo repo and one of
# three million across a monorepo have nothing in common distributionally, so
# every number here is meant to be overridden rather than trusted.
#
# The first gate is 30 days rather than a week because the gate has to be longer
# than the recurrence period of the work it describes. Release processes recur
# in weeks; build knowledge surfaces when the build breaks, which might be twice
# a year. A seven-day gate keeps only what is relevant to the task in front of
# you - which is exactly the knowledge an agent could rediscover cheapest.
DEFAULT_GATES = (
    TierGate(tier=1, min_queries=50, min_days=30),
    TierGate(tier=2, min_queries=500, min_days=180),
    TierGate(tier=3, min_queries=5000, min_days=730),
)


@dataclass(frozen=True)
class AuditPolicy:
    gates: tuple[TierGate, ...] = DEFAULT_GATES
    #: Any one of these is enough to survive a gate. The bar is deliberately at
    #: "something happened", not at a ratio: applied/surfaced measures trigger
    #: precision, and a memory shown a hundred times and applied twenty times
    #: delivered more than one shown twice and applied twice.
    min_surfaced_direct: int = 1
    min_applied: int = 1
    min_spread_days: int = 2
    #: A memory several others point at is load-bearing even if rarely matched
    #: on its own, so authored links alone can carry it through a gate.
    min_degree: int = 2
    #: Ceiling on what one run may act on. A miscalibrated threshold should cost
    #: a slice and get noticed, not the store.
    max_actions_per_run: int = 50
    max_action_fraction: float = 0.1
    #: Deletion is opt-in separately from archiving, and covers exactly one case
    #: a server can decide alone: a memory whose successor is still standing.
    #: Everything else that could justify deletion - a duplicate, a memory that
    #: is wrong and carries no warning, a subsystem that no longer exists -
    #: needs judgment or a repository this process cannot see.
    delete_superseded: bool = False

    def gate_for(self, tier: int) -> TierGate | None:
        for gate in self.gates:
            if gate.tier == tier:
                return gate
        return None  # past the last gate: nothing further is reviewed

    def cap_for(self, store_size: int) -> int:
        return max(1, min(self.max_actions_per_run, int(store_size * self.max_action_fraction)))

    def describe(self) -> dict[str, Any]:
        return {
            "gates": [{"tier": g.tier, "min_queries": g.min_queries, "min_days": g.min_days}
                      for g in self.gates],
            "min_surfaced_direct": self.min_surfaced_direct,
            "min_applied": self.min_applied,
            "min_spread_days": self.min_spread_days,
            "min_degree": self.min_degree,
            "max_actions_per_run": self.max_actions_per_run,
            "max_action_fraction": self.max_action_fraction,
            "delete_superseded": self.delete_superseded,
        }


@dataclass
class Finding:
    memory_id: str
    slug: str
    tier: int
    verdict: str
    reason: str
    evidence: dict[str, Any]


@dataclass
class AuditReport:
    run_id: int | None
    project: str
    applied: bool
    queries: int
    examined: int
    due: int
    promoted: int
    archived: int
    capped: int
    deleted: int = 0
    findings: list[Finding] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "project": self.project, "applied": self.applied,
            "queries": self.queries, "examined": self.examined, "due": self.due,
            "promoted": self.promoted, "archived": self.archived,
            "deleted": self.deleted, "capped": self.capped,
        }


def _age_days(stamp: str | None, now: datetime) -> int:
    if not stamp:
        return 0
    try:
        then = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return 0
    return max(0, (now - then).days)


def _collect(store: SqliteMemoryStore) -> tuple[int, list[dict[str, Any]]]:
    """Every memory with its tier clocks, merged counters and authored degree.

    One pass over four indexed queries rather than a read per memory: the sweep
    has to stay cheap enough to run on a schedule.
    """
    project = store.project
    queries = store.connection.execute(
        "SELECT queries FROM projects WHERE id=?", (project,)).fetchone()["queries"]

    rows = store.connection.execute(
        "SELECT uuid, slug, status, tier, tier_since, tier_since_query, created, archived_at "
        "FROM memories WHERE project_id=?", (project,)).fetchall()

    counters: dict[str, dict[str, Any]] = {}
    for row in store.connection.execute(
        "SELECT memory_id, surfaced, surfaced_direct, applied, spread_bits, spread_epoch "
        "FROM usage WHERE project_id=?", (project,)
    ):
        entry = counters.setdefault(row["memory_id"], {
            "surfaced": 0, "surfaced_direct": 0, "applied": 0, "_bits": 0, "_epoch": None})
        entry["surfaced"] += row["surfaced"]
        entry["surfaced_direct"] += row["surfaced_direct"]
        entry["applied"] += row["applied"]
        entry["_bits"], entry["_epoch"] = _merge_spread(
            entry["_bits"], entry["_epoch"], row["spread_bits"], row["spread_epoch"])

    # Authored links only. Derived edges are computed similarity, so counting
    # them would let a memory keep itself alive by resembling its neighbours.
    degree: dict[str, int] = {}
    for row in store.connection.execute(
        "SELECT dst, COUNT(*) AS n FROM edges WHERE project_id=? AND kind<>'derived' "
        "GROUP BY dst", (project,)
    ):
        degree[row["dst"]] = row["n"]

    # A memory is safely deletable only if the thing that replaced it is still
    # standing. If B superseded A and B was later marked wrong, deleting A
    # leaves nothing: the old answer gone and the new one known to be false.
    live_successor: dict[str, str] = {}
    for row in store.connection.execute(
        "SELECT e.dst AS old, e.src AS new, m.slug AS new_slug FROM edges e "
        "JOIN memories m ON m.project_id=e.project_id AND m.uuid=e.src "
        "WHERE e.project_id=? AND e.kind='supersedes' AND m.status='active' "
        "AND m.archived_at IS NULL", (project,)
    ):
        live_successor[row["old"]] = row["new_slug"]

    memories = []
    for row in rows:
        counts = counters.get(row["uuid"], {})
        memories.append({
            "uuid": row["uuid"], "slug": row["slug"], "status": row["status"],
            "tier": row["tier"], "tier_since": row["tier_since"],
            "tier_since_query": row["tier_since_query"], "created": row["created"],
            "archived_at": row["archived_at"],
            "surfaced": counts.get("surfaced", 0),
            "surfaced_direct": counts.get("surfaced_direct", 0),
            "applied": counts.get("applied", 0),
            "spread_days": bin(counts.get("_bits", 0) & SPREAD_MASK).count("1"),
            "degree": degree.get(row["uuid"], 0),
            "live_successor": live_successor.get(row["uuid"]),
        })
    return queries, memories


def _judge(memory: dict[str, Any], policy: AuditPolicy, queries: int, now: datetime) -> Finding:
    tier = memory["tier"]
    gate = policy.gate_for(tier)
    exposure = max(0, queries - memory["tier_since_query"])
    days = _age_days(memory["tier_since"] or memory["created"], now)
    evidence = {
        "exposure_queries": exposure, "days_in_tier": days,
        "surfaced": memory["surfaced"], "surfaced_direct": memory["surfaced_direct"],
        "applied": memory["applied"], "spread_days": memory["spread_days"],
        "degree": memory["degree"],
    }
    make = lambda verdict, reason: Finding(  # noqa: E731
        memory["uuid"], memory["slug"], tier, verdict, reason, evidence)

    if memory["archived_at"]:
        return make(VERDICT_HOLD, "already archived")

    # Deletion is decided on positive evidence, not on silence, so it is checked
    # before the exposure gates rather than as a harsher grade of "quiet".
    if policy.delete_superseded and memory["live_successor"]:
        return make(VERDICT_DELETE,
                    f"superseded by '{memory['live_successor']}', which is still active")

    if gate is None:
        return make(VERDICT_HOLD, f"tier {tier} is past the last gate; not reviewed")
    if exposure < gate.min_queries:
        return make(VERDICT_HOLD,
                    f"only {exposure} queries of exposure, needs {gate.min_queries}")
    if days < gate.min_days:
        return make(VERDICT_HOLD, f"only {days} days in tier {tier}, needs {gate.min_days}")

    if memory["surfaced_direct"] >= policy.min_surfaced_direct:
        return make(VERDICT_PROMOTE,
                    f"matched a query directly {memory['surfaced_direct']}x")
    if memory["applied"] >= policy.min_applied:
        return make(VERDICT_PROMOTE, f"reported as applied {memory['applied']}x")
    if memory["spread_days"] >= policy.min_spread_days:
        return make(VERDICT_PROMOTE, f"recalled on {memory['spread_days']} distinct days")
    if memory["degree"] >= policy.min_degree:
        return make(VERDICT_PROMOTE, f"{memory['degree']} other memories link to it")
    return make(VERDICT_ARCHIVE,
                f"no direct match, no applied use, no spread, no incoming links "
                f"after {exposure} queries and {days} days")


def run_audit(store: SqliteMemoryStore, policy: AuditPolicy | None = None,
              apply: bool = False, record: bool = True) -> AuditReport:
    """Judge every memory that is due, and record what was decided.

    ``apply`` is not implemented yet by design: the sweep is meant to be watched
    against a real store before it is allowed to move anything, and the same
    code path produces the report either way, so what you observe is what you
    will get.
    """
    policy = policy or AuditPolicy()
    now = datetime.now(timezone.utc)
    queries, memories = _collect(store)

    findings = [_judge(memory, policy, queries, now) for memory in memories]
    cap = policy.cap_for(len(memories))

    # Archive the weakest first when capped, so a run that cannot do everything
    # spends its budget where the evidence is thinnest. Deletions count against
    # the same budget: one run should never be able to empty a store.
    removals = sorted((f for f in findings if f.verdict in (VERDICT_ARCHIVE, VERDICT_DELETE)),
                      key=lambda f: (f.verdict != VERDICT_DELETE, f.evidence["surfaced"],
                                     f.evidence["degree"], f.slug))
    capped = max(0, len(removals) - cap)
    for finding in removals[cap:]:
        finding.reason = f"would {finding.verdict}, held back by the per-run cap of {cap}"
        finding.verdict = VERDICT_HOLD

    count = lambda verdict: len([f for f in findings if f.verdict == verdict])  # noqa: E731
    report = AuditReport(
        run_id=None, project=store.project, applied=bool(apply), queries=queries,
        examined=len(memories), due=len([f for f in findings if f.verdict != VERDICT_HOLD]),
        promoted=count(VERDICT_PROMOTE), archived=count(VERDICT_ARCHIVE),
        deleted=count(VERDICT_DELETE), capped=capped, findings=findings,
    )
    if apply:
        _apply(store, report, queries, now)
    if record:
        report.run_id = _record(store, report, policy, now)
    return report


def _apply(store: SqliteMemoryStore, report: AuditReport, queries: int,
           now: datetime) -> None:
    """Carry out the verdicts.

    Idempotent by construction: every decision is re-derived from the tier and
    archive columns, so a run interrupted halfway leaves a consistent store and
    the next run simply picks up what is still due.
    """
    stamp = _now()
    for finding in report.findings:
        if finding.verdict == VERDICT_PROMOTE:
            # The tier clock restarts on promotion: the next review is measured
            # from here, which is what makes each tier a longer reprieve.
            with store.connection:
                store.connection.execute(
                    "UPDATE memories SET tier=tier+1, tier_since=?, tier_since_query=? "
                    "WHERE project_id=? AND uuid=?",
                    (stamp, queries, store.project, finding.memory_id))
            finding.tier += 1
        elif finding.verdict == VERDICT_ARCHIVE:
            store.archive_memory(finding.slug, archived=True)
        elif finding.verdict == VERDICT_DELETE:
            # delete_memory writes the body to `revisions` before removing it,
            # so even this is recoverable from the database itself.
            store.delete_memory(finding.slug, finding.slug)


def _record(store: SqliteMemoryStore, report: AuditReport, policy: AuditPolicy,
            now: datetime) -> int:
    """Persist the run and its findings, including for a run that changed nothing.

    Watching what the sweep *would* do is the entire point of this phase, so the
    record is written whether or not anything was acted on.
    """
    stamp = _now()
    with store.connection:
        cursor = store.connection.execute(
            "INSERT INTO audit_runs(project_id, started, finished, applied, queries, examined, "
            "due, promoted, archived, deleted, capped, policy) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (store.project, stamp, stamp, int(report.applied), report.queries, report.examined,
             report.due, report.promoted, report.archived, report.deleted, report.capped,
             json.dumps(policy.describe())),
        )
        run_id = cursor.lastrowid
        store.connection.executemany(
            "INSERT INTO audit_findings(run_id, project_id, memory_id, slug, tier, verdict, "
            "reason, evidence) VALUES (?,?,?,?,?,?,?,?)",
            [(run_id, store.project, f.memory_id, f.slug, f.tier, f.verdict, f.reason,
              json.dumps(f.evidence)) for f in report.findings],
        )
    return run_id


def last_run(store: SqliteMemoryStore, verdicts: tuple[str, ...] | None = None) -> dict[str, Any]:
    """The most recent run for this project, with its findings."""
    run = store.connection.execute(
        "SELECT * FROM audit_runs WHERE project_id=? ORDER BY id DESC LIMIT 1",
        (store.project,)).fetchone()
    if run is None:
        return {"run": None, "findings": []}
    sql = "SELECT * FROM audit_findings WHERE run_id=?"
    params: list[Any] = [run["id"]]
    if verdicts:
        sql += f" AND verdict IN ({','.join('?' * len(verdicts))})"
        params += list(verdicts)
    findings = [
        {"id": row["slug"], "tier": row["tier"], "verdict": row["verdict"],
         "reason": row["reason"], "evidence": json.loads(row["evidence"])}
        for row in store.connection.execute(sql, params)
    ]
    return {"run": {key: run[key] for key in run.keys()}, "findings": findings}


def format_report(report: AuditReport, limit: int = 20) -> str:
    """Human-readable summary for the CLI."""
    did, would = ("promoted", "archived") if report.applied else ("would promote", "would archive")
    lines = [
        f"audit of '{report.project}' - "
        + ("changes applied" if report.applied else "report only, nothing changed"),
        f"  {report.examined} memories, {report.queries} queries served",
        f"  {did} {report.promoted}, {would} {report.archived}"
        + (f", deleted {report.deleted}" if report.applied and report.deleted
           else f", would delete {report.deleted}" if report.deleted else "")
        + (f", {report.capped} held back by the cap" if report.capped else ""),
    ]
    holds = [f for f in report.findings if f.verdict == VERDICT_HOLD]
    if holds:
        lines.append(f"  {len(holds)} not yet reviewable")
    for verdict in (VERDICT_DELETE, VERDICT_ARCHIVE, VERDICT_PROMOTE):
        chosen = [f for f in report.findings if f.verdict == verdict]
        if not chosen:
            continue
        lines.append(f"\n{verdict}:")
        for finding in chosen[:limit]:
            lines.append(f"  {finding.slug}  ({finding.reason})")
        if len(chosen) > limit:
            lines.append(f"  ... and {len(chosen) - limit} more")
    if not report.due:
        soonest = sorted(report.findings, key=lambda f: -f.evidence["exposure_queries"])[:1]
        if soonest:
            gap = soonest[0].reason
            lines.append(f"\nNothing is due yet. Closest: {soonest[0].slug} - {gap}")
    return "\n".join(lines)


def with_overrides(policy: AuditPolicy, **overrides: Any) -> AuditPolicy:
    """Apply CLI overrides, ignoring the ones left unset."""
    return replace(policy, **{k: v for k, v in overrides.items() if v is not None})
