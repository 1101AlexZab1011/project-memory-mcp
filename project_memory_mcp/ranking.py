"""Relevance ranking for memory retrieval: BM25 text scoring over memory
documents, a weighted relationship graph, and personalized PageRank over that
graph to turn "is related" into "how related".

Design notes:

- Pure standard library. The store stays plain JSON files; everything here is
  computed in memory from those files and never persisted.
- Relatedness has two tiers. *Curated* edges are the author-written
  ``relationships.related`` links and carry full weight. *Derived* edges are
  computed from label and file overlap, carry reduced weight, and exist only
  in memory - they are never written back to the store.
- Ranking is seeded, not global. Plain PageRank scores a node's importance in
  the whole graph, which surfaces the same few hubs for every query.
  Personalized PageRank restarts the walk at the query's best text matches, so
  the score answers "how close is this to what was asked" instead.
"""

from __future__ import annotations

import math
import re
from typing import Any, Iterable

# --------------------------------------------------------------------- tuning

BM25_K1 = 1.5
BM25_B = 0.75

# Field boosts, applied by repeating a field's tokens in the document.
FIELD_WEIGHTS = {
    "id": 3,
    "description": 3,
    "triggers": 2,
    "tags": 2,
    "labels": 1,
    "remembered_facts": 1,
    "solution_pattern": 1,
    "pitfalls": 1,
    "scope": 1,
}

CURATED_EDGE_WEIGHT = 1.0
DERIVED_EDGE_MAX_WEIGHT = 0.5
DERIVED_LABEL_WEIGHT = 0.7
DERIVED_FILE_WEIGHT = 0.3
DERIVED_THRESHOLD = 0.34

# Keep only a memory's strongest derived neighbours. Without this the derived
# edge count grows as N^2, which makes both the build and every PageRank
# iteration quadratic. It is also a quality bound: a memory with two hundred
# derived neighbours is not meaningfully related to any of them.
DERIVED_MAX_NEIGHBOURS = 10

# A label or file shared by most of the store says nothing about relatedness -
# the same reasoning that gives common words a near-zero IDF. Such groups are
# skipped as candidate sources. The floor keeps small stores behaving exactly
# as before, where even a broad label is still informative.
DERIVED_MAX_GROUP_FRACTION = 0.25
DERIVED_MIN_GROUP_FLOOR = 50

PAGERANK_ALPHA = 0.15
PAGERANK_MAX_ITER = 100
PAGERANK_TOLERANCE = 1e-8

SCORE_TEXT_WEIGHT = 1.0
# Measured on an 82-memory store against 8 labelled symptom->memory queries:
# text-only scored MRR 0.917, graph at 0.6 scored 0.906. For plain text lookup
# the walk should stay a tie-breaker, not a driver - BM25 over memories this
# verbose already has near-total recall, so there is little left to expand
# into. The walk earns a much higher weight when it is seeded at a memory
# (see RELATED_GRAPH_WEIGHT), which is a different question than text lookup.
SCORE_GRAPH_WEIGHT = 0.3
SCORE_LABEL_WEIGHT = 0.3
# When ranking "what relates to this memory", the walk *is* the signal.
RELATED_GRAPH_WEIGHT = 1.0

# Multiplicative confidence by lifecycle status. `wrong` memories stay
# retrievable - they are kept deliberately as warnings - but rank last.
STATUS_FACTORS = {"active": 1.0, "stale": 0.7, "superseded": 0.4, "wrong": 0.2}

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
_CAMEL_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    """Split text into search tokens.

    Code identifiers are split on case boundaries in addition to being kept
    whole, so a query for "replicated" matches a memory tagged ``bReplicates``
    and "synchronous build" matches ``bForceSynchronousInstanceBuild``. Both
    the whole identifier and its parts are emitted, so exact-identifier
    queries still score highest.
    """
    tokens: list[str] = []
    for raw in _TOKEN_RE.findall(text):
        lowered = raw.lower()
        tokens.append(lowered)
        parts = _CAMEL_RE.findall(raw)
        if len(parts) > 1:
            tokens.extend(part.lower() for part in parts if len(part) > 1)
    return tokens


