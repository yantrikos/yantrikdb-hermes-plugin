"""Tests for the yantrikdb-hermes installer CLI."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

CLI_PATH = Path(__file__).resolve().parents[1] / "yantrikdb" / "cli.py"


def _load_cli():
    spec = importlib.util.spec_from_file_location("yantrikdb_cli_under_test", CLI_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows pathlib symlink_to requires admin/developer-mode; --copy is the documented path on Windows.",
)
def test_install_defaults_to_user_plugin_symlink(tmp_path, capsys):
    cli = _load_cli()
    hermes_home = tmp_path / "home"

    rc = cli.main(["install", "--hermes-home", str(hermes_home)])

    assert rc == 0
    target = hermes_home / "plugins" / "yantrikdb"
    assert target.is_symlink()
    assert target.resolve() == CLI_PATH.parent.resolve()
    out = capsys.readouterr().out
    assert "linked yantrikdb plugin into" in out
    assert "hermes config set memory.provider yantrikdb" in out


def test_install_copy_mode_creates_user_plugin_directory(tmp_path):
    cli = _load_cli()
    hermes_home = tmp_path / "home"

    rc = cli.main(["install", "--hermes-home", str(hermes_home), "--copy"])

    assert rc == 0
    target = hermes_home / "plugins" / "yantrikdb"
    assert target.is_dir()
    assert not target.is_symlink()
    assert (target / "__init__.py").exists()
    assert (target / "plugin.yaml").exists()


def test_install_refuses_existing_target_without_force(tmp_path, capsys):
    cli = _load_cli()
    hermes_home = tmp_path / "home"
    target = hermes_home / "plugins" / "yantrikdb"
    target.mkdir(parents=True)

    rc = cli.main(["install", "--hermes-home", str(hermes_home)])

    assert rc == 3
    err = capsys.readouterr().err
    assert "already exists" in err
    assert "--force" in err


def test_legacy_positional_path_still_installs_into_checkout(tmp_path):
    cli = _load_cli()
    hermes_root = tmp_path / "hermes-agent"
    (hermes_root / "plugins" / "memory").mkdir(parents=True)

    rc = cli.main(["install", str(hermes_root)])

    assert rc == 0
    target = hermes_root / "plugins" / "memory" / "yantrikdb"
    assert target.is_dir()
    assert (target / "__init__.py").exists()
    assert (target / "plugin.yaml").exists()
