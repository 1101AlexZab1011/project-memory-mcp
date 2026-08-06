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
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import __version__
from .server import TOOLS, McpServer
from .sqlite_store import SqliteMemoryStore, StoreError

MAX_BODY_BYTES = 4 * 1024 * 1024


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


class _Handler(BaseHTTPRequestHandler):
    server_version = f"project-memory-mcp/{__version__}"
    registry: _StoreRegistry
    token: str

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
        path = urlparse(self.path).path
        if path not in ("/", "/health"):
            self._send(404, {"error": "not found"})
            return
        self._send(200, {
            "service": "project-memory-mcp",
            "version": __version__,
            "database": str(self.registry.database),
            "endpoint": "/mcp?project=<project-id>",
            "tools": [t["name"] for t in TOOLS],
        })

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
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


def run_http_server(database: Path | str, bind: str, port: int, token: str) -> int:
    registry = _StoreRegistry(database)
    handler = type("Handler", (_Handler,), {"registry": registry, "token": token})
    httpd = ThreadingHTTPServer((bind, port), handler)
    projects = SqliteMemoryStore.list_projects(
        __import__("sqlite3").connect(str(Path(database)))
    )
    print(
        f"project-memory-mcp {__version__} serving {Path(database)} on http://{bind}:{port}/mcp\n"
        f"  projects: {', '.join(projects) or '(none - create one with `migrate` first)'}\n"
        f"  clients:  \"url\": \"http://{bind}:{port}/mcp?project=<id>\" with an "
        f"Authorization: Bearer header",
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
