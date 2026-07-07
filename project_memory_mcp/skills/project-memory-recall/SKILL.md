---
name: project-memory-recall
description: Use before or during a task when a similar project-specific bug, workflow, subsystem issue, debugging pattern, build problem, or implementation convention may have been solved before and stored in .project-memory.
---

# Project Memory Recall

Use this skill to consult compact project-specific memories stored in `.project-memory/`.

The goal is to retrieve only relevant prior lessons that may speed up the current task. Do not load the whole memory store unless it is tiny or there is no better option.

## Memory Store

Memory files are JSON files under:

```text
.project-memory/active/
```

A lightweight index may exist at:

```text
.project-memory/INDEX.json
```

A canonical label registry may exist at:

```text
.project-memory/labels.json
```

Each memory has structured `labels` for cheap cluster retrieval, plus short `description` and `triggers` fields for relevance checks inside that cluster.

Prefer the `project-memory` MCP server when available. Use manual JSON/index inspection only when MCP tools are unavailable.

## When To Use

Use when the current task appears similar to a previous project-specific issue, such as:

- repeated bug symptoms;
- familiar subsystem confusion;
- build, packaging, deployment, or test workflow issues;
- errors that may have been diagnosed before;
- project conventions that are easy to forget;
- user phrases like "we solved this before", "this happened again", "recall memory", or "check project memory".

Do not use for generic programming questions unless the task clearly depends on project-specific knowledge.

## Workflow

1. Use MCP first when available.
   - Call `list_labels` to inspect canonical labels when the needed cluster is unclear.
   - Call `search_memories` with `label_query` using `all` / `any` / `not` or a string expression with `AND`, `OR`, `NOT`.
   - Use broad `OR`/`any` queries for vague symptoms and precise `AND`/`all` queries for specific subsystem/context matches.
   - Only call `get_memory` for selected lightweight results.
   - When a selected memory is useful but may have related context, call `get_memory_neighborhood` with a bounded `depth` and `max_nodes`.

2. If MCP is unavailable, locate the memory store.
   - Prefer `.project-memory/INDEX.json` if it exists.
   - Otherwise inspect `.project-memory/active/*.json`.

3. First pass: inspect only lightweight fields.
   - `id`
   - `status`
   - `description`
   - `labels`
   - `tags`
   - `scope`
   - `triggers`

4. Ignore memories with these statuses unless needed for history:
   - `wrong`
   - `superseded`

5. Select only directly relevant memories.
   Relevance requires overlap with at least one of:
   - same subsystem;
   - same workflow;
   - same error symptom;
   - same files or modules;
   - same build/runtime context;
   - same project convention;
   - same misleading assumption.

6. Read full content only for selected memories.
   Use:
   - `remembered_facts`
   - `solution_pattern`
   - `pitfalls`
   - `evidence`
   - `relationships`

7. Apply memory cautiously.
   - Treat memory as guidance, not authority.
   - Current code, tests, logs, and build output override memory.
   - If current evidence contradicts memory, mention the contradiction and continue using current evidence.

8. When memory materially affects the task, briefly state which memory was used and what it contributed.

9. If a memory appears stale, wrong, misleading, or incomplete, remember that after the task is resolved the `project-memory-remember` skill may need to update it.

## Output Behavior

If useful memories are found, say briefly:

```text
Using project memory: <id> - <one-sentence reason>
```

If no useful memory is found, do not make a long report. Continue with the task normally.
