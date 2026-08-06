# Memory Lifecycle and Distribution (0.5.0)

## Purpose

Two problems, one design.

**Memories accumulate and nothing removes them.** Auto-remember fires on the model's own
judgment, which means the store grows without a corresponding path out. Nothing today
distinguishes a lesson that has proven itself from one recorded once and never touched again.

**A single server is a single point of failure.** With 0.4.0 the agent's memory is exactly as
available as one machine. If it sleeps, every agent using it loses memory entirely — not
degraded, gone. Adding a second person makes that worse, not better.

The answer to both is the same structure: memories start local and unproven, earn their way to
a shared store, and are reviewed on an escalating schedule. The memory hierarchy becomes the
network topology.

## Why aging alone is not enough, and why usage alone is not either

A pure age rule deletes knowledge that is rare and expensive to relearn. Value is roughly
*cost to re-derive* × *probability of needing it*; frequency measures only the second factor,
and the facts recalled constantly are usually the ones an agent could rediscover in minutes
anyway.

A pure usage rule has a subtler failure: `surfaced` is decided by the ranker, not by the
memory. A memory that consistently lands eleventh when recall returns ten is never surfaced,
never applied, and would be deleted — so a ranking error becomes permanent and unfalsifiable.

The resolution is that non-recall becomes meaningful only over long horizons and enough
queries. Over a year and thousands of queries, a memory that never surfaced is genuinely
worthless. Over seven days it may simply have a recurrence period longer than the window —
release processes recur in weeks, build-system knowledge surfaces when the build breaks.

So the gates must be *late enough to be evidence* and the early ones must be *reversible*.

## Decisions taken

- **Capacity-bounded tiers, not calendar tiers.** Tier 1 holds N memories; filling it triggers
  review of what has lived there long enough.
- **Archive before delete.** Early gates remove a memory from the ranked pool and keep it on
  disk. Only the long gate deletes.
- **Delete on evidence, never on absence of evidence.** Low counters are absence.
- **The nursery is local, the shared tiers are remote.** Clients own what they create until it
  is promoted.
- **Reads are cached, not cloned.** A new client starts empty and accumulates its working set.
- **Each side audits only what it exclusively owns.** No coordination, no conflicting verdicts.
- **The server never trusts client clocks.** Age is measured in server time.
- **No TLS in this server.** The overlay network provides it. See "Network".

## Architecture

```text
   client machine                                    host machine
  ┌───────────────────────────────┐                ┌──────────────────────────┐
  │ agent ── local MCP server     │                │ project-memory-mcp serve │
  │            │                  │                │                          │
  │   ┌────────┴────────┐         │  promotion ───▶│  ┌────────────────────┐  │
  │   │ nursery (owned) │─────────┼───────────────▶│  │ shared tiers       │  │
  │   └─────────────────┘         │  corrections   │  │ SQLite + FTS5      │  │
  │   ┌─────────────────┐         │                │  └────────────────────┘  │
  │   │ cache (borrowed)│◀────────┼─── query ──────│         │                │
  │   └─────────────────┘         │    changelog   │  remote sweep            │
  └───────────────────────────────┘                │  dedup at promotion      │
                                                   │  web UI on :8765/        │
   browser ─────────────────────────────────────▶  └──────────────────────────┘
```

The client holds two stores with different rules. The **nursery** is memories it created and
still owns: it writes them, ranks them, sweeps them, and promotes them. The **cache** is
memories the remote owns: read-only, evictable, refreshed from a changelog, never swept.

The browser is always a thin client. It cannot hold a replica, and it does not need to.

## The lifecycle

```text
        created                    promoted
           │                          │
   ┌───────▼────────┐        ┌────────▼────────┐      ┌──────────┐      ┌─────────┐
   │ tier 1         │───────▶│ tier 2          │─────▶│ tier 3   │─────▶│  ...    │
   │ nursery, local │        │ shared          │      │ shared   │      │         │
   └───────┬────────┘        └────────┬────────┘      └────┬─────┘      └─────────┘
           │                          │                    │
           ▼                          ▼                    ▼
      archived (local)          archived (shared) ─────────────────▶ deleted
                                                              (evidence only)
```

Promotion out of the nursery is the moment a memory becomes shared, and therefore the moment
it becomes visible to other people, subject to dedup, and eligible for a secret scan.

**Capacity, not calendar, triggers review.** When tier 1 exceeds N, the memories that have
lived there long enough are judged. Everything else waits.

**Eligibility is measured in queries, not days.** A project that goes quiet for six weeks
serves no queries, so its memories are never judged as "unused" — they were never asked. Age in
wall-clock time is a separate signal, not the clock.

## Signals

