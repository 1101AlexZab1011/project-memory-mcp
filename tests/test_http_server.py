"""HTTP transport: token auth, project scoping, and tool dispatch."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from project_memory_mcp.http_server import _Handler, _StoreRegistry
from project_memory_mcp.sqlite_store import SqliteMemoryStore

TOKEN = "test-token"


def memory(memory_id, description, labels):
    return {
        "schema_version": 1, "id": memory_id, "status": "active", "description": description,
        "tags": [], "labels": labels,
        "scope": {"project": "p", "area": "a", "files": [], "applies_to": []},
        "triggers": ["trigger for " + memory_id], "remembered_facts": [description],
        "solution_pattern": [], "pitfalls": [],
        "evidence": {"created_from_task": "t", "last_validated": "2026-01-01"},
        "relationships": {"related": [], "supersedes": [], "superseded_by": []},
    }


class HttpServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "memory.db"
        seed = SqliteMemoryStore(self.db, "demo")
        seed.add_label("area:x", "x")
        seed.create_memory(memory("cache-race", "Session cache invalidation races the auth refresh.", ["area:x"]))
        seed.close()

        registry = _StoreRegistry(self.db)
        self.addCleanup(registry.close)
        handler = type("H", (_Handler,), {"registry": registry, "token": TOKEN})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.addCleanup(self.httpd.server_close)
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.httpd.shutdown)

    def call(self, name, arguments, token=TOKEN, project="demo", headers=None):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": name, "arguments": arguments}}).encode()
        head = {"Content-Type": "application/json"}
        if token is not None:
            head["Authorization"] = "Bearer " + token
        head.update(headers or {})
        request = urllib.request.Request(self.base + "/mcp?project=" + project, data=body, headers=head)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def payload(self, response):
        return json.loads(response["result"]["content"][0]["text"])

    def test_tool_call_succeeds_with_a_valid_token(self):
        status, response = self.call("recall", {"query": "cache invalidation", "limit": 1, "full_count": 0})
        self.assertEqual(200, status)
        self.assertEqual(["cache-race"], [m["id"] for m in self.payload(response)["memories"]])

    def test_missing_authorization_is_rejected(self):
        status, body = self.call("list_labels", {}, token=None)
        self.assertEqual(401, status)
        self.assertIn("Authenticate with a signature", body["error"])

    def test_wrong_token_is_rejected(self):
        status, body = self.call("list_labels", {}, token="nope")
        self.assertEqual(401, status)
        self.assertEqual("invalid token", body["error"])

    def test_non_bearer_scheme_is_rejected(self):
        status, body = self.call("list_labels", {}, headers={"Authorization": "Basic abc"})
        self.assertEqual(401, status)
        self.assertIn("Bearer", body["error"])

    def test_unexpanded_env_var_says_so(self):
        # Otherwise a client whose ${VAR} was never expanded sees a bare
        # "invalid token" and hunts for the wrong problem.
        status, body = self.call("list_labels", {}, token="${PROJECT_MEMORY_TOKEN}")
        self.assertEqual(401, status)
        self.assertIn("unexpanded", body["error"])

    def test_unknown_project_fails_loudly(self):
        status, body = self.call("list_labels", {}, project="typoed")
        self.assertEqual(404, status)
        self.assertIn("Unknown project", body["error"])
        self.assertIn("demo", body["error"])

    def test_missing_project_is_rejected(self):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}).encode()
        request = urllib.request.Request(self.base + "/mcp", data=body,
                                         headers={"Authorization": "Bearer " + TOKEN})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(400, caught.exception.code)

    def test_health_endpoint_needs_no_token(self):
        with urllib.request.urlopen(self.base + "/health", timeout=10) as response:
            payload = json.loads(response.read())
        self.assertEqual("project-memory-mcp", payload["service"])
        self.assertIn("recall", payload["tools"])

    def test_unknown_path_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(self.base + "/nope", timeout=10)
        self.assertEqual(404, caught.exception.code)

    def test_writes_work_over_http(self):
        status, response = self.call("create_memory", {
            "memory": memory("via-http", "A memory created through the HTTP transport.", ["area:x"])})
        self.assertEqual(200, status)
        self.assertEqual("via-http", self.payload(response)["created"])
        status, response = self.call("get_memory", {"id": "via-http"})
        self.assertEqual("via-http", self.payload(response)["id"])

    def test_concurrent_requests_do_not_corrupt_the_connection(self):
        # ThreadingHTTPServer hands requests to arbitrary threads; SQLite
        # connections are not thread-safe, so this would fail without the lock.
        #
        # The barrier is the point. Started without one, four threads spawned in
        # a loop can each finish before the next begins, and the test passes
        # having never overlapped a single request - proving nothing about the
        # thing it is named after. Releasing them together is what makes the
        # contention real.
        #
        # Failures are captured per worker rather than raised, and sorted into
        # two kinds, because they mean opposite things. A non-200 is the server
        # getting it wrong - the lock. A transport error is this machine running
        # out of sockets under the rest of the suite, which is not this server's
        # defect and should not be read as one. Both still fail; only the
        # message differs, because a test that hides one of them is worse than
        # a test that occasionally reports the environment.
        statuses: list[int] = []
        transport: list[str] = []
        guard = threading.Lock()
        ready = threading.Barrier(4, timeout=30)

        def hammer():
            ready.wait()
            for _ in range(5):
                try:
                    status = self.call("recall", {"query": "cache", "limit": 1, "full_count": 0})[0]
                except Exception as error:  # noqa: BLE001 - the point is to name it
                    with guard:
                        transport.append(f"{type(error).__name__}: {error}")
                else:
                    with guard:
                        statuses.append(status)

        threads = [threading.Thread(target=hammer) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        self.assertEqual(
            [], transport,
            f"{len(transport)} of 20 requests never reached the server. This is a connection "
            f"failure, not a wrong answer - suspect socket exhaustion from the rest of the "
            f"suite before suspecting the lock: {transport}")
        self.assertEqual(20, len(statuses),
                         f"only {len(statuses)} of 20 workers finished")
        bad = [s for s in statuses if s != 200]
        self.assertEqual(
            [], bad,
            f"{len(bad)} of 20 concurrent requests did not return 200: {bad}. A non-200 here "
            f"means the store was touched from two threads at once.")
