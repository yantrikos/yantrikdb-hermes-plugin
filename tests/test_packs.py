"""v0.11.0 — attachable expertise (engine packs).

A pack is a sealed, signed knowledge file: mount it to gain its knowledge and
rules, unmount to give them back leaving the host database byte-for-byte as it
was. The engine's own framing drives two design choices tested here:

- `mount` is transient and never writes to the database, so auto-mount uses it
  and shutdown gives the packs back. `install` is the deliberate, durable
  counterpart and is never implicit.
- `pack_context()` is assembled BY THE ENGINE so every consumer injects the
  same block; we pass it through verbatim rather than reformatting it.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_client(client_module) -> MagicMock:
    c = MagicMock(spec=client_module.YantrikDBClient)
    c.health.return_value = {"status": "ok"}
    c.pack_action.return_value = {"pack_id": "wordpress-expert@0.2.0"}
    c.pack_context.return_value = {"context": "PACK RULES: prefer block themes."}
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


class TestPackToolGating:
    def test_hidden_by_default(self, provider_module):
        p = provider_module.YantrikDBMemoryProvider()
        names = {s["name"] for s in p.get_tool_schemas()}
        assert "yantrikdb_packs" not in names

    def test_visible_in_core_profile_when_enabled(
        self, provider_module, monkeypatch,
    ):
        """Enabling packs and then being unable to reach them because the
        profile is `core` would be a trap — the flag is the opt-in."""
        monkeypatch.setenv("YANTRIKDB_PACKS_ENABLED", "true")
        monkeypatch.setenv("YANTRIKDB_TOOL_PROFILE", "core")
        p = provider_module.YantrikDBMemoryProvider()
        names = {s["name"] for s in p.get_tool_schemas()}
        assert "yantrikdb_packs" in names

    def test_call_refused_when_disabled(
        self, provider_module, mock_client, monkeypatch,
    ):
        p = _provider(provider_module, mock_client, monkeypatch)
        out = json.loads(p.handle_tool_call("yantrikdb_packs", {"action": "list"}))
        assert "error" in out
        mock_client.pack_action.assert_not_called()


class TestPackDispatch:
    def test_actions_reach_the_backend(
        self, provider_module, mock_client, monkeypatch,
    ):
        p = _provider(provider_module, mock_client, monkeypatch,
                      YANTRIKDB_PACKS_ENABLED="true")
        p.handle_tool_call("yantrikdb_packs",
                           {"action": "mount", "path": "wp.ydbpack"})
        args, kwargs = mock_client.pack_action.call_args
        assert args[0] == "mount"
        assert kwargs["path"] == "wp.ydbpack"

    def test_defaults_to_list(self, provider_module, mock_client, monkeypatch):
        p = _provider(provider_module, mock_client, monkeypatch,
                      YANTRIKDB_PACKS_ENABLED="true")
        p.handle_tool_call("yantrikdb_packs", {})
        assert mock_client.pack_action.call_args.args[0] == "list"

    def test_mount_invalidates_cached_context(
        self, provider_module, mock_client, monkeypatch,
    ):
        """The injected block is built from the mounted set, so it is stale the
        instant that set changes."""
        p = _provider(provider_module, mock_client, monkeypatch,
                      YANTRIKDB_PACKS_ENABLED="true")
        p._pack_context_cache = "stale"
        p.handle_tool_call("yantrikdb_packs",
                           {"action": "mount", "path": "wp.ydbpack"})
        assert p._pack_context_cache is None


class TestPackContextBlock:
    def test_injected_verbatim(self, provider_module, mock_client, monkeypatch):
        p = _provider(provider_module, mock_client, monkeypatch,
                      YANTRIKDB_PACKS_ENABLED="true")
        block = p.system_prompt_block()
        assert "Mounted knowledge packs" in block
        assert "prefer block themes" in block

    def test_absent_when_packs_disabled(
        self, provider_module, mock_client, monkeypatch,
    ):
        p = _provider(provider_module, mock_client, monkeypatch)
        assert "Mounted knowledge packs" not in p.system_prompt_block()

    def test_absent_when_no_pack_declares_context(
        self, provider_module, mock_client, monkeypatch,
    ):
        mock_client.pack_context.return_value = {"context": None}
        p = _provider(provider_module, mock_client, monkeypatch,
                      YANTRIKDB_PACKS_ENABLED="true")
        assert "Mounted knowledge packs" not in p.system_prompt_block()

    def test_capped(self, provider_module, mock_client, monkeypatch):
        mock_client.pack_context.return_value = {"context": "X" * 20000}
        p = _provider(provider_module, mock_client, monkeypatch,
                      YANTRIKDB_PACKS_ENABLED="true",
                      YANTRIKDB_PACK_CONTEXT_MAX_CHARS="300")
        assert len(p.system_prompt_block()) < 2000

    def test_yields_to_a_tight_context_window(
        self, provider_module, mock_client, monkeypatch,
    ):
        """Attached expertise is worth nothing if it crowds out the
        conversation that needed it."""
        p = _provider(provider_module, mock_client, monkeypatch,
                      YANTRIKDB_PACKS_ENABLED="true")
        roomy = len(p.system_prompt_block())
        p.on_turn_start(9, "msg", remaining_tokens=2000)
        assert len(p.system_prompt_block()) < roomy


class TestAutoMountLifecycle:
    def test_mounts_declared_packs_transiently(
        self, provider_module, mock_client, monkeypatch,
    ):
        """`mount`, never `install`: a session that opens with packs must
        leave nothing behind when it ends."""
        _provider(provider_module, mock_client, monkeypatch,
                  YANTRIKDB_PACKS_ENABLED="true",
                  YANTRIKDB_AUTO_MOUNT_PACKS="a.ydbpack, b.ydbpack")
        actions = [c.args[0] for c in mock_client.pack_action.call_args_list]
        assert actions == ["mount", "mount"]

    def test_one_bad_pack_does_not_break_the_session(
        self, provider_module, mock_client, monkeypatch, client_module,
    ):
        mock_client.pack_action.side_effect = [
            client_module.YantrikDBClientError("pack refused: embedder mismatch"),
            {"pack_id": "good@1.0"},
        ]
        p = _provider(provider_module, mock_client, monkeypatch,
                      YANTRIKDB_PACKS_ENABLED="true",
                      YANTRIKDB_AUTO_MOUNT_PACKS="bad.ydbpack,good.ydbpack")
        assert p._mounted_pack_ids == ["good@1.0"]

    def test_shutdown_gives_the_packs_back(
        self, provider_module, mock_client, monkeypatch,
    ):
        p = _provider(provider_module, mock_client, monkeypatch,
                      YANTRIKDB_PACKS_ENABLED="true",
                      YANTRIKDB_AUTO_MOUNT_PACKS="a.ydbpack")
        mock_client.pack_action.reset_mock()
        p.shutdown()
        unmounts = [c for c in mock_client.pack_action.call_args_list
                    if c.args[0] == "unmount"]
        assert len(unmounts) == 1

    def test_nothing_mounted_when_flag_off(
        self, provider_module, mock_client, monkeypatch,
    ):
        _provider(provider_module, mock_client, monkeypatch,
                  YANTRIKDB_AUTO_MOUNT_PACKS="a.ydbpack")
        mock_client.pack_action.assert_not_called()


class TestRecallReachesPackKnowledge:
    """The half that is easy to ship broken.

    Mounting brings a pack's RULES in via `pack_context()`, but its RECORDS
    land in whatever namespace the author sealed them under. Recall is scoped
    to the agent's own namespace, so without widening, a mount delivers the
    constitution and none of the knowledge — and every surface still looks
    healthy. Caught on a real sealed pack: recall returned 0 hits while the
    pack was mounted and the prompt block was present.
    """

    def test_pack_namespaces_join_the_recall_set(
        self, provider_module, mock_client, monkeypatch,
    ):
        mock_client.pack_namespaces.return_value = ["wp"]
        p = _provider(provider_module, mock_client, monkeypatch,
                      YANTRIKDB_PACKS_ENABLED="true")
        assert "wp" in p._fallback_recall_namespaces()

    def test_not_widened_when_packs_disabled(
        self, provider_module, mock_client, monkeypatch,
    ):
        mock_client.pack_namespaces.return_value = ["wp"]
        p = _provider(provider_module, mock_client, monkeypatch)
        assert "wp" not in p._fallback_recall_namespaces()

    def test_cache_invalidated_on_mount(
        self, provider_module, mock_client, monkeypatch,
    ):
        """A pack mounted mid-session must become recallable immediately."""
        mock_client.pack_namespaces.return_value = []
        p = _provider(provider_module, mock_client, monkeypatch,
                      YANTRIKDB_PACKS_ENABLED="true")
        assert p._fallback_recall_namespaces() == []
        mock_client.pack_namespaces.return_value = ["wp"]
        p.handle_tool_call("yantrikdb_packs", {"action": "mount", "path": "w.ydbpack"})
        assert "wp" in p._fallback_recall_namespaces()

    def test_probe_failure_never_breaks_recall(
        self, provider_module, mock_client, monkeypatch, client_module,
    ):
        mock_client.pack_namespaces.side_effect = client_module.YantrikDBServerError("x")
        p = _provider(provider_module, mock_client, monkeypatch,
                      YANTRIKDB_PACKS_ENABLED="true")
        assert p._fallback_recall_namespaces() == []


class TestHttpModeIsHonest:
    def test_http_refuses_rather_than_returning_empty(self, client_module):
        """Returning an empty list would read as 'no packs are mounted' — a
        different and misleading claim from 'packs are unavailable here'."""
        cfg = client_module.YantrikDBConfig(mode="http", url="http://x", token="t")
        c = client_module.YantrikDBClient(cfg)
        with pytest.raises(client_module.YantrikDBClientError, match="embedded"):
            c.pack_action("list")
        assert c.pack_context()["context"] is None


class TestPackHitsAreMarkedInRecall:
    """A pack's records are recallable alongside the agent's own memories
    (v0.11). Rendered as identical bullets, the agent cannot tell a stranger's
    claim from something the user told it — the same conflation v0.12 fixed at
    the block level, one layer deeper. Engine 0.12.0 returning `namespace` on
    recall hits is what makes the distinction possible.
    """

    def test_pack_sourced_hit_is_labelled(self, provider_module):
        block = provider_module._format_recall_block(
            [{"text": "Prefer block themes.", "score": 0.9, "namespace": "wp"}],
            third_party_namespaces=("wp",),
        )
        assert "third-party" in block.lower()

    def test_own_memory_is_not_labelled(self, provider_module):
        block = provider_module._format_recall_block(
            [{"text": "The user prefers dark mode.", "score": 0.9,
              "namespace": "hermes:ws:coder"}],
            third_party_namespaces=("wp",),
        )
        assert "third-party" not in block.lower()

    def test_no_labelling_without_mounted_packs(self, provider_module):
        """Never spend prompt tokens on a distinction that cannot apply."""
        block = provider_module._format_recall_block(
            [{"text": "x", "score": 0.9, "namespace": "wp"}],
        )
        assert "third-party" not in block.lower()
