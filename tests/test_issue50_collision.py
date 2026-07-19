"""Issue #50 — embedded package-name collision + silent-success hardening (v0.9.2).

The plugin's own top-level package is named ``yantrikdb`` (to match
``plugin.yaml`` and the ``hermes plugins install`` layout), identical to the
engine distribution it depends on. When the plugin dir wins ``sys.path``
resolution, ``from yantrikdb._yantrikdb_rust import YantrikDB`` binds to the
plugin (no ``_yantrikdb_rust``) and embedded init fails — even though the real
engine is installed. These tests simulate that shadow WITHOUT a native engine,
so they run in CI, and pin the two-layer fix:

  Layer 1 — ``load_engine_yantrikdb_class`` recovers by loading the engine's
            extension from its installed distribution, with truthful errors.
  Layer 2 — a dropped write (backend never initialized) is loud + counted, and
            ``is_available`` tells the truth under the shadow.
"""

from __future__ import annotations

import importlib
import logging
import sys
import types

import pytest


def _emb(provider_module):
    return importlib.import_module(provider_module.__name__ + ".embedded")


def _shadow_engine(monkeypatch) -> None:
    """Simulate the plugin package shadowing the engine on ``sys.path``:
    a ``yantrikdb`` package with an empty ``__path__`` and no ``_yantrikdb_rust``
    submodule, so the fast-path import raises ``ImportError`` deterministically —
    regardless of whether a real engine is installed in the test environment."""
    fake = types.ModuleType("yantrikdb")
    fake.__path__ = []  # a package, but with no discoverable submodules
    monkeypatch.setitem(sys.modules, "yantrikdb", fake)
    monkeypatch.delitem(sys.modules, "yantrikdb._yantrikdb_rust", raising=False)


class TestEngineLoaderCollision:
    def test_collision_with_missing_engine_raises_truthful(
        self, monkeypatch, provider_module, client_module,
    ):
        emb = _emb(provider_module)
        _shadow_engine(monkeypatch)
        monkeypatch.setattr(emb, "find_engine_ext_path", lambda: None)
        with pytest.raises(client_module.YantrikDBError, match="not installed"):
            emb.load_engine_yantrikdb_class()

    def test_collision_loads_engine_from_located_path(
        self, monkeypatch, tmp_path, provider_module,
    ):
        emb = _emb(provider_module)
        _shadow_engine(monkeypatch)
        # A stand-in "extension": a .py file exposing YantrikDB. spec_from_file_
        # location uses SourceFileLoader for .py, exercising the same load path a
        # real compiled .so would take.
        fake_ext = tmp_path / "_yantrikdb_rust.py"
        fake_ext.write_text("class YantrikDB:\n    marker = 'real-engine'\n")
        monkeypatch.setattr(emb, "find_engine_ext_path", lambda: str(fake_ext))
        try:
            cls = emb.load_engine_yantrikdb_class()
            assert getattr(cls, "marker", None) == "real-engine"
            # Loaded module cached so a later plain import resolves the engine
            # despite the shadowing package (fixes is_available too).
            assert sys.modules.get("yantrikdb._yantrikdb_rust") is not None
        finally:
            sys.modules.pop("yantrikdb._yantrikdb_rust", None)

    def test_find_engine_ext_path_fallback_scan_excludes_own_dir(
        self, monkeypatch, tmp_path, provider_module,
    ):
        emb = _emb(provider_module)
        from importlib import metadata as ilm

        def _raise(_name):
            raise ilm.PackageNotFoundError()

        # Force the metadata branch to miss so the sys.path scan runs.
        monkeypatch.setattr(ilm, "distribution", _raise)
        engine_root = tmp_path / "site"
        (engine_root / "yantrikdb").mkdir(parents=True)
        ext = engine_root / "yantrikdb" / "_yantrikdb_rust.cpython-311.so"
        ext.write_bytes(b"\x00")
        monkeypatch.syspath_prepend(str(engine_root))
        found = emb.find_engine_ext_path()
        assert found is not None and found.endswith(".so")


class TestDroppedWriteSignal:
    """Layer 2 — a write dropped because the backend never initialized must be
    loud and counted, never silent (issue #50)."""

    def _uninit_provider(self, provider_module):
        p = provider_module.YantrikDBMemoryProvider()
        p._client = None
        p._cron_skipped = False
        p._init_error = "collision: engine shadowed by plugin (issue #50)"
        return p

    def test_dropped_write_logs_once_and_keeps_counting(
        self, provider_module, caplog,
    ):
        p = self._uninit_provider(provider_module)
        with caplog.at_level(logging.ERROR):
            p.on_memory_write("add", "memory", "Don is CEO of Agilicus")
            p.on_memory_write("add", "user", "Don uses he/him")
        assert p._dropped_writes == 2
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1  # logged once, not per write
        assert "DROPPED" in errors[0].getMessage()

    def test_non_qualifying_writes_do_not_count(self, provider_module):
        p = self._uninit_provider(provider_module)
        p.on_memory_write("delete", "memory", "x")   # not an add
        p.on_memory_write("add", "scratch", "x")      # unmirrored target
        p.on_memory_write("add", "memory", "")        # empty content
        assert p._dropped_writes == 0


class TestIsAvailableTruthful:
    """Layer 2 — availability reflects reality under the shadow so
    `hermes memory status` doesn't report a phantom-absent backend."""

    def test_available_via_locator_when_import_shadowed(
        self, monkeypatch, provider_module,
    ):
        p = provider_module.YantrikDBMemoryProvider()
        emb = _emb(provider_module)
        _shadow_engine(monkeypatch)  # plain import fails
        monkeypatch.setattr(
            emb, "find_engine_ext_path", lambda: "/opt/_yantrikdb_rust.so",
        )
        assert p.is_available() is True

    def test_unavailable_when_locator_empty(self, monkeypatch, provider_module):
        p = provider_module.YantrikDBMemoryProvider()
        emb = _emb(provider_module)
        _shadow_engine(monkeypatch)
        monkeypatch.setattr(emb, "find_engine_ext_path", lambda: None)
        assert p.is_available() is False


class TestNotAvailablePromptBlock:
    def test_block_forbids_claiming_saves_and_names_collision(
        self, provider_module,
    ):
        p = provider_module.YantrikDBMemoryProvider()
        p._client = None
        p._cron_skipped = False
        p._init_error = "boom"
        block = p.system_prompt_block()
        assert "NOT AVAILABLE" in block
        assert "Do NOT tell the user their memories were saved" in block
        assert "issue #50" in block.lower() or "#50" in block
