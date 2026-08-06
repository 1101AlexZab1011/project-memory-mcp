# Architecture Cleanup (after 0.6.0)

## Purpose

Twelve phases of features landed on a data model that held, which is the part that was expensive
to get right. What did not keep up is the layering. This plan fixes four specific things, in the
order that risk and dependency dictate rather than the order they were noticed.

Nothing here adds a feature. If a step changes what the product does, it is in the wrong plan.

## The problems

### 1. The store does network I/O while holding the project lock

`store.promote()` calls `federation.RemoteClient.call()`, which is `urllib.request.urlopen` with
a 4 second timeout. In the HTTP server that runs inside `with lock:`
([http_server.py:455](../project_memory_mcp/http_server.py)), and the lock is per project and
covers reads too.

So one `promote_memory` call to an unreachable remote stalls **every other request for that
project** for up to 4 seconds. This is reachable today: `promote_memory` is a live tool.

`store.federated_recall()` has the same shape and is worse per call — it fans out to up to 8
remotes — but it is **not wired to any tool**, so nothing can reach it except tests. That is
problem 4 below, and it is why the fan-out is not the urgent half.

Severity: latent bug, reachable, invisible so far only because the live server has no remotes.

### 2. `sqlite_store.py` is a god object

2,045 lines and 39 public methods. It is the store, and also promotion, federation, messaging,
dedup, anchors, remotes, visibility and usage counting. Every phase added methods here because
this was the object holding the connection.

It has not collapsed — the methods are short and covered — but it is a third of the codebase and
the file where an unrelated break is most likely.

Severity: maintainability, not correctness. Which is why it is scheduled after the bug and not
before it.

### 3. The browser UI is an untested string

About 300 lines of JavaScript inside a Python string literal in `ui.py`. No linting, no parsing,
no tests. The UI tests exercise the JSON endpoints and never the page, so a typo in an element
id or an event handler ships silently and is found by a person clicking.

No build step is a real virtue and this plan keeps it. The problem is not that the JS is inline;
it is that nothing ever reads it except a browser.

### 4. Federated recall is built and unreachable

`federated_recall` exists, is tested, and no tool calls it. `recall` is local-only. So a machine
can publish to a remote and list remotes, but an agent can never *read* from one.

Half of phase 11 is therefore dead code in production. Found while planning this, not before.

### 5. One test fails about one full-suite run in four

`test_concurrent_requests_do_not_corrupt_the_connection`, never in isolation, predates the
Computer. Instrumented in `6038ba6` so the next failure names a cause instead of a count.

## Order, and why

```text
  1. outbox-only promotion   ── fixes the reachable bug, smallest possible change
  2. network out of the store ── completes the seam, makes 4 safe to wire
  3. wire federated recall    ── only now, because 1-2 make it not hold the lock
  4. split the store          ── large, risky, purely internal: last
  5. make the UI checkable    ── independent, can slot anywhere
  6. settle the flaky test    ── waiting on evidence, not on work
```

Steps 5 and 6 depend on nothing and can be done in any gap.

---

## Step 1 — Promotion always goes through the outbox

**The change.** `promote()` stops calling the network. It validates (visibility, ownership,
tier, secret scan), writes an outbox row, and returns. The Computer's `outbox` job delivers.

The machinery already exists and already runs first among jobs. The only reason promotion also
tries inline is that it was written before the Computer existed.

**What it costs.** The return contract collapses from two shapes to one: always `{"queued": id,
"remote": name}`, never `{"promoted": ...}`. That is a simplification, but it is a visible
change to the tool result, so the tool description has to say that publication is asynchronous
and how to check on it.

**What stays inline.** The secret scan. It is local, fast, and the entire point is to refuse
before the memory is anywhere else. Same for the tier and visibility checks — an agent should
be told immediately that a memory is not eligible, not discover it from a job log.

**Also add.** A way to see what happened: extend `list_remotes` output, or add an
`outbox_status` tool returning queued items with their last error. Without it, "queued" is a
promise with no receipt.

**Tests to update.** `test_federation.py` (4 assertions expecting `promoted`),
`test_secret_scan.py` (3). Plus a new one: promotion to a *reachable* remote still does not
touch the network during the call.

**Done when** no `urlopen` can be reached from `store.promote`, and a promote call against a
black-holed address returns in milliseconds.

## Step 2 — All network calls leave the store

