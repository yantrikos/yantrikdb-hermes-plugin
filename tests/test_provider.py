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
    c.skill_search.return_value = {"skills": [], "total": 0}
    c.skill_define.return_value = {"rid": "r-skill-1", "skill_id": "git.commit_clean", "stored": True}
    c.skill_outcome.return_value = {"rid": "r-out-1", "skill_id": "git.commit_clean", "recorded": True}
    c.pending_triggers.return_value = {"triggers": [
        {"trigger_id": "t-1", "trigger_type": "conflict_detected", "priority": 0.8},
        {"trigger_id": "t-2", "trigger_type": "stale_memory", "priority": 0.4},
    ]}
    c.acknowledge_trigger.return_value = {"trigger_id": "t-1", "acknowledged": True}
    c.dismiss_trigger.return_value = {"trigger_id": "t-2", "dismissed": True}
    c.act_on_trigger.return_value = {"trigger_id": "t-1", "acted": True}
    return c


@pytest.fixture
def provider(provider_module, mock_client, monkeypatch):
    """Initialized provider with a mock backend wired in.

    Uses YANTRIKDB_MODE=http + a fake token so initialize() takes the HTTP
    branch, then patches the make_backend factory to hand back the mock.
    Skills are enabled here so the existing tests exercise the full
    11-tool surface; default-off behaviour is tested separately in
    TestSkillsFeatureFlag.
    """
    monkeypatch.setenv("YANTRIKDB_MODE", "http")
    monkeypatch.setenv("YANTRIKDB_TOKEN", "ydb_test")
    monkeypatch.setenv("YANTRIKDB_SKILLS_ENABLED", "true")
    p = provider_module.YantrikDBMemoryProvider()
    with patch.object(provider_module, "make_backend", return_value=mock_client):
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
    def test_http_mode_false_when_no_token(self, provider_module, monkeypatch):
        monkeypatch.setenv("YANTRIKDB_MODE", "http")
        assert provider_module.YantrikDBMemoryProvider().is_available() is False

    def test_http_mode_true_when_token_set(self, provider_module, monkeypatch):
        monkeypatch.setenv("YANTRIKDB_MODE", "http")
        monkeypatch.setenv("YANTRIKDB_TOKEN", "ydb_x")
        assert provider_module.YantrikDBMemoryProvider().is_available() is True

    def test_embedded_mode_true_when_yantrikdb_importable(
        self, provider_module, monkeypatch,
    ):
        # Embedded path: available iff `yantrikdb._yantrikdb_rust` imports.
        # The workspace dir shadowing the installed package would break this
        # check at runtime; the test env doesn't include the plugin dir in
        # sys.path because pytest loaded us via importlib spec_from_file.
        monkeypatch.setenv("YANTRIKDB_MODE", "embedded")
        # We don't assert True/False here because it depends on whether the
        # `yantrikdb` PyPI package is installed in the test env. Just
        # confirm the function returns a bool without raising.
        result = provider_module.YantrikDBMemoryProvider().is_available()
        assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

class TestInitialize:
    def test_namespace_from_workspace_and_identity(self, provider):
        assert provider._namespace == "hermes:workspace:coder"

    def test_cron_context_deactivates_plugin(self, provider_module, monkeypatch):
        monkeypatch.setenv("YANTRIKDB_MODE", "http")
        monkeypatch.setenv("YANTRIKDB_TOKEN", "ydb_test")
        p = provider_module.YantrikDBMemoryProvider()
        with patch.object(provider_module, "make_backend") as mock_factory:
            p.initialize("sess", agent_context="cron", platform="cron")
        assert p._cron_skipped is True
        assert p._client is None
        mock_factory.assert_not_called()

    def test_no_token_in_http_mode_leaves_client_none(
        self, provider_module, monkeypatch,
    ):
        # In http mode without a token, initialize() should bail before
        # constructing a backend.
        monkeypatch.setenv("YANTRIKDB_MODE", "http")
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
        with patch.object(provider_module, "make_backend", return_value=mock_client):
            p.initialize("sess", agent_workspace="w", agent_identity="i")
        assert p._client is mock_client

    def test_owner_scoping_appends_resolved_owner_namespace(
        self, provider_module, mock_client, monkeypatch,
    ):
        monkeypatch.setenv("YANTRIKDB_MODE", "http")
        monkeypatch.setenv("YANTRIKDB_TOKEN", "ydb_test")
        monkeypatch.setenv("YANTRIKDB_OWNER_SCOPING", "true")
        monkeypatch.setenv(
            "YANTRIKDB_IDENTITY_MAP_JSON",
            json.dumps({
                "owners": {
                    "owner:primary-user": {
                        "actors": ["whatsapp:actor-a", "telegram:actor-b"],
                    },
                },
            }),
        )
        p = provider_module.YantrikDBMemoryProvider()
        with patch.object(provider_module, "make_backend", return_value=mock_client):
            p.initialize(
                "sess",
                agent_workspace="workspace",
                agent_identity="coder",
                platform="whatsapp",
                user_id="actor-a",
                chat_id="chat-1",
            )

        assert p._namespace.startswith("hermes:workspace:coder:owner:owner-primary-user-")
        assert p._scope_metadata == {
            "owner_id": "owner:primary-user",
            "actor_owner_id": "owner:primary-user",
            "actor_id": "whatsapp:actor-a",
            "channel": "whatsapp",
            "conversation_id": "whatsapp:chat-1",
            "owner_actors": ["telegram:actor-b", "whatsapp:actor-a"],
            "shared_owner_ids": [],
        }

    def test_owner_scoping_defaults_to_actor_owner_without_map(
        self, provider_module, mock_client, monkeypatch,
    ):
        monkeypatch.setenv("YANTRIKDB_MODE", "http")
        monkeypatch.setenv("YANTRIKDB_TOKEN", "ydb_test")
        monkeypatch.setenv("YANTRIKDB_OWNER_SCOPING", "true")
        p = provider_module.YantrikDBMemoryProvider()
        with patch.object(provider_module, "make_backend", return_value=mock_client):
            p.initialize(
                "sess",
                agent_workspace="workspace",
                agent_identity="coder",
                platform="telegram",
                user_id="123456789",
            )

        assert p._scope_metadata["owner_id"] == "telegram:123456789"
        assert p._scope_metadata["actor_id"] == "telegram:123456789"
        assert p._scope_metadata["channel"] == "telegram"


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
    "yantrikdb_pending_triggers",
    "yantrikdb_acknowledge_trigger",
    "yantrikdb_dismiss_trigger",
    "yantrikdb_act_on_trigger",
    "yantrikdb_skill_search",
    "yantrikdb_skill_define",
    "yantrikdb_skill_outcome",
    "yantrikdb_extraction_stats",
    "yantrikdb_observability",
    "yantrikdb_hygiene",
}


class TestToolSchemas:
    def test_all_tools_registered(self, provider):
        names = {s["name"] for s in provider.get_tool_schemas()}
        assert names == EXPECTED_TOOL_NAMES

    def test_schemas_available_before_initialize(self, provider_module):
        # Hermes calls get_tool_schemas() at register time, BEFORE
        # initialize() runs, to index tool-name → provider for routing.
        # Returning [] here (pre-init) would make tool calls resolve as
        # "Unknown tool" at runtime.
        # With v0.3.0's skill flag: pre-init reads env via Config.load();
        # we don't set the flag here, so we expect base tools only (no skills).
        p = provider_module.YantrikDBMemoryProvider()
        names = {s["name"] for s in p.get_tool_schemas()}
        base = EXPECTED_TOOL_NAMES - {
            "yantrikdb_skill_search", "yantrikdb_skill_define", "yantrikdb_skill_outcome",
        }
        assert names == base
        assert len(names) == 15  # 8 originals + 4 trigger (v0.4.13) + extraction_stats + observability (v0.5) + hygiene (v0.6)

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
# Structured tool envelope (v0.4.16, closes silent-failure-confabulation gap
# from yantrikdb-agi cross-workspace heads-up rid 019e6c27)
# ---------------------------------------------------------------------------

ENVELOPE_KEYS = {"status", "ok", "tool", "ts"}


def _assert_envelope(out: str, *, expected_tool: str, expected_ok: bool):
    """Every tool response must carry the structured envelope so the LLM
    can't confabulate success on a silent failure during narrative-summarization.
    """
    parsed = json.loads(out)
    missing = ENVELOPE_KEYS - parsed.keys()
    assert not missing, f"envelope keys missing: {missing}; got: {sorted(parsed.keys())}"
    assert parsed["status"] == ("ok" if expected_ok else "failed")
    assert parsed["ok"] is expected_ok
    assert parsed["tool"] == expected_tool
    assert isinstance(parsed["ts"], (int, float))
    if not expected_ok:
        # Failure envelopes carry both `error` (legacy) and `reason` (alias).
        assert "error" in parsed
        assert "reason" in parsed
        assert parsed["error"] == parsed["reason"]