| Signal | What it measures | Merges by |
|---|---|---|
| `surfaced` | Did recall return it | sum across replicas |
| `applied` | Did it inform the work | sum across replicas |
| spread bitmap | Distinct days with a recall | bitwise OR |
| wall-clock age | Did the problem outlive the session | server timestamp |
| graph degree | Do other memories depend on it | computed remotely |

**Spread, not rate.** `uses ÷ elapsed` rewards bursts: thirty hits during one afternoon's
debugging outscores one hit a month for a year, though the second is what proved durable. Count
distinct days instead, as a 64-day rolling bitmap — one bit per day, eight bytes, set on
recall, popcount to read. It merges across replicas by OR, which is exact rather than
approximate, and its storage is constant.

**Graph-surfaced memories must not free-ride.** `_record_usage` currently increments `surfaced`
for everything in a result set, including memories pulled in by the graph walk rather than by
matching the query. Left alone, a junk memory attached to a popular one inherits immortality
from its neighbour. Direct text matches and graph expansion must be counted separately, and
only the former advances a tier.

**`applied` is aspirational.** `record_memory_use` exists and the recall skill mentions it, but
calling it is model-discretionary and it has not been observed to fire. Until that changes the
gates run on `surfaced` alone — which is recorded automatically, cannot be under-reported, and
is sufficient. `applied` refines ordering within a cohort; it does not gate.

## Local audit — the nursery sweep

Triggered by a capacity check on the write path that enqueues the project, plus a slow floor
timer so the ladder keeps moving when writing is quiet. A background worker does the work; the
agent's call never waits.

Three outcomes, which is what distinguishes it from the remote sweep:

- **Promote** — earned it; goes to the outbox.
- **Archive** — did not earn it; stays local, leaves the ranked pool.
- **Delete** — evidence only: duplicate, superseded with a live successor, or marked wrong.

Two details that are easy to get wrong:

**Counters travel with the promotion.** A memory that proved itself over three weeks locally
must arrive carrying that history, or it looks brand new and has to earn its place twice.

**Archived memories do not count toward capacity.** Otherwise the nursery fills with archives,
the gate stops firing, and the ladder silently stalls.

The nursery is capacity-bounded and single-user, so this sweep stays cheap no matter how large
the shared store grows.

## Remote audit — the shared sweep

Triggered by per-tier capacity bounds, a daily floor timer, and a dedup check on each arriving
promotion.

Outcomes are promotion between tiers, archival out of the ranked pool, and evidence-based
deletion. Cost stays bounded because only what is due is examined: work per sweep is the sum
over tiers of `tier_size ÷ tier_window`, and larger tiers carry proportionally longer windows.

Four safeguards belong here specifically, because this sweep acts on data other people depend
on:

- **A cap per run** on how much may be archived or deleted. A miscalibrated threshold costs a
  slice and gets noticed, rather than the store.
- **A written record** of what was touched and why, surfaced in the UI. You must be able to
  audit the auditor.
- **Report-only mode.** Run the full sweep, write the record, change nothing. Same code path,
  so what you observe is what you will get.
- **Idempotent and interruptible.** State lives in tier and timestamp columns, never in sweep
  progress.

## Deletion rules

Only four cases justify removal, and all four are positive knowledge about the memory:

**Redundancy.** Two memories carrying one fact. Merge and delete the loser.

**Supersession with a live successor.** B replaces A and B is still active. *Live* matters: if
B was later marked wrong and A is already gone, both the old answer and the new one are lost.

**Wrong with no warning value.** The only case where keeping is actively harmful rather than
merely wasteful — a false memory still ranks at 0.2 and can still surface.

**Dead scope.** The subsystem it describes no longer exists. This is checkable, but only by an
agent inside the repository; the server has no filesystem access to any project.

## Merging

Similarity nominates. It never decides.

Mechanics: one memory is kept; anything the other has that it lacks is folded in; counters are
summed, since both were evidence about one fact; links are repointed; the loser remains as a
redirect so that a bad merge can be undone and stale references still resolve.

Three deciders, in order of cost:

1. **Near-identical** — text, labels, and files all matching — merges automatically.
2. **Everything else goes to an agent.** "Same fact or different facts?" is reading
   comprehension, which a model does well and a similarity score does not.
3. **Anything the agent is unsure about goes to the UI.**

Sync increases duplicate pressure — two people working offline will independently record the
same lesson — so this path carries more load than it would in a single store.

## Sync

The governing rule: **sync is never on the critical path of a recall.** Data a few minutes
stale beats a recall that blocks on a round-trip or fails when the host is asleep.

**Push** is opportunistic and batched with a short debounce, retried from the outbox on
failure. The agent never sees an error and never waits.

**Fetch** happens on connect and on a background timer, never before a recall.

**Manual sync** is exposed as a CLI command and a UI button.

