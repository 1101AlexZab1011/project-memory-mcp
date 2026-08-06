"""Catch a credential at the moment a memory would leave the machine that made it.

The skill text asks agents not to record secrets. That instruction is advisory,
and this project has direct evidence that advisory instructions in these skills
do not reliably fire: `record_memory_use` has never been called once. So the
rule is enforced where it actually matters rather than only stated.

**Promotion, not writing.** A secret sitting in a local nursery memory is on the
machine that already holds the secret. Promotion is the first moment it crosses
to somebody who does not, which makes it the only place worth paying for a scan.

**Three tiers, in descending order of how much they can be trusted.**

Structural patterns (PEM blocks, JWTs, connection strings with a password) and
vendor prefixes (``sk_live_``, ``AKIA``, ``ghp_``) are self-identifying and cost
almost nothing in false positives - vendors chose those prefixes so that
scanners could find them. They carry the overwhelming majority of real leaks.

Entropy is the last tier and the weakest, so it is gated. Measured naively it
does not work at all: English prose runs about 4.3 bits per character, *higher*
than a real GitHub token, because Shannon over a short string mostly reports
length and character variety rather than unpredictability. It only separates
after tokenizing, restricting to a credential-shaped alphabet, and requiring a
name like ``key`` or ``token`` nearby. Even then it exists for one narrow case:
an in-house credential format that no prefix rule knows about.

**Hex is deliberately not a tier of its own.** A 40-character git SHA scores
3.95 bits per character against the usual 3.0 threshold, and so does every MD5
and every dash-stripped UUID. In a store whose memories are largely *about
commits*, an ungated hex rule is a commit-SHA detector. It runs only when a
credential-ish name sits beside it.

**What is ours is allowlisted explicitly.** Every promoted memory carries an
Ed25519 fingerprint in ``author_key``, and public keys are high-entropy
precisely so they can be published. That they survive a naive scan at all is an
accident of the ``:`` in ``SHA256:`` not being a base64 character - an accident
that disappears the moment anyone adds ``:`` to the delimiter set. The allowlist
here is load-bearing, not a refinement.

Findings are always **masked**. A report that echoed the matched value would
copy the secret into an error message, a job log and an HTTP response - three
new places it did not use to be.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterator

#: Shortest run of credential-shaped characters worth measuring. Below this the
#: entropy estimate is dominated by length and says nothing.
MIN_TOKEN_LENGTH = 20

#: Bits per character. Base64 secrets sit near 4.7-5.1; prose tokens that
#: survive the alphabet test sit well below.
BASE64_BITS = 4.5

#: Hex needs a lower bar (16 symbols cap it near 4.0) and more length, because
#: everything hex-shaped in this corpus is a hash.
HEX_BITS = 3.0
HEX_MIN_LENGTH = 32

BASE64_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/_-")
HEX_CHARS = set("0123456789abcdefABCDEF")

#: Fields holding key material by design. Skipped outright rather than
#: allowlisted by pattern, so a change to the fingerprint format cannot quietly
#: turn every promotion into a finding.
SKIP_FIELDS = frozenset({"author_key", "fingerprint", "public_key", "uuid"})

#: Values that are high-entropy in order to be published. Removed before
#: tokenizing rather than filtered afterwards, so no later rule can see them.
ALLOWED = re.compile(r"\bSHA256:[A-Za-z0-9+/=_-]{20,}")

#: `_` counts as a boundary, because `STRIPE_KEY` is the shape this is for and
#: `\bkey\b` would not match it. The lookahead keeps `monkey` and `keyboard` out.
NAME_NEARBY = re.compile(
    r"(?:^|[^A-Za-z0-9])(api[-_]?key|secret|token|password|passwd|pwd|credentials?|auth|key)s?"
    r"(?![A-Za-z0-9])", re.IGNORECASE)

STRUCTURAL: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("private key", re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"),
     "a PEM private key block"),
    ("json web token", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}"),
     "a JWT, which grants access until it expires"),
    ("connection string", re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s:/@]+:[^\s/@]{3,}@"),
     "a connection string with the password inline"),
)

VENDOR: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai key", re.compile(r"\bsk-[A-Za-z0-9]{20,}")),
    ("stripe key", re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("github token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("aws access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("google api key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}")),
    ("gitlab token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20}")),
    ("npm token", re.compile(r"\bnpm_[A-Za-z0-9]{36}")),
    ("sendgrid key", re.compile(r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}")),
    ("hugging face token", re.compile(r"\bhf_[A-Za-z0-9]{30,}")),
)

#: Splitting on `=` and `:` too, so an assignment yields the value on its own
#: rather than a token whose entropy is diluted by the variable name.
SPLIT = re.compile(r"[\s\"'`,;=:()\[\]{}<>|]+")

#: Cap the report. Twenty findings and two hundred say the same thing, and the
#: message is read by an agent that has to act on it.
MAX_FINDINGS = 20


@dataclass(frozen=True)
class Finding:
    """One reason a memory was held, with the value never spelled out."""

    rule: str
    field: str
    excerpt: str
    why: str

    def describe(self) -> str:
        return f"{self.field}: {self.rule} - {self.why} ({self.excerpt})"


def shannon(text: str) -> float:
    """Bits per character over the string's own distribution."""
    total = len(text)
    if not total:
        return 0.0
    return -sum((n / total) * math.log2(n / total) for n in Counter(text).values())


