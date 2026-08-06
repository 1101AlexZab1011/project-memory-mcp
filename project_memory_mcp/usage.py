"""Usage counters, and the spread bitmap they are read through.

Its own module because it is its own table with its own arithmetic. Everywhere
else in the store, one row is the truth about one memory. Here a memory has one
row *per replica*, and reading a counter means combining them - which is the
only reason the numbers survive several machines using the same project.

**Counters are grow-only and per replica.** A machine only ever writes rows it
owns, so pushing its counters to a server is an idempotent overwrite rather than
a stream of increments that would double-count on a retry. Reading sums across
replicas.

**Spread is a bitmap, not a count.** One bit per day over a 64-day window,
newest in bit 0, and the number of distinct days is a popcount. Two machines
using a memory on the same day is one day of spread, not two - so replicas merge
by OR, which a counter could not express. It answers a question the raw totals
cannot: whether a memory was used repeatedly over time or heavily in one burst,
which is the difference between a durable lesson and a single afternoon.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

#: Days the spread bitmap remembers. 64 so it fits one SQLite integer.
SPREAD_WINDOW_DAYS = 64
SPREAD_MASK = (1 << SPREAD_WINDOW_DAYS) - 1


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today() -> int:
    """Days since the epoch, UTC. The unit of the spread bitmap."""
    return datetime.now(timezone.utc).toordinal()


def as_signed(bits: int) -> int:
    """SQLite integers are signed 64-bit; bit 63 set would overflow on write."""
    bits &= SPREAD_MASK
    return bits - (1 << SPREAD_WINDOW_DAYS) if bits >> (SPREAD_WINDOW_DAYS - 1) else bits


def touch_spread(bits: int, epoch: int | None, day: int) -> tuple[int, int]:
    """Set today's bit, shifting the window forward if the epoch has moved."""
    bits &= SPREAD_MASK
    if epoch is None:
        return as_signed(1), day
    delta = day - epoch
    if delta == 0:
        return as_signed(bits | 1), epoch
    if delta < 0:
        # A clock that went backwards, or a replica that is behind. Record the
        # day in the past rather than shifting the window the wrong way.
        back = -delta
        if back < SPREAD_WINDOW_DAYS:
            bits |= 1 << back
        return as_signed(bits), epoch
    if delta >= SPREAD_WINDOW_DAYS:
        return as_signed(1), day
    return as_signed(((bits << delta) & SPREAD_MASK) | 1), day


def merge_spread(bits_a: int, epoch_a: int | None,
                 bits_b: int, epoch_b: int | None) -> tuple[int, int | None]:
    """Combine two replicas' windows, aligned to the newer epoch."""
    if epoch_a is None:
        return bits_b & SPREAD_MASK, epoch_b
    if epoch_b is None:
        return bits_a & SPREAD_MASK, epoch_a
    newest = max(epoch_a, epoch_b)
    merged = 0
    for bits, epoch in ((bits_a, epoch_a), (bits_b, epoch_b)):
        shift = newest - epoch
        if shift < SPREAD_WINDOW_DAYS:
            merged |= ((bits & SPREAD_MASK) << shift) & SPREAD_MASK
    return merged, newest


def spread_days(bits: int) -> int:
    return bin(bits & SPREAD_MASK).count("1")


def record(store: Any, surfaced: Iterable[str], direct: Iterable[str] = (),
           applied: Iterable[str] = ()) -> None:
    """Bump this replica's counters for the memories a call touched.

    Every id here is a uuid. Rows are keyed by replica as well as memory, so this
    only ever writes counters this machine owns.
    """
    surfaced, direct, applied = set(surfaced), set(direct), set(applied)
    touched = sorted(surfaced | direct | applied)
    if not touched:
        return
    stamp, day = _now(), today()
    with store.connection:
        for memory_id in touched:
            row = store.connection.execute(
                "SELECT spread_bits, spread_epoch FROM usage "
                "WHERE project_id=? AND memory_id=? AND replica_id=?",
                (store.project, memory_id, store.replica_id),
            ).fetchone()
            bits, epoch = touch_spread(
                row["spread_bits"] if row else 0, row["spread_epoch"] if row else None, day)
            store.connection.execute(
                "INSERT INTO usage(project_id, memory_id, replica_id, surfaced, surfaced_direct, "
                "applied, last_surfaced, last_applied, spread_bits, spread_epoch) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(project_id, memory_id, replica_id) DO UPDATE SET "
                "surfaced=surfaced+excluded.surfaced, "
                "surfaced_direct=surfaced_direct+excluded.surfaced_direct, "
                "applied=applied+excluded.applied, "
                "last_surfaced=COALESCE(excluded.last_surfaced, last_surfaced), "
                "last_applied=COALESCE(excluded.last_applied, last_applied), "
                "spread_bits=excluded.spread_bits, spread_epoch=excluded.spread_epoch",
                (store.project, memory_id, store.replica_id,
                 1 if memory_id in surfaced else 0,
                 1 if memory_id in direct else 0,
                 1 if memory_id in applied else 0,
                 stamp if memory_id in surfaced else None,
                 stamp if memory_id in applied else None,
                 bits, epoch),
            )


def load(store: Any) -> dict[str, Any]:
    """Counters per memory, summed across every replica that reported one.

    Grow-only counters from independent replicas add; the spread bitmaps OR,
    because two machines using a memory on the same day is one day of spread.
    """
    rows = store.connection.execute(
        "SELECT m.slug AS slug, u.surfaced AS surfaced, u.surfaced_direct AS surfaced_direct, "
        "u.applied AS applied, u.last_surfaced AS last_surfaced, u.last_applied AS last_applied, "
        "u.spread_bits AS spread_bits, u.spread_epoch AS spread_epoch "
        "FROM usage u JOIN memories m ON m.project_id=u.project_id AND m.uuid=u.memory_id "
        "WHERE u.project_id=?", (store.project,)
    ).fetchall()
    merged: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = merged.setdefault(row["slug"], {
            "surfaced": 0, "surfaced_direct": 0, "applied": 0,
            "last_surfaced": None, "last_applied": None, "_bits": 0, "_epoch": None,
        })
        entry["surfaced"] += row["surfaced"]
        entry["surfaced_direct"] += row["surfaced_direct"]
        entry["applied"] += row["applied"]
        for field in ("last_surfaced", "last_applied"):
            if row[field] and (entry[field] is None or row[field] > entry[field]):
                entry[field] = row[field]
        entry["_bits"], entry["_epoch"] = merge_spread(
            entry["_bits"], entry["_epoch"], row["spread_bits"], row["spread_epoch"])
    for entry in merged.values():
        entry["spread_days"] = spread_days(entry.pop("_bits"))
        entry.pop("_epoch")
    return {"memories": merged}