**Counters push as snapshots, not increments.** Each replica exclusively owns its own counter
rows, so "replica A's count for memory X is 47" is an idempotent overwrite. It can be sent once
a minute or once a day with identical results — no double-counting, no ordering requirement. A
thousand increments collapse to one row per touched memory per flush. Without this, counter
traffic swamps everything else in the system.

**Cache refresh uses the changelog.** The server stamps every change with an increasing
sequence number; the client asks what changed since the last one it saw and applies only the
changes touching memories it holds. Cost scales with change volume, not store size.

Order of application on reconnect: status changes first (`wrong`, `archived`), then content
edits, then deletions. A cached memory the server has marked wrong is worse than no cache at
all — the agent recalls it offline and acts on information already known to be false. An
offline client can be wrong for as long as it is offline; that is the price of working
disconnected, and it should be stated rather than papered over.

## What agents may change

Statistics cannot tell you a memory is false. Only an agent in the actual code can notice that
a build command changed, that a file no longer exists, that two memories contradict each other,
or — the strongest signal available — that it followed the advice and the advice failed.

| Action | Nursery | Shared |
|---|---|---|
| Mark status (`stale`, `wrong`) | free | free |
| Add information | free | free |
| Replace or remove information | free | allowed, versioned, attributed |
| Hard delete | on evidence | **no** — mark wrong; server or human decides |

Additive edits cannot conflict harmfully. Replacements can: one agent seeing `ninja release` in
its checkout may rewrite a memory that is still correct for someone on another branch. So every
replacement records which client made it and keeps the prior version.

**Shared edits go to the server, never to the local cached copy.** Cached rows are evictable, so
an edit living only in the cache can vanish; and two people editing their own copies restores
the multi-master conflicts that making the remote authoritative was meant to remove. Offline,
edits queue in the outbox.

**Optimistic versioning is required.** The cached copy carries a version; the edit declares
which version it is changing. If the server has moved on, it rejects and the client re-reads.
Without this, a correction queued offline on Monday silently wipes two other people's
corrections when it lands on Friday.

## Security

The 0.4.0 shared static token is right for one person and wrong for this design, in three
specific ways — each of which breaks something above.

| Missing | What it breaks |
|---|---|
| Attribution | Per-replica counters and edit tracking; a client can claim any identity |
| Revocation | A lost laptop means rotating for everyone, so nobody rotates |
| Permissions | The edit table above is advisory; anyone reaching the port can delete the store |

**Per-client credentials.**

```sql
clients(client_id PK, replica_uuid, name, role, token_hash,
        project_scope, created, last_seen, revoked)
```

Store the hash, never the token — this server ships scheduled backups and JSON export, and
plaintext credentials would leave in every snapshot. The table is excluded from export.

Two roles cover the real cases. **contributor** promotes, edits, and marks status.
**admin** additionally hard-deletes, enrolls, and revokes.

**Project scoping** becomes possible for the first time. One server hosts many projects and one
token currently opens all of them; a credential should open only the projects its holder works
on.

### Attribution

Each client is named at enrollment — "alex-desktop", "ci-runner", "laptop" — and the server
records that name against every write it accepts: creation, promotion, status marks, content
edits, merges, deletions. The `revisions` table already stores prior bodies, so adding the
client id there yields a full "who changed what, when" history at nearly no cost.

The name must be a *display field*, never the identity. Attribution keys on the credential's
`client_id`; renaming a machine then leaves history intact, and two people picking "laptop" is
harmless. This is the same rule as memory slugs, for the same reason.

A self-asserted name is trustworthy here only because the server issues the credential and
stores the name beside it, so a client cannot write under someone else's identity. This is why
attribution requires per-client credentials and cannot be retrofitted onto a shared token.

Naming is a setup decision made by a person, defaulting to the hostname — not something an
agent invents per session, which would scatter history across names that identify nothing.

Where it surfaces: always in the UI, and in `get_memory` detail. Not in every `recall` hit,
where it would spend context on provenance the agent usually does not need. It earns its place
on the detail view — knowing a memory came from someone else's machine is exactly the context
that explains why it describes a build command yours doesn't have.

**Joining** must not send the real credential over chat or email:

1. An admin creates a client in the UI and receives a short code, valid 15 minutes, single use.
2. The new machine runs the join command with the server address and that code.
3. It generates its own replica UUID and sends it with the code.
4. The server burns the code, creates the client row, and returns a long-lived token.
5. The client stores the token with restricted file permissions.

A leaked code after use is worthless; before use its window is fifteen minutes and one attempt.

Nothing is downloaded on join. Because of the nursery-and-cache design, joining a store of
three million memories takes the same fifteen seconds as joining an empty one.

## Network

