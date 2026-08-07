# Correctness Pass (after 0.7.0)

## Purpose

The architecture cleanup fixed the layering. This one fixes the things the layering was hiding:
code that is reachable and broken, and code that is present, documented, and wired to nothing.

Every item below was reproduced by running it, not inferred by reading. The suite is green on all
of them — 344 tests — which is the common thread and the reason they survived twelve phases.

Nothing here adds a feature. Three steps *remove* one. If a step makes the product do something
new, it is in the wrong plan.

## The findings

| # | What | Kind | Reachable by |
|---|---|---|---|
| 1 | UI API ignores `project_scope` | authorization bypass | any enrolled client with a token |
| 2 | A deleted memory permanently jams the outbox | data-flow dead end | one `delete_memory` |
| 3 | Three tests never run (duplicate class name) | coverage hole | — |
| 4 | `pmm migrate` raises `TypeError` on every call | broken on contact | anyone following the startup banner |
| 5 | `enroll` creates a phantom `bootstrap` project | side effect | every enrollment code minted |
| 6 | The remote cache is write-only for its stated purpose | unfinished feature | every federated recall |
| 7 | Outbound request signing is unreachable | unfinished feature | every remote call |
| 8 | `related_label_query` accepted everywhere, read nowhere | false promise to the agent | every `create_memory` |
| 9 | A remote can never be disabled | missing path | — |
| 10 | Dead code: `flush_usage`, `recent()`, stale `build/`, orphaned comment | clutter | — |
| 11 | `search_memories` scans and parses the whole project | O(n) read | every `search_memories` |

## Order, and why

```text
   1. un-shadow the three tests   ── free, and may change what we think is true
   2. scope the UI session        ── the bypass; destructive and confirmed
   3. outbox survives a delete    ── silently disables federation, one call away
   4. decide `migrate`            ── broken on contact, blocks nothing, embarrassing
   5. stop `enroll` making a project
   ---- above: things that are wrong. below: things that are absent ----
   6. finish or drop the remote cache
   7. wire outbound signing
   8. delete `related_label_query`
   9. enable/disable a remote
  10. dead code sweep
  11. index `search_memories`
```

**Step 1 goes first because it is free and it is evidence.** Those three tests have never executed
against current code — they were shadowed before the uuid migration, the usage extraction and the
spread bitmap all landed. They may pass. If they fail, they are reporting on the counter code that
steps 6 and 7 will lean on, and that changes this plan rather than being a footnote to it.

Steps 2 and 3 are the two that hurt. Everything below the line is the system describing itself
inaccurately, which costs most over time and nothing today.

Steps 9, 10 and 11 depend on nothing and can fill any gap.

---

## Step 1 — Un-shadow the three replica tests

**The change.** `tests/test_lifecycle_schema.py` defines `ReplicaCounterTests` twice, at line 128
and line 368. Python rebinds the name; pytest collects only the second. Rename the first to what
it actually covers — it is store-level (`StoreCase`), the second is `usage`-level.

Lost today:

- `test_counters_from_two_replicas_add_up`
- `test_a_replica_only_ever_writes_its_own_row`
- `test_the_replica_id_is_stable_across_reopening`

**Do this before reading anything else in this plan as settled.** Run them, and if they fail, fix
the code before continuing — three untested claims about per-replica counters are load-bearing for
steps 6 and 7.

**Also.** Add a collection-count guard so a shadowed class cannot happen again silently: assert the
suite collects a minimum number of tests, or lint for duplicate top-level names. The second is
better and is four lines of `ast`.

**Done when** both classes are collected, and the suite fails if a module defines the same
top-level class twice.

### Outcome ✔

The first class is now `ReplicaIdentityTests` — a better name anyway, since the three ask about
*identity* (is a foreign row summed on read, left alone on write, and does this machine's id survive
a reopen) while the other class drives `usage.record` under two identities to check the arithmetic.

**All three pass.** The counter code was sound, so steps 6 and 7 stand as scoped. That was the
question worth asking first, and the answer is the boring one.

