"""Structural rules about the code, enforced rather than written down.

Two of them, both learned from a defect rather than chosen up front.

**The store must not be able to reach the network.** A store method that opens a
socket is a store method that can block on somebody else's machine, and in the
HTTP server it does that while holding the project lock - so one slow remote
stalls every request for that project, reads included. That was a real bug in
`promote`, fixed by the outbox. The check walks imports rather than trusting the
top of the file, because the easy way to reintroduce the problem is a
function-local `from . import federation` - which is invisible to anything that
only reads module headers.

**A module must not define the same top-level name twice.** Python rebinds
silently, so the second definition wins and the first is discarded before pytest
ever sees it. `test_lifecycle_schema.py` defined `ReplicaCounterTests` twice and
three tests had never run in any suite, on any commit, while reporting green.

A rule written in a document gets violated. These fail the build.
"""

from __future__ import annotations

import ast
import textwrap
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


TESTS = Path(__file__).resolve().parent


def redefined_top_level_names(path: Path) -> list[str]:
    """Names bound more than once at module level, in definition order.

    Only classes and functions. A module-level constant reassigned on purpose is
    ordinary; a `def` or `class` written twice is always a mistake, because the
    first one becomes unreachable the moment the second is read.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    seen: set[str] = set()
    duplicates: list[str] = []
    for node in tree.body:
        if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name in seen and node.name not in duplicates:
            duplicates.append(node.name)
        seen.add(node.name)
    return duplicates


class ShadowingTests(unittest.TestCase):
    """A redefined class or function silently discards the first one.

    In a test file that means tests which never run while the suite reports
    green - which is the worst failure a test suite has, because it is
    indistinguishable from success.
    """

    def test_no_module_defines_the_same_name_twice(self):
        for path in sorted(list(TESTS.glob("test_*.py")) + list(PACKAGE.glob("*.py"))):
            with self.subTest(module=path.name):
                self.assertEqual(
                    [], redefined_top_level_names(path),
                    f"{path.name} defines these twice at module level. Python keeps only the "
                    "second, so the first is dead code - and if it is a TestCase, its tests "
                    "never run.")

    def test_nothing_is_defined_after_a_files_main_block(self):
        """`if __name__ == "__main__"` reads as the end of the file. Make it be.

        This is the *cause* the check above only catches the effect of. A class
        appended below that block looks appended to the end of the module, so
        nobody re-reads what is already up there - which is how one file came to
        define `ReplicaCounterTests` twice and lose three tests. Three test
        files had definitions sitting below the marker; seven of them.
        """
        for path in sorted(list(TESTS.glob("test_*.py")) + list(PACKAGE.glob("*.py"))):
            lines = path.read_text(encoding="utf-8").splitlines()
            marker = [i for i, line in enumerate(lines) if line.startswith("if __name__")]
            if not marker:
                continue
            stranded = [line.split("(")[0] for line in lines[marker[0]:]
                        if line.startswith(("class ", "def "))]
            with self.subTest(module=path.name):
                self.assertEqual(
                    [], stranded,
                    f"{path.name} defines these below its `if __name__` block, which every "
                    "reader treats as the end of the file. Move the block to the end.")


def arguments_the_dispatcher_reads() -> set[str]:
    """Every `args[...]` and `args.get(...)` name in McpServer._call_tool."""
    import inspect

    from project_memory_mcp.server import McpServer

    source = textwrap.dedent(inspect.getsource(McpServer._call_tool))
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
                and node.value.id == "args" and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)):
            names.add(node.slice.value)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get" and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "args" and node.args
                and isinstance(node.args[0], ast.Constant)):
            names.add(node.args[0].value)
    return names


class ToolSurfaceTests(unittest.TestCase):
    """The tool schemas are a promise to an agent. They have to be true.

    `related_label_query` was declared on `create_memory` and `update_memory`,
    accepted by the dispatcher, threaded into both store methods - and read
    nowhere. Both returned a hardcoded `related_candidates: []` while the
    description promised "likely related candidates after creation".

    Nothing tested the schemas at all, which is why it lasted. An agent cannot
    tell a parameter that does nothing from one that does; it just writes the
    call it was told to write and gets a plausible empty answer back.
    """

    def test_every_declared_argument_is_read_by_the_dispatcher(self):
        from project_memory_mcp.server import TOOLS

        read = arguments_the_dispatcher_reads()
        unread = sorted(f"{tool['name']}.{prop}"
                        for tool in TOOLS
                        for prop in tool["inputSchema"]["properties"]
                        if prop not in read)
        self.assertEqual(
            [], unread,
            "these tool arguments are declared to agents and never read. Either use them or "
            "delete them - an argument that does nothing is worse than a missing one, because "
            "the caller believes it worked.")

    def test_every_tool_refuses_arguments_it_did_not_declare(self):
        # What makes deleting one an honest change rather than a silent one: a
        # caller still passing it gets told, instead of having it ignored.
        from project_memory_mcp.server import TOOLS

        for tool in TOOLS:
            with self.subTest(tool=tool["name"]):
                self.assertFalse(tool["inputSchema"]["additionalProperties"])

    def test_every_tool_has_a_description(self):
        from project_memory_mcp.server import TOOLS

        for tool in TOOLS:
            with self.subTest(tool=tool["name"]):
                self.assertTrue((tool.get("description") or "").strip())


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
