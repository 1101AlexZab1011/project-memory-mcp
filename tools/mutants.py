"""Break each property the suite claims to protect, and see if it notices.

A test that passes with its own bug reinstated is decorative. That has happened
three times in this session already, each time because the test checked a
calculation instead of the behaviour built on it. Guessing which others are
hollow is exactly the reasoning that produced them, so this asks the suite
instead.

Each entry is a surgical edit that should make at least one test fail. Anything
reported as SURVIVED is a property nothing actually guards.
"""
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

MUTANTS = [
    ("secret scan skipped at promotion", "federation.py",
     "    if not allow_secrets:\n        findings = secret_scan.scan(body)",
     "    if False:\n        findings = secret_scan.scan(body)"),

    ("secret scan skipped at delivery", "federation.py",
     "            findings = secret_scan.scan(body)\n            if findings:",
     "            findings = []\n            if findings:"),

    ("tier gate not enforced on publication", "federation.py",
     "    if state[\"tier\"] < MIN_PROMOTION_TIER and not force:",
     "    if False:"),

    ("private memories may be published", "federation.py",
     "    if state[\"visibility\"] != \"public\":\n        raise StoreError(",
     "    if False:\n        raise StoreError("),

    ("borrowed copies may be republished", "federation.py",
     "    if state[\"borrowed\"]:",
     "    if False:"),

    ("re-queueing a promotion duplicates it", "federation.py",
     "        if existing:\n            store.connection.execute(\n"
     "                \"UPDATE outbox SET queued_at=?, last_error=NULL WHERE id=?\",",
     "        if False:\n            store.connection.execute(\n"
     "                \"UPDATE outbox SET queued_at=?, last_error=NULL WHERE id=?\","),

    ("browsing the UI records usage", "http_server.py",
     "                                             record=False)[\"memories\"]",
     "                                             record=True)[\"memories\"]"),

    ("deletion of superseded is on by default", "audit.py",
     "    delete_superseded: bool = False",
     "    delete_superseded: bool = True"),

    ("archived memories get re-judged", "audit.py",
     "    if memory[\"archived_at\"]:\n        return make(VERDICT_HOLD, \"already archived\")",
     "    if False:\n        return make(VERDICT_HOLD, \"already archived\")"),

    ("message bodies lose the untrusted label", "messages.py",
     "        \"untrusted_body\": row[\"body\"],",
     "        \"body\": row[\"body\"],"),

    ("our own fingerprints stop being allowlisted", "secret_scan.py",
     "    safe = ALLOWED.sub(\" \", text)",
     "    safe = text"),

    ("key-material fields are scanned again", "secret_scan.py",
     "SKIP_FIELDS = frozenset({\"author_key\", \"fingerprint\", \"public_key\", \"uuid\"})",
     "SKIP_FIELDS = frozenset()"),

    ("entropy runs without a credential name nearby", "secret_scan.py",
     "    if not NAME_NEARBY.search(safe):\n        return found",
     "    if False:\n        return found"),

    ("federated recall is routed under the lock", "http_server.py",
     "    if params.get(\"name\") != \"recall\":\n        return False",
     "    if True:\n        return False"),

    ("the audit's per-run action cap is ignored", "audit.py",
     "        return max(1, min(self.max_actions_per_run, int(store_size * self.max_action_fraction)))",
     "        return 10**9"),

    ("the audit's fraction cap is ignored", "audit.py",
     "int(store_size * self.max_action_fraction)",
     "10**9"),

    ("clock skew on signatures is not checked", "identity.py",
     "MAX_CLOCK_SKEW_SECONDS = 300",
     "MAX_CLOCK_SKEW_SECONDS = 10**9"),
]


def run_suite():
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:randomly"],
        cwd=ROOT, capture_output=True, text=True, timeout=900)
    return result.returncode != 0, (result.stdout or "").strip().splitlines()[-1:]


survived, killed, skipped = [], [], []
for name, filename, old, new in MUTANTS:
    path = ROOT / "project_memory_mcp" / filename
    good = path.read_text(encoding="utf-8")
    if old not in good:
        skipped.append(name)
        print(f"  SKIP     {name} (pattern not found in {filename})", flush=True)
        continue
    path.write_text(good.replace(old, new, 1), encoding="utf-8")
    try:
        detected, tail = run_suite()
    finally:
        path.write_text(good, encoding="utf-8")
    if detected:
        killed.append(name)
        print(f"  caught   {name}", flush=True)
    else:
        survived.append(name)
        print(f"  SURVIVED {name}   <-- nothing guards this", flush=True)

print()
print(f"{len(killed)} caught, {len(survived)} survived, {len(skipped)} skipped")
for name in survived:
    print(f"  unguarded: {name}")
