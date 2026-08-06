"""Keypair identity, enrollment, and what a credential is allowed to do.

The property these protect: the private key never leaves the machine, so the
server holds nothing worth stealing and a fingerprint means the same thing on
every server that has seen it.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

from project_memory_mcp import clients, identity
from project_memory_mcp.http_server import _Handler, _Sessions, _StoreRegistry
from project_memory_mcp.sqlite_store import SqliteMemoryStore
from project_memory_mcp.validation import StoreError

SHARED = "shared-token"
CACHE = "Session cache invalidation races the auth refresh under load."


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


class KeyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "client_key.pem"

    def test_a_key_is_created_once_and_reused(self):
        first = identity.public_bytes(identity.load_or_create(self.path))
        second = identity.public_bytes(identity.load_or_create(self.path))
        self.assertEqual(first, second)
        self.assertEqual(32, len(first))

    def test_the_fingerprint_is_stable_and_openssh_shaped(self):
        public = identity.public_bytes(identity.load_or_create(self.path))
        self.assertTrue(identity.fingerprint(public).startswith("SHA256:"))
        self.assertEqual(identity.fingerprint(public), identity.fingerprint(public))

    def test_a_signature_covers_the_body_not_just_the_route(self):
        # Otherwise a captured signature would authorise any body at that path.
        key = identity.load_or_create(self.path)
        public = identity.public_bytes(key)
        headers = identity.sign_request(key, "POST", "/mcp", b'{"a":1}')
        identity.verify_request(public, headers, "POST", "/mcp", b'{"a":1}')
        with self.assertRaises(identity.IdentityError):
            identity.verify_request(public, headers, "POST", "/mcp", b'{"a":2}')

    def test_a_signature_does_not_transfer_to_another_route(self):
        key = identity.load_or_create(self.path)
        headers = identity.sign_request(key, "POST", "/mcp", b"{}")
        with self.assertRaises(identity.IdentityError):
            identity.verify_request(identity.public_bytes(key), headers, "POST", "/enroll", b"{}")

    def test_a_stale_signature_is_refused(self):
        key = identity.load_or_create(self.path)
        old = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        headers = identity.sign_request(key, "POST", "/mcp", b"{}", timestamp=old)
        with self.assertRaises(identity.IdentityError) as caught:
            identity.verify_request(identity.public_bytes(key), headers, "POST", "/mcp", b"{}")
        self.assertIn("replay", str(caught.exception))

    def test_another_key_cannot_sign_for_this_one(self):
        mine = identity.load_or_create(self.path)
        theirs = identity.load_or_create(Path(self.tmp.name) / "other.pem")
        headers = identity.sign_request(theirs, "POST", "/mcp", b"{}")
        with self.assertRaises(identity.IdentityError):
            identity.verify_request(identity.public_bytes(mine), headers, "POST", "/mcp", b"{}")


class EnrollmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "memory.db"
        SqliteMemoryStore(self.db, "demo").close()
        self.connection = sqlite3.connect(self.db)
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        self.key = identity.load_or_create(Path(self.tmp.name) / "client_key.pem")
        self.public = identity.encode_public(identity.public_bytes(self.key))

    def code(self, **kwargs):
        return clients.create_code(self.connection, **kwargs)["code"]

    def test_enrolling_with_a_key_stores_nothing_secret(self):
        result = clients.redeem_code(self.connection, self.code(), "alice-desktop", self.public)
        self.assertNotIn("token", result)
        row = self.connection.execute("SELECT * FROM clients").fetchone()
        self.assertIsNone(row["token_hash"])
        self.assertEqual(self.public, row["public_key"])

    def test_a_code_works_once(self):
        code = self.code()
        clients.redeem_code(self.connection, code, "first", self.public)
        other = identity.load_or_create(Path(self.tmp.name) / "second.pem")
        with self.assertRaises(StoreError):
            clients.redeem_code(self.connection, code, "second",
                                identity.encode_public(identity.public_bytes(other)))

    def test_an_expired_code_is_refused(self):
        code = self.code()
        self.connection.execute("UPDATE enrollment_codes SET expires_at='2020-01-01T00:00:00Z'")
        self.connection.commit()
        with self.assertRaises(StoreError):
            clients.redeem_code(self.connection, code, "late", self.public)

    def test_the_same_key_cannot_enroll_twice_on_one_server(self):
        clients.redeem_code(self.connection, self.code(), "alice", self.public)
        with self.assertRaises(StoreError):
            clients.redeem_code(self.connection, self.code(), "alice-again", self.public)

    def test_a_client_that_cannot_sign_gets_a_token_returned_once(self):
        result = clients.redeem_code(self.connection, self.code(), "browser-only")
        self.assertIn("token", result)
        stored = self.connection.execute("SELECT token_hash FROM clients").fetchone()["token_hash"]
        self.assertNotEqual(result["token"], stored)  # only the hash is kept

    def test_roles_and_project_scope_come_from_the_code(self):
        code = self.code(role="admin", projects=["demo"])
        clients.redeem_code(self.connection, code, "ops", self.public)
        client = clients.authenticate(
            self.connection, identity.sign_request(self.key, "POST", "/mcp", b"{}"),
            "POST", "/mcp", b"{}", SHARED)
        self.assertTrue(client.is_admin)
        self.assertTrue(client.may_access("demo"))
        self.assertFalse(client.may_access("other"))

    def test_a_revoked_client_is_refused(self):
        result = clients.redeem_code(self.connection, self.code(), "gone", self.public)
        clients.revoke(self.connection, result["client_id"])
        with self.assertRaises(StoreError):
            clients.authenticate(
                self.connection, identity.sign_request(self.key, "POST", "/mcp", b"{}"),
                "POST", "/mcp", b"{}", SHARED)

    def test_revoking_keeps_what_the_client_wrote(self):
        # Attribution is history; history is not editable by whoever holds the
        # newest credential.
        result = clients.redeem_code(self.connection, self.code(), "gone", self.public)
        clients.revoke(self.connection, result["client_id"])
        listed = clients.list_clients(self.connection)
        self.assertEqual(1, len(listed))
        self.assertTrue(listed[0]["revoked_at"])

    def test_the_shared_token_still_works(self):
        # Enrolling the first client must not lock out an existing deployment.
        client = clients.authenticate(
            self.connection, {"Authorization": f"Bearer {SHARED}"}, "POST", "/mcp", b"{}", SHARED)
        self.assertEqual("shared-token", client.client_id)

    def test_a_wrong_token_is_refused(self):
        with self.assertRaises(StoreError):
            clients.authenticate(self.connection, {"Authorization": "Bearer nope"},
                                 "POST", "/mcp", b"{}", SHARED)


class SignedRequestTests(unittest.TestCase):
    """The whole path: enroll over HTTP, then make a signed call."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "memory.db"
        seed = SqliteMemoryStore(self.db, "demo")
        seed.add_label("area:x", "x")
        seed.create_memory(memory("cache-race", CACHE, ["area:x"]))
        seed.close()

        registry = _StoreRegistry(self.db)
        self.addCleanup(registry.close)
        self.registry = registry
        handler = type("H", (_Handler,), {"registry": registry, "token": SHARED,
                                          "sessions": _Sessions(), "ui_enabled": True})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.addCleanup(self.httpd.server_close)
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.shutdown)
        self.key = identity.load_or_create(Path(self.tmp.name) / "client_key.pem")

    def post(self, path, payload, headers=None):
        body = json.dumps(payload).encode()
        request = urllib.request.Request(
            self.base + path, data=body,
            headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode())

    def enroll(self, role="contributor", projects=None):
        code = clients.create_code(self.registry.control(), role=role, projects=projects)["code"]
        return self.post("/enroll", {
            "code": code, "name": "alice-desktop",
            "public_key": identity.encode_public(identity.public_bytes(self.key))})

    def call(self, tool, args, sign=True):
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": tool, "arguments": args}}
        body = json.dumps(payload).encode()
        path = "/mcp?project=demo"
        headers = identity.sign_request(self.key, "POST", path, body) if sign else {}
        request = urllib.request.Request(
            self.base + path, data=body,
            headers={"Content-Type": "application/json", **headers})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode())

    def test_enrollment_over_http_returns_no_secret(self):
        status, result = self.enroll()
        self.assertEqual(200, status)
        self.assertNotIn("token", result)
        self.assertTrue(result["fingerprint"].startswith("SHA256:"))

    def test_a_signed_request_is_accepted(self):
        self.enroll()
        status, response = self.call("recall", {"query": "cache invalidation", "limit": 1})
        self.assertEqual(200, status)
        self.assertIn("result", response)

    def test_an_unenrolled_key_is_refused(self):
        status, response = self.call("recall", {"query": "cache"})
        self.assertEqual(401, status)
        self.assertIn("not enrolled", response["error"])

    def test_a_tampered_body_is_refused(self):
        self.enroll()
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "recall", "arguments": {"query": "cache"}}}
        signed = json.dumps(payload).encode()
        headers = identity.sign_request(self.key, "POST", "/mcp?project=demo", signed)
        tampered = json.dumps({**payload, "params": {"name": "delete_memory",
                                                     "arguments": {"id": "cache-race"}}}).encode()
        request = urllib.request.Request(
            self.base + "/mcp?project=demo", data=tampered,
            headers={"Content-Type": "application/json", **headers})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(401, caught.exception.code)

    def test_project_scope_is_enforced(self):
        self.enroll(projects=["other-project"])
        status, response = self.call("recall", {"query": "cache"})
        self.assertEqual(403, status)
        self.assertIn("no access", response["error"])

    def test_writes_record_who_made_them(self):
        self.enroll()
        self.call("create_memory", {"memory": memory(
            "new-lesson", "A lesson written by an authenticated client.", ["area:x"])})
        store, _ = self.registry.get("demo")
        row = store.connection.execute(
            "SELECT author_name, author_key FROM memories WHERE slug='new-lesson'").fetchone()
        self.assertEqual("alice-desktop", row["author_name"])
        self.assertEqual(identity.fingerprint(identity.public_bytes(self.key)), row["author_key"])

    def test_the_shared_token_still_reaches_the_tools(self):
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "recall", "arguments": {"query": "cache", "limit": 1}}}
        status, response = self.post("/mcp?project=demo", payload,
                                     {"Authorization": f"Bearer {SHARED}"})
        self.assertEqual(200, status)
        self.assertIn("result", response)


if __name__ == "__main__":
    unittest.main()
