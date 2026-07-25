"""v0.10.0 — `on_delegation`: persist what a delegated sub-agent found.

Hermes delegates work to child agents and calls `on_delegation(task, result,
child_session_id)` when one returns. No other Hermes memory provider implements
this hook, so today the child's finding lives only in the transcript: the
sub-agent investigates, reports back, its session ends, and nothing is
recallable next session — the parent remembers *having delegated* but not what
came back.

These tests pin the write shape (episodic, parent namespace, child stamped),
the bounds, and the honesty rule that a write which cannot happen is never
silently dropped.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_client(client_module) -> MagicMock:
    c = MagicMock(spec=client_module.YantrikDBClient)
    c.health.return_value = {"status": "ok"}
    c.remember.return_value = {"rid": "r-1"}
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


def _wait(p):
    for t in list(getattr(p, "_threads", []) or []):
        t.join(timeout=2)
    import threading
    for t in threading.enumerate():
        if t.name == "yantrikdb-delegation":
            t.join(timeout=2)


class TestDelegationCapture:
    def test_records_task_and_result(self, provider_module, mock_client, monkeypatch):
        p = _provider(provider_module, mock_client, monkeypatch)
        p.on_delegation("investigate the flaky test",
                        "it was a timezone assumption in fixtures",
                        child_session_id="child-42")
        _wait(p)
        assert mock_client.remember.called
        text = mock_client.remember.call_args.args[0]
        assert "investigate the flaky test" in text
        assert "timezone assumption" in text

    def test_written_as_episodic_in_parent_namespace(
        self, provider_module, mock_client, monkeypatch,
    ):
        """The parent must be able to recall it — a delegated finding stored
        in the child's scope would be invisible to the agent that asked."""
        p = _provider(provider_module, mock_client, monkeypatch)
        p.on_delegation("t", "r", child_session_id="child-42")
        _wait(p)
        kwargs = mock_client.remember.call_args.kwargs
        assert kwargs["memory_type"] == "episodic"
        assert kwargs["namespace"] == p._namespace

    def test_child_session_is_stamped(self, provider_module, mock_client, monkeypatch):
        """Provenance: a later reader must be able to tell a delegated finding
        from something the agent established first-hand."""
        p = _provider(provider_module, mock_client, monkeypatch)
        p.on_delegation("t", "r", child_session_id="child-42")
        _wait(p)
        md = mock_client.remember.call_args.kwargs["metadata"]
        assert md["source"] == "hermes_delegation"
        assert md["child_session_id"] == "child-42"

    def test_long_result_is_bounded(self, provider_module, mock_client, monkeypatch):
        """A verbose sub-agent must not be able to flood the substrate."""
        p = _provider(provider_module, mock_client, monkeypatch,
                      YANTRIKDB_DELEGATION_MAX_LEN="200")
        p.on_delegation("t", "R" * 5000, child_session_id="c")
        _wait(p)
        assert len(mock_client.remember.call_args.args[0]) < 1000


class TestDelegationGuards:
    def test_opt_out(self, provider_module, mock_client, monkeypatch):
        p = _provider(provider_module, mock_client, monkeypatch,
                      YANTRIKDB_CAPTURE_DELEGATIONS="false")
        p.on_delegation("t", "r", child_session_id="c")
        _wait(p)
        mock_client.remember.assert_not_called()

    def test_empty_task_or_result_is_skipped(
        self, provider_module, mock_client, monkeypatch,
    ):
        p = _provider(provider_module, mock_client, monkeypatch)
        p.on_delegation("", "r")
        p.on_delegation("t", "   ")
        _wait(p)
        mock_client.remember.assert_not_called()

    def test_dropped_write_is_loud_not_silent(self, provider_module, caplog):
        """Issue #50 layer 2 generalised: a write path that cannot observe
        failure must never fail quietly."""
        p = provider_module.YantrikDBMemoryProvider()
        p._client = None
        p._cron_skipped = False
        p._config = None
        p._init_error = "engine unavailable"
        with caplog.at_level(logging.ERROR):
            p.on_delegation("t", "r", child_session_id="c")
        # config is None => capture flag unreadable => we return before writing,
        # but must not pretend success either; with a config present the drop
        # is logged.
        p._config = provider_module.YantrikDBConfig()
        with caplog.at_level(logging.ERROR):
            p.on_delegation("t", "r", child_session_id="c")
        assert p._dropped_writes >= 1
        assert any(r.levelno == logging.ERROR for r in caplog.records)

    def test_engine_error_does_not_raise(
        self, provider_module, mock_client, monkeypatch, client_module,
    ):
        p = _provider(provider_module, mock_client, monkeypatch)
        mock_client.remember.side_effect = client_module.YantrikDBServerError("boom")
        p.on_delegation("t", "r", child_session_id="c")  # must not raise
        _wait(p)


class TestHookIsActuallyCallable:
    """What makes the hook fire is the SIGNATURE, not the manifest.

    Verified against a real Hermes v0.15.1 install: `MemoryManager` invokes
    `provider.on_delegation(task, result, child_session_id=...)` directly, and
    wraps it in a try/except that only logs at debug. So a signature mismatch
    doesn't raise — it disappears into a debug line and the feature is silently
    dead. That makes this test the one that matters.

    (The `hooks:` list in plugin.yaml is documentation: Hermes parses
    `provides_hooks`, a different key belonging to the separate plugin-hook
    system, and never consults either for memory-provider methods.)
    """

    def test_signature_matches_hermes_call_shape(self, provider_module):
        import inspect
        sig = inspect.signature(
            provider_module.YantrikDBMemoryProvider.on_delegation)
        params = sig.parameters
        assert list(params)[1:3] == ["task", "result"], "positional order"
        assert params["child_session_id"].kind is inspect.Parameter.KEYWORD_ONLY
        assert params["child_session_id"].default == ""
        # Hermes documents that extra kwargs may be added over time; absorbing
        # them is what keeps a future Hermes release from silently killing this.
        assert any(p.kind is inspect.Parameter.VAR_KEYWORD
                   for p in params.values()), "must accept **kwargs"

    def test_call_in_hermes_shape_records(
        self, provider_module, mock_client, monkeypatch,
    ):
        """Exactly how MemoryManager calls it, including an unknown kwarg."""
        p = _provider(provider_module, mock_client, monkeypatch)
        p.on_delegation("t", "r", child_session_id="c", some_future_kwarg=1)
        _wait(p)
        assert mock_client.remember.called
