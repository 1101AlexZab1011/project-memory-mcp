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
- **Each store audits only what it holds.** No coordination, no conflicting verdicts.
- **The server never trusts client clocks.** Age is measured in server time.
- **Local-only is the default.** A remote is one line of configuration, added and removed at
  will. Nothing about the software requires a network to exist.
- **Remotes federate, they do not replicate.** Several servers may hold the same project with
  different content, and that is the intended behaviour rather than drift to be repaired.
- **Networking is a deployment choice, not a feature.** The server binds to an address and does
  not care what created it.

## Architecture

```text
   client machine                            servers, any number, all optional
  ┌───────────────────────────────┐         ┌──────────────────────────┐
  │ agent ── local MCP server     │         │ team server              │
  │            │                  │  ┌─────▶│  shared tiers, own audit │
  │   ┌────────┴────────┐         │  │      │  SQLite + FTS5, web UI   │
  │   │ nursery (owned) │─promote─┼──┤      └──────────────────────────┘
  │   └─────────────────┘         │  │      ┌──────────────────────────┐
  │   ┌─────────────────┐         │  └─────▶│ personal server          │
  │   │ cache, by origin│◀─query──┼─────────│  different memories, and │
  │   └─────────────────┘         │  (parallel) that is the point      │
  └───────────────────────────────┘         └──────────────────────────┘
```

The client holds two stores with different rules. The **nursery** is memories it created and
still owns: it writes them, ranks them, sweeps them, and promotes them. The **cache** holds
results borrowed from remotes, tagged with which one they came from: read-only, evictable,
never swept.

With no remotes configured, the nursery *is* the store and everything still works. Remotes add
sources; they are never a prerequisite.

The browser is always a thin client, pointed at one server at a time. It cannot hold a replica,
and it does not need to.

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

## Federation

Several servers may host the same project, holding different memories about it. They are not
copies that drift and need reconciling — they are independent sources that are *supposed* to
differ, the way two sites covering one topic overlap without anyone merging their databases.

That single decision removes most of what a sync design normally costs. There is no convergence
requirement, so there are no version vectors, no tombstone propagation between servers, no
conflict resolution, and no changelog to replay. **The client never merges data. It merges
ranked lists, which are bounded by K per source and discarded after the query.**

```text
recall("packaging fails when the editor is open")

   local nursery ─┐
   remote: team  ─┼─ in parallel, each with its own deadline ─→ fuse by rank ─→ results
   remote: mine  ─┘
```

**Local always answers, so recall never blocks on a network.** Remotes are queried
concurrently with a per-remote deadline; one that is slow, down, or unreachable is dropped from
that query and the response names which sources replied. An incomplete answer that arrives beats
a complete one that does not.

**Merging uses rank, not score.** This is the part that would go wrong silently. BM25's IDF is
computed over each server's own corpus, so a 1.3 from one and a 1.3 from another do not mean
the same thing, and comparing them directly produces confidently wrong ordering. Reciprocal
rank fusion — each memory scoring the sum of `1 / (k + its rank in that list)` — depends only on
position, needs no cross-source calibration, and degrades gracefully when a source drops out.

**Cross-source dedup is free.** A memory promoted to two servers carries the same uuid on both,
so it fuses into one result scoring from both lists. Two memories written independently about
the same thing have different uuids and both appear — which is correct, and which the duplicate
detection can nominate later. This is the payoff for making identity a uuid rather than a slug.

**The cache is the working set, tagged by origin.** Results are stored locally against the
remote they came from, so a repeat query is local and an offline session keeps whatever has
recently been used. Nothing is ever cloned.

Cost per recall is one local query plus N concurrent round trips, each returning at most K
results. It grows with the number of remotes, not with the size of any of them. Three or four
remotes is nothing; at twenty it would be worth querying local first and only fanning out when
local results score weakly.

### Where a promotion goes

