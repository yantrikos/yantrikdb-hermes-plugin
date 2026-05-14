"""``yantrikdb-hermes`` CLI — expose the pip-installed provider to Hermes.

Hermes discovers user-installed memory providers from
``$HERMES_HOME/plugins/<name>/``. Pip installs this package on Python's
import path, but Hermes' memory-provider discovery is filesystem-based, so
this CLI creates the small bridge:

    pip install yantrikdb-hermes-plugin
    yantrikdb-hermes install

By default the bridge is a tiny shim directory at
``$HERMES_HOME/plugins/yantrikdb`` that imports the pip-installed provider
package. This keeps package upgrades in site-packages while avoiding Hermes'
user-plugin namespace from breaking package-relative imports. Use ``--copy``
to install a physical copy instead.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

DEFAULT_PLUGIN_NAME = "yantrikdb"


def _plugin_source_dir() -> Path:
    """Return the directory of the installed yantrikdb_hermes_plugin package.

    pyproject.toml maps the on-disk ``yantrikdb/`` directory to the
    importable package name ``yantrikdb_hermes_plugin``. The provider source
    lives in this package's directory; Hermes should see it as a plugin named
    ``yantrikdb``.
    """
    return Path(__file__).resolve().parent


def _default_hermes_home() -> Path:
    """Return the Hermes home directory used for user-installed plugins."""
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser().resolve()


def _copy_provider(src: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=False)
    for entry in src.iterdir():
        if entry.name == "__pycache__":
            continue
        if entry.is_dir():
            shutil.copytree(entry, target / entry.name)
        else:
            shutil.copy2(entry, target / entry.name)


def _install_provider_shim(src: Path, target: Path) -> None:
    """Install a Hermes-discoverable shim for the pip package.

    Hermes loads user memory providers under an internal namespace
    (``_hermes_user_memory.<name>``). Loading the full pip package directory
    directly under that namespace breaks package-relative imports in the
    provider. The shim keeps Hermes discovery filesystem-based while importing
    the real provider through its normal package name.
    """
    target.mkdir(parents=True, exist_ok=False)
    (target / "__init__.py").write_text(
        '"""Hermes user-plugin shim for the pip-installed YantrikDB provider."""\n'
        "from yantrikdb_hermes_plugin import YantrikDBMemoryProvider\n\n\n"
        "def register(ctx):\n"
        "    ctx.register_memory_provider(YantrikDBMemoryProvider())\n",
        encoding="utf-8",
    )
    plugin_yaml = src / "plugin.yaml"
    if plugin_yaml.exists():
        shutil.copy2(plugin_yaml, target / "plugin.yaml")


def _replace_target(target: Path, *, force: bool) -> None:
    if not target.exists() and not target.is_symlink():
        return
    if not force:
        raise FileExistsError(
            f"{target} already exists. Re-run with --force to overwrite, "
            "or remove it first."
        )
    if target.is_symlink() or target.is_file():
        target.unlink()
    else:
        shutil.rmtree(target)


def _install_user_plugin(args: argparse.Namespace) -> int:
    hermes_home = Path(args.hermes_home).expanduser().resolve() if args.hermes_home else _default_hermes_home()
    plugins_dir = hermes_home / "plugins"
    target = plugins_dir / DEFAULT_PLUGIN_NAME
    src = _plugin_source_dir()

    plugins_dir.mkdir(parents=True, exist_ok=True)

    try:
        _replace_target(target, force=args.force)
    except FileExistsError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3

    if args.copy:
        _copy_provider(src, target)
        action = "copied"
    else:
        _install_provider_shim(src, target)
        action = "registered"

    print(f"{action} yantrikdb plugin into {target}")
    print()
    print("Next steps:")
    print("  1. hermes config set memory.provider yantrikdb")
    print(f"  2. configure {hermes_home}/.env if you want to override defaults:")
    print("       YANTRIKDB_MODE=embedded")
    print(f"       YANTRIKDB_DB_PATH={hermes_home}/yantrikdb-memory.db")
    print("       YANTRIKDB_NAMESPACE=hermes")
    print("  3. hermes memory status     # should show: Status: available [OK]")
    print()
    print("Skills (opt-in, v0.3.0+): set YANTRIKDB_SKILLS_ENABLED=true")
    return 0


def _install_legacy_source_tree(args: argparse.Namespace) -> int:
    """Backward-compatible install path for older Hermes checkouts/docs."""
    hermes_root = Path(args.hermes_root).expanduser().resolve()
    plugins_memory = hermes_root / "plugins" / "memory"
    target = plugins_memory / DEFAULT_PLUGIN_NAME

    if not plugins_memory.is_dir():
        print(
            f"error: {plugins_memory} does not exist — is {hermes_root} "
            "actually a Hermes Agent checkout?",
            file=sys.stderr,
        )
        return 2

    try:
        _replace_target(target, force=args.force)
    except FileExistsError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3

    _copy_provider(_plugin_source_dir(), target)
    print(f"copied yantrikdb plugin into {target}")
    print()
    print("Next steps:")
    print("  1. hermes config set memory.provider yantrikdb")
    print("  2. hermes memory status     # should show: Status: available [OK]")
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    if args.hermes_root:
        return _install_legacy_source_tree(args)
    return _install_user_plugin(args)


def cmd_uninstall(args: argparse.Namespace) -> int:
    """Remove the Hermes user-plugin registration created by install."""
    hermes_home = Path(args.hermes_home).expanduser().resolve() if args.hermes_home else _default_hermes_home()
    target = hermes_home / "plugins" / DEFAULT_PLUGIN_NAME

    if not target.exists() and not target.is_symlink():
        print(f"yantrikdb plugin registration not found at {target}")
        return 0

    if target.is_symlink() or target.is_file():
        target.unlink()
    else:
        shutil.rmtree(target)

    print(f"removed yantrikdb plugin registration from {target}")
    print()
    print("Next steps:")
    print("  1. If YantrikDB is the active provider, choose another provider:")
    print("       hermes memory setup")
    print("     or disable the external provider:")
    print("       hermes config set memory.provider null")
    print("  2. Optionally uninstall the pip package from the Hermes environment:")
    print("       pip uninstall yantrikdb-hermes-plugin yantrikdb")
    print("  3. Restart Hermes if it is running as a gateway/service:")
    print("       hermes gateway restart")
    return 0


def cmd_path(args: argparse.Namespace) -> int:
    """Print the on-disk path of the installed provider source.

    Useful for users who would rather create their own symlink:
        ln -s "$(yantrikdb-hermes path)" "$HERMES_HOME/plugins/yantrikdb"
    """
    print(_plugin_source_dir())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="yantrikdb-hermes",
        description=(
            "Manage the YantrikDB Hermes memory provider installed via pip. "
            "Hermes loads memory providers from $HERMES_HOME/plugins/, so this "
            "CLI bridges the pip package to Hermes' filesystem discovery."
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_install = sub.add_parser(
        "install",
        help="register the pip-installed provider with Hermes",
    )
    p_install.add_argument(
        "hermes_root",
        nargs="?",
        help=(
            "deprecated: path to a Hermes Agent checkout. If supplied, the "
            "provider is copied into <hermes_root>/plugins/memory/yantrikdb. "
            "Omit this argument to install as a user plugin under $HERMES_HOME/plugins/."
        ),
    )
    p_install.add_argument(
        "--hermes-home",
        type=str,
        default=None,
        help="Hermes home directory for user-plugin install (default: $HERMES_HOME or ~/.hermes)",
    )
    p_install.add_argument(
        "--copy",
        action="store_true",
        help="copy the full provider package instead of creating the default lightweight shim",
    )
    p_install.add_argument(
        "-f", "--force",
        action="store_true",
        help="overwrite an existing yantrikdb provider directory or symlink",
    )
    p_install.set_defaults(func=cmd_install)

    p_uninstall = sub.add_parser(
        "uninstall",
        help="remove the Hermes user-plugin registration",
    )
    p_uninstall.add_argument(
        "--hermes-home",
        type=str,
        default=None,
        help="Hermes home directory for user-plugin uninstall (default: $HERMES_HOME or ~/.hermes)",
    )
    p_uninstall.set_defaults(func=cmd_uninstall)

    p_path = sub.add_parser(
        "path",
        help="print the on-disk path of the installed provider source",
    )
    p_path.set_defaults(func=cmd_path)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
