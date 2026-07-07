"""Minimal stdio MCP server exposing the project-memory store as tools.

Speaks JSON-RPC over stdin/stdout, accepting both newline-delimited JSON and
Content-Length framed messages. No third-party dependencies.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .store import STORE_DIR_NAME, MemoryStore, StoreError


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required or [],
            "additionalProperties": False,
        },
    }


TOOLS = [
    _tool("list_labels", "Return canonical project-memory labels grouped by prefix.", {}),
    _tool(
        "search_memories",
        "Search lightweight memory index records by label query, status, and optional text.",
        {
            "label_query": {
                "description": "Either an object with all/any/not arrays or a string expression using AND, OR, NOT, and parentheses.",
                "type": ["object", "string", "null"],
                "additionalProperties": True,
            },
            "status_filter": {
                "description": "A status string, array of statuses, 'all', or omitted for active+stale.",
                "type": ["array", "string", "null"],
                "items": {"type": "string"},
            },
            "text_query": {"type": ["string", "null"]},
            "limit": {"type": ["integer", "null"], "minimum": 1},
        },
    ),
    _tool("get_memory", "Return the full JSON for a memory id.", {"id": {"type": "string"}}, ["id"]),
    _tool(
        "get_memory_neighborhood",
        "Return a bounded relationship tree/graph around a memory.",
        {
            "id": {"type": "string"},
            "depth": {"type": "integer", "minimum": 0, "default": 1},
            "max_nodes": {"type": "integer", "minimum": 1, "default": 25},
        },
        ["id"],
    ),
    _tool(
        "create_memory",
        "Create a memory JSON file, synchronize bidirectional relationships, regenerate INDEX.json, and validate.",
        {
            "memory": {"type": "object", "additionalProperties": True},
            "related_label_query": {
                "type": ["object", "string", "null"],
                "additionalProperties": True,
                "description": "Optional label query used to return likely related candidates after creation.",
            },
        },
        ["memory"],
    ),
    _tool(
        "update_memory",
        "Patch an existing memory, synchronize relationships, regenerate INDEX.json, and validate.",
        {
            "id": {"type": "string"},
            "patch": {"type": "object", "additionalProperties": True},
            "related_label_query": {
                "type": ["object", "string", "null"],
                "additionalProperties": True,
                "description": "Optional label query used to return likely related candidates after update.",
            },
        },
        ["id", "patch"],
    ),
    _tool(
        "add_label",
        "Add a canonical label to .project-memory/labels.json and validate the store.",
        {"label": {"type": "string"}, "description": {"type": "string"}},
        ["label", "description"],
    ),
    _tool(
        "delete_memory",
        "Delete a memory after exact-id confirmation and remove dangling relationship references.",
        {"id": {"type": "string"}, "confirm_exact_id": {"type": "string"}},
        ["id", "confirm_exact_id"],
    ),
]


class McpServer:
    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        if "id" not in message:
            return None
        method = message.get("method")
        try:
            if method == "initialize":
                result = {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "project-memory-mcp", "version": __version__},
                }
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                params = message.get("params") or {}
                result = self._call_tool(params.get("name"), params.get("arguments") or {})
            else:
                return self._error(message["id"], -32601, f"Method not found: {method}")
            return {"jsonrpc": "2.0", "id": message["id"], "result": result}
        except Exception as exc:
            return self._error(message["id"], -32000, str(exc))

    def _call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            if name == "list_labels":
                payload = self.store.list_labels()
            elif name == "search_memories":
                payload = self.store.search_memories(
                    label_query=args.get("label_query"),
                    status_filter=args.get("status_filter"),
                    text_query=args.get("text_query"),
                    limit=args.get("limit"),
                )
            elif name == "get_memory":
                payload = self.store.get_memory(args["id"])
            elif name == "get_memory_neighborhood":
                payload = self.store.get_memory_neighborhood(
                    args["id"],
                    depth=args.get("depth", 1),
                    max_nodes=args.get("max_nodes", 25),
                )
            elif name == "create_memory":
                payload = self.store.create_memory(args["memory"], args.get("related_label_query"))
            elif name == "update_memory":
                payload = self.store.update_memory(args["id"], args["patch"], args.get("related_label_query"))
            elif name == "add_label":
                payload = self.store.add_label(args["label"], args["description"])
            elif name == "delete_memory":
                payload = self.store.delete_memory(args["id"], args["confirm_exact_id"])
            else:
                raise StoreError(f"Unknown tool: {name}")
            return {"content": [{"type": "text", "text": json.dumps(payload, indent=2, ensure_ascii=True)}]}
        except Exception as exc:
            return {"isError": True, "content": [{"type": "text", "text": str(exc)}]}

    @staticmethod
    def _error(message_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


def read_message() -> dict[str, Any] | None:
    first = sys.stdin.buffer.readline()
    if not first:
        return None
    if first.startswith(b"{"):
        return json.loads(first.decode("utf-8"))
    headers: dict[str, str] = {}
    line = first
    while line and line not in (b"\r\n", b"\n"):
        text = line.decode("ascii").strip()
        if ":" in text:
            key, value = text.split(":", 1)
            headers[key.lower()] = value.strip()
        line = sys.stdin.buffer.readline()
    length = int(headers.get("content-length", "0"))
    if length <= 0:
        return None
    body = sys.stdin.buffer.read(length)
    return json.loads(body.decode("utf-8"))


def write_message(message: dict[str, Any]) -> None:
    body = json.dumps(message, separators=(",", ":"), ensure_ascii=True)
    sys.stdout.buffer.write((body + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def run_server(root: Path | str | None = None) -> int:
    store = MemoryStore(root)
    if not store.memory_root.is_dir():
        print(
            f"project-memory-mcp: no {STORE_DIR_NAME} store found at {store.root}; "
            "tools will fail until you run 'project-memory-mcp init'.",
            file=sys.stderr,
        )
    server = McpServer(store)
    while True:
        message = read_message()
        if message is None:
            break
        response = server.handle(message)
        if response is not None:
            write_message(response)
    return 0
