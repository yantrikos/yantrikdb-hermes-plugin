"""v0.8 — the self-directing substrate.

Gap→task automation on session end + the "your memory's agenda" block.
Mock-backend dispatch tests; the real-engine loop is exercised by the
demo script.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_client(client_module) -> MagicMock:
    c = MagicMock(spec=client_module.YantrikDBClient)
    c.health.return_value = {"status": "ok"}
    c.think.return_value = {"consolidation_count": 0, "conflicts_found": 0}
    c.knowledge_gaps.return_value = {"gaps": []}
    c.task_list.return_value = {"tasks": []}
    c.task_add.return_value = {"id": "t1"}
    return c


def _provider(provider_module, mock_client, monkeypatch, home: Path, **env):
    monkeypatch.setenv("YANTRIKDB_MODE", "http")
    monkeypatch.setenv("YANTRIKDB_TOKEN", "ydb_test")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    p = provider_module.YantrikDBMemoryProvider()
    with patch.object(provider_module, "make_backend", return_value=mock_client):
        p.initialize("s1", agent_workspace="ws", agent_identity="coder",
                     platform="cli", hermes_home=str(home))
    return p


class TestGapToTask:
    def test_creates_tasks_for_gaps(
        self, provider_module, mock_client, monkeypatch, tmp_path,
    ):
        p = _provider(provider_module, mock_client, monkeypatch, tmp_path,
                      YANTRIKDB_AUTO_GAP_TASKS="true")
        mock_client.knowledge_gaps.return_value = {"gaps": [
            {"query": "kubernetes ingress config", "count": 6},
            {"query": "oncall escalation path", "count": 4},
        ]}
        p.on_session_end([])
        titles = [c.args[0] for c in mock_client.task_add.call_args_list]
        assert "Resolve knowledge gap: kubernetes ingress config" in titles
        assert "Resolve knowledge gap: oncall escalation path" in titles

    def test_dedups_against_existing_tasks(
        self, provider_module, mock_client, monkeypatch, tmp_path,
    ):
        p = _provider(provider_module, mock_client, monkeypatch, tmp_path,
                      YANTRIKDB_AUTO_GAP_TASKS="true")
        mock_client.knowledge_gaps.return_value = {"gaps": [
            {"query": "kubernetes ingress config"},
            {"query": "oncall escalation path"},
        ]}
        mock_client.task_list.return_value = {"tasks": [
            {"id": "t0", "title": "Resolve knowledge gap: kubernetes ingress config"},
        ]}
        p.on_session_end([])
        titles = [c.args[0] for c in mock_client.task_add.call_args_list]
        assert titles == ["Resolve knowledge gap: oncall escalation path"]

    def test_respects_cap(
        self, provider_module, mock_client, monkeypatch, tmp_path,
    ):
        p = _provider(provider_module, mock_client, monkeypatch, tmp_path,
                      YANTRIKDB_AUTO_GAP_TASKS="true", YANTRIKDB_GAP_TASK_MAX="1")
        mock_client.knowledge_gaps.return_value = {"gaps": [
            {"query": "a"}, {"query": "b"}, {"query": "c"},
        ]}
        p.on_session_end([])
        assert mock_client.task_add.call_count == 1

    def test_off_by_default(
        self, provider_module, mock_client, monkeypatch, tmp_path,
    ):
        p = _provider(provider_module, mock_client, monkeypatch, tmp_path)
        mock_client.knowledge_gaps.return_value = {"gaps": [{"query": "x"}]}
        p.on_session_end([])
        mock_client.task_add.assert_not_called()

    def test_graceful_when_apis_absent(
        self, provider_module, mock_client, monkeypatch, tmp_path,
    ):
        p = _provider(provider_module, mock_client, monkeypatch, tmp_path,
                      YANTRIKDB_AUTO_GAP_TASKS="true")
        mock_client.knowledge_gaps.side_effect = AttributeError("old engine")
        p.on_session_end([])  # must not raise
        mock_client.task_add.assert_not_called()


class TestAgendaBlock:
    def test_off_by_default(
        self, provider_module, mock_client, monkeypatch, tmp_path,
    ):
        p = _provider(provider_module, mock_client, monkeypatch, tmp_path)
        assert p._format_agenda_block() == ""

    def test_surfaces_tasks_and_gaps(
        self, provider_module, mock_client, monkeypatch, tmp_path,
    ):
        p = _provider(provider_module, mock_client, monkeypatch, tmp_path,
                      YANTRIKDB_SURFACE_AGENDA="true")
        mock_client.task_list.return_value = {"tasks": [
            {"id": "t1", "title": "Resolve knowledge gap: oncall path", "priority": "medium"},
        ]}
        mock_client.knowledge_gaps.return_value = {"gaps": [
            {"query": "kubernetes ingress config"},
        ]}
        block = p._format_agenda_block()
        assert "Your memory's agenda" in block
        assert "oncall path" in block
        assert "kubernetes ingress config" in block

    def test_empty_when_nothing_pending(
        self, provider_module, mock_client, monkeypatch, tmp_path,
    ):
        p = _provider(provider_module, mock_client, monkeypatch, tmp_path,
                      YANTRIKDB_SURFACE_AGENDA="true")
        assert p._format_agenda_block() == ""

    def test_agenda_in_system_prompt_block(
        self, provider_module, mock_client, monkeypatch, tmp_path,
    ):
        p = _provider(provider_module, mock_client, monkeypatch, tmp_path,
                      YANTRIKDB_SURFACE_AGENDA="true")
        mock_client.knowledge_gaps.return_value = {"gaps": [{"query": "topic X"}]}
        assert "Your memory's agenda" in p.system_prompt_block()