def _iter_field_text(memory: dict[str, Any], field: str) -> Iterable[str]:
    if field == "scope":
        scope = memory.get("scope") or {}
        yield str(scope.get("area") or "")
        for value in scope.get("files") or []:
            yield str(value)
        for value in scope.get("applies_to") or []:
            yield str(value)
        return
    value = memory.get(field)
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                yield item


def document_tokens(memory: dict[str, Any]) -> list[str]:
    """Flatten a memory into a weighted token bag for BM25."""
    tokens: list[str] = []
    for field, weight in FIELD_WEIGHTS.items():
        field_tokens: list[str] = []
        for chunk in _iter_field_text(memory, field):
            field_tokens.extend(tokenize(chunk))
        tokens.extend(field_tokens * weight)
    return tokens


class TextIndex:
    """In-memory BM25 index over the memory store."""

    def __init__(self, documents: dict[str, list[str]]) -> None:
        self.doc_len = {doc_id: len(tokens) for doc_id, tokens in documents.items()}
        self.avg_len = (sum(self.doc_len.values()) / len(self.doc_len)) if self.doc_len else 0.0
        self.frequencies: dict[str, dict[str, int]] = {}
        self.doc_freq: dict[str, int] = {}
        for doc_id, tokens in documents.items():
            counts: dict[str, int] = {}
            for token in tokens:
                counts[token] = counts.get(token, 0) + 1
            self.frequencies[doc_id] = counts
            for token in counts:
                self.doc_freq[token] = self.doc_freq.get(token, 0) + 1

    def score(self, query: str) -> dict[str, float]:
        """Return BM25 scores for every document matching at least one query
        token. Documents with no match are omitted rather than scored zero."""
        query_tokens = tokenize(query)
        if not query_tokens or not self.doc_len:
            return {}
        total_docs = len(self.doc_len)
        average = self.avg_len or 1.0
        scores: dict[str, float] = {}
        for token in set(query_tokens):
            doc_freq = self.doc_freq.get(token)
            if not doc_freq:
                continue
            idf = math.log(1.0 + (total_docs - doc_freq + 0.5) / (doc_freq + 0.5))
            for doc_id, counts in self.frequencies.items():
                freq = counts.get(token)
                if not freq:
                    continue
                length_norm = 1.0 - BM25_B + BM25_B * (self.doc_len[doc_id] / average)
                contribution = idf * (freq * (BM25_K1 + 1.0)) / (freq + BM25_K1 * length_norm)
                scores[doc_id] = scores.get(doc_id, 0.0) + contribution
        return scores


