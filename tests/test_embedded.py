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
from unittest.mock import MagicMock

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
            embedder_model2vec="",
            embedder_huggingface="",
            embedding_dim=0,
        )
        defaults.update(overrides)
        return client_module.YantrikDBConfig(**defaults)
    return _build


@pytest.fixture
def embedders_module(plugin):
    """The yantrikdb_plugin_under_test.embedders submodule (v0.4.2+)."""
    return sys.modules[plugin[0].__name__ + ".embedders"]


class _FakeLoader:
    """Stand-in for Model2VecEmbedder / SentenceTransformerEmbedder.

    Captures construction args, advertises a fixed dim, and is detectable
    via isinstance() so tests can assert that `set_embedder` was handed
    the right loader (not some other object).
    """

    last_init: tuple[type, str] | None = None  # (cls, model_name) of last instance created

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self.embedding_dim = 128  # arbitrary; tests just assert the engine sees it
        type(self).last_init = (type(self), model_name)

    def encode(self, text: str) -> list[float]:
        return [0.0] * self.embedding_dim


class _FakeModel2VecLoader(_FakeLoader):
    pass


class _FakeHFLoader(_FakeLoader):
    embedding_dim = 384  # type: ignore[assignment]  # distinguishable from model2vec

    def __init__(self, model_name: str) -> None:
        super().__init__(model_name)
        self.embedding_dim = 384


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

    def test_reads_model2vec_name(self, client_module, monkeypatch):
        monkeypatch.setenv(
            "YANTRIKDB_EMBEDDER_MODEL2VEC",
            "minishlab/potion-multilingual-128M",
        )
        cfg = client_module.YantrikDBConfig.from_env()
        assert cfg.embedder_model2vec == "minishlab/potion-multilingual-128M"
        # No embedding_dim required — auto-probed.
        assert cfg.embedding_dim == 0

    def test_reads_huggingface_name(self, client_module, monkeypatch):
        monkeypatch.setenv(
            "YANTRIKDB_EMBEDDER_HF",
            "sentence-transformers/all-MiniLM-L6-v2",
        )
        cfg = client_module.YantrikDBConfig.from_env()
        assert cfg.embedder_huggingface == "sentence-transformers/all-MiniLM-L6-v2"
        assert cfg.embedding_dim == 0

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
        embedded_module.EmbeddedYantrikDBClient(cfg)
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
        embedded_module.EmbeddedYantrikDBClient(cfg)
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
        embedded_module.EmbeddedYantrikDBClient(cfg)
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
# Path 2 — built-in model2vec loader (v0.4.2+)
# ---------------------------------------------------------------------------

class TestModel2VecLoaderPath:
    def test_model2vec_path_instantiates_loader_and_probes_dim(
        self, embedded_module, embedders_module, mock_engine_class,
        make_config, monkeypatch,
    ):
        monkeypatch.setattr(embedders_module, "Model2VecEmbedder", _FakeModel2VecLoader)
        _FakeModel2VecLoader.last_init = None

        cfg = make_config(embedder_model2vec="minishlab/potion-multilingual-128M")
        embedded_module.EmbeddedYantrikDBClient(cfg)

        # Loader was constructed with the model name from the env var
        assert _FakeModel2VecLoader.last_init == (
            _FakeModel2VecLoader, "minishlab/potion-multilingual-128M",
        )
        # Engine constructed with the loader's *probed* dim, NOT the
        # config's zero dim (auto-probe is the whole point of v0.4.2).
        mock_engine_class.assert_called_once()
        assert mock_engine_class.call_args.kwargs["embedding_dim"] == 128
        # with_default NOT used
        mock_engine_class.with_default.assert_not_called()
        # set_embedder called once with the loader instance
        instance = mock_engine_class.return_value
        instance.set_embedder.assert_called_once()
        passed = instance.set_embedder.call_args.args[0]
        assert isinstance(passed, _FakeModel2VecLoader)
        # set_embedder_named NOT called (this is the custom-instance path)
        instance.set_embedder_named.assert_not_called()

    def test_model2vec_path_does_not_require_embedding_dim_env(
        self, embedded_module, embedders_module, mock_engine_class,
        make_config, monkeypatch,
    ):
        # Explicit test: embedding_dim=0 in config is FINE for the
        # model2vec path because the loader auto-probes.
        monkeypatch.setattr(embedders_module, "Model2VecEmbedder", _FakeModel2VecLoader)
        cfg = make_config(
            embedder_model2vec="minishlab/potion-base-8M",
            embedding_dim=0,  # explicit
        )
        # Should not raise — auto-probe handles it.
        embedded_module.EmbeddedYantrikDBClient(cfg)
        assert mock_engine_class.call_args.kwargs["embedding_dim"] == 128


# ---------------------------------------------------------------------------
# Path 3 — built-in sentence-transformers loader (v0.4.2+)
# ---------------------------------------------------------------------------