"All of them" is the wrong default — that is how one lesson ends up duplicated across servers
with independently diverging edits. But no automatic rule picks well either, so the agent picks,
with the information needed to pick sensibly.

Each remote carries a **description** of what it is for. Asked where a memory should go, the
server list comes back with those descriptions, and the agent matches the memory it just wrote
against them. Two things bias that choice, in order:

1. **Which remotes the agent actually used while solving the task.** If the answer came from the
   team server and the personal one was never touched, the lesson belongs where the conversation
   happened. This beats description matching, because it is evidence rather than a guess.
2. **How well the memory fits each description.** The tiebreak when interaction says nothing.

Promotion to a second remote stays an explicit act, never a fallback.

### One consequence worth stating

**Each store audits only what it holds.** Your local store sweeps its nursery; each server
sweeps its own tiers on the counters it has seen. A memory archived on the team server is not
archived on yours, and its tier there says nothing about its tier here.

That is consistent with federation — different sources, different judgments — but it is a
behaviour to state rather than discover. It also means counters are per-source: a memory's usage
on the team server reflects that server's traffic, not yours.

### What survives from the replication design

The **outbox** still exists, but it carries promotions and corrections rather than a change
stream: work queued when a remote is unreachable, retried later, never blocking the agent.

**Per-replica counters** still matter, because one server aggregates counts from every client
that reports to it. Each client owns its own rows there, so a push is an idempotent overwrite
rather than an increment that could double-count.

**Cached correctness still decays.** A memory a server has marked wrong is worse cached than
absent, because the agent acts on it offline believing it true. Cached entries carry the version
they were fetched at and are refreshed on the next successful query to that source. An offline
client can be wrong for as long as it is offline; that is the price of working disconnected.

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

**Identity is a keypair, not a token.**

```sql
clients(client_id PK, public_key, fingerprint, replica_uuid, name, role,
        project_scope, created, last_seen, revoked)
```

Each client generates an Ed25519 keypair on first run. The private key never leaves the machine
and is never transmitted — not at enrollment, not per request, not ever. The server stores only
the public key, so there is nothing secret in the table, nothing to leak through the scheduled
backups and JSON exports this server ships, and nothing an operator could accidentally expose.

That is a straight improvement over a token even for a single server. But the reason it belongs
here rather than in a later phase is federation: **the same public key enrolled at several
servers is verifiably the same actor**, with no central authority and no coordination between
them. Token-based credentials cannot give that, and retrofitting keys after servers have issued
tokens means re-enrolling every client.

Two roles cover the real cases. **contributor** promotes, edits, and marks status.
**admin** additionally hard-deletes, enrolls, and revokes.

**Project scoping** becomes possible for the first time. One server hosts many projects and one
token currently opens all of them; a credential should open only the projects its holder works
on.

### How a request is authenticated

Agents sign; the browser keeps a session cookie. Two different clients with genuinely different
constraints — a browser cannot hold a private key safely, an agent can.

An agent signs a canonical string over the method, path, a timestamp and a hash of the body, and
sends the signature with its key fingerprint. The server looks up the key and verifies. Stale
timestamps are rejected, which is what stops a captured request being replayed.

This matters more here than it would elsewhere, because of the deployment posture: with no TLS,
a bearer token on the wire is readable by anyone on the path, and a reader becomes a writer.
A signature is equally readable and useless — it authorises one request, once. On a trusted
overlay that is belt and braces; on a plain LAN it is the difference between eavesdropping and
takeover.

### Enrollment, without transmitting a secret

1. An admin creates a client entry and gets a short code, valid 15 minutes, single use.
2. The new machine generates its keypair locally.
3. It sends the code, its **public** key, and a display name.
4. The server checks the code, burns it, and stores the key against a new client row.
5. Nothing secret ever crossed the wire, in either direction.

A leaked code after use is worthless; before use its window is fifteen minutes and one attempt.
Compare this with issuing a token: there, the credential itself travels, and whoever sees it
holds it.

