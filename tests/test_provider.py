"""Tests for YantrikDBMemoryProvider (__init__.py).

Provider tests swap in a ``MagicMock`` client so no network happens. The
goal is to pin the contract with Hermes (tool names, dispatch, hook
semantics, circuit breaker) rather than exhaustively retest HTTP — that
lives in test_client.py.
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_client(client_module) -> MagicMock:
    c = MagicMock(spec=client_module.YantrikDBClient)
    c.health.return_value = {"status": "ok"}
    c.remember.return_value = {"rid": "r-new"}
    c.recall.return_value = {"results": [], "total": 0}
    c.forget.return_value = {"rid": "r-x", "found": True}
    c.think.return_value = {
        "consolidation_count": 2,
        "conflicts_found": 1,
        "patterns_new": 0,
        "patterns_updated": 0,
        "duration_ms": 50,
        "triggers": [],
    }
    c.conflicts.return_value = {"conflicts": []}
    c.resolve_conflict.return_value = {"conflict_id": "c1", "strategy": "keep_winner"}
    c.relate.return_value = {"edge_id": "e1"}
    c.stats.return_value = {
        "active_memories": 42,
        "consolidated_memories": 3,
        "tombstoned_memories": 5,
        "edges": 17,
        "entities": 12,
        "operations": 128,
        "open_conflicts": 1,
        "pending_triggers": 0,
    }
    return c


@pytest.fixture
def provider(provider_module, mock_client, monkeypatch):
    """Initialized provider with a mock client wired in."""
    monkeypatch.setenv("YANTRIKDB_TOKEN", "ydb_test")
    p = provider_module.YantrikDBMemoryProvider()
    with patch.object(provider_module, "YantrikDBClient", return_value=mock_client):
        p.initialize(
            "sess-1",
            agent_workspace="workspace",
            agent_identity="coder",
            platform="cli",
        )
    return p


def _wait_for_thread(t, timeout: float = 2.0) -> None:
    if t is not None and t.is_alive():
        t.join(timeout=timeout)


# ---------------------------------------------------------------------------
# Identity & availability
# ---------------------------------------------------------------------------

class TestIdentity:
    def test_name(self, provider_module):
        assert provider_module.YantrikDBMemoryProvider().name == "yantrikdb"


class TestIsAvailable:
    def test_false_when_no_token(self, provider_module):
        assert provider_module.YantrikDBMemoryProvider().is_available() is False

    def test_true_when_token_set(self, provider_module, monkeypatch):
        monkeypatch.setenv("YANTRIKDB_TOKEN", "ydb_x")
        assert provider_module.YantrikDBMemoryProvider().is_available() is True


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

class TestInitialize:
    def test_namespace_from_workspace_and_identity(self, provider):
        assert provider._namespace == "hermes:workspace:coder"

    def test_cron_context_deactivates_plugin(self, provider_module, monkeypatch):
        monkeypatch.setenv("YANTRIKDB_TOKEN", "ydb_test")
        p = provider_module.YantrikDBMemoryProvider()
        with patch.object(provider_module, "YantrikDBClient") as mock_cls:
            p.initialize("sess", agent_context="cron", platform="cron")
        assert p._cron_skipped is True
        assert p._client is None
        mock_cls.assert_not_called()

    def test_no_token_leaves_client_none(self, provider_module, monkeypatch):
        monkeypatch.delenv("YANTRIKDB_TOKEN", raising=False)
        p = provider_module.YantrikDBMemoryProvider()
        p.initialize("sess", agent_workspace="w", agent_identity="i")
        assert p._client is None

    def test_health_failure_does_not_abort_init(
        self, provider_module, client_module, mock_client, monkeypatch,
    ):
        monkeypatch.setenv("YANTRIKDB_TOKEN", "ydb_test")
        mock_client.health.side_effect = client_module.YantrikDBTransientError("down")
        p = provider_module.YantrikDBMemoryProvider()
        with patch.object(provider_module, "YantrikDBClient", return_value=mock_client):
            p.initialize("sess", agent_workspace="w", agent_identity="i")
        assert p._client is mock_client


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

EXPECTED_TOOL_NAMES = {
    "yantrikdb_remember",
    "yantrikdb_recall",
    "yantrikdb_forget",
    "yantrikdb_think",
    "yantrikdb_conflicts",
    "yantrikdb_resolve_conflict",
    "yantrikdb_relate",
    "yantrikdb_stats",
}


class TestToolSchemas:
    def test_six_tools_registered(self, provider):
        names = {s["name"] for s in provider.get_tool_schemas()}
        assert names == EXPECTED_TOOL_NAMES

    def test_schemas_available_before_initialize(self, provider_module):
        # Hermes calls get_tool_schemas() at register time, BEFORE
        # initialize() runs, to index tool-name → provider for routing.
        # Returning [] here (pre-init) would make tool calls resolve as
        # "Unknown tool" at runtime.
        p = provider_module.YantrikDBMemoryProvider()
        names = {s["name"] for s in p.get_tool_schemas()}
        assert names == EXPECTED_TOOL_NAMES

    def test_no_tools_in_cron_context(self, provider_module, monkeypatch):
        monkeypatch.setenv("YANTRIKDB_TOKEN", "ydb_test")
        p = provider_module.YantrikDBMemoryProvider()
        p.initialize("sess", agent_context="cron", platform="cron")
        assert p.get_tool_schemas() == []

    def test_schema_shape_is_valid(self, provider):
        for schema in provider.get_tool_schemas():
            assert {"name", "description", "parameters"} <= schema.keys()
            params = schema["parameters"]
            assert params["type"] == "object"
            assert "properties" in params
            assert "required" in params


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

class TestHandleToolCall:
    def test_remember_dispatches(self, provider, mock_client):
        out = provider.handle_tool_call(
            "yantrikdb_remember",
            {
                "text": "User prefers dark mode",
                "importance": 0.8,
                "domain": "preference",
            },
        )
        mock_client.remember.assert_called_once()
        call = mock_client.remember.call_args
        assert call.args[0] == "User prefers dark mode"
        assert call.kwargs["importance"] == 0.8
        assert call.kwargs["domain"] == "preference"
        assert call.kwargs["namespace"] == "hermes:workspace:coder"
        parsed = json.loads(out)
        assert parsed["stored"] is True
        assert parsed["rid"] == "r-new"

    def test_remember_rejects_empty_text(self, provider, mock_client):
        out = provider.handle_tool_call("yantrikdb_remember", {})
        mock_client.remember.assert_not_called()
        assert "Missing required parameter" in json.loads(out)["error"]

    def test_recall_dispatches(self, provider, mock_client):
        mock_client.recall.return_value = {
            "results": [
                {
                    "rid": "r1",
                    "text": "fact",
                    "score": 0.9,
                    "importance": 0.7,
                    "created_at": "x",
                    "domain": "work",
                    "why_retrieved": ["semantic_match", "graph-connected via Alice"],
                },
            ],
            "total": 1,
        }
        out = provider.handle_tool_call("yantrikdb_recall", {"query": "dark mode"})
        mock_client.recall.assert_called_once()
        parsed = json.loads(out)
        assert parsed["count"] == 1
        assert parsed["results"][0]["rid"] == "r1"
        # Explainable recall — reasons must reach the agent
        assert parsed["results"][0]["why_retrieved"] == [
            "semantic_match", "graph-connected via Alice",
        ]

    def test_recall_handles_missing_why_retrieved(self, provider, mock_client):
        mock_client.recall.return_value = {
            "results": [{"rid": "r1", "text": "fact", "score": 0.9}],
        }
        out = provider.handle_tool_call("yantrikdb_recall", {"query": "x"})
        assert json.loads(out)["results"][0]["why_retrieved"] == []

    def test_recall_caps_top_k_at_50(self, provider, mock_client):
        provider.handle_tool_call(
            "yantrikdb_recall", {"query": "x", "top_k": 200},
        )
        assert mock_client.recall.call_args.kwargs["top_k"] == 50

    def test_forget_dispatches(self, provider, mock_client):
        out = provider.handle_tool_call("yantrikdb_forget", {"rid": "r1"})
        mock_client.forget.assert_called_once_with("r1")
        assert json.loads(out)["found"] is True

    def test_think_dispatches(self, provider, mock_client):
        out = provider.handle_tool_call(
            "yantrikdb_think", {"run_pattern_mining": True},
        )
        assert mock_client.think.call_args.kwargs["run_pattern_mining"] is True
        parsed = json.loads(out)
        assert parsed["consolidated"] == 2
        assert parsed["conflicts_found"] == 1

    def test_conflicts_dispatches(self, provider, mock_client):
        out = provider.handle_tool_call("yantrikdb_conflicts", {})
        mock_client.conflicts.assert_called_once()
        assert json.loads(out)["count"] == 0

    def test_relate_dispatches(self, provider, mock_client):
        out = provider.handle_tool_call(
            "yantrikdb_relate",
            {"entity": "Alice", "target": "Acme", "relationship": "works_at"},
        )
        mock_client.relate.assert_called_once_with("Alice", "Acme", "works_at")
        assert json.loads(out)["edge_id"] == "e1"

    def test_relate_requires_all_three_fields(self, provider, mock_client):
        out = provider.handle_tool_call(
            "yantrikdb_relate", {"entity": "Alice", "target": "Acme"},
        )
        mock_client.relate.assert_not_called()
        assert "Missing required" in json.loads(out)["error"]

    def test_stats_dispatches(self, provider, mock_client):
        out = provider.handle_tool_call("yantrikdb_stats", {})
        mock_client.stats.assert_called_once()
        parsed = json.loads(out)
        assert parsed["active_memories"] == 42
        assert parsed["open_conflicts"] == 1
        assert parsed["edges"] == 17

    def test_resolve_conflict_keep_winner(self, provider, mock_client):
        out = provider.handle_tool_call(
            "yantrikdb_resolve_conflict",
            {
                "conflict_id": "c1",
                "strategy": "keep_winner",
                "winner_rid": "r2",
                "resolution_note": "newer fact is correct",
            },
        )
        mock_client.resolve_conflict.assert_called_once()
        call = mock_client.resolve_conflict.call_args
        assert call.args[0] == "c1"
        assert call.kwargs["strategy"] == "keep_winner"
        assert call.kwargs["winner_rid"] == "r2"
        assert call.kwargs["resolution_note"] == "newer fact is correct"
        parsed = json.loads(out)
        assert parsed["resolved"] is True

    def test_resolve_conflict_merge_requires_new_text(self, provider, mock_client):
        out = provider.handle_tool_call(
            "yantrikdb_resolve_conflict",
            {"conflict_id": "c1", "strategy": "merge"},
        )
        mock_client.resolve_conflict.assert_not_called()
        assert "new_text" in json.loads(out)["error"]

    def test_resolve_conflict_keep_winner_requires_winner_rid(
        self, provider, mock_client,
    ):
        out = provider.handle_tool_call(
            "yantrikdb_resolve_conflict",
            {"conflict_id": "c1", "strategy": "keep_winner"},
        )
        mock_client.resolve_conflict.assert_not_called()
        assert "winner_rid" in json.loads(out)["error"]

    def test_resolve_conflict_requires_id_and_strategy(
        self, provider, mock_client,
    ):
        out = provider.handle_tool_call(
            "yantrikdb_resolve_conflict", {"conflict_id": "c1"},
        )
        mock_client.resolve_conflict.assert_not_called()
        assert "Missing required" in json.loads(out)["error"]

    def test_unknown_tool_returns_error(self, provider):
        out = provider.handle_tool_call("yantrikdb_unknown", {})
        assert "Unknown tool" in json.loads(out)["error"]

    def test_inactive_provider_rejects(self, provider_module):
        p = provider_module.YantrikDBMemoryProvider()
        out = p.handle_tool_call("yantrikdb_recall", {"query": "x"})
        assert "not active" in json.loads(out)["error"].lower()


# ---------------------------------------------------------------------------
# Error taxonomy
# ---------------------------------------------------------------------------

class TestErrorTaxonomy:
    def test_auth_error_surface(self, provider, client_module, mock_client):
        mock_client.recall.side_effect = client_module.YantrikDBAuthError("bad")
        out = provider.handle_tool_call("yantrikdb_recall", {"query": "x"})
        assert "auth" in json.loads(out)["error"].lower()

    def test_client_error_does_not_trip_breaker(
        self, provider, client_module, mock_client,
    ):
        mock_client.recall.side_effect = client_module.YantrikDBClientError("nope")
        for _ in range(10):
            provider.handle_tool_call("yantrikdb_recall", {"query": "x"})
        assert provider._breaker_open() is False

    def test_transient_error_trips_breaker(
        self, provider, client_module, mock_client,
    ):
        mock_client.recall.side_effect = client_module.YantrikDBTransientError("x")
        for _ in range(5):
            provider.handle_tool_call("yantrikdb_recall", {"query": "x"})
        assert provider._breaker_open() is True

    def test_breaker_short_circuits_calls(
        self, provider, client_module, mock_client,
    ):
        mock_client.recall.side_effect = client_module.YantrikDBTransientError("x")
        for _ in range(5):
            provider.handle_tool_call("yantrikdb_recall", {"query": "x"})
        mock_client.recall.reset_mock()
        out = provider.handle_tool_call("yantrikdb_recall", {"query": "x"})
        mock_client.recall.assert_not_called()
        err = json.loads(out)["error"].lower()
        assert "breaker" in err or "unavailable" in err

    def test_success_resets_failure_counter(
        self, provider, client_module, mock_client,
    ):
        mock_client.recall.side_effect = [
            client_module.YantrikDBTransientError("x"),
            client_module.YantrikDBTransientError("x"),
            {"results": []},
        ]
        provider.handle_tool_call("yantrikdb_recall", {"query": "x"})
        provider.handle_tool_call("yantrikdb_recall", {"query": "x"})
        provider.handle_tool_call("yantrikdb_recall", {"query": "x"})
        assert provider._failure_count == 0


# ---------------------------------------------------------------------------
# sync_turn
# ---------------------------------------------------------------------------

class TestSyncTurn:
    def test_writes_user_not_assistant(self, provider, mock_client):
        provider.sync_turn("user message", "assistant reply text")
        _wait_for_thread(provider._sync_thread)
        mock_client.remember.assert_called_once()
        call = mock_client.remember.call_args
        assert call.args[0] == "user message"
        assert "assistant reply text" not in call.args[0]
        assert call.kwargs["metadata"]["role"] == "user"

    def test_skips_empty_user_message(self, provider, mock_client):
        provider.sync_turn("", "assistant reply")
        _wait_for_thread(provider._sync_thread)
        mock_client.remember.assert_not_called()

    def test_skips_whitespace_only(self, provider, mock_client):
        provider.sync_turn("   \n\t ", "assistant reply")
        _wait_for_thread(provider._sync_thread)
        mock_client.remember.assert_not_called()


# ---------------------------------------------------------------------------
# Optional hooks
# ---------------------------------------------------------------------------

class TestOnSessionEnd:
    def test_triggers_think(self, provider, mock_client):
        provider.on_session_end([{"role": "user", "content": "hi"}])
        mock_client.think.assert_called_once()

    def test_auto_think_disabled_skips_call(self, provider, mock_client):
        provider._config.auto_think_on_session_end = False
        provider.on_session_end([])
        mock_client.think.assert_not_called()


class TestOnPreCompress:
    def test_returns_recall_block(self, provider, mock_client):
        mock_client.recall.return_value = {
            "results": [
                {"text": "important fact", "score": 0.95},
                {"text": "another fact", "score": 0.88},
            ],
        }
        messages = [{"role": "user", "content": "tell me about X"}]
        block = provider.on_pre_compress(messages)
        assert "important fact" in block
        assert "another fact" in block

    def test_empty_messages_returns_empty(self, provider, mock_client):
        assert provider.on_pre_compress([]) == ""
        mock_client.recall.assert_not_called()

    def test_no_results_returns_empty(self, provider, mock_client):
        mock_client.recall.return_value = {"results": []}
        messages = [{"role": "user", "content": "hmm"}]
        assert provider.on_pre_compress(messages) == ""


class TestOnMemoryWrite:
    def test_mirrors_user_add(self, provider, mock_client):
        provider.on_memory_write("add", "user", "Pranab likes Rust")
        # wait for spawned thread
        for _ in range(20):
            if mock_client.remember.called:
                break
            time.sleep(0.05)
        mock_client.remember.assert_called_once()
        call = mock_client.remember.call_args
        assert call.args[0] == "Pranab likes Rust"
        assert call.kwargs["domain"] == "user"

    def test_mirrors_memory_add(self, provider, mock_client):
        provider.on_memory_write("add", "memory", "Decision X was made")
        for _ in range(20):
            if mock_client.remember.called:
                break
            time.sleep(0.05)
        mock_client.remember.assert_called_once()
        assert mock_client.remember.call_args.kwargs["domain"] == "work"

    def test_skips_non_add_action(self, provider, mock_client):
        provider.on_memory_write("remove", "user", "x")
        time.sleep(0.1)
        mock_client.remember.assert_not_called()

    def test_skips_unrelated_target(self, provider, mock_client):
        provider.on_memory_write("add", "other", "x")
        time.sleep(0.1)
        mock_client.remember.assert_not_called()


# ---------------------------------------------------------------------------
# Namespace derivation
# ---------------------------------------------------------------------------

class TestDeriveNamespace:
    def test_all_three_parts(self, provider_module):
        ns = provider_module._derive_namespace(
            "hermes", {"agent_workspace": "w", "agent_identity": "i"},
        )
        assert ns == "hermes:w:i"

    def test_only_workspace(self, provider_module):
        ns = provider_module._derive_namespace("hermes", {"agent_workspace": "w"})
        assert ns == "hermes:w"

    def test_only_identity(self, provider_module):
        ns = provider_module._derive_namespace("hermes", {"agent_identity": "i"})
        assert ns == "hermes:i"

    def test_neither(self, provider_module):
        assert provider_module._derive_namespace("hermes", {}) == "hermes"


# ---------------------------------------------------------------------------
# Config schema & save_config
# ---------------------------------------------------------------------------

class TestConfigSchema:
    def test_schema_lists_required_token(self, provider_module):
        p = provider_module.YantrikDBMemoryProvider()
        fields = p.get_config_schema()
        token_field = next(f for f in fields if f["key"] == "token")
        assert token_field["secret"] is True
        assert token_field["required"] is True

    def test_save_config_writes_json(self, provider_module, tmp_path):
        p = provider_module.YantrikDBMemoryProvider()
        p.save_config({"namespace": "custom"}, str(tmp_path))
        saved = json.loads((tmp_path / "yantrikdb.json").read_text())
        assert saved["namespace"] == "custom"

    def test_save_config_merges_with_existing(self, provider_module, tmp_path):
        (tmp_path / "yantrikdb.json").write_text(json.dumps({"url": "http://x"}))
        p = provider_module.YantrikDBMemoryProvider()
        p.save_config({"namespace": "new"}, str(tmp_path))
        saved = json.loads((tmp_path / "yantrikdb.json").read_text())
        assert saved["url"] == "http://x"
        assert saved["namespace"] == "new"


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

def test_register_installs_provider(provider_module):
    collector = MagicMock()
    provider_module.register(collector)
    collector.register_memory_provider.assert_called_once()
    installed = collector.register_memory_provider.call_args.args[0]
    assert installed.__class__.__name__ == "YantrikDBMemoryProvider"
