---
name: project-memory-remember
description: Use after resolving a bug, task, build issue, debugging session, or project-specific problem to capture the reusable lesson in project memory. Invoke it on your own judgment, without waiting to be asked, whenever completed work produced durable project-specific knowledge (a non-obvious subsystem fact, a recurring failure mode, a misleading symptom, a build/test procedure, a hidden convention) or proved an existing memory stale, wrong, or superseded. Also use when the user explicitly asks to remember a lesson, update project memory, or decide whether a resolved issue should become reusable project memory.
---

# Project Memory Remember

Use this skill after a task has been resolved or mostly resolved.

The goal is to record compact, reusable, project-specific knowledge that will help future agents solve similar tasks faster.

## Invocation

This skill is self-triggering. At the end of a request that is semantically complete — a
delivered answer, a finished change, a concluded investigation, an established blocker — decide
for yourself whether the work produced durable knowledge, and act on that decision without
asking permission first.

- **Invoke it** when the work produced a non-obvious subsystem fact, a recurring failure mode, a
  misleading symptom, a build/packaging/test procedure, or a hidden convention — or when it
  proved an existing memory stale, wrong, or superseded. Write the memory, then state what was
  stored.
- **Stay silent** when the work was a one-off fix, a generic programming question, a pure code
  read, a typo, or something already documented in the repo's agent instructions or plainly
  visible in the code. Do not announce that nothing was worth remembering.

Intermediate updates, plans, clarifying questions, and tool results are not completed requests;
do not fire on those. A summary counts only when it is the requested final deliverable.

Self-triggering is not a licence to write a memory per task. The bar below still decides; the
only difference is that you apply it yourself instead of asking. If the bar is not met, make no
changes and say nothing about memory — silence is the correct output.

If the user says "don't remember this", "skip memory", or similar for the current request, do
not invoke this skill at all. They can always remove a stored memory afterwards with
`project-memory-forget`.

Memory writes go to a shared database, not to the working tree. They take effect immediately
for every agent and device using the same store, and are not part of any commit — so mention
what you stored when reporting, since it will not show up in `git status`.

## Memory Store

Memories live in a database reached through the `project-memory` MCP server. Every memory
carries a status (`active`, `stale`, `superseded`, or `wrong`) and is reached by the tools
below, never by reading files.

## What Is Worth Remembering

Add or update memory only if the lesson is:

- project-specific;
- non-obvious;
- likely to recur;
- useful for future debugging, implementation, testing, packaging, deployment, or codebase navigation;
- faster to know upfront than rediscover;
- expressible as concrete facts, rules, pitfalls, or solution patterns.

Good memory candidates include:

- subsystem architecture;
- hidden project conventions;
- repeated failure modes;
- misleading symptoms;
- non-obvious relationships between files;
- build or packaging procedures;
- debugging paths that should be avoided;
- old memories that became stale or misleading.

Do not remember:

- ordinary syntax fixes;
- generic programming knowledge;
- one-off bugs;
- typos;
- speculation;
- full transcripts;
- large command outputs;
- temporary observations;
- facts already documented clearly elsewhere;
- secrets, credentials, tokens, or personal data.

Rule of thumb: store a memory only if it would save at least 10-20 minutes later or prevent a likely wrong debugging path.

## Workflow

1. Inspect existing memories.
   - Call `recall` with the lesson you are about to store, phrased as you would write its
     `description`. Ranked results show immediately whether this ground is already covered.
   - Use `list_labels` to see the canonical labels before choosing any.
   - `search_memories` remains available for an exhaustive unranked sweep of a label cluster,
     and `get_memory` for reading a specific candidate in full.
   - If the MCP server is unreachable, stop here. Memories live in a database and there is
     no file to read instead; storing nothing is correct, and writing a memory you could not
     check for duplicates is not.

2. Decide whether the resolved task contains reusable knowledge.
   - If not, report that nothing should be remembered and explain why.
   - Also check the reverse case: did solving this task rely on a recalled memory that turned out to be wrong, stale, or misleading, or did the task's outcome make an existing memory invalid or unusable? If so, that memory must be updated (`wrong`/`stale`/`superseded`) or deleted per step 3 — do not leave a disproven memory sitting as `active`.

3. If something is worth remembering, decide whether to:
   - create a new memory;
   - edit an existing active memory;
   - mark an existing memory as `stale`, `wrong`, or `superseded`;
   - delete only if the memory is invalid junk, unsafe to store, or a pure duplicate with no historical value;
   - delete or mark `wrong` any memory that was recalled during this task and led the agent down a wrong path, or that this task's resolution has proven false or completely unusable going forward.

4. Before creating a new memory, check for duplicates with `recall`.
   - Query with the draft `description` plus the concrete symptoms you would put in `triggers`.
     Ranked text matching catches a lesson already stored under different wording, which a
     label-cluster scan misses - and near-duplicates are the main way a store degrades.
   - Read the top few results in full before deciding they are distinct.
   - Prefer editing an existing memory whenever the same triggers, subsystem, and lesson
     already exist. Two memories covering one lesson is worse than one imperfect memory:
     both then surface for the same query and neither is authoritative.
   - Create a new memory only when the lesson is genuinely separate, not merely a new
     instance of a stored pattern - in that case extend the existing memory's `triggers`.
   - **Keep these results.** The memories that ranked highest against the draft are the same
     ones most likely to deserve a `related` link, so this one call answers both questions.
     Step 7 reuses them rather than searching again.

