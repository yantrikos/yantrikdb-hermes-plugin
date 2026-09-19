"""YANTRIKDB_* settings resolve through Hermes' profile secret scope (#89).

The fake below models ``agent.secret_scope.get_secret`` in two Hermes
versions. Both agree that with no scope, multiplexing raises and a
single-profile process reads os.environ, and that under multiplexing a scope
miss returns the default. They differ on a scope miss WITHOUT multiplexing:
hermes-agent main (35f69a9) falls through to os.environ, while the released
0.19.0 treats the scope as authoritative and returns the default. 0.19.0
installs a scope around every cron job, so that difference is live.
"""

from __future__ import annotations

import logging
import os
import sys
import types

import pytest


def _install_secret_scope(
    monkeypatch,
    scope: dict[str, str] | None,
    *,
    multiplex: bool = True,
    hermes: str = "main",
):
    mod = types.ModuleType("agent.secret_scope")

    class UnscopedSecretError(RuntimeError):
        pass

    def get_secret(name: str, default: str | None = None) -> str | None:
        if scope is not None:
            val = scope.get(name)
            if val is not None:
                return val
            if multiplex or hermes == "0.19.0":
                return default
            return os.environ.get(name, default)
        if multiplex:
            raise UnscopedSecretError(name)
        return os.environ.get(name, default)

    mod.UnscopedSecretError = UnscopedSecretError
    mod.get_secret = get_secret
    mod.current_secret_scope = lambda: scope
    mod.is_multiplex_active = lambda: multiplex
    monkeypatch.setitem(sys.modules, "agent.secret_scope", mod)
    return UnscopedSecretError


@pytest.fixture(autouse=True)
def _fresh_warnings(client_module, monkeypatch):
    monkeypatch.setattr(client_module, "_IGNORED_ENV_WARNED", set(), raising=False)


def test_multiplex_scope_beats_launch_profile_env(client_module, monkeypatch):
    monkeypatch.setenv("YANTRIKDB_MODE", "http")
    monkeypatch.setenv("YANTRIKDB_TOKEN", "launch-token")
    monkeypatch.setenv("YANTRIKDB_DB_PATH", "/tmp/launch.db")
    monkeypatch.setenv("YANTRIKDB_NAMESPACE", "launch")

    _install_secret_scope(
        monkeypatch,
        {
            "YANTRIKDB_MODE": "http",
            "YANTRIKDB_TOKEN": "profile-b-token",
            "YANTRIKDB_DB_PATH": "/tmp/profile-b.db",
            "YANTRIKDB_NAMESPACE": "profile-b",
        },
    )

    cfg = client_module.YantrikDBConfig.from_env()

    assert cfg.mode == "http"
    assert cfg.token == "profile-b-token"
    assert cfg.db_path == "/tmp/profile-b.db"
    assert cfg.namespace == "profile-b"
    assert "launch" not in {cfg.token, cfg.db_path, cfg.namespace}


def test_secondary_profile_does_not_inherit_launch_settings(client_module, monkeypatch):
    monkeypatch.setenv("YANTRIKDB_MODE", "http")
    monkeypatch.setenv("YANTRIKDB_TOKEN", "launch-token")
    monkeypatch.setenv("YANTRIKDB_NAMESPACE", "launch")
    _install_secret_scope(monkeypatch, {"OPENAI_API_KEY": "profile-b-key"})

    cfg = client_module.YantrikDBConfig.from_env()

    assert cfg.token == ""
    assert cfg.namespace != "launch"


def test_unscoped_multiplex_read_fails_closed(client_module, monkeypatch):
    monkeypatch.setenv("YANTRIKDB_MODE", "http")
    monkeypatch.setenv("YANTRIKDB_TOKEN", "launch-token")
    error_type = _install_secret_scope(monkeypatch, None)

    with pytest.raises(error_type):
        client_module.YantrikDBConfig.from_env()


@pytest.mark.parametrize("hermes", ["main", "0.19.0"])
@pytest.mark.parametrize("scope", [None, {}], ids=["no-scope", "cron-scope"])
def test_single_profile_process_env_still_applies(client_module, monkeypatch, scope, hermes):
    """Without multiplexing, config resolves from the process environment on
    every path, including under cron-scope: the .env-only scope Hermes 0.19.0
    installs around each cron job, which on 0.19.0 is authoritative."""
    monkeypatch.setenv("YANTRIKDB_MODE", "http")
    monkeypatch.setenv("YANTRIKDB_URL", "http://ydb:7438")
    monkeypatch.setenv("YANTRIKDB_TOKEN", "ydb_docker")
    _install_secret_scope(monkeypatch, scope, multiplex=False, hermes=hermes)

    cfg = client_module.YantrikDBConfig.from_env()

    assert (cfg.mode, cfg.url, cfg.token) == ("http", "http://ydb:7438", "ydb_docker")


def test_hermes_without_multiplex_flag_reads_process_env(client_module, monkeypatch):
    mod = types.ModuleType("agent.secret_scope")

    def get_secret(name: str, default: str | None = None) -> str | None:
        raise AssertionError("scope consulted on a Hermes without multiplexing")

    mod.get_secret = get_secret
    monkeypatch.setitem(sys.modules, "agent.secret_scope", mod)
    monkeypatch.setenv("YANTRIKDB_MODE", "http")

    assert client_module.YantrikDBConfig.from_env().mode == "http"


def test_process_env_setting_dropped_under_multiplex_is_logged(
    client_module, monkeypatch, caplog,
):
    monkeypatch.setenv("YANTRIKDB_MODE", "http")
    monkeypatch.setenv("YANTRIKDB_TOKEN", "ydb_secret_value")
    _install_secret_scope(monkeypatch, {})

    with caplog.at_level(logging.WARNING):
        cfg = client_module.YantrikDBConfig.from_env()
        client_module.YantrikDBConfig.from_env()

    assert cfg.mode == "embedded"
    warned = [r.getMessage() for r in caplog.records if "profile's .env" in r.getMessage()]
    names = [m.split(" ", 1)[0] for m in warned]
    assert names.count("YANTRIKDB_MODE") == 1, "warn once per name, not per read"
    assert names.count("YANTRIKDB_TOKEN") == 1
    assert not any("ydb_secret_value" in m for m in warned)


def test_no_warning_when_profile_env_sets_the_name(client_module, monkeypatch, caplog):
    monkeypatch.setenv("YANTRIKDB_MODE", "http")
    _install_secret_scope(monkeypatch, {"YANTRIKDB_MODE": "http"})

    with caplog.at_level(logging.WARNING):
        cfg = client_module.YantrikDBConfig.from_env()

    assert cfg.mode == "http"
    assert not [r for r in caplog.records if "profile's .env" in r.getMessage()]


def test_config_schema_uses_scoped_mode(provider_module, monkeypatch):
    monkeypatch.setenv("YANTRIKDB_MODE", "embedded")
    _install_secret_scope(
        monkeypatch,
        {
            "YANTRIKDB_MODE": "http",
            "YANTRIKDB_TOKEN": "profile-b-token",
        },
    )

    schema = provider_module.YantrikDBMemoryProvider().get_config_schema()
    keys = {field["key"] for field in schema}

    assert "token" in keys
    assert "url" in keys
    assert "db_path" not in keys
