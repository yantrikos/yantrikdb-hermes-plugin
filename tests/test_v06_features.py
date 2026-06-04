"""v0.6 Wave F + G — self-tuning recall and proactive hygiene.

Mock-backend dispatch tests (no real engine). The recall-quality side of
self-tuning is covered end-to-end by test_recall_benchmark.py; here we
verify the plumbing: the feedback sidecar, the reinforce arg, the re-rank
ordering, and the hygiene scan/apply paths.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_client(client_module) -> MagicMock:
    c = MagicMock(spec=client_module.YantrikDBClient)
    c.health.return_value = {"status": "ok"}
    c.remember.return_value = {"rid": "r-new"}
    c.recall.return_value = {"results": [], "total": 0}
    c.forget.return_value = {"rid": "r-x", "found": True}
    c.think.return_value = {"consolidation_count": 2, "conflicts_found": 1}
    c.conflicts.return_value = {"conflicts": []}
    c.stats.return_value = {
        "active_memories": 42, "consolidated_memories": 3,
        "tombstoned_memories": 5, "edges": 17, "entities": 12,
        "operations": 128, "open_conflicts": 1, "pending_triggers": 0,
    }
    return c


def _make_provider(provider_module, mock_client, monkeypatch, home: Path,
                   *, self_tuning: bool = True, surface_hygiene: bool = False):
    monkeypatch.setenv("YANTRIKDB_MODE", "http")
    monkeypatch.setenv("YANTRIKDB_TOKEN", "ydb_test")
    monkeypatch.setenv(
        "YANTRIKDB_SELF_TUNING_RECALL", "true" if self_tuning else "false",
    )
    if surface_hygiene:
        monkeypatch.setenv("YANTRIKDB_SURFACE_HYGIENE", "true")
    p = provider_module.YantrikDBMemoryProvider()
    with patch.object(provider_module, "make_backend", return_value=mock_client):
        p.initialize(
            "sess-1", agent_workspace="ws", agent_identity="coder",
            platform="cli", hermes_home=str(home),
        )
    return p


def _recall_payload(rids_scores):
    return {"results": [
        {"rid": rid, "text": f"memory {rid}", "score": score,
         "importance": 0.5, "domain": "general", "why_retrieved": ["semantic"]}
        for rid, score in rids_scores
    ], "total": len(rids_scores)}


class TestSelfTuningRecall:
    def test_reinforce_writes_feedback_sidecar(
        self, provider_module, mock_client, monkeypatch, tmp_path,
    ):
        p = _make_provider(provider_module, mock_client, monkeypatch, tmp_path)
        mock_client.recall.return_value = _recall_payload([("a", 1.0)])
        p.handle_tool_call("yantrikdb_recall", {"query": "x", "reinforce": ["a"]})
        ledger = json.loads(
            (tmp_path / "yantrikdb-recall-feedback.json").read_text("utf-8"),
        )
        assert ledger["a"]["reinforced"] == 1
        # "a" was also surfaced in the same call.
        assert ledger["a"]["surfaced"] >= 1

    def test_reinforce_ignored_when_self_tuning_off(
        self, provider_module, mock_client, monkeypatch, tmp_path,
    ):
        p = _make_provider(
            provider_module, mock_client, monkeypatch, tmp_path,
            self_tuning=False,
        )
        mock_client.recall.return_value = _recall_payload([("a", 1.0)])
        p.handle_tool_call("yantrikdb_recall", {"query": "x", "reinforce": ["a"]})
        assert not (tmp_path / "yantrikdb-recall-feedback.json").exists()

    def test_reinforcement_reranks_results(
        self, provider_module, mock_client, monkeypatch, tmp_path,
    ):
        p = _make_provider(provider_module, mock_client, monkeypatch, tmp_path)
        # b ranks below a by raw score, but gets reinforced 3× → +0.15 boost,
        # enough to overtake a (gap 0.10).
        mock_client.recall.return_value = _recall_payload([("a", 1.00), ("b", 0.90)])
        for _ in range(3):
            p.handle_tool_call("yantrikdb_recall", {"query": "x", "reinforce": ["b"]})
        out = json.loads(p.handle_tool_call("yantrikdb_recall", {"query": "x"}))
        order = [r["rid"] for r in out["results"]]
        assert order[0] == "b"  # reinforced memory climbed to the top
        assert any("reinforced" in w for w in out["results"][0]["why_retrieved"])

    def test_boost_is_capped(self, provider_module, mock_client, monkeypatch, tmp_path):
        p = _make_provider(provider_module, mock_client, monkeypatch, tmp_path)
        mock_client.recall.return_value = _recall_payload([("a", 1.0)])
        for _ in range(50):  # way past the cap
            p.handle_tool_call("yantrikdb_recall", {"query": "x", "reinforce": ["a"]})
        # _recall_boost caps at self_tuning_max_boost (default 0.15)
        assert p._recall_boost(50) == pytest.approx(0.15)
        assert p._recall_boost(1) == pytest.approx(0.05)


class TestHygiene:
    def test_scan_returns_digest(
        self, provider_module, mock_client, monkeypatch, tmp_path,
    ):
        p = _make_provider(provider_module, mock_client, monkeypatch, tmp_path)
        mock_client.conflicts.return_value = {"conflicts": [
            {"conflict_id": "c1", "a": "x", "b": "y"},
        ]}
        out = json.loads(p.handle_tool_call("yantrikdb_hygiene", {"action": "scan"}))
        assert out["ok"] is True
        assert out["engine"]["active_memories"] == 42
        assert out["open_conflicts_total"] == 1
        assert "summary" in out
        assert isinstance(out["recommended_actions"], list)

    def test_apply_forget_loops_and_counts(
        self, provider_module, mock_client, monkeypatch, tmp_path,
    ):
        p = _make_provider(provider_module, mock_client, monkeypatch, tmp_path)
        mock_client.forget.side_effect = [
            {"rid": "r1", "found": True},
            {"rid": "r2", "found": False},
        ]
        out = json.loads(p.handle_tool_call("yantrikdb_hygiene", {
            "action": "apply", "forget_rids": ["r1", "r2"],
        }))
        assert mock_client.forget.call_count == 2
        assert out["forgotten_count"] == 1  # only r1 was found

    def test_apply_consolidate_runs_think(
        self, provider_module, mock_client, monkeypatch, tmp_path,
    ):
        p = _make_provider(provider_module, mock_client, monkeypatch, tmp_path)
        out = json.loads(p.handle_tool_call("yantrikdb_hygiene", {
            "action": "apply", "consolidate": True,
        }))
        mock_client.think.assert_called_once()
        assert out["consolidated"] == 2

    def test_apply_empty_is_error(
        self, provider_module, mock_client, monkeypatch, tmp_path,
    ):
        p = _make_provider(provider_module, mock_client, monkeypatch, tmp_path)
        out = json.loads(p.handle_tool_call("yantrikdb_hygiene", {"action": "apply"}))
        assert out["ok"] is False

    def test_low_usefulness_surfaces_unreinforced(
        self, provider_module, mock_client, monkeypatch, tmp_path,
    ):
        p = _make_provider(provider_module, mock_client, monkeypatch, tmp_path)
        mock_client.recall.return_value = _recall_payload([("stale", 0.9)])
        for _ in range(4):  # surfaced 4×, never reinforced
            p.handle_tool_call("yantrikdb_recall", {"query": "x"})
        out = json.loads(p.handle_tool_call("yantrikdb_hygiene", {"action": "scan"}))
        rids = [e["rid"] for e in out["low_usefulness"]]
        assert "stale" in rids

    def test_forget_purges_feedback_ledger(
        self, provider_module, mock_client, monkeypatch, tmp_path,
    ):
        p = _make_provider(provider_module, mock_client, monkeypatch, tmp_path)
        mock_client.recall.return_value = _recall_payload([("stale", 0.9)])
        for _ in range(4):
            p.handle_tool_call("yantrikdb_recall", {"query": "x"})
        mock_client.forget.return_value = {"rid": "stale", "found": True}
        p.handle_tool_call("yantrikdb_hygiene", {
            "action": "apply", "forget_rids": ["stale"],
        })
        ledger = json.loads(
            (tmp_path / "yantrikdb-recall-feedback.json").read_text("utf-8"),
        )
        assert "stale" not in ledger


class TestHygieneSurfacing:
    def test_block_empty_when_flag_off(
        self, provider_module, mock_client, monkeypatch, tmp_path,
    ):
        p = _make_provider(provider_module, mock_client, monkeypatch, tmp_path)
        assert p._format_hygiene_block() == ""

    def test_block_surfaces_candidates_when_on(
        self, provider_module, mock_client, monkeypatch, tmp_path,
    ):
        p = _make_provider(
            provider_module, mock_client, monkeypatch, tmp_path,
            surface_hygiene=True,
        )
        mock_client.recall.return_value = _recall_payload([("stale", 0.9)])
        for _ in range(4):
            p.handle_tool_call("yantrikdb_recall", {"query": "x"})
        block = p._format_hygiene_block()
        assert "Memory hygiene" in block
        assert "stale" in block
