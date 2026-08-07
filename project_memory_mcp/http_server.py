"""MCP over HTTP, so one host can serve memory to every device that can reach it.

The MCP server itself is the network service: there is no separate database
protocol, because the tools already are the query API. SQLite stays embedded
and is only ever opened from local disk - never a network share, where its
locking is documented to corrupt.

Security posture, deliberately narrow:

- A shared static bearer token, checked on every request in constant time.
- The listening interface is an explicit argument. There is no default, because
  binding to 0.0.0.0 would also publish the store on every other network the
  host is attached to, and that is a decision rather than a convenience.
- No TLS. Intended for a trusted LAN or an encrypted overlay (Radmin, Tailscale,
  WireGuard). Over a hostile network the token would travel in the clear.
"""

from __future__ import annotations

import hmac
import json
import secrets
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote_plus, urlparse

from . import __version__
from . import clients
from . import federation
from . import ui
from .server import TOOLS, McpServer
from .sqlite_store import SqliteMemoryStore, StoreError

MAX_BODY_BYTES = 4 * 1024 * 1024
SESSION_TTL_SECONDS = 12 * 3600
SESSION_COOKIE = "pm_session"


class _StoreRegistry:
    """One store per project, shared across request threads.

    SQLite connections are not thread-safe by default and ThreadingHTTPServer
    hands each request to whichever thread is free, so connections are opened
    with check_same_thread=False and every call is serialized by a lock. WAL
    would allow concurrent readers, but the store also writes on read (usage
    counters), so one lock per project is the honest simplification.
    """

    def __init__(self, database: Path | str) -> None:
        self.database = Path(database)
        self._stores: dict[str, SqliteMemoryStore] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self._control: sqlite3.Connection | None = None

    def control(self) -> sqlite3.Connection:
        """Connection for the server-wide tables - clients and enrollment codes.

        Those are not scoped to a project, so they cannot ride on a project
        store, and authentication has to work before any project is known.
        """
        with self._guard:
            if self._control is None:
                self._control = sqlite3.connect(self.database, check_same_thread=False)
                self._control.row_factory = sqlite3.Row
            return self._control

    def get(self, project: str) -> tuple[SqliteMemoryStore, threading.Lock]:
        with self._guard:
            if project not in self._stores:
                store = SqliteMemoryStore(self.database, project, create=False, check_same_thread=False)
                self._stores[project] = store
                self._locks[project] = threading.Lock()
            return self._stores[project], self._locks[project]

    def close(self) -> None:
        with self._guard:
            for store in self._stores.values():
                store.close()
            self._stores.clear()
            if self._control is not None:
                self._control.close()
                self._control = None


def _talks_to_other_machines(message: dict[str, Any]) -> bool:
    """Whether this request will wait on a remote server.

    Exactly one tool does, and only when asked to. Deciding here rather than
    inside the dispatcher is deliberate: the transport owns the lock, so the
    transport is what gets to say which calls run under it.
    """
    if message.get("method") != "tools/call":
        return False
    params = message.get("params") or {}
    if params.get("name") != "recall":
        return False
    return bool((params.get("arguments") or {}).get("include_remotes"))


class _Sessions:
    """Browser sessions for the UI.

    The cookie carries a random id, not the token: a leaked session expires and
    can be revoked, whereas a leaked master token is the whole store. MCP
    clients keep using the Bearer header and never touch this.
    """

    def __init__(self) -> None:
        self._issued: dict[str, float] = {}
        self._guard = threading.Lock()

    def create(self) -> str:
        sid = secrets.token_urlsafe(32)
        with self._guard:
            self._issued[sid] = time.time() + SESSION_TTL_SECONDS
        return sid

    def valid(self, sid: str | None) -> bool:
        if not sid:
            return False
        with self._guard:
            expiry = self._issued.get(sid)
            if expiry is None:
                return False
            if expiry < time.time():
                del self._issued[sid]
                return False
            return True

    def drop(self, sid: str | None) -> None:
        if sid:
            with self._guard:
                self._issued.pop(sid, None)


