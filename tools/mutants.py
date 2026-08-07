"""Break each property the suite claims to protect, and see if it notices.

A test that passes with its own bug reinstated is decorative. That has happened
three times in this session already, each time because the test checked a
calculation instead of the behaviour built on it. Guessing which others are
hollow is exactly the reasoning that produced them, so this asks the suite
instead.

Each entry is a surgical edit that should make at least one test fail. Anything
reported as SURVIVED is a property nothing actually guards.
"""
import atexit
import pathlib
import signal
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

#: A mutant that hangs is worse than one that fails: it tells you nothing and it
#: blocks whatever is driving the run. Anything past this is reported as HUNG.
RUN_TIMEOUT_SECONDS = 300

#: Written beside a file while it is mutated. If this process is killed - which
#: is exactly what happened once - the next run finds the backup and puts the
#: original back, rather than leaving a sabotaged source in the working tree for
#: somebody to commit.
BACKUP_SUFFIX = ".mutation-backup"


def restore_orphans() -> None:
    for backup in sorted((ROOT / "project_memory_mcp").glob("*" + BACKUP_SUFFIX)):
        target = backup.with_suffix("")
        target.write_text(backup.read_text(encoding="utf-8"), encoding="utf-8")
        backup.unlink()
        print(f"  restored {target.name} left mutated by an earlier run", flush=True)


class Mutation:
    """Applies one edit, and puts the file back however this process ends."""

    def __init__(self, path: pathlib.Path, old: str, new: str) -> None:
        self.path, self.good = path, path.read_text(encoding="utf-8")
        self.backup = path.with_suffix(path.suffix + BACKUP_SUFFIX)
        self.mutated = self.good.replace(old, new, 1)

    def __enter__(self) -> "Mutation":
        self.backup.write_text(self.good, encoding="utf-8")
        self.path.write_text(self.mutated, encoding="utf-8")
        atexit.register(self.__exit__, None, None, None)
        return self

    def __exit__(self, *_exc) -> bool:
        if self.backup.exists():
            self.path.write_text(self.good, encoding="utf-8")
            self.backup.unlink()
        return False


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
    """True if the suite noticed. None if the mutant made it hang."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:randomly"],
            cwd=ROOT, capture_output=True, text=True, timeout=RUN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return None
    return result.returncode != 0


def main() -> int:
    # SIGTERM does not run atexit handlers on its own, and being killed is
    # exactly the case that left a sabotaged file in the working tree once.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    restore_orphans()

    survived, killed, hung, skipped = [], [], [], []
    for name, filename, old, new in MUTANTS:
        path = ROOT / "project_memory_mcp" / filename
        if old not in path.read_text(encoding="utf-8"):
            skipped.append(name)
            print(f"  SKIP     {name} (pattern not in {filename})", flush=True)
            continue
        with Mutation(path, old, new):
            detected = run_suite()
        if detected is None:
            hung.append(name)
            label = "HUNG    "
        elif detected:
            killed.append(name)
            label = "caught  "
        else:
            survived.append(name)
            label = "SURVIVED"
        print(f"  {label} {name}"
              f"{'' if detected else '   <-- nothing guards this' if detected is False else ''}",
              flush=True)

    print("")
    print(f"{len(killed)} caught, {len(survived)} survived, "
          f"{len(hung)} hung, {len(skipped)} skipped")
    for name in survived:
        print(f"  unguarded: {name}")
    for name in hung:
        print(f"  hung (a test that hangs cannot tell you anything): {name}")
    for name in skipped:
        print(f"  not tested: {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
