"""DD-01 (2026-11-21): writes from this cut on carry a provenance claim.

The 22,967 records written before it stay unmarked on purpose — their origin is
not knowable, and inventing one afterwards is the failure mode that left 22,184
of them in `domain=general` with no basis at all.
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def embedded_module(plugin):
    return sys.modules[plugin[0].__name__ + ".embedded"]


def _client(embedded_module):
    db = MagicMock()
    db.record_text.return_value = "rid-1"
    client = embedded_module.EmbeddedYantrikDBClient.__new__(
        embedded_module.EmbeddedYantrikDBClient
    )
    client._db = db
    client.config = MagicMock(max_text_len=25000, namespace="hermes")
    return client, db


def test_an_automatic_write_gets_both_fields(embedded_module):
    client, db = _client(embedded_module)

    client.remember("Die Sitzung lief ohne Unterbrechung.")

    meta = db.record_text.call_args.kwargs["metadata"]
    assert meta["temporal_kind"] == "observation"
    assert meta["confidence_basis"] == "asserted"


def test_an_explicit_basis_wins(embedded_module):
    client, db = _client(embedded_module)

    client.remember(
        "Der Rechunk-Fix ist gemergt.",
        metadata={"temporal_kind": "decision", "confidence_basis": "verification"},
    )

    meta = db.record_text.call_args.kwargs["metadata"]
    assert meta["temporal_kind"] == "decision"
    assert meta["confidence_basis"] == "verification"


def test_a_partial_claim_is_completed_not_overwritten(embedded_module):
    client, db = _client(embedded_module)

    client.remember("Vermutung über den Grund.", metadata={"confidence_basis": "inference"})

    meta = db.record_text.call_args.kwargs["metadata"]
    assert meta["confidence_basis"] == "inference"
    assert meta["temporal_kind"] == "observation"


def test_an_unknown_payload_is_not_reshaped(embedded_module):
    """A caller's own structure is not ours to rewrite."""
    client, db = _client(embedded_module)

    client.remember("Notiz.", metadata="not-a-dict")

    assert db.record_text.call_args.kwargs["metadata"] == "not-a-dict"


def test_a_defined_skill_is_a_rule_not_an_observation(embedded_module):
    """Skills and outcomes write memories too — they were the two paths that
    bypassed remember(), so the cut would have been half-enforced.

    Tested at the helper, not end-to-end: `skill_define` runs a client-side
    schema check whose exact rules would make this test about the validator.
    The call sites pass the helper (reading, not a test) — noted in the PR.
    """
    meta = embedded_module._with_default_provenance(
        {"skill_id": "git.commit"}, temporal_kind="rule",
    )
    assert meta["temporal_kind"] == "rule"
    assert meta["confidence_basis"] == "asserted"


def test_the_http_backend_sets_the_same_claim(client_module):
    """The transport must not decide whether a record carries provenance."""
    import inspect
    src = inspect.getsource(client_module.YantrikDBClient.remember)
    assert 'setdefault("temporal_kind", "observation")' in src
    assert 'setdefault("confidence_basis", "asserted")' in src