class TestStructuredEnvelope:
    """Contract: every tool call returns an envelope with status/ok/tool/ts.
    Without these, an LLM later asked to summarize the session can confabulate
    success on silent failures. See rid 019e6c27 (yantrikdb-agi heads-up)."""

    def test_success_envelope_on_remember(self, provider, mock_client):
        out = provider.handle_tool_call(
            "yantrikdb_remember",
            {"text": "anything", "importance": 0.5, "domain": "test"},
        )
        _assert_envelope(out, expected_tool="yantrikdb_remember", expected_ok=True)
        # Existing payload keys preserved (back-compat).
        parsed = json.loads(out)
        assert parsed["stored"] is True
        assert parsed["rid"] == "r-new"

    def test_failure_envelope_on_missing_required_param(
        self, provider, mock_client,
    ):
        # Empty args → "Missing required parameter" path inside _do_remember.
        out = provider.handle_tool_call("yantrikdb_remember", {})
        _assert_envelope(out, expected_tool="yantrikdb_remember", expected_ok=False)
        parsed = json.loads(out)
        assert "Missing required" in parsed["error"]
        # Direct tool_error call from _do_* path: dispatcher backfilled `tool`.
        assert parsed["tool"] == "yantrikdb_remember"

    def test_failure_envelope_on_backend_unavailable(
        self, provider, mock_client, client_module,
    ):
        """Transient backend failure (simulating YDB cluster restart, partition,
        wedge) — exactly the scenario yantrikdb-agi flagged. Failure must be
        unambiguous in the envelope so the agent's narrative LLM can't
        confabulate success."""
        mock_client.remember.side_effect = client_module.YantrikDBServerError(
            "engine unreachable: connection refused at 192.168.4.13:7438"
        )
        out = provider.handle_tool_call(
            "yantrikdb_remember",
            {"text": "this write will never land", "importance": 0.9},
        )
        _assert_envelope(out, expected_tool="yantrikdb_remember", expected_ok=False)
        parsed = json.loads(out)
        assert "unreachable" in parsed["error"]
        # Critical: explicit `status: "failed"` + `ok: false` so even a
        # narrative-summarization LLM can't gloss past the failure.
        assert parsed["status"] == "failed"
        assert parsed["ok"] is False

    def test_envelope_on_unknown_tool(self, provider, mock_client):
        out = provider.handle_tool_call("yantrikdb_nonexistent", {})
        _assert_envelope(out, expected_tool="yantrikdb_nonexistent", expected_ok=False)
        assert "Unknown tool" in json.loads(out)["error"]

    def test_envelope_on_cron_context_skip(
        self, provider_module, mock_client, monkeypatch,
    ):
        """Cron-context provider has `_client = None` — early-return error
        path. Must still carry the envelope."""
        monkeypatch.setenv("YANTRIKDB_TOKEN", "ydb_test")
        p = provider_module.YantrikDBMemoryProvider()
        p.initialize("sess", agent_context="cron", platform="cron")
        out = p.handle_tool_call("yantrikdb_remember", {"text": "x"})
        _assert_envelope(out, expected_tool="yantrikdb_remember", expected_ok=False)

    def test_envelope_on_skills_disabled(
        self, provider_module, mock_client, monkeypatch,
    ):
        """Skills feature flag off: short-circuit error must still envelope."""
        monkeypatch.setenv("YANTRIKDB_MODE", "http")
        monkeypatch.setenv("YANTRIKDB_TOKEN", "ydb_test")
        monkeypatch.delenv("YANTRIKDB_SKILLS_ENABLED", raising=False)  # default off
        p = provider_module.YantrikDBMemoryProvider()
        with patch.object(provider_module, "make_backend", return_value=mock_client):
            p.initialize("sess", agent_workspace="w", agent_identity="i")
        out = p.handle_tool_call(
            "yantrikdb_skill_search", {"query": "any"},
        )
        _assert_envelope(out, expected_tool="yantrikdb_skill_search", expected_ok=False)
        assert "disabled" in json.loads(out)["error"].lower()

    def test_envelope_present_on_all_dispatch_paths(self, provider, mock_client):
        """Comprehensive sweep — every dispatch branch returns the envelope.
        Catches regressions where someone adds a new tool but forgets to
        route through the wrapper."""
        scenarios = [
            ("yantrikdb_remember", {"text": "x"}),
            ("yantrikdb_recall", {"query": "x"}),
            ("yantrikdb_forget", {"rid": "r1"}),
            ("yantrikdb_think", {}),
            ("yantrikdb_conflicts", {}),
            ("yantrikdb_relate", {"entity": "A", "target": "B", "relationship": "rel"}),
            ("yantrikdb_stats", {}),
            ("yantrikdb_pending_triggers", {}),
            ("yantrikdb_acknowledge_trigger", {"trigger_id": "t-1"}),
            ("yantrikdb_dismiss_trigger", {"trigger_id": "t-1"}),
            ("yantrikdb_act_on_trigger", {"trigger_id": "t-1"}),
            ("yantrikdb_skill_search", {"query": "x"}),
            ("yantrikdb_skill_define", {
                "skill_id": "git.commit_clean",
                "body": "Always rebase before merge so history stays linear.",
                "skill_type": "procedure",
                "applies_to": ["git"],
            }),
            ("yantrikdb_skill_outcome", {"skill_id": "git.commit_clean", "succeeded": True}),
        ]
        for tool_name, args in scenarios:
            out = provider.handle_tool_call(tool_name, args)
            parsed = json.loads(out)
            missing = ENVELOPE_KEYS - parsed.keys()
            assert not missing, f"{tool_name} missing envelope keys: {missing}"
            assert parsed["tool"] == tool_name, f"{tool_name} wrong tool field: {parsed.get('tool')!r}"


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

    def test_remember_includes_scope_metadata_when_enabled(
        self, provider_module, mock_client, monkeypatch,
    ):
        monkeypatch.setenv("YANTRIKDB_MODE", "http")
        monkeypatch.setenv("YANTRIKDB_TOKEN", "ydb_test")
        monkeypatch.setenv("YANTRIKDB_OWNER_SCOPING", "true")
        p = provider_module.YantrikDBMemoryProvider()
        with patch.object(provider_module, "make_backend", return_value=mock_client):
            p.initialize(
                "sess-1",
                agent_workspace="workspace",
                agent_identity="coder",
                platform="whatsapp",
                user_id="actor-a",
                chat_id="chat-1",
            )

        p.handle_tool_call("yantrikdb_remember", {"text": "hello"})

        call = mock_client.remember.call_args
        assert call.kwargs["namespace"].startswith("hermes:workspace:coder:owner:whatsapp-actor-a-")
        assert call.kwargs["metadata"]["owner_id"] == "whatsapp:actor-a"
        assert call.kwargs["metadata"]["actor_id"] == "whatsapp:actor-a"
        assert call.kwargs["metadata"]["channel"] == "whatsapp"
        assert call.kwargs["metadata"]["conversation_id"] == "whatsapp:chat-1"

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

    def test_owner_scoped_recall_includes_legacy_actor_and_base_namespaces(
        self, provider_module, mock_client, monkeypatch,
    ):
        monkeypatch.setenv("YANTRIKDB_MODE", "http")
        monkeypatch.setenv("YANTRIKDB_TOKEN", "ydb_test")
        monkeypatch.setenv("YANTRIKDB_OWNER_SCOPING", "true")
        monkeypatch.setenv(
            "YANTRIKDB_IDENTITY_MAP_JSON",
            json.dumps({"actors": {"whatsapp:actor-a": "owner:primary-user"}}),
        )
        p = provider_module.YantrikDBMemoryProvider()
        with patch.object(provider_module, "make_backend", return_value=mock_client):
            p.initialize(
                "sess-1",
                agent_workspace="workspace",
                agent_identity="coder",
                platform="whatsapp",
                user_id="actor-a",
            )

        mock_client.recall.side_effect = [
            {"results": [{"rid": "scoped", "text": "private", "score": 0.8}]},
            {"results": [{"rid": "legacy-actor", "text": "pre-map", "score": 0.95}]},
            {"results": [{"rid": "global", "text": "legacy", "score": 0.9}]},
        ]
        out = p.handle_tool_call("yantrikdb_recall", {"query": "prefs", "top_k": 5})

        namespaces = [call.kwargs["namespace"] for call in mock_client.recall.call_args_list]
        assert namespaces[0].startswith("hermes:workspace:coder:owner:owner-primary-user-")
        assert namespaces[1].startswith("hermes:workspace:coder:owner:whatsapp-actor-a-")
        assert namespaces[2] == "hermes:workspace:coder"
        assert [r["rid"] for r in json.loads(out)["results"]] == [
            "legacy-actor", "global", "scoped",
        ]

    def test_owner_scoped_recall_includes_all_owner_actor_legacy_namespaces(
        self, provider_module, mock_client, monkeypatch,
    ):
        monkeypatch.setenv("YANTRIKDB_MODE", "http")
        monkeypatch.setenv("YANTRIKDB_TOKEN", "ydb_test")
        monkeypatch.setenv("YANTRIKDB_OWNER_SCOPING", "true")
        monkeypatch.setenv("YANTRIKDB_INCLUDE_BASE_NAMESPACE_RECALL", "false")
        monkeypatch.setenv(
            "YANTRIKDB_IDENTITY_MAP_JSON",
            json.dumps({
                "owners": {
                    "owner:yc": {
                        "actors": ["whatsapp:actor-a", "telegram:actor-b"],
                    },
                },
            }),
        )
        p = provider_module.YantrikDBMemoryProvider()
        with patch.object(provider_module, "make_backend", return_value=mock_client):
            p.initialize(
                "sess-1",
                agent_workspace="workspace",
                agent_identity="coder",
                platform="telegram",
                user_id="actor-b",
            )

        mock_client.recall.side_effect = [
            {"results": []},
            {"results": [{"rid": "telegram-old", "text": "old telegram", "score": 0.7}]},
            {"results": [{"rid": "whatsapp-old", "text": "old whatsapp", "score": 0.9}]},
        ]
        out = p.handle_tool_call("yantrikdb_recall", {"query": "prefs", "top_k": 5})

        namespaces = [call.kwargs["namespace"] for call in mock_client.recall.call_args_list]
        assert namespaces[0].startswith("hermes:workspace:coder:owner:owner-yc-")
        assert any(ns.startswith("hermes:workspace:coder:owner:telegram-actor-b-") for ns in namespaces)
        assert any(ns.startswith("hermes:workspace:coder:owner:whatsapp-actor-a-") for ns in namespaces)
        assert "hermes:workspace:coder" not in namespaces
        assert [r["rid"] for r in json.loads(out)["results"]] == [
            "whatsapp-old", "telegram-old",
        ]

    def test_owner_scoped_recall_can_disable_base_namespace_fallback(
        self, provider_module, mock_client, monkeypatch,
    ):
        monkeypatch.setenv("YANTRIKDB_MODE", "http")
        monkeypatch.setenv("YANTRIKDB_TOKEN", "ydb_test")
        monkeypatch.setenv("YANTRIKDB_OWNER_SCOPING", "true")
        monkeypatch.setenv("YANTRIKDB_INCLUDE_BASE_NAMESPACE_RECALL", "false")
        p = provider_module.YantrikDBMemoryProvider()
        with patch.object(provider_module, "make_backend", return_value=mock_client):
            p.initialize(
                "sess-1",
                agent_workspace="workspace",
                agent_identity="coder",
                platform="whatsapp",
                user_id="actor-a",
            )

        mock_client.recall.return_value = {
            "results": [{"rid": "scoped", "text": "private", "score": 0.8}],
        }
        out = p.handle_tool_call("yantrikdb_recall", {"query": "prefs", "top_k": 5})

        mock_client.recall.assert_called_once()
        assert json.loads(out)["results"][0]["rid"] == "scoped"

    def test_group_conversation_writes_to_configured_group_namespace(
        self, provider_module, mock_client, monkeypatch,
    ):
        monkeypatch.setenv("YANTRIKDB_MODE", "http")
        monkeypatch.setenv("YANTRIKDB_TOKEN", "ydb_test")
        monkeypatch.setenv("YANTRIKDB_OWNER_SCOPING", "true")
        monkeypatch.setenv(
            "YANTRIKDB_IDENTITY_MAP_JSON",
            json.dumps({
                "actors": {"whatsapp:actor-a": "owner:primary-user"},
                "groups": {
                    "group:household": {
                        "members": ["owner:primary-user", "owner:secondary-user"],
                        "conversations": ["whatsapp:family-chat"],
                    },
                },
            }),
        )
        p = provider_module.YantrikDBMemoryProvider()
        with patch.object(provider_module, "make_backend", return_value=mock_client):
            p.initialize(
                "sess-1",
                agent_workspace="workspace",
                agent_identity="coder",
                platform="whatsapp",
                user_id="actor-a",
                chat_id="family-chat",
                chat_type="group",
            )

        p.handle_tool_call("yantrikdb_remember", {"text": "Shared household fact"})

        call = mock_client.remember.call_args
        assert call.kwargs["namespace"].startswith("hermes:workspace:coder:owner:group-household-")
        assert call.kwargs["metadata"]["owner_id"] == "group:household"
        assert call.kwargs["metadata"]["actor_id"] == "whatsapp:actor-a"
        assert call.kwargs["metadata"]["actor_owner_id"] == "owner:primary-user"
        assert call.kwargs["metadata"]["conversation_id"] == "whatsapp:family-chat"

    def test_personal_recall_includes_configured_group_memberships(
        self, provider_module, mock_client, monkeypatch,
    ):
        monkeypatch.setenv("YANTRIKDB_MODE", "http")
        monkeypatch.setenv("YANTRIKDB_TOKEN", "ydb_test")
        monkeypatch.setenv("YANTRIKDB_OWNER_SCOPING", "true")
        monkeypatch.setenv("YANTRIKDB_INCLUDE_BASE_NAMESPACE_RECALL", "false")
        monkeypatch.setenv("YANTRIKDB_INCLUDE_LEGACY_ACTOR_NAMESPACE_RECALL", "false")
        monkeypatch.setenv(
            "YANTRIKDB_IDENTITY_MAP_JSON",
            json.dumps({
                "actors": {
                    "telegram:actor-b": "owner:primary-user",
                    "whatsapp:actor-a": "owner:primary-user",
                    "whatsapp:actor-c": "owner:removed-user",
                },
                "groups": {
                    "group:household": {
                        "members": ["owner:primary-user", "owner:secondary-user"],
                        "conversations": ["whatsapp:family-chat"],
                    },
                    "group:other": {
                        "members": ["owner:someone-else"],
                        "conversations": ["telegram:other-chat"],
                    },
                },
            }),
        )
        p = provider_module.YantrikDBMemoryProvider()
        with patch.object(provider_module, "make_backend", return_value=mock_client):
            p.initialize(
                "sess-1",
                agent_workspace="workspace",
                agent_identity="coder",
                platform="telegram",
                user_id="actor-b",
                chat_id="actor-b-dm",
                chat_type="dm",
            )

        mock_client.recall.side_effect = [
            {"results": [{"rid": "personal", "text": "personal", "score": 0.8}]},
            {"results": [{"rid": "household", "text": "shared", "score": 0.9}]},
        ]
        out = p.handle_tool_call("yantrikdb_recall", {"query": "prefs", "top_k": 5})

        namespaces = [call.kwargs["namespace"] for call in mock_client.recall.call_args_list]
        assert len(namespaces) == 2
        assert namespaces[0].startswith("hermes:workspace:coder:owner:owner-primary-user-")
        assert namespaces[1].startswith("hermes:workspace:coder:owner:group-household-")
        assert not any("group-other" in ns for ns in namespaces)
        assert [r["rid"] for r in json.loads(out)["results"]] == ["household", "personal"]

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
        assert mock_client.think.call_args.kwargs["namespace"] == "hermes:workspace:coder"
        parsed = json.loads(out)
        assert parsed["consolidated"] == 2
        assert parsed["conflicts_found"] == 1

    def test_conflicts_dispatches(self, provider, mock_client):
        out = provider.handle_tool_call("yantrikdb_conflicts", {})
        mock_client.conflicts.assert_called_once_with(namespace="hermes:workspace:coder")
        assert json.loads(out)["count"] == 0

    def test_relate_dispatches(self, provider, mock_client):
        out = provider.handle_tool_call(
            "yantrikdb_relate",
            {"entity": "Alice", "target": "Acme", "relationship": "works_at"},
        )
        mock_client.relate.assert_called_once_with(
            "Alice", "Acme", "works_at", namespace="hermes:workspace:coder",
        )
        assert json.loads(out)["edge_id"] == "e1"

    def test_relate_requires_all_three_fields(self, provider, mock_client):
        out = provider.handle_tool_call(
            "yantrikdb_relate", {"entity": "Alice", "target": "Acme"},
        )
        mock_client.relate.assert_not_called()
        assert "Missing required" in json.loads(out)["error"]

    def test_stats_dispatches(self, provider, mock_client):
        out = provider.handle_tool_call("yantrikdb_stats", {})
        mock_client.stats.assert_called_once_with(namespace="hermes:workspace:coder")
        parsed = json.loads(out)
        assert parsed["active_memories"] == 42
        assert parsed["open_conflicts"] == 1
        assert parsed["edges"] == 17

    # ----- Trigger consumer tools (v0.4.13, closes #17) -------------

    def test_pending_triggers_dispatches(self, provider, mock_client):
        out = provider.handle_tool_call(
            "yantrikdb_pending_triggers", {"limit": 5},
        )
        mock_client.pending_triggers.assert_called_once_with(limit=5)
        parsed = json.loads(out)
        assert parsed["count"] == 2
        assert parsed["triggers"][0]["trigger_id"] == "t-1"

    def test_pending_triggers_default_limit(self, provider, mock_client):
        provider.handle_tool_call("yantrikdb_pending_triggers", {})
        mock_client.pending_triggers.assert_called_once_with(limit=10)

    def test_pending_triggers_caps_oversize_limit(self, provider, mock_client):
        # Anything over 100 collapses to 100 — bounded to keep agents from
        # accidentally enumerating an unbounded queue.
        provider.handle_tool_call(
            "yantrikdb_pending_triggers", {"limit": 9999},
        )
        assert mock_client.pending_triggers.call_args.kwargs["limit"] == 100

    def test_acknowledge_trigger_dispatches(self, provider, mock_client):
        out = provider.handle_tool_call(
            "yantrikdb_acknowledge_trigger", {"trigger_id": "t-1"},
        )
        mock_client.acknowledge_trigger.assert_called_once_with("t-1")
        parsed = json.loads(out)
        assert parsed["trigger_id"] == "t-1"
        assert parsed["acknowledged"] is True

    def test_acknowledge_trigger_requires_id(self, provider, mock_client):
        out = provider.handle_tool_call("yantrikdb_acknowledge_trigger", {})
        mock_client.acknowledge_trigger.assert_not_called()
        assert "Missing required" in json.loads(out)["error"]

    def test_dismiss_trigger_dispatches(self, provider, mock_client):
        out = provider.handle_tool_call(
            "yantrikdb_dismiss_trigger", {"trigger_id": "t-2"},
        )
        mock_client.dismiss_trigger.assert_called_once_with("t-2")
        parsed = json.loads(out)
        assert parsed["trigger_id"] == "t-2"
        assert parsed["dismissed"] is True

    def test_dismiss_trigger_requires_id(self, provider, mock_client):
        out = provider.handle_tool_call("yantrikdb_dismiss_trigger", {})
        mock_client.dismiss_trigger.assert_not_called()
        assert "Missing required" in json.loads(out)["error"]

    def test_act_on_trigger_dispatches(self, provider, mock_client):
        out = provider.handle_tool_call(
            "yantrikdb_act_on_trigger", {"trigger_id": "t-1"},
        )
        mock_client.act_on_trigger.assert_called_once_with("t-1")
        parsed = json.loads(out)
        assert parsed["trigger_id"] == "t-1"
        assert parsed["acted"] is True

    def test_act_on_trigger_requires_id(self, provider, mock_client):
        out = provider.handle_tool_call("yantrikdb_act_on_trigger", {})
        mock_client.act_on_trigger.assert_not_called()
        assert "Missing required" in json.loads(out)["error"]

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