**The change.** The store keeps only local work: outbox rows, the remotes table, cache writes.
Everything that speaks to another machine moves to `federation.py`.

Specifically, `federated_recall` inverts. Instead of a store method that calls out, it becomes:

```python
# federation.py
def recall_across(store, query, limit, ..., private_key=None) -> dict:
    local = store.recall(...)          # caller holds no lock here
    answers, failures = fan_out(...)   # network, outside any lock
    store.cache_remote_results(answers)  # short, local, lock-safe
    return fuse(...)
```

The store gains one small public method (`cache_remote_results`) and loses three
(`federated_recall`, `_remote_clients`, and the network half of `promote`).

**Enforce it with a test, not a convention.** A test that walks the import graph reachable from
`sqlite_store` and fails if `urllib`, `http`, or `socket` appears. Layering rules that are only
written down get violated; this one fails the build.

**Caller changes.** `http_server.do_POST` must not hold the project lock across a federated
recall. The pattern is: take the lock for the local query, release, fan out, take it again for
the cache write. That is the same take-a-slice-and-yield discipline the Computer already uses,
so it is a known shape rather than a new idea.

**Done when** the import-graph test passes and the lock is provably not held during a fan-out.

## Step 3 — Wire federated recall to the tool surface

**The change.** `recall` gains `include_remotes` (default false), routing to
`federation.recall_across` when true and when remotes are configured.

Default false on purpose: a local-only store is the normal case, and a default that silently
adds network latency to the most common tool would be a bad trade for the many to serve the few.

The result already reports `sources_answered` and `sources_unreachable`; the tool description
must say that a partial answer is normal and not an error.

**Done when** an agent can read from a remote, and a store with no remotes behaves exactly as
it does today.

## Step 4 — Split the store

Extraction with delegation, in small commits, never a rewrite. The order below is cheapest and
safest first, and each step should leave the suite green on its own.

**4a. Delete the pass-through methods.** `send_message`, `read_messages`, `unread_messages` are
one-line delegators to `messages.py`. `server.py` can call `messages` directly with
`store.connection`. Three methods gone for no behaviour change.

**4b. Extract maintenance.** `archive_memory`, `archived`, `rebuild_derived_edges`,
`check_anchors`, `set_root_path`, `root_path` into `maintenance.py` as functions taking a store.
These are exactly what the Computer calls, so this also puts the Computer's dependencies in one
place.

**4c. Extract dedup.** `duplicate_candidates`, `merge_memories` into `dedup.py`.

**4d. Extract usage.** `load_usage`, `record_use` and the spread-bitmap helpers into `usage.py`.
This one is last of the four because the counters are read from more places than anything else.

**What stays** is the store proper: CRUD, search, ranking support, labels, validation, schema
and migrations.

**Target, so it is falsifiable:** `sqlite_store.py` under 1,200 lines and under 25 public
methods. Not a number for its own sake — it is the check that the split actually happened rather
than moving code and re-importing it back.

**Risk.** This is the step most likely to break something quietly. Mitigation: no signature
changes during extraction. Move the body, leave a delegating method, update callers, then delete
the delegator in a separate commit. Anything that wants to change behaviour is a different
commit from anything that moves it.

### Outcome, and where the plan above was wrong ✔

Done: `messages.py` took the actor guard and the recall notice, `usage.py` took the counters and
the spread bitmap, `maintenance.py` took anchor checking and duplicate nomination.

**2,045 → 1,620 lines, 39 → 30 public methods.** The stated target of 1,200 and 25 is not met,
and it was not a reachable number — it was set before anyone measured the file. What is actually
in there:

| | lines |
|---|---|
| class body | 957 |
| `SCHEMA` DDL string | 229 |
| migrations and `_upgrade` | 213 |
| module helpers, imports, docstring | ~220 |

Reaching 1,200 would mean cutting another 420, and the only pieces that size are `recall` (163
lines and the heart of the product), the schema, or the migrations. All three are the store
being the store. The floor for this file, honestly, is around 1,500.

Two groupings from 4b and 4c were wrong and were **not** carried out:

- **`rebuild_derived_edges` stays.** It needs `_materialize_derived_edges`, which every write
  also uses. Extracting it would mean making that public — trading an internal call for a
  wider public surface, which is worse than the thing being fixed.
