"""Management UI: session auth, browsing, search, and triage actions."""

from __future__ import annotations

import http.cookiejar
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from project_memory_mcp.http_server import _Handler, _Sessions, _StoreRegistry
from project_memory_mcp.sqlite_store import SqliteMemoryStore

TOKEN = "ui-token"


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


CACHE = "Session cache invalidation races the auth refresh."
SHADER = "Shader compilation stalls on a cold start."


class UiTests(unittest.TestCase):
    ui_enabled = True

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "memory.db"
        seed = SqliteMemoryStore(self.db, "demo")
        seed.add_label("area:x", "x")
        seed.add_label("area:z", "z")
        seed.create_memory(memory("cache-race", CACHE, ["area:x"]))
        seed.create_memory(memory("shader-stall", SHADER, ["area:z"]))
        seed.close()

        registry = self.registry = _StoreRegistry(self.db)
        self.addCleanup(registry.close)
        handler = type("H", (_Handler,), {"registry": registry, "token": TOKEN,
                                          "sessions": _Sessions(), "ui_enabled": self.ui_enabled})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.addCleanup(self.httpd.server_close)
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.shutdown)
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    # ---------------------------------------------------------------- helpers

    def get(self, path):
        try:
            with self.opener.open(self.base + path, timeout=10) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode()

    def post(self, path, payload=None, form=None):
        if form is not None:
            request = urllib.request.Request(
                self.base + path, data=form.encode(),
                headers={"Content-Type": "application/x-www-form-urlencoded"})
        else:
            request = urllib.request.Request(
                self.base + path, data=json.dumps(payload or {}).encode(),
                headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=10) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode()

    def login(self):
        status, _ = self.post("/api/login", form="token=" + TOKEN)
        self.assertEqual(200, status)

    # ------------------------------------------------------------------ tests

    def test_root_redirects_to_login_when_signed_out(self):
        status, body = self.get("/")
        self.assertEqual(200, status)
        self.assertIn("Sign in", body)

    def test_api_requires_a_session(self):
        status, _ = self.get("/api/memories?project=demo")
        self.assertEqual(401, status)

    def test_wrong_token_is_rejected_and_issues_no_cookie(self):
        status, body = self.post("/api/login", form="token=wrong")
        self.assertEqual(401, status)
        self.assertIn("not accepted", body)
        self.assertEqual([], [c.name for c in self.jar])

    def test_session_cookie_is_httponly_strict_and_not_the_token(self):
        self.login()
        cookie = [c for c in self.jar if c.name == "pm_session"][0]
        self.assertTrue(cookie.has_nonstandard_attr("HttpOnly"))
        self.assertEqual("Strict", cookie.get_nonstandard_attr("SameSite"))
        self.assertNotEqual(TOKEN, cookie.value)

    def test_browse_lists_newest_first(self):
        self.login()
        status, body = self.get("/api/memories?project=demo&limit=10")
        self.assertEqual(200, status)
        self.assertEqual(["shader-stall", "cache-race"], [m["id"] for m in json.loads(body)["memories"]])

    def test_browse_includes_usage_counters(self):
        self.login()
        _, body = self.get("/api/memories?project=demo&limit=10")
        self.assertIn("usage", json.loads(body)["memories"][0])

    def test_search_uses_the_same_ranking_as_recall(self):
        self.login()
        _, body = self.get("/api/memories?project=demo&q=cache+invalidation&limit=5")
        self.assertEqual("cache-race", json.loads(body)["memories"][0]["id"])

    def test_label_filter(self):
        self.login()
        _, body = self.get("/api/memories?project=demo&label=area:z&limit=10")
        self.assertEqual(["shader-stall"], [m["id"] for m in json.loads(body)["memories"]])

    def test_detail_returns_the_whole_memory(self):
        self.login()
        _, body = self.get("/api/memory?project=demo&id=cache-race")
        payload = json.loads(body)
        self.assertEqual(CACHE, payload["memory"]["description"])
        self.assertIn("usage", payload)

    def test_status_change_persists(self):
        self.login()
        status, _ = self.post("/api/status", {"project": "demo", "id": "cache-race", "status": "wrong"})
        self.assertEqual(200, status)
        _, body = self.get("/api/memory?project=demo&id=cache-race")
        self.assertEqual("wrong", json.loads(body)["memory"]["status"])

    def test_archiving_takes_a_memory_out_of_the_listing(self):
        self.login()
        status, _ = self.post("/api/archive", {"project": "demo", "id": "cache-race"})
        self.assertEqual(200, status)
        _, body = self.get("/api/memories?project=demo&limit=10")
        self.assertEqual(["shader-stall"], [m["id"] for m in json.loads(body)["memories"]])

    def test_archived_memories_have_their_own_listing(self):
        # The only way back once something leaves the ranked pool.
        self.login()
        self.post("/api/archive", {"project": "demo", "id": "cache-race"})
        _, body = self.get("/api/memories?project=demo&status=archived&limit=10")
        rows = json.loads(body)["memories"]
        self.assertEqual(["cache-race"], [m["id"] for m in rows])
        self.assertTrue(rows[0]["archived_at"])

    def test_restoring_puts_it_back(self):
        self.login()
        self.post("/api/archive", {"project": "demo", "id": "cache-race"})
        status, _ = self.post("/api/archive", {"project": "demo", "id": "cache-race", "archived": False})
        self.assertEqual(200, status)
        _, body = self.get("/api/memories?project=demo&limit=10")
        self.assertIn("cache-race", [m["id"] for m in json.loads(body)["memories"]])

    def test_detail_reports_archive_state_and_tier(self):
        self.login()
        self.post("/api/archive", {"project": "demo", "id": "cache-race"})
        _, body = self.get("/api/memory?project=demo&id=cache-race")
        payload = json.loads(body)
        self.assertTrue(payload["archived_at"])
        self.assertEqual(1, payload["tier"])

    def counters(self):
        store, lock = self.registry.get("demo")
        with lock:
            usage = store.load_usage()["memories"]
            queries = store.connection.execute(
                "SELECT queries FROM projects WHERE id='demo'").fetchone()["queries"]
        return usage, queries

    def test_browsing_does_not_change_the_data_an_audit_reads(self):
        # A human reading the store must not alter the counters the audit judges
        # on, or reviewing a report changes the thing being reviewed. The UI
        # deliberately calls recall with record=False; nothing checked that until
        # a mutation run removed it and the whole suite still passed.
        self.login()
        before_usage, before_queries = self.counters()

        self.get("/api/memories?project=demo&q=cache+invalidation&limit=5")  # ranked path
        self.get("/api/memories?project=demo&limit=5")                       # listing path
        self.get("/api/memory?project=demo&id=cache-race")
        self.get("/api/memories?project=demo&status=archived&limit=5")

        after_usage, after_queries = self.counters()
        self.assertEqual(before_usage, after_usage,
                         "browsing the UI recorded usage against memories it showed")
        self.assertEqual(before_queries, after_queries,
                         "browsing the UI spent the exposure the audit measures gates in")

    def test_an_agent_recall_does_still_record(self):
        # The negative above is only meaningful if recording works at all -
        # otherwise deleting the feature would satisfy it.
        store, lock = self.registry.get("demo")
        with lock:
            store.recall("cache invalidation", limit=5)
        _, queries = self.counters()
        self.assertEqual(1, queries)

    def test_the_audit_endpoint_reports_the_last_run(self):
        self.login()
        status, body = self.get("/api/audit?project=demo")
        self.assertEqual(200, status)
        self.assertIsNone(json.loads(body)["run"])

    def test_delete_removes_the_memory(self):
        self.login()
        status, body = self.post("/api/delete", {"project": "demo", "id": "cache-race"})
        self.assertEqual(200, status)
        self.assertEqual("cache-race", json.loads(body)["deleted"])
        self.assertEqual(404, self.get("/api/memory?project=demo&id=cache-race")[0])

    def test_mutations_require_a_session(self):
        status, _ = self.post("/api/delete", {"project": "demo", "id": "cache-race"})
        self.assertEqual(401, status)

    # --------------------------------------------------- human promotion path

    def test_remotes_are_listed_and_an_empty_list_is_a_valid_answer(self):
        self.login()
        status, body = self.get("/api/remotes?project=demo")
        self.assertEqual(200, status)
        self.assertEqual([], json.loads(body)["remotes"])

    def test_detail_reports_visibility_so_the_button_knows_what_to_offer(self):
        self.login()
        _, body = self.get("/api/memory?project=demo&id=cache-race")
        payload = json.loads(body)
        self.assertEqual("private", payload["visibility"])
        self.assertFalse(payload["borrowed"])

    def test_visibility_can_be_changed_from_the_ui(self):
        # Without this, every memory is created private and the publish button
        # would be permanently disabled.
        self.login()
        status, _ = self.post("/api/visibility",
                              {"project": "demo", "id": "cache-race", "visibility": "public"})
        self.assertEqual(200, status)
        _, body = self.get("/api/memory?project=demo&id=cache-race")
        self.assertEqual("public", json.loads(body)["visibility"])

    def test_a_human_can_publish_a_memory_that_has_not_earned_a_tier(self):
        # The non-statistical path: a tier-1 memory that would never accrue the
        # usage to promote itself. The remote is down, so it queues.
        from project_memory_mcp import federation

        self.login()
        store, lock = self.registry.get("demo")
        with lock:
            federation.add_remote(store.connection, "team", "http://127.0.0.1:9/", "the team")
        self.post("/api/visibility",
                  {"project": "demo", "id": "cache-race", "visibility": "public"})
        status, body = self.post("/api/promote", {"project": "demo", "id": "cache-race",
                                                  "remote": "team", "force": True})
        self.assertEqual(200, status)
        self.assertEqual("cache-race", json.loads(body)["queued"])

    def test_publishing_an_unearned_memory_without_force_is_refused(self):
        from project_memory_mcp import federation

        self.login()
        store, lock = self.registry.get("demo")
        with lock:
            federation.add_remote(store.connection, "team", "http://127.0.0.1:9/", "the team")
        self.post("/api/visibility",
                  {"project": "demo", "id": "cache-race", "visibility": "public"})
        status, body = self.post("/api/promote",
                                 {"project": "demo", "id": "cache-race", "remote": "team"})
        self.assertEqual(400, status)
        self.assertIn("has not earned publication", json.loads(body)["error"])

    def test_the_ui_cannot_wave_a_credential_through(self):
        # allow_secrets is deliberately not exposed here: judging a match needs
        # the memory read next to the code, not a button in a browser.
        from project_memory_mcp import federation

        self.login()
        store, lock = self.registry.get("demo")
        with lock:
            federation.add_remote(store.connection, "team", "http://127.0.0.1:9/", "the team")
            store.update_memory("cache-race", {
                "remembered_facts": ["STRIPE_KEY=" + "sk_" + "live_" + "4eC39HqLyjWDarjtT1zdp7dc"]})
        self.post("/api/visibility",
                  {"project": "demo", "id": "cache-race", "visibility": "public"})
        status, body = self.post("/api/promote", {"project": "demo", "id": "cache-race",
                                                  "remote": "team", "force": True,
                                                  "allow_secrets": True})
        self.assertEqual(400, status)
        self.assertIn("stripe key", json.loads(body)["error"])

    def test_unknown_project_is_reported(self):
        self.login()
        status, body = self.get("/api/memories?project=nope")
        self.assertEqual(404, status)
        self.assertIn("Unknown project", json.loads(body)["error"])

    def test_an_expired_session_is_rejected(self):
        # The TTL is the reason a leaked cookie is survivable. Nothing checked
        # that it is ever enforced.
        import time as _time

        from project_memory_mcp.http_server import _Sessions

        self.login()
        self.assertEqual(200, self.get("/api/projects")[0])
        sessions: _Sessions = self.httpd.RequestHandlerClass.sessions
        with sessions._guard:
            for sid in list(sessions._issued):
                sessions._issued[sid] = _time.time() - 1
        self.assertEqual(401, self.get("/api/projects")[0],
                         "an expired session still worked")

    def test_logout_invalidates_the_session(self):
        self.login()
        self.assertEqual(200, self.get("/api/projects")[0])
        self.post("/api/logout")
        self.assertEqual(401, self.get("/api/projects")[0])


class UiDisabledTests(unittest.TestCase):
    """--no-ui serves MCP only: no pages, no session routes, health still up."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        db = Path(self.tmp.name) / "memory.db"
        SqliteMemoryStore(db, "demo").close()
        registry = _StoreRegistry(db)
        self.addCleanup(registry.close)
        handler = type("H", (_Handler,), {"registry": registry, "token": TOKEN,
                                          "sessions": _Sessions(), "ui_enabled": False})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.addCleanup(self.httpd.server_close)
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.shutdown)

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=10) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode()

    def test_pages_are_gone(self):
        self.assertEqual(404, self.get("/")[0])
        self.assertEqual(404, self.get("/login")[0])

    def test_api_routes_are_gone(self):
        self.assertEqual(404, self.get("/api/projects")[0])

    def test_health_still_answers(self):
        status, body = self.get("/health")
        self.assertEqual(200, status)
        self.assertEqual("project-memory-mcp", json.loads(body)["service"])
