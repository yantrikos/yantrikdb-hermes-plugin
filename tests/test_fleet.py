"""v0.13.0 — a view across the fleet, without breaking what it isolates.

Hermes users run N agents, not one. Each gets `{base}:{workspace}:{identity}`
so nothing contaminates anything else — which also means every surface this
plugin offers is single-agent. An operator running twenty agents cannot ask
"what is my fleet working on", "which agent is stuck", or "has anyone already
learned this".

The isolation is right; the missing thing is a view ACROSS it. That makes the
boundary conditions the important tests, not the happy path: a fleet view that
leaks across the wrong boundary is worse than no fleet view.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_client(client_module) -> MagicMock:
    c = MagicMock(spec=client_module.YantrikDBClient)
    c.health.return_value = {"status": "ok"}
    c.list_records.return_value = {"records": [
        {"namespace": "hermes:acme:coder", "created_at": 100.0},
        {"namespace": "hermes:acme:coder", "created_at": 200.0},
        {"namespace": "hermes:acme:researcher", "created_at": 150.0},
        {"namespace": "hermes:acme:triage", "created_at": 50.0},
        {"namespace": "hermes:OTHERWORKSPACE:coder", "created_at": 999.0},
    ]}
    c.task_list.return_value = {"tasks": [{"id": "t1"}, {"id": "t2"}]}
    return c


def _provider(provider_module, mock_client, monkeypatch, **env):
    monkeypatch.setenv("YANTRIKDB_MODE", "http")
    monkeypatch.setenv("YANTRIKDB_TOKEN", "ydb_test")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    p = provider_module.YantrikDBMemoryProvider()
    with patch.object(provider_module, "make_backend", return_value=mock_client):
        p.initialize("s1", agent_workspace="acme", agent_identity="coder",
                     platform="cli")
    return p


def _fleet(p):
    return json.loads(p.handle_tool_call("yantrikdb_fleet", {}))


class TestFleetBoundaries:
    """The tests that matter. A fleet view is only acceptable if it cannot
    become a privacy leak."""

    def test_refuses_under_owner_scoping(
        self, provider_module, mock_client, monkeypatch,
    ):
        """Under owner scoping, sibling namespaces are PEOPLE, not agents.
        Enumerating them would be exactly the identity-contamination failure
        that scoping exists to prevent."""
        p = _provider(provider_module, mock_client, monkeypatch,
                      YANTRIKDB_FLEET_VIEW="true", YANTRIKDB_OWNER_SCOPING="true")
        out = _fleet(p)
        assert "error" in out
        assert "owner_scoping" in out["error"]
        mock_client.list_records.assert_not_called()

    def test_does_not_cross_workspaces(
        self, provider_module, mock_client, monkeypatch,
    ):
        """Siblings are agents of the SAME workspace. Reaching across
        workspaces would pull in unrelated deployments that merely share a
        tenant prefix."""
        p = _provider(provider_module, mock_client, monkeypatch,
                      YANTRIKDB_FLEET_VIEW="true")
        seen = {a["namespace"] for a in _fleet(p)["sibling_agents"]}
        assert not any("OTHERWORKSPACE" in ns for ns in seen)

    def test_disabled_by_default(self, provider_module, mock_client, monkeypatch):
        p = _provider(provider_module, mock_client, monkeypatch)
        out = _fleet(p)
        assert "error" in out
        mock_client.list_records.assert_not_called()

    def test_tool_hidden_unless_enabled(self, provider_module, monkeypatch):
        p = provider_module.YantrikDBMemoryProvider()
        assert "yantrikdb_fleet" not in {s["name"] for s in p.get_tool_schemas()}
        monkeypatch.setenv("YANTRIKDB_FLEET_VIEW", "true")
        p2 = provider_module.YantrikDBMemoryProvider()
        assert "yantrikdb_fleet" in {s["name"] for s in p2.get_tool_schemas()}


class TestFleetOverview:
    def test_lists_sibling_agents_not_itself(
        self, provider_module, mock_client, monkeypatch,
    ):
        p = _provider(provider_module, mock_client, monkeypatch,
                      YANTRIKDB_FLEET_VIEW="true")
        out = _fleet(p)
        names = {a["namespace"] for a in out["sibling_agents"]}
        assert "hermes:acme:researcher" in names
        assert "hermes:acme:triage" in names
        assert out["this_agent"] not in names, "an agent is not its own sibling"

    def test_reports_activity_and_open_work(
        self, provider_module, mock_client, monkeypatch,
    ):
        p = _provider(provider_module, mock_client, monkeypatch,
                      YANTRIKDB_FLEET_VIEW="true")
        agent = next(a for a in _fleet(p)["sibling_agents"]
                     if a["namespace"] == "hermes:acme:researcher")
        assert agent["memories"] == 1
        assert agent["last_seen"] == 150.0
        assert agent["open_tasks"] == 2

    def test_truncation_is_declared(
        self, provider_module, mock_client, monkeypatch,
    ):
        """A capped scan that reports itself as complete is a silent lie about
        coverage — the operator would read a partial fleet as the whole one."""
        mock_client.list_records.return_value = {"records": [
            {"namespace": "hermes:acme:x", "created_at": 1.0} for _ in range(5)
        ]}
        p = _provider(provider_module, mock_client, monkeypatch,
                      YANTRIKDB_FLEET_VIEW="true", YANTRIKDB_FLEET_SCAN_LIMIT="5")
        assert _fleet(p)["truncated"] is True

    def test_one_unreachable_sibling_does_not_blank_the_view(
        self, provider_module, mock_client, monkeypatch, client_module,
    ):
        mock_client.task_list.side_effect = client_module.YantrikDBServerError("down")
        p = _provider(provider_module, mock_client, monkeypatch,
                      YANTRIKDB_FLEET_VIEW="true")
        out = _fleet(p)
        assert out["sibling_agents"], "agents should still be listed"
        assert all(a["open_tasks"] is None for a in out["sibling_agents"]), (
            "unknown must read as unknown, never as zero open tasks"
        )
