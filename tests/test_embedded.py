"""Tests for the v0.4.0 embedder-config paths in EmbeddedYantrikDBClient.

The embedded client supports three ways to attach an embedder:

  1. Default (no env): YantrikDB.with_default(db_path) — bundled potion-2M.
  2. Bundled-named: YANTRIKDB_EMBEDDER + YANTRIKDB_EMBEDDING_DIM
     → YantrikDB(db_path, embedding_dim=N) + set_embedder_named(name).
  3. Custom Python embedder: YANTRIKDB_EMBEDDER_CLASS + YANTRIKDB_EMBEDDING_DIM
     → import path → instantiate → set_embedder(instance).

These tests pin which engine method is called for each path, what errors
surface when config is incomplete, and that the dim contract is enforced.

The yantrikdb engine itself is mocked — we test the plugin's branching
logic, not the engine's behavior (the engine has its own test suite).
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def embedded_module(plugin):
    """The yantrikdb_plugin_under_test.embedded submodule, loaded via conftest."""
    return sys.modules[plugin[0].__name__ + ".embedded"]


@pytest.fixture
def mock_engine_class(embedded_module, monkeypatch):
    """Mock yantrikdb._yantrikdb_rust.YantrikDB so tests don't touch the real engine.

    Returns the MagicMock class; each test can inspect call_args on it,
    and the instances it produces (.return_value) behave like fully-loaded
    YantrikDB instances (has_embedder() = True, methods callable).
    """
    fake_engine_module = types.ModuleType("yantrikdb")
    fake_rust_module = types.ModuleType("yantrikdb._yantrikdb_rust")
    cls = MagicMock(name="YantrikDB")
    # Default behavior: any constructed instance reports has_embedder() True
    # so the init path completes; individual tests override as needed.
    cls.return_value.has_embedder.return_value = True
    cls.with_default.return_value.has_embedder.return_value = True
    fake_rust_module.YantrikDB = cls
    monkeypatch.setitem(sys.modules, "yantrikdb", fake_engine_module)
    monkeypatch.setitem(sys.modules, "yantrikdb._yantrikdb_rust", fake_rust_module)
    return cls


@pytest.fixture
def make_config(client_module):
    """Build a YantrikDBConfig for embedded tests."""
    def _build(**overrides):
        defaults = dict(
            mode="embedded",
            db_path="/tmp/test-mem.db",
            namespace="hermes-test",
            embedder_name="",
            embedder_class="",
            embedding_dim=0,
        )
        defaults.update(overrides)
        return client_module.YantrikDBConfig(**defaults)
    return _build


# ---------------------------------------------------------------------------
# Stub embedder classes for the _CLASS path tests. Placed in a stable module
# location (this module) so tests can use a real dotted path.
# ---------------------------------------------------------------------------

class _GoodEmbedder:
    """An object the engine would accept: has .encode(text) -> list[float]."""

    def encode(self, text: str) -> list[float]:
        # Deterministic stub — actual values don't matter for these tests.
        return [0.1] * 64


class _BadEmbedderNoEncode:
    """An object without an .encode() method — should be rejected by the plugin."""

    def something_else(self):
        return None


# ---------------------------------------------------------------------------
# Config from_env — pin the env-var → config-field wiring
# ---------------------------------------------------------------------------

class TestEmbedderConfigFromEnv:
    def test_default_no_env(self, client_module):
        cfg = client_module.YantrikDBConfig.from_env()
        assert cfg.embedder_name == ""
        assert cfg.embedder_class == ""
        assert cfg.embedding_dim == 0

    def test_reads_embedder_name(self, client_module, monkeypatch):
        monkeypatch.setenv("YANTRIKDB_EMBEDDER", "potion-base-8M")
        monkeypatch.setenv("YANTRIKDB_EMBEDDING_DIM", "256")
        cfg = client_module.YantrikDBConfig.from_env()
        assert cfg.embedder_name == "potion-base-8M"
        assert cfg.embedding_dim == 256

    def test_reads_embedder_class(self, client_module, monkeypatch):
        monkeypatch.setenv(
            "YANTRIKDB_EMBEDDER_CLASS",
            "tests.test_embedded._GoodEmbedder",
        )
        monkeypatch.setenv("YANTRIKDB_EMBEDDING_DIM", "384")
        cfg = client_module.YantrikDBConfig.from_env()
        assert cfg.embedder_class == "tests.test_embedded._GoodEmbedder"
        assert cfg.embedding_dim == 384

    def test_bad_dim_falls_back_to_zero(self, client_module, monkeypatch):
        monkeypatch.setenv("YANTRIKDB_EMBEDDING_DIM", "not-a-number")
        cfg = client_module.YantrikDBConfig.from_env()
        assert cfg.embedding_dim == 0


# ---------------------------------------------------------------------------
# Path 1 — default with_default (no env)
# ---------------------------------------------------------------------------

class TestDefaultEmbedderPath:
    def test_uses_with_default_when_no_env(
        self, embedded_module, mock_engine_class, make_config,
    ):
        cfg = make_config()  # no embedder_name, no embedder_class
        client = embedded_module.EmbeddedYantrikDBClient(cfg)
        mock_engine_class.with_default.assert_called_once()
        # Plain constructor not called
        mock_engine_class.assert_not_called()
        # No set_embedder* on the with_default instance (it auto-attached)
        with_default_instance = mock_engine_class.with_default.return_value
        with_default_instance.set_embedder_named.assert_not_called()
        with_default_instance.set_embedder.assert_not_called()


# ---------------------------------------------------------------------------
# Path 2 — bundled-named via YANTRIKDB_EMBEDDER
# ---------------------------------------------------------------------------

class TestNamedEmbedderPath:
    def test_bundled_name_with_dim_calls_set_embedder_named(
        self, embedded_module, mock_engine_class, make_config,
    ):
        cfg = make_config(embedder_name="potion-base-8M", embedding_dim=256)
        client = embedded_module.EmbeddedYantrikDBClient(cfg)
        # Constructed with explicit embedding_dim
        mock_engine_class.assert_called_once()
        ctor_call = mock_engine_class.call_args
        assert ctor_call.kwargs["embedding_dim"] == 256
        # And set_embedder_named was called with the right name
        instance = mock_engine_class.return_value
        instance.set_embedder_named.assert_called_once_with("potion-base-8M")
        # with_default was NOT used
        mock_engine_class.with_default.assert_not_called()

    def test_bundled_name_without_dim_raises(
        self, embedded_module, mock_engine_class, make_config, client_module,
    ):
        cfg = make_config(embedder_name="potion-base-8M", embedding_dim=0)
        with pytest.raises(client_module.YantrikDBError, match="EMBEDDING_DIM"):
            embedded_module.EmbeddedYantrikDBClient(cfg)
        # Engine not constructed
        mock_engine_class.assert_not_called()
        mock_engine_class.with_default.assert_not_called()


# ---------------------------------------------------------------------------
# Path 3 — custom Python class via YANTRIKDB_EMBEDDER_CLASS
# ---------------------------------------------------------------------------

class TestClassEmbedderPath:
    def test_valid_class_path_attaches_instance(
        self, embedded_module, mock_engine_class, make_config,
    ):
        cfg = make_config(
            embedder_class="tests.test_embedded._GoodEmbedder",
            embedding_dim=64,
        )
        client = embedded_module.EmbeddedYantrikDBClient(cfg)
        mock_engine_class.assert_called_once()
        assert mock_engine_class.call_args.kwargs["embedding_dim"] == 64
        instance = mock_engine_class.return_value
        instance.set_embedder.assert_called_once()
        # The argument should be a _GoodEmbedder instance
        passed = instance.set_embedder.call_args.args[0]
        assert isinstance(passed, _GoodEmbedder)
        # set_embedder_named was NOT called (class path is mutually exclusive)
        instance.set_embedder_named.assert_not_called()

    def test_class_path_without_dim_raises(
        self, embedded_module, mock_engine_class, make_config, client_module,
    ):
        cfg = make_config(
            embedder_class="tests.test_embedded._GoodEmbedder",
            embedding_dim=0,
        )
        with pytest.raises(client_module.YantrikDBError, match="EMBEDDING_DIM"):
            embedded_module.EmbeddedYantrikDBClient(cfg)

    def test_class_without_encode_method_raises(
        self, embedded_module, mock_engine_class, make_config, client_module,
    ):
        cfg = make_config(
            embedder_class="tests.test_embedded._BadEmbedderNoEncode",
            embedding_dim=64,
        )
        with pytest.raises(client_module.YantrikDBError, match=".encode"):
            embedded_module.EmbeddedYantrikDBClient(cfg)

    def test_malformed_class_path_raises(
        self, embedded_module, mock_engine_class, make_config, client_module,
    ):
        cfg = make_config(
            embedder_class="not_a_dotted_path",
            embedding_dim=64,
        )
        with pytest.raises(client_module.YantrikDBError, match="dotted import path"):
            embedded_module.EmbeddedYantrikDBClient(cfg)

    def test_unknown_class_path_raises_with_actionable_message(
        self, embedded_module, mock_engine_class, make_config, client_module,
    ):
        cfg = make_config(
            embedder_class="nonexistent.module.Embedder",
            embedding_dim=64,
        )
        with pytest.raises(
            client_module.YantrikDBError, match="failed to import",
        ):
            embedded_module.EmbeddedYantrikDBClient(cfg)


# ---------------------------------------------------------------------------
# Path mutual exclusion + precedence
# ---------------------------------------------------------------------------

class TestPathPrecedence:
    def test_class_path_takes_precedence_over_name(
        self, embedded_module, mock_engine_class, make_config,
    ):
        # Both set — class wins (more specific, doesn't rely on upstream
        # bundling decisions).
        cfg = make_config(
            embedder_class="tests.test_embedded._GoodEmbedder",
            embedder_name="potion-base-8M",
            embedding_dim=64,
        )
        embedded_module.EmbeddedYantrikDBClient(cfg)
        instance = mock_engine_class.return_value
        # set_embedder (custom class) called, set_embedder_named NOT called
        instance.set_embedder.assert_called_once()
        instance.set_embedder_named.assert_not_called()
