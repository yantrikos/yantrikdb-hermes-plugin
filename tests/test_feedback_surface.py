"""The learning loop needs labels, and only the plugin can hand them over.

The engine has `recall_feedback` and `reject_recalled`; the provider kept its own
JSON sidecar and never called them, so `run_learning()` stayed at
`insufficient_evidence` with 4,847 impressions and zero labels. These tests pin
the two paths that now reach the engine.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch


def _provider(provider_module, client):
    p = provider_module.YantrikDBMemoryProvider()
    client.recall.return_value = {"results": [{"rid": "r1", "text": "t", "score": 0.5}]}
    with patch.object(provider_module, "make_backend", return_value=client):
        p.initialize("s1", agent_workspace="ws", agent_identity="coder", platform="cli")
    return p


def test_reinforce_is_also_a_label_for_the_engine(provider_module):
    client = MagicMock()
    p = _provider(provider_module, client)

    p.handle_tool_call("yantrikdb_recall", {"query": "where did we leave it",
                                            "reinforce": ["r1"]})

    client.recall_feedback.assert_called_once_with(
        "r1", "relevant", query_text="where did we leave it",
    )


def test_reject_tool_reaches_the_engine(provider_module):
    client = MagicMock()
    client.reject_recalled.return_value = {"rejected": ["a", "b"]}
    p = _provider(provider_module, client)

    out = json.loads(p.handle_tool_call("yantrikdb_reject_recalled",
                                        {"rids": ["a", "b"]}))

    client.reject_recalled.assert_called_once_with(["a", "b"])
    assert out["rejected"] == ["a", "b"]


def test_reject_without_rids_is_a_tool_error(provider_module):
    client = MagicMock()
    p = _provider(provider_module, client)

    out = json.loads(p.handle_tool_call("yantrikdb_reject_recalled", {"rids": []}))

    assert "error" in out
    client.reject_recalled.assert_not_called()


def test_a_failing_label_never_breaks_the_recall(provider_module):
    """Feedback is a side effect of a recall, not a precondition."""
    client = MagicMock()
    client.recall_feedback.side_effect = RuntimeError("no feedback route")
    p = _provider(provider_module, client)

    out = json.loads(p.handle_tool_call("yantrikdb_recall", {"query": "q",
                                                             "reinforce": ["r1"]}))

    assert out["results"][0]["rid"] == "r1"