def _mask(token: str) -> str:
    """Enough to recognise which string was meant, not enough to use it."""
    head = token[:6]
    return f"{head}... {len(token)} chars"


def _tokens(text: str) -> Iterator[str]:
    for token in SPLIT.split(text):
        token = token.strip(".,!?")
        if len(token) >= MIN_TOKEN_LENGTH:
            yield token


def scan_text(text: str, field: str = "") -> list[Finding]:
    """Findings for one string. Order matters: cheap and certain rules first."""
    if not text:
        return []
    found: list[Finding] = []
    seen: set[tuple[str, str]] = set()

    def add(rule: str, token: str, why: str) -> None:
        excerpt = _mask(token)
        if (rule, excerpt) in seen:
            return
        seen.add((rule, excerpt))
        found.append(Finding(rule=rule, field=field, excerpt=excerpt, why=why))

    safe = ALLOWED.sub(" ", text)

    for rule, pattern, why in STRUCTURAL:
        for match in pattern.finditer(safe):
            add(rule, match.group(0), why)
    for rule, pattern in VENDOR:
        for match in pattern.finditer(safe):
            add(rule, match.group(0), "a credential in a format its vendor publishes")

    # Entropy runs only beside a name that suggests a credential. Without that
    # condition it flags file paths and commit hashes and nothing else.
    if not NAME_NEARBY.search(safe):
        return found
    for token in _tokens(safe):
        chars = set(token)
        if chars <= HEX_CHARS and len(token) >= HEX_MIN_LENGTH and shannon(token) >= HEX_BITS:
            add("high-entropy hex", token,
                f"{len(token)} hex characters next to a credential name")
        elif chars <= BASE64_CHARS and shannon(token) >= BASE64_BITS:
            add("high-entropy string", token,
                f"{shannon(token):.1f} bits/char over a base64 alphabet, "
                "next to a credential name")
    return found


def _walk(node: Any, path: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in SKIP_FIELDS:
                continue
            yield from _walk(value, f"{path}.{key}" if path else str(key))
    elif isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            yield from _walk(value, f"{path}[{index}]")
    elif isinstance(node, str):
        yield path, node


def scan(memory: Any) -> list[Finding]:
    """Every finding in a whole memory body, capped."""
    found: list[Finding] = []
    for field, text in _walk(memory):
        found.extend(scan_text(text, field))
        if len(found) >= MAX_FINDINGS:
            return found[:MAX_FINDINGS]
    return found


def explain(memory_id: str, findings: list[Finding]) -> str:
    """The message an agent gets instead of a publication.

    It says what to do, because a refusal an agent cannot act on becomes a
    refusal an agent routes around.
    """
    lines = [
        f"'{memory_id}' looks like it contains a secret, so it was not published.",
        "",
        *(f"  - {f.describe()}" for f in findings),
        "",
        "Record where a secret lives and what shape it takes - the file, the variable "
        "name, who rotates it - never the value. Edit the memory and promote again.",
        "If this is a false positive (a public key, a commit hash, a sample), promote "
        "with allow_secrets=true.",
    ]
    return "\n".join(lines)