But passing was not the whole story, and this is the part worth keeping. Mutation-checking the three
before trusting them, `test_a_replica_only_ever_writes_its_own_row` **survived** a mutant that keyed
every write to the constant `"shared"`. The test inserts a row owned by `'other-machine'` and asserts
it is untouched — and a write to `"shared"` does not touch it either. It proved this replica does not
write to a row named `other-machine`; it never checked the write landed on *this replica's own id*,
which is the property in its name. Now asserts both halves, and the mutant is caught.

The stray `if __name__ == "__main__": unittest.main()` sitting mid-file is how the collision happened
— the second class was appended below it, where nothing reads. Moved to the end.

The guard lives in `tests/test_layering.py`, which already enforces structural rules with `ast`: no
module in the package or the suite may bind the same class or function name twice at top level. Only
`def` and `class`, since a reassigned module constant is ordinary. Verified by renaming the class
back and watching it fail, naming both the module and the duplicated name.

**344 → 348 tests**: three revived, one guard.

A note for the steps below. Two things passed here that a green suite had asserted for months: the
three tests were never collected, and one of them was decorative. Neither shows up as a failure —
only as a number nobody counts. Step 2 makes the same kind of claim about authorization, which is
why its mutation check is not optional.

## Step 2 — The UI session carries an identity

**The problem.** `_require_project` is called on `/mcp` and nowhere else. `/api/*` gates only on a
session cookie, and `_Sessions` stores an expiry and nothing else. `clients.token_is_valid` returns
true for any non-revoked client token without reading `project_scope`.

Reproduced against a client scoped to one project:

```text
MCP  → secret-proj : 403 this client has no access to project 'secret-proj'
UI   → GET  /api/memories?project=secret-proj : 200  (full bodies)
UI   → GET  /api/projects                     : 200  ["public-proj","secret-proj"]
UI   → POST /api/delete   on secret-proj      : 200  {"deleted": ...}
```

The delete succeeded. Note the inversion this creates: enrolling **with** a key yields no bearer
token and therefore no UI access at all, while enrolling **without** one yields unscoped access to
every project. The weaker credential is the more powerful one.

**The change.** Three parts, none large.

1. Replace `clients.token_is_valid` with `clients.client_for_token(connection, presented) ->
   Client | None`. The shared master token resolves to `SHARED_TOKEN_CLIENT`, which is already
   defined as unscoped admin for exactly this reason.
2. `_Sessions.create(client)` stores the `Client`; `_has_session()` becomes `_session_client()`.
3. Every `/api/*` handler that names a project calls the same `_require_project` the MCP path uses.
   `/api/projects` filters its list by `may_access`.

**What it costs.** Nothing to the master-token operator, who is unscoped by definition. A scoped
client loses access it should never have had, which is the change.

**Verify by mutation, not only by test.** Make `may_access` return `True` unconditionally and
confirm the suite fails on the UI path as well as the MCP path. The MCP-path assertion already
exists and passed throughout this bug's life, which is precisely why a second one is needed.

**Done when** the repro above returns 403 on all three UI calls, and `/api/projects` shows one
project.

## Step 3 — A deleted memory cannot jam the outbox

**The problem.** `delete_memory` cleans `memories`, `labels`, `files`, `usage`, `memories_fts` and
`edges` — not `outbox`. The orphaned row's `LEFT JOIN` then yields `slug=NULL`, and delivery dies
on it:

```text
promote → queued.  delete → deleted.  outbox rows left: 1
deliver_outbox RAISED: StoreError: Unknown memory id: None
```

`ORDER BY o.id` puts the orphan first, so it blocks every promotion queued behind it, on every run,
forever. `Computer.run_one` catches the exception and records a failed job, so federation stops
working and nothing says so.

**The change.** Both halves, because they fix different things:

- `delete_memory` deletes the memory's outbox rows. This prevents new orphans.
- `deliver_outbox` skips a row whose memory is gone, and drops it with a recorded reason. This
  handles orphans already sitting in a live database, and any future path that removes a memory
  without going through `delete_memory`.

