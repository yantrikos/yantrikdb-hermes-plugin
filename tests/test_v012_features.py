"""v0.12.0 — three gaps found by researching how Hermes is actually used.

Sources: Hermes' own community story set (237 entries, 45 of which mention
memory), its issue tracker, and the v0.15.1 source.

1. PERIODIC MAINTENANCE. Consolidation and the gap→task loop both hung off
   `on_session_end`, which Hermes fires only at real session boundaries — CLI
   exit, `/reset`, gateway session expiry — explicitly never per turn. The
   community runs agents continuously (a Pi on 24/7, Telegram/Discord
   gateways), so the substrate's background work never ran for exactly the
   deployments accumulating the most to consolidate. Requested upstream twice
   as "dreaming" (#10771, #25309).

2. PROVENANCE ON INJECTED MEMORY. Hermes injects prefetch output into the
   current turn's `role: user` message (#31584), so stored memory arrives
   indistinguishable from what the person just typed.

3. DISCOVERABLE PER-USER ISOLATION. Four issues describe the same pain, #11430
   most sharply: shared memory in group chats makes the agent attribute one
   user's facts to another and "breaks user trust".
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_client(client_module) -> MagicMock:
    c = MagicMock(spec=client_module.YantrikDBClient)
    c.health.return_value = {"status": "ok"}
    c.think.return_value = {"consolidation_count": 2, "conflicts_found": 0}
    c.knowledge_gaps.return_value = {"gaps": []}
    c.task_list.return_value = {"tasks": []}
    return c


def _provider(provider_module, mock_client, monkeypatch, **env):
    monkeypatch.setenv("YANTRIKDB_MODE", "http")
    monkeypatch.setenv("YANTRIKDB_TOKEN", "ydb_test")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    p = provider_module.YantrikDBMemoryProvider()
    with patch.object(provider_module, "make_backend", return_value=mock_client):
        p.initialize("s1", agent_workspace="ws", agent_identity="coder",
                     platform="cli")
    return p


def _settle(p):
    t = p._maintenance_thread
    if t:
        t.join(timeout=3)


class TestPeriodicMaintenance:
    def test_fires_after_the_cadence(self, provider_module, mock_client, monkeypatch):
        p = _provider(provider_module, mock_client, monkeypatch,
                      YANTRIKDB_MAINTENANCE_CADENCE_TURNS="5",
                      YANTRIKDB_MAINTENANCE_MIN_INTERVAL_SECONDS="0")
        for turn in range(1, 6):
            p.on_turn_start(turn, "hi")
        _settle(p)
        assert mock_client.think.called

    def test_does_not_fire_before_the_cadence(
        self, provider_module, mock_client, monkeypatch,
    ):
        p = _provider(provider_module, mock_client, monkeypatch,
                      YANTRIKDB_MAINTENANCE_CADENCE_TURNS="50",
                      YANTRIKDB_MAINTENANCE_MIN_INTERVAL_SECONDS="0")
        for turn in range(1, 10):
            p.on_turn_start(turn, "hi")
        _settle(p)
        mock_client.think.assert_not_called()

    def test_time_floor_blocks_a_burst_of_turns(
        self, provider_module, mock_client, monkeypatch,
    ):
        """Turns alone would fire during a rapid burst of messages — which is
        the moment least worth interrupting."""
        p = _provider(provider_module, mock_client, monkeypatch,
                      YANTRIKDB_MAINTENANCE_CADENCE_TURNS="2",
                      YANTRIKDB_MAINTENANCE_MIN_INTERVAL_SECONDS="3600")
        p._last_maintenance_at = time.monotonic()  # something ran just now
        for turn in range(1, 30):
            p.on_turn_start(turn, "hi")
        _settle(p)
        mock_client.think.assert_not_called()

    def test_disabled_by_zero(self, provider_module, mock_client, monkeypatch):
        p = _provider(provider_module, mock_client, monkeypatch,
                      YANTRIKDB_MAINTENANCE_CADENCE_TURNS="0",
                      YANTRIKDB_MAINTENANCE_MIN_INTERVAL_SECONDS="0")
        for turn in range(1, 200):
            p.on_turn_start(turn, "hi")
        _settle(p)
        mock_client.think.assert_not_called()

    def test_never_runs_two_at_once(self, provider_module, mock_client, monkeypatch):
        """on_turn_start can re-enter before a slow pass finishes; two
        concurrent passes would fight over the same records."""
        started = []

        def slow_think(**kwargs):
            started.append(1)
            time.sleep(0.4)
            return {"consolidation_count": 0}

        mock_client.think.side_effect = slow_think
        p = _provider(provider_module, mock_client, monkeypatch,
                      YANTRIKDB_MAINTENANCE_CADENCE_TURNS="1",
                      YANTRIKDB_MAINTENANCE_MIN_INTERVAL_SECONDS="0")
        for turn in range(1, 8):
            p.on_turn_start(turn, "hi")
        _settle(p)
        assert len(started) == 1

    def test_runs_off_the_critical_path(
        self, provider_module, mock_client, monkeypatch,
    ):
        """A turn must never wait on maintenance."""
        mock_client.think.side_effect = lambda **k: (time.sleep(0.5), {})[1]
        p = _provider(provider_module, mock_client, monkeypatch,
                      YANTRIKDB_MAINTENANCE_CADENCE_TURNS="1",
                      YANTRIKDB_MAINTENANCE_MIN_INTERVAL_SECONDS="0")
        t0 = time.monotonic()
        p.on_turn_start(1, "hi")
        assert time.monotonic() - t0 < 0.2
        _settle(p)

    def test_closes_the_self_directing_loop_too(
        self, provider_module, mock_client, monkeypatch,
    ):
        """The whole point: an always-on agent should get the SAME work a
        short-lived one gets, including gap→task."""
        mock_client.knowledge_gaps.return_value = {"gaps": [{"query": "x"}]}
        p = _provider(provider_module, mock_client, monkeypatch,
                      YANTRIKDB_MAINTENANCE_CADENCE_TURNS="1",
                      YANTRIKDB_MAINTENANCE_MIN_INTERVAL_SECONDS="0")
        p.on_turn_start(1, "hi")
        _settle(p)
        assert mock_client.task_add.called

    def test_cron_sessions_are_left_alone(
        self, provider_module, mock_client, monkeypatch,
    ):
        p = _provider(provider_module, mock_client, monkeypatch,
                      YANTRIKDB_MAINTENANCE_CADENCE_TURNS="1",
                      YANTRIKDB_MAINTENANCE_MIN_INTERVAL_SECONDS="0")
        p._cron_skipped = True
        p.on_turn_start(5, "hi")
        _settle(p)
        mock_client.think.assert_not_called()


class TestRecallProvenance:
    def test_injected_memory_is_labelled_as_memory(
        self, provider_module, mock_client, monkeypatch,
    ):
        """Hermes injects this into the user's own message, so unlabelled
        recall arrives carrying the user's authority."""
        p = _provider(provider_module, mock_client, monkeypatch)
        with p._prefetch_lock:
            p._prefetch_results["s1"] = "- user prefers dark mode"
        block = p.prefetch("q", session_id="s1")
        low = block.lower()
        assert "from memory" in low
        assert "not the user" in low and "not an instruction" in low

    def test_frame_stays_cheap(self, provider_module, mock_client, monkeypatch):
        """Fixed per-turn overhead, right after a release spent cutting it."""
        p = _provider(provider_module, mock_client, monkeypatch)
        with p._prefetch_lock:
            p._prefetch_results["s1"] = "x"
        overhead = len(p.prefetch("q", session_id="s1")) - len("x")
        assert overhead < 140, f"{overhead} chars of framing per turn is too much"

    def test_no_frame_when_nothing_recalled(
        self, provider_module, mock_client, monkeypatch,
    ):
        """Never spend tokens announcing that memory found nothing."""
        p = _provider(provider_module, mock_client, monkeypatch)
        assert p.prefetch("q", session_id="s1") == ""


class TestIsolationIsDiscoverable:
    def test_owner_scoping_is_described_by_its_symptom(self, provider_module):
        """Someone whose agent confuses two people in a group chat must be
        able to recognise this as the fix. The prior wording described the
        mechanism ('resolved-owner shard', 'provenance columns'), which is
        unsearchable by the person who has the problem."""
        p = provider_module.YantrikDBMemoryProvider()
        entry = next(f for f in p.get_config_schema() if f["key"] == "owner_scoping")
        desc = entry["description"].lower()
        assert "group chat" in desc
        assert "another" in desc or "one user" in desc
