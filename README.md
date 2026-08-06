# project-memory-mcp

File-based, git-friendly **project memory for coding agents** — a JSON memory store that
lives inside your repository, served to agents over the
[Model Context Protocol](https://modelcontextprotocol.io), with matching agent skills
for disciplined recall and curation.

Coding agents (Claude Code, Codex, and others) forget everything between sessions.
This tool gives each repository a small, reviewable knowledge base of hard-won,
project-specific lessons — recurring bugs, misleading symptoms, hidden conventions,
build quirks — so future sessions don't re-derive them from scratch.

## Design

- **Plain JSON files in your repo.** One file per memory under `.project-memory/active/`.
  No database, no embeddings, no external service. Memories diff, merge, and get code-reviewed
  like any other file, and they travel with the repository.
- **Label graph instead of vector search.** Memories carry canonical `prefix:kebab-case`
  labels from a registry you control. Agents retrieve by label cluster
  (`area:auth AND kind:bug`), then use `description`/`triggers` for cheap relevance checks —
  deterministic and inspectable.
- **Typed relationships.** Memories cross-link with `related` (with a required reason),
  `supersedes`, and `superseded_by`. Links are enforced to be bidirectional, and a
  neighborhood query walks the graph with bounded depth.
- **Lifecycle statuses, not deletion.** `active` / `stale` / `superseded` / `wrong` —
  disproven memories become warnings instead of silently disappearing.
- **Strict validation.** A JSON Schema plus a built-in validator that checks the whole
  store: field shapes, label registry membership, filename/id agreement, and relationship
  bidirectionality. Every mutation is transactional — validated, and rolled back on failure.
- **Zero runtime dependencies.** Pure Python standard library.

## Installation

Not yet on PyPI — install straight from GitHub:

```bash
pip install git+https://github.com/1101AlexZab1011/project-memory-mcp
# or: pipx install git+https://github.com/1101AlexZab1011/project-memory-mcp
# or: uv tool install git+https://github.com/1101AlexZab1011/project-memory-mcp
```

Or clone and run without installing:

```bash
git clone https://github.com/1101AlexZab1011/project-memory-mcp
cd project-memory-mcp
python -m project_memory_mcp --help
```

Requires Python 3.10+.

## Quick start

**1. Initialize a store in your project:**

```bash
cd /path/to/your/project
project-memory-mcp init
```

This scaffolds:

```text
.project-memory/
  README.md            store rules for humans and agents
  labels.json          canonical label registry (starter kind:/context: labels)
  memory.schema.json   JSON Schema for memory files
  active/              one JSON file per memory
  .gitignore           keeps usage.json local (written for you)
```

Commit the whole folder.

**2. Register the MCP server with your agent.**

Claude Code — add to your project's `.mcp.json`:

```json
{
  "mcpServers": {
    "project-memory": {
      "type": "stdio",
      "command": "project-memory-mcp",
      "args": ["serve"]
    }
  }
}
```

Codex — add to `~/.codex/config.toml`:

```toml
[mcp_servers.project-memory]
command = "project-memory-mcp"
args = ["serve"]
```

The server finds the store by walking up from its working directory to the nearest
`.project-memory/`; pass `--root /path/to/project` to pin it explicitly.

### Sharing one store across devices

A SQLite-backed store can be served over HTTP so every device on a network reaches the same
memory. Import an existing file store first, then serve it:

```bash
project-memory-mcp migrate --from ./.project-memory --project my-project --database ~/memory.db
PROJECT_MEMORY_TOKEN=$(openssl rand -hex 24)   project-memory-mcp serve --http --database ~/memory.db --bind 192.168.1.50 --port 8765
```

Each project's `.mcp.json` then points at it:

```json
{
  "mcpServers": {
    "project-memory": {
      "type": "http",
      "url": "http://192.168.1.50:8765/mcp?project=my-project",
      "headers": { "Authorization": "Bearer ${PROJECT_MEMORY_TOKEN}" }
    }
  }
}
```

Keep the token in an environment variable rather than the file: `.mcp.json` is usually
committed, and a secret in git history is the hardest kind to remove. If the variable is not
set where the client starts, the server says so specifically instead of reporting a bad token.

`--bind` is required and has no default. Use `127.0.0.1` for this machine only, or one
interface's address — a VPN adapter such as Radmin or Tailscale works and extends the store
beyond the local network. `0.0.0.0` publishes the store on *every* network the host is
attached to, which is a decision rather than a convenience.

The database file must stay on the host's local disk. Never place it on a network share:
SQLite's locking is documented to corrupt there, and the HTTP layer exists precisely so the
file does not have to travel.

Security is deliberately narrow — one shared static token, checked in constant time, and no
TLS. That suits a trusted LAN or an encrypted overlay. It is not enough for a hostile network,
where the token would travel in the clear.

Unknown project ids return 404 with the list of known projects, rather than quietly serving an
empty store.

**3. (Optional but recommended) Install the agent skills:**

```bash
project-memory-mcp install-skills --claude   # -> .claude/skills/  (Claude Code)
project-memory-mcp install-skills --codex    # -> .agents/skills/  (Codex)
project-memory-mcp install-skills --dest some/other/skills/dir
```

Three skills teach the agent *when and how* to use the store well:

| Skill | Purpose |
| --- | --- |
| `project-memory-recall` | Retrieve only relevant lessons before/during a task, cheaply (labels first, full files last). |
| `project-memory-remember` | After a task, decide what is durable enough to store, deduplicate, cross-link, and validate. |
| `project-memory-forget` | Safely delete a memory and clean up every reference to it. |

`project-memory-remember` is **self-triggering**: at the end of a completed request the agent
decides for itself whether the work produced a durable lesson and writes it without asking. A
store only stays useful if it gets written to, and an agent that must ask permission every time
mostly ends up not asking. The quality bar is unchanged — one-off fixes, generic programming
questions, and pure code reads are still not memories — and when nothing clears the bar the
agent stays silent rather than reporting an empty result. Tell it "don't remember this" to
suppress a single request, or use `project-memory-forget` to remove anything it stored.

Re-run `install-skills` after upgrading the package to refresh the copies.

## MCP tools

| Tool | Description |
| --- | --- |
| `recall` | **Ranked retrieval in one call.** Scores every memory by text relevance, graph proximity, and label overlap; returns the best matches with the top few inlined in full. |
| `list_labels` | Canonical labels grouped by prefix. |
| `search_memories` | Filter memories by label query, status, and optional substring. Unranked; prefer `recall`. |
| `record_memory_use` | Report which recalled memories actually informed the work. |
| `get_memory` | Full JSON for one memory id. |
| `get_memory_neighborhood` | Bounded relationship graph around a memory (`depth`, `max_nodes`). |
| `create_memory` | Create a memory; syncs bidirectional links, validates the store. |
| `update_memory` | Deep-merge a patch into a memory; same sync + validation. |
| `add_label` | Register a new canonical label. |
| `delete_memory` | Delete after exact-id confirmation; removes dangling references. |

Label queries accept either structured form —
`{"all": ["area:auth"], "any": ["kind:bug", "kind:workflow"], "not": ["context:testing"]}` —
or an expression string: `area:auth AND (kind:bug OR kind:workflow) AND NOT context:testing`.

### Ranked recall

`recall` collapses the usual `search_memories` → several `get_memory` sequence into a single
call, and orders the result instead of returning a flat cluster. Three signals combine:

- **Text** — BM25 over every field, weighted by importance. Code identifiers are split on case
  boundaries as well as kept whole, so a query for `replicated` matches a memory tagged
  `bReplicates`, and an exact identifier still scores highest.
- **Graph** — personalized PageRank over the relationship graph, restarted at the query's best
  matches. Authored `related` links carry full weight; extra low-weight edges are derived from
  label and file overlap so near-neighbours that were never explicitly linked stay reachable.
  These derived edges are computed in memory and never written to the store.
- **Labels** — overlap with an explicit label filter.

Lifecycle status scales the final score, so `stale` and `wrong` memories still surface — they
are kept deliberately as warnings — but rank below current ones.

Three ways to call it:

```jsonc
{"query": "packaging fails when the editor is open"}  // symptom lookup
{"related_to": "cache-invalidation-race"}             // what to read alongside this
{}                                                    // most central memories: orient me
{"order": "recent", "limit": 10}                      // what has been learned lately
{"order": "recent", "limit": 10, "offset": 10}        // page back through history
```

`order: "recent"` skips ranking entirely and returns newest first, which makes it the
cheapest way into the store — recency is an ordering, not a relevance judgment, so there is
nothing to score. `offset` pages back through history. Filters still apply, so
`{"order": "recent", "label_query": "area:auth"}` is "what have we learned about auth
lately". Memories are stamped with `evidence.created` on write; ones predating that field
fall back to `last_validated`.

`related_to` anchors the walk at one memory, turning "is related" into a *degree* of
relatedness: authored links rank first, then memories reachable through the graph. With no
query and no anchor the restart is uniform, which is ordinary PageRank over the store.

### Usage signal

Ranking itself is a pure function of the store and the query — the same query always returns
the same ordering, and retrieval never rewrites a memory. But that leaves the store with no
idea which memories are ever actually *useful*, which matters because an agent that decides
for itself what to remember tends to over-capture rather than under-capture.

So two counters are kept in `.project-memory/usage.json`, deliberately outside the memory
files:

- **surfaced** — incremented by `recall` for every memory it returns. Automatic.
- **applied** — incremented by `record_memory_use`, which the agent calls for the memories that
  genuinely changed what it did. It cannot be inferred; only the caller knows.

The gap between them is the signal. Surfaced often and applied never means a memory is
crowding every result set without earning its place — different from being wrong or stale, and
invisible to any other measure. `usage.json` is git-ignored by `init`: it records what one
machine retrieved, which is noise in everyone else's diff, and it is disposable — deleting it
costs history, never correctness.

Ranking is pure standard library and holds its BM25 index and adjacency for as long as no
memory file changes, so repeat calls in a live server skip the rebuild entirely.

## CLI

```text
project-memory-mcp init            [--root DIR] [--force]
project-memory-mcp validate        [--root DIR]
project-memory-mcp serve           [--root DIR]
project-memory-mcp install-skills  [--root DIR] [--claude] [--codex] [--dest DIR]
```

`validate` checks the whole store and exits non-zero on any problem. Use it in CI or a
pre-commit hook to keep hand-edited memories honest.

There is no generated index. Memory files are the only source of truth, parsed on demand
and cached in memory until one of them changes, so a hand-written memory file is live
immediately with no catalogue step. (Stores created before 0.3.0 have an `INDEX.json`;
nothing reads it any more and it is safe to delete.)

## Memory format

```json
{
  "schema_version": 1,
  "id": "cache-invalidation-race",
  "status": "active",
  "description": "Session cache invalidation races the auth refresh; symptoms look like random logouts.",
  "tags": ["cache", "auth"],
  "labels": ["area:auth", "kind:bug", "context:runtime"],
  "scope": {
    "project": "my-project",
    "area": "auth",
    "files": ["src/auth/session.ts"],
    "applies_to": ["session refresh flow"]
  },
  "triggers": ["random logouts", "session expired immediately after login"],
  "remembered_facts": [
    "The cache TTL and the refresh token TTL are configured in two different places."
  ],
  "solution_pattern": [
    "Invalidate the session cache inside the refresh transaction, not after it."
  ],
  "pitfalls": [
    "Reproducing locally needs two concurrent tabs; a single tab never hits the race."
  ],
  "evidence": {
    "created_from_task": "Debugging intermittent logout reports",
    "last_validated": "2026-07-07"
  },
  "relationships": {
    "related": [
      { "id": "token-refresh-clock-skew", "reason": "Both affect the session refresh flow." }
    ],
    "supersedes": [],
    "superseded_by": []
  }
}
```

Statuses: `active` (use normally), `stale` (verify against current code),
`superseded` (replaced — see `superseded_by`), `wrong` (kept as a warning).

Label conventions (starter registry ships `kind:` and `context:` labels; add your own):

- `kind:` — type of lesson: `kind:bug`, `kind:workflow`, `kind:architecture`, `kind:convention`
- `context:` — situation: `context:build`, `context:runtime`, `context:testing`, `context:tooling`, `context:deployment`
- `area:` — *your* project's subsystems: `area:auth`, `area:renderer`, …
- `signal:` — recurring concrete symptoms: `signal:port-conflict`, `signal:file-lock`, …

## What belongs in the store

Store lessons that are project-specific, non-obvious, likely to recur, and cheaper to
know upfront than rediscover. Do **not** store generic programming knowledge, one-off
fixes, transcripts, speculation — or secrets, credentials, and personal data (the store
is plain text committed to your repository).

## Development

```bash
git clone https://github.com/1101AlexZab1011/project-memory-mcp
cd project-memory-mcp
python -m unittest discover -s tests -v
```

No dependencies to install; tests use only the standard library.

## License

[MIT](LICENSE)
