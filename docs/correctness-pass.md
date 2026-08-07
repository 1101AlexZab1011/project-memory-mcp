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

### Outcome ✔

```text
GET  /api/memories?project=theirs : 403 this client has no access to project 'theirs'
GET  /api/projects                : 200 ["mine"]
POST /api/delete   on theirs      : 403, and the memory is still there
```

The scoping rule is now written once, in `_Handler._may_access`. Both transports ask it and differ
only in how they refuse: `/mcp` sends its own 403, while the UI raises `_Forbidden` so the check can
live inside `_store_for` — the one call every project-scoped route already makes. A route added
later cannot forget it. Forgetting it once, in a place that looked like it had no security
responsibility, is the whole shape of this bug.

`_Forbidden` is separate from `StoreError` so "not yours" answers 403 rather than the 404 or 400 the
UI maps storage failures to. A scoped client is authenticated, not anonymous, so confirming the
project exists tells it nothing it could not get from `/mcp` — which has always answered 403 by
name.

`token_is_valid` is gone, replaced by `client_for_token`, which returns *who* holds the token. It is
deliberately not merged with `authenticate` even though the lookup is identical: that one raises to
distinguish "unknown" from "revoked" because an MCP client needs to know whether to re-enroll, while
this one returns None either way, because an unauthenticated browser must not learn that a token it
presented was real but withdrawn.

Seven mutants, all caught, including three new ones: scoping not enforced, a session that forgets
which client opened it, and a UI login that accepts a revoked client's token. The pre-existing
"project scoping is not enforced" mutant had to be repointed — it targeted text inside
`_require_project`, which only `/mcp` ever called, so it passed throughout the bug's life. That is
the same failure as step 1: an assertion aimed at the path that was already correct.

**Two process notes, both of which produced a wrong answer before they produced a right one.**

The first verification of the fix showed the bug still open. The repro script lived in a temp
directory, so Python put *that* directory first on `sys.path` and fell through to a released 0.7.0
in site-packages — same `__version__`, different code. Added to step 10.

`test_an_expired_session_is_rejected` broke, because it reaches into `_Sessions._issued` and wrote a
bare float where the entry is now `(expiry, client)`. It failed at the transport instead of the
assertion — and a test that raises on unpacking "catches" every mutant, so the `expired sessions
stay valid` result from the first run was worthless. Re-checked after fixing it, and it now fails on
the assertion. White-box tests have to be re-read whenever the state they reach into changes shape;
they do not announce it.

**348 → 355 tests.**

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

### Outcome ✔

All three paths drain:

```text
delete a queued memory  -> {'deleted': ..., 'cancelled_promotions': 1}, outbox empty
a pre-existing orphan   -> dropped; the live promotion behind it survives for its retry
merged away while queued-> dropped; publishing a lesson just folded into another is nobody's ask
a sleeping server       -> still queued, untouched
```

`delete_memory` now removes the memory's outbox rows and reports `cancelled_promotions` when there
were any — whoever deleted it may not have known it was waiting to go somewhere. `deliver_outbox`
drops rows whose memory is gone *or archived*, which covers databases written before this and any
future path that removes a memory without going through `delete_memory`.

Five mutants, all caught, including the counterweight: making the drop condition `if True` — throwing
away every undeliverable promotion — fails, because a promotion to a sleeping server must still be
retried. Without that one, "drop things that cannot be delivered" would have been satisfied by
destroying the outbox's entire reason for existing.

**Deviation from this plan, deliberate.** The step above says to surface failures "in `/api/audit` or
a sibling endpoint". I did not add an endpoint. Checking first: **`/api/audit` has no UI consumer**,
`Computer.last_error` is written and never read, and `BackupRunner.last_error` likewise. A fourth
thing nothing reads would have repeated the exact mistake this pass exists to correct.

`Computer.run_one` now writes failures to **stderr** — every occurrence, not the first only, because
a job still failing on the hundredth sweep is still broken and a message that stops arriving reads
as a problem that went away. Successes stay quiet: a sweep across every project every hour would
drown the log it is meant to be readable in. stderr is the one channel this server has that somebody
is already watching.

The three unread surfaces go to step 10 as a group. A jobs panel in the UI is a real feature and
belongs in its own plan, not smuggled into a bug fix.

**One related case found and deliberately left.** A remote that is *removed* while promotions to it
are queued leaves rows that `deliver_outbox` skips forever — `by_name` only contains enabled remotes,
so they are neither delivered nor dropped nor reported. Same family, different cause, and it is
step 9 that owns the remote lifecycle. Noted there.

**355 → 361 tests.**

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

### Outcome ✔ — deleted

The question the step asked was answerable from the repository, and the answer was already written
down twice:

- **README:** a `.project-memory` directory "was the pre-0.4.0 layout and nothing reads it any more."
- **`project-memory-recall/SKILL.md`:** "it is a leftover from before the database. Do not read it —
  it is not maintained and its contents may contradict the store."

