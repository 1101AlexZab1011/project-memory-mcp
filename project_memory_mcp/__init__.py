"""project-memory-mcp: file-based, git-friendly project memory for coding agents."""

from .store import MemoryStore, StoreError, find_store_root

__version__ = "0.1.0"

__all__ = ["MemoryStore", "StoreError", "find_store_root", "__version__"]