class _Handler(BaseHTTPRequestHandler):
    server_version = f"project-memory-mcp/{__version__}"
    registry: _StoreRegistry
    token: str
    sessions: _Sessions
    ui_enabled: bool = True
    client: clients.Client | None = None

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter than the default
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # ----------------------------------------------------------------- helpers

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status: int, body: str, headers: dict[str, str] | None = None) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def _cookie(self) -> str | None:
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == SESSION_COOKIE:
                return value
        return None

    def _has_session(self) -> bool:
        return self.sessions.valid(self._cookie())

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY_BYTES:
            return b""
        return self.rfile.read(length)

    def _json_body(self) -> dict[str, Any]:
        try:
            return json.loads(self._read_body().decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}

    def _store_for(self, project: str):
        if not project:
            raise StoreError("missing project")
        return self.registry.get(project)

    def _identify(self, method: str, path: str, body: bytes = b"") -> bool:
        """Authenticate the caller and remember who it is for this request.

        A signature is checked first: it proves possession of a key without
        putting anything reusable on the wire, which is what makes it worth
        having when the deployment has no TLS. Per-client tokens come next, and
        the shared token last, so an existing setup keeps working while clients
        are enrolled one at a time.
        """
        self.client = None
        try:
            self.client = clients.authenticate(
                self.registry.control(), self.headers, method, path, body, self.token)
            return True
        except StoreError as error:
            self._send(401, {"error": str(error)})
            return False

    # There is no _require_admin here, deliberately. It existed, was called by
    # nothing, and a mutation run that made it return True for everyone changed
    # no test - which is how dead code looks from the outside.
    #
    # Nothing over HTTP is admin-only. The privileged operations - minting
    # enrollment codes, revoking a client - are CLI commands, so they are
    # already gated by having an account on the machine holding the database,
    # which is a stronger check than a role column. The `role` on a client is
    # therefore recorded and not yet enforced anywhere; anything that needs it
    # should call clients.Client.is_admin at the point it matters rather than
    # trusting a helper to have been used.

    def _require_project(self, project: str) -> bool:
        if self.client is None or self.client.may_access(project):
            return True
        self._send(403, {"error": f"this client has no access to project '{project}'"})
        return False

    # ------------------------------------------------------------------ routes

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)

        if path == "/health":
            self._send(200, {
                "service": "project-memory-mcp",
                "version": __version__,
                "database": str(self.registry.database),
                "endpoint": "/mcp?project=<project-id>",
                "tools": [t["name"] for t in TOOLS],
            })
            return
        if not self.ui_enabled:
            self._send(404, {"error": "management UI is disabled; POST JSON-RPC to /mcp"})
            return
        if path == "/login":
            self._send_html(200, ui.login_page())
            return
        if path == "/":
            if not self._has_session():
                self._send_html(303, "", {"Location": "/login"})
                return
            self._send_html(200, ui.app_page())
            return
        if path.startswith("/api/"):
            if not self._has_session():
                self._send(401, {"error": "not signed in"})
                return
            self._api_get(path, query)
            return
        self._send(404, {"error": "not found"})

    def _api_get(self, path: str, query: dict[str, list[str]]) -> None:
        def one(key: str, default: str = "") -> str:
            return (query.get(key) or [default])[0]

        try:
            if path == "/api/projects":
                import sqlite3

                connection = sqlite3.connect(str(self.registry.database))
                try:
                    self._send(200, {"projects": SqliteMemoryStore.list_projects(connection)})
                finally:
                    connection.close()
                return

            store, lock = self._store_for(one("project"))
            if path == "/api/labels":
                with lock:
                    self._send(200, {"labels": sorted(store.list_labels()["labels"])})
                return

            if path == "/api/memories":
                limit = max(1, min(100, int(one("limit", "25"))))
                offset = max(0, int(one("offset", "0")))
                status = one("status") or None
                if status == "archived":
                    # Archived memories are outside the ranked pool, so they
                    # have their own listing rather than a status filter.
                    with lock:
                        rows = store.archived(limit=limit, offset=offset)["memories"]
                        counters = store.load_usage()["memories"]
                    for row in rows:
                        row["usage"] = counters.get(row["id"], {})
                    self._send(200, {"memories": rows, "offset": offset})
                    return
                label = one("label") or None
                text = one("q").strip()
                with lock:
                    if text:
                        # The same ranking the agent gets, so a human searching
                        # sees what recall would have returned, not a different
                        # answer from a second search path.
                        # record=False: a human reading the store must not
                        # change the counters the audit reads, or reviewing a
                        # report would alter the data the report is about.
                        found = store.recall(query=text, label_query=label, status_filter=status,
                                             limit=limit + offset, full_count=0,
                                             record=False)["memories"]
                        rows = found[offset:offset + limit]
                    else:
                        rows = store.recall(order="recent", label_query=label, status_filter=status,
                                            limit=limit, offset=offset, full_count=0,
                                            record=False)["memories"]
                    counters = store.load_usage()["memories"]
                for row in rows:
                    row["usage"] = counters.get(row["id"], {})
                self._send(200, {"memories": rows, "offset": offset})
                return

            if path == "/api/memory":
                memory_id = one("id")
                with lock:
                    memory = store.get_memory(memory_id)
                    usage = store.load_usage()["memories"].get(memory_id, {})
                    row = store.connection.execute(
                        "SELECT archived_at, tier, visibility, origin_remote FROM memories "
                        "WHERE project_id=? AND slug=?",
                        (store.project, memory_id)).fetchone()
                self._send(200, {"memory": memory, "usage": usage,
                                 "archived_at": row["archived_at"] if row else None,
                                 "tier": row["tier"] if row else 1,
                                 "visibility": row["visibility"] if row else "private",
                                 # A cached copy belongs to the server it came
                                 # from; publishing it onward is not ours to do.
                                 "borrowed": bool(row and row["origin_remote"])})
                return

            if path == "/api/remotes":
                with lock:
                    self._send(200, {"remotes": [r.describe()
                                                 for r in federation.list_remotes(
                                                     store.connection, enabled_only=True)]})
                return

            if path == "/api/audit":
                # Read-only: the last recorded run, never a new sweep. Running
                # one from a page load would put a scan on a request thread and
                # let a refresh rewrite the history being read.
                from .audit import last_run

                verdicts = tuple(v for v in one("verdict").split(",") if v)
                with lock:
                    self._send(200, last_run(store, verdicts or None))
                return
        except StoreError as exc:
            self._send(404, {"error": str(exc)})
            return
        except (ValueError, KeyError) as exc:
            self._send(400, {"error": str(exc)})
            return
        self._send(404, {"error": "not found"})

    def _api_post(self, path: str) -> None:
        # Read the body up front, always. Responding to a POST without consuming
        # it leaves bytes in the socket, and the next thing the client does on
        # that connection fails - which showed up as two UI tests that failed
        # roughly one run in ten and looked like flakiness.
        raw = self._read_body()

        if path == "/api/login":
            presented = ""
            for pair in raw.decode("utf-8", "replace").split("&"):
                key, _, value = pair.partition("=")
                if key == "token":
                    presented = unquote_plus(value)
            accepted = bool(self.token) and hmac.compare_digest(presented, self.token)
            if not accepted:
                # A per-client token works here too: whoever holds a credential
                # for this server can read it in a browser.
                accepted = clients.token_is_valid(self.registry.control(), presented)
            if not accepted:
                self._send_html(401, ui.login_page("That token was not accepted."))
                return
            sid = self.sessions.create()
            # SameSite=Strict is what defends the status and delete routes: a
            # cross-site request will not carry this cookie, so no separate CSRF
            # token is needed. HttpOnly keeps it out of reach of page scripts.
            cookie = SESSION_COOKIE + "=" + sid + "; Path=/; HttpOnly; SameSite=Strict"
            self._send_html(303, "", {"Location": "/", "Set-Cookie": cookie})
            return

        if path == "/api/logout":
            self.sessions.drop(self._cookie())
            self._send(200, {"ok": True})
            return

        if not self._has_session():
            self._send(401, {"error": "not signed in"})
            return
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {}
        try:
            store, lock = self._store_for(payload.get("project") or "")
            memory_id = payload.get("id") or ""
            if path == "/api/status":
                with lock:
                    store.update_memory(memory_id, {"status": payload.get("status")})
                self._send(200, {"ok": True, "id": memory_id})
                return
            if path == "/api/archive":
                archived = bool(payload.get("archived", True))
                with lock:
                    result = store.archive_memory(payload["id"], archived=archived)
                self._send(200, result)
                return

            if path == "/api/delete":
                with lock:
                    self._send(200, store.delete_memory(memory_id, memory_id))
                return

            if path == "/api/visibility":
                # Without this the promote button is disabled on almost
                # everything: memories are created private, and nothing else in
                # the UI could ever change that.
                with lock:
                    self._send(200, store.set_visibility(
                        memory_id, str(payload.get("visibility") or "")))
                return

            if path == "/api/promote":
                # The non-statistical path to publication. A lesson that is
                # hard-won, rarely needed and expensive to rediscover will never
                # accrue the usage that earns a tier, and a person is the only
                # thing that can tell that apart from a memory nobody wanted.
                # force is theirs to give; allow_secrets deliberately is not -
                # waving a credential through is a decision for whoever can read
                # the memory next to the code, not for a button in a browser.
                with lock:
                    self._send(200, federation.promote(
                        store, memory_id, str(payload.get("remote") or ""),
                        force=bool(payload.get("force"))))
                return
        except StoreError as exc:
            self._send(400, {"error": str(exc)})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if self.ui_enabled and parsed.path.startswith("/api/"):
            self._api_post(parsed.path)
            return

        # The body is read before authenticating, because a signature covers it:
        # that is what makes a signature authorise one request rather than one
        # endpoint. Size is bounded first so an oversized body cannot be used to
        # make the server read forever before it has decided who is calling.
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0 or length > MAX_BODY_BYTES:
            self._send(400, {"error": "oversized request body"})
            return
        raw = self.rfile.read(length) if length else b""

        if parsed.path == "/enroll":
            self._enroll(raw)
            return
        if parsed.path != "/mcp":
            self._send(404, {"error": "not found; POST JSON-RPC to /mcp?project=<project-id>"})
            return
        # Sign over the full request target, query included: the project lives
        # in the query string, so a signature that omitted it could be replayed
        # against a different project.
        if not self._identify("POST", self.path, raw):
            return

        project = (parse_qs(parsed.query).get("project") or [""])[0]
        if not project:
            self._send(400, {"error": "missing ?project=<project-id> in the URL"})
            return
        if not self._require_project(project):
            return
        if not raw:
            self._send(400, {"error": "missing request body"})
            return
        try:
            message = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._send(400, {"error": f"invalid JSON: {exc}"})
            return

        try:
            store, lock = self.registry.get(project)
        except StoreError as exc:
            # Unknown project ids fail loudly rather than serving an empty store.
            self._send(404, {"error": str(exc)})
            return

        # The identity is per request, and store.actor is thread-local, so it is
        # set the same way on both paths and always cleared. Setting it only on
        # the locked path would leave the unlocked one reading whatever this
        # thread last did - which is how a caller ends up being told the
        # unread-message count of a different client.
        actor = self.client.describe() if self.client else None
        try:
            store.actor = actor
            if _talks_to_other_machines(message):
                # Run outside the project lock, and hand the lock down instead. A
                # federated recall waits on other people's servers; holding this
                # project's lock across that would stall every other request for
                # it, reads included. recall_across takes the lock for its two
                # database phases and drops it for the fan-out.
                response = McpServer(store, db_lock=lock).handle(message)
            else:
                with lock:
                    response = McpServer(store).handle(message)
        finally:
            store.actor = None
        if response is None:
            self.send_response(204)
            self.end_headers()
            return
        self._send(200, response)

    def _enroll(self, raw: bytes) -> None:
        """Redeem an enrollment code. The only unauthenticated write there is.

        It has to be: a machine enrolling has no credential yet. The code is what
        stands in for one - single use, short lived, and worthless afterwards.
        """
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._send(400, {"error": f"invalid JSON: {exc}"})
            return
        try:
            result = clients.redeem_code(
                self.registry.control(), str(payload.get("code") or ""),
                str(payload.get("name") or ""), payload.get("public_key"))
        except StoreError as exc:
            self._send(400, {"error": str(exc)})
            return
        self._send(200, result)