So the project instructs agents not to open the format, while shipping an importer for it that had
raised `TypeError` on every call since schema v2 — untested, and recommended by the server's own
startup banner to anyone with an empty database. Nobody can have depended on a command nobody could
run. Gone: `cmd_migrate`, its parser, and `migrate_from_files`.

The argument does not rest on "no such directory exists anywhere" — that is not something this
thread can establish, and one project on this machine is deliberately out of scope. It rests on the
importer having been unusable for many releases either way. If a file store does surface during a
future migration, write the importer against that directory as a fixture. That beats keeping an
untested one written against a guess.

Three documents were pointing at it and now do not: the startup banner (`init`), the README's import
recipe, and `server-architecture.md`'s Migration section, which now records why it went.

The test asserts the property rather than the word: it reads the banner line out of `http_server.py`,
extracts the command in backticks, and requires argparse to accept it. Naming `init` in a test would
have passed just as well while the banner said something else.

**Two structural things fixed on the way through, both the same family as step 1.**

`test_cli.py` had `DestructiveConfirmationTests(CliTests)` — inheriting the parent's tests along with
its fixture, so seven tests ran twice and my new class would have made it three times. Extracted a
`CliCase` fixture with no tests of its own. **The file reported 26 tests and had 12.**

And the cause behind step 1's bug: three test files had definitions sitting *below* their
`if __name__ == "__main__"` block — seven definitions in total. Everyone reads that as the end of the
file, which is exactly how a class gets appended below one that already exists and shadows it. Moved
to the end in all three, and `test_layering.py` now fails if anything is defined after the marker.
Step 1's guard catches the effect; this one catches the cause. Verified by moving a block back.

**361 → 358 tests, and the drop is the point:** −4 duplicate CLI runs, +3 for the removal, +1 guard.

**One honest note.** During this step's first full run,
`test_concurrent_requests_do_not_corrupt_the_connection` failed once — the known flake from the
architecture cleanup, historically about one run in four. It did not reproduce: three runs in
isolation and four full-suite runs since, all clean. Because it did not recur I could not read which
of the two kinds the instrumentation classified it as, so this is a sighting, not a diagnosis. It is
unrelated to anything in this step, which touches the CLI, three documents and test structure.

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

### Outcome ✔

```text
before enroll: ['real-project']
after  enroll: ['real-project']
```

**The step was two bugs, not one, and the second was worse.** Looking for a way to create the tables
without a project turned up the reason there wasn't one: the schema script only ever ran as a side
effect of opening a store. So a database with *no projects at all* had no `clients` table, and
`authenticate` died on `OperationalError: no such table: clients` — which `_identify` does not catch,
so a bad token got a **500 where it meant 401**. Reachable by design rather than by accident: `serve`
starts happily on an empty database and prints "(none - create one with `init` first)".

Both are the same gap — no way to say "make this database ready" without also saying which project.
`ensure_schema()` is that way. `cmd_enroll` calls it instead of opening a phantom store, and
`run_http_server` calls it before serving. `SqliteMemoryStore.__init__` shares the same `_prepare`,
so there is one description of what a prepared database is rather than two that can drift.

Empty database now:

```text
bad token   -> 401  (was 500)
good token  -> 404 Unknown project: demo   (which is what create=False is for)
```

Two mutants, both caught: preparing a database that creates no tables, and enrolling that goes back
to opening a "bootstrap" store.

**358 → 362 tests.**

**Note for whoever deploys this.** Existing databases still contain the `bootstrap` row — this stops
it being created, it does not clean up after the versions that did. Deleting somebody's project row
as a side effect of an upgrade is not an improvement over an empty project sitting in a list, so it
is a manual `DELETE FROM projects WHERE id='bootstrap'` on any database that has one, and only after
confirming it holds no memories. There is no release-notes file to put this in, which is why it is
here.

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

### Outcome ✔ — finished, in a different shape than planned

**This step was ten problems, not five, and the five new ones changed the design.** Walking the
paths rather than reading the code turned up:

| | |
|---|---|
| `create_memory` refused a slug that was free locally | its existence check saw borrowed rows. Consult a remote, lose the name |
| `_uuid_for` / `get_memory` resolved ambiguously | no `ORDER BY`, no origin filter — which row won was unspecified |
| `validate_store` reported errors that were not ours | a cached memory's links point at things never cached |
| the audit swept borrowed rows | against a schema comment promising it never would |
| **a backup round-trip dropped `origin_remote`** | silently turning somebody else's lesson into one this machine owned, and could publish onward |

All ten had one cause: borrowed copies sat in `memories` behind a nullable column that every query
had to remember, and almost none did. **So the plan's "Not doing: no second table" was wrong**, and
wrong for a reason it could not have known when written — it assumed one table meant one set of
query paths, and one table had in fact produced ten divergent ones, five of them incorrect.

