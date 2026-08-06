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
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote_plus, urlparse

from . import __version__
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

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        if not header:
            self._send(401, {"error": "missing Authorization header; expected 'Bearer <token>'"})
            return False
        if not header.startswith("Bearer "):
            self._send(401, {"error": "Authorization header must use the Bearer scheme"})
            return False
        presented = header[len("Bearer "):].strip()
        if presented.startswith("${") and presented.endswith("}"):
            # A client sent the literal placeholder, so its config was never
            # expanded. Saying so turns a baffling auth failure into a one-line
            # fix; it leaks nothing, since the value is the client's own text.
            self._send(401, {"error": f"token was sent unexpanded as {presented!r} - the environment "
                                      "variable is not set where the MCP client starts"})
            return False
        if not hmac.compare_digest(presented, self.token):
            self._send(401, {"error": "invalid token"})
            return False
        return True

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
                label = one("label") or None
                text = one("q").strip()
                with lock:
                    if text:
                        # The same ranking the agent gets, so a human searching
                        # sees what recall would have returned, not a different
                        # answer from a second search path.
                        found = store.recall(query=text, label_query=label, status_filter=status,
                                             limit=limit + offset, full_count=0)["memories"]
                        rows = found[offset:offset + limit]
                    else:
                        rows = store.recall(order="recent", label_query=label, status_filter=status,
                                            limit=limit, offset=offset, full_count=0)["memories"]
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
                self._send(200, {"memory": memory, "usage": usage})
                return
        except StoreError as exc:
            self._send(404, {"error": str(exc)})
            return
        except (ValueError, KeyError) as exc:
            self._send(400, {"error": str(exc)})
            return
        self._send(404, {"error": "not found"})

    def _api_post(self, path: str) -> None:
        if path == "/api/login":
            presented = ""
            for pair in self._read_body().decode("utf-8", "replace").split("&"):
                key, _, value = pair.partition("=")
                if key == "token":
                    presented = unquote_plus(value)
            if not hmac.compare_digest(presented, self.token):
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
        payload = self._json_body()
        try:
            store, lock = self._store_for(payload.get("project") or "")
            memory_id = payload.get("id") or ""
            if path == "/api/status":
                with lock:
                    store.update_memory(memory_id, {"status": payload.get("status")})
                self._send(200, {"ok": True, "id": memory_id})
                return
            if path == "/api/delete":
                with lock:
                    self._send(200, store.delete_memory(memory_id, memory_id))
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
        if parsed.path != "/mcp":
            self._send(404, {"error": "not found; POST JSON-RPC to /mcp?project=<project-id>"})
            return
        if not self._authorized():
            return

        project = (parse_qs(parsed.query).get("project") or [""])[0]
        if not project:
            self._send(400, {"error": "missing ?project=<project-id> in the URL"})
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY_BYTES:
            self._send(400, {"error": "missing or oversized request body"})
            return
        try:
            message = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._send(400, {"error": f"invalid JSON: {exc}"})
            return

        try:
            store, lock = self.registry.get(project)
        except StoreError as exc:
            # Unknown project ids fail loudly rather than serving an empty store.
            self._send(404, {"error": str(exc)})
            return

        with lock:
            response = McpServer(store).handle(message)
        if response is None:
            self.send_response(204)
            self.end_headers()
            return
        self._send(200, response)


def run_http_server(database: Path | str, bind: str, port: int, token: str,
                    ui_enabled: bool = True) -> int:
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
    browser = f"http://{bind}:{port}/" if ui_enabled else "(disabled)"
    print(
        "\n".join([
            f"project-memory-mcp {__version__} serving {Path(database)} on http://{bind}:{port}/mcp",
            f"  projects: {', '.join(projects) or '(none - create one with `migrate` first)'}",
            f"  clients:  url http://{bind}:{port}/mcp?project=<id> with an Authorization: Bearer header",
            f"  browser:  {browser}",
        ]),
        file=sys.stderr,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        registry.close()
    return 0