class RankingContext:
    """Prebuilt ranking structures for a fixed snapshot of the store.

    Tokenizing every memory and building the BM25 index costs roughly ten
    times what answering a query costs, so a long-lived server should build
    this once per store revision and reuse it across calls.
    """

    def __init__(self, memories: dict[str, dict[str, Any]], include_derived: bool = True) -> None:
        self.memories = memories
        self.include_derived = include_derived
        self.text_index = TextIndex(
            {memory_id: document_tokens(memory) for memory_id, memory in memories.items()}
        )
        self.adjacency = build_adjacency(memories, include_derived=include_derived)


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def build_adjacency(
    memories: dict[str, dict[str, Any]],
    include_derived: bool = True,
) -> dict[str, dict[str, float]]:
    """Build a weighted undirected adjacency map over the memory graph.

    Curated ``relationships.related`` links get full weight. When
    ``include_derived`` is set, pairs with enough label/file overlap get an
    extra low-weight edge so that "obviously adjacent but never linked"
    memories are still reachable by the walk.
    """
    adjacency: dict[str, dict[str, float]] = {memory_id: {} for memory_id in memories}

    def connect(source: str, target: str, weight: float) -> None:
        if source == target or source not in adjacency or target not in adjacency:
            return
        adjacency[source][target] = max(adjacency[source].get(target, 0.0), weight)
        adjacency[target][source] = max(adjacency[target].get(source, 0.0), weight)

    for memory_id, memory in memories.items():
        relationships = memory.get("relationships") or {}
        for entry in relationships.get("related") or []:
            target = entry.get("id") if isinstance(entry, dict) else entry
            if isinstance(target, str):
                connect(memory_id, target, CURATED_EDGE_WEIGHT)
        for field in ("supersedes", "superseded_by"):
            for target in relationships.get(field) or []:
                if isinstance(target, str):
                    connect(memory_id, target, CURATED_EDGE_WEIGHT)

    if not include_derived:
        return adjacency

    label_sets = {
        memory_id: set(memory.get("labels") or []) for memory_id, memory in memories.items()
    }
    file_sets = {
        memory_id: set((memory.get("scope") or {}).get("files") or [])
        for memory_id, memory in memories.items()
    }

    # Only pairs sharing a label or a file can clear the threshold, so generate
    # candidates from an inverted index rather than comparing all N^2 pairs.
    groups: dict[tuple[str, str], list[str]] = {}
    for memory_id in sorted(memories):
        for label in label_sets[memory_id]:
            groups.setdefault(("label", label), []).append(memory_id)
        for path in file_sets[memory_id]:
            groups.setdefault(("file", path), []).append(memory_id)

    group_limit = max(DERIVED_MIN_GROUP_FLOOR, int(len(memories) * DERIVED_MAX_GROUP_FRACTION))
    scored: dict[str, list[tuple[float, str]]] = {}
    compared: set[tuple[str, str]] = set()
    for members in groups.values():
        if len(members) < 2 or len(members) > group_limit:
            continue
        for index, left in enumerate(members):
            for right in members[index + 1 :]:
                pair = (left, right)
                if pair in compared:
                    continue
                compared.add(pair)
                similarity = DERIVED_LABEL_WEIGHT * _jaccard(label_sets[left], label_sets[right])
                similarity += DERIVED_FILE_WEIGHT * _jaccard(file_sets[left], file_sets[right])
                if similarity >= DERIVED_THRESHOLD:
                    scored.setdefault(left, []).append((similarity, right))
                    scored.setdefault(right, []).append((similarity, left))

    # Keep each memory's strongest neighbours. connect() is symmetric, so a
    # memory can end up with more than DERIVED_MAX_NEIGHBOURS edges if others
    # chose it - that is the usual way a k-nearest-neighbour graph is made
    # undirected, and it keeps a genuinely central memory reachable.
    for memory_id, candidates in scored.items():
        candidates.sort(key=lambda item: (-item[0], item[1]))
        for similarity, other in candidates[:DERIVED_MAX_NEIGHBOURS]:
            connect(memory_id, other, DERIVED_EDGE_MAX_WEIGHT * similarity)
    return adjacency


def personalized_pagerank(
    adjacency: dict[str, dict[str, float]],
    seeds: dict[str, float] | None = None,
    alpha: float = PAGERANK_ALPHA,
    max_iter: int = PAGERANK_MAX_ITER,
    tolerance: float = PAGERANK_TOLERANCE,
) -> dict[str, float]:
    """Random walk with restart over a weighted graph.

    ``seeds`` is the restart distribution: pass the query's best text matches
    to get proximity-to-query scores, or omit it for a uniform restart, which
    degenerates to ordinary PageRank (global centrality - useful for auditing
    the store, not for answering a query).
    """
    nodes = list(adjacency)
    if not nodes:
        return {}

    if seeds:
        total = sum(weight for weight in seeds.values() if weight > 0)
        if total <= 0:
            restart = {node: 1.0 / len(nodes) for node in nodes}
        else:
            restart = {node: 0.0 for node in nodes}
            for node, weight in seeds.items():
                if node in restart and weight > 0:
                    restart[node] = weight / total
    else:
        restart = {node: 1.0 / len(nodes) for node in nodes}

    out_weight = {node: sum(neighbors.values()) for node, neighbors in adjacency.items()}
    rank = dict(restart)

    for _ in range(max_iter):
        nxt = {node: alpha * restart[node] for node in nodes}
        dangling_mass = 0.0
        for node in nodes:
            mass = rank[node]
            if mass <= 0.0:
                continue
            total_out = out_weight[node]
            if total_out <= 0.0:
                # No neighbours: return the mass to the restart distribution
                # instead of letting it leak out of the graph.
                dangling_mass += mass
                continue
            share = (1.0 - alpha) * mass / total_out
            for neighbor, weight in adjacency[node].items():
                nxt[neighbor] += share * weight
        if dangling_mass:
            spill = (1.0 - alpha) * dangling_mass
            for node in nodes:
                nxt[node] += spill * restart[node]
        delta = sum(abs(nxt[node] - rank[node]) for node in nodes)
        rank = nxt
        if delta < tolerance:
            break
    return rank


