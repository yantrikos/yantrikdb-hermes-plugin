"""``yantrikdb-hermes`` CLI — copy the plugin source into a Hermes install.

Hermes loads memory plugins from ``$HERMES_ROOT/plugins/memory/<name>/``.
Pip alone can't drop files there because Hermes' plugin discovery is
filesystem-based, not import-path-based. This CLI bridges the gap:

    pip install yantrikdb-hermes-plugin
    yantrikdb-hermes install ~/hermes-agent

After installation, configure Hermes via ``$HERMES_HOME/.env``:

    YANTRIKDB_MODE=embedded
    YANTRIKDB_DB_PATH=~/.hermes/yantrikdb-memory.db
    YANTRIKDB_NAMESPACE=hermes

Then ``hermes memory status`` should show the plugin as available.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path


def _plugin_source_dir() -> Path:
    """Return the directory of the installed yantrikdb_hermes_plugin package.

    pyproject.toml maps the on-disk ``yantrikdb/`` directory to the
    importable package name ``yantrikdb_hermes_plugin``. The plugin
    source lives in this package's directory; we copy it (renamed to
    ``yantrikdb``) into the user's Hermes install.
    """
    return Path(__file__).resolve().parent


def cmd_install(args: argparse.Namespace) -> int:
    hermes_root = Path(args.hermes_root).expanduser().resolve()
    plugins_memory = hermes_root / "plugins" / "memory"
    target = plugins_memory / "yantrikdb"

    if not plugins_memory.is_dir():
        print(
            f"error: {plugins_memory} does not exist — is {hermes_root} "
            "actually a Hermes Agent checkout?",
            file=sys.stderr,
        )
        return 2

    if target.exists():
        if not args.force:
            print(
                f"error: {target} already exists. Re-run with --force to overwrite, "
                "or remove it first.",
                file=sys.stderr,
            )
            return 3
        shutil.rmtree(target)

    src = _plugin_source_dir()
    target.mkdir(parents=True, exist_ok=False)
    for entry in src.iterdir():
        if entry.name == "cli.py":
            # The CLI is a packaging concern, not part of the plugin Hermes loads.
            continue
        if entry.name == "__pycache__":
            continue
        if entry.is_dir():
            shutil.copytree(entry, target / entry.name)
        else:
            shutil.copy2(entry, target / entry.name)

    # Use ASCII-only output so Windows cp1252 consoles don't choke.
    # Hermes' UI handles unicode fine; this is the bare CLI install path.
    print(f"installed yantrikdb plugin into {target}")
    print()
    print("Next steps:")
    print("  1. hermes config set memory.provider yantrikdb")
    print(f"  2. configure {hermes_root}/.env (or $HERMES_HOME/.env):")
    print("       YANTRIKDB_MODE=embedded")
    print("       YANTRIKDB_DB_PATH=~/.hermes/yantrikdb-memory.db")
    print("       YANTRIKDB_NAMESPACE=hermes")
    print("  3. hermes memory status     # should show: Status: available [OK]")
    print()
    print("Skills (opt-in, v0.3.0+): set YANTRIKDB_SKILLS_ENABLED=true")
    return 0


def cmd_path(args: argparse.Namespace) -> int:
    """Print the on-disk path of the installed plugin source.

    Useful for users who'd rather symlink than copy:
        ln -s "$(yantrikdb-hermes path)" ~/hermes-agent/plugins/memory/yantrikdb
    """
    print(_plugin_source_dir())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="yantrikdb-hermes",
        description=(
            "Manage the YantrikDB Hermes memory plugin installed via pip. "
            "Hermes loads plugins from $HERMES_ROOT/plugins/memory/, so this "
            "CLI bridges the pip → filesystem gap."
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_install = sub.add_parser(
        "install",
        help="copy the plugin source into a Hermes Agent checkout",
    )
    p_install.add_argument(
        "hermes_root",
        help="path to your hermes-agent checkout (the directory containing 'plugins/')",
    )
    p_install.add_argument(
        "-f", "--force",
        action="store_true",
        help="overwrite an existing plugins/memory/yantrikdb/ directory",
    )
    p_install.set_defaults(func=cmd_install)

    p_path = sub.add_parser(
        "path",
        help="print the on-disk path of the installed plugin source",
    )
    p_path.set_defaults(func=cmd_path)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
