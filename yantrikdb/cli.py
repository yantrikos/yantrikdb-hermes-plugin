"""``yantrikdb-hermes`` CLI — expose the pip-installed provider to Hermes.

Hermes discovers user-installed memory providers from
``$HERMES_HOME/plugins/<name>/``. Pip installs this package on Python's
import path, but Hermes' memory-provider discovery is filesystem-based, so
this CLI creates the small bridge:

    pip install yantrikdb-hermes-plugin
    yantrikdb-hermes install

By default the bridge is a symlink from ``$HERMES_HOME/plugins/yantrikdb``
to this pip-installed provider package, so package upgrades are picked up
without copying files again. Use ``--copy`` on platforms where symlinks are
not desirable.
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
        try:
            target.symlink_to(src, target_is_directory=True)
            action = "linked"
        except OSError as e:
            # Windows raises OSError when symlinks require admin or
            # developer-mode (the default on stock Windows). Give the user
            # an actionable next step instead of a bare stack trace.
            if sys.platform == "win32":
                print(
                    f"error: could not create symlink at {target}: {e}\n"
                    "Windows requires admin or developer-mode for symlinks. "
                    "Re-run with --copy to install a physical copy instead:\n"
                    f"  yantrikdb-hermes install --hermes-home {hermes_home} --copy",
                    file=sys.stderr,
                )
                return 4
            raise

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
        help="copy files instead of creating a symlink for the user-plugin install",
    )
    p_install.add_argument(
        "-f", "--force",
        action="store_true",
        help="overwrite an existing yantrikdb provider directory or symlink",
    )
    p_install.set_defaults(func=cmd_install)

    p_path = sub.add_parser(
        "path",
        help="print the on-disk path of the installed provider source",
    )
    p_path.set_defaults(func=cmd_path)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