def start_computer(registry: _StoreRegistry, database: Path | str,
                   interval_seconds: int = 3600) -> tuple[Any, Any]:
    """Bring up the worker that does the periodic work on memories.

    It shares the registry's per-project locks, so a sweep and a request never
    touch one project at the same time, and it holds each lock only for a slice
    at a time so serving keeps flowing while heavy work runs.
    """
    from .computer import Computer, Scheduler

    def projects() -> list[str]:
        return SqliteMemoryStore.list_projects(registry.control())

    def open_store(project: str):
        store, _ = registry.get(project)
        return store

    def lock_for(project: str):
        _, lock = registry.get(project)
        return lock

    # The registry owns these stores, so the worker must not close them.
    computer = Computer(open_store=lambda p: _Borrowed(open_store(p)),
                        lock_for=lock_for, database=database)
    computer.start()
    scheduler = Scheduler(computer, projects, interval_seconds=interval_seconds)
    scheduler.start()
    return computer, scheduler


class _Borrowed:
    """A store the worker uses but does not own, so close() is a no-op."""

    def __init__(self, store: Any) -> None:
        self._store = store

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    def close(self) -> None:
        return None


def run_http_server(database: Path | str, bind: str, port: int, token: str,
                    ui_enabled: bool = True, compute_interval: int = 3600) -> int:
    registry = _StoreRegistry(database)
    handler = type("Handler", (_Handler,), {
        "registry": registry, "token": token, "sessions": _Sessions(), "ui_enabled": ui_enabled})
    httpd = ThreadingHTTPServer((bind, port), handler)
    import sqlite3

    connection = sqlite3.connect(str(Path(database)))
    try:
        projects = SqliteMemoryStore.list_projects(connection)
    finally:
        connection.close()

    # Open every project now rather than on its first request. Opening is what
    # applies a pending schema migration, and doing that inside a request means
    # one client pays for it, and a failure surfaces as somebody's 500 instead
    # of as a server that refused to start.
    for name in projects:
        registry.get(name)

    computer = scheduler = None
    if compute_interval > 0:
        computer, scheduler = start_computer(registry, database, compute_interval)

    browser = f"http://{bind}:{port}/" if ui_enabled else "(disabled)"
    print(
        "\n".join([
            f"project-memory-mcp {__version__} serving {Path(database)} on http://{bind}:{port}/mcp",
            f"  projects: {', '.join(projects) or '(none - create one with `migrate` first)'}",
            f"  clients:  url http://{bind}:{port}/mcp?project=<id> with an Authorization: Bearer header",
            f"  browser:  {browser}",
            f"  computer: {'running, every %ds' % compute_interval if computer else 'disabled'}",
        ]),
        file=sys.stderr,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if scheduler is not None:
            scheduler.stop()
        if computer is not None:
            computer.stop()
        httpd.server_close()
        registry.close()
    return 0
