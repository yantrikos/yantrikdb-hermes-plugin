"""Catch mock-vs-real signature drift between provider and client backends.

`tests/test_provider.py` mocks the client, so a kwarg the provider
passes can drop off the real HTTP/Embedded client signatures without
any test failure. PR #11 hit this in code review: the provider's
``_do_think`` / ``_do_relate`` / ``_do_conflicts`` started passing
``namespace=self._namespace``, the HTTP client accepted it, but the
embedded client did not — and the mocked provider tests still passed.
Embedded mode would have TypeError'd in production at the first tool
call.

The test below stays close to the failure mode: it inspects both
client signatures (no instantiation, no engine binary) and asserts
that every kwarg the HTTP client accepts is also accepted by the
embedded client for the methods the provider dispatches to. HTTP is
the network contract; embedded must keep parity or pip-install mode
breaks.

Asymmetric on purpose: embedded may have local-only extras (cache
tuning, etc.) that HTTP doesn't expose. The break-in-production
shape is HTTP-has / embedded-missing, so that's what we assert.
"""

from __future__ import annotations

import inspect
import sys
import types
from unittest.mock import MagicMock

import pytest

# Methods the provider's _do_<tool> dispatch calls.
_PROVIDER_DISPATCHED_METHODS: list[str] = [
    "remember",
    "recall",
    "forget",
    "think",
    "conflicts",
    "relate",
    "stats",
]


@pytest.fixture
def _embedded_module_for_parity(plugin, monkeypatch):
    """Local embedded fixture — `test_embedded.py` defines one but it isn't
    in conftest, so we stub the engine the same way here."""
    fake_engine_module = types.ModuleType("yantrikdb")
    fake_rust_module = types.ModuleType("yantrikdb._yantrikdb_rust")
    cls = MagicMock(name="YantrikDB")
    cls.return_value.has_embedder.return_value = True
    cls.with_default.return_value.has_embedder.return_value = True
    fake_rust_module.YantrikDB = cls
    monkeypatch.setitem(sys.modules, "yantrikdb", fake_engine_module)
    monkeypatch.setitem(sys.modules, "yantrikdb._yantrikdb_rust", fake_rust_module)
    return sys.modules[plugin[0].__name__ + ".embedded"]


def _kwargs(klass: type, method_name: str) -> set[str]:
    sig = inspect.signature(getattr(klass, method_name))
    return {name for name in sig.parameters if name != "self"}


@pytest.mark.parametrize("method", _PROVIDER_DISPATCHED_METHODS)
def test_embedded_accepts_every_kwarg_http_accepts(
    client_module, _embedded_module_for_parity, method: str,
) -> None:
    """Every kwarg the HTTP client accepts on a provider-dispatched method
    must also be accepted by the embedded client.

    HTTP is the network contract that all backends must support, so if
    HTTP grows a kwarg (e.g. ``namespace``) and embedded doesn't, the
    provider's call site will TypeError in embedded mode while mocked
    provider tests pass. PR #11 introduced exactly this drift before
    review caught it; this test prevents the next one."""
    http_kwargs = _kwargs(client_module.YantrikDBClient, method)
    emb_kwargs = _kwargs(
        _embedded_module_for_parity.EmbeddedYantrikDBClient, method,
    )
    missing = http_kwargs - emb_kwargs
    assert not missing, (
        f"EmbeddedYantrikDBClient.{method}() is missing kwarg(s) "
        f"{sorted(missing)} that YantrikDBClient.{method}() accepts. "
        f"Provider calls passing these will TypeError in embedded mode "
        f"(the default pip-install backend) while mocked provider tests "
        f"keep passing. Add the kwarg to the embedded signature."
    )
