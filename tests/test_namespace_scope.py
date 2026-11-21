"""An empty scope must not look like a healthy memory.

The namespace is derived per agent identity, so a session can read a scope that
holds nothing while the store is full — and every status line still says "ok".
These tests pin the two probes that tell the states apart.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch


def _client(here, there):
    """A backend whose two namespaces answer with the given record lists."""
    c = MagicMock()
    c.health.return_value = {"status": "ok"}
    by_ns = {}

    def _list_records(*, namespace=None, limit=50, **kw):
        return {"records": by_ns.get(namespace, []), "next_cursor": None}

    # The resolved namespace is only known after initialize(); map the base and
    # everything else through the same call by filling by_ns in the test.
    c.list_records.side_effect = _list_records
    c._by_ns = by_ns
    c._here = here
    c._there = there
    return c


def _provider(provider_module, client, monkeypatch):
    p = provider_module.YantrikDBMemoryProvider()
    with patch.object(provider_module, "make_backend", return_value=client):
        p.initialize(
            "s1", agent_workspace="ws", agent_identity="coder", platform="cli",
        )
    return p


def _wire(client, resolved, base):
    client._by_ns[resolved] = client._here
    client._by_ns[base] = client._there


def test_empty_scope_over_a_full_base_is_named(provider_module, monkeypatch):
    client = _client([], [{"rid": "r1"}])
    p = provider_module.YantrikDBMemoryProvider()
    # Predict the derived names from the provider's own resolution, then wire.
    with patch.object(provider_module, "make_backend", return_value=client):
        # first pass resolves the namespace; the probes see empty maps
        p.initialize("s1", agent_workspace="ws", agent_identity="coder", platform="cli")
        _wire(client, p._namespace, p._config.namespace)
        p._namespace_hint = None
        p._check_namespace_scope()

    assert p._namespace_hint, "an empty scope over a full base must be named"
    assert p._namespace in p._namespace_hint
    assert p._config.namespace in p._namespace_hint
    assert "Memory scope" in p.system_prompt_block()


def test_a_populated_scope_says_nothing(provider_module, monkeypatch):
    client = _client([{"rid": "r1"}], [{"rid": "r0"}])
    p = provider_module.YantrikDBMemoryProvider()
    with patch.object(provider_module, "make_backend", return_value=client):
        p.initialize("s1", agent_workspace="ws", agent_identity="coder", platform="cli")
        _wire(client, p._namespace, p._config.namespace)
        p._check_namespace_scope()

    assert p._namespace_hint is None
    assert "Memory scope" not in p.system_prompt_block()


def test_an_unreadable_probe_stays_quiet(provider_module):
    """An older engine without list_records is not evidence of a wrong scope."""
    client = _client([], [])
    client.list_records.side_effect = AttributeError("no list_records")
    p = provider_module.YantrikDBMemoryProvider()
    with patch.object(provider_module, "make_backend", return_value=client):
        p.initialize("s1", agent_workspace="ws", agent_identity="coder", platform="cli")

    assert p._namespace_hint is None
