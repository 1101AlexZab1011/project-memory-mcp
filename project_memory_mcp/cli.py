"""Command-line interface: init, validate, serve, install-skills, migrate, backup."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

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
        delete_superseded=args.delete_superseded or None,
    )
    if args.apply:
        # Applying is the only step here that moves anything, and the report is
        # cheap, so the operator reads what will happen before it happens.
        preview = run_audit(store, policy=policy, apply=False, record=False)
        print(format_report(preview, limit=args.limit))
        if not preview.due:
            print("\nNothing to apply.")
            store.close()
            return 0
        if not args.yes:
            print(f"\nRe-run with --yes to apply this to '{args.project}'.", file=sys.stderr)
            store.close()
            return 1
        print()

    try:
        report = run_audit(store, policy=policy, apply=args.apply, record=not args.no_record)
    finally:
        store.close()

    print(format_report(report, limit=args.limit))
    if report.run_id is not None:
        tail = "changes applied" if report.applied else "nothing was changed"
        print(f"\nrecorded as run {report.run_id}; {tail}")
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
                                   ui_enabled=not args.no_ui,
                                   compute_interval=args.compute_interval)
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


DEFAULT_HOME = Path.home() / ".project-memory"


def _slugify(name: str) -> str:
    """A directory name turned into a valid project id."""
    out, dash = [], False
    for char in name.lower():
        if char.isalnum():
            out.append(char)
            dash = False
        elif out and not dash:
            out.append("-")
            dash = True
    return "".join(out).strip("-") or "project"


def _merge_mcp_json(path: Path, entry: dict[str, Any]) -> str:
    """Add our server to .mcp.json without disturbing anything already there.

    Projects routinely configure several MCP servers. Overwriting the file to
    add one would silently remove the others, which is the kind of damage a
    setup command must never do.
    """
    config: dict[str, Any] = {}
    if path.is_file():
        try:
            config = json.loads(path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError:
            return f"left {path.name} alone: it is not valid JSON, add the server by hand"
    servers = config.setdefault("mcpServers", {})
    existing = servers.get("project-memory")
    if existing == entry:
        return f"{path.name} already points here"
    servers["project-memory"] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return f"{'updated' if existing else 'added project-memory to'} {path.name}"


def _toml_string(value: str) -> str:
    r"""Quote a value for TOML.

    Windows paths are the reason this exists: ``C:\Users\...`` inside a basic
    string is a string of invalid escapes, and the file will not parse.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _merge_codex_toml(path: Path, command: str, args: list[str]) -> str:
    """Same for Codex. Hand-written TOML: one table, appended if absent."""
    block = "\n".join([
        "[mcp_servers.project-memory]",
        f"command = {_toml_string(command)}",
        "args = [" + ", ".join(_toml_string(a) for a in args) + "]",
    ])
    text = path.read_text(encoding="utf-8-sig") if path.is_file() else ""
    if "[mcp_servers.project-memory]" in text:
        return f"left {path.name} alone: it already has a project-memory entry"
    path.parent.mkdir(parents=True, exist_ok=True)
    separator = "" if not text or text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
    path.write_text(text + separator + block + "\n", encoding="utf-8")
    return f"{'appended to' if text else 'created'} {path.name}"


