"""Are the audit's gates reachable by a project that is actually used?

Each tier gate needs BOTH exposure (relevance-mode recalls served by the project
since the memory entered the tier) AND wall-clock days. Whichever takes longer
binds. This runs the real `_judge` rather than reasoning about it, so the answer
is the code's, not mine.

No real store is read: memories are synthetic and the usage history is imposed
by setting the counters the audit reads.
"""
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from project_memory_mcp.audit import DEFAULT_GATES, AuditPolicy, run_audit  # noqa: E402
from project_memory_mcp.sqlite_store import SqliteMemoryStore  # noqa: E402

#: Recalls per day, as a project is really used. A "query" is one relevance-mode
#: recall; browsing the UI does not count, and neither does a `recent` listing.
PROFILES = {
    "heavy (daily, 20/day)": 20.0,
    "steady (weekday, 8/day)": 8.0 * 5 / 7,
    "moderate (3 days/wk, 10)": 10.0 * 3 / 7,
    "light (weekly, 5)": 5.0 / 7,
    "occasional (2/month, 5)": 10.0 / 30,
}


def days_to_gate(gate, rate):
    """Days until a memory entering this tier could be reviewed at all."""
    by_queries = gate.min_queries / rate if rate else float("inf")
    return max(by_queries, gate.min_days), ("queries" if by_queries > gate.min_days else "days")


print("Time for ONE memory to become reviewable, per tier gate")
print(f"{'usage':<26} " + " ".join(f"{'tier ' + str(g.tier):>16}" for g in DEFAULT_GATES))
print("-" * 78)
for label, rate in PROFILES.items():
    cells = []
    for gate in DEFAULT_GATES:
        d, binding = days_to_gate(gate, rate)
        cells.append(f"{d:>8.0f}d ({binding[:3]})")
    print(f"{label:<26} " + " ".join(f"{c:>16}" for c in cells))

print()
print("Cumulative: a memory written today reaches each tier no sooner than")
print(f"{'usage':<26} {'tier 2':>12} {'tier 3':>12} {'tier 4':>12}")
print("-" * 66)
for label, rate in PROFILES.items():
    total, cells = 0.0, []
    for gate in DEFAULT_GATES:
        total += days_to_gate(gate, rate)[0]
        cells.append(f"{total / 365:>9.1f}yr" if total > 400 else f"{total:>8.0f}d")
    print(f"{label:<26} " + " ".join(f"{c:>12}" for c in cells))

print()
print("Crossover: below this rate the QUERY gate binds, above it the DAY gate does")
for gate in DEFAULT_GATES:
    print(f"  tier {gate.tier}: {gate.min_queries / gate.min_days:>5.2f} recalls/day")

# --------------------------------------------------------------- against _judge
print()
print("Confirming with the real audit: one memory, 30 days old, varying exposure")
tmp = tempfile.TemporaryDirectory()
store = SqliteMemoryStore(Path(tmp.name) / "m.db", "demo")
store.add_label("area:x", "x")
store.create_memory({
    "schema_version": 1, "id": "subject", "status": "active",
    "description": "A lesson nobody has looked at yet.", "tags": [], "labels": ["area:x"],
    "scope": {"project": "p", "area": "a", "files": [], "applies_to": []},
    "triggers": ["subject"], "remembered_facts": ["a fact"],
    "solution_pattern": [], "pitfalls": [],
    "evidence": {"created_from_task": "t", "last_validated": "2026-01-01"},
    "relationships": {"related": [], "supersedes": [], "superseded_by": []},
})
old = (datetime.now(timezone.utc) - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%SZ")
with store.connection:
    store.connection.execute(
        "UPDATE memories SET tier_since=?, tier_since_query=0 WHERE project_id='demo'", (old,))

for exposure in (0, 25, 49, 50, 200):
    with store.connection:
        store.connection.execute("UPDATE projects SET queries=? WHERE id='demo'", (exposure,))
    report = run_audit(store, policy=AuditPolicy(), apply=False)
    finding = report.findings[0]
    print(f"  exposure {exposure:>4} queries -> {finding.verdict:<8} {finding.reason}")
store.close()