Cached copies now live in `cached_memories` (schema v8, with a migration that moves existing rows,
recovers the remote's real `created` from the stored body, and drops the usage counters recorded
against them). Every one of those five became structurally impossible rather than filtered. The
three places that *do* have to reach both tables — `text_candidates`, `_body_by_uuid`, `get_memory` —
are the whole cost, and their failure mode is inverted: forget one and a cached memory does not show
up, which is visible and harmless.

The original five, all fixed as specified: cached rows are indexed at write so `recall` finds them
offline; they keep the remote's own `created`; results carry `origin`; they are gone from `recent`,
`search_memories` and the timeline; and an `evict` job bounds them by age (30 days) and count (500).
Surfacing a borrowed copy no longer records usage — counters feed the audit, and retention is not
this machine's decision to make about another server's memory.

**Ten mutants, all caught, including two counterweights** — eviction that takes the whole cache, and
a `get_memory` that prefers a borrowed copy — because "drop what is not ours" is satisfiable by
dropping everything.

**Two tests were asserting the arrangement rather than the property.**
`test_a_cached_slug_does_not_collide_with_a_local_one` checked that two rows coexisted in `memories`
with different `origin_remote` values. It passed for the whole life of the bug because it created
the local memory *first*. Reverse the order — consult a remote, then write your own lesson, which is
the ordinary sequence — and `create_memory` raised. Both orders are now tested, by behaviour.

**One mutant survived the first pass**, and the reason is worth keeping. Deleting the FTS cleanup
from `evict_cache` left `test_an_evicted_copy_stops_being_searchable` green, because `recall` reads
each candidate's body and skips what it cannot find — a dangling index row produces no visible wrong
answer. The harm is real but invisible: the index grows forever, and candidate slots are capped, so
enough stale rows push real memories out of range. Now asserted directly on the index, with a note
saying why the behavioural check cannot see it.

**362 → 380 tests.**

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

**Also here, found during step 3.** A remote *removed* while promotions to it are queued leaves
outbox rows that can never drain: `deliver_outbox` builds `by_name` from enabled remotes only, so
those rows are skipped every run — never delivered, never dropped, never reported. Same family as
the orphan step 3 fixed, different cause, and it belongs with the remote lifecycle rather than with
memory deletion.

Decide which it is, because they are genuinely different: a **disabled** remote's queue should wait,
that is what disabling means. A **removed** remote's queue should be dropped with a reason, or
removal should refuse while work is pending. Dropping is the simpler answer and matches what removal
already implies about the URL and token.

**Done when** a disabled remote is skipped by recall and delivery, survives a restart, can be
re-enabled without re-entering its credentials, and removing one leaves nothing undeliverable behind.

## Step 10 — Dead code sweep

Each of these is independently small; together they are the difference between a codebase that
describes itself and one that used to.

- **`server.py:495`** — `if hasattr(store, "flush_usage")`. No store has that method; it is a
  leftover from the file backend. Delete both lines.
- **Three surfaces nothing reads**, found while looking for somewhere to report a failing job in
  step 3: `/api/audit` is served and never fetched by `app.js`; `Computer.last_error` and
  `BackupRunner.last_error` are both assigned and never read. Either give them a reader or delete
  them. A maintenance panel in the UI would give all three one at once and is the better answer —
  but it is a feature, so it belongs in its own plan rather than being folded in here. What is *not*
  acceptable is leaving them as they are, because each one reads like the system is being watched.
- **`sqlite_store.recent()`** — only tests call it. Production uses `recall(order="recent")`. Delete
  it and move its two assertions onto the path that ships.
- **`sqlite_store.py:217`** — the comment describing `enrollment_codes` sits above `remotes`, and
  `enrollment_codes` has none. Move it back.
- **`server.py:11`** — unused `pathlib.Path` import. `http_server.py:278,608` — redundant local
  `import sqlite3`, already imported at module scope.
- **`build/`** — 394K of pre-refactor package copy, untracked and gitignored, but grep and every
  editor index read it and it answers questions with stale code. Delete it.
- **The non-editable install in site-packages** — a released 0.7.0 copy shadows the working tree for
  any script whose own directory is not the repo, because Python puts the *script's* directory on
  `sys.path` first and the repo is never on it at all. It reports the same `__version__` as the
  working tree, so nothing distinguishes them at a glance. This cost real time during step 2: the
  first verification of the fix appeared to show the bug still open, because it was testing the
  released build. Reinstall editable (`pip install -e .`) so there is one copy of this package on
  the machine.
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

- ~~**No second table for cached remote memories.** One schema, one set of query paths. If step 6
  chooses (a), an indexed column is enough to separate them.~~ **Overturned by step 6.** The
  assumption was that one table means one set of query paths. It did not: one table produced ten
  divergent paths, five of them wrong, including a backup round-trip that laundered other servers'
  memories into this machine's own. An indexed column separates rows only for queries that remember
  to ask. See the outcome above.
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
