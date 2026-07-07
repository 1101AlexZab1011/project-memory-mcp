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
  store: field shapes, label registry membership, filename/id agreement, relationship
  bidirectionality, and index freshness. Every mutation is transactional — validated,
  and rolled back on failure.
- **Zero runtime dependencies.** Pure Python standard library.

## Installation

```bash
pip install project-memory-mcp        # or: pipx install / uv tool install
```

Or run straight from a clone (no install needed):

```bash
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
  INDEX.json           generated search index
  active/              one JSON file per memory
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

Re-run `install-skills` after upgrading the package to refresh the copies.

## MCP tools

| Tool | Description |
| --- | --- |
| `list_labels` | Canonical labels grouped by prefix. |
| `search_memories` | Search the lightweight index by label query, status, and optional text. |
| `get_memory` | Full JSON for one memory id. |
| `get_memory_neighborhood` | Bounded relationship graph around a memory (`depth`, `max_nodes`). |
| `create_memory` | Create a memory; syncs bidirectional links, regenerates the index, validates. |
| `update_memory` | Deep-merge a patch into a memory; same sync + validation. |
| `add_label` | Register a new canonical label. |
| `delete_memory` | Delete after exact-id confirmation; removes dangling references. |

Label queries accept either structured form —
`{"all": ["area:auth"], "any": ["kind:bug", "kind:workflow"], "not": ["context:testing"]}` —
or an expression string: `area:auth AND (kind:bug OR kind:workflow) AND NOT context:testing`.

## CLI

```text
project-memory-mcp init            [--root DIR] [--force]
project-memory-mcp validate        [--root DIR] [--fix-index]
project-memory-mcp serve           [--root DIR]
project-memory-mcp install-skills  [--root DIR] [--claude] [--codex] [--dest DIR]
```

`validate` checks the whole store and exits non-zero on any problem; `--fix-index`
regenerates `INDEX.json` from the memory files (refusing if the store itself is invalid).
Use it in CI or a pre-commit hook to keep hand-edited memories honest.

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
git clone <this-repo>
cd project-memory-mcp
python -m unittest discover -s tests -v
```

No dependencies to install; tests use only the standard library.

## License

[MIT](LICENSE)
