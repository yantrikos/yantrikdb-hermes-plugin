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