5. Write compact valid JSON.
   Use this structure (set `scope.project` to this project's name):

```json
{
  "schema_version": 1,
  "id": "short-stable-slug",
  "status": "active",
  "description": "One-sentence summary of the reusable project knowledge.",
  "tags": [],
  "labels": [],
  "scope": {
    "project": "<project-name>",
    "area": "",
    "files": [],
    "applies_to": []
  },
  "triggers": [],
  "remembered_facts": [],
  "solution_pattern": [],
  "pitfalls": [],
  "evidence": {
    "created_from_task": "",
    "last_validated": "YYYY-MM-DD"
  },
  "relationships": {
    "related": [
      { "id": "other-memory-slug", "reason": "Why the two memories are relevant to each other." }
    ],
    "supersedes": [],
    "superseded_by": []
  }
}
```

6. Assign canonical labels.
   - Call `list_labels` for the registry.
   - Reuse existing labels whenever they reasonably describe the memory.
   - Add a new label only if the lesson is a fundamentally new, durable retrieval subclass that existing labels cannot express.
   - Do not add synonyms, one-off labels, or labels for details already covered by `description`, `triggers`, `tags`, or `scope.files`.
   - If adding a label, update the canonical registry with a concise description, or call MCP `add_label`.
   - Validation must fail if any memory uses a label not present in the registry.

7. Cross-link related memories.
   - This step runs whenever a memory is created, or whenever an existing memory's `remembered_facts`, `solution_pattern`, or `pitfalls` change substantively. Skip it for pure status changes (e.g. marking something `stale` or `wrong`).
   - **Write links on one side only.** The store mirrors them: it copies each `{id, reason}`
     onto the target memory automatically, with the identical reason string, and mirrors
     `supersedes`/`superseded_by` too. Adding the back-reference by hand is not just redundant -
     each one is another `update_memory`, and every write re-validates the whole store.
   - **For a new memory**, take the candidates from step 4 and put the links straight into the
     initial `create_memory` call. Do not call `recall` with `related_to` for a memory that does
     not exist yet - it resolves the id and will fail.
   - **For an existing memory**, call `recall` with `related_to` set to its id. That ranks the
     store by degree of relatedness: authored links first, then memories reachable through the
     graph.
   - Work down the ranking and stop when candidates stop being genuinely related. Scores are a
     prompt for judgment, not a threshold to apply mechanically. Check `why` before trusting a
     result - a candidate carried by `graph` score is a neighbour of something relevant, and for
     a brand-new memory it has no graph position at all, so text and label overlap are the only
     real signals at creation time.
   - Apply a real quality bar: link only when there is genuine subsystem, error-mode, or file overlap, not superficial topical resemblance.
   - Author one `reason` string per pair, phrased to make sense read from either end, since both
     sides get that same sentence.
   - `relationships.supersedes` / `relationships.superseded_by` stay plain memory-id arrays.

8. Keep memories granular.
   - One memory = one reusable lesson.
   - Do not merge unrelated lessons just because they came from the same task.
   - Do not create many tiny memories if one coherent memory captures the reusable pattern.

9. Keep fields concise.
   - `description`: one sentence.
   - `triggers`: concrete symptoms and phrases.
   - `remembered_facts`: atomic facts.
   - `solution_pattern`: practical steps or rules.
   - `pitfalls`: likely future mistakes.

10. Validate JSON.
    - Run `project-memory-mcp validate`.
    - There is no index to update: memory files are the only source of truth, so a written file is live immediately.
    - Ensure each edited file parses as JSON.
    - Ensure no comments or trailing commas exist.
    - Ensure `id` matches filename.
    - Ensure required fields exist.
    - Ensure every label exists in the registry (`list_labels`).
    - Ensure every `relationships.related` entry is an `{id, reason}` object, not a bare string, and that links are bidirectional.

11. Report the result to the user.

## Status Rules

Use `active` when the memory is current and should be used normally.

Use `stale` when the memory may still be useful but must be checked against current code.

Use `superseded` when a newer memory replaces it. Fill `relationships.superseded_by`.

Use `wrong` when the memory is false or caused a wrong debugging path. Keep it only if it is useful as a warning.

Prefer `superseded` or `wrong` over physical deletion unless the file is invalid junk, unsafe to store, or a pure duplicate.

## Required Final Report

Always report in this format:

```text
Memory update result:
- Created: <files or none>
- Edited: <files or none>
- Cross-linked: <memory ids linked, with one-line reason each, or none>
- Superseded/wrong: <files or none>
- Deleted: <files or none>
- Not remembered: <reason or none>
- Stored lesson: <short summary or none>
```

When nothing was worth remembering, the report depends on how the skill was invoked:

- **The user explicitly asked** to update memory: be explicit that nothing was stored, and why.

```text
Memory update result:
- Created: none
- Edited: none
- Cross-linked: none
- Superseded/wrong: none
- Deleted: none
- Not remembered: this was a one-off issue and did not reveal reusable project-specific knowledge.
- Stored lesson: none
```

- **Self-triggered** on your own judgment: say nothing at all about memory. Do not print an
  empty report — an automatic check that finds nothing should be invisible.
