"""v0.9.3 — process-global engine cache for embedded mode.

Hermes builds a memory provider per agent/session and they all resolve to the
same database, so before this cache a host running N agents opened the same
file N times — each engine spawning its own materializer workers + compactor.
Measured on a 32-logical-CPU box with 4k records: 6 engines idled at 15.3% of
the machine (135 OS threads) vs 3.7% (52 threads) shared — 4.13x.

These tests use a stub engine class (no native wheel needed) so they run in CI.
"""

from __future__ import annotations

import importlib
import threading
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def emb(provider_module):
    m = importlib.import_module(provider_module.__name__ + ".embedded")
    m.reset_engine_cache()
    yield m
    m.reset_engine_cache()


class _StubEngine:
    """Stands in for the native YantrikDB handle."""

    instances = 0

    def __init__(self, *args, **kwargs):
        type(self).instances += 1
        self.id = type(self).instances

    @classmethod
    def with_default(cls, db_path):
        return cls(db_path)

    def has_embedder(self):
        return True

    def set_embedder(self, instance):
        self.embedder = instance

    def set_embedder_named(self, name):
        self.embedder = name


@pytest.fixture
def stub(emb):
    _StubEngine.instances = 0
    with patch.object(emb, "load_engine_yantrikdb_class", return_value=_StubEngine):
        yield _StubEngine


def _cfg(client_module, tmp_path, **kw):
    return client_module.YantrikDBConfig(
        mode="embedded", db_path=str(tmp_path / "memory.db"), **kw,
    )


class TestEngineSharing:
    def test_same_config_shares_one_engine(self, emb, stub, client_module, tmp_path):
        a = emb.EmbeddedYantrikDBClient(_cfg(client_module, tmp_path))
        b = emb.EmbeddedYantrikDBClient(_cfg(client_module, tmp_path))
        c = emb.EmbeddedYantrikDBClient(_cfg(client_module, tmp_path))
        assert a._db is b._db is c._db
        assert stub.instances == 1  # constructed once, not three times

    def test_distinct_db_paths_do_not_share(
        self, emb, stub, client_module, tmp_path,
    ):
        a = emb.EmbeddedYantrikDBClient(_cfg(client_module, tmp_path))
        other = client_module.YantrikDBConfig(
            mode="embedded", db_path=str(tmp_path / "other.db"),
        )
        b = emb.EmbeddedYantrikDBClient(other)
        assert a._db is not b._db
        assert stub.instances == 2

    def test_distinct_embedders_do_not_share(
        self, emb, stub, client_module, tmp_path,
    ):
        """Embedder choice determines vector dim — must never share a handle."""
        a = emb.EmbeddedYantrikDBClient(_cfg(client_module, tmp_path))
        b = emb.EmbeddedYantrikDBClient(
            _cfg(client_module, tmp_path, embedder_name="potion-base-8M",
                 embedding_dim=256),
        )
        assert a._db is not b._db

    def test_opt_out_restores_per_provider_engines(
        self, emb, stub, client_module, tmp_path,
    ):
        a = emb.EmbeddedYantrikDBClient(
            _cfg(client_module, tmp_path, share_engine=False))
        b = emb.EmbeddedYantrikDBClient(
            _cfg(client_module, tmp_path, share_engine=False))
        assert a._db is not b._db
        assert stub.instances == 2

    def test_share_engine_defaults_true(self, client_module, monkeypatch):
        monkeypatch.delenv("YANTRIKDB_SHARE_ENGINE", raising=False)
        assert client_module.YantrikDBConfig.from_env().share_engine is True

    def test_share_engine_env_opt_out(self, client_module, monkeypatch):
        monkeypatch.setenv("YANTRIKDB_SHARE_ENGINE", "false")
        assert client_module.YantrikDBConfig.from_env().share_engine is False


class TestCacheKey:
    def test_key_is_path_normalised(self, emb, client_module, tmp_path):
        """Same database reached by different spellings shares one entry."""
        p = tmp_path / "memory.db"
        k1 = emb._engine_cache_key(str(p), _cfg(client_module, tmp_path))
        k2 = emb._engine_cache_key(
            str(tmp_path / "sub" / ".." / "memory.db"),
            _cfg(client_module, tmp_path),
        )
        assert k1 == k2


class TestThreadSafety:
    def test_concurrent_construction_converges_on_one_engine(
        self, emb, stub, client_module, tmp_path,
    ):
        """Racing providers may both build, but all must end up on one handle."""
        results: list = []
        barrier = threading.Barrier(8)

        def build():
            barrier.wait()
            results.append(
                emb.EmbeddedYantrikDBClient(_cfg(client_module, tmp_path))._db)

        threads = [threading.Thread(target=build) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 8
        assert len({id(r) for r in results}) == 1  # all adopted the same engine


class TestCacheHitSkipsEmbedderLoad:
    def test_hit_does_not_reload_embedder(
        self, emb, stub, client_module, tmp_path,
    ):
        """A cache hit must short-circuit before embedder materialisation —
        that load is the expensive part of the model2vec / HF paths."""
        cfg = _cfg(client_module, tmp_path,
                   embedder_model2vec="minishlab/potion-base-8M")
        loader = MagicMock()
        loader.return_value.embedding_dim = 256
        # embedded.py does `from .embedders import Model2VecEmbedder` at call
        # time, so patching the attribute on the submodule is what takes effect
        # (and keeps the test offline — no model download).
        with patch.object(emb._embedders_mod, "Model2VecEmbedder", loader):
            emb.EmbeddedYantrikDBClient(cfg)
            assert loader.call_count == 1
            emb.EmbeddedYantrikDBClient(cfg)
            emb.EmbeddedYantrikDBClient(cfg)
        assert loader.call_count == 1  # cache hits never re-loaded the model