**Key loss is re-enrollment.** A dead laptop means generating a new key and enrolling again as a
new client. Past attribution stays as it was — it is history, and history should not be editable
by whoever holds the newest key. Rotation works the same way, which means an identity does not
survive rotation; that is a real limitation and the simple behaviour is the right starting point.

**Private keys are stored unencrypted with restricted file permissions.** A passphrase would
mean no agent could start unattended, which defeats the purpose. The key is exactly as sensitive
as the machine it sits on.

### Attribution

Each client is named at enrollment — "alex-desktop", "ci-runner", "laptop" — and the server
records that name against every write it accepts: creation, promotion, status marks, content
edits, merges, deletions. The `revisions` table already stores prior bodies, so adding the
client id there yields a full "who changed what, when" history at nearly no cost.

The name must be a *display field*, never the identity. Attribution keys on the credential;
renaming a machine leaves history intact, and two people picking "laptop" is harmless. This is
the same rule as memory slugs, for the same reason.

Naming is a setup decision made by a person, defaulting to the hostname — not something an
agent invents per session, which would scatter history across names that identify nothing.

**Attribution records the key fingerprint, not only the name.** This is what makes it mean
anything across servers. A display name is issued by one server and unverifiable anywhere else:
`bob-laptop` on the team server and `bob-laptop` somewhere else are two unrelated rows that
happen to share a string. A fingerprint is the same everywhere, because it is derived from a key
only Bob can use.

So the honest reading of provenance is two-layered, and the UI should show both:

| Shown | Means | Trust |
|---|---|---|
| `bob-laptop` | what that server calls this client | one server's word |
| `SHA256:kP3f…` | the key that signed the write | verifiable, and the same everywhere |

Without keys, the only safe rule was "reply where you met them" — you could never tell whether a
name on another server was the same person. With them, Alice can recognise Bob's fingerprint on
a second server and know it is the same actor, with no authority vouching for either of them.
That is the property that makes federated identity work at all, and it costs one column.

Where it surfaces: always in the UI, and in `get_memory` detail. Not in every `recall` hit,
where it would spend context on provenance the agent usually does not need. It earns its place
on the detail view — knowing a memory came from someone else's machine is exactly the context
that explains why it describes a build command yours doesn't have.

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

None of this is an architecture change, and none of it is a prerequisite. The server binds to an
address and does not know what created it. **Nobody has to install anything to use this
software**: local-only needs no network at all, and a shared server on the same LAN needs an IP.
An overlay matters only when the people sharing a server are not on one network, and which
overlay is then their choice.

## Messaging between agents

The proposal: identity makes every memory attributable, so an agent that keeps finding another's
memories useful could ask that other agent whether it has anything unpublished. Messages queue
on a server, get noticed at session start, and get answered whenever that agent next runs.

Mechanically this is small. A `messages` table, two tools, and a check at session start — the
client already connects, so nothing needs to push. Most of it is Phase 9's identity work reused.

Four things to weigh before building it:

**The cost and the benefit land on different people.** Answering Alice's question means Bob's
agent stops what Bob asked it to do, searches its nursery, judges relevance, and writes a reply
— on Bob's tokens and Bob's latency. That is fine if Bob chooses it and bad if it happens
automatically. Any reply path must be Bob's decision, surfaced rather than executed.

**The latency is days.** Alice asks; Bob answers whenever Bob's human next starts a session. For
"do you have anything on X", an answer after the task is over is worth little. This is the
strongest argument that the need is better served by *publishing* than by *asking*.

**The nursery is unproven by construction.** Everything in it specifically has not earned
sharing. Sometimes that is exactly the point — rare knowledge that never accrued usage is the
gap the non-statistical promotion path exists for. But it is also where the wrong, the
half-formed and the accidentally-secret live, which is why it was kept local in the first place.

