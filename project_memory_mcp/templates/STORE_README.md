# Project Memories

This folder stores compact project-specific memories for coding agents.

The purpose is to help future agent sessions recall reusable lessons from previous
debugging or implementation work without loading long transcripts or wasting context.

Managed by the `project-memory-mcp` tool (store scaffolding, validation, and MCP server).

## Structure

```text
.project-memory/
  README.md            this file
  labels.json          canonical label registry
  memory.schema.json   JSON Schema for memory files
  INDEX.json           generated search index (not the source of truth)
  active/              one JSON file per memory
```

## Rules

- One JSON file per reusable lesson.
- Store only durable project-specific knowledge.
- Do not store generic programming facts, one-off typo fixes, speculation, or transcripts.
- Do not store secrets, credentials, tokens, or personal data — memory files are plain
  text committed to the repository.
- Keep each memory small.
- Use `labels` for structured cluster retrieval.
- Use `description`, `tags`, and `triggers` for cheap relevance checks inside a selected cluster.
- Current code, tests, logs, and build output override memory.
- Prefer marking outdated memories `stale`/`wrong`/`superseded` over deleting them.
- Run `project-memory-mcp validate` after editing memory files by hand.
- Run `project-memory-mcp validate --fix-index` to regenerate `INDEX.json`.

## Labels

`labels.json` is the canonical label registry. Memory labels use `prefix:kebab-case`
and must exist in that registry.

Recommended prefixes:

- `kind:` — what type of lesson this is (bug pattern, workflow, architecture, convention).
- `context:` — the situation it applies to (build, runtime, testing, tooling, deployment).
- `area:` — your project's subsystems. Add these per project, e.g. `area:auth`,
  `area:renderer`, `area:billing`.
- `signal:` — recurring concrete symptoms, e.g. `signal:port-conflict`, `signal:file-lock`.

Reuse existing labels whenever possible. Add a new label only when a memory represents
a durable retrieval class that existing labels cannot express.

Use the `project-memory` MCP server for recall and mutation when available:

- `list_labels`
- `search_memories`
- `get_memory`
- `get_memory_neighborhood`
- `create_memory`
- `update_memory`
- `add_label`
- `delete_memory`

## Status Values

- `active`: current and usable.
- `stale`: maybe useful, but verify against current code.
- `superseded`: replaced by a newer memory.
- `wrong`: false or misleading; kept only as a warning.

All memories live under `active/` regardless of status; the `status` field inside the
file is what matters.
