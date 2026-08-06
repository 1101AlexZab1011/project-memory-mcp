"""Command-line interface: init, validate, serve, install-skills, migrate, backup."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from . import __version__

PACKAGE_ROOT = Path(__file__).resolve().parent
TEMPLATES_ROOT = PACKAGE_ROOT / "templates"
SKILLS_ROOT = PACKAGE_ROOT / "skills"

SKILL_NAMES = ("project-memory-recall", "project-memory-remember", "project-memory-forget")


def cmd_init(args: argparse.Namespace) -> int:
    """Create a project in a database and seed its label registry.

    A project otherwise does not exist until its first memory is written, which
    means it is absent from the server's project list and cannot be served.
    """
    from .sqlite_store import SqliteMemoryStore, StoreError

    try:
        store = SqliteMemoryStore(args.database, args.project)
    except StoreError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    existing = store.list_labels()["labels"]
    if existing and not args.force:
        print(f"kept {len(existing)} existing label(s)")
    else:
        seed = json.loads((TEMPLATES_ROOT / "labels.json").read_text(encoding="utf-8"))
        for label, data in sorted(seed["labels"].items()):
            store.add_label(label, data["description"])
        print(f"seeded {len(seed['labels'])} labels")

    print(f"Project '{args.project}' ready in {store.path} ({store.count()} memories)")
    store.close()
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    """Report which memories have earned their place. Changes nothing."""
    from .audit import AuditPolicy, format_report, run_audit, with_overrides
    from .sqlite_store import SqliteMemoryStore, StoreError

    try:
        store = SqliteMemoryStore(args.database, args.project, create=False)
    except StoreError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    policy = with_overrides(
        AuditPolicy(),
        min_surfaced_direct=args.min_direct,
        min_applied=args.min_applied,
        min_spread_days=args.min_spread_days,
        min_degree=args.min_degree,
        max_actions_per_run=args.max_actions,
    )
    try:
        report = run_audit(store, policy=policy, apply=args.apply, record=not args.no_record)
    except NotImplementedError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    finally:
        store.close()

    print(format_report(report, limit=args.limit))
    if report.run_id is not None:
        print(f"\nrecorded as run {report.run_id}; nothing was changed")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    """Re-check every stored memory against the schema.

    Writes are validated already, so this catches drift from a migration, a
    restore, or an edit made outside the tools - not ordinary use.
    """
    from .sqlite_store import SqliteMemoryStore, StoreError

    try:
        store = SqliteMemoryStore(args.database, args.project, create=False)
    except StoreError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    errors = store.validate_store()
    count = store.count()
    store.close()
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"Project memory validation passed. Memories: {count}")
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
            return run_http_server(args.database, args.bind, args.port, token,
                               ui_enabled=not args.no_ui)
        finally:
            if scheduler is not None:
                scheduler.stop()

    if not args.database or not args.project:
        print("error: stdio serving requires --database and --project.", file=sys.stderr)
        return 1
    from .sqlite_store import SqliteMemoryStore

    store = SqliteMemoryStore(args.database, args.project)
    print(f"project-memory-mcp: serving project '{args.project}' from {store.path} "
          f"({store.count()} memories)", file=sys.stderr)
    return run_server(store)


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
        description="Shared, database-backed project memory for coding agents, served over MCP.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Create a project in a database and seed its labels.")
    init_parser.add_argument("--database", required=True, help="Path to the SQLite database.")
    init_parser.add_argument("--project", required=True, help="Project id (lowercase kebab-case).")
    init_parser.add_argument("--force", action="store_true", help="Re-seed labels even if some already exist.")
    init_parser.set_defaults(func=cmd_init)

    validate_parser = subparsers.add_parser("validate", help="Re-check every stored memory against the schema.")
    validate_parser.add_argument("--database", required=True, help="Path to the SQLite database.")
    validate_parser.add_argument("--project", required=True, help="Project id inside the database.")
    validate_parser.set_defaults(func=cmd_validate)

    audit_parser = subparsers.add_parser(
        "audit", help="Report which memories have earned their place. Changes nothing.")
    audit_parser.add_argument("--database", required=True, help="Path to the SQLite database.")
    audit_parser.add_argument("--project", required=True, help="Project id inside the database.")
    audit_parser.add_argument("--limit", type=int, default=20, help="Findings to print per verdict.")
    audit_parser.add_argument("--no-record", action="store_true",
                              help="Print the report without storing the run.")
    audit_parser.add_argument("--apply", action="store_true",
                              help="Not implemented: acting on verdicts is a later phase.")
    # Thresholds are deliberately overridable. A store of 200 memories in a solo
    # repo and one of three million share no distribution, so no default here
    # should be trusted without looking at what it would do first.
    audit_parser.add_argument("--min-direct", type=int, default=None,
                              help="Direct query matches needed to survive a gate (default 1).")
    audit_parser.add_argument("--min-applied", type=int, default=None,
                              help="Reported applications needed to survive a gate (default 1).")
    audit_parser.add_argument("--min-spread-days", type=int, default=None,
                              help="Distinct recall days needed to survive a gate (default 2).")
    audit_parser.add_argument("--min-degree", type=int, default=None,
                              help="Incoming authored links needed to survive a gate (default 2).")
    audit_parser.add_argument("--max-actions", type=int, default=None,
                              help="Ceiling on memories one run may act on (default 50).")
    audit_parser.set_defaults(func=cmd_audit)

    serve_parser = subparsers.add_parser("serve", help="Run the MCP server over stdio or HTTP.")
    serve_parser.add_argument("--database", default=None, help="Path to the SQLite database.")
    serve_parser.add_argument("--project", default=None,
                              help="Project id inside the database (required over stdio).")
    serve_parser.add_argument("--http", action="store_true",
                              help="Serve MCP over HTTP so other devices can connect, instead of stdio.")
    serve_parser.add_argument("--bind", default=None,
                              help="Interface address to listen on. Required with --http; no default.")
    serve_parser.add_argument("--port", type=int, default=8765, help="Port to listen on (default: 8765).")
    serve_parser.add_argument("--no-ui", action="store_true",
                              help="Disable the browser management UI; serve MCP only.")
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
