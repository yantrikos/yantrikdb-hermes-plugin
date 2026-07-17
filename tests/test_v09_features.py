"""v0.9 — idempotent remember + typed-exception mapping (mock-backend).

The real engine 0.10 semantics (zero-write dedup, divergent conflict) are
exercised by tests/test_semantic_contract.py against a live engine.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_client(client_module) -> MagicMock:
    c = MagicMock(spec=client_module.YantrikDBClient)
    c.health.return_value = {"status": "ok"}
    c.remember.return_value = {"rid": "r-new"}
    return c


def _provider(provider_module, mock_client, monkeypatch):
    monkeypatch.setenv("YANTRIKDB_MODE", "http")
    monkeypatch.setenv("YANTRIKDB_TOKEN", "ydb_test")
    p = provider_module.YantrikDBMemoryProvider()
    with patch.object(provider_module, "make_backend", return_value=mock_client):
        p.initialize("s1", agent_workspace="ws", agent_identity="coder", platform="cli")
    return p


class TestIdempotentRememberTool:
    def test_key_is_passed_through(self, provider_module, mock_client, monkeypatch):
        p = _provider(provider_module, mock_client, monkeypatch)
        p.handle_tool_call("yantrikdb_remember",
                           {"text": "fact", "idempotency_key": "k1"})
        assert mock_client.remember.call_args.kwargs.get("idempotency_key") == "k1"

    def test_no_key_passes_none(self, provider_module, mock_client, monkeypatch):
        p = _provider(provider_module, mock_client, monkeypatch)
        p.handle_tool_call("yantrikdb_remember", {"text": "fact"})
        assert mock_client.remember.call_args.kwargs.get("idempotency_key") is None

    def test_conflict_surfaces_existing_rid(
        self, provider_module, mock_client, monkeypatch,
    ):
        p = _provider(provider_module, mock_client, monkeypatch)
        mock_client.remember.return_value = {
            "rid": "r-existing", "idempotency_conflict": True, "detail": "…",
        }
        out = json.loads(p.handle_tool_call(
            "yantrikdb_remember", {"text": "new", "idempotency_key": "k1"}))
        assert out["ok"] is True
        assert out["idempotency_conflict"] is True
        assert out["rid"] == "r-existing"
        assert out["stored"] is False


class TestHttpKeyRefusal:
    def test_http_remember_refuses_key(self, client_module):
        cfg = client_module.YantrikDBConfig(mode="http", url="http://x", token="t")
        client = client_module.YantrikDBClient(cfg)
        with pytest.raises(client_module.YantrikDBClientError, match="http mode"):
            client.remember("fact", idempotency_key="k1")

    def test_http_remember_ok_without_key(self, client_module):
        cfg = client_module.YantrikDBConfig(mode="http", url="http://x", token="t")
        client = client_module.YantrikDBClient(cfg)
        # no key → no early refusal; it would proceed to _request (patched away)
        with patch.object(client, "_request", return_value={"rid": "r1"}) as req:
            out = client.remember("fact")
        assert out["rid"] == "r1"
        assert "idempotency_key" not in req.call_args.args[2]


class TestTypedExceptionMap:
    def test_map_well_formed_and_maps_by_type(self, provider_module, client_module):
        import importlib
        emb = importlib.import_module(provider_module.__name__ + ".embedded")
        # Each entry is (engine exc type, plugin taxonomy type). Empty on
        # engines <0.10 that don't export the typed exceptions.
        for exc_type, tax in emb._TYPED_EXC_MAP:
            assert isinstance(exc_type, type)
            assert issubclass(tax, client_module.YantrikDBError)

        # _map_engine_error branches on TYPE: a fake typed exception injected
        # into the map routes to its taxonomy class, not the string heuristic.
        class _FakeBackpressure(RuntimeError):
            pass

        emb._TYPED_EXC_MAP.insert(0, (_FakeBackpressure, client_module.YantrikDBTransientError))
        try:
            mapped = emb._map_engine_error("op", _FakeBackpressure("anything at all"))
            assert isinstance(mapped, client_module.YantrikDBTransientError)
        finally:
            emb._TYPED_EXC_MAP.pop(0)
