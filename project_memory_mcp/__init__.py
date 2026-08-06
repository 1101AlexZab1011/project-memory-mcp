"""project-memory-mcp: shared, database-backed project memory for coding agents."""

from .sqlite_store import SqliteMemoryStore
from .validation import StoreError

__version__ = "0.7.0"

__all__ = ["SqliteMemoryStore", "StoreError", "__version__"]