- **`merge_memories` stays.** It reaches five private methods (`_write`, `_visibility_of`,
  `_synchronize_relationships`, `_body_by_uuid`, `_uuid_for`). Same trade, five times over.

`load_usage` and `record_use` remain on the store as thin delegators. That is deliberate rather
than a shortfall: callers speak slugs, the counters are keyed by uuid, and translating between
the two is exactly the boundary the store exists to hold. The arithmetic moved; the boundary
did not.

The lesson worth keeping: a line-count target set without opening the file measures how bold the
plan was, not how well the code is organised.

## Step 5 — Make the UI checkable without adding a build step

**The change.** Move the JavaScript out of the Python string into `ui/app.js`, read from disk at
import and inlined into the page exactly as now. Same single file served, same no-build promise,
but the JS becomes a file a linter and an editor can see.

**Then add the cheap test that catches the real bug.** Parse the page for every `$('#name')` the
script references and assert each id exists in the HTML, and vice versa for the ids the script
must find. This needs no JavaScript runtime and no new dependency, and it catches the class of
error that actually happens: a renamed or typo'd element id.

Full DOM testing would need a browser and a dependency. Not worth it for 300 lines; the id check
gets most of the value for almost nothing.

**Done when** renaming an element id in the HTML without updating the script fails the suite.

### Outcome ✔

`assets/app.js`, read at import and inlined at serve time. One self-contained document over the
wire, exactly as before; the difference is that 175 lines are now a file a linter, an editor and
a test can read. `ui.py` drops from 300 lines to 125.

`tests/test_ui_assets.py` checks both directions — every `$('#id')` the script queries exists in
the markup, and every id the markup defines is queried. Verified by renaming `id="publish"` to
`id="publishBtn"`: both directions fail and name the id.

## Step 6 — Settle the flaky test

No work to schedule, only a trigger. The instrumentation from `6038ba6` records the failing
worker and its exception. The next occurrence either names a cause — in which case fix that — or
shows a clean count mismatch, which would point at the harness rather than the server.

One thing worth doing now regardless: give the test an explicit barrier so all four threads
start together. Today they start as they are spawned, which means the test may not be exercising
the contention it claims to.

**Do not** mark it skip or add a retry. A test that fails one run in four is reporting
something.

### Outcome — improved, not proven ✔

The barrier is in, and it mattered more than expected: without it, four threads spawned in a
loop could each finish before the next began, so the test could pass having never overlapped a
single request. It was not reliably exercising the thing it is named after.

Failures are now sorted into two kinds, because they mean opposite things. A non-200 is the
server getting it wrong — the lock. A transport error is this machine running out of sockets
under the rest of the suite, which is not this server's defect. Both still fail; only the
message differs.

**Nine consecutive clean full-suite runs.** At the historical rate of roughly one failure in
four, nine clean runs would happen about 7% of the time — so this is evidence, not proof.

A plausible mechanism exists and is worth recording: before step 1, `promote` opened outbound
connections inline, and the federation tests promote repeatedly to dead addresses
(`127.0.0.1:9`, `192.0.2.1:9`). Every one of those was a real connect attempt leaving a socket
in `TIME_WAIT`. Moving promotion onto the outbox removed that traffic from the suite entirely,
which fits the socket-exhaustion reading of the original failure. Fits — it does not confirm.

The instrumentation stays. If it recurs, it now says which kind it was.

## Not doing

- **No storage abstraction.** SQLite is the only backend and there is no second one coming. An
  interface with one implementation is a cost with no payer.
- **No async rewrite.** Threads plus per-project locks are adequate at this scale and the
  Computer already handles the heavy work. Async would touch every module to fix a problem the
  outbox already solves.
- **No UI framework.** The zero-dependency, zero-build property is worth more than the
  convenience.
- **No counter rollup cache.** Zero replicas today; a materialised aggregate would add a
  staleness surface for no current gain.

## Risks

- **Step 4 is the dangerous one and delivers the least visible value.** If time runs short, do
  1 through 3 and stop. A large store that works beats a well-split store that regressed.
- **Step 1 changes a tool's result shape.** Any agent or script reading `promoted` breaks. The
  live server has no remotes and no second client, so today the blast radius is zero — which is
  exactly why it should happen now rather than later.
- **Step 3 adds latency to `recall` for anyone who opts in**, and recall is the hottest path in
  the system. The default must stay local-only.
