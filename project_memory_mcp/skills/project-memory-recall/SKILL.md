---
name: project-memory-recall
description: Check project memory for prior lessons before working on a task in this repository - recurring bugs, misleading symptoms, subsystem behavior, build/test/deployment quirks, or conventions that were solved before and written down. One `recall` call answers this; make it whenever a task touches this project's code, build, or tooling and you cannot already rule out that something relevant was stored. Also use on explicit cues like "we solved this before", "this happened again", "check project memory".
---

# Project Memory Recall

Consult the project's stored lessons before spending effort rediscovering one.

The cost asymmetry is the whole point: a `recall` call is one round trip, while
re-deriving a lesson that was already written down can cost an hour. You cannot know
whether the store has something relevant until you look, so when a task touches this
project's code, build, or tooling, look — and move on quickly when nothing scores well.

## When To Use

Worth a call:

- repeated or familiar-sounding bug symptoms;
- confusion about how a subsystem behaves;
- build, packaging, deployment, or test workflow problems;
- errors that may have been diagnosed before;
- project conventions that are easy to forget;
- before starting non-trivial work in an unfamiliar part of the repo;
- explicit cues: "we solved this before", "this happened again", "check project memory".

Not worth a call:

- generic programming questions with no project-specific component;
- work that touches nothing in this repository;
- a store you have already queried this session for the same thing.

## Workflow

1. **Make one `recall` call.** Pass the task, symptom, or error text as `query` — plain
   language is fine, and code identifiers match on case boundaries, so `bReplicates` is
   found by "replicated".

   ```jsonc
   {"query": "packaging fails when the editor is open", "limit": 8}
   ```

   The results come back ranked by text relevance, proximity in the memory graph, and
   label overlap, with the top few memories inlined in full. That is usually the whole
   retrieval step — no follow-up calls needed.

2. **Use the other modes when they fit.**
   - `{"related_to": "<memory-id>"}` — rank the store by how strongly it relates to a
     memory you already have. Use after a hit to pull in its neighbourhood.
   - `{}` with no query — the most central memories, as an overview of an unfamiliar store.
   - Add `label_query` to constrain a search you already know the shape of
     (`"area:auth AND kind:bug"`); omit it when you are describing a symptom.

3. **Read the scores before trusting the ranking.** Each result carries `why` with its
   `text`, `graph`, and `label` components. A hit resting almost entirely on `graph`
   score is a neighbour of something relevant, not necessarily relevant itself. Weak
   scores across the board mean the store has nothing for this task — say nothing and
   carry on.

4. **Note the status.** `stale` and `wrong` memories are returned deliberately and rank
   lower. A `wrong` memory is a warning about a path that failed before, not advice.

5. **Read further only when a result earns it.** The top results arrive in full; for
   others use `get_memory`. Reach for `list_labels`, `search_memories`, or
   `get_memory_neighborhood` only when you need an exhaustive unranked sweep of a label
   cluster — `recall` covers ordinary retrieval.

6. **Apply memory cautiously.** It is guidance, not authority. Current code, tests, logs,
   and build output override it. If present evidence contradicts a memory, say so and
   follow the evidence.

7. **Say what you used, and record it.** When memory materially changed your approach:

   ```text
   Using project memory: <id> - <one-sentence reason>
   ```

   Then call `record_memory_use` with those ids. `recall` already logs that a memory was
   *shown*; only you know whether it was *used*, and a memory that keeps being shown and
   never used is crowding out better results. Record only what genuinely changed your
   approach — recording everything returned makes the signal worthless.

   When nothing useful comes back, say nothing about memory, record nothing, and continue
   with the task.

8. **Notice rot.** If a memory turns out stale, wrong, or misleading, the
   `project-memory-remember` skill should update or retire it once the task is resolved.

## If The MCP Server Is Unavailable

Memories live in a database reached through this server. There is no file to read instead,
so say plainly that project memory is unreachable and carry on with the task.

Do not treat this as "nothing was found". An unreachable store and an empty store look
identical from the outside and mean opposite things: the first says prior lessons may exist
and you cannot see them, the second says none were recorded. Never report that no memory
exists when you could not reach it.

If the repository contains a `.project-memory/` directory, it is a leftover from before the
database. Do not read it — it is not maintained and its contents may contradict the store.
