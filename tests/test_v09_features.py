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
    def _emb(self, provider_module):
        import importlib
        return importlib.import_module(provider_module.__name__ + ".embedded")

    def _fake_engine_exc(self, name):
        # An exception whose class LOOKS like an engine typed exception
        # (class name + top-level module "yantrikdb"), without importing the
        # engine — mirrors how _map_engine_error identifies them.
        cls = type(name, (RuntimeError,), {"__module__": "yantrikdb"})
        return cls

    def test_transient_typed_exc_maps_to_transient(self, provider_module, client_module):
        emb = self._emb(provider_module)
        exc = self._fake_engine_exc("Backpressure")("queue is full")
        mapped = emb._map_engine_error("record", exc)
        assert isinstance(mapped, client_module.YantrikDBTransientError)

    def test_caller_typed_exc_maps_to_client(self, provider_module, client_module):
        emb = self._emb(provider_module)
        exc = self._fake_engine_exc("ProvenanceInconsistent")("nope")
        mapped = emb._map_engine_error("record", exc)
        assert isinstance(mapped, client_module.YantrikDBClientError)

    def test_non_engine_same_name_is_not_typed(self, provider_module, client_module):
        # A "Backpressure" from some OTHER module must NOT be treated as the
        # engine's typed exception (module guard). Falls to string/server.
        emb = self._emb(provider_module)
        other = type("Backpressure", (RuntimeError,), {"__module__": "somelib"})
        mapped = emb._map_engine_error("record", other("x"))
        assert not isinstance(mapped, client_module.YantrikDBTransientError)

    def test_no_side_effect_import_of_yantrikdb(self, provider_module):
        # Guard against regressions: embedded.py must not import the engine
        # (same-named package) at load — it identifies typed excs by name.
        import inspect
        emb = self._emb(provider_module)
        src = inspect.getsource(emb._map_engine_error)
        assert "import yantrikdb" not in src
