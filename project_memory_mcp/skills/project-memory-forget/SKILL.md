---
name: project-memory-forget
description: Use when the user asks to forget, delete, remove, retract, or invalidate a stored project memory (or memories matching a description), whether given as an exact id or a free-text description of the lesson, subsystem, or symptom.
---

# Project Memory Forget

Use this skill to permanently remove one or more memories from `.project-memory/` and to clean up every reference to those memories elsewhere in the store, so nothing is left dangling.

Prefer the `project-memory` MCP server when available:

- Use `search_memories` to resolve the target.
- Use `delete_memory` with `confirm_exact_id` for deletion and cleanup.
- Fall back to the manual workflow below only when MCP tools are unavailable.

This is a destructive operation on the memory store. Treat it with the same care as any other destructive action: be certain which memory the user means before deleting, and never guess on an ambiguous query.

Prefer `project-memory-remember`'s `stale` / `wrong` / `superseded` status path when a memory should be kept for historical or warning value. Use this skill only when the user explicitly wants a memory gone entirely.

## Memory Store

```text
.project-memory/
  README.md
  labels.json
  memory.schema.json
  INDEX.json
  active/
```

## Step 1 — Resolve the query to specific memory ids

The user's query may be:

- an exact memory id (e.g. "forget mcp-server-breaks-build");
- a description of the lesson, subsystem, or symptom (e.g. "forget the thing about the old caching workaround");
- broad enough to plausibly match several memories.

Do the cheap scan first:

- Read `.project-memory/INDEX.json` and compare the query against `id`, `description`, `tags`, and `triggers` for every entry.
- Only open full memory JSON files for candidates that remain ambiguous after the index-level scan.

Resolution rules:

- If exactly one memory clearly matches, proceed to Step 2.
- If multiple memories could plausibly match, or the query is vague, list the candidates (`id` + one-line `description`) and ask the user to confirm which ones to remove before deleting anything.
- If nothing matches, say so and stop. Do not delete an unrelated memory just to have done something.

## Step 2 — Find every reference to the memory before deleting it

Before deleting a memory file, find every other memory that points at it, since removing it must not leave a dangling reference:

- Search for the memory's `id` string across `.project-memory/active/*.json`. `INDEX.json` does not carry relationship data, so it cannot be used for this check — search the full files.
- A hit can appear in `relationships.related[].id`, `relationships.supersedes[]`, or `relationships.superseded_by[]`.
- Record every file that references the id being deleted and which field it appears in.

## Step 3 — Delete and clean up

For each memory confirmed for removal:

1. Delete its JSON file from `active/`.
2. For every other memory file found in Step 2 that references the deleted id:
   - Remove the matching `{id, reason}` entry from `relationships.related`.
   - Remove the id from `relationships.supersedes` / `relationships.superseded_by` if present.
3. If the deleted memory itself had `relationships.supersedes` or `relationships.superseded_by`, decide whether removing it breaks a chain that needs resolving:
   - If the deleted memory superseded another memory that is still marked `superseded`, ask the user (or use judgment and state the assumption made) whether that older memory should return to `active` now that its replacement is gone, or stay `superseded` with the dangling `superseded_by` reference simply cleared.
   - Never silently leave a `superseded_by` or `supersedes` entry pointing at an id that no longer exists.
4. If deleting multiple memories that were related only to each other, no cross-file cleanup is needed beyond removing the deleted files themselves.

## Step 4 — Regenerate the index and validate

- Run `project-memory-mcp validate --fix-index` to regenerate `INDEX.json` cleanly from the remaining files — safer than hand-editing the index after a deletion.
- Run `project-memory-mcp validate` (without `--fix-index`) to confirm the store is valid afterward.

## Step 5 — Report

Always report in this format:

```text
Memory forget result:
- Deleted: <ids and file paths, or none>
- Cleaned references in: <files edited to remove relationships, or none>
- Chain adjustments: <any status changes made to unblock a supersedes/superseded_by chain, or none>
- Not deleted: <candidates considered but excluded, and why, or none>
```

If no memory matched the query:

```text
Memory forget result:
- Deleted: none
- Cleaned references in: none
- Chain adjustments: none
- Not deleted: no memory in .project-memory/ matched "<query>".
```