def cmd_setup(args: argparse.Namespace) -> int:
    """Take a project from nothing to a working memory store.

    Everything here is local: a database under the user's home directory, the
    skills, and client config pointing at them. No server, no network, no token.
    A remote is something you add later if you ever want one.
    """
    from .sqlite_store import SqliteMemoryStore, StoreError

    root = Path(args.root).resolve() if args.root else Path.cwd()
    project = args.project or _slugify(root.name)
    database = Path(args.database).resolve() if args.database else DEFAULT_HOME / "memory.db"
    steps: list[str] = []

    try:
        store = SqliteMemoryStore(database, project)
    except StoreError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if store.list_labels()["labels"]:
        steps.append(f"project '{project}' already exists ({store.count()} memories)")
    else:
        seed = json.loads((TEMPLATES_ROOT / "labels.json").read_text(encoding="utf-8"))
        for label, data in sorted(seed["labels"].items()):
            store.add_label(label, data["description"])
        steps.append(f"created project '{project}' with {len(seed['labels'])} starter labels")
    store.close()

    targets = []
    if not args.codex_only:
        targets.append(root / ".claude" / "skills")
    if args.codex or args.codex_only:
        targets.append(root / ".agents" / "skills")
    for destination in targets:
        for skill_name in SKILL_NAMES:
            shutil.copytree(SKILLS_ROOT / skill_name, destination / skill_name, dirs_exist_ok=True)
        steps.append(f"installed {len(SKILL_NAMES)} skills into {destination.relative_to(root)}")

    serve_args = ["serve", "--database", str(database), "--project", project]
    if not args.codex_only:
        steps.append(_merge_mcp_json(root / ".mcp.json", {
            "type": "stdio", "command": "project-memory-mcp", "args": serve_args}))
    if args.codex or args.codex_only:
        steps.append(_merge_codex_toml(
            root / ".codex" / "config.toml", "project-memory-mcp", serve_args))

    print(f"project memory ready for {root}")
    for step in steps:
        print(f"  {step}")
    print(f"\ndatabase: {database}")
    print("Restart the agent to pick up the new configuration.")
    print("This store is local and needs no server. To share it later, run `serve --http` "
          "on one machine and point the others at it.")
    return 0


def cmd_enroll(args: argparse.Namespace) -> int:
    """Mint an enrollment code, or list and revoke clients. Run on the server."""
    import sqlite3

    from . import clients
    from .sqlite_store import SqliteMemoryStore
    from .validation import StoreError

    database = Path(args.database)
    if not database.is_file():
        print(f"error: no database at {database}", file=sys.stderr)
        return 1
    SqliteMemoryStore(database, "bootstrap", create=True).close()  # ensures the tables exist
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        if args.list:
            rows = clients.list_clients(connection)
            if not rows:
                print("No clients enrolled. The shared token is still the only credential.")
                return 0
            for row in rows:
                state = "revoked" if row["revoked_at"] else row["auth"]
                print(f"{row['name']:24s} {row['role']:12s} {state:8s} "
                      f"{row['fingerprint'] or '-'}  {row['client_id']}")
            return 0
        if args.revoke:
            print(clients.revoke(connection, args.revoke)["revoked"], "revoked")
            return 0

        result = clients.create_code(
            connection, name=args.name, role=args.role,
            projects=args.project or None)
    except StoreError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    finally:
        connection.close()

    print(f"enrollment code: {result['code']}")
    print(f"  role     {result['role']}")
    print(f"  projects {result['projects']}")
    print(f"  expires  in {result['valid_for_minutes']} minutes, single use")
    print("\nOn the other machine:")
    print(f"  project-memory-mcp join --server <url> --code {result['code']} --name <machine>")
    print("\nThe code is the only thing that has to travel. It is worthless once used,")
    print("and no secret is sent back - the client keeps its private key.")
    return 0


