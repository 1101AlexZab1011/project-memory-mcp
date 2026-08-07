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

#: A word is a run of letters and digits in *any* script. `[A-Za-z0-9]+` was
#: ASCII-only, which did not merely degrade for other languages - it erased
#: them. Cyrillic and CJK text produced no tokens at all, so a memory written in
#: Russian or Japanese was indexed as empty and could never be recalled by any
#: word it contained; recall simply returned nothing, with no error to notice.
#: Accented Latin was worse in its way: `café naïve` split into `caf`, `na`,
#: `ve`, quietly indexing fragments and matching things it should not.
#:
#: `[^\W_]` is "word character except underscore", and in Python 3 `\w` is
#: Unicode-aware by default, so this covers every script while still breaking on
#: the underscores and punctuation that separate identifiers.
_TOKEN_RE = re.compile(r"[^\W_]+")

#: Case-boundary splitting for identifiers, which is where the ASCII assumption
#: is still true and deliberately so. Code identifiers are overwhelmingly ASCII,
#: and this only ever *adds* extra tokens - the whole word is emitted either way
#: by `tokenize`, so a script this does not split is still fully searchable.
#:
#: Concretely: `КэшШейдера` yields one token rather than two, and a Japanese
#: compound yields one. Both are findable; neither is decomposed. That is a
#: smaller loss than it looks, and much smaller than the one this replaced,
#: where they yielded nothing at all.
_CAMEL_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")

#: Scripts written without spaces between words. Widening the character class
#: above is enough for Cyrillic or Greek, which separate their words the way
#: English does - but a Japanese sentence is one unbroken run, so it indexes as
#: a single enormous term that only an identical sentence could ever match.
#:
#: Character bigrams are the usual answer where a real segmenter is not
#: available, and they are symmetric here: `tokenize` builds both the index and
#: the query, so both sides get the same bigrams and meet in the middle. A
#: nine-character query shares eight bigrams with the sentence containing it.
_CJK_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿･-ﾟ]")


def tokenize(text: str) -> list[str]:
    """Split text into search tokens.

    Code identifiers are split on case boundaries in addition to being kept
    whole, so a query for "replicated" matches a memory tagged ``bReplicates``
    and "synchronous build" matches ``bForceSynchronousInstanceBuild``. Both
    the whole identifier and its parts are emitted, so exact-identifier
    queries still score highest.

    Every script, not only Latin. This is the write-time half of retrieval - it
    builds the FTS index and it parses the query - so a script it cannot see is
    a language the store cannot remember anything in.
    """
    tokens: list[str] = []
    for raw in _TOKEN_RE.findall(text):
        lowered = raw.lower()
        tokens.append(lowered)
        if not raw.isascii():
            if _CJK_RE.search(raw) and len(lowered) > 1:
                # No spaces to split on, so the "word" above is a whole clause.
                # Bigrams give the index and the query something in common.
                tokens.extend(lowered[i:i + 2] for i in range(len(lowered) - 1))
            # Case-splitting is an identifier heuristic and its pattern is
            # ASCII. Run it on `naïve` and it finds the Latin runs either side
            # of the accent - `na`, `ve` - and indexes those as if they were
            # words. The whole token is already emitted above, so skipping this
            # costs nothing and stops an accented word polluting the index with
            # fragments of itself.
            continue
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
