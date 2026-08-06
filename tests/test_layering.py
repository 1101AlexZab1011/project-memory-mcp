"""The store must not be able to reach the network.

A store method that opens a socket is a store method that can block on somebody
else's machine, and in the HTTP server it does that while holding the project
lock - so one slow remote stalls every request for that project, reads included.
That was a real bug in `promote`, fixed by the outbox.

A rule written in a document gets violated. This one fails the build.

The check walks imports rather than trusting the top of the file, because the
easy way to reintroduce the problem is a function-local `from . import
federation` - which is invisible to anything that only reads module headers.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "project_memory_mcp"

#: Reaching any of these means the module can do network I/O.
NETWORK = {"urllib", "http", "socket", "ssl", "ftplib", "smtplib", "telnetlib", "asyncio"}

#: Modules that are supposed to talk to other machines. Everything else is
#: checked for what it can reach *through* its imports, so these terminate the
#: walk rather than being walked into.
ALLOWED_TO_REACH_THE_NETWORK = {"federation", "http_server", "server", "cli", "identity",
                                "clients", "backup"}


def imports_of(module: str) -> tuple[set[str], set[str]]:
    """Every import in a module, at any nesting depth: (stdlib-ish, in-package)."""
    tree = ast.parse((PACKAGE / f"{module}.py").read_text(encoding="utf-8"))
    external: set[str] = set()
    internal: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                external.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # from . import x   /   from .x import y
                if node.module:
                    internal.add(node.module.split(".")[0])
                else:
                    internal.update(alias.name for alias in node.names)
            elif node.module:
                external.add(node.module.split(".")[0])
    return external, internal


def reachable_from(start: str) -> set[str]:
    """Every non-package module reachable from `start` through in-package imports."""
    seen: set[str] = set()
    found: set[str] = set()
    queue = [start]
    while queue:
        module = queue.pop()
        if module in seen or not (PACKAGE / f"{module}.py").exists():
            continue
        seen.add(module)
        external, internal = imports_of(module)
        found |= external
        for name in internal:
            if name not in ALLOWED_TO_REACH_THE_NETWORK:
                queue.append(name)
    return found


class LayeringTests(unittest.TestCase):
    def test_the_store_cannot_reach_the_network(self):
        leaked = reachable_from("sqlite_store") & NETWORK
        self.assertEqual(
            set(), leaked,
            f"sqlite_store can reach {sorted(leaked)}. Storage must not do I/O it cannot "
            "bound - move the call into federation.py and have the Computer run it.")

    def test_the_store_does_not_import_federation_at_all(self):
        # Stronger than the reachability check and easier to read in a failure:
        # if the store needs federation, the dependency is pointing the wrong way.
        _, internal = imports_of("sqlite_store")
        self.assertNotIn(
            "federation", internal,
            "sqlite_store imports federation. The dependency runs the other way: federation "
            "reads and writes through the store, and the store knows nothing about remotes.")

    def test_the_audit_cannot_reach_the_network(self):
        # The audit runs unattended on a timer. A network call in there would be
        # a background thread blocking on a remote with nobody watching.
        leaked = reachable_from("audit") & NETWORK
        self.assertEqual(set(), leaked, f"audit can reach {sorted(leaked)}")

    def test_federation_is_still_the_module_that_owns_the_network(self):
        # The negative tests above are only meaningful if something still does
        # it - otherwise deleting the feature would make them pass.
        external, _ = imports_of("federation")
        self.assertIn("urllib", external)


if __name__ == "__main__":
    unittest.main()
