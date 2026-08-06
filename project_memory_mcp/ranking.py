"""Tokenization and graph scoring.

What remains here after the file backend was removed: the tokenizer used at
write time to build the FTS index, and personalized PageRank, run over the
bounded subgraph that ``SqliteMemoryStore`` expands around a query's best
matches.

BM25 scoring, the in-memory text index, adjacency construction, and whole-store
ranking all lived here to serve the file backend. FTS5 does the first two on
disk, and the store builds its own bounded adjacency, so they are gone rather
than kept as a second implementation of the same thing.

Ranking is seeded, not global. Plain PageRank scores a node's importance in the
whole graph, which surfaces the same few hubs for every query. Personalized
PageRank restarts the walk at the query's best text matches, so the score
answers "how close is this to what was asked" instead.
"""

from __future__ import annotations

import re

PAGERANK_ALPHA = 0.15
PAGERANK_MAX_ITER = 100
PAGERANK_TOLERANCE = 1e-8

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