**While here, decide the archived case.** `merge_memories` archives the loser rather than deleting
it, so its slug survives and a queued promotion for it stays deliverable. Publishing a memory that
was just merged away is almost certainly wrong. Treat archived like deleted: drop it from the
outbox with a reason.

**One job failing must be visible.** The deeper problem is that `run_one` swallows this. Surface
`Computer.last_error` and the last failed job in `/api/audit` or a sibling endpoint — a background
worker whose failures only reach a log nobody opens is a worker you cannot trust.

**Done when** delete-then-deliver drains cleanly, a pre-existing orphan is dropped rather than
raising, and a failing job is visible without reading the `jobs` table by hand.

## Step 4 — Decide what `migrate` is for

**The problem.** `migrate_from_files` calls `store._write(memory)`; `_write` has required
`memory_uuid` since schema v2. Every invocation raises `TypeError`. No test covers it, and the
server's startup banner recommends it:

```text
projects: (none - create one with `migrate` first)
```

**The decision, and it is a decision.** The file backend is gone. This imports a `.project-memory`
directory that only exists on installations that predate schema v1, and the live store was migrated
long ago. So:

- **If any such directory still exists** — fix it. The change is one line plus a uuid, and it needs
  a test that imports a real fixture directory and validates the result.
- **If none does** — delete the command, the function and the banner text. A command that has never
  worked in its current form is not a migration path, it is a claim.

Answer that before writing code. **Default to deleting it**: nothing in the repository, the tests or
the docs references a file-backed store as a live thing, and keeping a broken import path is worse
than not offering one.

Either way the banner must stop naming a command that does not do what it says. `init` is what
creates a project.

**Done when** `migrate` either works against a fixture or does not exist, and the startup banner
names a command that works.

## Step 5 — Minting a code stops creating a project

**The problem.** `cmd_enroll` opens `SqliteMemoryStore(database, "bootstrap", create=True)` to force
the server-wide tables into existence. That inserts a row in `projects`:

```text
before enroll: ['real-project']
after  enroll: ['bootstrap', 'real-project']
```

`bootstrap` then appears in the UI project list, is swept by the scheduler, and is offered to every
client.

**The change.** Create the tables without creating a project. The schema script is already
idempotent and separable from the `projects` insert; run the DDL directly against the connection.

**Also clean up.** Existing databases have the phantom row. A one-line note in the release text is
enough — deleting somebody's project row automatically is not this command's business, and a
`bootstrap` project holding nothing is harmless once it stops being created.

**Done when** minting a code against a fresh database leaves `projects` empty.

## Step 6 — Finish the remote cache, or drop it

**The problem is larger than it first looks.** `cache_remote_results` claims the cache is the
working set: "whatever has recently been needed stays reachable when a remote is not." It is not.
Cached rows are inserted directly, bypassing `_write`, so **`_index_text` never runs on them** and
they are absent from FTS. The consequence:

```text
recall(query='shader stalls') : ['local-one']                    ← cache invisible
recent()                      : ['remote-one', 'local-one']      ← cache leaks in
recall(order='recent')        : ['remote-one', 'local-one']      ← cache leaks in
search_memories()             : ['local-one', 'remote-one']      ← cache leaks in
```

So the cache is unreachable by the one path that would use it, and leaks into the three that should
not show it. `created` is set to the caching timestamp, which sorts borrowed memories to the *top*
of "what have I learned lately". `_light_record` does not carry `origin_remote`, so a caller cannot
tell. And nothing ever evicts, despite the schema comment calling these rows evictable — every
federated recall adds more.

**Two honest options.**

**(a) Finish it.** Index cached rows into FTS so they are reachable offline; keep the remote's own
`created` rather than stamping now; add `origin` to `_light_record` so borrowed results are labelled
everywhere, not only in the fused federated response; exclude them from the local browse paths
(`recent`, `timeline_window`, `search_memories`) unless asked for; add an eviction job to the
Computer bounded by age or count.

