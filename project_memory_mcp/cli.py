"""Command-line interface: init, validate, serve, install-skills."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from . import __version__
from .store import STORE_DIR_NAME, MemoryStore, find_store_root

PACKAGE_ROOT = Path(__file__).resolve().parent
TEMPLATES_ROOT = PACKAGE_ROOT / "templates"
SKILLS_ROOT = PACKAGE_ROOT / "skills"

SKILL_NAMES = ("project-memory-recall", "project-memory-remember", "project-memory-forget")

# template file in this package -> file name inside .project-memory/
STORE_TEMPLATES = {
    "labels.json": "labels.json",
    "memory.schema.json": "memory.schema.json",
    "STORE_README.md": "README.md",
}


def _resolve_root(root_arg: str | None, require_store: bool) -> Path | None:
    if root_arg:
        root = Path(root_arg).resolve()
        if require_store and not (root / STORE_DIR_NAME).is_dir():
            print(f"error: no {STORE_DIR_NAME} store in {root}. Run 'project-memory-mcp init' first.", file=sys.stderr)
            return None
        return root
    root = find_store_root()
    if root is None:
        if require_store:
            print(
                f"error: no {STORE_DIR_NAME} store found at or above {Path.cwd()}. "
                "Run 'project-memory-mcp init' first, or pass --root.",
                file=sys.stderr,
            )
            return None
        return Path.cwd()
    return root


def cmd_init(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve() if args.root else Path.cwd()
    store_dir = root / STORE_DIR_NAME
    active_dir = store_dir / "active"
    active_dir.mkdir(parents=True, exist_ok=True)
    # Git does not track empty directories, so a store committed before its
    # first memory is written would arrive at the next clone without active/
    # and fail validation. Keep the directory alive with a placeholder.
    gitkeep = active_dir / ".gitkeep"
    if not gitkeep.exists():
        gitkeep.write_text("", encoding="utf-8")
    # usage.json is local telemetry, not part of the shared store: it records
    # what this machine retrieved, which is noise in everyone else's diff.
    store_ignore = store_dir / ".gitignore"
    if not store_ignore.exists():
        store_ignore.write_text("usage.json\n", encoding="utf-8")

    for template_name, target_name in STORE_TEMPLATES.items():
        target = store_dir / target_name
        if target.exists() and not args.force:
            print(f"kept existing {target.relative_to(root)}")
            continue
        shutil.copyfile(TEMPLATES_ROOT / template_name, target)
        print(f"wrote {target.relative_to(root)}")

    store = MemoryStore(root)
    errors = store.validate_store()
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"Initialized project memory store in {store_dir}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    root = _resolve_root(args.root, require_store=True)
    if root is None:
        return 1
    store = MemoryStore(root)
    errors = store.validate_store()
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    count = len(list(store.active_root.glob("*.json"))) if store.active_root.is_dir() else 0
    print(f"Project memory validation passed. Memories: {count}")
    legacy_index = store.memory_root / "INDEX.json"
    if legacy_index.is_file():
        # Stores created before 0.3.0 carry a generated index that nothing reads
        # any more. Say so once rather than deleting a file we did not write.
        print(
            f"note: {legacy_index.relative_to(root)} is obsolete as of 0.3.0 and is no longer "
            "read or updated. It is safe to delete.",
            file=sys.stderr,
        )
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import run_server

    if args.http:
        if not args.database:
            print("error: --http requires --database.", file=sys.stderr)
            return 1
        if not args.bind:
            # No default on purpose: 0.0.0.0 would publish the store on every
            # network the host touches, which is a decision, not a convenience.
            print("error: --bind is required with --http. Use 127.0.0.1 for this machine only, "
                  "or the address of one interface (for example a VPN adapter) to reach other "
                  "devices. Passing 0.0.0.0 exposes the store on every network this host is on.",
                  file=sys.stderr)
            return 1
        token = args.token or os.environ.get("PROJECT_MEMORY_TOKEN")
        if not token:
            print("error: a token is required. Pass --token or set PROJECT_MEMORY_TOKEN.", file=sys.stderr)
            return 1
        from .backup import BackupScheduler
        from .http_server import run_http_server

        scheduler = None
        if args.backup_dir:
            scheduler = BackupScheduler(args.database, args.backup_dir, args.backup_interval, args.backup_keep)
            scheduler.start()
            print(f"project-memory-mcp: snapshotting to {args.backup_dir} every "
                  f"{scheduler.interval}s, keeping {args.backup_keep}", file=sys.stderr)
        else:
            print("project-memory-mcp: WARNING no --backup-dir. Leaving git means every clone "
                  "stopped being a replica, so an unbacked failure loses the whole store.",
                  file=sys.stderr)
        try:
            return run_http_server(args.database, args.bind, args.port, token)
        finally:
            if scheduler is not None:
                scheduler.stop()

    if args.database:
        if not args.project:
            print("error: --project is required with --database.", file=sys.stderr)
            return 1
        from .sqlite_store import SqliteMemoryStore

        store = SqliteMemoryStore(args.database, args.project)
        print(f"project-memory-mcp: serving project '{args.project}' from {store.path} "
              f"({store.count()} memories)", file=sys.stderr)
        return run_server(store=store)
    root = _resolve_root(args.root, require_store=False)
    return run_server(root)


def cmd_install_skills(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve() if args.root else Path.cwd()
    destinations: list[Path] = []
    if args.claude:
        destinations.append(root / ".claude" / "skills")
    if args.codex:
        destinations.append(root / ".agents" / "skills")
    for dest in args.dest or []:
        destinations.append(Path(dest).resolve())
    if not destinations:
        destinations.append(root / ".claude" / "skills")

    for destination in destinations:
        for skill_name in SKILL_NAMES:
            target = destination / skill_name
            shutil.copytree(SKILLS_ROOT / skill_name, target, dirs_exist_ok=True)
        print(f"Installed {len(SKILL_NAMES)} skills into {destination}")
    return 0



def cmd_migrate(args: argparse.Namespace) -> int:
    from .sqlite_store import migrate_from_files

    source = Path(args.source).resolve()
    result = migrate_from_files(args.database, args.project, source)
    print(f"Imported {result['imported']} memories and {result['labels']} labels "
          f"into project '{result['project']}' at {result['database']}")
    for name in result["skipped"]:
        print(f"skipped (not a memory): {name}", file=sys.stderr)
    if result["errors"]:
        for error in result["errors"]:
            print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"The source store at {source} was left untouched.")
    return 0



def cmd_backup(args: argparse.Namespace) -> int:
    from .backup import export_json, snapshot_database

    if args.format == "json":
        result = export_json(args.database, args.out, args.project)
        print(f"Wrote {result['written']}")
        for name, count in result["projects"].items():
            print(f"  {name}: {count} memories")
        return 0
    target = snapshot_database(args.database, args.out, args.keep)
    print(f"Wrote {target} ({target.stat().st_size / 1024:.0f} KB); keeping the newest {args.keep}")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    from .backup import import_json

    result = import_json(args.database, args.source)
    print(f"Restored into {result['database']}")
    for name, count in result["projects"].items():
        print(f"  {name}: {count} memories")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="project-memory-mcp",
        description="File-based, git-friendly project memory for coding agents, served over MCP.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help=f"Scaffold a {STORE_DIR_NAME}/ store in a project.")
    init_parser.add_argument("--root", default=None, help="Project root to initialize (default: current directory).")
    init_parser.add_argument("--force", action="store_true", help="Overwrite existing store template files.")
    init_parser.set_defaults(func=cmd_init)

    validate_parser = subparsers.add_parser("validate", help="Validate the store.")
    validate_parser.add_argument("--root", default=None, help="Project root (default: search upward from cwd).")
    validate_parser.set_defaults(func=cmd_validate)

    serve_parser = subparsers.add_parser("serve", help="Run the stdio MCP server.")
    serve_parser.add_argument("--root", default=None, help="Project root (default: search upward from cwd).")
    serve_parser.add_argument("--database", default=None,
                              help="Serve from a SQLite database instead of memory files.")
    serve_parser.add_argument("--project", default=None,
                              help="Project id inside the database (required with --database over stdio).")
    serve_parser.add_argument("--http", action="store_true",
                              help="Serve MCP over HTTP so other devices can connect, instead of stdio.")
    serve_parser.add_argument("--bind", default=None,
                              help="Interface address to listen on. Required with --http; no default.")
    serve_parser.add_argument("--port", type=int, default=8765, help="Port to listen on (default: 8765).")
    serve_parser.add_argument("--backup-dir", default=None,
                              help="Snapshot the database into this directory while serving.")
    serve_parser.add_argument("--backup-interval", type=int, default=3600,
                              help="Seconds between snapshots (default: 3600, minimum 60).")
    serve_parser.add_argument("--backup-keep", type=int, default=7,
                              help="How many snapshots to retain (default: 7).")
    serve_parser.add_argument("--token", default=None,
                              help="Shared bearer token. Falls back to PROJECT_MEMORY_TOKEN.")
    serve_parser.set_defaults(func=cmd_serve)

    skills_parser = subparsers.add_parser(
        "install-skills",
        help="Copy the project-memory agent skills into a project (default: .claude/skills/).",
    )
    skills_parser.add_argument("--root", default=None, help="Project root (default: current directory).")
    skills_parser.add_argument("--claude", action="store_true", help="Install into <root>/.claude/skills/.")
    skills_parser.add_argument("--codex", action="store_true", help="Install into <root>/.agents/skills/.")
    skills_parser.add_argument("--dest", action="append", help="Install into a custom skills directory (repeatable).")
    skills_parser.set_defaults(func=cmd_install_skills)

    migrate_parser = subparsers.add_parser(
        "migrate", help="Import a file-backed .project-memory store into a SQLite database.")
    migrate_parser.add_argument("--from", dest="source", required=True, help="Path to a .project-memory directory.")
    migrate_parser.add_argument("--project", required=True, help="Project id to import into (lowercase kebab-case).")
    migrate_parser.add_argument("--database", required=True, help="Path to the SQLite database file.")
    migrate_parser.set_defaults(func=cmd_migrate)

    backup_parser = subparsers.add_parser("backup", help="Snapshot or export a database-backed store.")
    backup_parser.add_argument("--database", required=True, help="Path to the SQLite database file.")
    backup_parser.add_argument("--out", required=True,
                               help="Destination directory (db snapshots) or file path (json export).")
    backup_parser.add_argument("--format", choices=("db", "json"), default="db",
                               help="'db' is a byte-exact snapshot; 'json' is a portable export.")
    backup_parser.add_argument("--project", default=None, help="Export one project only (json format).")
    backup_parser.add_argument("--keep", type=int, default=7, help="How many db snapshots to retain.")
    backup_parser.set_defaults(func=cmd_backup)

    restore_parser = subparsers.add_parser("restore", help="Restore a JSON export into a database.")
    restore_parser.add_argument("--database", required=True, help="Path to the SQLite database file.")
    restore_parser.add_argument("--from", dest="source", required=True, help="Path to a JSON export.")
    restore_parser.set_defaults(func=cmd_restore)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
