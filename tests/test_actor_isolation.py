"""Who the caller is must not depend on a lock taken for something else.

One store object serves every request thread, and `store.actor` is the only
per-request state on it. It used to be a plain attribute, correct only because
the project lock happened to span the whole of handle(). Step 3 let one call run
outside that lock, and the invariant went with it: a request could read the
identity another thread had left behind, and be told the unread-message count
belonging to a different client.

These tests pin both halves - that threads cannot see each other's identity, and
that a request always leaves the field clean for whoever gets the thread next.
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from project_memory_mcp import clients, identity, messages
from project_memory_mcp.server import McpServer
from project_memory_mcp.sqlite_store import SqliteMemoryStore

CACHE = "Session cache invalidation races the auth refresh."


def memory(memory_id, description):
    return {
        "schema_version": 1, "id": memory_id, "status": "active", "description": description,
        "tags": [], "labels": ["area:x"],
        "scope": {"project": "p", "area": "a", "files": [], "applies_to": []},
        "triggers": ["cache"], "remembered_facts": [description],
        "solution_pattern": [], "pitfalls": [],
        "evidence": {"created_from_task": "t", "last_validated": "2026-01-01"},
        "relationships": {"related": [], "supersedes": [], "superseded_by": []},
    }


class ActorIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = SqliteMemoryStore(self.root / "m.db", "demo")
        self.addCleanup(self.store.close)
        self.store.add_label("area:x", "x")
        self.store.create_memory(memory("cache-race", CACHE), visibility="public")
        self.alice = self.enroll("alice-desktop", "a.pem")
        self.bob = self.enroll("bob-laptop", "b.pem")

        # Three questions for Bob. Only Bob should ever hear about them.
        self.store.actor = self.describe(self.alice)
        for i in range(3):
            messages.send_from(self.store, "bob-laptop", f"question {i}")
        self.store.actor = None

    def enroll(self, name, key_file):
        key = identity.load_or_create(self.root / key_file)
        public = identity.encode_public(identity.public_bytes(key))
        code = clients.create_code(self.store.connection)["code"]
        return clients.redeem_code(self.store.connection, code, name, public)

    @staticmethod
    def describe(enrolled):
        return {"client_id": enrolled["client_id"], "name": enrolled["name"],
                "fingerprint": enrolled["fingerprint"]}

    def recall(self):
        response = McpServer(self.store).handle({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "recall", "arguments": {"query": "cache"}}})
        return json.loads(response["result"]["content"][0]["text"])

    def test_one_thread_cannot_see_another_thread_s_caller(self):
        # Bob's thread holds an identity for the whole test. An unidentified
        # request on a second thread must not inherit it.
        holding = threading.Event()
        release = threading.Event()
        seen: list[object] = []

        def bobs_request():
            self.store.actor = self.describe(self.bob)
            holding.set()
            release.wait(timeout=10)
            self.store.actor = None

        thread = threading.Thread(target=bobs_request)
        thread.start()
        try:
            holding.wait(timeout=10)
            self.assertIsNone(self.store.actor,
                              "this thread can see the identity another thread set")
            seen.append(self.recall().get("notices"))
        finally:
            release.set()
            thread.join(timeout=10)
        self.assertEqual([None], seen,
                         "an unidentified caller was told another client's unread count")

    def test_the_right_client_still_gets_its_own_notice(self):
        # The isolation must not silence the feature it protects.
        self.store.actor = self.describe(self.bob)
        try:
            notices = self.recall().get("notices")
        finally:
            self.store.actor = None
        self.assertTrue(notices)
        self.assertIn("3 unread", notices[0])

    def test_the_transport_clears_the_actor_on_both_routing_paths(self):
        # Threads are per request today, but a pooled server would reuse them,
        # and a stale actor is a leak that outlives the request that caused it.
        # Both branches must sit inside one try/finally rather than only the
        # locked one.
        import inspect

        from project_memory_mcp.http_server import _Handler

        source = inspect.getsource(_Handler.do_POST)
        tail = source[source.index("_talks_to_other_machines(message)"):]
        self.assertIn("finally:", tail)
        self.assertIn("store.actor = None", tail)
        self.assertEqual(
            1, tail.count("store.actor = None"),
            "the actor should be cleared once, after both branches - not per branch")


if __name__ == "__main__":
    unittest.main()