Radmin is the wrong dependency: Windows and iOS only, nothing for Android or Linux, and
proprietary. The leased address `26.114.199.95` is also hardcoded in three places, so every one
of them breaks if the host, network, or tunnel changes:

```text
<project>/.mcp.json                          client
<project>/.codex/config.toml                 client
<host>/project-memory/run-server.cmd         server bind
```

The clients cannot stop hardcoding an address until there is a *name* to point at, and that
name comes from the overlay's DNS. So de-hardcoding is not independent work to do first — it
lands with the network switch, in one pass.

**Use a WireGuard-based overlay and refer to the server by name.** Tailscale covers every
platform, traverses NAT automatically so a laptop works away from the LAN, and gives stable
addresses with DNS names. Headscale is an open-source control plane for the same clients if
depending on a third party is unacceptable. Plain WireGuard is fully open but needs manual keys,
manual addressing, and a public endpoint for NAT traversal.

**This is also the answer to TLS.** An authenticated, encrypted overlay covers MCP traffic, the
UI login, and everything else on that port, with no certificates to manage and nothing new in
the codebase. The rule becomes: do not implement TLS here; make the network trustworthy and bind
only to it. If the server is ever bound to a plain LAN address as a fallback, credentials are in
the clear on that network — that warning belongs in the docs.

None of this is an architecture change. The server binds to an address and does not know what
created it.

## Secrets

[SKILL.md:98](../project_memory_mcp/skills/project-memory-remember/SKILL.md) lists
`secrets, credentials, tokens, or personal data` among eleven things not to remember. That is
weak in three ways: it reads as a ban on the *topic* rather than on the *value*, so a useful
memory about where configuration lives is either skipped or recorded with the secret attached;
it carries the same weight as "typos" though it is a harm rule and not a triviality rule; and it
predates sharing, when a leaked secret sat in a repo that already had access control.

Two changes. State it positively and separately — record where a secret lives and what shape it
takes, never the value. And scan at promotion for obvious secret shapes, holding the memory for
review rather than publishing it. Skill text is advisory, and `applied` being zero everywhere is
direct evidence that advisory instructions in these skills do not reliably fire; the scan is
what enforces it.

## Risks

- **The correctness half depends on agents reporting what they notice, and we have evidence
  that discretionary reporting does not happen.** `record_memory_use` has never fired. If
  marking `stale` and `wrong` behaves the same way, the statistical half — which cannot detect
  wrongness — is all that remains. This is cheap to test before building on it: run agents
  against the current store for two weeks and count how often they mark anything.
- **Thresholds are guesses until there is data.** N, the tier windows, the similarity cutoff.
  They must be relative to a store's own distribution or configurable, never constants fitted to
  one project. Report-only mode exists so that the first set can be observed rather than
  trusted.
- **Offline quality drifts.** Tiers are assigned remotely, so a long-disconnected replica
  accumulates unaudited memories competing at full weight. Nothing breaks and nothing slows —
  ranking is size-independent — but results get quietly worse the longer it stays away.
- **Split-corpus ranking is approximate.** BM25's IDF depends on the corpus, so a score from
  the nursery and one from the remote are not directly comparable. Interleaving by rank rather
  than score is sound and slightly worse than a unified store.
- **The graph walk cannot cross the boundary cheaply.** Each side walks its own graph and
  results merge, so cross-boundary relationships are weaker until promotion.
- **Rare-but-critical knowledge can still die in a nursery.** It never accumulates enough local
  usage to earn promotion, so a hard-won lesson evaporates on one laptop. Promotion needs a
  non-statistical path: the agent marking a memory as worth sharing, and a human promoting from
  the UI.
- **Complexity.** This is the largest piece of work in the project so far, and sync is where
  distributed systems go wrong. The phasing below exists to keep the irreversible parts last.

## Phases

6. **Sync-ready schema.** UUID identity with the slug demoted to a display field; per-replica
   counter rows; the spread bitmap; separated direct/graph surfacing counts. No behaviour
   change and no sync — but these are the parts that are cheap at 82 memories and painful at
   82,000, and impossible once two writers have both created `shader-compile-stall`.
7. **Audit, report-only.** The full sweep on a single store, writing its record, changing
   nothing. Watch it against real usage before letting it act.
8. **Audit acting.** Archive tier, evidence-based deletion, dedup with the agent as decider.
9. **Per-client credentials.** Client table, roles, enrollment codes, project scoping,
   revocation. Overlay-network documentation and the removal of the hardcoded address.
10. **Nursery, cache, and sync.** Promotion, outbox, changelog, optimistic versioning,
    tombstones.

Each phase is useful alone. Phases 6–8 deliver the cleanup you want on the current
single-server setup; 9 is worth doing the moment a second person appears; 10 is only worth
building when unreliability actually costs something.
