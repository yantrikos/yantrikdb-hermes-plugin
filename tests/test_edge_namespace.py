"""An edge has no namespace — say so instead of dropping the argument.

`relate()`/`link()` on the engine take (source, target, rel_type). The wrapper
accepted `namespace` and never forwarded it, so a caller believed the edge was
scoped. The provider passed its own namespace on every call, which is how the
silence stayed invisible in tests.
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def embedded_module(plugin):
    """Same loader the other suites use: the plugin modules are spec-loaded."""
    return sys.modules[plugin[0].__name__ + ".embedded"]


def test_relate_refuses_a_namespace(embedded_module, client_module):
    db = MagicMock()
    client = embedded_module.EmbeddedYantrikDBClient.__new__(
        embedded_module.EmbeddedYantrikDBClient
    )
    client._db = db

    with pytest.raises(client_module.YantrikDBError) as exc:
        client.relate("alice", "acme", "works_at", namespace="hermes")

    msg = str(exc.value)
    assert "store-wide" in msg
    assert "namespace" in msg
    db.relate.assert_not_called()


def test_relate_without_namespace_is_unchanged(embedded_module):
    db = MagicMock()
    db.relate.return_value = "edge-1"
    client = embedded_module.EmbeddedYantrikDBClient.__new__(
        embedded_module.EmbeddedYantrikDBClient
    )
    client._db = db

    out = client.relate("alice", "acme", "works_at")

    assert out == {"edge_id": "edge-1"}
    db.relate.assert_called_once_with("alice", "acme", rel_type="works_at", weight=1.0)


def test_the_relate_tool_never_sends_a_namespace(provider_module):
    """The provider used to pass its own namespace — which the wrapper dropped."""
    client = MagicMock()
    client.relate.return_value = {"edge_id": "e1"}
    p = provider_module.YantrikDBMemoryProvider()
    with patch.object(provider_module, "make_backend", return_value=client):
        p.initialize("s1", agent_workspace="ws", agent_identity="coder", platform="cli")

    p.handle_tool_call("yantrikdb_relate",
                       {"entity": "alice", "target": "acme",
                        "relationship": "works_at"})

    args, kwargs = client.relate.call_args
    assert args == ("alice", "acme", "works_at")
    assert "namespace" not in kwargs
