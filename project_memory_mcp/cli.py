"""Command-line interface: init, validate, serve, install-skills."""

from __future__ import annotations

import argparse
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
    (store_dir / "active").mkdir(parents=True, exist_ok=True)

    for template_name, target_name in STORE_TEMPLATES.items():
        target = store_dir / target_name
        if target.exists() and not args.force:
            print(f"kept existing {target.relative_to(root)}")
            continue
        shutil.copyfile(TEMPLATES_ROOT / template_name, target)
        print(f"wrote {target.relative_to(root)}")

    store = MemoryStore(root)
    store.regenerate_index()
    print(f"wrote {store.index_path.relative_to(root)}")

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
    if args.fix_index:
        # Validate content first so a broken store never silently overwrites the index.
        errors = store.validate_store(check_index=False)
        if not errors:
            store.regenerate_index()
            print(f"Regenerated {store.index_path}")
    errors = store.validate_store()
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    count = len(list(store.active_root.glob("*.json"))) if store.active_root.is_dir() else 0
    print(f"Project memory validation passed. Memories: {count}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import run_server

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

    validate_parser = subparsers.add_parser("validate", help="Validate the store and optionally regenerate the index.")
    validate_parser.add_argument("--root", default=None, help="Project root (default: search upward from cwd).")
    validate_parser.add_argument("--fix-index", action="store_true", help="Regenerate INDEX.json from memory files.")
    validate_parser.set_defaults(func=cmd_validate)

    serve_parser = subparsers.add_parser("serve", help="Run the stdio MCP server.")
    serve_parser.add_argument("--root", default=None, help="Project root (default: search upward from cwd).")
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

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