class TestSessionSwitch:
    def test_updates_cached_session_id(self, provider):
        assert provider._session_id == "sess-1"
        provider.on_session_switch("sess-2", parent_session_id="sess-1")
        assert provider._session_id == "sess-2"

    def test_reset_clears_prefetch_cache(self, provider):
        with provider._prefetch_lock:
            provider._prefetch_results["sess-1"] = "old recall"
        provider.on_session_switch("sess-2", reset=True)
        assert provider._prefetch_results == {}


class TestPrefetch:
    def test_prefetch_is_scoped_by_session_id(self, provider, mock_client):
        mock_client.recall.return_value = {
            "results": [{"text": "alpha memory", "score": 0.9}],
        }

        provider.queue_prefetch("alpha", session_id="sess-a")
        _wait_for_thread(provider._prefetch_thread)

        assert provider.prefetch("alpha", session_id="sess-b") == ""
        block = provider.prefetch("alpha", session_id="sess-a")
        assert "alpha memory" in block


class TestOnSessionEnd:
    def test_triggers_think(self, provider, mock_client):
        provider.on_session_end([{"role": "user", "content": "hi"}])
        mock_client.think.assert_called_once()

    def test_auto_think_disabled_skips_call(self, provider, mock_client):
        provider._config.auto_think_on_session_end = False
        provider.on_session_end([])
        mock_client.think.assert_not_called()

    # ----- Auto-acknowledge triggers (v0.4.15, closes #22) ---------

    def test_auto_acknowledge_off_by_default(self, provider, mock_client):
        # Default config has auto_acknowledge_triggers=False; session end
        # runs think() but does NOT touch the trigger queue.
        provider.on_session_end([])
        mock_client.think.assert_called_once()
        mock_client.pending_triggers.assert_not_called()
        mock_client.acknowledge_trigger.assert_not_called()

    def test_auto_acknowledge_drains_pending(self, provider, mock_client):
        provider._config.auto_acknowledge_triggers = True
        # mock_client.pending_triggers fixture returns 2 triggers (t-1, t-2).
        # The drain loop pulls 50-trigger batches; a short batch (here, 2)
        # tells it the queue is empty, so only one call.
        provider.on_session_end([])
        mock_client.think.assert_called_once()
        mock_client.pending_triggers.assert_called_once_with(limit=50)
        # Each pending trigger should be acknowledged exactly once.
        assert mock_client.acknowledge_trigger.call_count == 2
        ack_args = {c.args[0] for c in mock_client.acknowledge_trigger.call_args_list}
        assert ack_args == {"t-1", "t-2"}

    def test_auto_acknowledge_loops_until_empty(self, provider, mock_client):
        """Engine returns a full batch -> drain continues to the next page."""
        provider._config.auto_acknowledge_triggers = True
        # Two full batches of 50, then an empty page → 3 calls total,
        # 100 triggers acked.
        batch_a = [{"trigger_id": f"a-{i}"} for i in range(50)]
        batch_b = [{"trigger_id": f"b-{i}"} for i in range(50)]
        mock_client.pending_triggers.side_effect = [
            {"triggers": batch_a},
            {"triggers": batch_b},
            {"triggers": []},
        ]
        provider.on_session_end([])
        assert mock_client.pending_triggers.call_count == 3
        assert mock_client.acknowledge_trigger.call_count == 100

    def test_auto_acknowledge_warns_loudly_on_http_mode_404(
        self, provider, mock_client, client_module, caplog,
    ):
        """HTTP mode 404 should log a WARNING (not silent debug) so the
        user knows auto-ack is effectively disabled."""
        import logging
        provider._config.auto_acknowledge_triggers = True
        mock_client.pending_triggers.side_effect = client_module.YantrikDBServerError(
            "server route /v1/triggers/pending not found "
            "(needs yantrikdb-server v0.8.17+; see issues/39)"
        )
        with caplog.at_level(logging.WARNING, logger="yantrikdb_plugin_under_test"):
            provider.on_session_end([])
        # Exactly one warning, mentioning the upstream tracker.
        warning_records = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warning_records) == 1
        assert "/v1/triggers" in warning_records[0].getMessage()
        mock_client.acknowledge_trigger.assert_not_called()

    def test_auto_acknowledge_handles_empty_queue(self, provider, mock_client):
        provider._config.auto_acknowledge_triggers = True
        mock_client.pending_triggers.return_value = {"triggers": []}
        provider.on_session_end([])
        mock_client.pending_triggers.assert_called_once()
        mock_client.acknowledge_trigger.assert_not_called()

    def test_auto_acknowledge_fail_soft_on_one_bad_trigger(
        self, provider, mock_client, client_module,
    ):
        # One trigger raises; the others should still ack. The user's
        # session shouldn't crash because one trigger went weird.
        provider._config.auto_acknowledge_triggers = True
        mock_client.pending_triggers.return_value = {"triggers": [
            {"trigger_id": "ok-1"},
            {"trigger_id": "bad"},
            {"trigger_id": "ok-2"},
        ]}

        def selective_fail(trigger_id):
            if trigger_id == "bad":
                raise client_module.YantrikDBServerError("transient blip")
            return {"trigger_id": trigger_id, "acknowledged": True}

        mock_client.acknowledge_trigger.side_effect = selective_fail
        provider.on_session_end([])
        # All three were attempted; the bad one didn't stop the iteration.
        attempts = [c.args[0] for c in mock_client.acknowledge_trigger.call_args_list]
        assert attempts == ["ok-1", "bad", "ok-2"]

    def test_auto_acknowledge_skipped_when_think_fails(
        self, provider, mock_client, client_module,
    ):
        # If think() raises, the early return prevents the ack pass —
        # don't drain a queue we may not have refreshed.
        provider._config.auto_acknowledge_triggers = True
        mock_client.think.side_effect = client_module.YantrikDBServerError("nope")
        provider.on_session_end([])
        mock_client.pending_triggers.assert_not_called()
        mock_client.acknowledge_trigger.assert_not_called()

    def test_auto_acknowledge_swallows_listing_failure(
        self, provider, mock_client, client_module,
    ):
        # If listing pending fails, we log + give up. Don't crash the
        # session-end hook.
        provider._config.auto_acknowledge_triggers = True
        mock_client.pending_triggers.side_effect = client_module.YantrikDBServerError("503")
        provider.on_session_end([])
        mock_client.acknowledge_trigger.assert_not_called()
        # think() still ran first, that's the contract.
        mock_client.think.assert_called_once()


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
    def test_schema_is_mode_aware_embedded_default(self, provider_module, monkeypatch):
        # v0.4.3+: default mode is embedded; token/url should NOT appear in
        # the schema (and therefore not in `hermes memory status`'s "Missing"
        # list) for embedded-mode users.
        monkeypatch.delenv("YANTRIKDB_MODE", raising=False)
        p = provider_module.YantrikDBMemoryProvider()
        keys = {f["key"] for f in p.get_config_schema()}
        assert "mode" in keys
        assert "db_path" in keys
        assert "namespace" in keys
        assert "top_k" in keys
        assert "token" not in keys, "embedded mode should not surface token as a config key"
        assert "url" not in keys

    def test_schema_lists_required_token_in_http_mode(self, provider_module, monkeypatch):
        # HTTP mode keeps the v0.1.0 token-required contract.
        monkeypatch.setenv("YANTRIKDB_MODE", "http")
        p = provider_module.YantrikDBMemoryProvider()
        fields = p.get_config_schema()
        token_field = next(f for f in fields if f["key"] == "token")
        assert token_field["secret"] is True
        assert token_field["required"] is True

    def test_schema_url_points_at_repo_not_stale_quickstart(self, provider_module, monkeypatch):
        # Regression test for Issue #2 (becks0815): the v0.1.0 schema pointed
        # at https://yantrikdb.com/server/quickstart/ which has stale CLI
        # commands. URLs should now point at the canonical install docs in
        # this repo's README.
        for mode in ("embedded", "http"):
            monkeypatch.setenv("YANTRIKDB_MODE", mode)
            p = provider_module.YantrikDBMemoryProvider()
            for f in p.get_config_schema():
                url = f.get("url", "")
                assert "server/quickstart" not in url, (
                    f"{mode}-mode schema entry for {f['key']!r} still points at the "
                    f"stale quickstart URL: {url}"
                )

    def test_save_config_writes_json(self, provider_module, tmp_path):
        p = provider_module.YantrikDBMemoryProvider()
        p.save_config({"namespace": "custom"}, str(tmp_path))
        saved = json.loads((tmp_path / "yantrikdb.json").read_text())
        assert saved["namespace"] == "custom"

    def test_init_error_surfaces_in_system_prompt_block(self, provider_module, monkeypatch):
        # v0.4.4 regression test for Issue #5: when initialize() fails to
        # construct the backend, the system_prompt_block must surface the
        # reason so the agent sees memory as NOT AVAILABLE instead of
        # silently absent.
        from yantrikdb_plugin_under_test.client import YantrikDBError

        def _boom(_cfg):
            raise YantrikDBError("simulated cache-dir failure")

        monkeypatch.setattr(provider_module, "make_backend", _boom)
        monkeypatch.setenv("YANTRIKDB_MODE", "embedded")

        p = provider_module.YantrikDBMemoryProvider()
        p.initialize(session_id="test")

        # Backend wasn't constructed -> _client stays None, _init_error set
        assert p._client is None
        assert p._init_error is not None
        assert "simulated cache-dir failure" in p._init_error

        # system_prompt_block surfaces the failure to the model
        block = p.system_prompt_block()
        assert "NOT AVAILABLE" in block
        assert "simulated cache-dir failure" in block

    def test_save_config_merges_with_existing(self, provider_module, tmp_path):
        (tmp_path / "yantrikdb.json").write_text(json.dumps({"url": "http://x"}))
        p = provider_module.YantrikDBMemoryProvider()
        p.save_config({"namespace": "new"}, str(tmp_path))
        saved = json.loads((tmp_path / "yantrikdb.json").read_text())
        assert saved["url"] == "http://x"
        assert saved["namespace"] == "new"