**(b) Drop it.** Delete `cache_remote_results` and the `origin_remote` rows it writes. Federated
recall becomes strictly online: remotes answer or they do not, which is already what
`sources_unreachable` reports.

**Recommend (a)**, but not strongly, and the reason to decide rather than drift is that (a) is
roughly a day and (b) is an hour. Federation without an offline fallback is a weaker product — a
laptop that just consulted a team server should still see what it read when the VPN drops. That is
worth a day. But it is worth it only if the cache is *finished*; a cache that cannot be searched is
strictly worse than none, because it costs storage, pollutes the timeline and grows without bound
in exchange for nothing.

**Whichever is chosen, the leak into local browse paths is a bug and gets fixed either way.** Under
(b) it disappears with the rows.

**Done when** cached memories are either reachable by `recall` and absent from local browse and
bounded in number, or gone.

## Step 7 — Wire outbound signing

**The problem.** `private_key` is a parameter on `RemoteClient`, `deliver_outbox` and
`recall_across`. **Nothing ever passes it.** The only place a key is loaded is `cmd_join`, which
writes it to `~/.project-memory/client_key.pem` and prints:

> The private key never left this machine. The same key enrolled elsewhere is verifiably the same
> client, which is how identity works across servers.

Nothing reads it again. Every outbound call falls back to a bearer token. Inbound verification in
`clients.authenticate` is complete and tested; the other half of the handshake is unreachable.

**The change.** Load the key where the outbound calls are constructed and thread it through:

- `outbox_job` and `cmd_compute` — for delivery.
- `McpServer` / `recall_across` — for federated recall.

Load it lazily from the default path, treat absence as "no key, use the token" rather than an error,
and do not generate one implicitly. A machine that never ran `join` has no business minting an
identity as a side effect of a background sweep.

**Test it end to end**, not by asserting a header is present: stand up a second store as a remote,
enroll the first machine's key with it, and confirm delivery succeeds with *no* bearer token
configured. That is the property — token-free federation — and it is currently unreachable.

**Done when** a promotion is delivered to a remote where the only credential is an enrolled key.

## Step 8 — Delete `related_label_query`

**The problem.** It is a parameter on the `create_memory` and `update_memory` tool schemas, on
`McpServer._call_tool`, and on both store methods. It is never read. `related_candidates` is
hardcoded `[]` in both returns. The tool description tells the agent it returns "likely related
candidates after creation."

**The change.** Remove it — from the schemas, the dispatcher, both signatures, and both return
payloads.

**Why remove rather than implement.** `recall(related_to=...)` already does this, better, on demand,
and after the agent has seen the memory land. Implementing a second, weaker path that fires
automatically on every write would add a query to the hottest write path to answer a question
nobody asked at that moment.

**What it costs.** A visible change to two tool schemas. Any caller passing it gets a schema
rejection instead of silence — which is the improvement.

**Done when** the string `related_label_query` does not appear in the package.

## Step 9 — A remote can be disabled

**The problem.** `remotes.enabled` is read in six places and written by nothing but the schema
default. There is no way to stop federating with a remote except `--remove`, which discards its URL,
token and description. The `(disabled)` branch in `cmd_remote` is unreachable.

**The change.** `remote --disable NAME` / `--enable NAME`, one `UPDATE` each.

Small, but it is the difference between "temporarily unreachable, leave it configured" and "gone",
and those are different intentions. A remote that is down for a week should not require re-typing
its token to come back.

**Done when** a disabled remote is skipped by recall and delivery, survives a restart, and can be
re-enabled without re-entering its credentials.

## Step 10 — Dead code sweep

Each of these is independently small; together they are the difference between a codebase that
describes itself and one that used to.

- **`server.py:495`** — `if hasattr(store, "flush_usage")`. No store has that method; it is a
  leftover from the file backend. Delete both lines.
- **`sqlite_store.recent()`** — only tests call it. Production uses `recall(order="recent")`. Delete
  it and move its two assertions onto the path that ships.
- **`sqlite_store.py:217`** — the comment describing `enrollment_codes` sits above `remotes`, and
  `enrollment_codes` has none. Move it back.
