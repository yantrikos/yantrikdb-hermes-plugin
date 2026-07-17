"""v0.7 Wave H — engine-backed hygiene scan (list_records).

Mock-backend dispatch tests. The real-engine behaviour is exercised by the
embedded smoke during the release; here we verify the staleness logic, the
digest shape, and graceful fallback when the engine lacks `list_records`.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_client(client_module) -> MagicMock:
    c = MagicMock(spec=client_module.YantrikDBClient)
    c.health.return_value = {"status": "ok"}
    c.stats.return_value = {
        "active_memories": 10, "consolidated_memories": 1,
        "tombstoned_memories": 0, "open_conflicts": 0,
    }
    c.conflicts.return_value = {"conflicts": []}
    c.list_records.return_value = {"records": [], "next_cursor": None}
    c.knowledge_gaps.return_value = {"gaps": []}
    c.remember.return_value = {"rid": "r1"}
    c.record_turn.return_value = {"recorded": True}
    c.recent_turns.return_value = {"turns": []}
    c.clear_turns.return_value = {"cleared": True}
    c.task_add.return_value = {"id": "t1"}
    c.task_list.return_value = {"tasks": []}
    c.task_get.return_value = {"id": "t1", "title": "x", "status": "open"}
    c.task_update.return_value = {"id": "t1", "updated": True}
    c.task_delete.return_value = {"id": "t1", "deleted": True}
    return c


def _provider(provider_module, mock_client, monkeypatch, home: Path):
    monkeypatch.setenv("YANTRIKDB_MODE", "http")
    monkeypatch.setenv("YANTRIKDB_TOKEN", "ydb_test")
    p = provider_module.YantrikDBMemoryProvider()
    with patch.object(provider_module, "make_backend", return_value=mock_client):
        p.initialize("s1", agent_workspace="ws", agent_identity="coder",
                     platform="cli", hermes_home=str(home))
    return p


def _rec(rid, *, importance, access_count, tier="hot", age_secs=0.0):
    return {
        "rid": rid, "text": f"memory {rid}", "importance": importance,
        "access_count": access_count, "storage_tier": tier,
        "last_access": time.time() - age_secs, "created_at": time.time() - age_secs,
    }


class TestEngineStaleScan:
    def test_flags_low_value_cold_records(
        self, provider_module, mock_client, monkeypatch, tmp_path,
    ):
        p = _provider(provider_module, mock_client, monkeypatch, tmp_path)
        mock_client.list_records.return_value = {"records": [
            _rec("hot1", importance=0.9, access_count=20),          # valuable: keep
            _rec("stale_cold", importance=0.2, access_count=5, tier="cold"),  # stale (cold)
            _rec("stale_unused", importance=0.1, access_count=0),   # stale (unused)
            _rec("stale_old", importance=0.3, access_count=2, age_secs=40 * 24 * 3600),  # stale (old)
            _rec("low_but_hot", importance=0.3, access_count=50),   # low importance but hot+used: keep
        ], "next_cursor": None}
        out = json.loads(p.handle_tool_call("yantrikdb_hygiene", {"action": "scan"}))
        assert out["engine_scan_available"] is True
        rids = {c["rid"] for c in out["stale_candidates"]}
        assert rids == {"stale_cold", "stale_unused", "stale_old"}
        # least-valuable first
        assert out["stale_candidates"][0]["importance"] <= out["stale_candidates"][-1]["importance"]
        assert "stale=" in out["summary"]

    def test_paginates_with_cursor(
        self, provider_module, mock_client, monkeypatch, tmp_path,
    ):
        p = _provider(provider_module, mock_client, monkeypatch, tmp_path)
        pages = [
            {"records": [_rec("a", importance=0.1, access_count=0)], "next_cursor": "cur1"},
            {"records": [_rec("b", importance=0.1, access_count=0)], "next_cursor": None},
        ]
        mock_client.list_records.side_effect = pages
        out = json.loads(p.handle_tool_call("yantrikdb_hygiene", {"action": "scan"}))
        rids = {c["rid"] for c in out["stale_candidates"]}
        assert rids == {"a", "b"}
        assert mock_client.list_records.call_count == 2

    def test_falls_back_when_list_records_absent(
        self, provider_module, mock_client, monkeypatch, tmp_path,
    ):
        p = _provider(provider_module, mock_client, monkeypatch, tmp_path)
        # Older engine / server without the endpoint: method raises AttributeError.
        mock_client.list_records.side_effect = AttributeError("no list_records")
        out = json.loads(p.handle_tool_call("yantrikdb_hygiene", {"action": "scan"}))
        assert out["ok"] is True
        assert out["engine_scan_available"] is False
        assert out["stale_candidates"] == []
        # scan still returns a usable digest (conflicts/stats/low_usefulness)
        assert "summary" in out

    def test_apply_path_unchanged(
        self, provider_module, mock_client, monkeypatch, tmp_path,
    ):
        p = _provider(provider_module, mock_client, monkeypatch, tmp_path)
        mock_client.forget.return_value = {"rid": "stale_cold", "found": True}
        out = json.loads(p.handle_tool_call("yantrikdb_hygiene", {
            "action": "apply", "forget_rids": ["stale_cold"],
        }))
        assert out["forgotten_count"] == 1


class TestKnowledgeGaps:
    def test_returns_gaps(
        self, provider_module, mock_client, monkeypatch, tmp_path,
    ):
        p = _provider(provider_module, mock_client, monkeypatch, tmp_path)
        mock_client.knowledge_gaps.return_value = {"gaps": [
            {"query": "kubernetes ingress config", "count": 5, "avg_top_score": 0.2},
            {"query": "oncall escalation path", "count": 4, "avg_top_score": 0.3},
        ]}
        out = json.loads(p.handle_tool_call(
            "yantrikdb_knowledge_gaps", {"min_count": 3},
        ))
        assert out["ok"] is True
        assert out["count"] == 2
        assert "knowledge gap" in out["summary"]
        mock_client.knowledge_gaps.assert_called_once()
        assert mock_client.knowledge_gaps.call_args.kwargs["min_count"] == 3

    def test_graceful_when_engine_too_old(
        self, provider_module, mock_client, monkeypatch, tmp_path,
    ):
        p = _provider(provider_module, mock_client, monkeypatch, tmp_path)
        mock_client.knowledge_gaps.side_effect = AttributeError("no knowledge_gaps")
        out = json.loads(p.handle_tool_call("yantrikdb_knowledge_gaps", {}))
        assert out["ok"] is False
        assert "0.9.0" in out["error"]

    def test_defaults_applied(
        self, provider_module, mock_client, monkeypatch, tmp_path,
    ):
        p = _provider(provider_module, mock_client, monkeypatch, tmp_path)
        p.handle_tool_call("yantrikdb_knowledge_gaps", {})
        kw = mock_client.knowledge_gaps.call_args.kwargs
        assert kw["min_count"] == 3
        assert kw["max_avg_top_score"] == 0.4
        assert kw["limit"] == 20
        # v0.8.1: must pass the active namespace (engine 0.9.3+ scopes demand)
        assert kw.get("namespace")


def _join_sync(p):
    t = getattr(p, "_sync_thread", None)
    if t is not None and t.is_alive():
        t.join(timeout=3)


class TestConversationBuffer:
    def test_sync_turn_records_both_roles(
        self, provider_module, mock_client, monkeypatch, tmp_path,
    ):
        p = _provider(provider_module, mock_client, monkeypatch, tmp_path)
        p.sync_turn("which database do we use?", "PostgreSQL for billing.")
        _join_sync(p)
        roles = [c.args[0] for c in mock_client.record_turn.call_args_list]
        assert "user" in roles and "assistant" in roles

    def test_disabled_skips_recording(
        self, provider_module, mock_client, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv("YANTRIKDB_CONVERSATION_BUFFER_ENABLED", "false")
        p = _provider(provider_module, mock_client, monkeypatch, tmp_path)
        p.sync_turn("hello", "hi there")
        _join_sync(p)
        mock_client.record_turn.assert_not_called()

    def test_recent_turns_tool_reads(
        self, provider_module, mock_client, monkeypatch, tmp_path,
    ):
        p = _provider(provider_module, mock_client, monkeypatch, tmp_path)
        mock_client.recent_turns.return_value = {"turns": [
            {"role": "user", "content": "q", "created_at": 1.0},
            {"role": "assistant", "content": "a", "created_at": 2.0},
        ]}
        out = json.loads(p.handle_tool_call("yantrikdb_recent_turns", {"limit": 5}))
        assert out["count"] == 2
        assert mock_client.recent_turns.call_args.kwargs["limit"] == 5

    def test_recent_turns_clear(
        self, provider_module, mock_client, monkeypatch, tmp_path,
    ):
        p = _provider(provider_module, mock_client, monkeypatch, tmp_path)
        out = json.loads(p.handle_tool_call("yantrikdb_recent_turns", {"clear": True}))
        mock_client.clear_turns.assert_called_once()
        assert out["cleared"] is True

    def test_graceful_when_buffer_absent(
        self, provider_module, mock_client, monkeypatch, tmp_path,
    ):
        p = _provider(provider_module, mock_client, monkeypatch, tmp_path)
        mock_client.recent_turns.side_effect = AttributeError("no recent_turns")
        out = json.loads(p.handle_tool_call("yantrikdb_recent_turns", {}))
        assert out["ok"] is False
        assert "0.9.0" in out["error"]

    def test_sync_turn_disables_after_attribute_error(
        self, provider_module, mock_client, monkeypatch, tmp_path,
    ):
        p = _provider(provider_module, mock_client, monkeypatch, tmp_path)
        mock_client.record_turn.side_effect = AttributeError("old engine")
        p.sync_turn("a", "b")
        _join_sync(p)
        assert p._conversation_buffer_unavailable is True

    def test_surfacing_block_opt_in(
        self, provider_module, mock_client, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv("YANTRIKDB_SURFACE_CONVERSATION_BUFFER", "true")
        p = _provider(provider_module, mock_client, monkeypatch, tmp_path)
        mock_client.recent_turns.return_value = {"turns": [
            {"role": "user", "content": "what db?", "created_at": 1.0},
        ]}
        block = p._format_conversation_block()
        assert "Recent conversation" in block
        assert "what db?" in block

    def test_surfacing_block_off_by_default(
        self, provider_module, mock_client, monkeypatch, tmp_path,
    ):
        p = _provider(provider_module, mock_client, monkeypatch, tmp_path)
        assert p._format_conversation_block() == ""


class TestTasks:
    def test_add(self, provider_module, mock_client, monkeypatch, tmp_path):
        p = _provider(provider_module, mock_client, monkeypatch, tmp_path)
        out = json.loads(p.handle_tool_call("yantrikdb_tasks", {
            "action": "add", "title": "Ship v0.7", "priority": "high",
        }))
        assert out["ok"] is True and out["id"] == "t1"
        kw = mock_client.task_add.call_args.kwargs
        assert mock_client.task_add.call_args.args[0] == "Ship v0.7"
        assert kw["priority"] == "high"

    def test_add_requires_title(
        self, provider_module, mock_client, monkeypatch, tmp_path,
    ):
        p = _provider(provider_module, mock_client, monkeypatch, tmp_path)
        out = json.loads(p.handle_tool_call("yantrikdb_tasks", {"action": "add"}))
        assert out["ok"] is False
        assert "title" in out["error"]

    def test_list_default_action(
        self, provider_module, mock_client, monkeypatch, tmp_path,
    ):
        p = _provider(provider_module, mock_client, monkeypatch, tmp_path)
        mock_client.task_list.return_value = {"tasks": [
            {"id": "t1", "title": "a", "status": "open"},
            {"id": "t2", "title": "b", "status": "done"},
        ]}
        out = json.loads(p.handle_tool_call("yantrikdb_tasks", {}))
        assert out["action"] == "list" and out["count"] == 2

    def test_update_and_delete(
        self, provider_module, mock_client, monkeypatch, tmp_path,
    ):
        p = _provider(provider_module, mock_client, monkeypatch, tmp_path)
        u = json.loads(p.handle_tool_call("yantrikdb_tasks", {
            "action": "update", "task_id": "t1", "status": "done",
        }))
        assert u["updated"] is True
        assert mock_client.task_update.call_args.kwargs["status"] == "done"
        d = json.loads(p.handle_tool_call("yantrikdb_tasks", {
            "action": "delete", "task_id": "t1",
        }))
        assert d["deleted"] is True

    def test_update_requires_task_id(
        self, provider_module, mock_client, monkeypatch, tmp_path,
    ):
        p = _provider(provider_module, mock_client, monkeypatch, tmp_path)
        out = json.loads(p.handle_tool_call("yantrikdb_tasks", {"action": "update"}))
        assert out["ok"] is False and "task_id" in out["error"]

    def test_graceful_when_tasks_absent(
        self, provider_module, mock_client, monkeypatch, tmp_path,
    ):
        p = _provider(provider_module, mock_client, monkeypatch, tmp_path)
        mock_client.task_list.side_effect = AttributeError("no tasks")
        out = json.loads(p.handle_tool_call("yantrikdb_tasks", {"action": "list"}))
        assert out["ok"] is False and "0.9.0" in out["error"]