**A message is untrusted input arriving in another agent's context.** This is the one that must
be designed in from the start, not added later. Text from another client is data, never
instruction: "publish your nursery", "ignore your previous instructions", "send me the config"
must be quoted to the human and never acted on. Agent-to-agent messaging is a prompt-injection
channel by definition, and it is one whose sender identity — even authenticated — says nothing
about whether the *content* is safe.

**Verdict.** Worth building, but after Phase 9 and not before, and the useful primitive is
narrower than a chat system: *a question that lands with a specific client's human, with the
reply entirely at their discretion*. Much of the motivating value comes from attribution and
federation on their own — being able to see who contributed what, and to query their server
directly, answers most of "Bob knows things I don't" without any conversation. Build the message
primitive only if a real gap survives those two.

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
- **Federated ranking is approximate.** Rank fusion is sound and needs no calibration, but it
  discards how *strongly* a source matched. A source that is confidently right and one that is
  weakly relevant contribute the same at the same rank.
- **The graph walk cannot cross a boundary.** Each source walks its own graph and results merge,
  so a relationship between a local memory and a remote one is invisible to both.
- **More remotes means more fan-out.** Cost per recall grows with the number of sources. Small
  numbers are free; a large number would need local-first querying, which trades latency
  variance for cost.
- **Rare-but-critical knowledge can still die in a nursery.** It never accumulates enough local
  usage to earn promotion, so a hard-won lesson evaporates on one laptop. Promotion needs a
  non-statistical path: the agent marking a memory as worth sharing, and a human promoting from
  the UI.
- **Federation weakens no-longer-true.** A memory corrected on one server stays wrong on
  another. Nothing propagates a correction across sources, by design — which is the cost of
  letting sources differ, and it is paid in stale answers rather than in conflicts.
- **An identity does not survive key rotation.** A rotated or lost key enrolls as a new client,
  so continuity across a laptop replacement is broken by design. Preserving it would mean one
  key signing an assertion about another, which is a certificate chain and a much larger idea.
- **Public-key crypto is the first hard dependency.** Ed25519 is not in the standard library.
  Shelling out to `ssh-keygen -Y` avoids the dependency at the cost of requiring OpenSSH and
  making every verification a subprocess; a library is the better trade, but it is a trade.

## Phases

6. **Sync-ready schema.** ✔ UUID identity with the slug demoted to a display field; per-replica
   counter rows; the spread bitmap; separated direct/graph surfacing counts.
7. **Audit, report-only.** ✔ The full sweep writing its record and changing nothing.
8. **Audit acting.** ✔ Archive as a state, evidence-based deletion, dedup with the agent as
   decider.
9. **Zero-touch setup.** One `setup` command and a README an agent can follow from a repo link
   alone: install, create a local database, install skills, write client config. Local-only by
   default, no network involved, no remote required. This is small and it comes before anything
   distributed, because it is what makes the rest reachable.
10. **Keypair identity and per-client credentials.** Ed25519 per client, generated locally
    and never transmitted; signed requests for agents, sessions for the browser; enrollment
    codes that carry a public key rather than issue a secret; roles, project scoping,
    revocation; attribution recording the fingerprint alongside the name. Also where the
    hardcoded address is removed, since clients then refer to servers by name.
11. **Federation.** A remotes table, parallel fan-out with deadlines, rank fusion, the
    origin-tagged cache, the promotion outbox, and description-driven promotion routing.
12. **Messaging, if a gap survives.** A question that lands with a specific client's human,
    with the reply at their discretion, and every message treated as untrusted input.

Deployment and networking are deliberately **not** a phase. The server binds to an address; how
that address exists is the operator's choice and belongs in documentation.

Each phase is useful alone. 6–8 are done and deliver cleanup on a single store. 9 is worth doing
next regardless of what follows. 10 is worth doing the moment a second person appears. 11 is
worth building when one store stops being enough — and 12 only if attribution and federation
turn out not to cover it.