- **`server.py:11`** — unused `pathlib.Path` import. `http_server.py:278,608` — redundant local
  `import sqlite3`, already imported at module scope.
- **`build/`** — 394K of pre-refactor package copy, untracked and gitignored, but grep and every
  editor index read it and it answers questions with stale code. Delete it.
- **`reindex`** — has a priority entry but appears in no scheduler default, so the similarity graph
  is never rebuilt unless somebody runs `compute --kind reindex` by hand. Either add it to the
  default kinds at a low frequency, or delete the priority entry and document it as a manual
  operation. It is currently neither.

**Done when** `pyflakes` is clean on `project_memory_mcp/`, and `reindex` is either scheduled or
documented as manual.

## Step 11 — `search_memories` stops scanning the project

**The problem.** It selects every body in the project and JSON-parses each one, then filters labels
and text in Python. `limit` short-circuits only once matches accumulate, so a query that matches
nothing reads and parses the entire store.

`recall` was made fully indexed in phase 2. This sibling path was not, and it is still on the tool
surface.

**The change.** Narrow the candidate set with an index before parsing anything.

The label grammar does not reduce uniformly, and it is worth being precise about that because it
decides how much is possible. The **dict** form (`all`/`any`/`not`) is exactly three sets and pushes
into SQL directly against `labels_by_label`. The **string** form compiles through `_LabelParser`
into an arbitrary boolean predicate over nested AND/OR/NOT, which does not.

What both share is `expression.used_labels` — every label the query mentions. For an expression with
no negation, a memory can only match if it carries at least one of those labels, so that set is a
sound index-backed prefilter. With a `NOT` present it is not sound, because a memory matching
`NOT area:x` need carry no mentioned label at all.

So: push the dict form into SQL; for the string form, prefilter on `used_labels` when the expression
contains no negation and fall back to the scan when it does. Track that in `LabelExpression` as a
`has_negation` flag set during compilation — one line in the parser, and it is the difference
between a sound optimisation and a wrong one.

Text is the second half. The grammar allows substring matching that FTS does not express, so either
accept FTS semantics here (matching `recall`, and arguably more correct) or keep the Python filter
and apply it only to rows that survived the label prefilter.

**Measure before and after** on a seeded store of a few thousand memories, and record the numbers in
this document. The last time a cost was guessed here it was wrong by a wide margin — derived edges
were blamed for what `_synchronize_relationships` was doing.

**This is last on purpose.** It is a performance fix on a path that is not the hot one; `recall` is.
Do it when the correctness work is done.

## Not doing

- **No second table for cached remote memories.** One schema, one set of query paths. If step 6
  chooses (a), an indexed column is enough to separate them.
- **No role enforcement over HTTP.** The `role` column stays recorded and unenforced. The privileged
  operations are CLI commands, gated by having an account on the machine holding the database, which
  is stronger than a column. Adding a role check to the UI would imply a permission model that does
  not exist. Step 2 fixes *scope*, which does.
- **No automatic cleanup of existing `bootstrap` projects.** Step 5 stops it happening; deleting
  somebody's project row as a side effect of an upgrade is not an improvement over an empty project
  sitting there.
- **No key generation outside `join`.** Step 7 uses a key if one exists. A background job that mints
  an identity is a background job that creates a client nobody enrolled.

## Risks

- **Step 2 is the only one with a security claim attached, and claims need proof.** The mutation
  check is not optional there. An assertion that passes both before and after the fix is worse than
  no assertion, and this bug survived exactly such an assertion on the MCP path.
- **Step 6(a) is the largest item here and the least urgent.** If it slips, take the leak fix alone —
  excluding borrowed rows from local browse is a few WHERE clauses and removes the visible harm.
- **Step 4 deletes a command.** Low blast radius, since it has never worked in this form, but it is
  the one step that removes something a user could in principle be scripting.
- **Steps 6 and 7 both lean on per-replica counter behaviour that step 1 has not yet verified.** That
  ordering is deliberate. If step 1 turns up a failure, re-read those two before starting them.
