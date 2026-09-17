"""Assert the plugin's calls against the REAL engine's accepted surface.

Issue #87. ``think()`` was unusable on the embedded backend — the default
one — for a month, and no test noticed, because every layer that could
have caught it was mocked:

* ``test_provider.py`` mocks the client, so a mock accepts any kwarg.
* ``test_embedded.py`` mocks the engine, so a mock accepts any config key.
* ``test_signature_parity.py`` compares the two clients to each other,
  and both were wrong in the same way.

The missing assertion is against the thing that actually rejects: the
engine. ``think()`` takes a config DICT, so its accepted keys are not in
any Python signature and no signature-level check can see them. Since
engine 0.15.0 an unrecognized key raises, which is what turned the
long-standing silently-ignored ``namespace`` into a hard outage.

Two layers here, deliberately:

1. ``TestThinkConfigKeys`` runs everywhere, engine or not. It captures the
   config the embedded client really builds and compares it to the key
   set the engine documents, so a newly added unknown key fails in plain
   CI.
2. ``TestAgainstRealEngine`` skips unless the engine is importable, and
   then calls the plugin's own client against a real store. That is the
   ground truth, and it is the layer the CI ``engine-contract`` job
   exists to run.
"""

from __future__ import annotations

import logging
import sys
import types
from unittest.mock import MagicMock

import pytest

# The engine's accepted think() config keys, mirrored from the binding's
# KNOWN array in crates/yantrikdb-python/src/py_engine/cognition.rs.
#
# Stated here rather than read back off the object under test, on purpose:
# a gate that derives its expectations from the thing it validates enforces
# nothing. The real engine checks this list in TestAgainstRealEngine below.
ENGINE_THINK_CONFIG_KEYS = frozenset({
    "importance_threshold",
    "decay_threshold",
    "max_triggers",
    "run_consolidation",
    "run_conflict_scan",
    "run_pattern_mining",
    "min_active_memories",
    "run_personality",
    "consolidation_limit",
    "consolidation_time_window_days",
    "consolidation_sim_threshold",
    "extract_attribute_claims",
    "consolidation_min_cluster",
    "consolidation_require_entity_overlap",
})


def _require_real_engine(embedded_module) -> str:
    """Skip unless the engine is genuinely installed, else return its path.

    A plain ``find_spec("yantrikdb")`` is the WRONG probe inside this repo:
    the plugin package is itself named ``yantrikdb`` and wins name
    resolution, so that check reports "engine present" when it is looking at
    the plugin, or "absent" when the engine is installed but shadowed. Both
    answers turn this suite into a silent no-op.

    ``find_engine_ext_path`` is the plugin's own issue-#50 resolver: it goes
    through ``importlib.metadata`` to the engine DISTRIBUTION, so shadowing
    cannot fool it. Using the same resolver the production code uses also
    means this suite runs in exactly the conditions the code runs in.
    """
    ext_path = embedded_module.find_engine_ext_path()
    if ext_path is None:
        pytest.skip("real yantrikdb engine not installed (CI: engine-contract job)")
    return ext_path


# ---------------------------------------------------------------------------
# Layer 1 — runs with or without the engine
# ---------------------------------------------------------------------------

class TestThinkConfigKeys:
    """Every key the embedded client sends must be one the engine knows."""

    @pytest.fixture
    def captured_cfg(self, plugin, client_module, monkeypatch):
        """Call the embedded client's think() and return the cfg it built."""
        embedded = sys.modules[plugin[0].__name__ + ".embedded"]

        seen: dict = {}

        engine = MagicMock(name="engine")
        engine.has_embedder.return_value = True
        engine.think.side_effect = lambda cfg: seen.update(cfg) or {}

        cls = MagicMock(name="YantrikDB")
        cls.return_value = engine
        cls.with_default.return_value = engine
        fake_engine_module = types.ModuleType("yantrikdb")
        fake_rust_module = types.ModuleType("yantrikdb._yantrikdb_rust")
        fake_rust_module.YantrikDB = cls
        monkeypatch.setitem(sys.modules, "yantrikdb", fake_engine_module)
        monkeypatch.setitem(sys.modules, "yantrikdb._yantrikdb_rust", fake_rust_module)

        config = client_module.YantrikDBConfig(
            mode="embedded",
            db_path="/tmp/contract-test.db",
            namespace="hermes:workspace:coder",
        )

        def _capture(**kwargs):
            seen.clear()
            embedded.EmbeddedYantrikDBClient(config).think(**kwargs)
            return dict(seen)

        return _capture

    def test_default_call_sends_only_known_keys(self, captured_cfg):
        cfg = captured_cfg()
        unknown = set(cfg) - ENGINE_THINK_CONFIG_KEYS
        assert not unknown, (
            f"think() would be sent config keys the engine rejects: {sorted(unknown)}"
        )

    def test_every_call_shape_sends_only_known_keys(self, captured_cfg):
        """The shapes the provider's three call sites actually use."""
        shapes = [
            {"run_pattern_mining": False},                       # hygiene apply
            {"run_pattern_mining": False, "run_personality": False},  # maintenance
            {"run_pattern_mining": True, "consolidation_limit": 5},   # think tool
        ]
        for shape in shapes:
            unknown = set(captured_cfg(**shape)) - ENGINE_THINK_CONFIG_KEYS
            assert not unknown, f"{shape} sends unknown keys {sorted(unknown)}"

    def test_namespace_is_never_sent(self, captured_cfg):
        """The #87 regression, stated directly."""
        assert "namespace" not in captured_cfg()

    def test_namespace_is_not_even_accepted(self, plugin, client_module):
        """Both clients must refuse the kwarg, so a caller cannot re-add it
        silently. A parameter that is accepted and dropped is how this bug
        stayed invisible on the HTTP backend."""
        embedded = sys.modules[plugin[0].__name__ + ".embedded"]
        import inspect
        for klass in (
            embedded.EmbeddedYantrikDBClient,
            client_module.YantrikDBClient,
        ):
            params = inspect.signature(klass.think).parameters
            assert "namespace" not in params, (
                f"{klass.__name__}.think still accepts a namespace it cannot honor"
            )