def cmd_join(args: argparse.Namespace) -> int:
    """Enroll this machine with a server, using a code. Run on the client."""
    import urllib.error
    import urllib.request

    from . import identity

    key_path = Path(args.key) if args.key else DEFAULT_HOME / "client_key.pem"
    try:
        private_key = identity.load_or_create(key_path)
    except identity.IdentityError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    public = identity.public_bytes(private_key)

    payload = json.dumps({
        "code": args.code,
        "name": args.name or Path.home().name,
        "public_key": identity.encode_public(public),
    }).encode()
    url = args.server.rstrip("/") + "/enroll"
    request = urllib.request.Request(url, data=payload,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")
        print(f"error: server refused enrollment: {detail}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"error: cannot reach {url}: {error}", file=sys.stderr)
        return 1

    print(f"enrolled with {args.server} as '{result['name']}' ({result['role']})")
    print(f"  fingerprint {identity.fingerprint(public)}")
    print(f"  private key {key_path}")
    print(f"  projects    {result['projects']}")
    print("\nThe private key never left this machine. The same key enrolled elsewhere")
    print("is verifiably the same client, which is how identity works across servers.")
    return 0


def cmd_compute(args: argparse.Namespace) -> int:
    """Run the memory maintenance worker.

    Inside `serve` this runs as a thread. As its own process it does the same
    work against the same database, on another machine if you like - which is
    the point once the jobs are heavy enough to matter: computation should not
    have to share an interpreter with request handling.
    """
    import time as _time

    from .computer import JOB_KINDS, Computer, Scheduler, make_job
    from .sqlite_store import SqliteMemoryStore

    database = Path(args.database)
    if not database.is_file():
        print(f"error: no database at {database}", file=sys.stderr)
        return 1

    import sqlite3

    connection = sqlite3.connect(database)
    try:
        projects = [args.project] if args.project else SqliteMemoryStore.list_projects(connection)
    finally:
        connection.close()
    if not projects:
        print("No projects in this database.", file=sys.stderr)
        return 1

    computer = Computer(open_store=lambda p: SqliteMemoryStore(database, p, create=False),
                        database=database)

    if args.once:
        for project in projects:
            for kind in (args.kind or list(JOB_KINDS)):
                result = computer.run_one(make_job(kind, project))
                print(f"{project:24s} {kind:8s} {result['outcome']:7s} {result['detail']}")
        return 0

    computer.start()
    scheduler = Scheduler(computer, lambda: projects, interval_seconds=args.interval,
                          kinds=tuple(args.kind) if args.kind else ("outbox", "audit", "dedup"))
    scheduler.start()
    print(f"project-memory-mcp computer: {len(projects)} project(s), every {scheduler.interval}s",
          file=sys.stderr)
    print("  jobs: " + ", ".join(sorted(JOB_KINDS)), file=sys.stderr)
    try:
        while True:
            _time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        scheduler.stop()
        computer.stop()
    return 0


def cmd_remote(args: argparse.Namespace) -> int:
    """Manage the servers this machine federates with. Zero of them is normal."""
    from . import federation
    from .sqlite_store import SqliteMemoryStore, StoreError

    store = SqliteMemoryStore(args.database, args.project, create=False)
    try:
        if args.remove:
            print(federation.remove_remote(store.connection, args.remove)["removed"], "removed")
            return 0
        if args.url:
            federation.add_remote(store.connection, args.name, args.url,
                                  description=args.description, token=args.token)
            print(f"remote '{args.name}' -> {args.url}")
            if not args.description:
                print("  no description set. Add one with --description: it is what an agent")
                print("  reads when deciding which server a memory belongs in.")
            return 0
        remotes = federation.list_remotes(store.connection)
        if not remotes:
            print("No remotes. This store is local-only, which is a complete setup.")
            return 0
        for remote in remotes:
            state = "" if remote.enabled else " (disabled)"
            print(f"{remote.name:16s} {remote.url}{state}")
            if remote.description:
                print(f"                 {remote.description}")
        return 0
    except StoreError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    finally:
        store.close()


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

    setup_parser = subparsers.add_parser(
        "setup", help="Set a project up from scratch: local database, skills, client config.")
    setup_parser.add_argument("--root", default=None, help="Project directory (default: current).")
    setup_parser.add_argument("--project", default=None,
                              help="Project id (default: derived from the directory name).")
    setup_parser.add_argument("--database", default=None,
                              help=f"Database path (default: {DEFAULT_HOME / 'memory.db'}).")
    setup_parser.add_argument("--codex", action="store_true", help="Also configure Codex.")
    setup_parser.add_argument("--codex-only", action="store_true", help="Configure Codex instead of Claude.")
    setup_parser.set_defaults(func=cmd_setup)

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
                              help="Carry out the verdicts. Prints the report first and "
                                   "requires --yes.")
    audit_parser.add_argument("--yes", action="store_true",
                              help="Confirm --apply after reading the report.")
    audit_parser.add_argument("--delete-superseded", action="store_true",
                              help="Also delete memories whose successor is still active. "
                                   "Bodies are kept in the revisions table.")
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
    serve_parser.add_argument("--compute-interval", type=int, default=3600,
                              help="Seconds between maintenance sweeps (default: 3600). "
                                   "0 disables the built-in worker, for when it runs separately.")
    serve_parser.add_argument("--token", default=None,
                              help="Shared bearer token. Falls back to PROJECT_MEMORY_TOKEN.")
    serve_parser.set_defaults(func=cmd_serve)

    enroll_parser = subparsers.add_parser(
        "enroll", help="Mint an enrollment code, or list and revoke clients. Run on the server.")
    enroll_parser.add_argument("--database", required=True, help="Path to the SQLite database.")
    enroll_parser.add_argument("--name", default=None, help="Suggested name for the new client.")
    enroll_parser.add_argument("--role", default="contributor", choices=("contributor", "admin"))
    enroll_parser.add_argument("--project", action="append", default=None,
                               help="Restrict to this project. Repeatable; default is every project.")
    enroll_parser.add_argument("--list", action="store_true", help="List enrolled clients instead.")
    enroll_parser.add_argument("--revoke", default=None, metavar="CLIENT_ID",
                               help="Revoke a client instead. Its writes keep their attribution.")
    enroll_parser.set_defaults(func=cmd_enroll)

    join_parser = subparsers.add_parser(
        "join", help="Enroll this machine with a server using a code. Run on the client.")
    join_parser.add_argument("--server", required=True, help="Base URL, e.g. http://host:8765")
    join_parser.add_argument("--code", required=True, help="Enrollment code from the server admin.")
    join_parser.add_argument("--name", default=None, help="How this machine should be listed.")
    join_parser.add_argument("--key", default=None,
                             help=f"Private key path (default: {DEFAULT_HOME / 'client_key.pem'}).")
    join_parser.set_defaults(func=cmd_join)

    compute_parser = subparsers.add_parser(
        "compute", help="Run the memory maintenance worker: tiering, archiving, outbox, dedup.")
    compute_parser.add_argument("--database", required=True, help="Path to the SQLite database.")
    compute_parser.add_argument("--project", default=None, help="One project (default: all).")
    compute_parser.add_argument("--kind", action="append", default=None,
                                choices=("audit", "outbox", "dedup"),
                                help="Run only this job kind. Repeatable.")
    compute_parser.add_argument("--interval", type=int, default=3600,
                                help="Seconds between sweeps (default: 3600).")
    compute_parser.add_argument("--once", action="store_true",
                                help="Run each job once and exit, printing what happened.")
    compute_parser.set_defaults(func=cmd_compute)

    remote_parser = subparsers.add_parser(
        "remote", help="Add, list or remove servers this machine federates with.")
    remote_parser.add_argument("--database", required=True, help="Path to the local database.")
    remote_parser.add_argument("--project", required=True, help="Project id.")
    remote_parser.add_argument("--name", default=None, help="Short name for the remote.")
    remote_parser.add_argument("--url", default=None, help="Base URL. Adds or updates the remote.")
    remote_parser.add_argument("--description", default=None,
                               help="What this server is for. Agents read it to route promotions.")
    remote_parser.add_argument("--token", default=None,
                               help="Bearer token, if this machine has no enrolled key there.")
    remote_parser.add_argument("--remove", default=None, metavar="NAME", help="Remove a remote.")
    remote_parser.set_defaults(func=cmd_remote)

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