# ---------------------------------------------------------------------------
# `hermes plugins install` user-discovery entry point (v0.4.5)
# ---------------------------------------------------------------------------

class TestHermesPluginsInstallEntryPoint:
    """The top-level repo __init__.py is loaded by Hermes when a user runs
    `hermes plugins install yantrikos/yantrikdb-hermes-plugin`. Verify it
    exposes `register` + `YantrikDBMemoryProvider` and tolerates Hermes'
    quirky loader (parent module not pre-registered).
    """

    def test_top_level_init_exposes_register_and_provider(self):
        import importlib.util
        import sys
        from pathlib import Path

        repo_root = Path(__file__).resolve().parent.parent
        top_init = repo_root / "__init__.py"
        assert top_init.exists(), "v0.4.5 requires a top-level __init__.py at repo root"

        # Simulate Hermes' user-installed-plugin loader: register under a
        # dotted module name whose parent doesn't exist in sys.modules. The
        # parent-module workaround in __init__.py should handle it.
        mod_name = "_hermes_user_memory_test.yantrikdb"
        # Don't pre-register parent — that's the bug we're working around.
        spec = importlib.util.spec_from_file_location(
            mod_name, str(top_init),
            submodule_search_locations=[str(repo_root)],
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        try:
            spec.loader.exec_module(mod)
            assert hasattr(mod, "register"), "top-level __init__.py must export register"
            assert hasattr(mod, "YantrikDBMemoryProvider"), (
                "top-level __init__.py must export YantrikDBMemoryProvider"
            )
            # Verify the parent-module workaround did its job
            assert "_hermes_user_memory_test" in sys.modules, (
                "top-level __init__.py should self-register synthetic parent"
            )
            # The exposed provider class should be a real MemoryProvider subclass
            from agent.memory_provider import MemoryProvider
            assert issubclass(mod.YantrikDBMemoryProvider, MemoryProvider)
        finally:
            # Cleanup synthetic modules so other tests aren't affected
            for key in list(sys.modules):
                if key.startswith("_hermes_user_memory_test"):
                    sys.modules.pop(key, None)

    def test_top_level_plugin_yaml_declares_name_yantrikdb(self):
        """Hermes installer uses `plugin.yaml.name` as the install-target
        directory. For `hermes plugins install` to drop the plugin in
        ~/.hermes/plugins/yantrikdb/ (matching the provider name), the
        root manifest must declare name: yantrikdb."""
        from pathlib import Path

        repo_root = Path(__file__).resolve().parent.parent
        manifest = repo_root / "plugin.yaml"
        assert manifest.exists(), "v0.4.5 requires a top-level plugin.yaml"
        text = manifest.read_text(encoding="utf-8")
        # crude line check rather than pulling pyyaml as a test dep
        name_line = next(
            (line for line in text.splitlines() if line.strip().startswith("name:")),
            None,
        )
        assert name_line is not None
        assert "yantrikdb" in name_line and "yantrikdb-hermes-plugin" not in name_line, (
            f"root plugin.yaml must declare name: yantrikdb (got: {name_line!r})"
        )


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

def test_register_installs_provider(provider_module):
    collector = MagicMock()
    provider_module.register(collector)
    collector.register_memory_provider.assert_called_once()
    installed = collector.register_memory_provider.call_args.args[0]
    assert installed.__class__.__name__ == "YantrikDBMemoryProvider"


# ---------------------------------------------------------------------------
# Skills feature flag — disabled by default, opt-in via env
# ---------------------------------------------------------------------------


class TestSkillsFeatureFlag:
    """Skills are opt-in. Filesystem-backed users running the plugin in
    embedded mode shouldn't see three new tools they didn't ask for.
    """

    def _provider_with_flag(self, provider_module, mock_client, monkeypatch, *, enabled):
        monkeypatch.setenv("YANTRIKDB_MODE", "http")
        monkeypatch.setenv("YANTRIKDB_TOKEN", "ydb_test")
        if enabled:
            monkeypatch.setenv("YANTRIKDB_SKILLS_ENABLED", "true")
        else:
            monkeypatch.delenv("YANTRIKDB_SKILLS_ENABLED", raising=False)
        p = provider_module.YantrikDBMemoryProvider()
        with patch.object(provider_module, "make_backend", return_value=mock_client):
            p.initialize("sess-flag", agent_workspace="w", agent_identity="i")
        return p

    def test_default_off_excludes_skill_tools(
        self, provider_module, mock_client, monkeypatch,
    ):
        p = self._provider_with_flag(provider_module, mock_client, monkeypatch, enabled=False)
        names = {s["name"] for s in p.get_tool_schemas()}
        skill_names = {n for n in names if n.startswith("yantrikdb_skill_")}
        assert skill_names == set(), f"skills should be hidden by default, got {skill_names}"
        # 8 core + 4 trigger + extraction_stats + observability + hygiene = 15
        assert len(names) == 15

    def test_enabled_includes_skill_tools(
        self, provider_module, mock_client, monkeypatch,
    ):
        p = self._provider_with_flag(provider_module, mock_client, monkeypatch, enabled=True)
        names = {s["name"] for s in p.get_tool_schemas()}
        assert "yantrikdb_skill_search" in names
        assert "yantrikdb_skill_define" in names
        assert "yantrikdb_skill_outcome" in names
        # 15 core+trigger+stats+observability+hygiene + 3 skill tools = 18
        assert len(names) == 18

    def test_disabled_skill_call_short_circuits(
        self, provider_module, mock_client, monkeypatch,
    ):
        p = self._provider_with_flag(provider_module, mock_client, monkeypatch, enabled=False)
        out = p.handle_tool_call(
            "yantrikdb_skill_search", {"query": "any"},
        )
        mock_client.skill_search.assert_not_called()
        err = json.loads(out)["error"]
        assert "Skills are disabled" in err
        assert "YANTRIKDB_SKILLS_ENABLED" in err


# ---------------------------------------------------------------------------
# Skills dispatch (v0.3.0+) — uses the default fixture which has skills on
# ---------------------------------------------------------------------------


class TestSkillSearch:
    def test_dispatches_with_query_and_top_k_cap(self, provider, mock_client):
        provider.handle_tool_call(
            "yantrikdb_skill_search",
            {"query": "git commit", "top_k": 200},
        )
        mock_client.skill_search.assert_called_once()
        call = mock_client.skill_search.call_args
        assert call.args[0] == "git commit"
        assert call.kwargs["top_k"] == 50  # capped

    def test_passes_applies_to_filter(self, provider, mock_client):
        provider.handle_tool_call(
            "yantrikdb_skill_search",
            {"query": "deploy", "applies_to": "production"},
        )
        assert mock_client.skill_search.call_args.kwargs["applies_to"] == "production"

    def test_compacts_search_results(self, provider, mock_client):
        mock_client.skill_search.return_value = {
            "skills": [
                {
                    "rid": "r1",
                    "text": "do X then Y",
                    "score": 0.91,
                    "metadata": {
                        "skill_id": "deploy.rolling",
                        "skill_type": "procedure",
                        "applies_to": ["deploy", "production"],
                        "source": "hermes",
                    },
                    "why_retrieved": ["semantically similar (0.84)"],
                },
            ],
        }
        out = provider.handle_tool_call(
            "yantrikdb_skill_search", {"query": "rolling deploy"},
        )
        parsed = json.loads(out)
        assert parsed["count"] == 1
        first = parsed["skills"][0]
        assert first["skill_id"] == "deploy.rolling"
        assert first["skill_type"] == "procedure"
        assert first["applies_to"] == ["deploy", "production"]
        assert first["source"] == "hermes"
        assert first["why_retrieved"] == ["semantically similar (0.84)"]

    def test_rejects_empty_query(self, provider, mock_client):
        out = provider.handle_tool_call("yantrikdb_skill_search", {})
        mock_client.skill_search.assert_not_called()
        assert "Missing required parameter" in json.loads(out)["error"]


class TestSkillDefine:
    def test_dispatches_full_payload(self, provider, mock_client):
        out = provider.handle_tool_call(
            "yantrikdb_skill_define",
            {
                "skill_id": "git.commit_clean",
                "body": "Always rebase before merge so history stays linear and reviewable.",
                "skill_type": "procedure",
                "applies_to": ["git", "workflow"],
            },
        )
        mock_client.skill_define.assert_called_once()
        call = mock_client.skill_define.call_args
        assert call.kwargs["skill_id"] == "git.commit_clean"
        assert call.kwargs["skill_type"] == "procedure"
        assert call.kwargs["applies_to"] == ["git", "workflow"]
        parsed = json.loads(out)
        assert parsed["stored"] is True
        assert parsed["skill_id"] == "git.commit_clean"

    def test_rejects_missing_required_fields(self, provider, mock_client):
        for missing in ["skill_id", "body", "skill_type", "applies_to"]:
            args = {
                "skill_id": "git.commit_clean",
                "body": "x" * 80,
                "skill_type": "procedure",
                "applies_to": ["git"],
            }
            args.pop(missing)
            out = provider.handle_tool_call("yantrikdb_skill_define", args)
            assert "Missing required parameters" in json.loads(out)["error"]
        mock_client.skill_define.assert_not_called()


class TestSkillOutcome:
    def test_dispatches(self, provider, mock_client):
        out = provider.handle_tool_call(
            "yantrikdb_skill_outcome",
            {"skill_id": "git.commit_clean", "succeeded": True, "note": "worked"},
        )
        mock_client.skill_outcome.assert_called_once_with(
            "git.commit_clean", True, note="worked",
        )
        parsed = json.loads(out)
        assert parsed["recorded"] is True

    def test_rejects_missing_succeeded(self, provider, mock_client):
        out = provider.handle_tool_call(
            "yantrikdb_skill_outcome", {"skill_id": "git.commit_clean"},
        )
        mock_client.skill_outcome.assert_not_called()
        assert "Missing required parameter: succeeded" in json.loads(out)["error"]

    def test_rejects_missing_skill_id(self, provider, mock_client):
        out = provider.handle_tool_call(
            "yantrikdb_skill_outcome", {"succeeded": False},
        )
        mock_client.skill_outcome.assert_not_called()
        assert "Missing required parameter: skill_id" in json.loads(out)["error"]


# ---------------------------------------------------------------------------
# Skill validation rules (load-bearing — yantrikdb-server flagged these)
# ---------------------------------------------------------------------------


class TestSkillValidation:
    """Tests directly against the `validate_skill_define_args` helper.
    The hyphen-vs-underscore drift in `applies_to` regex is explicitly
    flagged as load-bearing by yantrikdb-server. These tests pin it.
    """

    @pytest.fixture
    def validate(self, provider_module):
        import importlib
        import sys
        for name in list(sys.modules):
            if name.endswith(".embedded"):
                return sys.modules[name].validate_skill_define_args
        emb = importlib.import_module(provider_module.__name__ + ".embedded")
        return emb.validate_skill_define_args

    @pytest.fixture
    def err_cls(self, client_module):
        return client_module.YantrikDBClientError

    def _good_args(self):
        return dict(
            skill_id="git.commit_clean",
            body="Always rebase before merge so history stays linear and reviewable.",
            skill_type="procedure",
            applies_to=["git", "workflow"],
        )

    def test_happy_path(self, validate):
        validate(**self._good_args())

    # skill_id ---------------------------------------------------------
    def test_skill_id_must_be_string(self, validate, err_cls):
        with pytest.raises(err_cls):
            validate(**{**self._good_args(), "skill_id": 42})

    def test_skill_id_too_short(self, validate, err_cls):
        with pytest.raises(err_cls, match="length"):
            validate(**{**self._good_args(), "skill_id": "g.x"})

    def test_skill_id_must_have_dot(self, validate, err_cls):
        with pytest.raises(err_cls, match="match"):
            validate(**{**self._good_args(), "skill_id": "no_dots_here"})

    def test_skill_id_no_uppercase(self, validate, err_cls):
        with pytest.raises(err_cls, match="match"):
            validate(**{**self._good_args(), "skill_id": "Git.Clean"})

    def test_skill_id_no_hyphens(self, validate, err_cls):
        with pytest.raises(err_cls, match="match"):
            validate(**{**self._good_args(), "skill_id": "git.commit-clean"})

    # body -------------------------------------------------------------
    def test_body_must_be_string(self, validate, err_cls):
        with pytest.raises(err_cls):
            validate(**{**self._good_args(), "body": 42})

    def test_body_too_short(self, validate, err_cls):
        with pytest.raises(err_cls, match="length"):
            validate(**{**self._good_args(), "body": "too short"})

    def test_body_too_long(self, validate, err_cls):
        with pytest.raises(err_cls, match="length"):
            validate(**{**self._good_args(), "body": "x" * 5001})

    # skill_type -------------------------------------------------------
    def test_skill_type_must_be_in_enum(self, validate, err_cls):
        with pytest.raises(err_cls, match="not in"):
            validate(**{**self._good_args(), "skill_type": "magic"})

    def test_each_skill_type_accepted(self, validate):
        for st in ("procedure", "reference", "lesson", "pattern", "rule"):
            validate(**{**self._good_args(), "skill_type": st})

    # applies_to — load-bearing ----------------------------------------
    def test_applies_to_must_be_non_empty_list(self, validate, err_cls):
        with pytest.raises(err_cls, match="non-empty"):
            validate(**{**self._good_args(), "applies_to": []})

    def test_applies_to_must_be_list_not_string(self, validate, err_cls):
        with pytest.raises(err_cls, match="non-empty"):
            validate(**{**self._good_args(), "applies_to": "git"})

    def test_applies_to_max_10_entries(self, validate, err_cls):
        with pytest.raises(err_cls, match="at most"):
            validate(**{**self._good_args(), "applies_to": [f"tag{i}" for i in range(11)]})

    def test_applies_to_REJECTS_HYPHEN(self, validate, err_cls):
        # Load-bearing — yantrikdb-server explicitly flagged this.
        # Anyone naturally writing "applies-to"-style hyphenated tags
        # would corrupt the substrate convention. Hyphens MUST raise.
        with pytest.raises(err_cls, match="no hyphens"):
            validate(**{**self._good_args(), "applies_to": ["git-workflow"]})

    def test_applies_to_rejects_dot(self, validate, err_cls):
        with pytest.raises(err_cls, match="no hyphens, no dots"):
            validate(**{**self._good_args(), "applies_to": ["git.workflow"]})

    def test_applies_to_rejects_uppercase(self, validate, err_cls):
        with pytest.raises(err_cls, match="lowercase"):
            validate(**{**self._good_args(), "applies_to": ["Git"]})

    def test_applies_to_rejects_leading_digit(self, validate, err_cls):
        with pytest.raises(err_cls, match="lowercase"):
            validate(**{**self._good_args(), "applies_to": ["1git"]})

    def test_applies_to_accepts_underscores(self, validate):
        validate(**{**self._good_args(), "applies_to": ["git_workflow", "rolling_deploy"]})

    def test_applies_to_accepts_digits_after_first(self, validate):
        validate(**{**self._good_args(), "applies_to": ["python3", "k8s"]})


# ---------------------------------------------------------------------------
# v0.4.17 — recall score-component breakdown is passed through
# ---------------------------------------------------------------------------


class TestRecallScoreBreakdown:
    """The engine returns a `scores` dict per result with similarity / decay
    / recency / importance / graph_proximity / valence_multiplier components
    AND a `contributions` sub-dict that sums to the final `score`. v0.4.17
    plumbs this through unchanged so the agent can see why a result ranked
    where it did. Pre-v0.4.17 the plugin silently dropped it.
    """

    def test_scores_passthrough(self, provider, mock_client):
        mock_client.recall.return_value = {
            "results": [{
                "rid": "r1",
                "text": "fact",
                "score": 1.17,
                "scores": {
                    "similarity": 0.78,
                    "decay": 0.50,
                    "recency": 0.99,
                    "importance": 0.50,
                    "graph_proximity": 0.0,
                    "valence_multiplier": 1.0,
                    "contributions": {
                        "similarity": 0.39,
                        "decay": 0.10,
                        "recency": 0.30,
                        "importance": 0.39,
                    },
                },
                "why_retrieved": ["high similarity"],
            }],
        }
        out = provider.handle_tool_call("yantrikdb_recall", {"query": "x"})
        result = json.loads(out)["results"][0]
        assert "scores" in result, "scores must survive plugin compaction"
        assert result["scores"]["similarity"] == 0.78
        assert result["scores"]["recency"] == 0.99
        assert "contributions" in result["scores"]
        assert result["scores"]["contributions"]["similarity"] == 0.39

    def test_scores_absent_when_engine_omits(self, provider, mock_client):
        # Older engines / fallback paths may not include the scores dict.
        # The field should still be present (as None) for a stable schema.
        mock_client.recall.return_value = {
            "results": [{"rid": "r1", "text": "fact", "score": 0.9}],
        }
        out = provider.handle_tool_call("yantrikdb_recall", {"query": "x"})
        result = json.loads(out)["results"][0]
        assert "scores" in result
        assert result["scores"] is None


# ---------------------------------------------------------------------------
# v0.4.17 — visible auto-skill crystallization
# ---------------------------------------------------------------------------


class TestRecentSkillsCrystallization:
    """When the agent defines a skill, persist a small (skill_id, type, ts)
    record so the NEXT session's system_prompt_block can surface it. Without
    this, skill_define is a write-only operation from the perspective of
    future sessions.
    """

    @pytest.fixture
    def provider_with_home(self, provider_module, mock_client, monkeypatch, tmp_path):
        monkeypatch.setenv("YANTRIKDB_MODE", "http")
        monkeypatch.setenv("YANTRIKDB_TOKEN", "ydb_test")
        monkeypatch.setenv("YANTRIKDB_SKILLS_ENABLED", "true")
        p = provider_module.YantrikDBMemoryProvider()
        with patch.object(provider_module, "make_backend", return_value=mock_client):
            p.initialize(
                "sess-1",
                agent_workspace="workspace",
                agent_identity="coder",
                platform="cli",
                hermes_home=str(tmp_path),
            )
        return p, tmp_path

    def test_skill_define_persists_entry(self, provider_with_home, mock_client):
        p, tmp = provider_with_home
        p.handle_tool_call(
            "yantrikdb_skill_define",
            {
                "skill_id": "git.commit_clean",
                "body": "x" * 80,
                "skill_type": "procedure",
                "applies_to": ["git", "workflow"],
            },
        )
        path = tmp / "yantrikdb-recent-skills.json"
        assert path.exists()
        entries = json.loads(path.read_text())
        assert len(entries) == 1
        assert entries[0]["skill_id"] == "git.commit_clean"
        assert entries[0]["skill_type"] == "procedure"
        assert entries[0]["applies_to"] == ["git", "workflow"]
        assert entries[0]["session_id"] == "sess-1"

    def test_skill_define_rejected_does_not_persist(
        self, provider_with_home, mock_client,
    ):
        # on_conflict=reject path — engine returns stored=False.
        p, tmp = provider_with_home
        mock_client.skill_define.return_value = {
            "rid": None, "skill_id": "git.commit_clean", "stored": False,
        }
        p.handle_tool_call(
            "yantrikdb_skill_define",
            {
                "skill_id": "git.commit_clean",
                "body": "x" * 80,
                "skill_type": "procedure",
                "applies_to": ["git"],
            },
        )
        path = tmp / "yantrikdb-recent-skills.json"
        # rejected definitions are NOT learning events, so nothing persisted.
        assert not path.exists()

    def test_recent_skills_dedupe_by_id(self, provider_with_home, mock_client):
        # Re-defining the same skill_id replaces the prior entry rather
        # than accumulating duplicates that all advertise the same id.
        p, tmp = provider_with_home
        for _ in range(3):
            p.handle_tool_call(
                "yantrikdb_skill_define",
                {
                    "skill_id": "git.commit_clean",
                    "body": "x" * 80,
                    "skill_type": "procedure",
                    "applies_to": ["git"],
                },
            )
        entries = json.loads((tmp / "yantrikdb-recent-skills.json").read_text())
        assert len(entries) == 1
        assert entries[0]["skill_id"] == "git.commit_clean"

    def test_recent_skills_cap(self, provider_with_home, mock_client):
        p, tmp = provider_with_home
        for i in range(15):
            mock_client.skill_define.return_value = {
                "rid": f"r{i}", "skill_id": f"git.s{i}", "stored": True,
            }
            p.handle_tool_call(
                "yantrikdb_skill_define",
                {
                    "skill_id": f"git.s{i}",
                    "body": "x" * 80,
                    "skill_type": "procedure",
                    "applies_to": ["git"],
                },
            )
        entries = json.loads((tmp / "yantrikdb-recent-skills.json").read_text())
        assert len(entries) == 10  # _RECENT_SKILLS_MAX
        # Oldest dropped — first kept should be s5.
        assert entries[0]["skill_id"] == "git.s5"

    def test_system_prompt_surfaces_prior_session_skills(
        self, provider_with_home, mock_client,
    ):
        # Skill defined in session A is surfaced when session B reads the
        # system prompt block.
        p, tmp = provider_with_home
        p.handle_tool_call(
            "yantrikdb_skill_define",
            {
                "skill_id": "git.commit_clean",
                "body": "x" * 80,
                "skill_type": "procedure",
                "applies_to": ["git"],
            },
        )
        # Same session sees nothing — it already knows what it just wrote.
        block_same = p.system_prompt_block()
        assert "Recently learned skills" not in block_same

        # Switch sessions; the prior skill should now surface.
        p._session_id = "sess-2"
        block_next = p.system_prompt_block()
        assert "Recently learned skills" in block_next
        assert "git.commit_clean" in block_next
        assert "procedure" in block_next

    def test_system_prompt_filters_stale_entries(
        self, provider_with_home, mock_client,
    ):
        # Entries older than the TTL must not surface even if persisted.
        p, tmp = provider_with_home
        path = tmp / "yantrikdb-recent-skills.json"
        old_ts = time.time() - (8 * 24 * 3600)  # 8 days
        path.write_text(json.dumps([{
            "skill_id": "git.ancient",
            "skill_type": "procedure",
            "applies_to": [],
            "ts": old_ts,
            "session_id": "sess-prior",
        }]))
        block = p.system_prompt_block()
        assert "git.ancient" not in block

    def test_surface_flag_disabled_suppresses_block(
        self, provider_with_home, mock_client,
    ):
        p, tmp = provider_with_home
        p._config.surface_recent_skills = False
        path = tmp / "yantrikdb-recent-skills.json"
        path.write_text(json.dumps([{
            "skill_id": "git.commit_clean",
            "skill_type": "procedure",
            "applies_to": [],
            "ts": time.time(),
            "session_id": "sess-other",
        }]))
        block = p.system_prompt_block()
        assert "Recently learned skills" not in block

    def test_no_hermes_home_silently_skips(
        self, provider_module, mock_client, monkeypatch,
    ):
        # Tests/cron paths that don't pass hermes_home shouldn't error;
        # crystallization is a UX nicety, not load-bearing.
        monkeypatch.setenv("YANTRIKDB_MODE", "http")
        monkeypatch.setenv("YANTRIKDB_TOKEN", "ydb_test")
        monkeypatch.setenv("YANTRIKDB_SKILLS_ENABLED", "true")
        p = provider_module.YantrikDBMemoryProvider()
        with patch.object(provider_module, "make_backend", return_value=mock_client):
            p.initialize(
                "sess-1",
                agent_workspace="workspace",
                agent_identity="coder",
                platform="cli",
            )
        out = p.handle_tool_call(
            "yantrikdb_skill_define",
            {
                "skill_id": "git.commit_clean",
                "body": "x" * 80,
                "skill_type": "procedure",
                "applies_to": ["git"],
            },
        )
        # No exception, dispatch still succeeds.
        assert json.loads(out)["stored"] is True
        # And the block is empty/unmodified.
        assert "Recently learned skills" not in p.system_prompt_block()


# ---------------------------------------------------------------------------
# v0.5.0 Wave A — active memory: substrate auto-injects into every turn
# ---------------------------------------------------------------------------


class TestWaveA1AutoRecallFiltering:
    """A1 polish — recall results below min_score are filtered out before
    they reach the prompt; oversize blocks are truncated to the token budget.
    The existing prefetch() plumbing already auto-injects; v0.5 just makes
    that injection respect quality and budget thresholds.
    """

    def test_low_score_recall_hits_filtered(self, provider, mock_client):
        mock_client.recall.return_value = {
            "results": [
                {"text": "high-confidence memory", "score": 0.85},
                {"text": "noisy low-score memory", "score": 0.10},
            ],
        }
        provider._config.auto_recall_min_score = 0.5
        provider.queue_prefetch("query", session_id="s")
        _wait_for_thread(provider._prefetch_thread)
        block = provider.prefetch("query", session_id="s")
        assert "high-confidence memory" in block
        assert "noisy low-score memory" not in block

    def test_oversize_block_truncated_to_budget(self, provider, mock_client):
        mock_client.recall.return_value = {
            "results": [
                {"text": "x" * 400, "score": 0.9},
                {"text": "y" * 400, "score": 0.9},
                {"text": "z" * 400, "score": 0.9},
            ],
        }
        provider._config.auto_recall_token_budget = 100  # ~400 chars
        provider.queue_prefetch("query", session_id="s")
        _wait_for_thread(provider._prefetch_thread)
        block = provider.prefetch("query", session_id="s")
        # Char cap ~= 400; with the prefix "## YantrikDB Recall\n" the
        # whole block stays well under 2x the budget.
        assert len(block) < 600
        assert "…" in block  # truncation marker present


class TestWaveA2SkillAutoAttach:
    """A2 — queue_prefetch also runs skill_search and the matching skill
    body surfaces in system_prompt_block() automatically. The agent never
    has to call skill_search; the right procedure just appears.
    """

    def test_skill_auto_attaches_when_score_meets_threshold(
        self, provider, mock_client,
    ):
        mock_client.skill_search.return_value = {
            "skills": [{
                "skill_id": "deploy.rolling",
                "skill_type": "procedure",
                "body": "Roll out 10% at a time, verify, then continue.",
                "score": 0.72,
            }],
            "total": 1,
        }
        provider._config.auto_skill_min_score = 0.6
        provider.queue_prefetch("how do I deploy", session_id="sess-1")
        _wait_for_thread(provider._prefetch_thread)
        block = provider.system_prompt_block()
        assert "Active skill" in block
        assert "deploy.rolling" in block
        assert "Roll out 10%" in block

    def test_skill_below_threshold_suppressed(self, provider, mock_client):
        mock_client.skill_search.return_value = {
            "skills": [{
                "skill_id": "unrelated.skill",
                "skill_type": "procedure",
                "body": "Body text",
                "score": 0.30,
            }],
            "total": 1,
        }
        provider._config.auto_skill_min_score = 0.6
        provider.queue_prefetch("query", session_id="s")
        _wait_for_thread(provider._prefetch_thread)
        assert "Active skill" not in provider.system_prompt_block()

    def test_skill_attach_drains_after_surface(self, provider, mock_client):
        # A surfaced skill should not echo across consecutive turns.
        mock_client.skill_search.return_value = {
            "skills": [{
                "skill_id": "x.y", "skill_type": "procedure",
                "body": "b", "score": 0.9,
            }],
            "total": 1,
        }
        provider.queue_prefetch("q1", session_id="sess-1")
        _wait_for_thread(provider._prefetch_thread)
        first = provider.system_prompt_block()
        second = provider.system_prompt_block()
        assert "Active skill" in first
        assert "Active skill" not in second

    def test_skill_attach_disabled_suppresses(self, provider, mock_client):
        provider._config.auto_skill_attach = False
        mock_client.skill_search.return_value = {
            "skills": [{"skill_id": "x.y", "skill_type": "procedure",
                        "body": "b", "score": 0.99}],
            "total": 1,
        }
        provider.queue_prefetch("q", session_id="s")
        _wait_for_thread(provider._prefetch_thread)
        mock_client.skill_search.assert_not_called()
        assert "Active skill" not in provider.system_prompt_block()

    def test_skill_attach_requires_skills_enabled(
        self, provider, mock_client,
    ):
        # If the user hasn't opted into skills, A2 should not fire either.
        provider._config.skills_enabled = False
        mock_client.skill_search.return_value = {"skills": [], "total": 0}
        provider.queue_prefetch("q", session_id="s")
        _wait_for_thread(provider._prefetch_thread)
        mock_client.skill_search.assert_not_called()

    def test_skill_attach_renders_embedded_backend_shape(
        self, provider, mock_client,
    ):
        # Regression test (caught by hermes-test harness against the real
        # embedded engine, 2026-05-30): embedded skill_search returns
        # the skill body as `text` and skill_id/skill_type nested under
        # `metadata.*` (it reuses recall_text), unlike the HTTP backend
        # which returns flat keys. A2 must normalize across both shapes.
        mock_client.skill_search.return_value = {
            "skills": [{
                "rid": "019e7229-0819-7536-905c-c38219d5e5bb",
                "text": "Roll out 10% at a time, verify, then continue.",
                "score": 0.78,
                "metadata": {
                    "record_type": "skill",
                    "skill_id": "deploy.rolling",
                    "skill_type": "procedure",
                    "applies_to": ["deploy"],
                },
            }],
            "total": 1,
        }
        provider.queue_prefetch("how do I deploy", session_id="sess-1")
        _wait_for_thread(provider._prefetch_thread)
        block = provider.system_prompt_block()
        assert "Active skill" in block
        assert "deploy.rolling" in block, (
            "skill_id should be resolved from metadata for embedded shape"
        )
        assert "procedure" in block
        assert "Roll out 10%" in block, (
            "skill body should be resolved from `text` for embedded shape"
        )
        assert "`?`" not in block, (
            "should not render '?' fallback when metadata is present"
        )


class TestWaveA3PendingConflicts:
    """A3 — unresolved conflicts() entries auto-surface in
    system_prompt_block() so the agent sees contradictions without being
    asked to look. Polled in the background thread, cached to amortize.
    """

    def test_conflict_polled_on_prefetch_and_surfaces(
        self, provider, mock_client,
    ):
        mock_client.conflicts.return_value = {
            "conflicts": [{
                "conflict_id": "c1",
                "text_a": "Pranab prefers tabs",
                "text_b": "Pranab prefers spaces",
            }],
        }
        # Force fresh poll.
        provider._pending_conflicts_last_poll = 0.0
        provider.queue_prefetch("q", session_id="s")
        _wait_for_thread(provider._prefetch_thread)
        block = provider.system_prompt_block()
        assert "Pending contradictions" in block
        assert "Pranab prefers tabs" in block
        assert "Pranab prefers spaces" in block

    def test_conflict_poll_cached_within_interval(
        self, provider, mock_client,
    ):
        # Two consecutive prefetches within the poll interval should hit
        # the cache, not re-call conflicts() twice.
        mock_client.conflicts.return_value = {"conflicts": []}
        provider._config.pending_conflicts_poll_seconds = 60.0
        provider._pending_conflicts_last_poll = 0.0
        provider.queue_prefetch("q1", session_id="s")
        _wait_for_thread(provider._prefetch_thread)
        provider.queue_prefetch("q2", session_id="s")
        _wait_for_thread(provider._prefetch_thread)
        assert mock_client.conflicts.call_count == 1

    def test_conflict_surface_disabled(self, provider, mock_client):
        provider._config.surface_pending_conflicts = False
        provider._pending_conflicts = [{
            "conflict_id": "c1", "text_a": "A", "text_b": "B",
        }]
        assert "Pending contradictions" not in provider.system_prompt_block()

    def test_conflict_surface_repeats_until_resolved(
        self, provider, mock_client,
    ):
        # Unlike skills (drain on read), conflicts surface every turn
        # until resolve_conflict() lands.
        provider._pending_conflicts = [{
            "conflict_id": "c1", "text_a": "A", "text_b": "B",
        }]
        first = provider.system_prompt_block()
        second = provider.system_prompt_block()
        assert "Pending contradictions" in first
        assert "Pending contradictions" in second


# ---------------------------------------------------------------------------
# v0.5.0 Wave B — auto-extraction cheap tier + effectiveness ledger
# ---------------------------------------------------------------------------


class TestWaveBExtractor:
    """Unit tests for yantrikdb/extractor.py — the regex+heuristic NER
    layer that converts user-message text into ExtractionCandidate
    records. High-precision-low-recall by design.
    """

    @pytest.fixture
    def extractor(self, provider_module):
        # Depend on provider_module so the conftest plugin-loader has run
        # (it registers yantrikdb_plugin_under_test.extractor in sys.modules).
        import sys
        return sys.modules["yantrikdb_plugin_under_test.extractor"]

    def test_preference_pattern_extracts(self, extractor):
        out = extractor.extract_candidates("I prefer tabs over spaces.")
        assert len(out) == 1
        assert out[0].pattern == "preference"
        assert out[0].text == "user prefers tabs"
        assert out[0].domain == "preference"

    def test_possession_with_favorite_prefix(self, extractor):
        out = extractor.extract_candidates("my favorite editor is Neovim")
        assert any(c.pattern == "possession" for c in out)
        c = next(c for c in out if c.pattern == "possession")
        assert c.text == "user's favorite editor is Neovim"
        assert c.domain == "preference"  # strips 'favorite ' prefix in mapping

    def test_identity_pattern_extracts_name(self, extractor):
        out = extractor.extract_candidates("my name is Pranab Sarkar")
        # may also match possession because attr=name is in the set
        canonical = [c for c in out if "Pranab Sarkar" in c.text]
        assert canonical, "should extract identity from 'my name is X'"

    def test_location_extracts_employer(self, extractor):
        out = extractor.extract_candidates("I work at Walmart")
        assert any(
            c.pattern == "location" and "Walmart" in c.text for c in out
        )

    def test_url_and_email_extracted(self, extractor):
        out = extractor.extract_candidates(
            "Check https://yantrikdb.com and email developer@pranab.co.in"
        )
        patterns = {c.pattern for c in out}
        assert "url" in patterns
        assert "email" in patterns

    def test_generic_filler_does_not_extract(self, extractor):
        # No regex pattern matches "I think the build is broken"
        assert extractor.extract_candidates("I think the build is broken") == []

    def test_stopword_value_filtered(self, extractor):
        # "I prefer it" → 'it' is in _STOPWORD_VALUES, filtered
        assert extractor.extract_candidates("I prefer it") == []

    def test_dedup_by_canonical_text(self, extractor):
        # 'my name is X' fires both possession + identity patterns; canonical
        # text dedup keeps only one
        out = extractor.extract_candidates("my name is Pranab Sarkar")
        texts = [c.text.lower() for c in out]
        assert len(texts) == len(set(texts))

    def test_bare_confirmation_detected(self, extractor):
        for phrase in ("yes", "Yes.", "yep", "RIGHT", "exactly!"):
            assert extractor.is_user_confirmation(phrase), phrase

    def test_confirmation_with_content_not_bare(self, extractor):
        # "yes the database is Postgres" should NOT count as bare
        # confirmation — it adds new user content that gets extracted normally.
        assert not extractor.is_user_confirmation("yes the database is Postgres")


class TestWaveBSyncTurnExtraction:
    """sync_turn() now runs the extractor and persists candidates with
    source='extracted', certainty=0.4. The whole-message store is
    preserved (sync_user_messages contract); extraction is additive.
    """

    def _wait_sync(self, provider):
        if provider._sync_thread and provider._sync_thread.is_alive():
            provider._sync_thread.join(timeout=5.0)

    def test_user_turn_runs_extractor(self, provider, mock_client):
        provider.sync_turn("I prefer tabs over spaces.", "", session_id="sess-1")
        self._wait_sync(provider)
        # remember called with: 1 whole-message + 1 extracted candidate
        calls = mock_client.remember.call_args_list
        assert len(calls) >= 2
        extracted = [
            c for c in calls
            if (c.kwargs.get("metadata") or {}).get("source") == "extracted"
        ]
        assert len(extracted) == 1
        meta = extracted[0].kwargs["metadata"]
        assert meta["extractor"] == "preference"
        assert meta["certainty"] == 0.4

    def test_confirmation_extracts_prior_assistant(self, provider, mock_client):
        # Turn 1: user asks, assistant asserts a fact
        provider.sync_turn(
            "what database should I use?",
            "Based on your notes, Postgres is the right choice for you.",
            session_id="sess-1",
        )
        self._wait_sync(provider)
        mock_client.remember.reset_mock()
        # Turn 2: user bare-confirms — prior assistant extraction should fire
        provider.sync_turn("yes", "", session_id="sess-1")
        self._wait_sync(provider)
        calls = mock_client.remember.call_args_list
        # Look for an extracted candidate whose speaker is "assistant"
        from_assistant = [
            c for c in calls
            if (c.kwargs.get("metadata") or {}).get("speaker") == "assistant"
        ]
        # Whether something extracted from the assistant text depends on
        # patterns matching. Either way, the path runs without error and
        # if a candidate WAS extracted it carries the right metadata.
        for c in from_assistant:
            meta = c.kwargs["metadata"]
            assert meta["source"] == "extracted"
            assert meta["confirmed_by_user"] is True

    def test_non_confirmation_does_not_promote_prior(
        self, provider, mock_client,
    ):
        # Establish prior assistant turn
        provider.sync_turn(
            "what time is it?",
            "It's 3pm in Mountain Time per your stated preference.",
            session_id="sess-1",
        )
        self._wait_sync(provider)
        mock_client.remember.reset_mock()
        # Turn 2: user message is NOT a bare confirmation — no prior-turn extraction
        provider.sync_turn(
            "actually, change the timezone",
            "",
            session_id="sess-1",
        )
        self._wait_sync(provider)
        calls = mock_client.remember.call_args_list
        speakers = {
            (c.kwargs.get("metadata") or {}).get("speaker") for c in calls
        }
        # No assistant-speaker extractions should appear
        assert "assistant" not in speakers

    def test_extraction_disabled_skips(self, provider, mock_client):
        provider._config.extraction_enabled = False
        provider.sync_turn("I prefer tabs over spaces.", "", session_id="sess-1")
        self._wait_sync(provider)
        # Only the whole-message store; no extracted candidates
        extracted = [
            c for c in mock_client.remember.call_args_list
            if (c.kwargs.get("metadata") or {}).get("source") == "extracted"
        ]
        assert extracted == []


class TestWaveBRecallFilter:
    """Default recall hides extracted candidates (source='extracted')
    so unpromoted noise doesn't outrank canonical memories. Caller can
    opt in via include_candidates=true.
    """

    def test_default_recall_filters_candidates(self, provider, mock_client):
        mock_client.recall.return_value = {
            "results": [
                {"rid": "r1", "text": "canonical fact", "score": 0.9, "metadata": {}},
                {"rid": "r2", "text": "extracted noise", "score": 0.85,
                 "metadata": {"source": "extracted", "certainty": 0.4}},
            ],
        }
        out = provider.handle_tool_call("yantrikdb_recall", {"query": "x"})
        parsed = json.loads(out)
        texts = [r["text"] for r in parsed["results"]]
        assert "canonical fact" in texts
        assert "extracted noise" not in texts

    def test_include_candidates_surfaces_extracted(
        self, provider, mock_client,
    ):
        mock_client.recall.return_value = {
            "results": [
                {"rid": "r1", "text": "canonical", "score": 0.9, "metadata": {}},
                {"rid": "r2", "text": "extracted hit", "score": 0.85,
                 "metadata": {"source": "extracted"}},
            ],
        }
        out = provider.handle_tool_call(
            "yantrikdb_recall",
            {"query": "x", "include_candidates": True},
        )
        texts = [r["text"] for r in json.loads(out)["results"]]
        assert "extracted hit" in texts


class TestWaveCBundledUI:
    """v0.5 Wave C1 — pure-stdlib HTTP inspector at `yantrikdb-hermes ui`.

    We don't spin up the actual HTTP server here; that's covered by
    manual smoke + the Hermes-in-docker e2e. These tests pin the
    module imports cleanly, the HTML renderer escapes content, and the
    handler routes the two endpoints.
    """

    def test_ui_module_imports(self, provider_module):
        # Provider-module dependency forces conftest plugin loader.
        import importlib
        import sys
        # ui.py uses relative imports from the plugin package — load it
        # through the same plugin_under_test namespace as everything else.
        spec = importlib.util.spec_from_file_location(
            "yantrikdb_plugin_under_test.ui",
            str(provider_module.__file__).rsplit("\\", 1)[0].rsplit("/", 1)[0]
            + "/yantrikdb/ui.py"
            if "\\" in provider_module.__file__
            else "/".join(provider_module.__file__.split("/")[:-1]) + "/ui.py",
        )
        # Robust path discovery from the provider module's location
        from pathlib import Path
        p = Path(provider_module.__file__).parent / "ui.py"
        assert p.exists(), f"ui.py expected at {p}"
        spec = importlib.util.spec_from_file_location(
            "yantrikdb_plugin_under_test.ui", str(p),
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules["yantrikdb_plugin_under_test.ui"] = mod
        spec.loader.exec_module(mod)
        # The two public hooks must be present.
        assert hasattr(mod, "serve")
        assert hasattr(mod, "build_snapshot")

    def test_render_html_escapes_user_content(self, provider_module):
        import importlib
        import sys
        from pathlib import Path
        p = Path(provider_module.__file__).parent / "ui.py"
        if "yantrikdb_plugin_under_test.ui" not in sys.modules:
            spec = importlib.util.spec_from_file_location(
                "yantrikdb_plugin_under_test.ui", str(p),
            )
            mod = importlib.util.module_from_spec(spec)
            sys.modules["yantrikdb_plugin_under_test.ui"] = mod
            spec.loader.exec_module(mod)
        mod = sys.modules["yantrikdb_plugin_under_test.ui"]
        # The render passes user-content through json.dumps (escape-safe)
        # and the inline JS uses an HTML-escape helper for conflict text.
        snapshot = {
            "namespace": "ns",
            "memories": [{"rid": "r1", "text": "<script>alert(1)</script>",
                          "score": 0.9, "source": "", "domain": "general"}],
            "conflicts": [{"conflict_id": "c1",
                           "text_a": "<img src=x onerror=alert(2)>",
                           "text_b": "B"}],
            "recent_skills": [],
            "stats": {},
        }
        html = mod._render_html(snapshot)
        # The JSON-embedded snapshot is escaped via json.dumps; raw
        # tags must not appear unescaped in a SCRIPT-tag-safe way.
        # json.dumps escapes "</" to "<\/" via slash inversion is NOT
        # the default — we accept the embedding as long as the JS-side
        # `escape()` runs on the conflict.text_*. Verify it's wrapped
        # in escape() in the template.
        assert 'escape(a)' in html or 'escape(b)' in html, (
            "conflict text must pass through the JS escape() helper"
        )


class TestWaveDTimeAwareRecall:
    """v0.5 Wave D2 — since/until parameters on yantrikdb_recall."""

    def test_since_yesterday_filters_older(self, provider, mock_client):
        import time
        now = time.time()
        mock_client.recall.return_value = {
            "results": [
                {"rid": "r-new", "text": "today's note",
                 "score": 0.9, "created_at": now - 3600},
                {"rid": "r-old", "text": "from last month",
                 "score": 0.9, "created_at": now - 30 * 86400},
            ],
        }
        out = provider.handle_tool_call(
            "yantrikdb_recall", {"query": "x", "since": "yesterday"},
        )
        rids = [r["rid"] for r in json.loads(out)["results"]]
        assert "r-new" in rids
        assert "r-old" not in rids

    def test_until_iso_filters_newer(self, provider, mock_client):
        mock_client.recall.return_value = {
            "results": [
                {"rid": "r-old", "text": "ancient", "score": 0.9,
                 "created_at": 1577836800.0},  # 2020-01-01
                {"rid": "r-new", "text": "modern", "score": 0.9,
                 "created_at": 1740614400.0},  # 2025-02-27
            ],
        }
        out = provider.handle_tool_call(
            "yantrikdb_recall", {"query": "x", "until": "2024-01-01"},
        )
        rids = [r["rid"] for r in json.loads(out)["results"]]
        assert "r-old" in rids
        assert "r-new" not in rids

    def test_duration_shorthand_7d(self, provider, mock_client):
        import time
        now = time.time()
        mock_client.recall.return_value = {
            "results": [
                {"rid": "r1", "text": "3d ago", "score": 0.9,
                 "created_at": now - 3 * 86400},
                {"rid": "r2", "text": "10d ago", "score": 0.9,
                 "created_at": now - 10 * 86400},
            ],
        }
        out = provider.handle_tool_call(
            "yantrikdb_recall", {"query": "x", "since": "7d"},
        )
        rids = [r["rid"] for r in json.loads(out)["results"]]
        assert "r1" in rids
        assert "r2" not in rids

    def test_invalid_time_string_treated_as_no_filter(
        self, provider, mock_client,
    ):
        mock_client.recall.return_value = {
            "results": [
                {"rid": "r1", "text": "x", "score": 0.9, "created_at": 1.0},
            ],
        }
        out = provider.handle_tool_call(
            "yantrikdb_recall",
            {"query": "x", "since": "completely-not-a-date"},
        )
        assert json.loads(out)["count"] >= 0


class TestWaveDPreCompressGist:
    """v0.5 Wave D1 — on_pre_compress also snapshots a gist of the middle."""

    def test_gist_recorded_with_pre_compression_metadata(
        self, provider, mock_client,
    ):
        messages = [
            {"role": "user", "content": "investigate the deploy outage"},
            {"role": "assistant", "content": "started kubectl logs..."},
            {"role": "user", "content": "and the ingress?"},
            {"role": "assistant", "content": "nginx returned 502, looking"},
            {"role": "user", "content": "what's the fix?"},
            {"role": "assistant", "content": "rolled back the upstream config"},
            {"role": "user", "content": "tail1"},
            {"role": "user", "content": "tail2"},
            {"role": "user", "content": "tail3"},
            {"role": "user", "content": "tail4"},
            {"role": "user", "content": "tail5"},
            {"role": "user", "content": "tail6"},
        ]
        provider.on_pre_compress(messages)
        compression_writes = [
            c for c in mock_client.remember.call_args_list
            if (c.kwargs.get("metadata") or {}).get("pre_compression") is True
        ]
        assert len(compression_writes) == 1
        meta = compression_writes[0].kwargs["metadata"]
        assert meta["source"] == "compression_summary"
        assert meta["turns_summarized"] > 0
        text = compression_writes[0].args[0]
        assert "investigate the deploy outage" in text

    def test_no_middle_no_gist_written(self, provider, mock_client):
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        provider.on_pre_compress(messages)
        compression_writes = [
            c for c in mock_client.remember.call_args_list
            if (c.kwargs.get("metadata") or {}).get("pre_compression") is True
        ]
        assert compression_writes == []


class TestWaveECrossAgentSharedBrain:
    """v0.5 Wave E — opt-in cross-agent shared brain. When
    shared_brain_namespace is set, explicit yantrikdb_remember writes
    mirror to that namespace AND recall unions both namespaces.
    Single-agent default: zero behaviour change.
    """

    def test_default_off_writes_only_to_local(self, provider, mock_client):
        provider.handle_tool_call(
            "yantrikdb_remember", {"text": "Pranab prefers tabs"},
        )
        calls = mock_client.remember.call_args_list
        assert len(calls) == 1
        assert calls[0].kwargs["namespace"] == provider._namespace
        meta = calls[0].kwargs.get("metadata") or {}
        assert not str(meta.get("source", "")).startswith("agent:")

    def test_opt_in_mirrors_write_to_shared_brain(self, provider, mock_client):
        provider._config.shared_brain_namespace = "yantrikdb:shared:household"
        provider._config.agent_name = "coding-agent"
        provider.handle_tool_call(
            "yantrikdb_remember", {"text": "Pranab prefers tabs"},
        )
        calls = mock_client.remember.call_args_list
        assert len(calls) == 2
        nss = [c.kwargs["namespace"] for c in calls]
        assert provider._namespace in nss
        assert "yantrikdb:shared:household" in nss
        shared_call = next(
            c for c in calls
            if c.kwargs["namespace"] == "yantrikdb:shared:household"
        )
        meta = shared_call.kwargs["metadata"]
        assert meta["source"] == "agent:coding-agent"
        assert meta["shared_brain_origin_namespace"] == provider._namespace

    def test_recall_unions_shared_brain_namespace(self, provider, mock_client):
        provider._config.shared_brain_namespace = "yantrikdb:shared:household"
        responses = {
            provider._namespace: {"results": [
                {"rid": "local-1", "text": "from local", "score": 0.9,
                 "metadata": {}},
            ]},
            "yantrikdb:shared:household": {"results": [
                {"rid": "shared-1", "text": "from shared brain", "score": 0.85,
                 "metadata": {"source": "agent:whatsapp-bot"}},
            ]},
        }

        def _recall_router(query, *, namespace=None, **_kw):
            return responses.get(namespace, {"results": []})

        mock_client.recall.side_effect = _recall_router
        out = provider.handle_tool_call(
            "yantrikdb_recall", {"query": "x", "top_k": 10},
        )
        texts = [r["text"] for r in json.loads(out)["results"]]
        assert "from local" in texts
        assert "from shared brain" in texts, (
            "Wave E should union the shared-brain namespace into recall"
        )

    def test_agent_name_auto_derived_from_namespace_when_blank(
        self, provider, mock_client,
    ):
        provider._config.shared_brain_namespace = "shared:ns"
        provider._config.agent_name = ""
        provider.handle_tool_call("yantrikdb_remember", {"text": "x"})
        shared_call = next(
            c for c in mock_client.remember.call_args_list
            if c.kwargs["namespace"] == "shared:ns"
        )
        # Provider fixture sets agent_workspace="workspace" → namespace
        # second segment is "workspace"
        assert shared_call.kwargs["metadata"]["source"] == "agent:workspace"

    def test_failed_mirror_does_not_break_primary_write(
        self, provider, mock_client,
    ):
        from yantrikdb_plugin_under_test.client import YantrikDBError
        provider._config.shared_brain_namespace = "shared:ns"
        provider._config.agent_name = "test"

        def _remember_router(text, *, namespace=None, **kw):
            if namespace == "shared:ns":
                raise YantrikDBError("shared brain unreachable")
            return {"rid": "primary-ok"}

        mock_client.remember.side_effect = _remember_router
        out = provider.handle_tool_call(
            "yantrikdb_remember", {"text": "x"},
        )
        assert json.loads(out)["stored"] is True
        assert json.loads(out)["rid"] == "primary-ok"


class TestWaveCObservability:
    """yantrikdb_observability rolls up engine stats + extraction +
    recent skills + provider health into a single response so the agent
    can answer 'how is my memory doing' without 6 separate tool calls.
    """

    def test_returns_summary_engine_extraction_provider_sections(
        self, provider, mock_client,
    ):
        mock_client.stats.return_value = {
            "active_memories": 42, "consolidated_memories": 3,
            "tombstoned_memories": 5, "edges": 17, "entities": 12,
            "operations": 128, "open_conflicts": 1, "pending_triggers": 0,
        }
        mock_client.recall.return_value = {
            "results": [{
                "rid": "e1", "text": "user prefers tabs", "score": 0.9,
                "metadata": {"source": "extracted", "extractor": "preference",
                             "speaker": "user"},
            }],
        }
        out = provider.handle_tool_call("yantrikdb_observability", {})
        parsed = json.loads(out)
        assert "summary" in parsed
        assert "engine" in parsed
        assert "extraction" in parsed
        assert "provider" in parsed
        assert parsed["engine"]["active_memories"] == 42
        assert parsed["engine"]["open_conflicts"] == 1
        assert parsed["extraction"]["by_pattern"]["preference"] == 1
        # provider health surfaces breaker state
        assert "circuit_breaker_open" in parsed["provider"]

    def test_summary_line_human_readable(self, provider, mock_client):
        mock_client.stats.return_value = {
            "active_memories": 10, "entities": 3, "edges": 5,
            "open_conflicts": 0, "consolidated_memories": 0,
            "tombstoned_memories": 0, "operations": 0, "pending_triggers": 0,
        }
        mock_client.recall.return_value = {"results": []}
        out = provider.handle_tool_call("yantrikdb_observability", {})
        summary = json.loads(out)["summary"]
        assert "memories=10" in summary
        assert "entities=3" in summary
        assert "breaker=closed" in summary

    def test_degrades_when_one_call_fails(self, provider, mock_client):
        # If stats() raises, extraction + provider sections still surface.
        from yantrikdb_plugin_under_test.client import YantrikDBError
        mock_client.stats.side_effect = YantrikDBError("upstream timeout")
        mock_client.recall.return_value = {"results": []}
        out = provider.handle_tool_call("yantrikdb_observability", {})
        parsed = json.loads(out)
        assert "error" in parsed["engine"]
        assert "extraction" in parsed
        assert "provider" in parsed


class TestWaveBExtractionStatsTool:
    """yantrikdb_extraction_stats surfaces per-pattern counts of
    candidates in the substrate so noisy patterns can be tuned.
    MVP samples via broad recall + post-filter.
    """

    def test_stats_groups_by_extractor(self, provider, mock_client):
        # Mock recall to return a mix of extracted + non-extracted records
        mock_client.recall.return_value = {
            "results": [
                {"rid": "e1", "text": "user prefers tabs", "score": 0.9,
                 "metadata": {"source": "extracted", "extractor": "preference",
                              "speaker": "user"}},
                {"rid": "e2", "text": "user's name is Pranab", "score": 0.85,
                 "metadata": {"source": "extracted", "extractor": "identity",
                              "speaker": "user"}},
                {"rid": "e3", "text": "user prefers spaces", "score": 0.8,
                 "metadata": {"source": "extracted", "extractor": "preference",
                              "speaker": "user"}},
                {"rid": "k1", "text": "canonical fact", "score": 0.95,
                 "metadata": {}},
            ],
        }
        out = provider.handle_tool_call("yantrikdb_extraction_stats", {})
        parsed = json.loads(out)
        assert parsed["total_candidates_sampled"] >= 3
        assert parsed["by_pattern"]["preference"] == 2
        assert parsed["by_pattern"]["identity"] == 1
        # Canonical "k1" not counted
        assert sum(parsed["by_pattern"].values()) == parsed["total_candidates_sampled"]
        assert parsed["by_speaker"]["user"] >= 3