class TestHuggingFaceLoaderPath:
    def test_hf_path_instantiates_loader_and_probes_dim(
        self, embedded_module, embedders_module, mock_engine_class,
        make_config, monkeypatch,
    ):
        monkeypatch.setattr(
            embedders_module, "SentenceTransformerEmbedder", _FakeHFLoader,
        )
        _FakeHFLoader.last_init = None

        cfg = make_config(embedder_huggingface="sentence-transformers/all-MiniLM-L6-v2")
        embedded_module.EmbeddedYantrikDBClient(cfg)

        assert _FakeHFLoader.last_init == (
            _FakeHFLoader, "sentence-transformers/all-MiniLM-L6-v2",
        )
        mock_engine_class.assert_called_once()
        # HF fake advertises 384 dim — different from model2vec's 128 so
        # the assertion proves which loader was used.
        assert mock_engine_class.call_args.kwargs["embedding_dim"] == 384
        mock_engine_class.with_default.assert_not_called()
        instance = mock_engine_class.return_value
        instance.set_embedder.assert_called_once()
        passed = instance.set_embedder.call_args.args[0]
        assert isinstance(passed, _FakeHFLoader)


# ---------------------------------------------------------------------------
# Missing-dep error messages — actionable, point at the right extra
# ---------------------------------------------------------------------------

class TestLoaderMissingDeps:
    def test_model2vec_missing_dep_raises_actionable(
        self, embedders_module, client_module, monkeypatch,
    ):
        # Force the `from model2vec import StaticModel` import inside
        # Model2VecEmbedder.__init__ to fail.
        monkeypatch.setitem(sys.modules, "model2vec", None)
        with pytest.raises(client_module.YantrikDBError, match="model2vec"):
            embedders_module.Model2VecEmbedder("some/model")

    def test_hf_missing_dep_raises_actionable(
        self, embedders_module, client_module, monkeypatch,
    ):
        monkeypatch.setitem(sys.modules, "sentence_transformers", None)
        with pytest.raises(client_module.YantrikDBError, match="sentence-transformers"):
            embedders_module.SentenceTransformerEmbedder("some/model")


# ---------------------------------------------------------------------------
# Path mutual exclusion + precedence (extended for v0.4.2)
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

    def test_class_path_takes_precedence_over_model2vec(
        self, embedded_module, embedders_module, mock_engine_class,
        make_config, monkeypatch,
    ):
        # Custom class is the escape hatch — most specific user intent.
        monkeypatch.setattr(embedders_module, "Model2VecEmbedder", _FakeModel2VecLoader)
        _FakeModel2VecLoader.last_init = None
        cfg = make_config(
            embedder_class="tests.test_embedded._GoodEmbedder",
            embedder_model2vec="minishlab/potion-base-8M",
            embedding_dim=64,
        )
        embedded_module.EmbeddedYantrikDBClient(cfg)
        # Model2VecEmbedder was NOT instantiated (class path won)
        assert _FakeModel2VecLoader.last_init is None
        # Engine got user-set dim (64), not the loader-probed dim (128)
        assert mock_engine_class.call_args.kwargs["embedding_dim"] == 64

    def test_model2vec_path_takes_precedence_over_hf(
        self, embedded_module, embedders_module, mock_engine_class,
        make_config, monkeypatch,
    ):
        # Both built-in loaders set: model2vec wins (alphabetical isn't
        # the rule — the order is "lighter loader wins" because
        # model2vec is the static-embedding family and faster to
        # construct; users who want HF specifically should not set both).
        monkeypatch.setattr(embedders_module, "Model2VecEmbedder", _FakeModel2VecLoader)
        monkeypatch.setattr(
            embedders_module, "SentenceTransformerEmbedder", _FakeHFLoader,
        )
        _FakeModel2VecLoader.last_init = None
        _FakeHFLoader.last_init = None
        cfg = make_config(
            embedder_model2vec="minishlab/potion-base-8M",
            embedder_huggingface="sentence-transformers/all-MiniLM-L6-v2",
        )
        embedded_module.EmbeddedYantrikDBClient(cfg)
        # model2vec loader was used
        assert _FakeModel2VecLoader.last_init is not None
        # HF loader was NOT
        assert _FakeHFLoader.last_init is None
        assert mock_engine_class.call_args.kwargs["embedding_dim"] == 128

    def test_hf_path_takes_precedence_over_named(
        self, embedded_module, embedders_module, mock_engine_class,
        make_config, monkeypatch,
    ):
        # HF (built-in loader, picks an exact HF model) wins over
        # bundled-named (which depends on which named variants the
        # engine version happens to ship).
        monkeypatch.setattr(
            embedders_module, "SentenceTransformerEmbedder", _FakeHFLoader,
        )
        _FakeHFLoader.last_init = None
        cfg = make_config(
            embedder_huggingface="sentence-transformers/all-MiniLM-L6-v2",
            embedder_name="potion-base-8M",
            embedding_dim=256,  # would apply to named path
        )
        embedded_module.EmbeddedYantrikDBClient(cfg)
        # HF loader used
        assert _FakeHFLoader.last_init is not None
        # Engine got HF probed dim (384), not the user-set 256 from named path
        assert mock_engine_class.call_args.kwargs["embedding_dim"] == 384
        # set_embedder_named NOT called (HF path uses set_embedder)
        instance = mock_engine_class.return_value
        instance.set_embedder_named.assert_not_called()
        instance.set_embedder.assert_called_once()