# ---------------------------------------------------------------------------
# Layer 2 — ground truth, needs the engine
# ---------------------------------------------------------------------------

class TestAgainstRealEngine:
    """Drive the plugin's embedded client against a real store.

    Reverting the #87 fix makes these fail; mocks cannot.
    """

    @pytest.fixture
    def real_client(self, plugin, client_module, tmp_path, monkeypatch):
        embedded = sys.modules[plugin[0].__name__ + ".embedded"]
        _require_real_engine(embedded)
        # Undo any module stub an earlier test installed, so the loader below
        # reaches the real engine rather than a leftover MagicMock.
        for name in ("yantrikdb", "yantrikdb._yantrikdb_rust"):
            monkeypatch.delitem(sys.modules, name, raising=False)
        config = client_module.YantrikDBConfig(
            mode="embedded",
            db_path=str(tmp_path / "contract.db"),
            namespace="hermes:workspace:coder",
        )
        return embedded.EmbeddedYantrikDBClient(config)

    def test_think_succeeds(self, real_client):
        out = real_client.think(run_pattern_mining=False, run_personality=False)
        assert isinstance(out, dict)

    def test_think_tool_shape_succeeds(self, real_client):
        out = real_client.think(run_pattern_mining=True, consolidation_limit=5)
        assert isinstance(out, dict)

    def test_engine_key_list_still_matches(self, real_client):
        """Pin the mirrored key list to the installed engine.

        The engine names its full known-key set in the rejection message, so
        a key added or removed upstream shows up here instead of silently
        aging the constant above.
        """
        with pytest.raises(Exception) as exc:
            real_client._db.think({"definitely_not_a_key": True})
        message = str(exc.value)
        reported = {
            key for key in ENGINE_THINK_CONFIG_KEYS if f'"{key}"' in message
        }
        assert reported == set(ENGINE_THINK_CONFIG_KEYS), (
            "engine's known think() keys drifted from ENGINE_THINK_CONFIG_KEYS; "
            f"missing from engine: {sorted(set(ENGINE_THINK_CONFIG_KEYS) - reported)}"
        )


# ---------------------------------------------------------------------------
# The other half of #87: the failure was invisible
# ---------------------------------------------------------------------------

class TestMaintenanceFailureIsAudible:
    """A broken maintenance pass must announce itself at least once.

    Reported as the reason the bug was expensive to find: at DEBUG, a
    feature that never ran looked exactly like a feature with nothing to do.
    """

    @pytest.fixture
    def failing_provider(self, provider_module, client_module, monkeypatch):
        from unittest.mock import patch
        client = MagicMock()
        client.think.side_effect = client_module.YantrikDBError(
            'think failed: think(): unknown config key "namespace"',
        )
        monkeypatch.setenv("YANTRIKDB_MODE", "http")
        monkeypatch.setenv("YANTRIKDB_TOKEN", "ydb_test")
        p = provider_module.YantrikDBMemoryProvider()
        with patch.object(provider_module, "make_backend", return_value=client):
            p.initialize("sess-1", agent_workspace="workspace", agent_identity="coder")
        return p

    def test_first_failure_warns(self, failing_provider, caplog):
        with caplog.at_level(logging.WARNING):
            failing_provider._run_maintenance(reason="session-end")
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings, "a failed maintenance pass logged nothing at WARNING"
        assert "think failed" in warnings[0].getMessage()

    def test_later_failures_do_not_flood(self, failing_provider, caplog):
        with caplog.at_level(logging.WARNING):
            for _ in range(5):
                failing_provider._run_maintenance(reason="periodic")
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) == 1, (
            f"expected exactly one warning across repeated failures, got {len(warnings)}"
        )