def _normalize(scores: dict[str, float]) -> dict[str, float]:
    if not scores:
        return {}
    largest = max(scores.values())
    if largest <= 0:
        return {key: 0.0 for key in scores}
    return {key: value / largest for key, value in scores.items()}


def rank_memories(
    memories: dict[str, dict[str, Any]],
    query: str = "",
    query_labels: Iterable[str] | None = None,
    related_to: str | None = None,
    seed_count: int = 5,
    include_derived: bool = True,
    graph_weight: float | None = None,
    context: "RankingContext | None" = None,
) -> list[dict[str, Any]]:
    """Rank memories against a free-text query, a set of labels, and/or a
    memory to find relatives of.

    ``related_to`` seeds the walk at one memory instead of at text matches,
    which answers "what should be read alongside this" with a degree of
    relatedness rather than a yes/no link. It combines with ``query``: seeding
    at both biases the walk toward the part of that memory's neighbourhood
    which also matches the text.

    Returns entries sorted by descending score, each carrying its component
    sub-scores so callers (and humans) can see *why* something ranked where it
    did.
    """
    if not memories:
        return []
    if graph_weight is None:
        graph_weight = RELATED_GRAPH_WEIGHT if related_to else SCORE_GRAPH_WEIGHT
    if context is None or context.include_derived != include_derived:
        context = RankingContext(memories, include_derived=include_derived)

    text_scores = _normalize(context.text_index.score(query)) if query else {}

    wanted_labels = set(query_labels or [])
    label_scores: dict[str, float] = {}
    if wanted_labels:
        for memory_id, memory in memories.items():
            overlap = len(wanted_labels & set(memory.get("labels") or []))
            if overlap:
                label_scores[memory_id] = overlap / len(wanted_labels)

    seeds: dict[str, float] = {}
    for memory_id, score in sorted(text_scores.items(), key=lambda item: -item[1])[:seed_count]:
        seeds[memory_id] = score
    for memory_id, score in label_scores.items():
        seeds[memory_id] = seeds.get(memory_id, 0.0) + score
    if related_to and related_to in memories:
        # Weight the anchor at least as heavily as everything else combined so
        # the walk stays centred on its neighbourhood.
        seeds[related_to] = max(1.0, sum(seeds.values()))

    graph_scores = _normalize(personalized_pagerank(context.adjacency, seeds or None))

    ranked: list[dict[str, Any]] = []
    for memory_id, memory in memories.items():
        if memory_id == related_to:
            continue  # the caller already has the memory they anchored on
        text = text_scores.get(memory_id, 0.0)
        graph = graph_scores.get(memory_id, 0.0)
        label = label_scores.get(memory_id, 0.0)
        base = SCORE_TEXT_WEIGHT * text + graph_weight * graph + SCORE_LABEL_WEIGHT * label
        factor = STATUS_FACTORS.get(memory.get("status", "active"), 1.0)
        ranked.append(
            {
                "id": memory_id,
                "score": round(base * factor, 6),
                "text_score": round(text, 6),
                "graph_score": round(graph, 6),
                "label_score": round(label, 6),
                "status_factor": factor,
            }
        )
    ranked.sort(key=lambda entry: (-entry["score"], entry["id"]))
    return ranked
